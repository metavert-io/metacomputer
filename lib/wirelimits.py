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


_QUOTE_S = re.compile(r'(\\*)"')
_QUOTE_B = re.compile(rb'(\\*)"')


def _structural(seg):
    if isinstance(seg, (bytes, bytearray)):
        return seg.count(b"[") + seg.count(b"{") + seg.count(b",") + seg.count(b":")
    return seg.count("[") + seg.count("{") + seg.count(",") + seg.count(":")


def _brackets(data):
    if isinstance(data, (bytes, bytearray)):
        return data.count(b"[") + data.count(b"{")
    return data.count("[") + data.count("{")


def _nodes(data, limit=None):
    """An upper bound on the Python objects a JSON text becomes: the top value, and one more for every container
    ([ or {), every value after the first in a container (a comma) and every object key (a colon), counted outside
    strings. When the same count over the whole text (strings included, so never lower) is within limit, that's
    the answer; otherwise strings are skipped in one pass over the quotes, each quote's run of backslashes saying
    whether it ends a string."""
    quick = 1 + _structural(data)
    if limit is not None and quick <= limit:
        return quick
    pat = _QUOTE_B if isinstance(data, (bytes, bytearray)) else _QUOTE_S
    total, start, inside = 1, 0, False
    for m in pat.finditer(data):
        if inside:
            if not len(m.group(1)) % 2:          # not escaped: the string ends here
                inside, start = False, m.end()
        else:
            total += _structural(data[start:m.start()])
            inside = True
    if not inside:
        total += _structural(data[start:])
    return total


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
    rest was left out. The child never blocks on a full pipe, and nothing grows without limit."""

    def __init__(self, stream, path=None, keep=None, file_max=None, append=False):
        self.stream = stream
        self.path = path
        self.keep = STDERR_KEEP if keep is None else keep
        self.file_max = STDERR_FILE_MAX if file_max is None else file_max
        self.append = append
        self.buf = bytearray()
        self.total = 0
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

    def _run(self):
        f = self._open()
        written, cut = 0, False
        try:
            fd = self.stream.fileno()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                with self.lock:
                    self.total += len(chunk)
                    self.buf += chunk
                    if len(self.buf) > self.keep:
                        del self.buf[:len(self.buf) - self.keep]
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
                    f.close()
                except OSError:
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
