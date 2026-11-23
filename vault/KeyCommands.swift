// mcomp-vault: creating the vault and rotating its keys (all in your own terminal).
import Darwin
import Foundation

/// `--passphrase-stdin` (a passphrase piped in by a script) is refused: here the passphrase is typed on your
/// own terminal, never read from a pipe a program could fill.
func refusePassphraseStdin(_ argv: [String]) throws {
    if argv.contains(where: { $0 == "--passphrase-stdin" || $0.hasPrefix("--passphrase-stdin=") }) {
        throw usageError("--passphrase-stdin isn't supported: the vault passphrase is typed on your own terminal, never piped in (a script could pass it, and so could any program running as you)")
    }
}

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
    try refusePassphraseStdin(argv)
    // --passphrase, as other vaults spell it: this vault always uses one (typed twice, never an argument).
    let a = try parseArgs(argv, command: "init")
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
    var fresh = Payload.fresh(boundary: lastAuditEvent(paths.audit).0)
    let oldVault = testOldVault()
    if oldVault {
        fresh.audit_key = nil
        fresh.audit_key_since = nil
    }
    try withFileLock(paths.lock) {
        guard !vaultExists(paths) else { throw VaultError("a vault already exists at \(paths.vault)") }
        try savePayload(paths, key, fresh)
    }
    audit(paths, ["op": "init", "result": "ok", "enclave": enclave, "recovery_key": recovery != nil],
          macKey: oldVault ? nil : vaultAuditKey(paths, key))
    oprint("Created the vault at \(paths.vault)" + (enclave ? ", bound to this Mac's Secure Enclave." : " (passphrase only, no Secure Enclave)."))
    if let r = recovery { showRecoveryKey(r) }
    oprint("Next: metacomputer secrets set NAME --kind password --origin https://site.example --username you")
    return Exit.ok
}

/// Re-encrypt the payload under a fresh data key and new key settings. The new key setup
/// (PBKDF2, the Enclave) is made first; then one lock covers reading the latest contents
/// with the old key and writing them with the new one, so nothing written meanwhile is lost.
func rewrite(_ paths: Paths, from old: VaultKey, passphrase: [UInt8], enclave: Bool) throws -> (VaultKey, String?) {
    let (key, recovery) = try newVaultKey(passphrase: passphrase, enclave: enclave)
    testPause("rekey")
    try withFileLock(paths.lock) {
        var current = try loadPayload(paths, old)
        let made = current.ensureAuditKey(boundary: lastAuditEvent(paths.audit).0)
        try savePayload(paths, key, current)
        if made, let k = current.auditKeyBytes { adoptSinkStates(paths, current, key: k) }
    }
    return (key, recovery)
}

func cmdRekey(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, command: "rekey")
    guard !(a.has("enclave") && a.has("no-enclave")) else { throw usageError("--enclave or --no-enclave, not both") }
    try requireTTY("rekey")
    installPromptSignalHandlers()
    oprint("The current passphrase:")
    let (old, _) = try unlockKey(paths)
    let enclave = a.has("enclave") ? true : (a.has("no-enclave") ? false : old.usesEnclave)
    if enclave && !enclaveAvailable() { throw VaultError("this Mac has no Secure Enclave; use --no-enclave") }
    oprint("The new passphrase:")
    var pass = try readPassphrase(confirm: true)
    defer { wipe(&pass) }
    let (key, recovery) = try rewrite(paths, from: old, passphrase: pass, enclave: enclave)
    audit(paths, ["op": "rekey", "result": "ok", "enclave": enclave, "recovery_key": recovery != nil], macKey: vaultAuditKey(paths, key))
    oprint("Rekeyed: new passphrase, new salt, new data key" + (enclave ? ", a new Secure Enclave key" : "")
           + (old.hasRecovery ? "; the old recovery key no longer works" : "") + ". An unlocked vault locks itself; unlock again.")
    if let r = recovery { showRecoveryKey(r) }
    return Exit.ok
}

func cmdRecover(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, command: "recover")
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
    let (key, recovery) = try rewrite(paths, from: old, passphrase: pass, enclave: enclave)
    audit(paths, ["op": "recover", "result": "ok", "enclave": enclave], macKey: vaultAuditKey(paths, key))
    oprint("Recovered: new passphrase, new data key" + (enclave ? ", bound to this Mac's Secure Enclave" : " (passphrase only)")
           + "; the old recovery key no longer works.")
    if let r = recovery { showRecoveryKey(r) }
    return Exit.ok
}

/// A new key for sealed browser sessions. The whole run holds the vault's lock. Every sealed
/// session the vault lists must open with a key it holds (the current one, or one kept from a
/// rotation that was cut short) before anything changes; an unreadable one stops it with
/// nothing changed. The new key is saved with every older key kept, then each file is
/// rewritten, then checked; only then are the older keys dropped. Cut short at any point,
/// every file still opens, and running it again finishes the job.
func cmdRotateSessionKey(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    _ = try parseArgs(argv, command: "rotate-session-key")
    try requireTTY("rotate-session-key")
    installPromptSignalHandlers()
    let (key, _) = try unlockKey(paths)
    var n = 0
    var resumed = false
    try withFileLock(paths.lock) {
        var p = try loadPayload(paths, key)
        if p.ensureAuditKey(boundary: lastAuditEvent(paths.audit).0), let k = p.auditKeyBytes { adoptSinkStates(paths, p, key: k) }
        resumed = !(p.session_keys_old ?? []).isEmpty || p.session_key_prev != nil
        let ring = p.sessionKeyring
        guard !ring.isEmpty else { throw VaultError("the vault's session key is damaged") }
        var plains: [(String, [UInt8])] = []
        defer { for i in plains.indices { wipe(&plains[i].1) } }
        for name in p.sessions.keys.sorted() {
            let path = paths.sessions + "/\(name).sealed"
            guard let sealed = try? readFileBytes(path) else {
                throw VaultError("the sealed session \(name) can't be read (\(path)); nothing changed. Fix it, or remove it with `metacomputer secrets session delete \(name)`")
            }
            let aad = Array("mcomp-session|1|\(name)".utf8)
            guard let plain = ring.lazy.compactMap({ try? openGCM(key: $0, combined: sealed, aad: aad) }).first else {
                throw VaultError("the sealed session \(name) doesn't open with any key the vault holds (changed or damaged); nothing changed. Remove it with `metacomputer secrets session delete \(name)`")
            }
            plains.append((name, plain))
        }
        var newKey = randomBytes(32)
        defer { wipe(&newKey) }
        // 1. The new key in the vault, every older one kept.
        p.session_keys_old = ring.map { Data($0).base64EncodedString() }
        p.session_key_prev = nil
        p.session_key = Data(newKey).base64EncodedString()
        try savePayload(paths, key, p)
        testPause("rotate-saved")
        // 2. Each file under the new key, one at a time.
        for (i, (name, plain)) in plains.enumerated() {
            let path = paths.sessions + "/\(name).sealed"
            try writeFileAtomic(path, try sealGCM(key: newKey, plaintext: plain, aad: Array("mcomp-session|1|\(name)".utf8)))
            testStopAfter("rotate", i + 1)
        }
        // 3. Every file opens with the new key: the older ones go.
        for (name, _) in plains {
            guard let sealed = try? readFileBytes(paths.sessions + "/\(name).sealed"),
                  (try? openGCM(key: newKey, combined: sealed, aad: Array("mcomp-session|1|\(name)".utf8))) != nil else {
                throw VaultError("the sealed session \(name) didn't check out under the new key; the older keys stay in the vault, so nothing is lost. Run rotate-session-key again")
            }
        }
        p.session_keys_old = nil
        p.session_key_rotated = isoTime()
        try savePayload(paths, key, p)
        n = plains.count
    }
    audit(paths, ["op": "rotate_session_key", "result": "ok", "sessions": n, "resumed": resumed], macKey: vaultAuditKey(paths, key))
    oprint("New session key; \(n) sealed session(s) re-encrypted." + (resumed ? " (An earlier rotation had been cut short; it's finished too.)" : ""))
    return Exit.ok
}

/// Test builds only: wait at a named point until the file $MCOMP_VAULT_TEST_PAUSE_<NAME> exists
/// (after creating <that>.waiting), so a test can act in between.
func testPause(_ point: String) {
    #if MCOMP_VAULT_TESTING
    let v = "MCOMP_VAULT_TEST_PAUSE_" + point.uppercased().replacingOccurrences(of: "-", with: "_")
    guard let path = ProcessInfo.processInfo.environment[v], !path.isEmpty else { return }
    _ = FileManager.default.createFile(atPath: path + ".waiting", contents: Data())
    for _ in 0..<600 where !FileManager.default.fileExists(atPath: path) { usleep(50_000) }
    #endif
}

/// Test builds only: exit (as if killed) after the Nth step of a named loop.
func testStopAfter(_ point: String, _ step: Int) {
    #if MCOMP_VAULT_TESTING
    let v = "MCOMP_VAULT_TEST_STOP_" + point.uppercased() + "_AFTER"
    if let s = ProcessInfo.processInfo.environment[v], Int(s) == step { _exit(9) }
    #endif
}

/// Test builds only: make the vault as an earlier version did (no audit key), to test the upgrade.
func testOldVault() -> Bool {
    #if MCOMP_VAULT_TESTING
    return ProcessInfo.processInfo.environment["MCOMP_VAULT_TEST_OLD_VAULT"] == "1"
    #else
    return false
    #endif
}
