// mcomp-vault: audit sinks, places off this Mac that receive every audit event.
//
// The local audit log (audit.jsonl, a hash chain) is the spool: each sink receives every
// event after the one that added it, in order. The unlock process delivers (and
// `audit-sink test` while locked); each delivery holds sinks/NAME.lock.
//
// Nothing a process of this user can edit is taken on trust:
// - each record made by a process holding the vault key carries a creation MAC under the
//   vault's audit key (audit() in Util.swift); the dispatcher checks it, and a record whose MAC
//   fails, or that the unlock process wrote with another hash, or that only the vault
//   writes but has no MAC, is never signed for a sink: an `audit_tamper` notice goes in
//   its place. Records without a MAC that a locked client may write (refusals while
//   locked) are sent marked unverified;
// - the place each sink got to is kept in memory by the unlock process; the saved copy
//   (sinks/NAME.state) carries a MAC under the audit key and is ignored when it fails;
// - a required sink must acknowledge the very `begin` record of a use (in memory) before
//   the use goes ahead.
//
// Kinds (any number of each), in the event stream's wrappings:
//   webhook  one CloudEvents 1.0 event per audit event (structured mode);
//            X-Metacomputer-Signature: sha256=HMAC(body); mcompverified: the creation MAC checked out
//   otlp     OpenTelemetry logs over OTLP/HTTP (JSON): POST URL/v1/logs, one LogRecord per
//            event (body = the event's JSON, which metacomputer.signature signs)
//   syslog   RFC 5424 over TLS (RFC 5425 octet counting) to syslog+tls://host:port;
//            [metacomputer@32473 ... verified= sig=] with sig = HMAC(MSG)
//   ntfy     one message per event for a phone (no signature)
// Each sink has its own HMAC key (shown once when it's added).
import Foundation
import Network
import Security

let sinkKinds = ["webhook", "otlp", "syslog", "ntfy"]
let syslogSDID = "metacomputer@32473"   // 32473: IANA's example enterprise number (RFC 5612), a documented placeholder

struct SinkState: Codable {
    var delivered: Int
    var error: String?
    var attempt: String?
    var failures: Int
    var sink: String?      // the sink's stamp: a state file left by an earlier sink of the same name doesn't apply
    var mac: String? = nil // over name, stamp and delivered, under the vault's audit key
}

func sinkDir(_ paths: Paths) -> String { paths.dir + "/sinks" }

func sinkStamp(_ sink: AuditSink) -> String { "\(sink.created)|\(sink.start_seq)" }

func sinkStateMAC(_ key: [UInt8], _ name: String, _ s: SinkState) -> String {
    hmacSHA256Hex(key: key, data: Array("mcomp-vault|sink-state|\(name)|\(s.sink ?? "")|\(s.delivered)".utf8))
}

/// A sink's saved state, and whether its place can be trusted. Without the audit key (display
/// only) it's returned as found. A state whose MAC fails is replaced by a fresh one: delivery
/// starts again from the sink's first event (receivers drop what they already have by `id`).
func loadSinkState(_ paths: Paths, _ name: String, _ sink: AuditSink, key: [UInt8]? = nil) -> (SinkState, Bool) {
    let fresh = SinkState(delivered: sink.start_seq, error: nil, attempt: nil, failures: 0, sink: sinkStamp(sink))
    guard let b = try? readFileBytes(sinkDir(paths) + "/\(name).state", max: 1 << 16),
          let s = try? JSONDecoder().decode(SinkState.self, from: Data(b)), s.sink == sinkStamp(sink) else { return (fresh, true) }
    guard let k = key else { return (s, true) }
    if let m = s.mac, m == sinkStateMAC(k, name, s) { return (s, true) }
    var f = fresh
    f.error = "its saved delivery state didn't check out; sending again from its first event"
    return (f, false)
}

func saveSinkState(_ paths: Paths, _ name: String, _ s: SinkState, key: [UInt8]) {
    var st = s
    st.mac = sinkStateMAC(key, name, st)
    try? ensurePrivateDir(sinkDir(paths))
    if let d = try? JSONEncoder().encode(st) { try? writeFileAtomic(sinkDir(paths) + "/\(name).state", Array(d)) }
}

/// nil when the URL suits the kind, else why not.
func checkSinkURL(_ kind: String, _ url: String) -> String? {
    guard clean(url, max: 2048) == url, let u = URLComponents(string: url), let host = u.host, !host.isEmpty,
          u.user == nil, u.password == nil else { return "not a plain URL" }
    let loopback = host == "127.0.0.1" || host == "localhost"
    switch kind {
    case "syslog":
        guard u.scheme == "syslog+tls" else { return "syslog sinks use syslog+tls://host:port (TLS only; no plain TCP or UDP)" }
    default:
        guard u.scheme == "https" || (u.scheme == "http" && loopback) else { return "use https:// (http only for 127.0.0.1 or localhost, for testing)" }
        if kind == "ntfy", u.path.split(separator: "/").isEmpty { return "an ntfy URL names its topic: https://ntfy.sh/<topic>" }
    }
    return nil
}

/// A sink's URL without its path and query, which can hold a secret (webhook paths, ntfy topics):
/// what `audit-sink list` shows; `audit-sink show NAME --key` (passphrase) shows all of it.
func maskedURL(_ s: String) -> String {
    guard var u = URLComponents(string: s) else { return "?" }
    let more = !(u.path.isEmpty || u.path == "/") || u.query != nil
    u.path = ""
    u.query = nil
    u.fragment = nil
    return (u.string ?? "?") + (more ? "/..." : "")
}

func validHeaderName(_ s: String) -> Bool { matches(s, "^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$") }

/// "Name: value" from --header; nil when it isn't a safe header line.
func parseHeaderLine(_ s: String) -> (String, String)? {
    guard let c = s.firstIndex(of: ":") else { return nil }
    let name = String(s[..<c]).trimmingCharacters(in: .whitespaces)
    let value = String(s[s.index(after: c)...]).trimmingCharacters(in: .whitespaces)
    guard validHeaderName(name), !value.isEmpty, value.count <= 4096, clean(value, max: 4096) == value else { return nil }
    return (name, value)
}

// MARK: - HTTP (no redirects, no cookies, no cache)

final class NoRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

final class HTTPBox: @unchecked Sendable {
    var data: Data?
    var response: URLResponse?
    var error: Error?
}

func plainSessionConfig(timeout: Double) -> URLSessionConfiguration {
    let cfg = URLSessionConfiguration.ephemeral
    cfg.timeoutIntervalForRequest = timeout
    cfg.timeoutIntervalForResource = timeout * 4
    cfg.httpCookieStorage = nil
    cfg.httpShouldSetCookies = false
    cfg.urlCache = nil
    cfg.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
    cfg.connectionProxyDictionary = [:]   // straight to the host: no system or PAC proxy sees requests or keys
    return cfg
}

/// POST; returns nil on a 2xx answer, else why not.
func httpPost(_ url: URL, headers: [(String, String)], body: Data, timeout: Double = 8) -> String? {
    httpPostAnswer(url, headers: headers, body: body, timeout: timeout).0
}

/// POST; (nil, the answer's body) on a 2xx answer, else (why not, nil).
func httpPostAnswer(_ url: URL, headers: [(String, String)], body: Data, timeout: Double = 8) -> (String?, Data?) {
    let session = URLSession(configuration: plainSessionConfig(timeout: timeout), delegate: NoRedirects(), delegateQueue: nil)
    defer { session.finishTasksAndInvalidate() }
    var req = URLRequest(url: url)
    req.httpMethod = "POST"
    req.httpBody = body
    for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
    let box = HTTPBox()
    let sem = DispatchSemaphore(value: 0)
    session.dataTask(with: req) { d, r, e in
        box.data = d
        box.response = r
        box.error = e
        sem.signal()
    }.resume()
    if sem.wait(timeout: .now() + timeout + 2) == .timedOut { session.invalidateAndCancel(); return ("no answer in \(Int(timeout)) s", nil) }
    if let e = box.error { return (oneLine(e.localizedDescription, max: 160), nil) }
    guard let h = box.response as? HTTPURLResponse else { return ("no HTTP answer", nil) }
    return (200..<300).contains(h.statusCode) ? (nil, box.data ?? Data()) : ("HTTP \(h.statusCode)", nil)
}

/// An OTLP export answer that accepted only part of the batch (partialSuccess with rejected
/// records) is a failure: those records weren't stored. nil when every record was accepted.
func otlpRejection(_ body: Data?) -> String? {
    guard let b = body, !b.isEmpty, let o = try? JSONSerialization.jsonObject(with: b) as? [String: Any],
          let ps = (o["partialSuccess"] ?? o["partial_success"]) as? [String: Any] else { return nil }
    let r = ps["rejectedLogRecords"] ?? ps["rejected_log_records"]
    let n = (r as? NSNumber)?.int64Value ?? (r as? String).flatMap { Int64($0) } ?? 0
    guard n > 0 else { return nil }
    let msg = clean((ps["errorMessage"] ?? ps["error_message"]) as? String ?? "", max: 160)
    return "the collector rejected \(n) record(s)" + (msg.isEmpty ? "" : ": \(msg)")
}

/// The signature a single record carries (OTLP's metacomputer.signature, syslog's sig=): over
/// its verification status and its line, so neither can change without the other.
func recordSignature(_ key: [UInt8], _ raw: String, verified: Bool) -> String {
    "sha256=" + hmacSHA256Hex(key: key, data: Array(("verified=\(verified)\n" + raw).utf8))
}

// MARK: - syslog over TLS

final class Holder<T>: @unchecked Sendable {
    var value: T
    init(_ v: T) { value = v }
}

func certificateFromPEM(_ pem: String) -> SecCertificate? {
    guard let a = pem.range(of: "-----BEGIN CERTIFICATE-----"), let z = pem.range(of: "-----END CERTIFICATE-----"), a.upperBound < z.lowerBound else { return nil }
    let b64 = pem[a.upperBound..<z.lowerBound].filter { !$0.isWhitespace }
    guard let der = Data(base64Encoded: String(b64)) else { return nil }
    return SecCertificateCreateWithData(nil, der as CFData)
}

/// Send octet-counted frames over one TLS connection; nil when all were sent.
func syslogSend(host: String, port: UInt16, caPEM: String?, frames: [Data], timeout: Double = 8) -> String? {
    let queue = DispatchQueue(label: "mcomp.syslog")
    let tls = NWProtocolTLS.Options()
    if let pem = caPEM {
        guard let ca = certificateFromPEM(pem) else { return "the sink's CA certificate can't be read" }
        let anchor = Holder(ca)
        sec_protocol_options_set_verify_block(tls.securityProtocolOptions, { _, trustRef, complete in
            let trust = sec_trust_copy_ref(trustRef).takeRetainedValue()
            SecTrustSetAnchorCertificates(trust, [anchor.value] as CFArray)
            SecTrustSetAnchorCertificatesOnly(trust, true)
            var err: CFError?
            complete(SecTrustEvaluateWithError(trust, &err))
        }, queue)
    }
    guard let p = NWEndpoint.Port(rawValue: port) else { return "bad port" }
    let conn = NWConnection(host: NWEndpoint.Host(host), port: p, using: NWParameters(tls: tls))
    let state = Holder<String?>("pending")
    let ready = DispatchSemaphore(value: 0)
    conn.stateUpdateHandler = { s in
        switch s {
        case .ready: state.value = nil; ready.signal()
        case .failed(let e): state.value = "TLS connection failed: \(e)"; ready.signal()
        case .waiting(let e): state.value = "can't connect: \(e)"; ready.signal()
        default: break
        }
    }
    conn.start(queue: queue)
    defer { conn.cancel() }
    if ready.wait(timeout: .now() + timeout) == .timedOut { return "no TLS connection in \(Int(timeout)) s" }
    if let e = state.value { return oneLine(e, max: 200) }
    for f in frames {
        let sent = DispatchSemaphore(value: 0)
        let err = Holder<String?>(nil)
        conn.send(content: f, completion: .contentProcessed { e in
            if let e = e { err.value = "\(e)" }
            sent.signal()
        })
        if sent.wait(timeout: .now() + timeout) == .timedOut { return "sending timed out" }
        if let e = err.value { return oneLine(e, max: 200) }
    }
    return nil
}

// MARK: - formats

/// An event's severity, as the events stream names it: info, warn (refused, locked) or error.
func severityOf(_ e: [String: Any]) -> (name: String, text: String, otlp: Int, syslog: Int) {
    switch e["result"] as? String {
    case "denied", "locked": return ("warn", "WARN", 13, 4)
    case "error": return ("error", "ERROR", 17, 3)
    default: return ("info", "INFO", 9, 6)
    }
}

/// This Mac's short host name for the sinks (gethostname: no DNS lookup, unlike ProcessInfo.hostName).
let hostLabelValue: String = {
    var buf = [CChar](repeating: 0, count: 256)
    guard gethostname(&buf, buf.count - 1) == 0 else { return "-" }
    let h = String(decoding: buf.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    let short = h.split(separator: ".").first.map(String.init) ?? h
    let ascii = short.unicodeScalars.filter { $0.value > 32 && $0.value < 127 }
    return ascii.isEmpty ? "-" : String(String(String.UnicodeScalarView(ascii)).prefix(255))
}()

func hostLabel() -> String { hostLabelValue }

/// "2026-11-17T18:04:05.123456Z" (or an older event's offset form) as Unix nanoseconds.
func epochNanos(_ iso: String) -> String {
    if iso.hasSuffix("Z"), let dot = iso.firstIndex(of: ".") {
        var t = tm()
        let base = String(iso[..<dot])
        let frac = String(iso[iso.index(after: dot)..<iso.index(before: iso.endIndex)])
        guard frac.allSatisfy(\.isNumber), frac.count <= 9,
              base.withCString({ strptime($0, "%Y-%m-%dT%H:%M:%S", &t) }) != nil else { return "0" }
        let secs = Int64(timegm(&t))
        let nanos = Int64(frac + String(repeating: "0", count: 9 - frac.count)) ?? 0
        return String(secs * 1_000_000_000 + nanos)
    }
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    guard let d = f.date(from: iso) else { return "0" }
    return String(Int64(d.timeIntervalSince1970) * 1_000_000_000)
}

/// CloudEvents 1.0, structured mode, as the events stream sends them: `id` the event's UUID
/// (at-least-once delivery: receivers drop duplicates by it), `source` metacomputer/<host>,
/// `subject` the VM, `type` io.metavert.metacomputer.vault.<op>, `data` the event itself;
/// the extension `mcompverified` says whether its creation MAC checked out.
func cloudEventBody(_ ev: OutEvent) -> String {
    let e = ev.e
    let op = (e["op"] as? String) ?? "event"
    var s = "{\"data\":" + ev.raw + ",\"datacontenttype\":\"application/json\",\"id\":"
        + canonicalString((e["id"] as? String) ?? (e["hash"] as? String) ?? "")
        + ",\"mcompverified\":" + (ev.verified ? "true" : "false")
        + ",\"source\":" + canonicalString("metacomputer/" + hostLabel()) + ",\"specversion\":\"1.0\""
    if let vm = e["vm"] as? String { s += ",\"subject\":" + canonicalString(vm) }
    return s + ",\"time\":" + canonicalString((e["ts"] as? String) ?? "") + ",\"type\":"
        + canonicalString("io.metavert.metacomputer.vault." + op) + "}"
}

/// OTLP/HTTP JSON logs, as the events stream sends them (family "vault", type = op), plus the
/// chain (hash, prev) and each record's own signature.
func otlpBody(_ events: [OutEvent], key: [UInt8]) -> String {
    func s(_ k: String, _ v: String) -> [String: Any] { ["key": k, "value": ["stringValue": v]] }
    func i(_ k: String, _ v: Int) -> [String: Any] { ["key": k, "value": ["intValue": String(v)]] }
    let observed = epochNanos(auditTime())
    let records: [[String: Any]] = events.map { ev in
        let raw = ev.raw, e = ev.e
        var attrs: [[String: Any]] = [s("metacomputer.family", "vault"),
                                      ["key": "metacomputer.verified", "value": ["boolValue": NSNumber(value: ev.verified)]]]
        for (k, name) in [("op", "metacomputer.type"), ("vm", "metacomputer.vm"), ("id", "metacomputer.id"), ("name", "metacomputer.secret"),
                          ("field", "metacomputer.field"), ("origin", "metacomputer.origin"), ("result", "metacomputer.outcome"),
                          ("hash", "metacomputer.hash"), ("prev", "metacomputer.prev")] {
            if let v = e[k] as? String { attrs.append(s(name, v)) }
        }
        if let n = (e["seq"] as? NSNumber)?.intValue { attrs.append(i("metacomputer.seq", n)) }
        if let r = e["requester"] as? [String: Any] {
            if let p = r["project"] as? String { attrs.append(s("metacomputer.project", p)) }
            if let p = (r["claude_pid"] as? NSNumber)?.intValue { attrs.append(i("metacomputer.requester.pid", p)) }
        }
        attrs.append(s("metacomputer.signature", recordSignature(key, raw, verified: ev.verified)))
        let sev = severityOf(e)
        return ["timeUnixNano": epochNanos((e["ts"] as? String) ?? ""), "observedTimeUnixNano": observed, "severityNumber": sev.otlp,
                "severityText": sev.text, "body": ["stringValue": raw], "attributes": attrs]
    }
    let resource: [String: Any] = ["attributes": [s("service.name", "metacomputer-vault"), s("service.version", buildSourceHash),
                                                  s("host.name", hostLabel())]]
    let body: [String: Any] = ["resourceLogs": [["resource": resource,
                                                 "scopeLogs": [["scope": ["name": "metacomputer.vault", "version": "1"], "logRecords": records]]]]]
    return canonicalJSON(body)
}

func sdEscape(_ s: String) -> String {
    s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"").replacingOccurrences(of: "]", with: "\\]")
}

/// One RFC 5424 message, as the events stream sends them (facility user, PROCID "-",
/// SD-ID metacomputer@32473 with seq, family, vm, id), plus hash, prev and the signature
/// of MSG; without the octet count.
func syslogMessage(_ ev: OutEvent, key: [UInt8]) -> String {
    let raw = ev.raw, e = ev.e
    let pri = 1 * 8 + severityOf(e).syslog
    let op = String(((e["op"] as? String) ?? "event").unicodeScalars.filter { $0.value > 32 && $0.value < 127 }.prefix(32).map(Character.init))
    let sig = recordSignature(key, raw, verified: ev.verified)
    var sd = "[\(syslogSDID) seq=\"\((e["seq"] as? NSNumber)?.intValue ?? 0)\" family=\"vault\""
    if let vm = e["vm"] as? String { sd += " vm=\"\(sdEscape(vm))\"" }
    sd += " id=\"\(sdEscape((e["id"] as? String) ?? ""))\" hash=\"\(sdEscape((e["hash"] as? String) ?? ""))\""
        + " prev=\"\(sdEscape((e["prev"] as? String) ?? ""))\" verified=\"\(ev.verified)\" sig=\"\(sig)\"]"
    let ts = (e["ts"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "-"
    return "<\(pri)>1 \(ts) \(hostLabel()) metacomputer-vault - \(op.isEmpty ? "-" : op) \(sd) \(raw)"
}

/// One line for a phone: seq, what, outcome and where. Never a value.
func ntfySummary(_ e: [String: Any]) -> String {
    var s = "#\((e["seq"] as? NSNumber)?.intValue ?? 0) \((e["op"] as? String) ?? "event")"
    for k in ["name", "field", "vm"] { if let v = e[k] as? String { s += " \(k)=\(v)" } }
    s += " -> \((e["result"] as? String) ?? "ok")"
    if let o = e["origin"] as? String { s += " \(o)" }
    if let r = e["requester"] as? [String: Any], let p = r["project"] as? String { s += " [Claude in \(tildePath(p))]" }
    if let why = e["reason"] as? String { s += ": \(why)" }
    return clean(s, max: 400)
}

/// ntfy's JSON publishing, as the events stream does it: POST to the server's root with the
/// topic in the body; priority 4 for warn and error, 3 for info.
func ntfyRequest(_ sinkURL: String, _ ev: OutEvent) -> (URL, String)? {
    let e = ev.e
    guard var u = URLComponents(string: sinkURL) else { return nil }
    let parts = u.path.split(separator: "/").map(String.init)
    guard let topic = parts.last, !topic.isEmpty else { return nil }
    u.path = parts.count > 1 ? "/" + parts.dropLast().joined(separator: "/") + "/" : "/"
    u.query = nil
    u.fragment = nil
    guard let root = u.url else { return nil }
    let sev = severityOf(e)
    let title = "\((e["vm"] as? String) ?? hostLabel()) - vault: \(((e["op"] as? String) ?? "event").replacingOccurrences(of: "_", with: " "))"
    let tags = ["lock"] + (sev.name == "warn" ? ["warning"] : sev.name == "error" ? ["rotating_light"] : [])
    let body: [String: Any] = ["topic": topic, "title": clean(title, max: 200), "message": (ev.verified ? "" : "[unverified] ") + ntfySummary(e),
                               "priority": sev.name == "info" ? 3 : 4, "tags": tags]
    return (root, canonicalJSON(body))
}

// MARK: - delivery

/// One event as a sink gets it: the record's own line, whether its creation MAC checked out,
/// or, for a record that failed the checks, an `audit_tamper` notice in its place.
struct OutEvent {
    let seq: Int
    let raw: String
    let e: [String: Any]
    let verified: Bool
    let tamper: Bool
}

/// Uses a locked client may record without the vault key (it has none): refusals while locked.
func clientMayWrite(_ e: [String: Any]) -> Bool {
    (e["result"] as? String) == "locked" && ["fill", "session_save", "session_load", "token"].contains(e["op"] as? String ?? "")
}

/// A notice that stands in for a record that failed its checks. Its id is fixed by the seq and
/// the reason, so a repeated delivery is dropped like any other.
func tamperEvent(_ seq: Int, _ reason: String, seen: String?) -> OutEvent {
    let h = sha256Hex(Array("mcomp-tamper|\(seq)|\(reason)|\(seen ?? "")".utf8))
    let c = Array(h)
    let id = String(c[0..<8]) + "-" + String(c[8..<12]) + "-5" + String(c[13..<16]) + "-a" + String(c[17..<20]) + "-" + String(c[20..<32])
    var e: [String: Any] = ["op": "audit_tamper", "result": "error", "seq": seq, "reason": reason, "id": id, "ts": auditTime()]
    if let s = seen { e["hash_seen"] = s }
    return OutEvent(seq: seq, raw: canonicalJSON(e), e: e, verified: true, tamper: true)
}

/// The events after `after`, in order, each checked: its hash, its place in the chain, the
/// hash the unlock process saw when it wrote it (`known`), and its creation MAC.
func pendingEvents(_ paths: Paths, after: Int, limit: Int, auditKey: [UInt8], known: [Int: String], legacyThrough: Int = 0) -> [OutEvent] {
    guard let bytes = try? readFileBytes(paths.audit, max: 1 << 30) else { return [] }
    var prevHash: String?
    var last = after
    var linkOK = true
    var out: [OutEvent] = []
    for l in bytes.split(separator: 0x0A, omittingEmptySubsequences: true) {
        guard let e = parseJSONObject(Array(l)), let seq = (e["seq"] as? NSNumber)?.intValue else { continue }
        if seq <= after {
            if seq == after { prevHash = e["hash"] as? String }
            continue
        }
        if seq <= last {
            out.append(tamperEvent(seq, "a second record with seq \(seq), or one out of order, in the local log; not sent", seen: e["hash"] as? String))
            linkOK = false
            continue
        }
        if seq > last + 1 {
            out.append(tamperEvent(last + 1, "records \(last + 1)..\(seq - 1) are missing from the local log", seen: nil))
            linkOK = false
        }
        let h = e["hash"] as? String
        let raw = String(decoding: l, as: UTF8.self)
        var problem: String?
        var verified = false
        if h != eventHash(e) {
            problem = "record \(seq) doesn't match its own hash (changed after it was written); not sent"
        } else if let k = known[seq], k != h {
            problem = "record \(seq) isn't the one the unlock process wrote; not sent"
        } else if e.keys.contains("mac") {
            // Present: it must be a string that checks out. Only an absent MAC means "unverified".
            if let m = e["mac"] as? String, m == auditRecordMAC(auditKey, e) { verified = true } else {
                problem = "record \(seq) fails its creation MAC; not sent"
            }
        } else if seq > legacyThrough && !clientMayWrite(e) {
            problem = "record \(seq) (\(clean(e["op"] as? String ?? "?", max: 40))) has no creation MAC, which only a locked client's refusals may lack; not sent"
        }
        if problem == nil, linkOK, let p = prevHash, (e["prev"] as? String) != p {
            out.append(tamperEvent(seq, "record \(seq) doesn't link to record \(seq - 1) (the chain was changed)", seen: h))
        }
        if let why = problem {
            out.append(tamperEvent(seq, why, seen: h))
        } else {
            out.append(OutEvent(seq: seq, raw: raw, e: e, verified: verified, tamper: false))
        }
        prevHash = h
        linkOK = true
        last = seq
        if out.count >= limit { break }
    }
    return out
}

struct Delivery {
    var problem: String?
    var delivered: Int          // the place reached: every event up to here was sent and acknowledged
    var genuine: [(seq: Int, id: String, hash: String)] = []   // records sent as themselves (not tamper notices)
    var tampered: [OutEvent] = []
    var stateTrusted = true     // false: the saved place failed its MAC (delivery started again from the first event)
    var incomplete = false      // stopped at the deadline with more to send
}

/// Send what's pending for one sink, starting after `from` (the unlock process's own place in
/// memory) or, without it, after the sink's saved place (MAC-checked).
func deliverSink(_ paths: Paths, _ name: String, _ sink: AuditSink, auditKey: [UInt8], from: Int?, known: [Int: String],
                 legacyThrough: Int = 0, deadline: Date = .distantFuture) -> Delivery {
    try? ensurePrivateDir(sinkDir(paths))
    let res: Delivery? = try? withFileLock(sinkDir(paths) + "/\(name).lock") { () -> Delivery in
        let loaded = loadSinkState(paths, name, sink, key: auditKey)
        var state = loaded.0
        let trusted = loaded.1
        if let f = from { state.delivered = f }
        guard let key = Data(base64Encoded: sink.hmac_key).map(Array.init) else {
            return Delivery(problem: "the sink's key is damaged", delivered: state.delivered)
        }
        var d = Delivery(problem: nil, delivered: state.delivered)
        d.stateTrusted = from != nil || trusted
        while d.problem == nil {
            if Date() >= deadline { d.incomplete = true; break }   // the rest goes on the next round
            let events = pendingEvents(paths, after: d.delivered, limit: sink.kind == "otlp" ? 50 : 100, auditKey: auditKey, known: known,
                                       legacyThrough: legacyThrough)
            if events.isEmpty { break }
            var sentThrough = d.delivered
            var sent: [OutEvent] = []
            switch sink.kind {
            case "otlp":
                var url = sink.url.hasSuffix("/") ? String(sink.url.dropLast()) : sink.url
                if !url.hasSuffix("/v1/logs") { url += "/v1/logs" }
                let body = otlpBody(events, key: key)
                if let u = URL(string: url) {
                    let (why, answer) = httpPostAnswer(u, headers: sinkHeaders(sink, body, contentType: "application/json"), body: Data(body.utf8))
                    d.problem = why ?? otlpRejection(answer)
                } else {
                    d.problem = "bad URL"
                }
                if d.problem == nil { sent = events }
            case "syslog":
                guard let u = URLComponents(string: sink.url), let host = u.host else { d.problem = "bad URL"; break }
                let frames = events.map { ev -> Data in
                    let m = Array(syslogMessage(ev, key: key).utf8)
                    return Data(Array("\(m.count) ".utf8) + m)
                }
                d.problem = syslogSend(host: host, port: UInt16(u.port ?? 6514), caPEM: sink.ca_pem, frames: frames)
                if d.problem == nil { sent = events }
            default:
                for ev in events {
                    var target: URL?
                    let body: String
                    if sink.kind == "ntfy" {
                        guard let (root, b) = ntfyRequest(sink.url, ev) else { d.problem = "bad URL (https://server/topic)"; break }
                        target = root
                        body = b
                    } else {
                        target = URL(string: sink.url)
                        body = cloudEventBody(ev)
                    }
                    let headers = sinkHeaders(sink, body, contentType: sink.kind == "ntfy" ? "application/json" : "application/cloudevents+json; charset=utf-8")
                    d.problem = target.map { httpPost($0, headers: headers, body: Data(body.utf8)) } ?? "bad URL"
                    if d.problem != nil { break }
                    sent.append(ev)
                }
            }
            for ev in sent {
                sentThrough = max(sentThrough, ev.seq)
                if ev.tamper { d.tampered.append(ev) } else {
                    d.genuine.append((ev.seq, (ev.e["id"] as? String) ?? "", (ev.e["hash"] as? String) ?? ""))
                }
            }
            if sentThrough > d.delivered { d.delivered = sentThrough; state.failures = 0 }
            if sent.isEmpty && d.problem == nil { break }
        }
        state.delivered = d.delivered
        state.error = d.problem
        state.attempt = isoTime()
        if d.problem != nil { state.failures += 1 }
        saveSinkState(paths, name, state, key: auditKey)
        return d
    }
    return res ?? Delivery(problem: "can't lock the sink's state", delivered: from ?? sink.start_seq)
}

func sinkHeaders(_ sink: AuditSink, _ body: String, contentType: String) -> [(String, String)] {
    let key = Array(Data(base64Encoded: sink.hmac_key) ?? Data())
    return [("Content-Type", contentType), ("User-Agent", "metacomputer-vault/1"),
            ("X-Metacomputer-Signature", "sha256=" + hmacSHA256Hex(key: key, data: Array(body.utf8)))]
        + sink.headers.compactMap { $0.count == 2 ? ($0[0], $0[1]) : nil }
}

/// The events not yet delivered to a sink, from its saved place (for display).
func sinkBacklog(_ paths: Paths, _ name: String, _ sink: AuditSink, from: Int? = nil) -> Int {
    max(0, lastAuditEvent(paths.audit).0 - (from ?? loadSinkState(paths, name, sink).0.delivered))
}
