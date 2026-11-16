"""Helpers shared by the claude-virtual-computer GUI tests. All GUI actions go through Cua
Driver MCP tools via TestRun.call(); `guest()`/`guest_session()` are used only
for environment prep (closing leftovers before recording starts) and for
independent, out-of-band verification (xprop, files on disk)."""
from __future__ import annotations

import re
import time

from harness import Step, guest, guest_session


def wait_window(t, app, title_part=None, timeout=40, min_width=200):
    """Poll list_windows until a window of `app` (and title part) exists."""
    end = time.time() + timeout
    while time.time() < end:
        ws = t.call("list_windows", {"app": app}).structured.get("windows", [])
        ws = [w for w in ws if w["width"] >= min_width and (title_part is None or title_part in w["title"])]
        if ws:
            ws.sort(key=lambda w: (w.get("z_index") is not None, w.get("z_index") or 0), reverse=True)
            return ws[0]
        time.sleep(1)
    raise Step(f"no {app!r} window{' titled ' + repr(title_part) if title_part else ''} after {timeout}s")


def wait_title(t, pid, part, timeout=30):
    end = time.time() + timeout
    titles = []
    while time.time() < end:
        titles = [w["title"] for w in t.call("list_windows", {"pid": pid}).structured.get("windows", []) if w["width"] > 200]
        if any(part in x for x in titles):
            return next(x for x in titles if part in x)
        time.sleep(1)
    raise Step(f"window title never contained {part!r} (last: {titles})")


def wait_gone(t, app, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        ws = [w for w in t.call("list_windows", {"app": app}).structured.get("windows", []) if w["width"] > 200]
        if not ws:
            return True
        time.sleep(1)
    return False


def state(t, pid, wid, query=None, fmt=None, timeout_ms=3000, **kw):
    a = {"pid": pid, "window_id": wid, "timeout_ms": timeout_ms, **kw}
    if query:
        a["query"] = query
    if fmt:
        a["tree_format"] = fmt
    return t.call("get_window_state", a)


def token(st, pattern):
    """element_token for the first markdown row matching `pattern`
    (regex applied after '[N] ')."""
    m = re.search(r"\[(\d+)\] " + pattern, st.text)
    if not m:
        raise Step(f"no element matching {pattern!r} in the accessibility tree")
    return f"{st.structured['snapshot_id']}:{m.group(1)}"


def wait_element(t, pid, wid, pattern, timeout=30, **kw):
    """Poll get_window_state until a row matches `pattern`. A window can be
    mapped before its toolkit has published the accessibility tree (seen on a
    cold start right after a reset: 1 element instead of 37). Returns the last
    read, whose element tokens are current."""
    end = time.time() + timeout
    st = None
    while time.time() < end:
        st = t.call("get_window_state", {"pid": pid, "window_id": wid, "timeout_ms": 3000, **kw})
        if re.search(r"\[(\d+)\] " + pattern, st.text):
            return st
        time.sleep(1)
    raise Step(f"no element matching {pattern!r} after {timeout}s (last read: {st.structured.get('element_count') if st else None} elements)")


def find_elements(st, role=None, label_re=None):
    out = []
    for e in st.structured.get("elements", []):
        if role and e.get("role") != role:
            continue
        if label_re and not re.search(label_re, e.get("label") or ""):
            continue
        out.append(e)
    return out


def active_window_xprop():
    """Out-of-band: the X server's _NET_ACTIVE_WINDOW id and title."""
    p = guest_session("w=$(xprop -root _NET_ACTIVE_WINDOW | sed -E 's/.*# (0x[0-9a-f]+).*/\\1/'); "
                      "echo $w; xprop -id $w _NET_WM_NAME 2>/dev/null | cut -d= -f2-")
    lines = p.stdout.strip().splitlines()
    wid = int(lines[0], 16) if lines and lines[0].startswith("0x") else None
    title = lines[1].strip().strip('"') if len(lines) > 1 else None
    return wid, title


def prep_close(*procs):
    """Environment prep before recording: make sure no leftover instance of an
    app under test is running (a previous failed run could leave one)."""
    # Match the full command line (process names are truncated to 15 chars,
    # e.g. "gnome-calculato"); "[g]nome-..." keeps pkill from matching the
    # shell that runs it.
    for p in procs:
        pat = f"[{p[0]}]{p[1:]}"
        guest(f"pkill -u $(id -u) -f '{pat}' 2>/dev/null; true")
    deadline = time.time() + 10
    while time.time() < deadline:
        left = [p for p in procs if guest(f"pgrep -u $(id -u) -f '[{p[0]}]{p[1:]}' >/dev/null").returncode == 0]
        if not left:
            break
        time.sleep(0.5)


def retry(t, label, fn, attempts=2, recover=None):
    """Run fn(); on failure record a recovery attempt, run recover(), retry."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < attempts:
                t.recoveries.append(f"{label}: {type(e).__name__}: {str(e)[:160]} -> retry {i + 2}/{attempts}")
                t.note(f"recovering ({label}): {e}")
                if recover:
                    recover()
                time.sleep(2)
    raise last


def odt_paragraphs(path):
    """Out-of-band: (tag, style, text) for each h/p in an ODT's content.xml."""
    p = guest("python3 - <<'EOF'\n"
              "import zipfile,re,json,html\n"
              f"z=zipfile.ZipFile({path!r}); s=z.read('content.xml').decode()\n"
              "out=[]\n"
              "for m in re.finditer(r'<text:(h|p)([^>]*)>(.*?)</text:(?:h|p)>', s, re.S):\n"
              "    st=re.search(r'text:style-name=\"([^\"]+)\"', m.group(2))\n"
              "    txt=html.unescape(re.sub(r'<[^>]+>', '', m.group(3)))\n"
              "    out.append([m.group(1), st.group(1) if st else '', txt])\n"
              "print(json.dumps({'mimetype': z.read('mimetype').decode(), 'paras': out}))\n"
              "EOF")
    import json
    if p.returncode != 0:
        raise Step(f"could not read {path}: {p.stderr[:200]}")
    return json.loads(p.stdout)


def quit_firefox(t, pid, wid):
    """Ctrl+Q, then answer Firefox's in-window "Close window and quit Firefox?"
    modal (an AT-SPI dialog inside the main window, not a separate X window)."""
    t.call("hotkey", {"pid": pid, "window_id": wid, "keys": ["ctrl", "q"], "delivery_mode": "foreground"})
    end = time.time() + 8
    while time.time() < end:
        time.sleep(0.8)
        ws = [x for x in t.call("list_windows", {"pid": pid}).structured.get("windows", []) if x["width"] > 100]
        if not ws:
            return True
        st = t.call("get_window_state", {"pid": pid, "window_id": wid, "query": "Quit", "timeout_ms": 3000}, ok_required=False)
        m = re.search(r"\[(\d+)\] push button \"Quit Firefox\"", st.text or "")
        if m and st.structured.get("snapshot_id"):
            t.milestone("quit confirmation dialog (in-window modal) -> Quit Firefox")
            t.call("click", {"pid": pid, "window_id": wid, "element_token": f"{st.structured['snapshot_id']}:{m.group(1)}"})
            break
    return wait_gone(t, "firefox", timeout=20)


OVERLAY_LABELS = {"close", "×", "x", "minimize", "hide", "dismiss", "maybe later", "no thanks",
                  "close banner", "close this banner", "not now"}


def dismiss_page_overlays(t, pid, wid):
    """Press in-page close/minimize/'maybe later' controls (fundraising banners,
    popups) through accessibility. Only controls inside the page (below the
    browser toolbar), never the window's own buttons."""
    st = t.call("get_window_state", {"pid": pid, "window_id": wid, "tree_format": "elements", "timeout_ms": 5000})
    hits = [e for e in st.structured.get("elements", [])
            if e.get("role") in ("push button", "link") and (e.get("frame") or {}).get("y", 0) > 100
            and (e.get("label") or "").strip().lower() in OVERLAY_LABELS]
    for e in hits[:3]:
        r = t.call("click", {"pid": pid, "window_id": wid, "element_token": e["element_token"]}, ok_required=False)
        if not r.is_error:
            t.recoveries.append(f"dismissed an in-page overlay via its accessible {e.get('role')} {e.get('label')!r}")
            break
    time.sleep(1)
    return len(hits)


def maximize(t, pid, wid):
    """Give a window the whole work area (EWMH request via set_window_frame,
    confirmed against list_windows geometry by the driver)."""
    return t.call("set_window_frame", {"pid": pid, "window_id": wid, "x": 0, "y": 27, "width": 1920, "height": 1000},
                  ok_required=False)


def in_view(st, element):
    """True when an element's frame lies inside its window's bounds."""
    f, wb = element.get("frame") or {}, st.structured.get("window_bounds") or {}
    if not f or not wb:
        return True
    cy = f.get("y", 0) + f.get("h", 0) / 2
    return 0 <= cy <= wb.get("height", 10**6) - 10
