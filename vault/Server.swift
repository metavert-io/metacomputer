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

/// A standing approval: for one kind of use of one secret or session, until the vault locks
/// (claudePID nil) or until that Claude Code process (pid + start time) is gone.
struct Grant {
    let kind: String
    let name: String
    let claudePID: pid_t?
    let claudeStart: Double?
}

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
    var grants: [Grant] = []
    var stopReason: String?
    lazy var hub = ProxyHub(paths: paths, key: key, sshPath: runner.sshPath)
    // audit sinks: when each may be tried next, the last problem shown, the log's size last time
    var sinkNext: [String: Date] = [:]
    var sinkShown: [String: String] = [:]
    var auditSize: off_t = -1

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

    // MARK: approvals

    func granted(_ kind: String, _ name: String, _ who: Requester) -> String? {
        grants.removeAll { g in g.claudePID != nil && !(Requester(clientPID: 0, clientExe: "", claudePID: g.claudePID, claudeStart: g.claudeStart, project: nil).claudeAlive) }
        for g in grants where g.kind == kind && g.name == name {
            if g.claudePID == nil { return "grant:unlock" }
            if g.claudePID == who.claudePID, let a = g.claudeStart, let b = who.claudeStart, abs(a - b) < 0.001 { return "grant:session" }
        }
        return nil
    }

    /// Ask at the unlock terminal. The person types NAME (this use), NAME session (this
    /// Claude Code session) or NAME unlock (until the vault locks). Returns (allowed, the
    /// approval to record, why not).
    func approve(_ kind: String, _ name: String, _ question: String, _ who: Requester, vm: String, site: String?) -> (Bool, String, String) {
        if let g = granted(kind, name, who) { return (true, g, "") }
        if let m = multiplexer, !allowMultiplexer {
            return (false, "each:no", "the unlock terminal runs inside \(m), where any program can type into it, so confirmations there are refused: run unlock outside \(m) (or with --allow-multiplexer)")
        }
        let t = min(promptTimeout, remaining())
        guard t > 0 else { return (false, "each:no", "the vault's time is up") }
        dropEvent(type: "approval_waiting", severity: "warn", vm: vm,
                  message: "Vault approval waiting: \(kind.replacingOccurrences(of: "_", with: " ")) of \(name)" + (site.map { " into \($0)" } ?? "")
                    + " on VM \(vm), asked by \(who.describe)",
                  action: "Type \(name) in the vault's terminal to allow", project: who.project,
                  data: ["op": kind, "name": name, "site": site ?? ""])
        guard let scope = askApproval(clean(question, max: 500), name: name, timeout: t) else {
            return (false, "each:no", "not allowed at the vault's terminal")
        }
        switch scope {
        case .unlock:
            grants.append(Grant(kind: kind, name: name, claudePID: nil, claudeStart: nil))
            return (true, "each:unlock", "")
        case .session:
            if let p = who.claudePID {
                grants.append(Grant(kind: kind, name: name, claudePID: p, claudeStart: who.claudeStart))
                return (true, "each:session", "")
            }
            say("  (not from a Claude Code session: allowed for this use only)")
            return (true, "each:yes", "")
        case .use:
            return (true, "each:yes", "")
        }
    }

    /// The events hook (needs_you): one JSON file per event in the shared spool, if it exists.
    func dropEvent(type: String, severity: String, vm: String, message: String, action: String, project: String?, data: [String: Any]) {
        dropSpoolEvent(family: "needs_you", type: type, severity: severity, vm: vm, message: message, action: action, project: project, data: data)
    }

    // MARK: audit sinks

    /// Try to deliver what's pending. force: ignore the backoff. Returns the sinks still
    /// behind, with why, and whether any of those is required.
    @discardableResult
    func flushSinks(force: Bool, onlyRequired: Bool = false) -> [(String, String, Bool)] {
        guard let p = try? load(), let sinks = p.audit_sinks, !sinks.isEmpty else { return [] }
        var behind: [(String, String, Bool)] = []
        for name in sinks.keys.sorted() {
            let sink = sinks[name]!
            if onlyRequired && !sink.required { continue }
            if sinkBacklog(paths, name, sink) == 0 { sinkShown[name] = nil; continue }
            if !force, let next = sinkNext[name], next > Date() {
                behind.append((name, loadSinkState(paths, name, sink).error ?? "retrying", sink.required))
                continue
            }
            if let problem = deliverSink(paths, name, sink) {
                let failures = loadSinkState(paths, name, sink).failures
                sinkNext[name] = Date().addingTimeInterval(min(60, pow(2, Double(min(failures, 6)))))
                if sinkShown[name] != problem {
                    say("\(clockTime())  audit sink \(name): can't deliver (\(problem)); retrying" + (sink.required ? "; uses are refused until it can" : ""))
                    sinkShown[name] = problem
                }
                behind.append((name, problem, sink.required))
            } else {
                if sinkShown[name] != nil { say("\(clockTime())  audit sink \(name): delivering again") }
                sinkShown[name] = nil
                sinkNext[name] = nil
            }
        }
        return behind
    }

    /// nil when every required sink has the record of this use before it happens; else the
    /// refusal. With a required sink, each use is first recorded as op "begin" (what, which
    /// secret, VM, requester) and that record must reach every required sink; the outcome
    /// follows as its own event. Without one, nothing extra is written.
    func requiredSinkProblem(_ op: String, _ fields: [String: Any], _ who: Requester) -> String? {
        guard let p = try? load(), p.audit_sinks?.values.contains(where: { $0.required }) == true else { return nil }
        var rec = fields
        rec["op"] = "begin"
        rec["use"] = op
        rec["result"] = "pending"
        rec["requester"] = who.record
        audit(paths, rec)
        guard let (name, why, _) = flushSinks(force: true, onlyRequired: true).first(where: { $0.2 }) else { return nil }
        return "the required audit sink \(name) can't be reached (\(why)); uses are refused until it can"
    }

    /// Only when the audit log grew, or a sink is behind and its retry time has come.
    func sinkTick() {
        var st = stat()
        let size = stat(paths.audit, &st) == 0 ? st.st_size : 0
        if size != auditSize || sinkNext.values.contains(where: { $0 <= Date() }) {
            auditSize = size
            flushSinks(force: false)
        }
    }

    // MARK: serving

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
                      "multiplexer": multiplexer ?? "none", "pid": Int(getpid()), "enclave": key.usesEnclave])
        flushSinks(force: true)

        var why = "locked"
        while true {
            if receivedSignal != 0 {
                why = receivedSignal == SIGHUP ? "the terminal closed" : (receivedSignal == SIGINT ? "Ctrl-C" : "signal \(receivedSignal)")
                break
            }
            if let s = stopReason { why = s; break }
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
            sinkTick()
        }
        close(fd)
        unlink(paths.socket)
        hub.shutdown()
        audit(paths, ["op": "lock", "result": "ok", "reason": why])
        flushSinks(force: true)
        key.wipeKey()
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
        if Date() >= expires || stopReason != nil {
            reply(c, ["ok": false, "status": "locked", "reason": "the vault is locked"])
            return
        }
        let who = Requester.of(pid)
        var res: [String: Any]
        switch op {
        case "status": res = opStatus()
        case "list": res = opList()
        case "fill": res = opFill(req, who)
        case "session_save": res = opSessionSave(req, who)
        case "session_load": res = opSessionLoad(req, who)
        case "token": res = opToken(req, who)
        case "tokens": res = opTokens()
        case "revoke": res = opRevoke(req, who)
        case "sinks": res = opSinks()
        case "sink_flush": res = opSinkFlush(req)
        default: res = ["ok": false, "status": "error", "reason": "unknown request \(clean(op, max: 40))"]
        }
        setTimeouts(c, seconds: 10)
        reply(c, res)
    }

    func reply(_ c: Int32, _ obj: [String: Any]) {
        let safe = (cleanAll(obj) as? [String: Any]) ?? ["ok": false, "status": "error"]
        _ = writeAll(c, Array((jsonLine(safe) + "\n").utf8))
    }

    /// The vault, re-read for every request. If its key settings changed (rekey, recover),
    /// this unlock can't read it any more: it locks.
    func load() throws -> Payload {
        do { return try loadPayload(paths, key) } catch let e as VaultError {
            if e.code == Exit.locked && stopReason == nil {
                stopReason = "the vault's key changed (rekey or recover)"
                say("\(clockTime())  the vault's key changed (rekey or recover): locking; unlock again")
            }
            throw e
        }
    }

    // MARK: status and list

    func opStatus() -> [String: Any] {
        ["ok": true, "status": "ok", "unlocked": true, "since": isoTime(started), "expires": isoTime(expires),
         "expires_in_s": remaining(), "confirm": confirmEach ? "each" : "session", "multiplexer": multiplexer ?? "none",
         "pid": Int(getpid()), "build": Build.kind, "enclave": key.usesEnclave, "tokens": hub.live().count]
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
                                        : (s.kind == "totp" ? ["otp"] : (s.kind == "api" ? ["token"] : ["text"]))]
            if let u = s.username { d["username"] = u }
            if let l = s.last_used { d["last_used"] = l }
            if let r = s.rotated { d["rotated"] = r }
            if let pr = s.projects, !pr.isEmpty { d["projects"] = pr }
            if let h = s.api_host { d["api_host"] = h }
            if let pp = s.api_path_prefix { d["api_path_prefix"] = pp }
            if let t = s.api_header, let (name, _, _) = splitTemplate(t) { d["api_header"] = name }
            return d
        }
        let vms: [[String: Any]] = p.vms.keys.sorted().map { n in
            let b = p.vms[n]!
            var d: [String: Any] = ["name": n, "ssh_port": b.ssh_port, "host_key": hostKeyFingerprint(b.host_key_line),
                                    "key": b.ops_key_path, "user": b.guest_user, "bound": b.bound]
            if let f = b.fill_user { d["fill_user"] = f }
            if let k = b.fill_key_path { d["fill_key"] = k }
            if let u = b.proxy_user { d["proxy_user"] = u }
            if let pp = b.proxy_port { d["proxy_port"] = pp }
            if let g = b.gateway_path { d["gateway"] = g }
            if b.fill_user == nil || b.gateway_path == nil || b.gateway_sha256 == nil { d["needs_bind"] = true }
            return d
        }
        let sessions: [[String: Any]] = p.sessions.keys.sorted().map { n in
            let m = p.sessions[n]!
            var d: [String: Any] = ["name": n, "vm": m.vm, "profile": m.profile, "saved_at": m.saved_at, "bytes": m.bytes,
                                    "loads": m.loads, "approval": m.approval ?? "each"]
            if let l = m.last_loaded { d["last_loaded"] = l }
            return d
        }
        var out: [String: Any] = ["ok": true, "status": "ok", "secrets": secrets, "vms": vms, "sessions": sessions]
        if let r = p.session_key_rotated { out["session_key_rotated"] = r }
        return out
    }

    // MARK: results

    func finish(_ op: String, _ fields: [String: Any], _ res: [String: Any], _ who: Requester, approval: String) -> [String: Any] {
        var out = fields
        for k in ["ok", "status", "origin", "chars", "reason", "bytes"] { if let v = res[k] { out[k] = v } }
        // How the VM typed it: "element" (into the checked element) or "keyboard" (the fallback).
        if let r = res["route"] as? String, ["element", "keyboard"].contains(r) { out["route"] = r }
        out = (cleanAll(out) as? [String: Any]) ?? fields
        let status = out["status"] as? String ?? "error"
        var rec = fields
        rec["op"] = op
        rec["result"] = status
        rec["approval"] = approval
        rec["requester"] = who.record
        for k in ["origin", "chars", "reason", "bytes", "route", "token_id", "ttl_s"] { if let v = out[k] ?? res[k] { rec[k] = v } }
        audit(paths, rec)
        var line = "          -> \(status)"
        if let o = out["origin"] as? String { line += ": \(o)" }
        if let n = out["chars"] as? Int { line += ", \(n) characters" }
        if let b = out["bytes"] as? Int { line += ", \(b) bytes" }
        if let r = out["reason"] as? String { line += " (\(r))" }
        say(line)
        flushSinks(force: true)
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

    /// nil when the secret may be used for this requester's project.
    func projectProblem(_ name: String, _ s: Secret, _ who: Requester) -> String? {
        guard let projects = s.projects, !projects.isEmpty else { return nil }
        if let p = who.project, insideAny(p, projects) { return nil }
        return "\(name) is limited to Claude Code sessions in \(projects.map(tildePath).joined(separator: ", ")); this request came from \(who.describe)"
    }

    // MARK: fill

    func opFill(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["name", "vm", "field", "profile"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, let field = req["field"] as? String,
              validName(name), validName(vm), ["password", "username", "otp", "text"].contains(field),
              (req["profile"] as? String).map(validName) ?? (req["profile"] == nil),
              (req["selector"] as? String)?.count ?? 0 <= 300 else {
            return finish("fill", fields, failed("bad request"), who, approval: "-")
        }
        let profile = req["profile"] as? String
        let selector = (req["selector"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        if let sel = selector { fields["selector"] = clean(sel, max: 300) }
        say("\(clockTime())  fill \(name) (\(field)) on VM \(vm)" + (profile.map { ", profile \($0)" } ?? "")
            + (selector.map { ", selector '\(promptText($0, max: 80))'" } ?? "") + ", asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("fill", fields, e.code == Exit.locked ? lockedResult(e.message) : failed(e.message), who, approval: "-") } catch {
            return finish("fill", fields, failed("cannot read the vault"), who, approval: "-")
        }
        guard let s = p.secrets[name] else {
            return finish("fill", fields, failed("no secret named \(name) (metacomputer secrets list)"), who, approval: "-")
        }
        if s.kind == "api" {
            return finish("fill", fields, denied("\(name) is an api secret: use `metacomputer secrets token \(name)` and the proxy in the VM"), who, approval: "-")
        }
        let allowedFields = s.kind == "password" ? ["password", "username"] : (s.kind == "totp" ? ["otp"] : ["text"])
        guard allowedFields.contains(field) else {
            return finish("fill", fields, denied("\(name) is a \(s.kind) secret; it fills \(allowedFields.joined(separator: " or ")), not \(field)"), who, approval: "-")
        }
        if field == "username" && (s.username ?? "").isEmpty {
            return finish("fill", fields, denied("\(name) has no username stored"), who, approval: "-")
        }
        if let why = projectProblem(name, s, who) { return finish("fill", fields, denied(why), who, approval: "-") }
        guard let t = target(p, vm) else {
            return finish("fill", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-")
        }
        if let why = requiredSinkProblem("fill", fields, who) { return finish("fill", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(t) {
            return finish("fill", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }
        var base: [String: Any] = ["field": field]
        if let pr = profile { base["profile"] = pr }
        if let sel = selector { base["selector"] = sel }
        var origins = s.origins

        var approval = "session"
        if confirmEach || s.approval == "each" {
            if let g = granted("fill", name, who) {
                approval = g
            } else {
                var chk = base
                chk["mode"] = "check"
                chk["origins"] = origins
                let r: [String: Any]
                do { r = try runner.fill(t, request: chk, value: nil, timeout: TimeInterval(min(60, remaining()))) } catch let e as VaultError {
                    return finish("fill", fields, failed(e.message), who, approval: "each")
                } catch { return finish("fill", fields, failed("the check failed"), who, approval: "each") }
                guard r["ok"] as? Bool == true else { return finish("fill", fields, r, who, approval: "each") }
                guard originOK(r, s.origins), let origin = r["origin"] as? String else {
                    return finish("fill", fields, failed("the VM reported a site outside this secret's sites; refusing"), who, approval: "each")
                }
                let (yes, how, why) = approve("fill", name, "  Fill \(name) (\(field)) into \(origin) on VM \(vm)"
                                              + (profile.map { ", Chrome profile \($0)" } ?? "")
                                              + (selector.map { ", selector '\(promptText($0, max: 80))'" } ?? "")
                                              + "?\n  Asked by \(who.describe).", who, vm: vm, site: origin)
                approval = how
                if !yes { return finish("fill", fields, denied(why), who, approval: approval) }
                if how == "each:yes" { origins = [origin] }   // only the site that was approved, this time
                if let problem = runner.listenerProblem(t) {
                    return finish("fill", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: approval)
                }
            }
        }

        var value: [UInt8]
        switch field {
        case "username": value = Array((s.username ?? "").utf8)
        case "otp":
            guard let seed = base32Decode(s.value), !seed.isEmpty else {
                return finish("fill", fields, failed("\(name)'s seed is damaged"), who, approval: approval)
            }
            // A code with under 3 s left could expire on its way; wait for the next one.
            let left = 30 - UInt64(Date().timeIntervalSince1970) % 30
            if left < 3 && remaining() > Int(left) + 1 { Thread.sleep(forTimeInterval: Double(left) + 0.2) }
            value = Array(totp(secret: seed, time: UInt64(Date().timeIntervalSince1970)).utf8)
        default: value = Array(s.value.utf8)
        }
        defer { wipe(&value) }
        guard Date() < expires, receivedSignal == 0 else {
            return finish("fill", fields, lockedResult("the vault locked before the value was sent"), who, approval: approval)
        }
        var req2 = base
        req2["mode"] = "fill"
        req2["origins"] = origins
        let r: [String: Any]
        do { r = try runner.fill(t, request: req2, value: value, timeout: TimeInterval(min(60, remaining()))) } catch let e as VaultError {
            return finish("fill", fields, failed(e.message), who, approval: approval)
        } catch {
            return finish("fill", fields, failed("the fill failed"), who, approval: approval)
        }
        if r["ok"] as? Bool == true {
            guard originOK(r, origins) else {
                return finish("fill", fields, failed("the VM reported a site outside this secret's sites"), who, approval: approval)
            }
            do {
                try updateVault(paths, key) { p in
                    p.secrets[name]?.uses += 1
                    p.secrets[name]?.last_used = isoTime()
                }
            } catch { say("          (couldn't record the use in the vault)") }
        }
        return finish("fill", fields, r, who, approval: approval)
    }

    // MARK: api tokens

    func opToken(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        let fields = echo(req, ["name", "vm"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, validName(name), validName(vm) else {
            return finish("token", fields, failed("bad request"), who, approval: "-")
        }
        let ttlAsked = (req["ttl_s"] as? NSNumber)?.intValue ?? 900
        say("\(clockTime())  token for \(name) on VM \(vm) (\(ttlAsked / 60) min), asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("token", fields, e.code == Exit.locked ? lockedResult(e.message) : failed(e.message), who, approval: "-") } catch {
            return finish("token", fields, failed("cannot read the vault"), who, approval: "-")
        }
        guard let s = p.secrets[name] else { return finish("token", fields, failed("no secret named \(name) (metacomputer secrets list)"), who, approval: "-") }
        guard s.kind == "api", let host = s.api_host, let template = s.api_header, let (hName, hPrefix, hSuffix) = splitTemplate(template) else {
            return finish("token", fields, denied("\(name) is a \(s.kind) secret; tokens are for api secrets (metacomputer secrets fill is for the others)"), who, approval: "-")
        }
        if let why = projectProblem(name, s, who) { return finish("token", fields, denied(why), who, approval: "-") }
        guard let t = target(p, vm) else { return finish("token", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-") }
        if let why = requiredSinkProblem("token", fields, who) { return finish("token", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(t) {
            return finish("token", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }
        var approval = "session"
        if confirmEach || s.approval == "each" {
            let (yes, how, why) = approve("token", name, "  Give the VM \(vm) a \(max(1, min(ttlAsked, remaining()) / 60))-minute token for \(name) (\(host))?\n  Asked by \(who.describe).",
                                          who, vm: vm, site: "https://\(host)")
            approval = how
            if !yes { return finish("token", fields, denied(why), who, approval: approval) }
        }
        let listener: ProxyListener
        do { listener = try hub.ensure(t) } catch let e as VaultError { return finish("token", fields, failed(e.message), who, approval: approval) } catch {
            return finish("token", fields, failed("cannot start the proxy"), who, approval: approval)
        }
        let ttl = max(1, min(ttlAsked, 8 * 3600, remaining()))
        let (tok, raw) = hub.issue(name: name, vm: vm, ttl: ttl, requester: who.record)
        let base = "http://127.0.0.1:\(listener.guestPort)/\(name)/"
        var res: [String: Any] = ["ok": true, "status": "ok", "token_id": tok.id, "ttl_s": ttl]
        res = finish("token", fields, res, who, approval: approval)
        do {
            try updateVault(paths, key) { p in
                p.secrets[name]?.uses += 1
                p.secrets[name]?.last_used = isoTime()
            }
        } catch { say("          (couldn't record the use in the vault)") }
        res["token_id"] = tok.id
        res["token"] = raw
        res["base_url"] = base
        res["header"] = "\(hName): \(hPrefix)\(raw)\(hSuffix)"
        res["api_host"] = host
        res["expires"] = isoTime(tok.expires)
        res["expires_in_s"] = ttl
        return res
    }

    func opTokens() -> [String: Any] {
        let list: [[String: Any]] = hub.live().map { t in
            var d: [String: Any] = ["id": t.id, "name": t.name, "vm": t.vm, "expires": isoTime(t.expires),
                                    "expires_in_s": max(0, Int(t.expires.timeIntervalSinceNow)), "requests": t.requests]
            if let pr = t.requester["project"] as? String { d["project"] = pr }
            return d
        }
        return ["ok": true, "status": "ok", "tokens": list]
    }

    func opRevoke(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        let id = req["id"] as? String
        guard id == nil || (id.map { matches($0, "^[0-9a-f]{8}$") } ?? false) else { return ["ok": false, "status": "error", "reason": "bad token id"] }
        let gone = hub.revoke(id)
        say("\(clockTime())  revoked \(gone.count) token(s)" + (id.map { " (\($0))" } ?? " (all)") + ", asked by \(who.describe)")
        audit(paths, ["op": "revoke", "result": "ok", "token_ids": gone.map { $0.id }, "requester": who.record])
        flushSinks(force: true)
        return ["ok": true, "status": "ok", "revoked": gone.map { $0.id }]
    }

    // MARK: audit sinks, for list / show / test

    func opSinks() -> [String: Any] {
        guard let p = try? load() else { return ["ok": false, "status": "error", "reason": "cannot read the vault"] }
        let list: [[String: Any]] = (p.audit_sinks ?? [:]).keys.sorted().map { n in
            let s = p.audit_sinks![n]!
            let st = loadSinkState(paths, n, s)
            var d: [String: Any] = ["name": n, "kind": s.kind, "url": maskedURL(s.url), "required": s.required, "created": s.created,
                                    "headers": s.headers.compactMap { $0.first }, "delivered": st.delivered, "backlog": sinkBacklog(paths, n, s)]
            if let e = st.error { d["error"] = e }
            if let a = st.attempt { d["last_attempt"] = a }
            if s.ca_pem != nil { d["ca"] = true }
            return d
        }
        return ["ok": true, "status": "ok", "sinks": list]
    }

    func opSinkFlush(_ req: [String: Any]) -> [String: Any] {
        guard let name = req["name"] as? String, let p = try? load(), let s = p.audit_sinks?[name] else {
            return ["ok": false, "status": "error", "reason": "no such audit sink"]
        }
        if let why = deliverSink(paths, name, s) { return ["ok": false, "status": "error", "reason": why] }
        return ["ok": true, "status": "ok", "delivered": loadSinkState(paths, name, s).delivered]
    }

    // MARK: sealed browser sessions (the desktop account)

    func sessionAAD(_ name: String) -> [UInt8] { Array("mcomp-session|1|\(name)".utf8) }

    /// The archive of a sealed session, under the current session key or, while a rotation
    /// was cut short, the previous one.
    func openSession(_ p: Payload, _ name: String) -> [UInt8]? {
        guard let sealed = try? readFileBytes(paths.sessions + "/\(name).sealed") else { return nil }
        for k in [p.session_key, p.session_key_prev].compactMap({ $0 }) {
            if let d = Data(base64Encoded: k), d.count == 32, let plain = try? openGCM(key: Array(d), combined: sealed, aad: sessionAAD(name)) {
                return plain
            }
        }
        return nil
    }

    func opSessionSave(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        let fields = echo(req, ["name", "vm", "profile"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, let profile = req["profile"] as? String,
              validName(name), validName(vm), validName(profile) else {
            return finish("session_save", fields, failed("bad request"), who, approval: "-")
        }
        let replace = req["replace"] as? Bool == true
        say("\(clockTime())  session save \(name) from profile \(profile) on VM \(vm)" + (replace ? " (replace)" : "") + ", asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("session_save", fields, failed(e.message), who, approval: "-") } catch {
            return finish("session_save", fields, failed("cannot read the vault"), who, approval: "-")
        }
        let old = p.sessions[name]
        if old != nil && !replace {
            return finish("session_save", fields, denied("a session named \(name) exists (--replace overwrites it, after you confirm)"), who, approval: "-")
        }
        guard let t = target(p, vm) else {
            return finish("session_save", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-")
        }
        if let why = requiredSinkProblem("session_save", fields, who) { return finish("session_save", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(t) {
            return finish("session_save", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }
        // Saving always asks (unless a standing approval covers it): the agent starts it, and a saved login is a credential.
        let question = old.map { "  Replace the sealed session \(name) (saved \($0.saved_at) from VM \($0.vm), profile \($0.profile)) with profile \(profile) from VM \(vm)?" }
            ?? "  Save Chrome profile \(profile) from VM \(vm) as the sealed session \(name)?"
        let (yes, approval, why) = approve("session_save", name, question + "\n  Asked by \(who.describe).", who, vm: vm, site: nil)
        if !yes { return finish("session_save", fields, denied(why), who, approval: approval) }
        if let problem = runner.listenerProblem(t) {
            return finish("session_save", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: approval)
        }
        let r: ProcResult
        do { r = try runner.opsSSH(t, remote: sessionSaveScript(user: t.binding.guest_user, profile: profile), input: [],
                                   timeout: TimeInterval(min(600, remaining())), maxStdout: 1 << 31) }
        catch { return finish("session_save", fields, failed("cannot reach the VM"), who, approval: approval) }
        var archive = r.stdout
        defer { wipe(&archive) }
        if r.interrupted || r.timedOut || r.tooLarge || r.status != 0 || archive.count < 2 || archive[0] != 0x1F || archive[1] != 0x8B {
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish in time" : r.tooLarge ? "the profile is too large"
                : (r.stderr.isEmpty ? "exit \(r.status)" : oneLine(String(decoding: r.stderr, as: UTF8.self), max: 200))
            return finish("session_save", fields, r.status == 4 ? denied(why) : failed(why), who, approval: approval)
        }
        do {
            try ensurePrivateDir(paths.sessions)
            let bytes = archive.count
            let a = archive
            // Sealed and recorded under the vault's lock, with whatever session key is current then.
            try updateVault(paths, key) { p in
                guard let d = Data(base64Encoded: p.session_key), d.count == 32 else { throw VaultError("the vault's session key is damaged") }
                try writeFileAtomic(paths.sessions + "/\(name).sealed", try sealGCM(key: Array(d), plaintext: a, aad: sessionAAD(name)))
                p.sessions[name] = SessionMeta(vm: vm, profile: profile, saved_at: isoTime(), bytes: bytes, last_loaded: nil, loads: 0,
                                               approval: old?.approval)
            }
            return finish("session_save", fields, ["ok": true, "status": "ok", "bytes": bytes], who, approval: approval)
        } catch let e as VaultError {
            return finish("session_save", fields, failed(e.message), who, approval: approval)
        } catch {
            return finish("session_save", fields, failed("cannot seal the session"), who, approval: approval)
        }
    }

    func opSessionLoad(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["name", "vm", "profile"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, validName(name), validName(vm),
              (req["profile"] as? String).map(validName) ?? (req["profile"] == nil) else {
            return finish("session_load", fields, failed("bad request"), who, approval: "-")
        }
        say("\(clockTime())  session load \(name) on VM \(vm), asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("session_load", fields, failed(e.message), who, approval: "-") } catch {
            return finish("session_load", fields, failed("cannot read the vault"), who, approval: "-")
        }
        guard let meta = p.sessions[name] else {
            return finish("session_load", fields, failed("no saved session named \(name) (metacomputer secrets list)"), who, approval: "-")
        }
        let profile = req["profile"] as? String ?? meta.profile
        fields["profile"] = profile
        // Don't forge or rewrite the driver's own records.
        guard profile == meta.profile else {
            return finish("session_load", fields, denied("\(name) was saved from profile \(meta.profile). The driver records a profile's name inside it (.cua-driver-owned-profile.json), so the session works only under that name: leave out --profile, or use --profile \(meta.profile)"), who, approval: "-")
        }
        guard let t = target(p, vm) else {
            return finish("session_load", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-")
        }
        if let why = requiredSinkProblem("session_load", fields, who) { return finish("session_load", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(t) {
            return finish("session_load", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }
        var approval = "session"
        if confirmEach || meta.approval != "session" || meta.vm != vm {
            let question = meta.vm != vm
                ? "  Load the sealed session \(name), saved from VM \(meta.vm), into a different VM, \(vm) (profile \(profile); replaces it)?"
                : "  Load the sealed session \(name) into profile \(profile) on VM \(vm) (replaces that profile)?"
            // A load into another VM always asks this time, whatever was granted before.
            let kind = meta.vm != vm ? "session_load_elsewhere:\(vm)" : "session_load"
            let (yes, how, why) = approve(kind, name, question + "\n  Asked by \(who.describe).", who, vm: vm, site: nil)
            approval = how
            if !yes { return finish("session_load", fields, denied(why), who, approval: approval) }
            if let problem = runner.listenerProblem(t) {
                return finish("session_load", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: approval)
            }
        }
        guard var archive = openSession(p, name) else {
            return finish("session_load", fields, failed("the sealed session file is missing or was changed"), who, approval: approval)
        }
        defer { wipe(&archive) }
        guard Date() < expires, receivedSignal == 0 else {
            return finish("session_load", fields, lockedResult("the vault locked before the session was sent"), who, approval: approval)
        }
        let r: ProcResult
        do { r = try runner.opsSSH(t, remote: sessionLoadScript(user: t.binding.guest_user, profile: profile), input: archive,
                                   timeout: TimeInterval(min(600, remaining()))) }
        catch { return finish("session_load", fields, failed("cannot reach the VM"), who, approval: approval) }
        if r.interrupted || r.timedOut || r.status != 0 {
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish in time"
                : (r.stderr.isEmpty ? "exit \(r.status)" : oneLine(String(decoding: r.stderr, as: UTF8.self), max: 200))
            return finish("session_load", fields, (r.status == 4 || r.status == 5) ? denied(why) : failed(why), who, approval: approval)
        }
        do {
            try updateVault(paths, key) { p in
                p.sessions[name]?.loads += 1
                p.sessions[name]?.last_loaded = isoTime()
            }
        } catch { say("          (couldn't record the use in the vault)") }
        return finish("session_load", fields, ["ok": true, "status": "ok", "bytes": archive.count], who, approval: approval)
    }
}

// MARK: - the events hook

/// The shared events spool (architecture.md, "Event spool"): MCOMP_EVENTS_SPOOL, or (release
/// builds) ~/Library/Application Support/metacomputer/events/spool. Made (0700) if missing;
/// nil when it can't be used (not a plain folder of this user's).
func eventsSpool() -> String? {
    let env = ProcessInfo.processInfo.environment["MCOMP_EVENTS_SPOOL"] ?? ""
    #if MCOMP_VAULT_TESTING
    let dir = env
    #else
    let dir = env.isEmpty ? homeDirectory() + "/Library/Application Support/metacomputer/events/spool" : env
    #endif
    guard dir.hasPrefix("/") else { return nil }
    var st = stat()
    if lstat(dir, &st) != 0 {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }
    guard lstat(dir, &st) == 0, (st.st_mode & S_IFMT) == S_IFDIR, st.st_uid == getuid() else { return nil }
    if st.st_mode & 0o077 != 0 { chmod(dir, 0o700) }
    return dir
}

/// A spool file's name: UTC YYYYMMDDTHHMMSS.ffffffZ (fixed width, so names sort in time
/// order), a dash, 16 random letters and digits, .json.
func spoolFileName() -> String {
    var tv = timeval()
    gettimeofday(&tv, nil)
    var t = time_t(tv.tv_sec)
    var g = tm()
    gmtime_r(&t, &g)
    let stamp = String(format: "%04d%02d%02dT%02d%02d%02d.%06dZ", Int(g.tm_year) + 1900, Int(g.tm_mon) + 1, Int(g.tm_mday),
                       Int(g.tm_hour), Int(g.tm_min), Int(g.tm_sec), Int(tv.tv_usec))
    return stamp + "-" + randomBytes(8).map { String(format: "%02x", $0) }.joined() + ".json"
}

/// One event file, written atomically (a dot-name in the same folder, then a rename). Never
/// blocks, never fails a request. Never a value or a token.
func dropSpoolEvent(family: String, type: String, severity: String, vm: String, message: String, action: String,
                    project: String?, data: [String: Any]) {
    guard let dir = eventsSpool() else { return }
    var ev: [String: Any] = ["family": family, "type": type, "severity": severity, "vm": clean(vm, max: 64),
                             "message": clean(message, max: 400), "action": clean(action, max: 200),
                             "data": (cleanAll(data, max: 200) as? [String: Any]) ?? [:]]
    if let p = project { ev["project"] = clean(p, max: 1024) }
    let name = spoolFileName()
    let final = dir + "/" + name
    let tmp = dir + "/." + name + ".tmp"
    let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0o600)
    guard fd >= 0 else { return }
    let ok = writeAll(fd, Array((jsonLine(ev) + "\n").utf8))
    close(fd)
    if !ok || rename(tmp, final) != 0 { unlink(tmp) }
}
