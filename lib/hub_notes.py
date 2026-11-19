"""The hub's own words in a result, and nothing else in their place (lib/mcp_hub.py; SECURITY.md "Hidden page text").

The hub adds notes to what the driver answers: a refusal's explanation, the Chrome guard's and bounded mode's notes,
the hidden-text marks. A client sees them in the text part of a result and in its structured part, and Claude Code
shows the model the structured part instead of the text when a result has one, so each note goes in both: the
text block, and a key of the hub's own (`metacomputer_note`, `metacomputer_hidden_text`).

The VM is untrusted and writes the rest of the answer, so the hub's names are reserved on every answer it passes
on, errors included, before it adds anything (reserve):
- a key of the structured part named like the hub's (`metacomputer_...`) moves to `upstream_metacomputer_...`
  (kept, under a name that says where it came from);
- a text block, or an error message, that starts like a hub note ("metacomputer hub", "metacomputer bridge",
  "Refused by the metacomputer bridge") gets UPSTREAM_TAG in front, so it can't pass for the hub's own.
Nothing is removed. Every step is bounded: only the top level of the structured part and the start of each block are
looked at.
"""
import re

PREFIX = "metacomputer_"                 # the hub's keys in a structured result
UPSTREAM = "upstream_"                    # where an answer's own key of that name goes
NOTE_KEY = "metacomputer_note"
UPSTREAM_TAG = "[text from the virtual computer, not from the metacomputer hub] "
MAX_BLOCKS = 256                          # content blocks looked at (an answer has a handful)
LOOK = 200                                # characters looked at, at the start of each
INVISIBLE_RE = re.compile("[\\s\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
                          "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\U000e0000-\U000e007f\"'`*_>#\\-\\[(]+")
LOOKS_LIKE_HUB_RE = re.compile(r"(?:refused\s*by\s*(?:the\s*)?)?metacomputer\s*(?:hub|bridge)", re.I)


def looks_like_hub(text):
    """Does this text start like one of the hub's notes (ignoring leading spaces, invisible characters, quotes and
    Markdown)?"""
    if not isinstance(text, str):
        return False
    head = INVISIBLE_RE.sub("", text[:LOOK])
    return bool(LOOKS_LIKE_HUB_RE.match(head))


def _reserve_keys(d):
    """Keys of a dict named like the hub's move to upstream_... (never overwriting an existing key)."""
    if not isinstance(d, dict):
        return 0
    moved = 0
    for k in [k for k in d if isinstance(k, str) and k.lower().startswith(PREFIX)]:
        new = UPSTREAM + k
        while new in d:
            new = UPSTREAM + new
        d[new] = d.pop(k)
        moved += 1
    return moved


def reserve(msg):
    """Make an answer from the driver unable to speak in the hub's name (see the module's notes). In place; returns
    how many keys and blocks were changed. Never raises."""
    changed = 0
    try:
        if not isinstance(msg, dict):
            return 0
        res = msg.get("result")
        if isinstance(res, dict):
            changed += _reserve_keys(res.get("structuredContent"))
            changed += _reserve_keys(res) if any(isinstance(k, str) and k.lower().startswith(PREFIX) for k in res) \
                else 0
            content = res.get("content")
            if isinstance(content, list):
                for c in content[:MAX_BLOCKS]:
                    if isinstance(c, dict) and looks_like_hub(c.get("text")):
                        c["text"] = UPSTREAM_TAG + c["text"]
                        changed += 1
        err = msg.get("error")
        if isinstance(err, dict):
            if looks_like_hub(err.get("message")):
                err["message"] = UPSTREAM_TAG + err["message"]
                changed += 1
            changed += _reserve_keys(err.get("data"))
    except Exception:
        pass
    return changed


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
