"""The hub's own words in a result, and nothing else in their place (lib/mcp_hub.py; SECURITY.md "Hidden page text").

The hub adds notes to what the driver answers: a refusal's explanation, the Chrome guard's and bounded mode's notes,
the hidden-text marks. A client sees them in the text part of a result and in its structured part, and Claude Code
shows the model the structured part instead of the text when a result has one, so each note goes in both: the
text block, and a key of the hub's own (`metacomputer_note`, `metacomputer_hidden_text`).

The VM is untrusted and writes the rest of the answer, so on every answer the hub passes on, errors included, before
it adds anything, reserve() goes through all of it (every content block, every string and key at any depth of the
result and of an error; no inspection limit to hide behind):
- a key named like the hub's (`metacomputer_...`) moves to `upstream_metacomputer_...` (kept, under a name that says
  where it came from);
- a string with a hub note's opening anywhere in it ("metacomputer hub", "metacomputer bridge", matched across
  spacing, invisible characters, punctuation and look-alike letters) gets UPSTREAM_TAG in front, so it can't pass for
  the hub's own.
Nothing is removed. It's one pass over the answer; the opening is found by a regular expression with no nested
repeats (each letter, then any run of characters that aren't letters).

After everything (reserve, the hidden-text marks, the hub's notes), fit() checks the final answer against what a
client takes, bytes and values; one past that is replaced by the hub's own notes alone, as an error that says so.
"""
import json
import re

import wirelimits

PREFIX = "metacomputer_"                 # the hub's keys in a structured result
UPSTREAM = "upstream_"                    # where an answer's own key of that name goes
NOTE_KEY = "metacomputer_note"
HIDDEN_KEY = "metacomputer_hidden_text"
UPSTREAM_TAG = "[text from the virtual computer, not from the metacomputer hub] "
HUB_OPENINGS = ("metacomputer hub", "metacomputer bridge", "refused by the metacomputer bridge")
FIT_MARGIN_BYTES = 64 << 10               # room kept below a client's byte limit ...
FIT_MARGIN_NODES = 1000                   # ... and below its value limit
NOTES_MAX_ADDED = 256 << 10               # what the hub's notes, marks and tags add to an answer, at most
TOO_BIG = ("The virtual computer's answer, with the hub's notes added, was larger than a client takes ({} bytes), so "
           "only the hub's notes are passed on, not the answer. Ask for less: a query, a smaller max_elements, or a "
           "region.")
TOO_BIG_ERROR = ("The virtual computer's error was larger than a client takes ({} bytes), so it isn't passed on. Call "
                 "again, or check the outcome with a small read.")

# Look-alikes that read as the same letter (fullwidth forms, Cyrillic and Greek letters).
_ALIKE = {"m": "\uff4d\u043c", "e": "\uff45\u0435\u0451\u0454", "t": "\uff54\u0442\u03c4", "a": "\uff41\u0430\u03b1",
          "c": "\uff43\u0441\u03f2", "o": "\uff4f\u043e\u03bf\u0585", "p": "\uff50\u0440\u03c1", "u": "\uff55\u03c5",
          "r": "\uff52\u0433", "h": "\uff48\u04bb", "b": "\uff42\u044c\u0432", "i": "\uff49\u0456\u03b9",
          "d": "\uff44\u0501", "g": "\uff47\u0261"}
# Between letters: anything that isn't a letter or digit (spaces, invisible characters, punctuation, Markdown).
_GAP = r"[^\w]*"


def _letters(word):
    out = []
    for ch in word:
        if ch == " ":
            continue
        alike = _ALIKE.get(ch, "")
        out.append("[" + re.escape(ch + ch.upper() + alike + alike.upper()) + "]")
    return _GAP.join(out)


OPENING_RE = re.compile(_letters("metacomputer") + _GAP + "(?:" + _letters("hub") + "|" + _letters("bridge") + ")")


def looks_like_hub(text):
    """Does this text carry a hub note's opening anywhere (across spacing, invisible characters, punctuation and
    look-alike letters)?"""
    return isinstance(text, str) and OPENING_RE.search(text) is not None


def reserve(msg):
    """Make an answer from the driver unable to speak in the hub's name (see the module's notes). In place, over all
    of the result and the error; returns (how many keys and strings changed, bytes added). Never raises."""
    changed, added = 0, 0
    try:
        if not isinstance(msg, dict):
            return 0, 0
        stack = [msg[k] for k in ("result", "error", "params") if isinstance(msg.get(k), (dict, list))]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k in [k for k in node if isinstance(k, str) and k.lower().startswith(PREFIX)]:
                    new = UPSTREAM + k
                    while new in node:
                        new = UPSTREAM + new
                    node[new] = node.pop(k)
                    changed, added = changed + 1, added + len(new) - len(k)
                items = node.items()
            else:
                items = enumerate(node)
            for k, v in list(items):
                if isinstance(v, str):
                    if looks_like_hub(v):
                        node[k] = UPSTREAM_TAG + v
                        changed, added = changed + 1, added + len(UPSTREAM_TAG)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
    except Exception:
        pass
    return changed, added


def add(res, text, front=True):
    """One of the hub's notes, in the text part (first, or last) and under NOTE_KEY in the structured part (first),
    joined to a hub note already there. Nothing from the VM can be under that key: reserve() ran first."""
    if not isinstance(res, dict) or not text:
        return
    content = res.get("content") if isinstance(res.get("content"), list) else []
    block = {"type": "text", "text": text}
    if front:
        content.insert(0, block)
    else:
        content.append(block)
    res["content"] = content
    sc = res.get("structuredContent")
    if isinstance(sc, dict):
        old = sc.get(NOTE_KEY) if isinstance(sc.get(NOTE_KEY), str) else ""
        note = (text + "\n" + old if front else old + "\n" + text) if old else text
        rest = [(k, v) for k, v in sc.items() if k != NOTE_KEY]
        sc.clear()
        sc[NOTE_KEY] = note
        sc.update(rest)


def fit_notification(msg, approx_size, max_bytes, max_nodes):
    """A notification (progress) from the driver after its changes: passed on when it fits, else dropped (None)."""
    if approx_size is not None and approx_size + NOTES_MAX_ADDED < min(max_bytes - FIT_MARGIN_BYTES,
                                                                       max_nodes - FIT_MARGIN_NODES):
        return msg
    return msg if fits(msg, max_bytes, max_nodes)[0] else None


def _hub_block(c):
    """A content block the hub added: it opens like a hub note and reserve() didn't mark it (it marks every block from
    the VM with an opening anywhere in it)."""
    t = c.get("text") if isinstance(c, dict) else None
    return isinstance(t, str) and not t.startswith(UPSTREAM_TAG) and t[:40].lower().startswith(HUB_OPENINGS)


def fits(msg, max_bytes, max_nodes):
    """Does this message, as the hub sends it, stay within what a client takes (with room to spare)?"""
    try:
        s = json.dumps(msg, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return False, 0
    ok = len(s) <= max_bytes - FIT_MARGIN_BYTES and \
        wirelimits._nodes(s, max_nodes - FIT_MARGIN_NODES) <= max_nodes - FIT_MARGIN_NODES
    return ok, len(s)


def fit(msg, approx_size, max_bytes, max_nodes):
    """The final check of an answer, after every change the hub made. Small answers (approx_size, an upper bound,
    well under both limits) pass unlooked-at; others are serialized and measured. One past a limit is replaced: a
    result by the hub's own notes and marks alone (an error that says so), an error by a fixed message."""
    if approx_size is not None and approx_size + NOTES_MAX_ADDED < min(max_bytes - FIT_MARGIN_BYTES,
                                                                       max_nodes - FIT_MARGIN_NODES):
        return msg
    ok, size = fits(msg, max_bytes, max_nodes)
    if ok:
        return msg
    out = {k: v for k, v in msg.items() if k in ("jsonrpc", "id")}
    res = msg.get("result")
    if isinstance(res, dict):
        sc = res.get("structuredContent") if isinstance(res.get("structuredContent"), dict) else {}
        blocks = [c for c in (res.get("content") if isinstance(res.get("content"), list) else [])[:64] if _hub_block(c)]
        note = TOO_BIG.format(size)
        new_sc = {NOTE_KEY: (sc[NOTE_KEY] + "\n" + note) if isinstance(sc.get(NOTE_KEY), str) else note}
        if isinstance(sc.get(HIDDEN_KEY), dict):
            new_sc[HIDDEN_KEY] = sc[HIDDEN_KEY]
        out["result"] = {"content": blocks + [{"type": "text", "text": note}], "isError": True,
                         "structuredContent": new_sc}
        if not fits(out, max_bytes, max_nodes)[0]:       # never: the hub's notes are small; but never send it either
            out["result"] = {"content": [{"type": "text", "text": note}], "isError": True,
                             "structuredContent": {NOTE_KEY: note}}
    else:
        err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
        code = err.get("code") if isinstance(err.get("code"), int) and not isinstance(err.get("code"), bool) else -32603
        out["error"] = {"code": code, "message": TOO_BIG_ERROR.format(size)}
    return out
