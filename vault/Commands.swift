// mcomp-vault: the commands.
import Darwin
import Foundation

let usageText = """
mcomp-vault: credentials for the metacomputer VM, used without passing through the conversation.
(Usually run as `metacomputer secrets ...`, which fills in the current VM.)

In your own terminal (these ask for the vault passphrase):
  init [--no-enclave]                    create the vault (bound to this Mac's Secure Enclave when it has one;
                                         prints a one-time recovery key)
  set NAME --kind password|totp|text --origin https://site [--origin ...]
      [--username U] [--approval session|each] [--project DIR ...] [--replace]
  set NAME --kind api --api-host api.example.com [--header 'Authorization: Bearer {value}']
      [--api-path-prefix /v1] [--approval session|each] [--project DIR ...] [--replace]
                                         store a secret (the value is typed, not echoed); --replace rotates it
  delete NAME                            remove a secret
  bind VM --port P --host-key-line LINE --fill-key PATH --fill-user U --ops-key PATH --ops-user U
      [--proxy-key PATH --proxy-user U --proxy-port N] --gateway PATH
                                         pin how to reach a VM (you confirm with "yes")
  rekey [--enclave | --no-enclave]       new passphrase, new data key (and a new recovery key)
  recover [--no-enclave]                 open the vault with its recovery key; set a new passphrase
  rotate-session-key                     re-encrypt every sealed browser session under a new key
  session policy NAME --approval session|each      whether loading a sealed session asks first
  session delete NAME                    remove a sealed browser session
  audit-sink add NAME --kind webhook|otlp|syslog|ntfy --url URL|ask [--header 'Name: value|{ask}' ...]
      [--ca FILE] [--required]           send every audit event off this Mac (prints its key once;
                                         `ask` and {ask} are typed with the echo off)
  audit-sink show NAME --key | remove NAME
  policy NAME --approval session|each    whether a secret's uses ask first (without typing its value again)
  presence on|off|auto                   presence mode by default (auto: on where this Mac has Touch ID)
  unlock [--for 30m] [--confirm session|each] [--allow-multiplexer] [--touch-id] [--require-presence | --no-require-presence]
                                         serve requests from this terminal until Ctrl-C or the time is up;
                                         --touch-id also shows macOS's Touch ID / login password dialog per question;
                                         presence mode: every approval needs that dialog or the passphrase

You, from any terminal (they need Touch ID / your login password, or the passphrase):
  approve ID [--passphrase]              allow one waiting request, for this one use
  rule add (--secret NAME | --site https://site | --session NAME) --project DIR --for 8h [--passphrase]
                                         an unattended rule: that use by Claude Code sessions in DIR doesn't ask,
                                         until it expires (at most 30d); never for an identity provider

From anywhere (Claude uses these; they work only while the vault is unlocked):
  list [--json]                          names, kinds, sites, approval, age, use counts (never values)
  fill NAME --vm VM --field password|username|otp|text [--profile P] [--selector CSS] [--request-id ID]
                                         type a value into the focused field of the VM's page-mode Chrome
  login NAME --vm VM --password-selector CSS [--username-selector CSS] [--profile P] [--request-id ID]
                                         sign in: username and password into those fields, under one approval
  session save NAME --vm VM --profile P [--replace]   seal a Chrome profile from the VM (always asks)
  session load NAME --vm VM [--profile P]            put a sealed profile back, under its own name
  token NAME --vm VM [--ttl 15m]         a short-lived token for an api secret, and the proxy URL in the VM
  tokens [--json]   revoke ID|--all      live tokens; end them
  requests [--json]                      uses waiting for your approval: id, who asked, site, masked username, age
  deny ID                                refuse a waiting request
  rule list [--json] | rule remove ID    unattended rules; remove one
  audit-sink list | show NAME | test NAME

Any time:
  status [--json]    audit [--tail N] [--json] | --head | --verify    selftest    version [--json]

fill, login, session and token: exit 0 ok, 2 refused, 3 locked, 4 error. A program outside Claude Code that
passes a request on for a Claude Code process adds --on-behalf-pid PID (the metacomputer hub does).
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
        case "rekey": return try cmdRekey(paths, rest)
        case "recover": return try cmdRecover(paths, rest)
        case "rotate-session-key": return try cmdRotateSessionKey(paths, rest)
        case "unlock": return try cmdUnlock(paths, rest)
        case "list": return try cmdList(paths, rest)
        case "fill": return try cmdFill(paths, rest)
        case "login": return try cmdLogin(paths, rest)
        case "requests": return try cmdRequests(paths, rest)
        case "approve": return try cmdApprove(paths, rest)
        case "deny": return try cmdDeny(paths, rest)
        case "rule": return try cmdRule(paths, rest)
        case "policy": return try cmdPolicy(paths, rest)
        case "presence": return try cmdPresence(paths, rest)
        case "session": return try cmdSession(paths, rest)
        case "token": return try cmdToken(paths, rest)
        case "tokens": return try cmdTokens(paths, rest)
        case "revoke": return try cmdRevoke(paths, rest)
        case "audit-sink": return try cmdAuditSink(paths, rest)
        case "status": return try cmdStatus(paths, rest)
        case "audit": return try cmdAudit(paths, rest)
        case "selftest": return rest == ["--local-auth"] ? localAuthSelftest() : selftest()
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
    _ = try readHeader(paths)
    var pass = try readPassphrase(confirm: false)
    defer { wipe(&pass) }
    return try openWithPassphrase(paths, pass)
}

/// The age of an ISO time, like "12 days".
func age(_ iso: String?) -> String? {
    guard let s = iso else { return nil }
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    guard let d = f.date(from: s) else { return nil }
    let days = Int(Date().timeIntervalSince(d) / 86400)
    return days == 0 ? "today" : (days == 1 ? "1 day ago" : "\(days) days ago")
}

// MARK: - set / delete

func cmdSet(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["kind", "origin", "username", "approval", "project", "api-host", "header", "api-path-prefix"],
                          flags: ["replace"])
    guard a.positional.count == 1 else {
        throw usageError("usage: set NAME --kind password|totp|text|api ... (mcomp-vault help)")
    }
    let name = a.positional[0]
    guard validName(name) else { throw usageError("names: letters, digits, '.', '_' and '-' (1-64), not starting with '-'") }
    let kind = a.last("kind") ?? ""
    guard ["password", "totp", "text", "api"].contains(kind) else { throw usageError("--kind password, totp, text or api") }
    var origins: [String] = []
    for o in a.all("origin") {
        guard let c = canonicalOrigin(o) else {
            throw usageError("--origin \(o): give a site as https://host[:port] (no path); http only for 127.0.0.1 or localhost with a port")
        }
        if !origins.contains(c) { origins.append(c) }
    }
    var apiHost: String?, apiHeader: String?, apiPrefix: String?
    if kind == "api" {
        guard origins.isEmpty else { throw usageError("--origin is for secrets typed into pages; api secrets name --api-host") }
        guard let h = a.last("api-host")?.lowercased(), matches(h, "^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$") else {
            throw usageError("--api-host: the API's host name, like api.example.com (no scheme or path; :port allowed)")
        }
        apiHost = h
        let template = a.last("header") ?? "Authorization: Bearer {value}"
        guard splitTemplate(template) != nil, clean(template, max: 400) == template else {
            throw usageError("--header: 'Name: text with {value}' (once), like 'Authorization: Bearer {value}' or 'x-api-key: {value}'")
        }
        apiHeader = template
        if let p = a.last("api-path-prefix") {
            guard p.hasPrefix("/"), clean(p, max: 200) == p, !p.contains(".."), !p.contains("?"), !p.contains("#") else {
                throw usageError("--api-path-prefix: a path like /v1 (whole segments: it allows /v1 and /v1/..., never /v10)")
            }
            // Kept without a trailing slash: the proxy matches whole segments (pathWithin), so
            // /v1 and /v1/ mean the same, and / means every path.
            var norm = p
            while norm.hasSuffix("/") { norm.removeLast() }
            apiPrefix = norm.isEmpty ? nil : norm
        }
    } else {
        guard a.last("api-host") == nil, a.last("header") == nil, a.last("api-path-prefix") == nil else {
            throw usageError("--api-host, --header and --api-path-prefix go with --kind api")
        }
        guard !origins.isEmpty else { throw usageError("at least one --origin https://site is needed: a secret fills only into the sites it names") }
    }
    let approval = a.last("approval") ?? "session"
    guard ["session", "each"].contains(approval) else { throw usageError("--approval session or each") }
    let username = a.last("username")
    if let u = username {
        guard kind == "password" else { throw usageError("--username goes with --kind password") }
        guard !u.isEmpty, u.count <= 256, clean(u, max: 256) == u else { throw usageError("--username: 1-256 printable characters") }
    }
    var projects: [String] = []
    for p in a.all("project") {
        var isDir: ObjCBool = false
        let full = realPath(absolutePath(p)) ?? ""
        guard clean(full, max: 4096) == full, FileManager.default.fileExists(atPath: full, isDirectory: &isDir), isDir.boolValue else {
            throw usageError("--project \(p): an existing folder")
        }
        if !projects.contains(full) { projects.append(full) }
    }
    try requireTTY("set")
    installPromptSignalHandlers()
    let (key, existing) = try unlockKey(paths)
    let old = existing.secrets[name]
    if old != nil && !a.has("replace") {
        throw VaultError("a secret named \(name) exists: add --replace to rotate its value (or delete it first)")
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
    case "api":
        value = try readHidden("API key for \(name) (\(apiHost!)): ")
    default:
        value = try readHidden("Value for \(name): ")
    }
    defer { wipe(&value) }
    guard !value.isEmpty, value.count <= 4096, String(validating: value, as: UTF8.self) != nil else {
        throw VaultError("the value must be 1-4096 bytes of text")
    }
    if kind == "api", value.count < 8 || !value.allSatisfy({ (0x21...0x7E).contains($0) }) {
        throw VaultError("an API key is at least 8 printable ASCII characters, without spaces")
    }
    let stored = String(decoding: value, as: UTF8.self)
    let now = isoTime()
    try updateVault(paths, key) { p in
        p.secrets[name] = Secret(kind: kind, value: stored, username: username, origins: origins, approval: approval,
                                 created: old?.created ?? now, rotated: old == nil ? nil : now, last_used: old?.last_used,
                                 uses: old?.uses ?? 0, projects: projects.isEmpty ? nil : projects,
                                 api_host: apiHost, api_header: apiHeader, api_path_prefix: apiPrefix,
                                 gen: randomBytes(8).map { String(format: "%02x", $0) }.joined())
    }
    var rec: [String: Any] = ["op": old == nil ? "set" : "rotate", "result": "ok", "name": name, "kind": kind, "approval_mode": approval]
    if !origins.isEmpty { rec["origins"] = origins }
    if let h = apiHost { rec["api_host"] = h }
    if !projects.isEmpty { rec["projects"] = projects }
    audit(paths, rec, macKey: vaultAuditKey(paths, key))
    let where_ = kind == "api" ? "https://\(apiHost!)" + (apiPrefix ?? "") : origins.joined(separator: ", ")
    oprint((old == nil ? "Saved" : "Rotated") + " \(name) (\(kind)) for \(where_)"
           + (projects.isEmpty ? "" : ", only for Claude Code sessions in \(projects.map(tildePath).joined(separator: ", "))")
           + (approval == "each" ? "; every use asks at the unlock terminal." : "."))
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
    audit(paths, ["op": "delete", "result": "ok", "name": name], macKey: vaultAuditKey(paths, key))
    oprint("Deleted \(name).")
    return Exit.ok
}

// MARK: - bind

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
    let usage = "usage: bind VM --port P --host-key-line LINE --fill-key PATH --fill-user U --ops-key PATH --ops-user U [--proxy-key PATH --proxy-user U --proxy-port N] --gateway PATH"
    let a = try parseArgs(argv, values: ["port", "host-key-line", "fill-key", "fill-user", "ops-key", "ops-user", "proxy-key", "proxy-user",
                                         "proxy-port", "gateway"])
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
    var proxyKey: String?, proxyUser: String?, proxyPort: Int?
    if let pk = a.last("proxy-key") {
        proxyKey = try checkKeyFile(pk, "proxy-key")
        guard let pu = a.last("proxy-user"), validUser(pu) else { throw usageError("--proxy-user: the VM's proxy account (mcproxy)") }
        guard let pp = a.last("proxy-port").flatMap({ Int($0) }), pp >= 1024, pp <= 65535 else { throw usageError("--proxy-port: the proxy's port inside the VM") }
        proxyUser = pu
        proxyPort = pp
    }
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
    """ + (proxyUser.map { "\n  api tokens    as \($0), key \(proxyKey!), proxy at 127.0.0.1:\(proxyPort!) in the VM" } ?? "\n  api tokens    (none: this VM has no proxy account)") + """

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
                      fill_key_path: fillKey, fill_user: fillUser, proxy_key_path: proxyKey, proxy_user: proxyUser, proxy_port: proxyPort,
                      gateway_path: gateway, gateway_sha256: gwSHA, bound: isoTime())
    try updateVault(paths, key) { p in p.vms[vm] = b }
    audit(paths, ["op": "bind", "result": "ok", "vm": vm, "ssh_port": port, "host_key": fp, "gateway": gateway, "proxy": proxyUser != nil], macKey: vaultAuditKey(paths, key))
    oprint("Bound VM \(vm): 127.0.0.1:\(port), host key \(fp), fills as \(fillUser), sessions as \(opsUser)"
           + (proxyUser.map { ", api tokens as \($0)" } ?? "") + ", gateway \(gateway)")
    return Exit.ok
}

// MARK: - unlock

func cmdUnlock(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["for", "confirm"], flags: ["allow-multiplexer", "touch-id", "require-presence", "no-require-presence"])
    guard a.positional.isEmpty else {
        throw usageError("usage: unlock [--for 30m] [--confirm session|each] [--allow-multiplexer] [--touch-id] [--require-presence | --no-require-presence]")
    }
    guard !(a.has("require-presence") && a.has("no-require-presence")) else { throw usageError("--require-presence or --no-require-presence, not both") }
    let dur = a.last("for") ?? "30m"
    guard let seconds = parseDuration(dur), seconds <= 12 * 3600 else { throw usageError("--for: like 90s, 30m or 2h (at most 12h)") }
    let confirm = a.last("confirm") ?? "session"
    guard ["session", "each"].contains(confirm) else { throw usageError("--confirm session or each") }
    try requireTTY("unlock")
    let mux = terminalMultiplexer()
    let flag: Bool? = a.has("require-presence") ? true : (a.has("no-require-presence") ? false : nil)
    let bio = detectBiometry()
    // With --touch-id or presence mode the confirmation is the system's dialog (or the passphrase), which nothing
    // typed into the pane can give. (The vault's own setting is read after the passphrase; this uses what's known.)
    if let m = mux, confirm == "each", !a.has("allow-multiplexer"), !a.has("touch-id"), !(flag ?? (bio == .touchID)) {
        throw VaultError("this terminal runs inside \(m), where any program of yours can type into it (including the answer to a confirmation), so --confirm each would protect nothing. Unlock in a terminal outside \(m), add --require-presence or --touch-id to confirm with Touch ID or your login password instead, or add --allow-multiplexer if you accept that")
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
    let (key, payload) = try unlockKey(paths)
    let presence = resolvePresence(flag: flag, setting: payload.require_presence, biometry: bio)
    let server = UnlockServer(paths: paths, key: key, seconds: seconds, confirmEach: confirm == "each",
                              multiplexer: mux, allowMultiplexer: a.has("allow-multiplexer"), touchID: a.has("touch-id"),
                              presence: presence)
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

func printResult(_ res: [String: Any], _ keys: [String] = ["ok", "name", "vm", "field", "profile", "origin", "chars", "bytes", "route",
                                                           "request_id", "reason"]) {
    var out: [String: Any] = [:]
    for k in keys { if let v = res[k] { out[k] = v } }
    if out["ok"] == nil { out["ok"] = false }
    oprint(jsonLine((cleanAll(out) as? [String: Any]) ?? ["ok": false]))
}

/// --request-id ID (the id the request gets if it has to wait for approval; 8 hex digits, unused) and
/// --on-behalf-pid PID (the Claude Code process a client says it asks for: shown and recorded as unverified, never trusted).
func useOptions(_ a: Args, _ req: inout [String: Any]) throws {
    if let id = a.last("request-id") {
        guard validRequestID(id) else { throw usageError("--request-id: 8 hex digits (0-9, a-f)") }
        req["request_id"] = id
    }
    if let p = a.last("on-behalf-pid") {
        guard let n = Int32(p), n > 1 else { throw usageError("--on-behalf-pid: a process id") }
        req["on_behalf_pid"] = Int(n)
    }
}

/// A use refused because the vault is locked: recorded, and said in the client's output.
func lockedUse(_ paths: Paths, _ op: String, _ fields: [String: Any]) -> Int32 {
    var rec = fields
    rec["op"] = op
    rec["result"] = "locked"
    if op == "fill" || op == "login" { rec["event"] = "login.denied" }
    audit(paths, rec)
    var out = fields
    out["ok"] = false
    out["reason"] = "locked"
    printResult(out, ["ok", "name", "vm", "field", "profile", "reason"])
    eprint("mcomp-vault: \(lockedHint(paths))")
    return Exit.locked
}

func cmdFill(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["vm", "field", "profile", "selector", "request-id", "on-behalf-pid"])
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
    try useOptions(a, &req)
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 300) } catch ClientError.locked {
        return lockedUse(paths, "fill", ["name": name, "vm": vm, "field": field])
    }
    printResult(res)
    return exitFor(res)
}

func cmdLogin(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: login NAME --vm VM --password-selector CSS [--username-selector CSS] [--profile P] [--request-id ID]"
    let a = try parseArgs(argv, values: ["vm", "profile", "password-selector", "username-selector", "request-id", "on-behalf-pid"])
    guard a.positional.count == 1, let vm = a.last("vm"), let ps = a.last("password-selector") else { throw usageError(usage) }
    let name = a.positional[0]
    guard validName(name), validName(vm) else { throw usageError("names: letters, digits, '.', '_' and '-'") }
    var req: [String: Any] = ["op": "login", "name": name, "vm": vm]
    for (flag, key, v) in [("password-selector", "password_selector", Optional(ps)), ("username-selector", "username_selector", a.last("username-selector"))] {
        guard let v else { continue }
        guard !v.isEmpty, v.count <= 300 else { throw usageError("--\(flag): 1-300 characters of CSS that match exactly one field") }
        req[key] = v
    }
    if let p = a.last("profile") {
        guard validName(p) else { throw usageError("--profile: a driver profile name") }
        req["profile"] = p
    }
    try useOptions(a, &req)
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 300) } catch ClientError.locked {
        return lockedUse(paths, "login", ["name": name, "vm": vm])
    }
    printResult(res, ["ok", "name", "vm", "profile", "origin", "chars", "fields", "username_hint", "request_id", "reason"])
    return exitFor(res)
}

func cmdToken(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["vm", "ttl", "request-id", "on-behalf-pid"])
    guard a.positional.count == 1, let vm = a.last("vm"), validName(a.positional[0]), validName(vm) else {
        throw usageError("usage: token NAME --vm VM [--ttl 15m]")
    }
    guard let ttl = parseDuration(a.last("ttl") ?? "15m"), ttl <= 8 * 3600 else { throw usageError("--ttl: like 90s, 15m or 2h (at most 8h)") }
    let name = a.positional[0]
    var req: [String: Any] = ["op": "token", "name": name, "vm": vm, "ttl_s": ttl]
    try useOptions(a, &req)
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 300) } catch ClientError.locked {
        return lockedUse(paths, "token", ["name": name, "vm": vm])
    }
    printResult(res, ["ok", "name", "vm", "token_id", "token", "base_url", "header", "api_host", "expires", "expires_in_s", "reason"])
    return exitFor(res)
}

func cmdTokens(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["json"])
    let res: [String: Any]
    do { res = try request(paths, ["op": "tokens"], timeout: 15) } catch ClientError.locked {
        if a.has("json") { oprint(jsonLine(["ok": false, "reason": "locked", "tokens": []])) } else { oprint("The vault is locked: no tokens are live.") }
        return Exit.locked
    }
    let list = res["tokens"] as? [[String: Any]] ?? []
    if a.has("json") { oprint(jsonPretty(["tokens": list])); return Exit.ok }
    oprint(list.isEmpty ? "No live tokens." : "Live tokens:")
    for t in list {
        oprint(clean("  \(t["id"] ?? "")  \(t["name"] ?? "") on VM \(t["vm"] ?? ""), \(t["expires_in_s"] ?? 0) s left, \(t["requests"] ?? 0) requests"
                     + ((t["project"] as? String).map { ", for Claude Code in \(tildePath($0))" } ?? ""), max: 300))
    }
    return Exit.ok
}

func cmdRevoke(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["all"])
    guard a.has("all") != (a.positional.count == 1), a.positional.allSatisfy({ matches($0, "^[0-9a-f]{8}$") }) else {
        throw usageError("usage: revoke ID | revoke --all")
    }
    var req: [String: Any] = ["op": "revoke"]
    if let id = a.positional.first { req["id"] = id }
    do {
        let res = try request(paths, req, timeout: 15)
        let gone = res["revoked"] as? [String] ?? []
        oprint(gone.isEmpty ? "No such live token." : "Revoked \(gone.joined(separator: ", ")).")
        return Exit.ok
    } catch ClientError.locked {
        oprint("The vault is locked: no tokens are live.")
        return Exit.ok
    }
}

func cmdSession(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let usage = "usage: session save NAME --vm VM --profile P [--replace] | load NAME --vm VM [--profile P] | policy NAME --approval session|each | delete NAME"
    guard let sub = argv.first else { throw usageError(usage) }
    let a = try parseArgs(Array(argv.dropFirst()), values: ["vm", "profile", "approval", "request-id", "on-behalf-pid"], flags: ["replace"])
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
        audit(paths, ["op": "session_policy", "result": "ok", "name": name, "approval_mode": ap], macKey: vaultAuditKey(paths, key))
        oprint(ap == "session"
               ? (existing.sessions[name]!.alwaysAsks
                  ? "Saved, but loading \(name) still asks every time: it holds, or as a whole browser profile may hold, an identity provider's sign-in."
                  : "Loading \(name) into the VM it was saved from no longer asks (other VMs and replacing it still do).")
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
        audit(paths, ["op": "session_delete", "result": "ok", "name": name], macKey: vaultAuditKey(paths, key))
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
    try useOptions(a, &req)
    let res: [String: Any]
    do { res = try request(paths, req, timeout: 1800) } catch ClientError.locked {
        return lockedUse(paths, "session_\(sub)", ["name": name, "vm": vm, "profile": profile ?? ""])
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
        var out: [String: Any] = ["secrets": res["secrets"] ?? [], "vms": res["vms"] ?? [], "sessions": res["sessions"] ?? []]
        if let r = res["session_key_rotated"] { out["session_key_rotated"] = r }
        oprint(jsonPretty(out))
        return Exit.ok
    }
    let secrets = res["secrets"] as? [[String: Any]] ?? []
    oprint(secrets.isEmpty ? "No secrets yet (metacomputer secrets set ...)." : "Secrets:")
    for s in secrets {
        let kind = s["kind"] as? String ?? ""
        let use = kind == "api" ? "[token] https://\(s["api_host"] ?? "")\(s["api_path_prefix"] ?? "") (\(s["api_header"] ?? "") header)"
            : "[--field \((s["fields"] as? [String] ?? []).joined(separator: "/"))] sites \((s["origins"] as? [String] ?? []).joined(separator: " "))"
        let user = (s["username"] as? String).map { " user \($0)" } ?? ""
        let used = (s["last_used"] as? String).map { "last used \($0)" } ?? "never used"
        let ages = "created \(age(s["created"] as? String) ?? "?")" + ((s["rotated"] as? String).flatMap { age($0) }.map { ", rotated \($0)" } ?? ", never rotated")
        let projects = (s["projects"] as? [String]).map { "  only in \($0.map(tildePath).joined(separator: ", "))" } ?? ""
        oprint(clean("  \(s["name"] ?? "")  \(kind) \(use)\(user)  approval \(s["approval"] ?? "")  \(ages)  \(s["uses"] ?? 0) uses, \(used)\(projects)", max: 600))
    }
    let vms = res["vms"] as? [[String: Any]] ?? []
    if !vms.isEmpty { oprint("VMs:") }
    for v in vms {
        oprint(clean("  \(v["name"] ?? "")  127.0.0.1:\(v["ssh_port"] ?? "")  host key \(v["host_key"] ?? "")  fills as \(v["fill_user"] ?? "-")"
                     + "  sessions as \(v["user"] ?? "")" + ((v["proxy_user"] as? String).map { "  api tokens as \($0)" } ?? "")
                     + (v["needs_bind"] != nil ? "  (bound by an earlier version: bind again)" : ""), max: 400))
    }
    let sessions = res["sessions"] as? [[String: Any]] ?? []
    if !sessions.isEmpty { oprint("Sealed browser sessions:" + ((res["session_key_rotated"] as? String).flatMap { age($0) }.map { " (key rotated \($0))" } ?? "")) }
    for s in sessions {
        oprint(clean("  \(s["name"] ?? "")  profile \(s["profile"] ?? "") from VM \(s["vm"] ?? ""), saved \(s["saved_at"] ?? ""), \(s["bytes"] ?? 0) bytes, "
                     + "loaded \(s["loads"] ?? 0)x, approval \(s["approval"] ?? "each")", max: 400))
    }
    return Exit.ok
}

func cmdStatus(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: [], flags: ["json"])
    var out: [String: Any] = ["vault": paths.vault, "socket": paths.socket, "exists": vaultExists(paths), "build": Build.kind,
                              "unlocked": false]
    if let h = try? readHeader(paths) {
        out["format"] = h.version
        out["enclave"] = h.enclave != nil
        out["recovery_key"] = h.wraps?.contains { $0.kind == "recovery" } ?? false
    }
    let bio = detectBiometry()
    out["biometry"] = bio.rawValue
    if let res = try? request(paths, ["op": "status"], timeout: 10), res["ok"] as? Bool == true {
        out["unlocked"] = true
        for k in ["expires", "expires_in_s", "confirm", "pid", "since", "multiplexer", "tokens", "presence", "presence_why"] { if let v = res[k] { out[k] = v } }
    } else {
        // Locked: the default here (the vault's own setting, inside it, and an unlock flag can change it).
        let (on, why, _) = resolvePresence(flag: nil, setting: nil, biometry: bio)
        out["presence_default"] = on
        out["presence_why"] = why
    }
    if a.has("json") { oprint(jsonLine(out)); return Exit.ok }
    if !(out["exists"] as? Bool ?? false) {
        oprint("vault:    none yet (\(paths.vault)); create it with: metacomputer secrets init")
    } else if out["unlocked"] as? Bool == true {
        oprint("vault:    unlocked until \(out["expires"] ?? "?") (\(out["expires_in_s"] ?? 0) s left; confirm \(out["confirm"] ?? "?"); pid \(out["pid"] ?? "?"); \(out["tokens"] ?? 0) live tokens)")
    } else {
        oprint("vault:    locked (the user unlocks it with: metacomputer secrets unlock, in their own terminal)")
    }
    if out["exists"] as? Bool == true {
        oprint("keys:     format \(out["format"] ?? "?"), " + ((out["enclave"] as? Bool == true) ? "bound to this Mac's Secure Enclave, with a recovery key" : "passphrase only (no Secure Enclave)"))
    }
    if let on = out["presence"] as? Bool {
        oprint("approvals: " + (on ? "presence mode: Touch ID or the login password (or the passphrase) for every approval" : "typed at the unlock terminal")
               + " (\(out["presence_why"] ?? "?"))")
    } else if let on = out["presence_default"] as? Bool {
        oprint("approvals: " + (on ? "presence mode by default here" : "typed by default here") + " (\(out["presence_why"] ?? "?"); "
               + "`secrets presence on|off|auto` or an unlock flag changes it)")
    }
    oprint("file:     \(paths.vault)")
    oprint("socket:   \(paths.socket)")
    #if MCOMP_VAULT_TESTING
    oprint("build:    TEST (MCOMP_VAULT_TEST_* settings are honoured; never use it for real secrets)")
    #endif
    return Exit.ok
}

func cmdAudit(_ paths: Paths, _ argv: [String]) throws -> Int32 {
    let a = try parseArgs(argv, values: ["tail"], flags: ["json", "verify", "head"])
    let n = Int(a.last("tail") ?? "20") ?? 20
    guard fileExists(paths.audit) else { oprint("No audit records yet (\(paths.audit))."); return Exit.ok }
    if a.has("head") {
        // The last record as SEQ:HASH, for verify-audit.py --expect-head (note it at a moment you trust).
        let (seq, h) = lastAuditEvent(paths.audit)
        oprint("\(seq):\(h)")
        return Exit.ok
    }
    if a.has("verify") {
        // Only the unlock process holds the audit key that checks each record's creation MAC.
        let res: [String: Any]
        do { res = try request(paths, ["op": "audit_verify"], timeout: 120) } catch ClientError.locked {
            eprint("mcomp-vault: \(lockedHint(paths)) (checking the records' creation MACs needs the unlocked vault)")
            return Exit.locked
        }
        let ok = res["ok"] as? Bool == true
        if a.has("json") { oprint(jsonPretty(res)); return ok ? Exit.ok : Exit.error }
        guard res["records"] != nil else {
            // The check itself couldn't run (the log or the vault couldn't be read): never "checks out".
            eprint(clean("mcomp-vault: audit --verify failed: \(res["reason"] as? String ?? "no answer")", max: 400))
            return Exit.error
        }
        let probs = res["problems"] as? [String] ?? []
        oprint(clean("\(res["records"] ?? 0) records: \(res["verified"] ?? 0) with a valid creation MAC, \(res["unverified"] ?? 0) "
                     + "written without the vault key (refusals while locked), \(res["legacy"] ?? 0) from before the vault had an audit key; "
                     + "head \(res["head"] ?? 0):\(res["head_hash"] ?? "")", max: 500))
        for p in probs { oprint(clean("  BAD " + p, max: 300)) }
        if let n = res["problem_count"] as? Int, n > probs.count { oprint("  ... and \(n - probs.count) more") }
        if ok && probs.isEmpty {
            oprint("The local log checks out.")
            return Exit.ok
        }
        if let r = res["reason"] as? String, probs.isEmpty { oprint(clean("  BAD " + r, max: 300)) }
        oprint("The local log doesn't check out.")
        return Exit.error
    }
    let lines = String(decoding: try readFileBytes(paths.audit, max: 256 << 20), as: UTF8.self).split(separator: "\n").suffix(max(1, n))
    for l in lines {
        if a.has("json") { oprint(clean(String(l), max: 4000)); continue }
        guard let r = parseJSONObject(Array(l.utf8)) else { continue }
        var s = "#\(r["seq"] ?? "-") \(r["ts"] ?? r["time"] ?? "")  \(r["op"] ?? "")"
        for k in ["name", "field", "vm", "profile"] { if let v = r[k] { s += " \(k)=\(v)" } }
        s += "  -> \(r["result"] ?? "")"
        if let o = r["origin"] { s += " \(o)" }
        if let c = r["chars"] { s += " (\(c) chars)" }
        if let st = r["status"] { s += " HTTP \(st)" }
        if let ap = r["approval"] as? String, ap != "-" { s += " [approval \(ap)]" }
        if let rq = r["requester"] as? [String: Any], let p = rq["project"] as? String { s += " [Claude Code in \(tildePath(p))]" }
        if let why = r["reason"] { s += ": \(why)" }
        oprint(clean(s, max: 600))
    }
    return Exit.ok
}

func cmdVersion(_ argv: [String]) -> Int32 {
    let info: [String: Any] = ["build": Build.kind, "source": buildSourceHash, "enclave_available": enclaveAvailable()]
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
    // HMAC-SHA256 (RFC 4231 test case 2) and HKDF-SHA256 (RFC 5869 test case 1).
    check("HMAC-SHA256 RFC 4231 #2", hmacSHA256Hex(key: Array("Jefe".utf8), data: Array("what do ya want for nothing?".utf8))
          == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")
    check("HKDF-SHA256 RFC 5869 #1", sha256HexRaw(hkdf32([UInt8](repeating: 0x0b, count: 22), salt: hexToBytes("000102030405060708090a0b0c"),
                                                         info: hexToBytes("f0f1f2f3f4f5f6f7f8f9")))
          == "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf")
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
    let (rk, rkText) = newRecoveryKey()
    check("recovery key round trip", parseRecoveryKey(rkText) == rk && parseRecoveryKey(rkText.lowercased().replacingOccurrences(of: "-", with: " ")) == rk)
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
    check("KDF rounds out of range refused", (try? passphraseKey(Array("x".utf8), KDFParams(name: "pbkdf2-sha256", iterations: 1 << 40,
                                                                                          salt: Data(randomBytes(16)).base64EncodedString()))) == nil)
    // Canonical JSON, as Python's json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True) writes it.
    let sample: [String: Any] = ["b": 1, "a": [true, false, "x\"y\\z\n\u{7f}é😀"] as [Any], "c": ["n": -2] as [String: Any]]
    check("canonical JSON", canonicalJSON(sample)
          == "{\"a\":[true,false,\"x\\\"y\\\\z\\n\\u007f\\u00e9\\ud83d\\ude00\"],\"b\":1,\"c\":{\"n\":-2}}")
    check("API header templates", splitTemplate("Authorization: Bearer {value}").map { "\($0.0)|\($0.1)|\($0.2)" } == "Authorization|Bearer |"
          && splitTemplate("x-api-key: {value}") != nil && splitTemplate("x: {value}{value}") == nil && splitTemplate("x: none") == nil)
    oprint(failures == 0 ? "selftest: all passed (\(Build.kind) build)" : "selftest: \(failures) failed")
    return failures == 0 ? Exit.ok : Exit.failed
}

func sha256HexRaw(_ b: [UInt8]) -> String { b.map { String(format: "%02x", $0) }.joined() }

/// `selftest --local-auth`: one request to LocalAuthentication through the function every approval uses, with
/// no vault and no log. A release build runs it only with MCOMP_VAULT_LOCAL_AUTH=noninteractive, so it never
/// shows a dialog and can only report that nothing was allowed: tests use it to show that a release build
/// ignores the test builds' stand-in for the person (MCOMP_VAULT_TEST_LOCAL_AUTH).
func localAuthSelftest() -> Int32 {
    #if !MCOMP_VAULT_TESTING
    guard ProcessInfo.processInfo.environment["MCOMP_VAULT_LOCAL_AUTH"] == "noninteractive" else {
        eprint("mcomp-vault: selftest --local-auth runs only with MCOMP_VAULT_LOCAL_AUTH=noninteractive (otherwise it would show the Touch ID dialog)")
        return Exit.usage
    }
    #endif
    let a = startLocalAuth("check that nothing is approved here")
    let deadline = Date().addingTimeInterval(20)
    while !a.done && Date() < deadline { usleep(50_000) }
    if !a.done { a.cancel() }
    oprint(a.ok ? "local-auth: allowed" : "local-auth: not allowed (\(a.why))")
    return a.ok ? Exit.ok : Exit.failed
}
