#!/usr/bin/env python3
"""The browser features on a running test VM: a network allowlist enforced by the gateway (DNS, PTR, escaped names,
an allowed site loads, an unlisted one doesn't, the list ends on time), web gpu, debug, read, responsive --device,
the Firefox page tools (fill, select, check, upload, submit; a password field refused), a trace exported as a
Playwright test, and the hub's human-check note and refusal.

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_web_vm.py

A test VM only (its settings file in MCOMP_CONFIG, or a checkout whose config/local.env names a VM that isn't
`metacomputer`): it serves tests/fixtures/site inside the VM on 127.0.0.1:8000, opens browsers there, and sets and
lifts a one-minute allowlist. The VM must have been started by this checkout (its gateway is mcomp-netgw).
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
CLI = os.path.join(LAB, "bin", "metacomputer")
sys.path.insert(0, os.path.join(LAB, "lib"))
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"   [{str(detail)[-400:]}]"), flush=True)


def mc(*args, timeout=600, stdin=None):
    r = subprocess.run([CLI, *args], capture_output=True, text=True, timeout=timeout, input=stdin,
                       stdin=None if stdin is not None else subprocess.DEVNULL)
    return r.returncode, r.stdout + r.stderr


def vm_name():
    rc, out = mc("where")
    m = re.search(r"^vm\s+(\S+)", out, re.M)
    return m.group(1) if m else ""


NETTEST = r"""
for u in https://example.com https://www.wikipedia.org; do printf '%s ' "$u"; curl -s -o /dev/null -m 8 -w '%{http_code}\n' "$u" || echo 000; done
python3 - <<'PY'
import socket, struct, random
def q(labels, qtype):
    qn = b"".join(bytes([len(l)]) + l.encode() for l in labels) + b"\0"
    pkt = struct.pack(">HHHHHH", random.randrange(65536), 0x0100, 1, 0, 0, 0) + qn + struct.pack(">HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5); s.sendto(pkt, ("__GW__", 53))
    try:
        return {0: "NOERROR", 3: "NXDOMAIN", 5: "REFUSED"}.get(s.recv(4096)[3] & 0xF, "OTHER")
    except socket.timeout:
        return "TIMEOUT"
print("dns-unlisted", q(["wikipedia", "org"], 1))
print("dns-ptr", q(["secret-123", "exfil", "example", "org"], 12))
print("dns-escaped", q(["secret.example", "com"], 16))
PY
"""


def net_checks():
    print("network allowlist (the gateway)")
    gw = re.search(r"^NET_GATEWAY_IP=(\S+)", open(os.path.join(LAB, "config", "defaults.env")).read(), re.M).group(1)
    script = NETTEST.replace("__GW__", gw)
    rc, before = mc("sh", "-c", script)
    check("before: both sites load", "https://example.com 200" in before and "https://www.wikipedia.org 200" in before, before)
    rc, out = mc("net", "allow", "example.com", "--for", "1m")
    check("net allow: in force, by the gateway's own word", rc == 0 and "in force: the gateway reports" in out, out)
    rc, during = mc("sh", "-c", script)
    check("during: the listed site loads", "https://example.com 200" in during, during)
    check("…an unlisted one doesn't", "https://www.wikipedia.org 000" in during, during)
    check("…the gateway answers no DNS for an unlisted name, a PTR question or an escaped name",
          "dns-unlisted REFUSED" in during and "dns-ptr REFUSED" in during and "dns-escaped REFUSED" in during, during)
    rc, out = mc("net", "allow", "wikipedia.org")
    check("widening without a terminal: refused", rc == 3 and "your own terminal" in out, out)
    deadline = time.time() + 120
    while time.time() < deadline:
        rc, st = mc("net", "status")
        if "open:" in st:
            break
        time.sleep(5)
    check("the list ends on time, by itself", "open:" in st, st)
    rc, after = mc("sh", "-c", script)
    check("after: both sites load again", "https://www.wikipedia.org 200" in after, after)


def web_checks():
    print("web gpu, debug, read, devices")
    gh = "/home/" + re.search(r"^GUEST_USER=(\S+)", open(os.path.join(LAB, "config", "defaults.env")).read(), re.M).group(1)
    rc, out = mc("put", os.path.join(LAB, "tests", "fixtures", "site"), "--to", gh + "/Inbox", "--overwrite")
    mc("sh", "-c", "pkill -f 'http.server 8000' ; cd ~/Inbox/site && setsid -f python3 -m http.server 8000 "
                   "--bind 127.0.0.1 </dev/null >/dev/null 2>&1; sleep 1")
    u = "http://127.0.0.1:8000"
    rc, out = mc("web", "gpu", "https://example.com", "--browser", "both", "--json")
    try:
        d = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        d = {}
    b = d.get("browsers") or {}
    check("web gpu: WebGL and WebGPU in Chrome (software)", (b.get("chrome") or {}).get("webgl", {}).get("renderer")
          and (b.get("chrome") or {}).get("webgpu", {}).get("available") is True, out[-600:])
    check("…and in Firefox", (b.get("firefox") or {}).get("webgl", {}).get("renderer")
          and (b.get("firefox") or {}).get("webgpu", {}).get("available") is True, out[-600:])
    rc, out = mc("web", "debug", f"{u}/a11y.html")
    check("web debug: the missing image is a failed request and a console error", rc == 0 and "x.png" in out
          and "Failed requests" in out and "Console errors" in out, out)
    rc, out = mc("web", "read", f"{u}/page2.html")
    check("web read: the page's text", rc == 0 and "Arrived." in out, out)
    rc, out = mc("web", "responsive", f"{u}/form.html", "--device", "iphone-15,desktop")
    check("responsive --device: phone and desktop emulation", rc == 0 and "| iphone-15 |" in out and "| desktop |" in out
          and "| 3 |" in out, out)


def page_checks():
    print("Firefox page tools")
    u = "http://127.0.0.1:8000"
    rc, out = mc("web", "page", "open", f"{u}/form.html", "--name", "vmt")
    refs = dict(re.findall(r"^(e\d+)\s+\S+\s+'([^']*)'", out, re.M))
    by = {v: k for k, v in refs.items()}
    check("page open: the controls by ref", rc == 0 and "Name" in by and "Submit" in by, out)
    steps = [("fill", by.get("Name"), "Ada"), ("select", by.get("Colour"), "Blue"), ("click", by.get("I agree"), None),
             ("click", by.get("Submit"), None)]
    for action, ref, val in steps:
        rc, o = mc("web", "page", action, ref or "e0", *([val] if val else []), "--name", "vmt")
        check(f"page {action} {ref}", rc == 0, o)
    rc, out = mc("web", "page", "read", "--name", "vmt", "--text")
    check("…and the page shows what was entered", "name=Ada colour=blue agree=true" in out, out)
    mc("web", "page", "close", "--name", "vmt")
    rc, out = mc("web", "page", "open", f"{u}/login.html", "--name", "vmt2")
    pw = re.search(r"^(e\d+)\s+input\[password\]", out, re.M)
    rc, out = mc("web", "page", "fill", pw.group(1) if pw else "e0", "x", "--name", "vmt2")
    check("a password field is refused (secrets fill --human)", rc != 0 and "secrets fill --human" in out, out)
    mc("web", "page", "close", "--name", "vmt2")
    rc, out = mc("sh", "-c", "pgrep -fc mcomp-page- || true")
    check("closing leaves no browser of the helper's running", out.strip().splitlines()[-1] == "0", out)


def trace_and_botcheck():
    print("trace export, human checks")
    import mcomp_sdk
    u = "http://127.0.0.1:8000"
    mc("trace", "start", "vmtest", "--session", "vmtr")
    c = mcomp_sdk.Hub(kind="call", session="vmtr").connect()
    try:
        c.call("launch_app", {"name": "firefox", "additional_arguments": ["--new-window", f"{u}/form.html"]})
        w = None
        for _ in range(30):
            ws = (c.call("list_windows", {"app": "firefox"}).structured or {}).get("windows", [])
            w = next((x for x in ws if "Lab form" in (x.get("title") or "")), None)
            if w:
                break
            time.sleep(1)
        pid, wid = w["pid"], w["window_id"]
        c.call("run_steps", {"pid": pid, "window_id": wid, "steps": [
            {"click": {"role": "check box", "name": "I agree"}},
            {"click": {"role": "push button", "name": "Submit"}, "expect": [{"text": "agree=true"}]}]})
        mc("trace", "stop")
        rc, code = mc("trace", "export", "vmtest", "--url", f"{u}/form.html")
        check("trace export: a Playwright test of the steps", rc == 0 and 'getByRole("checkbox", { name: "I agree" })' in code
              and 'getByRole("button", { name: "Submit" })' in code and "toBeVisible()" in code, code)
        c.call("launch_app", {"name": "firefox", "additional_arguments": ["--new-window", f"{u}/botcheck.html"]})
        for _ in range(30):
            ws = (c.call("list_windows", {"app": "firefox"}).structured or {}).get("windows", [])
            w = next((x for x in ws if (x.get("title") or "").startswith("Sign in")), None)
            if w:
                break
            time.sleep(1)
        pid, wid = w["pid"], w["window_id"]
        r = c.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": False,
                                        "query": "robot|Continue"})
        check("a human check on screen: the hub's note", "bot check" in (r.text or ""), (r.text or "")[:300])
        r2 = c.call("press_key", {"pid": pid, "window_id": wid, "key": "Tab"})
        check("…and acting there is refused (bot_check)", r2.is_error and (r2.structured or {}).get("code") == "bot_check",
              (r2.text or "")[:200])
    finally:
        c.close()
        mc("trace", "delete", "vmtest")


if __name__ == "__main__":
    name = vm_name()
    if not os.environ.get("MCOMP_CONFIG") and name in ("", "metacomputer"):
        print("a test VM only: set MCOMP_CONFIG to its settings file (this test opens browsers and sets an allowlist)")
        sys.exit(2)
    print(f"test VM: {name}")
    net_checks()
    web_checks()
    page_checks()
    trace_and_botcheck()
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
