"""Test E: multi-application workflow with the clipboard.

Firefox: open a Wikipedia article, select its lead paragraph with a real
triple-click and copy it (Ctrl+C). The agent reads the copied passage
(clipboard_read) and extracts its first sentence as the fact. Writer: type a
source line through AT-SPI, paste the passage as unformatted text, save as
fact.odt, quit. Thunar: open the folder, reopen the file. Verification: the
reopened document (accessibility tree) and the file on disk both contain the
source line and the passage, and the extracted fact appears in it.
"""
import os
import re
import time

from common import (dismiss_page_overlays, find_elements, in_view, maximize, odt_paragraphs, prep_close, quit_firefox,
                    retry, state, token, wait_element, wait_title, wait_window)
from harness import Step, guest
from labconfig import GUEST_HOME
from test_d_libreoffice import exit_libreoffice, open_in_thunar, open_writer, save_as

TITLE = "Multi-application workflow"
TASK = ("Browser: read a passage from Wikipedia's Ada Lovelace article and copy it; Writer: create a document "
        "with the source and the passage, save it; file manager: locate and reopen it; verify")
URL = "https://en.wikipedia.org/wiki/Ada_Lovelace"
DOCS = os.path.join(GUEST_HOME, "Documents")
FILE = "fact.odt"
FG = {"delivery_mode": "foreground"}


def norm(s):
    s = re.sub(r"\[\s*\d+\s*\]", "", s or "")          # citation markers
    return re.sub(r"\s+", " ", s.replace(" ", " ").replace("￼", "")).strip()


def run(t):
    prep_close("firefox", "soffice", "oosplash")
    guest(f"mkdir -p {DOCS}; rm -f {DOCS}/{FILE} {DOCS}/.~lock.{FILE}#")

    # 1. Browser: the article
    t.call("launch_app", {"name": "firefox"})
    w = wait_window(t, "firefox", title_part="Mozilla Firefox", min_width=600)
    fpid, fwid = w["pid"], w["window_id"]
    maximize(t, fpid, fwid)
    time.sleep(2)
    t.call("hotkey", {"pid": fpid, "window_id": fwid, "keys": ["ctrl", "l"], **FG})
    t.call("type_text", {"pid": fpid, "window_id": fwid, "text": URL, **FG})
    t.call("press_key", {"pid": fpid, "window_id": fwid, "key": "Return", **FG})
    wait_title(t, fpid, "Ada Lovelace - Wikipedia", timeout=30)
    time.sleep(2)

    # 2. Select the lead paragraph with a real triple-click and copy it
    def copy_lead():
        st = state(t, fpid, fwid, query="Augusta Ada King", fmt="elements", timeout_ms=8000)
        paras = [e for e in find_elements(st, role="paragraph") if "Augusta Ada King" in (e.get("label") or "")]
        if not paras:
            raise Step("lead paragraph not found in the accessibility tree")
        t.evidence["lead_paragraph_a11y"] = norm(paras[0].get("label"))[:400]
        if not in_view(st, paras[0]):
            raise Step("lead paragraph is outside the visible part of the page")
        t.call("click", {"pid": fpid, "window_id": fwid, "element_token": paras[0]["element_token"], "count": 3, **FG})
        t.call("hotkey", {"pid": fpid, "window_id": fwid, "keys": ["ctrl", "c"], **FG})
        time.sleep(0.5)
        cb = t.call("clipboard_read", {"include_text": True})
        text = (cb.structured or {}).get("text") or ""
        if "Ada" not in text or len(text) < 80:
            raise Step(f"clipboard holds {text[:80]!r}")
        return norm(text)
    def make_room():
        # live page content (e.g. a fundraising banner) can push the lead paragraph
        # out of view: dismiss overlays first, then scroll the page a little
        if not dismiss_page_overlays(t, fpid, fwid):
            st = state(t, fpid, fwid, query="Ada Lovelace", fmt="elements", timeout_ms=5000)
            doc = find_elements(st, role="document web")
            t.call("scroll", {"pid": fpid, "window_id": fwid, "direction": "down", "amount": 6, "by": "line",
                              **({"element_token": doc[0]["element_token"]} if doc else {}), **FG}, ok_required=False)
            t.recoveries.append("scrolled the article down to bring the lead paragraph into view")

    passage = retry(t, "copy the lead paragraph", copy_lead, attempts=4, recover=make_room)
    m = re.match(r"(.{40,}?[a-z)\]]\.)\s", passage + " ")
    fact = m.group(1) if m else passage[:200]
    t.evidence["passage"] = passage[:600]
    t.evidence["fact"] = fact
    t.check("fact read from the page (copied passage, first sentence)",
            fact.startswith("Augusta Ada King") and "mathematician" in passage, fact[:200])
    t.shot("article with lead selected", fpid, fwid)

    # 3. Writer: source line via AT-SPI, then the passage pasted as plain text
    w = open_writer(t)
    pid, wid = w["pid"], w["window_id"]
    t.shot("writer opened", pid, wid)
    st = wait_element(t, pid, wid, r'paragraph ""')  # document published to AT-SPI
    source = f"Source: {URL}"
    r = t.call("type_text", {"pid": pid, "window_id": wid, "element_token": token(st, r'paragraph ""'), "text": source})
    t.check("source line typed (AT-SPI read-back)", r.structured.get("effect") == "confirmed", r.text[:140])
    t.call("press_key", {"pid": pid, "window_id": wid, "key": "End", **FG})
    t.call("press_key", {"pid": pid, "window_id": wid, "key": "Return", **FG})
    t.call("hotkey", {"pid": pid, "window_id": wid, "keys": ["ctrl", "alt", "shift", "v"], **FG})  # paste unformatted
    time.sleep(1.5)
    st = state(t, pid, wid, query="Augusta", fmt="elements", timeout_ms=5000)
    pasted = [norm(e.get("label")) for e in find_elements(st, role="paragraph") if "Augusta Ada King" in (e.get("label") or "")]
    t.check("passage pasted into the document (AT-SPI)", pasted and fact in pasted[0], pasted[:1])
    t.shot("writer with fact", pid, wid)

    # 4. save, quit
    save_as(t, pid, wid, f"{DOCS}/{FILE}")
    wait_title(t, pid, FILE, timeout=20)
    t.check("LibreOffice closed", exit_libreoffice(t, pid, wid), "File > Exit LibreOffice")

    # 5. file manager: locate and reopen
    tpid, twid = open_in_thunar(t, DOCS, FILE)
    w = wait_window(t, "libreoffice", title_part=FILE, timeout=60, min_width=600)
    pid, wid = w["pid"], w["window_id"]
    time.sleep(1.5)
    st = state(t, pid, wid, fmt="elements", timeout_ms=5000, query="Source")
    has_source = any(norm(e.get("label")) == source for e in find_elements(st, role="paragraph"))
    st = state(t, pid, wid, fmt="elements", timeout_ms=5000, query="Augusta")
    has_fact = any(fact in norm(e.get("label")) for e in find_elements(st, role="paragraph"))
    t.check("reopened document shows the source line and the fact (AT-SPI)", has_source and has_fact,
            f"source={has_source} fact={has_fact}")
    t.shot("reopened fact.odt", pid, wid)

    # 6. independent check of the saved file
    odt = odt_paragraphs(f"{DOCS}/{FILE}")
    texts = [norm(p[2]) for p in odt["paras"]]
    t.evidence["odt_paragraphs"] = [x[:200] for x in texts]
    t.check("file on disk: source line + passage containing the fact",
            source in texts and any(fact in x for x in texts), t.evidence["odt_paragraphs"][:2])

    # 7. tidy up
    exit_libreoffice(t, pid, wid)
    t.call("hotkey", {"pid": tpid, "window_id": twid, "keys": ["ctrl", "w"], **FG}, ok_required=False)
    t.check("Firefox quit", quit_firefox(t, fpid, fwid), "no Firefox window remains")
    t.shot("final desktop")
    return True
