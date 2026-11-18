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
    if name == "browser_prepare":
        ok(mid, {"status": "ok", "prepared": True, "prepared_pid": 4242})
    elif name == "list_windows":
        ok(mid, {"windows": [{"pid": 4242, "window_id": 7, "app_name": "Google-chrome", "title": "Chrome"}]})
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
    check("N4: a tab given without the Chrome it's in (pid, or a named profile) is refused, nothing typed",
          r0.is_error and r0.structured.get("error") == "invalid_argument" and "pid" in r0.structured.get("message", "")
          and not [c for c in calls()[n:] if c.get("kind", "").startswith("fill:")], r0.raw)
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
    u.send("\x03")
    u.wait(15)
    leaked = [v for v in VALUES if any(v in o for o in ts.outputs)]
    check(f"no value in any of {len(ts.outputs)} tool results", not leaked, leaked)
    return env


if __name__ == "__main__":
    sys.exit(main())
