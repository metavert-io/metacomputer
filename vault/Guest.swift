// mcomp-vault: reaching the VM. Only the unlock process does this: it checks who
// listens on the pinned port, then runs /usr/bin/ssh with the pinned key, user and
// host key, and writes the request (with the value) to ssh's standard input.
// The value is never on a command line, in an environment variable, or on disk.
import Darwin
import Foundation

struct ProcResult {
    var status: Int32 = -1
    var stdout: [UInt8] = []
    var stderr: [UInt8] = []
    var timedOut = false
    var interrupted = false
    var tooLarge = false
}

/// Run a program with the given stdin bytes, collecting stdout and stderr. The child
/// gets only fds 0-2, default signal handling and its own process group (so Ctrl-C
/// in the unlock terminal reaches the vault, which then stops the child).
func runProcess(_ path: String, _ args: [String], env: [String], stdin input: [UInt8],
                timeout: TimeInterval, maxStdout: Int = 1 << 20) -> ProcResult {
    var res = ProcResult()
    var inPipe: [Int32] = [0, 0], outPipe: [Int32] = [0, 0], errPipe: [Int32] = [0, 0]
    guard pipe(&inPipe) == 0, pipe(&outPipe) == 0, pipe(&errPipe) == 0 else { return res }

    var fa: posix_spawn_file_actions_t?
    posix_spawn_file_actions_init(&fa)
    defer { posix_spawn_file_actions_destroy(&fa) }
    posix_spawn_file_actions_adddup2(&fa, inPipe[0], 0)
    posix_spawn_file_actions_adddup2(&fa, outPipe[1], 1)
    posix_spawn_file_actions_adddup2(&fa, errPipe[1], 2)

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

    let argv = ([path] + args).map { strdup($0) } + [nil]
    let envp = env.map { strdup($0) } + [nil]
    defer {
        for p in argv { free(p) }
        for p in envp { free(p) }
    }
    var pid: pid_t = 0
    let rc = posix_spawn(&pid, path, &fa, &attr, argv, envp)
    close(inPipe[0]); close(outPipe[1]); close(errPipe[1])
    guard rc == 0 else {
        close(inPipe[1]); close(outPipe[0]); close(errPipe[0])
        res.stderr = Array("cannot run \(path): \(String(cString: strerror(rc)))".utf8)
        return res
    }

    var inFD = inPipe[1]
    _ = fcntl(inFD, F_SETFL, fcntl(inFD, F_GETFL) | O_NONBLOCK)
    var written = 0
    if input.isEmpty { close(inFD); inFD = -1 }
    var outOpen = true, errOpen = true
    let deadline = Date().addingTimeInterval(timeout)
    var buf = [UInt8](repeating: 0, count: 1 << 16)
    while outOpen || errOpen {
        if receivedSignal != 0 { res.interrupted = true; break }
        if Date() > deadline { res.timedOut = true; break }
        var fds: [pollfd] = []
        if inFD >= 0 { fds.append(pollfd(fd: inFD, events: Int16(POLLOUT), revents: 0)) }
        if outOpen { fds.append(pollfd(fd: outPipe[0], events: Int16(POLLIN), revents: 0)) }
        if errOpen { fds.append(pollfd(fd: errPipe[0], events: Int16(POLLIN), revents: 0)) }
        let r = poll(&fds, nfds_t(fds.count), 250)
        if r < 0 { if errno == EINTR { continue }; break }
        for f in fds where f.revents != 0 {
            if f.fd == inFD {
                let n = input.withUnsafeBytes { p in write(inFD, p.baseAddress! + written, min(input.count - written, 1 << 16)) }
                if n > 0 { written += n }
                if (n < 0 && errno != EAGAIN && errno != EINTR) || written >= input.count {
                    close(inFD); inFD = -1
                }
            } else {
                let n = read(f.fd, &buf, buf.count)
                if n > 0 {
                    if f.fd == outPipe[0] {
                        res.stdout.append(contentsOf: buf[0..<n])
                        if res.stdout.count > maxStdout { res.tooLarge = true; outOpen = false; errOpen = false }
                    } else if res.stderr.count < 65536 {
                        res.stderr.append(contentsOf: buf[0..<n])
                    }
                } else if n == 0 || (errno != EINTR && errno != EAGAIN) {
                    if f.fd == outPipe[0] { outOpen = false } else { errOpen = false }
                }
            }
        }
    }
    if inFD >= 0 { close(inFD) }
    close(outPipe[0]); close(errPipe[0])
    wipe(&buf)
    if res.timedOut || res.interrupted || res.tooLarge { kill(pid, SIGKILL) }
    var status: Int32 = 0
    while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
    if (status & 0x7F) == 0 { res.status = (status >> 8) & 0xFF } else { res.status = 128 + (status & 0x7F) }
    return res
}

/// A minimal environment for helpers: nothing of the caller's (no SSH_AUTH_SOCK).
func helperEnv() -> [String] {
    ["PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=\(homeDirectory())", "LANG=C", "LC_ALL=C"]
}

// MARK: - who listens on the pinned port

func processPath(_ pid: Int32) -> String? {
    var buf = [CChar](repeating: 0, count: 4096)
    let n = proc_pidpath(pid, &buf, UInt32(buf.count))
    return n > 0 ? stringFromCBuffer(buf) : nil
}

/// The VM's SSH port on this Mac is a forward served by the gateway, cua-netgw
/// (scripts/lib.sh netgw_render). Anything else listening there is refused.
/// Returns nil when it's the gateway, else why not (and whether it's a refusal).
func checkListener(port: Int, gatewayPath: String?, allowedNames: [String]) -> (reason: String, denied: Bool)? {
    let r = runProcess("/usr/sbin/lsof", ["-nP", "-iTCP:\(port)", "-sTCP:LISTEN", "-Fpc"], env: helperEnv(), stdin: [], timeout: 15)
    var procs: [(pid: Int32, name: String)] = []
    var pid: Int32 = -1
    for line in String(decoding: r.stdout, as: UTF8.self).split(separator: "\n") {
        if line.hasPrefix("p") { pid = Int32(line.dropFirst()) ?? -1 }
        if line.hasPrefix("c") && pid > 0 { procs.append((pid, String(line.dropFirst()))) }
    }
    if procs.isEmpty {
        return ("nothing listens on 127.0.0.1:\(port): is the VM running? (metacomputer start)", false)
    }
    for p in procs {
        guard allowedNames.contains(p.name) else {
            return ("127.0.0.1:\(port) is served by '\(p.name)' (pid \(p.pid)), not the VM's gateway (\(allowedNames.joined(separator: ", "))); refusing", true)
        }
        if let want = gatewayPath, !want.isEmpty {
            let have = processPath(p.pid).map { URL(fileURLWithPath: $0).resolvingSymlinksInPath().path }
            let pinned = URL(fileURLWithPath: want).resolvingSymlinksInPath().path
            guard have == pinned else {
                return ("127.0.0.1:\(port) is served by \(have ?? "an unknown program"), not the pinned gateway \(pinned); refusing", true)
            }
        }
    }
    return nil
}

// MARK: - ssh

struct GuestTarget {
    let vm: String
    let binding: VMBinding
}

/// POSIX shell single quotes.
func shellQuote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

final class GuestRunner {
    let sshPath: String
    let listenerNames: [String]
    let checkGatewayPath: Bool

    init() {
        var ssh = "/usr/bin/ssh"
        var names = ["cua-netgw"]
        var gatewayPath = true
        #if MCOMP_VAULT_TESTING
        // Test builds only (scripts/build-vault.sh --test): a stub instead of ssh, and
        // other listener names. Release builds don't contain these lines.
        let env = ProcessInfo.processInfo.environment
        if let s = env["MCOMP_VAULT_TEST_SSH"], !s.isEmpty { ssh = s }
        if let l = env["MCOMP_VAULT_TEST_LISTENERS"], !l.isEmpty {
            names = l.split(separator: ",").map(String.init)
            gatewayPath = false
        }
        #endif
        sshPath = ssh
        listenerNames = names
        checkGatewayPath = gatewayPath
    }

    func listenerProblem(_ t: GuestTarget) -> (reason: String, denied: Bool)? {
        checkListener(port: t.binding.ssh_port, gatewayPath: checkGatewayPath ? t.binding.gateway_path : nil, allowedNames: listenerNames)
    }

    /// ssh to the pinned endpoint, running `remote`, with `input` on its stdin.
    func ssh(_ t: GuestTarget, remote: String, input: [UInt8], timeout: TimeInterval, maxStdout: Int = 1 << 20) throws -> ProcResult {
        let dir = userTempDir() + "/mcomp-vault-\(getpid())-\(UInt32.random(in: 0...UInt32.max))"
        guard mkdir(dir, 0o700) == 0 else { throw VaultError("cannot make a temporary folder", code: Exit.error) }
        let kh = dir + "/known_hosts"
        defer {
            unlink(kh)
            rmdir(dir)
        }
        let line = "[127.0.0.1]:\(t.binding.ssh_port) \(t.binding.host_key_line)\n"
        try writeFileAtomic(kh, Array(line.utf8))
        let b = t.binding
        let args = ["-F", "/dev/null", "-T",
                    "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityFile=\(b.ops_key_path)",
                    "-o", "UserKnownHostsFile=\(kh)", "-o", "StrictHostKeyChecking=yes",
                    "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
                    "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
                    "-o", "ClearAllForwardings=yes", "-o", "PasswordAuthentication=no",
                    "-o", "KbdInteractiveAuthentication=no", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR",
                    "-p", "\(b.ssh_port)", "\(b.guest_user)@127.0.0.1", remote]
        return runProcess(sshPath, args, env: helperEnv(), stdin: input, timeout: timeout, maxStdout: maxStdout)
    }

    /// Run the embedded guest script (vault/guest_fill.py) with one JSON request.
    func guestFill(_ t: GuestTarget, request: [String: Any], value: [UInt8]?) throws -> [String: Any] {
        var req = request
        var valueString: String?
        if let v = value { valueString = String(decoding: v, as: UTF8.self); req["value"] = valueString }
        guard var encoded = try? JSONSerialization.data(withJSONObject: req) else {
            throw VaultError("cannot encode the request", code: Exit.error)
        }
        req["value"] = nil
        var inputBytes = Array(encoded) + [0x0A]
        wipe(&encoded)
        defer { wipe(&inputBytes) }
        let r = try ssh(t, remote: "python3 -I -c " + shellQuote(guestFillScript), input: inputBytes, timeout: 60)
        var stdout = String(decoding: r.stdout, as: UTF8.self)
        var stderr = String(decoding: r.stderr, as: UTF8.self)
        // Never pass the value on, whatever came back.
        if let v = valueString, v.count >= 4 {
            stdout = stdout.replacingOccurrences(of: v, with: "[redacted]")
            stderr = stderr.replacingOccurrences(of: v, with: "[redacted]")
        }
        if r.interrupted { return ["ok": false, "status": "error", "reason": "interrupted (the vault is locking)"] }
        if r.timedOut { return ["ok": false, "status": "error", "reason": "the VM didn't answer within 60 s"] }
        let last = stdout.split(separator: "\n").last.map(String.init) ?? ""
        guard var out = parseJSONObject(Array(last.utf8)), out["ok"] is Bool else {
            let why = stderr.isEmpty ? "exit \(r.status)" : oneLine(stderr, max: 200)
            return ["ok": false, "status": "error", "reason": "the VM side failed (\(why))"]
        }
        let ok = out["ok"] as? Bool ?? false
        out["status"] = ok ? "ok" : ((out["denied"] as? Bool ?? false) ? "denied" : "error")
        out["denied"] = nil
        if let reason = out["reason"] as? String { out["reason"] = oneLine(reason) }
        return out
    }
}
