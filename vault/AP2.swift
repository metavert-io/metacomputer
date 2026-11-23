// mcomp-vault: AP2 mandates, the buyer's side (SECURITY.md, "Paying for things"; docs/payments.md).
//
// The Agent Payments Protocol (google-agentic-commerce/AP2, v0.2) has a merchant give the shopping agent a signed
// Checkout JWT; a "Trusted Surface" shows it to the user, gets their approval, and signs two closed mandates with the
// user's key: a Checkout Mandate (`mandate.checkout.1`: the merchant's checkout JWT and its hash; v0.1 called this the
// cart mandate) and a Payment Mandate (`mandate.payment.1`: payee, amount, instrument, bound to that checkout by its
// hash). Here the vault on the user's Mac is that surface: after a purchase is approved (or allowed by an agent card's
// policy and caps), it signs both, as RFC 9901 SD-JWTs (ES256, the AP2 SDK's root-token form), with a P-256 key that
// never leaves the vault. No Metavert server takes part. Few merchants accept AP2 yet; nobody trusts this key until the
// user registers it with one who does.
import CryptoKit
import Darwin
import Foundation

/// The AP2 user key, from the vault (nil: none made yet, or damaged).
func ap2SigningKey(_ p: Payload) -> P256.Signing.PrivateKey? {
    guard let s = p.ap2_key, let d = Data(base64Encoded: s) else { return nil }
    return try? P256.Signing.PrivateKey(rawRepresentation: d)
}

/// The public key as a JWK, with its RFC 7638 thumbprint as `kid`.
func ap2PublicJWK(_ k: P256.Signing.PublicKey) -> [String: Any] {
    let raw = Array(k.x963Representation)          // 0x04 || x || y
    let x = base64url(Array(raw[1..<33])), y = base64url(Array(raw[33..<65]))
    let kid = base64url(Array(SHA256.hash(data: Array("{\"crv\":\"P-256\",\"kty\":\"EC\",\"x\":\"\(x)\",\"y\":\"\(y)\"}".utf8))))
    return ["kty": "EC", "crv": "P-256", "x": x, "y": y, "kid": kid, "alg": "ES256", "use": "sig"]
}

/// Compact JSON (keys sorted), as a disclosure or a JWT part carries it.
func compactJSON(_ v: Any) -> [UInt8] {
    (try? JSONSerialization.data(withJSONObject: v, options: [.sortedKeys, .withoutEscapingSlashes])).map(Array.init) ?? []
}

func base64urlDecode(_ s: String) -> [UInt8]? {
    guard matches(s, "^[A-Za-z0-9_-]*$") else { return nil }
    var t = s.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    while t.count % 4 != 0 { t += "=" }
    return Data(base64Encoded: t).map(Array.init)
}

/// A disclosure (RFC 9901 section 4.2): base64url of the JSON array, and its digest (sha-256 over that text, base64url).
func sdDisclosure(_ parts: [Any]) -> (String, String) {
    let d = base64url(compactJSON(parts))
    return (d, base64url(Array(SHA256.hash(data: Array(d.utf8)))))
}

/// A root SD-JWT as AP2's SDK makes one (`MandateClient.create`): `delegate_payload` is a one-element array whose
/// element is itself a disclosure, and `sdFields` of the mandate are disclosures inside it.
func ap2RootSDJWT(_ mandate: [String: Any], sdFields: [String], key: P256.Signing.PrivateKey, kid: String) -> String? {
    var claims = mandate
    var discs: [String] = []
    var digests: [String] = []
    for f in sdFields {
        guard let v = claims[f] else { continue }
        claims[f] = nil
        let (d, h) = sdDisclosure([base64url(randomBytes(16)), f, v])
        discs.append(d)
        digests.append(h)
    }
    if !digests.isEmpty { claims["_sd"] = digests.sorted() }
    let (elem, elemDigest) = sdDisclosure([base64url(randomBytes(16)), claims])
    let header: [String: Any] = ["alg": "ES256", "kid": kid]
    let payload: [String: Any] = ["delegate_payload": [["...": elemDigest]], "_sd_alg": "sha-256"]
    let input = base64url(compactJSON(header)) + "." + base64url(compactJSON(payload))
    guard let sig = try? key.signature(for: Array(input.utf8)) else { return nil }
    return input + "." + base64url(Array(sig.rawRepresentation)) + "~" + ([elem] + discs).joined(separator: "~") + "~"
}

/// What a merchant's checkout JWT says (untrusted: its signature isn't checked here), held to fixed shapes. Signing needs
/// all of it: the merchant (id, name and website), the checkout's own id, exactly one payable total in whole minor units
/// and one supported currency, with every form of the total in the payload agreeing.
struct CheckoutClaim {
    var minor: Int
    var currency: String
    var merchantID: String
    var merchantName: String
    var website: String         // the merchant's site, as an origin
    var checkoutID: String
    var alg: String
}

/// The JWS algorithms a merchant's checkout may name; `none` and anything else are refused.
let checkoutJWTAlgs: Set<String> = ["ES256", "ES384", "ES512", "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "EdDSA"]

/// Whether every JSON object in `bytes`, at any depth, names each key once (a key given twice is read differently by
/// different parsers, so a checkout with one isn't signed); nil when the bytes aren't a JSON object or array.
func jsonKeysUnique(_ bytes: [UInt8]) -> Bool? {
    final class Frame { var keys: Set<String> = []; let object: Bool; var expectKey: Bool; init(_ o: Bool) { object = o; expectKey = o } }
    var stack: [Frame] = []
    var i = 0, sawRoot = false
    let n = bytes.count
    while i < n {
        let c = bytes[i]
        if c == 0x22 {                                  // a string: a key where an object expects one
            var j = i + 1
            var esc = false
            while j < n {
                if esc { esc = false } else if bytes[j] == 0x5C { esc = true } else if bytes[j] == 0x22 { break }
                j += 1
            }
            guard j < n else { return nil }
            if let f = stack.last, f.object, f.expectKey {
                guard let k = (try? JSONSerialization.jsonObject(with: Data(bytes[i...j]), options: [.fragmentsAllowed])) as? String else { return nil }
                guard f.keys.insert(k).inserted else { return false }
                f.expectKey = false
            }
            i = j + 1
            continue
        }
        switch c {
        case 0x7B, 0x5B:                                // { [
            if stack.isEmpty { guard !sawRoot else { return nil }; sawRoot = true }
            guard stack.count < 64 else { return nil }
            stack.append(Frame(c == 0x7B))
        case 0x7D, 0x5D:                                // } ]
            guard let f = stack.popLast(), f.object == (c == 0x7D) else { return nil }
        case 0x2C:                                      // ,
            if let f = stack.last, f.object { f.expectKey = true }
        default: break
        }
        i += 1
    }
    return stack.isEmpty && sawRoot ? true : nil
}

/// `audience`: the vault's AP2 key id; a checkout that names an audience (aud) must name it. `now`: for exp and nbf.
func parseCheckoutJWT(_ jwt: String, audience: String? = nil, now: Date = Date()) -> (CheckoutClaim?, String?) {
    let parts = jwt.split(separator: ".", omittingEmptySubsequences: false)
    guard jwt.count <= 32 << 10, parts.count == 3, parts.allSatisfy({ !$0.isEmpty }), let hb = base64urlDecode(String(parts[0])),
          let pb = base64urlDecode(String(parts[1])), let sig = base64urlDecode(String(parts[2])), !sig.isEmpty,
          let header = (try? JSONSerialization.jsonObject(with: Data(hb))) as? [String: Any],
          let obj = (try? JSONSerialization.jsonObject(with: Data(pb))) as? [String: Any] else {
        return (nil, "the checkout JWT isn't a compact JWT with a JSON header and payload (at most 32 KiB)")
    }
    guard let alg = header["alg"] as? String, checkoutJWTAlgs.contains(alg) else {
        return (nil, "the checkout JWT's algorithm isn't one a merchant signs with (\"none\" and unknown ones are refused)")
    }
    guard jsonKeysUnique(pb) == true, jsonKeysUnique(hb) == true else {
        return (nil, "the checkout JWT names a field twice (at any depth), or isn't plain JSON")
    }
    // When it says when it's valid, or for whom, that holds: an expired checkout, or one addressed to another buyer, isn't signed.
    let t = now.timeIntervalSince1970
    for (k, cmp) in [("exp", { (v: Double) in v > t }), ("nbf", { (v: Double) in v <= t + 60 })] {
        if let v = obj[k] {
            guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), cmp(n.doubleValue) else {
                return (nil, k == "exp" ? "the checkout has expired (exp), or its exp isn't a time" : "the checkout isn't valid yet (nbf)")
            }
        }
    }
    if let av = obj["aud"] {
        let auds = (av as? String).map { [$0] } ?? (av as? [Any])?.compactMap { $0 as? String }
        guard let list = auds, !list.isEmpty, let want = audience, list.contains(want) else {
            return (nil, "the checkout is addressed (aud) to someone other than this vault's AP2 key: refused")
        }
    }
    func intAmount(_ v: Any?) -> Int?? {                // nil: absent; .some(nil): present but not a whole positive amount
        guard let v else { return nil }
        guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue == n.doubleValue.rounded(),
              n.doubleValue > 0, n.doubleValue <= 1_000_000_000 else { return .some(nil) }
        return .some(n.intValue)
    }
    var currencies: [String] = []
    if let c = obj["currency"] { guard let cs = c as? String else { return (nil, "the checkout's currency isn't text") }; currencies.append(cs.uppercased()) }
    var amounts: [Int] = []
    // UCP's Checkout: totals [{type: "total", amount: minor units}], exactly one of type total.
    if let tv = obj["totals"] {
        guard let totals = tv as? [Any] else { return (nil, "the checkout's totals aren't a list") }
        let payable = totals.compactMap { $0 as? [String: Any] }.filter { $0["type"] as? String == "total" }
        guard payable.count == 1, case .some(.some(let a)) = intAmount(payable[0]["amount"]) else {
            return (nil, "the checkout needs exactly one total (type total) in whole minor units")
        }
        amounts.append(a)
        // The payable total's own currency, when it names one, counts with the others: they must all agree.
        if let c = payable[0]["currency"] { guard let cs = c as? String else { return (nil, "the checkout's currency isn't text") }; currencies.append(cs.uppercased()) }
    }
    if let tv = obj["total"] {
        guard let t = tv as? [String: Any], case .some(.some(let a)) = intAmount(t["amount"]) else { return (nil, "the checkout's total isn't a whole amount") }
        amounts.append(a)
        if let c = t["currency"] { guard let cs = c as? String else { return (nil, "the checkout's currency isn't text") }; currencies.append(cs.uppercased()) }
    }
    guard let cur = currencies.first, currencies.allSatisfy({ $0 == cur }) else {
        return (nil, currencies.isEmpty ? "the checkout names no currency" : "the checkout names two currencies")
    }
    guard knownCurrencies.contains(cur) else { return (nil, "the checkout's currency (\(clean(cur, max: 8))) isn't one this supports") }
    // AP2's own example: total_price in major units; it must come out to whole minor units.
    if let tp = obj["total_price"] {
        guard let n = tp as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return (nil, "the checkout's total_price isn't a number") }
        var scale = 1.0
        for _ in 0..<currencyDigits(cur) { scale *= 10 }
        let v = n.doubleValue * scale
        guard v > 0, v <= 1e9, abs(v - v.rounded()) < 1e-6 else { return (nil, "the checkout's total_price isn't whole minor units of \(cur)") }
        amounts.append(Int(v.rounded()))
    }
    guard let m = amounts.first, amounts.allSatisfy({ $0 == m }) else {
        return (nil, amounts.isEmpty ? "the checkout has no total (UCP totals with type total, a total, or total_price)" : "the checkout's totals disagree")
    }
    guard let mer = obj["merchant"] as? [String: Any], let mid = mer["id"] as? String, !mid.isEmpty, mid.count <= 120, clean(mid, max: 120) == mid,
          let mname = mer["name"] as? String, !mname.isEmpty, mname.count <= 120, clean(mname, max: 120) == mname,
          let w = mer["website"] as? String, let site = canonicalOriginOfURL(w) else {
        return (nil, "the checkout must name its merchant: id, name and website")
    }
    guard let cid = (obj["id"] ?? obj["order_id"]) as? String, !cid.isEmpty, cid.count <= 200, clean(cid, max: 200) == cid else {
        return (nil, "the checkout has no id of its own (id or order_id)")
    }
    return (CheckoutClaim(minor: m, currency: cur, merchantID: mid, merchantName: mname, website: site, checkoutID: cid, alg: alg), nil)
}

/// The origin of a URL ("https://shop.example/about" -> "https://shop.example"), nil when it has none.
func canonicalOriginOfURL(_ s: String) -> String? {
    guard s.count <= 2048, let u = URL(string: s), let sc = u.scheme, let h = u.host, u.user == nil, u.password == nil else { return nil }
    return canonicalOrigin("\(sc)://\(h)" + (u.port.map { ":\($0)" } ?? ""))
}

/// The two closed mandates for an approved checkout: (checkout mandate, payment mandate, checkout hash, kid).
func ap2ClosedMandates(checkoutJWT: String, claim: CheckoutClaim, payeeID: String, payeeName: String, website: String,
                       instrumentID: String, instrumentLabel: String, key: P256.Signing.PrivateKey, ttl: Int, now: Date = Date(),
                       approvedBy: String = "user")
    -> (String, String, String, String)? {
    let kid = ap2PublicJWK(key.publicKey)["kid"] as! String
    let hash = base64url(Array(SHA256.hash(data: Array(checkoutJWT.utf8))))
    let iat = Int(now.timeIntervalSince1970)
    let checkout: [String: Any] = ["vct": "mandate.checkout.1", "checkout_jwt": checkoutJWT, "checkout_hash": hash, "iat": iat, "exp": iat + ttl]
    let payment: [String: Any] = ["vct": "mandate.payment.1", "transaction_id": hash,
                                  "payee": ["id": payeeID, "name": payeeName, "website": website],
                                  "payment_amount": ["amount": claim.minor, "currency": claim.currency],
                                  "payment_instrument": ["id": instrumentID, "type": "card", "description": instrumentLabel],
                                  // Risk signals from the trusted surface (AP2's risk_data): the merchant's signature on the
                                  // checkout wasn't verified here, and who approved it.
                                  "risk_data": ["checkout_signature_verified": false, "approved_by": approvedBy],
                                  "iat": iat, "exp": iat + ttl]
    guard let cm = ap2RootSDJWT(checkout, sdFields: ["checkout_jwt"], key: key, kid: kid),
          let pm = ap2RootSDJWT(payment, sdFields: [], key: key, kid: kid) else { return nil }
    return (cm, pm, hash, kid)
}

/// `ap2 init [--replace]` (the user, in their own terminal) | `ap2 key [--json]` | `ap2 sign CARD --vm VM --origin https://shop
/// --checkout-jwt FILE|- [--ttl 15m]` (while unlocked; an ask card asks, an agent card refuses: see opAP2Sign).
func cmdAP2(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: ap2 init [--replace] (your own terminal) | ap2 key [--json] | ap2 sign CARD --vm VM --origin https://shop "
        + "--checkout-jwt FILE|- [--ttl 15m]"
    guard let sub = argv.first else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), command: "ap2")
    switch sub {
    case "init":
        guard a.positional.isEmpty else { throw usageError(usage) }
        try requireTTY("ap2 init")
        installPromptSignalHandlers()
        let (key, existing) = try unlockKey(paths)
        defer { key.wipeKey() }
        if existing.ap2_key != nil && !a.has("replace") {
            throw VaultError("there's an AP2 key already: add --replace to make a new one (anyone who registered the old one needs the new one)")
        }
        let sk = P256.Signing.PrivateKey()
        try updateVault(paths, key) { p in p.ap2_key = sk.rawRepresentation.base64EncodedString() }
        let jwk = ap2PublicJWK(sk.publicKey)
        audit(paths, ["op": "ap2", "result": "ok", "change": existing.ap2_key == nil ? "init" : "replace", "kid": jwk["kid"] ?? ""],
              macKey: vaultAuditKey(paths, key))
        oprint("Made an AP2 signing key (kid \(jwk["kid"] ?? "")). Its public half, for a merchant or credential provider that accepts AP2:")
        oprint(jsonLine(jwk))
        oprint("Few merchants accept AP2 yet; docs/payments.md says what was tested and how a purchase signs one.")
        return Exit.ok
    case "key":
        let res: [String: Any]
        do { res = try request(paths, ["op": "ap2_key"], timeout: 15) } catch ClientError.locked {
            eprint("mcomp-vault: \(lockedHint(paths))")
            return Exit.locked
        }
        oprint(a.has("json") ? jsonLine(res) : jsonPretty(res))
        return exitFor(res)
    case "sign":
        guard a.positional.count == 1, validName(a.positional[0]), let vm = a.last("vm"), validName(vm), let origin = a.last("origin"),
              canonicalOrigin(origin) == origin, let src = a.last("checkout-jwt") else { throw usageError(usage) }
        let raw = src == "-" ? Array(FileHandle.standardInput.readData(ofLength: 64 << 10)) : try readFileBytes(absolutePath(src), max: 64 << 10)
        let jwt = String(decoding: raw, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard matches(jwt, "^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$") else { throw usageError("--checkout-jwt: the merchant's checkout JWT (compact form), in a file or on stdin (-)") }
        var req: [String: Any] = ["op": "ap2_sign", "name": a.positional[0], "vm": vm, "origin": origin, "checkout_jwt": jwt]
        guard let ttl = parseDuration(a.last("ttl") ?? "15m"), ttl >= 60, ttl <= 3600 else { throw usageError("--ttl: 1m to 1h (the mandates' lifetime)") }
        req["ttl_s"] = ttl
        try useOptions(a, &req)
        let res: [String: Any]
        do { res = try request(paths, req, timeout: 300, cancellable: true) } catch ClientError.locked {
            return lockedUse(paths, "ap2_sign", ["name": a.positional[0], "vm": vm, "origin": origin])
        }
        // The mandates are meant for the merchant (no card data in them): printed whole.
        var out: [String: Any] = [:]
        for k in ["ok", "name", "label", "origin", "total", "approval", "checkout_hash", "kid", "checkout_signature_verified", "checkout_mandate",
                  "payment_mandate", "expires", "request_id", "reason"] { if let v = res[k] { out[k] = v } }
        if out["ok"] == nil { out["ok"] = false }
        oprint(jsonLine((cleanAll(out, max: 70_000) as? [String: Any]) ?? ["ok": false]))
        return exitFor(res)
    default:
        throw usageError(usage)
    }
}
