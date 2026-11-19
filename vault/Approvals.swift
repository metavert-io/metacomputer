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
    var use: String                 // fill (fill and sign-in) | token | session_load | code | relay
    var secret: String?             // fill, token: this secret
    var site: String?               // fill: any secret's fill into this origin; code, relay: the site codes go to
    var session: String?            // session_load: this sealed session (into the VM it belongs to)
    var project: String             // Claude Code sessions working in this folder or below ("" for a code or relay rule
                                    // made without --project: any requester, a script included)
    var created: String
    var expires: String
    var expires_unix: Double
    var confirmed: String           // local-auth | passphrase
    var source: String? = nil       // code: where the value comes from ("stdin": a program's input, no terminal)
    var from_host: String? = nil    // relay: the host of the page the code or link is read from
    var link_hosts: [String]? = nil // code, relay: other hosts a sign-in link may go to (a tracking redirect), named by you

    var live: Bool { Date().timeIntervalSince1970 < expires_unix }

    func covers(use u: String, name: String, site s: String?, who: Requester) -> Bool {
        guard use == u, live, let p = who.project, who.claudePID != nil, insideAny(p, [project]) else { return false }
        switch u {
        case "session_load": return session == name
        case "code", "relay": return false          // coversCode / coversRelay
        default:
            if let sec = secret { return sec == name }
            if let st = site, let s { return st == s }
            return false
        }
    }

    /// A code or relay rule's requester check: with a project, Claude Code sessions working there (as other rules);
    /// without one, any requester (a script with no terminal is what a code rule is for).
    func requesterOK(_ who: Requester) -> Bool {
        if project.isEmpty { return true }
        guard let p = who.project, who.claudePID != nil else { return false }
        return insideAny(p, [project])
    }

    /// A one-time code from a program's input (no terminal) filled into `site` (never an identity provider: callers check).
    func coversCode(site s: String, who: Requester) -> Bool {
        use == "code" && live && site == s && source == "stdin" && requesterOK(who)
    }

    /// A code or link read from a page on `fromHost` and filled or opened in `site`.
    func coversRelay(site s: String, fromHost: String, who: Requester) -> Bool {
        use == "relay" && live && site == s && from_host == fromHost && requesterOK(who)
    }

    var what: String {
        switch use {
        case "session_load": return "load the sealed session \(session ?? "?")"
        case "token": return "API tokens for \(secret ?? "?")"
        case "code": return "one-time codes from a program's input into \(site ?? "?")" + linkWords
        case "relay": return "codes and sign-in links relayed from \(from_host ?? "?") into \(site ?? "?")" + linkWords
        default: return secret.map { "fills of \($0)" } ?? "fills into \(site ?? "?")"
        }
    }

    var linkWords: String { (link_hosts ?? []).isEmpty ? "" : " (links may also go to \((link_hosts ?? []).joined(separator: ", ")))" }

    var record: [String: Any] {
        var d: [String: Any] = ["rule_id": id, "use": use, "project": project, "expires": expires, "confirmed": confirmed]
        if let s = secret { d["name"] = s }
        if let s = site { d["origin"] = s }
        if let s = session { d["session"] = s }
        if let s = source { d["source"] = s }
        if let h = from_host { d["from_host"] = h }
        if let l = link_hosts, !l.isEmpty { d["link_hosts"] = l }
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

/// The longest text a confirmation (macOS's dialog) is given: a rule whose text is longer can't be confirmed there.
let maxConfirmText = 400

/// What confirming a rule allows, from exactly the fields that are stored: the secret, site or session, the project
/// and the absolute expiry. The dialog and the passphrase prompt show this text, and the rule stored must give it back
/// byte for byte.
func ruleConfirmText(secret: String?, site: String?, session: String?, project: String, expires: String) -> String {
    let what = secret.map { "uses of the secret \($0)" } ?? session.map { "loads of the sealed session \($0)" } ?? "fills into \(site ?? "?")"
    return "make an unattended rule: allow \(what) without asking, for Claude Code sessions in \(tildePath(project)), until \(expires)"
}

/// What confirming a code or relay rule allows, from exactly its stored fields (as ruleConfirmText).
func codeRuleConfirmText(use: String, site: String, fromHost: String?, linkHosts: [String], project: String, expires: String) -> String {
    let what = use == "relay"
        ? "relaying one-time codes and sign-in links read from pages on \(fromHost ?? "?") into \(site)"
        : "one-time codes from a program's input (no terminal) to be filled into \(site)"
    let links = linkHosts.isEmpty ? "" : ", with sign-in links also to \(linkHosts.joined(separator: ", "))"
    let whom = project.isEmpty ? "for any program that asks" : "for Claude Code sessions in \(tildePath(project))"
    return "make an unattended rule: allow \(what)\(links) without asking, \(whom), until \(expires)"
}

extension UnattendedRule {
    var confirmText: String {
        if use == "code" || use == "relay" {
            return codeRuleConfirmText(use: use, site: site ?? "?", fromHost: from_host, linkHosts: link_hosts ?? [], project: project, expires: expires)
        }
        return ruleConfirmText(secret: secret, site: site, session: session, project: project, expires: expires)
    }
}

/// The longest a code or relay rule lasts (the default when no --expires is given is shorter: defaultCodeRuleSeconds).
let defaultCodeRuleSeconds = 7 * 86400

/// A code or relay rule, checked: (rule, error). Identity providers always ask, so no rule names one as its site or
/// as a link's host.
func buildCodeRule(use: String, site: String, fromHost: String?, linkHosts: [String], project: String, expires exp: Date,
                   confirmed: String) -> (UnattendedRule?, String?) {
    guard use == "code" || use == "relay" else { return (nil, "a code rule is `code` or `relay`") }
    guard canonicalOrigin(site) == site else { return (nil, "the site is https://host[:port], no path") }
    if isIdentityProviderOrigin(site) { return (nil, "\(site) is an identity provider's sign-in page, which always asks: no rule can cover it") }
    if use == "relay" {
        guard let h = fromHost, validSiteHost(h) else { return (nil, "--from-host: the host of the page the code is read from, like mail.example.com") }
    }
    for h in linkHosts {
        guard validSiteHost(h) else { return (nil, "--link-host: a host name like links.example.com") }
        if isIdentityProviderHost(h) { return (nil, "\(h) is an identity provider's, which always asks: no rule can send links there") }
    }
    let now = Date()
    return (UnattendedRule(id: newRuleID(), use: use, secret: nil, site: site, session: nil, project: project,
                           created: isoTime(now), expires: isoTime(exp), expires_unix: exp.timeIntervalSince1970, confirmed: confirmed,
                           source: use == "code" ? "stdin" : nil, from_host: use == "relay" ? fromHost?.lowercased() : nil,
                           link_hosts: linkHosts.isEmpty ? nil : linkHosts.map { $0.lowercased() }), nil)
}

func newRuleID() -> String { randomBytes(4).map { String(format: "%02x", $0) }.joined() }

// MARK: - pending requests

/// How a waiting request was decided.
enum Decision {
    case terminal(ApprovalScope)        // the name typed on the unlock terminal (with its scope)
    case terminalNo                     // anything else typed there, the terminal closing
    case localAuth(ApprovalScope)       // Touch ID or the login password, in a dialog that named this scope
    case passphrase(ApprovalScope)      // `approve ID --passphrase [--scope S]`: a proof over this scope
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
    var localAuth: LocalAuthAttempt?    // the dialog in flight, if any: it names one scope, and only grants that one
    var autoLocalAuth = false           // the dialog `unlock --touch-id` showed by itself
    var waiters: [Int32] = []           // `approve ID` clients waiting for the dialog's answer
    var singleUse = false               // a one-time code or link: only this use, never a grant
    var forceLocalAuth = false          // the person is at this Mac (the code dialog): macOS's dialog shows at once

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

/// One request to macOS for the device owner, answered on another thread. Its scope is fixed when it starts and
/// named in the dialog's text: allowing it grants that scope and no other.
final class LocalAuthAttempt: @unchecked Sendable {
    let scope: ApprovalScope
    init(scope: ApprovalScope) { self.scope = scope }
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
func startLocalAuth(_ reason: String, scope: ApprovalScope = .use) -> LocalAuthAttempt {
    let a = LocalAuthAttempt(scope: scope)
    #if MCOMP_VAULT_TESTING
    testTrace("local-auth " + clean(reason, max: maxConfirmText))   // what the dialog would show
    testLocalAuthCount += 1
    let mode = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_LOCAL_AUTH"] ?? ""
    // allow-first-after:N: the first dialog of this process is allowed N seconds after it opens; later ones wait.
    if mode.hasPrefix("allow-first-after:"), let secs = Double(mode.dropFirst("allow-first-after:".count)) {
        if testLocalAuthCount == 1 { DispatchQueue.global().asyncAfter(deadline: .now() + secs) { a.finish(true, "") } }
        return a
    }
    switch mode {
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
    ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: clean(reason, max: maxConfirmText)) { ok, error in
        let e = error as NSError?
        let no = e.map { $0.domain == LAErrorDomain && ($0.code == LAError.userCancel.rawValue || $0.code == LAError.authenticationFailed.rawValue) } ?? false
        a.finish(ok, ok ? "" : localAuthError(e), refused: no)
    }
    #endif
    return a
}

#if MCOMP_VAULT_TESTING
nonisolated(unsafe) var testLocalAuthCount = 0
#endif

/// The words a dialog, a passphrase prompt and the audit use for how far an approval reaches.
func scopeWords(_ s: ApprovalScope, name: String) -> String {
    switch s {
    case .use: return "for this one use"
    case .session: return "for this use and the rest of this Claude Code session"
    case .unlock: return "for every use of \(name) until the vault locks"
    }
}

func scopeName(_ s: ApprovalScope) -> String { s == .use ? "use" : (s == .session ? "session" : "unlock") }

func parseScope(_ s: String) -> ApprovalScope? { ["use": .use, "session": .session, "unlock": .unlock][s] }

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

/// What LocalAuthentication's biometrics policy says about this Mac (no dialog): Touch ID with a finger enrolled
/// (or locked out for now), a sensor with no finger enrolled, no sensor (no Touch ID keyboard paired), or that it
/// can't tell right now: a paired Touch ID keyboard that's disconnected, biometry unavailable, any other error.
/// Only a clean answer counts as no Touch ID; anything else is `unavailable` and keeps presence mode on.
/// (macOS answers "not enrolled" on a Mac that has no sensor at all but could take a Touch ID keyboard.) Test
/// builds take MCOMP_VAULT_TEST_BIOMETRY (available, not-enrolled, none, unavailable; default none) instead;
/// release builds don't contain that switch. Returns the answer and, for `unavailable`, what the check said.
enum Biometry: String { case touchID = "touch-id", notEnrolled = "not-enrolled", none = "none", unavailable = "unavailable" }

func detectBiometry() -> (Biometry, String) {
    #if MCOMP_VAULT_TESTING
    switch ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_BIOMETRY"] ?? "" {
    case "available": return (.touchID, "")
    case "not-enrolled": return (.notEnrolled, "")
    case "unavailable": return (.unavailable, "the Touch ID keyboard is disconnected (test stand-in)")
    default: return (.none, "")
    }
    #else
    let ctx = LAContext()
    var err: NSError?
    if ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err) { return (.touchID, "") }
    guard let e = err else { return (.unavailable, "the check gave no answer") }
    guard e.domain == LAErrorDomain else { return (.unavailable, clean("\(e.domain) \(e.code)", max: 80)) }
    switch e.code {
    case LAError.biometryNotEnrolled.rawValue: return (.notEnrolled, "")
    case LAError.biometryLockout.rawValue: return (.touchID, "")          // a sensor and fingers, locked for now
    case LAError.biometryNotPaired.rawValue: return (.none, "")           // no Touch ID keyboard paired
    case LAError.biometryDisconnected.rawValue: return (.unavailable, "the paired Touch ID keyboard is disconnected")
    case LAError.biometryNotAvailable.rawValue: return (.unavailable, "Touch ID isn't available right now")
    default: return (.unavailable, clean("\(e.localizedDescription) (\(e.code))", max: 120))
    }
    #endif
}

/// Presence mode: every approval, and every grant (`NAME session`, `NAME unlock`), needs macOS's dialog (Touch ID
/// or the login password) or the passphrase (`approve ID --passphrase`); what's typed at the unlock terminal only
/// chooses how far it reaches. The default fails closed: on where Touch ID is, on when the check can't tell, and on
/// for good once this vault has seen Touch ID enrolled on this Mac (`seen`, kept inside the vault), until the
/// person turns it off (`presence off`, with the passphrase). Only a clean "no sensor" or "no finger enrolled" on a
/// vault that never saw Touch ID leaves typed answers as the default. Changed by `unlock --require-presence |
/// --no-require-presence`, else by the vault's setting (`presence on|off|auto`). Returns (on, why, a warning for the
/// unlock terminal or nil).
func resolvePresence(flag: Bool?, setting: String?, biometry b: Biometry, detail: String = "", seen: Bool = false) -> (Bool, String, String?) {
    let how = "`metacomputer secrets unlock --require-presence`, or `metacomputer secrets presence on` to keep it"
    if let f = flag {
        return f ? (true, "--require-presence", nil)
            : (false, "--no-require-presence", "typed answers approve here (--no-require-presence): a program that can type into this terminal could approve")
    }
    if setting == "on" { return (true, "the vault's setting (presence on)", nil) }
    if setting == "off" {
        return (false, "the vault's setting (presence off)", "typed answers approve here (presence off): a program that can type into this terminal could approve; \(how) changes that")
    }
    let off = "`metacomputer secrets presence off` (with the passphrase) turns it off if you mean it"
    switch b {
    case .touchID: return (true, "this Mac has Touch ID", nil)
    case .unavailable:
        return (true, "Touch ID can't be checked right now (\(detail)), so presence mode stays on",
                "Touch ID can't be checked right now (\(detail)): presence mode stays on, so the dialog asks for your login password or Touch ID once it's back. \(off)")
    case _ where seen:
        return (true, "this vault has seen Touch ID enrolled on this Mac, so presence mode stays on",
                "this Mac's Touch ID reports \(b == .notEnrolled ? "no finger enrolled" : "no sensor") now, but this vault has seen it enrolled: presence mode stays on. \(off)")
    case .notEnrolled:
        return (false, "this Mac's Touch ID has no fingerprint enrolled",
                "this Mac's Touch ID sensor has no fingerprint enrolled, so typed answers approve here; a program that can type into this terminal could approve. Enroll a finger, or \(how) (your login password then confirms)")
    case .none:
        return (false, "this Mac has no Touch ID",
                "this Mac has no Touch ID, so typed answers approve here; a program that can type into this terminal could approve. \(how) makes every approval need your login password in macOS's dialog")
    }
}

/// What the audit log says about presence: whether Touch ID was ever seen enrolled, and the last `presence on|off|auto`
/// setting that is authenticated: its creation MAC checks out under the vault's audit key (only a command that opened
/// the vault with the passphrase can make one) and it sits where the chain says (its seq follows the line before it and
/// its prev is that line's hash, so a genuine old record copied to the end doesn't count). A vault file put back to an
/// older copy (any program running as you can do that) loses both from inside the vault; the log, appended to
/// separately, still has them. The caller only ever makes presence mode stricter with this: a Touch ID record, real
/// or not, can only keep it on, and a setting counts only when it's authenticated.
func auditPresenceHistory(_ paths: Paths, macKey: [UInt8]?) -> (seen: Bool, lastMode: String?) {
    guard let bytes = try? readFileBytes(paths.audit, max: 1 << 30) else { return (false, nil) }
    var seen = false
    var last: String?
    var prevSeq: Int?
    var prevHash: String?
    let marker = Array("\"op\":\"presence\"".utf8)
    for raw in bytes.split(separator: 0x0A) {
        let line = Array(raw)
        // Every line's place in the chain; only presence records are parsed in full.
        guard line.count < 65536, let o = parseJSONObject(line) else { prevSeq = nil; prevHash = nil; continue }
        let seq = (o["seq"] as? NSNumber)?.intValue
        let hash = o["hash"] as? String
        defer { prevSeq = seq; prevHash = hash }
        guard line.firstRange(of: marker) != nil, o["op"] as? String == "presence", let mode = o["mode"] as? String else { continue }
        if mode == "biometry_seen" { seen = true; continue }
        guard ["on", "off", "auto"].contains(mode), let k = macKey, let mac = o["mac"] as? String, mac == auditRecordMAC(k, o),
              let s = seq, let ps = prevSeq, s == ps + 1, let p = o["prev"] as? String, p == prevHash, hash == eventHash(o) else { continue }
        last = mode
    }
    return (seen, last)
}

/// How strict a presence setting is: on, then the default (auto), then off.
func presenceRank(_ setting: String?) -> Int { setting == "on" ? 2 : (setting == "off" ? 0 : 1) }

/// `presence on|off|auto`: the vault's own setting for presence mode (the passphrase, like `policy`).
func cmdPresence(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: [])
    guard a.positional.count == 1, ["on", "off", "auto"].contains(a.positional[0]) else {
        throw usageError("usage: presence on|off|auto  (auto: on where this Mac has Touch ID)")
    }
    let mode = a.positional[0]
    try requireTTY("presence")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    defer { key.wipeKey() }
    try updateVault(paths, key) { p in p.require_presence = mode == "auto" ? nil : mode }
    audit(paths, ["op": "presence", "result": "ok", "mode": mode], macKey: vaultAuditKey(paths, key))
    let (bio, detail) = detectBiometry()
    let (on, why, _) = resolvePresence(flag: nil, setting: mode == "auto" ? nil : mode, biometry: bio, detail: detail,
                                       seen: existing.biometry_seen != nil)
    oprint("Presence mode: \(mode) (now \(on ? "on" : "off"): \(why)). It applies from the next unlock; `unlock --require-presence` or `--no-require-presence` overrides it for one unlock.")
    return Exit.ok
}

// MARK: - the passphrase proof

/// A key only a process holding the vault's data key has: the unlock process, and a command that just opened
/// the vault with the passphrase.
func approvalProofKey(_ key: VaultKey) -> [UInt8] {
    hkdf32(key.bytes, salt: Array("mcomp-vault".utf8), info: "mcomp-vault|approval-proof|1")
}

/// Binds the approval to this request, its one-time challenge, the summary the person was shown and how far it
/// reaches (the scope they were shown): a proof for one use can't be presented for a session or the whole unlock.
func approvalProof(_ k: [UInt8], id: String, challenge: String, summary: String, scope: ApprovalScope) -> String {
    hmacSHA256Hex(key: k, data: Array("mcomp-vault|approve|2|\(id)|\(challenge)|\(scopeName(scope))|\(summary)".utf8))
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
    let a = try parseArgs(argv, values: ["scope"], flags: ["passphrase", "json"])
    guard a.positional.count == 1, validRequestID(a.positional[0]) else {
        throw usageError("usage: approve ID [--passphrase] [--scope use|session|unlock]  (ID: 8 hex digits, from `requests`)")
    }
    // How far it reaches is part of what's confirmed: the dialog's text and the passphrase proof both name it.
    guard let scope = parseScope(a.last("scope") ?? "use") else { throw usageError("--scope use, session or unlock (default: use, this one use)") }
    let id = a.positional[0]
    var req: [String: Any] = ["op": "approve", "id": id, "method": "local-auth", "scope": scopeName(scope)]
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
        let name = r["name"] as? String ?? "it"
        oprint(clean("Approve request \(id), \(scopeWords(scope, name: name)): \(summary)?", max: 600))
        oprint("Your vault passphrase approves it \(scopeWords(scope, name: name)); Ctrl-C refuses nothing and leaves it waiting.")
        let (key, _) = try unlockKey(paths)
        var k = approvalProofKey(key)
        defer { wipe(&k); key.wipeKey() }
        req["method"] = "passphrase"
        req["proof"] = approvalProof(k, id: id, challenge: challenge, summary: summary, scope: scope)
    }
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 200) } catch ClientError.locked {
        throw VaultError("the vault is locked: nothing waits for approval", code: Exit.locked)
    }
    if a.has("json") { printResult(res, ["ok", "id", "status", "state", "site", "kind", "reason"]); return exitFor(res) }
    if let st = res["state"] as? String {
        // A one-time code request: approving it acts at once (Codes.swift), and this says how that went.
        let done = st == "filled" ? ((res["kind"] as? String) == "link" ? "the sign-in link was opened" : "the code was filled")
            : (st == "given" ? "it waits for `fill --request \(id)`" : "it wasn't filled: \(res["reason"] as? String ?? st)")
        if res["ok"] as? Bool == true || st == "given" { oprint(clean("Approved request \(id) for this one use: \(done).", max: 400)) } else {
            eprint(clean("mcomp-vault: request \(id): \(res["reason"] as? String ?? "not approved")", max: 400))
        }
        return exitFor(res)
    }
    if res["ok"] as? Bool == true { oprint("Approved request \(id) " + (res["scope_words"] as? String ?? "for this one use") + ".") } else {
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
func buildRule(_ p: Payload, use: String, secret: String?, site: String?, session: String?, project: String, expires exp: Date,
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
    return (UnattendedRule(id: newRuleID(), use: u, secret: secret, site: site, session: session, project: project,
                           created: isoTime(now), expires: isoTime(exp), expires_unix: exp.timeIntervalSince1970, confirmed: confirmed), nil)
}

func cmdRule(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: rule add (--secret NAME | --site https://site | --session NAME) --project DIR --for 8h [--passphrase]"
        + " | rule add code --site https://site --from stdin [--link-host H ...] [--project DIR] [--expires 7d] [--passphrase]"
        + " | rule add relay --from-host H --to https://site [--link-host H ...] [--project DIR] [--expires 7d] [--passphrase]"
        + " | rule list [--json] | rule remove ID"
    guard let sub = argv.first else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), values: ["secret", "site", "session", "project", "for", "from", "from-host", "to",
                                                            "link-host", "expires"], flags: ["passphrase", "json"])
    switch sub {
    case "add":
        if let kind = a.positional.first, kind == "code" || kind == "relay" {
            guard a.positional.count == 1 else { throw usageError(usage) }
            return try cmdCodeRuleAdd(paths, kind, a)
        }
        guard a.positional.isEmpty else { throw usageError(usage) }
        for k in ["from", "from-host", "to", "link-host", "expires"] where a.last(k) != nil {
            throw usageError("--\(k) goes with `rule add code` or `rule add relay`")
        }
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
            // What the passphrase confirms, shown before it's asked for: the rule, for whom, and the expiry it will have
            // (fixed now, so a slow passphrase doesn't stretch it), and the rule stored must say exactly that.
            let exp = Date().addingTimeInterval(TimeInterval(seconds))
            let shown = ruleConfirmText(secret: a.last("secret"), site: site, session: a.last("session"), project: project, expires: isoTime(exp))
            oprint(clean("Make an unattended rule: " + shown.dropFirst("make an unattended rule: ".count) + ". Your vault passphrase confirms it.", max: 4500))
            let (key, existing) = try unlockKey(paths)
            defer { key.wipeKey() }
            let (rule, why) = buildRule(existing, use: "", secret: a.last("secret"), site: site, session: a.last("session"), project: project,
                                        expires: exp, confirmed: "passphrase")
            guard let rule else { throw VaultError(why ?? "can't make that rule") }
            guard rule.confirmText == shown else { throw VaultError("the rule to be stored isn't the one shown; nothing was saved") }
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
            let whom = (r["project"] as? String ?? "").isEmpty ? "for any program" : "for Claude Code sessions in \(tildePath(r["project"] as? String ?? "?"))"
            oprint(clean("  \(r["id"] ?? "")  \(r["what"] ?? "") \(whom), "
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

/// `rule add code|relay ...`: a standing approval for one-time codes from a program's input, or for relays, into one
/// site. Made like every rule: macOS's Touch ID / password dialog (shown by the unlock process) or the passphrase here.
func cmdCodeRuleAdd(_ paths: Paths, _ kind: String, _ a: Args) throws -> Int32 {
    for k in ["secret", "session", "for"] where a.last(k) != nil {
        throw usageError("--\(k) isn't for a \(kind) rule (`rule add \(kind)` takes --expires for how long it lasts)")
    }
    let siteFlag = kind == "code" ? "site" : "to"
    guard let raw = a.last(siteFlag), let site = canonicalOrigin(raw) else {
        throw usageError("rule add \(kind): --\(siteFlag) https://site (the site the codes go to; https://host[:port], no path)")
    }
    if kind == "code" {
        guard a.last("from") == "stdin" else {
            throw usageError("rule add code: --from stdin (codes a program pipes in with no terminal; the only source a code rule covers)")
        }
        guard a.last("from-host") == nil, a.last("site") != nil, a.last("to") == nil else { throw usageError("rule add code takes --site, not --to or --from-host") }
    } else {
        guard a.last("from") == nil, a.last("site") == nil else { throw usageError("rule add relay takes --from-host H --to https://site") }
        guard let h = a.last("from-host"), validSiteHost(h) else {
            throw usageError("rule add relay: --from-host H (the host of the page the code is read from, like mail.example.com)")
        }
    }
    let linkHosts = a.all("link-host").map { $0.lowercased() }
    guard linkHosts.count <= 8, linkHosts.allSatisfy(validSiteHost) else { throw usageError("--link-host: a host name like links.example.com (at most 8)") }
    var project = ""
    if let pr = a.last("project") {
        var isDir: ObjCBool = false
        project = realPath(absolutePath(pr)) ?? ""
        guard clean(project, max: 4096) == project, FileManager.default.fileExists(atPath: project, isDirectory: &isDir), isDir.boolValue else {
            throw usageError("--project \(pr): an existing folder")
        }
        guard project != "/" && project != realPath(homeDirectory()) else {
            throw usageError("--project \(pr): name the project's own folder, not / or your home folder")
        }
    }
    var seconds = defaultCodeRuleSeconds
    if let e = a.last("expires") {
        guard let n = parseDuration(e), n <= maxRuleSeconds else { throw usageError("--expires: like 8h, 7d or 30d (at most 30d; 7d when left out)") }
        seconds = n
    }
    let fromHost = a.last("from-host")?.lowercased()
    let unlocked = (try? request(paths, ["op": "status"], timeout: 10))?["ok"] as? Bool == true
    if a.has("passphrase") || !unlocked {
        try requireTTY("rule add")
        installPromptSignalHandlers()
        let exp = Date().addingTimeInterval(TimeInterval(seconds))
        let shown = codeRuleConfirmText(use: kind, site: site, fromHost: fromHost, linkHosts: linkHosts, project: project, expires: isoTime(exp))
        oprint(clean("Make an unattended rule: " + shown.dropFirst("make an unattended rule: ".count) + ". Your vault passphrase confirms it.", max: 4500))
        let (key, _) = try unlockKey(paths)
        defer { key.wipeKey() }
        let (rule, why) = buildCodeRule(use: kind, site: site, fromHost: fromHost, linkHosts: linkHosts, project: project, expires: exp,
                                        confirmed: "passphrase")
        guard let rule else { throw VaultError(why ?? "can't make that rule") }
        guard rule.confirmText == shown else { throw VaultError("the rule to be stored isn't the one shown; nothing was saved") }
        try updateVault(paths, key) { p in p.rules = (p.rules ?? []).filter { $0.live } + [rule] }
        var rec = rule.record
        rec["op"] = "rule_add"
        rec["result"] = "ok"
        audit(paths, rec, macKey: vaultAuditKey(paths, key))
        oprint(clean("Rule \(rule.id): \(rule.what) without asking, " + (project.isEmpty ? "for any program" : "for Claude Code sessions in \(tildePath(project))")
                     + ", until \(rule.expires).", max: 500))
        return Exit.ok
    }
    var req: [String: Any] = ["op": "rule_add", "use": kind, "site": site, "for_s": seconds, "project": project]
    if let h = fromHost { req["from_host"] = h }
    if !linkHosts.isEmpty { req["link_hosts"] = linkHosts }
    let res = try request(paths, req, timeout: 200)
    if res["ok"] as? Bool == true {
        oprint(clean("Rule \(res["id"] ?? "?"): \(res["what"] ?? "") without asking, " + (project.isEmpty ? "for any program" : "for Claude Code sessions in \(tildePath(project))")
                     + ", until \(res["expires"] ?? "?").", max: 500))
        return Exit.ok
    }
    eprint(clean("mcomp-vault: no rule made: \(res["reason"] as? String ?? "no answer")", max: 400))
    return exitFor(res)
}

// MARK: - command: policy (a secret's approval, without retyping its value)

func cmdPolicy(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: policy NAME [--approval session|each] [--answer-timeout 30m|default]"
    let a = try parseArgs(argv, values: ["approval", "answer-timeout"], flags: [])
    let ap = a.last("approval"), at = a.last("answer-timeout")
    guard a.positional.count == 1, validName(a.positional[0]), ap != nil || at != nil, ap.map({ ["session", "each"].contains($0) }) ?? true else {
        throw usageError(usage)
    }
    // --answer-timeout D (an api secret's limit for one proxied answer) or `default` (back to the default).
    var timeout: Int? = nil
    if let t = at, t != "default" { timeout = try parseAnswerTimeout(t) }
    let name = a.positional[0]
    try requireTTY("policy")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    defer { key.wipeKey() }
    guard let s = existing.secrets[name] else { throw VaultError("no secret named \(name)") }
    if at != nil && s.kind != "api" { throw usageError("--answer-timeout is for api secrets (\(name) is a \(s.kind) secret)") }
    try updateVault(paths, key) { p in
        if let ap { p.secrets[name]?.approval = ap }
        if at != nil { p.secrets[name]?.api_answer_timeout_s = timeout }
    }
    var rec: [String: Any] = ["op": "policy", "result": "ok", "name": name]
    if let ap { rec["approval_mode"] = ap }
    if at != nil { rec["answer_timeout_s"] = timeout ?? defaultAnswerTimeout }
    audit(paths, rec, macKey: vaultAuditKey(paths, key))
    if let ap {
        let idp = s.kind == "api" ? isIdentityProviderHost(originHost(s.api_host ?? "")) : s.origins.contains(where: isIdentityProviderOrigin)
        oprint(ap == "session"
               ? "Uses of \(name) no longer ask (unless the unlock says --confirm each)" + (idp ? "; on an identity provider's site they still ask, every time." : ".")
               : "Every use of \(name) asks first.")
    }
    if at != nil {
        oprint("Each proxied answer for \(name) may take up to \(durationWords(timeout ?? defaultAnswerTimeout))"
               + (timeout == nil ? " (the default)." : ". Tokens already given use it from their next request."))
    }
    return Exit.ok
}
