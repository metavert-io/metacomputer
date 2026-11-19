// mcomp-vault: one-time sign-in codes and links (SECURITY.md, "One-time codes and sign-in links").
//
// One request model, three ways in:
//  - by hand: `ask` files a request and returns its id at once; the person gives the value with `give ID` (stdin only:
//    a hidden prompt on a terminal, one line from a pipe), or, with INTERACTIVE_PROMPTS=on at unlock, in a dialog the
//    unlock process shows (osascript's hidden answer; the value comes back on the dialog's own pipe, straight here);
//  - from a script: `code` reads the value from stdin and asks, gives and fills in one step;
//  - from inside the session: `relay` reads the code (or a sign-in link) from an element of a page in the VM's
//    page-mode Chrome and fills it (or opens the link) in another tab.
// A request lives in the unlock process only: id, site, kind (code | link), why, requester, VM, an optional target (a
// tab on the site and a field, or the human-mode browser's focused field), when it was made, when it expires (10
// minutes by default, at most 15) and a state (pending, given, filled, expired, refused). Single use. The value is held
// in memory only between give (or the relay's read) and its fill, and wiped then, at expiry and at lock.
//
// Approvals keep the vault's promise: nothing a program does approves. A value given at a terminal or in the dialog
// follows the existing path (Touch ID / the login password, the passphrase, or a line typed at the unlock terminal).
// A value from a pipe with no terminal, and every relay, goes ahead without a question only under an unattended rule
// the person made (`rule add code|relay`); otherwise the request waits for `secrets approve ID` and the command exits
// at once (Exit.needsApproval). Identity providers always ask: no rule covers them.
import Darwin
import Foundation

let codeDefaultTTL = 600
let codeMaxTTL = 900

/// Why a link can't go ahead without a person: its host isn't the site's and no rule lists it. Fixed words: the host
/// itself is never named (whoever chose it could have spelled a value in it).
let unlistedLinkReason = "the link goes to a host that isn't the site's and that no rule lists (the host isn't shown); nothing was opened"

/// The shortest a request may live: 30 s. Test builds take MCOMP_VAULT_TEST_CODE_MIN_TTL (seconds) so expiry can be
/// checked quickly; release builds don't contain that switch.
func codeMinTTL() -> Int {
    #if MCOMP_VAULT_TESTING
    if let s = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_CODE_MIN_TTL"], let n = Int(s), n >= 1 { return n }
    #endif
    return 30
}
let codeWhyMax = 200
let codeLinkMax = 2048
/// The longest text a relay reads from a page element (the rest is ignored, never kept).
let relayTextMax = 8192
/// How long a finished request stays visible to `requests` and `ask --wait`.
let codeKeepFinished: TimeInterval = 600

// MARK: - what a code and a link may look like

/// The shape a one-time code must have: letters and digits (`alnum`, the default) or digits only, of a length within
/// minLen...maxLen (4 to 10 by default; never shorter than 4 or longer than 16).
struct CodeShape {
    var digitsOnly = false
    var minLen = 4
    var maxLen = 10

    var words: String { (digitsOnly ? "digits" : "letters and digits") + (minLen == maxLen ? ", \(minLen) long" : ", \(minLen)-\(maxLen) long") }

    /// --pattern digits|alnum and --length N|MIN-MAX.
    static func parse(pattern: String?, length: String?) -> CodeShape? {
        var s = CodeShape()
        switch pattern ?? "alnum" {
        case "alnum": s.digitsOnly = false
        case "digits": s.digitsOnly = true
        default: return nil
        }
        if let l = length {
            let parts = l.split(separator: "-", omittingEmptySubsequences: false).map { Int($0) }
            guard parts.count == 1 || parts.count == 2, parts.allSatisfy({ $0 != nil }) else { return nil }
            let a = parts[0]!, b = parts.count == 2 ? parts[1]! : parts[0]!
            guard a >= 4, b <= 16, a <= b else { return nil }
            s.minLen = a
            s.maxLen = b
        }
        return s
    }

    var record: [String: Any] { ["pattern": digitsOnly ? "digits" : "alnum", "min": minLen, "max": maxLen] }

    func fits(_ t: String) -> Bool {
        guard t.count >= minLen, t.count <= maxLen else { return false }
        return t.unicodeScalars.allSatisfy { u in
            (u >= "0" && u <= "9") || (!digitsOnly && ((u >= "a" && u <= "z") || (u >= "A" && u <= "Z")))
        }
    }
}

/// A code given by hand or piped in: surrounding space, and spaces or hyphens between groups ("482 913", "AB-12-CD"),
/// are dropped; what's left must fit the shape. nil when it doesn't look like a code.
func normalizeCode(_ raw: String, _ shape: CodeShape) -> String? {
    let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard t.count <= 64 else { return nil }
    let joined = String(t.unicodeScalars.filter { $0 != " " && $0 != "-" }.map(Character.init))
    return shape.fits(joined) ? joined : nil
}

/// The one code in a page element's text: its runs of letters and digits that fit the shape. With letters allowed, a
/// run must hold at least one digit, so ordinary words don't count. Exactly one distinct candidate, or a refusal (none,
/// or several: point the relay at the element that holds only the code).
func extractCode(fromText text: String, _ shape: CodeShape) -> (String?, String?) {
    var found: [String] = []
    var run = String.UnicodeScalarView()
    func flush() {
        if !run.isEmpty {
            let s = String(run)
            if shape.fits(s) && (shape.digitsOnly || s.unicodeScalars.contains { $0 >= "0" && $0 <= "9" }) && !found.contains(s) { found.append(s) }
            run = String.UnicodeScalarView()
        }
    }
    for u in text.unicodeScalars.prefix(relayTextMax) {
        if (u >= "0" && u <= "9") || (u >= "a" && u <= "z") || (u >= "A" && u <= "Z") { run.append(u) } else { flush() }
    }
    flush()
    if found.count == 1 { return (found[0], nil) }
    if found.isEmpty { return (nil, "the element holds nothing that looks like a code (\(shape.words))") }
    return (nil, "the element holds more than one thing that looks like a code; point --from at the element that holds only the code (or narrow --pattern / --length)")
}

/// The origin of a link (scheme://host[:port]) and its host, if it's one a sign-in link may be: https (http only for
/// 127.0.0.1 or localhost with a port, as sites themselves), no user name or password, printable, at most 2048 characters.
func linkOrigin(_ raw: String) -> (origin: String, host: String)? {
    let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !t.isEmpty, t.count <= codeLinkMax, clean(t, max: codeLinkMax) == t, !t.contains(" "),
          let c = URLComponents(string: t), let scheme = c.scheme?.lowercased(), let host = c.host?.lowercased(), !host.isEmpty,
          c.user == nil, c.password == nil else { return nil }
    let o = scheme + "://" + host + (c.port.map { ":\($0)" } ?? "")
    guard let canon = canonicalOrigin(o) else { return nil }
    return (canon, host)
}

/// Whether a link may be opened for a request on `site` without asking about its host: its origin is the site's own,
/// or its host is one the person listed (`--link-host`, https only).
func linkHostAllowed(_ link: (origin: String, host: String), site: String, extra: [String]) -> Bool {
    if link.origin == site { return true }
    return link.origin.hasPrefix("https://") && link.origin == "https://" + link.host && extra.contains(link.host)
}

// MARK: - where a code goes

/// A request's target: a tab on the site in the VM's page-mode Chrome (the one tab there; a field by selector, or the
/// focused one), or the focused field of a human-mode browser window (the VM's regular Firefox, or a Chrome started
/// normally), whose site comes from its address bar.
struct CodeTarget {
    var human = false
    var selector: String? = nil
    var profile: String? = nil
    var browserPID: Int? = nil
    var windowID: Int? = nil

    var record: [String: Any] {
        var d: [String: Any] = ["mode": human ? "human" : "page"]
        if let s = selector { d["selector"] = clean(s, max: 300) }
        if let p = profile { d["profile"] = p }
        if let b = browserPID { d["browser_pid"] = b }
        if let w = windowID { d["window_id"] = w }
        return d
    }

    var words: String {
        human ? "the focused field of the human-mode browser" + (windowID.map { " window \($0)" } ?? "")
            : (selector.map { "the field '\(promptText($0, max: 60))'" } ?? "the focused field") + " of the tab on the site"
                + (profile.map { ", Chrome profile \($0)" } ?? "")
    }

    /// From a request's fields (validated; nil + why when they're wrong).
    static func from(_ d: [String: Any]?) -> (CodeTarget?, String?) {
        guard let d else { return (nil, nil) }
        var t = CodeTarget()
        t.human = d["mode"] as? String == "human"
        if let s = d["selector"] as? String {
            guard !s.isEmpty, s.count <= 300, !t.human else { return (nil, "bad target selector") }
            t.selector = s
        }
        if let p = d["profile"] as? String {
            guard validName(p), !t.human else { return (nil, "bad target profile") }
            t.profile = p
        }
        if let b = (d["browser_pid"] as? NSNumber)?.intValue {
            guard b > 1, !t.human else { return (nil, "bad target browser pid") }
            t.browserPID = b
        }
        if let w = (d["window_id"] as? NSNumber)?.intValue {
            guard w > 0, t.human else { return (nil, "bad target window id") }
            t.windowID = w
        }
        return (t, nil)
    }
}

/// `--into TAB/FIELD`: TAB is the site (https://host[:port], a bare host meaning https, or http://localhost:PORT for
/// tests), FIELD a CSS selector (empty: the focused field). Returns the tab's origin and the selector.
func parseInto(_ s: String) -> (origin: String, selector: String?)? {
    var rest = s
    var scheme = "https://"
    for p in ["https://", "http://"] where rest.lowercased().hasPrefix(p) { scheme = p; rest = String(rest.dropFirst(p.count)) }
    let host: String, field: String
    if let slash = rest.firstIndex(of: "/") {
        host = String(rest[..<slash])
        field = String(rest[rest.index(after: slash)...])
    } else {
        host = rest
        field = ""
    }
    guard let origin = canonicalOrigin(scheme + host), field.count <= 300 else { return nil }
    return (origin, field.isEmpty ? nil : field)
}

// MARK: - the dialog (INTERACTIVE_PROMPTS=on)

/// The dialog's program: macOS's osascript, with the script fixed here and every changing word passed as an argument
/// (`on run argv`), never spliced into the script, so nothing in `why` can become AppleScript. The answer comes back on
/// the program's own stdout, a pipe only this process reads. Test builds may name a stand-in (MCOMP_VAULT_TEST_OSASCRIPT)
/// and never show a real dialog; release builds don't contain that switch.
let codeDialogScript: [String] = [
    "on run argv",
    "set theKind to item 1 of argv",
    "set theHost to item 2 of argv",
    "set theWhy to item 3 of argv",
    "set theID to item 4 of argv",
    "set theSecs to (item 5 of argv) as integer",
    "set theText to \"metacomputer asks for a one-time \" & theKind & \" for \" & theHost & \" (request \" & theID & \").\" & return & return & theWhy & return & return & \"Type or paste it below. It goes to the vault, which fills it only on \" & theHost & \".\"",
    "set r to display dialog theText default answer \"\" with hidden answer with title \"metacomputer secrets\" buttons {\"Cancel\", \"Give\"} default button \"Give\" cancel button \"Cancel\" giving up after theSecs",
    "if gave up of r then return \"\"",
    "return text returned of r",
    "end run",
]

func codeDialogProgram() -> String {
    #if MCOMP_VAULT_TESTING
    if let s = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_OSASCRIPT"], !s.isEmpty { return s }
    return "/nonexistent/a-test-build-never-shows-a-real-dialog"
    #else
    return "/usr/bin/osascript"
    #endif
}

/// One dialog in flight: started detached (the unlock process goes on serving), read without blocking, ended at expiry,
/// at lock, or when the value came another way.
final class CodeDialog {
    var pid: pid_t = 0
    var outFD: Int32 = -1
    var out: [UInt8] = []
    var finished = false
    var gotValue = false

    /// Start it; nil when it can't run.
    static func start(kind: String, host: String, why: String, id: String, seconds: Int) -> CodeDialog? {
        var outPipe: [Int32] = [0, 0], devnull = open("/dev/null", O_RDWR | O_CLOEXEC)
        guard devnull >= 0, pipe(&outPipe) == 0 else { if devnull >= 0 { close(devnull) }; return nil }
        var fa: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&fa)
        defer { posix_spawn_file_actions_destroy(&fa) }
        posix_spawn_file_actions_adddup2(&fa, devnull, 0)
        posix_spawn_file_actions_adddup2(&fa, outPipe[1], 1)
        posix_spawn_file_actions_adddup2(&fa, devnull, 2)
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETPGROUP))
        var defaults = sigset_t()
        sigemptyset(&defaults)
        for s in [SIGINT, SIGTERM, SIGHUP, SIGPIPE] { sigaddset(&defaults, s) }
        posix_spawnattr_setsigdefault(&attr, &defaults)
        var mask = sigset_t()
        sigemptyset(&mask)
        posix_spawnattr_setsigmask(&attr, &mask)
        posix_spawnattr_setpgroup(&attr, 0)
        let prog = codeDialogProgram()
        var args = [prog]
        for line in codeDialogScript { args += ["-e", line] }
        // The changing words, as arguments: the kind, the site's host, why (cleaned, one line), the id, the time left.
        args += [kind, host, clean(why, max: codeWhyMax), id, String(max(5, seconds))]
        let argv = args.map { strdup($0) } + [nil]
        let envp = helperEnv().map { strdup($0) } + [nil]
        defer { for p in argv { free(p) }; for p in envp { free(p) } }
        var pid: pid_t = 0
        let rc = posix_spawn(&pid, prog, &fa, &attr, argv, envp)
        close(outPipe[1])
        close(devnull)
        guard rc == 0 else { close(outPipe[0]); return nil }
        _ = fcntl(outPipe[0], F_SETFL, fcntl(outPipe[0], F_GETFL) | O_NONBLOCK)
        _ = fcntl(outPipe[0], F_SETFD, FD_CLOEXEC)
        let d = CodeDialog()
        d.pid = pid
        d.outFD = outPipe[0]
        return d
    }

    /// Read what's there; true once the dialog has ended (its answer, if any, in `out`).
    func poll() -> Bool {
        guard !finished else { return true }
        var buf = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = read(outFD, &buf, buf.count)
            if n > 0 {
                if out.count < codeLinkMax + 16 { out.append(contentsOf: buf[0..<min(n, codeLinkMax + 16 - out.count)]) }
                continue
            }
            if n == 0 { finish(); break }
            if errno == EAGAIN || errno == EINTR { break }
            finish()
            break
        }
        wipe(&buf)
        return finished
    }

    private func finish() {
        finished = true
        if outFD >= 0 { close(outFD); outFD = -1 }
        var st: Int32 = 0
        while waitpid(pid, &st, 0) < 0 && errno == EINTR {}
        // Cancel or no answer: the program exits non-zero or prints nothing.
        if !((st & 0x7F) == 0 && ((st >> 8) & 0xFF) == 0) { wipe(&out); out = [] }
    }

    /// The answer, one line, the caller's to wipe (empty: none).
    func takeValue() -> [UInt8] {
        var v = out
        wipe(&out)
        out = []
        while let l = v.last, l == 0x0A || l == 0x0D { v.removeLast() }
        if v.contains(0x0A) { wipe(&v); return [] }
        return v
    }

    func kill() {
        guard !finished else { return }
        Darwin.kill(-pid, SIGKILL)
        Darwin.kill(pid, SIGKILL)
        wipe(&out)
        out = []
        finish()
    }
}

// MARK: - the request

final class CodeRequest {
    let id: String
    let site: String
    let kind: String                    // code | link
    let why: String
    let vm: String
    let who: Requester
    let how: String                     // ask | code | relay
    let shape: CodeShape
    var target: CodeTarget?
    let created = Date()
    let expires: Date
    var state = "pending"               // pending | given | filled | expired | refused
    var value: [UInt8] = []
    var source: String?                 // terminal | dialog | stdin | relay
    var approved = false
    var approval = "-"
    var parked = false                  // waits for `approve ID` / `deny ID`
    var reason: String?
    var linkHost: String?               // a link's host: reported only when it's the site's or one a rule lists (reportedLinkHost)
    var linkOrigin: String?
    var linkOK = true                   // the link's origin is the site's
    var listedHosts: [String] = []      // the covering rule's --link-host list (chosen by origin, linkHostAllowed)
    var fromOrigin: String?             // relay: the page it was read from
    var dialog: CodeDialog?
    var finishedAt: Date?
    let challenge = randomBytes(16).map { String(format: "%02x", $0) }.joined()

    init(id: String, site: String, kind: String, why: String, vm: String, who: Requester, how: String, shape: CodeShape,
         target: CodeTarget?, ttl: Int) {
        self.id = id
        self.site = site
        self.kind = kind
        self.why = why
        self.vm = vm
        self.who = who
        self.how = how
        self.shape = shape
        self.target = target
        self.expires = Date().addingTimeInterval(TimeInterval(ttl))
    }

    var final: Bool { ["filled", "expired", "refused"].contains(state) }
    var identityProvider: Bool { isIdentityProviderOrigin(site) || (linkHost.map(isIdentityProviderHost) ?? false) }
    var siteHost: String { originHost(site) }

    /// The link's host as it may be shown to anyone but the person at the unlock terminal or macOS's dialog: only when
    /// the link goes to the site itself or to a host the person listed (`--link-host`, by origin). A host chosen by
    /// anyone else, a VM above all, could spell a value in its labels, so it's never echoed: not in results, `requests`,
    /// refusals, the audit log or events.
    var reportedLinkHost: String? {
        guard let h = linkHost, let o = linkOrigin else { return nil }
        return linkHostAllowed((o, h), site: site, extra: listedHosts) ? h : nil
    }

    /// What approving it allows: shown in `requests`, and signed by a passphrase proof. Never an unlisted link host.
    var summary: String { summaryText(showHost: false) }
    /// The same for the person only (the unlock terminal's question and macOS's dialog): a given link's real host. A
    /// relay never gets that far with an unlisted host (refused, unnamed), so this shows only a host someone gave.
    var privateSummary: String { summaryText(showHost: source != "relay") }

    func summaryText(showHost: Bool, tail: Bool = true) -> String {
        let host = reportedLinkHost ?? (showHost ? linkHost : nil)
        var s = kind == "link"
            ? "open a one-time sign-in link" + (host.map { " to \($0)" } ?? (linkHost != nil ? " to another host" : "")) + " in the tab on \(site)"
            : "fill a one-time code into \(site)"
        if kind == "link", !linkOK, let h = host { s += " (the link goes to \(h), not \(siteHost))" }
        if kind == "link", !linkOK, host == nil, linkHost != nil { s += " (the link doesn't go to \(siteHost))" }
        if let f = fromOrigin { s += ", read from a page on \(f)" }
        if let src = source, fromOrigin == nil { s += ", given " + (src == "stdin" ? "by a program (no terminal)" : (src == "dialog" ? "in the dialog" : "at a terminal")) }
        return clean(s + (tail ? " on VM \(vm), for: \(why), asked by \(who.describe)" : ", for: \(why)"), max: 400)
    }

    var listing: [String: Any] {
        var d: [String: Any] = ["id": id, "use": how, "kind": kind, "site": site, "state": state, "why": why, "vm": vm,
                                "created": isoTime(created), "expires": isoTime(expires),
                                "expires_in_s": max(0, Int(expires.timeIntervalSinceNow)), "age_s": Int(Date().timeIntervalSince(created)),
                                "requester": who.record, "asked_by": who.describe, "needs_approval": parked,
                                "identity_provider": identityProvider, "name": "request \(id)", "summary": summary, "challenge": challenge]
        if let t = target { d["target"] = t.record }
        if let s = source { d["source"] = s }
        if let r = reason { d["reason"] = r }
        if let h = reportedLinkHost { d["link_host"] = h }
        if let f = fromOrigin { d["from"] = f }
        if dialog != nil && !(dialog!.finished) { d["dialog"] = true }
        if let p = who.project { d["project"] = p }
        return d
    }

    func dropValue() {
        wipe(&value)
        value = []
    }
}

// MARK: - the unlock process's side

extension UnlockServer {
    /// Requests that can still be given, filled or approved (and finished ones, kept a while for `requests` and `--wait`).
    func codeTick() {
        let now = Date()
        for (id, cr) in codeRequests {
            if cr.final {
                if let f = cr.finishedAt, now.timeIntervalSince(f) > codeKeepFinished { codeRequests[id] = nil }
                continue
            }
            if now >= cr.expires {
                cr.dialog?.kill()
                cr.dialog = nil
                codeFinish(cr, state: "expired", reason: "it expired before it was filled")
                continue
            }
            if let d = cr.dialog, d.poll() {
                var v = d.takeValue()
                cr.dialog = nil
                defer { wipe(&v) }
                if v.isEmpty || cr.state != "pending" {
                    say("\(clockTime())  request \(cr.id): the dialog closed without a value")
                    continue
                }
                say("\(clockTime())  request \(cr.id): a value was given in the dialog")
                // The dialog gives (and, with your approval, approves) the value; it never fills by itself. Nothing here
                // holds this VM's dispatch lock or checks for an open handoff, so the fill waits for `fill --request ID`,
                // which bin/metacomputer runs like any fill.
                _ = codeGive(cr, value: v, source: "dialog", interactive: true, presentAtMac: true, act: false)
            }
        }
    }

    /// The request reaches a final state: its value is wiped, it's recorded, and the unlock terminal says so.
    func codeFinish(_ cr: CodeRequest, state: String, reason: String?, flush: Bool = true) {
        cr.state = state
        cr.reason = reason
        cr.parked = false
        cr.dropValue()
        cr.dialog?.kill()
        cr.dialog = nil
        cr.finishedAt = Date()
        if state == "expired" || state == "refused" {
            var rec: [String: Any] = ["op": state == "expired" ? "code_expire" : "code_refuse", "result": state == "expired" ? "error" : "denied",
                                      "request_id": cr.id, "origin": cr.site, "kind": cr.kind, "vm": cr.vm, "requester": cr.who.record]
            if let r = reason { rec["reason"] = r }
            record(rec)
            say("\(clockTime())  request \(cr.id) (\(cr.kind) for \(cr.site)) \(state)" + (reason.map { ": \($0)" } ?? ""))
            if flush { flushSinks(force: true) }
        }
    }

    func codeResult(_ cr: CodeRequest, _ extra: [String: Any] = [:]) -> [String: Any] {
        var out: [String: Any] = ["request_id": cr.id, "site": cr.site, "kind": cr.kind, "state": cr.state]
        switch cr.state {
        case "filled": out["ok"] = true; out["status"] = "filled"
        case "given": out["ok"] = !cr.parked; out["status"] = cr.parked ? "needs_approval" : "given"
        case "pending": out["ok"] = true; out["status"] = "pending"
        case "expired": out["ok"] = false; out["status"] = "expired"
        default: out["ok"] = false; out["status"] = "denied"
        }
        if cr.parked {
            out["approve"] = "metacomputer secrets approve \(cr.id)"
            out["reason"] = "waits for your approval: run `metacomputer secrets approve \(cr.id)` (Touch ID or your login password) in your own terminal, or `deny \(cr.id)`; it expires on its own at \(clockTime(cr.expires))"
        } else if let r = cr.reason, !["filled", "pending"].contains(cr.state) {
            out["reason"] = r
        }
        if let h = cr.reportedLinkHost { out["link_host"] = h }
        for (k, v) in extra { out[k] = v }
        return out
    }

    /// The fields a code request's client sent, checked: (shape, target, why) or a refusal.
    func codeCommon(_ req: [String: Any]) -> (CodeShape?, CodeTarget?, String?) {
        guard let shape = CodeShape.parse(pattern: req["pattern"] as? String, length: req["length"] as? String) else {
            return (nil, nil, "--pattern digits|alnum and --length N or MIN-MAX (4 to 16)")
        }
        let (t, why) = CodeTarget.from(req["target"] as? [String: Any])
        if let w = why { return (nil, nil, w) }
        return (shape, t, nil)
    }

    func newCode(_ req: [String: Any], _ who: Requester, how: String) -> (CodeRequest?, [String: Any]?) {
        guard let vm = req["vm"] as? String, validName(vm), let site = req["site"] as? String, canonicalOrigin(site) == site,
              let kind = req["kind"] as? String, kind == "code" || kind == "link" else {
            return (nil, failed("bad request: --vm, --site https://host and --kind code|link"))
        }
        let ttl = (req["ttl_s"] as? NSNumber)?.intValue ?? codeDefaultTTL
        guard ttl >= codeMinTTL(), ttl <= codeMaxTTL else { return (nil, failed("--ttl: 30 s to \(codeMaxTTL / 60) min")) }
        let why = clean((req["why"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines), max: codeWhyMax)
        guard !why.isEmpty || how != "ask" else { return (nil, failed("--why: what the code is for, in a few words")) }
        let (shapeOpt, target, problem) = codeCommon(req)
        if let p = problem { return (nil, failed(p)) }
        guard let shape = shapeOpt else { return (nil, failed("bad request")) }
        let p: Payload
        do { p = try load() } catch let e as VaultError { return (nil, e.code == Exit.locked ? lockedResult(e.message) : failed(e.message)) } catch {
            return (nil, failed("cannot read the vault"))
        }
        guard p.vms[vm] != nil else { return (nil, failed("VM \(vm) isn't bound to the vault (metacomputer secrets bind)")) }
        guard let id = newRequestID(req["request_id"] as? String) else {
            return (nil, failed("the request id isn't 8 hex digits, or was used already in this unlock"))
        }
        let cr = CodeRequest(id: id, site: site, kind: kind, why: why.isEmpty ? (how == "relay" ? "relay" : "a one-time \(kind)") : why,
                             vm: vm, who: who, how: how, shape: shape, target: target, ttl: ttl)
        codeRequests[id] = cr
        var rec: [String: Any] = ["op": "code_request", "result": "pending", "request_id": id, "origin": site, "kind": kind, "vm": vm,
                                  "use": how, "why": cr.why, "requester": who.record, "ttl_s": ttl]
        if let t = target { rec["target"] = t.record }
        if cr.identityProvider { rec["identity_provider"] = true }
        record(rec)
        return (cr, nil)
    }

    // MARK: ops

    /// `ask`: a request filed for the person; answered at once with its id.
    func opCodeAsk(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        let (crOpt, err) = newCode(req, who, how: "ask")
        guard let cr = crOpt else { return err ?? failed("bad request") }
        say("\(clockTime())  request \(cr.id): a one-time \(cr.kind) for \(cr.site) on VM \(cr.vm) (\(cr.why)), asked by \(who.describe)")
        say("          give it with `metacomputer secrets give \(cr.id)` in your own terminal (it reads the value hidden, never from the command line)")
        dropEvent(type: "code_requested", severity: "warn", vm: cr.vm,
                  message: "One-time \(cr.kind) wanted for \(cr.siteHost): \(cr.why)",
                  action: "In your own terminal: metacomputer secrets give \(cr.id)" + (interactivePrompts ? " (or answer the dialog on this Mac)" : ""),
                  project: who.project, data: ["site": cr.site, "kind": cr.kind, "request_id": cr.id, "why": cr.why])
        if interactivePrompts {
            if let d = CodeDialog.start(kind: cr.kind, host: cr.siteHost, why: cr.why, id: cr.id, seconds: Int(cr.expires.timeIntervalSinceNow)) {
                cr.dialog = d
                say("          a dialog asks for it on this Mac (INTERACTIVE_PROMPTS=on)")
            } else {
                say("          (the dialog couldn't be shown)")
            }
        }
        flushSinks(force: true)
        var out = codeResult(cr, ["expires": isoTime(cr.expires), "expires_in_s": Int(cr.expires.timeIntervalSinceNow),
                                  "dialog": cr.dialog != nil, "give": "metacomputer secrets give \(cr.id)"])
        out["ok"] = true
        return out
    }

    func opCodeStatus(_ req: [String: Any]) -> [String: Any] {
        guard let id = req["id"] as? String, validRequestID(id), let cr = codeRequests[id] else {
            return failed("no code request \(clean(req["id"] as? String ?? "", max: 12)) (it may have ended long ago, or the vault was locked since)")
        }
        var out = codeResult(cr, ["expires_in_s": max(0, Int(cr.expires.timeIntervalSinceNow))])
        if cr.state == "pending" { out["status"] = "pending" }
        return out
    }

    /// `give ID`: the value for a waiting request, from the giver's stdin.
    func opCodeGive(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let id = req["id"] as? String, validRequestID(id), let cr = codeRequests[id] else {
            return failed("no code request \(clean(req["id"] as? String ?? "", max: 12)) waits (metacomputer secrets requests)")
        }
        var v = Array((req["value"] as? String ?? "").utf8)
        defer { wipe(&v) }
        let interactive = req["interactive"] as? Bool == true
        return codeGive(cr, value: v, source: interactive ? "terminal" : "stdin", interactive: interactive, presentAtMac: false, giver: who)
    }

    /// `code`: ask, give and fill in one step (the value from the caller's stdin).
    func opCodePut(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard req["target"] != nil else { return failed("`code` needs where it goes: --into TAB/FIELD or --human") }
        let (crOpt, err) = newCode(req, who, how: "code")
        guard let cr = crOpt else { return err ?? failed("bad request") }
        say("\(clockTime())  request \(cr.id): a one-time \(cr.kind) for \(cr.site) on VM \(cr.vm), piped in by \(who.describe)")
        var v = Array((req["value"] as? String ?? "").utf8)
        defer { wipe(&v) }
        let interactive = req["interactive"] as? Bool == true
        return codeGive(cr, value: v, source: interactive ? "terminal" : "stdin", interactive: interactive, presentAtMac: false, giver: who)
    }

    /// The value for a request: checked against its kind and site, then approved (the person, or a rule), then filled when
    /// the request has a target.
    func codeGive(_ cr: CodeRequest, value: [UInt8], source: String, interactive: Bool, presentAtMac: Bool, giver: Requester? = nil,
                  act: Bool = true) -> [String: Any] {
        if cr.state != "pending" {
            if cr.state == "expired" { return codeResult(cr) }
            var out = codeResult(cr)
            out["ok"] = false
            out["status"] = cr.final ? (cr.state == "expired" ? "expired" : "denied") : "denied"
            out["reason"] = "request \(cr.id) was already given a value (one value per request)"
            return out
        }
        if Date() >= cr.expires {
            codeFinish(cr, state: "expired", reason: "it expired before it was filled")
            return codeResult(cr)
        }
        guard let text = String(validating: value, as: UTF8.self), !value.isEmpty else {
            return denied("no value was given (it comes from stdin: typed hidden at a terminal, or one line from a pipe)")
        }
        // The value's shape: a code, or an https link (to the site's own host or one a rule lists, else the person sees it).
        var normalized: [UInt8]
        if cr.kind == "code" {
            guard let c = normalizeCode(text, cr.shape) else {
                return denied("that doesn't look like a one-time code (\(cr.shape.words)); nothing was kept")
            }
            normalized = Array(c.utf8)
        } else {
            guard let l = linkOrigin(text) else {
                return denied("that isn't a sign-in link this vault opens (https, at most \(codeLinkMax) characters); nothing was kept")
            }
            cr.linkHost = l.host
            cr.linkOrigin = l.origin
            cr.linkOK = l.origin == cr.site
            normalized = Array(text.trimmingCharacters(in: .whitespacesAndNewlines).utf8)
        }
        cr.dialog?.kill()
        cr.dialog = nil
        cr.value = normalized
        wipe(&normalized)
        cr.source = source
        cr.state = "given"
        var rec: [String: Any] = ["op": "code_give", "result": "ok", "request_id": cr.id, "origin": cr.site, "kind": cr.kind, "vm": cr.vm,
                                  "source": source, "requester": (giver ?? cr.who).record]
        if let h = cr.reportedLinkHost { rec["link_host"] = h }
        record(rec)
        return codeDecide(cr, interactive: interactive, presentAtMac: presentAtMac, act: act)
    }

    /// Approve (or park) a request that holds its value, then act on it.
    /// The standing rule that covers this request now, if any: the right kind for its source, live, for its site, and,
    /// for a link to another host, one that lists that link's origin (linkHostAllowed). Never for an identity provider.
    func coveringCodeRule(_ cr: CodeRequest) -> UnattendedRule? {
        guard !cr.identityProvider, let p = try? load() else { return nil }
        let covering = (p.rules ?? []).filter { r in
            cr.how == "relay" ? r.coversRelay(site: cr.site, fromHost: cr.fromOrigin.map(originHost) ?? "", who: cr.who)
                : r.coversCode(site: cr.site, who: cr.who)
        }
        guard cr.kind == "link", !cr.linkOK else { return covering.first }
        guard let o = cr.linkOrigin, let h = cr.linkHost else { return nil }
        return covering.first { linkHostAllowed((o, h), site: cr.site, extra: $0.link_hosts ?? []) }
    }

    /// A rule-backed approval holds only while a rule still covers the request: checked again right before anything
    /// is sent to the VM, since a rule can expire or be removed while a request waits to be filled (or retried).
    func approvalStillHolds(_ cr: CodeRequest) -> Bool {
        guard cr.approval.hasPrefix("rule:") else { return cr.approved }
        guard let r = coveringCodeRule(cr) else { return false }
        cr.approval = "rule:\(r.id)"
        cr.listedHosts = r.link_hosts ?? []
        return true
    }

    /// Why a link can't go ahead without a person: its host isn't the site's and no rule lists it. Fixed words: the host
    /// itself is never named (whoever chose it could have spelled a value in it).

    func codeDecide(_ cr: CodeRequest, interactive: Bool, presentAtMac: Bool, act: Bool = true) -> [String: Any] {
        if !cr.identityProvider && !interactive {
            // No terminal: only a standing rule the person made lets it go ahead without asking.
            if let r = coveringCodeRule(cr) {
                cr.approved = true
                cr.approval = "rule:\(r.id)"
                cr.listedHosts = r.link_hosts ?? []
            } else if cr.kind == "link", !cr.linkOK, cr.how == "relay" || coveringCodeRuleIgnoringLink(cr) {
                // A tracking redirect's host that a covering rule doesn't list (or any unlisted host a relay read): refused,
                // unnamed. A link someone gave with no rule at all waits for the person instead (below), who sees its host.
                codeFinish(cr, state: "refused", reason: unlistedLinkReason)
                return codeResult(cr)
            }
        }
        if !cr.approved && !interactive {
            park(cr)
            return codeResult(cr)
        }
        if !cr.approved {
            // A person is there (a terminal, or the dialog on this Mac): the vault's own question, as for any use. The
            // question and macOS's dialog name a given link's real host; `requests` and the passphrase proof don't.
            let mine = cr.summaryText(showHost: cr.source != "relay", tail: false)
            let q = "  " + mine.prefix(1).uppercased() + mine.dropFirst() + "?"
            let o = approve("code", cr.id, q, cr.who, vm: cr.vm, site: cr.site, use: cr.kind == "link" ? "link" : "code",
                            summary: cr.summaryText(showHost: false, tail: false), identityProvider: cr.identityProvider,
                            fixedID: cr.id, singleUse: true, forceLocalAuth: presentAtMac,
                            dialogSummary: cr.summaryText(showHost: cr.source != "relay", tail: false))
            if cr.final { return codeResult(cr) }        // it expired (or was refused) while the question waited
            cr.approval = o.how
            guard o.yes else {
                codeFinish(cr, state: "refused", reason: o.why)
                return codeResult(cr)
            }
            cr.approved = true
        }
        guard act else {
            say("          -> request \(cr.id) given and approved; it's filled by `metacomputer secrets fill --request \(cr.id)`")
            return codeResult(cr)
        }
        return codeAct(cr)
    }

    /// Whether a rule covers this request's source and site, whatever its link hosts say (to tell "a rule covers it but
    /// doesn't list that host", refused, from "no rule at all", which waits for the person).
    func coveringCodeRuleIgnoringLink(_ cr: CodeRequest) -> Bool {
        guard let p = try? load() else { return false }
        return (p.rules ?? []).contains { r in
            cr.how == "relay" ? r.coversRelay(site: cr.site, fromHost: cr.fromOrigin.map(originHost) ?? "", who: cr.who)
                : r.coversCode(site: cr.site, who: cr.who)
        }
    }

    /// No rule and no person here: the request waits for `approve ID` (or `deny ID`, or its expiry). Nothing blocks.
    func park(_ cr: CodeRequest) {
        cr.parked = true
        var rec: [String: Any] = ["op": "request", "result": "pending", "request_id": cr.id, "use": cr.kind == "link" ? "link" : "code",
                                  "name": "request \(cr.id)", "vm": cr.vm, "origin": cr.site, "requester": cr.who.record]
        if cr.identityProvider { rec["identity_provider"] = true }
        record(rec)
        say("\(clockTime())  Request \(cr.id): \(cr.summary)")
        say("          waits for you: `metacomputer secrets approve \(cr.id)` (Touch ID or your login password; --passphrase for the vault passphrase) or `deny \(cr.id)`")
        dropEvent(type: "approval_waiting", severity: "warn", vm: cr.vm,
                  message: "Vault approval waiting: a one-time \(cr.kind) for \(cr.siteHost) on VM \(cr.vm)",
                  action: "In your own terminal: metacomputer secrets approve \(cr.id)", project: cr.who.project,
                  data: ["op": "code", "site": cr.site, "kind": cr.kind, "request_id": cr.id])
        flushSinks(force: true)
    }

    /// Fill the code (or open the link) where the request says; without a target it waits, given and approved, for
    /// `fill --request ID`.
    func codeAct(_ cr: CodeRequest) -> [String: Any] {
        guard let target = cr.target else {
            say("          -> request \(cr.id) given and approved; it waits for `metacomputer secrets fill --request \(cr.id)`")
            return codeResult(cr)
        }
        guard Date() < expires, receivedSignal == 0, stopReason == nil else {
            return lockedResult("the vault locked before the value was sent")
        }
        /// Right before anything goes to the VM: the request hasn't expired, a rule that approved it still covers it, and
        /// a step may take no longer than the request has left. nil (with the request finished or parked) otherwise.
        func dispatchTime() -> TimeInterval? {
            if cr.final { return nil }
            let left = min(Double(remaining()), cr.expires.timeIntervalSinceNow, 60)
            if left < 1 {
                codeFinish(cr, state: "expired", reason: "it expired before it was filled")
                return nil
            }
            if !approvalStillHolds(cr) {
                // The rule that let it go ahead expired or was removed meanwhile: now it waits for the person.
                cr.approved = false
                say("          request \(cr.id): the rule that covered it no longer does")
                park(cr)
                return nil
            }
            return left
        }
        guard dispatchTime() != nil else { return codeResult(cr) }
        let p: Payload
        do { p = try load() } catch { return failed("cannot read the vault") }
        guard let t = self.target(p, cr.vm) else { return failed("VM \(cr.vm) isn't bound to the vault (metacomputer secrets bind)") }
        if let why = requiredSinkProblem("code_fill", ["request_id": cr.id, "origin": cr.site, "vm": cr.vm], cr.who) {
            return denied(why)
        }
        if let problem = runner.listenerProblem(t) { return problem.denied ? denied(problem.reason) : failed(problem.reason) }
        if cr.kind == "link" && target.human {
            return denied("a sign-in link opens only in page-mode Chrome for now (human-mode browsers take codes)")
        }
        // A code is checked first, without the value (the tab on the site, the field): a refusal there keeps the request
        // for `fill --request ID`, since nothing reached the VM. Once the value is sent, the request is used, whatever
        // the VM says next (it could answer "refused" after receiving the value, to have it sent again).
        var rq: [String: Any] = cr.kind == "link" ? ["mode": "open", "origins": [cr.site], "field": "text"]
                                                   : ["field": "otp", "origins": [cr.site]]
        if target.human {
            if let w = target.windowID { rq["window_id"] = w }
        } else {
            if let s = target.selector, cr.kind == "code" { rq["selector"] = s }
            if let pr = target.profile { rq["profile"] = pr }
            if let bp = target.browserPID { rq["browser_pid"] = bp }
        }
        func step(_ request: [String: Any], _ value: [UInt8]?, _ timeout: TimeInterval) -> [String: Any] {
            codeInFlight = cr.id
            defer { codeInFlight = nil }
            do {
                return target.human ? try runner.human(t, request: request, value: value, timeout: timeout)
                                    : try runner.fill(t, request: request, value: value, timeout: timeout)
            } catch let e as VaultError { return failed(e.message) } catch { return failed("the step in the VM failed") }
        }
        if cr.kind == "code" {
            var chk = rq
            chk["mode"] = "check"
            guard let left = dispatchTime() else { return codeResult(cr) }
            let c = step(chk, nil, left)
            if cr.final { return codeResult(cr) }
            guard c["ok"] as? Bool == true, originOK(c, [cr.site]) else {
                let reason = (c["ok"] as? Bool == true) ? "the VM reported a site outside the request's site" : (c["reason"] as? String ?? "the check failed")
                if c["status"] as? String == "cancelled" {
                    codeFinish(cr, state: "refused", reason: requesterLeftReason)
                    return codeResult(cr)
                }
                cr.reason = reason
                record(["op": "code_fill", "result": c["status"] as? String == "denied" ? "denied" : "error", "request_id": cr.id, "origin": cr.site,
                        "kind": cr.kind, "vm": cr.vm, "approval": cr.approval, "target": target.record, "requester": cr.who.record,
                        "reason": reason, "step": "check"])
                say("          -> request \(cr.id) not filled (\(reason)); the value wasn't sent, so `fill --request \(cr.id)` can try again until \(clockTime(cr.expires))")
                flushSinks(force: true)
                var out = codeResult(cr)
                out["ok"] = false
                out["status"] = "denied"
                out["reason"] = reason
                return out
            }
            rq["mode"] = "fill"
        } else {
            // The link's own site, as checked here (and approved): the VM opens nothing else, and the tab must land on the
            // site or on that origin, after any redirect.
            let lo = cr.linkOrigin ?? ""
            rq["link_origin"] = lo
            rq["final_origins"] = lo == cr.site || lo.isEmpty ? [cr.site] : [cr.site, lo]
        }
        guard let left = dispatchTime() else { return codeResult(cr) }
        var r = step(rq, cr.value, left)
        let landed = (rq["final_origins"] as? [String]) ?? [cr.site]
        if r["ok"] as? Bool == true && !originOK(r, cr.kind == "link" ? landed : [cr.site]) {
            r = failed(cr.kind == "link" ? "the link led to another site (not shown); check the page" : "the VM reported a site outside the request's site")
        }
        if r["ok"] as? Bool == true, cr.kind == "link", let o = r["origin"] as? String, o != cr.site, isIdentityProviderOrigin(o) {
            r = failed("the link led to an identity provider's sign-in page, which always asks; nothing more was done")
        }
        let status = r["status"] as? String ?? "error"
        var rec: [String: Any] = ["op": "code_fill", "result": status == "ok" ? "ok" : status, "request_id": cr.id, "origin": cr.site,
                                  "kind": cr.kind, "vm": cr.vm, "approval": cr.approval, "target": target.record, "requester": cr.who.record]
        if let route = r["route"] as? String, ["element", "keyboard"].contains(route) { rec["route"] = route }
        // A relayed code's length is the VM's choice: not recorded (a given one's is the giver's, and is).
        if status == "ok", cr.kind == "code", cr.how != "relay" { rec["chars"] = cr.value.count }
        if let reason = r["reason"] as? String, status != "ok" { rec["reason"] = reason }
        if cr.how == "relay" { rec["op"] = "code_relay"; if let f = cr.fromOrigin { rec["from"] = f } }
        if cr.kind == "link", let h = cr.reportedLinkHost { rec["link_host"] = h }
        record(rec)
        var extra: [String: Any] = [:]
        if status == "ok" {
            let chars = cr.value.count
            codeFinish(cr, state: "filled", reason: nil)
            if cr.kind == "code" && cr.how != "relay" { extra["chars"] = chars }
            if let route = r["route"] as? String { extra["route"] = route }
            say("          -> request \(cr.id): " + (cr.kind == "link" ? "the link was opened in the tab on \(cr.site)" : "filled into \(cr.site)"))
        } else {
            // The value went to the VM: the request is used, whatever came back.
            codeFinish(cr, state: "refused", reason: status == "cancelled" ? requesterLeftReason : ((r["reason"] as? String) ?? "the fill failed"))
        }
        flushSinks(force: true)
        return codeResult(cr, extra)
    }

    /// Expire what's due, and nothing else: safe to call from any loop that waits (a question, a dialog, a step in the
    /// VM: runProcess calls it through waitHook), so a request never outlives its time, or keeps its value, because the
    /// unlock process was busy with something else. During a step, the request that step serves is left to it (the step
    /// is cut at that request's expiry anyway), and audit sinks are delivered to afterwards, not from inside the wait.
    func codeExpirySweep(duringStep: Bool = false) {
        let now = Date()
        for cr in codeRequests.values where !cr.final && now >= cr.expires && !(duringStep && cr.id == codeInFlight) {
            codeFinish(cr, state: "expired", reason: "it expired before it was filled", flush: !duringStep)
        }
    }

    /// `relay`: the code or link read from an element of a page in the VM, checked, then filled or opened in the target.
    func opCodeRelay(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let from = req["from"] as? [String: Any], let fromOrigin = from["origin"] as? String, canonicalOrigin(fromOrigin) == fromOrigin,
              let sel = from["selector"] as? String, !sel.isEmpty, sel.count <= 300 else {
            return failed("relay needs --from TAB/ELEMENT (a site and a CSS selector that matches exactly one element)")
        }
        guard let tgt = req["target"] as? [String: Any] else { return failed("relay needs --to TAB/FIELD") }
        if tgt["mode"] as? String == "human" {
            return denied("relay into a human-mode browser isn't supported yet: use --to with a page-mode Chrome tab, or `secrets code --human`")
        }
        let (crOpt, err) = newCode(req, who, how: "relay")
        guard let cr = crOpt else { return err ?? failed("bad request") }
        cr.fromOrigin = fromOrigin
        cr.source = "relay"
        say("\(clockTime())  request \(cr.id): relay a \(cr.kind) from \(fromOrigin) into \(cr.site) on VM \(cr.vm), asked by \(who.describe)")
        let p: Payload
        do { p = try load() } catch { codeFinish(cr, state: "refused", reason: "cannot read the vault"); return codeResult(cr) }
        guard let t = target(p, cr.vm) else { codeFinish(cr, state: "refused", reason: "the VM isn't bound"); return codeResult(cr) }
        if let problem = runner.listenerProblem(t) {
            codeFinish(cr, state: "refused", reason: problem.reason)
            return codeResult(cr)
        }
        var rq: [String: Any] = ["mode": "read", "what": cr.kind == "link" ? "links" : "text", "origins": [fromOrigin], "selector": sel,
                                 "field": "text"]
        if let pr = from["profile"] as? String, validName(pr) { rq["profile"] = pr }
        if let bp = (from["browser_pid"] as? NSNumber)?.intValue, bp > 1 { rq["browser_pid"] = bp }
        let read: GuestRead
        let readTime = min(Double(remaining()), cr.expires.timeIntervalSinceNow, 60)
        guard readTime >= 1 else { codeFinish(cr, state: "expired", reason: "it expired before it was filled"); return codeResult(cr) }
        codeInFlight = cr.id
        defer { codeInFlight = nil }
        do { read = try runner.read(t, request: rq, timeout: readTime) } catch let e as VaultError {
            codeFinish(cr, state: "refused", reason: e.message)
            return codeResult(cr)
        } catch { codeFinish(cr, state: "refused", reason: "reading the page failed"); return codeResult(cr) }
        codeInFlight = nil
        if cr.final { return codeResult(cr) }       // it ended while the page was read: nothing read is kept
        guard read.ok, read.origin == fromOrigin else {
            codeFinish(cr, state: "refused", reason: read.reason ?? "the VM reported a page outside --from's site")
            return codeResult(cr)
        }
        var value: [UInt8]
        if cr.kind == "code" {
            let (code, why) = extractCode(fromText: read.text, cr.shape)
            guard let c = code else { codeFinish(cr, state: "refused", reason: why); return codeResult(cr) }
            value = Array(c.utf8)
        } else {
            // Links: the one that goes to the site (or a host a rule lists); several distinct ones, or none, refuse.
            let rules = (p.rules ?? []).filter { $0.coversRelay(site: cr.site, fromHost: originHost(fromOrigin), who: who) }
            let extra = rules.flatMap { $0.link_hosts ?? [] }
            var parsed: [(String, (origin: String, host: String))] = []
            for l in read.links { if let o = linkOrigin(l), !parsed.contains(where: { $0.0 == l }) { parsed.append((l, o)) } }
            // Only links to the site or to a host a covering rule lists are candidates: the VM chose every other one, and
            // its host is never shown or asked about. The refusals are fixed words (no counts, no hosts: the VM's choice).
            let onSite = parsed.filter { linkHostAllowed($0.1, site: cr.site, extra: extra) }
            guard onSite.count == 1 else {
                codeFinish(cr, state: "refused", reason: onSite.isEmpty
                           ? "the element holds no link to the site, or to a host a rule lists (other hosts aren't shown); nothing was opened"
                           : "the element holds more than one link to the site; point --from at the one link")
                return codeResult(cr)
            }
            let pick = onSite[0]
            cr.linkHost = pick.1.host
            cr.linkOrigin = pick.1.origin
            cr.linkOK = pick.1.origin == cr.site
            cr.listedHosts = extra
            value = Array(pick.0.trimmingCharacters(in: .whitespacesAndNewlines).utf8)
        }
        cr.value = value
        wipe(&value)
        cr.state = "given"
        var rec: [String: Any] = ["op": "code_give", "result": "ok", "request_id": cr.id, "origin": cr.site, "kind": cr.kind, "vm": cr.vm,
                                  "source": "relay", "from": fromOrigin, "requester": who.record]
        if let h = cr.reportedLinkHost { rec["link_host"] = h }
        record(rec)
        return codeDecide(cr, interactive: false, presentAtMac: false)
    }

    /// `fill --request ID`: a given, approved request into its target (or the one named now).
    func opCodeFill(_ req: [String: Any], _ who: Requester) -> [String: Any] {
        guard let id = req["id"] as? String, validRequestID(id), let cr = codeRequests[id] else {
            return failed("no code request \(clean(req["id"] as? String ?? "", max: 12)) (metacomputer secrets requests)")
        }
        if Date() >= cr.expires && !cr.final { codeFinish(cr, state: "expired", reason: "it expired before it was filled") }
        if cr.final { return codeResult(cr) }
        guard cr.state == "given" else {
            var out = codeResult(cr)
            out["ok"] = false
            out["status"] = "denied"
            out["reason"] = "request \(id) has no value yet (the person gives it with `metacomputer secrets give \(id)`)"
            return out
        }
        if cr.parked { return codeResult(cr) }
        if req["target"] != nil {
            let (t, why) = CodeTarget.from(req["target"] as? [String: Any])
            guard let t else { return failed(why ?? "bad target") }
            cr.target = t
        }
        guard cr.target != nil else { return failed("where should it go? Add --selector CSS (or focus the field), or --human") }
        return codeAct(cr)
    }

    // MARK: approve and deny for a waiting code request

    /// `approve ID` for a request that waits (parked): the same confirmation as any approval, Touch ID / the login password
    /// in macOS's dialog (this waits for it, serving other connections meanwhile) or a passphrase proof; then it acts.
    func opApproveCode(_ req: [String: Any], _ who: Requester, _ cr: CodeRequest) -> [String: Any] {
        guard cr.parked, !cr.final else { return failed("request \(cr.id) doesn't wait for approval") }
        if Date() >= cr.expires {
            codeFinish(cr, state: "expired", reason: "it expired before it was approved")
            return codeResult(cr)
        }
        guard parseScope(req["scope"] as? String ?? "use") == .use else {
            return denied("a one-time code is approved for this one use only (no --scope)")
        }
        switch req["method"] as? String ?? "local-auth" {
        case "passphrase":
            var k = approvalProofKey(key)
            defer { wipe(&k) }
            guard let proof = req["proof"] as? String,
                  constantTimeEqual(proof, approvalProof(k, id: cr.id, challenge: cr.challenge, summary: cr.summary, scope: .use)) else {
                record(["op": "approve", "result": "denied", "request_id": cr.id, "method": "passphrase",
                        "reason": "the passphrase proof doesn't check out", "requester": who.record])
                say("\(clockTime())  an approval of request \(cr.id) was refused: its passphrase proof doesn't check out (from \(who.describe))")
                return denied("the passphrase proof doesn't check out")
            }
            cr.approval = "each:passphrase"
        case "local-auth":
            say("\(clockTime())  request \(cr.id): asking macOS for Touch ID or the login password (`approve` from \(who.describe))")
            // macOS's dialog is the person's: it may name a given link's real host (privateSummary); nothing else does.
            let la = startLocalAuth("approve request \(cr.id), for this one use: \(cr.privateSummary)")
            let deadline = min(cr.expires, Date().addingTimeInterval(TimeInterval(min(promptTimeout, max(1, remaining())))))
            while !la.done {
                checkVault()
                codeExpirySweep()
                // The `approve` that asked went away (killed, or withdrawn): nothing is approved, nothing is filled.
                if requesterGone() {
                    la.cancel()
                    say("          -> approval of request \(cr.id) withdrawn: the `approve` that asked went away; it still waits")
                    return ["ok": false, "status": "cancelled", "reason": requesterLeftReason, "id": cr.id]
                }
                if receivedSignal != 0 || stopReason != nil || Date() >= deadline || cr.final { la.cancel(); break }
                var pfd = pollfd(fd: listenFD, events: Int16(POLLIN), revents: 0)
                if poll(&pfd, 1, 200) > 0, pfd.revents & Int16(POLLIN) != 0 {
                    let c = accept(listenFD, nil, nil)
                    if c >= 0 { _ = fcntl(c, F_SETFD, FD_CLOEXEC); serviceDuringWait(c) }
                }
            }
            guard la.ok, !cr.final else {
                say("          -> not approved (\(la.why))")
                return denied("not approved: \(cr.final ? "the request ended meanwhile" : la.why). Add --passphrase to confirm with the vault passphrase instead")
            }
            cr.approval = "each:local-auth"
        default:
            return failed("approve: the method is local-auth or passphrase")
        }
        cr.parked = false
        cr.approved = true
        record(["op": "authorize", "result": "ok", "request_id": cr.id, "use": cr.kind == "link" ? "link" : "code", "name": "request \(cr.id)",
                "vm": cr.vm, "origin": cr.site, "approval": cr.approval, "confirmed": cr.approval == "each:passphrase" ? "passphrase" : "local-auth",
                "presence": requirePresence, "requester": cr.who.record])
        say("          allowed (\(cr.approval == "each:passphrase" ? "the vault passphrase" : "Touch ID or the login password")), for this one use")
        if requesterGone() {
            // Allowed, but the `approve` that asked has gone: the fill it would wait for isn't started (it stays approved
            // for `fill --request ID` until it expires).
            say("          -> not filled now: the `approve` that asked went away")
            return ["ok": false, "status": "cancelled", "reason": requesterLeftReason, "id": cr.id]
        }
        var out = codeAct(cr)
        if out["status"] as? String == "filled" || out["status"] as? String == "given" { out["ok"] = true; out["approved"] = true }
        out["id"] = cr.id
        out["scope_words"] = "for this one use"
        return out
    }

    func opDenyCode(_ cr: CodeRequest, _ who: Requester) -> [String: Any] {
        guard !cr.final else { return failed("request \(cr.id) has already ended (\(cr.state))") }
        cr.dialog?.kill()
        cr.dialog = nil
        codeFinish(cr, state: "refused", reason: "refused with `deny \(cr.id)`")
        say("\(clockTime())  request \(cr.id) refused with `deny` (from \(who.describe))")
        return ["ok": true, "status": "ok", "id": cr.id]
    }

    /// Lock: every value goes, and every dialog closes.
    func codeLockdown() {
        for cr in codeRequests.values {
            cr.dialog?.kill()
            cr.dialog = nil
            cr.dropValue()
        }
        codeRequests = [:]
    }
}

// MARK: - client commands

/// The value for `give` and `code`: from stdin only. On a terminal, typed with the echo off; from a pipe, one line, at
/// most 4096 bytes (a longer line is refused, not cut). Never from the command line or the environment.
func readCodeValue(prompt: String) throws -> ([UInt8], Bool) {
    if isatty(0) == 1 {
        installPromptSignalHandlers()
        return (try readHidden(prompt), isatty(2) == 1)
    }
    var line = [UInt8]()
    var c: UInt8 = 0
    while true {
        let n = read(0, &c, 1)
        if n < 0 && errno == EINTR { continue }
        if n <= 0 || c == 0x0A { break }
        line.append(c)
        if line.count > 4096 { wipe(&line); throw VaultError("the value on stdin is longer than 4096 bytes; refusing", code: Exit.denied) }
    }
    c = 0
    if line.last == 0x0D { line.removeLast() }
    return (line, false)
}

/// --into / --human / --window-id / --profile / --browser-pid as a request's target, and the site it names.
func codeTargetOptions(_ a: Args, site: String?, required: Bool) throws -> ([String: Any]?, String?) {
    if a.has("human") {
        guard a.last("into") == nil, a.last("profile") == nil, a.last("browser-pid") == nil else {
            throw usageError("--human goes to the focused field of a human-mode browser: no --into, --profile or --browser-pid")
        }
        var t: [String: Any] = ["mode": "human"]
        if let w = a.last("window-id") {
            guard let n = Int(w), n > 0 else { throw usageError("--window-id: the browser window's id (list_windows)") }
            t["window_id"] = n
        }
        return (t, nil)
    }
    if a.last("window-id") != nil { throw usageError("--window-id goes with --human") }
    guard let into = a.last("into") else {
        if required { throw usageError("where it goes: --into TAB/FIELD (a page-mode Chrome tab on the site and a CSS selector) or --human") }
        return (nil, nil)
    }
    guard let (origin, sel) = parseInto(into) else {
        throw usageError("--into TAB/FIELD: the tab's site (https://host or host[:port]; http only for localhost or 127.0.0.1 with a port) and an optional CSS selector, like x.com/#otp")
    }
    if let s = site, s != origin { throw usageError("--into names \(origin), but the request is for \(s): a code goes only into a tab on its own site") }
    var t: [String: Any] = ["mode": "page"]
    if let s = sel { t["selector"] = s }
    if let p = a.last("profile") {
        guard validName(p) else { throw usageError("--profile: a driver profile name") }
        t["profile"] = p
    }
    if let b = a.last("browser-pid") {
        guard let n = Int(b), n > 1 else { throw usageError("--browser-pid: a process id") }
        t["browser_pid"] = n
    }
    return (t, origin)
}

func codeExit(_ res: [String: Any]) -> Int32 {
    switch res["status"] as? String {
    case "filled", "given", "pending", "ok": return Exit.ok
    case "needs_approval": return Exit.needsApproval
    case "expired": return Exit.expired
    case "denied", "refused": return Exit.denied
    case "locked": return Exit.locked
    default: return Exit.error
    }
}

let codeResultKeys = ["ok", "status", "state", "request_id", "site", "kind", "link_host", "chars", "route", "expires", "expires_in_s",
                      "dialog", "give", "approve", "reason"]

func printCodeResult(_ res: [String: Any], json: Bool, verb: String) {
    var out: [String: Any] = [:]
    for k in codeResultKeys { if let v = res[k] { out[k] = v } }
    if out["ok"] == nil { out["ok"] = false }
    if out["status"] == nil { out["status"] = "error" }
    let safe = (cleanAll(out) as? [String: Any]) ?? ["ok": false, "status": "error"]
    if json { oprint(jsonLine(safe)); return }
    let id = safe["request_id"] as? String ?? "?"
    switch safe["status"] as? String ?? "" {
    case "filled": oprint("Request \(id): " + ((safe["kind"] as? String) == "link" ? "the sign-in link was opened" : "the code was filled") + " on \(safe["site"] ?? "").")
    case "given": oprint("Request \(id): given and approved; it waits for `metacomputer secrets fill --request \(id)`.")
    case "pending": oprint("Request \(id): waiting for the \(safe["kind"] ?? "code") (until \(safe["expires"] ?? "?")). The person gives it with: metacomputer secrets give \(id)")
    case "needs_approval": oprint(clean("Request \(id): \(verb), and it waits for your approval. Run in your own terminal: metacomputer secrets approve \(id)", max: 400))
    default: eprint(clean("mcomp-vault: request \(id): \(safe["status"] ?? "error"): \(safe["reason"] ?? "no answer")", max: 500))
    }
}

func cmdAsk(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: ask --vm VM --site https://site --kind code|link --why TEXT [--into TAB/FIELD | --human [--window-id N]] [--ttl 10m] [--wait DUR] [--pattern digits|alnum] [--length N|A-B] [--json]"
    let a = try parseArgs(argv, values: ["vm", "site", "kind", "why", "into", "window-id", "profile", "browser-pid", "ttl", "wait", "pattern",
                                         "length", "request-id"], flags: ["human", "json"])
    guard a.positional.isEmpty, let vm = a.last("vm"), validName(vm), let rawSite = a.last("site"), let why = a.last("why") else { throw usageError(usage) }
    guard let site = canonicalOrigin(rawSite) else { throw usageError("--site https://host[:port] (no path)") }
    let kind = a.last("kind") ?? "code"
    guard kind == "code" || kind == "link" else { throw usageError("--kind code or link") }
    guard !why.trimmingCharacters(in: .whitespaces).isEmpty, why.count <= 400 else { throw usageError("--why: what it's for, in a few words (at most 400 characters)") }
    var ttl = codeDefaultTTL
    if let t = a.last("ttl") { guard let n = parseDuration(t), n >= codeMinTTL(), n <= codeMaxTTL else { throw usageError("--ttl: 30s to 15m") }; ttl = n }
    var wait: Int?
    if let w = a.last("wait") { guard let n = parseDuration(w), n <= codeMaxTTL + 60 else { throw usageError("--wait: like 90s or 10m") }; wait = n }
    guard CodeShape.parse(pattern: a.last("pattern"), length: a.last("length")) != nil else { throw usageError("--pattern digits|alnum, --length N or MIN-MAX (4 to 16)") }
    let (target, _) = try codeTargetOptions(a, site: site, required: false)
    var req: [String: Any] = ["op": "code_ask", "vm": vm, "site": site, "kind": kind, "why": why, "ttl_s": ttl]
    if let t = target { req["target"] = t }
    if let p = a.last("pattern") { req["pattern"] = p }
    if let l = a.last("length") { req["length"] = l }
    if let id = a.last("request-id") { guard validRequestID(id) else { throw usageError("--request-id: 8 hex digits") }; req["request_id"] = id }
    var res: [String: Any]
    do { res = try request(paths, req, timeout: 60) } catch ClientError.locked {
        audit(paths, ["op": "code_request", "result": "locked", "vm": vm, "origin": site, "kind": kind])
        printCodeResult(["ok": false, "status": "locked", "reason": "locked"], json: a.has("json"), verb: "asked")
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    guard let w = wait, res["ok"] as? Bool == true, let id = res["request_id"] as? String else {
        printCodeResult(res, json: a.has("json"), verb: "asked")
        return codeExit(res)
    }
    if !a.has("json") { printCodeResult(res, json: false, verb: "asked") }
    // --wait: until it's filled (or given, without a target), refused or expired, or the wait ends.
    let deadline = Date().addingTimeInterval(TimeInterval(w))
    installCancelSignalHandlers()
    while Date() < deadline && cancelSignals == 0 {
        usleep(500_000)
        do { res = try request(paths, ["op": "code_status", "id": id], timeout: 15) } catch ClientError.locked {
            printCodeResult(["ok": false, "status": "locked", "request_id": id, "reason": "the vault locked"], json: a.has("json"), verb: "asked")
            return Exit.locked
        }
        let st = res["status"] as? String ?? ""
        if st != "pending" && !(st == "needs_approval") { break }
    }
    printCodeResult(res, json: a.has("json"), verb: "given")
    return codeExit(res)
}

func cmdGive(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["json"])
    guard a.positional.count == 1, validRequestID(a.positional[0]) else {
        throw usageError("usage: give ID [--json]  (the value comes from stdin only: typed hidden at a terminal, or one line from a pipe; never on the command line)")
    }
    let id = a.positional[0]
    // Make sure there's something to give to before asking for the value.
    let st: [String: Any]
    do { st = try request(paths, ["op": "code_status", "id": id], timeout: 15) } catch ClientError.locked {
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    guard st["status"] as? String == "pending" else {
        printCodeResult(st["request_id"] == nil ? ["ok": false, "status": "error", "request_id": id, "reason": st["reason"] ?? "no such request"] : st,
                        json: a.has("json"), verb: "given")
        return st["request_id"] == nil ? Exit.error : (st["status"] as? String == "expired" ? Exit.expired : Exit.denied)
    }
    if isatty(0) == 1 {
        eprint(clean("Request \(id): a one-time \(st["kind"] ?? "code") for \(st["site"] ?? "?")" + ((st["why"] as? String).map { " (\($0))" } ?? ""), max: 400))
    }
    var (value, interactive) = try readCodeValue(prompt: "One-time \(st["kind"] as? String ?? "code") (hidden): ")
    defer { wipe(&value) }
    var res: [String: Any]
    do {
        res = try request(paths, ["op": "code_give", "id": id, "value": String(decoding: value, as: UTF8.self), "interactive": interactive],
                          timeout: 300, cancellable: true)
    } catch ClientError.locked {
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    printCodeResult(res, json: a.has("json"), verb: "given")
    return codeExit(res)
}

func cmdCode(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: code --vm VM --site https://site (--into TAB/FIELD | --human [--window-id N]) [--kind code|link] [--why TEXT] [--pattern digits|alnum] [--length N|A-B] [--json]  < value"
    let a = try parseArgs(argv, values: ["vm", "site", "kind", "why", "into", "window-id", "profile", "browser-pid", "pattern", "length", "ttl",
                                         "request-id", "on-behalf-pid"], flags: ["human", "json"])
    guard a.positional.isEmpty else {
        throw usageError("`code` takes the value from stdin only, never from the command line (pipe it in: fetch-code | metacomputer secrets code ...)")
    }
    guard let vm = a.last("vm"), validName(vm), let rawSite = a.last("site"), let site = canonicalOrigin(rawSite) else { throw usageError(usage) }
    let kind = a.last("kind") ?? "code"
    guard kind == "code" || kind == "link" else { throw usageError("--kind code or link") }
    guard CodeShape.parse(pattern: a.last("pattern"), length: a.last("length")) != nil else { throw usageError("--pattern digits|alnum, --length N or MIN-MAX (4 to 16)") }
    let (target, _) = try codeTargetOptions(a, site: site, required: true)
    var req: [String: Any] = ["op": "code_put", "vm": vm, "site": site, "kind": kind, "why": a.last("why") ?? "", "target": target as Any]
    if let p = a.last("pattern") { req["pattern"] = p }
    if let l = a.last("length") { req["length"] = l }
    if let t = a.last("ttl") { guard let n = parseDuration(t), n >= codeMinTTL(), n <= codeMaxTTL else { throw usageError("--ttl: 30s to 15m") }; req["ttl_s"] = n }
    try useOptions(a, &req)
    var (value, interactive) = try readCodeValue(prompt: "One-time \(kind) for \(site) (hidden): ")
    defer { wipe(&value) }
    req["value"] = String(decoding: value, as: UTF8.self)
    req["interactive"] = interactive
    var res: [String: Any]
    do { res = try request(paths, req, timeout: 300, cancellable: true) } catch ClientError.locked {
        req["value"] = nil
        audit(paths, ["op": "code_request", "result": "locked", "vm": vm, "origin": site, "kind": kind])
        printCodeResult(["ok": false, "status": "locked", "reason": "locked"], json: a.has("json"), verb: "piped in")
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    req["value"] = nil
    printCodeResult(res, json: a.has("json"), verb: "piped in")
    return codeExit(res)
}

func cmdRelay(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: relay --vm VM --from TAB/ELEMENT --to TAB/FIELD --site https://site [--kind code|link] [--pattern digits|alnum] [--length N|A-B] [--from-profile P] [--profile P] [--browser-pid PID] [--json]"
    let a = try parseArgs(argv, values: ["vm", "from", "to", "site", "kind", "pattern", "length", "profile", "from-profile", "browser-pid", "ttl",
                                         "request-id", "on-behalf-pid"], flags: ["json", "human"])
    guard a.positional.isEmpty, let vm = a.last("vm"), validName(vm), let f = a.last("from"), let rawSite = a.last("site"),
          let site = canonicalOrigin(rawSite) else { throw usageError(usage) }
    if a.has("human") { throw usageError("relay into a human-mode browser isn't supported yet; use `secrets code --human` (or a page-mode Chrome tab with --to)") }
    guard let to = a.last("to") else { throw usageError(usage) }
    let kind = a.last("kind") ?? "code"
    guard kind == "code" || kind == "link" else { throw usageError("--kind code or link") }
    guard CodeShape.parse(pattern: a.last("pattern"), length: a.last("length")) != nil else { throw usageError("--pattern digits|alnum, --length N or MIN-MAX (4 to 16)") }
    guard let (fromOrigin, fromSel) = parseInto(f), let sel = fromSel else {
        throw usageError("--from TAB/ELEMENT: the tab's site and a CSS selector for the element that holds the code or link, like mail.example.com/#message .code")
    }
    guard let (toOrigin, toSel) = parseInto(to) else { throw usageError("--to TAB/FIELD: the tab's site and an optional CSS selector, like x.com/#otp") }
    guard toOrigin == site else { throw usageError("--to names \(toOrigin), but --site is \(site): a code goes only into a tab on its own site") }
    var target: [String: Any] = ["mode": "page"]
    if let s = toSel { target["selector"] = s }
    if let p = a.last("profile") { guard validName(p) else { throw usageError("--profile: a driver profile name") }; target["profile"] = p }
    if let b = a.last("browser-pid") { guard let n = Int(b), n > 1 else { throw usageError("--browser-pid: a process id") }; target["browser_pid"] = n }
    var from: [String: Any] = ["origin": fromOrigin, "selector": sel]
    if let p = a.last("from-profile") ?? a.last("profile") { guard validName(p) else { throw usageError("--from-profile: a driver profile name") }; from["profile"] = p }
    if let b = target["browser_pid"] { from["browser_pid"] = b }
    var req: [String: Any] = ["op": "code_relay", "vm": vm, "site": site, "kind": kind, "from": from, "target": target, "why": "relay"]
    if let p = a.last("pattern") { req["pattern"] = p }
    if let l = a.last("length") { req["length"] = l }
    if let t = a.last("ttl") { guard let n = parseDuration(t), n >= codeMinTTL(), n <= codeMaxTTL else { throw usageError("--ttl: 30s to 15m") }; req["ttl_s"] = n }
    try useOptions(a, &req)
    var res: [String: Any]
    do { res = try request(paths, req, timeout: 300, cancellable: true) } catch ClientError.locked {
        audit(paths, ["op": "code_relay", "result": "locked", "vm": vm, "origin": site, "kind": kind])
        printCodeResult(["ok": false, "status": "locked", "reason": "locked"], json: a.has("json"), verb: "read")
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    if !a.has("json"), res["status"] as? String == "filled" {
        oprint(clean("relayed: " + (kind == "link" ? "opened" : "filled") + " (request \(res["request_id"] ?? "?"))", max: 200))
        return Exit.ok
    }
    printCodeResult(res, json: a.has("json"), verb: "read")
    return codeExit(res)
}

/// `fill --request ID`: a given request's value into the focused field (or --selector) of the tab on its site, or --human.
func cmdFillRequest(_ paths: Paths, _ a: Args) throws -> Int32 {
    guard let id = a.last("request"), validRequestID(id), a.positional.isEmpty, a.last("field") == nil else {
        throw usageError("usage: fill --request ID [--selector CSS] [--profile P] [--browser-pid PID] | fill --request ID --human [--window-id N]")
    }
    var req: [String: Any] = ["op": "code_fill", "id": id]
    if a.has("human") {
        var t: [String: Any] = ["mode": "human"]
        if let w = a.last("window-id") { guard let n = Int(w), n > 0 else { throw usageError("--window-id: a window id") }; t["window_id"] = n }
        req["target"] = t
    } else if a.last("selector") != nil || a.last("profile") != nil || a.last("browser-pid") != nil {
        var t: [String: Any] = ["mode": "page"]
        if let s = a.last("selector") { guard s.count <= 300 else { throw usageError("--selector: at most 300 characters") }; t["selector"] = s }
        if let p = a.last("profile") { guard validName(p) else { throw usageError("--profile: a driver profile name") }; t["profile"] = p }
        if let b = a.last("browser-pid") { guard let n = Int(b), n > 1 else { throw usageError("--browser-pid: a process id") }; t["browser_pid"] = n }
        req["target"] = t
    }
    var res: [String: Any]
    do { res = try request(paths, req, timeout: 300, cancellable: true) } catch ClientError.locked {
        eprint("mcomp-vault: \(lockedHint(paths))")
        printCodeResult(["ok": false, "status": "locked", "request_id": id, "reason": "locked"], json: true, verb: "filled")
        return Exit.locked
    }
    printCodeResult(res, json: true, verb: "filled")
    return codeExit(res)
}
