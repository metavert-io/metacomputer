#!/usr/bin/env python3
"""Human checks on screen (CAPTCHAs, Cloudflare's interstitial, press-and-hold), for the hub (BOT_CHECK).

The hub looks at what a window read returns (the accessibility tree's rows, the window's title) for the marks the
common human checks leave: their widgets' accessible names and their pages' fixed wording. When one is there, it
tells Claude to hand the computer to the user (`metacomputer handoff --why captcha`), sends `needs_you.captcha`,
and (BOT_CHECK=on) refuses acting calls aimed at that window until a fresh read of it no longer shows the check.
A page that only talks about CAPTCHAs (an article, docs) carries none of these marks.

Only fixed wording is matched, and only the kind found leaves this module: never page text. The check is for the
agent's sake (not flailing at a check it mustn't pass), not a security boundary: a page can hide or fake the marks.
"""
import json
import re

MAX_SCAN = 400_000                      # characters of a result looked at (a big tree is cut there)

# (kind, what it is for a person, pattern). Matched case-insensitively against each tree row and the title.
# The rows look like `- [12] check box "I'm not a robot" [actions=[...]]`; names are in double quotes. A challenge's
# domain or product name counts only as the name of a frame or document (where its widget sits), never in a page's
# ordinary text, so a page that links to or writes about one isn't marked.
MARKS = [
    ("cloudflare", "Cloudflare's \"Verify you are human\" check",
     r"verify you are human by completing the action below|checking if the site connection is secure|"
     r"needs to review the security of your connection before proceeding|"
     r"widget containing a cloudflare security challenge|" + r'(?:frame|document web) "[^"\n]{0,200}challenges\.cloudflare\.com'),
    ("turnstile", "a Cloudflare Turnstile check", r'(?:frame|document web) "[^"\n]{0,200}turnstile[^"\n]{0,40}security challenge'),
    ("hcaptcha", "an hCaptcha check",
     r'widget containing checkbox for hcaptcha security challenge|(?:frame|document web) "[^"\n]{0,200}hcaptcha'),
    ("recaptcha", "a reCAPTCHA check",
     r"\"i'm not a robot\"|\"i am not a robot\"|title=\"recaptcha\"|\brecaptcha challenge expires\b|"
     r"(?:frame|iframe|document|section|panel)[^\n]{0,20}\"recaptcha\""),
    ("press_and_hold", "a press-and-hold check", r"press (?:&|and) hold(?: the button)? to confirm you are a human"),
    ("datadome", "a DataDome check", r'(?:frame|document web) "[^"\n]{0,200}captcha-delivery\.com'),
    ("arkose", "an Arkose (FunCaptcha) check", r'(?:frame|document web) "[^"\n]{0,200}(?:funcaptcha|arkoselabs\.com)'),
    ("google_sorry", "Google's \"unusual traffic\" check",
     r"our systems have detected unusual traffic from your computer network"),
    ("generic", "a human check",
     r"\"(?:please )?verify (?:that )?you(?:'re| are) (?:a )?human\"|\"are you a robot\??\"|"
     r"\"confirm you are not a robot\""),
]
_RX = [(k, label, re.compile(p, re.I)) for k, label, p in MARKS]
TITLE_RX = re.compile(r"^\s*just a moment\.{0,3}\s*$|^\s*attention required!? \| cloudflare\s*$", re.I)
KINDS = {k for k, _l, _p in MARKS} | {"cloudflare_title"}
LABEL = {k: label for k, label, _p in MARKS}
LABEL["cloudflare_title"] = "Cloudflare's \"Just a moment\" page"


def _texts(res):
    """The text a window read hands back: the text parts, and the structured tree (markdown or a diff)."""
    out = []
    if not isinstance(res, dict):
        return out
    for c in (res.get("content") or [])[:256]:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            out.append(c["text"])
    sc = res.get("structuredContent") if isinstance(res.get("structuredContent"), dict) else {}
    for k in ("tree_markdown", "tree_diff"):
        if isinstance(sc.get(k), str):
            out.append(sc[k])
    return out


def title_of(res):
    sc = res.get("structuredContent") if isinstance(res, dict) and isinstance(res.get("structuredContent"), dict) else {}
    t = sc.get("window_title")
    return t[:500] if isinstance(t, str) else ""


def detect(res):
    """The kind of human check a window read shows, or None. Never raises."""
    try:
        if not isinstance(res, dict) or res.get("isError"):
            return None
        if TITLE_RX.search(title_of(res)):
            return "cloudflare_title"
        budget = MAX_SCAN
        for t in _texts(res):
            chunk = t[:budget]
            budget -= len(chunk)
            for kind, _label, rx in _RX:
                if rx.search(chunk):
                    return kind
            if budget <= 0:
                break
    except Exception:
        return None
    return None


def note(kind, window_id, refusing):
    what = LABEL.get(kind, "a human check")
    where = f"window {window_id}" if isinstance(window_id, int) else "this window"
    tail = (f" Until a fresh read of {where} no longer shows it, the hub refuses acting calls aimed there"
            " (BOT_CHECK=on); reads still work." if refusing else "")
    return (f"metacomputer hub: bot check: {where} shows {what}. Don't try to pass it, retry it or switch browsers, "
            "modes or profiles to get around it: hand it to the user now with `metacomputer handoff --why captcha "
            "--say \"<what to do, and where>\"` (SKILL.md, \"When a step is the user's\")." + tail)


REFUSED = ("Refused by the metacomputer hub (bot check): {where} showed {what} at its last read. A person has to do "
           "that: run `metacomputer handoff --why captcha --say \"<what to do>\"` and wait for it; when it returns, "
           "read the window again (a read without the check lifts this). Nothing was sent.")


def refusal(kind, window_id):
    return REFUSED.format(where=f"window {window_id}" if isinstance(window_id, int) else "the window",
                          what=LABEL.get(kind, "a human check"))


def event_of(kind, host=None):
    """needs_you.captcha: fixed words and the kind only."""
    return (f"Claude is stopped at {LABEL.get(kind, 'a human check')}: it needs you to complete it",
            {"kind": kind if kind in KINDS else "generic"})


if __name__ == "__main__":
    import sys
    if sys.argv[1:]:             # no arguments (it reads a page's JSON on stdin): -h says so; anything else is refused
        print("usage: botcheck.py < PAGE.json", file=sys.stdout if sys.argv[1:] in (["-h"], ["--help"]) else sys.stderr)
        sys.exit(0 if sys.argv[1:] in (["-h"], ["--help"]) else 2)
    import os
    if os.environ.get("MCOMP_PARSE_ONLY") == "1":   # tests/test_help.py's valid forms: taken, nothing done
        print("parse-only: botcheck.py: taken " + json.dumps({"flags": [], "values": [], "words": 0, "max_words": 0}))
        sys.exit(99)        # parse only: nothing was done (never 0: a check run this way refuses)
    print(json.dumps({"kind": detect(json.load(sys.stdin))}))
