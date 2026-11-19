#!/usr/bin/env python3
"""One-time codes and sign-in links, and human-mode fills, against a running test VM, end to end:

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_vault_codes_vm.py

Two pages from tests/fixtures/codes are served in the VM: a "mail" page on http://localhost:8000 showing a code and
sign-in links, and a "site" page on http://127.0.0.1:8000 with a code field (it says how many characters it got and
whether they match). Then, through `metacomputer secrets` (a TEST build of the vault with its own folder in /tmp, bound
to the test VM; never the installed vault, its data or a Keychain item):
1. page mode, `code` from a pipe: needs approval with no rule (exit 7); with `rule add code --from stdin` it's filled
   into page-mode Chrome on the site, and the page sees the right code;
2. `relay` from the mail page into the site page (code), then `relay --kind link` (the site's own link opens in the
   site's tab; a link to another host is refused under the rule);
3. human mode in the VM's regular Firefox: `code --human` into the focused field (the page sees the code); `fill
   --human --field password` refused while a non-password field has the focus, filled once the password field has it;
   and a page whose title names another site (misleading.html) is refused for that site, from the address bar.

The VM's own secret-fill comes from its build. If it predates the relay's read and the link's open (a VM built before this
change), the test's ssh stand-in (MCOMP_VAULT_TEST_SSH, test builds only) runs this repository's secret-fill for just
those two modes, as the desktop user over the real ssh; everything else uses the VM's real accounts. A test VM only:
it starts a web server and leaves driver profiles there (vmcodes-site, vmcodes-mail).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402

LAB = ts.LAB
MC = os.path.join(LAB, "bin", "metacomputer")
check = ts.check
PASS = ts.PASS
PW = "Dummy-vm-codes-pass-61"
CODE = "482913"
SITE = "http://127.0.0.1:8000"
MAIL = "http://localhost:8000"
VALUES = [PASS, PW, CODE, "419275"]

SSH_STANDIN = r'''#!/usr/bin/python3 -I
# The test's ssh stand-in: everything goes to the real /usr/bin/ssh as asked, except a fill-account request for a relay's
# read or a link's open on a VM whose secret-fill predates them: that one runs this repository's secret-fill as the
# desktop user (ops key), over the same pinned connection.
import json, os, sys
OPS_KEY, NEW_FILL, MARK = {ops!r}, {fill!r}, {mark!r}
argv = sys.argv[1:]
data = sys.stdin.buffer.read()
try:
    mode = json.loads(data).get("mode")
except ValueError:
    mode = None
if mode in ("read", "open") and any(a == "mcfill@127.0.0.1" for a in argv) and os.path.exists(MARK):
    argv = ["IdentityFile=" + OPS_KEY if a.startswith("IdentityFile=") else ("cua@127.0.0.1" if a == "mcfill@127.0.0.1" else a) for a in argv]
    src = open(NEW_FILL).read()
    argv[-1] = "exec python3 -I -c " + "'" + src.replace("'", "'\\''") + "'" + " --desktop-uid 1000"
r, w = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(w)
    os.dup2(r, 0)
    os.execv("/usr/bin/ssh", ["/usr/bin/ssh"] + argv)
os.close(r)
os.write(w, data)
os.close(w)
_, st = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(st))
'''


def main():
    if not os.environ.get("MCOMP_CONFIG"):
        print("set MCOMP_CONFIG to a test VM's settings file (this test changes things in that VM)")
        return 2
    td = tempfile.mkdtemp(prefix="mcvcv-", dir="/tmp")
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
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import mcp_hub
    import mcomp_sdk
    env = mcomp_sdk.resolve_env(cfg)
    sshdir = env.get("MCOMP_SSH_DIR") or env.get("SSH_DIR")
    mark = os.path.join(td, "old-secret-fill")
    standin = os.path.join(td, "ssh")
    open(standin, "w").write(SSH_STANDIN.format(ops=os.path.join(sshdir, "ops"), mark=mark,
                                                fill=os.path.join(LAB, "provisioning/guest/rootfs/opt/metacomputer/bin/secret-fill")))
    os.chmod(standin, 0o755)
    venv = dict(os.environ, MCOMP_VAULT_DIR=vdir, MCOMP_VAULT_BIN=BIN, MCOMP_VAULT_ALLOW_TEST_BUILD="1", MCOMP_VAULT_TEST_LOCAL_AUTH="allow",
                MCOMP_VAULT_TEST_PROMPT_TIMEOUT="60", MCOMP_EVENTS_DIR=os.path.join(td, "events"), MCOMP_EVENTS_SPOOL=os.path.join(td, "spool"),
                MCOMP_VAULT_TEST_SSH=standin)
    for k in ("MCOMP_VAULT_TEST_LISTENERS", "MCOMP_DIRECT_BRIDGE", "MCOMP_HUB_PID"):
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

    outputs, cli_outputs = [], []

    def mc(*args, stdin=None, timeout=180):
        r = subprocess.run([MC, *args], env=venv, capture_output=True, text=True, timeout=timeout,
                           input=stdin if stdin is not None else "")
        outputs.append(r.stdout + r.stderr)
        cli_outputs.append(r.stdout + r.stderr)
        return r

    def js(text):
        try:
            return json.loads(text.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    st = mc("status")
    check("the test VM is running and ready", st.returncode == 0 and "READY" in st.stdout, st.stdout[-400:])
    old = mc("sh", "--override", "-c", "grep -c 'mcomp:read' /opt/metacomputer/bin/secret-fill || true")
    if old.stdout.strip() in ("", "0"):
        open(mark, "w").close()
        print("note: this VM's secret-fill predates the relay's read and the link's open; those two run this repository's "
              "secret-fill as the desktop user through the test's ssh stand-in")
    # `put` never replaces what's there: the pages of an earlier run go first.
    mc("sh", "--override", "-c", "rm -rf ~/metacomputer-runs/codes ~/metacomputer-runs/'codes ('*")
    r = mc("put", os.path.join(LAB, "tests", "fixtures", "codes"), "--to", "~/metacomputer-runs")
    r2 = subprocess.run([MC, "sh", "--override", "-"], input=(
        "pkill -f 'http.server 8000' 2>/dev/null; pkill -x firefox-bin 2>/dev/null; pkill -x firefox 2>/dev/null; sleep 2\n"
        "rm -rf ~/metacomputer-runs/codes.old; [ -d ~/metacomputer-runs/codes ] || exit 3\n"
        "rm -rf ~/.local/state/cua-driver/browser-profiles/vmcodes-site ~/.local/state/cua-driver/browser-profiles/vmcodes-mail\n"
        "cd ~/metacomputer-runs/codes && (setsid nohup python3 -m http.server 8000 --bind 127.0.0.1 >/tmp/codes-srv.log 2>&1 &)\n"
        "sleep 1; curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/mail.html\n"), env=venv, capture_output=True, text=True, timeout=120)
    check("the test pages are served in the VM (mail on localhost:8000, site on 127.0.0.1:8000)", r2.stdout.strip().endswith("200"),
          (r.stderr[-200:], r2.stdout[-200:], r2.stderr[-200:]))

    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    rc2, out2 = tty([BIN, "set", "demo", "--kind", "password", "--origin", SITE, "--username", "vm-user@example.com"],
                    [("Vault passphrase", PASS + "\n"), ("Password for demo", PW + "\n"), ("same password again", PW + "\n")])
    rc3, out3 = tty([BIN, "set", "bank", "--kind", "password", "--origin", "https://bank.example"],
                    [("Vault passphrase", PASS + "\n"), ("Password for bank", PW + "\n"), ("same password again", PW + "\n")])
    rc4, out4 = tty([MC, "secrets", "bind"], [("Type yes to pin these settings", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("a test vault (two logins), bound to the VM", rc == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0 and "Bound VM" in out4,
          (out[-200:], out2[-200:], out4[-300:]))
    u = ts.Pty([BIN, "unlock", "--for", "30m", *allow], venv)
    procs.append(u)
    u.expect("Vault passphrase: ")
    u.send(PASS + "\n")
    u.expect("unlocked until")
    time.sleep(0.5)

    env.update({k: v for k, v in venv.items() if k.startswith("MCOMP_VAULT") or k.startswith("MCOMP_EVENTS")})
    mcp_hub.stop_hub(env)
    h = mcomp_sdk.Hub(env=env, timeout=300, session="vmcodes")
    h.connect()

    def call(tool, args, timeout=300):
        res = h.call(tool, args, timeout=timeout)
        outputs.append(json.dumps(res.raw))
        return res

    def open_chrome(sess, profile, url):
        pid = None
        for _ in range(10):
            p = call("browser_prepare", {"session": sess, "allow_launch": True, "profile": {"mode": "isolated_named", "name": profile}})
            pid = p.structured.get("prepared_pid")
            if pid:
                break
            time.sleep(2)
        win = None
        for _ in range(40):
            ws = (call("list_windows", {"pid": pid, "session": sess}).structured.get("windows") or []) if pid else []
            if ws:
                win = ws[0]
                break
            time.sleep(0.25)
        b = call("get_browser_state", {"pid": pid, "window_id": (win or {}).get("window_id"), "session": sess}).structured
        tgt = {"target_id": b.get("target_id"), "tab_id": ((b.get("tabs") or [{}])[0]).get("tab_id"), "session": sess}
        call("browser_navigate", dict(tgt, url=url))
        time.sleep(1.5)
        return pid, tgt

    def page_text(tgt, want, tries=20):
        text = ""
        for _ in range(tries):
            s_ = h.call("get_browser_state", dict(tgt, snapshot_format="semantic_v2", query=want))
            text = s_.text + json.dumps(s_.structured)
            if want in text:
                break
            time.sleep(0.5)
        return text

    def submit(tgt):
        snap = call("get_browser_state", dict(tgt, snapshot_format="dom_refs_v1")).structured
        go = next((x.get("ref") for x in snap.get("refs") or [] if "id=go" in str(x.get("label") or "")), None)
        if go:
            call("browser_click", dict(tgt, ref=go, delivery_mode="foreground"))
        return go

    # --- 1. page mode: code from a pipe -----------------------------------------------------------------------
    spid, stgt = open_chrome("vmcodes-s", "vmcodes-site", SITE + "/site.html")
    check("page-mode Chrome on the site page (through the hub)", bool(spid) and stgt.get("target_id"))
    r = mc("secrets", "code", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin=CODE + "\n")
    rid = js(r.stdout).get("request_id")
    check("code from a pipe with no rule: exit 7, the request id and the one command, never the value",
          r.returncode == 7 and js(r.stdout).get("approve") == f"metacomputer secrets approve {rid}" and CODE not in r.stdout + r.stderr,
          (r.returncode, r.stdout[-300:], r.stderr[-200:]))
    r = mc("secrets", "deny", rid)
    r = mc("secrets", "rule", "add", "code", "--site", SITE, "--from", "stdin", "--expires", "1h")
    check("rule add code --site S --from stdin (the test build's Touch ID stand-in confirms it)", r.returncode == 0, r.stdout + r.stderr)
    r = mc("secrets", "code", "--site", SITE, "--into", SITE + "/#otp", "--json", stdin=CODE + "\n")
    check("code from a pipe under the rule: {status: filled}", r.returncode == 0 and js(r.stdout).get("status") == "filled"
          and CODE not in r.stdout + r.stderr, (r.stdout[-300:], r.stderr[-300:]))
    submit(stgt)
    t = page_text(stgt, "Signed in")
    check("the site page got the code (its own line: 6 characters, matches)", "6 characters (matches)" in t, t[-300:])

    # --- 2. relay: a code, then links, from the mail page -----------------------------------------------------------
    mpid, mtgt = open_chrome("vmcodes-m", "vmcodes-mail", MAIL + "/mail.html")
    check("a second page-mode Chrome on the mail page", bool(mpid))
    call("browser_navigate", dict(stgt, url=SITE + "/site.html"))
    time.sleep(1.5)
    r = mc("secrets", "relay", "--from", MAIL + "/#code", "--to", SITE + "/#otp", "--site", SITE, "--json")
    check("relay with no relay rule: exit 7 (needs approval), nothing filled", r.returncode == 7 and CODE not in r.stdout + r.stderr, r.stdout[-300:])
    r = mc("secrets", "rule", "add", "relay", "--from-host", "localhost", "--to", SITE, "--expires", "1h")
    check("rule add relay --from-host localhost --to SITE", r.returncode == 0, r.stdout + r.stderr)
    r = mc("secrets", "relay", "--from", MAIL + "/#code", "--to", SITE + "/#otp", "--site", SITE)
    check("relay under the rule: Claude learns only 'relayed: filled'", r.returncode == 0 and r.stdout.strip().startswith("relayed: filled")
          and CODE not in r.stdout + r.stderr, (r.stdout[-300:], r.stderr[-300:]))
    submit(stgt)
    t = page_text(stgt, "Signed in")
    check("the site page got the code read from the mail page", "6 characters (matches)" in t, t[-300:])
    r = mc("secrets", "relay", "--from", MAIL + "/#two", "--to", SITE + "/#otp", "--site", SITE, "--json")
    check("relay from an element with two codes: refused with the reason", r.returncode == 2 and "look like a code" in r.stdout, r.stdout[-300:])
    r = mc("secrets", "relay", "--kind", "link", "--from", MAIL + "/#signin-link", "--to", SITE, "--site", SITE, "--json")
    check("relay --kind link: the site's own link is opened in the site's tab", r.returncode == 0 and js(r.stdout).get("status") == "filled",
          (r.stdout[-300:], r.stderr[-300:]))
    t = page_text(stgt, "Signed in by link")
    check("...the site page says it was opened by the link", "Signed in by link" in t, t[-300:])
    r = mc("secrets", "relay", "--kind", "link", "--from", MAIL + "/#tracked-link", "--to", SITE, "--site", SITE, "--json")
    check("relay --kind link to another host (a tracking redirect): refused under the rule, nothing opened",
          r.returncode == 2 and "localhost" in r.stdout, r.stdout[-300:])
    h.call("end_session", {"session": "vmcodes-s"})
    h.call("end_session", {"session": "vmcodes-m"})

    # --- 3. human mode in Firefox ----------------------------------------------------------------------------------
    la = call("launch_app", {"launch_path": "/usr/bin/firefox", "additional_arguments": ["--new-window", SITE + "/site.html"]})
    fpid = la.structured.get("pid")
    time.sleep(6)
    wins = [w for w in (call("list_windows", {"pid": fpid}).structured.get("windows") or []) if "Example" in str(w.get("title"))]
    fwin = wins[0]["window_id"] if wins else None
    check("Firefox (human mode) on the site page", bool(fpid) and fwin, (la.text[-200:], wins))

    def focus(label, role="entry"):
        s_ = call("get_window_state", {"pid": fpid, "window_id": fwin, "include_screenshot": False})
        snap = re.search(r"snapshot_id=(s[0-9a-f]+)", s_.text)
        rows = re.findall(r"\[(\d+)\] " + re.escape(role) + r' "' + re.escape(label) + '"', s_.text)
        if snap and rows:
            call("click", {"pid": fpid, "window_id": fwin, "element_token": f"{snap.group(1)}:{rows[-1]}", "delivery_mode": "foreground"})
            time.sleep(0.5)
            return True
        return False

    def read_window(want):
        text = ""
        for _ in range(10):
            text = call("get_window_state", {"pid": fpid, "window_id": fwin, "include_screenshot": False, "query": want}).text
            if want in text:
                break
            time.sleep(0.5)
        return text

    focus("Code")
    r = mc("secrets", "code", "--site", SITE, "--human", "--window-id", str(fwin), "--json", stdin=CODE + "\n")
    check("code --human: filled into Firefox's focused field (the address bar names the site)",
          r.returncode == 0 and js(r.stdout).get("status") == "filled" and CODE not in r.stdout + r.stderr, (r.stdout[-300:], r.stderr[-300:]))
    focus("Continue", role="push button")
    t = read_window("Signed in")
    check("the page in Firefox got the code (6 characters, matches)", "6 characters (matches)" in t, t[-300:])
    focus("Note")
    r = mc("secrets", "fill", "demo", "--field", "password", "--human", "--window-id", str(fwin))
    check("fill --human --field password with a non-password field focused: refused, nothing typed", r.returncode == 2
          and "kind of field" in js(r.stdout).get("reason", ""), r.stdout[-300:])
    focus("Password", role="password text")
    r = mc("secrets", "fill", "demo", "--field", "password", "--human", "--window-id", str(fwin))
    check("fill --human --field password with the password field focused: filled (chars counted by the vault)",
          r.returncode == 0 and js(r.stdout).get("chars") == len(PW) and js(r.stdout).get("mode") == "human" and PW not in r.stdout + r.stderr,
          (r.stdout[-300:], r.stderr[-200:]))
    call("hotkey", {"pid": fpid, "window_id": fwin, "keys": ["ctrl", "l"], "delivery_mode": "foreground"})
    call("type_text", {"pid": fpid, "window_id": fwin, "text": SITE + "/misleading.html\n", "delivery_mode": "foreground"})
    time.sleep(2.5)
    focus("Password", role="password text")
    r = mc("secrets", "fill", "bank", "--field", "password", "--human", "--window-id", str(fwin))
    check("a page whose title names https://bank.example (misleading.html): the bank login is refused, the address bar names 127.0.0.1:8000",
          r.returncode == 2 and PW not in r.stdout + r.stderr, r.stdout[-300:])
    mc("sh", "--override", "-c", "pkill -x firefox-bin; pkill -x firefox; true")

    h.close()
    term = u.text()
    u.send("\x03")
    u.wait(15)
    mc("sh", "--override", "-c", "pkill -f 'http.server 8000'; true")
    # A code is ordinary text on a page (the mail page shows it; Firefox's tree shows a filled code field): the commands'
    # output and the unlock terminal must never hold one, and nothing may hold the password.
    leaked = [v for v in VALUES if any(v in o for o in cli_outputs) or v in term] + [PW] * any(PW in o for o in outputs)
    check(f"no value in any of {len(cli_outputs)} command outputs or the unlock terminal, and the password nowhere in "
          f"{len(outputs)} tool results", not leaked, leaked)
    return env


if __name__ == "__main__":
    sys.exit(main())
