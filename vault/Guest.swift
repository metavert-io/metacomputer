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
    var cancelled = false       // the program whose request this step serves went away (requesterProbe): ended
}

/// Whether the request this process is serving was withdrawn (only the unlock process's own thread asks).
func requesterLeft() -> Bool {
    guard Thread.isMainThread, let probe = requesterProbe else { return false }
    return probe()
}

/// Run a program with the given stdin bytes, collecting stdout and stderr. The child
/// gets only fds 0-2, default signal handling and its own process group (so Ctrl-C
/// in the unlock terminal reaches the vault, which then stops the child).
func runProcess(_ path: String, _ args: [String], env: [String], stdin input: [UInt8],
                timeout: TimeInterval, maxStdout: Int = 1 << 20) -> ProcResult {
    var res = ProcResult()
    // A request whose requester went away starts no further step.
    if requesterLeft() { res.cancelled = true; return res }
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
        if Thread.isMainThread, let hook = waitHook {
            waitHook = nil              // never nested
            hook()
            waitHook = hook
        }
        if receivedSignal != 0 { res.interrupted = true; break }
        if requesterLeft() { res.cancelled = true; break }
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
    if res.timedOut || res.interrupted || res.tooLarge || res.cancelled {
        kill(-pid, SIGKILL)         // its own process group (ssh and anything it started), then the child itself
        kill(pid, SIGKILL)
    }
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


let rebindHint = "if you moved the checkout or rebuilt the gateway, run `metacomputer secrets bind` again"

/// The VM's SSH port on this Mac is a forward served by the gateway, mcomp-netgw (cua-netgw with
/// metacomputer's patch; scripts/build-tools.sh), or cua-netgw itself for a VM whose gateway was
/// built by an earlier version (scripts/lib.sh netgw_render). The program listening there must have that name, be
/// the gateway binary pinned at bind (its path, as the kernel reports it), and that file
/// must still have the contents pinned at bind (sha256).
/// Returns nil when it is, else why not (and whether it's a refusal).
func checkListener(port: Int, gatewayPath: String?, gatewaySHA256: String?, allowedNames: [String]) -> (reason: String, denied: Bool)? {
    guard let want = gatewayPath, !want.isEmpty, let wantHash = gatewaySHA256, !wantHash.isEmpty else {
        return ("this VM was bound by an earlier version without its gateway: run `metacomputer secrets bind` again", false)
    }
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
    let pinned = URL(fileURLWithPath: want).resolvingSymlinksInPath().path
    for p in procs {
        guard allowedNames.contains(p.name) else {
            return ("127.0.0.1:\(port) is served by '\(clean(p.name, max: 40))' (pid \(p.pid)), not the VM's gateway (\(allowedNames.joined(separator: ", "))); refusing", true)
        }
        let have = processPath(p.pid).map { URL(fileURLWithPath: $0).resolvingSymlinksInPath().path }
        guard have == pinned else {
            return ("127.0.0.1:\(port) is served by \(clean(have ?? "an unknown program", max: 200)), not the pinned gateway \(clean(pinned, max: 200)); refusing (\(rebindHint))", true)
        }
    }
    guard let bytes = try? readFileBytes(pinned, max: 512 << 20), sha256Hex(bytes) == wantHash else {
        return ("the gateway program \(clean(pinned, max: 200)) isn't the one pinned at bind (its contents changed); refusing (\(rebindHint))", true)
    }
    return nil
}

/// sha256 of a file, for pinning the gateway.
func fileSHA256(_ path: String) -> String? {
    (try? readFileBytes(path, max: 512 << 20)).map(sha256Hex)
}

// MARK: - ssh

struct GuestTarget {
    let vm: String
    let binding: VMBinding
}

/// POSIX shell single quotes.
func shellQuote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

/// What `fill` runs in the VM. sshd forces it for the fill account whatever is asked for;
/// naming it keeps the intent readable in the guest's logs.
let secretFillCommand = "/opt/metacomputer/bin/secret-fill"

final class GuestRunner {
    let sshPath: String
    let listenerNames: [String]

    init() {
        var ssh = "/usr/bin/ssh"
        var names = ["mcomp-netgw", "cua-netgw"]
        #if MCOMP_VAULT_TESTING
        // Test builds only (scripts/build-vault.sh --test): a stub instead of ssh, and
        // other listener names (the pinned gateway path is still checked). Release builds
        // don't contain these lines.
        let env = ProcessInfo.processInfo.environment
        if let s = env["MCOMP_VAULT_TEST_SSH"], !s.isEmpty { ssh = s }
        if let l = env["MCOMP_VAULT_TEST_LISTENERS"], !l.isEmpty { names = l.split(separator: ",").map(String.init) }
        #endif
        sshPath = ssh
        listenerNames = names
    }

    func listenerProblem(_ t: GuestTarget) -> (reason: String, denied: Bool)? {
        // A computer reached over SSH elsewhere has no gateway on this Mac: ssh's check of its pinned host key is the
        // check (and a binding without a valid alias for it is refused before any connection).
        if t.binding.isRemote {
            guard let a = t.binding.host_key_alias, validHostKeyAlias(a), validHost(t.binding.sshDestination) else {
                return ("the binding of VM \(t.vm) names an address without its host key's name: bind it again", true)
            }
            // Its provider's tunnel: the program pinned at bind, unchanged (as the gateway's is checked).
            if let why = tunnelProblem(t.binding) { return (why, true) }
            // The computer it was bound to must still be the one of that name: deleted, or made again, it isn't,
            // and the old server (which may still exist, and accept these keys) must get nothing.
            guard let want = t.binding.computer_id,
                  readComputerID(computerIDPath(opsKey: t.binding.ops_key_path)) == want else {
                return ("the computer bound as VM \(t.vm) was deleted or made again since it was bound (its identity "
                        + "changed): nothing was sent. If this is the computer you mean, run `metacomputer secrets bind` "
                        + "again", true)
            }
            return nil
        }
        return checkListener(port: t.binding.ssh_port, gatewayPath: t.binding.gateway_path, gatewaySHA256: t.binding.gateway_sha256,
                      allowedNames: listenerNames)
    }

    /// ssh to the pinned endpoint as user with key, running `remote`, with `input` on its stdin.
    func ssh(_ t: GuestTarget, user: String, key: String, remote: String, input: [UInt8], timeout: TimeInterval,
             maxStdout: Int = 1 << 20) throws -> ProcResult {
        let b = t.binding
        guard let pin = pinnedHostKeyOptions(binding: b) else {
            throw VaultError("the pinned host key for VM \(t.vm) is damaged: bind it again", code: Exit.error)
        }
        guard let proxy = proxyCommand(b) else {
            throw VaultError("the tunnel pinned for VM \(t.vm) can't be run: bind it again", code: Exit.error)
        }
        let args = sshBaseOptions(key: key, port: b.ssh_port, remote: b.isRemote, proxy: proxy) + pin
            + ["-T", "-o", "ClearAllForwardings=yes", "-o", "LogLevel=ERROR", "\(user)@\(b.sshDestination)", remote]
        return runProcess(sshPath, args, env: helperEnv(), stdin: input, timeout: max(1, timeout), maxStdout: maxStdout)
    }

    /// The desktop account (sealed browser sessions).
    func opsSSH(_ t: GuestTarget, remote: String, input: [UInt8], timeout: TimeInterval, maxStdout: Int = 1 << 20) throws -> ProcResult {
        try ssh(t, user: t.binding.guest_user, key: t.binding.ops_key_path, remote: remote, input: input, timeout: timeout, maxStdout: maxStdout)
    }

    /// One request to the VM's secret-fill, as the fill account. The value (if any) goes
    /// only to ssh's standard input.
    func fill(_ t: GuestTarget, request: [String: Any], value: [UInt8]?, timeout: TimeInterval) throws -> [String: Any] {
        guard let user = t.binding.fill_user, let key = t.binding.fill_key_path else {
            return ["ok": false, "status": "error",
                    "reason": "VM \(t.vm) was bound by an earlier version without the fill account: run `metacomputer secrets bind` again"]
        }
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
        let r = try ssh(t, user: user, key: key, remote: secretFillCommand, input: inputBytes, timeout: timeout)
        var stderr = String(decoding: r.stderr, as: UTF8.self)
        if let v = valueString, !v.isEmpty { stderr = stderr.replacingOccurrences(of: v, with: "[redacted]") }
        if r.cancelled { return ["ok": false, "status": "cancelled", "reason": requesterLeftReason] }
        if r.interrupted { return ["ok": false, "status": "error", "reason": "interrupted (the vault is locking)"] }
        if r.timedOut { return ["ok": false, "status": "error", "reason": "the VM didn't answer in time"] }
        if r.status == 255 && stderr.contains("Permission denied") {
            return ["ok": false, "status": "error",
                    "reason": "the VM refused the fill account (\(user)). VMs built before secrets support have none: rebuild the VM to use secrets (or bind again if its keys changed)"]
        }
        let last = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
        guard let raw = parseJSONObject(Array(last.utf8)), raw["ok"] is Bool else {
            // Nothing the VM wrote is passed on, not even its exit status: it knows every value it was ever given and
            // could spell one in any of it, on this request or a later one (a check sends no value, but the VM still has
            // the last one).
            return ["ok": false, "status": "error", "reason": "the VM side failed (its own output isn't passed on)"]
        }
        // Never pass the value on, whatever came back: anything holding it, at any depth or length.
        if let v = valueString, !v.isEmpty, containsString(raw, v) {
            return ["ok": false, "status": "error", "reason": "result withheld (it contained the value)"]
        }
        // Keep only the fields a fill answers with, each of its type; anything else is dropped,
        // and a field of the wrong type makes the whole answer an error.
        guard var out = fillResultSchema(raw) else {
            return ["ok": false, "status": "error", "reason": "the VM side answered in an unexpected form"]
        }
        let ok = out["ok"] as? Bool ?? false
        out["status"] = ok ? "ok" : ((out["denied"] as? Bool ?? false) ? "denied" : "error")
        out["denied"] = nil
        // Every answer, a check's as much as a fill's: the VM knows every value it was ever given, so nothing it chose
        // goes on. Its reason becomes one of this process's own sentences; the site only as one of the sites this request
        // named (this process's own copy, else none: a VM could spell a value in a host name); the count of characters
        // as this process counts the value (a fill that worked); mode and field the request's; the route one of two words.
        if let reason = out["reason"] as? String { out["reason"] = fillReason(reason) }
        // An opened link reports where it landed: kept only as one of the sites the request named for that.
        let allowed = (request["final_origins"] as? [String]) ?? (request["origins"] as? [String] ?? [])
        out["origin"] = (out["origin"] as? String).flatMap { o in allowed.first { $0 == o } }
        out["chars"] = (ok && valueString != nil && request["mode"] as? String != "open") ? valueString!.utf16.count : nil
        out["mode"] = request["mode"] as? String
        out["field"] = request["field"] as? String
        out["profile"] = nil
        if let r = out["route"] as? String, r != "element" && r != "keyboard" { out["route"] = nil }
        out = out.filter { ["ok", "status", "reason", "origin", "chars", "mode", "field", "route"].contains($0.key) }
        return out
    }

    /// A relay's source (Codes.swift): the text of one element of the tab on `origins`, or the links in it, as the fill
    /// account reads them in the browser's isolated world. What comes back is held to a fixed form and size, kept in this
    /// process's memory only for the code check, and never printed, logged or recorded.
    func read(_ t: GuestTarget, request: [String: Any], timeout: TimeInterval) throws -> GuestRead {
        guard let user = t.binding.fill_user, let key = t.binding.fill_key_path else {
            return GuestRead(ok: false, origin: nil, text: "", links: [],
                             reason: "VM \(t.vm) was bound by an earlier version without the fill account: run `metacomputer secrets bind` again")
        }
        guard let encoded = try? JSONSerialization.data(withJSONObject: request) else { throw VaultError("cannot encode the request", code: Exit.error) }
        let r = try ssh(t, user: user, key: key, remote: secretFillCommand, input: Array(encoded) + [0x0A], timeout: timeout, maxStdout: 256 << 10)
        if r.cancelled { return GuestRead(ok: false, origin: nil, text: "", links: [], reason: requesterLeftReason) }
        if r.timedOut || r.interrupted { return GuestRead(ok: false, origin: nil, text: "", links: [], reason: "the VM didn't answer in time") }
        if r.tooLarge { return GuestRead(ok: false, origin: nil, text: "", links: [], reason: "the VM's answer was too large") }
        var raw = r.stdout
        defer { wipe(&raw) }
        let last = raw.split(separator: 0x0A).last.map(Array.init) ?? []
        guard let o = parseJSONObject(last), let okN = o["ok"] as? NSNumber, CFGetTypeID(okN) == CFBooleanGetTypeID() else {
            return GuestRead(ok: false, origin: nil, text: "", links: [], reason: "the VM side failed (its own output isn't passed on)")
        }
        guard okN.boolValue else {
            return GuestRead(ok: false, origin: nil, text: "", links: [], reason: (o["reason"] as? String).map(fillReason) ?? "the VM refused to read the page")
        }
        let allowed = request["origins"] as? [String] ?? []
        let origin = (o["origin"] as? String).flatMap { v in allowed.first { $0 == v } }
        var text = ""
        if let tx = o["text"] { guard let s = tx as? String else { return GuestRead(ok: false, origin: nil, text: "", links: [], reason: "the VM side answered in an unexpected form") }; text = String(s.prefix(relayTextMax)) }
        var links: [String] = []
        if let ls = o["links"] {
            guard let arr = ls as? [Any], arr.count <= 20, arr.allSatisfy({ ($0 as? String).map { $0.count <= codeLinkMax } ?? false }) else {
                return GuestRead(ok: false, origin: nil, text: "", links: [], reason: "the VM side answered in an unexpected form")
            }
            links = arr.compactMap { $0 as? String }
        }
        return GuestRead(ok: origin != nil, origin: origin, text: text, links: links, reason: origin == nil ? "the VM reported a page outside --from's site" : nil)
    }
}

/// What a relay read in the VM (only ever in this process's memory).
struct GuestRead {
    var ok: Bool
    var origin: String?
    var text: String
    var links: [String]
    var reason: String?
}

/// Why a fill or a check failed, in this process's words: the reasons secret-fill gives, matched by what they say,
/// never copied, since the VM has every value it was ever given and could put one in any form into its answer.
func fillReason(_ guest: String) -> String {
    let known: [(String, String)] = [
        ("couldn't be watched to the end", "the link's page couldn't be watched to the end; its loading was stopped and anything paused was refused, and nothing more was done (check the page)"),
        ("the link tried to take the tab to another site", "the link tried to take the tab to another site (not shown); that was stopped before it was visited, and nothing more was done (check the page)"),
        ("the link led to another site", "the link led to another site (not shown); nothing more was done (check the page)"),
        ("the tab didn't settle on the link's page", "the tab didn't settle on the link's page in time; check the page"),
        ("the selector matches no element", "the selector matches no element on the page"),
        ("the element is in a frame", "the element is inside a frame; open what it shows in its own tab"),
        ("the link isn't one this", "the VM refused the link"),
        ("navigation failed", "the tab didn't open the link"),
        ("the page's address and the address bar disagree", "the address bar and the page's own address disagree (an address typed but not opened?); nothing was typed"),
        ("the address bar", "the browser's address bar doesn't name the page's site; nothing was typed"),
        ("no browser window", "no human-mode browser window is active (click into the field in Firefox, or a Chrome started normally)"),
        ("the window isn't a browser", "the active window isn't a browser (Firefox, or a Chrome started normally)"),
        ("no accessibility", "the browser's accessibility tree isn't reachable"),
        ("the focused element is in the browser's own", "the focus is in the browser's own controls (its address bar), not the page; click into the field"),
        ("the field is inside a frame", "the focused field is inside a frame, whose site the address bar doesn't show; human mode refuses it"),
        ("human-mode fills need a standard driver", "this VM takes only bounded driver sessions, and human-mode fills type through the driver in standard mode"),
        ("human-mode typing failed", "the driver didn't type it (its own words aren't passed on)"),
        ("the human-mode site", "the page isn't on the expected site; nothing was typed"),
        ("the page changed during the fill", "the page changed during the fill; the value may have gone elsewhere (check the page)"),
        ("the focus moved while filling", "the focus moved while filling; the text may have gone to another field (check the page)"),
        ("the field holds", "the field doesn't hold as many characters as were typed (a length limit, or the page changed it)"),
        ("no open tab on", "no open tab on the secret's sites in the driver's Chrome; open the site first"),
        ("no Chrome with DevTools", "no Chrome with DevTools is running; fills need page-mode Chrome (browser_prepare)"),
        ("no Chrome is running on profile", "no Chrome is running on that profile"),
        ("the selector isn't valid CSS", "the selector isn't valid CSS (it searches the page itself, not frames or shadow roots)"),
        ("the selector matches", "the selector doesn't match exactly one element"),
        ("no field is focused", "no field is focused in the page; click into the field first"),
        ("the focused field is read-only", "the focused field is read-only or disabled"),
        ("the focused field is inside", "the focused field is inside a frame the fill can't look into"),
        ("the focused element is", "the focused element isn't the kind of field this fills"),
        ("tabs are open on the allowed sites", "several tabs are open on the allowed sites; close the others (a fill goes only to a tab that's alone on its site)"),
        ("no chrome with browser process", "no Chrome with that browser process id has DevTools open"),
        ("the field is inside too many nested frames", "the field is inside too many nested frames"),
        ("the tab is on", "the tab isn't on one of the secret's sites; nothing was typed"),
        ("the page changed after the check", "the page changed after the check; nothing was typed"),
        ("the page changed before the fill", "the page changed before the fill; nothing was typed"),
        ("the field lost the focus", "the field lost the focus before the fill; nothing was typed"),
        ("the page refused", "the page refused a check (its own words aren't passed on)"),
        ("bad profile name", "the VM refused the profile name"),
        ("bad browser pid", "the VM refused the browser process id"),
        ("bad request", "the VM refused the request's form"),
        ("can't tell which user runs the desktop", "the VM can't tell which user runs the desktop; refusing"),
        ("the tab has no page", "the tab has no page"),
        ("ran out of time", "the VM side ran out of time"),
        ("devtools", "Chrome's DevTools failed or refused a step (its own words aren't passed on)"),
        ("result withheld", "result withheld (it contained the value)"),
    ]
    let g = guest.lowercased()
    for (prefix, text) in known where g.hasPrefix(prefix.lowercased()) || g.contains(prefix.lowercased()) { return text }
    return "the VM reported that it didn't complete (its own words aren't passed on); check the page and the focused field"
}


/// The answer secret-fill may give: these fields, each a scalar of its type (nil: wrong form).
func fillResultSchema(_ raw: [String: Any]) -> [String: Any]? {
    let bools: Set<String> = ["ok", "denied"]
    let strings: Set<String> = ["mode", "field", "origin", "route", "reason", "profile"]
    let ints: Set<String> = ["chars"]
    // The fields that name one of a few things take only those: anything else is dropped, here, before it's kept anywhere.
    let enums: [String: Set<String>] = ["route": ["element", "keyboard"], "mode": ["fill", "check", "open"],
                                        "field": ["password", "username", "otp", "text"]]
    var out: [String: Any] = [:]
    for (k, v) in raw {
        if bools.contains(k) {
            guard let n = v as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }
            out[k] = n.boolValue
        } else if let allowed = enums[k] {
            guard let s = v as? String else { return nil }
            if allowed.contains(s) { out[k] = s }
        } else if strings.contains(k) {
            guard let s = v as? String else { return nil }
            out[k] = clean(s, max: 300)
        } else if ints.contains(k) {
            guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue == Double(n.intValue),
                  n.intValue >= 0, n.intValue <= 1_000_000 else { return nil }
            out[k] = n.intValue
        }
    }
    return out
}

/// Whether any string anywhere in a JSON value (keys included) contains `needle`.
func containsString(_ v: Any, _ needle: String) -> Bool {
    if let s = v as? String { return s.contains(needle) }
    if let a = v as? [Any] { return a.contains { containsString($0, needle) } }
    if let d = v as? [String: Any] { return d.contains { $0.key.contains(needle) || containsString($0.value, needle) } }
    return false
}

/// ssh options every vault connection uses: no config file (not ~/.ssh/config, not the system's),
/// no agent, only the given key, no password prompts. ProxyCommand is none, or a binding's pinned tunnel (proxyCommand).
func sshBaseOptions(key: String, port: Int, remote: Bool = false, proxy: String = "none") -> [String] {
    ["-F", "none", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityFile=\(key)", "-o", "IdentityAgent=none",
     "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
     "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no", "-o", "ConnectTimeout=\(remote ? 15 : 5)",
     "-o", "ProxyCommand=\(proxy)", "-o", "ProxyJump=none", "-o", "PermitLocalCommand=no", "-p", "\(port)"]
}

/// What's wrong with a binding's tunnel now, or nil: its program must be the one pinned at bind (path and contents),
/// its arguments words the ProxyCommand passes as they are. A binding without one has nothing to check.
func tunnelProblem(_ b: VMBinding) -> String? {
    guard let prog = b.tunnel_program else {
        return b.tunnel_sha256 == nil && b.tunnel_args == nil ? nil : "the binding's tunnel is incomplete: bind it again"
    }
    guard matches(prog, "^/[A-Za-z0-9._/+@_-]{1,400}$"), let want = b.tunnel_sha256, (b.tunnel_args ?? []).allSatisfy(validTunnelArg) else {
        return "the binding's tunnel isn't one this vault runs: bind it again"
    }
    guard let have = fileSHA256(prog), have == want else {
        return "the tunnel program \(clean(prog, max: 200)) isn't the one pinned at bind (its contents changed, or it's gone): "
            + "nothing was sent. If you updated it, run `metacomputer secrets bind` again"
    }
    return nil
}

/// ssh's ProxyCommand for a binding: "none" (straight to its address), or its pinned tunnel run through this vault
/// (`mcomp-vault _tunnel -- PROGRAM ARGS...`, which ends the tunnel when ssh goes); nil when it can't be said safely.
/// ssh hands the line to /bin/sh (helperEnv has no SHELL) after its own %-expansion: every word is single-quoted, and
/// none may hold a quote or a %.
func proxyCommand(_ b: VMBinding) -> String? {
    guard let prog = b.tunnel_program else { return "none" }
    guard tunnelProblem(b) == nil, let me = processPath(getpid()),
          !me.contains("'"), !me.contains("%"), me.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value != 0x7f }) else { return nil }
    return ([me, "_tunnel", "--", prog] + (b.tunnel_args ?? [])).map { "'\($0)'" }.joined(separator: " ")
}

// MARK: - the tunnel wrapper (ssh's ProxyCommand)

nonisolated(unsafe) var tunnelStop: Int32 = 0

/// `mcomp-vault _tunnel -- PROGRAM [ARGS...]`: run a provider's tunnel on ssh's standard input and output, and end it,
/// and everything it started, when ssh goes. ssh sends its ProxyCommand SIGHUP when it exits, and a tunnel that
/// ignores SIGHUP (Fly's `sprite proxy -W` does) would stay, keeping the computer awake and billing. So the program
/// runs in a session of its own; when this process gets SIGHUP, SIGTERM or SIGINT, or its parent (ssh) is gone, or the
/// program exits, the whole session's group gets SIGTERM, then SIGKILL after 3 s. Its exit status is the program's.
/// It leaves ssh's process group and session first, so a group kill aimed at ssh can't end it before the tunnel.
/// The same as lib/proxywrap.py, for the vault's own connections: no interpreter in between.
func runTunnel(_ argv: [String]) -> Int32 {
    guard argv.count >= 2, argv[0] == "--", argv[1].hasPrefix("/") else {
        eprint("usage: mcomp-vault _tunnel -- PROGRAM [ARGS...]")
        return 2
    }
    let prog = argv[1]
    let parent = getppid()
    // Out of ssh's process group and session first: whoever ends ssh's connection may SIGKILL that whole group at
    // once (runProcess on a timeout, Tunnel.stop), and this process must outlive ssh long enough to end the tunnel,
    // which isn't in that group. A process that already leads its group can't (EPERM), and stays.
    _ = setsid()
    for s in [SIGHUP, SIGTERM, SIGINT] { signal(s) { _ in tunnelStop = 1 } }
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    defer { posix_spawnattr_destroy(&attr) }
    posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETSID | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK))
    var defaults = sigset_t()
    sigemptyset(&defaults)
    for s in [SIGINT, SIGTERM, SIGHUP, SIGPIPE, SIGQUIT] { sigaddset(&defaults, s) }
    posix_spawnattr_setsigdefault(&attr, &defaults)
    var mask = sigset_t()
    sigemptyset(&mask)
    posix_spawnattr_setsigmask(&attr, &mask)
    let cargv = argv.dropFirst().map { strdup($0) } + [nil]
    defer { for p in cargv { free(p) } }
    var pid: pid_t = 0
    let rc = posix_spawn(&pid, prog, nil, &attr, cargv, environ)
    guard rc == 0 else {
        eprint("mcomp-vault _tunnel: cannot run \(clean(prog, max: 200)): \(String(cString: strerror(rc)))")
        return 127
    }
    var status: Int32 = 0
    var result: Int32 = 1
    var reaped = false
    while true {
        let r = waitpid(pid, &status, WNOHANG)
        if r == pid {
            reaped = true
            result = (status & 0x7F) == 0 ? (status >> 8) & 0xFF : 128 + (status & 0x7F)
            break
        }
        if tunnelStop != 0 || getppid() != parent { break }
        usleep(200_000)
    }
    endTunnelGroup(pid, leaderReaped: reaped)
    return result
}

/// SIGTERM to the tunnel's whole group, SIGKILL to whatever is left after 3 s; returns once the group is empty (the
/// leader reaped as it goes, so its zombie doesn't count).
func endTunnelGroup(_ pgid: pid_t, leaderReaped: Bool) {
    var reaped = leaderReaped
    var st: Int32 = 0
    for (sig, wait) in [(SIGTERM, 3.0), (SIGKILL, 1.0)] {
        if kill(-pgid, sig) != 0 && errno == ESRCH { break }
        let deadline = Date().addingTimeInterval(wait)
        while Date() < deadline {
            if !reaped && waitpid(pgid, &st, WNOHANG) == pgid { reaped = true }
            if kill(-pgid, 0) != 0 && errno == ESRCH { return }
            usleep(50_000)
        }
    }
    if !reaped { _ = waitpid(pgid, &st, 0) }
}

/// The pinned host key, given to ssh without any file a process of this user could change in
/// between: no known_hosts files at all, and KnownHostsCommand running /bin/echo (on the
/// sealed system volume) with the pinned line as its arguments. nil when the line isn't a key.
func pinnedHostKeyOptions(port: Int, hostKeyLine: String, alias: String? = nil) -> [String]? {
    let parts = hostKeyLine.split(separator: " ").map(String.init)
    guard parts.count == 2, matches(parts[0], "^[a-z0-9-]{3,40}$"), matches(parts[1], "^[A-Za-z0-9+/=]{20,2000}$") else { return nil }
    // On this Mac the key is the one for the VM's loopback port; a computer elsewhere is looked up under its alias
    // (HostKeyAlias), never under its address.
    var name = "[127.0.0.1]:\(port)"
    var extra: [String] = []
    if let a = alias {
        guard validHostKeyAlias(a) else { return nil }
        name = a
        extra = ["-o", "HostKeyAlias=\(a)"]
    }
    return extra + ["-o", "UserKnownHostsFile=/dev/null", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "KnownHostsCommand=/bin/echo \(name) \(parts[0]) \(parts[1])",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no", "-o", "HashKnownHosts=no"]
}

/// The pinned host key options for a binding: under its alias when it's a computer elsewhere.
func pinnedHostKeyOptions(binding b: VMBinding) -> [String]? {
    if b.isRemote {
        guard let a = b.host_key_alias, validHost(b.sshDestination) else { return nil }
        return pinnedHostKeyOptions(port: b.ssh_port, hostKeyLine: b.host_key_line, alias: a)
    }
    return pinnedHostKeyOptions(port: b.ssh_port, hostKeyLine: b.host_key_line)
}
