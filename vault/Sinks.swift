// mcomp-vault: audit sinks, places off this Mac that receive every audit event.
//
// The local audit log (audit.jsonl, a hash chain) is the spool: each sink keeps its own
// place in it (sinks/NAME.state, 0600) and receives every later event, in order. The
// unlock process delivers (and `audit-sink test`); each delivery holds sinks/NAME.lock.
// Before sending, the chain is checked: a changed local log stops delivery.
//
// Kinds (any number of each):
//   webhook  one CloudEvents 1.0 event per audit event (structured mode), POSTed as
//            application/cloudevents+json; X-Metacomputer-Signature: sha256=HMAC(body)
//   otlp     OpenTelemetry logs over OTLP/HTTP (JSON): POST URL/v1/logs, one LogRecord
//            per event (body = the event's JSON, which metacomputer.signature signs);
//            the batch body is signed in X-Metacomputer-Signature too
//   syslog   RFC 5424 messages over TLS (RFC 5425, octet counting) to syslog+tls://host:port;
//            [mcomp@32473 seq= hash= prev= sig=] with sig = HMAC(MSG); MSG = the event's JSON
//   ntfy     one line per event for a phone (no signature; seq shows gaps)
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
    var sink: String?      // the sink's `created` stamp: a state file left by an earlier sink of the same name doesn't apply
}

func sinkDir(_ paths: Paths) -> String { paths.dir + "/sinks" }

func loadSinkState(_ paths: Paths, _ name: String, _ sink: AuditSink) -> SinkState {
    if let b = try? readFileBytes(sinkDir(paths) + "/\(name).state", max: 1 << 16),
       let s = try? JSONDecoder().decode(SinkState.self, from: Data(b)), s.sink == "\(sink.created)|\(sink.start_seq)" {
        return s
    }
    return SinkState(delivered: sink.start_seq, error: nil, attempt: nil, failures: 0, sink: "\(sink.created)|\(sink.start_seq)")
}

func saveSinkState(_ paths: Paths, _ name: String, _ s: SinkState) {
    try? ensurePrivateDir(sinkDir(paths))
    if let d = try? JSONEncoder().encode(s) { try? writeFileAtomic(sinkDir(paths) + "/\(name).state", Array(d)) }
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
    if sem.wait(timeout: .now() + timeout + 2) == .timedOut { session.invalidateAndCancel(); return "no answer in \(Int(timeout)) s" }
    if let e = box.error { return oneLine(e.localizedDescription, max: 160) }
    guard let h = box.response as? HTTPURLResponse else { return "no HTTP answer" }
    return (200..<300).contains(h.statusCode) ? nil : "HTTP \(h.statusCode)"
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
/// `subject` the VM, `type` io.metavert.metacomputer.vault.<op>, `data` the event itself.
func cloudEventBody(_ raw: String, _ e: [String: Any]) -> String {
    let op = (e["op"] as? String) ?? "event"
    var s = "{\"data\":" + raw + ",\"datacontenttype\":\"application/json\",\"id\":"
        + canonicalString((e["id"] as? String) ?? (e["hash"] as? String) ?? "")
        + ",\"source\":" + canonicalString("metacomputer/" + hostLabel()) + ",\"specversion\":\"1.0\""
    if let vm = e["vm"] as? String { s += ",\"subject\":" + canonicalString(vm) }
    return s + ",\"time\":" + canonicalString((e["ts"] as? String) ?? "") + ",\"type\":"
        + canonicalString("io.metavert.metacomputer.vault." + op) + "}"
}

/// OTLP/HTTP JSON logs, as the events stream sends them (family "vault", type = op), plus the
/// chain (hash, prev) and each record's own signature.
func otlpBody(_ events: [(String, [String: Any])], key: [UInt8]) -> String {
    func s(_ k: String, _ v: String) -> [String: Any] { ["key": k, "value": ["stringValue": v]] }
    func i(_ k: String, _ v: Int) -> [String: Any] { ["key": k, "value": ["intValue": String(v)]] }
    let observed = epochNanos(auditTime())
    let records: [[String: Any]] = events.map { raw, e in
        var attrs: [[String: Any]] = [s("metacomputer.family", "vault")]
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
        attrs.append(s("metacomputer.signature", "sha256=" + hmacSHA256Hex(key: key, data: Array(raw.utf8))))
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
func syslogMessage(_ raw: String, _ e: [String: Any], key: [UInt8]) -> String {
    let pri = 1 * 8 + severityOf(e).syslog
    let op = String(((e["op"] as? String) ?? "event").unicodeScalars.filter { $0.value > 32 && $0.value < 127 }.prefix(32).map(Character.init))
    let sig = "sha256=" + hmacSHA256Hex(key: key, data: Array(raw.utf8))
    var sd = "[\(syslogSDID) seq=\"\((e["seq"] as? NSNumber)?.intValue ?? 0)\" family=\"vault\""
    if let vm = e["vm"] as? String { sd += " vm=\"\(sdEscape(vm))\"" }
    sd += " id=\"\(sdEscape((e["id"] as? String) ?? ""))\" hash=\"\(sdEscape((e["hash"] as? String) ?? ""))\""
        + " prev=\"\(sdEscape((e["prev"] as? String) ?? ""))\" sig=\"\(sig)\"]"
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
func ntfyRequest(_ sinkURL: String, _ e: [String: Any]) -> (URL, String)? {
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
    let body: [String: Any] = ["topic": topic, "title": clean(title, max: 200), "message": ntfySummary(e),
                               "priority": sev.name == "info" ? 3 : 4, "tags": tags]
    return (root, canonicalJSON(body))
}

// MARK: - delivery

/// The audit events after `after`, in order, checked against the chain. On a broken chain,
/// the error names the first bad seq and nothing past it is returned.
func pendingEvents(_ paths: Paths, after: Int, limit: Int = 200) -> ([(String, [String: Any])], String?) {
    guard let bytes = try? readFileBytes(paths.audit, max: 1 << 30) else { return ([], nil) }
    var prevHash: String?
    var out: [(String, [String: Any])] = []
    for l in bytes.split(separator: 0x0A, omittingEmptySubsequences: true) {
        guard let e = parseJSONObject(Array(l)), let seq = (e["seq"] as? NSNumber)?.intValue else { continue }
        if seq <= after {
            prevHash = seq == after ? e["hash"] as? String : nil
            continue
        }
        let raw = String(decoding: l, as: UTF8.self)
        let expectSeq = (out.last.flatMap { ($0.1["seq"] as? NSNumber)?.intValue }).map { $0 + 1 } ?? after + 1
        guard (e["hash"] as? String) == eventHash(e), seq == expectSeq,
              prevHash == nil || (e["prev"] as? String) == prevHash else {
            return (out, "the local audit log was changed at seq \(seq); not sending past it (look at audit.jsonl)")
        }
        out.append((raw, e))
        prevHash = e["hash"] as? String
        if out.count >= limit { break }
    }
    return (out, nil)
}

/// Send what's pending for one sink; returns nil when nothing is left pending, else why.
func deliverSink(_ paths: Paths, _ name: String, _ sink: AuditSink) -> String? {
    try? ensurePrivateDir(sinkDir(paths))
    let res: String?? = try? withFileLock(sinkDir(paths) + "/\(name).lock") { () -> String? in
        var state = loadSinkState(paths, name, sink)
        guard let key = Data(base64Encoded: sink.hmac_key).map(Array.init) else { return "the sink's key is damaged" }
        var problem: String?
        while problem == nil {
            let (events, chainError) = pendingEvents(paths, after: state.delivered, limit: sink.kind == "otlp" ? 50 : 100)
            if events.isEmpty { problem = chainError; break }
            var sentThrough = state.delivered
            switch sink.kind {
            case "otlp":
                var url = sink.url.hasSuffix("/") ? String(sink.url.dropLast()) : sink.url
                if !url.hasSuffix("/v1/logs") { url += "/v1/logs" }
                let body = otlpBody(events, key: key)
                problem = URL(string: url).map { httpPost($0, headers: sinkHeaders(sink, body, contentType: "application/json"), body: Data(body.utf8)) }
                    ?? "bad URL"
                if problem == nil { sentThrough = (events.last?.1["seq"] as? NSNumber)?.intValue ?? sentThrough }
            case "syslog":
                guard let u = URLComponents(string: sink.url), let host = u.host else { problem = "bad URL"; break }
                let frames = events.map { raw, e -> Data in
                    let m = Array(syslogMessage(raw, e, key: key).utf8)
                    return Data(Array("\(m.count) ".utf8) + m)
                }
                problem = syslogSend(host: host, port: UInt16(u.port ?? 6514), caPEM: sink.ca_pem, frames: frames)
                if problem == nil { sentThrough = (events.last?.1["seq"] as? NSNumber)?.intValue ?? sentThrough }
            default:
                for (raw, e) in events {
                    var target: URL?
                    let body: String
                    if sink.kind == "ntfy" {
                        guard let (root, b) = ntfyRequest(sink.url, e) else { problem = "bad URL (https://server/topic)"; break }
                        target = root
                        body = b
                    } else {
                        target = URL(string: sink.url)
                        body = cloudEventBody(raw, e)
                    }
                    let headers = sinkHeaders(sink, body, contentType: sink.kind == "ntfy" ? "application/json" : "application/cloudevents+json; charset=utf-8")
                    problem = target.map { httpPost($0, headers: headers, body: Data(body.utf8)) } ?? "bad URL"
                    if problem != nil { break }
                    sentThrough = (e["seq"] as? NSNumber)?.intValue ?? sentThrough
                }
            }
            if sentThrough > state.delivered { state.delivered = sentThrough; state.failures = 0 }
            if problem == nil && chainError != nil { problem = chainError }
        }
        state.error = problem
        state.attempt = isoTime()
        if problem != nil { state.failures += 1 }
        saveSinkState(paths, name, state)
        return problem
    }
    return res ?? "can't lock the sink's state"
}

func sinkHeaders(_ sink: AuditSink, _ body: String, contentType: String) -> [(String, String)] {
    let key = Array(Data(base64Encoded: sink.hmac_key) ?? Data())
    return [("Content-Type", contentType), ("User-Agent", "metacomputer-vault/1"),
            ("X-Metacomputer-Signature", "sha256=" + hmacSHA256Hex(key: key, data: Array(body.utf8)))]
        + sink.headers.compactMap { $0.count == 2 ? ($0[0], $0[1]) : nil }
}

/// The events not yet delivered to a sink.
func sinkBacklog(_ paths: Paths, _ name: String, _ sink: AuditSink) -> Int {
    max(0, lastAuditEvent(paths.audit).0 - loadSinkState(paths, name, sink).delivered)
}
