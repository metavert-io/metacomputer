"""A contributor's manifest values on their way into a file made from them.

The kinds of event sink, the providers and the coding agents each live in a folder of their own (sinks/, providers/,
coding-agents/), so that a pull request from outside can change one folder and nothing else. That makes a manifest's
values a contributor's input: to the vault's build (Swift), to the shell metacomputer runs (bin/metacomputer and the
generated lib/cloud/providers.sh and lib/agentkit/agents.sh), to a URL or a header, to CODEOWNERS and to the docs. So
every such value is held to a whitelist of its own before anything is made from it (the checks below, which each
framework's check calls with the field's name: lib/sinkkit/manifests.py and vault.py, lib/cloud/manifests.py and
capabilities.py, lib/agentkit/manifests.py and recognition.py; the generators, scripts/build-vault.sh and the tests run
those), and is then written for where it lands (the writers below), so a value that somehow got past its check still
can't change what the file does.

A check returns None when the value is one, else what it should be, for a message that names the folder and the field.
A writer raises ValueError for a value it can't write safely. Nothing here reads a manifest or names one. Works under
any python3 (macOS's 3.9 included)."""
import re

# --- checks: None when the value is one, else what it should be -------------------------------------------------------
TEXT_LIMIT = 200
# A path a URL is given (the part after its host): letters, digits and . _ ~ / - only, so it can't add a user, a host, a
# port, a query or a fragment to the URL it's appended to.
URL_PATH = re.compile(r"/[A-Za-z0-9._~/-]*")
# A Content-Type: a type, /, a subtype, then any number of ; name=value parameters, each an HTTP token.
TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
CONTENT_TYPE = re.compile(rf"{TOKEN}/{TOKEN}(?:; ?{TOKEN}={TOKEN})*")
# A structured-data ID (RFC 5424's SD-NAME): 1 to 32 printable ASCII characters but =, space, ] and ".
SD_ID = re.compile(r"[\x21\x23-\x3c\x3e-\x5c\x5e-\x7e]{1,32}")
# A CODEOWNERS owner: a GitHub @login or an @org/team.
OWNER = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99})?")
# A page of the docs (docs/<name>.md).
DOCS_PAGE = re.compile(r"docs/[a-z0-9_-]+\.md")


def text(v, limit=TEXT_LIMIT):
    """One line of printable characters, not blank, at most `limit` long: no line break, no control character, no
    format character (a bidirectional override, a zero-width one), no separator but the space."""
    if not isinstance(v, str) or not v.strip():
        return "text"
    if len(v) > limit:
        return f"at most {limit} characters"
    if not v.isprintable():
        return "one line of printable characters (no line break, control or format character)"
    return None


def matches(v, pattern, what, limit=None):
    """v is a string the regular expression pattern (a str or a compiled one) matches whole, at most limit long."""
    if not isinstance(v, str) or (limit is not None and len(v) > limit) or not re.fullmatch(pattern, v):
        return what
    return None


def number(v, lo, hi):
    """A whole number from lo to hi (never True or False)."""
    if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
        return None
    return f"a whole number from {lo} to {hi}"


def port(v):
    return number(v, 1, 65535)


def url_path(v):
    if (isinstance(v, str) and len(v) <= 256 and URL_PATH.fullmatch(v) and "//" not in v and ".." not in v):
        return None
    return "a path that starts with / and has only letters, digits and . _ ~ / - (no //, no .., at most 256 characters)"


def content_type(v):
    return matches(v, CONTENT_TYPE, "a content type: TYPE/SUBTYPE, then any ; NAME=VALUE (HTTP tokens), at most 128 "
                                    "characters", 128)


def sd_id(v):
    return matches(v, SD_ID, "a structured-data ID: 1 to 32 printable ASCII characters, none of = ] \" or a space")


def owners(v):
    """A list of GitHub @names (@login or @org/team), one at least."""
    if isinstance(v, (list, tuple)) and 0 < len(v) <= 20 and all(isinstance(o, str) and OWNER.fullmatch(o)
                                                               for o in v):
        return None
    return "a list of GitHub @names (@login or @org/team)"


def docs_page(v):
    return matches(v, DOCS_PAGE, "a page under docs/ (docs/NAME.md: lowercase letters, digits, - and _)")


# --- writers: the value, written for where it lands ---------------------------------------------------------------
def swift_string(v):
    """A Swift string literal of v: \\ and " escaped, and every character outside printable ASCII as \\u{...}."""
    if not isinstance(v, str):
        raise ValueError(f"not text: {v!r}")
    out = []
    for c in v:
        if c in '\\"':
            out.append("\\" + c)
        elif " " <= c <= "~":
            out.append(c)
        elif 0xD800 <= ord(c) <= 0xDFFF:
            raise ValueError("a lone surrogate can't be in a Swift string")
        else:
            out.append("\\u{%x}" % ord(c))
    return '"' + "".join(out) + '"'


def swift_int(v, lo=-(2 ** 63), hi=2 ** 63 - 1):
    """A Swift Int literal of v, a whole number from lo to hi (never True or False)."""
    if number(v, lo, hi):
        raise ValueError(f"not a whole number from {lo} to {hi}: {v!r}")
    return str(v)


def sh_quote(v):
    """v as one shell word, in single quotes (a ' in it closed, escaped and reopened): nothing in it is expanded."""
    v = str(v)
    if "\0" in v:
        raise ValueError("a NUL can't be in a shell word")
    return "'" + v.replace("'", "'\\''") + "'"


def sh_comment(v):
    """v as part of a shell comment's line: every character that could end that line or reach a terminal as it is (a
    line break, a control or format character) shown as ?."""
    return "".join(c if c.isprintable() else "?" for c in str(v))


_MD_ESCAPE = re.compile(r"([\\`|<>\[\]])")


def _one_line(v):
    return "".join(c if c.isprintable() else " " for c in str(v))


def md_text(v):
    """v as plain Markdown text (a table's cell, a list item, a heading): a line break, control or format character as
    a space, and \\ ` | < > [ ] escaped, so it can't start a link, an image, HTML, code, a new cell or a new line."""
    return _MD_ESCAPE.sub(r"\\\1", _one_line(v))


def md_code(v, table=False):
    """v as a Markdown code span, fenced with more backticks than any run in it; in a table's cell (table) its | is
    escaped, which GitHub's tables read inside a code span too."""
    s = _one_line(v)
    if table:
        s = s.replace("|", "\\|")
    fence = "`" * (max((len(r) for r in re.findall(r"`+", s)), default=0) + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def md_link(label, dest):
    """A Markdown link: its text as md_text, its destination a relative path (and #section) of safe characters."""
    if not isinstance(dest, str) or not re.fullmatch(r"[A-Za-z0-9._~/#-]+", dest):
        raise ValueError(f"not a link's destination: {dest!r}")
    return f"[{md_text(label)}]({dest})"


def codeowners_line(path, names):
    """A CODEOWNERS line: a folder (/a/b/) and its owners, each a GitHub @name."""
    if not isinstance(path, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)+", path) or ".." in path:
        raise ValueError(f"not a folder for CODEOWNERS: {path!r}")
    if owners(list(names)):
        raise ValueError(f"{path}: not GitHub @names: {list(names)!r}")
    return f"{path} {' '.join(names)}"
