#!/usr/bin/env python3
"""A purchase against a running test VM, end to end, the way an agent pays (skills/metacomputer/SECRETS.md, "Paying"):

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_payments_vm.py

tests/fixtures/site/checkout.html (a test checkout page: its total, card fields and a "Place order" button that shows only
the lengths of what was typed and whether the number passes the card check) is served inside the VM and opened in
page-mode Chrome through the hub. Then `metacomputer secrets pay` reads the page's total, asks (a TEST build's stand-in for
the Touch ID dialog approves through `approve ID`) and fills a test card (4242 4242 4242 4242) through the vault's fill
account; the order is placed through the driver and the page reports what it got. A second purchase whose tab moves to
another site before its fill is refused with nothing typed. A third goes through the VM's regular Firefox (human mode):
its total from the accessibility tree, the parts typed into the focused fields as keys. Every output is checked for the
card's parts.

It uses a TEST build of the vault (scripts/build-vault.sh --test) with its own vault folder in /tmp, binds it to the test
VM from MCOMP_CONFIG, and never touches the installed vault or its data. A test VM only: it starts a web server in the VM
and leaves a driver profile there (vmtest-pay). Test card numbers only; nothing is bought.
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
import test_vault_vm as tvv  # noqa: E402
import test_vault_payments as tvp  # noqa: E402

LAB = ts.LAB
MC = os.path.join(LAB, "bin", "metacomputer")
check = ts.check
PASS = ts.PASS
NUMBER = "4242424242424242"           # a test card number
HOLDER = "Vm Testbuyer Kq"
ZIP = "94107"
SITE = "http://localhost:8000"
VALUES = [PASS, NUMBER, "4242 4242 4242 4242", HOLDER]


def main():
    if not os.environ.get("MCOMP_CONFIG"):
        print("set MCOMP_CONFIG to a test VM's settings file (this test changes things in that VM)")
        return 2
    td = tempfile.mkdtemp(prefix="mcpv-", dir="/tmp")
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
    venv = dict(os.environ, MCOMP_VAULT_DIR=vdir, MCOMP_VAULT_BIN=BIN, MCOMP_VAULT_ALLOW_TEST_BUILD="1", MCOMP_VAULT_TEST_LOCAL_AUTH="allow",
                MCOMP_VAULT_TEST_PROMPT_TIMEOUT="60", MCOMP_EVENTS_DIR=os.path.join(td, "events"), MCOMP_EVENTS_SPOOL=os.path.join(td, "spool"))
    for k in ("MCOMP_VAULT_TEST_SSH", "MCOMP_VAULT_TEST_LISTENERS", "MCOMP_DIRECT_BRIDGE", "MCOMP_HUB_PID", "MCOMP_VAULT_TEST_FETCH_LOOPBACK"):
        venv.pop(k, None)
    outputs = []

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
        outputs.append(p.text())
        return rc, p.text()

    def mc(*args, timeout=180, stdin=None):
        r = subprocess.run([MC, *args], env=venv, capture_output=True, text=True, timeout=timeout, input=stdin or "")
        outputs.append(r.stdout + r.stderr)
        return r

    st = mc("status")
    check("the test VM is running and ready", st.returncode == 0 and "READY" in st.stdout, st.stdout[-400:])
    r = mc("put", os.path.join(LAB, "tests", "fixtures", "site"), "--to", "~/metacomputer-runs")
    srv = os.path.join(td, "echo_server.py")
    open(srv, "w").write(tvv.ECHO_SERVER)
    r2 = mc("sh", "--override", "-", stdin=(
        "pkill -f 'echo_server.py|http.server 8000' 2>/dev/null; sleep 0.5\n"
        "rm -rf ~/.local/state/cua-driver/browser-profiles/vmtest-pay\n"
        f"cat > /tmp/echo_server.py <<'EOF'\n{tvv.ECHO_SERVER}\nEOF\n"
        "(setsid nohup python3 /tmp/echo_server.py ~/metacomputer-runs/site >/tmp/echo.log 2>&1 &)\n"
        "sleep 1; curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/checkout.html\n"), timeout=120)
    check("the test checkout page is served in the VM", r.returncode == 0 and r2.stdout.strip().endswith("200"), (r.stderr[-200:], r2.stdout[-200:]))

    # The vault: a test build, its own folder, a test card, bound to this VM.
    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    rc2, out2 = tty([BIN, "set", "testvisa", "--kind", "card", "--label", "Test Visa"],
                    [("Vault passphrase", PASS + "\n"), ("Card number for testvisa", NUMBER + "\n"), ("Expiry (MM/YY)", "11/30\n"),
                     ("Security code (CVC)", "123\n"), ("Name on the card", HOLDER + "\n"), ("Billing ZIP", ZIP + "\n")])
    rc3, out3 = tty([MC, "secrets", "bind"], [("Type yes to pin these settings", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("a test vault with a test card (every purchase asks), bound to the VM (its real gateway and pinned host key)",
          rc == 0 and rc2 == 0 and rc3 == 0 and "Bound VM" in out3 and "every purchase asks you first" in out2, (out[-200:], out2[-300:], out3[-300:]))
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
    mcp_hub.stop_hub(env)
    h = mcomp_sdk.Hub(env=env, timeout=300, session="vmpay")
    h.connect()

    def call(tool, args, timeout=300):
        r = h.call(tool, args, timeout=timeout)
        outputs.append(json.dumps(r.raw))
        return r

    def secrets(*args):
        return subprocess.Popen([MC, "secrets", *args], env=venv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)

    def answer(p, timeout=240):
        o, e = p.communicate(timeout=timeout)
        outputs.append(o + e)
        try:
            return p.returncode, json.loads(o.strip().splitlines()[-1]), o + e
        except (ValueError, IndexError):
            return p.returncode, {}, o + e

    def approve_next():
        u.expect("Request ", timeout=120)
        rid = (re.findall(r"Request ([0-9a-f]{8}):", u.text()) or [None])[-1]
        ra = subprocess.run([BIN, "approve", rid or "00000000"], env=venv, capture_output=True, text=True, timeout=120)
        outputs.append(ra.stdout + ra.stderr)
        return rid, ra

    # Page-mode Chrome on the test checkout, through the hub.
    pid, prep = None, ""
    for _ in range(10):
        pr = call("browser_prepare", {"session": "vmpay", "allow_launch": True, "profile": {"mode": "isolated_named", "name": "vmtest-pay"}})
        pid, prep = pr.structured.get("prepared_pid"), pr.text
        if pid:
            break
        time.sleep(2)
    win = None
    for _ in range(40):
        ws = (call("list_windows", {"pid": pid, "session": "vmpay"}).structured.get("windows") or []) if pid else []
        if ws:
            win = ws[0]
            break
        time.sleep(0.25)
    b = call("get_browser_state", {"pid": pid, "window_id": (win or {}).get("window_id"), "session": "vmpay"}).structured
    tgt = {"target_id": b.get("target_id"), "tab_id": ((b.get("tabs") or [{}])[0]).get("tab_id"), "session": "vmpay"}
    call("browser_navigate", dict(tgt, url=SITE + "/checkout.html"))
    check("page-mode Chrome opens on the test checkout (through the hub)", bool(pid) and tgt.get("target_id"), prep[-400:])
    time.sleep(1.5)

    # --- 1. a purchase: the total, the approval, the five parts, the order placed ------------------------------------
    p = secrets("pay", "testvisa", "--origin", SITE, "--total-selector", "#order-total", "--browser-pid", str(pid),
                "--fill", "number=#cc-number", "--fill", "exp=#cc-exp", "--fill", "cvc=#cc-csc", "--fill", "name=#cc-name", "--fill", "zip=#postal")
    rid, ra = approve_next()
    rc, res, text = answer(p)
    term = u.text()
    check("`secrets pay` reads the page's total in the VM's Chrome and asks: the question names the site, the total (the page's "
          "claim) and the card's label", "Pay USD 12.34 with Test Visa (card testvisa) on VM" in term and f"Card parts go to: {SITE}" in term
          and "the page's own claim" in term
          and "not checked: a page on the VM's own loopback" in term, term[-1200:])
    check("after `approve ID` (the test build's stand-in for Touch ID) the five parts are filled through the fill account; the "
          "output has the purchase id and the parts, no value", rid is not None and ra.returncode == 0 and rc == 0 and res.get("ok") is True
          and res.get("filled") == ["number", "exp", "cvc", "name", "zip"] and res.get("total") == "USD 12.34"
          and not any(v in text for v in VALUES), (ra.stderr[-200:], text[-500:]))
    snap = call("get_browser_state", dict(tgt, snapshot_format="dom_refs_v1")).structured
    place = next((r.get("ref") for r in snap.get("refs") or [] if "id=place" in str(r.get("label") or "")), None)
    if place:
        call("browser_click", dict(tgt, ref=place, delivery_mode="foreground"))
    seen = ""
    for _ in range(20):
        s_ = h.call("get_browser_state", dict(tgt, snapshot_format="semantic_v2", query="Order placed"))
        seen = s_.text + json.dumps(s_.structured)
        if "Order placed" in seen:
            break
        time.sleep(0.5)
    m = re.search(r"number (\d+) digits, card check (\w+), expiry ([\w/]+), security code (\d+) digits, name (\d+) characters, ZIP (\d+) characters", seen)
    check("the order is placed through the driver; the test page itself got a 16-digit number that passes the card check, an MM/YY "
          "expiry, a 3-digit code, the name and the ZIP (lengths only)", bool(place) and m is not None and m.groups()
          == ("16", "passes", "MM/YY", "3", str(len(HOLDER)), str(len(ZIP))), seen[-500:])
    r = subprocess.run([BIN, "pay", "--purchase", res.get("purchase_id") or "0" * 32, "--done"], env=venv, capture_output=True, text=True, timeout=60)
    outputs.append(r.stdout + r.stderr)
    check("`pay --done` ends the purchase", r.returncode == 0 and '"outcome":"done"' in r.stdout.replace(" ", ""), r.stdout[-300:])

    # --- 2. a second purchase whose tab moves to another site before the fill: refused, nothing typed ------------------
    call("browser_navigate", dict(tgt, url=SITE + "/checkout.html"))
    time.sleep(1.5)
    p = secrets("pay", "testvisa", "--origin", SITE, "--total-selector", "#order-total", "--browser-pid", str(pid))
    rid2, ra2 = approve_next()
    rc, res2, text = answer(p)
    call("browser_navigate", dict(tgt, url="http://127.0.0.1:8000/checkout.html"))       # another origin (127.0.0.1, not localhost)
    time.sleep(1.5)
    pf = secrets("pay", "--purchase", res2.get("purchase_id") or "0" * 32, "--field", "number", "--selector", "#cc-number")
    rcf, resf, textf = answer(pf)
    val = h.call("get_browser_state", dict(tgt, snapshot_format="semantic_v2", query="Card number"))
    check("a purchase approved for localhost:8000 whose tab moved to 127.0.0.1:8000 is refused at its fill, and nothing is typed there",
          rc == 0 and rcf == 2 and resf.get("ok") is False and NUMBER not in json.dumps(val.raw), (textf[-400:]))
    call("browser_navigate", dict(tgt, url=SITE + "/checkout.html"))
    time.sleep(1.5)
    pf = secrets("pay", "--purchase", res2.get("purchase_id") or "0" * 32, "--field", "number", "--selector", "#cc-number")
    rcf2, resf2, textf2 = answer(pf)
    check("...back on the approved site, the same purchase fills the number (one part, by its selector)", rcf2 == 0
          and resf2.get("filled") == ["number"], textf2[-400:])
    subprocess.run([BIN, "pay", "--purchase", res2.get("purchase_id") or "0" * 32, "--done"], env=venv, capture_output=True, text=True, timeout=60)

    # --- 3. human mode: the VM's regular Firefox, its total from the accessibility tree, the focused field ------------
    def firefox_windows():
        return [w for w in (call("list_windows", {}).structured.get("windows") or []) if w.get("app_name") == "firefox"]

    la, wins = None, []
    for _ in range(2):
        la = call("launch_app", {"launch_path": "/usr/bin/firefox", "additional_arguments": ["--new-window", SITE + "/checkout.html"]})
        time.sleep(6)
        prompt = [w for w in firefox_windows() if "Troubleshoot Mode" in str(w.get("title"))]
        if prompt:
            call("press_key", {"pid": prompt[0]["pid"], "window_id": prompt[0]["window_id"], "key": "Escape", "delivery_mode": "foreground"})
            time.sleep(3)
            continue
        wins = [w for w in firefox_windows() if "Test checkout" in str(w.get("title"))]
        break
    fwin = wins[0]["window_id"] if wins else None
    fpid = wins[0]["pid"] if wins else None
    check("Firefox (human mode) on the test checkout", bool(fpid) and fwin, (la.text[-200:] if la else "", wins))

    def focus(label, role="entry"):
        s_ = call("get_window_state", {"pid": fpid, "window_id": fwin, "include_screenshot": False})
        snap = re.search(r"snapshot_id=(s[0-9a-f]+)", s_.text)
        rows = re.findall(r"\[(\d+)\] " + re.escape(role) + r' "' + re.escape(label) + '"', s_.text)
        if snap and rows:
            call("click", {"pid": fpid, "window_id": fwin, "element_token": f"{snap.group(1)}:{rows[-1]}", "delivery_mode": "foreground"})
            time.sleep(0.5)
            return True
        return False

    p = secrets("pay", "testvisa", "--origin", SITE, "--human", "--window-id", str(fwin))
    rid3, ra3 = approve_next()
    rc, res3, text = answer(p)
    check("human mode: `pay --human` reads the total from Firefox's accessibility tree after \"Total\" (the site from the address "
          "bar) and asks; approved with `approve ID`", rc == 0 and res3.get("total") == "USD 12.34" and res3.get("purchase_id")
          and ra3.returncode == 0, text[-400:])
    ok_parts = []
    for part, label in (("number", "Card number"), ("exp", "Expiry")):
        focused = focus(label)
        pf = secrets("pay", "--purchase", res3.get("purchase_id") or "0" * 32, "--field", part, "--human", "--window-id", str(fwin))
        rcp, resp, textp = answer(pf)
        ok_parts.append(focused and rcp == 0 and resp.get("ok") is True or (part, focused, rcp, resp.get("reason")))
    check("human mode: the number and the expiry go into Firefox's focused fields as keys through the driver", ok_parts == [True, True], ok_parts + [u.text()[-900:]])
    back = call("get_window_state", {"pid": fpid, "window_id": fwin, "include_screenshot": False, "query": "Card number"})
    check("a window read after the fill shows the field's number masked by the hub, never the number itself",
          "[card number hidden by metacomputer]" in json.dumps(back.raw) and NUMBER not in json.dumps(back.raw), back.text[-400:])
    focus("Place order", role="push button")
    seen = ""
    for _ in range(10):
        seen = call("get_window_state", {"pid": fpid, "window_id": fwin, "include_screenshot": False, "query": "Order placed"}).text
        if "Order placed" in seen:
            break
        time.sleep(0.5)
    check("...and the test page in Firefox got a 16-digit number that passes the card check and an MM/YY expiry",
          "number 16 digits, card check passes, expiry MM/YY" in seen, seen[-400:])
    subprocess.run([BIN, "pay", "--purchase", res3.get("purchase_id") or "0" * 32, "--done"], env=venv, capture_output=True, text=True, timeout=60)
    mc("sh", "--override", "-c", "pkill -x firefox-bin; pkill -x firefox; true")
    h.call("end_session", {"session": "vmpay"})
    h.close()

    # --- the audit trail -----------------------------------------------------------------------------------------------
    recs = [json.loads(l) for l in open(os.path.join(vdir, "audit.jsonl"))]
    pays = [x for x in recs if str(x.get("event", "")).startswith("payment.")]
    check("the audit log has the purchases: request, authorize (local-auth), each part filled, the refusal, the ends; with the "
          "card's label and the site, never a value", any(x.get("event") == "payment.authorize" and x.get("confirmed") == "local-auth" for x in pays)
          and sum(1 for x in pays if x.get("event") == "payment.fill" and x.get("label") == "Test Visa" and x.get("origin") == SITE) == 8
          and any(x.get("event") == "payment.denied" and x.get("op") == "pay_fill" for x in pays)
          and sum(1 for x in pays if x.get("event") == "payment.end") == 3 and not any(v in json.dumps(recs) for v in VALUES),
          [x.get("event") for x in pays])
    u.send("\x03")
    u.wait(15)
    outputs.append(u.text())
    mc("sh", "--override", "-c", "pkill -f echo_server.py; true", timeout=60)
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no card part or passphrase in any of {len(outputs)} tool results, command outputs or the unlock terminal", not leaked, leaked)
    return env


if __name__ == "__main__":
    rc = tvp.in_agent_session()        # a purchase is resumed only by the session that started it
    sys.exit(main() if rc is None else rc)
