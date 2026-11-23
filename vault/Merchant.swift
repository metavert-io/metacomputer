// mcomp-vault: a merchant's published rules for agents, and Web Bot Auth (SECURITY.md, "Paying for things").
//
// Before a card goes into a checkout, the unlock process reads the merchant's /agents.md and its UCP profile
// (/.well-known/ucp) from this Mac. Where /agents.md says not to script checkout forms or enter card details (Shopify's
// stores say so, for one), the purchase is refused and the agent hands payment to the user (`metacomputer handoff`).
// What's read is untrusted: only fixed rule names, a few payment-handler ids of a fixed shape and yes/no answers come
// out of it, never its words.
//
// Web Bot Auth (an IETF draft: HTTP Message Signatures, RFC 9421, with an Ed25519 key whose directory the user hosts) is
// off unless the user turns it on: then these requests, and `wba sign`'s, carry a signature naming that directory.
import CryptoKit
import Darwin
import Foundation

// MARK: - the rules

struct MerchantRules {
    var agentsMD = "skipped"            // found | none | unclear (truncated, not text, a web page, an odd answer) | error | skipped
    var ucp = "skipped"                 // found | none | error | skipped
    var forbids: [String] = []          // no-scripted-checkout | no-card-entry
    var buyerContinues = false          // it asks agents to hand checkout to the buyer
    var shopify = false
    var handlers: [String] = []         // UCP payment handler ids (reverse-DNS names only)
    var ap2 = false                     // the UCP profile mentions AP2
    var note: String?                   // why it wasn't read, in this program's words
    var signed = false                  // the requests carried a Web Bot Auth signature

    /// A ban, or the store asking agents to have the buyer complete checkout (a handoff, not a card fill).
    var forbidden: Bool { !forbids.isEmpty || buyerContinues }
    /// Whether the rules are known: read whole and understood, or plainly absent. Anything else is "can't tell", which an
    /// agent card refuses and an approval shows; it's never taken as permission.
    var known: Bool { agentsMD == "found" || agentsMD == "none" }

    /// One line for the approval, the output and the terminal.
    var summary: String {
        if forbidden {
            var parts = forbids.map { $0 == "no-card-entry" ? "entering card details" : "scripted checkout" }
            if buyerContinues { parts.append("anything but the buyer completing checkout") }
            return "its /agents.md forbids " + parts.joined(separator: " and ")
        }
        switch agentsMD {
        case "found": return "/agents.md published, nothing in it against card entry" + (shopify ? " (a Shopify store)" : "")
        case "none": return "no /agents.md published" + (shopify ? " (a Shopify store)" : "")
        case "error", "unclear": return "can't tell (\(note ?? "unreadable"))"
        default: return "can't tell (\(note ?? "not checked"))"
        }
    }

    var record: [String: Any] {
        var d: [String: Any] = ["agents_md": agentsMD, "ucp": ucp, "forbids": forbids, "buyer_continues": buyerContinues,
                                "shopify": shopify, "payment_handlers": handlers, "ap2_advertised": ap2, "summary": summary]
        if signed { d["web_bot_auth"] = true }
        return d
    }
}

/// Lower-cased, Markdown emphasis and code marks dropped, whitespace folded: what the patterns read.
func normalizeRulesText(_ s: String) -> String {
    var t = String(s.prefix(256 << 10)).lowercased()
    for ch in ["*", "`", "_", "\u{2019}"] { t = t.replacingOccurrences(of: ch, with: ch == "\u{2019}" ? "'" : "") }
    return t.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
}

/// The rules in a merchant's /agents.md that matter for a card fill. Patterns stay within one sentence; one that reads a
/// sentence as a ban when it isn't errs toward handing payment to the user.
func parseAgentsMD(_ text: String) -> (forbids: [String], buyerContinues: Bool, shopify: Bool) {
    let t = normalizeRulesText(text)
    let neg = "(?:do not|don't|never|must not|may not|should not|cannot|can't|are not (?:allowed|permitted) to|is not (?:allowed|permitted))"
    let s = "[^.!?;]"
    let scripted = [
        "\(neg)\\b\(s){0,80}\\bscript(?:ed|ing|s)?\\b\(s){0,60}\\b(?:checkout|check-out|forms?|payments?|page controls)",
        "\(neg)\\b\(s){0,40}\\bfall ?back\\b\(s){0,40}\\bscript",
        "\\b(?:automated|scripted|bot|headless)[- ]?(?:checkouts?|purchases?|purchasing)\\b\(s){0,40}\\b(?:not (?:allowed|permitted)|prohibited|forbidden|disallowed)",
        "\\b(?:no|disallow:?)\\s+(?:automated|scripted|bot|headless)[- ]?(?:checkouts?|purchases?|purchasing)\\b",
    ]
    let cardEntry = [
        "\(neg)\\b\(s){0,60}\\b(?:enter|entering|type|typing|fill|filling|input|submit|submitting|paste|pasting)\\b\(s){0,40}\\b(?:card|payment|credit card) (?:details|information|info|numbers?|data|credentials?|forms?|fields?)",
    ]
    let handoff = [
        "\\bask the (?:buyer|user|customer|shopper|human) to (?:continue|complete|finish|pay)\\b",
        "\\bhand (?:off|over)\\b\(s){0,40}\\b(?:buyer|user|customer|shopper|human)\\b",
    ]
    func any(_ ps: [String]) -> Bool { ps.contains { matches(t, $0) } }
    var forbids: [String] = []
    if any(scripted) { forbids.append("no-scripted-checkout") }
    if any(cardEntry) { forbids.append("no-card-entry") }
    return (forbids, any(handoff), t.contains("shopify"))
}

/// A UCP profile's payment handler ids (reverse-DNS names, at most 16) and whether it names AP2 anywhere.
func parseUCPProfile(_ bytes: [UInt8]) -> (handlers: [String], ap2: Bool, shopify: Bool)? {
    guard let obj = try? JSONSerialization.jsonObject(with: Data(bytes.prefix(1 << 20))) else { return nil }
    var handlers: [String] = []
    var ap2 = false
    var seen = 0
    func walk(_ v: Any, _ key: String, _ depth: Int) {
        seen += 1
        guard depth < 12, seen < 20000 else { return }
        if let s = v as? String {
            let l = s.lowercased()
            if l.contains("ap2") { ap2 = true }
            if key.contains("handler") || key.contains("payment") || key == "id" || key == "name",
               matches(l, "^[a-z][a-z0-9-]{0,40}(\\.[a-z0-9_-]{1,40}){1,6}$"), key.contains("handler") || l.contains("pay") || l.contains("card"),
               !handlers.contains(l), handlers.count < 16 {
                handlers.append(l)
            }
        } else if let a = v as? [Any] {
            for x in a { walk(x, key, depth + 1) }
        } else if let d = v as? [String: Any] {
            for (k, x) in d {
                if k.lowercased().contains("ap2") { ap2 = true }
                walk(x, k.lowercased(), depth + 1)
            }
        }
    }
    walk(obj, "", 0)
    return (handlers.sorted(), ap2, handlers.contains { $0.hasPrefix("dev.shopify.") })
}

// MARK: - reading them

/// GET a URL on the merchant's own site: no cookies or cache, redirects only within that site, at most `maxBytes`.
final class SiteFetch: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    let origin: String
    let maxBytes: Int
    let lock = NSLock()
    var data = [UInt8]()
    var status = 0
    var headers: [String: String] = [:]
    var failure: String?
    var truncated = false               // more than maxBytes came: what's here isn't the whole answer
    let done = DispatchSemaphore(value: 0)

    init(origin: String, maxBytes: Int) {
        self.origin = origin
        self.maxBytes = maxBytes
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        let o = request.url.flatMap { u in u.scheme.flatMap { s in u.host.map { h in "\(s)://\(h)" + (u.port.map { ":\($0)" } ?? "") } } }
        completionHandler(o.flatMap(canonicalOrigin) == origin ? request : nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
        lock.lock()
        if let h = response as? HTTPURLResponse {
            status = h.statusCode
            for (k, v) in h.allHeaderFields { if let ks = k as? String, let vs = v as? String { headers[ks.lowercased()] = String(vs.prefix(200)) } }
        }
        lock.unlock()
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive d: Data) {
        lock.lock()
        if data.count + d.count > maxBytes { truncated = true }
        data.append(contentsOf: d.prefix(max(0, maxBytes - data.count)))
        let full = truncated
        lock.unlock()
        if full { dataTask.cancel() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        if let e = error as NSError?, !(e.code == NSURLErrorCancelled && truncated) {
            failure = e.code == NSURLErrorTimedOut ? "no answer in time" : (e.code == NSURLErrorCancelled ? "a redirect to another site" : "network error \(e.code)")
        }
        lock.unlock()
        done.signal()
    }
}

/// (status, body, headers, why not, truncated): truncated when the answer was longer than the limit (only its start is here).
func fetchFromSite(_ origin: String, path: String, timeout: TimeInterval, extraHeaders: [String: String] = [:]) -> (Int, [UInt8], [String: String], String?, Bool) {
    guard let url = URL(string: origin + path) else { return (0, [], [:], "bad address", false) }
    let cfg = URLSessionConfiguration.ephemeral
    cfg.httpCookieStorage = nil
    cfg.httpShouldSetCookies = false
    cfg.urlCache = nil
    cfg.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
    cfg.timeoutIntervalForRequest = timeout
    cfg.timeoutIntervalForResource = timeout
    cfg.connectionProxyDictionary = [:]
    let f = SiteFetch(origin: origin, maxBytes: 256 << 10)
    let session = URLSession(configuration: cfg, delegate: f, delegateQueue: nil)
    var req = URLRequest(url: url)
    req.setValue("metacomputer-vault (+https://github.com/metavert-io/metacomputer)", forHTTPHeaderField: "User-Agent")
    req.setValue("text/markdown, text/plain, application/json;q=0.9, */*;q=0.1", forHTTPHeaderField: "Accept")
    for (k, v) in extraHeaders { req.setValue(v, forHTTPHeaderField: k) }
    let task = session.dataTask(with: req)
    task.resume()
    if f.done.wait(timeout: .now() + timeout + 2) == .timedOut { task.cancel(); _ = f.done.wait(timeout: .now() + 2) }
    session.invalidateAndCancel()
    f.lock.lock()
    defer { f.lock.unlock() }
    if let why = f.failure { return (0, [], [:], why, false) }
    return (f.status, f.data, f.headers, nil, f.truncated)
}

/// What marks a web page (anywhere in an answer, lower-cased): an /agents.md that has one isn't plainly rules.
let htmlMarkers = ["<html", "<!doctype", "<head", "<body", "<script", "<meta", "<title", "<iframe", "<style", "<?xml", "<svg"]

/// A Content-Type's media type, lower-cased, without its parameters ("" when there's none).
func mediaType(_ h: String?) -> String {
    (h ?? "").lowercased().split(separator: ";").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
}

/// Whether an origin is the VM's own loopback (a page served inside the VM, which this Mac can't reach).
func isLoopbackOrigin(_ origin: String) -> Bool {
    origin.hasPrefix("http://127.0.0.1:") || origin.hasPrefix("http://localhost:")
}

/// The merchant's rules, read now from this Mac (each request signed with Web Bot Auth when that's on).
func merchantRules(_ origin: String, wba: WebBotAuthKey? = nil, timeout: TimeInterval = 6) -> MerchantRules {
    var r = MerchantRules()
    if isLoopbackOrigin(origin) {
        var fetchLoopback = false
        #if MCOMP_VAULT_TESTING
        // Test builds only: a stand-in merchant served on this Mac's loopback.
        fetchLoopback = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_FETCH_LOOPBACK"] == "1"
        #endif
        if !fetchLoopback {
            r.note = "not checked: a page on the VM's own loopback, which this Mac can't reach"
            return r
        }
    }
    func headers() -> [String: String] {
        guard let k = wba, k.on, let h = wbaSignatureHeaders(k, authority: originAuthority(origin)) else { return [:] }
        r.signed = true
        return h
    }
    let (st, body, hdr, why, cut) = fetchFromSite(origin, path: "/agents.md", timeout: timeout, extraHeaders: headers())
    // Only a whole, readable rules file counts as rules, and only a plain 404 or 410 as none: anything else is "can't tell".
    if let why {
        r.agentsMD = "error"
        r.note = why
    } else if st == 404 || st == 410 {
        r.agentsMD = "none"
    } else if st != 200 {
        r.agentsMD = "unclear"
        r.note = "HTTP \(st)"
    } else if cut {
        r.agentsMD = "unclear"
        r.note = "its /agents.md is larger than this reads whole"
    } else if let text = String(bytes: body, encoding: .utf8) {
        // Plainly an agents.md, or "can't tell": served as text or Markdown (or with no type), no web page anywhere in it
        // (its markers, in the whole answer), no control characters.
        let ctype = mediaType(hdr["content-type"])
        let low = text.lowercased()
        if !["", "text/plain", "text/markdown", "text/x-markdown"].contains(ctype) {
            r.agentsMD = "unclear"
            r.note = "the site answered /agents.md as \(clean(ctype, max: 60)), not text or Markdown"
        } else if htmlMarkers.contains(where: { low.contains($0) }) {
            r.agentsMD = "unclear"
            r.note = "the site answered /agents.md with a web page"
        } else if text.unicodeScalars.contains(where: { ($0.value < 0x20 && !["\n", "\r", "\t"].contains($0)) || $0.value == 0x7F || $0.value == 0xFFFD }) {
            r.agentsMD = "unclear"
            r.note = "its /agents.md isn't plain text"
        } else {
            r.agentsMD = "found"
            let p = parseAgentsMD(text)
            r.forbids = p.forbids
            r.buyerContinues = p.buyerContinues
            r.shopify = p.shopify
        }
    } else {
        r.agentsMD = "unclear"
        r.note = "its /agents.md isn't text"
    }
    if (hdr["powered-by"] ?? "").lowercased().contains("shopify") || hdr["x-shopid"] != nil { r.shopify = true }
    let (st2, body2, hdr2, why2, cut2) = fetchFromSite(origin, path: "/.well-known/ucp", timeout: timeout, extraHeaders: headers())
    let ctype2 = mediaType(hdr2["content-type"])
    if why2 != nil {
        r.ucp = "error"
    } else if cut2 {
        r.ucp = "unclear"
    } else if st2 == 200, ctype2 == "application/json" || ctype2.hasSuffix("+json"), let u = parseUCPProfile(body2) {
        r.ucp = "found"
        r.handlers = u.handlers
        r.ap2 = u.ap2
        if u.shopify { r.shopify = true }
    } else {
        r.ucp = st2 == 404 || st2 == 410 ? "none" : "unclear"
    }
    return r
}

/// host[:port] of an origin, as HTTP's @authority.
func originAuthority(_ origin: String) -> String {
    guard let r = origin.range(of: "://") else { return origin }
    return String(origin[r.upperBound...])
}

/// `merchant-rules https://shop [--json]` (anyone, no unlock): what a merchant publishes for agents, read now.
/// `--file PATH` / `--ucp-file PATH` read saved copies instead.
func cmdMerchantRules(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, command: "merchant-rules")
    var r = MerchantRules()
    var origin = ""
    if a.last("file") != nil || a.last("ucp-file") != nil {
        guard a.positional.isEmpty else { throw usageError("usage: merchant-rules https://shop [--json] | merchant-rules --file AGENTS.md [--ucp-file PROFILE.json]") }
        if let f = a.last("file") {
            let p = parseAgentsMD(String(decoding: try readFileBytes(absolutePath(f), max: 1 << 20), as: UTF8.self))
            (r.agentsMD, r.forbids, r.buyerContinues, r.shopify) = ("found", p.forbids, p.buyerContinues, p.shopify)
        }
        if let f = a.last("ucp-file") {
            guard let u = parseUCPProfile(try readFileBytes(absolutePath(f), max: 1 << 20)) else { throw VaultError("\(f) isn't a JSON profile") }
            (r.ucp, r.handlers, r.ap2) = ("found", u.handlers, u.ap2)
            if u.shopify { r.shopify = true }
        }
    } else {
        guard a.positional.count == 1, let o = canonicalOrigin(a.positional[0]) else {
            throw usageError("usage: merchant-rules https://shop [--json]   (the site as https://host)")
        }
        origin = o
        r = merchantRules(o)
    }
    var out = r.record
    out["ok"] = true
    if !origin.isEmpty { out["origin"] = origin }
    out["card_fill"] = r.forbidden ? "forbidden" : (r.known ? "allowed" : "unclear")
    if r.forbidden || !r.known {
        out["next"] = "stop at the review step and hand payment to the user: metacomputer handoff --why payment --say \"…\""
    }
    if a.has("json") { oprint(jsonLine((cleanAll(out) as? [String: Any]) ?? [:])); return Exit.ok }
    oprint(clean("agent rules:    \(r.summary)", max: 300))
    oprint("card fill:      " + (r.forbidden ? "forbidden here: hand payment to the user (metacomputer handoff --why payment)"
                                 : (r.known ? "allowed (with your approval)" : "can't tell: an agent card refuses; hand payment to the user if unsure")))
    if r.buyerContinues { oprint("checkout:       the site asks agents to have the buyer continue on its checkout page") }
    oprint("UCP profile:    \(r.ucp)" + (r.handlers.isEmpty ? "" : ", payment handlers \(r.handlers.joined(separator: ", "))")
           + (r.ap2 ? "; mentions AP2" : ""))
    if r.shopify { oprint("Shopify:        yes: check out through your own Shop account (metacomputer shop) instead of a card fill") }
    return Exit.ok
}

// MARK: - Web Bot Auth

struct WebBotAuthKey: Codable {
    var key: String                     // base64: the Ed25519 private key (raw, 32 bytes)
    var agent: String                   // https://host[/path]: where the user hosts the key directory (the Signature-Agent)
    var on: Bool                        // signing on (off until `wba on`)
    var created: String
}

/// The JWK thumbprint (RFC 7638, SHA-256, base64url) of an Ed25519 public key: its keyid.
func ed25519Thumbprint(_ pub: [UInt8]) -> String {
    base64url(Array(SHA256.hash(data: Array("{\"crv\":\"Ed25519\",\"kty\":\"OKP\",\"x\":\"\(base64url(pub))\"}".utf8))))
}

/// An RFC 9421 signature base: each covered component on its line, then @signature-params.
func signatureBase(_ components: [(String, String)], params: String) -> String {
    components.map { "\"\($0.0)\": \($0.1)" }.joined(separator: "\n") + "\n\"@signature-params\": " + params
}

/// The three headers a request to `authority` carries: Signature-Agent, Signature-Input and Signature (tag web-bot-auth,
/// covering @authority and signature-agent, valid `ttl` seconds), or nil when the key is damaged.
func wbaSignatureHeaders(_ k: WebBotAuthKey, authority: String, now: Date = Date(), ttl: Int = 60, tag: String = "web-bot-auth") -> [String: String]? {
    guard let raw = Data(base64Encoded: k.key), let sk = try? Curve25519.Signing.PrivateKey(rawRepresentation: raw) else { return nil }
    let created = Int(now.timeIntervalSince1970)
    let kid = ed25519Thumbprint(Array(sk.publicKey.rawRepresentation))
    let agent = "\"\(k.agent)\""
    let covered = tag == "web-bot-auth" ? "(\"@authority\" \"signature-agent\")" : "(\"@authority\")"
    let params = covered + ";created=\(created);keyid=\"\(kid)\";alg=\"ed25519\";expires=\(created + ttl);nonce=\"\(base64url(randomBytes(32)))\";tag=\"\(tag)\""
    var comps = [("@authority", authority.lowercased())]
    if tag == "web-bot-auth" { comps.append(("signature-agent", agent)) }
    guard let sig = try? sk.signature(for: Array(signatureBase(comps, params: params).utf8)) else { return nil }
    var h = ["Signature-Input": "sig1=" + params, "Signature": "sig1=:" + Data(sig).base64EncodedString() + ":"]
    if tag == "web-bot-auth" { h["Signature-Agent"] = agent }
    return h
}

/// The key directory the user hosts at their agent URL's /.well-known/http-message-signatures-directory.
func wbaDirectory(_ k: WebBotAuthKey) -> [String: Any]? {
    guard let raw = Data(base64Encoded: k.key), let sk = try? Curve25519.Signing.PrivateKey(rawRepresentation: raw) else { return nil }
    return ["keys": [["kty": "OKP", "crv": "Ed25519", "x": base64url(Array(sk.publicKey.rawRepresentation))]]]
}

/// `wba init --agent https://… [--replace] | on | off` (the user, in their own terminal) and `wba status | directory [--headers] | sign URL`
/// (anyone, while the vault is unlocked).
func cmdWebBotAuth(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: wba init --agent https://your.site [--replace] | wba on | wba off   (your own terminal)\n"
        + "       wba status | wba directory [--headers] | wba sign https://site   (while unlocked)"
    guard let sub = argv.first else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), command: "wba")
    switch sub {
    case "init", "on", "off":
        guard a.positional.isEmpty else { throw usageError(usage) }
        var agent: String?
        if sub == "init" {
            guard let ag = a.last("agent"), let u = URL(string: ag), u.scheme == "https", let h = u.host, !h.isEmpty, u.query == nil, u.fragment == nil,
                  ag.count <= 200, clean(ag, max: 200) == ag, !ag.contains("\""), !ag.contains("\\") else {
                throw usageError("--agent: the https:// address of the site where you'll host the key directory, like https://agent.example.com")
            }
            agent = ag.hasSuffix("/") ? String(ag.dropLast()) : ag
        }
        try requireTTY("wba \(sub)")
        installPromptSignalHandlers()
        let (key, existing) = try unlockKey(paths)
        defer { key.wipeKey() }
        if sub == "init" {
            if existing.wba != nil && !a.has("replace") { throw VaultError("there's a Web Bot Auth key already: add --replace to make a new one (sites that registered the old one must get the new one)") }
            var sk = Curve25519.Signing.PrivateKey().rawRepresentation
            defer { wipe(&sk) }
            let k = WebBotAuthKey(key: sk.base64EncodedString(), agent: agent!, on: false, created: isoTime())
            try updateVault(paths, key) { p in p.wba = k }
            let pub = Array((try? Curve25519.Signing.PrivateKey(rawRepresentation: sk))?.publicKey.rawRepresentation ?? Data())
            audit(paths, ["op": "wba", "result": "ok", "change": existing.wba == nil ? "init" : "replace", "keyid": ed25519Thumbprint(pub), "agent": agent!],
                  macKey: vaultAuditKey(paths, key))
            oprint("Made a Web Bot Auth key (keyid \(ed25519Thumbprint(pub))). It's off until `metacomputer secrets wba on`.")
            oprint("Host its directory at \(agent!)/.well-known/http-message-signatures-directory (`metacomputer secrets wba directory --headers`")
            oprint("prints it while the vault is unlocked), then register that address with the sites that ask for it (docs/payments.md).")
            return Exit.ok
        }
        guard existing.wba != nil else { throw VaultError("no Web Bot Auth key yet: `metacomputer secrets wba init --agent https://your.site` first") }
        try updateVault(paths, key) { p in p.wba?.on = sub == "on" }
        audit(paths, ["op": "wba", "result": "ok", "change": sub], macKey: vaultAuditKey(paths, key))
        oprint(sub == "on" ? "Web Bot Auth is on: the vault's requests to merchants, and `wba sign`, carry a signature naming \(existing.wba!.agent)."
               : "Web Bot Auth is off: nothing is signed.")
        return Exit.ok
    case "status", "directory", "sign":
        var req: [String: Any] = ["op": "wba", "what": sub]
        if sub == "sign" {
            guard a.positional.count == 1, let u = URL(string: a.positional[0]), let s = u.scheme, ["https", "http"].contains(s), let h = u.host,
                  let o = canonicalOrigin("\(s)://\(h)" + (u.port.map { ":\($0)" } ?? "")) else { throw usageError("usage: wba sign https://site/path") }
            req["origin"] = o
        } else if a.last("days") != nil && !a.has("headers") {
            throw usageError("--days goes with --headers (how long the directory's signature lasts)")
        } else if a.has("headers") {
            req["headers"] = true
            let days = Int(a.last("days") ?? "30") ?? 0
            guard days >= 1, days <= 90 else { throw usageError("--days: 1-90") }
            req["days"] = days
        }
        let res: [String: Any]
        do { res = try request(paths, req, timeout: 15) } catch ClientError.locked {
            eprint("mcomp-vault: \(lockedHint(paths))")
            return Exit.locked
        }
        if a.has("json") || sub != "status" { oprint(jsonPretty((cleanAll(res, max: 2000) as? [String: Any]) ?? [:])); return exitFor(res) }
        oprint(clean("Web Bot Auth: \(res["state"] ?? "?")" + ((res["keyid"] as? String).map { ", keyid \($0)" } ?? "")
                     + ((res["agent"] as? String).map { ", directory at \($0)/.well-known/http-message-signatures-directory" } ?? ""), max: 400))
        return exitFor(res)
    default:
        throw usageError(usage)
    }
}
