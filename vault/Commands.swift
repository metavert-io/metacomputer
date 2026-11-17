// mcomp-vault: the commands.
import Darwin
import Foundation

let usageText = """
mcomp-vault: credentials for the metacomputer VM, used without ever reaching Claude.
(Usually run as `metacomputer secrets ...`, which fills in the current VM.)

In your own terminal (these ask for the vault passphrase):
  init                                   create the vault
  set NAME --kind password|totp|text --origin https://site [--origin ...]
      [--username U] [--approval session|each]      store a secret (the value is typed, not echoed)
  delete NAME                            remove a secret
  bind VM --port P --host-key-line LINE --fill-key PATH --fill-user U
      --ops-key PATH --ops-user U --gateway PATH   pin how to reach a VM (you confirm with "yes")
  session policy NAME --approval session|each      whether loading a sealed session asks first
  session delete NAME                    remove a sealed browser session
  unlock [--for 30m] [--confirm session|each] [--allow-multiplexer]
                                         serve requests from this terminal until Ctrl-C or the time is up

From anywhere (Claude uses these; they work only while the vault is unlocked):
  list [--json]                          names, kinds, sites, approval, use counts (never values)
  fill NAME --vm VM --field password|username|otp|text [--profile P] [--selector CSS]
                                         type a value into the focused field of the VM's page-mode Chrome
  session save NAME --vm VM --profile P [--replace]   seal a Chrome profile from the VM (always asks)
  session load NAME --vm VM [--profile P]            put a sealed profile back, under its own name

Any time:
  status [--json]    audit [--tail N] [--json]    selftest    version [--json]

fill and session: exit 0 ok, 2 refused, 3 locked, 4 error.
"""

func runMain(_ argv: [String]) -> Int32 {
    signal(SIGPIPE, SIG_IGN)
    hardenProcess()
    let paths = Paths.current()
    guard let cmd = argv.first else { oprint(usageText); return Exit.ok }
    let rest = Array(argv.dropFirst())
    #if MCOMP_VAULT_TESTING
    // A test build never touches a real vault: it needs its own folder, outside ~/Library.
    if !["selftest", "version", "help", "-h", "--help"].contains(cmd) {
        let d = ProcessInfo.processInfo.environment["MCOMP_VAULT_DIR"] ?? ""
        let lib = URL(fileURLWithPath: homeDirectory() + "/Library").resolvingSymlinksInPath().path + "/"
        let resolved = URL(fileURLWithPath: absolutePath(d.isEmpty ? "/" : d)).resolvingSymlinksInPath().path + "/"
        if d.isEmpty || resolved.hasPrefix(lib) {
            eprint("mcomp-vault: this is a TEST build; it runs only with MCOMP_VAULT_DIR set to a folder outside ~/Library")
            return Exit.failed
        }
    }
    #endif
    do {
        switch cmd {
        case "init": return try cmdInit(paths, rest)
        case "set": return try cmdSet(paths, rest)
        case "delete": return try cmdDelete(paths, rest)
        case "bind": return try cmdBind(paths, rest)
        case "unlock": return try cmdUnlock(paths, rest)
        case "list": return try cmdList(paths, rest)
        case "fill": return try cmdFill(paths, rest)
        case "session": return try cmdSession(paths, rest)
        case "status": return try cmdStatus(paths, rest)
        case "audit": return try cmdAudit(paths, rest)
        case "selftest": return selftest()
        case "version": return cmdVersion(rest)
        case "help", "-h", "--help": oprint(usageText); return Exit.ok
        default: throw usageError("unknown command '\(cmd)' (mcomp-vault help)")
        }
    } catch let e as VaultError {
        eprint("mcomp-vault: \(e.message)")
        return e.code
    } catch {
        eprint("mcomp-vault: \(error)")
        return Exit.failed
    }
}

// MARK: - passphrase helpers

/// Ask for the passphrase and open the vault with it (the key is the caller's).
func unlockKey(_ paths: Paths) throws -> (VaultKey, Payload) {
    let header = try readHeader(paths)
    var pass = try readPassphrase(confirm: false)
    defer { wipe(&pass) }
    let key = try deriveKey(pass, header.kdf)
    let payload = try decryptPayload(header, key)
    return (key, payload)
}

// MARK: - init / set / delete / bind

func cmdInit(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    _ = try parseArgs(argv, values: [])
    try requireTTY("init")
    try ensurePrivateDir(paths.dir)
    guard !vaultExists(paths) else { throw VaultError("a vault already exists at \(paths.vault)") }
    installPromptSignalHandlers()
    var pass = try readPassphrase(confirm: true)
    defer { wipe(&pass) }
    let key = try deriveKey(pass, newKDF())
    try withFileLock(paths.lock) {
        guard !vaultExists(paths) else { throw VaultError("a vault already exists at \(paths.vault)") }
        try savePayload(paths, key, Payload.fresh())
    }
    audit(paths, ["op": "init", "result": "ok"])
    oprint("Created the vault at \(paths.vault).")
    oprint("Next: metacomputer secrets set NAME --kind password --origin https://site.example --username you")
    return Exit.ok
}

func cmdSet(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["kind", "origin", "username", "approval"])
    guard a.positional.count == 1 else { throw usageError("usage: set NAME --kind password|totp|text --origin https://site [--username U] [--approval session|each]") }
    let name = a.positional[0]
    guard validName(name) else { throw usageError("names: letters, digits, '.', '_' and '-' (1-64)") }
    let kind = a.last("kind") ?? ""
    guard ["password", "totp", "text"].contains(kind) else { throw usageError("--kind password, totp or text") }
    var origins: [String] = []
    for o in a.all("origin") {
        guard let c = canonicalOrigin(o) else {
            throw usageError("--origin \(o): give a site as https://host[:port] (no path); http only for 127.0.0.1 or localhost with a port")
        }
        if !origins.contains(c) { origins.append(c) }
    }
    guard !origins.isEmpty else { throw usageError("at least one --origin https://site is needed: a secret fills only into the sites it names") }
    let approval = a.last("approval") ?? "session"
    guard ["session", "each"].contains(approval) else { throw usageError("--approval session or each") }
    let username = a.last("username")
    if let u = username {
        guard kind == "password" else { throw usageError("--username goes with --kind password") }
        guard !u.isEmpty, u.count <= 256, clean(u, max: 256) == u else { throw usageError("--username: 1-256 printable characters") }
    }
    try requireTTY("set")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    if existing.secrets[name] != nil {
        let a = readVisible("A secret named \(name) exists. Replace it? [y/N] ").lowercased()
        guard a == "y" || a == "yes" else { throw VaultError("left unchanged") }
    }
    var value: [UInt8]
    switch kind {
    case "password":
        value = try readHidden("Password for \(name): ")
        var again = try readHidden("The same password again: ")
        defer { wipe(&again) }
        guard again == value else { wipe(&value); throw VaultError("the two passwords differ") }
    case "totp":
        var raw = try readHidden("TOTP seed for \(name) (base32, or an otpauth:// address): ")
        defer { wipe(&raw) }
        var seed = try parseTOTPSeed(String(decoding: raw, as: UTF8.self))
        value = Array(base32Encode(seed).utf8)
        wipe(&seed)
    default:
        value = try readHidden("Value for \(name): ")
    }
    defer { wipe(&value) }
    guard !value.isEmpty, value.count <= 4096, String(validating: value, as: UTF8.self) != nil else {
        throw VaultError("the value must be 1-4096 bytes of text")
    }
    let stored = String(decoding: value, as: UTF8.self)
    try updateVault(paths, key) { p in
        p.secrets[name] = Secret(kind: kind, value: stored, username: username, origins: origins, approval: approval,
                                 created: isoTime(), last_used: nil, uses: 0)
    }
    audit(paths, ["op": "set", "result": "ok", "name": name, "kind": kind, "origins": origins, "approval_mode": approval])
    oprint("Saved \(name) (\(kind)) for \(origins.joined(separator: ", "))" + (approval == "each" ? "; every use asks at the unlock terminal." : "."))
    return Exit.ok
}

func cmdDelete(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [])
    guard a.positional.count == 1, validName(a.positional[0]) else { throw usageError("usage: delete NAME") }
    let name = a.positional[0]
    try requireTTY("delete")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    guard existing.secrets[name] != nil else { throw VaultError("no secret named \(name)") }
    try updateVault(paths, key) { p in p.secrets[name] = nil }
    audit(paths, ["op": "delete", "result": "ok", "name": name])
    oprint("Deleted \(name).")
    return Exit.ok
}

/// A path bind will show and pin: printable text only, so it can't disguise itself on the terminal.
func checkPrintablePath(_ p: String, _ flag: String) throws {
    guard clean(p, max: 4096) == p else { throw usageError("--\(flag): the path has control or invisible characters; refusing") }
}

/// An SSH private key file for bind: a plain path (ssh gets it on its command line), 0600.
func checkKeyFile(_ raw: String, _ flag: String) throws -> String {
    let p = absolutePath(raw)
    try checkPrintablePath(p, flag)
    guard !p.contains(where: { $0 == " " || $0 == "\"" || $0 == "'" || $0 == "\t" }) else {
        throw usageError("--\(flag): a path without spaces or quotes")
    }
    var st = stat()
    guard stat(p, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG else { throw VaultError("no key file at \(p)") }
    guard st.st_mode & 0o077 == 0 else { throw VaultError("\(p) is readable by others (chmod 600 it)") }
    return p
}

func cmdBind(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: bind VM --port P --host-key-line LINE --fill-key PATH --fill-user U --ops-key PATH --ops-user U --gateway PATH"
    let a = try parseArgs(argv, values: ["port", "host-key-line", "fill-key", "fill-user", "ops-key", "ops-user", "gateway"])
    guard a.positional.count == 1, validName(a.positional[0]) else { throw usageError(usage) }
    let vm = a.positional[0]
    guard let ps = a.last("port"), let port = Int(ps), port >= 1, port <= 65535 else { throw usageError("--port: the VM's SSH port on 127.0.0.1") }
    guard let hkl = a.last("host-key-line"), let hostKey = normalizeHostKey(hkl) else {
        throw usageError("--host-key-line: the VM's public host key ('ssh-ed25519 AAAA...', or its known_hosts line)")
    }
    guard let fk = a.last("fill-key"), let ok = a.last("ops-key") else { throw usageError(usage) }
    let fillKey = try checkKeyFile(fk, "fill-key"), opsKey = try checkKeyFile(ok, "ops-key")
    guard let fillUser = a.last("fill-user"), validUser(fillUser) else { throw usageError("--fill-user: the VM's fill account (mcfill)") }
    guard let opsUser = a.last("ops-user"), validUser(opsUser) else { throw usageError("--ops-user: the VM's desktop user (e.g. cua)") }
    guard let g = a.last("gateway"), !g.isEmpty else { throw usageError("--gateway: the gateway program that serves the port (cua-netgw)") }
    try checkPrintablePath(absolutePath(g), "gateway")
    let gateway = URL(fileURLWithPath: absolutePath(g)).resolvingSymlinksInPath().path
    try checkPrintablePath(gateway, "gateway")
    guard access(gateway, X_OK) == 0 else { throw VaultError("no gateway program at \(gateway)") }
    guard let gwSHA = fileSHA256(gateway) else { throw VaultError("cannot read the gateway program at \(gateway)") }
    let gwHash = String(gwSHA.prefix(16))
    try requireTTY("bind")
    installPromptSignalHandlers()

    // These settings come from files any program of yours can change (the config, known_hosts),
    // so the person checks them here before they're pinned.
    let fp = hostKeyFingerprint(hostKey)
    oprint("""
    About to pin VM \(vm):
      SSH port      127.0.0.1:\(port)
      host key      \(fp) (\(hostKey.split(separator: " ").first ?? ""))
      fills         as \(fillUser), key \(fillKey)
      sessions      as \(opsUser), key \(opsKey)
      gateway       \(gateway) (sha256 \(gwHash)...)
    Check the host key on the VM itself before you agree: in the live view (metacomputer view),
    open a terminal in the VM and run
      ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
    Its SHA256 fingerprint must be the one above. If it isn't, don't pin it.
    """)
    let answer = readVisible("Type yes to pin these settings: ")
    guard answer == "yes" else { throw VaultError("not pinned") }
    let (key, _) = try unlockKey(paths)
    let b = VMBinding(ssh_port: port, host_key_line: hostKey, ops_key_path: opsKey, guest_user: opsUser,
                      fill_key_path: fillKey, fill_user: fillUser, gateway_path: gateway, gateway_sha256: gwSHA, bound: isoTime())
    try updateVault(paths, key) { p in p.vms[vm] = b }
    audit(paths, ["op": "bind", "result": "ok", "vm": vm, "ssh_port": port, "host_key": fp, "gateway": gateway])
    oprint("Bound VM \(vm): 127.0.0.1:\(port), host key \(fp), fills as \(fillUser), sessions as \(opsUser), gateway \(gateway)")
    return Exit.ok
}

// MARK: - unlock

func cmdUnlock(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["for", "confirm"], flags: ["allow-multiplexer"])
    guard a.positional.isEmpty else { throw usageError("usage: unlock [--for 30m] [--confirm session|each] [--allow-multiplexer]") }
    let dur = a.last("for") ?? "30m"
    guard let seconds = parseDuration(dur), seconds <= 12 * 3600 else { throw usageError("--for: like 90s, 30m or 2h (at most 12h)") }
    let confirm = a.last("confirm") ?? "session"
    guard ["session", "each"].contains(confirm) else { throw usageError("--confirm session or each") }
    try requireTTY("unlock")
    let mux = terminalMultiplexer()
    if let m = mux, confirm == "each", !a.has("allow-multiplexer") {
        throw VaultError("this terminal runs inside \(m), where any program of yours can type into it (including the answer to a confirmation), so --confirm each would protect nothing. Unlock in a terminal outside \(m), or add --allow-multiplexer if you accept that")
    }
    try ensurePrivateDir(paths.dir)
    _ = try readHeader(paths)
    _ = try unixAddress(paths.socket)
    // One unlock at a time: hold this lock for as long as we run.
    let lfd = open(paths.unlockLock, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o600)
    guard lfd >= 0 else { throw VaultError("cannot open \(paths.unlockLock)") }
    if flock(lfd, LOCK_EX | LOCK_NB) != 0 {
        let other = (try? readFileBytes(paths.unlockLock, max: 64)).map { String(decoding: $0, as: UTF8.self) } ?? "?"
        close(lfd)
        throw VaultError("the vault is already unlocked in another terminal (pid \(clean(other.trimmingCharacters(in: .whitespacesAndNewlines), max: 12)))")
    }
    ftruncate(lfd, 0)
    _ = writeAll(lfd, Array("\(getpid())\n".utf8))
    installPromptSignalHandlers()
    let (key, _) = try unlockKey(paths)
    let server = UnlockServer(paths: paths, key: key, seconds: seconds, confirmEach: confirm == "each",
                              multiplexer: mux, allowMultiplexer: a.has("allow-multiplexer"))
    let rc = try server.serve()
    ftruncate(lfd, 0)
    close(lfd)
    return rc
}

// MARK: - client side

enum ClientError: Error { case locked }

/// One request to the unlock process; .locked when nothing is serving.
func request(_ paths: Paths, _ req: [String: Any], timeout: Int) throws -> [String: Any] {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { throw VaultError("cannot make a socket", code: Exit.error) }
    defer { close(fd) }
    var addr = try unixAddress(paths.socket)
    let rc = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    if rc != 0 { throw ClientError.locked }
    guard let uid = peerUID(fd), uid == getuid() else { throw VaultError("the vault socket belongs to another user; refusing", code: Exit.error) }
    setTimeouts(fd, seconds: timeout)
    guard writeAll(fd, Array((jsonLine(req) + "\n").utf8)) else { throw ClientError.locked }
    guard let msg = readMessage(fd), let res = parseJSONObject(msg) else {
        throw VaultError("no answer from the unlock process", code: Exit.error)
    }
    if res["status"] as? String == "locked" { throw ClientError.locked }
    return res
}

let lockedMessage = "the vault is locked. Ask the user to run `metacomputer secrets unlock` in their own terminal (never ask for the passphrase or the value in chat), then try again."
let noVaultMessage = "there is no vault yet. Ask the user to create one and store the secret in their own terminal (`metacomputer secrets init`, then `metacomputer secrets set NAME ...`); never ask for the value in chat."

func lockedHint(_ paths: Paths) -> String { vaultExists(paths) ? lockedMessage : noVaultMessage }

func exitFor(_ res: [String: Any]) -> Int32 {
    switch res["status"] as? String {
    case "ok": return Exit.ok
    case "denied": return Exit.denied
    case "locked": return Exit.locked
    default: return Exit.error
    }
}

func printResult(_ res: [String: Any]) {
    var out: [String: Any] = [:]
    for k in ["ok", "name", "vm", "field", "profile", "origin", "chars", "bytes", "reason"] { if let v = res[k] { out[k] = v } }
    if out["ok"] == nil { out["ok"] = false }
    oprint(jsonLine((cleanAll(out) as? [String: Any]) ?? ["ok": false]))
}

func cmdFill(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["vm", "field", "profile", "selector"])
    guard a.positional.count == 1, let vm = a.last("vm"), let field = a.last("field") else {
        throw usageError("usage: fill NAME --vm VM --field password|username|otp|text [--profile P] [--selector CSS]")
    }
    let name = a.positional[0]
    guard validName(name), validName(vm) else { throw usageError("names: letters, digits, '.', '_' and '-'") }
    guard ["password", "username", "otp", "text"].contains(field) else { throw usageError("--field password, username, otp or text") }
    var req: [String: Any] = ["op": "fill", "name": name, "vm": vm, "field": field]
    if let p = a.last("profile") {
        guard validName(p) else { throw usageError("--profile: a driver profile name") }
        req["profile"] = p
    }
    if let s = a.last("selector") {
        guard s.count <= 300 else { throw usageError("--selector: at most 300 characters") }
        req["selector"] = s
    }
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 300) } catch ClientError.locked {
        audit(paths, ["op": "fill", "result": "locked", "name": name, "vm": vm, "field": field])
        printResult(["ok": false, "name": name, "vm": vm, "field": field, "reason": "locked"])
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    printResult(res)
    return exitFor(res)
}

func cmdSession(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: session save NAME --vm VM --profile P [--replace] | load NAME --vm VM [--profile P] | policy NAME --approval session|each | delete NAME"
    guard let sub = argv.first else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), values: ["vm", "profile", "approval"], flags: ["replace"])
    if sub == "policy" {
        guard a.positional.count == 1, validName(a.positional[0]), let ap = a.last("approval"), ["session", "each"].contains(ap) else {
            throw usageError("usage: session policy NAME --approval session|each")
        }
        let name = a.positional[0]
        try requireTTY("session policy")
        installPromptSignalHandlers()
        let (key, existing) = try unlockKey(paths)
        guard existing.sessions[name] != nil else { throw VaultError("no saved session named \(name)") }
        try updateVault(paths, key) { p in p.sessions[name]?.approval = ap }
        audit(paths, ["op": "session_policy", "result": "ok", "name": name, "approval_mode": ap])
        oprint(ap == "session"
               ? "Loading \(name) into the VM it was saved from no longer asks (other VMs and replacing it still do)."
               : "Loading \(name) asks at the unlock terminal each time.")
        return Exit.ok
    }
    if sub == "delete" {
        guard a.positional.count == 1, validName(a.positional[0]) else { throw usageError("usage: session delete NAME") }
        let name = a.positional[0]
        try requireTTY("session delete")
        installPromptSignalHandlers()
        let (key, existing) = try unlockKey(paths)
        guard existing.sessions[name] != nil else { throw VaultError("no saved session named \(name)") }
        try updateVault(paths, key) { p in p.sessions[name] = nil }
        unlink(paths.sessions + "/\(name).sealed")
        audit(paths, ["op": "session_delete", "result": "ok", "name": name])
        oprint("Deleted the sealed session \(name).")
        return Exit.ok
    }
    guard sub == "save" || sub == "load" else { throw usageError(usage) }
    let profile = a.last("profile")
    guard a.positional.count == 1, let vm = a.last("vm"), validName(a.positional[0]), validName(vm),
          profile.map(validName) ?? (sub == "load") else {
        throw usageError(sub == "save" ? "usage: session save NAME --vm VM --profile P [--replace]" : "usage: session load NAME --vm VM [--profile P]")
    }
    let name = a.positional[0]
    var req: [String: Any] = ["op": "session_\(sub)", "name": name, "vm": vm]
    if let p = profile { req["profile"] = p }
    if a.has("replace") { req["replace"] = true }
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 1800) } catch ClientError.locked {
        audit(paths, ["op": "session_\(sub)", "result": "locked", "name": name, "vm": vm, "profile": profile ?? ""])
        printResult(["ok": false, "name": name, "vm": vm, "profile": profile ?? "", "reason": "locked"])
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    printResult(res)
    return exitFor(res)
}

func cmdList(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["json"])
    let res: [String: Any]
    do { res = try request(paths, ["op": "list"], timeout: 15) } catch ClientError.locked {
        if a.has("json") { oprint(jsonLine(["ok": false, "reason": "locked"])) }
        eprint("mcomp-vault: \(lockedHint(paths))")
        return Exit.locked
    }
    guard res["ok"] as? Bool == true else {
        eprint("mcomp-vault: \(res["reason"] as? String ?? "list failed")")
        return Exit.error
    }
    if a.has("json") {
        oprint(jsonPretty(["secrets": res["secrets"] ?? [], "vms": res["vms"] ?? [], "sessions": res["sessions"] ?? []]))
        return Exit.ok
    }
    let secrets = res["secrets"] as? [[String: Any]] ?? []
    oprint(secrets.isEmpty ? "No secrets yet (metacomputer secrets set ...)." : "Secrets:")
    for s in secrets {
        let fields = (s["fields"] as? [String] ?? []).joined(separator: "/")
        let user = (s["username"] as? String).map { " user \($0)" } ?? ""
        let used = (s["last_used"] as? String).map { "last used \($0)" } ?? "never used"
        oprint("  \(s["name"] ?? "")  \(s["kind"] ?? "") [--field \(fields)]\(user)  sites \((s["origins"] as? [String] ?? []).joined(separator: " "))"
               + "  approval \(s["approval"] ?? "")  \(s["uses"] ?? 0) uses, \(used)")
    }
    let vms = res["vms"] as? [[String: Any]] ?? []
    if !vms.isEmpty { oprint("VMs:") }
    for v in vms {
        oprint("  \(v["name"] ?? "")  127.0.0.1:\(v["ssh_port"] ?? "")  host key \(v["host_key"] ?? "")  fills as \(v["fill_user"] ?? "-")"
               + "  sessions as \(v["user"] ?? "")" + (v["needs_bind"] != nil ? "  (bound by an earlier version: bind again)" : ""))
    }
    let sessions = res["sessions"] as? [[String: Any]] ?? []
    if !sessions.isEmpty { oprint("Sealed browser sessions:") }
    for s in sessions {
        oprint("  \(s["name"] ?? "")  profile \(s["profile"] ?? "") from VM \(s["vm"] ?? ""), saved \(s["saved_at"] ?? ""), \(s["bytes"] ?? 0) bytes, "
               + "loaded \(s["loads"] ?? 0)x, approval \(s["approval"] ?? "each")")
    }
    return Exit.ok
}

func cmdStatus(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["json"])
    var out: [String: Any] = ["vault": paths.vault, "socket": paths.socket, "exists": vaultExists(paths), "build": Build.kind,
                              "unlocked": false]
    if let res = try? request(paths, ["op": "status"], timeout: 10), res["ok"] as? Bool == true {
        out["unlocked"] = true
        for k in ["expires", "expires_in_s", "confirm", "pid", "since", "multiplexer"] { if let v = res[k] { out[k] = v } }
    }
    if a.has("json") { oprint(jsonLine(out)); return Exit.ok }
    if !(out["exists"] as? Bool ?? false) {
        oprint("vault:    none yet (\(paths.vault)); create it with: metacomputer secrets init")
    } else if out["unlocked"] as? Bool == true {
        oprint("vault:    unlocked until \(out["expires"] ?? "?") (\(out["expires_in_s"] ?? 0) s left; confirm \(out["confirm"] ?? "?"); pid \(out["pid"] ?? "?"))")
    } else {
        oprint("vault:    locked (the user unlocks it with: metacomputer secrets unlock, in their own terminal)")
    }
    oprint("file:     \(paths.vault)")
    oprint("socket:   \(paths.socket)")
    #if MCOMP_VAULT_TESTING
    oprint("build:    TEST (MCOMP_VAULT_TEST_* settings are honoured; never use it for real secrets)")
    #endif
    return Exit.ok
}

func cmdAudit(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["tail"], flags: ["json"])
    let n = Int(a.last("tail") ?? "20") ?? 20
    guard fileExists(paths.audit) else { oprint("No audit records yet (\(paths.audit))."); return Exit.ok }
    let lines = String(decoding: try readFileBytes(paths.audit, max: 256 << 20), as: UTF8.self).split(separator: "\n").suffix(max(1, n))
    for l in lines {
        if a.has("json") { oprint(clean(String(l), max: 4000)); continue }
        guard let r = parseJSONObject(Array(l.utf8)) else { continue }
        var s = "\(r["time"] ?? "")  \(r["op"] ?? "")"
        for k in ["name", "field", "vm", "profile"] { if let v = r[k] { s += " \(k)=\(v)" } }
        s += "  -> \(r["result"] ?? "")"
        if let o = r["origin"] { s += " \(o)" }
        if let c = r["chars"] { s += " (\(c) chars)" }
        if let ap = r["approval"] as? String, ap != "-" { s += " [approval \(ap)]" }
        if let why = r["reason"] { s += ": \(why)" }
        oprint(clean(s, max: 600))
    }
    return Exit.ok
}

func cmdVersion(_ argv: [String]) -> Int32 {
    let info: [String: Any] = ["build": Build.kind, "source": buildSourceHash]
    if argv.contains("--json") { oprint(jsonLine(info)) } else {
        oprint("mcomp-vault \(Build.kind) build, source \(buildSourceHash)")
    }
    return Exit.ok
}

// MARK: - selftest

func selftest() -> Int32 {
    var failures = 0
    func check(_ name: String, _ ok: Bool) {
        oprint("\(ok ? "PASS" : "FAIL")  \(name)")
        if !ok { failures += 1 }
    }
    // RFC 6238 appendix B (SHA1; seed "12345678901234567890").
    let seed = Array("12345678901234567890".utf8)
    let vectors: [(UInt64, String)] = [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                                       (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")]
    for (t, want) in vectors {
        check("TOTP SHA1 8 digits at \(t)", totp(secret: seed, time: t, digits: 8) == want)
        check("TOTP SHA1 6 digits at \(t)", totp(secret: seed, time: t, digits: 6) == String(want.suffix(6)))
    }
    // PBKDF2-HMAC-SHA256 (RFC 7914 section 11 / common test vectors).
    check("PBKDF2-SHA256 c=1", pbkdf2SHA256(password: Array("password".utf8), salt: Array("salt".utf8), iterations: 1).map(sha256HexRaw)
          == "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b")
    check("PBKDF2-SHA256 c=4096", pbkdf2SHA256(password: Array("password".utf8), salt: Array("salt".utf8), iterations: 4096).map(sha256HexRaw)
          == "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a")
    // AES-256-GCM round trip, and a changed byte or wrong AAD refused.
    let k = randomBytes(32), msg = Array("round trip".utf8), aad = Array("aad".utf8)
    let sealed = (try? sealGCM(key: k, plaintext: msg, aad: aad)) ?? []
    check("AES-GCM round trip", (try? openGCM(key: k, combined: sealed, aad: aad)) == msg)
    var bad = sealed
    if !bad.isEmpty { bad[bad.count / 2] ^= 1 }
    check("AES-GCM refuses a changed byte", (try? openGCM(key: k, combined: bad, aad: aad)) == nil)
    check("AES-GCM refuses other AAD", (try? openGCM(key: k, combined: sealed, aad: Array("other".utf8))) == nil)
    // base32 (RFC 4648 section 10).
    let b32: [(String, String)] = [("", ""), ("MY======", "f"), ("MZXQ====", "fo"), ("MZXW6===", "foo"), ("MZXW6YQ=", "foob"),
                                   ("MZXW6YTB", "fooba"), ("MZXW6YTBOI======", "foobar"), ("mzxw6ytboi", "foobar")]
    for (enc, dec) in b32 { check("base32 '\(enc)'", base32Decode(enc) == Array(dec.utf8)) }
    check("base32 encode", base32Encode(Array("foobar".utf8)) == "MZXW6YTBOI")
    check("base32 rejects '1'", base32Decode("MZXW1") == nil)
    check("otpauth URI", (try? parseTOTPSeed("otpauth://totp/Example:a?secret=JBSWY3DPEHPK3PXP&issuer=Example")) == base32Decode("JBSWY3DPEHPK3PXP"))
    check("otpauth SHA256 refused", (try? parseTOTPSeed("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&algorithm=SHA256")) == nil)
    // Origins.
    let origins: [(String, String?)] = [
        ("https://example.com", "https://example.com"), ("https://Example.COM/", "https://example.com"),
        ("https://example.com:443", "https://example.com"), ("https://example.com:8443", "https://example.com:8443"),
        ("https://example.com/login", nil), ("http://example.com", nil), ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://localhost:8000", "http://localhost:8000"), ("http://127.0.0.1", nil), ("ftp://example.com", nil),
        ("https://user@example.com", nil), ("https://-bad.example", nil), ("https://example.com:0", nil),
        ("https://example.com:99999", nil), ("example.com", nil), ("https://", nil), ("https://a..b", nil)]
    for (i, want) in origins { check("origin '\(i)'", canonicalOrigin(i) == want) }
    // Host keys and names.
    let blob = [0, 0, 0, 11] + Array("ssh-ed25519".utf8) + [0, 0, 0, 32] + [UInt8](repeating: 7, count: 32)
    let b64 = Data(blob).base64EncodedString()
    check("host key from known_hosts line", normalizeHostKey("[127.0.0.1]:2277 ssh-ed25519 \(b64)") == "ssh-ed25519 \(b64)")
    check("host key with the wrong type refused", normalizeHostKey("ssh-rsa \(b64)") == nil)
    check("names", validName("x-login.1") && !validName("a/b") && !validName("..") && !validName(""))
    check("names can't start with '-'", !validName("-x") && !validName("--profile") && validName("x-"))
    // Text from the VM or a client can't drive the terminal.
    check("clean: escape sequences", clean("ok\u{1B}[2J\u{1B}]0;t\u{07}x") == "ok?[2J?]0;t?x")
    check("clean: C1, bidi and zero-width", clean("a\u{9B}b\u{202E}c\u{200B}d") == "a?b?c?d")
    check("clean: newlines and length", clean("a\nb\r\tc") == "a b  c" && clean("abcdef", max: 3) == "abc...")
    check("prompt text: whitespace collapsed, capped", promptText("a  \n b\tc", max: 80) == "a b c" && promptText(String(repeating: "x", count: 200), max: 80).count == 83)
    check("KDF rounds out of range refused", (try? deriveKey(Array("x".utf8), KDFParams(name: "pbkdf2-sha256", iterations: 1 << 40,
                                                                                         salt: Data(randomBytes(16)).base64EncodedString()))) == nil)
    oprint(failures == 0 ? "selftest: all passed (\(Build.kind) build)" : "selftest: \(failures) failed")
    return failures == 0 ? Exit.ok : Exit.failed
}

func sha256HexRaw(_ b: [UInt8]) -> String { b.map { String(format: "%02x", $0) }.joined() }
