// mcomp-vault: shared helpers (paths, errors, arguments, JSON, audit log).
import Darwin
import Foundation
import os

// Exit codes. fill/session: 0 ok, 2 denied, 3 locked, 4 error.
enum Exit {
    static let ok: Int32 = 0
    static let failed: Int32 = 1
    static let denied: Int32 = 2
    static let locked: Int32 = 3
    static let error: Int32 = 4
    static let unconfirmed: Int32 = 5   // a request was sent and no answer came: the unlock process may still be on it (`settle`)
    static let expired: Int32 = 6       // a one-time code request ran out of time (or was already used) before it was filled
    static let needsApproval: Int32 = 7 // a one-time code request waits for `secrets approve ID`: nothing asked, nothing waits here
    static let usage: Int32 = 64
}

struct VaultError: Error {
    let message: String
    let code: Int32
    init(_ message: String, code: Int32 = Exit.failed) {
        self.message = message
        self.code = code
    }
}

func usageError(_ message: String) -> VaultError { VaultError(message, code: Exit.usage) }

// Plain write(2): a terminal that has gone away (EIO) must not end the process
// before the vault locks itself properly.
func eprint(_ s: String) { _ = writeAll(2, Array((s + "\n").utf8)) }

func oprint(_ s: String) { _ = writeAll(1, Array((s + "\n").utf8)) }

func eprintRaw(_ s: String) { _ = writeAll(2, Array(s.utf8)) }

// MARK: - build information

enum Build {
    #if MCOMP_VAULT_TESTING
    static let isTest = true
    #else
    static let isTest = false
    #endif
    static var kind: String { isTest ? "test" : "release" }
}

// MARK: - paths

/// The vault's folder: MCOMP_VAULT_DIR, or ~/Library/Application Support/metacomputer.
struct Paths: Sendable {
    let dir: String
    var vault: String { dir + "/vault.json" }
    var socket: String { dir + "/vault.sock" }
    var lock: String { dir + "/vault.lock" }
    var unlockLock: String { dir + "/unlock.lock" }
    var audit: String { dir + "/audit.jsonl" }
    var sessions: String { dir + "/sessions" }

    static func current() -> Paths {
        if let d = ProcessInfo.processInfo.environment["MCOMP_VAULT_DIR"], !d.isEmpty {
            return Paths(dir: absolutePath(d))
        }
        return Paths(dir: homeDirectory() + "/Library/Application Support/metacomputer")
    }
}

func homeDirectory() -> String {
    if let pw = getpwuid(getuid()), let d = pw.pointee.pw_dir {
        return String(cString: d)
    }
    return NSHomeDirectory()
}

func absolutePath(_ p: String) -> String {
    if p.hasPrefix("/") { return p }
    if p == "~" { return homeDirectory() }
    if p.hasPrefix("~/") { return homeDirectory() + String(p.dropFirst(1)) }
    return FileManager.default.currentDirectoryPath + "/" + p
}

/// A private folder of ours: made 0700 when missing; refused when it's a link or someone else's.
func ensurePrivateDir(_ path: String) throws {
    var st = stat()
    if lstat(path, &st) != 0 {
        if mkdir(path, 0o700) != 0 && errno != EEXIST {
            throw VaultError("cannot create \(path): \(String(cString: strerror(errno)))")
        }
        guard lstat(path, &st) == 0 else { throw VaultError("cannot read \(path)") }
    }
    guard (st.st_mode & S_IFMT) == S_IFDIR else { throw VaultError("\(path) is not a plain folder (a link?); refusing") }
    guard st.st_uid == getuid() else { throw VaultError("\(path) belongs to another user; refusing") }
    if st.st_mode & 0o077 != 0 { chmod(path, 0o700) }
}

/// A NUL-terminated C buffer as a String.
func stringFromCBuffer(_ buf: [CChar]) -> String {
    String(decoding: buf.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
}

func fileExists(_ path: String) -> Bool {
    var st = stat()
    return lstat(path, &st) == 0
}

/// The per-user temporary folder (not $TMPDIR, which the environment can change).
func userTempDir() -> String {
    let n = confstr(_CS_DARWIN_USER_TEMP_DIR, nil, 0)
    if n > 0 {
        var buf = [CChar](repeating: 0, count: n)
        if confstr(_CS_DARWIN_USER_TEMP_DIR, &buf, n) > 0 {
            var s = stringFromCBuffer(buf)
            while s.hasSuffix("/") && s.count > 1 { s.removeLast() }
            return s
        }
    }
    return "/tmp"
}

// MARK: - file I/O

/// The last `count` bytes of a file (all of it when smaller); nil when it can't be read.
func readTail(_ path: String, _ count: Int) -> [UInt8]? {
    let fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
    guard fd >= 0 else { return nil }
    defer { close(fd) }
    var st = stat()
    guard fstat(fd, &st) == 0 else { return nil }
    let size = Int(st.st_size)
    let n = min(size, count)
    var buf = [UInt8](repeating: 0, count: n)
    var got = 0
    while got < n {
        let r = buf.withUnsafeMutableBytes { pread(fd, $0.baseAddress! + got, n - got, off_t(size - n + got)) }
        if r < 0 { if errno == EINTR { continue }; return nil }
        if r == 0 { break }
        got += r
    }
    return Array(buf[0..<got])
}

func readFileBytes(_ path: String, max: Int = 1 << 31) throws -> [UInt8] {
    let fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
    guard fd >= 0 else { throw VaultError("cannot read \(path): \(String(cString: strerror(errno)))") }
    defer { close(fd) }
    var out = [UInt8]()
    var chunk = [UInt8](repeating: 0, count: 1 << 16)
    while true {
        let n = read(fd, &chunk, chunk.count)
        if n < 0 {
            if errno == EINTR { continue }
            throw VaultError("cannot read \(path)")
        }
        if n == 0 { break }
        out.append(contentsOf: chunk[0..<n])
        if out.count > max { throw VaultError("\(path) is too large") }
    }
    wipe(&chunk)
    return out
}

func writeAll(_ fd: Int32, _ bytes: [UInt8]) -> Bool {
    var off = 0
    while off < bytes.count {
        let n = bytes.withUnsafeBytes { p in write(fd, p.baseAddress! + off, bytes.count - off) }
        if n < 0 {
            if errno == EINTR { continue }
            return false
        }
        off += n
    }
    return true
}

/// writeAll that gives up at an absolute deadline, however slowly the other side reads: each write waits at most a
/// second (and never past the deadline), and one that times out is tried again only while time is left.
func writeAllBefore(_ fd: Int32, _ bytes: [UInt8], _ deadline: Date) -> Bool {
    var off = 0
    while off < bytes.count {
        let left = deadline.timeIntervalSinceNow
        guard left > 0 else { return false }
        let wait = min(left, 1)
        var tv = timeval(tv_sec: Int(wait), tv_usec: Int32((wait - wait.rounded(.down)) * 1_000_000))
        if tv.tv_sec == 0 && tv.tv_usec < 1000 { tv.tv_usec = 1000 }
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        let n = bytes.withUnsafeBytes { p in write(fd, p.baseAddress! + off, bytes.count - off) }
        if n < 0 {
            if errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK { continue }
            return false
        }
        off += n
    }
    return true
}

/// Write a file atomically (temporary file in the same folder, fsync, rename), mode 0600.
/// Write a file so that, after a crash or power loss, it holds either the old contents or
/// the new, and once this returns the new contents and the rename are on disk: the data goes
/// to a temp file flushed with F_FULLFSYNC (plain fsync on macOS may leave it in the drive's
/// cache), is renamed over the file, and the folder is flushed the same way.
func writeFileAtomic(_ path: String, _ bytes: [UInt8]) throws {
    let tmp = path + ".tmp-\(getpid())"
    unlink(tmp)
    let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw VaultError("cannot write \(tmp): \(String(cString: strerror(errno)))") }
    let ok = writeAll(fd, bytes) && fullSync(fd)
    close(fd)
    guard ok, rename(tmp, path) == 0 else {
        unlink(tmp)
        throw VaultError("cannot write \(path)")
    }
    guard syncFolder(of: path) else { throw VaultError("cannot flush the folder of \(path)") }
    testTrace("durable " + path)
}

/// Test builds only: append a line to $MCOMP_VAULT_TEST_TRACE (the order of durable writes).
func testTrace(_ line: String) {
    #if MCOMP_VAULT_TESTING
    guard let p = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_TRACE"], !p.isEmpty else { return }
    let fd = open(p, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0o600)
    guard fd >= 0 else { return }
    _ = writeAll(fd, Array((line + "\n").utf8))
    close(fd)
    #endif
}

/// Flush a file descriptor to stable storage (F_FULLFSYNC, falling back to fsync).
func fullSync(_ fd: Int32) -> Bool {
    if fcntl(fd, F_FULLFSYNC) == 0 { return true }
    return fsync(fd) == 0
}

/// Flush a path's folder, so a rename into it is durable.
func syncFolder(of path: String) -> Bool {
    let dir = (path as NSString).deletingLastPathComponent
    let fd = open(dir.isEmpty ? "." : dir, O_RDONLY | O_CLOEXEC | O_DIRECTORY)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    return fullSync(fd)
}

/// Run body while holding an flock on path (made 0600 if missing).
func withFileLock<T>(_ path: String, _ body: () throws -> T) throws -> T {
    let fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw VaultError("cannot open the lock \(path)") }
    defer { close(fd) }
    while flock(fd, LOCK_EX) != 0 {
        if errno != EINTR { throw VaultError("cannot lock \(path)") }
    }
    defer { flock(fd, LOCK_UN) }
    return try body()
}

// MARK: - time

/// Now in UTC, RFC 3339 with microseconds ("2026-11-17T18:04:05.123456Z"), as the events stream writes times.
func auditTime() -> String {
    var tv = timeval()
    gettimeofday(&tv, nil)
    var t = time_t(tv.tv_sec)
    var g = tm()
    gmtime_r(&t, &g)
    return String(format: "%04d-%02d-%02dT%02d:%02d:%02d.%06dZ", Int(g.tm_year) + 1900, Int(g.tm_mon) + 1, Int(g.tm_mday),
                  Int(g.tm_hour), Int(g.tm_min), Int(g.tm_sec), Int(tv.tv_usec))
}

func isoTime(_ d: Date = Date()) -> String {
    let f = ISO8601DateFormatter()
    f.timeZone = TimeZone.current
    f.formatOptions = [.withInternetDateTime]
    return f.string(from: d)
}

func clockTime(_ d: Date = Date()) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss"
    return f.string(from: d)
}

/// "90s", "30m", "2h", "7d" -> seconds (each caller sets its own maximum).
func parseDuration(_ s: String) -> Int? {
    guard let unit = s.last, let n = Int(s.dropLast()), n > 0, n <= 1_000_000 else { return nil }
    switch unit {
    case "s": return n
    case "m": return n * 60
    case "h": return n * 3600
    case "d": return n * 86400
    default: return nil
    }
}

// MARK: - JSON

func jsonLine(_ obj: [String: Any]) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]) else {
        return "{}"
    }
    return String(decoding: d, as: UTF8.self)
}

func jsonPretty(_ obj: Any) -> String {
    guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .prettyPrinted, .withoutEscapingSlashes]) else {
        return "{}"
    }
    return String(decoding: d, as: UTF8.self)
}

func parseJSONObject(_ bytes: [UInt8]) -> [String: Any]? {
    (try? JSONSerialization.jsonObject(with: Data(bytes))) as? [String: Any]
}

// MARK: - validation

func matches(_ s: String, _ pattern: String) -> Bool {
    s.range(of: pattern, options: .regularExpression) != nil
}

/// Secret, VM, session and profile names: letters, digits, '.', '_', '-'; not starting with '-'.
func validName(_ s: String) -> Bool { matches(s, "^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$") && s != "." && s != ".." }

func validUser(_ s: String) -> Bool { matches(s, "^[a-z_][a-z0-9_-]{0,31}$") }

/// Canonical origin for a secret: https://host[:port]; http://127.0.0.1:PORT and
/// http://localhost:PORT only for testing. nil when it isn't one.
func canonicalOrigin(_ input: String) -> String? {
    var s = input.lowercased()
    guard let sep = s.range(of: "://") else { return nil }
    let scheme = String(s[..<sep.lowerBound])
    s = String(s[sep.upperBound...])
    if s.hasSuffix("/") { s.removeLast() }
    if s.isEmpty || s.contains("/") || s.contains("?") || s.contains("#") || s.contains("@") || s.contains("[") {
        return nil
    }
    var host = s
    var port: Int?
    if let c = s.lastIndex(of: ":") {
        host = String(s[..<c])
        let p = String(s[s.index(after: c)...])
        guard !p.isEmpty, p.count <= 5, p.allSatisfy({ $0.isASCII && $0.isNumber }), let n = Int(p), n >= 1, n <= 65535 else {
            return nil
        }
        port = n
    }
    guard host.count <= 253, !host.hasSuffix(".") else { return nil }
    let labels = host.split(separator: ".", omittingEmptySubsequences: false)
    for l in labels {
        guard !l.isEmpty, l.count <= 63, !l.hasPrefix("-"), !l.hasSuffix("-"),
              l.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-") }) else { return nil }
    }
    switch scheme {
    case "https":
        return "https://" + host + ((port == nil || port == 443) ? "" : ":\(port!)")
    case "http":
        guard host == "127.0.0.1" || host == "localhost", let p = port else { return nil }
        return "http://" + host + (p == 80 ? "" : ":\(p)")
    default:
        return nil
    }
}

// MARK: - arguments

struct Args {
    var positional: [String] = []
    var values: [String: [String]] = [:]
    var flags: Set<String> = []

    func last(_ k: String) -> String? { values[k]?.last }
    func all(_ k: String) -> [String] { values[k] ?? [] }
    func has(_ k: String) -> Bool { flags.contains(k) }
}

/// --name VALUE / --name=VALUE for valueFlags, --name for boolFlags. A later value wins
/// (all() keeps every one), so a wrapper can put defaults first.
func parseArgs(_ argv: [String], values valueFlags: Set<String>, flags boolFlags: Set<String> = []) throws -> Args {
    var a = Args()
    var i = 0
    while i < argv.count {
        let x = argv[i]
        if x.hasPrefix("--") && x.count > 2 {
            var name = String(x.dropFirst(2))
            var inline: String?
            if let eq = name.firstIndex(of: "=") {
                inline = String(name[name.index(after: eq)...])
                name = String(name[..<eq])
            }
            if valueFlags.contains(name) {
                if let v = inline {
                    a.values[name, default: []].append(v)
                } else {
                    guard i + 1 < argv.count else { throw usageError("--\(name) needs a value") }
                    a.values[name, default: []].append(argv[i + 1])
                    i += 1
                }
            } else if boolFlags.contains(name) && inline == nil {
                a.flags.insert(name)
            } else {
                throw usageError("unknown option --\(name)")
            }
        } else {
            a.positional.append(x)
        }
        i += 1
    }
    return a
}

// MARK: - audit log

/// Strings (also inside arrays and objects) made safe to print with clean().
func cleanAll(_ v: Any, max: Int = 300) -> Any {
    if let s = v as? String { return clean(s, max: max) }
    if let a = v as? [Any] { return a.map { cleanAll($0, max: max) } }
    if let d = v as? [String: Any] { return d.mapValues { cleanAll($0, max: max) } }
    return v
}

// The audit log is a hash chain: every event carries seq (one more than the event before),
// ts, prev (the previous event's hash) and hash = sha256 of its canonical JSON without
// "hash". Canonical JSON is what Python's json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=True) writes, so scripts/verify-audit.py can recompute every hash. Each
// event also goes to macOS's unified log (subsystem io.metavert.metacomputer.vault), which
// a normal user can't edit, and from the unlock process to any audit sinks (Sinks.swift).

let zeroHash = String(repeating: "0", count: 64)

/// JSON as Python writes it with sort_keys, compact separators and ensure_ascii.
func canonicalJSON(_ v: Any) -> String {
    if let s = v as? String { return canonicalString(s) }
    if let n = v as? NSNumber {
        if CFGetTypeID(n) == CFBooleanGetTypeID() { return n.boolValue ? "true" : "false" }
        return String(n.int64Value)
    }
    if let a = v as? [Any] { return "[" + a.map(canonicalJSON).joined(separator: ",") + "]" }
    if let d = v as? [String: Any] {
        return "{" + d.keys.sorted { Array($0.unicodeScalars.map { $0.value }).lexicographicallyPrecedes($1.unicodeScalars.map { $0.value }) }
            .map { canonicalString($0) + ":" + canonicalJSON(d[$0]!) }.joined(separator: ",") + "}"
    }
    return "null"
}

func canonicalString(_ s: String) -> String {
    var out = "\""
    for u in s.utf16 {
        switch u {
        case 0x22: out += "\\\""
        case 0x5C: out += "\\\\"
        case 0x0A: out += "\\n"
        case 0x0D: out += "\\r"
        case 0x09: out += "\\t"
        case 0x08: out += "\\b"
        case 0x0C: out += "\\f"
        case 0x20...0x7E: out.unicodeScalars.append(Unicode.Scalar(UInt8(u)))
        default: out += String(format: "\\u%04x", u)
        }
    }
    return out + "\""
}

func eventHash(_ e: [String: Any]) -> String {
    var x = e
    x["hash"] = nil
    return sha256Hex(Array(canonicalJSON(x).utf8))
}

/// A record stays small whatever a caller puts in it: a list keeps its first 32 items (how many more there
/// were goes in `<key>_more`), and a record still over 64 KiB keeps only the fields that say what it was.
func boundAuditRecord(_ r: [String: Any]) -> [String: Any] {
    var out = r
    for (k, v) in r {
        if let a = v as? [Any], a.count > 32 {
            out[k] = Array(a.prefix(32))
            out[k + "_more"] = a.count - 32
        }
    }
    guard canonicalJSON(out).utf8.count > 64 << 10 else { return out }
    var small: [String: Any] = ["truncated": true]
    for k in ["op", "result", "name", "vm", "use", "request_id", "event", "approval", "reason", "requester"] {
        if let v = out[k] { small[k] = v }
    }
    return small
}

/// The last event in the audit log: (seq, hash). A line from an earlier build without a
/// chain counts by the hash of its bytes, so the chain still links to it.
func lastAuditEvent(_ path: String) -> (Int, String) {
    // Read back far enough to hold the whole last line: a record longer than what was read is read
    // further back, never taken for a line from an earlier build.
    var size = 1 << 18
    var bytes: [UInt8] = []
    while true {
        guard let b = readTail(path, size), !b.isEmpty else { return (0, zeroHash) }
        bytes = b
        var end = bytes.count
        while end > 0 && bytes[end - 1] == 0x0A { end -= 1 }
        if bytes.count < size || bytes[0..<end].contains(0x0A) || size >= 1 << 30 { break }
        size *= 4
    }
    var lines = bytes.split(separator: 0x0A, omittingEmptySubsequences: true)
    if bytes.count == size, lines.count > 1 { lines.removeFirst() }  // a partial first line
    guard let last = lines.last else { return (0, zeroHash) }
    if let e = parseJSONObject(Array(last)), let seq = (e["seq"] as? NSNumber)?.intValue, let h = e["hash"] as? String {
        return (seq, h)
    }
    var seq = 0
    for l in lines.reversed() {
        if let e = parseJSONObject(Array(l)), let s = (e["seq"] as? NSNumber)?.intValue { seq = s; break }
    }
    return (seq, sha256Hex(Array(last)))
}

/// Append one event (metadata only, never a value) to audit.jsonl (0600) and the unified log.
/// With `macKey` (the vault's audit key: only a process holding the vault key has it), the
/// record carries `mac`, HMAC-SHA256 over its canonical JSON without `mac` and `hash`: the
/// unlock process checks it before it signs the record for a sink. Without one (a refusal
/// written while the vault is locked), the record is unauthenticated. Returns the stored
/// record, or nil when it couldn't be written (the caller decides whether to go on).
@discardableResult
func audit(_ paths: Paths, _ record: [String: Any], macKey: [UInt8]? = nil) -> [String: Any]? {
    var r = boundAuditRecord((cleanAll(record) as? [String: Any]) ?? [:])
    r["ts"] = auditTime()
    r["id"] = UUID().uuidString.lowercased()   // the CloudEvents id: receivers drop repeated deliveries by it
    for k in ["time", "mac", "hash", "seq", "prev"] { r[k] = nil }
    var stored: [String: Any]?
    var why = "can't lock the audit log"
    _ = try? withFileLock(paths.dir + "/audit.lock") {
        let (seq, prev) = lastAuditEvent(paths.audit)
        r["seq"] = seq + 1
        r["prev"] = prev
        if let k = macKey { r["mac"] = auditRecordMAC(k, r) }
        r["hash"] = eventHash(r)
        let line = canonicalJSON(r)
        let fd = open(paths.audit, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { why = "can't open audit.jsonl: \(String(cString: strerror(errno)))"; return }
        defer { close(fd) }
        var st = stat()
        let before = fstat(fd, &st) == 0 ? st.st_size : -1
        guard writeAll(fd, Array((line + "\n").utf8)), fullSync(fd) else {
            why = "can't write audit.jsonl: \(String(cString: strerror(errno)))"
            if before >= 0 { _ = ftruncate(fd, before) }   // no half line for the chain to trip over
            return
        }
        unifiedLog(line)
        stored = r
    }
    if stored == nil { unifiedLog("audit record NOT written (\(why)): " + canonicalJSON(r)) }
    return stored
}

/// The creation MAC of an audit record: over its canonical JSON without `mac` and `hash`.
func auditRecordMAC(_ key: [UInt8], _ r: [String: Any]) -> String {
    var x = r
    x["mac"] = nil
    x["hash"] = nil
    return hmacSHA256Hex(key: key, data: Array(canonicalJSON(x).utf8))
}

/// Every audit event in the unified log too: `log show --predicate 'subsystem == "io.metavert.metacomputer.vault"'`.
func unifiedLog(_ line: String) {
    #if MCOMP_VAULT_TESTING
    let subsystem = "io.metavert.metacomputer.vault.test"
    #else
    let subsystem = "io.metavert.metacomputer.vault"
    #endif
    Logger(subsystem: subsystem, category: "audit").log("\(line, privacy: .public)")
}

// MARK: - who is asking

struct ProcEntry {
    let pid: pid_t
    let ppid: pid_t
    let name: String
    let start: Double
}

/// Whether a process runs in the foreground of a terminal, as the kernel reports it: it has a controlling terminal,
/// and its process group is that terminal's foreground group. Attribution only: any program of yours can start one in
/// a pseudo-terminal it makes.
func inTerminalForeground(_ pid: pid_t) -> Bool {
    var k = kinfo_proc()
    var size = MemoryLayout<kinfo_proc>.stride
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
    guard pid > 1, sysctl(&mib, 4, &k, &size, nil, 0) == 0, size > 0, k.kp_proc.p_pid == pid else { return false }
    guard k.kp_proc.p_flag & P_CONTROLT != 0, k.kp_eproc.e_tdev != -1 else { return false }   // -1: NODEV
    return k.kp_eproc.e_tpgid > 0 && k.kp_eproc.e_tpgid == k.kp_eproc.e_pgid
}

func procEntry(_ pid: pid_t) -> ProcEntry? {
    var k = kinfo_proc()
    var size = MemoryLayout<kinfo_proc>.stride
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
    guard sysctl(&mib, 4, &k, &size, nil, 0) == 0, size > 0, k.kp_proc.p_pid == pid else { return nil }
    let name = withUnsafeBytes(of: k.kp_proc.p_comm) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
    let st = k.kp_proc.p_un.__p_starttime
    return ProcEntry(pid: pid, ppid: k.kp_eproc.e_ppid, name: name, start: Double(st.tv_sec) + Double(st.tv_usec) / 1e6)
}

func procCwd(_ pid: pid_t) -> String? {
    var vpi = proc_vnodepathinfo()
    guard proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, &vpi, Int32(MemoryLayout<proc_vnodepathinfo>.size)) > 0 else { return nil }
    let s = withUnsafeBytes(of: vpi.pvi_cdir.vip_path) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
    return s.isEmpty ? nil : s
}

func procArgs(_ pid: pid_t) -> [String] {
    return procArgsOrNil(pid) ?? []
}

/// A process's arguments, exactly (KERN_PROCARGS2: what the process left there, so a program that renamed itself shows
/// its new name first), or nil when they can't be read (another account's process, a zombie, no such process).
func procArgsOrNil(_ pid: pid_t) -> [String]? {
    var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
    var size = 0
    guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > 4 else { return nil }
    var buf = [UInt8](repeating: 0, count: size)
    guard sysctl(&mib, 3, &buf, &size, nil, 0) == 0, size > 4 else { return nil }
    let argc = Int(buf.withUnsafeBytes { $0.load(as: Int32.self) })
    guard argc > 0 else { return nil }
    var i = 4
    while i < size && buf[i] != 0 { i += 1 }
    while i < size && buf[i] == 0 { i += 1 }
    var out: [String] = []
    while out.count < argc && i < size {
        let a = i
        while i < size && buf[i] != 0 { i += 1 }
        out.append(String(decoding: buf[a..<i], as: UTF8.self))
        i += 1
    }
    return out.isEmpty ? nil : out
}

/// An interpreter whose arguments couldn't be read: it may be an agent, so it counts as one, and as no single session
/// (lib/owners.py UNKNOWN_AGENT).
let unknownAgent = "unidentified agent"

/// The agent session a process is, if any: "Claude Code" (a process named claude, node running a script named
/// claude*, or the native installer's version file) or "Codex" (its native binary, named codex; npm and Homebrew start
/// it from a node launcher, and the IDE extensions and the desktop app run `codex app-server`, one process for all of
/// their conversations); else what it executes (agentKindOf): "OpenClaw", "Hermes Agent", "Gemini CLI" or
/// unknownAgent.
func agentKind(_ p: pid_t, _ e: ProcEntry) -> String? {
    if e.name == "claude"
        || (e.name == "node" && procArgs(p).dropFirst().prefix(2).contains { URL(fileURLWithPath: $0).lastPathComponent.hasPrefix("claude") }) {
        return "Claude Code"
    }
    var unreadable = false
    #if MCOMP_VAULT_TESTING
    // tests/test_vault_ops.py: a stand-in interpreter of this name whose arguments can't be read
    unreadable = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_UNREADABLE_ARGS"] == e.name
    #endif
    return agentKindOf(e.name, exe: processPath(p), args: { unreadable ? nil : procArgsOrNil(p) }, cwd: { procCwd(p) })
}

/// os.path.basename: what follows the last "/".
func pathBase(_ s: String) -> String {
    return s.split(separator: "/", omittingEmptySubsequences: false).last.map(String.init) ?? s
}

/// The agents' programs by file name, a folder in a script's path, python -m modules, their packages' identifiers,
/// the interpreters' options that take a value, preload or carry code, and bun's and deno's commands: lib/owners.py
/// AGENT_NAMES, AGENT_PATH_WORDS, AGENT_MODULES, AGENT_PACKAGE_WORDS, AGENT_PACKAGE_FOLDERS, AGENT_PACKAGES,
/// OPENCLAW_HELPERS, NODE_VALUE_FLAGS, NODE_PRELOAD_FLAGS, NODE_CODE_FLAGS, DENO_VALUE_FLAGS, PYTHON_VALUE_FLAGS,
/// BUN_COMMANDS, DENO_COMMANDS, SCRIPT_COMMANDS.
let agentNames: [String: String] = [
    "gemini": "Gemini CLI", "gemini.js": "Gemini CLI", "openclaw": "OpenClaw", "openclaw.mjs": "OpenClaw",
    "openclaw-gateway": "OpenClaw", "hermes": "Hermes Agent", "hermes-agent": "Hermes Agent", "hermes-acp": "Hermes Agent"]
let agentPathWords: [(String, String)] = [("/@google/gemini-cli/", "Gemini CLI"), ("hermes_cli", "Hermes Agent")]
let agentModules: [String: String] = ["hermes_cli": "Hermes Agent", "hermes_agent": "Hermes Agent"]
let agentPackageWords: [(String, String)] = [
    ("@google/gemini-cli", "Gemini CLI"), ("gemini-cli-core", "Gemini CLI"), ("openclaw.mjs", "OpenClaw"),
    ("hermes_cli", "Hermes Agent"), ("hermes-agent", "Hermes Agent")]
let agentPackageFolders: [(String, String)] = [("gemini-cli", "Gemini CLI"), ("openclaw", "OpenClaw")]
let agentPackages: [String: String] = [
    "@google/gemini-cli": "Gemini CLI", "@google/gemini-cli-core": "Gemini CLI", "openclaw": "OpenClaw",
    "hermes-agent": "Hermes Agent"]
let openclawHelpers = ["dist", "process", "supervisor"]
let nodeValueFlags: Set<String> = [
    "-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions", "--input-type", "--title",
    "--env-file", "--env-file-if-exists", "--watch-path", "--redirect-warnings", "--secure-heap", "--secure-heap-min",
    "--openssl-config", "--icu-data-dir", "--diagnostic-dir", "--report-dir", "--report-directory", "--report-filename",
    "--report-signal", "--cpu-prof-dir", "--cpu-prof-name", "--cpu-prof-interval", "--heap-prof-dir",
    "--heap-prof-name", "--heap-prof-interval", "--disable-warning", "--experimental-config-file", "--inspect-port",
    "--debug-port", "--max-http-header-size", "--experimental-sea-config", "--test-name-pattern", "--test-reporter",
    "--test-reporter-destination", "--localstorage-file", "--trace-event-categories", "--trace-event-file-pattern",
    "--tls-cipher-list", "--tls-keylog", "--use-largepages", "--watch-kill-signal", "--dns-result-order",
    "--disable-proto", "--unhandled-rejections", "--experimental-default-type", "--stack-size", "--max-old-space-size",
    "--max-semi-space-size",
    "--preload", "--cwd", "--config", "--tsconfig-override", "-d", "--define", "-l", "--main-fields", "--extension-order"]
let nodePreloadFlags: Set<String> = ["-r", "--require", "--import", "--loader", "--experimental-loader", "--preload"]
let nodeCodeFlags: Set<String> = ["-e", "--eval", "-p", "--print", "--run"]
let denoValueFlags: Set<String> = ["-c", "--config", "--import-map", "--location", "--seed", "--cert", "--lock", "--ext",
                                   "-L", "--log-level"]
let pythonValueFlags: Set<String> = ["--check-hash-based-pycs"]
let bunCommands: Set<String> = ["run", "x", "exec", "test", "build", "install", "i", "add", "remove", "rm", "update",
                                "create", "init", "pm", "link", "unlink", "upgrade", "repl", "outdated", "publish", "patch"]
let denoCommands: Set<String> = ["run", "serve", "test", "bench", "check", "compile", "eval", "repl", "task", "install",
                                 "uninstall", "fmt", "lint", "info", "doc", "upgrade", "types", "completions", "coverage",
                                 "init", "jupyter", "publish", "add", "remove", "outdated", "clean", "lsp", "vendor"]
let scriptCommands: Set<String> = ["run", "serve", "test", "bench", "check", "compile"]

/// The agent a process is by a name (its kernel name or its executable's path), as lib/owners.py agent_kind.
func nameKind(_ s: String) -> String? {
    let base = pathBase(s)
    if base == "claude" || matches(s, "/claude/versions/[0-9]+\\.[0-9]+\\.[0-9]+[^/]*$") { return "Claude Code" }
    if base == "codex" { return "Codex" }
    return agentNames[base]
}

func pathAgent(_ path: String) -> String? {
    if let a = agentNames[pathBase(path)] { return a }
    return agentPathWords.first(where: { path.contains($0.0) })?.1
}

/// The agent whose package a text names, as lib/owners.py package_agent.
func packageAgent(_ text: String, folder: Bool = false) -> String? {
    if let w = agentPackageWords.first(where: { text.contains($0.0) }) { return w.1 }
    let parts = text.components(separatedBy: "/")
    let folders = folder ? parts : Array(parts.dropLast())
    let helper = parts.indices.contains { i in
        parts[i] == "openclaw" && Array(parts[min(i + 1, parts.count)..<min(i + 4, parts.count)]) == openclawHelpers
    }
    for (name, agent) in agentPackageFolders where folders.contains(name) && !(agent == "OpenClaw" && helper) {
        return agent
    }
    return nil
}

func realOrSelf(_ path: String) -> String {
    guard let r = realpath(path, nil) else { return path }
    defer { free(r) }
    return String(cString: r)
}

func isDirectory(_ path: String) -> Bool {
    var dir: ObjCBool = false
    return FileManager.default.fileExists(atPath: path, isDirectory: &dir) && dir.boolValue
}

func fileAgent(_ path: String) -> String? {
    let real = realOrSelf(path)
    return pathAgent(path) ?? pathAgent(real) ?? packageAgent(real, folder: isDirectory(real))
}

/// The agent a process is, from what it executes, as lib/owners.py process_agent: its kernel name, then its executable
/// (the realpath of its text image: a native gemini, a single-file build), then for an interpreter (by either) the
/// program it runs (interpreterAgent; args() nil: they can't be read, which counts as an agent: unknownAgent).
func agentKindOf(_ name: String, exe: String?, args: () -> [String]?, cwd: @escaping () -> String?) -> String? {
    if let k = nameKind(name) { return k }
    if let x = exe, let k = nameKind(x) { return k }
    let interp: String? = isInterpreter(name) ? name : (exe.map(pathBase).flatMap { isInterpreter($0) ? $0 : nil })
    guard let interp = interp else { return nil }
    guard let a = args() else { return unknownAgent }
    let low = interp.lowercased()
    let kind = low.hasPrefix("python") ? "python" : (low == "bun" || low == "deno" ? low : "node")
    return interpreterAgent(a, cwd: cwd, kind: kind)
}

/// A process the kernel names after an interpreter (node, bun, python): the agent is told by what it runs.
func isInterpreter(_ name: String) -> Bool {
    let n = pathBase(name)
    return n == "node" || n == "nodejs" || n == "bun" || n == "deno" || n == "Python" || matches(n, "^python[0-9.]*$")
}

/// What an interpreter's argument names, resolved against the process's working folder (lib/owners.py _resolve).
enum Resolved { case file(String), missing, unreadable }

func resolveArg(_ arg: String, cwd: () -> String?, kind: String) -> Resolved {
    var path = arg
    if !arg.hasPrefix("/") {
        guard let w = cwd(), !w.isEmpty else { return .unreadable }
        path = w.hasSuffix("/") ? w + arg : w + "/" + arg
    }
    for p in [path] + (kind == "python" || kind == "exact" ? [] : [path + ".js", path + ".mjs", path + ".cjs"])
    where FileManager.default.fileExists(atPath: p) {
        return .file(p)
    }
    return .missing
}

/// lib/owners.py _package_rule: an agent's package named by any argument, or by what one naming a file resolves to.
func packageRule(_ args: ArraySlice<String>, cwd: () -> String?) -> String? {
    for a in args {
        var values = [a]
        if let eq = a.firstIndex(of: "=") { values.append(String(a[a.index(after: eq)...])) }
        for v in values where !v.isEmpty {
            if let got = packageAgent(v) { return got }
            if v.hasPrefix("-") || v.utf8.count > 4096 { continue }
            if case .file(let p) = resolveArg(v, cwd: cwd, kind: "exact") {
                let real = realOrSelf(p)
                if let got = packageAgent(real, folder: isDirectory(real)) { return got }
            }
        }
    }
    return nil
}

/// lib/owners.py _preload_agent: a file a preload names, or its package.
func preloadAgent(_ spec: String, cwd: () -> String?, kind: String) -> String? {
    if spec.hasPrefix(".") || spec.hasPrefix("/"), case .file(let p) = resolveArg(spec, cwd: cwd, kind: kind) {
        return fileAgent(p)
    }
    let parts = spec.components(separatedBy: "/")
    let key = spec.hasPrefix("@") ? parts.prefix(2).joined(separator: "/") : (parts.first ?? "")
    return agentPackages[key]
}

func scriptAt(_ arg: String, cwd: () -> String?, kind: String) -> String? {
    guard case .file(let p) = resolveArg(arg, cwd: cwd, kind: kind) else { return unknownAgent }
    return fileAgent(p)
}

/// The agent an interpreter's exact arguments run, else nil, or unknownAgent when it can't be told: as lib/owners.py
/// script_agent, step for step. Its title (argv[0]); an agent's package named by any argument or by what one naming
/// an existing file resolves to; then the program in its place: a preload, python's -m module, or the script (the first
/// argument that isn't an option or the value of an option known to take one). Code is no script. Unknown when the
/// script's place names no file, the working folder can't be read for a relative one, or the program is standard input.
func interpreterAgent(_ argv: [String], cwd: @escaping () -> String?, kind: String) -> String? {
    guard let first0 = argv.first else { return nil }
    if let t = agentNames[pathBase(first0)] { return t }
    if let got = packageRule(argv.dropFirst(), cwd: cwd) { return got }
    var i = 1, command = false
    let n = argv.count
    while i < n {
        let a = argv[i]
        if a == "-" { return unknownAgent }
        if a == "--" { return i + 1 < n ? scriptAt(argv[i + 1], cwd: cwd, kind: kind) : nil }
        if a.hasPrefix("-") {
            let eqIndex = a.firstIndex(of: "=")
            let flag = eqIndex.map { String(a[..<$0]) } ?? a
            let value = eqIndex.map { String(a[a.index(after: $0)...]) } ?? ""
            if kind == "python" {
                if !a.hasPrefix("--") {
                    let chars = Array(a)
                    var j = 1
                    while j < chars.count {
                        if chars[j] == "W" || chars[j] == "X" {
                            if j + 1 >= chars.count { i += 1 }
                            break
                        }
                        if chars[j] == "c" || chars[j] == "m" {
                            let rest = String(chars[(j + 1)...])
                            let v = !rest.isEmpty ? rest : (i + 1 < n ? argv[i + 1] : "")
                            return chars[j] == "m" ? agentModules[v.components(separatedBy: ".").first ?? ""] : nil
                        }
                        j += 1
                    }
                } else if pythonValueFlags.contains(flag) && eqIndex == nil {
                    i += 1
                }
            } else {
                let body = a.dropFirst()
                if nodeCodeFlags.contains(flag)
                    || (!a.hasPrefix("--") && kind != "deno" && a.count > 2 && (body.contains("e") || body.contains("p"))) {
                    return nil
                }
                if nodePreloadFlags.contains(flag) && kind != "deno" {
                    let spec = eqIndex != nil ? value : (i + 1 < n ? argv[i + 1] : "")
                    if !spec.isEmpty, let got = preloadAgent(spec, cwd: cwd, kind: kind) { return got }
                }
                if eqIndex == nil && (kind == "deno" ? denoValueFlags : nodeValueFlags).contains(flag) { i += 1 }
            }
            i += 1
            continue
        }
        if (kind == "bun" || kind == "deno") && !command && (kind == "bun" ? bunCommands : denoCommands).contains(a) {
            command = true
            if !scriptCommands.contains(a) { return nil }
            i += 1
            continue
        }
        return scriptAt(a, cwd: cwd, kind: kind)
    }
    return nil
}

/// An agent process that runs all of its conversations, whose requests therefore name no one session: Codex's app
/// server (`codex app-server`: the IDE extension, the desktop app, the terminal app's background server), OpenClaw's
/// gateway (`openclaw-gateway`, or `openclaw gateway ...`) and Hermes Agent's gateway (every Hermes conversation's tools
/// run there; its own CLI and TUI are clients of it). Arguments that can't be read count as shared: unknown never
/// names a session. The label it returns names the process in the vault's question and records.
func sharedAgentProcess(_ kind: String, _ p: pid_t) -> String? {
    switch kind {
    case "Codex":
        let args = procArgs(p)
        return args.isEmpty || args.dropFirst().contains("app-server") ? "Codex app server" : nil
    case "OpenClaw":
        let args = procArgs(p)
        let title = args.first.map { URL(fileURLWithPath: $0).lastPathComponent } ?? ""
        return args.isEmpty || title == "openclaw-gateway" || args.dropFirst().prefix(3).contains("gateway")
            ? "OpenClaw gateway" : nil
    case "Hermes Agent":
        return "Hermes Agent gateway"
    case unknownAgent:
        return "an unidentified agent's process"
    default:
        return nil
    }
}

/// Who sent a request: the client process and the nearest agent session process above it (Claude Code, Codex,
/// OpenClaw, Hermes Agent or Gemini CLI; agentKind), with its start time and working folder (the project). The fields keep their older names (claudePID):
/// they mean that agent session. Attribution only: any process running as you can arrange the same ancestry.
struct Requester {
    let clientPID: pid_t
    let clientExe: String
    let claudePID: pid_t?
    let claudeStart: Double?
    let project: String?
    var agent: String? = nil        // "Claude Code", "Codex", "OpenClaw", "Hermes Agent", "Gemini CLI": what claudePID is;
                                    // with no claudePID, the shared process it came from ("Codex app server", ...)
    // What the client says about whom it asks for (`--on-behalf-pid`): the Claude Code process it names.
    // Shown and recorded, never used to decide anything (project limits, rules and grants use the fields above).
    var claimedClaudePID: pid_t? = nil
    var claimedProject: String? = nil
    var claimedClientPID: pid_t? = nil

    static func of(_ pid: pid_t) -> Requester {
        let exe = pid > 0 ? (processPath(pid).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "?") : "?"
        var p = pid
        for _ in 0..<64 {
            guard p > 1, let e = procEntry(p) else { break }
            // Claude Code from the native installer runs as its version file (.../claude/versions/2.1.325).
            if p != pid, let kind = agentKind(p, e) {
                if let shared = sharedAgentProcess(kind, p) {
                    // Codex's app server, OpenClaw's and Hermes Agent's gateways run every conversation in this one
                    // process: it names no single session, so it gets none of a session's answers, project rules or
                    // tokens (each use asks). Arguments that can't be read count the same way: unknown never names a
                    // session.
                    return Requester(clientPID: pid, clientExe: exe, claudePID: nil, claudeStart: nil, project: nil,
                                     agent: shared)
                }
                let cwd = procCwd(p).map { realPath($0) ?? $0 }
                return Requester(clientPID: pid, clientExe: exe, claudePID: p, claudeStart: e.start, project: cwd, agent: kind)
            }
            p = e.ppid
        }
        return Requester(clientPID: pid, clientExe: exe, claudePID: nil, claudeStart: nil, project: nil)
    }

    /// Who asked, with what the client says about whom it asks for (`on_behalf_pid`) kept beside it as an
    /// unverified claim. The claim is attribution only: anything running as you can name any live process,
    /// so it never satisfies a project limit, an unattended rule or a grant; those see the client itself.
    static func of(_ pid: pid_t, onBehalf: pid_t?) -> Requester {
        var direct = of(pid)
        guard let ob = onBehalf, ob > 1, ob != pid, procEntry(ob) != nil else { return direct }
        let c = of(ob)
        guard c.claudePID != nil, c.claudePID != direct.claudePID else { return direct }
        direct.claimedClaudePID = c.claudePID
        direct.claimedProject = c.project
        direct.claimedClientPID = ob
        return direct
    }

    /// Is that Claude Code process still the same one (same pid, same start)?
    var claudeAlive: Bool {
        guard let p = claudePID, let s = claudeStart, let e = procEntry(p) else { return false }
        return abs(e.start - s) < 0.001
    }

    var record: [String: Any] {
        var d: [String: Any] = ["client_pid": Int(clientPID), "client": clientExe]
        if let p = claudePID { d["claude_pid"] = Int(p) }
        if let a = agent, a != "Claude Code" { d["agent"] = a }
        if let pr = project { d["project"] = pr }
        if let c = claimedClaudePID {
            var b: [String: Any] = ["claude_pid": Int(c), "verified": false]
            if let pr = claimedProject { b["project"] = pr }
            if let cp = claimedClientPID { b["client_pid"] = Int(cp) }
            d["on_behalf"] = b
        }
        return d
    }

    /// One printable line (a folder or program name can hold a newline or terminal codes).
    var describe: String {
        let claim = claimedClaudePID.map { ", for an agent session in \(tildePath(claimedProject ?? "?")) (pid \($0)) as it says (unverified)" } ?? ""
        if let p = claudePID {
            return clean("\(agent ?? "Claude Code") in \(tildePath(project ?? "?")) (pid \(p))" + claim, max: 300)
        }
        if agent == "Codex app server" {
            return clean("\(clientExe) (pid \(clientPID)) under Codex's app server, whose conversations share one process"
                         + " (no session's answers apply)" + claim, max: 300)
        }
        if agent == "an unidentified agent's process" {
            return clean("\(clientExe) (pid \(clientPID)) under an interpreter whose arguments couldn't be read, counted as an"
                         + " agent (no session's answers apply)" + claim, max: 300)
        }
        if let a = agent, a.hasSuffix(" gateway") {
            return clean("\(clientExe) (pid \(clientPID)) under \(a.dropLast(" gateway".count))'s gateway, whose conversations"
                         + " share one process (no session's answers apply)" + claim, max: 300)
        }
        return clean("\(clientExe) (pid \(clientPID)), not from Claude Code, Codex, OpenClaw, Hermes Agent"
                     + " or Gemini CLI" + claim, max: 300)
    }
}

func tildePath(_ p: String) -> String {
    let h = homeDirectory()
    return p == h ? "~" : (p.hasPrefix(h + "/") ? "~" + p.dropFirst(h.count) : p)
}

/// The path with every symbolic link resolved, as the kernel reports a process's folder
/// (/tmp/x -> /private/tmp/x); nil when it doesn't exist.
func realPath(_ p: String) -> String? {
    guard let r = realpath(p, nil) else { return nil }
    defer { free(r) }
    return String(cString: r)
}

/// Is folder inside (or equal to) one of the given folders?
func insideAny(_ folder: String, _ roots: [String]) -> Bool {
    roots.contains { folder == $0 || folder.hasPrefix($0.hasSuffix("/") ? $0 : $0 + "/") }
}

/// Printable text on one line, at most max characters. Anything that could move the
/// cursor, change colours or reorder text on a terminal goes: C0 and C1 controls, DEL,
/// format characters (bidi overrides, zero-width), line and paragraph separators,
/// private-use and unassigned code points. Tabs and newlines become spaces, the rest '?'.
/// Every string that came from the VM, a client or the system is passed through this
/// before it's printed in the unlock terminal, written to the audit log or sent back.
func clean(_ s: String, max: Int = 300) -> String {
    var out = String.UnicodeScalarView()
    var n = 0
    for u in s.unicodeScalars {
        if n >= max { break }
        switch u.properties.generalCategory {
        case .control, .format, .lineSeparator, .paragraphSeparator, .privateUse, .surrogate, .unassigned:
            out.append(u == "\n" || u == "\r" || u == "\t" ? " " : "?")
        default:
            out.append(u)
        }
        n += 1
    }
    let r = String(out)
    return s.unicodeScalars.count > max ? r + "..." : r
}

/// Keep a message short, printable and on one line (terminal, results, audit log).
func oneLine(_ s: String, max: Int = 300) -> String { clean(s, max: max) }

/// A size the VM can steer (an API answer through the proxy, a sealed session's archive), as one of a few fixed values:
/// the smallest of 0, 1 KiB, 64 KiB, 1 MiB, 16 MiB, 256 MiB, 4 GiB and 1 TiB that holds it. An exact count could spell a
/// value the VM was given, a few bytes per request; one of eight values carries three bits at most.
func sizeBound(_ n: Int) -> Int {
    for b in [0, 1 << 10, 1 << 16, 1 << 20, 1 << 24, 1 << 28, 1 << 32] where n <= b { return b }
    return 1 << 40
}

/// A count the VM can steer (requests made with a token), as one of a few words.
func countClass(_ n: Int) -> String {
    n <= 0 ? "none" : n < 10 ? "1-9" : n < 100 ? "10-99" : "100 or more"
}

/// A client's text (a selector) inside a question: printable, runs of spaces collapsed, short.
func promptText(_ s: String, max: Int) -> String {
    clean(clean(s, max: 4096).split(whereSeparator: { $0 == " " }).joined(separator: " "), max: max)
}

/// No core dumps (they would hold the decrypted vault) and no debugger, even one of yours.
func hardenProcess() {
    var rl = rlimit(rlim_cur: 0, rlim_max: 0)
    _ = setrlimit(RLIMIT_CORE, &rl)
    typealias PtraceFn = @convention(c) (Int32, pid_t, UnsafeMutableRawPointer?, Int32) -> Int32
    if let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "ptrace") {   // RTLD_DEFAULT
        _ = unsafeBitCast(sym, to: PtraceFn.self)(31, 0, nil, 0)              // PT_DENY_ATTACH
    }
}
