// mcomp-vault: approvals beyond the unlock terminal. Pending requests with ids (`requests`,
// `approve ID`, `deny ID`), confirming with Touch ID or the login password (LocalAuthentication,
// shown by macOS itself) or with the vault passphrase, unattended rules that expire, and the
// identity providers that always ask.
//
// Who can approve: only the person. Every process here runs as the same user as Claude Code, so a
// request on the vault's socket proves nothing about who sent it. An approval therefore needs one
// of: a line typed on the unlock terminal (as before), the system's own Touch ID / password dialog
// shown by the unlock process (nothing another process sends can answer it), or a proof that only
// the vault passphrase can make (an HMAC under a key derived from the vault's data key, which the
// approving command gets by opening the vault with the passphrase). A terminal of its own isn't
// enough on its own: `script` or `expect` can make one. Denying, listing and removing a rule
// only restrict, so they need nothing.
import Darwin
import Foundation
import LocalAuthentication

// MARK: - identity providers

/// Sign-in pages of identity providers: a login there opens every site that trusts it, so a use on
/// one always asks, every time. `NAME session` and `NAME unlock` answers, `unlock --confirm session`,
/// a secret's `--approval session` and unattended rules never cover them. `*.` covers subdomains.
let identityProviderHosts: [String] = [
    // Google
    "accounts.google.com", "myaccount.google.com", "accounts.youtube.com",
    // Microsoft (Entra ID, personal accounts)
    "login.microsoftonline.com", "login.microsoftonline.us", "login.partner.microsoftonline.cn", "login.microsoft.com",
    "login.live.com", "login.windows.net", "account.live.com", "account.microsoft.com",
    // Apple
    "appleid.apple.com", "idmsa.apple.com", "account.apple.com", "iforgot.apple.com",
    // Okta
    "*.okta.com", "*.oktapreview.com", "*.okta-emea.com", "*.okta-gov.com",
]

func isIdentityProviderHost(_ host: String) -> Bool {
    let h = host.lowercased()
    for e in identityProviderHosts {
        if e.hasPrefix("*.") {
            if h.hasSuffix("." + e.dropFirst(2)) { return true }
        } else if h == e {
            return true
        }
    }
    return false
}

/// The host of a canonical origin (https://host[:port], or an api secret's host[:port]).
func originHost(_ origin: String) -> String {
    var s = origin
    if let r = s.range(of: "://") { s = String(s[r.upperBound...]) }
    if let c = s.lastIndex(of: ":") { s = String(s[..<c]) }
    return s
}

func isIdentityProviderOrigin(_ origin: String) -> Bool { isIdentityProviderHost(originHost(origin)) }

/// Whether a cookie with this host key is sent to an identity provider's sign-in pages: a host-only
/// cookie ("accounts.google.com") on that host, a domain cookie (".google.com") on it or any of its
/// subdomains.
func cookieReachesIdentityProvider(_ hostKey: String) -> Bool {
    let domainCookie = hostKey.hasPrefix(".")
    let d = (domainCookie ? String(hostKey.dropFirst()) : hostKey).lowercased()
    guard !d.isEmpty else { return false }
    for e in identityProviderHosts {
        if e.hasPrefix("*.") {
            let base = String(e.dropFirst(2))
            if d.hasSuffix("." + base) || (domainCookie && d == base) { return true }
        } else if d == e || (domainCookie && e.hasSuffix("." + d)) {
            return true
        }
    }
    return false
}

// MARK: - masked usernames

/// A username as the audit log, `requests` and results show it: its first character, then `***`,
/// then the domain of an email address (`a***@example.com`).
func maskUsername(_ u: String) -> String {
    let s = clean(u, max: 256)
    guard let first = s.first else { return "" }
    if let at = s.lastIndex(of: "@"), at > s.startIndex, s.index(after: at) < s.endIndex {
        return String(first) + "***" + String(s[at...])
    }
    return String(first) + "***"
}

// MARK: - unattended rules

/// A standing approval the person wrote: this use of one secret (or of any secret on one site, or of one
/// sealed session) by Claude Code sessions in one folder, without asking, until it expires. Made only with
/// Touch ID / the login password or the passphrase; never covers an identity provider.
struct UnattendedRule: Codable {
    var id: String                  // 8 hex digits
    var use: String                 // fill (fill and sign-in) | token | session_load
    var secret: String?             // fill, token: this secret
    var site: String?               // fill: any secret's fill into this origin
    var session: String?            // session_load: this sealed session (into the VM it belongs to)
    var project: String             // Claude Code sessions working in this folder or below
    var created: String
    var expires: String
    var expires_unix: Double
    var confirmed: String           // local-auth | passphrase

    var live: Bool { Date().timeIntervalSince1970 < expires_unix }

    func covers(use u: String, name: String, site s: String?, who: Requester) -> Bool {
        guard use == u, live, let p = who.project, who.claudePID != nil, insideAny(p, [project]) else { return false }
        switch u {
        case "session_load": return session == name
        default:
            if let sec = secret { return sec == name }
            if let st = site, let s { return st == s }
            return false
        }
    }

    var what: String {
        switch use {
        case "session_load": return "load the sealed session \(session ?? "?")"
        case "token": return "API tokens for \(secret ?? "?")"
        default: return secret.map { "fills of \($0)" } ?? "fills into \(site ?? "?")"
        }
    }

    var record: [String: Any] {
        var d: [String: Any] = ["rule_id": id, "use": use, "project": project, "expires": expires, "confirmed": confirmed]
        if let s = secret { d["name"] = s }
        if let s = site { d["origin"] = s }
        if let s = session { d["session"] = s }
        return d
    }

    var listing: [String: Any] {
        var d = record
        d["id"] = id
        d["created"] = created
        d["expires_in_s"] = max(0, Int(expires_unix - Date().timeIntervalSince1970))
        d["what"] = what
        return d
    }
}

let maxRuleSeconds = 30 * 86400

func newRuleID() -> String { randomBytes(4).map { String(format: "%02x", $0) }.joined() }

// MARK: - pending requests

/// How a waiting request was decided.
enum Decision {
    case terminal(ApprovalScope)        // the name typed on the unlock terminal (with its scope)
    case terminalNo                     // anything else typed there, the terminal closing
    case localAuth                      // Touch ID or the login password, in the system's dialog
    case passphrase                     // `approve ID --passphrase`
    case denied(String)                 // `deny ID`, or a refusal at the Touch ID dialog
}

/// A use waiting for the person. Lives only while the unlock process waits for its answer.
final class PendingRequest {
    let id: String
    let challenge: String = randomBytes(16).map { String(format: "%02x", $0) }.joined()
    let use: String                     // fill | login | token | session_save | session_load
    let name: String
    let vm: String
    let site: String?
    let usernameHint: String?
    let who: Requester
    let created = Date()
    let identityProvider: Bool
    let summary: String                 // one line: what approving it allows (shown, and signed by a passphrase proof)
    var decision: Decision?
    var selectedScope: ApprovalScope?   // presence mode: what was typed, waiting for the dialog or the passphrase to confirm it
    var localAuth: LocalAuthAttempt?
    var autoLocalAuth = false           // the dialog `unlock --touch-id` showed by itself
    var waiters: [Int32] = []           // `approve ID` clients waiting for the dialog's answer

    init(id: String, use: String, name: String, vm: String, site: String?, usernameHint: String?, who: Requester,
         identityProvider: Bool, summary: String) {
        self.id = id
        self.use = use
        self.name = name
        self.vm = vm
        self.site = site
        self.usernameHint = usernameHint
        self.who = who
        self.identityProvider = identityProvider
        self.summary = summary
    }

    var listing: [String: Any] {
        var d: [String: Any] = ["id": id, "use": use, "name": name, "vm": vm, "age_s": Int(Date().timeIntervalSince(created)),
                                "created": isoTime(created), "requester": who.record, "asked_by": who.describe,
                                "summary": summary, "challenge": challenge, "identity_provider": identityProvider]
        if let s = site { d["site"] = s }
        if let u = usernameHint { d["username_hint"] = u }
        if let p = who.project { d["project"] = p }
        return d
    }
}

// MARK: - Touch ID or the login password (LocalAuthentication)

/// One request to macOS for the device owner, answered on another thread.
final class LocalAuthAttempt: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false
    private var allowed = false
    private var reason = ""
    private var saidNo = false
    private var context: LAContext?

    var done: Bool { lock.lock(); defer { lock.unlock() }; return finished }
    var ok: Bool { lock.lock(); defer { lock.unlock() }; return allowed }
    var why: String { lock.lock(); defer { lock.unlock() }; return reason }
    /// The person answered no (cancelled, or failed to authenticate), as opposed to no dialog being possible.
    var refused: Bool { lock.lock(); defer { lock.unlock() }; return saidNo }

    func set(_ c: LAContext) { lock.lock(); context = c; lock.unlock() }

    func finish(_ ok: Bool, _ why: String, refused: Bool = false) {
        lock.lock()
        if !finished { finished = true; allowed = ok; reason = why; saidNo = refused }
        lock.unlock()
    }

    func cancel() {
        lock.lock()
        let c = context
        context = nil
        lock.unlock()
        c?.invalidate()
        finish(false, "cancelled")
    }
}

/// Ask macOS to confirm the device owner (`deviceOwnerAuthentication`): Touch ID, or the login password on a
/// Mac without it. macOS draws the dialog, naming this program and `reason`; no other process can answer it.
/// Returns at once; poll `done`. Test builds never show the real dialog: MCOMP_VAULT_TEST_LOCAL_AUTH stands
/// in for the person there (allow, deny, wait), and without it there is none. Release builds don't contain
/// that switch. MCOMP_VAULT_LOCAL_AUTH=noninteractive (release builds too) never shows the dialog: for a Mac
/// nobody sits at, where approvals then come from the terminal or the passphrase.
func startLocalAuth(_ reason: String) -> LocalAuthAttempt {
    let a = LocalAuthAttempt()
    #if MCOMP_VAULT_TESTING
    testTrace("local-auth " + reason)
    switch ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_LOCAL_AUTH"] ?? "" {
    case "allow": DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) { a.finish(true, "") }
    case "deny": DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) { a.finish(false, "you cancelled the dialog", refused: true) }
    case "wait": break
    default: a.finish(false, "a test build never shows the Touch ID dialog")
    }
    #else
    let ctx = LAContext()
    if ProcessInfo.processInfo.environment["MCOMP_VAULT_LOCAL_AUTH"] == "noninteractive" { ctx.interactionNotAllowed = true }
    a.set(ctx)
    var err: NSError?
    guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &err) else {
        a.finish(false, "this Mac can't ask for Touch ID or the login password here (\(localAuthError(err)))")
        return a
    }
    ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: clean(reason, max: 200)) { ok, error in
        let e = error as NSError?
        let no = e.map { $0.domain == LAErrorDomain && ($0.code == LAError.userCancel.rawValue || $0.code == LAError.authenticationFailed.rawValue) } ?? false
        a.finish(ok, ok ? "" : localAuthError(e), refused: no)
    }
    #endif
    return a
}

func localAuthError(_ e: NSError?) -> String {
    guard let e else { return "no answer" }
    if e.domain == LAErrorDomain {
        switch e.code {
        case LAError.userCancel.rawValue: return "you cancelled the dialog"
        case LAError.authenticationFailed.rawValue: return "Touch ID or the password wasn't accepted"
        case LAError.notInteractive.rawValue: return "no dialog can be shown here (not interactive)"
        case LAError.systemCancel.rawValue, LAError.appCancel.rawValue: return "the dialog was closed"
        case LAError.passcodeNotSet.rawValue: return "this account has no login password"
        default: break
        }
    }
    return clean("\(e.localizedDescription) (\(e.code))", max: 160)
}

// MARK: - presence mode

/// Whether this Mac has Touch ID with a finger enrolled (LocalAuthentication's biometrics policy; no dialog).
/// A sensor with no fingerprints counts as none. Test builds take MCOMP_VAULT_TEST_BIOMETRY (available,
/// not-enrolled, none; default none) instead; release builds don't contain that switch.
enum Biometry: String { case touchID = "touch-id", notEnrolled = "not-enrolled", none = "none" }

func detectBiometry() -> Biometry {
    #if MCOMP_VAULT_TESTING
    switch ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_BIOMETRY"] ?? "" {
    case "available": return .touchID
    case "not-enrolled": return .notEnrolled
    default: return .none
    }
    #else
    let ctx = LAContext()
    var err: NSError?
    if ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err) { return .touchID }
    if let e = err, e.domain == LAErrorDomain {
        if e.code == LAError.biometryNotEnrolled.rawValue { return .notEnrolled }
        if e.code == LAError.biometryLockout.rawValue { return .touchID }   // a sensor and fingers, locked for now
    }
    return .none
    #endif
}

/// Presence mode: every approval, and every grant (`NAME session`, `NAME unlock`), needs macOS's dialog (Touch ID
/// or the login password) or the passphrase (`approve ID --passphrase`); what's typed at the unlock terminal only
/// chooses how far it reaches. The default: on where Touch ID is (so nothing typed into a terminal a program could
/// control approves), off elsewhere. Changed by `unlock --require-presence | --no-require-presence`, else by the
/// vault's setting (`presence on|off|auto`). Returns (on, why, a warning for the unlock terminal or nil).
func resolvePresence(flag: Bool?, setting: String?, biometry b: Biometry) -> (Bool, String, String?) {
    let how = "`metacomputer secrets unlock --require-presence`, or `metacomputer secrets presence on` to keep it"
    if let f = flag {
        return f ? (true, "--require-presence", nil)
            : (false, "--no-require-presence", "typed answers approve here (--no-require-presence): a program that can type into this terminal could approve")
    }
    if setting == "on" { return (true, "the vault's setting (presence on)", nil) }
    if setting == "off" {
        return (false, "the vault's setting (presence off)", "typed answers approve here (presence off): a program that can type into this terminal could approve; \(how) changes that")
    }
    switch b {
    case .touchID: return (true, "this Mac has Touch ID", nil)
    case .notEnrolled:
        return (false, "this Mac's Touch ID has no fingerprint enrolled",
                "this Mac's Touch ID sensor has no fingerprint enrolled, so typed answers approve here; a program that can type into this terminal could approve. Enroll a finger, or \(how) (your login password then confirms)")
    case .none:
        return (false, "this Mac has no Touch ID",
                "this Mac has no Touch ID, so typed answers approve here; a program that can type into this terminal could approve. \(how) makes every approval need your login password in macOS's dialog")
    }
}

/// `presence on|off|auto`: the vault's own setting for presence mode (the passphrase, like `policy`).
func cmdPresence(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: [])
    guard a.positional.count == 1, ["on", "off", "auto"].contains(a.positional[0]) else {
        throw usageError("usage: presence on|off|auto  (auto: on where this Mac has Touch ID)")
    }
    let mode = a.positional[0]
    try requireTTY("presence")
    installPromptSignalHandlers()
    let (key, _) = try unlockKey(paths)
    defer { key.wipeKey() }
    try updateVault(paths, key) { p in p.require_presence = mode == "auto" ? nil : mode }
    audit(paths, ["op": "presence", "result": "ok", "mode": mode], macKey: vaultAuditKey(paths, key))
    let (on, why, _) = resolvePresence(flag: nil, setting: mode == "auto" ? nil : mode, biometry: detectBiometry())
    oprint("Presence mode: \(mode) (now \(on ? "on" : "off"): \(why)). It applies from the next unlock; `unlock --require-presence` or `--no-require-presence` overrides it for one unlock.")
    return Exit.ok
}

// MARK: - the passphrase proof

/// A key only a process holding the vault's data key has: the unlock process, and a command that just opened
/// the vault with the passphrase.
func approvalProofKey(_ key: VaultKey) -> [UInt8] {
    hkdf32(key.bytes, salt: Array("mcomp-vault".utf8), info: "mcomp-vault|approval-proof|1")
}

/// Binds the approval to this request, its one-time challenge and the summary the person was shown.
func approvalProof(_ k: [UInt8], id: String, challenge: String, summary: String) -> String {
    hmacSHA256Hex(key: k, data: Array("mcomp-vault|approve|1|\(id)|\(challenge)|\(summary)".utf8))
}

func constantTimeEqual(_ a: String, _ b: String) -> Bool {
    let x = Array(a.utf8), y = Array(b.utf8)
    guard x.count == y.count else { return false }
    var d: UInt8 = 0
    for i in 0..<x.count { d |= x[i] ^ y[i] }
    return d == 0
}

// MARK: - commands: requests, approve, deny

func validRequestID(_ s: String) -> Bool { matches(s, "^[0-9a-f]{8}$") }

func cmdRequests(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["json"])
    guard a.positional.isEmpty else { throw usageError("usage: requests [--json]") }
    let res: [String: Any]
    do { res = try request(paths, ["op": "requests"], timeout: 15) } catch ClientError.locked {
        if a.has("json") { oprint(jsonLine(["ok": false, "reason": "locked", "requests": []])) } else { oprint("The vault is locked: nothing waits for approval.") }
        return Exit.locked
    }
    let list = res["requests"] as? [[String: Any]] ?? []
    if a.has("json") {
        oprint(jsonPretty(["requests": list.map { r in r.filter { $0.key != "challenge" } }]))
        return Exit.ok
    }
    if list.isEmpty { oprint("Nothing waits for approval."); return Exit.ok }
    oprint("Waiting for approval:")
    for r in list {
        oprint(clean("  \(r["id"] ?? "")  \(r["age_s"] ?? 0) s  \(r["summary"] ?? "")"
                     + ((r["username_hint"] as? String).map { "  user \($0)" } ?? "")
                     + ((r["identity_provider"] as? Bool == true) ? "  (identity provider)" : ""), max: 600))
    }
    oprint("Approve one with `metacomputer secrets approve ID` (Touch ID or your login password; --passphrase uses the vault passphrase), or refuse it with `metacomputer secrets deny ID`.")
    return Exit.ok
}

func cmdApprove(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["passphrase", "json"])
    guard a.positional.count == 1, validRequestID(a.positional[0]) else { throw usageError("usage: approve ID [--passphrase]  (ID: 8 hex digits, from `requests`)") }
    let id = a.positional[0]
    var req: [String: Any] = ["op": "approve", "id": id, "method": "local-auth"]
    if a.has("passphrase") {
        try requireTTY("approve --passphrase")
        installPromptSignalHandlers()
        let pending: [String: Any]
        do { pending = try request(paths, ["op": "requests"], timeout: 15) } catch ClientError.locked {
            throw VaultError("the vault is locked: nothing waits for approval", code: Exit.locked)
        }
        guard let r = (pending["requests"] as? [[String: Any]] ?? []).first(where: { ($0["id"] as? String) == id }),
              let challenge = r["challenge"] as? String, let summary = r["summary"] as? String else {
            throw VaultError("no request \(id) waits for approval (metacomputer secrets requests)")
        }
        oprint(clean("Approve request \(id): \(summary)?", max: 600))
        oprint("Your vault passphrase approves this one use; Ctrl-C refuses nothing and leaves it waiting.")
        let (key, _) = try unlockKey(paths)
        var k = approvalProofKey(key)
        defer { wipe(&k); key.wipeKey() }
        req["method"] = "passphrase"
        req["proof"] = approvalProof(k, id: id, challenge: challenge, summary: summary)
    }
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 200) } catch ClientError.locked {
        throw VaultError("the vault is locked: nothing waits for approval", code: Exit.locked)
    }
    if a.has("json") { printResult(res, ["ok", "id", "reason"]); return exitFor(res) }
    if res["ok"] as? Bool == true { oprint("Approved request \(id) for this one use.") } else {
        eprint(clean("mcomp-vault: not approved: \(res["reason"] as? String ?? "no answer")", max: 400))
    }
    return exitFor(res)
}

func cmdDeny(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["json"])
    guard a.positional.count == 1, validRequestID(a.positional[0]) else { throw usageError("usage: deny ID  (ID: 8 hex digits, from `requests`)") }
    let id = a.positional[0]
    let res: [String: Any]
    do { res = try request(paths, ["op": "deny", "id": id], timeout: 15) } catch ClientError.locked {
        oprint("The vault is locked: nothing waits for approval.")
        return Exit.locked
    }
    if a.has("json") { printResult(res, ["ok", "id", "reason"]); return exitFor(res) }
    if res["ok"] as? Bool == true { oprint("Refused request \(id).") } else {
        eprint(clean("mcomp-vault: \(res["reason"] as? String ?? "no answer")", max: 400))
    }
    return exitFor(res)
}

// MARK: - commands: rules

/// The rule a `rule add` command describes, checked against the vault: (rule, error).
func buildRule(_ p: Payload, use: String, secret: String?, site: String?, session: String?, project: String, seconds: Int,
               confirmed: String) -> (UnattendedRule?, String?) {
    var u = use
    if let s = secret {
        guard let sec = p.secrets[s] else { return (nil, "no secret named \(s)") }
        if sec.kind == "api" {
            u = "token"
            if isIdentityProviderHost(originHost(sec.api_host ?? "")) { return (nil, "\(s) goes to an identity provider, which always asks: no rule can cover it") }
        } else {
            u = "fill"
            if !sec.origins.isEmpty && sec.origins.allSatisfy(isIdentityProviderOrigin) {
                return (nil, "every site of \(s) is an identity provider, which always asks: no rule can cover it")
            }
        }
    } else if let st = site {
        if isIdentityProviderOrigin(st) { return (nil, "\(st) is an identity provider's sign-in page, which always asks: no rule can cover it") }
        u = "fill"
    } else if let ss = session {
        guard let m = p.sessions[ss] else { return (nil, "no saved session named \(ss)") }
        if m.alwaysAsks {
            return (nil, "the sealed session \(ss) asks at every load (it holds, or as a whole browser profile may hold, an identity provider's sign-in): no rule can cover it")
        }
        u = "session_load"
    }
    let now = Date()
    let exp = now.addingTimeInterval(TimeInterval(seconds))
    return (UnattendedRule(id: newRuleID(), use: u, secret: secret, site: site, session: session, project: project,
                           created: isoTime(now), expires: isoTime(exp), expires_unix: exp.timeIntervalSince1970, confirmed: confirmed), nil)
}

func cmdRule(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: rule add (--secret NAME | --site https://site | --session NAME) --project DIR --for 8h [--passphrase] | rule list [--json] | rule remove ID"
    guard let sub = argv.first else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), values: ["secret", "site", "session", "project", "for"], flags: ["passphrase", "json"])
    switch sub {
    case "add":
        guard a.positional.isEmpty else { throw usageError(usage) }
        let picked = [a.last("secret"), a.last("site"), a.last("session")].compactMap { $0 }
        guard picked.count == 1 else { throw usageError("rule add: exactly one of --secret NAME, --site https://site or --session NAME") }
        if let s = a.last("secret") ?? a.last("session"), !validName(s) { throw usageError("names: letters, digits, '.', '_' and '-'") }
        var site: String?
        if let s = a.last("site") {
            guard let c = canonicalOrigin(s) else { throw usageError("--site: https://host[:port], no path") }
            site = c
        }
        guard let pr = a.last("project") else { throw usageError("rule add: --project DIR (the Claude Code sessions it covers: those working in DIR or below)") }
        var isDir: ObjCBool = false
        let project = realPath(absolutePath(pr)) ?? ""
        guard clean(project, max: 4096) == project, FileManager.default.fileExists(atPath: project, isDirectory: &isDir), isDir.boolValue else {
            throw usageError("--project \(pr): an existing folder")
        }
        guard project != "/" && project != realPath(homeDirectory()) else {
            throw usageError("--project \(pr): name the project's own folder, not / or your home folder")
        }
        guard let f = a.last("for") else { throw usageError("rule add: --for DURATION is required (a rule always expires): like 30m, 8h or 7d, at most 30d") }
        guard let seconds = parseDuration(f), seconds <= maxRuleSeconds else { throw usageError("--for: like 30m, 8h or 7d (at most 30d)") }
        let unlocked = (try? request(paths, ["op": "status"], timeout: 10))?["ok"] as? Bool == true
        if a.has("passphrase") || !unlocked {
            try requireTTY("rule add")
            installPromptSignalHandlers()
            let (key, existing) = try unlockKey(paths)
            defer { key.wipeKey() }
            let (rule, why) = buildRule(existing, use: "", secret: a.last("secret"), site: site, session: a.last("session"), project: project,
                                        seconds: seconds, confirmed: "passphrase")
            guard let rule else { throw VaultError(why ?? "can't make that rule") }
            try updateVault(paths, key) { p in p.rules = (p.rules ?? []).filter { $0.live } + [rule] }
            var rec = rule.record
            rec["op"] = "rule_add"
            rec["result"] = "ok"
            audit(paths, rec, macKey: vaultAuditKey(paths, key))
            oprint(clean("Rule \(rule.id): \(rule.what) without asking, for Claude Code sessions in \(tildePath(project)), until \(rule.expires).", max: 500))
            return Exit.ok
        }
        var req: [String: Any] = ["op": "rule_add", "project": project, "for_s": seconds]
        if let s = a.last("secret") { req["secret"] = s }
        if let s = site { req["site"] = s }
        if let s = a.last("session") { req["session"] = s }
        let res = try request(paths, req, timeout: 200)
        if res["ok"] as? Bool == true {
            oprint(clean("Rule \(res["id"] ?? "?"): \(res["what"] ?? "") without asking, for Claude Code sessions in \(tildePath(project)), until \(res["expires"] ?? "?").", max: 500))
            return Exit.ok
        }
        eprint(clean("mcomp-vault: no rule made: \(res["reason"] as? String ?? "no answer")", max: 400))
        return exitFor(res)
    case "list":
        let res: [String: Any]
        do { res = try request(paths, ["op": "rule_list"], timeout: 15) } catch ClientError.locked {
            if a.has("json") { oprint(jsonLine(["ok": false, "reason": "locked", "rules": []])) } else { oprint("The vault is locked: its rules are inside it. They apply only while it's unlocked.") }
            return Exit.locked
        }
        let list = res["rules"] as? [[String: Any]] ?? []
        if a.has("json") { oprint(jsonPretty(["rules": list])); return Exit.ok }
        oprint(list.isEmpty ? "No unattended rules." : "Unattended rules (uses that don't ask):")
        for r in list {
            oprint(clean("  \(r["id"] ?? "")  \(r["what"] ?? "") for Claude Code sessions in \(tildePath(r["project"] as? String ?? "?")), "
                         + "until \(r["expires"] ?? "?") (\((r["expires_in_s"] as? Int ?? 0) / 60) min left), confirmed with \(r["confirmed"] ?? "?")", max: 500))
        }
        return Exit.ok
    case "remove":
        guard a.positional.count == 1, validRequestID(a.positional[0]) else { throw usageError("usage: rule remove ID") }
        let id = a.positional[0]
        do {
            let res = try request(paths, ["op": "rule_remove", "id": id], timeout: 15)
            if res["ok"] as? Bool == true { oprint("Removed rule \(id)."); return Exit.ok }
            eprint(clean("mcomp-vault: \(res["reason"] as? String ?? "no answer")", max: 400))
            return exitFor(res)
        } catch ClientError.locked {
            // Locked: the rules are inside the vault; removing one needs the passphrase to rewrite it.
            try requireTTY("rule remove (while the vault is locked)")
            installPromptSignalHandlers()
            let (key, existing) = try unlockKey(paths)
            defer { key.wipeKey() }
            guard (existing.rules ?? []).contains(where: { $0.id == id }) else { throw VaultError("no rule \(id)") }
            try updateVault(paths, key) { p in p.rules = (p.rules ?? []).filter { $0.id != id && $0.live } }
            audit(paths, ["op": "rule_remove", "result": "ok", "rule_id": id], macKey: vaultAuditKey(paths, key))
            oprint("Removed rule \(id).")
            return Exit.ok
        }
    default:
        throw usageError(usage)
    }
}

// MARK: - command: policy (a secret's approval, without retyping its value)

func cmdPolicy(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["approval"], flags: [])
    guard a.positional.count == 1, validName(a.positional[0]), let ap = a.last("approval"), ["session", "each"].contains(ap) else {
        throw usageError("usage: policy NAME --approval session|each")
    }
    let name = a.positional[0]
    try requireTTY("policy")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    defer { key.wipeKey() }
    guard let s = existing.secrets[name] else { throw VaultError("no secret named \(name)") }
    try updateVault(paths, key) { p in p.secrets[name]?.approval = ap }
    audit(paths, ["op": "policy", "result": "ok", "name": name, "approval_mode": ap], macKey: vaultAuditKey(paths, key))
    let idp = s.kind == "api" ? isIdentityProviderHost(originHost(s.api_host ?? "")) : s.origins.contains(where: isIdentityProviderOrigin)
    oprint(ap == "session"
           ? "Uses of \(name) no longer ask (unless the unlock says --confirm each)" + (idp ? "; on an identity provider's site they still ask, every time." : ".")
           : "Every use of \(name) asks first.")
    return Exit.ok
}
