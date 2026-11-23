// mcomp-vault: `audit-sink add|list|show|test|remove`.
// add, remove and show --key need your terminal and the passphrase; list, show and test
// go through the unlock process when the vault is unlocked.
import Darwin
import Foundation

func cmdAuditSink(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: audit-sink add NAME --kind webhook|otlp|syslog|ntfy --url URL|ask [--header 'Name: value|{ask}' ...] [--ca FILE] [--required] | list | show NAME [--key] | test NAME | remove NAME"
    guard let sub = argv.first else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), values: ["kind", "url", "header", "ca"], flags: ["required", "key", "json"],
                          repeatable: ["header"])
    let name = a.positional.first
    if sub != "list" { guard a.positional.count == 1, let n = name, validName(n) else { throw usageError(usage) } }
    switch sub {
    case "add": return try sinkAdd(paths, name!, a)
    case "remove": return try sinkRemove(paths, name!)
    case "list": return try sinkList(paths, nil, json: a.has("json"))
    case "show":
        if a.has("key") { return try sinkShowKey(paths, name!) }
        return try sinkList(paths, name!, json: a.has("json"))
    case "test": return try sinkTest(paths, name!)
    default: throw usageError(usage)
    }
}

func sinkAdd(_ paths: Paths, _ name: String, _ a: Args) throws -> Int32 {
    guard let kind = a.last("kind"), sinkKinds.contains(kind) else { throw usageError("--kind webhook, otlp, syslog or ntfy") }
    if a.has("required") && (kind == "syslog" || kind == "ntfy") {
        throw usageError("--required needs a sink that acknowledges each record: \(kind) can't (syslog over TLS confirms only that "
                         + "bytes left this Mac; ntfy keeps no record). Use a webhook or OTLP sink for --required, "
                         + "or a collector that accepts OTLP and forwards to syslog")
    }
    // `--url ask` and a header value of `{ask}` are typed with the echo off, so a secret in them
    // (a webhook path, an ntfy topic, a collector's token) stays off the command line.
    guard var url = a.last("url") else { throw usageError("--url: where to send the events (or `ask`)") }
    if url != "ask", let why = checkSinkURL(kind, url) { throw usageError("--url: \(why)") }
    var headers: [[String]] = []
    for h in a.all("header") {
        guard let (k, v) = parseHeaderLine(h) else { throw usageError("--header: 'Name: value' on one line, printable (value {ask} to type it)") }
        guard kind != "syslog" else { throw usageError("--header goes with webhook, otlp and ntfy sinks") }
        headers.append([k, v])
    }
    var ca: String?
    if let f = a.last("ca") {
        guard kind == "syslog" else { throw usageError("--ca goes with syslog sinks") }
        let pem = String(decoding: try readFileBytes(absolutePath(f), max: 1 << 20), as: UTF8.self)
        guard certificateFromPEM(pem) != nil else { throw VaultError("\(f) doesn't hold a PEM certificate") }
        ca = pem
    }
    try requireTTY("audit-sink add")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    guard existing.audit_sinks?[name] == nil else { throw VaultError("an audit sink named \(name) exists (remove it first)") }
    if url == "ask" {
        var raw = try readHidden("URL for the audit sink \(name): ")
        defer { wipe(&raw) }
        url = String(decoding: raw, as: UTF8.self).trimmingCharacters(in: .whitespaces)
        if let why = checkSinkURL(kind, url) { throw VaultError("that URL: \(why)") }
    }
    for i in headers.indices where headers[i][1] == "{ask}" {
        var raw = try readHidden("Value of the \(headers[i][0]) header: ")
        defer { wipe(&raw) }
        let v = String(decoding: raw, as: UTF8.self).trimmingCharacters(in: .whitespaces)
        guard let (_, ok) = parseHeaderLine("\(headers[i][0]): \(v)") else { throw VaultError("that header value: one line, printable") }
        headers[i][1] = ok
    }
    var hk = randomBytes(32)
    defer { wipe(&hk) }
    let hkB64 = Data(hk).base64EncodedString()
    try updateVault(paths, key) { p in
        var sinks = p.audit_sinks ?? [:]
        sinks[name] = AuditSink(kind: kind, url: url, headers: headers, required: a.has("required"),
                                hmac_key: Data(hk).base64EncodedString(), ca_pem: ca, created: isoTime(), start_seq: lastAuditEvent(paths.audit).0)
        p.audit_sinks = sinks
    }
    // The sink's own first event is this one.
    audit(paths, ["op": "audit_sink_add", "result": "ok", "name": name, "kind": kind, "url_host": URLComponents(string: url)?.host ?? "?",
                  "required": a.has("required")], macKey: vaultAuditKey(paths, key))
    oprint("Added the audit sink \(name) (\(kind), \(maskedURL(url)))" + (a.has("required") ? "; uses are refused while it can't be reached." : "."))
    if kind != "ntfy" {
        oprint("""

        Its signing key (shown once; `audit-sink show \(name) --key` shows it again, with the passphrase):
            \(hkB64)
        Give it to whatever receives the events: every event is signed with HMAC-SHA256 under the 32 bytes
        this base64 decodes to
        (webhook and otlp: the X-Metacomputer-Signature header; otlp records and syslog: their own signature).
        scripts/verify-audit.py checks a capture with it.

        """)
    }
    oprint("The unlock process sends events from now on, in order; events made while the vault is locked go at the next unlock.")
    return Exit.ok
}

func sinkRemove(_ paths: Paths, _ name: String) throws -> Int32 {
    try requireTTY("audit-sink remove")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    guard existing.audit_sinks?[name] != nil else { throw VaultError("no audit sink named \(name)") }
    try updateVault(paths, key) { p in p.audit_sinks?[name] = nil }
    for ext in ["state", "lock"] { unlink(sinkDir(paths) + "/\(name).\(ext)") }
    audit(paths, ["op": "audit_sink_remove", "result": "ok", "name": name], macKey: vaultAuditKey(paths, key))
    oprint("Removed the audit sink \(name).")
    return Exit.ok
}

func sinkShowKey(_ paths: Paths, _ name: String) throws -> Int32 {
    try requireTTY("audit-sink show --key")
    installPromptSignalHandlers()
    let (_, p) = try unlockKey(paths)
    guard let s = p.audit_sinks?[name], let k = Data(base64Encoded: s.hmac_key) else { throw VaultError("no audit sink named \(name)") }
    oprint("\(name) (\(s.kind), \(s.url)) signing key (base64): \(k.base64EncodedString())")
    return Exit.ok
}

func sinkList(_ paths: Paths, _ only: String?, json: Bool) throws -> Int32 {
    var list: [[String: Any]]
    var detailed = true
    if let res = try? request(paths, ["op": "sinks"], timeout: 15), res["ok"] as? Bool == true {
        list = res["sinks"] as? [[String: Any]] ?? []
    } else {
        // Locked: the names and delivery states only (the settings are inside the vault).
        detailed = false
        let files = (try? FileManager.default.contentsOfDirectory(atPath: sinkDir(paths))) ?? []
        let last = lastAuditEvent(paths.audit).0
        list = files.filter { $0.hasSuffix(".state") }.sorted().compactMap { f in
            let n = String(f.dropLast(6))
            guard let b = try? readFileBytes(sinkDir(paths) + "/" + f, max: 1 << 16), let s = try? JSONDecoder().decode(SinkState.self, from: Data(b)) else { return nil }
            var d: [String: Any] = ["name": n, "delivered": s.delivered, "backlog": max(0, last - s.delivered)]
            if let e = s.error { d["error"] = e }
            if let at = s.attempt { d["last_attempt"] = at }
            return d
        }
    }
    if let n = only { list = list.filter { ($0["name"] as? String) == n } }
    if json { oprint(jsonPretty(["sinks": list, "detailed": detailed])); return Exit.ok }
    if list.isEmpty {
        oprint(only.map { "No audit sink named \($0)" + (detailed ? "." : " has delivered anything yet (unlock to see its settings).") }
               ?? "No audit sinks" + (detailed ? " (audit-sink add ...)." : " have delivered anything yet (unlock to see their settings)."))
        return only == nil ? Exit.ok : Exit.error
    }
    for s in list {
        var line = "  \(s["name"] ?? "")"
        if detailed {
            line += "  \(s["kind"] ?? "") \(s["url"] ?? "")" + ((s["required"] as? Bool == true) ? " (required)" : "")
            if let h = s["headers"] as? [String], !h.isEmpty { line += " headers: \(h.joined(separator: ", "))" }
        }
        line += "  delivered through #\(s["delivered"] ?? 0), \(s["backlog"] ?? 0) waiting"
        if let e = s["error"] as? String { line += "; last error: \(e)" }
        oprint(clean(line, max: 600))
    }
    if !detailed { oprint("(the vault is locked: unlock it to see each sink's settings)") }
    return Exit.ok
}

func sinkTest(_ paths: Paths, _ name: String) throws -> Int32 {
    // Unlocked: the unlock process records the test (with the vault's MAC) and delivers.
    if let res = try? request(paths, ["op": "sink_flush", "name": name], timeout: 60) {
        if res["ok"] as? Bool == true {
            oprint("Sent: \(name) has every event through #\((res["delivered"] as? NSNumber)?.intValue ?? 0).")
            return Exit.ok
        }
        eprint("mcomp-vault: \(name): \(res["reason"] as? String ?? "can't deliver")")
        return Exit.error
    }
    // Locked: deliver from here, with the passphrase.
    try requireTTY("audit-sink test (while the vault is locked)")
    installPromptSignalHandlers()
    let (key, p0) = try unlockKey(paths)
    guard p0.audit_sinks?[name] != nil, let ak = vaultAuditKey(paths, key), let p = try? loadPayload(paths, key),
          let s = p.audit_sinks?[name] else { throw VaultError("no audit sink named \(name)") }
    audit(paths, ["op": "audit_sink_test", "result": "ok", "name": name], macKey: ak)
    let d = deliverSink(paths, name, s, auditKey: ak, from: nil, known: [:], legacyThrough: p.legacyThrough)
    if !d.stateTrusted {
        oprint("\(name): its saved delivery state didn't check out (changed outside the vault?); sent again from its first event.")
    }
    for t in d.tampered { eprint("mcomp-vault: AUDIT LOG CHANGED: \(clean(t.e["reason"] as? String ?? "a record failed its checks", max: 300))") }
    if let why = d.problem { throw VaultError("\(name): \(why)", code: Exit.error) }
    oprint("Sent: \(name) has every event through #\(d.delivered).")
    return Exit.ok
}
