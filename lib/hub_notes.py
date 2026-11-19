"""The hub's own words in a result, and nothing else in their place (lib/mcp_hub.py; SECURITY.md "Hidden page text").

The hub adds notes to what the driver answers: a refusal's explanation, the Chrome guard's and bounded mode's notes,
the hidden-text marks. A client sees them in the text part of a result and in its structured part, and Claude Code
shows the model the structured part instead of the text when a result has one, so each note goes in both: the
text block, and a key of the hub's own (`metacomputer_note`, `metacomputer_hidden_text`).

The VM is untrusted and writes the rest of the answer, so on every answer the hub passes on, errors included, before
it adds anything, reserve() goes through all of it (every content block, every string and key at any depth of the
result and of an error; no inspection limit to hide behind):
- a key named like the hub's (`metacomputer_...`, in any case or look-alike letters) moves to
  `upstream_metacomputer_...` (kept, under a name that says where it came from);
- a string or a key with a hub note's opening anywhere in it ("metacomputer hub", "metacomputer bridge", matched
  across spacing, punctuation, invisible characters and look-alike letters) gets UPSTREAM_TAG in front, so it can't
  pass for the hub's own.
Nothing is removed. It's one pass over the answer; the opening is found by a regular expression with no nested
repeats (each letter, then any run of characters that aren't letters), in the text as a reader sees it (as_read: the
characters a reader doesn't see taken out, and look-alikes the letter classes don't hold folded to the letter).

The boundary: what's authoritative is the reserved structured keys (NOTE_KEY, HIDDEN_KEY), which only the hub writes;
the tag on look-alike openings is best effort (letters that look alike are endless; the common ones are covered).

After everything (reserve, the hidden-text marks, the hub's notes), fit() checks the final answer against what a
client takes, bytes and values; one past that is replaced by the hub's own notes alone (every block the hub added,
wherever it sits: they're Block objects, which nothing parsed from the VM is), as an error that says so.
"""
import json
import re
import unicodedata

import wirelimits

PREFIX = "metacomputer_"                 # the hub's keys in a structured result
UPSTREAM = "upstream_"                    # where an answer's own key of that name goes
NOTE_KEY = "metacomputer_note"
HIDDEN_KEY = "metacomputer_hidden_text"
UPSTREAM_TAG = "[text from the virtual computer, not from the metacomputer hub] "
FIT_MARGIN_BYTES = 64 << 10               # room kept below a client's byte limit ...
FIT_MARGIN_NODES = 1000                   # ... and below its value limit
NOTES_MAX_ADDED = 256 << 10               # what the hub's notes, marks and tags add to an answer, at most
TOO_BIG = ("The virtual computer's answer, with the hub's notes added, was larger than a client takes ({} bytes), so "
           "only the hub's notes are passed on, not the answer. Ask for less: a query, a smaller max_elements, or a "
           "region.")
TOO_BIG_ERROR = ("The virtual computer's error was larger than a client takes ({} bytes), so it isn't passed on. Call "
                 "again, or check the outcome with a small read.")

# --- characters a reader doesn't see ----------------------------------------------------------------------------------
# Unicode's Default_Ignorable_Code_Point (DerivedCoreProperties.txt) and every other format character. unicodedata
# names the format characters (category Cf) for the Unicode version Python ships; the rest of that property, which
# unicodedata can't name, is Other_Default_Ignorable_Code_Point and Variation_Selector (PropList.txt), listed here.
# Some of them (the Hangul fillers) count as letters to \w, so "anything that isn't a letter" alone misses them.
OTHER_IGNORABLE = (
    (0x034F, 0x034F),       # combining grapheme joiner
    (0x115F, 0x1160),       # Hangul choseong and jungseong fillers (letters, Lo)
    (0x17B4, 0x17B5),       # Khmer inherent vowels
    (0x180B, 0x180F),       # Mongolian free variation selectors (and the vowel separator, Cf)
    (0x2065, 0x2065),       # reserved, among the invisible operators
    (0x3164, 0x3164),       # Hangul filler (a letter, Lo)
    (0xFE00, 0xFE0F),       # variation selectors 1-16
    (0xFFA0, 0xFFA0),       # halfwidth Hangul filler (a letter, Lo)
    (0xFFF0, 0xFFF8),       # reserved, before the specials
    (0xE0000, 0xE0FFF),     # tags, variation selectors 17-256, reserved
)
# Unicode assigns characters in planes 0-3 and 14 only (4-13 are unassigned, 15-16 private use); scanning those keeps
# the hub's start quick. tests/test_hidden_text.py checks the scan against all of Unicode.
SCANNED_PLANES = ((0x0, 0x40000), (0xE0000, 0xF0000))


def _runs(cps):
    """Code points as a character class's body (runs of neighbours as ranges)."""
    out, cps = [], sorted(cps)
    i = 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        out.append(re.escape(chr(cps[i])) + ("-" + re.escape(chr(cps[j])) if j > i else ""))
        i = j + 1
    return "".join(out)


def ignorable_code_points(planes=SCANNED_PLANES):
    cps = {cp for lo, hi in planes for cp in range(lo, hi) if unicodedata.category(chr(cp)) == "Cf"}
    for lo, hi in OTHER_IGNORABLE:
        cps.update(range(lo, hi + 1))
    return cps


IGNORABLE = _runs(ignorable_code_points())
IGNORABLE_RE = re.compile("[" + IGNORABLE + "]+")   # taken out before matching (lib/hidden_text.py norm, excerpt)

# --- letters that read as the same letter -------------------------------------------------------------------------
# Every compatibility form whose NFKC is the letter (fullwidth, circled, superscript, mathematical, ...: from
# unicodedata, in the first two planes where they all are), and Cyrillic and Greek letters that look the same. Those
# that are letters to \w in the first plane (BMP) are in the expression's letter classes; the rest (circled letters,
# which aren't letters to \w; mathematical and squared letters, past the BMP) are folded to the letter first (FOLD),
# only in text that has one of them, so the classes stay fast lookups and apart from the spacing between letters.
_CONFUSABLE = {"m": "\u043c", "e": "\u0435\u0451\u0454", "t": "\u0442\u03c4", "a": "\u0430\u03b1",
               "c": "\u0441\u03f2", "o": "\u043e\u03bf\u0585", "p": "\u0440\u03c1", "u": "\u03c5", "r": "\u0433",
               "h": "\u04bb", "b": "\u044c\u0432", "i": "\u0456\u03b9\u0131", "d": "\u0501", "g": "\u0261"}
_WORD_RE = re.compile(r"[^\W_]")


def _alike():
    out = {ch: set(ch + ch.upper() + _CONFUSABLE.get(ch, "") + _CONFUSABLE.get(ch, "").upper())
           for ch in "metacomputerhubbridge"}
    fold = {}
    for cp in range(0x80, 0x20000):
        n = unicodedata.normalize("NFKC", chr(cp))
        if len(n) == 1 and n.lower() in out:
            if cp < 0x10000 and _WORD_RE.match(chr(cp)):
                out[n.lower()].add(chr(cp))
            else:
                fold[cp] = n.lower()
    return out, fold


_ALIKE, FOLD = _alike()
FOLD_RE = re.compile("[" + _runs(cp for cp in FOLD if cp < 0x10000) + "\U00010000-\U0010ffff]")
# Between letters: anything that isn't a letter or digit (spaces, punctuation, underscores, Markdown). Disjoint from
# the letters, so a failed match gives back each character once: the search stays linear (no nested repeats).
_GAP = r"[\W_]*"


def _letters(word):
    return _GAP.join("[" + _runs(ord(c) for c in _ALIKE[ch]) + "]" for ch in word if ch != " ")


OPENING_RE = re.compile(_letters("metacomputer") + _GAP + "(?:" + _letters("hub") + "|" + _letters("bridge") + ")")
NAME_RE = re.compile(_GAP + _letters("metacomputer"))     # a key named like the hub's (at its start)


def as_read(text):
    """Text as the openings are matched in: without the characters a reader doesn't see (IGNORABLE_RE), and with the
    look-alike letters outside the letter classes folded (FOLD). ASCII text has neither: as is."""
    if text.isascii():
        return text
    text = IGNORABLE_RE.sub("", text)
    return text.translate(FOLD) if FOLD_RE.search(text) else text


def looks_like_hub(text):
    """Does this text carry a hub note's opening anywhere (across spacing, punctuation, characters a reader doesn't
    see, and look-alike letters)?"""
    return isinstance(text, str) and OPENING_RE.search(as_read(text)) is not None


def _key_front(key):
    """What goes in front of a key from the VM: UPSTREAM_TAG when it has a hub note's opening in it (words, as in a
    value), UPSTREAM when it's named like the hub's keys (`metacomputer_...`, in any case or look-alike letters),
    else None (left alone)."""
    v = as_read(key)
    if OPENING_RE.search(v):
        return UPSTREAM_TAG
    return UPSTREAM if NAME_RE.match(v) else None


class Block(dict):
    """A content block the hub made. fit() keeps every one of these, wherever it sits; nothing parsed from the VM's
    JSON is one (json.loads makes plain dicts)."""


def block(text):
    return Block(type="text", text=text)


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
                for k, front in [(k, _key_front(k)) for k in node if isinstance(k, str)]:
                    if front is None:
                        continue
                    new = front + k
                    while new in node:
                        new = front + new
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
    if front:
        content.insert(0, block(text))
    else:
        content.append(block(text))
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
    result by the hub's own notes and marks alone (every Block, wherever it sat, and the reserved structured keys; an
    error that says so), an error by a fixed message."""
    if approx_size is not None and approx_size + NOTES_MAX_ADDED < min(max_bytes - FIT_MARGIN_BYTES,
                                                                       max_nodes - FIT_MARGIN_NODES):
        return msg
    ok, size = fits(msg, max_bytes, max_nodes)
    if ok:
        return msg
    out = {k: v for k, v in msg.items() if k in ("jsonrpc", "id")}
    res = msg.get("result")
    if isinstance(res, dict):
        had_sc = isinstance(res.get("structuredContent"), dict)
        sc = res["structuredContent"] if had_sc else {}
        blocks = [c for c in (res.get("content") if isinstance(res.get("content"), list) else [])
                  if isinstance(c, Block)]
        note = TOO_BIG.format(size)
        if had_sc:      # add() kept its notes under NOTE_KEY, hidden_text its own under HIDDEN_KEY
            new_sc = {NOTE_KEY: (sc[NOTE_KEY] + "\n" + note) if isinstance(sc.get(NOTE_KEY), str) else note}
        else:           # the notes were in the text part only; the structured part is what Claude Code shows
            new_sc = {NOTE_KEY: "\n".join([c["text"] for c in blocks] + [note])}
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
