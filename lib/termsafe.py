"""Text from the VM (or a web page) on its way to a terminal.

Window titles, page text, file names and messages from the virtual computer are
data. Printed raw, an escape sequence in them could move the cursor, rewrite what
is on screen, spoof a prompt or set the clipboard (OSC 52). escape_for_terminal()
shows such characters as visible escapes instead. JSON output (--json) is not
passed through here: it stays exact, and json.dumps escapes control characters
itself.
"""
import re

# Control characters (but newline and tab), DEL, C1 controls and the bidirectional
# overrides and isolates, which can move the cursor, rewrite what's on screen, set the
# clipboard (OSC 52) or make text read differently from what it is. Lone surrogates
# (bytes that weren't UTF-8, decoded with surrogateescape) are shown as escapes too.
UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩\ud800-\udfff]")


def _escaped(m):
    c = ord(m.group())
    if 0xDC80 <= c <= 0xDCFF:          # a byte that wasn't UTF-8 (surrogateescape)
        return f"\\x{c - 0xDC00:02x}"
    return f"\\x{c:02x}" if c < 0x100 else f"\\u{c:04x}"


def escape_for_terminal(text):
    """text with every unsafe character shown as \\xNN or \\uNNNN. Bytes are decoded as
    UTF-8 first, and bytes that aren't UTF-8 are shown as \\xNN."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "backslashreplace")
    return UNSAFE.sub(_escaped, str(text))


def filter_stream(src, dst, limit=1 << 20):
    """Copy src to dst (binary files) escaped, line by line, as it arrives: the shell's way to put text from the
    VM (an SSH command's stderr, a recorder's log) on a terminal. After `limit` bytes one line says the rest was
    left out, and the rest is read and dropped, so the writer never blocks and nothing grows."""
    sent = 0
    cut = False
    while True:
        line = src.readline(65536)
        if not line:
            break
        if cut:
            continue
        if sent + len(line) > limit:
            dst.write(f"[metacomputer: more than {limit} bytes; the rest was left out]\n".encode())
            dst.flush()
            cut = True
            continue
        sent += len(line)
        end = b"\n" if line.endswith(b"\n") else b""
        dst.write(escape_for_terminal(line[:len(line) - len(end)]).encode() + end)
        dst.flush()


if __name__ == "__main__":
    # ... | termsafe.py [--max BYTES]   (bin/metacomputer and scripts/lib.sh: the VM's stderr on a terminal)
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import strictargs           # its command line, by metacomputer's rules, before anything is read
    _p = strictargs.expect(sys.argv[1:], ["[--max BYTES]"], prog="termsafe.py")
    lim = 1 << 20
    try:
        lim = int(_p.opts.get("--max", lim))
    except ValueError:
        print("termsafe.py: --max takes a number of bytes", file=sys.stderr)
        sys.exit(2)
    try:
        filter_stream(sys.stdin.buffer, sys.stdout.buffer, lim)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
