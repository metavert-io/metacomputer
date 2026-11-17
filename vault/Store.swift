// mcomp-vault: the encrypted vault file.
//
// vault.json (0600) = a header (format, KDF parameters, cipher) and one AES-256-GCM
// sealed payload. The key is PBKDF2-HMAC-SHA256(passphrase, salt, >= 600k rounds);
// the header is the sealed box's additional authenticated data, so a changed
// header fails to open. Saving keeps the salt and rounds (a new nonce each time),
// so an unlocked vault's key stays valid while `set`/`bind` change the file.
import Darwin
import Foundation

struct Secret: Codable {
    var kind: String                // password | totp | text
    var value: String               // totp: the seed in base32
    var username: String?
    var origins: [String]
    var approval: String            // session | each
    var created: String
    var last_used: String?
    var uses: Int
}

struct VMBinding: Codable {
    var ssh_port: Int
    var host_key_line: String       // "TYPE BASE64"
    var ops_key_path: String        // the desktop account: sealed browser sessions
    var guest_user: String
    var fill_key_path: String?      // the fill account (mcfill): fills. nil: bound by an earlier version
    var fill_user: String?
    var gateway_path: String?       // the program that must serve ssh_port (cua-netgw)
    var gateway_sha256: String?     // ...and its contents when bound
    var bound: String
}

struct SessionMeta: Codable {
    var vm: String
    var profile: String
    var saved_at: String
    var bytes: Int
    var last_loaded: String?
    var loads: Int
    var approval: String?           // "session": loads into the same VM don't ask. nil or "each": they do
}

struct Payload: Codable {
    var secrets: [String: Secret]
    var vms: [String: VMBinding]
    var session_key: String         // base64, 32 bytes: seals browser sessions
    var sessions: [String: SessionMeta]
    var created: String

    static func fresh() -> Payload {
        var k = randomBytes(32)
        defer { wipe(&k) }
        return Payload(secrets: [:], vms: [:], session_key: Data(k).base64EncodedString(), sessions: [:], created: isoTime())
    }
}

struct KDFParams: Codable, Equatable {
    var name: String
    var iterations: Int
    var salt: String
}

struct VaultHeader: Codable {
    var format: String
    var version: Int
    var kdf: KDFParams
    var cipher: String
    var sealed: String

    var aad: [UInt8] {
        Array("\(format)|\(version)|\(kdf.name)|\(kdf.iterations)|\(kdf.salt)|\(cipher)".utf8)
    }
}

let vaultFormat = "mcomp-vault"
let vaultVersion = 1

/// The derived key, kept only by the processes that need it; wiped on lock.
final class VaultKey {
    var bytes: [UInt8]
    let kdf: KDFParams
    init(bytes: [UInt8], kdf: KDFParams) {
        self.bytes = bytes
        self.kdf = kdf
    }
    func wipeKey() { wipe(&bytes) }
    deinit { wipe(&bytes) }
}

func newKDF() -> KDFParams {
    KDFParams(name: "pbkdf2-sha256", iterations: kdfIterations, salt: Data(randomBytes(16)).base64EncodedString())
}

func deriveKey(_ passphrase: [UInt8], _ kdf: KDFParams) throws -> VaultKey {
    guard kdf.name == "pbkdf2-sha256", kdf.iterations >= kdfMinIterations else {
        throw VaultError("the vault file's key settings are weaker than this program accepts; refusing")
    }
    guard kdf.iterations <= kdfMaxIterations else {
        throw VaultError("the vault file's key settings are out of range (\(kdf.iterations) rounds); refusing")
    }
    guard let salt = Data(base64Encoded: kdf.salt), salt.count >= 16 else { throw VaultError("the vault file is damaged (salt)") }
    guard let k = pbkdf2SHA256(password: passphrase, salt: Array(salt), iterations: kdf.iterations) else {
        throw VaultError("key derivation failed")
    }
    return VaultKey(bytes: k, kdf: kdf)
}

func vaultExists(_ paths: Paths) -> Bool { fileExists(paths.vault) }

func readHeader(_ paths: Paths) throws -> VaultHeader {
    var st = stat()
    guard lstat(paths.vault, &st) == 0 else {
        throw VaultError("no vault yet at \(paths.vault) (run: metacomputer secrets init)")
    }
    guard (st.st_mode & S_IFMT) == S_IFREG, st.st_uid == getuid() else {
        throw VaultError("\(paths.vault) isn't a plain file of yours; refusing")
    }
    if st.st_mode & 0o077 != 0 { chmod(paths.vault, 0o600) }
    let bytes = try readFileBytes(paths.vault, max: 64 << 20)
    guard let h = try? JSONDecoder().decode(VaultHeader.self, from: Data(bytes)),
          h.format == vaultFormat, h.version == vaultVersion, h.cipher == "aes-256-gcm" else {
        throw VaultError("\(paths.vault) isn't a vault this program understands")
    }
    return h
}

func decryptPayload(_ header: VaultHeader, _ key: VaultKey) throws -> Payload {
    guard header.kdf == key.kdf else { throw VaultError("the vault file changed its key settings; unlock again", code: Exit.locked) }
    guard let sealed = Data(base64Encoded: header.sealed) else { throw VaultError("the vault file is damaged") }
    var plain: [UInt8]
    do {
        plain = try openGCM(key: key.bytes, combined: Array(sealed), aad: header.aad)
    } catch {
        throw VaultError("wrong passphrase (or the vault file was changed)")
    }
    defer { wipe(&plain) }
    guard let p = try? JSONDecoder().decode(Payload.self, from: Data(plain)) else {
        throw VaultError("the vault's contents are damaged")
    }
    return p
}

func loadPayload(_ paths: Paths, _ key: VaultKey) throws -> Payload {
    try decryptPayload(try readHeader(paths), key)
}

func savePayload(_ paths: Paths, _ key: VaultKey, _ payload: Payload) throws {
    let enc = JSONEncoder()
    enc.outputFormatting = [.sortedKeys]
    var plain = Array(try enc.encode(payload))
    defer { wipe(&plain) }
    var header = VaultHeader(format: vaultFormat, version: vaultVersion, kdf: key.kdf, cipher: "aes-256-gcm", sealed: "")
    header.sealed = Data(try sealGCM(key: key.bytes, plaintext: plain, aad: header.aad)).base64EncodedString()
    let henc = JSONEncoder()
    henc.outputFormatting = [.sortedKeys, .prettyPrinted]
    try writeFileAtomic(paths.vault, Array(try henc.encode(header)) + [0x0A])
}

/// Read, change and write the vault under its lock.
func updateVault(_ paths: Paths, _ key: VaultKey, _ change: (inout Payload) throws -> Void) throws {
    try withFileLock(paths.lock) {
        var p = try loadPayload(paths, key)
        try change(&p)
        try savePayload(paths, key, p)
    }
}

/// "ssh-ed25519 AAAA..." from a known_hosts line ("[127.0.0.1]:2277 ssh-ed25519 AAAA...")
/// or a .pub line; nil when it isn't a public key.
func normalizeHostKey(_ line: String) -> String? {
    let types: Set<String> = ["ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa"]
    let parts = line.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
    guard let i = parts.firstIndex(where: { types.contains($0) }), i + 1 < parts.count, i <= 1 else { return nil }
    let type = parts[i], b64 = parts[i + 1]
    guard let blob = Data(base64Encoded: b64), blob.count > 8 else { return nil }
    let b = Array(blob)
    let n = Int(b[0]) << 24 | Int(b[1]) << 16 | Int(b[2]) << 8 | Int(b[3])
    guard n > 0, 4 + n <= b.count, String(decoding: b[4..<(4 + n)], as: UTF8.self) == type else { return nil }
    return type + " " + b64
}

/// SHA256:... as ssh-keygen -l prints it.
func hostKeyFingerprint(_ normalized: String) -> String {
    let parts = normalized.split(separator: " ")
    guard parts.count >= 2, let blob = Data(base64Encoded: String(parts[1])) else { return "?" }
    var b64 = Data(hexToBytes(sha256Hex(Array(blob)))).base64EncodedString()
    while b64.hasSuffix("=") { b64.removeLast() }
    return "SHA256:" + b64
}

func hexToBytes(_ hex: String) -> [UInt8] {
    var out = [UInt8]()
    var it = hex.makeIterator()
    while let a = it.next(), let b = it.next() {
        if let v = UInt8(String([a, b]), radix: 16) { out.append(v) }
    }
    return out
}
