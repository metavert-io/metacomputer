#!/usr/bin/env python3
"""Chrome guard in the on-demand bridge (`claude-vc mcp-lazy`), end to end.

Runs through the same bridge Claude Code sessions use and checks that:
  1. background keys aimed at Chrome are sent in the foreground (with a note) and work;
  2. set_value and run_script aimed at Chrome are refused with the route to use;
  3. a run_steps batch aimed at Chrome is sent in the foreground;
  4. after another app's background typing leaves the driver's virtual devices
     behind (which blocks Chrome's keyboard), the bridge removes them before
     Chrome's next keyboard input, says so, and typing works.
Needs the VM running and the test pages served (it serves tests/fixtures/site
itself on 127.0.0.1:8766). Usage: python3 tests/test_chrome_guard.py
"""
import os
import subprocess
import sys
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "cualab"))
from mcp_client import MCPClient  # noqa: E402
from labconfig import GUEST_RUNS  # noqa: E402

CUA = os.path.join(LAB, "bin", "claude-vc")
PORT = 8766
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)


def ssh(cmd):
    return subprocess.run([CUA, "ssh", "ops", cmd], capture_output=True, text=True).stdout.strip()


def note(r):
    return (r.structured or {}).get("cua_lab_note") or ""


def title(c, pid):
    ws = (c.call("list_windows", {"pid": pid}).structured or {}).get("windows", [])
    return ws[0]["title"] if ws else ""


def navigate(c, pid, wid, path, background=True):
    mode = {} if background else {"delivery_mode": "foreground"}
    r1 = c.call("hotkey", {"pid": pid, "window_id": wid, "keys": ["ctrl", "l"], **mode})
    c.call("type_text", {"pid": pid, "window_id": wid, "text": f"127.0.0.1:{PORT}/{path}", **mode})
    c.call("press_key", {"pid": pid, "window_id": wid, "key": "Return", **mode})
    time.sleep(2)
    return r1


def main():
    subprocess.run([CUA, "put", os.path.join(LAB, "tests", "fixtures", "site"), "--to", GUEST_RUNS],
                   check=True, capture_output=True)
    ssh(f"cd {GUEST_RUNS}/site && (nohup python3 -m http.server {PORT} --bind 127.0.0.1 >/dev/null 2>&1 "
        f"& echo $! > /tmp/cua-guard-http.pid); sleep 1")
    ssh("pkill -u cua -x chrome; pkill -u cua -x gnome-calculator; sleep 1; true")   # start clean
    subprocess.run([CUA, "input-reset"], capture_output=True)
    c = MCPClient(cmd=[CUA, "mcp-lazy"])
    c.start()
    try:
        r = c.call("launch_app", {"launch_path": "/usr/bin/google-chrome-stable", "additional_arguments": [
            "--no-first-run", "--no-default-browser-check", f"http://127.0.0.1:{PORT}/form.html"]})
        win = None
        for _ in range(40):
            ws = [w for w in (c.call("list_windows", {"app": "chrome"}).structured or {}).get("windows", [])
                  if "Lab form" in w.get("title", "")]
            if ws:
                win = ws[0]
                break
            time.sleep(0.5)
        if not win:
            raise SystemExit("Chrome's window never appeared")
        pid, wid = win["pid"], win["window_id"]

        print("1. background keys to Chrome")
        r1 = navigate(c, pid, wid, "page2.html", background=True)
        check("bridge sent them in the foreground and said so", "foreground" in note(r1), note(r1)[:100])
        check("the keys worked (Chrome navigated)", "Page two" in title(c, pid), title(c, pid))

        print("2. refusals")
        r = c.call("set_value", {"pid": pid, "window_id": wid, "element_token": "s1:1", "value": "x"})
        check("set_value aimed at Chrome refused", r.is_error and "browser_type" in (r.text or ""), (r.text or "")[:90])
        r = c.call("run_script", {"script": f'const a = await cua.getApp({{pid: {pid}, windowId: {wid}}}); return 1;'})
        check("run_script aimed at Chrome refused", r.is_error and "run_steps" in (r.text or ""), (r.text or "")[:90])

        print("3. run_steps aimed at Chrome")
        navigate(c, pid, wid, "form.html", background=False)
        r = c.call("run_steps", {"pid": pid, "window_id": wid, "steps": [
            {"click": {"role": "entry", "name": "Name"}},
            {"tool": "type_text", "args": {"text": "Guarded"}},
            {"click": {"role": "button", "name": "Submit"}, "expect": [{"text": "name=Guarded"}]}]})
        check("batch sent in the foreground and the typing reached the page",
              not r.is_error and "foreground" in note(r), (note(r) or r.text or "")[:100])

        print("4. leftover virtual devices from another app")
        calc = c.call("launch_app", {"name": "gnome-calculator"}).structured
        time.sleep(2)
        cw = (c.call("list_windows", {"pid": calc["pid"]}).structured or {}).get("windows", [])[0]
        r = c.call("type_text", {"pid": calc["pid"], "window_id": cw["window_id"], "text": "7"})
        left = subprocess.run([CUA, "input-reset", "--check"], capture_output=True, text=True).stdout.strip()
        check("calculator's background typing left a virtual device pair", left == "1" and "mpx" in (r.text or ""),
              f"pairs={left}")
        r1 = navigate(c, pid, wid, "page2.html", background=False)
        after = subprocess.run([CUA, "input-reset", "--check"], capture_output=True, text=True).stdout.strip()
        check("bridge removed it before Chrome's keyboard input and said so",
              "removed 1" in note(r1) and after == "0", note(r1)[:120])
        check("Chrome's keyboard works afterwards", "Page two" in title(c, pid), title(c, pid))

        c.call("kill_app", {"pid": calc["pid"]})
        c.call("hotkey", {"pid": pid, "window_id": wid, "keys": ["ctrl", "shift", "q"], "delivery_mode": "foreground"})
    finally:
        c.close()
        ssh("kill $(cat /tmp/cua-guard-http.pid) 2>/dev/null; rm -f /tmp/cua-guard-http.pid; "
            "pkill -u cua -x chrome; pkill -u cua -x gnome-calculator; true")
    passed = sum(ok for _, ok, _ in results)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
