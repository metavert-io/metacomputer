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

/// Write a file atomically (temporary file in the same folder, fsync, rename), mode 0600.
func writeFileAtomic(_ path: String, _ bytes: [UInt8]) throws {
    let tmp = path + ".tmp-\(getpid())"
    unlink(tmp)
    let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw VaultError("cannot write \(tmp): \(String(cString: strerror(errno)))") }
    let ok = writeAll(fd, bytes) && fsync(fd) == 0
    close(fd)
    guard ok, rename(tmp, path) == 0 else {
        unlink(tmp)
        throw VaultError("cannot write \(path)")
    }
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

/// "90s", "30m", "2h" -> seconds.
func parseDuration(_ s: String) -> Int? {
    guard let unit = s.last, let n = Int(s.dropLast()), n > 0 else { return nil }
    switch unit {
    case "s": return n
    case "m": return n * 60
    case "h": return n * 3600
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

/// The last event in the audit log: (seq, hash). A line from an earlier build without a
/// chain counts by the hash of its bytes, so the chain still links to it.
func lastAuditEvent(_ path: String) -> (Int, String) {
    guard let bytes = readTail(path, 1 << 18), !bytes.isEmpty else { return (0, zeroHash) }
    var lines = bytes.split(separator: 0x0A, omittingEmptySubsequences: true)
    if bytes.count == 1 << 18, lines.count > 1 { lines.removeFirst() }  // a partial first line
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
    var r = (cleanAll(record) as? [String: Any]) ?? [:]
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
        guard writeAll(fd, Array((line + "\n").utf8)), fsync(fd) == 0 else {
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
    var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
    var size = 0
    guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > 4 else { return [] }
    var buf = [UInt8](repeating: 0, count: size)
    guard sysctl(&mib, 3, &buf, &size, nil, 0) == 0, size > 4 else { return [] }
    let argc = Int(buf.withUnsafeBytes { $0.load(as: Int32.self) })
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
    return out
}

/// Who sent a request: the client process and the nearest Claude Code process above it
/// (a process named claude, or node running a script named claude*), with its start time
/// and working folder (the project). Attribution only: any process running as you can
/// arrange the same ancestry.
struct Requester {
    let clientPID: pid_t
    let clientExe: String
    let claudePID: pid_t?
    let claudeStart: Double?
    let project: String?

    static func of(_ pid: pid_t) -> Requester {
        let exe = pid > 0 ? (processPath(pid).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "?") : "?"
        var p = pid
        for _ in 0..<64 {
            guard p > 1, let e = procEntry(p) else { break }
            let isClaude = e.name == "claude"
                || (e.name == "node" && procArgs(p).dropFirst().prefix(2).contains { URL(fileURLWithPath: $0).lastPathComponent.hasPrefix("claude") })
            if isClaude && p != pid {
                let cwd = procCwd(p).map { realPath($0) ?? $0 }
                return Requester(clientPID: pid, clientExe: exe, claudePID: p, claudeStart: e.start, project: cwd)
            }
            p = e.ppid
        }
        return Requester(clientPID: pid, clientExe: exe, claudePID: nil, claudeStart: nil, project: nil)
    }

    /// Is that Claude Code process still the same one (same pid, same start)?
    var claudeAlive: Bool {
        guard let p = claudePID, let s = claudeStart, let e = procEntry(p) else { return false }
        return abs(e.start - s) < 0.001
    }

    var record: [String: Any] {
        var d: [String: Any] = ["client_pid": Int(clientPID), "client": clientExe]
        if let p = claudePID { d["claude_pid"] = Int(p) }
        if let pr = project { d["project"] = pr }
        return d
    }

    /// One printable line (a folder or program name can hold a newline or terminal codes).
    var describe: String {
        if let p = claudePID {
            return clean("Claude Code in \(tildePath(project ?? "?")) (pid \(p))", max: 300)
        }
        return clean("\(clientExe) (pid \(clientPID)), not from Claude Code", max: 300)
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
