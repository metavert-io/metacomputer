// mcomp-vault: the API proxy behind `metacomputer secrets token`.
//
// While the vault is unlocked, a token request for an `api` secret starts (once per VM):
//  - a listener on 127.0.0.1:<random> on the Mac;
//  - an SSH reverse tunnel, as the VM's locked proxy account (mcproxy, whose key may only
//    forward 127.0.0.1:<proxy port> inside the VM), so tools in the VM reach the listener
//    at http://127.0.0.1:<proxy port>/.
// A request to http://127.0.0.1:<proxy port>/<NAME>/<path> must carry a live token for NAME
// and that VM in the secret's own auth header (or X-Metacomputer-Token). The proxy swaps in
// the real header, forwards to https://<api host><path> with certificate validation, never
// follows a redirect to another host, drops hop-by-hop headers, streams the answer back and
// audits each request (method, host, path without its query, status, bytes, token id; no
// bodies, no headers). The real key never enters the VM. Tokens die at their TTL, on
// revoke, or when the vault locks (this process ends). `token renew` moves a live token's
// expiry later, the token in the VM unchanged: never past the unlock, never more than
// tokenMaxLifetime after it was issued. An answer in flight ends with its token.
import Darwin
import Foundation

/// The longest a token may be asked for (`token --ttl`, `token renew --ttl`).
let tokenMaxTTL = 8 * 3600
/// The longest a token may live from when it was issued, however often it's renewed: the
/// longest unlock (`unlock --for` takes at most 12 h, and every token ends at lock anyway).
let tokenMaxLifetime = 12 * 3600
/// How long one proxied answer may take, from forwarding the request to its last byte: the
/// default, and the range an api secret's `--answer-timeout` may set.
let defaultAnswerTimeout = 600
let maxAnswerTimeout = 3600
let minAnswerTimeout = 10

struct APIToken {
    let id: String
    let hash: String            // sha256 of the token; the token itself isn't kept
    let name: String
    let vm: String
    let issued: Date
    var expires: Date           // later with each renewal, never past `limit`
    let ttl: Int                // the lifetime it was issued for: a renewal's step unless it names another
    let limit: Date             // issued + the longest lifetime: the latest it may ever expire
    var renewals: Int
    let requester: [String: Any]
    // Who it was made for, to tell its own Claude Code session (same pid and start time) from another requester when
    // it's renewed. nil: it wasn't made for a Claude Code session.
    let ownerClaudePID: pid_t?
    let ownerClaudeStart: Double?
    let ownerDescribe: String
    var requests: Int
    // What was approved: the secret's generation and its scope then. A token works only while
    // the secret is still that one (no set --replace or delete since) with that host, header
    // and path prefix.
    let gen: String
    let host: String
    let header: String
    let prefix: String?

    func fits(_ s: Secret) -> Bool {
        s.kind == "api" && secretGen(s) == gen && s.api_host == host && s.api_header == header && s.api_path_prefix == prefix
    }
}

final class Tunnel: @unchecked Sendable {
    let pid: pid_t
    let dir: String
    init(pid: pid_t, dir: String) {
        self.pid = pid
        self.dir = dir
    }
    var alive: Bool {
        var st: Int32 = 0
        return waitpid(pid, &st, WNOHANG) == 0
    }
    func stop() {
        kill(pid, SIGTERM)
        var st: Int32 = 0
        for _ in 0..<20 where waitpid(pid, &st, WNOHANG) == 0 { usleep(50_000) }
        if waitpid(pid, &st, WNOHANG) == 0 { kill(pid, SIGKILL); waitpid(pid, &st, 0) }
        for f in ["known_hosts", "ssh.err"] { unlink(dir + "/" + f) }
        rmdir(dir)
    }
}

final class ProxyListener: @unchecked Sendable {
    let vm: String
    let fd: Int32
    let macPort: Int
    let guestPort: Int
    var tunnel: Tunnel?
    /// This VM's connections at once: each VM has its own, so one VM holding all of its
    /// slots never slows another.
    let slots: DispatchSemaphore
    init(vm: String, fd: Int32, macPort: Int, guestPort: Int, slots: Int) {
        self.vm = vm
        self.fd = fd
        self.macPort = macPort
        self.guestPort = guestPort
        self.slots = DispatchSemaphore(value: slots)
    }
}

/// Everything the proxy threads share with the unlock process, behind one lock.
final class ProxyHub: @unchecked Sendable {
    let lock = NSLock()
    let paths: Paths
    let key: VaultKey
    let sshPath: String
    let upstreamScheme: String
    var tokens: [String: APIToken] = [:]        // by token hash
    var listeners: [String: ProxyListener] = [:]
    var closed = false
    /// Connections at once from one VM (each VM's listener has its own count, so one VM holding
    /// all of its slots never slows another). A connection over the limit waits up to slotWait
    /// seconds for a slot (its VM's accept loop with it), then gets 503.
    let slotsPerVM: Int
    let slotWait: TimeInterval
    /// The whole request head must arrive within this many seconds of the connection, however
    /// slowly its bytes trickle in; the token is checked right after it, before any body is read.
    let headDeadline: TimeInterval
    /// An authenticated request's body must arrive within this many seconds.
    let bodyDeadline: TimeInterval = 300
    /// The whole upstream answer must be through within this many seconds, unless the secret sets
    /// its own (`--answer-timeout`, minAnswerTimeout to maxAnswerTimeout).
    let answerTimeout: TimeInterval
    /// A token's longest life from its issue (tokenMaxLifetime; smaller in test builds).
    let maxLifetime: TimeInterval
    // Set by the unlock process before the first token: write an audit record with the vault's
    // MAC, and get a request's pre-use record acknowledged by every required audit sink.
    var recorder: (([String: Any]) -> [String: Any]?)?
    var preUse: (([String: Any], [String: Any]) -> String?)?

    init(paths: Paths, key: VaultKey, sshPath: String) {
        self.paths = paths
        self.key = key
        self.sshPath = sshPath
        var scheme = "https"
        var slots = 16
        var wait: TimeInterval = 15
        var head: TimeInterval = 10
        var answer = TimeInterval(defaultAnswerTimeout)
        var lifetime = TimeInterval(tokenMaxLifetime)
        #if MCOMP_VAULT_TESTING
        // Test builds only: plain http to a local stand-in API (no certificate to check), and
        // smaller limits so a test needn't wait long.
        let env = ProcessInfo.processInfo.environment
        if env["MCOMP_VAULT_TEST_API_HTTP"] == "1" { scheme = "http" }
        if let s = env["MCOMP_VAULT_TEST_PROXY_SLOTS"], let n = Int(s), n > 0 { slots = n }
        if let s = env["MCOMP_VAULT_TEST_SLOT_WAIT"], let n = Double(s), n > 0 { wait = n }
        if let s = env["MCOMP_VAULT_TEST_HEAD_DEADLINE"], let n = Double(s), n > 0 { head = n }
        if let s = env["MCOMP_VAULT_TEST_ANSWER_TIMEOUT"], let n = Double(s), n > 0 { answer = n }
        if let s = env["MCOMP_VAULT_TEST_TOKEN_MAX_LIFETIME"], let n = Double(s), n > 0 { lifetime = n }
        #endif
        upstreamScheme = scheme
        slotsPerVM = slots
        slotWait = wait
        headDeadline = head
        answerTimeout = answer
        maxLifetime = lifetime
    }

    /// How long an answer for this secret may take: its own setting when it has a valid one, else the default.
    func answerLimit(_ s: Secret) -> TimeInterval {
        if let n = s.api_answer_timeout_s, n >= minAnswerTimeout, n <= maxAnswerTimeout { return TimeInterval(n) }
        return answerTimeout
    }

    func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    // MARK: tokens

    func issue(name: String, vm: String, ttl: Int, requester who: Requester, secret s: Secret) -> (APIToken, String) {
        let raw = "mct_" + base64url(randomBytes(24))
        let now = Date()
        let limit = now.addingTimeInterval(maxLifetime)
        let t = APIToken(id: randomBytes(4).map { String(format: "%02x", $0) }.joined(), hash: sha256Hex(Array(raw.utf8)), name: name, vm: vm,
                         issued: now, expires: min(now.addingTimeInterval(TimeInterval(ttl)), limit), ttl: ttl, limit: limit, renewals: 0,
                         requester: who.record, ownerClaudePID: who.claudePID, ownerClaudeStart: who.claudeStart,
                         ownerDescribe: who.describe, requests: 0,
                         gen: secretGen(s), host: s.api_host ?? "", header: s.api_header ?? "", prefix: s.api_path_prefix)
        withLock { tokens[t.hash] = t }
        return (t, raw)
    }

    /// Move a live token's expiry to `until` (never past its limit, never earlier than it is). It must still be
    /// there and unexpired at this moment, on that VM: a revoke, an expiry or a lock that came first wins.
    /// Returns the token as it is now and whether its expiry moved, or nil and why not.
    func renew(id: String, vm: String, until: Date) -> (APIToken?, Bool, String) {
        withLock {
            if closed { return (nil, false, "the vault is locking") }
            guard var t = tokens.values.first(where: { $0.id == id }), t.vm == vm else {
                return (nil, false, "was revoked before it could be renewed")
            }
            guard t.expires > Date() else {
                tokens[t.hash] = nil
                return (nil, false, "expired before it could be renewed")
            }
            let target = min(until, t.limit)
            let moved = target > t.expires
            if moved { t.expires = target }
            t.renewals += 1
            tokens[t.hash] = t
            return (t, moved, "")
        }
    }

    /// Is this token still live: present, unexpired, and the proxy not shutting down?
    func stillLive(_ t: APIToken) -> Bool {
        withLock { !closed && tokens[t.hash].map { $0.expires > Date() } == true }
    }

    /// The vault is locking (`lock` accepted, its time up, a signal): every token ends now, before the unlock process
    /// gets round to stopping the listeners and tunnels. Answers in flight see it within a quarter of a second.
    func lockdown() {
        withLock {
            closed = true
            tokens = [:]
        }
    }

    // The vault file as last read for pruning: its inode, size and modification time.
    var vaultStamp: [Int] = []
    let refreshLock = NSLock()

    /// When the vault file changed (a `set --replace`, a `delete`, any write), read it again and end the tokens whose
    /// secret is gone or changed, so an answer streaming on one ends too. A stat each time; a read only on a change.
    /// Fails closed: a vault file that's missing, or that this unlock's key can't open and verify (a rekey, a recover, a
    /// damaged file), ends every token at once, and the change is read again next time (the file's stamp is kept only
    /// after it opened). Returns false then; the unlock process locks (UnlockServer.checkVault).
    @discardableResult
    func refreshIfVaultChanged() -> Bool {
        var st = stat()
        guard stat(paths.vault, &st) == 0 else {
            endAll()
            return false
        }
        let stamp = [Int(st.st_ino), Int(st.st_size), Int(st.st_mtimespec.tv_sec), Int(st.st_mtimespec.tv_nsec)]
        guard refreshLock.try() else { return true }  // another thread is checking this very change right now
        defer { refreshLock.unlock() }
        guard stamp != vaultStamp else { return true }
        guard let p = try? loadPayload(paths, key) else {
            endAll()
            return false
        }
        vaultStamp = stamp
        prune(p)
        return true
    }

    /// Every token ends (the proxy keeps running; the unlock process decides whether to lock).
    func endAll() {
        withLock { tokens = [:] }
    }

    /// Drop tokens whose secret is gone or changed (set --replace, delete); returns them.
    @discardableResult
    func prune(_ p: Payload) -> [APIToken] {
        withLock {
            let gone = tokens.values.filter { t in p.secrets[t.name].map { !t.fits($0) } ?? true }
            for t in gone { tokens[t.hash] = nil }
            return gone
        }
    }

    func live() -> [APIToken] {
        withLock {
            let now = Date()
            tokens = tokens.filter { $0.value.expires > now }
            return tokens.values.sorted { $0.issued < $1.issued }
        }
    }

    func revoke(_ id: String?) -> [APIToken] {
        withLock {
            let gone = tokens.values.filter { id == nil || $0.id == id }
            for t in gone { tokens[t.hash] = nil }
            return gone
        }
    }

    /// The live token this request presents for NAME on this VM, or nil.
    func check(_ raw: String, name: String, vm: String) -> APIToken? {
        withLock {
            guard !closed, var t = tokens[sha256Hex(Array(raw.utf8))], t.name == name, t.vm == vm, t.expires > Date() else { return nil }
            t.requests += 1
            tokens[t.hash] = t
            return t
        }
    }

    // MARK: listeners and tunnels

    /// The VM's listener and tunnel, started if needed. Throws with the reason.
    func ensure(_ target: GuestTarget) throws -> ProxyListener {
        let b = target.binding
        guard let user = b.proxy_user, let keyPath = b.proxy_key_path, let guestPort = b.proxy_port else {
            throw VaultError("VM \(target.vm) has no proxy account bound: VMs built before api tokens need a rebuild; then run `metacomputer secrets bind` again", code: Exit.error)
        }
        let l: ProxyListener
        if let have = withLock({ listeners[target.vm] }) {
            l = have
        } else {
            l = try openListener(vm: target.vm, guestPort: guestPort)
            withLock { listeners[target.vm] = l }
            let hub = self
            Thread.detachNewThread { hub.acceptLoop(l) }
        }
        if l.tunnel?.alive != true {
            l.tunnel?.stop()
            l.tunnel = try startTunnel(target, user: user, keyPath: keyPath, guestPort: guestPort, macPort: l.macPort)
        }
        return l
    }

    func openListener(vm: String, guestPort: Int) throws -> ProxyListener {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw VaultError("cannot make the proxy's socket", code: Exit.error) }
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        var a = sockaddr_in()
        a.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        a.sin_family = sa_family_t(AF_INET)
        a.sin_port = 0
        a.sin_addr.s_addr = inet_addr("127.0.0.1")
        let rc = withUnsafePointer(to: &a) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        guard rc == 0, listen(fd, 16) == 0 else { close(fd); throw VaultError("cannot start the proxy's listener", code: Exit.error) }
        var got = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &got) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) } }
        return ProxyListener(vm: vm, fd: fd, macPort: Int(UInt16(bigEndian: got.sin_port)), guestPort: guestPort, slots: slotsPerVM)
    }

    /// ssh -N -R 127.0.0.1:<guest port>:127.0.0.1:<mac port> as the proxy account, with the
    /// pinned port, host key and key. Waits until the forward is up, or says why not.
    func startTunnel(_ t: GuestTarget, user: String, keyPath: String, guestPort: Int, macPort: Int) throws -> Tunnel {
        let dir = userTempDir() + "/mcomp-tunnel-\(getpid())-\(UInt32.random(in: 0...UInt32.max))"
        guard mkdir(dir, 0o700) == 0 else { throw VaultError("cannot make a temporary folder", code: Exit.error) }
        let errPath = dir + "/ssh.err"
        // The pinned host key goes to ssh as KnownHostsCommand arguments, never through a file.
        guard let pin = pinnedHostKeyOptions(binding: t.binding) else {
            rmdir(dir)
            throw VaultError("the pinned host key for VM \(t.vm) is damaged: bind it again", code: Exit.error)
        }
        let args = sshBaseOptions(key: keyPath, port: t.binding.ssh_port, remote: t.binding.isRemote) + pin
            + ["-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
               "-o", "LogLevel=DEBUG1", "-R", "127.0.0.1:\(guestPort):127.0.0.1:\(macPort)", "\(user)@\(t.binding.sshDestination)"]
        let errFD = open(errPath, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0o600)
        let nullFD = open("/dev/null", O_RDWR | O_CLOEXEC)
        guard errFD >= 0, nullFD >= 0 else { throw VaultError("cannot start the tunnel", code: Exit.error) }
        var fa: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&fa)
        defer { posix_spawn_file_actions_destroy(&fa) }
        posix_spawn_file_actions_adddup2(&fa, nullFD, 0)
        posix_spawn_file_actions_adddup2(&fa, nullFD, 1)
        posix_spawn_file_actions_adddup2(&fa, errFD, 2)
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETPGROUP))
        var defaults = sigset_t()
        sigemptyset(&defaults)
        for s in [SIGINT, SIGTERM, SIGHUP, SIGPIPE, SIGQUIT] { sigaddset(&defaults, s) }
        posix_spawnattr_setsigdefault(&attr, &defaults)
        var mask = sigset_t()
        sigemptyset(&mask)
        posix_spawnattr_setsigmask(&attr, &mask)
        posix_spawnattr_setpgroup(&attr, 0)
        let argv = ([sshPath] + args).map { strdup($0) } + [nil]
        let envp = helperEnv().map { strdup($0) } + [nil]
        defer {
            for p in argv { free(p) }
            for p in envp { free(p) }
        }
        var pid: pid_t = 0
        let rc = posix_spawn(&pid, sshPath, &fa, &attr, argv, envp)
        close(errFD)
        close(nullFD)
        guard rc == 0 else { throw VaultError("cannot run ssh for the tunnel", code: Exit.error) }
        let tunnel = Tunnel(pid: pid, dir: dir)
        for _ in 0..<100 {
            let err = String(decoding: (try? readFileBytes(errPath, max: 1 << 20)) ?? [], as: UTF8.self)
            if err.contains("remote forward success") { return tunnel }
            if !tunnel.alive {
                tunnel.stop()
                if err.contains("Permission denied") {
                    throw VaultError("the VM refused the proxy account (\(user)): VMs built before api tokens have none; rebuild the VM to use them", code: Exit.error)
                }
                if err.contains("remote port forwarding failed") {
                    throw VaultError("the VM refused the forward to 127.0.0.1:\(guestPort) (in use, or not the port its proxy account allows)", code: Exit.error)
                }
                // The rest of ssh's error output can carry the VM's own words (its sshd's messages): not passed on.
                throw VaultError("the tunnel to the VM failed (ssh's own output isn't passed on)", code: Exit.error)
            }
            usleep(100_000)
        }
        tunnel.stop()
        throw VaultError("the tunnel to the VM didn't come up in 10 s", code: Exit.error)
    }

    func shutdown() {
        let ls = withLock { () -> [ProxyListener] in
            closed = true
            tokens = [:]
            return Array(listeners.values)
        }
        for l in ls {
            shutdownSocket(l.fd)
            close(l.fd)
            l.tunnel?.stop()
        }
    }

    // MARK: serving

    func acceptLoop(_ l: ProxyListener) {
        while !withLock({ closed }) {
            let c = accept(l.fd, nil, nil)
            if c < 0 {
                if errno == EINTR { continue }
                return
            }
            _ = fcntl(c, F_SETFD, FD_CLOEXEC)
            var one: Int32 = 1
            setsockopt(c, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
            // This VM's slots, never another's. With all of them taken (by connections that drip
            // their heads, say, each of which the head deadline ends), wait a while, then refuse.
            guard l.slots.wait(timeout: .now() + slotWait) == .success else {
                refuseBusy(c)
                continue
            }
            if withLock({ closed }) {
                l.slots.signal()
                close(c)
                return
            }
            let hub = self
            Thread.detachNewThread {
                hub.serve(c, l)
                close(c)
                l.slots.signal()
            }
        }
    }

    /// 503 for a connection over its VM's limit: the answer, then what the client already sent is
    /// read and dropped for at most 0.3 s (closing with it unread would reset the connection and
    /// lose the answer), then the socket is closed.
    func refuseBusy(_ c: Int32) {
        setTimeouts(c, seconds: 1)
        respond(c, 503, "too many connections from this VM at once; try again shortly")
        Darwin.shutdown(c, SHUT_WR)
        var buf = [UInt8](repeating: 0, count: 4096)
        let until = Date().addingTimeInterval(0.3)
        var total = 0
        while total < 65536 {
            let n = readBefore(c, &buf, until)
            if n <= 0 { break }
            total += n
        }
        close(c)
    }

    func respond(_ c: Int32, _ status: Int, _ message: String) {
        let body = Array((jsonLine(["error": message]) + "\n").utf8)
        let head = "HTTP/1.1 \(status) \(reason(status))\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        _ = writeAll(c, Array(head.utf8) + body)
    }

    func serve(_ c: Int32, _ l: ProxyListener) {
        setTimeouts(c, seconds: 30)
        // The head, within an absolute deadline; the token is checked before any body is read.
        let req: HTTPHead
        switch readHTTPHead(c, deadline: Date().addingTimeInterval(headDeadline)) {
        case .ok(let r): req = r
        case .bad(let code, let why): respond(c, code, why); return
        }
        // /<NAME>/<path>
        var target = req.target
        var query = ""
        if let q = target.firstIndex(of: "?") {
            query = String(target[q...])
            target = String(target[..<q])
        }
        let parts = target.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)
        guard target.hasPrefix("/"), parts.count >= 2 else { respond(c, 404, "use /<NAME>/<path> (metacomputer secrets token NAME)"); return }
        let name = String(parts[1].split(separator: "/", maxSplits: 1).first ?? "")
        let rest = "/" + (parts[1].split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false).dropFirst().first.map(String.init) ?? "")
        guard validName(name) else { respond(c, 404, "no such secret"); return }
        // What the VM sent (the secret's name it asked for, the method, the path) is the VM's to choose, and the VM knows
        // every value it was ever given: the audit log and its sinks get only this process's own words for it. The name
        // only once it's a stored api secret's, the method as one of the usual few, and the path not at all, only the
        // prefix it's within when the secret limits paths (the secret's own setting).
        var known: String? = nil
        let methodWord = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"].contains(req.method) ? req.method : "other"
        func pathFields(_ token: APIToken?) -> [String: Any] {
            var d: [String: Any] = [:]
            if let pre = token?.prefix { d["path_within"] = pre }
            return d
        }
        // The numbers the VM can steer are kept to a few values too: the answer's size as one of eight (sizeBound), whether
        // the key was withheld (not how often), and an answer the API gave by its status class ("2xx"): an exact count or
        // status could spell a value the VM was given, a few bytes per request. This process's own refusals keep theirs.
        func log(_ status: Int, _ bytes: Int, _ token: APIToken?, _ host: String, _ why: String? = nil, redacted: Int = 0,
                 upstream: Bool = false) {
            var rec: [String: Any] = ["op": "api", "vm": l.vm, "method": methodWord, "host": host,
                                      "status": upstream ? "\(min(9, max(1, status / 100)))xx" as Any : status as Any,
                                      "bytes": sizeBound(bytes),
                                      "result": (200..<400).contains(status) ? "ok" : (status == 401 || status == 403 ? "denied" : "error")]
            rec.merge(pathFields(token)) { a, _ in a }
            if let n = known { rec["name"] = n }
            if let t = token { rec["token_id"] = t.id; rec["requester"] = t.requester }
            if let w = why { rec["reason"] = w }
            if redacted > 0 { rec["key_withheld"] = true }
            if let r = recorder { _ = r(rec) } else { audit(paths, rec) }
        }
        guard let p = try? loadPayload(paths, key), let s = p.secrets[name], s.kind == "api", let host = s.api_host,
              let template = s.api_header, let (hName, hPrefix, hSuffix) = splitTemplate(template) else {
            respond(c, 404, "no api secret named \(name)")
            log(404, 0, nil, "-", "no api secret")
            return
        }
        known = name
        // The token, from the secret's own header or X-Metacomputer-Token.
        var presented: String?
        if let v = req.header(hName), v.hasPrefix(hPrefix), v.hasSuffix(hSuffix), v.count > hPrefix.count + hSuffix.count {
            presented = String(v.dropFirst(hPrefix.count).dropLast(hSuffix.count))
        }
        if presented == nil { presented = req.header("X-Metacomputer-Token") }
        guard let raw = presented, let token = check(raw, name: name, vm: l.vm) else {
            respond(c, 401, "no live token for \(name) on this VM (metacomputer secrets token \(name))")
            log(401, 0, nil, host, "no live token")
            return
        }
        // The token covers the secret as it was approved: not a newer value, host or scope.
        guard token.fits(s) else {
            _ = revoke(token.id)
            respond(c, 401, "\(name) changed since this token was made (set --replace or delete); get a new one")
            log(401, 0, token, host, "the secret changed since the token was made")
            return
        }
        let segments = rest.split(separator: "/").map { $0.removingPercentEncoding ?? "/" }
        guard !segments.contains(where: { $0 == ".." || $0 == "." || $0.contains("/") || $0.contains("\\") }),
              token.prefix.map({ pathWithin(rest, prefix: $0) }) ?? true else {
            respond(c, 403, "that path isn't allowed for \(name)")
            log(403, 0, token, host, "path not allowed")
            return
        }
        // Only now, with a live token for this VM, the body (within its own deadline).
        let body: [UInt8]
        switch readHTTPBody(c, req, deadline: Date().addingTimeInterval(bodyDeadline)) {
        case .ok(let b): body = b
        case .bad(let code, let why):
            respond(c, code, why)
            log(code, 0, token, host, why)
            return
        }
        // With a required audit sink, this request's own record must reach it first.
        if let gate = preUse, let why = gate(["name": name, "vm": l.vm, "method": methodWord, "host": host, "token_id": token.id]
                                             .merging(pathFields(token)) { a, _ in a }, token.requester) {
            respond(c, 503, why)
            log(503, 0, token, host, "required audit sink: \(why)")
            return
        }
        // Waiting on delivery takes time: just before forwarding, the token must still be live
        // (not revoked, not expired, the vault not locking) and the secret still the one it was
        // made for. The value used is the one read now.
        guard stillLive(token) else {
            let shutting = withLock { closed }
            respond(c, shutting ? 503 : 401, shutting ? "the vault is locking" : "the token ended while this request waited (expired or revoked)")
            log(shutting ? 503 : 401, 0, token, host, "token ended while waiting")
            return
        }
        guard let p2 = try? loadPayload(paths, key), let s2 = p2.secrets[name], token.fits(s2) else {
            _ = revoke(token.id)
            respond(c, 401, "\(name) changed while this request waited; get a new token")
            log(401, 0, token, host, "the secret changed while waiting")
            return
        }
        guard let url = URL(string: "\(upstreamScheme)://\(host)\(rest)\(query)") else { respond(c, 400, "bad path"); return }
        var u = URLRequest(url: url)
        u.httpMethod = req.method
        let dropped: Set<String> = ["connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te",
                                    "trailer", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding", "expect",
                                    "x-metacomputer-token", hName.lowercased()]
        let named = Set((req.header("Connection") ?? "").split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces).lowercased() })
        for (k, v) in req.headers where !dropped.contains(k.lowercased()) && !named.contains(k.lowercased()) { u.addValue(v, forHTTPHeaderField: k) }
        u.setValue(hPrefix + s2.value + hSuffix, forHTTPHeaderField: hName)
        if !body.isEmpty { u.httpBody = Data(body) }
        let stream = StreamBack(fd: c, upstreamHost: host, scheme: upstreamScheme,
                                proxyBase: "http://127.0.0.1:\(l.guestPort)/\(name)", secret: Array(s2.value.utf8))
        // The answer's time limit is the secret's own (--answer-timeout) or the default. The HTTP client's own limit for
        // the whole answer is set past it, so this one decides; 120 s without a byte from the API still ends it.
        let limit = answerLimit(s2)
        let cfg = plainSessionConfig(timeout: 120)
        cfg.timeoutIntervalForResource = limit + 30
        let session = URLSession(configuration: cfg, delegate: stream, delegateQueue: nil)
        // Writes to the VM's connection give up at the answer's deadline too: a VM that reads slowly can't stretch it.
        let deadline = Date().addingTimeInterval(limit)
        stream.writeDeadline = deadline
        session.dataTask(with: u).resume()
        // Wait for the answer in short steps: it ends with its token (revoked, expired without a renewal, its secret
        // replaced or deleted, the vault locking) or at its time limit, whichever comes first.
        var cut: (code: Int, why: String)? = nil
        while stream.done.wait(timeout: .now() + 0.25) == .timedOut {
            refreshIfVaultChanged()
            if !stillLive(token) {
                cut = withLock({ closed }) ? (503, "the vault locked during the answer; cancelled")
                    : (401, "the token ended during the answer (revoked, expired without a renewal, or its secret changed); cancelled")
                break
            }
            if Date() >= deadline {
                cut = (504, "the answer took over \(Int(limit)) s; cancelled")
                break
            }
        }
        if let c = cut {
            if stream.failure == nil { stream.failure = c.why }
            // Say why if nothing of the answer went back yet, then shut the connection: that also ends at once a write
            // the VM holds up by not reading.
            stream.cutOff(c.code, c.why)
            session.invalidateAndCancel()
            // Give the cancelled task a moment to report its end (didCompleteWithError).
            _ = stream.done.wait(timeout: .now() + 5)
        } else {
            session.invalidateAndCancel()
        }
        // Whatever it does, no callback writes to this connection from here on: the caller
        // closes the descriptor next, and its number could soon belong to another connection.
        stream.closeOutput()
        log(stream.status, stream.bytes, token, host, stream.failure, redacted: stream.redactions, upstream: true)
    }
}

/// Whether a request path is within an API secret's path prefix, by whole segments: the
/// prefix "/v1" (or "/v1/") allows /v1 and /v1/..., never /v10 or /v1-admin. A prefix of
/// "/" allows every path. Both are compared as sent (percent-encoded); encoded slashes and
/// dot segments are refused before this.
func pathWithin(_ path: String, prefix: String) -> Bool {
    var p = prefix
    while p.hasSuffix("/") { p.removeLast() }
    if p.isEmpty { return true }
    return path == p || path.hasPrefix(p + "/")
}

func shutdownSocket(_ fd: Int32) { Darwin.shutdown(fd, SHUT_RDWR) }

/// "Authorization: Bearer {value}" -> ("Authorization", "Bearer ", "").
func splitTemplate(_ t: String) -> (String, String, String)? {
    guard let c = t.firstIndex(of: ":") else { return nil }
    let name = String(t[..<c]).trimmingCharacters(in: .whitespaces)
    let v = String(t[t.index(after: c)...]).trimmingCharacters(in: .whitespaces)
    guard validHeaderName(name), let r = v.range(of: "{value}"), v.components(separatedBy: "{value}").count == 2 else { return nil }
    return (name, String(v[..<r.lowerBound]), String(v[r.upperBound...]))
}

func reason(_ status: Int) -> String {
    [200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently", 302: "Found", 304: "Not Modified", 307: "Temporary Redirect",
     308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 408: "Request Timeout",
     429: "Too Many Requests",
     500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"][status]
        ?? HTTPURLResponse.localizedString(forStatusCode: status).capitalized
}

/// A request's head: what the proxy needs to route and authenticate it before any body is read.
struct HTTPHead {
    let method: String
    let target: String
    let headers: [(String, String)]
    let contentLength: Int?     // checked: at most the body limit
    let chunked: Bool
    let early: [UInt8]          // bytes that arrived after the head (the start of the body)
    func header(_ name: String) -> String? { headers.first { $0.0.lowercased() == name.lowercased() }?.1 }
}

/// A request the proxy will take, or why not (an HTTP status and a reason).
enum Parsed<T> {
    case ok(T)
    case bad(Int, String)
}

let requestHeadLimit = 65536
let requestBodyLimit = 32 << 20

/// read(2) that gives up at an absolute deadline however slowly bytes trickle in: each read
/// waits at most until the deadline (and at most 30 s). -1 once the deadline has passed.
func readBefore(_ c: Int32, _ buf: inout [UInt8], _ deadline: Date) -> Int {
    let left = deadline.timeIntervalSinceNow
    guard left > 0 else { return -1 }
    let wait = min(left, 30)
    var tv = timeval(tv_sec: Int(wait), tv_usec: Int32((wait - wait.rounded(.down)) * 1_000_000))
    if tv.tv_sec == 0 && tv.tv_usec < 1000 { tv.tv_usec = 1000 }
    setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    while true {
        let n = read(c, &buf, buf.count)
        if n < 0 && errno == EINTR && deadline.timeIntervalSinceNow > 0 { continue }
        return n
    }
}

/// One HTTP/1.x request head, strictly: origin-form targets only (no absolute URIs, no CONNECT
/// or TRACE), CRLF line ends only, no folded or control-character headers, Content-Length or
/// chunked but never both, at most 64 KB, all of it before the deadline (408 after). Every size
/// is bounded before it's added, so no arithmetic can overflow.
func readHTTPHead(_ c: Int32, deadline: Date) -> Parsed<HTTPHead> {
    var buf = [UInt8]()
    var chunk = [UInt8](repeating: 0, count: 16384)
    var headEnd: Int?
    while headEnd == nil {
        let n = readBefore(c, &chunk, deadline)
        if n <= 0 {
            return deadline.timeIntervalSinceNow <= 0 ? .bad(408, "the request head didn't arrive in time") : .bad(400, "incomplete request")
        }
        let searchFrom = max(0, buf.count - 3)
        buf.append(contentsOf: chunk[0..<n])
        headEnd = findSubsequence(buf, [13, 10, 13, 10], from: searchFrom)
        if headEnd == nil && buf.count > requestHeadLimit { return .bad(431, "request head too large") }
    }
    guard let he = headEnd, he <= requestHeadLimit else { return .bad(431, "request head too large") }
    guard let head = String(bytes: buf[0..<he], encoding: .utf8) else { return .bad(400, "request head isn't UTF-8") }
    let lines = head.components(separatedBy: "\r\n")
    if lines.contains(where: { $0.contains("\r") || $0.contains("\n") }) { return .bad(400, "bare CR or LF in the request head") }
    let first = lines[0].components(separatedBy: " ")
    guard first.count == 3 else { return .bad(400, "malformed request line") }
    let (method, target, version) = (first[0], first[1], first[2])
    guard matches(method, "^[A-Z]{1,16}$") else { return .bad(400, "malformed method") }
    if method == "CONNECT" || method == "TRACE" { return .bad(405, "\(method) isn't allowed") }
    guard version == "HTTP/1.1" || version == "HTTP/1.0" else { return .bad(505, "HTTP/1.0 or 1.1 only") }
    guard target.hasPrefix("/"), target.unicodeScalars.allSatisfy({ $0.value > 0x20 && $0.value < 0x7F }) else {
        return .bad(400, "the target must be a path (/NAME/...), not an absolute URI")
    }
    var headers: [(String, String)] = []
    for l in lines.dropFirst() {
        if l.hasPrefix(" ") || l.hasPrefix("\t") { return .bad(400, "folded header lines aren't allowed") }
        guard let colon = l.firstIndex(of: ":") else { return .bad(400, "malformed header line") }
        let k = String(l[..<colon])
        let v = String(l[l.index(after: colon)...]).trimmingCharacters(in: CharacterSet(charactersIn: " \t"))
        guard validHeaderName(k) else { return .bad(400, "malformed header name") }
        if v.unicodeScalars.contains(where: { ($0.value < 0x20 && $0.value != 0x09) || $0.value == 0x7F }) {
            return .bad(400, "control characters in a header value")
        }
        headers.append((k, v))
    }
    let cl = headers.filter { $0.0.lowercased() == "content-length" }.map { $0.1 }
    let te = headers.filter { $0.0.lowercased() == "transfer-encoding" }.map { $0.1 }
    if !cl.isEmpty && !te.isEmpty { return .bad(400, "both Content-Length and Transfer-Encoding") }
    if cl.count > 1 { return .bad(400, "more than one Content-Length") }
    if te.count > 1 || (te.count == 1 && te[0].lowercased() != "chunked") { return .bad(501, "only Transfer-Encoding: chunked") }
    var length: Int?
    if let c0 = cl.first {
        guard matches(c0, "^[0-9]{1,10}$"), let n = Int(c0) else { return .bad(400, "malformed Content-Length") }
        guard n <= requestBodyLimit else { return .bad(413, "request body too large") }
        length = n
    }
    return .ok(HTTPHead(method: method, target: target, headers: headers, contentLength: length, chunked: te.count == 1,
                        early: Array(buf[(he + 4)...])))
}

/// The body that follows a head: Content-Length or chunked, at most 32 MB, all of it before the
/// deadline (408 after).
func readHTTPBody(_ c: Int32, _ h: HTTPHead, deadline: Date) -> Parsed<[UInt8]> {
    var rest = h.early
    var chunk = [UInt8](repeating: 0, count: 16384)
    var late = false
    // Read until `want` bytes are buffered; want is always bounded by the limits above.
    func fill(_ want: Int) -> Bool {
        while rest.count < want {
            let n = readBefore(c, &chunk, deadline)
            if n <= 0 {
                late = deadline.timeIntervalSinceNow <= 0
                return false
            }
            rest.append(contentsOf: chunk[0..<n])
            if rest.count > requestBodyLimit + (4 << 20) { return false }   // the body plus chunk framing, at most
        }
        return true
    }
    func incomplete(_ why: String) -> Parsed<[UInt8]> { late ? .bad(408, "the request body didn't arrive in time") : .bad(400, why) }
    var body = [UInt8]()
    if h.chunked {
        var pos = 0
        // One CRLF-ended line starting at pos, of at most 1 KB.
        func line() -> String?? {
            while true {
                if let e = findSubsequence(rest, [13, 10], from: pos) {
                    guard e - pos <= 1024, let s = String(bytes: rest[pos..<e], encoding: .utf8) else { return .some(nil) }
                    pos = e + 2
                    return .some(s)
                }
                if rest.count - pos > 1024 { return .some(nil) }
                if !fill(rest.count + 1) { return nil }
            }
        }
        while true {
            guard let l = line() else { return incomplete("incomplete chunked body") }
            guard let text = l, let sizeText = text.split(separator: ";", maxSplits: 1, omittingEmptySubsequences: false).first,
                  matches(String(sizeText), "^[0-9A-Fa-f]{1,8}$"), let size = Int(sizeText, radix: 16) else {
                return .bad(400, "malformed chunk size")
            }
            guard size <= requestBodyLimit - body.count else { return .bad(413, "request body too large") }
            if size == 0 {
                // Trailers, up to the empty line that ends the body.
                for _ in 0..<64 {
                    guard let t = line() else { return incomplete("incomplete chunked body") }
                    guard let tt = t else { return .bad(400, "malformed trailer") }
                    if tt.isEmpty { return .ok(body) }
                }
                return .bad(400, "too many trailers")
            }
            guard fill(pos + size + 2) else { return incomplete("incomplete chunked body") }
            guard rest[pos + size] == 13, rest[pos + size + 1] == 10 else { return .bad(400, "a chunk isn't followed by CRLF") }
            body.append(contentsOf: rest[pos..<(pos + size)])
            pos += size + 2
        }
    } else if let n = h.contentLength {
        guard fill(n) else { return incomplete("incomplete body") }
        body = Array(rest[0..<n])
    }
    return .ok(body)
}

/// The first place `needle` occurs in `hay` at or after `from`.
func findSubsequence(_ hay: [UInt8], _ needle: [UInt8], from: Int = 0) -> Int? {
    guard !needle.isEmpty, hay.count >= needle.count, from <= hay.count - needle.count else { return nil }
    var i = max(0, from)
    while i <= hay.count - needle.count {
        if hay[i] == needle[0] {
            var j = 1
            while j < needle.count && hay[i + j] == needle[j] { j += 1 }
            if j == needle.count { return i }
        }
        i += 1
    }
    return nil
}

/// Streams the upstream answer back to the VM's connection.
final class StreamBack: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    let fd: Int32
    let upstreamHost: String
    let scheme: String
    let proxyBase: String
    let done = DispatchSemaphore(value: 0)
    var status = 502
    var bytes = 0
    var failure: String?
    var headSent = false
    /// An API that echoes what it got (httpbin, debug endpoints) would hand the real key to the VM:
    /// every exact copy of it in the answer's headers and body is blanked on the way back.
    var secret: [UInt8]
    var carry: [UInt8] = []
    var redactions = 0
    static let marker = Array("[key withheld by metacomputer secrets]".utf8)
    /// Every write to the connection holds this lock and checks `outputClosed` first. serve()
    /// sets it (closeOutput) before it returns and the descriptor is closed, so a callback that
    /// runs late (after a timeout and cancel) can never write to a reused descriptor number.
    let writeLock = NSLock()
    var outputClosed = false
    /// Writes give up here (the answer's deadline), however slowly the VM reads.
    var writeDeadline = Date.distantFuture

    init(fd: Int32, upstreamHost: String, scheme: String, proxyBase: String, secret: [UInt8]) {
        self.fd = fd
        self.upstreamHost = upstreamHost
        self.scheme = scheme
        self.proxyBase = proxyBase
        self.secret = secret
    }

    deinit { wipe(&secret); wipe(&carry) }

    /// Write to the VM's connection unless serve() is done with it. False when nothing more
    /// should be written.
    func send(_ bytes: [UInt8]) -> Bool {
        writeLock.lock()
        defer { writeLock.unlock() }
        guard !outputClosed else { return false }
        return writeAllBefore(fd, bytes, writeDeadline)
    }

    /// The answer is cut: if nothing of it went back yet and no write is under way, the reason goes back (within a
    /// second); then the connection is shut both ways, which ends at once a write in progress that the VM holds up by
    /// not reading, and no write reaches it afterwards.
    func cutOff(_ code: Int, _ message: String) {
        if writeLock.try() {
            if !outputClosed && !headSent {
                status = code
                headSent = true
                let body = Array((jsonLine(["error": message]) + "\n").utf8)
                _ = writeAllBefore(fd, Array("HTTP/1.1 \(code) \(reason(code))\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8) + body,
                                   Date().addingTimeInterval(1))
            }
            outputClosed = true
            writeLock.unlock()
        }
        Darwin.shutdown(fd, SHUT_RDWR)
    }

    /// No write reaches the connection after this returns (one in progress finishes first).
    func closeOutput() {
        writeLock.lock()
        outputClosed = true
        writeLock.unlock()
    }

    /// Replace each copy of the secret; with `final` false, keep back a tail that could start one.
    func redact(_ input: [UInt8], final: Bool) -> [UInt8] {
        var buf = carry + input
        wipe(&carry)
        carry = []
        guard !secret.isEmpty else { return buf }
        var out = [UInt8]()
        out.reserveCapacity(buf.count)
        var i = 0
        let n = secret.count
        while i < buf.count {
            if buf[i] == secret[0], i + n <= buf.count, buf[i..<(i + n)].elementsEqual(secret) {
                out += StreamBack.marker
                redactions += 1
                i += n
                continue
            }
            if !final, buf.count - i < n, secret.starts(with: buf[i...]) {
                carry = Array(buf[i...])
                break
            }
            out.append(buf[i])
            i += 1
        }
        wipe(&buf)
        return out
    }

    func redactText(_ s: String) -> String {
        guard !secret.isEmpty, let v = String(bytes: secret, encoding: .utf8), s.contains(v) else { return s }
        redactions += 1
        return s.replacingOccurrences(of: v, with: String(decoding: StreamBack.marker, as: UTF8.self))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)   // never followed here; the 3xx comes back as the answer
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let h = response as? HTTPURLResponse else { completionHandler(.cancel); return }
        var location: String?
        if (300..<400).contains(h.statusCode), let loc = h.value(forHTTPHeaderField: "Location") {
            let abs = URL(string: loc, relativeTo: h.url)?.absoluteURL
            let sameHost = abs.map { $0.scheme == scheme && ($0.host ?? "") + ($0.port.map { ":\($0)" } ?? "") == upstreamHost } ?? false
            guard sameHost, let a = abs else {
                failure = "redirect to another host refused"
                writeError(502, "the API redirected to another host; refused")
                completionHandler(.cancel)
                return
            }
            location = proxyBase + sameHostLocation(a)
        }
        status = h.statusCode
        let skip: Set<String> = ["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer",
                                 "content-length", "content-encoding", "location"]
        var head = "HTTP/1.1 \(h.statusCode) \(reason(h.statusCode))\r\n"
        for (k, v) in h.allHeaderFields {
            guard let k = k as? String, let v = v as? String, !skip.contains(k.lowercased()), !v.contains("\r"), !v.contains("\n") else { continue }
            head += "\(k): \(redactText(v))\r\n"
        }
        if let l = location { head += "Location: \(redactText(l))\r\n" }
        head += "Connection: close\r\n\r\n"
        headSent = true
        if !send(Array(head.utf8)) { completionHandler(.cancel); return }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        bytes += data.count
        var chunk = redact(Array(data), final: false)
        defer { wipe(&chunk) }
        if !chunk.isEmpty, !send(chunk) { dataTask.cancel() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if headSent && !carry.isEmpty {
            var tail = redact([], final: true)
            _ = send(tail)
            wipe(&tail)
        }
        if let e = error, failure == nil {
            // The system's description can name the URL, whose path is the VM's: only the error's code.
            failure = "the API couldn't be reached (URL error \((e as NSError).code))"
            if !headSent { writeError(502, failure!) }
        }
        done.signal()
    }

    func writeError(_ code: Int, _ message: String) {
        status = code
        headSent = true
        let body = Array((jsonLine(["error": message]) + "\n").utf8)
        _ = send(Array("HTTP/1.1 \(code) \(reason(code))\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8) + body)
    }
}

/// The path, query and fragment of a same-host redirect's target, exactly as the API encoded
/// them: percent-escapes (an encoded slash stays %2F) and a trailing slash are kept, which
/// URL.path would decode or drop and so point somewhere else.
func sameHostLocation(_ a: URL) -> String {
    let c = URLComponents(url: a, resolvingAgainstBaseURL: true)
    var path = c?.percentEncodedPath ?? ""
    if path.isEmpty { path = "/" }
    if !path.hasPrefix("/") { path = "/" + path }
    return path + (c?.percentEncodedQuery.map { "?" + $0 } ?? "") + (c?.percentEncodedFragment.map { "#" + $0 } ?? "")
}
