"""Test C: Firefox, example.com, a second tab, Wikipedia's own search for
"Alan Turing".

Firefox ignores synthetic background key events and AT-SPI text insertion
into its address bar and web form fields (both measured: the driver's
read-back stays empty). So typing uses the driver's explicitly authorized
foreground route (real key events to the focused window), as Cua's Linux guide
prescribes; buttons (new tab, the site's Search button) are pressed through
accessibility actions. No CDP/Playwright/HTTP shortcuts: the driver's browser_*
CDP tools are deliberately not used.
"""
import time

from common import (active_window_xprop, dismiss_page_overlays, find_elements, maximize, prep_close, quit_firefox,
                    retry, state, token, wait_gone, wait_title, wait_window)
from harness import Step

TITLE = "Browser navigation"
TASK = ("Launch Firefox, open https://example.com and verify it, open a second tab, go to "
        "https://www.wikipedia.org, search for 'Alan Turing' with the site's search box, confirm the article")
FG = {"delivery_mode": "foreground"}


def navigate(t, pid, wid, url):
    t.call("hotkey", {"pid": pid, "window_id": wid, "keys": ["ctrl", "l"], **FG})
    t.call("type_text", {"pid": pid, "window_id": wid, "text": url, **FG})
    t.call("press_key", {"pid": pid, "window_id": wid, "key": "Return", **FG})


def run(t):
    prep_close("firefox")
    t.call("launch_app", {"name": "firefox"})
    w = wait_window(t, "firefox", title_part="Mozilla Firefox", min_width=600)
    pid, wid = w["pid"], w["window_id"]
    maximize(t, pid, wid)
    time.sleep(2)  # first paint of the new-tab page
    t.shot("firefox opened", pid, wid)

    # 1. example.com through the address bar
    navigate(t, pid, wid, "https://example.com")
    title = wait_title(t, pid, "Example Domain")
    doc = find_elements(state(t, pid, wid, query="Example Domain", fmt="elements", timeout_ms=5000),
                        role="document web", label_re=r"^Example Domain$")
    para = find_elements(state(t, pid, wid, query="documentation", fmt="elements", timeout_ms=5000),
                         role="paragraph", label_re=r"documentation examples")
    t.evidence["example_com"] = {"title": title, "document": [d.get("label") for d in doc],
                                 "paragraph": [p.get("label") for p in para][:1]}
    t.check("example.com loaded (title + page text via AT-SPI)",
            "Example Domain" in title and doc and para, t.evidence["example_com"])
    t.shot("example.com", pid, wid)

    # 2. second tab through the toolbar's accessible "Open a new tab" button
    st = state(t, pid, wid, query="new tab")
    t.call("click", {"pid": pid, "window_id": wid, "element_token": token(st, r'push button "Open a new tab')})
    time.sleep(1.5)
    tabs = find_elements(state(t, pid, wid, query="tab", fmt="elements"), role="page tab")
    t.check("second tab opened", len(tabs) == 2, [x.get("label") for x in tabs])

    # 3. wikipedia.org in the new tab
    navigate(t, pid, wid, "https://www.wikipedia.org")
    wait_title(t, pid, "Wikipedia")
    time.sleep(1.5)
    st = state(t, pid, wid, query="Search Wikipedia", timeout_ms=5000)
    t.shot("wikipedia portal", pid, wid)

    # 4. the site's search form: focus the field with a real click, type, read it
    #    back; if live page content (e.g. a fundraising banner) got in the way,
    #    dismiss it through its accessible button and try again.
    def fill_search():
        st = state(t, pid, wid, query="Search Wikipedia", timeout_ms=5000)
        t.call("click", {"pid": pid, "window_id": wid, "element_token": token(st, r'entry "Search Wikipedia"'), **FG})
        t.call("hotkey", {"pid": pid, "window_id": wid, "keys": ["ctrl", "a"], **FG})
        t.call("type_text", {"pid": pid, "window_id": wid, "text": "Alan Turing", **FG})
        st = state(t, pid, wid, query="Search Wikipedia", fmt="elements", timeout_ms=5000)
        entry = find_elements(st, role="entry", label_re="Search Wikipedia")
        val = entry[0].get("value") if entry else None
        if val != "Alan Turing":
            raise Step(f"search field reads back {val!r}")
        return val
    val = retry(t, "Wikipedia search field", fill_search, attempts=3,
                recover=lambda: dismiss_page_overlays(t, pid, wid))
    t.check("search field holds 'Alan Turing' (AT-SPI read-back)", val == "Alan Turing", val)
    st = state(t, pid, wid, query="Search", timeout_ms=5000)
    t.call("click", {"pid": pid, "window_id": wid, "element_token": token(st, r'push button "Search"')})

    # 5. the article
    title = wait_title(t, pid, "Alan Turing - Wikipedia", timeout=30)
    time.sleep(1.5)
    st = state(t, pid, wid, query="Alan Turing", fmt="elements", timeout_ms=8000)
    doc = find_elements(st, role="document web", label_re=r"^Alan Turing - Wikipedia$")
    heading = find_elements(st, role="heading", label_re=r"^Alan Turing$")
    lead = [e.get("label") for e in find_elements(st, label_re=r"Alan Mathison Turing")][:1]
    xwid, xtitle = active_window_xprop()
    t.evidence["article"] = {"title": title, "document": bool(doc), "heading": bool(heading),
                             "lead": (lead[0][:160] if lead else None), "xprop_active_title": xtitle}
    t.check("Alan Turing article displayed (title, document, H1 via AT-SPI; X title out-of-band)",
            doc and heading and xtitle and "Alan Turing - Wikipedia" in xtitle, t.evidence["article"])
    tabs = find_elements(state(t, pid, wid, query="tab", fmt="elements"), role="page tab")
    t.check("both tabs still open (Example Domain + article)",
            len(tabs) == 2 and any("Example Domain" in (x.get("label") or "") for x in tabs),
            [x.get("label") for x in tabs])
    t.shot("final state: Alan Turing article", pid, wid)

    # 6. quit Firefox (answers its in-window quit confirmation)
    t.check("Firefox quit", quit_firefox(t, pid, wid), "no Firefox window remains")
    return True
