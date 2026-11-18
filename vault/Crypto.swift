// mcomp-vault: cryptography. CryptoKit for AES-256-GCM, CommonCrypto for
// PBKDF2-HMAC-SHA256 and HMAC-SHA1 (TOTP), Security for random bytes.
import CommonCrypto
import CryptoKit
import Darwin
import Foundation
import Security

let kdfIterations = 600_000
let kdfMinIterations = 600_000
let kdfMaxIterations = 50_000_000      // a damaged or hostile header can't make unlock hang or crash

func randomBytes(_ n: Int) -> [UInt8] {
    var b = [UInt8](repeating: 0, count: n)
    let rc = b.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, n, $0.baseAddress!) }
    precondition(rc == errSecSuccess, "no random bytes")
    return b
}

/// Overwrite a buffer before letting it go (as far as Swift allows: copies made by
/// Foundation or the runtime are out of reach; the process exits when the vault locks).
func wipe(_ a: inout [UInt8]) {
    let n = a.count
    a.withUnsafeMutableBytes { p in
        if let base = p.baseAddress, n > 0 { _ = memset_s(base, n, 0, n) }
    }
    a.removeAll()
}

func wipe(_ d: inout Data) {
    let n = d.count
    d.withUnsafeMutableBytes { p in
        if let base = p.baseAddress, n > 0 { _ = memset_s(base, n, 0, n) }
    }
    d = Data()
}

func pbkdf2SHA256(password: [UInt8], salt: [UInt8], iterations: Int, length: Int = 32) -> [UInt8]? {
    guard !password.isEmpty, !salt.isEmpty, iterations > 0, iterations <= Int(UInt32.max) else { return nil }
    var out = [UInt8](repeating: 0, count: length)
    let rc = password.withUnsafeBytes { pw in
        salt.withUnsafeBytes { s in
            out.withUnsafeMutableBytes { o in
                CCKeyDerivationPBKDF(CCPBKDFAlgorithm(kCCPBKDF2),
                                     pw.baseAddress!.assumingMemoryBound(to: CChar.self), pw.count,
                                     s.baseAddress!.assumingMemoryBound(to: UInt8.self), s.count,
                                     CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA256), UInt32(iterations),
                                     o.baseAddress!.assumingMemoryBound(to: UInt8.self), length)
            }
        }
    }
    return rc == kCCSuccess ? out : nil
}

func hmacSHA1(key: [UInt8], data: [UInt8]) -> [UInt8] {
    var out = [UInt8](repeating: 0, count: Int(CC_SHA1_DIGEST_LENGTH))
    key.withUnsafeBytes { k in
        data.withUnsafeBytes { d in
            out.withUnsafeMutableBytes { o in
                CCHmac(CCHmacAlgorithm(kCCHmacAlgSHA1), k.baseAddress, k.count, d.baseAddress, d.count, o.baseAddress)
            }
        }
    }
    return out
}

func sha256Hex(_ bytes: [UInt8]) -> String {
    SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
}

/// HMAC-SHA256 as lowercase hex (audit sink signatures).
func hmacSHA256Hex(key: [UInt8], data: [UInt8]) -> String {
    HMAC<SHA256>.authenticationCode(for: data, using: SymmetricKey(data: key)).map { String(format: "%02x", $0) }.joined()
}

func base64url(_ bytes: [UInt8]) -> String {
    Data(bytes).base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

func sealGCM(key: [UInt8], plaintext: [UInt8], aad: [UInt8]) throws -> [UInt8] {
    let box = try AES.GCM.seal(plaintext, using: SymmetricKey(data: key), nonce: AES.GCM.Nonce(), authenticating: aad)
    guard let combined = box.combined else { throw VaultError("encryption failed") }
    return Array(combined)
}

func openGCM(key: [UInt8], combined: [UInt8], aad: [UInt8]) throws -> [UInt8] {
    let box = try AES.GCM.SealedBox(combined: combined)
    return Array(try AES.GCM.open(box, using: SymmetricKey(data: key), authenticating: aad))
}

// MARK: - TOTP (RFC 6238 over RFC 4226; HMAC-SHA1)

func hotp(secret: [UInt8], counter: UInt64, digits: Int) -> String {
    var c = counter.bigEndian
    let msg = withUnsafeBytes(of: &c) { Array($0) }
    let h = hmacSHA1(key: secret, data: msg)
    let off = Int(h[h.count - 1] & 0x0F)
    let bin = (UInt32(h[off] & 0x7F) << 24) | (UInt32(h[off + 1]) << 16) | (UInt32(h[off + 2]) << 8) | UInt32(h[off + 3])
    var mod: UInt32 = 1
    for _ in 0..<digits { mod *= 10 }
    let s = String(bin % mod)
    return String(repeating: "0", count: max(0, digits - s.count)) + s
}

func totp(secret: [UInt8], time: UInt64, step: UInt64 = 30, digits: Int = 6) -> String {
    hotp(secret: secret, counter: time / step, digits: digits)
}

// MARK: - base32 (RFC 4648)

private let base32Alphabet = Array("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567".utf8)

/// Spaces, dashes and padding are ignored; letters may be lower case. nil on anything else.
func base32Decode(_ s: String) -> [UInt8]? {
    var out = [UInt8]()
    var acc: UInt32 = 0
    var bits = 0
    for ch in s.uppercased().utf8 {
        if ch == UInt8(ascii: " ") || ch == UInt8(ascii: "-") || ch == UInt8(ascii: "=") { continue }
        guard let v = base32Alphabet.firstIndex(of: ch) else { return nil }
        acc = (acc << 5) | UInt32(v)
        bits += 5
        if bits >= 8 {
            bits -= 8
            out.append(UInt8((acc >> UInt32(bits)) & 0xFF))
        }
    }
    return out
}

func base32Encode(_ bytes: [UInt8]) -> String {
    var out = [UInt8]()
    var acc: UInt32 = 0
    var bits = 0
    for b in bytes {
        acc = (acc << 8) | UInt32(b)
        bits += 8
        while bits >= 5 {
            bits -= 5
            out.append(base32Alphabet[Int((acc >> UInt32(bits)) & 0x1F)])
        }
    }
    if bits > 0 { out.append(base32Alphabet[Int((acc << UInt32(5 - bits)) & 0x1F)]) }
    return String(decoding: out, as: UTF8.self)
}

/// A TOTP seed from a base32 string or an otpauth://totp/ URI. Only the usual
/// settings (SHA1, 6 digits, 30 s) are supported.
func parseTOTPSeed(_ input: String) throws -> [UInt8] {
    var seed = input.trimmingCharacters(in: .whitespacesAndNewlines)
    if seed.lowercased().hasPrefix("otpauth://") {
        guard let u = URLComponents(string: seed), u.host?.lowercased() == "totp" else {
            throw VaultError("not an otpauth://totp/ address")
        }
        var params: [String: String] = [:]
        for q in u.queryItems ?? [] { params[q.name.lowercased()] = q.value ?? "" }
        if let a = params["algorithm"], a.uppercased() != "SHA1" { throw VaultError("only SHA1 codes are supported (this one uses \(a))") }
        if let d = params["digits"], d != "6" { throw VaultError("only 6-digit codes are supported (this one has \(d))") }
        if let p = params["period"], p != "30" { throw VaultError("only 30-second codes are supported (this one uses \(p) s)") }
        guard let s = params["secret"], !s.isEmpty else { throw VaultError("the otpauth address has no secret") }
        seed = s
    }
    guard let bytes = base32Decode(seed) else { throw VaultError("not a base32 seed (letters A-Z and digits 2-7)") }
    guard bytes.count >= 10 else { throw VaultError("that seed is too short (\(bytes.count) bytes; at least 10)") }
    return bytes
}
