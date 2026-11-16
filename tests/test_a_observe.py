"""Test A: desktop observation (and proof the control path is the Linux guest).

Observes the desktop through Cua Driver only: driver health, running apps,
open windows (titles, geometry, stacking), the active window, an
accessibility tree with roles/labels/values/enabled state, screenshots, and a
state change (a window appearing; a tree diff after an edit). Closes the
editor through its unsaved-changes modal dialog.
"""
import json
import os
import platform
import socket

from common import active_window_xprop, prep_close, state, token, wait_gone, wait_window
from harness import guest

TITLE = "Desktop observation"
TASK = ("Connect to the VM, prove the desktop is the Linux guest, enumerate apps and windows, "
        "identify the active window, read accessibility data, capture screenshots, detect a state change")


def run(t):
    obs = {}
    # 1. Connected + which machine is this? (driver's own platform report)
    h = t.call("health_report", {})
    hs = h.structured
    obs["health"] = {"overall": hs.get("overall"), "platform": hs.get("platform"),
                     "os": next((c.get("data") for c in hs.get("checks", []) if c["name"] == "platform_supported"), None)}
    os_info = obs["health"]["os"] or {}
    t.check("driver reports a Linux arm64 guest (not this Mac)",
            hs.get("platform") == "linux" and "Ubuntu 24.04" in (os_info.get("os_version") or "")
            and os_info.get("architecture") == "arm64",
            f"driver: {hs.get('platform')} {os_info}; controller: {platform.system()} {platform.mac_ver()[0]} on {socket.gethostname()}")
    hn = guest("hostname").stdout.strip()
    obs["guest_hostname"] = hn

    # 2. Graphical desktop active: screen + desktop/panel windows
    size = t.call("get_screen_size", {}).structured
    obs["screen"] = size
    t.check("graphical desktop at 1920x1080", size.get("width") == 1920 and size.get("height") == 1080, size)
    t.shot("initial desktop")

    # 3. Running applications
    apps = t.call("list_apps", {}).structured.get("apps", [])
    running = sorted({a["name"] for a in apps if a.get("running")})
    obs["apps_running"] = running
    obs["apps_installed_count"] = len(apps)
    t.check("list_apps enumerates installed + running apps",
            len(apps) > 10 and any("Firefox" in a["name"] or "firefox" in (a.get("launch_path") or "") for a in apps),
            f"{len(apps)} apps, running: {running[:12]}")

    # 4. Open windows (titles, geometry, stacking) before
    before = t.call("list_windows", {}).structured.get("windows", [])
    obs["windows_before"] = [{k: w.get(k) for k in ("window_id", "pid", "app_name", "title", "bounds", "z_index")} for w in before]
    t.check("XFCE desktop + panel windows present",
            any(w["app_name"] == "Xfdesktop" for w in before) and any("panel" in w["app_name"].lower() for w in before),
            [f"{w['app_name']}:{w['title']}" for w in before])

    # 5. Open an editor -> state change: a new window appears
    prep_close("mousepad")
    t.call("launch_app", {"name": "mousepad"})
    w = wait_window(t, "mousepad")
    if w["title"] == "Mousepad":  # a startup prompt (e.g. crash-recovery "restore data?"), not the editor
        ps = t.shot("unexpected startup prompt", w["pid"], w["window_id"])
        t.recoveries.append(f"Mousepad opened a prompt first ({ps.text[:120]!r}); answered 'No' through its accessible button")
        t.call("click", {"pid": w["pid"], "window_id": w["window_id"], "element_token": token(ps, r'push button "(?:_)?No"')})
        w = wait_window(t, "mousepad", title_part=" - Mousepad")
    pid, wid = w["pid"], w["window_id"]
    after = t.call("list_windows", {}).structured.get("windows", [])
    new = [x for x in after if x["window_id"] not in {b["window_id"] for b in before} and x["width"] > 200]
    obs["windows_appeared"] = [{k: x.get(k) for k in ("window_id", "app_name", "title", "bounds")} for x in new]
    t.check("state change detected: new window in list_windows", any(x["window_id"] == wid for x in new),
            obs["windows_appeared"])

    # 6. Focus it and identify the active window two ways
    t.call("bring_to_front", {"pid": pid, "window_id": wid})
    wins = t.call("list_windows", {}).structured.get("windows", [])
    # Panels and the desktop are dock/desktop windows that always stack on top/bottom;
    # the frontmost application window is the highest z_index among the rest.
    apps_w = [x for x in wins if x.get("z_index") is not None and x["width"] > 200
              and x["app_name"] not in ("Xfce4-panel", "Xfdesktop")]
    top = max(apps_w, key=lambda x: x["z_index"])
    xwid, xtitle = active_window_xprop()
    obs["active_window"] = {"driver_frontmost": {"window_id": top["window_id"], "title": top["title"]},
                            "xprop": {"window_id": xwid, "title": xtitle}}
    t.check("active window: driver's frontmost == X server's _NET_ACTIVE_WINDOW",
            top["window_id"] == wid and xwid == wid, obs["active_window"])

    # 7. Accessibility tree: roles, labels, enabled/disabled, actions
    st = state(t, pid, wid, fmt="elements", timeout_ms=4000)
    els = st.structured.get("elements", [])
    roles = {}
    for e in els:
        roles[e.get("role")] = roles.get(e.get("role"), 0) + 1
    disabled = [f"{e.get('role')} {e.get('label')!r}" for e in els if e.get("enabled") is False]
    obs["a11y"] = {"window_title": st.structured.get("window_title"), "elements": len(els), "roles": roles,
                   "disabled_controls": disabled[:20],
                   "sample": [{k: e.get(k) for k in ("role", "label", "value", "enabled", "actions", "frame")} for e in els[:25]]}
    t.check("accessibility tree: roles, labels, enabled state",
            len(els) > 10 and "menu" in roles and len(disabled) > 0,
            f"{len(els)} elements; roles {dict(sorted(roles.items(), key=lambda kv: -kv[1])[:8])}; disabled e.g. {disabled[:3]}")

    # 8. Type through AT-SPI, then read only what changed (since=snapshot)
    md = state(t, pid, wid)
    snap = md.structured["snapshot_id"]
    tok = token(md, r'text "?')
    r = t.call("type_text", {"pid": pid, "window_id": wid, "element_token": tok,
                             "text": "Observed by Claude Code through Cua Driver."})
    obs["type_route"] = r.structured.get("route")
    diff = t.call("get_window_state", {"pid": pid, "window_id": wid, "since": snap})
    obs["since_diff"] = diff.text[:1500]
    t.check("tree diff (since) reports the edit", "Observed by Claude Code" in diff.text or "changed" in diff.text.lower(),
            diff.text[:200].replace("\n", " "))
    t.shot("editor with text", pid, wid)

    # 9. Close through the unsaved-changes modal dialog
    t.call("hotkey", {"pid": pid, "window_id": wid, "keys": ["ctrl", "q"], "delivery_mode": "foreground"})
    dlg = None
    for _ in range(10):
        ws = t.call("list_windows", {"pid": pid}).structured.get("windows", [])
        dlg = next((x for x in ws if x["window_id"] != wid and x["width"] > 100), None)
        if dlg:
            break
        import time; time.sleep(0.5)
    if dlg:
        ds = t.shot("unsaved-changes dialog", pid, dlg["window_id"])  # this read's tokens are current
        obs["modal_dialog"] = {"title": dlg["title"], "tree": ds.text[:800]}
        t.call("click", {"pid": pid, "window_id": dlg["window_id"],
                         "element_token": token(ds, r'push button "(?:_)?(?:Don.t Save|Do[n’\']t Save|Discard|Close without Saving)[^"]*"')})
    t.check("modal dialog handled and editor closed", dlg is not None and wait_gone(t, "mousepad"),
            f"dialog: {dlg and dlg['title']!r}")
    t.shot("final desktop")
    with open(os.path.join(t.res_dir, "observations.json"), "w") as f:
        json.dump(obs, f, indent=2)
    t.evidence["observations"] = os.path.relpath(os.path.join(t.res_dir, "observations.json"), os.path.dirname(os.path.dirname(t.res_dir)))
    return True
