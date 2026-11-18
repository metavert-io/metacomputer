"""Limits for what arrives over a connection the VM can write to.

The VM is untrusted. The hub, the relay, the SDK and the direct client already read
each message with a size limit; this module adds what a size limit doesn't cover:

- loads(): JSON that nests too deeply. A few kilobytes of `[[[[...` make Python's
  parser raise RecursionError, which the usual `except ValueError` doesn't catch, so
  a reader thread would die and leave its connection open and silent. Here it is a
  ValueError like any other bad message, and anything nested deeper than
  MAX_JSON_DEPTH is refused too, so later code (json.dumps, walks over a result)
  never meets a structure that deep. A message that would hold more than
  MAX_JSON_NODES values is refused before it's parsed, so a small frame can't
  turn into millions of Python objects; the count skips strings, so text full of
  commas or brackets (a CSV, source code, a page) isn't taken for structure. Both
  refusals are LimitError, so a reader can tell a message that is only too big for
  it (and answer the request it belongs to) from one that isn't JSON at all.
- StderrTail: a child's stderr (SSH carries the VM's) drained without growing: the
  last few kilobytes stay in memory for error messages, and a log file gets at most
  a fixed number of bytes.
"""
import json
import os
import re
import threading

MAX_JSON_DEPTH = 100          # MCP messages nest a handful of levels; Python's parser gives up near 1000
MAX_JSON_NODES = 1_000_000    # values in one message (a 5,000-element tree is tens of thousands; base64 has none)
STDERR_KEEP = 64 << 10        # what an error message can quote from a child's stderr
STDERR_FILE_MAX = 1 << 20     # what a child's stderr may add to its log file


class LimitError(ValueError):
    """A message over a content limit (too many values, nested too deeply), as opposed to one that isn't JSON."""


def _structural(seg, start=0, end=None):
    end = len(seg) if end is None else end
    if isinstance(seg, (bytes, bytearray)):
        return sum(seg.count(c, start, end) for c in (b"[", b"{", b",", b":"))
    return sum(seg.count(c, start, end) for c in ("[", "{", ",", ":"))


def _spans(data):
    """(in_string, start, end) for the text outside and inside JSON strings (quotes included), in order, in one
    linear pass: each quote is found with find(), and the backslashes right before it (counted back only to the
    quote before it, so no character is looked at twice) say whether it's escaped. An unterminated string runs to
    the end. No regular expression: nothing here can backtrack."""
    b = isinstance(data, (bytes, bytearray))
    q, bs = (b'"', b"\\") if b else ('"', "\\")
    n, pos = len(data), 0
    while True:
        i = data.find(q, pos)
        if i < 0:
            yield False, pos, n
            return
        yield False, pos, i
        prev = i
        while True:
            k = data.find(q, prev + 1)
            if k < 0:
                yield True, i, n
                return
            between = data[prev + 1:k]
            if (len(between) - len(between.rstrip(bs))) % 2 == 0:
                break                                # not escaped: the string ends here
            prev = k
        yield True, i, k + 1
        pos = k + 1


def _brackets(data):
    if isinstance(data, (bytes, bytearray)):
        return data.count(b"[") + data.count(b"{")
    return data.count("[") + data.count("{")


def _nodes(data, limit=None):
    """An upper bound on the Python objects a JSON text becomes: the top value, and one more for every container
    ([ or {), every value after the first in a container (a comma) and every object key (a colon), counted outside
    strings. When the same count over the whole text (strings included, so never lower) is within limit, that's
    the answer; otherwise strings are skipped (_spans, linear)."""
    quick = 1 + _structural(data)
    if limit is not None and quick <= limit:
        return quick
    return 1 + sum(_structural(data, s, e) for in_str, s, e in _spans(data) if not in_str)


_BRACKETS_B = re.compile(rb"[\[\]{}]")
_BRACKETS_S = re.compile(r"[\[\]{}]")
_INT_B = re.compile(rb"-?(?:0|[1-9][0-9]{0,17})(?=[ \t\r\n,}]|$)")
_INT_S = re.compile(r"-?(?:0|[1-9][0-9]{0,17})(?=[ \t\r\n,}]|$)")


def top_level_id(data, max_len=200):
    """The "id" member of the top-level JSON object in data, found without parsing it (for a message too big to
    parse, so the request it answers can be answered): a str (a plain string value: no escapes, at most max_len
    characters) or an int. None when the text isn't one object, has no such member, has it twice, or its value is
    anything else. Members of nested objects never count. One linear pass (_spans); nesting is followed by
    counting brackets outside strings, exactly wherever the top level could close."""
    b = isinstance(data, (bytes, bytearray))
    ws, colon, comma, lbrace, opens, shuts = (b" \t\r\n", b":", b",", b"{", (b"[", b"{"), (b"]", b"}")) if b else \
        (" \t\r\n", ":", ",", "{", ("[", "{"), ("]", "}"))
    idkey, bs, q = (b"id", b"\\", b'"') if b else ("id", "\\", '"')
    brackets, integer = (_BRACKETS_B, _INT_B) if b else (_BRACKETS_S, _INT_S)
    found = []
    depth, started, closed = 0, False, False
    last = None              # the last character before this string outside strings ('{' or ',': a key may follow)
    key = want_value = False
    for in_str, s, e in _spans(data):
        if closed:                                   # after the top-level object: nothing but whitespace
            if in_str or data[s:e].strip(ws):
                return None
            continue
        if in_str:
            if not started or e - s < 2 or data[e - 1:e] != q:
                return None
            if want_value:
                v = data[s + 1:e - 1]
                if len(v) > max_len or bs in v:
                    return None
                found.append(v.decode("utf-8", "replace") if b else v)
                want_value = False
            else:
                key = depth == 1 and last in (lbrace, comma) and data[s + 1:e - 1] == idkey
            last = None
            continue
        seg = data[s:e]
        if key:                                      # after the key "id": a colon, then its value
            key = False
            t = seg.lstrip(ws)
            if not t.startswith(colon):
                return None
            rest = t[1:].lstrip(ws)
            if not rest:
                want_value = True                    # a string comes next
            else:
                m = integer.match(rest)
                if not m:
                    return None                      # an object, array, float or literal: no request of ours
                found.append(int(m.group()))
        elif want_value:
            return None
        if not started:
            t = seg.lstrip(ws)
            if t:
                if t[:1] != lbrace:
                    return None                      # not an object (an array, a batch, a scalar)
                started = True
        closes = seg.count(shuts[0]) + seg.count(shuts[1])
        if closes and depth - closes <= 0:           # the top level may close in here: follow it bracket by bracket
            for m in brackets.finditer(seg):
                depth += 1 if m.group() in opens else -1
                if depth < 0:
                    return None
                if depth == 0 and started:
                    if seg[m.end():].strip(ws):
                        return None
                    closed = True
                    break
        else:
            depth += seg.count(opens[0]) + seg.count(opens[1]) - closes
        tail = seg.rstrip(ws)
        last = tail[-1:] if tail else None
    return found[0] if closed and len(found) == 1 else None


def _children(o):
    return iter(o.values()) if isinstance(o, dict) else iter(o)


def depth_exceeds(obj, limit):
    """True when obj (parsed JSON) nests containers more than limit levels deep. No recursion, and the stack holds
    one iterator per level (never every sibling), so its size is the depth."""
    if not isinstance(obj, (dict, list)):
        return False
    stack = [_children(obj)]
    while stack:
        if len(stack) > limit:
            return True
        for v in stack[-1]:
            if isinstance(v, (dict, list)):
                stack.append(_children(v))
                break
        else:
            stack.pop()
    return False


def loads(data, max_depth=None, max_nodes=None):
    """json.loads for a message off the wire. Raises ValueError (never RecursionError) when it isn't JSON, and
    LimitError (a ValueError) when it would hold more than max_nodes values (MAX_JSON_NODES, checked before anything
    is parsed) or nests deeper than max_depth (MAX_JSON_DEPTH)."""
    limit = MAX_JSON_DEPTH if max_depth is None else max_depth
    nodes = MAX_JSON_NODES if max_nodes is None else max_nodes
    if _nodes(data, nodes) > nodes:
        raise LimitError(f"JSON with more than {nodes:,} values")
    try:
        obj = json.loads(data)
    except RecursionError:
        raise LimitError("JSON nested too deeply") from None
    if _brackets(data) > limit and depth_exceeds(obj, limit):   # the count is a cheap upper bound on the depth
        raise LimitError(f"JSON nested more than {limit} levels deep")
    return obj


class StderrTail:
    """Drain a child's stderr on a thread of its own. The last `keep` bytes stay in memory (text()); at most
    `file_max` bytes go to the file at `path` (0600; appended to when append is true), then one line says the
    rest was left out. The child never blocks on a full pipe, and nothing grows without limit. withhold(): from then on
    nothing read is kept, in memory or the file (one line says so, and at the end how many bytes were left out)."""

    def __init__(self, stream, path=None, keep=None, file_max=None, append=False):
        self.stream = stream
        self.path = path
        self.keep = STDERR_KEEP if keep is None else keep
        self.file_max = STDERR_FILE_MAX if file_max is None else file_max
        self.append = append
        self.buf = bytearray()
        self.total = 0
        self.withheld = None        # why nothing read is kept any more (withhold), else None
        self.held = 0               # bytes read since then, none of them kept
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="stderr-tail")
        self.thread.start()

    def _open(self):
        if not self.path:
            return None
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if self.append else os.O_TRUNC)
        try:
            return os.fdopen(os.open(self.path, flags, 0o600), "ab" if self.append else "wb")
        except OSError:
            return None

    def withhold(self, why):
        """Keep nothing read from now on (sticky): a guest that may hold a secret writes here."""
        with self.lock:
            if self.withheld is None:
                self.withheld = str(why)[:200]

    def _run(self):
        f = self._open()
        written, cut, marked = 0, False, False
        try:
            fd = self.stream.fileno()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                with self.lock:
                    self.total += len(chunk)
                    withheld = self.withheld
                    if withheld is None:
                        self.buf += chunk
                        if len(self.buf) > self.keep:
                            del self.buf[:len(self.buf) - self.keep]
                    else:
                        self.held += len(chunk)
                if withheld is not None:
                    if f is not None and not marked:
                        try:
                            f.write(f"\n[metacomputer: from here on this stream is withheld ({withheld}); none of it is "
                                    "kept]\n".encode())
                            f.flush()
                        except (OSError, ValueError):
                            pass
                        marked = True
                    continue
                if f is not None and not cut:
                    room = self.file_max - written
                    try:
                        if len(chunk) <= room:
                            f.write(chunk)
                            written += len(chunk)
                        else:
                            f.write(chunk[:max(room, 0)])
                            f.write(f"\n[metacomputer: more than {self.file_max} bytes on stderr; the rest was "
                                    "left out]\n".encode())
                            cut = True
                        f.flush()
                    except (OSError, ValueError):
                        cut = True
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            if f is not None:
                try:
                    if self.held:
                        f.write(f"[metacomputer: {self.held} bytes withheld]\n".encode())
                    f.close()
                except (OSError, ValueError):
                    pass
            try:
                self.stream.close()
            except (OSError, AttributeError):
                pass
            self.done.set()

    def text(self, wait=0.0):
        """The last bytes read, as text. wait: seconds to wait for the stream's end first."""
        if wait:
            self.done.wait(wait)
        with self.lock:
            return bytes(self.buf).decode("utf-8", "replace")
