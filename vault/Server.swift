// mcomp-vault: `unlock`, the only process that holds the vault's key and its values.
//
// It runs in the foreground in the user's own terminal, serves requests on a Unix
// socket that only this user can reach (0600, peer uid checked), shows every request
// and its outcome there (never a value), asks there when a use needs approval, and
// locks on Ctrl-C, on its timeout, or when the terminal closes.
//
// Everything that comes from a client or the VM is untrusted: names are validated,
// and any text from them is passed through clean() before it's printed, written to
// the audit log or sent back, so it can't move the cursor or repaint a question.
import Darwin
import Foundation

// MARK: - Unix socket helpers

func unixAddress(_ path: String) throws -> sockaddr_un {
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    let cap = MemoryLayout.size(ofValue: addr.sun_path)
    guard bytes.count < cap else {
        throw VaultError("the socket path \(path) is too long for a Unix socket (\(bytes.count) of \(cap - 1) bytes); set MCOMP_VAULT_DIR to a shorter folder")
    }
    withUnsafeMutableBytes(of: &addr.sun_path) { p in
        for (i, b) in bytes.enumerated() { p[i] = b }
        p[bytes.count] = 0
    }
    addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    return addr
}

func setTimeouts(_ fd: Int32, seconds: Int) {
    var tv = timeval(tv_sec: seconds, tv_usec: 0)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    var one: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
}

/// One newline-terminated message (at most max bytes).
func readMessage(_ fd: Int32, max: Int = 1 << 20) -> [UInt8]? {
    var out = [UInt8]()
    var buf = [UInt8](repeating: 0, count: 4096)
    while true {
        let n = read(fd, &buf, buf.count)
        if n < 0 && errno == EINTR { continue }
        if n <= 0 { return out.isEmpty ? nil : out }
        if let nl = buf[0..<n].firstIndex(of: 0x0A) {
            out.append(contentsOf: buf[0..<nl])
            return out
        }
        out.append(contentsOf: buf[0..<n])
        if out.count > max { return nil }
    }
}

func peerUID(_ fd: Int32) -> uid_t? {
    var uid: uid_t = 0, gid: gid_t = 0
    return getpeereid(fd, &uid, &gid) == 0 ? uid : nil
}

func peerPID(_ fd: Int32) -> Int32 {
    var pid: Int32 = 0
    var len = socklen_t(MemoryLayout<Int32>.size)
    return getsockopt(fd, 0 /* SOL_LOCAL */, 0x002 /* LOCAL_PEERPID */, &pid, &len) == 0 ? pid : -1
}

// MARK: - what runs in the VM for sealed sessions (as the desktop account)

func profileRoot(_ user: String) -> String { "/home/\(user)/.local/state/cua-driver/browser-profiles" }

/// Refuse while a Chrome uses the profile. "[-]" keeps pgrep from matching this shell.
func inUseCheck(_ dir: String, _ profile: String) -> String {
    "if pgrep -u \"$(id -u)\" -f -- \"[-]-user-data-dir=\(dir)( |\\$)\" >/dev/null 2>&1; then echo \"Chrome is using profile \(profile); end its browser session first\" >&2; exit 4; fi\n"
}

func sessionSaveScript(user: String, profile: String) -> String {
    let root = profileRoot(user), d = root + "/" + profile
    let excludes = ["Singleton*", "DevToolsActivePort", "*/Cache", "*/Code Cache", "*/GPUCache", "*/DawnCache",
                    "*/DawnGraphiteCache", "*/DawnWebGPUCache", "GrShaderCache", "ShaderCache", "GraphiteDawnCache",
                    "component_crx_cache", "Crashpad"]
    return "set -e\n"
        + "[ -d '\(d)' ] && [ ! -L '\(d)' ] || { echo 'no Chrome profile named \(profile) in the VM' >&2; exit 3; }\n"
        + inUseCheck(d, profile)
        + "cd '\(root)'\n"
        + "exec tar " + excludes.map { "--exclude=" + shellQuote(profile + "/" + $0) }.joined(separator: " ")
        + " -czf - -- '\(profile)'\n"
}

/// Unpacks a sealed session with Python's tarfile "data" filter (Python 3.12+): no
/// absolute paths, no "..", no links leading outside, no devices, no set-id bits, owner
/// and permissions not taken from the archive. Every entry must sit in the profile folder.
let sessionLoader = #"""
# mcomp-session-load: unpack a sealed browser session into the driver's profile folder.
import io, os, shutil, sys, tarfile, tempfile
root, name = sys.argv[1], sys.argv[2]
def stop(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(5)
if not hasattr(tarfile, "data_filter"):
    stop("this VM's Python can't unpack safely (no tarfile data filter); refusing")
data = sys.stdin.buffer.read()
try:
    tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    members = tf.getmembers()
except (tarfile.TarError, OSError, EOFError):
    stop("the sealed session isn't a readable archive")
for m in members:
    parts = m.name.split("/")
    if m.name.startswith("/") or ".." in parts or parts[0] != name:
        stop("the sealed session has an entry outside its profile folder; refusing")
    if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
        stop("the sealed session has a device or other special file; refusing")
d = os.path.join(root, name)
if os.path.islink(root) or os.path.islink(d):
    stop("the profile folder is a symbolic link; refusing")
t = tempfile.mkdtemp(prefix=".mcomp-load.", dir=root)
try:
    try:
        tf.extractall(t, filter="data")
    except (tarfile.TarError, OSError) as e:
        stop("the sealed session can't be unpacked safely (" + type(e).__name__ + "); refusing")
    src = os.path.join(t, name)
    if not os.path.isdir(src) or os.path.islink(src):
        stop("the sealed session holds no profile folder")
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.rename(src, d)
finally:
    shutil.rmtree(t, ignore_errors=True)
"""#

func sessionLoadScript(user: String, profile: String) -> String {
    let root = profileRoot(user), d = root + "/" + profile
    return "set -e\n"
        + "[ ! -L '\(root)' ] && [ ! -L '\(d)' ] || { echo 'the profile folder is a symbolic link; refusing' >&2; exit 5; }\n"
        + inUseCheck(d, profile)
        + "mkdir -p '\(root)'\n"
        + "exec python3 -I -c \(shellQuote(sessionLoader)) '\(root)' '\(profile)'\n"
}

// MARK: - the server

final class UnlockServer {
    let paths: Paths
    let key: VaultKey
    let confirmEach: Bool
    let multiplexer: String?
    let allowMultiplexer: Bool
    let started = Date()
    let expires: Date
    let runner = GuestRunner()
    var promptTimeout = 120

    init(paths: Paths, key: VaultKey, seconds: Int, confirmEach: Bool, multiplexer: String?, allowMultiplexer: Bool) {
        self.paths = paths
        self.key = key
        self.confirmEach = confirmEach
        self.multiplexer = multiplexer
        self.allowMultiplexer = allowMultiplexer
        self.expires = Date().addingTimeInterval(TimeInterval(seconds))
        #if MCOMP_VAULT_TESTING
        if let t = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_PROMPT_TIMEOUT"], let n = Int(t), n > 0 { promptTimeout = n }
        #endif
    }

    /// Lines for the unlock terminal, each made printable.
    func say(_ s: String) {
        for line in s.split(separator: "\n", omittingEmptySubsequences: false) { oprint(clean(String(line), max: 400)) }
    }

    func remaining() -> Int { max(0, Int(expires.timeIntervalSinceNow)) }

    /// Ask at the unlock terminal: the person types `typed` to allow. (true, nil) or (false, why).
    func approve(_ question: String, typed: String) -> (Bool, String) {
        if let m = multiplexer, !allowMultiplexer {
            return (false, "the unlock terminal runs inside \(m), where any program can type into it, so confirmations there are refused: run unlock outside \(m) (or with --allow-multiplexer)")
        }
        let t = min(promptTimeout, remaining())
        guard t > 0 else { return (false, "the vault's time is up") }
        if askTyped(clean(question, max: 400), expected: typed, timeout: t) { return (true, "") }
        return (false, "not allowed at the vault's terminal")
    }

    func serve() throws -> Int32 {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw VaultError("cannot make a socket") }
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        var addr = try unixAddress(paths.socket)
        unlink(paths.socket)   // stale: we hold the unlock lock, so no other unlock owns it
        let oldMask = umask(0o177)
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        umask(oldMask)
        guard rc == 0 else { close(fd); throw VaultError("cannot listen on \(paths.socket): \(String(cString: strerror(errno)))") }
        chmod(paths.socket, 0o600)
        guard listen(fd, 8) == 0 else { close(fd); unlink(paths.socket); throw VaultError("cannot listen on \(paths.socket)") }

        let h: @convention(c) (Int32) -> Void = { sig in receivedSignal = sig }
        signal(SIGINT, h)
        signal(SIGTERM, h)
        signal(SIGHUP, h)

        let mins = Int(expires.timeIntervalSince(started)) / 60
        say("metacomputer secrets: unlocked until \(clockTime(expires))" + (mins > 0 ? " (\(mins) min)" : "")
            + (confirmEach ? "; every use asks here first" : "; secrets and sessions set to 'each' ask here first"))
        say("  Requests and their outcomes appear here (never values). Ctrl-C locks the vault.")
        if let m = multiplexer {
            say("  WARNING: this terminal runs inside \(m). Any program of yours can type into it (for example")
            say("  `tmux send-keys` or `screen -X stuff`), including the answer to a confirmation.")
            say(allowMultiplexer ? "  Confirmations are taken here anyway (--allow-multiplexer)."
                : "  Uses that need a confirmation are refused here. For those, unlock in a terminal outside \(m).")
        }
        #if MCOMP_VAULT_TESTING
        say("  TEST BUILD: honours MCOMP_VAULT_TEST_* settings; never use it for real secrets")
        #endif
        audit(paths, ["op": "unlock", "result": "ok", "until": isoTime(expires), "confirm": confirmEach ? "each" : "session",
                      "multiplexer": multiplexer ?? "none", "pid": Int(getpid())])

        var why = "locked"
        while true {
            if receivedSignal != 0 {
                why = receivedSignal == SIGHUP ? "the terminal closed" : (receivedSignal == SIGINT ? "Ctrl-C" : "signal \(receivedSignal)")
                break
            }
            if Date() >= expires { why = "time is up"; break }
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let r = poll(&pfd, 1, 500)
            if r < 0 && errno != EINTR { why = "socket error"; break }
            if r > 0 && pfd.revents & Int16(POLLIN) != 0 {
                let c = accept(fd, nil, nil)
                if c >= 0 {
                    _ = fcntl(c, F_SETFD, FD_CLOEXEC)
                    handle(c)
                    close(c)
                }
            }
        }
        close(fd)
        unlink(paths.socket)
        key.wipeKey()
        audit(paths, ["op": "lock", "result": "ok", "reason": why])
        if why == "Ctrl-C" { oprint("") }
        say("\(clockTime())  locked (\(why))")
        return Exit.ok
    }

    func handle(_ c: Int32) {
        setTimeouts(c, seconds: 10)
        guard let uid = peerUID(c), uid == getuid() else {
            audit(paths, ["op": "connect", "result": "denied", "reason": "another user"])
            say("\(clockTime())  refused a connection from another user")
            return
        }
        let pid = peerPID(c)
        guard let msg = readMessage(c), let req = parseJSONObject(msg), let op = req["op"] as? String else {
            reply(c, ["ok": false, "status": "error", "reason": "bad request"])
            return
        }
        if Date() >= expires {
            reply(c, ["ok": false, "status": "locked", "reason": "the vault is locked"])
            return
        }
        let who = pid > 0 ? (processPath(pid).map { URL(fileURLWithPath: $0).lastPathComponent + " " } ?? "") + "(pid \(pid))" : "an unknown process"
        var res: [String: Any]
        switch op {
        case "status": res = opStatus()
        case "list": res = opList()
        case "fill": res = opFill(req, who: who, pid: pid)
        case "session_save": res = opSessionSave(req, who: who, pid: pid)
        case "session_load": res = opSessionLoad(req, who: who, pid: pid)
        default: res = ["ok": false, "status": "error", "reason": "unknown request \(clean(op, max: 40))"]
        }
        setTimeouts(c, seconds: 10)
        reply(c, res)
    }

    func reply(_ c: Int32, _ obj: [String: Any]) {
        let safe = (cleanAll(obj) as? [String: Any]) ?? ["ok": false, "status": "error"]
        _ = writeAll(c, Array((jsonLine(safe) + "\n").utf8))
    }

    func load() throws -> Payload { try loadPayload(paths, key) }

    // MARK: status and list

    func opStatus() -> [String: Any] {
        ["ok": true, "status": "ok", "unlocked": true, "since": isoTime(started), "expires": isoTime(expires),
         "expires_in_s": remaining(), "confirm": confirmEach ? "each" : "session", "multiplexer": multiplexer ?? "none",
         "pid": Int(getpid()), "build": Build.kind]
    }

    func opList() -> [String: Any] {
        let p: Payload
        do { p = try load() } catch let e as VaultError { return ["ok": false, "status": "error", "reason": e.message] } catch {
            return ["ok": false, "status": "error", "reason": "cannot read the vault"]
        }
        let secrets: [[String: Any]] = p.secrets.keys.sorted().map { n in
            let s = p.secrets[n]!
            var d: [String: Any] = ["name": n, "kind": s.kind, "origins": s.origins, "approval": s.approval,
                                    "created": s.created, "uses": s.uses,
                                    "fields": s.kind == "password" ? (s.username == nil ? ["password"] : ["password", "username"])
                                        : (s.kind == "totp" ? ["otp"] : ["text"])]
            if let u = s.username { d["username"] = u }
            if let l = s.last_used { d["last_used"] = l }
            return d
        }
        let vms: [[String: Any]] = p.vms.keys.sorted().map { n in
            let b = p.vms[n]!
            var d: [String: Any] = ["name": n, "ssh_port": b.ssh_port, "host_key": hostKeyFingerprint(b.host_key_line),
                                    "key": b.ops_key_path, "user": b.guest_user, "bound": b.bound]
            if let f = b.fill_user { d["fill_user"] = f }
            if let k = b.fill_key_path { d["fill_key"] = k }
            if let g = b.gateway_path { d["gateway"] = g }
            if b.fill_user == nil || b.gateway_path == nil { d["needs_bind"] = true }
            return d
        }
        let sessions: [[String: Any]] = p.sessions.keys.sorted().map { n in
            let m = p.sessions[n]!
            var d: [String: Any] = ["name": n, "vm": m.vm, "profile": m.profile, "saved_at": m.saved_at, "bytes": m.bytes,
                                    "loads": m.loads, "approval": m.approval ?? "each"]
            if let l = m.last_loaded { d["last_loaded"] = l }
            return d
        }
        return ["ok": true, "status": "ok", "secrets": secrets, "vms": vms, "sessions": sessions]
    }

    // MARK: results

    func finish(_ op: String, _ fields: [String: Any], _ res: [String: Any], pid: Int32, approval: String) -> [String: Any] {
        var out = fields
        for k in ["ok", "status", "origin", "chars", "reason", "bytes"] { if let v = res[k] { out[k] = v } }
        out = (cleanAll(out) as? [String: Any]) ?? fields
        let status = out["status"] as? String ?? "error"
        var rec = fields
        rec["op"] = op
        rec["result"] = status
        rec["approval"] = approval
        rec["client_pid"] = Int(pid)
        for k in ["origin", "chars", "reason", "bytes"] { if let v = out[k] { rec[k] = v } }
        audit(paths, rec)
        var line = "          -> \(status)"
        if let o = out["origin"] as? String { line += ": \(o)" }
        if let n = out["chars"] as? Int { line += ", \(n) characters" }
        if let b = out["bytes"] as? Int { line += ", \(b) bytes" }
        if let r = out["reason"] as? String { line += " (\(r))" }
        say(line)
        return out
    }

    func denied(_ reason: String) -> [String: Any] { ["ok": false, "status": "denied", "reason": reason] }
    func failed(_ reason: String) -> [String: Any] { ["ok": false, "status": "error", "reason": reason] }
    func lockedResult(_ reason: String) -> [String: Any] { ["ok": false, "status": "locked", "reason": reason] }

    func target(_ p: Payload, _ vm: String) -> GuestTarget? {
        p.vms[vm].map { GuestTarget(vm: vm, binding: $0) }
    }

    /// The fields of a request as they may be echoed: printable and short, whatever was sent.
    func echo(_ req: [String: Any], _ keys: [String]) -> [String: Any] {
        var d: [String: Any] = [:]
        for k in keys { if let v = req[k] as? String { d[k] = clean(v, max: 64) } }
        return d
    }

    /// A site the VM reported must be one of the secret's own, written the same way.
    func originOK(_ res: [String: Any], _ allowed: [String]) -> Bool {
        guard let o = res["origin"] as? String else { return false }
        return canonicalOrigin(o) == o && allowed.contains(o)
    }

    // MARK: fill

    func opFill(_ req: [String: Any], who: String, pid: Int32) -> [String: Any] {
        var fields = echo(req, ["name", "vm", "field", "profile"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, let field = req["field"] as? String,
              validName(name), validName(vm), ["password", "username", "otp", "text"].contains(field),
              (req["profile"] as? String).map(validName) ?? (req["profile"] == nil),
              (req["selector"] as? String)?.count ?? 0 <= 300 else {
            return finish("fill", fields, failed("bad request"), pid: pid, approval: "-")
        }
        let profile = req["profile"] as? String
        let selector = (req["selector"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        if let sel = selector { fields["selector"] = clean(sel, max: 300) }
        say("\(clockTime())  fill \(name) (\(field)) on VM \(vm)" + (profile.map { ", profile \($0)" } ?? "")
            + (selector.map { ", selector '\(promptText($0, max: 80))'" } ?? "") + ", asked by \(who)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("fill", fields, failed(e.message), pid: pid, approval: "-") } catch {
            return finish("fill", fields, failed("cannot read the vault"), pid: pid, approval: "-")
        }
        guard let s = p.secrets[name] else {
            return finish("fill", fields, failed("no secret named \(name) (metacomputer secrets list)"), pid: pid, approval: "-")
        }
        let allowedFields = s.kind == "password" ? ["password", "username"] : (s.kind == "totp" ? ["otp"] : ["text"])
        guard allowedFields.contains(field) else {
            return finish("fill", fields, denied("\(name) is a \(s.kind) secret; it fills \(allowedFields.joined(separator: " or ")), not \(field)"), pid: pid, approval: "-")
        }
        if field == "username" && (s.username ?? "").isEmpty {
            return finish("fill", fields, denied("\(name) has no username stored"), pid: pid, approval: "-")
        }
        guard let t = target(p, vm) else {
            return finish("fill", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), pid: pid, approval: "-")
        }
        if let problem = runner.listenerProblem(t) {
            return finish("fill", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), pid: pid, approval: "-")
        }
        var base: [String: Any] = ["field": field]
        if let pr = profile { base["profile"] = pr }
        if let sel = selector { base["selector"] = sel }
        var origins = s.origins

        var approval = "session"
        if confirmEach || s.approval == "each" {
            var chk = base
            chk["mode"] = "check"
            chk["origins"] = origins
            let r: [String: Any]
            do { r = try runner.fill(t, request: chk, value: nil, timeout: TimeInterval(min(60, remaining()))) } catch let e as VaultError {
                return finish("fill", fields, failed(e.message), pid: pid, approval: "each")
            } catch { return finish("fill", fields, failed("the check failed"), pid: pid, approval: "each") }
            guard r["ok"] as? Bool == true else { return finish("fill", fields, r, pid: pid, approval: "each") }
            guard originOK(r, s.origins), let origin = r["origin"] as? String else {
                return finish("fill", fields, failed("the VM reported a site outside this secret's sites; refusing"), pid: pid, approval: "each")
            }
            let (yes, why) = approve("  Fill \(name) (\(field)) into \(origin) on VM \(vm)"
                                     + (profile.map { ", Chrome profile \($0)" } ?? "")
                                     + (selector.map { ", selector '\(promptText($0, max: 80))'" } ?? "") + "?", typed: name)
            approval = yes ? "each:yes" : "each:no"
            if !yes { return finish("fill", fields, denied(why), pid: pid, approval: approval) }
            origins = [origin]   // only the site that was approved
            if let problem = runner.listenerProblem(t) {
                return finish("fill", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), pid: pid, approval: approval)
            }
        }

        var value: [UInt8]
        switch field {
        case "username": value = Array((s.username ?? "").utf8)
        case "otp":
            guard let seed = base32Decode(s.value), !seed.isEmpty else {
                return finish("fill", fields, failed("\(name)'s seed is damaged"), pid: pid, approval: approval)
            }
            // A code with under 3 s left could expire on its way; wait for the next one.
            let left = 30 - UInt64(Date().timeIntervalSince1970) % 30
            if left < 3 && remaining() > Int(left) + 1 { Thread.sleep(forTimeInterval: Double(left) + 0.2) }
            value = Array(totp(secret: seed, time: UInt64(Date().timeIntervalSince1970)).utf8)
        default: value = Array(s.value.utf8)
        }
        defer { wipe(&value) }
        guard Date() < expires, receivedSignal == 0 else {
            return finish("fill", fields, lockedResult("the vault locked before the value was sent"), pid: pid, approval: approval)
        }
        var req2 = base
        req2["mode"] = "fill"
        req2["origins"] = origins
        let r: [String: Any]
        do { r = try runner.fill(t, request: req2, value: value, timeout: TimeInterval(min(60, remaining()))) } catch let e as VaultError {
            return finish("fill", fields, failed(e.message), pid: pid, approval: approval)
        } catch {
            return finish("fill", fields, failed("the fill failed"), pid: pid, approval: approval)
        }
        if r["ok"] as? Bool == true {
            guard originOK(r, origins) else {
                return finish("fill", fields, failed("the VM reported a site outside this secret's sites"), pid: pid, approval: approval)
            }
            do {
                try updateVault(paths, key) { p in
                    p.secrets[name]?.uses += 1
                    p.secrets[name]?.last_used = isoTime()
                }
            } catch { say("          (couldn't record the use in the vault)") }
        }
        return finish("fill", fields, r, pid: pid, approval: approval)
    }

    // MARK: sealed browser sessions (the desktop account)

    func sessionAAD(_ name: String) -> [UInt8] { Array("mcomp-session|1|\(name)".utf8) }

    func sessionKey(_ p: Payload) -> [UInt8]? {
        guard let d = Data(base64Encoded: p.session_key), d.count == 32 else { return nil }
        return Array(d)
    }

    func opSessionSave(_ req: [String: Any], who: String, pid: Int32) -> [String: Any] {
        let fields = echo(req, ["name", "vm", "profile"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, let profile = req["profile"] as? String,
              validName(name), validName(vm), validName(profile) else {
            return finish("session_save", fields, failed("bad request"), pid: pid, approval: "-")
        }
        let replace = req["replace"] as? Bool == true
        say("\(clockTime())  session save \(name) from profile \(profile) on VM \(vm)" + (replace ? " (replace)" : "") + ", asked by \(who)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("session_save", fields, failed(e.message), pid: pid, approval: "-") } catch {
            return finish("session_save", fields, failed("cannot read the vault"), pid: pid, approval: "-")
        }
        let old = p.sessions[name]
        if old != nil && !replace {
            return finish("session_save", fields, denied("a session named \(name) exists (--replace overwrites it, after you confirm)"), pid: pid, approval: "-")
        }
        guard let t = target(p, vm) else {
            return finish("session_save", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), pid: pid, approval: "-")
        }
        guard var skey = sessionKey(p) else { return finish("session_save", fields, failed("the vault's session key is damaged"), pid: pid, approval: "-") }
        defer { wipe(&skey) }
        if let problem = runner.listenerProblem(t) {
            return finish("session_save", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), pid: pid, approval: "-")
        }
        // Saving always asks: the agent starts it, and a saved login is a credential.
        let question = old.map { "  Replace the sealed session \(name) (saved \($0.saved_at) from VM \($0.vm), profile \($0.profile)) with profile \(profile) from VM \(vm)?" }
            ?? "  Save Chrome profile \(profile) from VM \(vm) as the sealed session \(name)?"
        let (yes, why) = approve(question, typed: name)
        let approval = yes ? "each:yes" : "each:no"
        if !yes { return finish("session_save", fields, denied(why), pid: pid, approval: approval) }
        if let problem = runner.listenerProblem(t) {
            return finish("session_save", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), pid: pid, approval: approval)
        }
        let r: ProcResult
        do { r = try runner.opsSSH(t, remote: sessionSaveScript(user: t.binding.guest_user, profile: profile), input: [],
                                   timeout: TimeInterval(min(600, remaining())), maxStdout: 1 << 31) }
        catch { return finish("session_save", fields, failed("cannot reach the VM"), pid: pid, approval: approval) }
        var archive = r.stdout
        defer { wipe(&archive) }
        if r.interrupted || r.timedOut || r.tooLarge || r.status != 0 || archive.count < 2 || archive[0] != 0x1F || archive[1] != 0x8B {
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish in time" : r.tooLarge ? "the profile is too large"
                : (r.stderr.isEmpty ? "exit \(r.status)" : oneLine(String(decoding: r.stderr, as: UTF8.self), max: 200))
            return finish("session_save", fields, r.status == 4 ? denied(why) : failed(why), pid: pid, approval: approval)
        }
        do {
            try ensurePrivateDir(paths.sessions)
            let sealed = try sealGCM(key: skey, plaintext: archive, aad: sessionAAD(name))
            try writeFileAtomic(paths.sessions + "/\(name).sealed", sealed)
            let bytes = archive.count
            try updateVault(paths, key) { p in
                p.sessions[name] = SessionMeta(vm: vm, profile: profile, saved_at: isoTime(), bytes: bytes, last_loaded: nil, loads: 0,
                                               approval: old?.approval)
            }
            return finish("session_save", fields, ["ok": true, "status": "ok", "bytes": bytes], pid: pid, approval: approval)
        } catch let e as VaultError {
            return finish("session_save", fields, failed(e.message), pid: pid, approval: approval)
        } catch {
            return finish("session_save", fields, failed("cannot seal the session"), pid: pid, approval: approval)
        }
    }

    func opSessionLoad(_ req: [String: Any], who: String, pid: Int32) -> [String: Any] {
        var fields = echo(req, ["name", "vm", "profile"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, validName(name), validName(vm),
              (req["profile"] as? String).map(validName) ?? (req["profile"] == nil) else {
            return finish("session_load", fields, failed("bad request"), pid: pid, approval: "-")
        }
        say("\(clockTime())  session load \(name) on VM \(vm), asked by \(who)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("session_load", fields, failed(e.message), pid: pid, approval: "-") } catch {
            return finish("session_load", fields, failed("cannot read the vault"), pid: pid, approval: "-")
        }
        guard let meta = p.sessions[name] else {
            return finish("session_load", fields, failed("no saved session named \(name) (metacomputer secrets list)"), pid: pid, approval: "-")
        }
        let profile = req["profile"] as? String ?? meta.profile
        fields["profile"] = profile
        // Don't forge or rewrite the driver's own records.
        guard profile == meta.profile else {
            return finish("session_load", fields, denied("\(name) was saved from profile \(meta.profile). The driver records a profile's name inside it (.cua-driver-owned-profile.json), so the session works only under that name: leave out --profile, or use --profile \(meta.profile)"), pid: pid, approval: "-")
        }
        guard let t = target(p, vm) else {
            return finish("session_load", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), pid: pid, approval: "-")
        }
        guard var skey = sessionKey(p) else { return finish("session_load", fields, failed("the vault's session key is damaged"), pid: pid, approval: "-") }
        defer { wipe(&skey) }
        if let problem = runner.listenerProblem(t) {
            return finish("session_load", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), pid: pid, approval: "-")
        }
        var approval = "session"
        if confirmEach || meta.approval != "session" || meta.vm != vm {
            let question = meta.vm != vm
                ? "  Load the sealed session \(name), saved from VM \(meta.vm), into a different VM, \(vm) (profile \(profile); replaces it)?"
                : "  Load the sealed session \(name) into profile \(profile) on VM \(vm) (replaces that profile)?"
            let (yes, why) = approve(question, typed: name)
            approval = yes ? "each:yes" : "each:no"
            if !yes { return finish("session_load", fields, denied(why), pid: pid, approval: approval) }
            if let problem = runner.listenerProblem(t) {
                return finish("session_load", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), pid: pid, approval: approval)
            }
        }
        var archive: [UInt8]
        do {
            let sealed = try readFileBytes(paths.sessions + "/\(name).sealed")
            archive = try openGCM(key: skey, combined: sealed, aad: sessionAAD(name))
        } catch {
            return finish("session_load", fields, failed("the sealed session file is missing or was changed"), pid: pid, approval: approval)
        }
        defer { wipe(&archive) }
        guard Date() < expires, receivedSignal == 0 else {
            return finish("session_load", fields, lockedResult("the vault locked before the session was sent"), pid: pid, approval: approval)
        }
        let r: ProcResult
        do { r = try runner.opsSSH(t, remote: sessionLoadScript(user: t.binding.guest_user, profile: profile), input: archive,
                                   timeout: TimeInterval(min(600, remaining()))) }
        catch { return finish("session_load", fields, failed("cannot reach the VM"), pid: pid, approval: approval) }
        if r.interrupted || r.timedOut || r.status != 0 {
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish in time"
                : (r.stderr.isEmpty ? "exit \(r.status)" : oneLine(String(decoding: r.stderr, as: UTF8.self), max: 200))
            return finish("session_load", fields, (r.status == 4 || r.status == 5) ? denied(why) : failed(why), pid: pid, approval: approval)
        }
        do {
            try updateVault(paths, key) { p in
                p.sessions[name]?.loads += 1
                p.sessions[name]?.last_loaded = isoTime()
            }
        } catch { say("          (couldn't record the use in the vault)") }
        return finish("session_load", fields, ["ok": true, "status": "ok", "bytes": archive.count], pid: pid, approval: approval)
    }
}
