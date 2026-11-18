// mcomp-vault: the user's terminal. Passphrases and values are read here with the
// echo off; the unlock process asks its questions here.
import Darwin
import Foundation

// Set by the signal handlers; read by the loops that wait (unlock, prompts, ssh).
nonisolated(unsafe) var receivedSignal: Int32 = 0
// The terminal settings to put back if a signal arrives while the echo is off.
nonisolated(unsafe) var savedTermios = termios()
nonisolated(unsafe) var termiosSaved: Int32 = 0

func requireTTY(_ what: String) throws {
    guard isatty(0) == 1 else {
        throw VaultError("\(what) needs your own terminal: run it yourself in Terminal (not through Claude, a script or a pipe)")
    }
}

/// While the echo is off, Ctrl-C puts the terminal back before the process ends.
func installPromptSignalHandlers() {
    let h: @convention(c) (Int32) -> Void = { sig in
        if termiosSaved != 0 { _ = tcsetattr(0, TCSAFLUSH, &savedTermios) }
        _ = write(2, "\n", 1)
        _exit(128 + sig)
    }
    signal(SIGINT, h)
    signal(SIGTERM, h)
    signal(SIGHUP, h)
}

/// A line from the terminal with the echo off. The bytes are the caller's to wipe.
func readHidden(_ prompt: String) throws -> [UInt8] {
    var old = termios()
    guard tcgetattr(0, &old) == 0 else { throw VaultError("cannot read the terminal settings") }
    var quiet = old
    quiet.c_lflag &= ~tcflag_t(ECHO | ECHONL)
    quiet.c_lflag |= tcflag_t(ICANON | ISIG)
    savedTermios = old
    termiosSaved = 1
    guard tcsetattr(0, TCSAFLUSH, &quiet) == 0 else { throw VaultError("cannot turn the terminal's echo off") }
    defer {
        _ = tcsetattr(0, TCSAFLUSH, &old)
        termiosSaved = 0
        eprint("")
    }
    eprintRaw(prompt)
    var line = [UInt8]()
    var c: UInt8 = 0
    while true {
        let n = read(0, &c, 1)
        if n < 0 && errno == EINTR { continue }
        if n <= 0 { break }
        if c == 0x0A { break }
        line.append(c)
        if line.count > 4096 {
            wipe(&line)
            throw VaultError("that line is too long")
        }
    }
    c = 0
    if line.last == 0x0D { line.removeLast() }
    return line
}

/// A line from the terminal with the echo on.
func readVisible(_ prompt: String) -> String {
    eprintRaw(prompt)
    var line = [UInt8]()
    var c: UInt8 = 0
    while true {
        let n = read(0, &c, 1)
        if n < 0 && errno == EINTR { continue }
        if n <= 0 || c == 0x0A { break }
        if line.count < 1024 { line.append(c) }
    }
    return String(decoding: line, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
}

func passphraseLength(_ p: [UInt8]) -> Int { String(decoding: p, as: UTF8.self).count }

/// The vault passphrase (and, for a new vault, the same again).
func readPassphrase(confirm: Bool) throws -> [UInt8] {
    var p = try readHidden(confirm ? "New vault passphrase (at least 12 characters): " : "Vault passphrase: ")
    if confirm {
        guard passphraseLength(p) >= 12 else {
            wipe(&p)
            throw VaultError("the passphrase must have at least 12 characters")
        }
        var again = try readHidden("The same passphrase again: ")
        defer { wipe(&again) }
        guard again == p else {
            wipe(&p)
            throw VaultError("the two passphrases differ")
        }
    }
    guard !p.isEmpty else { throw VaultError("no passphrase given") }
    return p
}

enum ApprovalScope { case use, session, unlock }

/// An approval on the unlock terminal. The person types the secret's (or session's) name
/// to allow this use, "NAME session" for this Claude Code session, or "NAME unlock" until
/// the vault locks; anything else refuses. Anything typed before the question is
/// discarded; no answer within the timeout, Ctrl-C or the terminal closing all refuse.
func askApproval(_ question: String, name: String, timeout: Int) -> ApprovalScope? {
    guard let answer = askLine(question + "\n  Type \(name) to allow this use (\(name) session: for this Claude Code session; "
                               + "\(name) unlock: until the vault locks). Anything else refuses: ", timeout: timeout) else { return nil }
    let words = answer.split(whereSeparator: { $0 == " " || $0 == "\t" }).map(String.init)
    guard words.first == name else { return nil }
    switch words.count == 1 ? "" : words.dropFirst().joined(separator: " ") {
    case "": return .use
    case "session": return .session
    case "unlock": return .unlock
    default: return nil
    }
}

/// One answer typed on the terminal (nil: none in time, or interrupted).
func askLine(_ question: String, timeout: Int) -> String? {
    guard timeout > 0 else { return nil }
    _ = tcflush(0, TCIFLUSH)
    eprintRaw(question)
    let deadline = Date().addingTimeInterval(TimeInterval(timeout))
    var line = [UInt8]()
    while Date() < deadline {
        if receivedSignal != 0 { eprint(""); return nil }
        var pfd = pollfd(fd: 0, events: Int16(POLLIN), revents: 0)
        let r = poll(&pfd, 1, 500)
        if r < 0 { if errno == EINTR { continue }; return nil }
        if r == 0 { continue }
        if pfd.revents & Int16(POLLHUP | POLLERR | POLLNVAL) != 0 && pfd.revents & Int16(POLLIN) == 0 { return nil }
        var c: UInt8 = 0
        let n = read(0, &c, 1)
        if n <= 0 { return nil }
        if c == 0x0A { return String(decoding: line, as: UTF8.self).trimmingCharacters(in: .whitespaces) }
        if line.count < 120 { line.append(c) }
    }
    eprint("\n  (no answer in \(timeout) s: not allowed)")
    return nil
}

/// The name of the terminal multiplexer (or scriptable terminal) this process runs in, or
/// nil. Any program of the user's can type into such a pane (tmux send-keys, screen -X
/// stuff, wezterm cli send-text, kitty @ send-text), so a confirmation typed there can be
/// forged. This is a list of known ones, not a proof of absence (SECURITY.md).
func terminalMultiplexer() -> String? {
    let env = ProcessInfo.processInfo.environment
    if env["TMUX"] != nil { return "tmux" }
    if env["STY"] != nil { return "screen" }
    if env["ZELLIJ"] != nil { return "zellij" }
    if env["WEZTERM_PANE"] != nil { return "WezTerm" }
    if env["KITTY_LISTEN_ON"] != nil { return "kitty (remote control)" }
    let known = ["tmux", "screen", "zellij", "byobu", "dtach", "abduco"]
    var pid = getppid()
    for _ in 0..<64 {
        guard pid > 1 else { break }
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        guard sysctl(&mib, 4, &info, &size, nil, 0) == 0, size > 0 else { break }
        let name = withUnsafeBytes(of: info.kp_proc.p_comm) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }.lowercased()
        if let k = known.first(where: { name == $0 || name.hasPrefix($0 + ":") || name.hasPrefix($0 + "-") }) { return k }
        pid = info.kp_eproc.e_ppid
    }
    return nil
}
