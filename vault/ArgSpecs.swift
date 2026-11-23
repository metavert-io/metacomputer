// mcomp-vault: what each command line takes, declared once.
//
// Every command (and each subcommand, and each form a command picks by an option: `pay --purchase`, `pay --done`,
// `fill --request`, `token renew`) declares the options it reads, as one value (given once), a list (given any number
// of times, read with all()) or a flag, and how many words it takes. runMain checks a command line against its own
// form's declaration before the command runs, so nothing a command wouldn't read gets through to be dropped: an option
// of another form or subcommand (`rule remove ID --site X`), a second value, a word too many (`revoke --all ID ID`), a
// request for usage given where a value or a word goes (`--site --help`, `-hx`). Each handler then reads its arguments
// with its command's options (parseArgs(_:command:)), from this same table.
import Foundation

struct ArgSpec: Sendable {
    var values: Set<String> = []
    var lists: Set<String> = []
    var flags: Set<String> = []
    var words: ClosedRange<Int> = 0...0
    var options: Set<String> { values.union(lists).union(flags) }
}

private let useOpts: Set<String> = ["request-id", "on-behalf-pid"]
private let cardValues: Set<String> = ["pay", "label", "currency", "max-purchase", "max-per"]
private let cardLists: Set<String> = ["merchant-max", "allow-frame"]
private let cardFlags: Set<String> = ["no-caps", "single-use", "merchant-locked", "no-frames"]

/// Every form, by its name: the command, then its subcommand or the option that picks the form.
let argSpecs: [String: ArgSpec] = [
    "init": ArgSpec(flags: ["enclave", "no-enclave", "passphrase"]),     // --passphrase: other vaults' word; it always asks
    "rekey": ArgSpec(flags: ["enclave", "no-enclave"]),
    "recover": ArgSpec(flags: ["no-enclave"]),
    "rotate-session-key": ArgSpec(),
    "set": ArgSpec(values: Set(["kind", "username", "approval", "api-host", "api-path-prefix", "answer-timeout", "header"]).union(cardValues),
                   lists: Set(["origin", "project"]).union(cardLists), flags: Set(["replace"]).union(cardFlags), words: 1...1),
    "delete": ArgSpec(words: 1...1),
    "bind": ArgSpec(values: ["port", "host-key-line", "fill-key", "fill-user", "ops-key", "ops-user", "proxy-key", "proxy-user", "proxy-port",
                             "gateway", "host", "host-key-alias", "tunnel", "tunnel-id"],
                    lists: ["tunnel-arg", "tunnel-check-arg"], words: 1...1),
    "unlock": ArgSpec(values: ["for", "confirm"],
                      flags: ["allow-multiplexer", "touch-id", "require-presence", "no-require-presence", "passphrase"]),
    "lock": ArgSpec(flags: ["json"]),
    "list": ArgSpec(flags: ["json"]),
    "status": ArgSpec(flags: ["json"]),
    "fill": ArgSpec(values: Set(["vm", "field", "profile", "selector", "origin", "browser-pid", "window-id"]).union(useOpts),
                    flags: ["human"], words: 1...1),
    "fill --request": ArgSpec(values: ["request", "vm", "selector", "profile", "browser-pid", "window-id"], flags: ["human"]),
    "login": ArgSpec(values: Set(["vm", "profile", "password-selector", "username-selector", "origin", "browser-pid"]).union(useOpts),
                     words: 1...1),
    "ask": ArgSpec(values: ["vm", "site", "kind", "why", "into", "window-id", "profile", "browser-pid", "ttl", "wait", "pattern", "length",
                            "request-id"], flags: ["human", "json"]),
    "give": ArgSpec(values: ["vm"], flags: ["json"], words: 1...1),
    "code": ArgSpec(values: Set(["vm", "site", "kind", "why", "into", "window-id", "profile", "browser-pid", "pattern", "length", "ttl"])
                        .union(useOpts), flags: ["human", "json"]),
    "relay": ArgSpec(values: Set(["vm", "from", "to", "site", "kind", "pattern", "length", "profile", "from-profile", "browser-pid", "ttl"])
                         .union(useOpts), flags: ["json", "human"]),       // --human: refused with what to use instead
    "pay": ArgSpec(values: Set(["vm", "origin", "total-selector", "total-label", "window-id", "profile", "browser-pid"]).union(useOpts),
                   lists: ["frame-origin", "fill"], flags: ["human"], words: 1...1),
    "pay --purchase": ArgSpec(values: ["purchase", "vm", "field", "selector"], flags: ["human"]),
    "pay --done": ArgSpec(values: ["purchase", "vm"], flags: ["done"]),
    "shop-complete": ArgSpec(values: Set(["vm", "shop", "instrument", "account"]).union(useOpts), words: 1...1),
    "shop-card": ArgSpec(values: Set(["spend-request", "wait"]).union(useOpts), words: 1...1),
    "shop-cli set": ArgSpec(values: ["cli", "runtime", "auth-file"]),
    "shop-cli show": ArgSpec(),
    "spend-resolve": ArgSpec(values: ["extra"], flags: ["discard-pending"], words: 0...1),
    "ap2 init": ArgSpec(flags: ["replace"]),
    "ap2 key": ArgSpec(flags: ["json"]),
    "ap2 sign": ArgSpec(values: Set(["vm", "origin", "checkout-jwt", "ttl"]).union(useOpts), words: 1...1),
    "wba init": ArgSpec(values: ["agent"], flags: ["replace"]),
    "wba on": ArgSpec(),
    "wba off": ArgSpec(),
    "wba status": ArgSpec(flags: ["json"]),
    "wba directory": ArgSpec(values: ["days"], flags: ["headers"]),
    "wba sign": ArgSpec(words: 1...1),
    "merchant-rules": ArgSpec(values: ["file", "ucp-file"], flags: ["json"], words: 0...1),
    "requests": ArgSpec(flags: ["json"]),
    "settle": ArgSpec(values: ["timeout"]),
    "approve": ArgSpec(values: ["scope", "vm"], flags: ["passphrase", "json"], words: 1...1),
    "deny": ArgSpec(flags: ["json"], words: 1...1),
    "rule add": ArgSpec(values: ["secret", "site", "session", "project", "for"], flags: ["passphrase"]),
    "rule add code": ArgSpec(values: ["site", "from", "project", "expires"], lists: ["link-host"], flags: ["passphrase"], words: 1...1),
    "rule add relay": ArgSpec(values: ["from-host", "to", "project", "expires"], lists: ["link-host"], flags: ["passphrase"], words: 1...1),
    "rule list": ArgSpec(flags: ["json"]),
    "rule remove": ArgSpec(words: 1...1),
    "policy": ArgSpec(values: Set(["approval", "answer-timeout"]).union(cardValues), lists: cardLists, flags: cardFlags, words: 1...1),
    "presence": ArgSpec(words: 1...1),
    "import-passwords": ArgSpec(values: ["browser", "profile", "csv"], lists: ["site"], flags: ["dry-run"]),
    "import-session": ArgSpec(values: ["browser", "app", "profile", "name", "vm", "to-profile"], lists: ["site"],
                              flags: ["allow-identity-provider", "replace", "dry-run"]),
    "session save": ArgSpec(values: Set(["vm", "profile"]).union(useOpts), flags: ["replace"], words: 1...1),
    "session load": ArgSpec(values: Set(["vm", "profile"]).union(useOpts), lists: ["site"], words: 1...1),
    "session policy": ArgSpec(values: ["approval"], words: 1...1),
    "session delete": ArgSpec(words: 1...1),
    "token": ArgSpec(values: Set(["vm", "ttl"]).union(useOpts), words: 1...1),
    "token renew": ArgSpec(values: Set(["vm", "ttl"]).union(useOpts), flags: ["all"], words: 0...1),
    "tokens": ArgSpec(flags: ["json"]),
    "revoke": ArgSpec(values: ["vm"], flags: ["all"], words: 0...1),
    "disconnect": ArgSpec(values: ["vm"]),
    "audit-sink add": ArgSpec(values: ["kind", "url", "ca"], lists: ["header"], flags: ["required"], words: 1...1),
    "audit-sink list": ArgSpec(flags: ["json"]),
    "audit-sink show": ArgSpec(flags: ["key", "json"], words: 1...1),
    "audit-sink test": ArgSpec(words: 1...1),
    "audit-sink remove": ArgSpec(words: 1...1),
    "audit": ArgSpec(values: ["tail"], flags: ["json", "verify", "head"]),
    "selftest": ArgSpec(flags: ["local-auth"]),
    "version": ArgSpec(flags: ["json"]),
    "help": ArgSpec(),
    "-h": ArgSpec(),
    "--help": ArgSpec(),
]

/// The commands whose first word is a subcommand (`rule add ...`): its form is "COMMAND SUB".
let subcommandCommands: Set<String> = ["ap2", "wba", "rule", "session", "audit-sink", "shop-cli"]

/// Commands that don't take arguments of their own: ssh's ProxyCommand for a provider's tunnel passes them on whole.
let passThroughCommands: Set<String> = ["_tunnel"]

/// A request for usage: -h or --help, and the words a person may mean as one (-hx, --help=x, --h, --he, --hel).
func isHelpWord(_ s: String) -> Bool {
    if s == "-h" || s == "--help" || s == "--h" || s == "--he" || s == "--hel" || s.hasPrefix("--help=") { return true }
    return s.hasPrefix("-h") && !s.hasPrefix("--")
}

/// The options of every form of COMMAND (its subcommands' and its option-picked forms' too), for its handler to read
/// its arguments with once runMain has checked them against their own form.
func commandOptions(_ command: String) -> ArgSpec {
    var u = ArgSpec(words: 0...Int.max)
    for (k, s) in argSpecs where k == command || k.hasPrefix(command + " ") {
        u.values.formUnion(s.values)
        u.lists.formUnion(s.lists)
        u.flags.formUnion(s.flags)
    }
    return u
}

/// Which form a command line is (its name in argSpecs) and the arguments that form's declaration is checked against,
/// picked as the command's handler picks it; nil when the handler refuses it itself before doing anything (a missing
/// or unknown subcommand: each says its usage).
func argSpecForm(_ cmd: String, _ rest: [String]) throws -> (String, [String])? {
    if subcommandCommands.contains(cmd) {
        guard let sub = rest.first, argSpecs["\(cmd) \(sub)"] != nil else { return nil }
        let args = Array(rest.dropFirst())
        if cmd == "rule" && sub == "add" {
            // `rule add code|relay ...`: the handler takes the first word after the options as the kind.
            let a = try scanArgs(args, commandOptions("rule add"))
            if let kind = a.positional.first, kind == "code" || kind == "relay" { return ("rule add \(kind)", args) }
        }
        return ("\(cmd) \(sub)", args)
    }
    switch cmd {
    case "token":
        // As cmdToken picks it: `token renew` with --all or a token id is a renewal; anything else is a token for a
        // secret (one named "renew" too).
        if rest.first == "renew" && (rest.contains("--all") || rest.dropFirst().contains(where: { matches($0, "^[0-9a-f]{8}$") })) {
            return ("token renew", Array(rest.dropFirst()))
        }
        return ("token", rest)
    case "fill":
        let a = try scanArgs(rest, commandOptions("fill"))
        return (a.values["request"] != nil ? "fill --request" : "fill", rest)
    case "pay":
        let a = try scanArgs(rest, commandOptions("pay"))
        return (a.flags.contains("done") ? "pay --done" : (a.values["purchase"] != nil ? "pay --purchase" : "pay"), rest)
    default:
        return argSpecs[cmd] == nil ? nil : (cmd, rest)
    }
}

/// runMain's check, before the command runs: the command line against its own form's declaration (parseArgs(_:form:)).
/// A command with no declaration and no subcommand is refused here, so a new one can't slip past.
func checkArgs(_ cmd: String, _ rest: [String]) throws {
    if passThroughCommands.contains(cmd) { return }
    try refusePassphraseStdin(rest)       // its own words for an option people try
    guard let (form, args) = try argSpecForm(cmd, rest) else {
        if subcommandCommands.contains(cmd) || argSpecs[cmd] != nil { return }   // the handler says its usage
        throw usageError("unknown command '\(cmd)' (mcomp-vault help)")
    }
    _ = try parseArgs(args, form: form)
}

/// --name VALUE / --name=VALUE for the options that take a value (each once, unless it's a list), --name for flags, and
/// the words, all by SPEC: an option it doesn't declare is refused, as is a request for usage given as a value or a word.
/// Word counts aren't checked (scanArgs is how a form is picked, and how a handler reads its words).
func scanArgs(_ argv: [String], _ spec: ArgSpec, form: String? = nil) throws -> Args {
    var a = Args()
    var i = 0
    let of = form.map { " of `\($0)`" } ?? ""
    while i < argv.count {
        let x = argv[i]
        if x.hasPrefix("--") && x.count > 2 {
            var name = String(x.dropFirst(2))
            var inline: String?
            if let eq = name.firstIndex(of: "=") {
                inline = String(name[name.index(after: eq)...])
                name = String(name[..<eq])
            }
            if spec.values.contains(name) || spec.lists.contains(name) {
                if a.values[name] != nil && !spec.lists.contains(name) {
                    throw usageError("--\(name) given twice")
                }
                let v: String
                if let s = inline {
                    v = s
                } else {
                    guard i + 1 < argv.count else { throw usageError("--\(name) needs a value") }
                    v = argv[i + 1]
                    i += 1
                }
                if isHelpWord(v) { throw usageError("--\(name) needs a value, not a request for usage (-h, --help) (mcomp-vault help)") }
                a.values[name, default: []].append(v)
            } else if spec.flags.contains(name) && inline == nil {
                if a.flags.contains(name) { throw usageError("--\(name) given twice") }    // a flag once, as a value
                a.flags.insert(name)
            } else if spec.flags.contains(name) {
                throw usageError("--\(name) takes no value")
            } else if isHelpWord(x) {
                throw usageError("a request for usage (-h, --help) isn't read after a command: the usage is `mcomp-vault help` (nothing was run)")
            } else if let f = form, commandOptions(String(f.split(separator: " ")[0])).options.contains(name) {
                throw usageError("--\(name) isn't an option\(of) (mcomp-vault help)")
            } else {
                throw usageError("unknown option --\(clean(name, max: 60))\(of) (mcomp-vault help)")
            }
        } else {
            if isHelpWord(x) {
                throw usageError("a request for usage (-h, --help) isn't read after a command: the usage is `mcomp-vault help` (nothing was run)")
            }
            a.positional.append(x)
        }
        i += 1
    }
    return a
}

/// A command line of the form FORM (a name in argSpecs): its options and its words, every one declared for that form.
func parseArgs(_ argv: [String], form: String) throws -> Args {
    guard let spec = argSpecs[form] else { throw VaultError("no declaration for `\(form)`") }
    let a = try scanArgs(argv, spec, form: form)
    guard spec.words.contains(a.positional.count) else {
        let (lo, hi) = (spec.words.lowerBound, spec.words.upperBound)
        let n = lo == hi ? (lo == 0 ? "no words" : "\(lo) word\(lo == 1 ? "" : "s")") : (lo == 0 ? "at most \(hi) word\(hi == 1 ? "" : "s")" : "\(lo) to \(hi) words")
        // The words aren't repeated: one may be a value typed where it doesn't go (`give ID 482913`).
        let hint = ["give", "code"].contains(form) ? "; a value goes in on stdin, never on the command line" : ""
        throw usageError("usage: `\(form)` takes \(n) besides its options (got \(a.positional.count)\(hint)) (mcomp-vault help)")
    }
    return a
}

/// A handler's arguments, read with every option of its command's forms. runMain has checked them against their own
/// form (checkArgs) before the handler runs.
func parseArgs(_ argv: [String], command: String) throws -> Args {
    try scanArgs(argv, commandOptions(command))
}

#if MCOMP_VAULT_TESTING
/// Test builds only: every form's declaration, as JSON (tests/test_vault_ops.py walks them).
func cmdArgSpecs() -> Int32 {
    var out: [String: Any] = [:]
    for (k, s) in argSpecs {
        out[k] = ["values": s.values.sorted(), "lists": s.lists.sorted(), "flags": s.flags.sorted(),
                  "min": s.words.lowerBound, "max": s.words.upperBound]
    }
    oprint(jsonLine(out))
    return Exit.ok
}
#endif
