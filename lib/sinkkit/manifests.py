"""What shared code needs from each kind of sink, read from its manifest (sinks/<kind>/manifest.json), so no shared
program names one: its words, its settings, what it carries.

A manifest is a JSON object, checked by check():
    label      its name in docs and messages ("OTLP")
    help       how `metacomputer help` lists it (default: label)
    about      what it sends, in a line (docs/sinks.md)
    docs       its section for people (docs/<page>.md#<section>, or its folder's README.md for a kind whose people's
               docs live there)
    owners     who reviews its folder (.github/CODEOWNERS)
    batch      at most this many events in one request (default 1)
    headers    whether it takes --header and --header-env (default true)
    signed     whether each sink of the kind gets a signing key of its own (its settings' hmac_key): shown when it's
               added and by `sink show --key` (default false)
    phone      whether it reaches a phone: then each sink of the kind has its own cap (lib/events.py PHONE_LIMITS), and
               sink-change announcements waiting for it together go as one message (default false)
    url_words  path segments its URLs hold that are never secret: `events sink list` shows them, and they're never
               taken for a secret to redact (lib/events.py mask_url, url_secret_parts)
    partial    its words for a receiver that accepts a request but refuses some of its events, which are then dropped,
               not retried (`events sink list` says them)
    format     what the event stream and the vault's audit sinks both send alike: sink.py reads it, and the vault's
               build compiles it in (lib/sinkkit/vault.py), so it's written once
    vault      present when the vault's audit sinks have this kind too: {"acknowledges": whether its receiver
               acknowledges each record (only such a sink can be --required), "headers": whether it takes --header}
Only lib/sinkkit, lib/events.py and scripts/gen-sinks.py read it. A manifest is a contributor's input (a pull request
may touch its folder only), and its values reach generated shell, Swift, CODEOWNERS and docs: check() holds each to a
whitelist of its own (lib/gensafe.py), and vault.py check holds the format to the vault's. Works under any python3
(macOS's 3.9 included)."""
import os
import re

from . import registry

KEYS = {"label", "help", "about", "docs", "owners", "batch", "headers", "signed", "phone", "url_words", "partial",
        "format", "vault"}
REQUIRED = ("label", "about", "docs", "owners")
VAULT_KEYS = {"acknowledges", "headers"}
WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")     # a kind's name (its folder, its registry line)
# Each text field's longest: one line of printable characters (lib/gensafe.py text).
TEXT = {"label": 64, "help": 64, "about": 200, "partial": 120}


def manifest(kind):
    """The kind's manifest with its defaults filled in."""
    raw = registry.data(kind)
    out = dict(raw)
    out["help"] = raw.get("help") or raw.get("label")
    out["owners"] = tuple(raw.get("owners") or ())
    out["batch"] = raw.get("batch", 1)
    out["headers"] = raw.get("headers", True)
    out["signed"] = raw.get("signed", False)
    out["phone"] = raw.get("phone", False)
    out["url_words"] = tuple(raw.get("url_words") or ())
    out["format"] = dict(raw.get("format") or {})
    return out


def batches():
    """{kind: how many events one request may carry}."""
    return {k: manifest(k)["batch"] for k in registry.SINKS}


def phones():
    """The kinds that reach a phone."""
    return tuple(k for k in registry.SINKS if manifest(k)["phone"])


def signed():
    """The kinds whose sinks have a signing key."""
    return tuple(k for k in registry.SINKS if manifest(k)["signed"])


def without_headers():
    """The kinds that take no headers."""
    return tuple(k for k in registry.SINKS if not manifest(k)["headers"])


def url_words():
    """Every kind's url_words."""
    return {w for k in registry.SINKS for w in manifest(k)["url_words"]}


def partial(kind):
    """The kind's words for a request accepted with some of its events refused, or None."""
    return manifest(kind).get("partial") if kind in registry.SINKS else None


def in_words(words, last="and"):
    """"A, B, C and D"."""
    words = list(words)
    return ", ".join(words) if len(words) < 2 else ", ".join(words[:-1]) + f" {last} " + words[-1]


def check(kind):
    """What's wrong with a kind's manifest and folder: [str] (empty: nothing). Each problem names its field."""
    import gensafe              # lib/gensafe.py: only checks need it, never the event stream itself
    if not (isinstance(kind, str) and KIND.fullmatch(kind)):
        return [f"the kind's name {kind!r} isn't a lowercase word (a letter, then letters, digits and _)"]
    try:
        raw = registry.data(kind)
    except (OSError, ValueError) as e:
        return [f"sinks/{kind}/manifest.json can't be read ({type(e).__name__})"]
    if not isinstance(raw, dict):
        return [f"sinks/{kind}/manifest.json isn't an object"]
    bad = [f"unknown key {k!r}" for k in sorted(set(raw) - KEYS)]
    bad += [f"{k!r} is missing" for k in REQUIRED if not raw.get(k)]
    for k in ("label", "help", "about", "docs", "partial"):
        if k in raw and not (isinstance(raw[k], str) and raw[k].strip()):
            bad.append(f"{k!r} isn't text")
        elif k in raw and k in TEXT and gensafe.text(raw[k], TEXT[k]):
            bad.append(f"{k!r} isn't {gensafe.text(raw[k], TEXT[k])}")
    for k in ("headers", "signed", "phone"):
        if k in raw and not isinstance(raw[k], bool):
            bad.append(f"{k!r} isn't true or false")
    if not isinstance(raw.get("owners"), list) or gensafe.owners(raw["owners"]):
        bad.append(f"'owners' isn't {gensafe.owners(None)}")
    b = raw.get("batch", 1)
    if not isinstance(b, int) or isinstance(b, bool) or not 1 <= b <= 1000:
        bad.append("'batch' isn't a number of events from 1 to 1000")
    words = raw.get("url_words", [])
    if not isinstance(words, list) or not all(isinstance(w, str) and WORD.fullmatch(w) for w in words):
        bad.append("'url_words' isn't a list of lowercase path segments")
    if isinstance(raw.get("docs"), str) and not re.fullmatch(rf"(docs/[a-z0-9_-]+|sinks/{re.escape(kind)}/README)\.md"
                                                             r"(#[a-z0-9-]+)?", raw["docs"]):
        bad.append(f"its docs aren't a page under docs/ or sinks/{kind}/README.md (and a section of it)")
    if not isinstance(raw.get("format", {}), dict):
        bad.append("'format' isn't an object")
    elif "content_type" in raw.get("format", {}) and gensafe.content_type(raw["format"]["content_type"]):
        bad.append(f"format.content_type isn't {gensafe.content_type(None)}")     # a header's value
    v = raw.get("vault")
    if v is not None and (not isinstance(v, dict) or set(v) != VAULT_KEYS
                          or not all(isinstance(x, bool) for x in v.values())):
        bad.append("'vault' isn't {\"acknowledges\": true|false, \"headers\": true|false}")
    if not os.path.isfile(os.path.join(registry.folder(kind), "sink.py")):
        bad.append(f"sinks/{kind}/sink.py is missing")
    return bad


def conflicts():
    """Words two kinds both claim (a label, a help word): [str]."""
    out, seen = [], {}
    for k in registry.SINKS:
        m = manifest(k)
        for key in ("label", "help"):
            v = (key, str(m.get(key)).lower())
            if v in seen and seen[v] != k:
                out.append(f"{k} and {seen[v]} both have {key} {m.get(key)!r}")
            seen.setdefault(v, k)
    return out
