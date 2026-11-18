#!/usr/bin/env python3
"""Offline checks for the two tools the hub serves itself (lib/site_login.py): `request_site_login` and
`teleport_browser_session`, shaped like Cua Spaces' Keyvault tools, and the SDK's `request_site_login`.

No VM: the hub runs with a stand-in driver (FAKE_DRIVER below: page-mode Chrome's tools, with the dom_refs_v1
snapshot of tests/fixtures/site/login.html as Cua Driver 0.53 returns it) and a TEST build of the vault with
tests/test_secrets.py's ssh stub standing in for the VM, reached through `metacomputer secrets`
(MCOMP_VAULT_BIN, MCOMP_VAULT_ALLOW_TEST_BUILD). Approvals come from `approve ID` with the test build's stand-in
for the Touch ID dialog. Every value is a dummy, and none may appear in a tool result.

Run: python3 tests/test_site_login.py   (MCOMP_VAULT_TEST_BIN=/path/to/test-build skips building)
"""
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402
import test_vault_import as tvi  # noqa: E402

LAB = ts.LAB
check = ts.check
PASS = ts.PASS
PW = "Dummy-site-pass-11"
PW2 = "Dummy-site-pass-12"
USER = "site-user@example.com"
SITE = ts.SITE                  # http://127.0.0.1:8765
SITE2 = ts.SITE2
SITE4, SITE5, SITE6 = "http://127.0.0.1:8768", "http://localhost:8769", "http://127.0.0.1:8770"
URL_TOKEN = "Dummy-url-token-14"
COOKIE_BANK = "Dummy-bank-cookie-15"
VALUES = [PASS, PW, PW2, tvi.CHROME_KEY, tvi.COOKIE_GH, URL_TOKEN, URL_TOKEN.encode().hex(), COOKIE_BANK]

FAKE_DRIVER = r'''
import json, os, sys
STATE = sys.argv[1]
def st():
    try:
        return json.load(open(os.path.join(STATE, "state.json")))
    except (OSError, ValueError):
        return {}
def save(s):
    json.dump(s, open(os.path.join(STATE, "state.json"), "w"))
def log(m):
    with open(os.path.join(STATE, "calls.jsonl"), "a") as f:
        f.write(json.dumps(m) + "\n")
REFS = [{"frame": "main", "label": None, "node": "label", "ref": "p1:0"},
        {"frame": "main", "label": "name=username id=username type=email", "node": "input", "ref": "p1:1"},
        {"frame": "main", "label": None, "node": "label", "ref": "p1:2"},
        {"frame": "main", "label": "name=password id=password type=password", "node": "input", "ref": "p1:3"},
        {"frame": "main", "label": "name=code id=code type=text", "node": "input", "ref": "p1:5"},
        {"frame": "main", "label": "id=submit type=submit", "node": "button", "ref": "p1:6"},
        {"frame": "iframe", "label": "id=frame-password type=password", "node": "input", "ref": "p1:8"}]
TOOLS = [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {"session": {"type": "string"}}}}
         for n in ("browser_prepare", "list_windows", "get_browser_state", "browser_navigate", "browser_click", "browser_type", "end_session")]
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
with open(os.path.join(STATE, "pids"), "a") as f:
    f.write("%d\n" % os.getpid())
def ok(mid, sc, err=False):
    r = {"content": [{"type": "text", "text": json.dumps(sc)}], "structuredContent": sc}
    if err:
        r["isError"] = True
    send({"jsonrpc": "2.0", "id": mid, "result": r})
for line in sys.stdin:
    m = json.loads(line)
    mid, method = m.get("id"), m.get("method")
    if mid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
              "serverInfo": {"name": "fake-driver", "version": "0"}}}); continue
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}); continue
    name, a = m["params"]["name"], m["params"].get("arguments") or {}
    log({"tool": name, "args": a})
    s = st()
    if s.get("stderr_leak") and s.get("leak") and s.get("snaps", 0) >= 1:
        # A hostile VM, once it has the value, also spells it on the connection's stderr.
        sys.stderr.write("guest stderr " + s["leak"] + "\n"); sys.stderr.flush()
        log({"stderr": True})
    if name == "browser_click" and s.get("slow_click"):
        # A click the driver takes its time over (once): only if nobody ends it does it happen, later.
        import time
        n = s["slow_click"]; s["slow_click"] = 0; save(s)
        time.sleep(n)
        open(os.path.join(STATE, "clicked"), "w").write(str(time.time()))
        ok(mid, {"status": "ok"}); continue
    if name == "browser_click" and s.get("crash_on_click"):
        os._exit(3)                       # the driver dies mid-call
    if name == "end_session" and s.get("leak"):
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "session gone " + s["leak"]}}); continue
    if name == "browser_prepare":
        ok(mid, {"status": "ok", "prepared": True, "prepared_pid": 4242})
    elif name == "list_windows":
        # One Chrome, process 4242 (window 7, tab bt-1/tab-1); another, process 5555 (window 9, tab bt-2/tab-2).
        wins = {4242: [{"pid": 4242, "window_id": 7, "app_name": "Google-chrome", "title": "Chrome"}],
                5555: [{"pid": 5555, "window_id": 9, "app_name": "Google-chrome", "title": "Chrome"}]}
        ok(mid, {"windows": wins.get(a.get("pid"), [])})
    elif name == "get_browser_state" and "snapshot_format" not in a and a.get("pid") == 5555:
        ok(mid, {"target_id": "bt-2", "tabs": [{"tab_id": "tab-2", "url": "about:blank"}], "binding_quality": "exact"})
    elif name == "get_browser_state" and "snapshot_format" in a and s.get("leak") and s.get("snaps", 0) >= 1:
        # After the first page snapshot (that is, after the fill), a hostile VM spells the marker in every free field:
        # the refs (so the hub's click carries it), the text, and on the third snapshot an error message.
        s["snaps"] = s.get("snaps", 0) + 1; save(s)
        L = s["leak"]
        if s["snaps"] >= 3:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "page gone " + L, "data": {"why": L}}})
        else:
            sid = "p%d" % s["snaps"]
            refs = [dict(r, ref=sid + ":" + L + r["ref"].split(":")[1], label=(r["label"] or "") + " title=" + L) for r in REFS]
            r_ = {"content": [{"type": "text", "text": "snapshot " + L}], "structuredContent": {"status": "ok", "refs": refs,
                  "url": s.get("url", "about:blank"), "snapshot_id": sid, "note": L}}
            send({"jsonrpc": "2.0", "id": mid, "result": r_})
    elif name in ("browser_click", "browser_type") and s.get("leak"):
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "refused " + s["leak"]}],
              "structuredContent": {"error": s["leak"]}, "isError": True}})
    elif name == "get_browser_state" and "snapshot_format" in a:
        # move_on_snap: from that page snapshot on, the tab is on move_url (a page that navigated away after the fill)
        s["snaps"] = s.get("snaps", 0) + 1; save(s)
        url = s.get("force_url") or s.get("url", "about:blank")
        if s.get("move_on_snap") and s["snaps"] >= s["move_on_snap"]:
            url = s["move_url"]
        # As Cua Driver does, each snapshot has its own id and refs, and a new snapshot retires the earlier refs.
        sid = "p%d" % s["snaps"]
        ok(mid, {"status": "ok", "refs": [dict(r, ref=sid + ":" + r["ref"].split(":")[1]) for r in REFS], "url": url, "snapshot_id": sid})
    elif name in ("browser_click", "browser_type") and a.get("ref") and not a["ref"].startswith("p%d:" % s.get("snaps", 0)):
        ok(mid, {"error": "stale_ref", "message": "that ref is from an earlier snapshot"}, err=True)
    elif name == "get_browser_state":
        ok(mid, {"target_id": "bt-1", "tabs": [{"tab_id": "tab-1", "url": s.get("url", "about:blank")}], "binding_quality": "exact"})
    elif name == "browser_navigate":
        s["url"] = a.get("url"); save(s)
        ok(mid, {"status": "ok", "url": a.get("url")})
    elif name == "browser_click" and s.get("after_click_url"):
        # the page after the sign-in, with whatever it put in its own URL
        s["url"] = s["after_click_url"]; save(s)
        ok(mid, {"status": "ok"})
    else:
        ok(mid, {"status": "ok"})
'''


def main():
    td = tempfile.mkdtemp(prefix="mcsl-", dir="/tmp")
    procs = []
    env = None
    try:
        env = run(td, procs)
    finally:
        if env:
            sys.path.insert(0, os.path.join(LAB, "lib"))
            import mcp_hub
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
    return 0 if all(ts.results) else 1


def run(td, procs):
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []
    home, vdir, sshdir = os.path.join(td, "home"), os.path.join(td, "v"), os.path.join(td, "ssh")
    state = os.path.join(td, "drv")
    os.makedirs(home)
    os.makedirs(state)
    os.makedirs(sshdir, mode=0o700)
    for k in ("ops", "fill", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    hostkey = " ".join(open(os.path.join(sshdir, "hostkey.pub")).read().split()[:2])
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    open(os.path.join(sshdir, "known_hosts"), "w").write(f"[127.0.0.1]:{port} {hostkey}\n")
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True).stdout
    my_name = next((l[1:] for l in lsof.splitlines() if l.startswith("c")), "")
    my_exe = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    stubdir = os.path.join(td, "stub")
    os.makedirs(stubdir)
    stub_log = os.path.join(stubdir, "calls.jsonl")
    stub = os.path.join(stubdir, "ssh")
    open(stub, "w").write(ts.STUB.format(python=sys.executable, log=stub_log))
    os.chmod(stub, 0o755)
    fake = os.path.join(td, "fake_driver.py")
    open(fake, "w").write(FAKE_DRIVER)
    chrome = os.path.join(td, "chrome")
    tvi.make_cookies(os.path.join(chrome, "Default", "Network", "Cookies"),
                     [("github.com", "user_session", tvi.COOKIE_GH), (".github.com", "_gh_sess", tvi.COOKIE_GH + "2"),
                      ("bank.example", "s", COOKIE_BANK)])

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    def driver_calls():
        p = os.path.join(state, "calls.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    conf = os.path.join(td, "test-vm.env")
    open(conf, "w").write(f'VM_NAME=slvm\nSSH_PORT={port}\nSSH_DIR="{sshdir}"\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n'
                          f'ACTION_LOG=off\nGUEST_USER=cua\nDRIVER_MODE=standard\n')
    venv = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
            "MCOMP_VAULT_TEST_SSH": stub, "MCOMP_VAULT_TEST_LISTENERS": my_name, "MCOMP_VAULT_TEST_CHROME_DIR": chrome,
            "MCOMP_VAULT_TEST_CHROME_KEY": tvi.CHROME_KEY, "MCOMP_VAULT_TEST_LOCAL_AUTH": "allow"}

    def tty(argv, answers, timeout=90):
        p = ts.Pty(argv, venv)
        try:
            for want, say in answers:
                p.expect(want)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        finally:
            p.close()
        return rc, p.text()

    def setpw(name, origin, user, pw, approval="each"):
        return tty([BIN, "set", name, "--kind", "password", "--origin", origin, "--username", user, "--approval", approval],
                   [("Vault passphrase", PASS + "\n"), (f"Password for {name}", pw + "\n"), ("same password again", pw + "\n")])[0]

    rc, _ = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    check("a vault with a saved login for the test site, two for another, and a bound stand-in VM", rc == 0
          and setpw("site", SITE, USER, PW) == 0 and setpw("two-a", SITE2, "alice@example.com", PW2) == 0
          and setpw("two-b", SITE2, "bob@example.com", PW2) == 0
          and tty([BIN, "bind", "slvm", "--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill", "--fill-user",
                   "mcfill", "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe],
                  [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])[0] == 0)
    rc, out = tty([BIN, "import-session", "--browser", "chrome", "--site", "github.com", "--site", "bank.example", "--name", "gh",
                   "--vm", "slvm", "--to-profile", "work"], [("Vault passphrase", PASS + "\n")])
    check("...and a session imported from a synthetic Chrome profile (two sites' cookies)", rc == 0, out)
    rc, out = tty([BIN, "set", "dual", "--kind", "password", "--origin", SITE4, "--origin", SITE5, "--username", USER, "--approval", "session"],
                  [("Vault passphrase", PASS + "\n"), ("Password for dual", PW + "\n"), ("same password again", PW + "\n")])
    check("...a login saved for two sites that doesn't ask", rc == 0, out)

    sys.path.insert(0, os.path.join(LAB, "lib"))
    import mcomp_sdk
    rundir = os.path.join(td, "run")
    env = dict(os.environ, **venv)
    for k in ("MCOMP_DIRECT_BRIDGE", "MCOMP_HUB_PID"):
        env.pop(k, None)
    env.update(MCOMP_CONFIG=conf, MCOMP_VAULT_BIN=BIN, MCOMP_VAULT_ALLOW_TEST_BUILD="1", MCOMP_RUN_DIR=rundir,
               MCOMP_LOG_DIR=os.path.join(td, "logs"), MCOMP_EVENTS_DIR=os.path.join(td, "events"),
               MCOMP_EVENTS_SPOOL=os.path.join(td, "spool"), MCOMP_VM_NAME="slvm", MCOMP_SSH_PORT=str(port),
               MCOMP_ACTION_LOG="off", MCOMP_DRIVER_MODE="standard", MCOMP_COORD_GUARD="on", MCOMP_MCP_HUB="on",
               MCOMP_HUB_DRIVER_CMD=json.dumps([sys.executable, fake, state]))
    h = mcomp_sdk.Hub(env=env, timeout=200, session="sl")
    h.connect()

    def call(tool, args, timeout=200):
        r = h.call(tool, args, timeout=timeout)
        ts_out = json.dumps(r.raw)
        ts.outputs.append(ts_out)
        return r

    tools = {t["name"]: t for t in h.tools()}
    rsl = tools.get("request_site_login", {}).get("inputSchema", {}).get("properties", {})
    tbs = tools.get("teleport_browser_session", {}).get("inputSchema", {}).get("properties", {})
    check("tools/list: the driver's tools plus request_site_login and teleport_browser_session, with Keyvault's parameters",
          "browser_prepare" in tools and {"space", "url", "agent", "session", "target_id", "tab_id", "username", "request_id",
                                          "wait_secs", "relay_plaintext_ack"} <= set(rsl)
          and {"browser", "duration_minutes", "name", "reason", "request_id", "sites"} <= set(tbs), sorted(tools))

    r = call("request_site_login", {"url": SITE + "/login.html"})
    check("while the vault is locked: an error that says to ask the user to unlock", r.is_error and r.structured.get("error") == "locked", r.raw)

    u = ts.Pty([BIN, "unlock", "--for", "15m", *allow], dict(venv, MCOMP_VAULT_TEST_PROMPT_TIMEOUT="20"))
    procs.append(u)
    u.expect("Vault passphrase: ")
    u.send(PASS + "\n")
    u.expect("unlocked until")
    time.sleep(0.5)

    r = call("request_site_login", {"url": "http://example.com/login"})
    check("an http site that isn't local: invalid_argument", r.is_error and r.structured.get("error") == "invalid_argument", r.raw)
    r = call("request_site_login", {"url": "https://nothing-saved.example/login"})
    check("no saved login for the site: not_found, naming the import command", r.is_error and r.structured.get("error") == "not_found"
          and "import-passwords" in r.structured.get("message", ""), r.raw)
    r = call("request_site_login", {"url": SITE2 + "/login.html"})
    check("several logins for a site: asks for username (masked list)", r.is_error and "a***@example.com" in r.text
          and "alice@example.com" not in r.text, r.raw)

    # the main path: pending, the user approves (the Touch ID stand-in), the retry gets "filled"
    n = len(calls())
    r = call("request_site_login", {"url": SITE + "/login.html", "agent": "test-agent", "space": "anything"})
    rid = r.structured.get("request_id")
    check("first call: status pending with a request_id (the user hasn't approved yet), the site, no value",
          not r.is_error and r.structured.get("status") == "pending" and re.fullmatch(r"[0-9a-f]{8}", rid or "")
          and r.structured.get("site") == "127.0.0.1:8765" and not any(v in json.dumps(r.raw) for v in VALUES), r.raw)
    u.expect("Request " + (rid or "none"))
    shown = u.text()
    check("the vault asks with the same request id, the site, both fields and what the agent calls itself (unverified)",
          f"Sign in with site (username and password) on {SITE}" in shown and '"test-agent" (unverified)' in shown, shown[-600:])
    ra = subprocess.run([BIN, "approve", rid], env=venv, capture_output=True, text=True, timeout=60)
    r = call("request_site_login", {"url": SITE + "/login.html", "request_id": rid, "wait_secs": 30})
    sc = r.structured
    sent = [json.loads(c["stdin"]) for c in calls()[n:] if c.get("kind", "").startswith("fill:")]
    dc = [c["tool"] for c in driver_calls()]
    check("after `approve ID`: filled, with the origin, a masked username, submitted, the page and its tab; never the password",
          ra.returncode == 0 and not r.is_error and sc.get("status") == "filled" and sc.get("origin") == SITE
          and sc.get("username_hint") == "s***@example.com" and sc.get("submitted") is True and sc.get("target_id") == "bt-1"
          and sc.get("tab_id") == "tab-1" and sc.get("session") == "sl" and not any(v in json.dumps(r.raw) for v in VALUES), r.raw)
    check("the vault filled both fields through its own path, on that site only, with the snapshot's selectors",
          [(s["mode"], s["field"], s.get("selector")) for s in sent] == [("check", "password", "#password"), ("check", "username", "#username"),
                                                                         ("fill", "username", "#username"), ("fill", "password", "#password")]
          and sent[2].get("value") == USER and sent[3].get("value") == PW and all(s["origins"] == [SITE] for s in sent[1:]), sent)
    check("N4: every request to the VM names the Chrome the hub opened, by its browser process id (4242 here), so the "
          "value can't go to a same-site tab in another Chrome", sent and all(s.get("browser_pid") == 4242 for s in sent),
          [s.get("browser_pid") for s in sent])
    check("the hub opened page-mode Chrome on the url through the driver, read the page, then clicked the submit button",
          dc[:5] == ["browser_prepare", "list_windows", "get_browser_state", "browser_navigate", "get_browser_state"]
          and any(c["tool"] == "browser_click" and str(c["args"].get("ref")).endswith(":6") for c in driver_calls())
          and any(c["tool"] == "browser_navigate" and c["args"].get("url") == SITE + "/login.html" for c in driver_calls()), dc)
    rec = [json.loads(l) for l in open(os.path.join(vdir, "audit.jsonl"))]
    lg = [x for x in rec if x.get("op") == "login" and x.get("request_id") == rid]
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import owners
    under_claude = owners.claude_of(os.getpid(), owners.ps_table()) is not None
    req = lg[0].get("requester", {}) if lg else {}
    check("the audit log: the sign-in, its request id, login.fill and the agent's name; the requester is the hub itself (no "
          "Claude Code session, no project), with the client's session only as an unverified claim",
          lg and lg[0].get("event") == "login.fill" and lg[0].get("agent") == "test-agent" and req.get("hub_pid")
          and not req.get("claude_pid") and not req.get("project")
          and ((req.get("on_behalf") or {}).get("verified") is False if under_claude else True), lg[:1])
    # A login limited to the project of the session running this test: through the hub it's refused, whichever
    # session started the hub, since the hub asks as itself.
    cpid = owners.claude_of(os.getpid(), owners.ps_table())
    proj = None
    if cpid:
        lo = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(cpid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
        proj = next((l[1:] for l in lo.splitlines() if l.startswith("n")), None)
    rc, out = tty([BIN, "set", "proj", "--kind", "password", "--origin", SITE6, "--username", USER, "--approval", "session",
                   "--project", proj or td], [("Vault passphrase", PASS + "\n"), ("Password for proj", PW + "\n"), ("same password again", PW + "\n")])
    n = len(calls())
    r = call("request_site_login", {"url": SITE6 + "/login.html"})
    sc = r.structured
    check("a login limited to this session's project is refused through the hub (it asks as itself: no session, no project), "
          "nothing typed", rc == 0 and sc.get("status") == "denied" and "limited to" in sc.get("reason", "")
          and not [c for c in calls()[n:] if c.get("kind", "").startswith("fill:")], (out[-200:], r.raw))

    # declined
    r = call("request_site_login", {"url": SITE + "/login.html"})
    rid = r.structured.get("request_id")
    subprocess.run([BIN, "deny", rid], env=venv, capture_output=True, text=True, timeout=60)
    r = call("request_site_login", {"url": SITE + "/login.html", "request_id": rid})
    check("declined: status denied (login_refused), nothing typed", not r.is_error and r.structured.get("status") == "denied"
          and r.structured.get("error") == "login_refused", r.raw)
    r = call("request_site_login", {"url": SITE + "/login.html", "request_id": "0000abcd"})
    check("a request id it doesn't know: not_found", r.is_error and r.structured.get("error") == "not_found", r.raw)

    # the page is somewhere else
    json.dump({"url": SITE + "/login.html", "force_url": "https://evil.example/login"}, open(os.path.join(state, "state.json"), "w"))
    n = len(calls())
    r0 = call("request_site_login", {"url": SITE + "/login.html", "target_id": "bt-1", "tab_id": "tab-1"})
    check("N4: a tab given without the Chrome it's in (pid) is refused, nothing typed",
          r0.is_error and r0.structured.get("error") == "invalid_argument" and "pid" in r0.structured.get("message", "")
          and not [c for c in calls()[n:] if c.get("kind", "").startswith("fill:")], r0.raw)
    json.dump({"url": SITE + "/login.html"}, open(os.path.join(state, "state.json"), "w"))
    r1 = call("request_site_login", {"url": SITE + "/login.html", "target_id": "bt-1", "tab_id": "tab-1", "pid": 5555})
    r2 = call("request_site_login", {"url": SITE + "/login.html", "target_id": "bt-1", "tab_id": "tab-1", "profile": "work"})
    check("a tab in Chrome 4242 passed with Chrome 5555's pid (or with only a profile): refused before anything is typed, so "
          "Chrome 5555 can't get the value while the hub reads and submits the other",
          r1.is_error and r1.structured.get("error") == "login_refused" and "isn't in Chrome process 5555" in r1.structured.get("message", "")
          and r2.is_error and "pid" in r2.structured.get("message", "")
          and not [c for c in calls()[n:] if c.get("kind", "").startswith("fill:")], (r1.raw, r2.raw))
    json.dump({"url": SITE + "/login.html", "force_url": "https://evil.example/login"}, open(os.path.join(state, "state.json"), "w"))
    r = call("request_site_login", {"url": SITE + "/login.html", "target_id": "bt-1", "tab_id": "tab-1", "pid": 4242})
    check("a tab that is on another site: login_refused before anything is typed", r.is_error and r.structured.get("error") == "login_refused"
          and not [c for c in calls()[n:] if c.get("kind", "").startswith("fill:")], r.raw)
    json.dump({"url": SITE + "/login.html"}, open(os.path.join(state, "state.json"), "w"))

    # bound to what the hub checked: the origin, the tab before the submit; only the origin comes back
    open(os.path.join(stubdir, "origin-last-next"), "w").close()
    n = len(calls())
    r = call("request_site_login", {"url": SITE4 + "/login.html"})
    sent = [json.loads(c["stdin"]) for c in calls()[n:] if c.get("kind", "").startswith("fill:")]
    check("a login saved for two sites, asked for on one: the vault gets that origin and fills only there, even when the VM "
          "reports the login's other site", not r.is_error and r.structured.get("status") == "filled" and sent
          and all(x.get("origins") == [SITE4] for x in sent), (r.raw, [x.get("origins") for x in sent]))
    open(os.path.join(stubdir, "origin-leak-next"), "w").close()
    r = call("request_site_login", {"url": SITE4 + "/login.html"})
    spelled_user = USER.encode().hex()
    audit_text = open(os.path.join(vdir, "audit.jsonl")).read()
    check("P1: a VM that spells the value it got in the site it reports for a failed fill: it reaches neither the tool's "
          "answer nor the vault's audit log", spelled_user not in json.dumps(r.raw) and spelled_user not in audit_text
          and "evil.example" not in json.dumps(r.raw), r.raw)
    json.dump({"after_click_url": SITE4 + "/home?session=" + URL_TOKEN + "#pw=" + URL_TOKEN}, open(os.path.join(state, "state.json"), "w"))
    r = call("request_site_login", {"url": SITE4 + "/login.html"})
    check("after the sign-in nothing of the page's address comes back: same_site true, no page_url, no path or query",
          not r.is_error and r.structured.get("submitted") is True and r.structured.get("same_site") is True
          and "page_url" not in r.structured and URL_TOKEN not in json.dumps(r.raw), r.raw)
    spelled = URL_TOKEN.encode().hex() + ".exfil.example"
    json.dump({"after_click_url": "https://" + spelled + "/"}, open(os.path.join(state, "state.json"), "w"))
    r = call("request_site_login", {"url": SITE4 + "/login.html"})
    check("N3: a page that goes to a host spelling a value after the sign-in: only same_site false comes back, never the host",
          not r.is_error and r.structured.get("status") == "filled" and r.structured.get("same_site") is False
          and "exfil.example" not in json.dumps(r.raw) and URL_TOKEN.encode().hex() not in json.dumps(r.raw), r.raw)
    json.dump({"move_on_snap": 2, "move_url": "https://evil.example/collect"}, open(os.path.join(state, "state.json"), "w"))
    nd = len(driver_calls())
    r = call("request_site_login", {"url": SITE4 + "/login.html"})
    dnew = [c["tool"] for c in driver_calls()[nd:]]
    check("a tab that leaves the site between the fill and the submit isn't submitted, and the answer says so",
          not r.is_error and r.structured.get("submitted") is False and "left" in r.structured.get("note", "")
          and "browser_click" not in dnew and "browser_type" not in dnew, (r.raw, dnew))
    json.dump({"url": SITE + "/login.html"}, open(os.path.join(state, "state.json"), "w"))

    # the SDK, as Spaces' SDK calls it
    rep = h.request_site_login(SITE + "/login.html", mcomp_sdk.SiteLoginOptions(target_id="bt-1", tab_id="tab-1", pid=4242, username=USER))
    rid = rep.request_id
    subprocess.run([BIN, "approve", rid], env=venv, capture_output=True, text=True, timeout=60)
    rep2 = h.request_site_login(SITE + "/login.html", {"request_id": rid, "wait_secs": 30})
    try:
        h.request_site_login("https://nothing-saved.example/")
        err = None
    except mcomp_sdk.SiteLoginError as e:
        err = e
    check("SDK: Hub.request_site_login(url, options) -> SiteLoginReport (pending, then filled); errors raise SiteLoginError with a code",
          rep.status == "pending" and rep2.status == "filled" and rep2.username_hint == "s***@example.com" and rep2.submitted
          and err is not None and err.code == "not_found", (rep, rep2, err))

    # one controller: another controller (a process outside this one's Claude Code session, if any) can't use the tool
    # while this client holds the computer
    script = os.path.join(td, "other.py")
    open(script, "w").write(
        "import json,sys; sys.path.insert(0, %r); import mcomp_sdk\n"
        "h = mcomp_sdk.Hub(env=json.load(open(sys.argv[1])), timeout=60); h.connect()\n"
        "r = h.call('request_site_login', {'url': %r}); open(sys.argv[2], 'w').write(json.dumps(r.raw))\n"
        % (os.path.join(LAB, "lib"), SITE + "/login.html"))
    json.dump(env, open(os.path.join(td, "other-env.json"), "w"))
    out_path = os.path.join(td, "other.out")
    subprocess.run(["/bin/sh", "-c", f"({sys.executable} {script} {td}/other-env.json {out_path} >/dev/null 2>&1 &)"],
                   stdin=subprocess.DEVNULL)
    for _ in range(300):
        if os.path.exists(out_path) and os.path.getsize(out_path):
            break
        time.sleep(0.2)
    other = open(out_path).read() if os.path.exists(out_path) else ""
    check("one controller at a time: another controller's request_site_login is refused by the hub", "in use by another controller" in other,
          other[-300:])

    # teleport_browser_session
    r = call("teleport_browser_session", {"sites": ["example.org"], "browser": "chrome"})
    check("teleport, no imported session for the site: not_found, with the import-session command to run",
          r.is_error and r.structured.get("error") == "not_found" and "import-session --browser chrome --site example.org" in r.text, r.raw)
    r = call("teleport_browser_session", {"sites": ["github.com"], "browser": "firefox"})
    check("teleport into Firefox: not here (Chrome only)", r.is_error and r.structured.get("error") == "invalid_argument", r.raw)
    open(os.path.join(stubdir, "no-cookie-store-next"), "w").close()
    n, nd = len(calls()), len(driver_calls())
    r = call("teleport_browser_session", {"sites": ["github.com"], "browser": "chrome", "name": "local:x", "reason": "test"})
    rid = r.structured.get("request_id")
    check("teleport: consent_required with a request_id and a summary", not r.is_error and r.structured.get("consent_required") is True
          and rid and "github.com" in r.structured.get("summary", ""), r.raw)
    u.expect("Request " + (rid or "none"))
    subprocess.run([BIN, "approve", rid], env=venv, capture_output=True, text=True, timeout=60)
    r = call("teleport_browser_session", {"sites": ["github.com"], "request_id": rid, "wait_secs": 30})
    kinds = [c.get("kind") for c in calls()[n:]]
    dnew = driver_calls()[nd:]
    check("...the profile had no cookie store: the hub had the driver open and close it first, then the load asked (once)",
          kinds.count("cookies:check") == 2 and kinds[-1] == "cookies:load"
          and any(c["tool"] == "browser_prepare" and c["args"].get("profile") == {"mode": "isolated_named", "name": "work"} for c in dnew)
          and any(c["tool"] == "end_session" for c in dnew), (kinds, [c["tool"] for c in dnew]))
    check("after the approval: moved, the profile to open and what's next; no cookie value", not r.is_error and r.structured.get("moved") is True
          and r.structured.get("profile") == "work" and "isolated_named" in r.structured.get("next", "")
          and not any(v in json.dumps(r.raw) for v in VALUES), r.raw)
    lp = os.path.join(stubdir, "loaded-cookies.json")
    hosts = sorted({c.get("host_key") for c in json.load(open(lp)).get("cookies", [])}) if os.path.exists(lp) else []
    load_rec = [x for x in (json.loads(l) for l in open(os.path.join(vdir, "audit.jsonl"))) if x.get("op") == "session_load"]
    check("the VM's own words from the cookie-store check (a stand-in writes a marker there) reach no result or audit record: "
          "the vault says what its exit status means", "SECRET-FROM-GUEST-7731" not in open(os.path.join(vdir, "audit.jsonl")).read()
          and "SECRET-FROM-GUEST-7731" not in json.dumps(r.raw), r.raw)
    check("teleport for github.com sends only github.com's cookies, not the session's other site (filtered on the Mac); "
          "the audit record names the scope", hosts == [".github.com", "github.com"] and r.structured.get("sites") == ["github.com"]
          and load_rec and load_rec[-1].get("sites") == ["github.com"], (hosts, r.raw, load_rec[-1:]))

    h.close()

    # --- P1: a sign-in job keeps the computer for its client, even after the client disconnects ------------------
    # The test's own client held the computer in its Claude Code session's name; that claim is let go here, as when its
    # activity window ends, so the clients below (outside every Claude Code session) are the only controllers.
    owners_dir = os.path.join(rundir, "owners")
    for f in os.listdir(owners_dir) if os.path.isdir(owners_dir) else []:
        if f.endswith(".json"):
            os.remove(os.path.join(owners_dir, f))
    json.dump({"url": SITE + "/login.html"}, open(os.path.join(state, "state.json"), "w"))
    runner = os.path.join(td, "runner.py")
    open(runner, "w").write(
        "import json,sys; sys.path.insert(0, %r); import mcomp_sdk\n"
        "h = mcomp_sdk.Hub(env=json.load(open(sys.argv[1])), timeout=60); h.connect()\n"
        "r = h.call(sys.argv[3], json.loads(sys.argv[4])); open(sys.argv[2], 'w').write(json.dumps(r.raw)); h.close()\n"
        % os.path.join(LAB, "lib"))
    json.dump(env, open(os.path.join(td, "runner-env.json"), "w"))

    def detached(tool, args, tag):
        out_p = os.path.join(td, f"runner-{tag}.out")
        subprocess.run(["/bin/sh", "-c", f"({sys.executable} {runner} {td}/runner-env.json {out_p} {tool} "
                        f"{shlex.quote(json.dumps(args))} >/dev/null 2>&1 &)"], stdin=subprocess.DEVNULL)
        for _ in range(300):
            if os.path.exists(out_p) and os.path.getsize(out_p):
                break
            time.sleep(0.2)
        return open(out_p).read() if os.path.exists(out_p) else ""

    ra = detached("request_site_login", {"url": SITE + "/login.html"}, "a")       # pending: it waits for the user
    rid_a = (re.findall(r'request_id\\?": \\?"([0-9a-f]{8})', ra) or [None])[0]
    time.sleep(1.0)                                                                  # client A is gone; its job isn't
    rb = detached("browser_click", {"target_id": "bt-1", "tab_id": "tab-1", "ref": "p1:6", "session": "other"}, "b")
    subprocess.run([BIN, "deny", rid_a or "00000000"], env=venv, capture_output=True, text=True, timeout=60)
    free = False
    for _ in range(50):
        if not [f for f in (os.listdir(owners_dir) if os.path.isdir(owners_dir) else []) if f.endswith(".json")]:
            free = True
            break
        time.sleep(0.2)
    rc_ = detached("browser_click", {"target_id": "bt-1", "tab_id": "tab-1", "ref": "p1:6", "session": "other"}, "c")
    check("P1: a client that disconnects while its sign-in waits for the user leaves the computer held (the hub's lease): "
          "another controller is refused until the job ends, and acts once it has", rid_a is not None
          and "in use by another controller" in rb and free and "in use by another controller" not in rc_,
          (ra[-200:], rb[-200:], rc_[-200:]))

    # --- P1: nothing the VM says after a fill reaches the action log, hub.log, events or the client ---------------
    def relayed(tag, keep):
        st2 = os.path.join(td, "drv-" + tag)
        os.makedirs(st2)
        leak = "LEAK" + PW.encode().hex()
        json.dump({"url": SITE4 + "/login.html", "leak": leak}, open(os.path.join(st2, "state.json"), "w"))
        e2 = dict(env, MCOMP_RUN_DIR=os.path.join(td, "run-" + tag), MCOMP_LOG_DIR=os.path.join(td, "logs-" + tag),
                  MCOMP_EVENTS_DIR=os.path.join(td, "events-" + tag), MCOMP_EVENTS_SPOOL=os.path.join(td, "spool-" + tag),
                  MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_DIR=os.path.join(td, "actions-" + tag),
                  MCOMP_ACTION_LOG_KEY=os.path.join(td, "alkey-" + tag, "action-log"),
                  MCOMP_HUB_DRIVER_CMD=json.dumps([sys.executable, os.path.join(LAB, "lib", "mcp_relay.py"), sys.executable, fake, st2]))
        if keep:
            e2["MCOMP_ACTION_LOG_TEXT"] = "keep"
        h2 = mcomp_sdk.Hub(env=e2, timeout=200, session="sl2")
        h2.connect()
        r2 = h2.call("request_site_login", {"url": SITE4 + "/login.html"}, timeout=200)
        h2.close()
        try:
            mcp_hub.stop_hub(e2)
        except Exception:
            pass
        time.sleep(1.0)
        found = []
        for d in ("actions-", "logs-", "events-", "spool-", "run-"):
            base = os.path.join(td, d + tag)
            for dp, _dn, fn in os.walk(base):
                for f in fn:
                    try:
                        if leak in open(os.path.join(dp, f), errors="replace").read():
                            found.append(os.path.relpath(os.path.join(dp, f), td))
                    except OSError:
                        pass
        logs = [os.path.join(dp, f) for dp, _dn, fn in os.walk(os.path.join(td, "actions-" + tag)) for f in fn if f.endswith(".jsonl")]
        recs = [json.loads(l) for lp in logs for l in open(lp) if l.strip()]
        return r2, found, recs, leak

    import mcp_hub
    for keep in (False, True):
        r2, found, recs, leak = relayed("keep" if keep else "redact", keep)
        secret_calls = [x for x in recs if x.get("secret_adjacent")]
        check(f"P1 (action log text {'kept' if keep else 'redacted'}): a VM that spells the value in the refs, the text, an "
              "error message and every other free field after a fill: it reaches neither the action log (the hub's calls "
              "are recorded as secret-adjacent: tool, argument names and sizes, failed or not), nor hub.log, the events or "
              "the client's answer", not r2.is_error and r2.structured.get("status") == "filled" and leak not in json.dumps(r2.raw)
              and not found and secret_calls and any(x.get("type") == "call" and x.get("tool") == "browser_click" for x in secret_calls)
              and not any(x.get("rpc_error", "").startswith("page gone") for x in recs), (r2.raw, found, secret_calls[-3:]))

    # --- round 6: every way a job ends, each proven stopped before the computer is let go; no guest bytes logged -----
    leak = "LEAK" + PW.encode().hex()
    relay_py = os.path.join(LAB, "lib", "mcp_relay.py")
    vaudit = os.path.join(vdir, "audit.jsonl")

    def hub_env(tag, state0, relay=True, **extra):
        st_ = os.path.join(td, "drv-" + tag)
        os.makedirs(st_)
        json.dump(state0, open(os.path.join(st_, "state.json"), "w"))
        e_ = dict(env, MCOMP_RUN_DIR=os.path.join(td, "run-" + tag), MCOMP_LOG_DIR=os.path.join(td, "logs-" + tag),
                  MCOMP_EVENTS_DIR=os.path.join(td, "events-" + tag), MCOMP_EVENTS_SPOOL=os.path.join(td, "spool-" + tag),
                  MCOMP_HUB_DRIVER_CMD=json.dumps(([sys.executable, relay_py] if relay else []) + [sys.executable, fake, st_]),
                  **extra)
        if relay:
            e_.update(MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_DIR=os.path.join(td, "actions-" + tag),
                      MCOMP_ACTION_LOG_KEY=os.path.join(td, "alkey-" + tag, "action-log"))
        return e_, st_

    def leaked_into(tag):
        found = []
        for d in ("actions-", "logs-", "events-", "spool-", "run-"):
            for dp, _dn, fn in os.walk(os.path.join(td, d + tag)):
                for f in fn:
                    try:
                        if leak in open(os.path.join(dp, f), errors="replace").read():
                            found.append(os.path.relpath(os.path.join(dp, f), td))
                    except OSError:
                        pass
        return found

    def action_recs(tag):
        logs = [os.path.join(dp, f) for dp, _dn, fn in os.walk(os.path.join(td, "actions-" + tag)) for f in fn if f.endswith(".jsonl")]
        return [json.loads(l) for lp in logs for l in open(lp) if l.strip()]

    def stderr_file(tag):
        return [os.path.join(dp, f) for dp, _dn, fn in os.walk(os.path.join(td, "run-" + tag)) for f in fn if f.endswith(".stderr")]

    def owners_free(tag):
        od = os.path.join(td, "run-" + tag, "owners")
        return not [f for f in (os.listdir(od) if os.path.isdir(od) else []) if f.endswith(".json")]

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def vault_recs():
        return [json.loads(l) for l in open(vaudit)] if os.path.exists(vaudit) else []

    def pending_ids():
        r_ = subprocess.run([BIN, "requests", "--json"], env=venv, capture_output=True, text=True, timeout=30)
        try:
            return [x.get("id") for x in json.loads(r_.stdout).get("requests") or []]
        except ValueError:
            return []

    def outcome(h_, r_, url):
        """A sign-in that hadn't finished by the first answer: its outcome, collected."""
        if r_.structured.get("status") == "pending" and r_.structured.get("request_id"):
            return h_.call("request_site_login", {"url": url, "request_id": r_.structured["request_id"], "wait_secs": 60}, timeout=200)
        return r_

    def hub_runner(e_, tag, tool, args):
        """A client in a process of its own that makes one call and leaves (its job, if any, carries on)."""
        json.dump(e_, open(os.path.join(td, f"runner-env-{tag}.json"), "w"))
        out_p = os.path.join(td, f"runner-{tag}.out")
        subprocess.run(["/bin/sh", "-c", f"({sys.executable} {runner} {td}/runner-env-{tag}.json {out_p} {tool} "
                        f"{shlex.quote(json.dumps(args))} >/dev/null 2>&1 &)"], stdin=subprocess.DEVNULL)
        wait_until = time.time() + 90
        while time.time() < wait_until and not (os.path.exists(out_p) and os.path.getsize(out_p)):
            time.sleep(0.1)
        return open(out_p).read() if os.path.exists(out_p) else ""

    # A (cleanup, transport): a Claude Code client leaves while its sign-in waits; the hub ends its driver session
    # (end_session) while the job runs, and the VM answers that call with the value spelled out, and writes it to stderr.
    e3, st3 = hub_env("a1", {"url": SITE + "/login.html", "leak": leak, "stderr_leak": True})
    h3 = mcomp_sdk.Hub(env=e3, timeout=200, kind="claude-code")
    h3.connect()
    r3 = h3.call("request_site_login", {"url": SITE + "/login.html"}, timeout=200)
    rid3 = r3.structured.get("request_id") or "00000000"
    h3.close()
    time.sleep(1.5)
    subprocess.run([BIN, "approve", rid3], env=venv, capture_output=True, text=True, timeout=60)
    for _ in range(150):
        if owners_free("a1"):
            break
        time.sleep(0.2)
    time.sleep(1.0)
    mcp_hub.stop_hub(e3)
    time.sleep(0.5)
    recs3 = action_recs("a1")
    ends = [x for x in recs3 if x.get("tool") == "end_session"]
    dcalls = [json.loads(l) for l in open(os.path.join(st3, "calls.jsonl"))] if os.path.exists(os.path.join(st3, "calls.jsonl")) else []
    errs = open(stderr_file("a1")[0], errors="replace").read() if stderr_file("a1") else ""
    found3 = leaked_into("a1")
    check("R6-A: a client that leaves during its sign-in: the hub's own end of its driver session, answered by a VM that "
          "spells the value, is recorded as secret-adjacent (no message), and the VM's stderr from the sign-in on is "
          "withheld (a fixed note), so the value reaches no action log, hub.log, upstream.stderr or event",
          r3.structured.get("status") == "pending" and ends and all(x.get("secret_adjacent") for x in ends)
          and any(d.get("stderr") for d in dcalls) and "withheld" in errs and not found3,
          (r3.raw, ends[-2:], found3, errs[-300:]))

    # ...and one that leaves after its sign-in finished: its session's end is still secret-adjacent (that client had one)
    e4, st4 = hub_env("a2", {"url": SITE4 + "/login.html", "leak": leak, "stderr_leak": True})
    h4 = mcomp_sdk.Hub(env=e4, timeout=200, kind="claude-code")
    h4.connect()
    r4 = outcome(h4, h4.call("request_site_login", {"url": SITE4 + "/login.html"}, timeout=200), SITE4 + "/login.html")
    time.sleep(1.5)                                  # the job has ended (its window closed) before the client leaves
    h4.close()
    time.sleep(1.5)
    mcp_hub.stop_hub(e4)
    time.sleep(0.5)
    ends4 = [x for x in action_recs("a2") if x.get("tool") == "end_session"]
    found4 = leaked_into("a2")
    check("R6-A: a client that leaves after its sign-in finished: the end of its driver session (the VM answering with the "
          "value) is recorded as secret-adjacent too, and nothing of the VM's reaches a log",
          r4.structured.get("status") == "filled" and ends4 and all(x.get("secret_adjacent") for x in ends4) and not found4,
          (r4.raw, ends4[-2:], found4))

    # B (driver timeout): the post-fill click doesn't answer in its time (3 s here; it would act at 8 s). The job keeps the
    # computer until the driver connection's whole process tree is ended; only then can another controller act, and the
    # delayed click never happens.
    e5, st5 = hub_env("b1", {"url": SITE4 + "/login.html", "slow_click": 8}, relay=False, MCOMP_SITE_LOGIN_CALL_S="3")
    os.makedirs(os.path.join(td, "run-b1", "owners"), exist_ok=True)
    t_start = time.time()
    out5 = hub_runner(e5, "b1", "request_site_login", {"url": SITE4 + "/login.html"})
    pid5 = None
    freed_alive = None
    for _ in range(400):
        if pid5 is None and os.path.exists(os.path.join(st5, "pids")):
            pid5 = int(open(os.path.join(st5, "pids")).read().split()[0])
        if pid5 is not None and owners_free("b1"):
            freed_alive = alive(pid5)                # the moment the computer is free: is the first driver still there?
            break
        time.sleep(0.05)
    other = hub_runner(e5, "b1c", "browser_click", {"target_id": "bt-1", "tab_id": "tab-1", "ref": "p9:6", "session": "other"})
    time.sleep(max(0.0, t_start + 12 - time.time()))
    mcp_hub.stop_hub(e5)
    hlog = "".join(open(os.path.join(dp, f), errors="replace").read() for dp, _dn, fn in os.walk(os.path.join(td, "logs-b1")) for f in fn)
    check("R6-B: a driver call of a sign-in that doesn't answer in time: its connection's process tree is ended before the "
          "job lets the computer go (the driver is gone when it's free), another controller then acts, and the delayed "
          "post-fill click never runs", pid5 is not None and freed_alive is False and "in use by another controller" not in other
          and not os.path.exists(os.path.join(st5, "clicked")) and "ending driver connection" in hlog,
          (pid5, freed_alive, other[-160:], os.path.exists(os.path.join(st5, "clicked")), "ending driver connection" in hlog, out5[-120:]))

    # B (lease past its life): a client leaves while its sign-in waits for the user; past the lease's life the hub withdraws
    # the vault call, and lets the computer go only after the vault confirmed (the question is gone; approving fails).
    e6, st6 = hub_env("b2", {"url": SITE + "/login.html"}, relay=False, MCOMP_ACTIVE_S="6")
    n6 = len(calls())
    out6 = hub_runner(e6, "b2", "request_site_login", {"url": SITE + "/login.html"})
    rid6 = (re.findall(r'request_id\\?": \\?"([0-9a-f]{8})', out6) or ["00000000"])[0]
    held_at = time.time()
    freed_at = None
    for _ in range(600):
        if owners_free("b2"):
            freed_at = time.time()
            break
        time.sleep(0.05)
    cancelled = [x for x in vault_recs() if x.get("request_id") == rid6 and x.get("result") == "cancelled"]
    still = rid6 in pending_ids()
    ap = subprocess.run([BIN, "approve", rid6], env=venv, capture_output=True, text=True, timeout=30)
    time.sleep(1.0)
    filled6 = [c for c in calls()[n6:] if c.get("kind") == "fill:fill"]
    mcp_hub.stop_hub(e6)
    check("R6-B: a lease past its life (6 s here) withdraws the sign-in's vault call: the vault withdraws the question and "
          "confirms (recorded as cancelled) before the computer is let go; approving afterwards finds nothing, and nothing is typed",
          freed_at is not None and freed_at - held_at >= 4 and cancelled and not still and ap.returncode != 0 and not filled6,
          (out6[-200:], freed_at and freed_at - held_at, cancelled[-1:], still, ap.stdout + ap.stderr, filled6))

    # B (hub stop): the hub stopping withdraws a sign-in waiting for the user, and exits only after the vault confirmed.
    e7, st7 = hub_env("b3", {"url": SITE + "/login.html", "leak": leak, "stderr_leak": True}, relay=False)
    h7 = mcomp_sdk.Hub(env=e7, timeout=200, session="sl7")
    h7.connect()
    r7 = h7.call("request_site_login", {"url": SITE + "/login.html"}, timeout=200)
    rid7 = r7.structured.get("request_id") or "00000000"
    mcp_hub.stop_hub(e7)
    cancelled7 = [x for x in vault_recs() if x.get("request_id") == rid7 and x.get("result") == "cancelled"]
    still7 = rid7 in pending_ids()
    try:
        h7.close()
    except Exception:
        pass
    check("R6-B: stopping the hub while a sign-in waits for the user: the vault call is withdrawn and confirmed (recorded as "
          "cancelled) before the hub has exited; nothing is left to approve", r7.structured.get("status") == "pending"
          and cancelled7 and not still7 and not leaked_into("b3"), (r7.raw, cancelled7[-1:], still7))

    # B (driver crash): the driver dies at the post-fill click: the connection's process tree ends with it, the job ends.
    e8, st8 = hub_env("b4", {"url": SITE4 + "/login.html", "crash_on_click": True}, relay=False)
    h8 = mcomp_sdk.Hub(env=e8, timeout=200, session="sl8")
    h8.connect()
    r8 = outcome(h8, h8.call("request_site_login", {"url": SITE4 + "/login.html"}, timeout=200), SITE4 + "/login.html")
    pid8 = int(open(os.path.join(st8, "pids")).read().split()[0]) if os.path.exists(os.path.join(st8, "pids")) else None
    h8.close()
    mcp_hub.stop_hub(e8)
    check("R6-B: the driver dying mid-job (at the post-fill click): the job ends at once (filled, not submitted), with the "
          "driver's process gone", r8.structured.get("status") == "filled" and r8.structured.get("submitted") is False
          and pid8 is not None and not alive(pid8), (r8.raw, pid8))
    u.send("\x03")
    u.wait(15)
    leaked = [v for v in VALUES if any(v in o for o in ts.outputs)]
    check(f"no value in any of {len(ts.outputs)} tool results", not leaked, leaked)
    return env


if __name__ == "__main__":
    sys.exit(main())
