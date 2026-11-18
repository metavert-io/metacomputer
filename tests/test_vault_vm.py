#!/usr/bin/env python3
"""The vault's compatibility tools against a running test VM, end to end:

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_vault_vm.py

1. request_site_login through the hub: the hub opens page-mode Chrome on tests/fixtures/site/login.html (served in
   the VM), the vault asks (a TEST build's stand-in for the Touch ID dialog approves through `approve ID`), then fills
   the username and password through its fill account and the hub submits; the page's own result line shows the
   lengths typed.
2. teleport_browser_session: a session imported from a synthetic Chrome profile on the Mac (a cookie for localhost,
   encrypted with a test key) is loaded into a driver profile's cookie store through the guarded session-load path;
   the VM's Chrome then sends that cookie to a page served in the VM, which shows only a hash of its value.

It uses a TEST build of the vault (scripts/build-vault.sh --test) with its own vault folder in /tmp, binds it to the
test VM from MCOMP_CONFIG, and never touches the installed vault, its data, or a real Chrome profile. A test VM only:
it starts a web server in the VM and leaves two driver profiles there (vmtest-login, vmtest-cookies).
"""
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402
import test_vault_import as tvi  # noqa: E402

LAB = ts.LAB
MC = os.path.join(LAB, "bin", "metacomputer")
check = ts.check
PASS = ts.PASS
USER = "vm-user@example.com"
PW = "Dummy-vm-pass-31"
COOKIE = "dummy-vm-cookie-value-41"
SITE = "http://localhost:8000"
VALUES = [PASS, PW, COOKIE, tvi.CHROME_KEY]

ECHO_SERVER = r'''
import hashlib, http.server, os, sys
os.chdir(sys.argv[1])
class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/cookie-check"):
            parts = [p.strip().split("=", 1) for p in (self.headers.get("Cookie") or "").split(";") if "=" in p]
            body = "<!doctype html><title>cookie check</title><p id=r>" + " ".join(
                n + ":" + hashlib.sha256(v.encode()).hexdigest()[:16] for n, v in parts) + " end</p>"
            data = body.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        else:
            super().do_GET()
    def log_message(self, *a):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
'''


def main():
    if not os.environ.get("MCOMP_CONFIG"):
        print("set MCOMP_CONFIG to a test VM's settings file (this test changes things in that VM)")
        return 2
    td = tempfile.mkdtemp(prefix="mcvm-", dir="/tmp")
    procs = []
    env = None
    try:
        env = run(td, procs)
    finally:
        sys.path.insert(0, os.path.join(LAB, "lib"))
        import mcp_hub
        if env:
            try:
                mcp_hub.stop_hub(env)
            except Exception:
                pass
        for p in procs:
            try:
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
            if isinstance(p, ts.Pty):
                p.close()
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(ts.results)}/{len(ts.results)} passed")
    return 0 if ts.results and all(ts.results) else 1


def run(td, procs):
    cfg = os.environ["MCOMP_CONFIG"]
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []
    vdir = os.path.join(td, "v")
    chrome = os.path.join(td, "chrome")
    # A synthetic Chrome profile: one cookie for localhost (not Secure, set from http://localhost:8000).
    path = os.path.join(chrome, "Default", "Network", "Cookies")
    tvi.make_cookies(path, [("localhost", "mc_session", COOKIE)])
    db = sqlite3.connect(path)
    now = int((time.time() + 11644473600) * 1_000_000)            # Chrome's time: microseconds since 1601
    db.execute("UPDATE cookies SET is_secure=0, source_scheme=1, source_port=8000, samesite=1, creation_utc=?, last_access_utc=?, "
               "last_update_utc=?, expires_utc=?", (now - 60_000_000, now, now, now + 365 * 86400 * 1_000_000))
    db.commit()
    db.close()
    venv = dict(os.environ, MCOMP_VAULT_DIR=vdir, MCOMP_VAULT_BIN=BIN, MCOMP_VAULT_ALLOW_TEST_BUILD="1",
                MCOMP_VAULT_TEST_CHROME_DIR=chrome, MCOMP_VAULT_TEST_CHROME_KEY=tvi.CHROME_KEY, MCOMP_VAULT_TEST_LOCAL_AUTH="allow",
                MCOMP_VAULT_TEST_PROMPT_TIMEOUT="60", MCOMP_EVENTS_DIR=os.path.join(td, "events"), MCOMP_EVENTS_SPOOL=os.path.join(td, "spool"))
    for k in ("MCOMP_VAULT_TEST_SSH", "MCOMP_VAULT_TEST_LISTENERS", "MCOMP_DIRECT_BRIDGE", "MCOMP_HUB_PID"):
        venv.pop(k, None)

    def tty(argv, answers, timeout=120):
        p = ts.Pty(argv, venv)
        try:
            for want, say in answers:
                p.expect(want, timeout=60)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        finally:
            p.close()
        return rc, p.text()

    def mc(*args, timeout=180):
        return subprocess.run([MC, *args], env=venv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    st = mc("status")
    check("the test VM is running and ready", st.returncode == 0 and "READY" in st.stdout, st.stdout[-400:])
    # the test site in the VM: the fixture pages, and /cookie-check (names and value hashes only)
    r = mc("put", os.path.join(LAB, "tests", "fixtures", "site"), "--to", "~/metacomputer-runs")
    srv = os.path.join(td, "echo_server.py")
    open(srv, "w").write(ECHO_SERVER)
    r2 = subprocess.run([MC, "sh", "--override", "-"], input=(
        "pkill -f 'echo_server.py|http.server 8000' 2>/dev/null; sleep 0.5\n"
        "rm -rf ~/.local/state/cua-driver/browser-profiles/vmtest-login ~/.local/state/cua-driver/browser-profiles/vmtest-cookies\n"
        f"cat > /tmp/echo_server.py <<'EOF'\n{ECHO_SERVER}\nEOF\n"
        "(setsid nohup python3 /tmp/echo_server.py ~/metacomputer-runs/site >/tmp/echo.log 2>&1 &)\n"
        "sleep 1; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login.html\n"), env=venv, capture_output=True, text=True, timeout=120)
    check("the test pages are served in the VM (login.html, /cookie-check)", r.returncode == 0 and r2.stdout.strip().endswith("200"),
          (r.stderr[-200:], r2.stdout[-200:], r2.stderr[-200:]))

    # the vault: a test build, its own folder, bound to this VM
    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    rc2, out2 = tty([BIN, "set", "demo", "--kind", "password", "--origin", SITE, "--username", USER, "--approval", "each"],
                    [("Vault passphrase", PASS + "\n"), ("Password for demo", PW + "\n"), ("same password again", PW + "\n")])
    rc3, out3 = tty([MC, "secrets", "bind"], [("Type yes to pin these settings", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("a test vault with a login for the VM's test site, bound to the VM (its real gateway and pinned host key)",
          rc == 0 and rc2 == 0 and rc3 == 0 and "Bound VM" in out3, (out[-200:], out2[-200:], out3[-400:]))
    rc, out = tty([MC, "secrets", "import-session", "--browser", "chrome", "--site", "localhost", "--name", "vmcookies",
                   "--to-profile", "vmtest-cookies"], [("Vault passphrase", PASS + "\n")])
    check("a session imported from the synthetic Chrome profile (one cookie for localhost)", rc == 0 and "Sealed 1 cookies" in out, out[-500:])
    u = ts.Pty([BIN, "unlock", "--for", "30m", *allow], venv)
    procs.append(u)
    u.expect("Vault passphrase: ")
    u.send(PASS + "\n")
    u.expect("unlocked until")
    time.sleep(0.5)

    sys.path.insert(0, os.path.join(LAB, "lib"))
    import mcp_hub
    import mcomp_sdk
    env = mcomp_sdk.resolve_env(cfg)
    env.update({k: v for k, v in venv.items() if k.startswith("MCOMP_VAULT") or k.startswith("MCOMP_EVENTS")})
    mcp_hub.stop_hub(env)          # a hub started earlier wouldn't have this test's vault settings
    h = mcomp_sdk.Hub(env=env, timeout=300, session="vmtest")
    h.connect()
    outputs = []

    def call(tool, args, timeout=300):
        r = h.call(tool, args, timeout=timeout)
        outputs.append(json.dumps(r.raw))
        return r

    # --- 1. request_site_login ------------------------------------------------------------------------
    r = call("request_site_login", {"url": SITE + "/login.html", "profile": "vmtest-login", "agent": "vm-test"})
    rid = r.structured.get("request_id")
    check("request_site_login: pending with a request id while the vault asks", not r.is_error and r.structured.get("status") == "pending"
          and rid, r.raw)
    u.expect("Request " + (rid or "none"), timeout=60)
    ra = subprocess.run([BIN, "approve", rid], env=venv, capture_output=True, text=True, timeout=120)
    r = call("request_site_login", {"url": SITE + "/login.html", "request_id": rid, "wait_secs": 90})
    sc = r.structured
    check("after `approve ID`: filled and submitted, on the test site, with a masked username; no value in the result",
          ra.returncode == 0 and not r.is_error and sc.get("status") == "filled" and sc.get("origin") == SITE and sc.get("submitted") is True
          and sc.get("username_hint") == "v***@example.com" and sc.get("same_site") is True and "page_url" not in sc
          and not any(v in json.dumps(r.raw) for v in VALUES), (ra.stderr, r.raw))
    tgt = {"target_id": sc.get("target_id"), "tab_id": sc.get("tab_id"), "session": sc.get("session")}
    text = ""
    for _ in range(20):
        s = h.call("get_browser_state", dict(tgt, snapshot_format="semantic_v2", query="Signed in"))
        text = s.text + json.dumps(s.structured)
        if "Signed in" in text:
            break
        time.sleep(0.5)
    m = re.search(r"username (\d+) characters, password (\d+) characters", text)
    check("the page itself says it got a username and a password of the right lengths (the VM's Chrome, the fill account)",
          m is not None and int(m.group(1)) == len(USER) and int(m.group(2)) == len(PW), text[-400:])
    term = u.text()
    check("the unlock terminal asked once for the sign-in, named the agent as unverified, and showed no value",
          term.count("Sign in with demo (username and password) on " + SITE) == 1 and '"vm-test" (unverified)' in term
          and not any(v in term for v in VALUES), term[-800:])
    h.call("end_session", {"session": sc.get("session") or "vmtest"})

    # --- 2. teleport_browser_session -------------------------------------------------------------------
    r = call("teleport_browser_session", {"sites": ["localhost"], "browser": "chrome"})
    rid = r.structured.get("request_id")
    check("teleport_browser_session: consent_required with a request id", not r.is_error and r.structured.get("consent_required") is True and rid,
          r.raw)
    u.expect("Request " + (rid or "none"), timeout=90)
    ra = subprocess.run([BIN, "approve", rid], env=venv, capture_output=True, text=True, timeout=120)
    r = call("teleport_browser_session", {"sites": ["localhost"], "request_id": rid, "wait_secs": 90})
    check("after `approve ID`: moved into the driver profile (the hub had the driver create its cookie store first)",
          ra.returncode == 0 and not r.is_error and r.structured.get("moved") is True and r.structured.get("profile") == "vmtest-cookies", r.raw)
    pid, prep = None, ""
    for _ in range(10):
        p = h.call("browser_prepare", {"session": "vmcookie", "allow_launch": True, "profile": {"mode": "isolated_named", "name": "vmtest-cookies"}})
        pid, prep = p.structured.get("prepared_pid"), p.text
        if pid:
            break
        time.sleep(2)
    check("Chrome opens on that profile", bool(pid), prep[-400:])
    win = None
    for _ in range(40):
        ws = h.call("list_windows", {"pid": pid, "session": "vmcookie"}).structured.get("windows") or []
        if ws:
            win = ws[0]
            break
        time.sleep(0.25)
    b = h.call("get_browser_state", {"pid": pid, "window_id": (win or {}).get("window_id"), "session": "vmcookie"}).structured
    t2 = {"target_id": b.get("target_id"), "tab_id": ((b.get("tabs") or [{}])[0]).get("tab_id"), "session": "vmcookie"}
    h.call("browser_navigate", dict(t2, url=SITE + "/cookie-check"))
    want = "mc_session:" + hashlib.sha256(COOKIE.encode()).hexdigest()[:16]
    seen = ""
    for _ in range(20):
        s = h.call("get_browser_state", dict(t2, snapshot_format="semantic_v2"))
        seen = s.text + json.dumps(s.structured)
        if "end" in seen and "mc_session" in seen:
            break
        time.sleep(0.5)
    check("the VM's Chrome sends the imported cookie: the page sees mc_session with the right value (as a hash)", want in seen, seen[-500:])
    h.call("end_session", {"session": "vmcookie"})

    h.close()
    u.send("\x03")
    u.wait(15)
    rc = subprocess.run([MC, "sh", "--override", "-c", "pkill -f echo_server.py; true"], env=venv, capture_output=True, text=True, timeout=60)
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no value in any of {len(outputs)} tool results", not leaked, leaked)
    return env


if __name__ == "__main__":
    sys.exit(main())
