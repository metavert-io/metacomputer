// mcomp-vault: shared helpers (paths, errors, arguments, JSON, audit log).
import Darwin
import Foundation

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

/// One JSON line per event in audit.jsonl (0600). Metadata only: never a value.
func audit(_ paths: Paths, _ record: [String: Any]) {
    var r = (cleanAll(record) as? [String: Any]) ?? [:]
    r["time"] = isoTime()
    let line = Array((jsonLine(r) + "\n").utf8)
    let fd = open(paths.audit, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { return }
    _ = writeAll(fd, line)
    close(fd)
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

/// No core dumps (they would hold the decrypted vault) and no debugger, even one of yours.
func hardenProcess() {
    var rl = rlimit(rlim_cur: 0, rlim_max: 0)
    _ = setrlimit(RLIMIT_CORE, &rl)
    typealias PtraceFn = @convention(c) (Int32, pid_t, UnsafeMutableRawPointer?, Int32) -> Int32
    if let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "ptrace") {   // RTLD_DEFAULT
        _ = unsafeBitCast(sym, to: PtraceFn.self)(31, 0, nil, 0)              // PT_DENY_ATTACH
    }
}
