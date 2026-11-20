#!/usr/bin/env python3
"""lib/botcheck.py, no VM: each common human check is recognised from the marks it leaves in a window read (tree rows
and the title), and pages that only talk about CAPTCHAs aren't. The hub's use of it is in tests/test_hub.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import botcheck  # noqa: E402

results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"   [{detail}]"))


def read(tree="", title=None, err=False):
    sc = {"tree_markdown": tree}
    if title is not None:
        sc["window_title"] = title
    return {"content": [{"type": "text", "text": tree}], "structuredContent": sc, "isError": err}


SEEN = [
    ("recaptcha", read('- [3] check box "I\'m not a robot" [actions=[press]]')),
    ("recaptcha", read('- [1] frame "reCAPTCHA" [actions=[]]')),
    ("hcaptcha", read('- [2] frame "Widget containing checkbox for hCaptcha security challenge" [actions=[]]')),
    ("cloudflare", read('- [4] frame "Widget containing a Cloudflare security challenge" [actions=[]]')),
    ("cloudflare", read('- [5] text "Verify you are human by completing the action below." [actions=[]]')),
    ("cloudflare", read('- [6] text "www.example.com needs to review the security of your connection before proceeding."')),
    ("cloudflare_title", read('- [0] document "Just a moment..."', title="Just a moment...")),
    ("press_and_hold", read('- [7] text "Press & Hold to confirm you are a human (and not a bot)."')),
    ("datadome", read('- [8] frame "https://geo.captcha-delivery.com/captcha/?initialCid=x"')),
    ("arkose", read('- [9] internal frame "https://client-api.arkoselabs.com/fc/gc/?token=x" [actions=[]]')),
    ("cloudflare", read('- [4] internal frame "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x" [actions=[]]')),
    ("google_sorry", read('- [1] text "Our systems have detected unusual traffic from your computer network."')),
    ("generic", read('- [2] heading "Verify you are human" [actions=[]]')),
]
for kind, res in SEEN:
    check(f"recognised: {kind}", botcheck.detect(res) == kind, botcheck.detect(res))

NOT = [
    ("an article about CAPTCHAs", read('- [0] heading "How reCAPTCHA and hCaptcha work"\n- [1] text "A CAPTCHA asks '
                                        'whether you are a robot. Cloudflare Turnstile is another."')),
    ("docs that name FunCaptcha", read('- [1] text "Documentation for FunCaptcha integration" [actions=[]]')),
    ("a link to Cloudflare's challenge domain", read('- [2] link "https://challenges.cloudflare.com" [actions=[jump]]')),
    ("an article mentioning hcaptcha.com", read('- [3] paragraph "See hcaptcha.com/ for pricing" [actions=[]]')),
    ("a robots.txt guide", read('- [0] heading "Are robots allowed?" [actions=[]]\n- [1] text "robots.txt tells robots"')),
    ("a title that only starts like the interstitial", read("", title="Just a moment of your time - Blog")),
    ("an error result", read('- [3] check box "I\'m not a robot"', err=True)),
    ("no result at all", None),
]
for label, res in NOT:
    check(f"not marked: {label}", botcheck.detect(res) is None, botcheck.detect(res))

check("a huge tree is read only so far (MAX_SCAN), without failing",
      botcheck.detect(read("x" * (botcheck.MAX_SCAN + 10) + '\n- [3] check box "I\'m not a robot"')) is None)
msg, data = botcheck.event_of("recaptcha")
check("the event carries the kind and fixed words only", data == {"kind": "recaptcha"} and "reCAPTCHA" in msg)
check("…and an unknown kind is reported as generic", botcheck.event_of("<script>")[1] == {"kind": "generic"})
n = botcheck.note("cloudflare", 7, True)
check("the note names the handoff and the refusal", "metacomputer handoff --why captcha" in n and "refuses acting calls" in n)
check("…and with BOT_CHECK=note it doesn't mention a refusal", "refuses" not in botcheck.note("cloudflare", 7, False))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
