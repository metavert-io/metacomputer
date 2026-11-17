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
// revoke, or when the vault locks (this process ends).
import Darwin
import Foundation

struct APIToken {
    let id: String
    let hash: String            // sha256 of the token; the token itself isn't kept
    let name: String
    let vm: String
    let issued: Date
    let expires: Date
    let requester: [String: Any]
    var requests: Int
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
    init(vm: String, fd: Int32, macPort: Int, guestPort: Int) {
        self.vm = vm
        self.fd = fd
        self.macPort = macPort
        self.guestPort = guestPort
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
    let slots = DispatchSemaphore(value: 32)    // connections at once

    init(paths: Paths, key: VaultKey, sshPath: String) {
        self.paths = paths
        self.key = key
        self.sshPath = sshPath
        var scheme = "https"
        #if MCOMP_VAULT_TESTING
        // Test builds only: plain http to a local stand-in API (no certificate to check).
        if ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_API_HTTP"] == "1" { scheme = "http" }
        #endif
        upstreamScheme = scheme
    }

    func withLock<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    // MARK: tokens

    func issue(name: String, vm: String, ttl: Int, requester: [String: Any]) -> (APIToken, String) {
        let raw = "mct_" + base64url(randomBytes(24))
        let t = APIToken(id: randomBytes(4).map { String(format: "%02x", $0) }.joined(), hash: sha256Hex(Array(raw.utf8)), name: name, vm: vm,
                         issued: Date(), expires: Date().addingTimeInterval(TimeInterval(ttl)), requester: requester, requests: 0)
        withLock { tokens[t.hash] = t }
        return (t, raw)
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
            guard var t = tokens[sha256Hex(Array(raw.utf8))], t.name == name, t.vm == vm, t.expires > Date() else { return nil }
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
        return ProxyListener(vm: vm, fd: fd, macPort: Int(UInt16(bigEndian: got.sin_port)), guestPort: guestPort)
    }

    /// ssh -N -R 127.0.0.1:<guest port>:127.0.0.1:<mac port> as the proxy account, with the
    /// pinned port, host key and key. Waits until the forward is up, or says why not.
    func startTunnel(_ t: GuestTarget, user: String, keyPath: String, guestPort: Int, macPort: Int) throws -> Tunnel {
        let dir = userTempDir() + "/mcomp-tunnel-\(getpid())-\(UInt32.random(in: 0...UInt32.max))"
        guard mkdir(dir, 0o700) == 0 else { throw VaultError("cannot make a temporary folder", code: Exit.error) }
        let kh = dir + "/known_hosts", errPath = dir + "/ssh.err"
        try writeFileAtomic(kh, Array("[127.0.0.1]:\(t.binding.ssh_port) \(t.binding.host_key_line)\n".utf8))
        let args = ["-F", "/dev/null", "-N", "-T",
                    "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityFile=\(keyPath)",
                    "-o", "UserKnownHostsFile=\(kh)", "-o", "StrictHostKeyChecking=yes", "-o", "GlobalKnownHostsFile=/dev/null",
                    "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                    "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "PasswordAuthentication=no",
                    "-o", "KbdInteractiveAuthentication=no", "-o", "ConnectTimeout=5", "-o", "ExitOnForwardFailure=yes",
                    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "LogLevel=DEBUG1",
                    "-R", "127.0.0.1:\(guestPort):127.0.0.1:\(macPort)",
                    "-p", "\(t.binding.ssh_port)", "\(user)@127.0.0.1"]
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
                let last = err.split(separator: "\n").last { !$0.hasPrefix("debug") }.map(String.init) ?? "exit"
                throw VaultError("the tunnel to the VM failed: \(oneLine(last, max: 160))", code: Exit.error)
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
            slots.wait()
            let hub = self
            Thread.detachNewThread {
                hub.serve(c, l)
                close(c)
                hub.slots.signal()
            }
        }
    }

    func respond(_ c: Int32, _ status: Int, _ message: String) {
        let body = Array((jsonLine(["error": message]) + "\n").utf8)
        let head = "HTTP/1.1 \(status) \(reason(status))\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        _ = writeAll(c, Array(head.utf8) + body)
    }

    func serve(_ c: Int32, _ l: ProxyListener) {
        setTimeouts(c, seconds: 30)
        guard let req = readHTTPRequest(c) else { respond(c, 400, "bad request"); return }
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
        func log(_ status: Int, _ bytes: Int, _ token: APIToken?, _ host: String, _ why: String? = nil, redacted: Int = 0) {
            var rec: [String: Any] = ["op": "api", "name": name, "vm": l.vm, "method": clean(req.method, max: 16), "host": host,
                                      "path": clean(rest, max: 200), "status": status, "bytes": bytes,
                                      "result": (200..<400).contains(status) ? "ok" : (status == 401 || status == 403 ? "denied" : "error")]
            if let t = token { rec["token_id"] = t.id; rec["requester"] = t.requester }
            if let w = why { rec["reason"] = w }
            if redacted > 0 { rec["key_withheld"] = redacted }
            audit(paths, rec)
        }
        guard let p = try? loadPayload(paths, key), let s = p.secrets[name], s.kind == "api", let host = s.api_host,
              let template = s.api_header, let (hName, hPrefix, hSuffix) = splitTemplate(template) else {
            respond(c, 404, "no api secret named \(name)")
            log(404, 0, nil, "-", "no api secret")
            return
        }
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
        // A required audit sink whose last delivery failed stops requests too (as it stops fills).
        for (n, sk) in (p.audit_sinks ?? [:]) where sk.required && loadSinkState(paths, n, sk).error != nil {
            respond(c, 503, "the required audit sink \(n) can't be reached; requests are refused until it can")
            log(503, 0, token, host, "required audit sink down")
            return
        }
        let segments = rest.split(separator: "/").map { $0.removingPercentEncoding ?? "/" }
        guard !segments.contains(where: { $0 == ".." || $0 == "." || $0.contains("/") || $0.contains("\\") }),
              s.api_path_prefix.map({ rest.hasPrefix($0) }) ?? true else {
            respond(c, 403, "that path isn't allowed for \(name)")
            log(403, 0, token, host, "path not allowed")
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
        u.setValue(hPrefix + s.value + hSuffix, forHTTPHeaderField: hName)
        if !req.body.isEmpty { u.httpBody = Data(req.body) }
        let stream = StreamBack(fd: c, upstreamHost: host, scheme: upstreamScheme,
                                proxyBase: "http://127.0.0.1:\(l.guestPort)/\(name)", secret: Array(s.value.utf8))
        let session = URLSession(configuration: plainSessionConfig(timeout: 120), delegate: stream, delegateQueue: nil)
        session.dataTask(with: u).resume()
        _ = stream.done.wait(timeout: .now() + 600)
        session.invalidateAndCancel()
        log(stream.status, stream.bytes, token, host, stream.failure, redacted: stream.redactions)
    }
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
     308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests",
     500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"][status]
        ?? HTTPURLResponse.localizedString(forStatusCode: status).capitalized
}

struct HTTPRequest {
    let method: String
    let target: String
    let headers: [(String, String)]
    let body: [UInt8]
    func header(_ name: String) -> String? { headers.first { $0.0.lowercased() == name.lowercased() }?.1 }
}

/// One HTTP/1.x request: head up to 64 KB, body by Content-Length or chunked, up to 32 MB.
func readHTTPRequest(_ c: Int32) -> HTTPRequest? {
    var buf = [UInt8]()
    var chunk = [UInt8](repeating: 0, count: 16384)
    var headEnd: Int?
    while headEnd == nil {
        let n = read(c, &chunk, chunk.count)
        if n <= 0 { return nil }
        buf.append(contentsOf: chunk[0..<n])
        if let r = findSubsequence(buf, [13, 10, 13, 10]) { headEnd = r }
        if buf.count > 65536 && headEnd == nil { return nil }
    }
    let head = String(decoding: buf[0..<headEnd!], as: UTF8.self)
    var rest = Array(buf[(headEnd! + 4)...])
    let lines = head.components(separatedBy: "\r\n")
    let first = lines[0].split(separator: " ")
    guard first.count == 3, first[2].hasPrefix("HTTP/1."), matches(String(first[0]), "^[A-Z]{1,16}$") else { return nil }
    var headers: [(String, String)] = []
    for l in lines.dropFirst() {
        guard let c = l.firstIndex(of: ":"), !l.hasPrefix(" "), !l.hasPrefix("\t") else { return nil }
        let k = String(l[..<c]), v = String(l[l.index(after: c)...]).trimmingCharacters(in: .whitespaces)
        guard validHeaderName(k) else { return nil }
        headers.append((k, v))
    }
    let req0 = HTTPRequest(method: String(first[0]), target: String(first[1]), headers: headers, body: [])
    let limit = 32 << 20
    func fill(_ want: Int) -> Bool {
        while rest.count < want {
            let n = read(c, &chunk, chunk.count)
            if n <= 0 { return false }
            rest.append(contentsOf: chunk[0..<n])
            if rest.count > limit + 65536 { return false }
        }
        return true
    }
    var body = [UInt8]()
    if (req0.header("Transfer-Encoding") ?? "").lowercased().contains("chunked") {
        var pos = 0
        while true {
            while findSubsequence(Array(rest[pos...]), [13, 10]) == nil { if !fill(rest.count + 1) { return nil } }
            let lineEnd = pos + findSubsequence(Array(rest[pos...]), [13, 10])!
            let sizeText = String(decoding: rest[pos..<lineEnd], as: UTF8.self).split(separator: ";").first.map(String.init) ?? ""
            guard let size = Int(sizeText.trimmingCharacters(in: .whitespaces), radix: 16), size >= 0, body.count + size <= limit else { return nil }
            pos = lineEnd + 2
            if size == 0 { break }
            if !fill(pos + size + 2) { return nil }
            body.append(contentsOf: rest[pos..<(pos + size)])
            pos += size + 2
        }
    } else if let cl = req0.header("Content-Length") {
        guard let n = Int(cl), n >= 0, n <= limit, fill(n) else { return nil }
        body = Array(rest[0..<n])
    }
    return HTTPRequest(method: req0.method, target: req0.target, headers: headers, body: body)
}

func findSubsequence(_ hay: [UInt8], _ needle: [UInt8]) -> Int? {
    guard hay.count >= needle.count else { return nil }
    for i in 0...(hay.count - needle.count) where hay[i] == needle[0] && Array(hay[i..<(i + needle.count)]) == needle { return i }
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

    init(fd: Int32, upstreamHost: String, scheme: String, proxyBase: String, secret: [UInt8]) {
        self.fd = fd
        self.upstreamHost = upstreamHost
        self.scheme = scheme
        self.proxyBase = proxyBase
        self.secret = secret
    }

    deinit { wipe(&secret); wipe(&carry) }

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
            location = proxyBase + a.path + (a.query.map { "?" + $0 } ?? "")
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
        if !writeAll(fd, Array(head.utf8)) { completionHandler(.cancel); return }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        bytes += data.count
        var chunk = redact(Array(data), final: false)
        defer { wipe(&chunk) }
        if !chunk.isEmpty, !writeAll(fd, chunk) { dataTask.cancel() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if headSent && !carry.isEmpty {
            var tail = redact([], final: true)
            _ = writeAll(fd, tail)
            wipe(&tail)
        }
        if let e = error, failure == nil {
            failure = oneLine(e.localizedDescription, max: 160)
            if !headSent { writeError(502, "the API couldn't be reached: \(failure!)") }
        }
        done.signal()
    }

    func writeError(_ code: Int, _ message: String) {
        status = code
        headSent = true
        let body = Array((jsonLine(["error": message]) + "\n").utf8)
        _ = writeAll(fd, Array("HTTP/1.1 \(code) \(reason(code))\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8) + body)
    }
}
