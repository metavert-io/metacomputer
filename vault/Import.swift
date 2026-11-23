// mcomp-vault: bringing logins in. `import-passwords` reads Chrome's saved passwords (a copy of its
// Login Data, decrypted with the "Chrome Safe Storage" key that macOS's Keychain gives this program
// after asking you), or a CSV export from Chrome, the macOS Passwords app, 1Password or Bitwarden.
// `import-session` seals a site's cookies from your own Chrome as a session the VM's Chrome can load.
//
// Everything here runs in your own terminal and needs the vault passphrase, like `set`. Values go
// from the file (or Chrome's database) into the encrypted vault and nowhere else: the output names
// sites, counts and masked usernames only. Imported logins and sessions start at `each` approval.
//
// Test builds never read your Chrome or ask the Keychain: MCOMP_VAULT_TEST_CHROME_DIR (a folder of
// synthetic profiles, outside ~/Library) and MCOMP_VAULT_TEST_CHROME_KEY (the stand-in for the Safe
// Storage password) are required there, and release builds don't contain them.
import CommonCrypto
import Darwin
import Foundation
import Security
import SQLite3

// MARK: - Chrome on this Mac

/// Chrome's user-data folder: ~/Library/Application Support/Google/Chrome (test builds: the stand-in).
func chromeUserDataDir() throws -> String {
    #if MCOMP_VAULT_TESTING
    let d = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_CHROME_DIR"] ?? ""
    let lib = URL(fileURLWithPath: homeDirectory() + "/Library").resolvingSymlinksInPath().path + "/"
    let resolved = URL(fileURLWithPath: absolutePath(d.isEmpty ? "/" : d)).resolvingSymlinksInPath().path + "/"
    guard !d.isEmpty, !resolved.hasPrefix(lib) else {
        throw VaultError("this TEST build reads Chrome only from MCOMP_VAULT_TEST_CHROME_DIR, a folder outside ~/Library")
    }
    return absolutePath(d)
    #else
    return homeDirectory() + "/Library/Application Support/Google/Chrome"
    #endif
}

/// A Chrome profile folder's name ("Default", "Profile 1"): no path, nothing hidden.
func validChromeProfile(_ s: String) -> Bool {
    matches(s, "^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$") && !s.contains("..")
}

/// The password Chrome keeps in the login keychain as "Chrome Safe Storage". macOS asks you whether
/// this program may read it (the item belongs to Chrome). Test builds use the stand-in and never ask.
func chromeSafeStoragePassword() throws -> [UInt8] {
    #if MCOMP_VAULT_TESTING
    guard let k = ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_CHROME_KEY"], !k.isEmpty else {
        throw VaultError("this TEST build never asks the Keychain: set MCOMP_VAULT_TEST_CHROME_KEY")
    }
    return Array(k.utf8)
    #else
    let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "Chrome Safe Storage",
                            kSecAttrAccount as String: "Chrome", kSecReturnData as String: true,
                            kSecMatchLimit as String: kSecMatchLimitOne]
    var out: CFTypeRef?
    let st = SecItemCopyMatching(q as CFDictionary, &out)
    switch st {
    case errSecSuccess:
        guard let d = out as? Data, !d.isEmpty else { throw VaultError("the Keychain gave an empty Chrome Safe Storage key") }
        return Array(d)
    case errSecItemNotFound: throw VaultError("no \"Chrome Safe Storage\" item in your login keychain: is Chrome installed and has it run?")
    case errSecUserCanceled, errSecAuthFailed: throw VaultError("macOS didn't allow reading Chrome's Safe Storage key (you chose Deny, or the password wasn't accepted)")
    case errSecInteractionNotAllowed: throw VaultError("macOS can't ask you about Chrome's Safe Storage key here (no window to show it in): run this in Terminal on the Mac's screen")
    default: throw VaultError("the Keychain refused Chrome's Safe Storage key (OSStatus \(st))")
    }
    #endif
}

/// Chrome's macOS key: PBKDF2-HMAC-SHA1(Safe Storage password, "saltysalt", 1003 rounds), 16 bytes.
func chromeMacKey(_ password: [UInt8]) -> [UInt8]? { pbkdf2SHA1(password, Array("saltysalt".utf8), 1003, 16) }

/// Chrome's key on Linux without a keyring (the "basic" store the VM's Chrome uses): "peanuts", one round.
func chromeLinuxKey() -> [UInt8] { pbkdf2SHA1(Array("peanuts".utf8), Array("saltysalt".utf8), 1, 16) ?? [] }

func pbkdf2SHA1(_ password: [UInt8], _ salt: [UInt8], _ rounds: Int, _ length: Int) -> [UInt8]? {
    var out = [UInt8](repeating: 0, count: length)
    let rc = password.withUnsafeBytes { pw in
        salt.withUnsafeBytes { s in
            out.withUnsafeMutableBytes { o in
                CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2), pw.baseAddress!.assumingMemoryBound(to: CChar.self), pw.count,
                                     s.baseAddress!.assumingMemoryBound(to: UInt8.self), s.count,
                                     CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA1), UInt32(rounds),
                                     o.baseAddress!.assumingMemoryBound(to: UInt8.self), length)
            }
        }
    }
    return rc == kCCSuccess ? out : nil
}

/// AES-128-CBC with Chrome's fixed IV (16 spaces) and PKCS#7 padding.
func chromeCBC(_ encrypt: Bool, key: [UInt8], _ input: [UInt8]) -> [UInt8]? {
    let iv = [UInt8](repeating: 0x20, count: 16)
    var out = [UInt8](repeating: 0, count: input.count + 16)
    var moved = 0
    let outCap = out.count
    let rc = key.withUnsafeBytes { k in
        iv.withUnsafeBytes { v in
            input.withUnsafeBytes { i in
                out.withUnsafeMutableBytes { o in
                    CCCrypt(CCOperation(encrypt ? kCCEncrypt : kCCDecrypt), CCAlgorithm(kCCAlgorithmAES), CCOptions(kCCOptionPKCS7Padding),
                            k.baseAddress, k.count, v.baseAddress, i.baseAddress, i.count, o.baseAddress, outCap, &moved)
                }
            }
        }
    }
    guard rc == kCCSuccess else { wipe(&out); return nil }
    let r = Array(out[0..<moved])
    wipe(&out)
    return r
}

/// A value Chrome encrypted ("v10" + AES-CBC); nil when it isn't one this program can open.
func chromeDecrypt(_ blob: [UInt8], key: [UInt8]) -> [UInt8]? {
    guard blob.count > 3, blob[0] == 0x76, blob[1] == 0x31, blob[2] == 0x30 else { return nil }   // "v10"
    return chromeCBC(false, key: key, Array(blob[3...]))
}

func chromeEncrypt(_ plain: [UInt8], key: [UInt8]) -> [UInt8] {
    Array("v10".utf8) + (chromeCBC(true, key: key, plain) ?? [])
}

// MARK: - reading a copy of a Chrome database

/// Copy a SQLite database (with its -wal and -journal) into a private folder, run body on the copy, and
/// remove the copy. Chrome keeps its databases open, so they're never read in place.
func withDatabaseCopy<T>(_ src: String, _ body: (OpaquePointer) throws -> T) throws -> T {
    var st = stat()
    guard lstat(src, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG else { throw VaultError("no database at \(src)") }
    let dir = userTempDir() + "/mcomp-import-\(getpid())-" + randomBytes(6).map { String(format: "%02x", $0) }.joined()
    guard mkdir(dir, 0o700) == 0 else { throw VaultError("cannot make a private folder for the copy") }
    defer {
        for ext in ["", "-wal", "-journal", "-shm"] { unlink(dir + "/db" + ext) }
        rmdir(dir)
    }
    for ext in ["", "-wal", "-journal"] where fileExists(src + ext) {
        var b = try readFileBytes(src + ext, max: 1 << 30)
        defer { wipe(&b) }
        let fd = open(dir + "/db" + ext, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard fd >= 0, writeAll(fd, b) else { if fd >= 0 { close(fd) }; throw VaultError("cannot copy \(src)") }
        close(fd)
    }
    var db: OpaquePointer?
    guard sqlite3_open_v2(dir + "/db", &db, SQLITE_OPEN_READWRITE, nil) == SQLITE_OK, let h = db else {
        sqlite3_close(db)
        throw VaultError("\(src) isn't a database this program can read")
    }
    defer {
        sqlite3_progress_handler(h, 0, nil, nil)
        sqlite3_close(h)
    }
    // The file may not be Chrome's: a schema can make a small file produce endless rows or huge values, so no
    // single value may pass 16 MB, and reading stops after importMaxSeconds.
    sqlite3_limit(h, SQLITE_LIMIT_LENGTH, 16 << 20)
    importDeadline = Date().timeIntervalSince1970 + importMaxSeconds
    sqlite3_progress_handler(h, 10_000, { _ in Date().timeIntervalSince1970 > importDeadline ? 1 : 0 }, nil)
    return try body(h)
}

/// Limits on what one database copy may yield, whoever prepared it (Chrome's own stay far below them).
let importMaxRows = 200_000
let importMaxBytes = 256 << 20
let importMaxSeconds: TimeInterval = 60
nonisolated(unsafe) var importDeadline: TimeInterval = 0

/// Refuse unless each name is an ordinary table: a view or a virtual table runs code of the file's choosing
/// (a recursive query, a join without end) each time it's read, and Chrome's databases have neither there.
func requireTables(_ db: OpaquePointer, _ names: [String]) throws {
    for n in names {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT type, sql FROM sqlite_master WHERE name = ?1 COLLATE NOCASE", -1, &stmt, nil) == SQLITE_OK,
              let s = stmt else { throw VaultError("the database's schema can't be read") }
        defer { sqlite3_finalize(s) }
        sqlite3_bind_text(s, 1, n, -1, unsafeBitCast(-1, to: sqlite3_destructor_type.self))
        var kinds: [String] = []
        var virtual = false
        var rc = sqlite3_step(s)
        while rc == SQLITE_ROW {
            kinds.append(sqlite3_column_text(s, 0).map { String(cString: $0) } ?? "")
            let sql = sqlite3_column_text(s, 1).map { String(cString: $0) } ?? ""
            if sql.uppercased().replacingOccurrences(of: " ", with: "").hasPrefix("CREATEVIRTUAL") { virtual = true }
            rc = sqlite3_step(s)
        }
        guard rc == SQLITE_DONE else { throw VaultError("the database's schema can't be read") }
        guard kinds == ["table"], !virtual else {
            throw VaultError(kinds.isEmpty ? "the database has no \(n) table: not one Chrome wrote"
                             : "the database's \(n) isn't an ordinary table (\(virtual ? "a virtual table" : kinds.joined(separator: ", "))): not one Chrome wrote, so it isn't read")
        }
    }
}

/// The rows of a query, each column by name (text, integer or blob), at most importMaxRows rows and importMaxBytes
/// of values; anything that stops the query early (an error, the time limit) is an error, never a shorter list.
func sqliteRows(_ db: OpaquePointer, _ sql: String) throws -> [[String: Any]] {
    var stmt: OpaquePointer?
    guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let s = stmt else {
        throw VaultError("the database doesn't have what was expected (\(String(cString: sqlite3_errmsg(db))))")
    }
    defer { sqlite3_finalize(s) }
    var rows: [[String: Any]] = []
    var bytes = 0
    var rc = sqlite3_step(s)
    while rc == SQLITE_ROW {
        guard rows.count < importMaxRows else { throw VaultError("the database holds more than \(importMaxRows) rows there: not one this imports") }
        var r: [String: Any] = [:]
        for i in 0..<sqlite3_column_count(s) {
            let name = String(cString: sqlite3_column_name(s, i))
            switch sqlite3_column_type(s, i) {
            case SQLITE_INTEGER: r[name] = Int64(sqlite3_column_int64(s, i))
            case SQLITE_TEXT:
                bytes += Int(sqlite3_column_bytes(s, i))
                r[name] = sqlite3_column_text(s, i).map { String(cString: $0) } ?? ""
            case SQLITE_BLOB:
                let n = Int(sqlite3_column_bytes(s, i))
                bytes += n
                if n > 0, let p = sqlite3_column_blob(s, i) { r[name] = Array(UnsafeRawBufferPointer(start: p, count: n)) } else { r[name] = [UInt8]() }
            default: break
            }
        }
        guard bytes <= importMaxBytes else { throw VaultError("the database's values there pass \(importMaxBytes >> 20) MB: not one this imports") }
        rows.append(r)
        rc = sqlite3_step(s)
    }
    guard rc == SQLITE_DONE else {
        throw VaultError(rc == SQLITE_INTERRUPT ? "reading the database took longer than \(Int(importMaxSeconds)) seconds: stopped, nothing imported"
                         : "reading the database failed part way (\(String(cString: sqlite3_errmsg(db)))): nothing imported")
    }
    return rows
}

// MARK: - logins, from Chrome or a CSV

/// One login to import: where, who, the password, and an optional TOTP seed (base32).
struct ImportedLogin {
    var origins: [String]
    var username: String
    var password: String
    var totp: String?
    var title: String?
}

/// The most sites one imported login keeps (the first ones listed): a login for more is cut to these, so one
/// cell of a file can't make a secret, or the audit record naming its sites, without bound.
let maxOriginsPerLogin = 20

/// The origins a URL from a saved login stands for (https only; http for localhost or 127.0.0.1 with a port),
/// at most maxOriginsPerLogin + 1 of them (the caller says when a login had more).
func originsOf(_ raw: String) -> [String] {
    var out: [String] = []
    for part in raw.split(whereSeparator: { $0 == "," || $0 == "\n" || $0 == " " }) {
        guard part.utf8.count <= 2048, let u = URLComponents(string: String(part)), let scheme = u.scheme?.lowercased(),
              let host = u.host, !host.isEmpty else { continue }
        let o = canonicalOrigin("\(scheme)://\(host)" + (u.port.map { ":\($0)" } ?? ""))
        if let o, !out.contains(o) { out.append(o) }
        if out.count > maxOriginsPerLogin { break }
    }
    return out
}

/// A login's origins cut to maxOriginsPerLogin, noting the cut.
func capOrigins(_ origins: [String], _ s: inout ImportSummary) -> [String] {
    guard origins.count > maxOriginsPerLogin else { return origins }
    s.skip("sites past the first \(maxOriginsPerLogin) of one login (the login comes in with its first \(maxOriginsPerLogin))")
    return Array(origins.prefix(maxOriginsPerLogin))
}

struct ImportSummary {
    var read = 0
    var skipped: [String: Int] = [:]
    var logins: [ImportedLogin] = []
    mutating func skip(_ why: String) { skipped[why, default: 0] += 1 }
}

/// Keep one login per (origin, username): the last one read wins.
func dedupe(_ s: inout ImportSummary) {
    var seen: [String: Int] = [:]
    var out: [ImportedLogin] = []
    for l in s.logins {
        let k = (l.origins.first ?? "") + "\n" + l.username
        if let i = seen[k] { out[i] = l; s.skip("a repeat of another login (the last one is kept)") } else { seen[k] = out.count; out.append(l) }
    }
    s.logins = out
}

/// Chrome's saved passwords from a copy of the profile's Login Data. With decrypt false (a dry run),
/// the passwords aren't opened, and no key is asked for.
func chromeLogins(profile: String, decrypt: Bool) throws -> ImportSummary {
    let path = try chromeUserDataDir() + "/" + profile + "/Login Data"
    guard fileExists(path) else { throw VaultError("no Chrome profile \"\(profile)\" with saved passwords (\(tildePath(path)))") }
    var key: [UInt8] = []
    defer { wipe(&key) }
    if decrypt {
        var pw = try chromeSafeStoragePassword()
        defer { wipe(&pw) }
        guard let k = chromeMacKey(pw) else { throw VaultError("key derivation failed") }
        key = k
    }
    var s = ImportSummary()
    try withDatabaseCopy(path) { db in
        try requireTables(db, ["logins"])
        for r in try sqliteRows(db, "SELECT origin_url, username_value, password_value, blacklisted_by_user FROM logins") {
            s.read += 1
            if (r["blacklisted_by_user"] as? Int64 ?? 0) != 0 { s.skip("a 'never save' entry"); continue }
            let url = r["origin_url"] as? String ?? ""
            if url.hasPrefix("android://") { s.skip("an app's login, not a website's"); continue }
            let origins = capOrigins(originsOf(url), &s)
            guard !origins.isEmpty else { s.skip("not an https site"); continue }
            let user = r["username_value"] as? String ?? ""
            var pass = ""
            if decrypt {
                guard var plain = chromeDecrypt(r["password_value"] as? [UInt8] ?? [], key: key) else {
                    s.skip("a password this program can't open (not Chrome's v10 format)"); continue
                }
                defer { wipe(&plain) }
                guard let t = String(validating: plain, as: UTF8.self) else {
                    s.skip("a password this program can't open (not Chrome's v10 format)"); continue
                }
                guard !t.isEmpty else { s.skip("an empty password"); continue }
                pass = t
            } else if (r["password_value"] as? [UInt8] ?? []).isEmpty {
                s.skip("an empty password"); continue
            }
            s.logins.append(ImportedLogin(origins: origins, username: user, password: pass, totp: nil, title: nil))
        }
    }
    dedupe(&s)
    return s
}

/// RFC 4180 CSV: quoted fields (with "" for a quote and line breaks inside), a byte-order mark ignored.
func parseCSV(_ text: String) -> [[String]] {
    var rows: [[String]] = [], row: [String] = [], field = ""
    var inQuotes = false, i = text.unicodeScalars.startIndex
    let u = text.unicodeScalars
    if i < u.endIndex, u[i] == "\u{FEFF}" { i = u.index(after: i) }
    var startedField = false
    while i < u.endIndex {
        let c = u[i]
        if inQuotes {
            if c == "\"" {
                let n = u.index(after: i)
                if n < u.endIndex, u[n] == "\"" { field.unicodeScalars.append("\""); i = u.index(after: n); continue }
                inQuotes = false
            } else {
                field.unicodeScalars.append(c)
            }
        } else {
            switch c {
            case "\"" where !startedField || field.isEmpty: inQuotes = true; startedField = true
            case ",": row.append(field); field = ""; startedField = false
            case "\r": break
            case "\n": row.append(field); rows.append(row); row = []; field = ""; startedField = false
            default: field.unicodeScalars.append(c); startedField = true
            }
        }
        i = u.index(after: i)
    }
    if startedField || !field.isEmpty || !row.isEmpty { row.append(field); rows.append(row) }
    return rows.filter { !($0.count == 1 && $0[0].isEmpty) }
}

/// Logins from a CSV export: Chrome (name,url,username,password,note), the macOS Passwords app
/// (Title,URL,Username,Password,Notes,OTPAuth), 1Password (Title,Url,Username,Password,OTPAuth,...) or
/// Bitwarden (folder,...,type,name,...,login_uri,login_username,login_password,login_totp). Returns the
/// format it recognised.
func csvLogins(_ path: String) throws -> (ImportSummary, String) {
    var bytes = try readFileBytes(absolutePath(path), max: 32 << 20)
    defer { wipe(&bytes) }
    guard let text = String(validating: bytes, as: UTF8.self) else { throw VaultError("\(path) isn't UTF-8 text") }
    var rows = parseCSV(text)
    guard let header = rows.first else { throw VaultError("\(path) is empty") }
    rows.removeFirst()
    let h = header.map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
    func col(_ names: [String]) -> Int? { names.lazy.compactMap { h.firstIndex(of: $0) }.first }
    guard let urlC = col(["login_uri", "url", "website", "web site"]), let passC = col(["login_password", "password"]) else {
        throw VaultError("\(path) has no url and password columns: not a Chrome, Passwords app, 1Password or Bitwarden export")
    }
    let userC = col(["login_username", "username", "user name", "login"])
    let otpC = col(["login_totp", "otpauth", "totp", "one-time password"])
    let typeC = h.contains("login_uri") ? col(["type"]) : nil
    let titleC = col(["name", "title"])
    let format = h.contains("login_uri") ? "Bitwarden" : (h.contains("otpauth") ? (h.contains("favorite") ? "1Password" : "Passwords app") : "Chrome")
    var s = ImportSummary()
    for r in rows {
        func v(_ c: Int?) -> String { c.flatMap { $0 < r.count ? r[$0] : nil } ?? "" }
        s.read += 1
        if let t = typeC, !v(t).isEmpty, v(t).lowercased() != "login" { s.skip("not a login (a note, card or identity)"); continue }
        let origins = capOrigins(originsOf(v(urlC)), &s)
        guard !origins.isEmpty else { s.skip("not an https site"); continue }
        let pass = v(passC)
        guard !pass.isEmpty, pass.utf8.count <= 4096 else { s.skip(pass.isEmpty ? "an empty password" : "a password over 4096 bytes"); continue }
        let user = v(userC)
        let otp = v(otpC).trimmingCharacters(in: .whitespaces)
        s.logins.append(ImportedLogin(origins: origins, username: user, password: pass, totp: otp.isEmpty ? nil : otp,
                                      title: titleC.map { v($0) }))
    }
    dedupe(&s)
    return (s, format)
}

/// A secret name for a site: its host, made a valid name, with -2, -3... when taken.
func importName(_ origin: String, _ taken: Set<String>, suffix: String = "") -> String {
    var base = originHost(origin).lowercased().filter { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "." || $0 == "-" || $0 == "_") }
    if base.isEmpty || base.hasPrefix("-") { base = "login" + base }
    base = String(base.prefix(60 - suffix.count)) + suffix
    var name = base, n = 2
    while taken.contains(name) || !validName(name) { name = String(base.prefix(58 - suffix.count)) + "-\(n)"; n += 1 }
    return name
}

/// A new 2FA code's name: its login's name with -otp (then -otp-2, -otp-3... when taken), so which account it
/// belongs to reads off the name as well as its totp_for.
func totpImportName(_ login: String, _ taken: Set<String>) -> String {
    let base = String(login.prefix(56)) + "-otp"
    var name = base, n = 2
    while taken.contains(name) || !validName(name) { name = base + "-\(n)"; n += 1 }
    return name
}

/// `import-passwords --browser chrome [--profile NAME] | --csv FILE [--site HOST ...] [--dry-run]`
func cmdImportPasswords(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: import-passwords --browser chrome [--profile NAME] [--site HOST ...] [--dry-run] | import-passwords --csv FILE [--site HOST ...] [--dry-run]"
    let a = try parseArgs(argv, values: ["browser", "profile", "csv", "site"], flags: ["dry-run"], repeatable: ["site"])
    guard a.positional.isEmpty, (a.last("browser") == nil) != (a.last("csv") == nil) else { throw usageError(usage) }
    if let b = a.last("browser"), b != "chrome" { throw usageError("--browser: only chrome (the others: export a CSV and use --csv)") }
    let profile = a.last("profile") ?? "Default"
    guard validChromeProfile(profile) else { throw usageError("--profile: a Chrome profile folder's name, like Default or \"Profile 1\"") }
    if a.last("profile") != nil && a.last("csv") != nil { throw usageError("--profile goes with --browser chrome") }
    let sites = try a.all("site").map { s -> String in
        let h = s.lowercased()
        guard matches(h, "^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$"), h.contains(".") || h == "localhost" else { throw usageError("--site: a host name like github.com") }
        return h
    }
    let dry = a.has("dry-run")
    try requireTTY("import-passwords")
    installPromptSignalHandlers()
    // The vault first (a dry run doesn't open it), then the source: for Chrome, macOS asks about its key.
    var key: VaultKey?
    var existing: Payload?
    if !dry {
        let (k, p) = try unlockKey(paths)
        key = k
        existing = p
    }
    defer { key?.wipeKey() }
    var summary: ImportSummary
    let source: String
    if let f = a.last("csv") {
        let (s, format) = try csvLogins(f)
        summary = s
        source = "\(format) export \(URL(fileURLWithPath: f).lastPathComponent)"
    } else {
        if !dry { oprint("macOS will ask whether mcomp-vault may use Chrome's \"Chrome Safe Storage\" key: allow it once to read the passwords.") }
        summary = try chromeLogins(profile: profile, decrypt: !dry)
        source = "Chrome (profile \(profile))"
    }
    defer { for i in summary.logins.indices { summary.logins[i].password = "" } }
    if !sites.isEmpty {
        let before = summary.logins.count
        summary.logins = summary.logins.filter { l in
            l.origins.contains { o in let h = originHost(o); return sites.contains { h == $0 || h.hasSuffix("." + $0) } }
        }
        for _ in 0..<(before - summary.logins.count) { summary.skip("not one of the --site hosts") }
    }
    oprint(clean("\(source): \(summary.read) entries read, \(summary.logins.count) logins to bring in", max: 300))
    var taken = Set((existing?.secrets.keys).map(Array.init) ?? [])
    var planned: [(String, String, ImportedLogin, String?, String)] = []   // (status, name, login, totp name, login id)
    for l in summary.logins {
        let o = l.origins[0]
        let match = existing?.secrets.first { $0.value.kind == "password" && $0.value.origins.contains(o) && ($0.value.username ?? "") == l.username }
        var status = "new", name: String
        // The login's own id, which never changes and is never reused (a name is: after a delete, the next import may
        // take it). A login from before ids existed gets one now.
        var loginID = newSecretID()
        if let m = match {
            name = m.key
            status = (m.value.value == l.password) ? "unchanged" : "updated"
            if let i = m.value.id { loginID = i }
        } else {
            name = importName(o, taken)
            taken.insert(name)
        }
        var totpName: String?
        if let seed = l.totp, (try? parseTOTPSeed(seed)) == nil {
            summary.skip("a 2FA seed this vault can't use (only SHA1, 6-digit, 30-second codes); the login itself comes in")
        } else if l.totp != nil {
            // Only the code recorded as this login's own (totp_for: the login's id) is replaced: never another account's,
            // not even one whose login had this name before.
            let mine = (existing?.secrets ?? [:]).filter { $0.value.kind == "totp" && $0.value.totp_for == loginID }.map(\.key).sorted()
            if mine.count > 1 {
                summary.skip("a 2FA seed for a login with more than one 2FA code already recorded as its own (left as they are; the login itself comes in)")
            } else if let t = mine.first {
                totpName = t
            } else {
                totpName = totpImportName(name, taken.union([name]))
                taken.insert(totpName!)
            }
        }
        planned.append((dry ? "would import" : status, name, l, totpName, loginID))
    }
    for (status, name, l, totpName, _) in planned {
        oprint(clean("  \(status.padding(toLength: 12, withPad: " ", startingAt: 0)) \(name)  \(l.origins.joined(separator: " "))"
                     + (l.username.isEmpty ? "" : "  \(maskUsername(l.username))") + (totpName.map { "  + 2FA code as \($0)" } ?? ""), max: 400))
    }
    for (why, n) in summary.skipped.sorted(by: { $0.key < $1.key }) { oprint("  skipped     \(n): \(why)") }
    if dry {
        oprint("Dry run: nothing was read from the passwords themselves, and nothing was saved. Run it again without --dry-run to import (it asks for the vault passphrase).")
        return Exit.ok
    }
    guard let key else { return Exit.failed }
    var counts: [String: Int] = [:], totpBad = 0
    var totpChanges: [(String, String, String, String, String)] = []   // (op, totp name, login name, login id, origin)
    let now = isoTime()
    try updateVault(paths, key) { p in
        for (status, name, l, totpName, loginID) in planned {
            counts[status, default: 0] += 1
            if status == "new" {
                p.secrets[name] = Secret(kind: "password", value: l.password, username: l.username.isEmpty ? nil : l.username, origins: l.origins,
                                         approval: "each", created: now, rotated: nil, last_used: nil, uses: 0, projects: nil,
                                         api_host: nil, api_header: nil, api_path_prefix: nil, gen: randomBytes(8).map { String(format: "%02x", $0) }.joined(),
                                         id: loginID)
            } else if p.secrets[name]?.id == nil {
                p.secrets[name]?.id = loginID
            }
            if status == "updated" {
                p.secrets[name]?.value = l.password
                p.secrets[name]?.rotated = now
                p.secrets[name]?.gen = randomBytes(8).map { String(format: "%02x", $0) }.joined()
            }
            if let seed = l.totp, let tn = totpName {
                guard var bytes = try? parseTOTPSeed(seed) else { totpBad += 1; continue }
                defer { wipe(&bytes) }
                let b32 = base32Encode(bytes)
                if p.secrets[tn] == nil {
                    p.secrets[tn] = Secret(kind: "totp", value: b32, username: nil, origins: [l.origins[0]], approval: "each", created: now,
                                           rotated: nil, last_used: nil, uses: 0, projects: nil, api_host: nil, api_header: nil,
                                           api_path_prefix: nil, gen: randomBytes(8).map { String(format: "%02x", $0) }.joined(),
                                           totp_for: loginID, id: newSecretID())
                    totpChanges.append(("set", tn, name, loginID, l.origins[0]))
                } else if p.secrets[tn]?.kind == "totp", p.secrets[tn]?.totp_for == loginID, p.secrets[tn]?.value != b32 {
                    p.secrets[tn]?.value = b32
                    p.secrets[tn]?.rotated = now
                    p.secrets[tn]?.gen = randomBytes(8).map { String(format: "%02x", $0) }.joined()
                    totpChanges.append(("rotate", tn, name, loginID, l.origins[0]))
                }
            }
        }
    }
    let ak = vaultAuditKey(paths, key)
    for (status, name, l, _, _) in planned where status != "unchanged" {
        var rec: [String: Any] = ["op": status == "new" ? "set" : "rotate", "result": "ok", "name": name, "kind": "password",
                                  "approval_mode": "each", "origins": l.origins, "imported_from": source]
        if !l.username.isEmpty { rec["username_hint"] = maskUsername(l.username) }
        audit(paths, rec, macKey: ak)
    }
    // Every 2FA code made or replaced has its own record, whether or not its login changed.
    for (op, tn, login, loginID, origin) in totpChanges {
        audit(paths, ["op": op, "result": "ok", "name": tn, "kind": "totp", "approval_mode": "each", "origins": [origin],
                      "totp_for": login, "totp_for_id": loginID, "imported_from": source], macKey: ak)
    }
    audit(paths, ["op": "import_passwords", "result": "ok", "source": source, "new": counts["new"] ?? 0, "updated": counts["updated"] ?? 0,
                  "unchanged": counts["unchanged"] ?? 0, "totp_changed": totpChanges.count, "skipped": summary.skipped.values.reduce(0, +)], macKey: ak)
    oprint("Imported \(counts["new"] ?? 0) new, updated \(counts["updated"] ?? 0), left \(counts["unchanged"] ?? 0) as they were"
           + (totpBad > 0 ? "; \(totpBad) 2FA seed(s) skipped (only SHA1, 6-digit, 30-second codes are supported)" : "") + ".")
    oprint("Every imported login asks before each use. `metacomputer secrets policy NAME --approval session` relaxes one; `rule add` allows one project for a while.")
    return Exit.ok
}

// MARK: - a site's session (cookies) from your Chrome

/// One cookie, with its value, as the VM's Chrome will hold it.
func cookieRecord(_ r: [String: Any], value: [UInt8]) -> [String: Any] {
    var c: [String: Any] = [:]
    for k in ["host_key", "name", "path", "top_frame_site_key"] { c[k] = r[k] as? String ?? "" }
    for k in ["expires_utc", "is_secure", "is_httponly", "last_access_utc", "has_expires", "is_persistent", "priority", "samesite",
              "source_scheme", "source_port", "creation_utc", "last_update_utc", "source_type", "has_cross_site_ancestor"] {
        if let v = r[k] as? Int64 { c[k] = v }
    }
    c["value"] = Data(value).base64EncodedString()
    return c
}

/// Does this cookie belong to one of the sites (the site itself, its subdomains, or a domain cookie above it)?
func cookieForSites(_ hostKey: String, _ sites: [String]) -> Bool {
    let domain = hostKey.hasPrefix(".")
    let h = (domain ? String(hostKey.dropFirst()) : hostKey).lowercased()
    return sites.contains { s in h == s || h.hasSuffix("." + s) || (domain && h.contains(".") && s.hasSuffix("." + h)) }
}

/// `import-session --browser chrome --site HOST [--site ...] --name NAME --vm VM --to-profile P [--profile NAME]
///  [--allow-identity-provider] [--replace] [--dry-run]`
func cmdImportSession(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: import-session --browser chrome --site HOST [--site ...] --name NAME --vm VM --to-profile P [--profile NAME] [--allow-identity-provider] [--replace] [--dry-run]"
    let a = try parseArgs(argv, values: ["browser", "app", "profile", "site", "name", "vm", "to-profile"],
                          flags: ["allow-identity-provider", "replace", "dry-run"], repeatable: ["site"])
    guard a.positional.isEmpty else { throw usageError(usage) }
    guard (a.last("browser") ?? a.last("app")) == "chrome" else { throw usageError("--browser chrome (the only browser this imports from)") }
    let profile = a.last("profile") ?? "Default"
    guard validChromeProfile(profile) else { throw usageError("--profile: a Chrome profile folder's name, like Default or \"Profile 1\"") }
    let sites = try a.all("site").map { s -> String in
        let h = s.lowercased()
        guard matches(h, "^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$"), h.contains(".") || h == "localhost" else { throw usageError("--site: a host name like github.com") }
        return h
    }
    guard !sites.isEmpty else { throw usageError("import-session: name each site with --site (there is no 'every site')") }
    let dry = a.has("dry-run")
    guard let name = a.last("name"), validName(name) else { throw usageError("--name: the sealed session's name (letters, digits, '.', '_', '-')") }
    guard let vm = a.last("vm"), validName(vm) else { throw usageError("--vm: the VM it's for (metacomputer secrets fills it in)") }
    guard let target = a.last("to-profile"), validName(target) else { throw usageError("--to-profile: the driver profile in the VM it loads into (browser_prepare's isolated_named name)") }
    try requireTTY("import-session")
    installPromptSignalHandlers()
    var key: VaultKey?
    var existing: Payload?
    if !dry {
        let (k, p) = try unlockKey(paths)
        key = k
        existing = p
        if p.sessions[name] != nil && !a.has("replace") { throw VaultError("a sealed session named \(name) exists: add --replace to overwrite it") }
    }
    defer { key?.wipeKey() }
    let dir = try chromeUserDataDir() + "/" + profile
    let cookiePath = [dir + "/Network/Cookies", dir + "/Cookies"].first(where: fileExists)
    guard let cookiePath else { throw VaultError("no cookie store in Chrome profile \"\(profile)\" (\(tildePath(dir)))") }
    var macKey: [UInt8] = []
    defer { wipe(&macKey) }
    if !dry {
        oprint("macOS will ask whether mcomp-vault may use Chrome's \"Chrome Safe Storage\" key: allow it once to read the cookies.")
        var pw = try chromeSafeStoragePassword()
        defer { wipe(&pw) }
        guard let k = chromeMacKey(pw) else { throw VaultError("key derivation failed") }
        macKey = k
    }
    var cookies: [[String: Any]] = []
    var perSite: [String: Int] = [:]
    var unreadable = 0
    var idpHosts: Set<String> = []
    var relabeled = 0
    try withDatabaseCopy(cookiePath) { db in
        try requireTables(db, ["meta", "cookies"])
        let version = Int(try sqliteRows(db, "SELECT value FROM meta WHERE key='version'").first?["value"] as? String ?? "0") ?? 0
        // From version 24 (Chrome 130) each encrypted value starts with the SHA-256 of its cookie's domain, which is what
        // ties a value to its host_key: without it, a cookie for another site could be relabeled as one of these.
        guard version >= 24 else {
            throw VaultError("Chrome's cookie store in profile \(profile) says it's version \(version); this import needs version 24 or later "
                             + "(Chrome 130 and later), where each cookie's value carries its own domain: update Chrome, open it once, then import")
        }
        for r in try sqliteRows(db, "SELECT * FROM cookies") {
            let hk = r["host_key"] as? String ?? ""
            guard cookieForSites(hk, sites) else { continue }
            perSite[hk, default: 0] += 1
            if cookieReachesIdentityProvider(hk) { idpHosts.insert(hk) }
            if dry { continue }
            var plain: [UInt8]
            if let enc = r["encrypted_value"] as? [UInt8], !enc.isEmpty {
                guard var p = chromeDecrypt(enc, key: macKey) else { unreadable += 1; continue }
                defer { wipe(&p) }
                let h = hexToBytes(sha256Hex(Array(hk.utf8)))
                guard p.count >= 32, Array(p[0..<32]) == h else { relabeled += 1; continue }
                plain = Array(p[32...])
            } else {
                // A value Chrome didn't encrypt was never secret on this Mac: whatever its label, it reveals nothing new.
                plain = Array((r["value"] as? String ?? "").utf8)
            }
            cookies.append(cookieRecord(r, value: plain))
            wipe(&plain)
        }
    }
    guard relabeled == 0 else {
        for i in cookies.indices { cookies[i]["value"] = "" }
        throw VaultError("\(relabeled) cookie\(relabeled == 1 ? "" : "s") in profile \(profile) carr\(relabeled == 1 ? "ies" : "y") another domain inside the encrypted value "
                         + "than the host it's filed under: the store was changed after Chrome wrote it. Nothing was imported")
    }
    let n = perSite.values.reduce(0, +)
    oprint(clean("Chrome (profile \(profile)): \(n) cookies for \(sites.joined(separator: ", "))", max: 300))
    for (h, c) in perSite.sorted(by: { $0.key < $1.key }) { oprint(clean("  \(h)  \(c) cookie\(c == 1 ? "" : "s")", max: 300)) }
    if unreadable > 0 { oprint("  skipped \(unreadable) this program can't open (not Chrome's v10 format)") }
    oprint("  localStorage and IndexedDB: not imported. Chrome keeps them in LevelDB stores that can't be merged into another profile reliably; a site that keeps its sign-in there will need a fresh sign-in in the VM.")
    guard n > 0 else { throw VaultError("Chrome has no cookies for \(sites.joined(separator: ", ")) in profile \(profile)") }
    let idp = !idpHosts.isEmpty
    if idp && !a.has("allow-identity-provider") {
        throw VaultError("these cookies reach an identity provider's sign-in pages (\(idpHosts.sorted().joined(separator: ", "))), which open every site that trusts them. Add --allow-identity-provider if you mean it; such a session asks at every load")
    }
    if dry {
        oprint("Dry run: no cookie was opened and nothing was saved." + (idp ? " (It would reach an identity provider: every load would ask.)" : ""))
        return Exit.ok
    }
    guard let key else { return Exit.failed }
    var payload = Array(jsonLine(["kind": "cookies", "version": 1, "source": "chrome:\(profile)", "sites": sites, "cookies": cookies]).utf8)
    defer { wipe(&payload) }
    for i in cookies.indices { cookies[i]["value"] = "" }
    try ensurePrivateDir(paths.sessions)
    let bytes = payload.count
    let pl = payload
    try updateVault(paths, key) { p in
        guard let d = Data(base64Encoded: p.session_key), d.count == 32 else { throw VaultError("the vault's session key is damaged") }
        try writeFileAtomic(paths.sessions + "/\(name).sealed", try sealGCM(key: Array(d), plaintext: pl, aad: Array("mcomp-session|1|\(name)".utf8)))
        // A replacement starts at `each` again, and keeps an identity provider's mark from what it replaces.
        let keepIdp = idp || p.sessions[name]?.identity_provider == true
        p.sessions[name] = SessionMeta(vm: vm, profile: target, saved_at: isoTime(), bytes: bytes, last_loaded: nil, loads: 0, approval: "each",
                                       identity_provider: keepIdp ? true : nil, kind: "cookies", source: "chrome:\(profile)", sites: sites)
    }
    audit(paths, ["op": "import_session", "result": "ok", "name": name, "vm": vm, "profile": target, "source": "chrome:\(profile)",
                  "sites": sites, "cookies": cookies.count, "identity_provider": idp], macKey: vaultAuditKey(paths, key))
    _ = existing
    oprint("Sealed \(cookies.count) cookies as the session \(name), for profile \(target) on VM \(vm). Every load asks first"
           + (idp ? " (always: it reaches an identity provider)." : " (`session policy \(name) --approval session` relaxes it for that VM)."))
    oprint("Load it with `metacomputer secrets session load \(name)` while no Chrome uses profile \(target); the profile must have been opened once in page-mode Chrome.")
    return Exit.ok
}

// MARK: - loading imported cookies in the VM (as the desktop account)

/// Puts imported cookies into a driver profile's own cookie store: the profile must exist (opened once by
/// the driver) and no Chrome may be using it. It adapts to the store's columns, replaces cookies with the same
/// host, name and path, and refuses a store encrypted with a keyring (v11).
let cookieLoader = #"""
# mcomp-cookie-load: put imported cookies into a driver profile's cookie store.
import json, os, sqlite3, sys
root, name = sys.argv[1], sys.argv[2]
def stop(msg, code=5):
    sys.stderr.write(msg + "\n")
    sys.exit(code)
d = os.path.join(root, name)
if os.path.islink(root) or os.path.islink(d):
    stop("the profile folder is a symbolic link; refusing")
db = None
for rel in ("Default/Network/Cookies", "Default/Cookies"):
    p = os.path.join(d, rel)
    if os.path.isfile(p) and not os.path.islink(p):
        db = p
        break
if db is None:
    stop("the VM has no driver profile " + name + " with a cookie store yet: open it once in page-mode Chrome "
         "(browser_prepare, profile isolated_named " + name + "), end that session, then load again", 6)
try:
    cookies = json.loads(sys.stdin.buffer.read())["cookies"]
    assert isinstance(cookies, list)
except Exception:
    stop("the imported session isn't readable")
con = sqlite3.connect(db, timeout=10, isolation_level=None)
try:
    ver = int((con.execute("SELECT value FROM meta WHERE key='version'").fetchone() or ["0"])[0])
    cols = {r[1]: r for r in con.execute("PRAGMA table_info(cookies)").fetchall()}
    if not {"host_key", "name", "path", "encrypted_value", "value"} <= set(cols):
        stop("this VM's Chrome keeps cookies in a form this loader doesn't know; refusing")
    row = con.execute("SELECT encrypted_value FROM cookies WHERE length(encrypted_value) > 3 LIMIT 1").fetchone()
    if row and bytes(row[0][:3]) != b"v10":
        stop("this VM's Chrome encrypts its cookies with a keyring, not its built-in key; refusing")
    con.execute("BEGIN IMMEDIATE")
    n = 0
    for c in cookies:
        vals = {k: c[k] for k in c if k not in ("enc", "enc_hashed") and k in cols}
        vals["value"] = ""
        vals["encrypted_value"] = bytes.fromhex(c["enc_hashed"] if ver >= 24 else c["enc"])
        for k, r in cols.items():
            if k not in vals and r[3] and r[4] is None and not r[5]:
                t = (r[2] or "").upper()
                vals[k] = "" if "TEXT" in t else (b"" if "BLOB" in t else 0)
        where = ["host_key=?", "name=?", "path=?"]
        args = [vals.get("host_key", ""), vals.get("name", ""), vals.get("path", "")]
        if "top_frame_site_key" in cols:
            where.append("top_frame_site_key=?")
            args.append(vals.get("top_frame_site_key", ""))
        con.execute("DELETE FROM cookies WHERE " + " AND ".join(where), args)
        keys = sorted(vals)
        con.execute("INSERT INTO cookies (" + ",".join('"' + k + '"' for k in keys) + ") VALUES (" + ",".join("?" * len(keys)) + ")",
                    [vals[k] for k in keys])
        n += 1
    con.execute("COMMIT")
except sqlite3.Error as e:
    try:
        con.execute("ROLLBACK")
    except sqlite3.Error:
        pass
    stop("the profile's cookie store couldn't be updated (" + type(e).__name__ + "); nothing changed")
print(json.dumps({"ok": True, "cookies": n}))
"""#

/// Before anyone is asked: does the profile have a cookie store to load into? (exit 6 when it doesn't)
func cookieStoreCheckScript(user: String, profile: String) -> String {
    let root = profileRoot(user), d = root + "/" + profile
    return "# mcomp-cookie-check\n"
        + "[ ! -L '\(root)' ] && [ ! -L '\(d)' ] || { echo 'the profile folder is a symbolic link; refusing' >&2; exit 5; }\n"
        + "[ -f '\(d)/Default/Network/Cookies' ] || [ -f '\(d)/Default/Cookies' ] || { echo 'the VM has no driver profile \(profile) with a cookie store yet: "
        + "open it once in page-mode Chrome (browser_prepare, profile isolated_named \(profile)), end that session, then load again' >&2; exit 6; }\n"
        + "exit 0\n"
}

func cookieLoadScript(user: String, profile: String) -> String {
    let root = profileRoot(user), d = root + "/" + profile
    return "set -e\n"
        + "[ ! -L '\(root)' ] && [ ! -L '\(d)' ] || { echo 'the profile folder is a symbolic link; refusing' >&2; exit 5; }\n"
        + inUseCheck(d, profile)
        + "exec python3 -I -c \(shellQuote(cookieLoader)) '\(root)' '\(profile)'\n"
}

/// The sealed cookies as the VM's loader takes them: each value encrypted with the VM's Chrome key, with and
/// without the domain hash that cookie stores from version 24 put before it. Never leaves this process except on
/// ssh's standard input.
/// With `only`, just the cookies a browser sends to those sites (cookieForSites).
func cookiesForVM(_ sealedPlain: [UInt8], only: [String]? = nil) -> [UInt8]? {
    guard let o = parseJSONObject(sealedPlain), o["kind"] as? String == "cookies", let list = o["cookies"] as? [[String: Any]] else { return nil }
    let key = chromeLinuxKey()
    var out: [[String: Any]] = []
    for c in list {
        if let only, !cookieForSites(c["host_key"] as? String ?? "", only) { continue }
        guard let b64 = c["value"] as? String, var v = Data(base64Encoded: b64).map(Array.init) else { return nil }
        defer { wipe(&v) }
        var r = c
        r["value"] = nil
        let host = c["host_key"] as? String ?? ""
        r["enc"] = sha256HexRaw(chromeEncrypt(v, key: key))
        r["enc_hashed"] = sha256HexRaw(chromeEncrypt(hexToBytes(sha256Hex(Array(host.utf8))) + v, key: key))
        out.append(r)
    }
    return Array(jsonLine(["cookies": out]).utf8)
}

/// How many cookies a cookiesForVM payload carries.
func cookieCount(_ payload: [UInt8]) -> Int {
    (parseJSONObject(payload)?["cookies"] as? [Any])?.count ?? 0
}

/// A host name as `--site` takes it (github.com, localhost).
func validSiteHost(_ s: String) -> Bool {
    let h = s.lowercased()
    return matches(h, "^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$") && (h.contains(".") || h == "localhost")
}
