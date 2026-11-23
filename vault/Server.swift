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

/// How long the unlock process waits for a client to send its whole request, from when it accepted the connection,
/// however slowly the bytes come.
let clientMessageSeconds: TimeInterval = 10

/// How often waitHook runs while the unlock process reads a client's request, whether or not bytes arrive meanwhile.
let readHookEvery: TimeInterval = 0.2

/// One newline-terminated message (at most max bytes). With `within`, an absolute deadline for the whole message (a
/// client that sends part of one and stalls, or trickles it, gets nil at that time), and waitHook runs at least every
/// readHookEvery seconds while it reads, however the bytes come (one-time code requests expire on time meanwhile): the
/// unlock process's reads of its clients use it. Both clocks are monotonic (the system's uptime).
func readMessage(_ fd: Int32, max: Int = 1 << 20, within: TimeInterval? = nil) -> [UInt8]? {
    var out = [UInt8]()
    var buf = [UInt8](repeating: 0, count: 4096)
    let now = { ProcessInfo.processInfo.systemUptime }
    let deadline = within.map { now() + $0 }
    var hookRan = now()
    while true {
        if let d = deadline {
            if now() - hookRan >= readHookEvery {
                hookRan = now()
                if Thread.isMainThread, let hook = waitHook {
                    waitHook = nil
                    hook()
                    waitHook = hook
                }
            }
            let left = d - now()
            if left <= 0 { return nil }
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let wait = Swift.min(left, Swift.max(0.001, readHookEvery - (now() - hookRan)))
            let r = poll(&pfd, 1, Int32(Swift.max(1, wait * 1000)))
            if r < 0 && errno == EINTR { continue }
            if r == 0 { continue }
        }
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

/// What approve() decided: whether the use goes ahead, the approval to record, why not, and the id of the
/// request it filed (nil when a grant or a rule covered it, or nothing was asked).
struct ApprovalOutcome {
    let yes: Bool
    let how: String
    let why: String
    var requestID: String? = nil
    /// Allowed for this one use only: the value goes only to the site that was named.
    var thisUseOnly: Bool { ["each:yes", "each:local-auth", "each:passphrase"].contains(how) }
}

/// Why a request stopped when the program that asked went away (or withdrew it) while it was being served.
let requesterLeftReason = "stopped: the program that asked went away or withdrew the request, so nothing further was done (a step in progress was ended)"
/// The requests whose requester may withdraw them while they run (Commands.swift, request(): cancellable).
let withdrawableOps: Set<String> = ["fill", "login", "session_save", "session_load", "token", "renew", "code_give", "code_put", "code_relay",
                                     "code_fill"]

/// A connection read while the unlock process waited for an answer, served once it's free.
struct DeferredRequest {
    let fd: Int32
    let req: [String: Any]
    let op: String
    let pid: Int32
}

final class UnlockServer {
    let paths: Paths
    let key: VaultKey
    let confirmEach: Bool
    let multiplexer: String?
    let allowMultiplexer: Bool
    let touchID: Bool                   // unlock --touch-id: each question also shows the system's Touch ID / password dialog
    let started = Date()
    let expires: Date
    let runner = GuestRunner()
    var promptTimeout = 120
    var grants: [Grant] = []
    var stopReason: String?
    var listenFD: Int32 = -1
    var pendingRequests: [String: PendingRequest] = [:]
    var usedRequestIDs: Set<String> = []
    var deferred: [DeferredRequest] = []
    // The connection of the fill, sign-in, session or token request being served, and whether its requester has gone
    // away or withdrawn it (requesterGone). Its answer is this process's acknowledgement that the work stopped.
    var requesterFD: Int32 = -1
    var requesterWithdrew = false
    var promptOpen = false              // a question waits on the terminal, its cursor at the end of the prompt
    var codeRequests: [String: CodeRequest] = [:]   // one-time code requests (Codes.swift): in memory only, gone at lock
    var codeInFlight: String?           // the code request a step in the VM is serving right now
    let interactivePrompts: Bool        // INTERACTIVE_PROMPTS=on at unlock: `ask` also shows a dialog on this Mac
    lazy var hub = ProxyHub(paths: paths, key: key, sshPath: runner.sshPath)
    // The audit log and its sinks. The audit key (from the vault) MACs every record this
    // process writes; `known` holds their hashes. Sink places, backoff and acknowledged begins
    // live here, in memory, under sinkLock (the proxy's threads deliver too).
    var auditKey: [UInt8] = []
    let knownLock = NSLock()
    var known: [Int: String] = [:]
    var maxWritten = 0              // the highest seq this process wrote: a lower or equal one again means the log was cut back
    let sinkLock = NSLock()
    var cursors: [String: Int] = [:]
    var sinkNext: [String: Date] = [:]
    var sinkShown: [String: String] = [:]
    var sinkFailures: [String: Int] = [:]
    var waitingBegins: Set<String> = []          // "id|hash" of begins waiting for their acknowledgements
    var ackedBegins: [String: Set<String>] = [:]   // per sink, the begins it accepted
    var tamperSeen: Set<String> = []
    var auditSize: off_t = -1

    // Presence mode (Approvals.swift, resolvePresence): every approval and grant needs the dialog or the
    // passphrase; a typed answer only chooses how far it reaches.
    let requirePresence: Bool
    let presenceWhy: String
    let presenceWarning: String?

    init(paths: Paths, key: VaultKey, seconds: Int, confirmEach: Bool, multiplexer: String?, allowMultiplexer: Bool, touchID: Bool = false,
         presence: (Bool, String, String?) = (false, "off", nil)) {
        self.paths = paths
        self.key = key
        self.confirmEach = confirmEach
        self.multiplexer = multiplexer
        self.allowMultiplexer = allowMultiplexer
        self.touchID = touchID
        (self.requirePresence, self.presenceWhy, self.presenceWarning) = presence
        self.expires = Date().addingTimeInterval(TimeInterval(seconds))
        // The person's own setting, as bin/metacomputer passed it to this unlock (the unlock always has their terminal).
        self.interactivePrompts = ProcessInfo.processInfo.environment["MCOMP_INTERACTIVE_PROMPTS"] == "on"
        #if MCOMP_VAULT_TESTING
        if let t = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_PROMPT_TIMEOUT"], let n = Int(t), n > 0 { promptTimeout = n }
        #endif
    }

    /// Lines for the unlock terminal, each made printable. While a question waits, they go on lines of
    /// their own below it.
    func say(_ s: String) {
        if promptOpen { eprint(""); promptOpen = false }
        for line in s.split(separator: "\n", omittingEmptySubsequences: false) { oprint(clean(String(line), max: 400)) }
    }

    func remaining() -> Int { max(0, Int(expires.timeIntervalSinceNow)) }

    /// The vault is on its way to locking: `lock`, a signal, or its time is up. Nothing more is handed out or renewed.
    var stopping: Bool { stopReason != nil || receivedSignal != 0 || Date() >= expires }

    /// The vault file, checked for changes (a stat; a read only on a change), from the main loop and while a question
    /// waits: a secret replaced or deleted ends its tokens; a vault this unlock can no longer open and verify (a rekey, a
    /// recover, a damaged or missing file) ends every token at once and locks.
    func checkVault() {
        guard stopReason == nil, !hub.refreshIfVaultChanged() else { return }
        hub.lockdown()
        _ = try? load()                     // a changed key says so itself (and locks)
        if stopReason == nil {
            stopReason = "the vault file can't be read and verified with this unlock's key any more"
            say("\(clockTime())  the vault file can't be read and verified with this unlock's key any more: locking; unlock again")
        }
    }

    // MARK: approvals

    func granted(_ kind: String, _ name: String, _ who: Requester) -> String? {
        grants.removeAll { g in g.claudePID != nil && !(Requester(clientPID: 0, clientExe: "", claudePID: g.claudePID, claudeStart: g.claudeStart, project: nil).claudeAlive) }
        for g in grants where g.kind == kind && g.name == name {
            if g.claudePID == nil { return "grant:unlock" }
            if g.claudePID == who.claudePID, let a = g.claudeStart, let b = who.claudeStart, abs(a - b) < 0.001 { return "grant:session" }
        }
        return nil
    }

    /// The unattended rule that covers this use, if any (never for an identity provider: callers check).
    func coveringRule(_ use: String, name: String, site: String?, _ who: Requester) -> UnattendedRule? {
        guard let p = try? load() else { return nil }
        return (p.rules ?? []).first { $0.covers(use: use, name: name, site: site, who: who) }
    }

    /// A new request id: the client's own (8 hex digits, not used before in this unlock) or a random one.
    func newRequestID(_ proposed: String?) -> String? {
        if let p = proposed {
            guard validRequestID(p), !usedRequestIDs.contains(p) else { return nil }
            usedRequestIDs.insert(p)
            return p
        }
        while true {
            let id = randomBytes(4).map { String(format: "%02x", $0) }.joined()
            if usedRequestIDs.insert(id).inserted { return id }
        }
    }

    /// Ask the person. A grant (`NAME session` / `NAME unlock` answered earlier) or an unattended rule
    /// may cover the use first, never for an identity provider. Otherwise it files a request with an id
    /// and waits for: the name typed at the unlock terminal (NAME this use, NAME session for this Claude
    /// Code session, NAME unlock until the vault locks), `approve ID` with Touch ID / the login password or
    /// the passphrase, the Touch ID dialog itself with `unlock --touch-id`, or `deny ID`. An identity
    /// provider's use is allowed for that one use, whatever is typed. `use` and `summary` name the use in the
    /// request list (`requests`), the dialog and the audit log.
    func approve(_ kind: String, _ name: String, _ question: String, _ who: Requester, vm: String, site: String?,
                 use: String, summary: String, usernameHint: String? = nil, identityProvider: Bool = false,
                 proposedID: String? = nil, ruleUse: String? = nil, fixedID: String? = nil, singleUse: Bool = false,
                 forceLocalAuth: Bool = false, dialogSummary: String? = nil) -> ApprovalOutcome {
        if !identityProvider && !singleUse {
            if let g = granted(kind, name, who) { return ApprovalOutcome(yes: true, how: g, why: "") }
            if let ru = ruleUse, let r = coveringRule(ru, name: name, site: site, who) {
                return ApprovalOutcome(yes: true, how: "rule:\(r.id)", why: "")
            }
        }
        // In presence mode a typed answer only chooses a scope, so a terminal others can type into may still be used.
        let terminalOK = multiplexer == nil || allowMultiplexer || requirePresence
        if let m = multiplexer, !terminalOK, !touchID {
            return ApprovalOutcome(yes: false, how: "each:no", why: "the unlock terminal runs inside \(m), where any program can type into it, so confirmations there are refused: run unlock outside \(m) (or with --allow-multiplexer, or --touch-id to confirm with Touch ID instead)")
        }
        let t = min(promptTimeout, remaining())
        guard t > 0 else { return ApprovalOutcome(yes: false, how: "each:no", why: "the vault's time is up") }
        guard let id = fixedID ?? newRequestID(proposedID) else {
            return ApprovalOutcome(yes: false, how: "each:no", why: "the request id \(clean(proposedID ?? "", max: 12)) isn't 8 hex digits, or was used already in this unlock")
        }
        let fullSummary = clean(summary + " on VM \(vm), asked by \(who.describe)", max: 400)
        let p = PendingRequest(id: id, use: use, name: name, vm: vm, site: site, usernameHint: usernameHint, who: who,
                               identityProvider: identityProvider, summary: fullSummary)
        p.singleUse = singleUse
        p.forceLocalAuth = forceLocalAuth
        // macOS's dialog (the person's own) may say more than `requests` does: a given link's real host.
        p.dialogSummary = dialogSummary.map { clean($0 + " on VM \(vm), asked by \(who.describe)", max: 400) }
        pendingRequests[id] = p
        defer { pendingRequests[id] = nil }
        var rec: [String: Any] = ["op": "request", "result": "pending", "request_id": id, "use": use, "name": name, "vm": vm,
                                  "requester": who.record]
        if let s = site { rec["origin"] = s }
        if let u = usernameHint { rec["username_hint"] = u }
        if identityProvider { rec["identity_provider"] = true }
        if ["fill", "login"].contains(use) { rec["event"] = "login.request" }
        record(rec)
        dropEvent(type: "approval_waiting", severity: "warn", vm: vm,
                  message: "Vault approval waiting: \(kind.replacingOccurrences(of: "_", with: " ")) of \(name)" + (site.map { " into \($0)" } ?? "")
                    + " on VM \(vm), asked by \(who.describe)",
                  action: "Type \(name) in the vault's terminal to allow", project: who.project,
                  data: ["op": kind, "name": name, "site": site ?? "", "request_id": id])
        let q = question.components(separatedBy: "\n").prefix(4).map { clean($0, max: 500) }.joined(separator: "\n")
            + "\n  Request \(id): or approve it from any terminal with `metacomputer secrets approve \(id)` (Touch ID or your login password)"
            + (identityProvider ? "\n  An identity provider's sign-in (or one that may hold one): what you allow covers this one use." : "")
            + (singleUse && !identityProvider ? "\n  A one-time code or link: what you allow covers this one use." : "")
        var prompt: String?
        if terminalOK && requirePresence {
            prompt = presencePrompt(q, name: name, id: id)
        } else if terminalOK {
            prompt = approvalPrompt(q, name: name)
        } else {
            say(q + "\n  This terminal runs inside \(multiplexer ?? "a multiplexer"), so typed answers are refused here: confirm with Touch ID or your login password in the dialog, or `deny \(id)`.")
        }
        let outcome = waitForDecision(p, prompt: prompt, name: name, timeout: t)
        var how: String
        var confirmed = "terminal"
        /// The scope chosen, as a grant where it reaches further than this use (never for an identity provider).
        func grant(_ scope: ApprovalScope, _ oneUse: String) -> String {
            switch scope {
            case _ where identityProvider && scope != .use:
                say("  (an identity provider's sign-in: allowed for this use only)")
                return oneUse
            case _ where singleUse && scope != .use:
                say("  (a one-time code or link: allowed for this use only)")
                return oneUse
            case .unlock:
                grants.append(Grant(kind: kind, name: name, claudePID: nil, claudeStart: nil))
                return "each:unlock"
            case .session:
                if let cp = who.claudePID {
                    grants.append(Grant(kind: kind, name: name, claudePID: cp, claudeStart: who.claudeStart))
                    return "each:session"
                }
                say("  (not from a Claude Code session: allowed for this use only)")
                return oneUse
            case .use:
                return oneUse
            }
        }
        switch outcome {
        case .terminal(let scope):
            how = grant(scope, "each:yes")
        case .localAuth(let scope):
            // Exactly the scope the dialog named: what was typed before or after it never widens it.
            say("          allowed with Touch ID or the login password, \(scopeWords(scope, name: name))")
            how = grant(scope, "each:local-auth")
            confirmed = "local-auth"
        case .passphrase(let scope):
            say("          allowed with the vault passphrase (`approve \(id) --passphrase`), \(scopeWords(scope, name: name))")
            how = grant(scope, "each:passphrase")
            confirmed = "passphrase"
        case .terminalNo:
            return ApprovalOutcome(yes: false, how: "each:no", why: "not allowed at the vault's terminal", requestID: id)
        case .denied(let why):
            return ApprovalOutcome(yes: false, how: "each:no", why: why, requestID: id)
        }
        var auth: [String: Any] = ["op": "authorize", "result": "ok", "request_id": id, "use": use, "name": name, "vm": vm,
                                   "approval": how, "confirmed": confirmed, "presence": requirePresence, "requester": who.record]
        if let s = site { auth["origin"] = s }
        if let u = usernameHint { auth["username_hint"] = u }
        if ["fill", "login"].contains(use) { auth["event"] = "login.authorize" }
        record(auth)
        return ApprovalOutcome(yes: true, how: how, why: "", requestID: id)
    }

    /// Wait for a decision on `p`: a line on the unlock terminal (when `prompt` is given), the Touch ID dialog
    /// (`--touch-id`, or one `approve ID` asked for), a passphrase proof or `deny ID`. Meanwhile other
    /// connections are served: status, list, tokens, requests, rule lists, approve and deny at once, the rest
    /// once this one is decided. Returns .terminalNo for no answer in time, Ctrl-C or the terminal closing.
    /// How far an approval of `p` can reach: an identity provider's use, and a use from outside Claude Code, never
    /// further than this use (so no dialog claims more than it can grant).
    func reachable(_ p: PendingRequest, _ scope: ApprovalScope) -> ApprovalScope {
        if p.identityProvider || p.singleUse { return .use }
        if scope == .session && p.who.claudePID == nil { return .use }
        return scope
    }

    /// macOS's dialog for `p`, naming how far allowing it reaches; allowing it grants that and nothing more.
    func scopedLocalAuth(_ p: PendingRequest, _ scope: ApprovalScope) -> LocalAuthAttempt {
        let sc = reachable(p, scope)
        return startLocalAuth("approve request \(p.id), \(scopeWords(sc, name: p.name)): \(p.dialogSummary ?? p.summary)", scope: sc)
    }

    func waitForDecision(_ p: PendingRequest, prompt: String?, name: String, timeout: Int) -> Decision {
        if let pr = prompt {
            _ = tcflush(0, TCIFLUSH)
            eprintRaw(pr)
            promptOpen = true
        }
        if touchID || requirePresence || p.forceLocalAuth {
            p.localAuth = scopedLocalAuth(p, .use)
            p.autoLocalAuth = true
        }
        let deadline = Date().addingTimeInterval(TimeInterval(timeout))
        var line = [UInt8]()
        var stdinOpen = prompt != nil
        var result: Decision = .terminalNo
        defer {
            if promptOpen { eprint(""); promptOpen = false }
            p.localAuth?.cancel()
            var allowed: ApprovalScope? = nil
            if case .localAuth(let sc) = result { allowed = sc }
            replyWaiters(p, allowed.map { ["ok": true, "status": "ok", "id": p.id, "scope_words": scopeWords($0, name: p.name)] }
                            ?? denied("request \(p.id) was decided another way, or not in time"))
        }
        while true {
            if receivedSignal != 0 { return result }
            checkVault()                    // a rekey or a damaged vault while the question waits locks it now
            if stopReason != nil { result = .denied("the vault was locked"); return result }
            if let d = p.decision { result = d; return d }
            codeExpirySweep()               // one-time code requests expire on time, even while this question waits
            if requesterGone() {
                // The program that asked went away (or withdrew it): nobody is left to act for, so no approval is taken.
                say("  (request \(p.id) withdrawn: the program that asked went away)")
                result = .denied("withdrawn: the program that asked went away before it was decided")
                return result
            }
            if Date() >= deadline {
                if promptOpen { eprint("\n  (no answer in \(timeout) s: not allowed)"); promptOpen = false } else {
                    say("  (no answer in \(timeout) s: not allowed)")
                }
                return result
            }
            if let la = p.localAuth, la.done {
                if la.ok {
                    p.decision = .localAuth(la.scope)
                    continue
                }
                replyWaiters(p, denied("not approved: \(la.why)"))
                if p.autoLocalAuth && la.refused {
                    p.decision = .denied("refused at the Touch ID dialog")
                    continue
                }
                say("          Touch ID / login password: \(la.why)" + (requirePresence
                    ? "; approve with `metacomputer secrets approve \(p.id) --passphrase` instead (one use; add --scope session or --scope unlock to reach further)"
                    : (stdinOpen ? "; you can still answer in this terminal" : "")))
                p.localAuth = nil
                p.autoLocalAuth = false
            }
            if prompt != nil, !promptOpen, stdinOpen {
                eprintRaw(requirePresence
                    ? "  (still waiting) \(name), \(name) session or \(name) unlock chooses how far it reaches; the dialog or the passphrase confirms; anything else refuses: "
                    : "  (still waiting) Type \(name) to allow (\(name) session / \(name) unlock); anything else refuses: ")
                promptOpen = true
            }
            var fds = [pollfd(fd: listenFD, events: Int16(POLLIN), revents: 0)]
            if stdinOpen { fds.append(pollfd(fd: 0, events: Int16(POLLIN), revents: 0)) }
            let r = poll(&fds, nfds_t(fds.count), 200)
            if r < 0 { if errno == EINTR { continue }; return result }
            if r == 0 { continue }
            if listenFD >= 0, fds[0].revents & Int16(POLLIN) != 0 {
                let c = accept(listenFD, nil, nil)
                if c >= 0 {
                    _ = fcntl(c, F_SETFD, FD_CLOEXEC)
                    serviceDuringWait(c)
                }
            }
            if stdinOpen && fds.count > 1 {
                let rv = fds[1].revents
                if rv & Int16(POLLIN) != 0 {
                    var ch: UInt8 = 0
                    let n = read(0, &ch, 1)
                    if n <= 0 { stdinOpen = false; if !(touchID || requirePresence) { return result } else { continue } }
                    if ch == 0x0A {
                        promptOpen = false
                        let answer = String(decoding: line, as: UTF8.self).trimmingCharacters(in: .whitespaces)
                        line = []
                        guard let scope = approvalAnswer(answer, name: name) else { result = .terminalNo; return result }
                        if !requirePresence { result = .terminal(scope); return result }
                        // Presence mode: what's typed only chooses, and the choice is part of what's confirmed. A dialog
                        // already allowed decides with its own scope (a scope typed after it isn't applied); one still
                        // open for another scope is withdrawn, and a new one names the scope typed.
                        let want = reachable(p, scope)
                        if let la = p.localAuth, la.done, la.ok {
                            p.decision = .localAuth(la.scope)
                            say("  (already allowed \(scopeWords(la.scope, name: name)); what was typed after that isn't applied)")
                            continue
                        }
                        if let la = p.localAuth, !la.done, la.scope == want {
                            say("  chosen: \(scopeWords(want, name: name)). The dialog open now asks for exactly that.")
                            continue
                        }
                        if let la = p.localAuth, !la.done {
                            la.cancel()
                            replyWaiters(p, denied("not approved: a different scope was chosen at the unlock terminal, and a new dialog asks for it"))
                        }
                        say("  chosen: \(scopeWords(want, name: name)). Confirm it in macOS's dialog, which names it, or with "
                            + "`metacomputer secrets approve \(p.id) --passphrase --scope \(scopeName(want))`.")
                        p.localAuth = scopedLocalAuth(p, want)
                        p.autoLocalAuth = true
                        continue
                    }
                    if line.count < 120 { line.append(ch) }
                } else if rv & Int16(POLLHUP | POLLERR | POLLNVAL) != 0 {
                    stdinOpen = false
                    if !(touchID || requirePresence) { return result }
                }
            }
        }
    }

    /// Answer the `approve ID` clients waiting for a dialog.
    func replyWaiters(_ p: PendingRequest, _ res: [String: Any]) {
        for c in p.waiters {
            reply(c, res)
            close(c)
        }
        p.waiters = []
    }

    /// One connection that arrived while the unlock process waits for an answer.
    func serviceDuringWait(_ c: Int32) {
        setTimeouts(c, seconds: 10)
        guard let uid = peerUID(c), uid == getuid() else {
            record(["op": "connect", "result": "denied", "reason": "another user"])
            say("\(clockTime())  refused a connection from another user")
            close(c)
            return
        }
        let pid = peerPID(c)
        guard let msg = readMessage(c, within: clientMessageSeconds), let req = parseJSONObject(msg), let op = req["op"] as? String else {
            reply(c, ["ok": false, "status": "error", "reason": "bad request"])
            close(c)
            return
        }
        let who = Requester.of(pid)
        var res: [String: Any]?
        switch op {
        case "status": res = opStatus()
        case "list": res = opList()
        case "tokens": res = opTokens()
        case "requests": res = opRequests()
        case "rule_list": res = opRuleList()
        case "code_status": res = opCodeStatus(req)
        case "deny": res = opDeny(req, who)
        // A revoke only restricts, and must win at once: it doesn't wait behind a question (a renewal that's waiting then
        // finds its token gone). Its audit record is written now; the sinks get it on the next delivery.
        case "revoke": res = opRevoke(req, who, flush: false)
        // A disconnect only closes (the computer can sleep), and must win at once too.
        case "disconnect": res = opDisconnect(req, who)
        case "lock":
            stopReason = "locked with `lock` (from \(who.describe))"
            hub.lockdown()          // every token ends now, before the answer: nothing is renewed or served after it
            say("\(clockTime())  `lock` from \(who.describe)")
            res = ["ok": true, "status": "ok"]
        case "approve":
            // A waiting code request's approval acts on the VM once it's allowed: it waits its turn behind the question.
            if let id = req["id"] as? String, let cr = codeRequests[id], pendingRequests[id] == nil, cr.parked {
                deferred.append(DeferredRequest(fd: c, req: req, op: op, pid: pid))
                return
            }
            res = opApprove(c, req, who)
            if res == nil { return }            // it waits for the dialog: replyWaiters answers it
        default:
            deferred.append(DeferredRequest(fd: c, req: req, op: op, pid: pid))
            return
        }
        reply(c, res ?? failed("no answer"))
        close(c)
    }

    // MARK: requests, approve, deny

    func opRequests() -> [String: Any] {
        let waiting = pendingRequests.values.sorted { $0.created < $1.created }.map { $0.listing }
        // One-time code requests still open (waiting for a value, a fill or an approval), unless one is also a question above.
        let codes = codeRequests.values.filter { !$0.final && pendingRequests[$0.id] == nil && Date() < $0.expires }
            .sorted { $0.created < $1.created }.map { $0.listing }
        return ["ok": true, "status": "ok", "requests": waiting + codes]
    }

    /// nil: the client waits for the Touch ID dialog's answer (its connection stays open).
    func opApprove(_ c: Int32, _ req: [String: Any], _ who: Requester) -> [String: Any]? {
        if let id = req["id"] as? String, pendingRequests[id] == nil, let cr = codeRequests[id] { return opApproveCode(req, who, cr) }
        guard let id = req["id"] as? String, validRequestID(id), let p = pendingRequests[id] else {
            return failed("no request \(clean(req["id"] as? String ?? "", max: 12)) waits for approval (metacomputer secrets requests)")
        }
        guard p.decision == nil else { return failed("request \(id) is already decided") }
        // How far it reaches is part of what's confirmed: one use unless the approver asked for more, and named it.
        guard let asked = parseScope(req["scope"] as? String ?? "use") else { return failed("approve: the scope is use, session or unlock") }
        let scope = reachable(p, asked)
        switch req["method"] as? String ?? "local-auth" {
        case "passphrase":
            var k = approvalProofKey(key)
            defer { wipe(&k) }
            guard let proof = req["proof"] as? String,
                  constantTimeEqual(proof, approvalProof(k, id: id, challenge: p.challenge, summary: p.summary, scope: asked)) else {
                record(["op": "approve", "result": "denied", "request_id": id, "method": "passphrase",
                        "reason": "the passphrase proof doesn't check out", "requester": who.record])
                say("\(clockTime())  an approval of request \(id) was refused: its passphrase proof doesn't check out (from \(who.describe))")
                return denied("the passphrase proof doesn't check out")
            }
            p.decision = .passphrase(scope)
            return ["ok": true, "status": "ok", "id": id, "scope_words": scopeWords(scope, name: p.name)]
        case "local-auth":
            // A dialog open for another scope is withdrawn: the new one names this approval's scope.
            if let la = p.localAuth, !la.done, la.scope != scope {
                la.cancel()
                replyWaiters(p, denied("not approved: another `approve` asked for a different scope, and a new dialog names it"))
                p.localAuth = nil
            }
            if p.localAuth == nil || p.localAuth!.done {
                say("\(clockTime())  request \(id): asking macOS for Touch ID or the login password, \(scopeWords(scope, name: p.name)) (`approve` from \(who.describe))")
                p.localAuth = scopedLocalAuth(p, scope)
                p.autoLocalAuth = false
            }
            p.waiters.append(c)
            return nil
        default:
            return failed("approve: the method is local-auth or passphrase")
        }
    }

    func opDeny(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        if let id = req["id"] as? String, pendingRequests[id] == nil, let cr = codeRequests[id] { return opDenyCode(cr, who) }
        guard let id = req["id"] as? String, validRequestID(id), let p = pendingRequests[id] else {
            return failed("no request \(clean(req["id"] as? String ?? "", max: 12)) waits for approval (metacomputer secrets requests)")
        }
        guard p.decision == nil else { return failed("request \(id) is already decided") }
        p.decision = .denied("refused with `deny \(id)`")
        say("\(clockTime())  request \(id) refused with `deny` (from \(who.describe))")
        return ["ok": true, "status": "ok", "id": id]
    }

    // MARK: unattended rules

    func opRuleList() -> [String: Any] {
        guard let p = try? load() else { return failed("cannot read the vault") }
        return ["ok": true, "status": "ok", "rules": (p.rules ?? []).filter { $0.live }.map { $0.listing }]
    }

    /// A rule made with Touch ID or the login password: the unlock process asks macOS, then writes it.
    func opRuleAdd(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        let codeUse = req["use"] as? String
        let isCodeRule = codeUse == "code" || codeUse == "relay"
        guard let project = req["project"] as? String, (isCodeRule && project.isEmpty) || project.hasPrefix("/"), clean(project, max: 4096) == project,
              let secs = (req["for_s"] as? NSNumber)?.intValue, secs > 0, secs <= maxRuleSeconds else {
            return failed("bad request")
        }
        let secret = req["secret"] as? String, site = req["site"] as? String, session = req["session"] as? String
        let built: (UnattendedRule?, String?)
        if isCodeRule {
            guard let st = site, secret == nil, session == nil else { return failed("bad request") }
            let links = (req["link_hosts"] as? [String]) ?? []
            guard links.count <= 8 else { return failed("bad request") }
            built = buildCodeRule(use: codeUse!, site: st, fromHost: req["from_host"] as? String, linkHosts: links, project: project,
                                  expires: Date().addingTimeInterval(TimeInterval(secs)), confirmed: "local-auth")
        } else {
            guard [secret, site, session].compactMap({ $0 }).count == 1, secret.map(validName) ?? true, session.map(validName) ?? true,
                  site.map({ canonicalOrigin($0) == $0 }) ?? true else { return failed("bad request") }
            let p: Payload
            do { p = try load() } catch { return failed("cannot read the vault") }
            // One rule, built now with its absolute expiry; the dialog shows exactly its stored fields, and it's stored as is.
            built = buildRule(p, use: "", secret: secret, site: site, session: session, project: project,
                              expires: Date().addingTimeInterval(TimeInterval(secs)), confirmed: "local-auth")
        }
        let (rule, why) = built
        guard let rule else { return failed(why ?? "can't make that rule") }
        let text = rule.confirmText
        guard clean(text, max: maxConfirmText) == text else {
            return denied("this rule can't be shown whole in macOS's dialog (\(text.count) characters, at most \(maxConfirmText)), so it can't be "
                          + "confirmed there: add --passphrase to confirm it in your own terminal, where it's shown in full")
        }
        say("\(clockTime())  rule asked for: \(text) (from \(who.describe)); asking macOS for Touch ID or the login password")
        let la = startLocalAuth(text)
        let deadline = Date().addingTimeInterval(TimeInterval(min(promptTimeout, max(1, remaining()))))
        while !la.done {
            checkVault()
            codeExpirySweep()
            if receivedSignal != 0 || stopReason != nil || Date() >= deadline { la.cancel(); break }
            var pfd = pollfd(fd: listenFD, events: Int16(POLLIN), revents: 0)
            if poll(&pfd, 1, 200) > 0, pfd.revents & Int16(POLLIN) != 0 {
                let c = accept(listenFD, nil, nil)
                if c >= 0 { _ = fcntl(c, F_SETFD, FD_CLOEXEC); serviceDuringWait(c) }
            }
        }
        guard la.ok else {
            var rec = rule.record
            rec["op"] = "rule_add"
            rec["result"] = "denied"
            rec["reason"] = la.why
            rec["requester"] = who.record
            record(rec)
            say("          -> no rule (\(la.why))")
            return denied("not confirmed: \(la.why). Add --passphrase to confirm with the vault passphrase in your own terminal instead")
        }
        do {
            try updateVault(paths, key) { p in p.rules = (p.rules ?? []).filter { $0.live } + [rule] }
        } catch { return failed("cannot write the vault") }
        var rec = rule.record
        rec["op"] = "rule_add"
        rec["result"] = "ok"
        rec["requester"] = who.record
        record(rec)
        say("          -> rule \(rule.id) made")
        flushSinks(force: true)
        var out = rule.listing
        out["ok"] = true
        out["status"] = "ok"
        return out
    }

    func opRuleRemove(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let id = req["id"] as? String, validRequestID(id) else { return failed("bad rule id") }
        do {
            var found = false
            try updateVault(paths, key) { p in
                found = (p.rules ?? []).contains { $0.id == id }
                p.rules = (p.rules ?? []).filter { $0.id != id && $0.live }
            }
            guard found else { return failed("no rule \(id)") }
        } catch { return failed("cannot write the vault") }
        record(["op": "rule_remove", "result": "ok", "rule_id": id, "requester": who.record])
        say("\(clockTime())  rule \(id) removed (from \(who.describe))")
        flushSinks(force: true)
        return ["ok": true, "status": "ok", "id": id]
    }

    /// The events hook (needs_you): one JSON file per event in the shared spool, if it exists.
    func dropEvent(type: String, severity: String, vm: String, message: String, action: String, project: String?, data: [String: Any]) {
        dropSpoolEvent(family: "needs_you", type: type, severity: severity, vm: vm, message: message, action: action, project: project, data: data)
    }

    // MARK: the audit log and its sinks

    /// Write an audit record with the vault's audit MAC, and remember its hash: delivery
    /// refuses a record at that seq with any other hash. nil when it couldn't be written, or when
    /// the log was cut back so that this record reuses a seq this process already wrote (or
    /// doesn't follow the record it wrote just before): then it isn't remembered, and the
    /// terminal says so.
    @discardableResult
    func record(_ rec: [String: Any]) -> [String: Any]? {
        // One writer at a time in this process, so seqs are remembered in the order written.
        knownLock.lock()
        guard let stored = audit(paths, rec, macKey: auditKey) else {
            knownLock.unlock()
            say("\(clockTime())  WARNING: the audit log couldn't be written (look at audit.jsonl and the unified log)")
            return nil
        }
        guard let s = (stored["seq"] as? NSNumber)?.intValue, let h = stored["hash"] as? String else { knownLock.unlock(); return nil }
        let reused = s <= maxWritten || known[s] != nil
        let unlinked = known[s - 1].map { ($0) != (stored["prev"] as? String) } ?? false
        if !reused && !unlinked {
            known[s] = h
            maxWritten = s
        }
        knownLock.unlock()
        if reused || unlinked {
            let why = reused ? "the log was cut back: seq \(s) was already used by this unlock" : "seq \(s) doesn't follow the record this unlock wrote before it"
            say("\(clockTime())  AUDIT LOG CHANGED: \(why); uses that need a required audit sink are refused")
            return nil
        }
        return stored
    }

    func knownSnapshot() -> [Int: String] {
        knownLock.lock()
        defer { knownLock.unlock() }
        return known
    }

    /// This process's own place for a sink (sinkLock held). Starts from the saved state, whose
    /// MAC must check out; otherwise from the sink's first event.
    func cursor(_ name: String, _ sink: AuditSink) -> Int {
        let k = name + "|" + sinkStamp(sink)
        if let c = cursors[k] { return c }
        let (st, trusted) = loadSinkState(paths, name, sink, key: auditKey)
        if !trusted {
            say("\(clockTime())  audit sink \(name): its saved delivery state doesn't check out (changed outside the vault?); sending again from its first event")
        }
        cursors[k] = st.delivered
        return st.delivered
    }

    /// Deliver to one sink (sinkLock held); notes tamper notices and acknowledged begins (by
    /// their id and hash, never just a seq). Returns the problem, if any, and whether it stopped
    /// at its deadline with more to send.
    func deliver(_ name: String, _ sink: AuditSink, _ p: Payload, deadline: Date) -> (String?, Bool) {
        let k = name + "|" + sinkStamp(sink)
        let d = deliverSink(paths, name, sink, auditKey: auditKey, from: cursor(name, sink), known: knownSnapshot(),
                            legacyThrough: p.legacyThrough, deadline: deadline)
        cursors[k] = d.delivered
        for g in d.genuine {
            let id = "\(g.id)|\(g.hash)"
            if waitingBegins.contains(id) { ackedBegins[k, default: []].insert(id) }
        }
        for t in d.tampered {
            let reason = (t.e["reason"] as? String) ?? "a record failed its checks"
            if tamperSeen.insert("\(t.seq)|\(reason)").inserted {
                say("\(clockTime())  AUDIT LOG CHANGED: \(reason); sinks get a notice in its place")
                record(["op": "audit_tamper", "result": "error", "target_seq": t.seq, "reason": reason])
            }
        }
        return (d.problem, d.incomplete)
    }

    /// Try to deliver what's pending. force: ignore the backoff. Returns the sinks still
    /// behind, with why, and whether any of those is required.
    @discardableResult
    func flushSinks(force: Bool, onlyRequired: Bool = false, within seconds: Double = 20) -> [(String, String, Bool)] {
        guard sinkLock.lock(before: Date().addingTimeInterval(seconds)) else { return [] }
        defer { sinkLock.unlock() }
        return flushLocked(force: force, onlyRequired: onlyRequired, deadline: Date().addingTimeInterval(seconds))
    }

    func flushLocked(force: Bool, onlyRequired: Bool, deadline: Date) -> [(String, String, Bool)] {
        guard let p = try? load(), let sinks = p.audit_sinks, !sinks.isEmpty else { return [] }
        var behind: [(String, String, Bool)] = []
        let last = lastAuditEvent(paths.audit).0
        for name in sinks.keys.sorted() {
            let sink = sinks[name]!
            if onlyRequired && !sink.required { continue }
            if cursor(name, sink) >= last { sinkShown[name] = nil; continue }
            if !force, let next = sinkNext[name], next > Date() {
                behind.append((name, sinkShown[name] ?? "retrying", sink.required))
                continue
            }
            if receivedSignal != 0 || Date() >= deadline {
                behind.append((name, "not tried this round", sink.required))
                continue
            }
            let (problem, incomplete) = deliver(name, sink, p, deadline: deadline)
            if let problem {
                sinkFailures[name, default: 0] += 1
                sinkNext[name] = Date().addingTimeInterval(min(60, pow(2, Double(min(sinkFailures[name]!, 6)))))
                if sinkShown[name] != problem {
                    say("\(clockTime())  audit sink \(name): can't deliver (\(problem)); retrying" + (sink.required ? "; uses are refused until it can" : ""))
                    sinkShown[name] = problem
                }
                behind.append((name, problem, sink.required))
            } else {
                if sinkShown[name] != nil { say("\(clockTime())  audit sink \(name): delivering again") }
                sinkShown[name] = nil
                sinkFailures[name] = 0
                sinkNext[name] = incomplete ? Date() : nil   // more to send: the next tick goes on
                if incomplete { behind.append((name, "more to send", sink.required)) }
            }
        }
        return behind
    }

    /// nil when every required sink has acknowledged the record of this use before it happens;
    /// else the refusal. With a required sink, each use is first recorded as op "begin" (what,
    /// which secret, VM, requester); this process must see every required sink accept that very
    /// record (its id and hash, as it wrote it) before the use goes ahead. A saved delivery
    /// place, an empty backlog or an earlier acknowledgement of the same seq proves nothing, and
    /// acknowledgements are dropped for every sink when this returns. Syslog can't be required:
    /// TLS delivery has no acknowledgement from the receiver. Without a required sink, nothing
    /// extra is written.
    func requiredSinkProblem(_ op: String, _ fields: [String: Any], requester: [String: Any]) -> String? {
        guard let p = try? load() else { return "the vault can't be read" }
        let required = (p.audit_sinks ?? [:]).filter { $0.value.required }
        if required.isEmpty { return nil }
        if let bad = required.keys.sorted().first(where: { ["syslog", "ntfy"].contains(required[$0]!.kind) }) {
            return "the required audit sink \(bad) is \(required[bad]!.kind), which can't acknowledge what it receives; make it optional (remove it and add it again without --required) or require a webhook or OTLP sink"
        }
        guard sinkLock.lock(before: Date().addingTimeInterval(30)) else {
            return "the audit sinks are busy delivering; uses that need a required audit sink are refused until they catch up"
        }
        defer { sinkLock.unlock() }
        var rec = fields
        rec["op"] = "begin"
        rec["use"] = op
        rec["result"] = "pending"
        rec["requester"] = requester
        guard let stored = record(rec), let id = stored["id"] as? String, let hash = stored["hash"] as? String else {
            return "the audit log can't be written (or was changed), and a required audit sink must get a record of every use first; uses are refused until it can"
        }
        let key = "\(id)|\(hash)"
        waitingBegins.insert(key)
        defer {
            waitingBegins.remove(key)
            for k in Array(ackedBegins.keys) { ackedBegins[k]?.remove(key) }
        }
        let behind = flushLocked(force: true, onlyRequired: true, deadline: Date().addingTimeInterval(20))
        for name in required.keys.sorted() {
            let k = name + "|" + sinkStamp(required[name]!)
            if ackedBegins[k]?.contains(key) == true { continue }
            let why = behind.first(where: { $0.0 == name })?.1 ?? "it didn't acknowledge the record of this use"
            return "the required audit sink \(name) can't be reached (\(why)); uses are refused until it can"
        }
        return nil
    }

    func requiredSinkProblem(_ op: String, _ fields: [String: Any], _ who: Requester) -> String? {
        requiredSinkProblem(op, fields, requester: who.record)
    }

    /// Only when the audit log grew, or a sink is behind and its retry time has come; at most a
    /// few seconds, so Ctrl-C and the clock are honoured.
    func sinkTick() {
        var st = stat()
        let size = stat(paths.audit, &st) == 0 ? st.st_size : 0
        guard sinkLock.lock(before: Date().addingTimeInterval(1)) else { return }
        defer { sinkLock.unlock() }
        if size != auditSize || sinkNext.values.contains(where: { $0 <= Date() }) {
            auditSize = size
            _ = flushLocked(force: false, onlyRequired: false, deadline: Date().addingTimeInterval(5))
        }
    }


    // MARK: serving

    func serve() throws -> Int32 {
        guard let ak = vaultAuditKey(paths, key) else { throw VaultError("cannot read the vault's audit key") }
        auditKey = ak
        hub.recorder = { [unowned self] rec in self.record(rec) }
        // One-time code requests expire on time (their values wiped) even while a step in the VM runs.
        waitHook = { [unowned self] in self.codeExpirySweep(duringStep: true) }
        defer { waitHook = nil }
        hub.preUse = { [unowned self] fields, requester in self.requiredSinkProblem("api", fields, requester: requester) }
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
            + (confirmEach ? "; every use asks here first" : "; secrets and sessions set to 'each' ask here first"))
        say("  Requests and their outcomes appear here (never values). Ctrl-C locks the vault.")
        if let m = multiplexer {
            say("  WARNING: this terminal runs inside \(m). Any program of yours can type into it (for example")
            say("  `tmux send-keys` or `screen -X stuff`), including the answer to a confirmation.")
            say(requirePresence ? "  Typing here only chooses how far an approval reaches; macOS's dialog or the passphrase confirms it."
                : (allowMultiplexer ? "  Confirmations are taken here anyway (--allow-multiplexer)."
                   : (touchID ? "  Typed confirmations are refused here; each question shows the Touch ID / login password dialog instead."
                      : "  Uses that need a confirmation are refused here. For those, unlock in a terminal outside \(m).")))
        }
        if requirePresence {
            say("  Presence mode (\(presenceWhy)): every approval, and every `session` or `unlock` answer, needs Touch ID or your")
            say("  login password in macOS's dialog, or the vault passphrase (`metacomputer secrets approve ID --passphrase`).")
            say("  The dialog and the passphrase prompt name how far each approval reaches; only what they name is granted.")
            if let w = presenceWarning { say("  Note: \(w).") }
            if ProcessInfo.processInfo.environment["MCOMP_VAULT_LOCAL_AUTH"] == "noninteractive" {
                say("  (MCOMP_VAULT_LOCAL_AUTH=noninteractive: no dialog is shown, so approvals come only from the passphrase.)")
            }
        } else {
            if touchID {
                say("  Each question also shows macOS's Touch ID / login password dialog (--touch-id); either answer counts.")
            }
            if let w = presenceWarning { say("  WARNING: \(w).") }
        }
        #if MCOMP_VAULT_TESTING
        say("  TEST BUILD: honours MCOMP_VAULT_TEST_* settings; never use it for real secrets")
        #endif
        var unlockRec: [String: Any] = ["op": "unlock", "result": "ok", "until": isoTime(expires), "confirm": confirmEach ? "each" : "session",
                                        "multiplexer": multiplexer ?? "none", "pid": Int(getpid()), "enclave": key.usesEnclave,
                                        "presence": requirePresence, "presence_why": presenceWhy]
        if touchID { unlockRec["touch_id"] = true }
        record(unlockRec)
        flushSinks(force: true)

        var why = "locked"
        while true {
            if receivedSignal != 0 {
                why = receivedSignal == SIGHUP ? "the terminal closed" : (receivedSignal == SIGINT ? "Ctrl-C" : "signal \(receivedSignal)")
                break
            }
            if let s = stopReason { why = s; break }
            if Date() >= expires { why = "time is up"; break }
            codeExpirySweep()
            // Requests that arrived while a question waited, in the order they came.
            if !deferred.isEmpty {
                let d = deferred.removeFirst()
                dispatch(d.fd, d.req, d.op, d.pid)
                close(d.fd)
                continue
            }
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
            codeTick()                      // one-time code requests: expiries, and answers from a dialog
            checkVault()                    // a secret replaced or deleted meanwhile ends its tokens (and their answers)
        }
        for d in deferred {
            reply(d.fd, ["ok": false, "status": "locked", "reason": "the vault is locked"])
            close(d.fd)
        }
        deferred = []
        close(fd)
        listenFD = -1
        unlink(paths.socket)
        codeLockdown()
        hub.shutdown()
        record(["op": "lock", "result": "ok", "reason": why])
        flushSinks(force: true)
        key.wipeKey()
        if why == "Ctrl-C" { oprint("") }
        say("\(clockTime())  locked (\(why))")
        return Exit.ok
    }

    func handle(_ c: Int32) {
        setTimeouts(c, seconds: 10)
        guard let uid = peerUID(c), uid == getuid() else {
            record(["op": "connect", "result": "denied", "reason": "another user"])
            say("\(clockTime())  refused a connection from another user")
            return
        }
        let pid = peerPID(c)
        guard let msg = readMessage(c, within: clientMessageSeconds), let req = parseJSONObject(msg), let op = req["op"] as? String else {
            reply(c, ["ok": false, "status": "error", "reason": "bad request"])
            return
        }
        dispatch(c, req, op, pid)
    }

    func dispatch(_ c: Int32, _ req: [String: Any], _ op: String, _ pid: Int32) {
        if Date() >= expires || stopReason != nil || receivedSignal != 0 {
            reply(c, ["ok": false, "status": "locked", "reason": "the vault is locked"])
            return
        }
        // A program outside Claude Code may pass on a request for a Claude Code process (recorded as a claim only).
        let onBehalf = (req["on_behalf_pid"] as? NSNumber).map { pid_t(truncatingIfNeeded: $0.intValue) }
        let who = Requester.of(pid, onBehalf: onBehalf)
        // A fill, sign-in, session or token request stops when its requester goes away or withdraws it (closes its side
        // of the connection): checked before each step, while it waits for an approval and during each ssh, whose process
        // group is then killed. Its answer, written below once it has stopped, is the acknowledgement the requester waits for.
        // An `approve` of a waiting one-time code request fills it once allowed: it's withdrawn like a fill when its
        // client goes away (the wrapper keeps its in-flight marker and settles, as for a fill).
        let withdrawable = withdrawableOps.contains(op)
            || (op == "approve" && (req["id"] as? String).flatMap { codeRequests[$0] }.map { $0.parked && pendingRequests[$0.id] == nil } == true)
        if withdrawable {
            requesterFD = c
            requesterWithdrew = false
            requesterProbe = { [unowned self] in self.requesterGone() }
        }
        defer {
            if withdrawable {
                requesterProbe = nil
                requesterFD = -1
                requesterWithdrew = false
            }
        }
        if withdrawable && requesterGone() {
            // It waited (behind a question, or in the queue) and its requester left meanwhile: it never starts.
            var rec = echo(req, ["name", "vm", "field", "profile"])
            rec["op"] = op
            rec["result"] = "cancelled"
            rec["reason"] = "withdrawn before it started: the program that asked went away"
            rec["requester"] = who.record
            record(rec)
            say("\(clockTime())  \(op.replacingOccurrences(of: "_", with: " ")) withdrawn before it started (the program that asked went away), asked by \(who.describe)")
            reply(c, ["ok": false, "status": "cancelled", "reason": "withdrawn before it started: the program that asked went away"])
            return
        }
        var res: [String: Any]
        switch op {
        case "status": res = opStatus()
        case "list": res = opList()
        case "fill": res = opFill(req, who)
        case "login": res = opLogin(req, who)
        case "session_save": res = opSessionSave(req, who)
        case "session_load": res = opSessionLoad(req, who)
        case "token": res = opToken(req, who)
        case "renew": res = opRenew(req, who)
        case "tokens": res = opTokens()
        case "revoke": res = opRevoke(req, who)
        case "disconnect": res = opDisconnect(req, who)
        case "sinks": res = opSinks()
        case "sink_flush": res = opSinkFlush(req, who)
        case "audit_verify": res = opAuditVerify()
        case "requests": res = opRequests()
        case "approve": res = opApprove(c, req, who) ?? failed("no request waits for approval")
        case "deny": res = opDeny(req, who)
        case "rule_add": res = opRuleAdd(req, who)
        case "rule_list": res = opRuleList()
        case "rule_remove": res = opRuleRemove(req, who)
        case "code_ask": res = opCodeAsk(req, who)
        case "code_status": res = opCodeStatus(req)
        case "code_give": res = opCodeGive(req, who)
        case "code_put": res = opCodePut(req, who)
        case "code_relay": res = opCodeRelay(req, who)
        case "code_fill": res = opCodeFill(req, who)
        case "settle":
            // Answered only here, in order: every request that reached this process before it has finished (requests
            // that arrive during a question wait behind it). A requester that couldn't confirm its own request stopped
            // asks this; once it's answered, nothing that request started still runs.
            res = ["ok": true, "status": "ok", "settled": true]
        case "lock":
            // It only restricts: anyone may. The loop ends after this reply.
            stopReason = "locked with `lock` (from \(who.describe))"
            hub.lockdown()          // every token ends now, before the answer: nothing is renewed or served after it
            say("\(clockTime())  `lock` from \(who.describe)")
            res = ["ok": true, "status": "ok"]
        default: res = ["ok": false, "status": "error", "reason": "unknown request \(clean(op, max: 40))"]
        }
        setTimeouts(c, seconds: 10)
        reply(c, res)
    }

    func reply(_ c: Int32, _ obj: [String: Any]) {
        let safe = (cleanAll(obj) as? [String: Any]) ?? ["ok": false, "status": "error"]
        _ = writeAll(c, Array((jsonLine(safe) + "\n").utf8))
    }

    /// Whether the requester of the request being served has gone away or withdrawn it: its connection hung up, or
    /// reads as ended (it closed its side for writing), or sent anything more (a request is one line). Sticky for the
    /// request. Only ever true for a fill, sign-in, session or token request (dispatch registers those).
    func requesterGone() -> Bool {
        if requesterWithdrew { return true }
        guard requesterFD >= 0 else { return false }
        var pfd = pollfd(fd: requesterFD, events: Int16(POLLIN), revents: 0)
        guard poll(&pfd, 1, 0) > 0 else { return false }
        if pfd.revents & Int16(POLLHUP | POLLERR | POLLNVAL) != 0 {
            requesterWithdrew = true
        } else if pfd.revents & Int16(POLLIN) != 0 {
            var b: UInt8 = 0
            let n = recv(requesterFD, &b, 1, MSG_PEEK | MSG_DONTWAIT)
            if n >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) { requesterWithdrew = true }
        }
        return requesterWithdrew
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
         "pid": Int(getpid()), "build": Build.kind, "enclave": key.usesEnclave, "tokens": hub.live().count,
         "touch_id": touchID, "requests": pendingRequests.count, "presence": requirePresence, "presence_why": presenceWhy]
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
            if s.kind == "api" { d["answer_timeout_s"] = Int(hub.answerLimit(s)) }
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
            if let h = b.ssh_host { d["ssh_host"] = h }
            if b.fill_user == nil || (!b.isRemote && (b.gateway_path == nil || b.gateway_sha256 == nil))
                || (b.isRemote && (b.host_key_alias == nil || b.computer_id == nil)) { d["needs_bind"] = true }
            return d
        }
        let sessions: [[String: Any]] = p.sessions.keys.sorted().map { n in
            let m = p.sessions[n]!
            var d: [String: Any] = ["name": n, "vm": m.vm, "profile": m.profile, "saved_at": m.saved_at, "bytes": m.bytes,
                                    "loads": m.loads, "approval": m.approval ?? "each"]
            if let l = m.last_loaded { d["last_loaded"] = l }
            if let k = m.kind { d["kind"] = k }
            if let s = m.sites { d["sites"] = s }
            if let s = m.source { d["source"] = s }
            if m.identity_provider == true { d["identity_provider"] = true }
            return d
        }
        var out: [String: Any] = ["ok": true, "status": "ok", "secrets": secrets, "vms": vms, "sessions": sessions]
        if let r = p.session_key_rotated { out["session_key_rotated"] = r }
        return out
    }

    // MARK: results

    func finish(_ op: String, _ fields: [String: Any], _ res: [String: Any], _ who: Requester, approval: String,
                requestID: String? = nil) -> [String: Any] {
        var out = fields
        for k in ["ok", "status", "origin", "chars", "reason", "bytes"] { if let v = res[k] { out[k] = v } }
        // How the VM typed it: "element" (into the checked element) or "keyboard" (the fallback).
        if let r = res["route"] as? String, ["element", "keyboard"].contains(r) { out["route"] = r }
        if let id = requestID { out["request_id"] = id }
        // The requester went away or withdrew it: whatever stopped it, it stopped for that reason (a use that completed
        // first is recorded as it was, and marked).
        let left = requesterFD >= 0 && requesterGone()
        if left && out["status"] as? String != "ok" {
            out["ok"] = false
            out["status"] = "cancelled"
            out["reason"] = requesterLeftReason
        }
        out = (cleanAll(out) as? [String: Any]) ?? fields
        let status = out["status"] as? String ?? "error"
        var rec = fields
        if left { rec["requester_left"] = true }
        rec["op"] = op
        rec["result"] = status
        rec["approval"] = approval
        rec["requester"] = who.record
        // The record takes the result as filtered above (never the raw answer, which a VM could fill with anything);
        // a token's id and lifetime are this process's own.
        for k in ["origin", "chars", "reason", "bytes", "route", "request_id"] { if let v = out[k] { rec[k] = v } }
        for k in ["token_id", "ttl_s"] { if let v = res[k] { rec[k] = v } }
        // The site-login event names other password vaults use, beside our own op.
        if op == "fill" || op == "login" {
            if status == "ok" { rec["event"] = "login.fill" } else if status == "denied" || status == "locked" { rec["event"] = "login.denied" }
        }
        record(rec)
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
        let only = req["origin"] as? String
        if let o = only { fields["for_origin"] = clean(o, max: 300) }
        let browserPID = (req["browser_pid"] as? NSNumber)?.intValue
        if let bp = browserPID { fields["browser_pid"] = bp }
        // Human mode: the focused field of a browser without DevTools (Firefox, a Chrome started normally), its site from the
        // address bar (Human.swift). No selector, profile or browser pid there: those name page-mode things.
        let human = req["human"] as? Bool == true
        let windowID = (req["window_id"] as? NSNumber)?.intValue
        if human {
            guard selector == nil, profile == nil, browserPID == nil, windowID.map({ $0 > 0 }) ?? true, field != "text" else {
                return finish("fill", fields, failed("bad request: a human-mode fill takes --field password|username|otp and optionally --window-id, nothing else"), who, approval: "-")
            }
            fields["mode"] = "human"
            if let w = windowID { fields["window_id"] = w }
        } else if windowID != nil {
            return finish("fill", fields, failed("bad request: --window-id goes with --human"), who, approval: "-")
        }
        if let why = browserBindingProblem(req, profile: profile, who) { return finish("fill", fields, denied(why), who, approval: "-") }
        say("\(clockTime())  fill \(name) (\(field)) on VM \(vm)" + (human ? ", human-mode browser" + (windowID.map { " window \($0)" } ?? "") : "")
            + (profile.map { ", profile \($0)" } ?? "")
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
        if let o = only, canonicalOrigin(o) != o || !s.origins.contains(o) {
            return finish("fill", fields, denied("--origin \(clean(o, max: 120)) isn't one of \(name)'s sites"), who, approval: "-")
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
        if let bp = browserPID { base["browser_pid"] = bp }
        if let sel = selector { base["selector"] = sel }
        if let w = windowID { base["window_id"] = w }
        /// One step in the VM: the fill account's secret-fill (page mode) or the desktop account's human-mode script.
        func guest(_ request: [String: Any], _ value: [UInt8]?) throws -> [String: Any] {
            human ? try runner.human(t, request: request, value: value, timeout: TimeInterval(min(60, remaining())))
                  : try runner.fill(t, request: request, value: value, timeout: TimeInterval(min(60, remaining())))
        }
        // --origin: the one site the caller checked; a tab on any other of the secret's sites is refused, not filled.
        var origins = only.map { [$0] } ?? s.origins
        let hint = s.kind == "password" ? s.username.map(maskUsername) : nil
        if let h = hint { fields["username_hint"] = h }
        let proposed = req["request_id"] as? String
        var requestID: String?

        // A use that may ask is always checked first, and the value then goes only to the site the VM reported: a
        // grant or a rule decides whether to ask, never where the value may go. A secret with an identity
        // provider among its sites is always checked too: a use on that site asks.
        let hasIdP = s.origins.contains(where: isIdentityProviderOrigin)
        var approval = "session"
        if confirmEach || s.approval == "each" || hasIdP || human {
            do {
                var chk = base
                chk["mode"] = "check"
                chk["origins"] = origins
                let r: [String: Any]
                do { r = try guest(chk, nil) } catch let e as VaultError {
                    return finish("fill", fields, failed(e.message), who, approval: "each")
                } catch { return finish("fill", fields, failed("the check failed"), who, approval: "each") }
                guard r["ok"] as? Bool == true else { return finish("fill", fields, r, who, approval: "each") }
                guard originOK(r, origins), let origin = r["origin"] as? String else {
                    return finish("fill", fields, failed("the VM reported a site outside this secret's sites; refusing"), who, approval: "each")
                }
                let idp = isIdentityProviderOrigin(origin)
                if idp || confirmEach || s.approval == "each" {
                    let o = approve("fill", name, "  Fill \(name) (\(field)) into \(origin) on VM \(vm)"
                                    + (profile.map { ", Chrome profile \($0)" } ?? "")
                                    + (selector.map { ", selector '\(promptText($0, max: 80))'" } ?? "")
                                    + "?\n  Asked by \(who.describe).", who, vm: vm, site: origin,
                                    use: "fill", summary: "fill \(name) (\(field)) into \(origin)", usernameHint: hint,
                                    identityProvider: idp, proposedID: proposed, ruleUse: "fill")
                    approval = o.how
                    requestID = o.requestID
                    if !o.yes { return finish("fill", fields, denied(o.why), who, approval: approval, requestID: requestID) }
                }
                origins = [origin]          // only the site that was checked (and approved, or covered), whatever covered it
                if let problem = runner.listenerProblem(t) {
                    return finish("fill", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: approval, requestID: requestID)
                }
            }
        }

        var value: [UInt8]
        switch field {
        case "username": value = Array((s.username ?? "").utf8)
        case "otp":
            guard let seed = base32Decode(s.value), !seed.isEmpty else {
                return finish("fill", fields, failed("\(name)'s seed is damaged"), who, approval: approval, requestID: requestID)
            }
            // A code with under 3 s left could expire on its way; wait for the next one.
            let left = 30 - UInt64(Date().timeIntervalSince1970) % 30
            if left < 3 && remaining() > Int(left) + 1 { Thread.sleep(forTimeInterval: Double(left) + 0.2) }
            value = Array(totp(secret: seed, time: UInt64(Date().timeIntervalSince1970)).utf8)
        default: value = Array(s.value.utf8)
        }
        defer { wipe(&value) }
        guard Date() < expires, receivedSignal == 0 else {
            return finish("fill", fields, lockedResult("the vault locked before the value was sent"), who, approval: approval, requestID: requestID)
        }
        var req2 = base
        req2["mode"] = "fill"
        req2["origins"] = origins
        let r: [String: Any]
        do { r = try guest(req2, value) } catch let e as VaultError {
            return finish("fill", fields, failed(e.message), who, approval: approval, requestID: requestID)
        } catch {
            return finish("fill", fields, failed("the fill failed"), who, approval: approval, requestID: requestID)
        }
        if r["ok"] as? Bool == true {
            guard originOK(r, origins) else {
                return finish("fill", fields, failed("the VM reported a site outside this secret's sites"), who, approval: approval, requestID: requestID)
            }
            do {
                try updateVault(paths, key) { p in
                    p.secrets[name]?.uses += 1
                    p.secrets[name]?.last_used = isoTime()
                }
            } catch { say("          (couldn't record the use in the vault)") }
        }
        return finish("fill", fields, r, who, approval: approval, requestID: requestID)
    }

    // MARK: sign in (a username and a password under one approval)

    /// Fill a saved login's username and password into the two fields the selectors name, with one approval
    /// (fills through the same path as `fill`: each field checked in the VM, the value only on ssh's input).
    /// Without a username selector, only the password. The site is the one the VM reports for the password
    /// field, which must be one of the secret's; the username field must be on the same site.
    func opLogin(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        var fields = echo(req, ["name", "vm", "profile"])
        guard let name = req["name"] as? String, let vm = req["vm"] as? String, validName(name), validName(vm),
              (req["profile"] as? String).map(validName) ?? (req["profile"] == nil),
              let passSel = req["password_selector"] as? String, !passSel.isEmpty, passSel.count <= 300,
              (req["username_selector"] as? String).map({ !$0.isEmpty && $0.count <= 300 }) ?? (req["username_selector"] == nil) else {
            return finish("login", fields, failed("bad request"), who, approval: "-")
        }
        let profile = req["profile"] as? String
        let userSel = req["username_selector"] as? String
        let only = req["origin"] as? String
        if let o = only { fields["for_origin"] = clean(o, max: 300) }
        let browserPID = (req["browser_pid"] as? NSNumber)?.intValue
        if let bp = browserPID { fields["browser_pid"] = bp }
        if let why = browserBindingProblem(req, profile: profile, who) { return finish("login", fields, denied(why), who, approval: "-") }
        fields["password_selector"] = clean(passSel, max: 300)
        if let u = userSel { fields["username_selector"] = clean(u, max: 300) }
        say("\(clockTime())  sign in with \(name) on VM \(vm)" + (profile.map { ", profile \($0)" } ?? "")
            + ", password '\(promptText(passSel, max: 60))'" + (userSel.map { ", username '\(promptText($0, max: 60))'" } ?? "")
            + ", asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("login", fields, e.code == Exit.locked ? lockedResult(e.message) : failed(e.message), who, approval: "-") } catch {
            return finish("login", fields, failed("cannot read the vault"), who, approval: "-")
        }
        guard let s = p.secrets[name] else {
            return finish("login", fields, failed("no secret named \(name) (metacomputer secrets list)"), who, approval: "-")
        }
        guard s.kind == "password" else {
            return finish("login", fields, denied("\(name) is a \(s.kind) secret; signing in needs a password"), who, approval: "-")
        }
        if userSel != nil && (s.username ?? "").isEmpty {
            return finish("login", fields, denied("\(name) has no username stored; leave out the username field"), who, approval: "-")
        }
        let hint = s.username.map(maskUsername)
        if let h = hint { fields["username_hint"] = h }
        if let o = only, canonicalOrigin(o) != o || !s.origins.contains(o) {
            return finish("login", fields, denied("--origin \(clean(o, max: 120)) isn't one of \(name)'s sites"), who, approval: "-")
        }
        if let why = projectProblem(name, s, who) { return finish("login", fields, denied(why), who, approval: "-") }
        guard let t = target(p, vm) else {
            return finish("login", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-")
        }
        if let why = requiredSinkProblem("login", fields, who) { return finish("login", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(t) {
            return finish("login", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }
        var base: [String: Any] = [:]
        if let pr = profile { base["profile"] = pr }
        if let bp = browserPID { base["browser_pid"] = bp }
        // Check both fields, without the values: the password field names the site.
        func check(_ field: String, _ sel: String, _ origins: [String]) -> ([String: Any], String?) {
            var chk = base
            chk["mode"] = "check"
            chk["field"] = field
            chk["selector"] = sel
            chk["origins"] = origins
            do {
                let r = try runner.fill(t, request: chk, value: nil, timeout: TimeInterval(min(60, remaining())))
                guard r["ok"] as? Bool == true else { return (r, nil) }
                guard originOK(r, origins), let o = r["origin"] as? String else {
                    return (failed("the VM reported a site outside this secret's sites; refusing"), nil)
                }
                return (r, o)
            } catch let e as VaultError { return (failed(e.message), nil) } catch { return (failed("the check failed"), nil) }
        }
        let (pr, originOpt) = check("password", passSel, only.map { [$0] } ?? s.origins)
        guard let origin = originOpt else { return finish("login", fields, pr, who, approval: "each") }
        if let us = userSel {
            let (ur, uo) = check("username", us, [origin])
            guard uo == origin else { return finish("login", fields, ur, who, approval: "each") }
        }
        let idp = isIdentityProviderOrigin(origin)
        var approval = "session"
        var requestID: String?
        if idp || confirmEach || s.approval == "each" {
            let o = approve("fill", name, "  Sign in with \(name) (" + (userSel == nil ? "password" : "username and password") + ") on \(origin), VM \(vm)"
                            + (profile.map { ", Chrome profile \($0)" } ?? "") + "?\n  Asked by \(who.describe).", who, vm: vm, site: origin,
                            use: "login", summary: "sign in with \(name) on \(origin)", usernameHint: hint, identityProvider: idp,
                            proposedID: req["request_id"] as? String, ruleUse: "fill")
            approval = o.how
            requestID = o.requestID
            if !o.yes { return finish("login", fields, denied(o.why), who, approval: approval, requestID: requestID) }
            if let problem = runner.listenerProblem(t) {
                return finish("login", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: approval, requestID: requestID)
            }
        }
        guard Date() < expires, receivedSignal == 0 else {
            return finish("login", fields, lockedResult("the vault locked before the value was sent"), who, approval: approval, requestID: requestID)
        }
        // Only the site that was checked (and approved), for both fields.
        func put(_ field: String, _ sel: String, _ v: String) -> [String: Any] {
            var value = Array(v.utf8)
            defer { wipe(&value) }
            var r2 = base
            r2["mode"] = "fill"
            r2["field"] = field
            r2["selector"] = sel
            r2["origins"] = [origin]
            do {
                let r = try runner.fill(t, request: r2, value: value, timeout: TimeInterval(min(60, remaining())))
                if r["ok"] as? Bool == true && !originOK(r, [origin]) { return failed("the VM reported a site outside this secret's sites") }
                return r
            } catch let e as VaultError { return failed(e.message) } catch { return failed("the fill failed") }
        }
        var filled: [String] = []
        if let us = userSel {
            let r = put("username", us, s.username ?? "")
            guard r["ok"] as? Bool == true else { return finish("login", fields, r, who, approval: approval, requestID: requestID) }
            filled.append("username")
        }
        let r = put("password", passSel, s.value)
        if r["ok"] as? Bool == true {
            filled.append("password")
            do {
                try updateVault(paths, key) { p in
                    p.secrets[name]?.uses += 1
                    p.secrets[name]?.last_used = isoTime()
                }
            } catch { say("          (couldn't record the use in the vault)") }
        }
        fields["fields"] = filled
        return finish("login", fields, r, who, approval: approval, requestID: requestID)
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
        let idp = isIdentityProviderHost(originHost(host))
        if confirmEach || s.approval == "each" || idp {
            let minutes = max(1, min(ttlAsked, remaining()) / 60)
            let o = approve("token", name, "  Give the VM \(vm) a \(minutes)-minute token for \(name) (\(host))?\n  Asked by \(who.describe).",
                            who, vm: vm, site: "https://\(host)", use: "token", summary: "a \(minutes)-minute API token for \(name) (\(host))",
                            identityProvider: idp, proposedID: req["request_id"] as? String, ruleUse: "token")
            approval = o.how
            if !o.yes { return finish("token", fields, denied(o.why), who, approval: approval, requestID: o.requestID) }
        }
        if requesterGone() { return finish("token", fields, failed(requesterLeftReason), who, approval: approval) }
        if stopping {
            hub.lockdown()
            return finish("token", fields, lockedResult("the vault is locking"), who, approval: approval)
        }
        let listener: ProxyListener
        do { listener = try hub.ensure(t) } catch let e as VaultError { return finish("token", fields, failed(e.message), who, approval: approval) } catch {
            return finish("token", fields, failed("cannot start the proxy"), who, approval: approval)
        }
        let ttl = max(1, min(ttlAsked, tokenMaxTTL, remaining()))
        let (tok, raw) = hub.issue(name: name, vm: vm, ttl: ttl, requester: who, secret: s, computer: t.binding.computer_id)
        let base = "http://127.0.0.1:\(listener.guestPort)/\(name)/"
        var res: [String: Any] = ["ok": true, "status": "ok", "token_id": tok.id, "ttl_s": ttl]
        res = finish("token", fields, res, who, approval: approval)
        if requesterGone() {
            // Nobody is left to hand it to: it ends now rather than live, unused, until it expires.
            let gone = hub.revoke(tok.id)
            record(["op": "revoke", "result": "ok", "token_ids": gone.map { $0.id }, "reason": "the requester went away before it got the token"])
            return ["ok": false, "status": "cancelled", "reason": requesterLeftReason]
        }
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
        if let p = try? load() { hub.prune(p) }   // a secret replaced or deleted ends its tokens
        let list: [[String: Any]] = hub.live().map { t in
            var d: [String: Any] = ["id": t.id, "name": t.name, "vm": t.vm, "expires": isoTime(t.expires),
                                    "expires_in_s": max(0, Int(t.expires.timeIntervalSinceNow)), "requests": countClass(t.requests),
                                    "renewals": t.renewals, "longest_until": isoTime(t.limit)]
            if let pr = t.requester["project"] as? String { d["project"] = pr }
            return d
        }
        return ["ok": true, "status": "ok", "tokens": list]
    }

    /// flush: deliver the record to the audit sinks now (not while a question waits: the next delivery takes it).
    func opRevoke(_ req: [String: Any], _ who: Requester, flush: Bool = true) -> [String: Any] {
        let id = req["id"] as? String
        let vm = req["vm"] as? String
        guard id == nil || (id.map { matches($0, "^[0-9a-f]{8}$") } ?? false) else { return ["ok": false, "status": "error", "reason": "bad token id"] }
        guard vm == nil || (id == nil && validName(vm!)) else { return ["ok": false, "status": "error", "reason": "bad VM name"] }
        let gone = vm.map { hub.revokeVM($0) } ?? hub.revoke(id)
        say("\(clockTime())  revoked \(gone.count) token(s)" + (id.map { " (\($0))" } ?? vm.map { " (VM \($0))" } ?? " (all)") + ", asked by \(who.describe)")
        record(["op": "revoke", "result": "ok", "token_ids": gone.map { $0.id }, "requester": who.record])
        if flush { flushSinks(force: true) }
        return ["ok": true, "status": "ok", "revoked": gone.map { $0.id }]
    }

    /// `disconnect --vm VM`: close the API proxy's tunnel to that VM now, and wait until its ssh (and the provider's tunnel
    /// it runs) is gone; tokens stay. Other VMs' tunnels are left alone.
    func opDisconnect(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let vm = req["vm"] as? String, validName(vm) else { return ["ok": false, "status": "error", "reason": "bad VM name"] }
        let n = hub.disconnect(vm)
        say("\(clockTime())  closed \(n) tunnel(s) to VM \(vm), asked by \(who.describe)")
        record(["op": "disconnect", "result": "ok", "vm": vm, "closed": n, "requester": who.record])
        return ["ok": true, "status": "ok", "closed": n]
    }

    // MARK: renewing api tokens

    /// `token renew ID|--all`: a live token lives longer and the token in the VM stays the same, so a program there
    /// doesn't restart. The same rules as a new token, checked again for each: the secret's --project limit, a required
    /// audit sink's record first, the pinned gateway, and the approval: an `each` secret, an unlock with --confirm each,
    /// an identity provider's host, or a renewer other than the token's own session or you at a terminal (renewer())
    /// asks (a `NAME session` / `NAME unlock` answer or an unattended rule for its tokens covers it, as it covers a new
    /// token); otherwise a `session` secret doesn't. Never past the unlock, never past
    /// tokenMaxLifetime after the token was issued, never shorter than it was. Whatever happened while it waited wins: a
    /// revoke, an expiry, a changed secret or the vault locking, and it isn't renewed. Only the Mac side can renew: the
    /// proxy in the VM has no way to ask.
    func opRenew(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let vm = req["vm"] as? String, validName(vm) else { return finish("renew", [:], failed("bad request"), who, approval: "-") }
        let all = req["all"] as? Bool == true
        let id = req["id"] as? String
        var fields: [String: Any] = ["vm": vm]
        if let id, matches(id, "^[0-9a-f]{8}$") { fields["token_id"] = id }
        guard all != (id != nil), id.map({ matches($0, "^[0-9a-f]{8}$") }) ?? true else {
            return finish("renew", fields, failed("bad request"), who, approval: "-")
        }
        var step: Int? = nil
        if let n = req["ttl_s"] {
            guard let v = (n as? NSNumber)?.intValue, v > 0, v <= tokenMaxTTL else { return finish("renew", fields, failed("bad request"), who, approval: "-") }
            step = v
        }
        // The client says whether it runs at a terminal (its stdin and stderr); renewOne also asks the kernel.
        let interactive = req["interactive"] as? Bool == true
        if let p = try? load() { hub.prune(p) }   // a secret replaced or deleted has ended its tokens
        let mine = hub.live().filter { $0.vm == vm && (all || $0.id == id) }
        if !all {
            guard let t = mine.first else {
                say("\(clockTime())  renew token \(id ?? "") on VM \(vm), asked by \(who.describe)")
                return finish("renew", fields, denied("no live token \(id ?? "") on VM \(vm) (metacomputer secrets tokens): an ended token can't be renewed; get a new one"),
                              who, approval: "-")
            }
            return renewOne(t, step: step, proposedID: req["request_id"] as? String, interactive: interactive, who)
        }
        say("\(clockTime())  renew every live token on VM \(vm) (\(mine.count)), asked by \(who.describe)")
        var renewed: [[String: Any]] = [], notRenewed: [[String: Any]] = []
        var worst = "ok"
        var lockedDuring = false
        for t in mine {
            if requesterGone() {
                notRenewed.append(["token_id": t.id, "name": t.name, "reason": requesterLeftReason])
                worst = "cancelled"
                continue
            }
            // A lock (or the time running out) while an earlier token's question waited stops the whole batch.
            if stopping {
                hub.lockdown()
                notRenewed.append(["token_id": t.id, "name": t.name, "reason": "the vault is locking: not renewed"])
                lockedDuring = true
                continue
            }
            let r = renewOne(t, step: step, proposedID: nil, interactive: interactive, who)
            if r["status"] as? String == "ok" {
                var d: [String: Any] = ["token_id": t.id, "name": t.name]
                for k in ["expires", "expires_in_s", "extended", "renewals", "longest_until"] { if let v = r[k] { d[k] = v } }
                renewed.append(d)
            } else {
                notRenewed.append(["token_id": t.id, "name": t.name, "reason": r["reason"] as? String ?? "not renewed"])
                let st = r["status"] as? String ?? "error"
                if st == "locked" { lockedDuring = true; continue }
                if worst == "ok" || (worst == "denied" && st != "denied") { worst = st }
            }
        }
        var out: [String: Any] = ["ok": worst == "ok", "status": worst, "vm": vm, "renewed": renewed, "not_renewed": notRenewed]
        if mine.isEmpty { out["reason"] = "no live tokens on VM \(vm)" }
        // Stopped by a lock: said as a refusal (exit 2) so the lists still reach the requester; every token has ended.
        if lockedDuring {
            out["ok"] = false
            out["status"] = "denied"
            out["reason"] = "the vault locked during the batch: the rest weren't renewed, and every token has ended"
        }
        return out
    }

    /// Who renews a token, in the vault's own words: `same`, the Claude Code session it was made for (same pid and start
    /// time); `terminal`, you at a terminal (no Claude Code process among the client's parents, the client says its stdin
    /// and stderr are a terminal, and the kernel puts it in a terminal's foreground); `other`, anyone else. The client's
    /// --on-behalf-pid claim never counts. Attribution, not isolation: a program of yours can pose as either.
    func renewer(_ t: APIToken, _ who: Requester, interactive: Bool) -> String {
        if let mine = t.ownerClaudePID, let ms = t.ownerClaudeStart, who.claudePID == mine,
           let s = who.claudeStart, abs(s - ms) < 0.001 { return "same" }
        if who.claudePID == nil && interactive && inTerminalForeground(who.clientPID) { return "terminal" }
        return "other"
    }

    /// One token's renewal (opRenew).
    func renewOne(_ t: APIToken, step asked: Int?, proposedID: String?, interactive: Bool, _ who: Requester) -> [String: Any] {
        let by = renewer(t, who, interactive: interactive)
        let fields: [String: Any] = ["name": t.name, "vm": t.vm, "token_id": t.id, "renewer": by]
        say("\(clockTime())  renew token \(t.id) for \(t.name) on VM \(t.vm), asked by \(who.describe)"
            + (by == "other" ? " (made for \(t.ownerDescribe))" : ""))
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("renew", fields, e.code == Exit.locked ? lockedResult(e.message) : failed(e.message), who, approval: "-") } catch {
            return finish("renew", fields, failed("cannot read the vault"), who, approval: "-")
        }
        // A token covers its secret as it was approved: a secret replaced or deleted since has ended it.
        guard let s = p.secrets[t.name], t.fits(s), let host = s.api_host else {
            _ = hub.revoke(t.id)
            return finish("renew", fields, denied("\(t.name) changed since this token was made (set --replace or delete); get a new token"), who, approval: "-")
        }
        if let why = projectProblem(t.name, s, who) { return finish("renew", fields, denied(why), who, approval: "-") }
        guard let target = target(p, t.vm) else { return finish("renew", fields, failed("VM \(t.vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-") }
        // How far it could reach from now: the step asked (else the lifetime it was issued for), never past the unlock or
        // its own limit. Worked out again after any question, from the time then.
        let step = min(asked ?? t.ttl, tokenMaxTTL)
        func reach() -> (Date, String?) {
            let byStep = Date().addingTimeInterval(TimeInterval(step))
            let cap = min(expires, t.limit)
            if cap <= byStep { return (cap, expires <= t.limit ? "the vault's unlock ends at \(clockTime(expires))" : "a token lives at most \(durationWords(Int(hub.maxLifetime))) after it was made (this one until \(clockTime(t.limit)))") }
            return (byStep, nil)
        }
        let (first, capWhy) = reach()
        if first <= t.expires.addingTimeInterval(1) {
            if let why = capWhy {
                return finish("renew", fields, denied("token \(t.id) can't live any longer: \(why). Get a new token after that (`metacomputer secrets token \(t.name)`)"), who, approval: "-")
            }
            // It already lives at least that long: nothing changes, so nothing is asked.
            var res: [String: Any] = ["ok": true, "status": "ok", "token_id": t.id, "ttl_s": max(0, Int(t.expires.timeIntervalSinceNow))]
            res = finish("renew", fields, res, who, approval: "-")
            res["extended"] = false
            res["expires"] = isoTime(t.expires)
            res["expires_in_s"] = max(0, Int(t.expires.timeIntervalSinceNow))
            res["renewals"] = t.renewals
            res["longest_until"] = isoTime(t.limit)
            return res
        }
        if let why = requiredSinkProblem("renew", fields, who) { return finish("renew", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(target) {
            return finish("renew", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }
        var approval = "session"
        var requestID: String? = nil
        let idp = isIdentityProviderHost(originHost(host))
        // A token made for another requester asks, like an `each` secret, whatever the secret's policy: a session can't
        // keep alive a token it wasn't given without you saying so.
        let otherRequester = by == "other"
        if confirmEach || s.approval == "each" || idp || otherRequester {
            let minutes = max(1, Int(first.timeIntervalSinceNow) / 60)
            let whose = otherRequester ? "\n  It was made for \(t.ownerDescribe), not for whoever asks now." : ""
            let o = approve("token", t.name, "  Keep token \(t.id) for \(t.name) (\(host)) on VM \(t.vm) working for \(minutes) more minutes (until \(clockTime(first)))?\n  Asked by \(who.describe).\(whose)",
                            who, vm: t.vm, site: "https://\(host)", use: "renew",
                            summary: "renew API token \(t.id) for \(t.name) (\(host)) for \(minutes) more minutes"
                                + (otherRequester ? ", made for another requester" : ""),
                            identityProvider: idp, proposedID: proposedID, ruleUse: "token")
            approval = o.how
            requestID = o.requestID
            if !o.yes { return finish("renew", fields, denied(o.why), who, approval: approval, requestID: o.requestID) }
        }
        if requesterGone() { return finish("renew", fields, failed(requesterLeftReason), who, approval: approval, requestID: requestID) }
        // What happened while it waited wins: the secret must still be the one it was made for...
        guard let p2 = try? load(), let s2 = p2.secrets[t.name], t.fits(s2) else {
            _ = hub.revoke(t.id)
            return finish("renew", fields, denied("\(t.name) changed while the renewal waited; get a new token"), who, approval: approval, requestID: requestID)
        }
        // ...the vault not locking...
        if stopping {
            hub.lockdown()
            return finish("renew", fields, lockedResult("the vault is locking: token \(t.id) isn't renewed"), who, approval: approval,
                          requestID: requestID)
        }
        // ...and the token still there, unexpired, at the moment it moves (a revoke or a lock that came first wins). It moves
        // to the expiry shown when it was asked for (`first`), never later, however long the answer took.
        let (nt, moved, why) = hub.renew(id: t.id, vm: t.vm, until: first)
        guard let nt else {
            return finish("renew", fields, denied("token \(t.id) \(why); it isn't renewed. Get a new one if it's still needed (`metacomputer secrets token \(t.name)`)"),
                          who, approval: approval, requestID: requestID)
        }
        // The record's numbers are this process's own: the token's id and how long it now has, never anything the VM chose.
        var res: [String: Any] = ["ok": true, "status": "ok", "token_id": nt.id, "ttl_s": max(0, Int(nt.expires.timeIntervalSinceNow))]
        res = finish("renew", fields, res, who, approval: approval, requestID: requestID)
        res["extended"] = moved
        res["expires"] = isoTime(nt.expires)
        res["expires_in_s"] = max(0, Int(nt.expires.timeIntervalSinceNow))
        res["renewals"] = nt.renewals
        res["longest_until"] = isoTime(nt.limit)
        return res
    }

    // MARK: audit sinks, for list / show / test

    func opSinks() -> [String: Any] {
        guard let p = try? load() else { return ["ok": false, "status": "error", "reason": "cannot read the vault"] }
        sinkLock.lock()
        defer { sinkLock.unlock() }
        let list: [[String: Any]] = (p.audit_sinks ?? [:]).keys.sorted().map { n in
            let s = p.audit_sinks![n]!
            let st = loadSinkState(paths, n, s).0
            let place = cursor(n, s)
            var d: [String: Any] = ["name": n, "kind": s.kind, "url": maskedURL(s.url), "required": s.required, "created": s.created,
                                    "headers": s.headers.compactMap { $0.first }, "delivered": place, "backlog": sinkBacklog(paths, n, s, from: place)]
            if let e = sinkShown[n] ?? st.error { d["error"] = e }
            if let a = st.attempt { d["last_attempt"] = a }
            if s.ca_pem != nil { d["ca"] = true }
            return d
        }
        return ["ok": true, "status": "ok", "sinks": list]
    }

    func opSinkFlush(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let name = req["name"] as? String, validName(name), let p = try? load(), let s = p.audit_sinks?[name] else {
            return ["ok": false, "status": "error", "reason": "no such audit sink"]
        }
        record(["op": "audit_sink_test", "result": "ok", "name": name, "requester": who.record])
        guard sinkLock.lock(before: Date().addingTimeInterval(30)) else {
            return ["ok": false, "status": "error", "reason": "the audit sinks are busy delivering; try again"]
        }
        defer { sinkLock.unlock() }
        let (why, incomplete) = deliver(name, s, p, deadline: Date().addingTimeInterval(30))
        if let why {
            sinkShown[name] = why
            return ["ok": false, "status": "error", "reason": why]
        }
        sinkShown[name] = nil
        sinkNext[name] = incomplete ? Date() : nil
        sinkFailures[name] = 0
        if incomplete { return ["ok": false, "status": "error", "reason": "more to send than fits one go; it goes on in the background"] }
        return ["ok": true, "status": "ok", "delivered": cursor(name, s)]
    }

    /// Every record's creation MAC and the chain, checked by the one process holding the audit
    /// key, and reconciled with what this unlock remembers writing. Lines that aren't records,
    /// an empty log, and a record this unlock wrote that's missing or changed are all problems.
    /// Records up to the vault's migration boundary predate the audit key: counted as legacy.
    func opAuditVerify() -> [String: Any] {
        guard let p = try? load() else { return ["ok": false, "status": "error", "reason": "cannot read the vault"] }
        guard let bytes = try? readFileBytes(paths.audit, max: 1 << 30) else {
            return ["ok": false, "status": "error", "reason": "cannot read audit.jsonl"]
        }
        var verified = 0, unverified = 0, legacy = 0, total = 0, lastSeq = 0, line = 0
        var prev: String?
        var bad: [String] = []
        var seen: [Int: String] = [:]
        let known = knownSnapshot()
        for l in bytes.split(separator: 0x0A, omittingEmptySubsequences: false) {
            line += 1
            if l.isEmpty { continue }
            guard let e = parseJSONObject(Array(l)) else { bad.append("line \(line) isn't a record"); continue }
            guard let seq = (e["seq"] as? NSNumber)?.intValue, let h = e["hash"] as? String else {
                // A line from before the chain existed is legacy only before the first chained record.
                if total == 0 && lastSeq == 0 && p.legacyThrough >= 0 { legacy += 1 } else { bad.append("line \(line) has no seq or hash") }
                continue
            }
            total += 1
            if lastSeq != 0 && seq != lastSeq + 1 { bad.append("seq \(seq) follows \(lastSeq)") }
            if h != eventHash(e) { bad.append("seq \(seq): its hash doesn't match") }
            if let pr = prev, (e["prev"] as? String) != pr { bad.append("seq \(seq): doesn't link to the record before") }
            if let k = known[seq], k != h { bad.append("seq \(seq): not the record this unlock wrote") }
            if e.keys.contains("mac") {
                if let m = e["mac"] as? String, m == auditRecordMAC(auditKey, e) { verified += 1 } else {
                    bad.append("seq \(seq): fails its creation MAC")
                }
            } else if seq <= p.legacyThrough {
                legacy += 1
            } else if clientMayWrite(e) {
                unverified += 1
            } else {
                bad.append("seq \(seq) (\(clean(e["op"] as? String ?? "?", max: 40))): no creation MAC")
            }
            seen[seq] = h
            prev = h
            lastSeq = seq
        }
        if total == 0 { bad.append("the log holds no records") }
        for (seq, h) in known.sorted(by: { $0.key < $1.key }) where seen[seq] != h {
            bad.append("seq \(seq), written by this unlock, is " + (seen[seq] == nil ? "missing" : "changed"))
        }
        return ["ok": bad.isEmpty, "status": bad.isEmpty ? "ok" : "error", "records": total, "verified": verified,
                "unverified": unverified, "legacy": legacy, "problems": Array(bad.prefix(50)), "problem_count": bad.count,
                "head": lastSeq, "head_hash": prev ?? ""]
    }

    // MARK: sealed browser sessions (the desktop account)

    func sessionAAD(_ name: String) -> [UInt8] { Array("mcomp-session|1|\(name)".utf8) }

    /// The archive of a sealed session, under the current session key or, while a rotation
    /// was cut short, the previous one.
    func openSession(_ p: Payload, _ name: String) -> [UInt8]? {
        guard let sealed = try? readFileBytes(paths.sessions + "/\(name).sealed") else { return nil }
        for k in p.sessionKeyring {
            if let plain = try? openGCM(key: k, combined: sealed, aad: sessionAAD(name)) { return plain }
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
        let o = approve("session_save", name, question + "\n  Asked by \(who.describe).", who, vm: vm, site: nil, use: "session_save",
                        summary: (old == nil ? "save" : "replace the sealed session \(name) with") + " Chrome profile \(profile)" + (old == nil ? " as the sealed session \(name)" : ""),
                        proposedID: req["request_id"] as? String)
        let approval = o.how
        if !o.yes { return finish("session_save", fields, denied(o.why), who, approval: approval, requestID: o.requestID) }
        if let problem = runner.listenerProblem(t) {
            return finish("session_save", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: approval)
        }
        let r: ProcResult
        do { r = try runner.opsSSH(t, remote: sessionSaveScript(user: t.binding.guest_user, profile: profile), input: [],
                                   timeout: TimeInterval(min(600, remaining())), maxStdout: 1 << 31) }
        catch { return finish("session_save", fields, failed("cannot reach the VM"), who, approval: approval) }
        var archive = r.stdout
        defer { wipe(&archive) }
        if let why = sealedSessionRefusal(r) { return finish("session_save", fields, failed(why), who, approval: approval) }
        if r.interrupted || r.timedOut || r.tooLarge || r.status != 0 || archive.count < 2 || archive[0] != 0x1F || archive[1] != 0x8B {
            // The VM's own words never go into a result or the audit log here: only what this process knows.
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish in time" : r.tooLarge ? "the profile is too large"
                : sessionFailure(r.status, profile: profile, saving: true)
            return finish("session_save", fields, r.status == 4 ? denied(why) : failed(why), who, approval: approval)
        }
        do {
            try ensurePrivateDir(paths.sessions)
            // The archive's size is the VM's to choose (a number it could spell something in): it's kept, shown and
            // recorded only as one of a few sizes (sizeBound).
            let bytes = sizeBound(archive.count)
            let a = archive
            // Sealed and recorded under the vault's lock, with whatever session key is current then.
            try updateVault(paths, key) { p in
                guard let d = Data(base64Encoded: p.session_key), d.count == 32 else { throw VaultError("the vault's session key is damaged") }
                try writeFileAtomic(paths.sessions + "/\(name).sealed", try sealGCM(key: Array(d), plaintext: a, aad: sessionAAD(name)))
                // A replacement keeps every restrictive mark the session had.
                p.sessions[name] = SessionMeta(vm: vm, profile: profile, saved_at: isoTime(), bytes: bytes, last_loaded: nil, loads: 0,
                                               approval: old?.approval, identity_provider: old?.identity_provider)
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
        // --site: only the cookies those sites get (imported sessions only), filtered here before anything is sent.
        var only: [String]? = nil
        if let raw = req["sites"] {
            guard let list = raw as? [String], !list.isEmpty, list.count <= 20, list.allSatisfy(validSiteHost) else {
                return finish("session_load", fields, failed("bad request: sites must be 1 to 20 host names"), who, approval: "-")
            }
            only = Array(Set(list.map { $0.lowercased() })).sorted()
            fields["sites"] = only
        }
        say("\(clockTime())  session load \(name) on VM \(vm), asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch let e as VaultError { return finish("session_load", fields, failed(e.message), who, approval: "-") } catch {
            return finish("session_load", fields, failed("cannot read the vault"), who, approval: "-")
        }
        guard let meta = p.sessions[name] else {
            return finish("session_load", fields, failed("no saved session named \(name) (metacomputer secrets list)"), who, approval: "-")
        }
        let cookies = meta.kind == "cookies"
        let profile = req["profile"] as? String ?? meta.profile
        fields["profile"] = profile
        if cookies { fields["kind"] = "cookies" }
        if let o = only {
            guard cookies else {
                return finish("session_load", fields, denied("\(name) is a whole browser profile, which loads whole: --site narrows only a session imported with import-session"), who, approval: "-")
            }
            let held = meta.sites ?? []
            if let outside = o.first(where: { s in !held.contains { s == $0 || s.hasSuffix("." + $0) } }) {
                return finish("session_load", fields, denied("\(name) holds cookies for \(held.joined(separator: ", ")) only, not \(outside)"), who, approval: "-")
            }
        }
        // Don't forge or rewrite the driver's own records. (Imported cookies go into a profile's own store, whatever its name.)
        guard profile == meta.profile || cookies else {
            return finish("session_load", fields, denied("\(name) was saved from profile \(meta.profile). The driver records a profile's name inside it (.cua-driver-owned-profile.json), so the session works only under that name: leave out --profile, or use --profile \(meta.profile)"), who, approval: "-")
        }
        guard let t = target(p, vm) else {
            return finish("session_load", fields, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)"), who, approval: "-")
        }
        if let why = requiredSinkProblem("session_load", fields, who) { return finish("session_load", fields, denied(why), who, approval: "-") }
        if let problem = runner.listenerProblem(t) {
            return finish("session_load", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: "-")
        }
        if cookies {
            // Nobody is asked to approve a load that can't work: the profile needs a cookie store first.
            if let r = try? runner.opsSSH(t, remote: cookieStoreCheckScript(user: t.binding.guest_user, profile: profile), input: [], timeout: 30) {
                if let why = sealedSessionRefusal(r) { return finish("session_load", fields, failed(why), who, approval: "-") }
                if r.status == 5 || r.status == 6 {
                    // The VM may hold cookies loaded earlier: its own words never go on, only the status's meaning.
                    let why = sessionFailure(r.status, profile: profile, saving: false)
                    return finish("session_load", fields, r.status == 5 ? denied(why) : failed(why), who, approval: "-")
                }
            }
        }
        var approval = "session"
        // A whole browser profile may hold any sign-in, an identity provider's included: every load asks, whatever
        // its policy, and no grant or rule covers it.
        let idp = meta.alwaysAsks
        if confirmEach || meta.approval != "session" || meta.vm != vm || idp {
            let what = !cookies ? "the sealed session"
                : only.map { "only the cookies for \($0.joined(separator: ", ")) (of those imported for \((meta.sites ?? []).joined(separator: ", ")))" }
                    ?? "the cookies imported for \((meta.sites ?? []).joined(separator: ", "))"
            let question = cookies
                ? (meta.vm != vm
                   ? "  Load \(name), \(what) (made for VM \(meta.vm)), into profile \(profile) on a different VM, \(vm)?"
                   : "  Load \(name), \(what), into profile \(profile) on VM \(vm)?")
                : (meta.vm != vm
                   ? "  Load the sealed session \(name), saved from VM \(meta.vm), into a different VM, \(vm) (profile \(profile); replaces it)?"
                   : "  Load the sealed session \(name) into profile \(profile) on VM \(vm) (replaces that profile)?")
            // A load into another VM always asks this time, whatever was granted before (and no rule covers it). An
            // answer given for some of a session's sites is kept for exactly those sites.
            let kind = (meta.vm != vm ? "session_load_elsewhere:\(vm)" : "session_load") + (only.map { "|sites=" + $0.joined(separator: ",") } ?? "")
            let o = approve(kind, name, question + "\n  Asked by \(who.describe).", who, vm: vm, site: nil, use: "session_load",
                            summary: "load the sealed session \(name) into profile \(profile)" + (only.map { " (only the cookies for \($0.joined(separator: ", ")))" } ?? ""),
                            identityProvider: idp,
                            proposedID: req["request_id"] as? String, ruleUse: (meta.vm == vm && !idp) ? "session_load" : nil)
            approval = o.how
            if !o.yes { return finish("session_load", fields, denied(o.why), who, approval: approval, requestID: o.requestID) }
            if let problem = runner.listenerProblem(t) {
                return finish("session_load", fields, problem.denied ? denied(problem.reason) : failed(problem.reason), who, approval: approval)
            }
        }
        guard var archive = openSession(p, name) else {
            return finish("session_load", fields, failed("the sealed session file is missing or was changed"), who, approval: approval)
        }
        defer { wipe(&archive) }
        if cookies {
            // Imported cookies: each value encrypted with the VM's Chrome key, merged into the profile's own store
            // (with --site, only the cookies those sites get).
            guard var input = cookiesForVM(archive, only: only) else {
                return finish("session_load", fields, failed("the sealed cookies can't be read"), who, approval: approval)
            }
            let sent = cookieCount(input)
            fields["cookies"] = sent
            guard sent != 0 else {
                wipe(&input)
                return finish("session_load", fields, failed("\(name) holds no cookies for \((only ?? []).joined(separator: ", "))"), who, approval: approval)
            }
            wipe(&archive)
            archive = input
            wipe(&input)
        }
        guard Date() < expires, receivedSignal == 0 else {
            return finish("session_load", fields, lockedResult("the vault locked before the session was sent"), who, approval: approval)
        }
        let r: ProcResult
        let script = cookies ? cookieLoadScript(user: t.binding.guest_user, profile: profile) : sessionLoadScript(user: t.binding.guest_user, profile: profile)
        do { r = try runner.opsSSH(t, remote: script, input: archive, timeout: TimeInterval(min(600, remaining()))) }
        catch { return finish("session_load", fields, failed("cannot reach the VM"), who, approval: approval) }
        if let why = sealedSessionRefusal(r) { return finish("session_load", fields, failed(why), who, approval: approval) }
        if r.interrupted || r.timedOut || r.status != 0 {
            // The VM has the session now and its stderr could carry any of it: only this process's own words go on.
            let why = r.interrupted ? "interrupted" : r.timedOut ? "the VM didn't finish in time" : sessionFailure(r.status, profile: profile, saving: false)
            return finish("session_load", fields, (r.status == 4 || r.status == 5) ? denied(why) : failed(why), who, approval: approval)
        }
        do {
            try updateVault(paths, key) { p in
                p.sessions[name]?.loads += 1
                p.sessions[name]?.last_loaded = isoTime()
            }
        } catch { say("          (couldn't record the use in the vault)") }
        return finish("session_load", fields, ["ok": true, "status": "ok", "bytes": sizeBound(archive.count)], who, approval: approval)
    }
}

/// A fill or sign-in may name the Chrome it's for: its profile, its browser process id (`--browser-pid`), or both.
/// In any case the VM refuses when more than one tab there is on the site (secret-fill). nil: fine.
func browserBindingProblem(_ req: [String: Any], profile: String?, _ who: Requester) -> String? {
    if let raw = req["browser_pid"] {
        guard let n = raw as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.intValue > 1, n.intValue < Int(Int32.max) else {
            return "--browser-pid: a process id"
        }
    }
    return nil
}

/// Why a sealed-session save or load failed, from its exit status alone: the VM's stderr is never passed on,
/// since after a load the VM holds the session's cookies and could write them there.
func sessionFailure(_ status: Int32, profile: String, saving: Bool) -> String {
    switch status {
    case 3: return "no Chrome profile named \(profile) in the VM"
    case 4: return "Chrome is using profile \(profile); end its browser session first"
    case 5: return "the VM refused to " + (saving ? "pack" : "unpack") + " it safely (a symbolic link, an entry outside the profile, a special file, or a cookie store it can't use)"
    case 6: return "the VM has no driver profile \(profile) with a cookie store yet: open it once in page-mode Chrome (browser_prepare, profile isolated_named \(profile)), end that session, then load again"
    default: return "the VM couldn't " + (saving ? "save" : "load") + " it (its own output isn't passed on)"
    }
}

/// Sealed sessions sign in to the VM as the desktop user with its static ops key. A VM on SSH
/// certificates (SSH_AUTH=cert) refuses that key: say so, rather than a bare ssh error.
func sealedSessionRefusal(_ r: ProcResult) -> String? {
    guard r.status == 255, String(decoding: r.stderr, as: UTF8.self).contains("Permission denied") else { return nil }
    return "the VM refused the desktop account's static key: sealed sessions need SSH_AUTH=key for now "
        + "(a VM on SSH certificates refuses it; fills and API tokens use their own accounts and still work)"
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
