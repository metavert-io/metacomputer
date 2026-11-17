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

/// y/N on the unlock terminal. Anything typed before the question is discarded; no
/// answer within the timeout, Ctrl-C or the terminal closing all mean no.
func askYesNo(_ question: String, timeout: Int) -> Bool {
    _ = tcflush(0, TCIFLUSH)
    eprintRaw(question + " [y/N] ")
    let deadline = Date().addingTimeInterval(TimeInterval(timeout))
    var line = [UInt8]()
    while Date() < deadline {
        if receivedSignal != 0 { eprint(""); return false }
        var pfd = pollfd(fd: 0, events: Int16(POLLIN), revents: 0)
        let r = poll(&pfd, 1, 500)
        if r < 0 { if errno == EINTR { continue }; return false }
        if r == 0 { continue }
        if pfd.revents & Int16(POLLHUP | POLLERR | POLLNVAL) != 0 && pfd.revents & Int16(POLLIN) == 0 { return false }
        var c: UInt8 = 0
        let n = read(0, &c, 1)
        if n <= 0 { return false }
        if c == 0x0A {
            let a = String(decoding: line, as: UTF8.self).trimmingCharacters(in: .whitespaces).lowercased()
            return a == "y" || a == "yes"
        }
        if line.count < 16 { line.append(c) }
    }
    eprint("\n  (no answer in \(timeout) s: not allowed)")
    return false
}
