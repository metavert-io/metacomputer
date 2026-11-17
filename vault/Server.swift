// mcomp-vault: `unlock`, the only process that holds the vault's key and its values.
//
// It runs in the foreground in the user's own terminal, serves requests on a Unix
// socket that only this user can reach (0600, peer uid checked), shows every request
// and its outcome there (never a value), asks there when a use needs approval, and
// locks on Ctrl-C, on its timeout, or when the terminal closes.
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

// MARK: - the server

final class UnlockServer {
    let paths: Paths
    let key: VaultKey
    let confirmEach: Bool
    let started = Date()
    let expires: Date
    let runner = GuestRunner()
    var promptTimeout = 120
    var listenFD: Int32 = -1

    init(paths: Paths, key: VaultKey, seconds: Int, confirmEach: Bool) {
        self.paths = paths
        self.key = key
        self.confirmEach = confirmEach
        self.expires = Date().addingTimeInterval(TimeInterval(seconds))
        #if MCOMP_VAULT_TESTING
        if let t = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_PROMPT_TIMEOUT"], let n = Int(t), n > 0 { promptTimeout = n }
        #endif
    }

    func say(_ s: String) { oprint(s) }

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
        listenFD = fd

        let h: @convention(c) (Int32) -> Void = { sig in receivedSignal = sig }
        signal(SIGINT, h)
        signal(SIGTERM, h)
        signal(SIGHUP, h)

        let mins = Int(expires.timeIntervalSince(started)) / 60
        say("metacomputer secrets: unlocked until \(clockTime(expires))" + (mins > 0 ? " (\(mins) min)" : "")
            + (confirmEach ? "; every use asks here first" : "; secrets set to 'each' ask here first"))
        say("  Requests and their outcomes appear here (never values). Ctrl-C locks the vault.")
        #if MCOMP_VAULT_TESTING
        say("  TEST BUILD: honours MCOMP_VAULT_TEST_* settings; never use it for real secrets")
        #endif
        audit(paths, ["op": "unlock", "result": "ok", "until": isoTime(expires), "confirm": confirmEach ? "each" : "session", "pid": Int(getpid())])

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
        say((why == "Ctrl-C" ? "\n" : "") + "\(clockTime())  locked (\(why))")
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
        default: res = ["ok": false, "status": "error", "reason": "unknown request \(oneLine(op, max: 40))"]
        }
        setTimeouts(c, seconds: 10)
        reply(c, res)
    }

    func reply(_ c: Int32, _ obj: [String: Any]) {
        _ = writeAll(c, Array((jsonLine(obj) + "\n").utf8))
    }

    func load() throws -> Payload { try loadPayload(paths, key) }

    // MARK: status and list

    func opStatus() -> [String: Any] {
        ["ok": true, "status": "ok", "unlocked": true, "since": isoTime(started), "expires": isoTime(expires),
         "expires_in_s": max(0, Int(expires.timeIntervalSinceNow)), "confirm": confirmEach ? "each" : "session",
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
            if let g = b.gateway_path { d["gateway"] = g }
            return d
        }
        let sessions: [[String: Any]] = p.sessions.keys.sorted().map { n in
            let m = p.sessions[n]!
            var d: [String: Any] = ["name": n, "vm": m.vm, "profile": m.profile, "saved_at": m.saved_at, "bytes": m.bytes, "loads": m.loads]
            if let l = m.last_loaded { d["last_loaded"] = l }
            return d
        }
        return ["ok": true, "status": "ok", "secrets": secrets, "vms": vms, "sessions": sessions]
    }

    // MARK: fill

    func finish(_ op: String, _ fields: [String: Any], _ res: [String: Any], pid: Int32, approval: String) -> [String: Any] {
        var out = fields
        for k in ["ok", "status", "origin", "chars", "reason", "bytes"] { if let v = res[k] { out[k] = v } }
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

    func target(_ p: Payload, _ vm: String) -> GuestTarget? {
        p.vms[vm].map { GuestTarget(vm: vm, binding: $0) }
    }

    func opFill(_ req: [String: Any], who: String, pid: Int32) -> [String: Any] {
        let name = req["name"] as? String ?? "", vm = req["vm"] as? String ?? "", field = req["field"] as? String ?? ""
        let profile = req["profile"] as? String, selector = req["selector"] as? String
        var fields: [String: Any] = ["name": name, "vm": vm, "field": field]
        if let pr = profile { fields["profile"] = pr }
        guard validName(name), validName(vm), ["password", "username", "otp", "text"].contains(field),
              profile.map(validName) ?? true, (selector?.count ?? 0) <= 300 else {
            return finish("fill", fields, failed("bad request"), pid: pid, approval: "-")
        }
        say("\(clockTime())  fill \(name) (\(field)) on VM \(vm), asked by \(who)")
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
        var base: [String: Any] = ["field": field, "origins": s.origins]
        if let pr = profile { base["profile"] = pr }
        if let sel = selector, !sel.isEmpty { base["selector"] = sel }

        var approval = "session"
        if confirmEach || s.approval == "each" {
            var chk = base
            chk["mode"] = "check"
            let r: [String: Any]
            do { r = try runner.guestFill(t, request: chk, value: nil) } catch let e as VaultError {
                return finish("fill", fields, failed(e.message), pid: pid, approval: "each")
            } catch { return finish("fill", fields, failed("the check failed"), pid: pid, approval: "each") }
            guard r["ok"] as? Bool == true else { return finish("fill", fields, r, pid: pid, approval: "each") }
            let origin = r["origin"] as? String ?? s.origins.joined(separator: ", ")
            let yes = askYesNo("  Allow fill of \(name) (\(field)) into \(origin) on VM \(vm)?", timeout: promptTimeout)
            approval = yes ? "each:yes" : "each:no"
            if !yes { return finish("fill", fields, denied("not allowed at the vault's terminal"), pid: pid, approval: approval) }
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
            if left < 3 { Thread.sleep(forTimeInterval: Double(left) + 0.2) }
            value = Array(totp(secret: seed, time: UInt64(Date().timeIntervalSince1970)).utf8)
        default: value = Array(s.value.utf8)
        }
        var req2 = base
        req2["mode"] = "fill"
        let r: [String: Any]
        do { r = try runner.guestFill(t, request: req2, value: value) } catch let e as VaultError {
            wipe(&value)
            return finish("fill", fields, failed(e.message), pid: pid, approval: approval)
        } catch {
            wipe(&value)
            return finish("fill", fields, failed("the fill failed"), pid: pid, approval: approval)
        }
        wipe(&value)
        if r["ok"] as? Bool == true {
            do {
                try updateVault(paths, key) { p in
                    p.secrets[name]?.uses += 1
                    p.secrets[name]?.last_used = isoTime()
                }
            } catch { say("          (couldn't record the use in the vault)") }
        }
        return finish("fill", fields, r, pid: pid, approval: approval)
    }

    // MARK: sealed browser sessions

    func profileRoot(_ user: String) -> String { "/home/\(user)/.local/state/cua-driver/browser-profiles" }

    /// Refuse while a Chrome uses the profile. "[-]" keeps pgrep from matching this shell.
    func inUseCheck(_ dir: String, _ profile: String) -> String {
        "if pgrep -u \"$(id -u)\" -f -- \"[-]-user-data-dir=\(dir)( |\\$)\" >/dev/null 2>&1; then echo \"Chrome is using profile \(profile); end its browser session first\" >&2; exit 4; fi\n"
    }

    func saveScript(user: String, profile: String) -> String {
        let root = profileRoot(user), d = root + "/" + profile
        let excludes = ["Singleton*", "DevToolsActivePort", "*/Cache", "*/Code Cache", "*/GPUCache", "*/DawnCache",
                        "*/DawnGraphiteCache", "*/DawnWebGPUCache", "GrShaderCache", "ShaderCache", "GraphiteDawnCache",
                        "component_crx_cache", "Crashpad"]
        return "set -e\n"
            + "[ -d '\(d)' ] || { echo 'no Chrome profile named \(profile) in the VM' >&2; exit 3; }\n"
            + inUseCheck(d, profile)
            + "cd '\(root)'\n"
            + "exec tar " + excludes.map { "--exclude=" + shellQuote(profile + "/" + $0) }.joined(separator: " ")
            + " -czf - '\(profile)'\n"
    }

    func loadScript(user: String, profile: String, archived: String) -> String {
        let root = profileRoot(user), d = root + "/" + profile
        return "set -e\n"
            + inUseCheck(d, profile)
            + "mkdir -p '\(root)'\n"
            + "t=$(mktemp -d '\(root)/.mcomp-load.XXXXXX')\n"
            + "trap 'rm -rf \"$t\"' EXIT\n"
            + "tar -C \"$t\" --no-same-owner -xzf -\n"
            + "[ -d \"$t/\(archived)\" ] || { echo 'the sealed session holds no profile folder' >&2; exit 5; }\n"
            + "rm -rf '\(d)'\n"
            + "mv \"$t/\(archived)\" '\(d)'\n"
    }

    func sessionAAD(_ name: String) -> [UInt8] { Array("mcomp-session|1|\(name)".utf8) }

    func sessionKey(_ p: Payload) -> [UInt8]? {
        guard let d = Data(base64Encoded: p.session_key), d.count == 32 else { return nil }
        return Array(d)
    }

    func sessionRequest(_ op: String, _ req: [String: Any]) -> (name: String, vm: String, profile: String, fields: [String: Any])? {
        let name = req["name"] as? String ?? "", vm = req["vm"] as? String ?? "", profile = req["profile"] as? String ?? ""
        guard validName(name), validName(vm), validName(profile) else { return nil }
        return (name, vm, profile, ["name": name, "vm": vm, "profile": profile])
    }

    func opSessionSave(_ req: [String: Any], who: String, pid: Int32) -> [String: Any] {
        guard let (name, vm, profile, fields) = sessionRequest("save", req) else {
            return finish("session_save", ["name": req["name"] as? String ?? ""], failed("bad request"), pid: pid, approval: "-")
        }
        say("\(clockTime())  session save \(name) from profile \(profile) on VM \(vm), asked by \(who)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("session_save", fields, failed(e.message), pid: pid, approval: "-") } catch {
            return finish("session_save", fields, failed("cannot read the vault"), pid: pid, approval: "-")
        }
        if p.sessions[name] != nil && req["replace"] as? Bool != true {
            return finish("session_save", fields, denied("a session named \(name) exists (--replace overwrites it)"), pid: pid, approval: "-")
        }
        guard let t = target(p, vm) else {
            return finish("session_save", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), pid: pid, approval: "-")
        }
        guard var skey = sessionKey(p) else { return finish("session_save", fields, failed("the vault's session key is damaged"), pid: pid, approval: "-") }
        defer { wipe(&skey) }
        if let problem = runner.listenerProblem(t) {
            return finish("session_save", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), pid: pid, approval: "-")
        }
        var approval = "session"
        if confirmEach {
            let yes = askYesNo("  Allow session save of \(name) from profile \(profile) on VM \(vm)?", timeout: promptTimeout)
            approval = yes ? "each:yes" : "each:no"
            if !yes { return finish("session_save", fields, denied("not allowed at the vault's terminal"), pid: pid, approval: approval) }
        }
        let r: ProcResult
        do { r = try runner.ssh(t, remote: saveScript(user: t.binding.guest_user, profile: profile), input: [], timeout: 600, maxStdout: 1 << 31) }
        catch { return finish("session_save", fields, failed("cannot reach the VM"), pid: pid, approval: approval) }
        var archive = r.stdout
        defer { wipe(&archive) }
        if r.interrupted || r.timedOut || r.tooLarge || r.status != 0 || archive.count < 2 || archive[0] != 0x1F || archive[1] != 0x8B {
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish within 600 s" : r.tooLarge ? "the profile is too large"
                : (r.stderr.isEmpty ? "exit \(r.status)" : oneLine(String(decoding: r.stderr, as: UTF8.self), max: 200))
            return finish("session_save", fields, r.status == 4 ? denied(why) : failed(why), pid: pid, approval: approval)
        }
        do {
            try ensurePrivateDir(paths.sessions)
            let sealed = try sealGCM(key: skey, plaintext: archive, aad: sessionAAD(name))
            try writeFileAtomic(paths.sessions + "/\(name).sealed", sealed)
            let bytes = archive.count
            try updateVault(paths, key) { p in
                p.sessions[name] = SessionMeta(vm: vm, profile: profile, saved_at: isoTime(), bytes: bytes, last_loaded: nil, loads: 0)
            }
            return finish("session_save", fields, ["ok": true, "status": "ok", "bytes": bytes], pid: pid, approval: approval)
        } catch let e as VaultError {
            return finish("session_save", fields, failed(e.message), pid: pid, approval: approval)
        } catch {
            return finish("session_save", fields, failed("cannot seal the session"), pid: pid, approval: approval)
        }
    }

    func opSessionLoad(_ req: [String: Any], who: String, pid: Int32) -> [String: Any] {
        guard let (name, vm, profile, fields) = sessionRequest("load", req) else {
            return finish("session_load", ["name": req["name"] as? String ?? ""], failed("bad request"), pid: pid, approval: "-")
        }
        say("\(clockTime())  session load \(name) into profile \(profile) on VM \(vm), asked by \(who)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("session_load", fields, failed(e.message), pid: pid, approval: "-") } catch {
            return finish("session_load", fields, failed("cannot read the vault"), pid: pid, approval: "-")
        }
        guard let meta = p.sessions[name] else {
            return finish("session_load", fields, failed("no saved session named \(name) (metacomputer secrets list)"), pid: pid, approval: "-")
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
        if confirmEach {
            let yes = askYesNo("  Allow session load of \(name) into profile \(profile) on VM \(vm) (replaces that profile)?", timeout: promptTimeout)
            approval = yes ? "each:yes" : "each:no"
            if !yes { return finish("session_load", fields, denied("not allowed at the vault's terminal"), pid: pid, approval: approval) }
        }
        var archive: [UInt8]
        do {
            let sealed = try readFileBytes(paths.sessions + "/\(name).sealed")
            archive = try openGCM(key: skey, combined: sealed, aad: sessionAAD(name))
        } catch {
            return finish("session_load", fields, failed("the sealed session file is missing or was changed"), pid: pid, approval: approval)
        }
        defer { wipe(&archive) }
        let r: ProcResult
        do { r = try runner.ssh(t, remote: loadScript(user: t.binding.guest_user, profile: profile, archived: meta.profile), input: archive, timeout: 600) }
        catch { return finish("session_load", fields, failed("cannot reach the VM"), pid: pid, approval: approval) }
        if r.interrupted || r.timedOut || r.status != 0 {
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish within 600 s"
                : (r.stderr.isEmpty ? "exit \(r.status)" : oneLine(String(decoding: r.stderr, as: UTF8.self), max: 200))
            return finish("session_load", fields, r.status == 4 ? denied(why) : failed(why), pid: pid, approval: approval)
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
