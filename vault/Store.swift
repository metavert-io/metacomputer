// mcomp-vault: the encrypted vault file.
//
// vault.json (0600) = a header and one AES-256-GCM sealed payload.
//
// Format 2 (envelope encryption, like a KMS data key):
//  - a random 32-byte data key seals the payload;
//  - the data key is sealed ("wrapped") under a key-encryption key from your passphrase:
//    PBKDF2-HMAC-SHA256(passphrase, salt, >= 600k rounds), and, when the vault uses the
//    Secure Enclave, also an ECDH secret between a Secure Enclave key (whose blob only
//    this Mac's Enclave can use) and a stored peer public key, combined with HKDF;
//  - with the Enclave, the data key is also wrapped under a one-time recovery key, which
//    opens the vault on any Mac (`recover`).
// Format 1 (earlier builds): the passphrase key seals the payload directly. It still
// opens; `rekey` converts it.
// Saving keeps the header's key settings (a new nonce each time), so an unlocked vault's
// key stays valid while `set`/`bind` change the file.
import CryptoKit
import Darwin
import Foundation

struct Secret: Codable {
    var kind: String                // password | totp | text | api
    var value: String               // totp: the seed in base32
    var username: String?
    var origins: [String]           // password/totp/text: the sites it fills into
    var approval: String            // session | each
    var created: String
    var rotated: String?            // when the value was last replaced (set --replace)
    var last_used: String?
    var uses: Int
    var projects: [String]?         // only for requests from Claude Code sessions in these folders
    var api_host: String?           // api: the host the proxy forwards to (host[:port])
    var api_header: String?         // api: "Name: template with {value}"
    var api_path_prefix: String?    // api: only paths under this
    var gen: String? = nil          // random, new with every set: API tokens are bound to it
    var totp_for: String? = nil     // totp: the `id` of the password secret whose account this code belongs to (import-passwords)
    var id: String? = nil           // random, made once and kept through --replace: what a 2FA code's totp_for names
    var api_answer_timeout_s: Int? = nil   // api: how long one proxied answer may take (nil: the default, 600 s)
}

func newSecretID() -> String { randomBytes(8).map { String(format: "%02x", $0) }.joined() }

/// A secret's generation: tokens made for an earlier value or policy don't fit a newer one.
func secretGen(_ s: Secret) -> String { s.gen ?? "\(s.created)|\(s.rotated ?? "-")" }

struct VMBinding: Codable {
    var ssh_port: Int
    var host_key_line: String       // "TYPE BASE64"
    var ops_key_path: String        // the desktop account: sealed browser sessions
    var guest_user: String
    var fill_key_path: String?      // the fill account (mcfill): fills. nil: bound by an earlier version
    var fill_user: String?
    var proxy_key_path: String?     // the proxy account (mcproxy): api tokens. nil: none on this VM
    var proxy_user: String?
    var proxy_port: Int?            // where the proxy listens inside the VM (127.0.0.1)
    var gateway_path: String?       // the program that must serve ssh_port (mcomp-netgw)
    var gateway_sha256: String?     // ...and its contents when bound
    var bound: String
    // A computer reached over SSH elsewhere (docs/remote.md): its address (nil: 127.0.0.1, through the gateway above),
    // with ssh_port its SSH port there, and the name its host key is pinned under (mcomp-NAME, never the address).
    // Such a binding has no gateway: the pinned host key is what's checked.
    var ssh_host: String? = nil
    var host_key_alias: String? = nil
    // ...and the computer it is (its instance or setup and its host key, hashed: lib/remote.py identity), as the
    // computer-id file beside its keys said when it was bound. A computer deleted, or made again under the same
    // name, has another: the binding then refuses until it's bound again.
    var computer_id: String? = nil
    // A computer with no address of its own (Fly Sprites): the program that carries its SSH (sprite proxy -W :22), its
    // contents pinned at bind like the gateway's, and its arguments. ssh runs it as its ProxyCommand through the vault
    // itself (`mcomp-vault _tunnel`), which ends it when ssh goes. nil: ssh connects straight to ssh_host.
    var tunnel_program: String? = nil
    var tunnel_sha256: String? = nil
    var tunnel_args: [String]? = nil
}

extension VMBinding {
    /// Where ssh connects: the computer's own address, or this Mac's loopback (the gateway's forward).
    var sshDestination: String { ssh_host ?? "127.0.0.1" }
    var isRemote: Bool { ssh_host != nil }
    var hasTunnel: Bool { tunnel_program != nil }
}

/// One argument of a tunnel program, as pinned: nothing a shell or ssh's ProxyCommand would read as anything but the
/// word itself (no spaces, quotes, `%`, `$` or other specials).
func validTunnelArg(_ s: String) -> Bool { matches(s, "^[A-Za-z0-9._:/@=+_-]{1,200}$") }

/// A computer's address for a binding: an IPv4 or IPv6 address or a DNS name, nothing that could be an option or a
/// user@ (it goes on ssh's command line after `user@`).
func validHost(_ s: String) -> Bool {
    guard !s.hasPrefix("-"), s.count <= 253 else { return false }
    if matches(s, "^[0-9]{1,3}(\\.[0-9]{1,3}){3}$") { return true }
    if matches(s, "^[0-9A-Fa-f:]{2,39}$") && s.contains(":") { return true }
    return matches(s, "^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
        && !matches(s, "^[0-9.]+$")
}

/// A computer elsewhere's identity file, beside its keys (lib/remote.py identity writes it; vm delete removes it).
func computerIDPath(opsKey: String) -> String { (opsKey as NSString).deletingLastPathComponent + "/computer-id" }

/// The identity in that file: 64 hex digits on one line, or nil.
func readComputerID(_ path: String) -> String? {
    guard let bytes = try? readFileBytes(path, max: 256) else { return nil }
    let s = String(decoding: bytes, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    return matches(s, "^[0-9a-f]{64}$") ? s : nil
}

/// The name a remote computer's host key is pinned under (scripts/lib.sh mc_hostkey_name).
func validHostKeyAlias(_ s: String) -> Bool { matches(s, "^mcomp-[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$") }

struct SessionMeta: Codable {
    var vm: String
    var profile: String
    var saved_at: String
    var bytes: Int
    var last_loaded: String?
    var loads: Int
    var approval: String?           // "session": loads into the same VM don't ask. nil or "each": they do
    var identity_provider: Bool? = nil   // holds an identity provider's sign-in: every load asks, whatever the policy
    var kind: String? = nil         // nil: a Chrome profile saved from a VM (a tar.gz); "cookies": a site's cookies imported from this Mac's Chrome
    var source: String? = nil       // cookies: where they came from ("chrome:Default")
    var sites: [String]? = nil      // cookies: the sites named at import
}

extension SessionMeta {
    /// Whether every load asks, whatever the policy, grants or rules: a session that holds an identity provider's
    /// sign-in, and a whole Chrome profile saved from a VM, which may hold any sign-in (an identity provider's
    /// included), so it's treated as one. Only imported cookies, whose sites are known, can be relaxed.
    var alwaysAsks: Bool { identity_provider == true || kind != "cookies" }
}

/// Somewhere off this Mac that receives every audit event (Sinks.swift).
struct AuditSink: Codable {
    var kind: String                // webhook | otlp | syslog | ntfy
    var url: String
    var headers: [[String]]         // [name, value]; values are never shown again
    var required: Bool              // fail closed: no uses while it can't be reached
    var hmac_key: String            // base64, 32 bytes
    var ca_pem: String?             // syslog: trust only this CA
    var created: String
    var start_seq: Int              // events after this one are sent
}

struct Payload: Codable {
    var secrets: [String: Secret]
    var vms: [String: VMBinding]
    var session_key: String         // base64, 32 bytes: seals browser sessions
    var session_key_prev: String?   // an earlier format's single previous key (read, never written)
    var session_key_rotated: String?
    var sessions: [String: SessionMeta]
    var audit_sinks: [String: AuditSink]?
    var created: String
    var session_keys_old: [String]? = nil   // every earlier session key a sealed file may still use (rotation cut short)
    var audit_key: String? = nil            // base64, 32 bytes: the MAC on each audit record and on sink states
    // The last audit seq written before the audit key existed (a vault from an earlier version,
    // or a log older than this vault): records up to it are legacy, unverified, not forgeries.
    var audit_key_since: Int? = nil
    var rules: [UnattendedRule]? = nil      // unattended rules (Approvals.swift): uses that don't ask, until they expire
    var require_presence: String? = nil     // "on" | "off": the unlock's presence mode when no flag says; nil: on where Touch ID is
    var biometry_seen: String? = nil        // when an unlock first found Touch ID enrolled: presence mode then stays on by default

    /// A new vault. `boundary`: the audit log's last seq now (records before the vault are legacy).
    static func fresh(boundary: Int) -> Payload {
        var k = randomBytes(32)
        var a = randomBytes(32)
        defer { wipe(&k); wipe(&a) }
        return Payload(secrets: [:], vms: [:], session_key: Data(k).base64EncodedString(), session_key_prev: nil,
                       session_key_rotated: nil, sessions: [:], audit_sinks: nil, created: isoTime(),
                       session_keys_old: nil, audit_key: Data(a).base64EncodedString(), audit_key_since: boundary)
    }

    /// Records up to this seq were written before the audit key: no MAC is expected on them.
    var legacyThrough: Int { audit_key_since ?? 0 }

    /// Every session key a sealed file may use: the current one first.
    var sessionKeyring: [[UInt8]] {
        var out: [[UInt8]] = []
        for k in [session_key] + (session_keys_old ?? []) + (session_key_prev.map { [$0] } ?? []) {
            if let d = Data(base64Encoded: k), d.count == 32, !out.contains(Array(d)) { out.append(Array(d)) }
        }
        return out
    }

    /// The audit MAC key, made if this vault predates it, with the migration boundary: the
    /// log's last seq now. Returns true when it was made.
    @discardableResult
    mutating func ensureAuditKey(boundary: Int) -> Bool {
        guard audit_key.flatMap({ Data(base64Encoded: $0) })?.count != 32 else { return false }
        var a = randomBytes(32)
        defer { wipe(&a) }
        audit_key = Data(a).base64EncodedString()
        audit_key_since = boundary
        return true
    }

    var auditKeyBytes: [UInt8]? { audit_key.flatMap { Data(base64Encoded: $0) }.map(Array.init).flatMap { $0.count == 32 ? $0 : nil } }
}

struct KDFParams: Codable, Equatable {
    var name: String
    var iterations: Int
    var salt: String
}

struct EnclaveParams: Codable, Equatable {
    var key: String                 // base64: the Secure Enclave key's blob (usable only by this Mac's Enclave)
    var peer: String                // base64: a P-256 public key (x9.63); its private half was thrown away
}

struct KeyWrap: Codable, Equatable {
    var kind: String                // passphrase | recovery
    var sealed: String              // base64: the data key, AES-256-GCM
}

struct VaultHeader: Codable {
    var format: String
    var version: Int
    var kdf: KDFParams
    var cipher: String
    var sealed: String
    var enclave: EnclaveParams?
    var wraps: [KeyWrap]?
    var created: String?

    /// v1: the payload's AAD. v2: what every wrap and the payload are bound to.
    var params: String {
        "\(format)|\(version)|\(kdf.name)|\(kdf.iterations)|\(kdf.salt)|\(cipher)"
            + (version >= 2 ? "|enclave=\(enclave?.key ?? "-")|peer=\(enclave?.peer ?? "-")" : "")
    }
    var payloadAAD: [UInt8] { Array((version >= 2 ? params + "|payload" : params).utf8) }
    func wrapAAD(_ kind: String) -> [UInt8] { Array((params + "|wrap|" + kind).utf8) }

    /// The parts that must stay the same for an unlocked key to still fit.
    func sameKey(_ o: VaultHeader) -> Bool { version == o.version && kdf == o.kdf && enclave == o.enclave && wraps == o.wraps }
}

let vaultFormat = "mcomp-vault"
let vaultVersion = 2

/// The payload key (format 2: the data key), and the header it belongs with. Kept only by
/// the processes that need it; wiped on lock.
final class VaultKey: @unchecked Sendable {
    var bytes: [UInt8]
    let template: VaultHeader
    init(bytes: [UInt8], template: VaultHeader) {
        self.bytes = bytes
        var t = template
        t.sealed = ""
        self.template = t
    }
    var usesEnclave: Bool { template.enclave != nil }
    var hasRecovery: Bool { template.wraps?.contains { $0.kind == "recovery" } ?? false }
    func wipeKey() { wipe(&bytes) }
    deinit { wipe(&bytes) }
}

func newKDF() -> KDFParams {
    KDFParams(name: "pbkdf2-sha256", iterations: kdfIterations, salt: Data(randomBytes(16)).base64EncodedString())
}

/// PBKDF2 of the passphrase with the header's settings (refusing weak or absurd ones).
func passphraseKey(_ passphrase: [UInt8], _ kdf: KDFParams) throws -> [UInt8] {
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
    return k
}

// MARK: - Secure Enclave

func enclaveAvailable() -> Bool { SecureEnclave.isAvailable }

/// A new Enclave key and the peer it agrees with: the parameters to store, and the secret.
func newEnclave() throws -> (EnclaveParams, [UInt8]) {
    let se: SecureEnclave.P256.KeyAgreement.PrivateKey
    do { se = try SecureEnclave.P256.KeyAgreement.PrivateKey() } catch {
        throw VaultError("the Secure Enclave refused to make a key (\(error)); use --no-enclave")
    }
    let peer = P256.KeyAgreement.PrivateKey()
    let shared = try se.sharedSecretFromKeyAgreement(with: peer.publicKey)
    let secret = shared.withUnsafeBytes { Array($0) }
    return (EnclaveParams(key: se.dataRepresentation.base64EncodedString(), peer: peer.publicKey.x963Representation.base64EncodedString()), secret)
}

/// The ECDH secret from this Mac's Enclave; fails on another Mac, or after the Enclave was reset.
func enclaveSecret(_ e: EnclaveParams) throws -> [UInt8] {
    guard let blob = Data(base64Encoded: e.key), let peerData = Data(base64Encoded: e.peer),
          let peer = try? P256.KeyAgreement.PublicKey(x963Representation: peerData) else {
        throw VaultError("the vault file's Enclave settings are damaged")
    }
    do {
        #if MCOMP_VAULT_TESTING
        // Stands in for another Mac or a reset Enclave (the blob is intact but can't be used).
        if ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_NO_ENCLAVE"] == "1" { throw VaultError("test: no Enclave") }
        #endif
        let se = try SecureEnclave.P256.KeyAgreement.PrivateKey(dataRepresentation: blob)
        return try se.sharedSecretFromKeyAgreement(with: peer).withUnsafeBytes { Array($0) }
    } catch {
        throw VaultError("this vault is bound to a Secure Enclave this Mac doesn't have (another Mac, or a reset Enclave): open it with `metacomputer secrets recover` and the recovery key")
    }
}

func hkdf32(_ ikm: [UInt8], salt: [UInt8], info: [UInt8]) -> [UInt8] {
    let k = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: ikm), salt: salt, info: info, outputByteCount: 32)
    return k.withUnsafeBytes { Array($0) }
}

func hkdf32(_ ikm: [UInt8], salt: [UInt8], info: String) -> [UInt8] { hkdf32(ikm, salt: salt, info: Array(info.utf8)) }

/// The key that wraps the data key, from the passphrase (and the Enclave when the vault uses it).
func passphraseKEK(_ passphrase: [UInt8], _ h: VaultHeader) throws -> [UInt8] {
    var p = try passphraseKey(passphrase, h.kdf)
    defer { wipe(&p) }
    let salt = Array(Data(base64Encoded: h.kdf.salt) ?? Data())
    if let e = h.enclave {
        var s = try enclaveSecret(e)
        defer { wipe(&s) }
        return hkdf32(p + s, salt: salt, info: "mcomp-vault|2|passphrase+enclave")
    }
    return hkdf32(p, salt: salt, info: "mcomp-vault|2|passphrase")
}

func recoveryKEK(_ recovery: [UInt8], _ h: VaultHeader) -> [UInt8] {
    hkdf32(recovery, salt: Array(Data(base64Encoded: h.kdf.salt) ?? Data()), info: "mcomp-vault|2|recovery")
}

/// A recovery key: 20 random bytes as 8 groups of 4 base32 characters.
func newRecoveryKey() -> ([UInt8], String) {
    let b = randomBytes(20)
    let s = base32Encode(b)
    let groups = stride(from: 0, to: s.count, by: 4).map { i -> String in
        let a = s.index(s.startIndex, offsetBy: i), z = s.index(a, offsetBy: min(4, s.count - i))
        return String(s[a..<z])
    }
    return (b, groups.joined(separator: "-"))
}

func parseRecoveryKey(_ s: String) -> [UInt8]? {
    guard let b = base32Decode(s.replacingOccurrences(of: "-", with: "").replacingOccurrences(of: " ", with: "")), b.count == 20 else { return nil }
    return b
}

// MARK: - reading and writing

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
          h.format == vaultFormat, h.version == 1 || h.version == 2, h.cipher == "aes-256-gcm" else {
        throw VaultError("\(paths.vault) isn't a vault this program understands")
    }
    if h.version == 2 && (h.wraps ?? []).isEmpty { throw VaultError("the vault file is damaged (no key wraps)") }
    return h
}

func unwrapDataKey(_ h: VaultHeader, kind: String, kek: [UInt8]) throws -> [UInt8] {
    guard let w = h.wraps?.first(where: { $0.kind == kind }), let sealed = Data(base64Encoded: w.sealed) else {
        throw VaultError(kind == "recovery" ? "this vault has no recovery key" : "the vault file is damaged (no \(kind) wrap)")
    }
    do { return try openGCM(key: kek, combined: Array(sealed), aad: h.wrapAAD(kind)) } catch {
        throw VaultError(kind == "recovery" ? "wrong recovery key (or the vault file was changed)" : "wrong passphrase (or the vault file was changed)")
    }
}

/// Open the vault with the passphrase: the key for its payload.
func openWithPassphrase(_ paths: Paths, _ passphrase: [UInt8]) throws -> (VaultKey, Payload) {
    let h = try readHeader(paths)
    let key: VaultKey
    if h.version == 1 {
        key = VaultKey(bytes: try passphraseKey(passphrase, h.kdf), template: h)
    } else {
        var kek = try passphraseKEK(passphrase, h)
        defer { wipe(&kek) }
        key = VaultKey(bytes: try unwrapDataKey(h, kind: "passphrase", kek: kek), template: h)
    }
    return (key, try decryptPayload(h, key))
}

func openWithRecovery(_ paths: Paths, _ recovery: [UInt8]) throws -> (VaultKey, Payload) {
    let h = try readHeader(paths)
    guard h.version == 2 else { throw VaultError("this vault has no recovery key") }
    var kek = recoveryKEK(recovery, h)
    defer { wipe(&kek) }
    let key = VaultKey(bytes: try unwrapDataKey(h, kind: "recovery", kek: kek), template: h)
    return (key, try decryptPayload(h, key))
}

/// A new format-2 key setup: a fresh data key wrapped under the passphrase (with the
/// Enclave if asked), and a recovery key when the Enclave is used. Returns the key and
/// the recovery key's text (to show once), if any.
func newVaultKey(passphrase: [UInt8], enclave: Bool) throws -> (VaultKey, String?) {
    var h = VaultHeader(format: vaultFormat, version: vaultVersion, kdf: newKDF(), cipher: "aes-256-gcm", sealed: "",
                        enclave: nil, wraps: [], created: isoTime())
    if enclave {
        var (params, secret) = try newEnclave()
        wipe(&secret)
        h.enclave = params
    }
    var dk = randomBytes(32)
    defer { wipe(&dk) }
    var kek = try passphraseKEK(passphrase, h)
    defer { wipe(&kek) }
    var wraps = [KeyWrap(kind: "passphrase", sealed: Data(try sealGCM(key: kek, plaintext: dk, aad: h.wrapAAD("passphrase"))).base64EncodedString())]
    var recoveryText: String?
    if enclave {
        var (rk, text) = newRecoveryKey()
        defer { wipe(&rk) }
        var rkek = recoveryKEK(rk, h)
        defer { wipe(&rkek) }
        wraps.append(KeyWrap(kind: "recovery", sealed: Data(try sealGCM(key: rkek, plaintext: dk, aad: h.wrapAAD("recovery"))).base64EncodedString()))
        recoveryText = text
    }
    h.wraps = wraps
    return (VaultKey(bytes: dk, template: h), recoveryText)
}

func decryptPayload(_ header: VaultHeader, _ key: VaultKey) throws -> Payload {
    guard header.sameKey(key.template) else {
        throw VaultError("the vault file changed its key settings (rekey or recover); unlock again", code: Exit.locked)
    }
    guard let sealed = Data(base64Encoded: header.sealed) else { throw VaultError("the vault file is damaged") }
    var plain: [UInt8]
    do {
        plain = try openGCM(key: key.bytes, combined: Array(sealed), aad: header.payloadAAD)
    } catch {
        throw VaultError(header.version == 1 ? "wrong passphrase (or the vault file was changed)" : "the vault file was changed")
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
    var header = key.template
    header.sealed = Data(try sealGCM(key: key.bytes, plaintext: plain, aad: header.payloadAAD)).base64EncodedString()
    let henc = JSONEncoder()
    henc.outputFormatting = [.sortedKeys, .prettyPrinted]
    try writeFileAtomic(paths.vault, Array(try henc.encode(header)) + [0x0A])
}

/// Read, change and write the vault under its lock.
func updateVault(_ paths: Paths, _ key: VaultKey, _ change: (inout Payload) throws -> Void) throws {
    try withFileLock(paths.lock) {
        var p = try loadPayload(paths, key)
        let made = p.ensureAuditKey(boundary: lastAuditEvent(paths.audit).0)
        try change(&p)
        try savePayload(paths, key, p)
        if made, let k = p.auditKeyBytes { adoptSinkStates(paths, p, key: k) }
    }
}

/// When an audit key is first made, the sink places saved by the earlier version (which had no
/// MAC) are taken as they are, once, and signed: from then on a changed one is noticed.
func adoptSinkStates(_ paths: Paths, _ p: Payload, key: [UInt8]) {
    for (name, sink) in p.audit_sinks ?? [:] {
        let (st, _) = loadSinkState(paths, name, sink)
        saveSinkState(paths, name, st, key: key)
    }
}

/// The audit MAC key of an open vault (made and saved first if the vault predates it).
func vaultAuditKey(_ paths: Paths, _ key: VaultKey) -> [UInt8]? {
    if let k = (try? loadPayload(paths, key))?.auditKeyBytes { return k }
    try? updateVault(paths, key) { _ in }
    return (try? loadPayload(paths, key))?.auditKeyBytes
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
