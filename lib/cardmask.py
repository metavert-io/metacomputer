"""Card details hidden in what an agent reads (SECURITY.md, "Paying for things").

Once the vault has typed a card's parts into a page in the VM, the page shows them: a later window read (Firefox's
accessibility tree holds a field's text), a page's own text, an error message or a progress note would hand them to the
agent, and into its transcript. The hub (lib/mcp_hub.py, on_upstream) passes every message from the driver through
mask_message before anything else sees it (its guards, traces, notes, events and every client), and the action-log relay
(lib/mcp_relay.py) and the hub's own diagnostics (driver stderr, connection errors) use the same masks before they log or
answer: answers, errors and notifications, keys and values, at any depth. A message it can't finish checking isn't
passed on.

Always: a card number. A run of 12-19 digits that starts like a card (2-6) and passes the card check, written whole or in
groups with up to three separators between them (spaces of any kind, dashes, dots, slashes, quotes and commas: so a number
split across a JSON list's strings counts too), standing on its own (no letter or digit right before or after it, so a
hash's or an identifier's digits aren't touched), and a JSON number of that shape (a whole number or a float with nothing
after the point), becomes CARD_MASK. (A number run into other digits or letters, or spelled some other way on purpose,
isn't caught: pattern masking can't stop a VM that means to hide one.)

While a purchase is open (the vault's <vault folder>/purchases-active.json, which holds shapes and a fingerprint, never a
part), for the VM it's open on:
- a card number split across fields or rows: groups of 3-6 digits close together in one text, or in neighbouring values
  of one JSON object or list (strings or numbers);
- anything shaped like an expiry (MM/YY, not inside a path or a fraction), and the value of a field named like one
  (expiry, exp_month, expiration_year ...);
- the security code: a number of its length in a field named like one (cvc, cvv, security_code ...), as the value of an
  object that names one (an accessibility node "Security code" with its value), or in text after such a name on the
  same line;
- the postal code: by its shape in such a field or object, after such a name in text, and anywhere in text when its
  shape has letters (UK, Canada);
- words whose salted SHA-256 is the cardholder's name (the same words both sides: letters, apostrophes and hyphens, so
  "J. Example" is "j example").
These are shapes, not values, so masking never confirms a value; the name's fingerprint confirms a guess of it, and no
more. Nothing else is changed: identifiers ("[123]" element refs, ports, words with digits in them) keep their digits,
and encoded binary data (an image's or audio's base64 `data`, a resource's non-text `blob`, data: URLs of media) is
never touched, so a screenshot stays a screenshot. Its pixels aren't masked (SECURITY.md says so).
"""
import base64
import collections
import hashlib
import json
import os
import re
import stat
import threading
import time

CARD_MASK = "[card number hidden by metacomputer]"
EXP_MASK = "[card expiry hidden by metacomputer]"
CVC_MASK = "[card code hidden by metacomputer]"
ZIP_MASK = "[postal code hidden by metacomputer]"
NAME_MASK = "[cardholder name hidden by metacomputer]"
MAX_NODES = 2_000_000                 # a message with more values than this isn't checked: it isn't passed on
CARD_START = "23456"


class Unfinished(Exception):
    """The check couldn't finish: the caller doesn't pass the message on."""


def luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_card(digits):
    return 12 <= len(digits) <= 19 and digits.isdigit() and digits[0] in CARD_START and luhn(digits)


def real_iin(digits):
    """Whether a number starts like a card a network issues (Visa, Mastercard incl. 2221-2720, Amex, Diners, Discover,
    JCB, UnionPay, Maestro): for numbers pieced together from separate groups, where four years in a row ("2021",
    "2022", ...) could pass the card check by chance."""
    d2, d4 = int(digits[:2]), int(digits[:4])
    return (digits[0] == "4" or 51 <= d2 <= 55 or 2221 <= d4 <= 2720 or d2 in (34, 37, 36, 38, 39, 62, 65, 50, 56, 57, 58)
            or 300 <= int(digits[:3]) <= 305 or d4 == 6011 or 644 <= int(digits[:3]) <= 649 or 3528 <= d4 <= 3589 or digits[:2] == "67")


_GROUP = re.compile(r"[0-9]+")


def _groups(text):
    return [(m.start(), m.end(), m.group()) for m in _GROUP.finditer(text)]


def _joinable(gap):
    """Up to three characters between two digit groups that keep them one number: anything but letters and digits."""
    return 1 <= len(gap) <= 3 and not any(c.isalnum() for c in gap)


def _bounded(text, s, e):
    """A number that stands on its own: no letter or digit right before or after it (so a hash's or base64's digits, or
    an identifier's, aren't read as a card number)."""
    return (s == 0 or not text[s - 1].isalnum()) and (e >= len(text) or not text[e].isalnum())


def _card_spans(text, groups):
    """[start, end) spans of card numbers: digit groups joined by separators, any run of whole groups of card length,
    standing on its own."""
    spans = []
    i = 0
    while i < len(groups):
        j = i
        while j + 1 < len(groups) and _joinable(text[groups[j][1]:groups[j + 1][0]]):
            j += 1
        run = groups[i:j + 1]
        for a in range(len(run)):
            digits = ""
            for b in range(a, len(run)):
                digits += run[b][2]
                if len(digits) > 19:
                    break
                if is_card(digits) and _bounded(text, run[a][0], run[b][1]):
                    spans.append((run[a][0], run[b][1]))
        i = j + 1
    return spans


def _split_card_spans(text, groups, gap_max=160):
    """While a purchase is open: a number split across fields or rows: 2-6 groups of 3-6 digits, each within gap_max
    characters of the one before, that together make a card number. Each of those groups is masked."""
    small = [g for g in groups if 3 <= len(g[2]) <= 6 and _bounded(text, g[0], g[1])]
    spans = []
    for a in range(len(small)):
        digits = ""
        for b in range(a, min(a + 6, len(small))):
            if b > a and small[b][0] - small[b - 1][1] > gap_max:
                break
            digits += small[b][2]
            if len(digits) > 19:
                break
            if b > a and is_card(digits) and real_iin(digits):
                spans += [(g[0], g[1]) for g in small[a:b + 1]]
    return spans


def _active_card_spans(text, groups):
    """While a purchase is open on that VM: any run of 12 or more digits, written whole or in groups joined by up to three
    characters that aren't letters or digits, whatever is around it and whether or not it passes the card check. Masks
    more than a card number (a long id, a timestamp) while the purchase is open; that's the trade (SECURITY.md)."""
    spans = []
    i = 0
    while i < len(groups):
        j = i
        while j + 1 < len(groups) and _joinable(text[groups[j][1]:groups[j + 1][0]]):
            j += 1
        if sum(len(g[2]) for g in groups[i:j + 1]) >= 12:
            spans.append((groups[i][0], groups[j][1]))
        i = j + 1
    return spans


def _replace(text, spans, mask):
    if not spans:
        return text, 0
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, last = [], 0
    for s, e in merged:
        out.append(text[last:s])
        out.append(mask)
        last = e
    out.append(text[last:])
    return "".join(out), len(merged)


# An expiry: MM/YY or MM-YYYY, not part of a path (/12/25/), a fraction (1.12/25) or a longer number.
_EXP = re.compile(r"(?<![0-9.,/\-])(?:0?[1-9]|1[0-2])\s{0,2}[/\-]\s{0,2}(?:20)?[0-9]{2}(?![0-9/\-])")
_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
# Names that mark a security code or a postal code, in text and in an object's fields.
_CVC_CUE = re.compile(r"\b(cvc2?|cvv2?|csc|cvn|cid|security\s*code|card\s*code|verification\s*(code|number|value))\b", re.I)
_ZIP_CUE = re.compile(r"\b(zip|postal|post\s*code|postcode)\b", re.I)
# Field names (lower-cased, letters and digits only).
_CVC_KEY = re.compile(r"(cvc|cvv|csc|cvn|securitycode|cardcode|verificationcode|verificationvalue)")
_ZIP_KEY = re.compile(r"(zip|postal|postcode)")
_EXP_KEY = re.compile(r"(expiry|expiration|^ccexp|^exp(month|year|mm|yy|yyyy|date)?$|expmonth|expyear)")
# The fields of an object that hold its value (an accessibility node's, a form field's).
_VALUE_KEYS = {"value", "text", "content", "current_value", "valuetext", "value_text"}
_CUE_KEYS = {"name", "label", "title", "description", "placeholder", "aria_label", "arialabel", "role_description", "help", "id", "autocomplete"}


def _zip_regex(shape, loose=False):
    """A postal code of that shape standing on its own; `loose` (after a cue word): ":" or "=" right before it too."""
    parts = []
    for ch in shape[:12]:
        parts.append({"9": "[0-9]", "A": "[A-Za-z]", " ": r"\s?", "-": "-?"}.get(ch, re.escape(ch)))
    before = r"(?<![A-Za-z0-9\[/#.,?&%])" if loose else r"(?<![A-Za-z0-9\[:/#.,=?&%])"
    return re.compile(before + "".join(parts) + r"(?![A-Za-z0-9\]%])")


def _cue_spans(text, rx, cue):
    """Matches of rx with a cue word (cue) before them on the same line, at most 40 characters back."""
    spans = []
    for m in rx.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if cue.search(text[max(line_start, m.start() - 40):m.start()]):
            spans.append(m.span())
    return spans


def _name_spans(text, fp):
    try:
        salt, want, words = fp["salt"], fp["sha256"], int(fp["words"])
    except (KeyError, TypeError, ValueError):
        return []
    if not (1 <= words <= 8):
        return []
    toks = list(_WORD.finditer(text))
    spans = []
    for i in range(len(toks) - words + 1):
        win = toks[i:i + words]
        # Between the words: spaces and punctuation ("J. Example"), never letters or digits.
        if any(not _joinable(text[win[k].end():win[k + 1].start()]) for k in range(words - 1)):
            continue
        norm = " ".join(t.group().lower() for t in win)
        if hashlib.sha256((salt + norm).encode()).hexdigest() == want:
            spans.append((win[0].start(), win[-1].end()))
    return spans


def _cvc_rx(n):
    return re.compile(r"(?<![0-9A-Za-z\[/#.,?&%\-])[0-9]{" + str(n) + r"}(?![0-9A-Za-z\]%]|[.,][0-9])")


def mask(text, active=()):
    """(text with card details masked, how many)."""
    if not isinstance(text, str) or not text:
        return text, 0
    total = 0
    groups = _groups(text)
    text, n = _replace(text, _card_spans(text, groups), CARD_MASK)
    total += n
    if active:
        groups = _groups(text)
        text, n = _replace(text, _active_card_spans(text, groups), CARD_MASK)
        total += n
    for hint in active:
        groups = _groups(text)
        text, n = _replace(text, _split_card_spans(text, groups), CARD_MASK)
        total += n
        if hint.get("exp"):
            text, n = _replace(text, [m.span() for m in _EXP.finditer(text)], EXP_MASK)
            total += n
        if isinstance(hint.get("name"), dict):
            text, n = _replace(text, _name_spans(text, hint["name"]), NAME_MASK)
            total += n
        shape = hint.get("zip_shape")
        if isinstance(shape, str) and shape and re.fullmatch(r"[9A -]{2,12}", shape):
            zrx = _zip_regex(shape)
            spans = [m.span() for m in zrx.finditer(text)] if "A" in shape else _cue_spans(text, _zip_regex(shape, loose=True), _ZIP_CUE)
            text, n = _replace(text, spans, ZIP_MASK)
            total += n
        cl = hint.get("cvc_len")
        if cl in (3, 4):
            text, n = _replace(text, _cue_spans(text, _cvc_rx(cl), _CVC_CUE), CVC_MASK)
            total += n
    return text, total


def _field_mask(kind, v, active):
    """The mask for a scalar value of a field (or an object) named like a card part, when its shape fits; else None."""
    if isinstance(v, bool) or v is None:
        return None
    s = str(int(v)) if isinstance(v, float) and v.is_integer() else (str(v) if isinstance(v, (int, str)) else None)
    if s is None:
        return None
    s = s.strip()
    for hint in active:
        if kind == "cvc" and hint.get("cvc_len") in (3, 4) and re.fullmatch(r"[0-9]{%d}" % hint["cvc_len"], s):
            return CVC_MASK
        if kind == "zip" and isinstance(hint.get("zip_shape"), str) and re.fullmatch(r"[9A -]{2,12}", hint["zip_shape"]):
            if _zip_regex(hint["zip_shape"]).fullmatch(s):
                return ZIP_MASK
        if kind == "exp" and hint.get("exp") and (re.fullmatch(r"[0-9]{1,4}", s) or _EXP.fullmatch(s)):
            return EXP_MASK
    return None


def _key_kind(key):
    k = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if _CVC_KEY.search(k):
        return "cvc"
    if _ZIP_KEY.search(k):
        return "zip"
    if _EXP_KEY.search(k):
        return "exp"
    return None


def _object_kinds(d):
    """Which card parts an object names in its descriptive fields ("Security code", "ZIP", "Expiry")."""
    words = " ".join(str(v) for k, v in d.items() if isinstance(v, str) and len(v) <= 200
                     and re.sub(r"[^a-z]", "", str(k).lower()) in _CUE_KEYS)
    kinds = set()
    if _CVC_CUE.search(words):
        kinds.add("cvc")
    if _ZIP_CUE.search(words):
        kinds.add("zip")
    if re.search(r"\bexp(iry|iration|ires)?\b|\bmm\s*/\s*yy\b", words, re.I):
        kinds.add("exp")
    return kinds


_MEDIA = ("image/", "audio/", "video/")


def _binary_fields(d):
    """The fields of an object that hold encoded binary data, never masked: an MCP image's or audio's `data`, a
    resource's `blob` (a text one is decoded, masked and encoded again instead), a field whose mime type is media."""
    skip = set()
    t = d.get("type")
    mime = d.get("mimeType") or d.get("mime_type") or d.get("mimetype")
    if t in ("image", "audio") and "data" in d:
        skip.add("data")
    if isinstance(mime, str) and mime.lower().startswith(_MEDIA):
        for k in ("data", "blob", "base64", "bytes"):
            if k in d:
                skip.add(k)
    if "blob" in d and not (isinstance(mime, str) and (mime.lower().startswith("text/") or "json" in mime.lower())):
        skip.add("blob")
    for k, v in d.items():
        kl = str(k).lower()
        if isinstance(v, str) and (kl in ("screenshot", "screenshot_base64", "image_base64", "png", "jpeg", "jpg", "webp", "thumbnail")
                                   or v[:11].lower() in ("data:image/", "data:audio/", "data:video/")):
            skip.add(k)
    return skip


def _json_text(s, active):
    """While a purchase is open: text that is a JSON object or list, masked as JSON (its fields' names count), then
    written again; (text, how many) or None when it isn't JSON."""
    t = s.lstrip()
    if not active or not t[:1] in ("{", "[") or len(s) > (4 << 20):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    if not isinstance(obj, (dict, list)):
        return None
    obj, n = mask_value(obj, active)
    return (json.dumps(obj), n) if n else (s, 0)


def _text_blob(d, active, count):
    """A resource's text `blob` (base64): decoded, masked (as JSON when it is JSON, so its fields' names count) and encoded
    again when something in it was masked."""
    b = d.get("blob")
    if not isinstance(b, str):
        return
    try:
        raw = base64.b64decode(b, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return
    js = _json_text(raw, active)
    out, n = js if js is not None else mask(raw, active)
    if n:
        d["blob"] = base64.b64encode(out.encode()).decode()
        count[0] += n


def mask_value(value, active=()):
    """A JSON value with card details masked in every string (keys too) and every number shaped like a card number, at
    any depth, without recursion. Returns (value, count); raises Unfinished past MAX_NODES."""
    count = [0]
    seen = [0]

    def scalar(v, kind=None):
        seen[0] += 1
        if seen[0] > MAX_NODES:
            raise Unfinished("too many values to check")
        if kind and active:
            fm = _field_mask(kind, v, active)
            if fm:
                count[0] += 1
                return fm
        if isinstance(v, str):
            js = _json_text(v, active)
            out, n = js if js is not None else mask(v, active)
            if js is not None and not n:
                out, n = mask(v, active)
            count[0] += n
            return out
        if isinstance(v, int) and not isinstance(v, bool) and is_card(str(abs(v))):
            count[0] += 1
            return CARD_MASK
        if isinstance(v, float) and v == v and abs(v) < 1e20 and v.is_integer() and is_card(str(int(abs(v)))):
            count[0] += 1
            return CARD_MASK
        return v

    def join_siblings(items):
        """While a purchase is open: neighbouring values of one object or list that are each 3-6 digits and together a
        card number; returns the indexes to mask."""
        idx = []
        digs = []
        for i, v in items:
            s = str(v) if isinstance(v, int) and not isinstance(v, bool) else (v.replace(" ", "") if isinstance(v, str) else None)
            digs.append((i, s if s is not None and s.isdigit() and 3 <= len(s) <= 6 else None))
        for a in range(len(digs)):
            joined = ""
            for b in range(a, min(a + 6, len(digs))):
                if digs[b][1] is None:
                    break
                joined += digs[b][1]
                if len(joined) > 19:
                    break
                if b > a and is_card(joined) and real_iin(joined):
                    idx += [digs[k][0] for k in range(a, b + 1)]
        return set(idx)

    root = [value]
    stack = [(root, 0, None)]
    while stack:
        holder, k, kind = stack.pop()
        v = holder[k]
        if isinstance(v, dict):
            skip = _binary_fields(v)
            if "blob" in v and "blob" not in skip:
                _text_blob(v, active, count)
                skip.add("blob")
            kinds = _object_kinds(v) if active else set()
            items = list(v.items())             # changed in place: whoever holds this object sees the masked keys
            v.clear()
            pairs = {}
            for key, x in items:
                nk = scalar(key) if isinstance(key, str) else key
                v[nk] = x
                pairs[nk] = key                 # two keys masked alike: the last one's value stays
            split = join_siblings([(nk, v[nk]) for nk in v.keys()]) if active else set()
            for nk, key in pairs.items():
                if key in skip:
                    continue
                if nk in split:
                    v[nk] = CARD_MASK
                    count[0] += 1
                    continue
                ck = _key_kind(key) if active else None
                if not ck and kinds and re.sub(r"[^a-z]", "", str(key).lower()) in _VALUE_KEYS and len(kinds) == 1:
                    ck = next(iter(kinds))
                stack.append((v, nk, ck))
        elif isinstance(v, list):
            split = join_siblings(list(enumerate(v))) if active else set()
            for i in range(len(v)):
                if i in split:
                    v[i] = CARD_MASK
                    count[0] += 1
                else:
                    stack.append((v, i, kind if not isinstance(v[i], (dict, list)) else None))
        else:
            holder[k] = scalar(v, kind)
    return root[0], count[0]


def mask_message(msg, active=()):
    """A JSON-RPC message from the driver (an answer, an error or a notification), masked in place: every key and value
    in it, the top level's keys too. Returns how many details were hidden; raises Unfinished (or anything else) when it
    couldn't finish, and the caller then doesn't pass it on."""
    if not isinstance(msg, dict):
        raise Unfinished("not a message")
    total = 0
    items = list(msg.items())
    msg.clear()
    for k, v in items:
        if k in ("jsonrpc", "id"):
            msg[k] = v
            continue
        nk, n = mask(k, active) if isinstance(k, str) else (k, 0)
        total += n
        msg[nk], n = mask_value(v, active)
        total += n
    return total


def mask_result(res, active=()):
    """A tool result (content items and structuredContent), masked in place. Returns how many were hidden."""
    if not isinstance(res, dict):
        return 0
    _, n = mask_value(res, active)
    return n


def mask_text(text, active=()):
    """Plain text (a log line, driver stderr, an error the hub writes itself), masked; never raises: text that can't be
    checked is withheld."""
    try:
        return mask(text, active)[0]
    except Exception:
        return "[text withheld: metacomputer couldn't check it for card details]"


def active_purchases(vault_dir, vm=None, now=None):
    """The open purchases' shapes from <vault_dir>/purchases-active.json, for this VM (all VMs when vm is None); [] when
    there are none. A file that can't be read while it exists counts as one purchase with every shape-based mask on."""
    path = os.path.join(vault_dir, "purchases-active.json")
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(1 << 20).decode("utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [{"exp": True, "cvc_len": 3, "unreadable": True}, {"cvc_len": 4, "unreadable": True}]
    now = time.time() if now is None else now
    out = []
    for p in data.get("purchases") or [] if isinstance(data, dict) else []:
        if isinstance(p, dict) and isinstance(p.get("until"), (int, float)) and p["until"] >= now and (vm is None or p.get("vm") in (None, vm)):
            out.append(p)
    return out


STICKY_MAX = 64     # masks of purchases no longer in the file a process keeps (StickyHints); beyond that the oldest go


def hint_key(h):
    """What names one purchase's masks (its shapes and fingerprint on its VM): not its time, ack id or parts filled."""
    return json.dumps({k: v for k, v in h.items() if k not in ("until", "ack", "filled")}, sort_keys=True, default=str)


class StickyHints:
    """The open purchases' masks as this process has read them: once read, a purchase's masks stay for the rest of the
    process's life, whatever the file's window or the clock says later. The file's window only adds; its expiry never
    removes what's loaded (what a fill made a page show may come back long after). Every purchase still in the file is
    kept, however many; only of those no longer in it are at most STICKY_MAX kept, the oldest dropped first, and none at
    all on a read that failed (it can't tell what's gone). A file that can't be read adds every shape-based mask for
    that read only."""

    def __init__(self, limit=STICKY_MAX):
        self.limit = limit
        self.lock = threading.Lock()
        self.seen = collections.OrderedDict()

    def update(self, fresh):
        """`fresh`: what active_purchases read now. Returns every mask this process holds, those included."""
        return self.update_kept(fresh)[0]

    def update_kept(self, fresh):
        """update(), and which of `fresh` it holds now (all of them: what's in the file is never let go), for an ack that
        names only what's kept."""
        once = [h for h in fresh if h.get("unreadable")]
        with self.lock:
            now = set()
            for h in fresh:
                if h.get("unreadable"):
                    continue
                k = hint_key(h)
                self.seen[k] = h
                self.seen.move_to_end(k)
                now.add(k)
            if not once:                # a read that failed says nothing about what's gone: nothing is let go then
                gone = [k for k in self.seen if k not in now]       # oldest first
                for k in gone[:max(0, len(gone) - self.limit)]:
                    del self.seen[k]
            kept = [h for h in fresh if not h.get("unreadable") and hint_key(h) in self.seen]
            return list(self.seen.values()) + once, kept


_STICKY = {}                # (vault folder, VM) -> StickyHints, for hints_for (the action-log relay)
_STICKY_LOCK = threading.Lock()


def process_start(pid):
    """When a process started, as [seconds, microseconds] (macOS proc_pidinfo), with its user id: ([s, us], uid), or None.
    With the pid, it names one process: a pid used again later has another start."""
    try:
        import ctypes
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        buf = ctypes.create_string_buffer(136)                  # struct proc_bsdinfo
        if libc.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3), ctypes.c_uint64(0), buf, ctypes.c_int(136)) != 136:
            return None                                         # (3: PROC_PIDTBSDINFO)
        raw = buf.raw
        uid = int.from_bytes(raw[20:24], "little")
        return [int.from_bytes(raw[120:128], "little"), int.from_bytes(raw[128:136], "little")], uid
    except (OSError, AttributeError, ValueError):
        return None


def ack_dir(vault_dir):
    """The folder (0700, this user's) where hubs say which open purchases they've seen."""
    return os.path.join(vault_dir, "hub-acks")


def ack_path(vault_dir, vm):
    """Where a hub says which open purchases on its VM it has seen (and so masks for): the vault waits for it before a
    card's first part leaves (vault/PaymentServer.swift, hubAckProblem)."""
    return os.path.join(ack_dir(vault_dir), re.sub(r"[^A-Za-z0-9._-]", "_", vm or "-") + ".json")


def _private_ack_dir(vault_dir):
    """The ack folder, made 0700 if it isn't there; None when the vault folder isn't (yet) or the ack folder isn't a
    folder of this user's (a link, someone else's)."""
    if not os.path.isdir(vault_dir):
        return None
    d = ack_dir(vault_dir)
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return None
    try:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return None
        if st.st_mode & 0o077:
            os.chmod(d, 0o700)
    except OSError:
        return None
    return d


def ack_purchases(vault_dir, vm, last=None, lock=None, start=None, hints=None):
    """What a hub does for its VM, many times a second: read the open purchases fresh and, when the set changes, write
    which it has seen (0600, replaced whole, in the 0700 ack folder), with its pid, that process's start time and its
    lock (RUN_DIR/hub/hub.pid, which it holds): the vault checks all three. No lock, no ack (a client without the hub
    doesn't say it masks). `hints`: what the hub has just loaded (StickyHints), so it never acknowledges a purchase it
    hasn't loaded; else read now. Returns the set written (or `last`, unchanged)."""
    if not lock:
        return last
    d = _private_ack_dir(vault_dir)
    if d is None:
        return last
    fresh = active_purchases(vault_dir, vm) if hints is None else hints
    seen = sorted(str(h["ack"]) for h in fresh if isinstance(h.get("ack"), str))
    path = ack_path(vault_dir, vm)
    if last is not None and seen == last and os.path.exists(path):
        return last
    if start is None:
        ps = process_start(os.getpid())
        start = ps[0] if ps else None
    if start is None:
        return last
    tmp = path + f".tmp-{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"v": 2, "vm": vm, "pid": os.getpid(), "start": list(start), "lock": os.path.realpath(lock),
                       "seen": seen, "t": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        return last
    return seen


def vault_dir_for(env):
    """The vault folder whose open purchases apply here: MCOMP_VAULT_DIR, the VM's own (MCOMP_VM_VAULT_DIR), or the
    default one."""
    return (env.get("MCOMP_VAULT_DIR") or env.get("MCOMP_VM_VAULT_DIR")
            or os.path.join(os.path.expanduser("~"), "Library", "Application Support", "metacomputer"))


def hints_for(env):
    """The masks for this process's VM (MCOMP_VM_NAME or VM_NAME; every VM's when it isn't known): the open purchases read
    now, and every one this process has read before (StickyHints: they stay for its life)."""
    vm = env.get("MCOMP_VM_NAME") or env.get("VM_NAME") or None
    vd = vault_dir_for(env)
    with _STICKY_LOCK:
        st = _STICKY.setdefault((vd, vm), StickyHints())
    return st.update(active_purchases(vd, vm))
