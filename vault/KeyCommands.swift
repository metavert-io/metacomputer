// mcomp-vault: creating the vault and rotating its keys (all in your own terminal).
import Darwin
import Foundation

func showRecoveryKey(_ text: String) {
    oprint("""

    RECOVERY KEY (shown once; nothing stores it):
        \(text)
    It opens this vault without the Secure Enclave: on another Mac, after a reset, or if the
    Enclave key is lost (`metacomputer secrets recover`). Write it down and keep it offline,
    away from this Mac. Anyone with it and the vault file can open the vault.

    """)
}

func cmdInit(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["enclave", "no-enclave"])
    guard !(a.has("enclave") && a.has("no-enclave")) else { throw usageError("--enclave or --no-enclave, not both") }
    let enclave = a.has("no-enclave") ? false : (a.has("enclave") || enclaveAvailable())
    if enclave && !enclaveAvailable() { throw VaultError("this Mac has no Secure Enclave; use --no-enclave") }
    try requireTTY("init")
    try ensurePrivateDir(paths.dir)
    guard !vaultExists(paths) else { throw VaultError("a vault already exists at \(paths.vault)") }
    installPromptSignalHandlers()
    var pass = try readPassphrase(confirm: true)
    defer { wipe(&pass) }
    let (key, recovery) = try newVaultKey(passphrase: pass, enclave: enclave)
    try withFileLock(paths.lock) {
        guard !vaultExists(paths) else { throw VaultError("a vault already exists at \(paths.vault)") }
        try savePayload(paths, key, Payload.fresh())
    }
    audit(paths, ["op": "init", "result": "ok", "enclave": enclave, "recovery_key": recovery != nil])
    oprint("Created the vault at \(paths.vault)" + (enclave ? ", bound to this Mac's Secure Enclave." : " (passphrase only, no Secure Enclave)."))
    if let r = recovery { showRecoveryKey(r) }
    oprint("Next: metacomputer secrets set NAME --kind password --origin https://site.example --username you")
    return Exit.ok
}

/// Re-encrypt the payload under a fresh data key and new key settings.
func rewrite(_ paths: Paths, _ payload: Payload, passphrase: [UInt8], enclave: Bool) throws -> (VaultKey, String?) {
    let (key, recovery) = try newVaultKey(passphrase: passphrase, enclave: enclave)
    try withFileLock(paths.lock) { try savePayload(paths, key, payload) }
    return (key, recovery)
}

func cmdRekey(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["enclave", "no-enclave"])
    guard !(a.has("enclave") && a.has("no-enclave")) else { throw usageError("--enclave or --no-enclave, not both") }
    try requireTTY("rekey")
    installPromptSignalHandlers()
    oprint("The current passphrase:")
    let (old, payload) = try unlockKey(paths)
    let enclave = a.has("enclave") ? true : (a.has("no-enclave") ? false : old.usesEnclave)
    if enclave && !enclaveAvailable() { throw VaultError("this Mac has no Secure Enclave; use --no-enclave") }
    oprint("The new passphrase:")
    var pass = try readPassphrase(confirm: true)
    defer { wipe(&pass) }
    // Read again under the lock, so nothing written since is lost.
    let current = try withFileLock(paths.lock) { try loadPayload(paths, old) }
    let (_, recovery) = try rewrite(paths, current, passphrase: pass, enclave: enclave)
    _ = payload
    audit(paths, ["op": "rekey", "result": "ok", "enclave": enclave, "recovery_key": recovery != nil])
    oprint("Rekeyed: new passphrase, new salt, new data key" + (enclave ? ", a new Secure Enclave key" : "")
           + (old.hasRecovery ? "; the old recovery key no longer works" : "") + ". An unlocked vault locks itself; unlock again.")
    if let r = recovery { showRecoveryKey(r) }
    return Exit.ok
}

func cmdRecover(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["no-enclave"])
    try requireTTY("recover")
    installPromptSignalHandlers()
    _ = try readHeader(paths)
    var raw = try readHidden("Recovery key: ")
    defer { wipe(&raw) }
    guard var rk = parseRecoveryKey(String(decoding: raw, as: UTF8.self)) else { throw VaultError("that isn't a recovery key (8 groups of 4 letters and digits)") }
    defer { wipe(&rk) }
    let (old, _) = try openWithRecovery(paths, rk)
    let enclave = !a.has("no-enclave") && enclaveAvailable()
    oprint("Opened with the recovery key. Choose a passphrase for this Mac:")
    var pass = try readPassphrase(confirm: true)
    defer { wipe(&pass) }
    let current = try withFileLock(paths.lock) { try loadPayload(paths, old) }
    let (_, recovery) = try rewrite(paths, current, passphrase: pass, enclave: enclave)
    audit(paths, ["op": "recover", "result": "ok", "enclave": enclave])
    oprint("Recovered: new passphrase, new data key" + (enclave ? ", bound to this Mac's Secure Enclave" : " (passphrase only)")
           + "; the old recovery key no longer works.")
    if let r = recovery { showRecoveryKey(r) }
    return Exit.ok
}

func cmdRotateSessionKey(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    _ = try parseArgs(argv, values: [])
    try requireTTY("rotate-session-key")
    installPromptSignalHandlers()
    let (key, _) = try unlockKey(paths)
    var n = 0
    try withFileLock(paths.lock) {
        var p = try loadPayload(paths, key)
        guard let oldKey = Data(base64Encoded: p.session_key).map(Array.init), oldKey.count == 32 else { throw VaultError("the vault's session key is damaged") }
        let prev = p.session_key_prev.flatMap { Data(base64Encoded: $0).map(Array.init) }
        var newKey = randomBytes(32)
        defer { wipe(&newKey) }
        // 1. every sealed session, re-encrypted to NAME.sealed.new
        var staged: [String] = []
        for name in p.sessions.keys.sorted() {
            let path = paths.sessions + "/\(name).sealed"
            guard let sealed = try? readFileBytes(path) else { continue }
            let aad = Array("mcomp-session|1|\(name)".utf8)
            guard var plain = (try? openGCM(key: oldKey, combined: sealed, aad: aad)) ?? prev.flatMap({ try? openGCM(key: $0, combined: sealed, aad: aad) }) else {
                throw VaultError("the sealed session \(name) can't be opened (changed or damaged); delete it first")
            }
            defer { wipe(&plain) }
            try writeFileAtomic(path + ".new", try sealGCM(key: newKey, plaintext: plain, aad: aad))
            staged.append(path)
        }
        // 2. the new key in the vault, the old one kept until the files are in place
        p.session_key_prev = p.session_key
        p.session_key = Data(newKey).base64EncodedString()
        try savePayload(paths, key, p)
        // 3. the new files in place; 4. the old key gone
        for path in staged { guard rename(path + ".new", path) == 0 else { throw VaultError("cannot replace \(path)") } }
        p.session_key_prev = nil
        p.session_key_rotated = isoTime()
        try savePayload(paths, key, p)
        n = staged.count
    }
    audit(paths, ["op": "rotate_session_key", "result": "ok", "sessions": n])
    oprint("New session key; \(n) sealed session(s) re-encrypted.")
    return Exit.ok
}
