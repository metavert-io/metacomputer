#!/usr/bin/env python3
"""Typing exactly, and the user's mouse and keyboard after the agent drove the VM, against a running test VM:

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_input_vm.py [T] [L] [K]     (default: T L)

T  Typing. A string with ' " ` ~ $ \\ and non-ASCII (é ü ß – € 日本), typed through `metacomputer call TOOL -` (the
   arguments on stdin: the route for text with quotes; in a single-quoted shell argument every ' is lost, see
   test_hub.py) into Mousepad, in the foreground and in the background, and into a textarea on a local page in
   Firefox (run_steps, human pacing), comes back exactly: Mousepad's saved file, and what the page posts.
L  The live view after the agent. Firefox driven in human mode (background input leaves the driver's "CUA v1.…" pair)
   and page-mode Chrome (browser_prepare) on a local page with a modal dialog, a cross-origin iframe dialog and a
   cookie banner. A handoff opened while slow background typing still runs leaves the pair (its reset is pending: with
   a pair left, Chrome ignores the live view's keyboard), and the waiting handoff removes it once that call has ended,
   with the handoff still open. Then the user's clicks and keys, sent the way Screen Sharing sends them (a VNC client
   on this Mac through the live view's port, tests/vncuser.py; x11vnc turns them into the X server's core pointer and
   keyboard events), reach the modal, the iframe and the banner, trusted, in Chrome and in Firefox. The user's "done"
   (a pseudo-terminal no agent session is above, tests/userterm.py) ends it, and the agent's next call acts.
K  (about 6 minutes) Page-mode Chrome is still open after a 6-minute handoff: Cua Driver ends a driver session after 5
   minutes without a call in it, and the hub keeps the session through the handoff.

A test VM only (the main VM is refused): it serves tests/fixtures/input on 127.0.0.1:8410 and :8411 in the VM, opens and
closes Mousepad, Firefox and Chrome, and reads the test VM's own live-view password file (VNC_PASSWORD_STORE=file;
without it L's live-view part is skipped), never printing it. Its events go to a temporary folder (and its spool too, unless
the VM's own settings file fixes it: a config/vms/ VM); no panel on the Mac's screen (MCOMP_HANDBACK_PANEL=off).
"""
import json
import os
import pty
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import userterm  # noqa: E402
import vncuser  # noqa: E402

MC = os.path.join(LAB, "bin", "metacomputer")
FIX = os.path.join(LAB, "tests", "fixtures", "input")
GUEST_DIR = "~/metacomputer-runs/input-test"
SITE = GUEST_DIR + "/input"
PORT, FRAME_PORT = 8410, 8411
URL = f"http://127.0.0.1:{PORT}"
SESSION = "input-vm-test"
CHROME_SESSION = "input-vm-chrome"
TEXT = "It's \"quoted\" `tick` ~tilde $HOME \\back, café naïve Grüße – €5 日本 'end'"
FAILS, PASSES = [], [0]


def check(name, cond, detail=""):
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:600]})" if detail and not cond else ""), flush=True)


def mc(env, *args, stdin=None, timeout=180):
    return subprocess.run([MC] + list(args), env=env, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def call(env, tool, args, session=SESSION, timeout=180):
    """One call through the hub, its arguments on stdin (`call TOOL -`): the result as JSON, or None."""
    r = mc(env, "call", "--session", session, "--timeout", str(timeout - 30), "--json", tool, "-",
           stdin=json.dumps(args, ensure_ascii=False), timeout=timeout)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"isError": True, "content": [{"type": "text", "text": (r.stdout + r.stderr)[-500:]}]}


def text_of(res):
    return " ".join(c.get("text", "") for c in (res or {}).get("content") or [] if isinstance(c, dict))


def sh(env, script, read_only=False):
    return mc(env, "sh", *(["--read-only"] if read_only else []), "-", stdin=script)


def guest_text(env, path):
    """A guest file's text exactly, copied out with `metacomputer get` (a read: it also works while the user has the
    computer, when `sh --read-only` waits for an agent), or None."""
    d = tempfile.mkdtemp(prefix="mcinput-get-")
    try:
        r = mc(env, "get", path, d)
        f = os.path.join(d, os.path.basename(path))
        if r.returncode != 0 or not os.path.isfile(f):
            return None
        with open(f, encoding="utf-8") as fh:
            return fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def log_lines(env):
    out = []
    for line in (guest_text(env, f"{GUEST_DIR}/typed.txt.log") or "").splitlines():
        try:
            d = json.loads(line)
            ev = json.loads(d["ev"])
            ev["port"] = d["port"]
            out.append(ev)
        except (ValueError, KeyError):
            pass
    return out


def windows(env):
    res = call(env, "list_windows", {})
    return ((res or {}).get("structuredContent") or {}).get("windows") or []


def window(env, pred, tries=40):
    for _ in range(tries):
        w = next((w for w in windows(env) if pred(w)), None)
        if w:
            return w
        time.sleep(0.5)
    return None


def pairs(env):
    r = mc(env, "input-reset", "--check")
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def as_user(env, *args):
    """A command at the user's own terminal: a pseudo-terminal in a process no agent session is above."""
    m, s = pty.openpty()
    p = userterm.spawn([MC] + list(args), env=env, stdin=s, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(s)
    try:
        o, e = p.communicate(timeout=180)
    finally:
        os.close(m)
    return p.returncode, o.decode(errors="replace"), e.decode(errors="replace")


def settings(cfg):
    """VNC_PORT and VNC_PASSWORD_STORE as the test VM's settings say (bash syntax, read after the defaults)."""
    r = subprocess.run(["/bin/bash", "-c", 'set -a; . "$1"; . "$2"; printf "%s\\n%s\\n" "$VNC_PORT" "${VNC_PASSWORD_STORE:-file}"',
                        "_", os.path.join(LAB, "config", "defaults.env"), cfg], capture_output=True, text=True, timeout=30)
    lines = r.stdout.splitlines()
    return (lines[0], lines[1]) if len(lines) >= 2 else (None, None)


# --- T: typing ---------------------------------------------------------------------------------------------------------
def typing(env):
    print("T: typing ' \" ` ~ $ \\ and non-ASCII exactly")
    home = sh(env, "printf '%s' \"$HOME\"\n").stdout.strip()     # the desktop user's, for launch_app
    for mode in ("foreground", "background"):
        path = f"{GUEST_DIR}/editor-{mode}.txt"
        sh(env, f"pkill -x mousepad; : > {path}; true\n")
        time.sleep(1)
        res = call(env, "launch_app", {"name": "mousepad", "additional_arguments": [home + path[1:]]})
        pid = ((res or {}).get("structuredContent") or {}).get("pid")
        w = window(env, lambda w: w.get("pid") == pid and f"editor-{mode}" in (w.get("title") or ""))
        if not w:
            check(f"Mousepad opens the file ({mode})", False, text_of(res))
            continue
        res = call(env, "type_text", {"pid": pid, "window_id": w["window_id"], "text": TEXT, "delivery_mode": mode})
        call(env, "hotkey", {"pid": pid, "window_id": w["window_id"], "keys": ["ctrl", "s"], "delivery_mode": "foreground"})
        time.sleep(1.5)
        got = guest_text(env, path)
        check(f"Mousepad, {mode} typing through `call type_text -`: the saved file has the text exactly", got == TEXT,
              (got, text_of(res)[:200]))
        call(env, "kill_app", {"pid": pid})
    sh(env, "pkill -x mousepad; pkill -x firefox-bin; pkill -x firefox; true\n")
    time.sleep(2)
    res = call(env, "launch_app", {"name": "firefox", "additional_arguments": [URL + "/typing.html"]})
    w = window(env, lambda w: "Typing check" in (w.get("title") or ""), tries=60)
    if not w:
        check("Firefox shows the typing page", False, text_of(res))
        return None
    time.sleep(1)
    res = call(env, "run_steps", {"pid": w["pid"], "window_id": w["window_id"], "delivery_mode": "foreground", "steps": [
        {"tool": "click", "args": {"role": "entry", "name": "Box"}}, {"tool": "type_text", "args": {"text": TEXT}}]})
    sent = call(env, "run_steps", {"pid": w["pid"], "window_id": w["window_id"], "steps": [
        {"tool": "click", "args": {"role": "push button", "name": "Send"}}]})
    got = None
    for _ in range(10):
        time.sleep(1)
        got = guest_text(env, f"{GUEST_DIR}/typed.txt")
        if got is not None:
            break
    check("Firefox, a textarea on a local page, run_steps with human pacing through `call run_steps -`: the page posts "
          "the text exactly", got == TEXT, (got, text_of(res)[-300:], text_of(sent)[-300:]))
    return w


# --- L: the live view after the agent ---------------------------------------------------------------------------------
def screen_point(calib, rect, extra=(0, 0)):
    """A page rect's centre in screen pixels, from a calibration pointerdown (screen minus client coordinates)."""
    dx, dy = calib["sx"] - calib["x"], calib["sy"] - calib["y"]
    return int(rect[0] + extra[0] + rect[2] / 2 + dx), int(rect[1] + extra[1] + rect[3] / 2 + dy)


def wait_log(env, pred, since, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        hit = [e for e in log_lines(env)[since:] if pred(e)]
        if hit:
            return hit[-1]
        time.sleep(0.4)
    return None


def free_point(w, others):
    """A point of window w's page, below the dialog page's button and iframe and above its banner, that no other window
    covers (its first click raises the window), or None."""
    b = w.get("bounds") or {}

    def inside(o, x, y):
        ob = o.get("bounds") or {}
        return ob.get("x", 0) <= x < ob.get("x", 0) + ob.get("width", 0) and ob.get("y", 0) <= y < ob.get("y", 0) + ob.get("height", 0)
    for fy in (0.55, 0.6, 0.5, 0.65, 0.7, 0.8, 0.3):
        for fx in (0.02, 0.97, 0.5, 0.25, 0.75, 0.9, 0.1):
            x, y = int(b["x"] + 10 + (b["width"] - 20) * fx), int(b["y"] + b["height"] * fy)
            if not any(inside(o, x, y) for o in others):
                return x, y
    return None


def user_round(env, v, browser, w, others, label):
    """The user's round in one browser window through the live view: a calibration click on blank page, then the modal
    (open, a name typed, OK), the cookie banner and the iframe's Confirm. Returns what the page logged."""
    n0 = len(log_lines(env))
    pt = free_point(w, others)
    if not pt:
        check(f"{label}: a point of its window no other window covers", False, (w, others))
        return []
    v.click(*pt)
    calib = wait_log(env, lambda e: e.get("b") == browser and e.get("what") == "pointerdown", n0)
    layout = next((e for e in reversed(log_lines(env)) if e.get("b") == browser and e.get("what") == "layout"), None)
    frame = next((e for e in reversed(log_lines(env)) if e.get("b") == browser and e.get("what") == "frame-layout"), None)
    if not (calib and layout and frame):
        check(f"{label}: the page sees the live view's calibration click", False, (calib, bool(layout), bool(frame)))
        return []
    v.click(*screen_point(calib, layout["open"]))
    modal = wait_log(env, lambda e: e.get("b") == browser and e.get("what") == "layout-modal", n0)
    if modal:
        v.click(*screen_point(calib, modal["name"]))
        v.type("user")
        v.click(*screen_point(calib, modal["ok"]))
    v.click(*screen_point(calib, layout["accept"]))
    v.click(*screen_point(calib, frame["confirm"], extra=(layout["frame"][0] + 2, layout["frame"][1] + 2)))
    time.sleep(1.5)
    return [e for e in log_lines(env)[n0:] if e.get("b") == browser]


def live_view(env, cfg, td):
    print("L: the user's clicks and keys through the live view, after the agent drove Firefox and page-mode Chrome")
    ff = window(env, lambda w: w.get("app_name") == "firefox")
    if not ff:
        check("Firefox is open (from T)", False)
        return
    call(env, "run_steps", {"pid": ff["pid"], "window_id": ff["window_id"], "delivery_mode": "foreground", "steps": [
        {"tool": "hotkey", "args": {"keys": ["ctrl", "l"]}}, {"tool": "type_text", "args": {"text": URL + "/dialog.html"}},
        {"tool": "press_key", "args": {"key": "Return"}}]})
    time.sleep(2)
    # Human mode's background input: the driver's own pair ("CUA v1.…") stays after it.
    call(env, "get_window_state", {"pid": ff["pid"], "window_id": ff["window_id"], "include_accessibility_tree": False})
    res = call(env, "scroll", {"pid": ff["pid"], "window_id": ff["window_id"], "x": 400, "y": 500, "direction": "up",
                               "amount": 2, "delivery_mode": "background"})
    check("Firefox: background input (the agent in human mode) leaves the driver's pair", (pairs(env) or 0) >= 1,
          (pairs(env), text_of(res)[:200]))
    # Page-mode Chrome on the same page.
    res = call(env, "browser_prepare", {"session": CHROME_SESSION, "allow_launch": True,
                                        "profile": {"mode": "isolated_new"}}, session=CHROME_SESSION)
    cpid = ((res or {}).get("structuredContent") or {}).get("prepared_pid")
    cw = window(env, lambda w: w.get("pid") == cpid)
    st = call(env, "get_browser_state", {"pid": cpid, "window_id": (cw or {}).get("window_id"), "session": CHROME_SESSION},
              session=CHROME_SESSION)
    sc = (st or {}).get("structuredContent") or {}
    tgt = {"target_id": sc.get("target_id"), "tab_id": ((sc.get("tabs") or [{}])[0]).get("tab_id"), "session": CHROME_SESSION}
    call(env, "browser_navigate", dict(tgt, url=URL + "/dialog.html"), session=CHROME_SESSION)
    time.sleep(2)
    check("page-mode Chrome (browser_prepare) shows the dialog page", bool(cw) and any(
        e.get("b") == "chrome" and e.get("what") == "layout" for e in log_lines(env)), (cpid, cw))
    # A handoff opened while slow background typing still runs in the driver: its reset is pending.
    sh(env, "pkill -x mousepad; true\n")
    res = call(env, "launch_app", {"name": "mousepad"})
    mpid = ((res or {}).get("structuredContent") or {}).get("pid")
    mw = window(env, lambda w: w.get("pid") == mpid)
    box = {}
    typing_call = threading.Thread(target=lambda: box.update(r=call(env, "type_text", {
        "pid": mpid, "window_id": (mw or {}).get("window_id"), "text": "x" * 300, "delay_ms": 60,
        "delivery_mode": "background"}, timeout=240)))
    typing_call.start()
    time.sleep(1.5)
    log = open(os.path.join(td, "opener.out"), "w+")
    opener = subprocess.Popen([MC, "handoff", "--why", "captcha", "--say", "Click through the dialog page", "--timeout",
                               "10m", "--json"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        end = time.time() + 60
        while time.time() < end:
            log.seek(0)
            if "Waiting up to" in log.read():
                break
            time.sleep(0.25)
        log.seek(0)
        said = log.read()
        check("a handoff opened while background typing still runs leaves the pair and says it'll go once that call "
              "ends", (pairs(env) or 0) >= 1 and "weren't removed yet" in said and "removed as soon as" in said, said[:700])
        typing_call.join(240)
        t_end = time.time()
        gone = None
        while time.time() < t_end + 20:
            if pairs(env) == 0:
                gone = time.time() - t_end
                break
            time.sleep(0.5)
        log.seek(0)
        said = log.read()
        check("once that call has ended, the waiting handoff removes the pair, the handoff still open, and says so",
              box.get("r") is not None and not box["r"].get("isError") and "Typed 300" in text_of(box["r"])
              and gone is not None and opener.poll() is None
              and "leftover virtual input devices were removed" in said, (gone, said[-500:], text_of(box.get("r"))[:200]))
        port, store = settings(cfg)
        pw_file = os.path.join(json.loads(mc(env, "hub", "env", "--json").stdout).get("MCOMP_SSH_DIR", ""), "vnc-pass")
        if store != "file" or not os.path.isfile(pw_file):
            print(f"SKIP the live view's clicks and keys: VNC_PASSWORD_STORE={store} (this test reads the file store)")
        else:
            with open(pw_file) as f:
                password = f.read().strip()
            ws = windows(env)
            cw = next((w for w in ws if w.get("pid") == cpid), cw)
            ff = next((w for w in ws if w.get("app_name") == "firefox"), ff)
            mp = [w for w in ws if w.get("pid") == mpid]      # the typing call's Mousepad, still open
            with vncuser.LiveView(port, password) as v:
                got_ch = user_round(env, v, "chrome", cw, mp, "Chrome (page mode)")
                got_ff = user_round(env, v, "firefox", ff, mp + [cw], "Firefox")
            for name, got in (("page-mode Chrome", got_ch), ("Firefox", got_ff)):
                what = {e.get("what") for e in got if e.get("trusted")}
                keys = "".join(e.get("key", "") for e in got if e.get("what") == "key" and e.get("trusted"))
                ok = next((e for e in got if e.get("what") == "modal-ok"), {})
                check(f"{name}, through the live view: the modal dialog opens and takes the typed name and OK",
                      {"open", "modal-ok"} <= what and keys == "user" and ok.get("name", "").endswith("user"),
                      (sorted(what), keys, ok))
                check(f"{name}, through the live view: the cookie banner's button and the cross-origin iframe's "
                      "dialog take the clicks", {"banner-accept", "iframe-confirm"} <= what, sorted(what))
        rc, o, e = as_user(env, "handoff", "done")
        check("the user's done (at their terminal) ends it", rc == 0, (rc, o[-200:], e[-200:]))
        rc = opener.wait(60)
        log.seek(0)
        res_line = json.loads(log.read().strip().splitlines()[-1] or "{}")
        check("the waiting handoff returns 0, done", rc == 0 and res_line.get("status") == "done", (rc, res_line))
        res = call(env, "set_agent_cursor_enabled", {"session": SESSION, "enabled": False})
        check("after done, the agent's acting call goes through", not (res or {}).get("isError"), text_of(res)[:200])
    finally:
        if opener.poll() is None:
            opener.terminate()
            as_user(env, "handoff", "cancel")
        call(env, "end_session", {"session": CHROME_SESSION}, session=CHROME_SESSION)


# --- K: page-mode Chrome through a long handoff -----------------------------------------------------------------------
def keep_browser(env, td):
    print("K: page-mode Chrome through a 6-minute handoff")
    res = call(env, "browser_prepare", {"session": CHROME_SESSION, "allow_launch": True,
                                        "profile": {"mode": "isolated_new"}}, session=CHROME_SESSION)
    cpid = ((res or {}).get("structuredContent") or {}).get("prepared_pid")
    opener = subprocess.Popen([MC, "handoff", "--why", "other", "--say", "Long handoff", "--timeout", "15m"], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        t0, alive = time.time(), True
        while time.time() - t0 < 360:
            time.sleep(30)
            r = mc(env, "inspect", "browser", "--json")     # a read that works during a handoff
            if not re.search(r'"pid":\s*%d\b' % int(cpid or 0), r.stdout):
                alive = False
                break
        check("page-mode Chrome is still open after 6 minutes of handoff (past the driver's 5-minute idle end)",
              bool(cpid) and alive, (cpid, int(time.time() - t0)))
        rc, o, e = as_user(env, "handoff", "done")
        opener.wait(60)
    finally:
        if opener.poll() is None:
            opener.terminate()
            as_user(env, "handoff", "cancel")
        call(env, "end_session", {"session": CHROME_SESSION}, session=CHROME_SESSION)


def main(argv):
    cfg = os.environ.get("MCOMP_CONFIG")
    if not cfg:
        print("set MCOMP_CONFIG to a test VM's settings file (this test acts on that VM's desktop)")
        return 2
    parts = [a.upper() for a in argv] or ["T", "L"]
    td = tempfile.mkdtemp(prefix="mcinput-")
    env = dict(os.environ, MCOMP_EVENTS_DIR=os.path.join(td, "events"), MCOMP_HANDBACK_PANEL="off")
    if not os.path.basename(os.path.dirname(os.path.abspath(cfg))) == "vms":   # a config/vms/ VM's spool is its own
        env["MCOMP_EVENTS_SPOOL"] = os.path.join(td, "spool")
    r = mc(env, "where")
    vm = next((line.split()[1] for line in r.stdout.splitlines() if line.startswith("vm ")), "")
    if vm in ("", "metacomputer"):
        print(f"refusing: MCOMP_CONFIG names the VM '{vm}'; use a test VM")
        return 2
    print(f"test VM: {vm}")
    try:
        st = mc(env, "status")
        check("the test VM is running and ready", st.returncode == 0 and "READY" in st.stdout, st.stdout[-300:])
        mc(env, "release")
        # A fresh driver runtime: nothing an earlier run left (a browser browser_prepare launched, a recording) keeps
        # the hub from reconnecting before background input.
        mc(env, "hub", "stop")
        sh(env, f"pkill -f 'serve.py 841'; rm -rf {GUEST_DIR}; mkdir -p {GUEST_DIR}; true\n")
        r = mc(env, "put", FIX, "--to", GUEST_DIR)
        r2 = sh(env, f"cd {SITE} && (setsid nohup python3 serve.py {PORT} {GUEST_DIR}/typed.txt >/dev/null 2>&1 &) && "
                     f"(setsid nohup python3 serve.py {FRAME_PORT} {GUEST_DIR}/typed.txt >/dev/null 2>&1 &)\n"
                     f"sleep 1; curl -s -o /dev/null -w '%{{http_code}}' {URL}/dialog.html\n")
        check("the test pages are served in the VM", r.returncode == 0 and r2.stdout.strip().endswith("200"),
              (r.stderr[-200:], r2.stdout[-200:], r2.stderr[-200:]))
        if "T" in parts:
            typing(env)
        if "L" in parts:
            if "T" not in parts:
                sh(env, "pkill -x firefox-bin; pkill -x firefox; true\n")
                call(env, "launch_app", {"name": "firefox", "additional_arguments": [URL + "/typing.html"]})
                window(env, lambda w: w.get("app_name") == "firefox", tries=60)
            live_view(env, cfg, td)
        if "K" in parts:
            keep_browser(env, td)
    finally:
        as_user(env, "handoff", "cancel")
        call(env, "end_session", {"session": SESSION})
        sh(env, f"pkill -f 'serve.py 841'; pkill -x mousepad; pkill -x firefox-bin; pkill -x firefox; rm -rf {GUEST_DIR}; true\n")
        mc(env, "input-reset")
        mc(env, "release")
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{PASSES[0]}/{PASSES[0] + len(FAILS)} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
