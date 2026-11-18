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
