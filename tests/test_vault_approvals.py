#!/usr/bin/env python3
"""Offline checks for the vault's approvals: request ids (`requests`, `approve ID`, `deny ID`), Touch ID /
login-password approval, passphrase proofs, unattended rules, identity providers, one-approval sign-in
(`login`) and the site-login event names in the audit log and sink payloads.

No VM needed; the same approach as tests/test_secrets.py and tests/test_vault_ops.py (whose stub, pseudo-
terminal and helpers this reuses): a TEST build of mcomp-vault, an ssh stub standing in for the VM, every
passphrase and value typed into a pseudo-terminal, every value a dummy. A test build never shows macOS's
Touch ID dialog: MCOMP_VAULT_TEST_LOCAL_AUTH=allow|deny|wait stands in for the person at it, and without it
there is none. A release build doesn't contain that switch: checked here too, by behaviour (it goes to the
real framework, told never to show a dialog) as well as by its strings.

The point of most checks: no approval can be produced by a program, however it talks to the vault. A
terminal of its own isn't enough (a pseudo-terminal is used here, as `script` or `expect` would); only the
name typed at the unlock terminal, the system dialog, or the passphrase approves.

Run: python3 tests/test_vault_approvals.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build [MCOMP_VAULT_REL_BIN=/path/to/release-build] python3 tests/test_vault_approvals.py
"""
import glob
import hashlib
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
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402
import test_vault_ops as tvo  # noqa: E402

LAB = ts.LAB
check = ts.check
PASS = ts.PASS
PW_VALUE = ts.PW_VALUE
USERNAME = ts.USERNAME
SITE, SITE2 = ts.SITE, ts.SITE2
IDP = "https://accounts.google.com"
IDP2 = "https://login.microsoftonline.com"
API_VALUE = "dummy-value-for-the-approvals-test"
VALUES = [PASS, PW_VALUE, API_VALUE]
REQ_RE = re.compile(r"Request ([0-9a-f]{8}):")
outputs = []


def wait_for(fn, timeout=20, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def main():
    td = tempfile.mkdtemp(prefix="mcva-", dir="/tmp")
    procs = []
    try:
        run(td, procs)
    finally:
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


class FakeVault:
    """A stand-in for the unlock process on another socket: answers `requests` with what it's told and keeps
    what an `approve` sends (what a program that took over the socket would see)."""

    def __init__(self, path, listing):
        self.listing, self.got = listing, []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(path)
        self.sock.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            data = b""
            while not data.endswith(b"\n"):
                d = c.recv(65536)
                if not d:
                    break
                data += d
            req = json.loads(data or b"{}")
            self.got.append(req)
            ans = {"ok": True, "status": "ok", "requests": self.listing} if req.get("op") == "requests" else \
                {"ok": False, "status": "denied", "reason": "a stand-in"}
            c.sendall((json.dumps(ans) + "\n").encode())
            c.close()

    def close(self):
        self.sock.close()


def run(td, procs):
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    REL = os.environ.get("MCOMP_VAULT_REL_BIN")
    if not REL:
        REL = os.path.join(td, "bin", "mcomp-vault")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--out", REL], capture_output=True, text=True)
        check("build: release binary", r.returncode == 0, r.stderr[-800:])
    blob = open(REL, "rb").read()
    check("the release build has no Touch ID stand-in (MCOMP_VAULT_TEST_LOCAL_AUTH) and no test switches",
          b"MCOMP_VAULT_TEST_LOCAL_AUTH" not in blob and b"MCOMP_VAULT_TEST_" not in blob)
    check("the test build has the Touch ID stand-in", b"MCOMP_VAULT_TEST_LOCAL_AUTH" in open(BIN, "rb").read())
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []

    # --- a fake VM endpoint: keys, a listener, the ssh stub ------------------------------------------------
    home, vdir, sshdir = os.path.join(td, "home"), os.path.join(td, "v"), os.path.join(td, "ssh")
    os.makedirs(home)
    os.makedirs(sshdir, mode=0o700)
    for k in ("ops", "fill", "proxy", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    hostkey = " ".join(open(os.path.join(sshdir, "hostkey.pub")).read().split()[:2])
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True).stdout
    my_name = next((l[1:] for l in lsof.splitlines() if l.startswith("c")), "")
    my_exe = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    stubdir = os.path.join(td, "stub")
    os.makedirs(stubdir)
    stub_log = os.path.join(stubdir, "calls.jsonl")
    stub = os.path.join(stubdir, "ssh")
    open(stub, "w").write(ts.STUB.format(python=sys.executable, log=stub_log))
    os.chmod(stub, 0o755)
    open(os.path.join(stubdir, "archive.tgz"), "wb").write(
        ts.tarball([("prof1", "dir", None), ("prof1/Default", "dir", None), ("prof1/Default/Cookies", "file", b"dummy-jar-91" * 40)]))
    fake = os.path.join(td, "fake", "claude")
    os.makedirs(os.path.dirname(fake))
    open(fake + ".c", "w").write(tvo.FAKE_CLAUDE_C)
    r = subprocess.run(["cc", "-o", fake, fake + ".c"], capture_output=True, text=True)
    check("a stand-in process named claude builds", r.returncode == 0, r.stderr)
    proj1, proj2 = os.path.realpath(os.path.join(td, "proj1")), os.path.realpath(os.path.join(td, "proj2"))
    os.makedirs(os.path.join(proj1, "sub"))
    os.makedirs(proj2)

    def flag(name, on=True):
        p = os.path.join(stubdir, name)
        if on:
            open(p, "w").close()
        elif os.path.exists(p):
            os.remove(p)

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
           "MCOMP_VAULT_TEST_SSH": stub}
    sock = os.path.join(vdir, "vault.sock")
    alog = os.path.join(vdir, "audit.jsonl")

    def client(*args, extra=None, timeout=120, binary=None):
        r = subprocess.run([binary or BIN, *args], capture_output=True, text=True, env=dict(env, **(extra or {})),
                           stdin=subprocess.DEVNULL, timeout=timeout)
        outputs.append(r.stdout + r.stderr)
        return r

    def background(*args, extra=None):
        return subprocess.Popen([BIN, *args], env=dict(env, **(extra or {})), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)

    def finish(proc, timeout=120):
        out, err = proc.communicate(timeout=timeout)
        outputs.append(out + err)
        return proc.returncode, out, err

    def js(text):
        try:
            return json.loads(text.strip().splitlines()[-1])
        except (ValueError, IndexError, AttributeError):
            return {}

    def tty(argv, answers, extra=None, timeout=90):
        p = ts.Pty(argv, dict(env, **(extra or {})))
        try:
            for want, say in answers:
                p.expect(want)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        finally:
            p.close()
        outputs.append(p.text())
        return rc, p.text()

    def raw(req):
        """One request on the vault's socket, as any program of the user's can send it."""
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(60)
        s.connect(sock)
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            d = s.recv(65536)
            if not d:
                break
            data += d
        s.close()
        return json.loads(data or b"{}")

    counter = [0]

    def detached_start(*args):
        """The client with no Claude Code process among its ancestors (its parent shell is orphaned): this test
        itself may run under Claude Code."""
        counter[0] += 1
        out = os.path.join(td, f"det{counter[0]}.out")
        script = f"({shlex.join([BIN, *args])} >{shlex.quote(out)} 2>&1; echo $? >{shlex.quote(out + '.rc')}) &"
        subprocess.run(["/bin/sh", "-c", script], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return out

    def detached_wait(out, timeout=60):
        wait_for(lambda: os.path.exists(out + ".rc") and open(out + ".rc").read().strip(), timeout)
        text = open(out).read() if os.path.exists(out) else ""
        outputs.append(text)
        rc = open(out + ".rc").read().strip() if os.path.exists(out + ".rc") else "999"
        return int(rc or 999), text

    def as_claude(project, *commands):
        counter[0] += 1
        outs = [os.path.join(td, f"cl{counter[0]}-{i}.out") for i in range(len(commands))]
        script = "; ".join(f"{shlex.join([BIN, *c])} >{shlex.quote(o)} 2>&1; echo $? >{shlex.quote(o + '.rc')}"
                           for c, o in zip(commands, outs))
        p = subprocess.Popen([fake, project, "/bin/sh", "-c", script], env=env, stdin=subprocess.DEVNULL)
        procs.append(p)
        return p, outs

    def claude_results(p, outs, timeout=60):
        p.wait(timeout)
        res = []
        for o in outs:
            text = open(o).read() if os.path.exists(o) else ""
            outputs.append(text)
            rc = open(o + ".rc").read().strip() if os.path.exists(o + ".rc") else "999"
            res.append((int(rc or 999), text))
        return res

    def audit_recs():
        return [json.loads(l) for l in open(alog)] if os.path.exists(alog) else []

    def start_unlock(args=(), extra=None):
        p = ts.Pty([BIN, "unlock", *args], dict(env, **(extra or {})))
        procs.append(p)
        p.expect("Vault passphrase: ")
        p.send(PASS + "\n")
        p.expect("unlocked until")
        wait_for(lambda: os.path.exists(sock), 5, 0.05)
        return p

    def lock(p):
        p.send("\x03")
        return p.wait(15)

    def next_request(u, timeout=20):
        """The id of the request the unlock terminal shows next."""
        u.expect("Request ", timeout)
        m = wait_for(lambda: REQ_RE.findall(u.text()), 5)
        return m[-1] if m else None

    def tty_soft(argv, answers):
        """tty(), but a command that never asks (it refused at once) gives (999, its output) instead of stopping the run."""
        try:
            return tty(argv, answers)
        except AssertionError as e:
            return 999, str(e)

    def soft(u, text, timeout=2):
        """Wait for text on the unlock terminal, without stopping the run when it doesn't come."""
        try:
            u.expect(text, timeout)
            return True
        except AssertionError:
            return False

    def pending():
        return client("requests", "--json")

    # --- the vault: secrets, a VM ------------------------------------------------------------------------
    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    check("init", rc == 0, out)

    def setpw(name, origins, approval="each", username=USERNAME):
        args = [BIN, "set", name, "--kind", "password", *sum((["--origin", o] for o in origins), []), "--approval", approval]
        if username:
            args += ["--username", username]
        return tty(args, [("Vault passphrase", PASS + "\n"), (f"Password for {name}", PW_VALUE + "\n"),
                          ("same password again", PW_VALUE + "\n")])[0]

    check("set the secrets: each, session, an identity provider's, one with an identity provider among its sites",
          setpw("web-each", [SITE, SITE2]) == 0 and setpw("web-sess", [SITE], "session") == 0
          and setpw("idp-login", [IDP], "session") == 0 and setpw("mixed", [SITE, IDP2], "session") == 0
          and setpw("other", [SITE2]) == 0)
    rc, out = tty([BIN, "set", "api-idp", "--kind", "api", "--api-host", "login.microsoftonline.com", "--approval", "session"],
                  [("Vault passphrase", PASS + "\n"), ("API key for api-idp", API_VALUE + "\n")])
    check("set an API secret whose host is an identity provider's", rc == 0, out)
    rc, out = tty([BIN, "bind", "avm", "--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill",
                   "--fill-user", "mcfill", "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--proxy-key", sshdir + "/proxy",
                   "--proxy-user", "mcproxy", "--proxy-port", "18765", "--gateway", my_exe],
                  [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("bind the stand-in VM", rc == 0, out)

    # --- locked ------------------------------------------------------------------------------------------
    r = pending()
    check("requests while locked: exit 3, an empty list", r.returncode == 3 and js(r.stdout).get("reason") == "locked")
    r = client("approve", "0123abcd")
    check("approve while locked: refused (exit 3)", r.returncode == 3, r.stderr)
    r = client("rule", "list")
    check("rule list while locked: exit 3 (the rules are inside the vault)", r.returncode == 3)
    r = client("rule", "add", "--secret", "web-each", "--project", proj1, "--for", "1h")
    check("rule add while locked and without a terminal: refused", r.returncode != 0 and "own terminal" in r.stderr, r.stderr)

    T = {"MCOMP_VAULT_TEST_LISTENERS": f"{my_name},cua-netgw", "MCOMP_VAULT_TEST_PROMPT_TIMEOUT": "8"}
    fill = ["fill", "web-each", "--vm", "avm", "--field", "password"]

    # --- 1. requests, approve with Touch ID (the stand-in allows), deny ---------------------------------------
    u = start_unlock(["--for", "20m", *allow], dict(T, MCOMP_VAULT_TEST_LOCAL_AUTH="allow"))
    proc = background(*fill)
    rid = next_request(u)
    u.expect("Type web-each to allow")
    reqs = json.loads(pending().stdout or "{}").get("requests", [])
    check("requests --json: the waiting request with its id, secret, site, VM, masked username and age (no challenge)",
          len(reqs) == 1 and reqs[0]["id"] == rid and reqs[0]["name"] == "web-each" and reqs[0]["site"] == SITE
          and reqs[0]["vm"] == "avm" and reqs[0]["username_hint"] == USERNAME[0] + "***" and isinstance(reqs[0]["age_s"], int)
          and "challenge" not in reqs[0] and USERNAME not in json.dumps(reqs), reqs)
    r = client("requests")
    check("requests: one line per request, and how to approve or refuse it", r.returncode == 0 and rid in r.stdout
          and "approve ID" in r.stdout and "deny ID" in r.stdout and USERNAME not in r.stdout, r.stdout)
    r = client("approve", rid)
    rc, out, _ = finish(proc)
    check("approve ID: Touch ID or the login password (the stand-in says yes) allows the waiting use", r.returncode == 0
          and "Approved request" in r.stdout and rc == 0 and js(out).get("ok") is True and js(out).get("request_id") == rid, (r.stdout, r.stderr, out))
    recs = audit_recs()
    rq = [x for x in recs if x.get("op") == "request" and x.get("request_id") == rid]
    au = [x for x in recs if x.get("op") == "authorize" and x.get("request_id") == rid]
    fl = [x for x in recs if x.get("op") == "fill" and x.get("request_id") == rid]
    check("audit: login.request, login.authorize (each:local-auth) and login.fill, each with the requester, origin, VM and a masked username",
          rq and au and fl and rq[0].get("event") == "login.request" and au[0].get("event") == "login.authorize"
          and au[0].get("approval") == "each:local-auth" and fl[0].get("event") == "login.fill" and fl[0].get("approval") == "each:local-auth"
          and all(x.get("origin") == SITE and x.get("vm") == "avm" and x.get("username_hint") == USERNAME[0] + "***"
                  and "client_pid" in x.get("requester", {}) for x in rq + au + fl), (rq, au, fl))
    check("...and only the approved site went to the VM with the value", json.loads(calls()[-1]["stdin"]).get("origins") == [SITE])

    proc = background(*fill)
    rid = next_request(u)
    r = client("deny", rid)
    rc, out, _ = finish(proc)
    check("deny ID: anyone may refuse (it only restricts); the use is refused, exit 2", r.returncode == 0 and rc == 2
          and "deny" in js(out).get("reason", ""), (r.stdout, out))
    d = [x for x in audit_recs() if x.get("op") == "fill" and x.get("request_id") == rid]
    check("...recorded as login.denied", d and d[0].get("event") == "login.denied" and d[0].get("result") == "denied", d)

    proc = background(*fill)
    rid = next_request(u)
    res = raw({"op": "approve", "id": rid, "method": "passphrase", "proof": "00" * 32})
    res2 = raw({"op": "approve", "id": rid, "method": "terminal"})
    res3 = raw({"op": "approve", "id": "deadbeef", "method": "local-auth"})
    still = [x["id"] for x in json.loads(pending().stdout or "{}").get("requests", [])]
    check("a raw approve with a forged passphrase proof is refused, and the request still waits", res.get("ok") is False
          and "proof" in res.get("reason", "") and still == [rid], (res, still))
    check("...an approve claiming the terminal (or any other method) is refused", res2.get("ok") is False, res2)
    check("...an approve of a request that doesn't exist is refused", res3.get("ok") is False and "no request" in res3.get("reason", ""), res3)
    r = client("approve", rid, "--passphrase")
    check("approve --passphrase without a terminal: refused before anything is asked", r.returncode != 0 and "own terminal" in r.stderr, r.stderr)
    rc_t, out_t = tty([BIN, "approve", rid, "--passphrase"], [("Vault passphrase", "not the passphrase 99\n")])
    still = [x["id"] for x in json.loads(pending().stdout or "{}").get("requests", [])]
    check("approve --passphrase on a terminal of its own (as `script` makes one) with the wrong passphrase: refused, still waiting",
          rc_t != 0 and still == [rid] and "web-each" in out_t, out_t)
    rc_t, out_t = tty([BIN, "approve", rid, "--passphrase"], [("Vault passphrase", PASS + "\n")])
    rc, out, _ = finish(proc)
    au = [x for x in audit_recs() if x.get("op") == "authorize" and x.get("request_id") == rid]
    check("approve --passphrase with the passphrase allows it (each:passphrase), and shows what it approves first", rc_t == 0
          and rc == 0 and au and au[0].get("approval") == "each:passphrase" and "Approve request " + rid in out_t, (out_t, out))
    bad = [x for x in audit_recs() if x.get("op") == "approve" and x.get("result") == "denied"]
    check("the refused approvals are in the audit log (who sent them, why)", bad and all("requester" in x for x in bad), bad)

    # A program that took over the socket shows other text, or keeps a proof to use later: neither works.
    proc = background(*fill)
    rid = next_request(u)
    real = raw({"op": "requests"})["requests"][0]
    fdir = os.path.join(td, "f")
    os.makedirs(fdir, mode=0o700)
    shutil.copy(os.path.join(vdir, "vault.json"), fdir)
    fakev = FakeVault(os.path.join(fdir, "vault.sock"), [dict(real, summary="something harmless")])
    tty([BIN, "approve", rid, "--passphrase"], [("Vault passphrase", PASS + "\n")], extra={"MCOMP_VAULT_DIR": fdir})
    forged = next((g for g in fakev.got if g.get("op") == "approve"), {})
    res = raw(forged) if forged else {}
    check("a passphrase proof made for other text (shown by a stand-in on the socket) doesn't approve the real request",
          forged.get("proof") and "something harmless" not in json.dumps(res) and res.get("ok") is False, (forged.keys(), res))
    fakev.listing = [real]
    fakev.got.clear()
    tty([BIN, "approve", rid, "--passphrase"], [("Vault passphrase", PASS + "\n")], extra={"MCOMP_VAULT_DIR": fdir})
    kept = next((g for g in fakev.got if g.get("op") == "approve"), {})
    raw({"op": "deny", "id": rid})
    finish(proc)
    proc = background(*fill)
    rid2 = next_request(u)
    res = raw(dict(kept, id=rid2)) if kept else {}
    res_old = raw(kept) if kept else {}
    raw({"op": "deny", "id": rid2})
    finish(proc)
    fakev.close()
    check("a proof kept from one request approves neither a later request nor the old one again", kept.get("proof")
          and res.get("ok") is False and res_old.get("ok") is False, (res, res_old))

    # The unlock terminal still answers, as before; no answer refuses.
    proc = background(*fill)
    next_request(u)
    u.expect("Type web-each to allow")
    u.send("web-each\n")
    rc, out, _ = finish(proc)
    check("typing the name at the unlock terminal still allows it (each:yes)", rc == 0, out)
    # A grant (`NAME session`) decides whether to ask, never where the value goes: the next use is checked first.
    p, outs = as_claude(proj2, fill, fill)
    next_request(u)
    u.expect("Type web-each to allow")
    u.send("web-each session\n")
    res = claude_results(p, outs)
    kinds = [c.get("kind") for c in calls()][-4:]
    last_fill = [json.loads(c["stdin"]) for c in calls() if c.get("kind") == "fill:fill"][-1]
    rec = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"][-1]
    check("a use a grant covers is still checked first, and the value goes only to the site reported then",
          [x[0] for x in res] == [0, 0] and rec.get("approval") == "grant:session" and kinds == ["fill:check", "fill:fill", "fill:check", "fill:fill"]
          and last_fill.get("origins") == [SITE], (res, kinds, last_fill.get("origins")))
    proc = background(*fill)
    next_request(u)
    rc, out, _ = finish(proc)
    check("no answer: refused after the timeout", rc == 2, out)
    u.expect("(no answer in 8 s")

    # While a question waits, other requests are served (list, status, requests at once; fills after it).
    proc = background(*fill)
    rid = next_request(u)
    t0 = time.time()
    r1 = client("list")
    r2 = client("status", "--json")
    quick = time.time() - t0
    proc2 = background("fill", "web-sess", "--vm", "avm", "--field", "password")
    time.sleep(1)
    early = proc2.poll()
    raw({"op": "deny", "id": rid})
    finish(proc)
    rc2, out2, _ = finish(proc2)
    check("while a question waits, list and status answer at once", r1.returncode == 0 and "web-each" in r1.stdout
          and json.loads(r2.stdout).get("unlocked") is True and quick < 5, (quick, r1.stdout[-200:]))
    check("...and another fill waits its turn, then runs", early is None and rc2 == 0, out2)

    # --- 2. identity providers always ask ------------------------------------------------------------------
    n = len(calls())
    proc = background("fill", "idp-login", "--vm", "avm", "--field", "password")
    next_request(u)
    u.expect("identity provider's sign-in")
    u.expect("Type idp-login to allow")
    u.send("idp-login session\n")
    rc, out, _ = finish(proc)
    check("a secret set to 'session' still asks on an identity provider's site", rc == 0 and [c["kind"] for c in calls()[n:]] == ["fill:check", "fill:fill"], out)
    u.expect("allowed for this use only")
    rec = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "idp-login"][-1]
    check("...'NAME session' there allows this use only (each:yes), recorded as an identity provider's request", rec.get("approval") == "each:yes"
          and any(x.get("op") == "request" and x.get("identity_provider") is True for x in audit_recs()), rec)
    proc = background("fill", "idp-login", "--vm", "avm", "--field", "password")
    next_request(u)
    u.expect("Type idp-login to allow")
    u.send("idp-login unlock\n")
    rc, out, _ = finish(proc)
    proc = background("fill", "idp-login", "--vm", "avm", "--field", "password")
    next_request(u)
    u.expect("Type idp-login to allow")
    u.send("no\n")
    rc2, out2, _ = finish(proc)
    check("...and so does 'NAME unlock': the next use asks again", rc == 0 and rc2 == 2, (out, out2))
    n = len(calls())
    r = client("fill", "mixed", "--vm", "avm", "--field", "password")
    sent = [json.loads(c["stdin"]) for c in calls()[n:]]
    check("a 'session' secret with an identity provider among its sites: on its other site no question, and the value goes only there",
          r.returncode == 0 and [s["mode"] for s in sent] == ["check", "fill"] and sent[-1]["origins"] == [SITE], (r.stdout, sent))
    flag("origin-last-next")
    proc = background("fill", "mixed", "--vm", "avm", "--field", "password")
    next_request(u)
    u.expect("Type mixed to allow")
    u.send("no\n")
    rc, out, _ = finish(proc)
    check("...on the identity provider's site it asks", rc == 2, out)
    proc = background("token", "api-idp", "--vm", "avm")
    next_request(u)
    u.expect("Type api-idp to allow")
    u.send("no\n")
    rc, out, _ = finish(proc)
    check("an API token for an identity provider's host asks, even set to 'session'", rc == 2, out)

    # --- 3. sign in: username and password under one approval ------------------------------------------------
    n = len(calls())
    proc = background("login", "web-each", "--vm", "avm", "--password-selector", "#pw", "--username-selector", "#user")
    asked_before = u.text().count("Type web-each to allow")
    rid = next_request(u)
    u.expect("Type web-each to allow")
    question = "Sign in with web-each (username and password) on " + SITE in u.text()
    u.send("web-each\n")
    rc, out, _ = finish(proc)
    sent = [json.loads(c["stdin"]) for c in calls()[n:]]
    res = js(out)
    check("login: one question, then the username and the password, each into its own field on the checked site",
          rc == 0 and question and [(s["mode"], s["field"], s.get("selector")) for s in sent] == [
              ("check", "password", "#pw"), ("check", "username", "#user"), ("fill", "username", "#user"), ("fill", "password", "#pw")]
          and sent[2]["value"] == USERNAME and sent[3]["value"] == PW_VALUE and all(s["origins"] == [SITE] for s in sent[1:])
          and u.text().count("Type web-each to allow") == asked_before + 1, (out, [(s["mode"], s["field"]) for s in sent]))
    check("...its result: the fields, the site, a masked username, never a value", res.get("fields") == ["username", "password"]
          and res.get("origin") == SITE and res.get("username_hint") == USERNAME[0] + "***" and USERNAME not in out and PW_VALUE not in out, out)
    lg = [x for x in audit_recs() if x.get("op") == "login"]
    check("...recorded as op login with login.fill", lg and lg[-1].get("event") == "login.fill" and lg[-1].get("request_id") == rid, lg[-1:])
    r = client("login", "web-sess", "--vm", "avm", "--password-selector", "#pw")
    check("login with a 'session' secret and only a password field: no question", r.returncode == 0 and js(r.stdout).get("fields") == ["password"], r.stdout)
    r = client("login", "web-sess", "--vm", "avm", "--password-selector", "#pw", "--request-id", "zz")
    check("login: a malformed --request-id is a usage error", r.returncode == 64)
    proc = background("login", "web-each", "--vm", "avm", "--password-selector", "#pw", "--request-id", "c0ffee01")
    rid = next_request(u)
    lst = json.loads(pending().stdout or "{}").get("requests", [])
    raw({"op": "deny", "id": "c0ffee01"})
    finish(proc)
    check("a client may choose its request's id (the hub does, to report it at once)", rid == "c0ffee01" and lst and lst[0]["id"] == "c0ffee01", (rid, lst))
    proc = background("login", "web-each", "--vm", "avm", "--password-selector", "#pw", "--request-id", "c0ffee01")
    rc, out, _ = finish(proc)
    check("...but never one used before in this unlock", rc == 2 and "used already" in js(out).get("reason", ""), out)

    # --- 4. unattended rules (made with Touch ID; the stand-in allows) ---------------------------------------
    r = client("rule", "add", "--secret", "web-each", "--project", proj1)
    check("rule add needs --for (a rule always expires)", r.returncode == 64 and "--for" in r.stderr)
    r = client("rule", "add", "--secret", "web-each", "--project", proj1, "--for", "31d")
    check("...at most 30 days", r.returncode == 64)
    r = client("rule", "add", "--site", IDP, "--project", proj1, "--for", "1h")
    r2 = client("rule", "add", "--secret", "idp-login", "--project", proj1, "--for", "1h")
    check("no rule for an identity provider (its site, or a secret only for its sites)", r.returncode != 0 and r2.returncode != 0
          and "identity provider" in r.stderr + r2.stderr, (r.stderr, r2.stderr))
    r = client("rule", "add", "--secret", "web-each", "--project", proj1, "--for", "1h")
    rule_id = (re.findall(r"Rule ([0-9a-f]{8})", r.stdout) or [None])[0]
    check("rule add with Touch ID (the stand-in): made", r.returncode == 0 and rule_id, (r.stdout, r.stderr))
    lst = json.loads(client("rule", "list", "--json").stdout or "{}").get("rules", [])
    check("rule list --json: the rule, its project, expiry and how it was confirmed", lst and lst[0]["id"] == rule_id
          and lst[0]["project"] == proj1 and lst[0]["confirmed"] == "local-auth" and lst[0]["expires_in_s"] > 3000, lst)
    p, outs = as_claude(os.path.join(proj1, "sub"), fill)
    res = claude_results(p, outs)
    rec = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"][-1]
    check("the rule covers a Claude Code session in that project (or below it): no question (rule:ID)", res[0][0] == 0
          and rec.get("approval") == f"rule:{rule_id}", (res, rec))
    last_fill = [json.loads(c["stdin"]) for c in calls() if c.get("kind") == "fill:fill"][-1]
    check("...and the value goes only to the site the VM reported at the check, not every site of the secret",
          last_fill.get("origins") == [SITE], last_fill.get("origins"))
    p, outs = as_claude(proj2, fill)
    next_request(u)
    u.send("no\n")
    res = claude_results(p, outs)
    check("...not one in another project: it asks", res[0][0] == 2, res)
    h = detached_start(*fill)
    next_request(u)
    u.send("no\n")
    rc, out = detached_wait(h)
    check("...nor a request from outside Claude Code", rc == 2, out)
    # on behalf of: a program says it asks for a Claude Code process. That's attribution only: it never satisfies a
    # rule (or a project limit, or a grant), since any program of the user's can name any live process.
    holder = subprocess.Popen([fake, proj1, "/bin/sleep", "60"], env=env)
    procs.append(holder)
    sleeper = wait_for(lambda: subprocess.run(["pgrep", "-P", str(holder.pid)], capture_output=True, text=True).stdout.split(), 5)
    h = detached_start(*fill, "--on-behalf-pid", sleeper[0] if sleeper else "2")
    asked_for = next_request(u)
    shown = u.text()
    u.send("no\n")
    rc, out = detached_wait(h)
    rec = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"][-1]
    req_ = rec.get("requester", {})
    check("a request passed on for a Claude Code process (--on-behalf-pid): the rule for that project doesn't cover it (it asks), "
          "and the record keeps the claim as unverified, apart from who asked",
          asked_for and rc == 2 and "project" not in req_ and req_.get("on_behalf", {}).get("project") == proj1
          and req_["on_behalf"].get("verified") is False and "as it says (unverified)" in shown, (out, req_))
    p, outs = as_claude(proj2, [*fill, "--on-behalf-pid", sleeper[0] if sleeper else "2"])
    next_request(u)
    u.send("no\n")
    res = claude_results(p, outs)
    check("...nor does a command from another Claude Code session that names one there", res[0][0] == 2, res)
    # The metacomputer hub serves every session from one process, which may sit under the session that started it: a
    # request from it (recognised by its command line, which only takes standing away) is the hub's own.
    hubfile = os.path.join(td, "hubfake", "lib", "mcp_hub.py")
    os.makedirs(os.path.dirname(hubfile), exist_ok=True)
    open(hubfile, "w").write("import subprocess, sys\nsys.exit(subprocess.call(sys.argv[2:]))\n")
    hp = subprocess.Popen([fake, proj1, sys.executable, hubfile, "serve", BIN, *fill, "--profile", "p1"], env=env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(hp)
    try:
        asked = next_request(u, 10)
        u.send("no\n")
    except AssertionError:
        asked = None
    hp.wait(60)
    rec = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"][-1]
    req_ = rec.get("requester", {})
    check("a request from the metacomputer hub started inside a Claude Code session in the rule's project: the hub asks as "
          "itself (no session, no project), so the rule doesn't cover it", asked and req_.get("hub_pid")
          and "claude_pid" not in req_ and "project" not in req_ and hp.returncode == 2, (req_, hp.returncode))
    # N4: a fill through the hub names the Chrome the hub checked (--profile or --browser-pid), or it's refused.
    hout = os.path.join(td, "hub-nobind.out")
    hp = subprocess.Popen([fake, proj1, "/bin/sh", "-c", shlex.join([sys.executable, hubfile, "serve", BIN, *fill]) + " >" + shlex.quote(hout) + " 2>&1"],
                          env=env, stdin=subprocess.DEVNULL)
    procs.append(hp)
    try:
        asked_nb = next_request(u, 6)
        u.send("no\n")
    except AssertionError:
        asked_nb = None
    hp.wait(60)
    nb = open(hout).read() if os.path.exists(hout) else ""
    check("N4: a fill through the hub that doesn't name the Chrome it checked (--profile or --browser-pid) is refused before "
          "anyone is asked", hp.returncode == 2 and not asked_nb and "must name the Chrome it checked" in nb, (nb[-300:], asked_nb))
    r = client(*fill, "--browser-pid", "notapid")
    check("...and --browser-pid takes a process id", r.returncode == 64 and "--browser-pid" in r.stderr, r.stderr)
    holder.kill()
    r = client("rule", "add", "--site", SITE2, "--project", proj1, "--for", "1h")
    site_rule = (re.findall(r"Rule ([0-9a-f]{8})", r.stdout) or [None])[0]
    p, outs = as_claude(proj1, ["fill", "other", "--vm", "avm", "--field", "password"])
    res = claude_results(p, outs)
    check("a rule for a site covers any secret's fill into it", r.returncode == 0 and res[0][0] == 0, (r.stdout, res))
    client("rule", "remove", rule_id)
    flag("origin-last-next")            # the tab is on web-each's second site, SITE2, which the site rule covers
    p, outs = as_claude(proj1, fill)
    res = claude_results(p, outs)
    last_fill = [json.loads(c["stdin"]) for c in calls() if c.get("kind") == "fill:fill"][-1]
    check("a site rule covers a two-site secret only on that site, and the value goes there alone",
          res[0][0] == 0 and last_fill.get("origins") == [SITE2], (res, last_fill.get("origins")))
    rule_id = (re.findall(r"Rule ([0-9a-f]{8})", client("rule", "add", "--secret", "web-each", "--project", proj1, "--for", "1h").stdout) or [None])[0]
    r = client("rule", "remove", rule_id)
    p, outs = as_claude(proj1, fill)
    next_request(u)
    u.send("no\n")
    res = claude_results(p, outs)
    check("rule remove (anyone may: it only restricts): the use asks again", r.returncode == 0 and res[0][0] == 2, (r.stdout, res))
    client("rule", "remove", site_rule)
    r = client("rule", "add", "--secret", "web-each", "--project", proj1, "--for", "2s")
    time.sleep(3)
    p, outs = as_claude(proj1, fill)
    next_request(u)
    u.send("no\n")
    res = claude_results(p, outs)
    check("a rule that has expired covers nothing", r.returncode == 0 and res[0][0] == 2, (r.stdout, res))
    r = client("rule", "add", "--secret", "web-each", "--project", proj1, "--for", "1h", "--passphrase")
    check("rule add --passphrase without a terminal: refused", r.returncode != 0 and "own terminal" in r.stderr, r.stderr)
    rc, out = tty([BIN, "rule", "add", "--secret", "web-each", "--project", proj1, "--for", "1h", "--passphrase"],
                  [("Vault passphrase", "not the passphrase 99\n")])
    check("...on a terminal of its own with the wrong passphrase: no rule", rc != 0 and not json.loads(client("rule", "list", "--json").stdout).get("rules"), out)
    rc, out = tty([BIN, "rule", "add", "--secret", "web-each", "--project", proj1, "--for", "1h", "--passphrase"],
                  [("Vault passphrase", PASS + "\n")])
    lst = json.loads(client("rule", "list", "--json").stdout or "{}").get("rules", [])
    check("...with the passphrase: made (confirmed passphrase), and the running unlock uses it at once", rc == 0 and lst
          and lst[0]["confirmed"] == "passphrase", (out, lst))
    shown_first = out.split("Vault passphrase")[0] if "Vault passphrase" in out else ""
    check("N1: the passphrase prompt for a rule comes after the rule itself is shown: what it allows, for whom, and until when",
          "Make an unattended rule: allow uses of the secret web-each" in shown_first and "until" in shown_first, out[-400:])
    # P2: the rule stored is exactly the rule shown: the expiry is fixed when it's shown, not when the passphrase arrives.
    pr = ts.Pty([BIN, "rule", "add", "--site", SITE2, "--project", proj1, "--for", "2h", "--passphrase"], env)
    procs.append(pr)
    pr.expect("Vault passphrase")
    time.sleep(2.5)
    pr.send(PASS + "\n")
    pr.wait(60)
    shown_rule = pr.text()
    pr.close()
    m = re.search(r"until (\S+)\. Your vault passphrase", shown_rule)
    lst2 = json.loads(client("rule", "list", "--json").stdout or "{}").get("rules", [])
    mine = [x for x in lst2 if x.get("site") == SITE2 or x.get("origin") == SITE2]
    check("P2: a rule confirmed with the passphrase is stored with exactly the expiry shown before the passphrase, however long "
          "the passphrase took", m and mine and mine[-1].get("expires") == m.group(1), (shown_rule[-300:], mine))
    for x in mine:
        client("rule", "remove", x["id"])
    deep = os.path.join(td, "p" * 120, "q" * 120, "r" * 120)
    os.makedirs(deep, exist_ok=True)
    trace_r = os.path.join(td, "rule-trace")
    lock(u)
    u = start_unlock(["--for", "20m", *allow], dict(T, MCOMP_VAULT_TEST_LOCAL_AUTH="allow", MCOMP_VAULT_TEST_TRACE=trace_r))
    r = client("rule", "add", "--secret", "web-each", "--project", deep, "--for", "1h")
    lst3 = json.loads(client("rule", "list", "--json").stdout or "{}").get("rules", [])
    shown_dialogs = [l for l in (open(trace_r).read().splitlines() if os.path.exists(trace_r) else []) if l.startswith("local-auth ")]
    check("P2: a rule too long to show whole in macOS's dialog (the project and the expiry would be cut off) is refused, "
          "not confirmed with a shortened text", r.returncode == 2 and "can't be shown whole" in r.stdout + r.stderr
          and not [x for x in lst3 if x.get("project") == deep], (r.stdout, r.stderr, shown_dialogs[-1:]))
    lst = lst + [x for x in lst3 if x not in lst]
    p, outs = as_claude(proj1, ["fill", "idp-login", "--vm", "avm", "--field", "password"])
    next_request(u)
    u.send("no\n")
    res = claude_results(p, outs)
    check("an identity provider's use still asks while rules exist", res[0][0] == 2, res)
    for x in lst:
        client("rule", "remove", x["id"])
    lock(u)

    # --- 5. the person says no at the dialog; no dialog at all -----------------------------------------------
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_LOCAL_AUTH="deny"))
    proc = background(*fill)
    rid = next_request(u)
    r = client("approve", rid)
    still = [x["id"] for x in json.loads(pending().stdout or "{}").get("requests", [])]
    u.expect("Type web-each to allow")
    u.send("web-each\n")
    rc, out, _ = finish(proc)
    check("approve ID when the person cancels the dialog: not approved, the request still waits (and the terminal can still allow it)",
          r.returncode == 2 and "cancelled" in r.stderr and still == [rid] and rc == 0, (r.stderr, still, out))
    res = raw({"op": "rule_add", "secret": "web-each", "project": proj1, "for_s": 3600})
    check("a raw rule_add when the person cancels the dialog: no rule", res.get("ok") is False
          and not json.loads(client("rule", "list", "--json").stdout).get("rules"), res)
    lock(u)
    u = start_unlock(["--for", "10m", *allow], T)
    proc = background(*fill)
    rid = next_request(u)
    r = client("approve", rid)
    res = raw({"op": "rule_add", "secret": "web-each", "project": proj1, "for_s": 3600})
    raw({"op": "deny", "id": rid})
    finish(proc)
    check("a test build without the stand-in shows no dialog: approve and rule add refuse", r.returncode == 2
          and "never shows" in r.stderr and res.get("ok") is False, (r.stderr, res))
    lock(u)

    # --- 6. unlock --touch-id: each question also shows the dialog ---------------------------------------------
    u = start_unlock(["--for", "10m", "--touch-id", *allow], dict(T, MCOMP_VAULT_TEST_LOCAL_AUTH="allow"))
    u.expect("--touch-id")
    r = client(*fill)
    rec = [x for x in audit_recs() if x.get("op") == "fill"][-1]
    check("--touch-id: the dialog (the stand-in says yes) allows a use without typing", r.returncode == 0 and rec.get("approval") == "each:local-auth", r.stdout)
    lock(u)
    u = start_unlock(["--for", "10m", "--touch-id", *allow], dict(T, MCOMP_VAULT_TEST_LOCAL_AUTH="deny"))
    r = client(*fill)
    check("--touch-id: cancelling the dialog refuses the use", r.returncode == 2 and "Touch ID" in js(r.stdout).get("reason", ""), r.stdout)
    lock(u)
    M = dict(T, TMUX="/tmp/tmux-test/default,1,0", MCOMP_VAULT_TEST_LOCAL_AUTH="wait")
    rc, out = tty([BIN, "unlock", "--confirm", "each"], [("runs inside tmux", None)], extra=M)
    check("inside tmux, --confirm each still needs --allow-multiplexer or --touch-id", rc != 0 and "--touch-id" in out, out)
    u = start_unlock(["--for", "10m", "--confirm", "each", "--touch-id"], M)
    proc = background("fill", "web-sess", "--vm", "avm", "--field", "password")
    rid = next_request(u)
    u.send("web-sess\n")
    time.sleep(1.5)
    typed_ok = proc.poll()
    raw({"op": "deny", "id": rid})
    rc, out, _ = finish(proc)
    check("inside tmux with --touch-id, a typed answer doesn't approve (only the dialog would)", typed_ok is None and rc == 2, out)
    lock(u)

    # --- 6b. presence mode: every approval needs the dialog or the passphrase; typing only chooses ---------------------
    # MCOMP_VAULT_TEST_BIOMETRY stands in for the Mac's Touch ID check (a test build never asks the real one).
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="none"))
    u.expect("WARNING: this Mac has no Touch ID")
    st = json.loads(client("status", "--json").stdout or "{}")
    proc = background(*fill)
    next_request(u)
    u.expect("Type web-each to allow")
    u.send("web-each\n")
    rc, out, _ = finish(proc)
    check("no Touch ID: typed approval stays the default, with a warning saying why and how to turn presence mode on",
          st.get("presence") is False and rc == 0 and "--require-presence" in u.text(), st)
    lock(u)
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="not-enrolled"))
    u.expect("no fingerprint enrolled")
    st = json.loads(client("status", "--json").stdout or "{}")
    check("a Touch ID sensor with no fingerprint counts as none, and the unlock says so", st.get("presence") is False, st)
    lock(u)
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="unavailable"))
    st = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    locked_un = json.loads(client("status", "--json", extra={"MCOMP_VAULT_TEST_BIOMETRY": "unavailable"}).stdout or "{}")
    check("Touch ID that can't be checked right now (a disconnected keyboard, an error) keeps presence mode on, and says why",
          st.get("presence") is True and "can't be checked right now" in st.get("presence_why", "") and "disconnected" in u.text()
          and locked_un.get("presence_default") is True, (st, locked_un))
    pre_touch = os.path.join(td, "vault-before-touch-id.json")
    shutil.copy(os.path.join(vdir, "vault.json"), pre_touch)       # a copy from before this vault saw Touch ID enrolled
    P = dict(T, MCOMP_VAULT_TEST_BIOMETRY="available", MCOMP_VAULT_TEST_LOCAL_AUTH="wait")
    u = start_unlock(["--for", "10m", *allow], P)
    u.expect("Presence mode (this Mac has Touch ID)")
    st = json.loads(client("status", "--json").stdout or "{}")
    proc = background(*fill)
    rid = next_request(u)
    u.expect("never approves on its own")
    u.send("web-each\n")
    soft(u, "chosen: for this one use")
    time.sleep(1.5)
    typed_only = proc.poll()
    rc_t, out_t = tty([BIN, "approve", rid, "--passphrase"], [("Vault passphrase", PASS + "\n")])
    rc, out, _ = finish(proc)
    au = [x for x in audit_recs() if x.get("op") == "authorize" and x.get("request_id") == rid]
    check("presence mode by default where Touch ID is: typing the name doesn't approve; the passphrase proof then does",
          st.get("presence") is True and typed_only is None and rc_t == 0 and rc == 0 and au and au[0]["approval"] == "each:passphrase"
          and au[0].get("presence") is True and au[0].get("confirmed") == "passphrase", (st, typed_only, out, au))
    # N1: what's authenticated names its scope, and nothing else can widen it.
    proc = background(*fill)
    rid = next_request(u)
    u.send("web-each unlock\n")
    soft(u, "chosen: for every use of web-each until the vault locks")
    rc_t, out_t = tty_soft([BIN, "approve", rid, "--passphrase"], [("Vault passphrase", PASS + "\n")])
    rc, out, _ = finish(proc)
    au = [x for x in audit_recs() if x.get("op") == "authorize" and x.get("request_id") == rid]
    proc = background(*fill)
    try:
        asked_again = next_request(u, 6)
    except AssertionError:
        asked_again = None
    if asked_again:
        raw({"op": "deny", "id": asked_again})
    finish(proc)
    check("N1: `NAME unlock` typed, then a passphrase proof for one use (it names \"this one use\"): one use only, no "
          "unlock-wide grant, and the next use asks again", rc_t == 0 and rc == 0 and "for this one use" in out_t
          and au and au[0].get("approval") == "each:passphrase" and asked_again, (out_t[-300:], au, asked_again))
    p, outs = as_claude(proj2, fill, fill)
    rid = next_request(u)
    u.send("web-each session\n")
    soft(u, "chosen: for this use and the rest of this Claude Code session")
    time.sleep(1)
    grants_before = p.poll()
    rc_s, out_s = tty_soft([BIN, "approve", rid, "--passphrase", "--scope", "session"], [("Vault passphrase", PASS + "\n")])
    res = claude_results(p, outs)
    recs = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"][-2:]
    check("...`NAME session` typed there only chooses: the grant exists once a confirmation naming that scope arrives "
          "(`approve ID --passphrase --scope session`, whose prompt names it)",
          grants_before is None and rc_s == 0 and "for this use and the rest of this Claude Code session" in out_s
          and [x[0] for x in res] == [0, 0] and [x.get("approval") for x in recs] == ["each:session", "grant:session"],
          (res, [x.get("approval") for x in recs], out_s[-300:]))
    proc = background(*fill)
    rid = next_request(u)
    rc_t, out_t = tty_soft([BIN, "approve", rid, "--passphrase", "--scope", "unlock"], [("Vault passphrase", PASS + "\n")])
    rc, out, _ = finish(proc)
    r2 = client(*fill)
    rec2 = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"][-1]
    check("...a wider scope comes only from a confirmation that names it: `approve ID --passphrase --scope unlock` "
          "(its prompt says until the vault locks), and then the next use doesn't ask",
          rc_t == 0 and rc == 0 and "until the vault locks" in out_t and r2.returncode == 0 and rec2.get("approval") == "grant:unlock",
          (out_t[-300:], rec2.get("approval")))
    lock(u)
    trace = os.path.join(td, "la-trace")
    u = start_unlock(["--for", "10m", *allow], dict(P, MCOMP_VAULT_TEST_LOCAL_AUTH="allow-first-after:3", MCOMP_VAULT_TEST_TRACE=trace))
    proc = background(*fill)
    rid = next_request(u)
    u.send("web-each unlock\n")
    rc, out, _ = finish(proc)
    dialogs = [l for l in (open(trace).read().splitlines() if os.path.exists(trace) else []) if l.startswith("local-auth ")]
    widened = [x for x in audit_recs() if x.get("name") == "web-each" and x.get("approval") in ("each:unlock", "grant:unlock")
               and x.get("request_id") == rid]
    check("N1: `NAME unlock` typed while the dialog for one use is open withdraws that dialog; a new one names the wider "
          "scope, and only it could allow that (here it wasn't answered), so no unlock-wide grant comes from the first",
          rc == 2 and not widened and len(dialogs) == 2 and "for this one use" in dialogs[0]
          and "for every use of web-each until the vault locks" in dialogs[1], (out, widened, dialogs))
    proc = background(*fill)
    next_request(u)
    u.send("nope\n")
    rc, out, _ = finish(proc)
    check("...and anything else typed refuses, as before", rc == 2, out)
    lock(u)
    u = start_unlock(["--for", "10m", *allow], dict(P, MCOMP_VAULT_TEST_LOCAL_AUTH="allow"))
    r = client(*fill)
    rec = [x for x in audit_recs() if x.get("op") == "fill"][-1]
    check("presence mode with the dialog answering (the stand-in): allowed, nothing typed (each:local-auth)", r.returncode == 0
          and rec.get("approval") == "each:local-auth", r.stdout)
    lock(u)
    u = start_unlock(["--for", "10m", "--confirm", "each"], dict(P, TMUX="/tmp/tmux-test/default,1,0", MCOMP_VAULT_TEST_LOCAL_AUTH="allow"))
    r = client("fill", "web-sess", "--vm", "avm", "--field", "password")
    check("presence mode inside tmux: --confirm each starts, and the dialog approves (typing could only choose)", r.returncode == 0, r.stdout)
    lock(u)
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="not-enrolled"))
    s_seen = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="none"))
    s_seen2 = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    check("N2: once this vault has seen Touch ID enrolled on this Mac, presence mode stays on by default when Touch ID "
          "later reports no finger, or no sensor (only `presence off` or an unlock flag turns it off)",
          s_seen.get("presence") is True and "seen Touch ID enrolled" in s_seen.get("presence_why", "") and s_seen2.get("presence") is True
          and any(x.get("op") == "presence" and x.get("mode") == "biometry_seen" for x in audit_recs()), (s_seen, s_seen2))
    # P2: the vault file put back to that older copy (any program of yours could): the audit log still says Touch ID was
    # seen enrolled, so presence mode stays on, and the unlock says why.
    cur = os.path.join(td, "vault-current.json")
    shutil.copy(os.path.join(vdir, "vault.json"), cur)
    shutil.copy(pre_touch, os.path.join(vdir, "vault.json"))
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="none"))
    s_rb = json.loads(client("status", "--json").stdout or "{}")
    rb_text = u.text()
    lock(u)
    shutil.copy(cur, os.path.join(vdir, "vault.json"))
    check("P2: a vault file put back to a copy from before Touch ID was seen enrolled: the audit log still has it, so presence "
          "mode stays on and the unlock says the file may be an older copy", s_rb.get("presence") is True and "older copy" in rb_text
          and any(x.get("op") == "presence" and x.get("mode") == "rollback_suspected" for x in audit_recs()), (s_rb, rb_text[-500:]))
    u = start_unlock(["--for", "10m", "--no-require-presence", *allow], P)
    s1 = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    u = start_unlock(["--for", "10m", "--require-presence", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="none"))
    s2 = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    check("either default can be switched for one unlock: --no-require-presence, --require-presence",
          s1.get("presence") is False and s2.get("presence") is True and s2.get("presence_why") == "--require-presence", (s1, s2))
    r = client("presence", "on")
    check("presence on|off|auto without a terminal: refused (it needs the passphrase)", r.returncode != 0 and "own terminal" in r.stderr, r.stderr)
    rc1, o1 = tty([BIN, "presence", "on"], [("Vault passphrase", PASS + "\n")])
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="none"))
    s1 = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    rc2, o2 = tty([BIN, "presence", "off"], [("Vault passphrase", PASS + "\n")])
    u = start_unlock(["--for", "10m", *allow], P)
    s2 = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="not-enrolled"))
    s_off = json.loads(client("status", "--json").stdout or "{}")
    lock(u)
    rc3, o3 = tty([BIN, "presence", "auto"], [("Vault passphrase", PASS + "\n")])
    locked_st = json.loads(client("status", "--json", extra={"MCOMP_VAULT_TEST_BIOMETRY": "available"}).stdout or "{}")
    check("the vault's own setting (presence on|off|auto, with the passphrase) changes the default; status shows the mode",
          rc1 == rc2 == rc3 == 0 and s1.get("presence") is True and "setting" in s1.get("presence_why", "")
          and s2.get("presence") is False and s_off.get("presence") is False and locked_st.get("presence_default") is True
          and any(x.get("op") == "presence" for x in audit_recs()), (s1, s2, locked_st))
    conf = os.path.join(td, "sec.env")
    open(conf, "w").write(f'VM_NAME=avm-not-a-vm\nSSH_PORT={port}\nSSH_DIR="{sshdir}"\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n')
    sec = subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "security", "--json"], capture_output=True, text=True, timeout=120,
                         env=dict(env, MCOMP_CONFIG=conf, MCOMP_VAULT_BIN=BIN, MCOMP_VAULT_TEST_BIOMETRY="available"))
    sec2 = subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "security"], capture_output=True, text=True, timeout=120,
                          env=dict(env, MCOMP_CONFIG=conf, MCOMP_VAULT_BIN=BIN, MCOMP_VAULT_TEST_BIOMETRY="none"))
    vsec = (json.loads(sec.stdout or "{}").get("vault") or {}) if sec.returncode == 0 else {}
    check("`metacomputer security` shows which approval mode is in force (presence where Touch ID is, typed elsewhere)",
          vsec.get("approvals") == "presence" and "approvals    typed" in sec2.stdout.replace("\x1b[33m", "").replace("\x1b[0m", ""),
          (sec.stdout[-300:], sec.stderr[-300:], sec2.stdout[-300:]))
    rd = os.path.join(td, "relstatus")
    os.makedirs(rd, mode=0o700)
    ra = subprocess.run([REL, "status", "--json"], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "MCOMP_VAULT_DIR": rd, "MCOMP_VAULT_TEST_BIOMETRY": "available"})
    rn = subprocess.run([REL, "status", "--json"], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "MCOMP_VAULT_DIR": rd, "MCOMP_VAULT_TEST_BIOMETRY": "none"})
    check("a release build ignores the Touch ID stand-in: it asks macOS (the same answer whatever the setting says)",
          json.loads(ra.stdout).get("biometry") == json.loads(rn.stdout).get("biometry") and b"MCOMP_VAULT_TEST_BIOMETRY" not in open(REL, "rb").read(),
          (ra.stdout, rn.stdout))

    # --- 6c. presence settings and a vault file put back to an older copy (a second vault, never shown Touch ID) --------
    v2 = os.path.join(td, "v2")
    X = {"MCOMP_VAULT_DIR": v2}
    tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")], extra=X)

    def v2_unlocked_presence():
        uu = start_unlock(["--for", "5m", *allow], dict(T, MCOMP_VAULT_TEST_BIOMETRY="none", **X))
        st = json.loads(client("status", "--json", extra=X).stdout or "{}")
        text = uu.text()
        lock(uu)
        return st.get("presence"), text

    v2_auto = os.path.join(td, "v2-auto.json")
    shutil.copy(os.path.join(v2, "vault.json"), v2_auto)              # its default setting: auto
    tty([BIN, "presence", "on"], [("Vault passphrase", PASS + "\n")], extra=X)
    shutil.copy(v2_auto, os.path.join(v2, "vault.json"))              # put back to the copy from before `presence on`
    on_a, text_a = v2_unlocked_presence()
    check("P2: a vault file put back to a copy that says auto, from before an audited `presence on`: presence mode stays on "
          "(the stricter of the file and the log's authenticated setting), and the unlock says why", on_a is True and "older copy" in text_a,
          text_a[-400:])
    tty([BIN, "presence", "off"], [("Vault passphrase", PASS + "\n")], extra=X)
    v2_off = os.path.join(td, "v2-off.json")
    shutil.copy(os.path.join(v2, "vault.json"), v2_off)
    tty([BIN, "presence", "on"], [("Vault passphrase", PASS + "\n")], extra=X)
    shutil.copy(v2_off, os.path.join(v2, "vault.json"))
    # ...and a `presence off` record appended to the log by a program of yours: it chains properly but has no valid MAC.
    lines = open(os.path.join(v2, "audit.jsonl")).read().splitlines()
    last = json.loads(lines[-1])

    def canon(o):
        return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    forged = {"op": "presence", "result": "ok", "mode": "off", "seq": last["seq"] + 1, "prev": last["hash"],
              "ts": last["ts"], "id": "00000000-0000-4000-8000-00000000f0f0"}
    forged["hash"] = hashlib.sha256(canon(forged).encode()).hexdigest()
    open(os.path.join(v2, "audit.jsonl"), "a").write(canon(forged) + "\n")
    on_b, text_b = v2_unlocked_presence()
    check("P2: ...a file put back to a copy that says off, with a `presence off` record appended to the log without the vault's "
          "MAC: the forged record relaxes nothing, presence mode stays on", on_b is True and "older copy" in text_b, text_b[-400:])
    tty([BIN, "presence", "off"], [("Vault passphrase", PASS + "\n")], extra=X)
    on_c, _ = v2_unlocked_presence()
    check("...while a real `presence off` (with the passphrase) still turns it off", on_c is False, on_c)

    # --- 7. a release build goes to the real framework: the stand-in doesn't approve it --------------------------
    # `selftest --local-auth` asks LocalAuthentication once, through the same function every approval uses, and
    # touches no vault and no log. A release build runs it only with MCOMP_VAULT_LOCAL_AUTH=noninteractive, which
    # makes the real framework refuse at once instead of showing a dialog here.
    seam = {"PATH": "/usr/bin:/bin", "MCOMP_VAULT_TEST_LOCAL_AUTH": "allow", "MCOMP_VAULT_LOCAL_AUTH": "noninteractive"}
    rt = subprocess.run([BIN, "selftest", "--local-auth"], env=seam, capture_output=True, text=True)
    rr = subprocess.run([REL, "selftest", "--local-auth"], env=seam, capture_output=True, text=True)
    rn = subprocess.run([REL, "selftest", "--local-auth"], env={"PATH": "/usr/bin:/bin", "MCOMP_VAULT_TEST_LOCAL_AUTH": "allow"},
                        capture_output=True, text=True)
    check("the test build's stand-in answers for the person", rt.returncode == 0 and "local-auth: allowed" in rt.stdout, rt.stdout + rt.stderr)
    check("a release build ignores it: the real framework is asked (told never to show a dialog) and nothing is allowed",
          rr.returncode == 1 and "local-auth: not allowed" in rr.stdout and "test build" not in rr.stdout, rr.stdout + rr.stderr)
    check("...and without that setting a release build won't run the check at all (it would show a dialog)",
          rn.returncode == 64 and "noninteractive" in rn.stderr, rn.stdout + rn.stderr)

    # --- 8. the event names reach the sinks ----------------------------------------------------------------
    rx = tvo.http_server(tvo.Receiver)
    rc, out = tty([BIN, "audit-sink", "add", "hook", "--kind", "webhook", "--url", f"http://127.0.0.1:{rx.server_port}/hook"],
                  [("Vault passphrase", PASS + "\n")])
    rc2, out2 = tty([BIN, "audit-sink", "add", "otel", "--kind", "otlp", "--url", f"http://127.0.0.1:{rx.server_port}/otlp"],
                    [("Vault passphrase", PASS + "\n")])
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_LOCAL_AUTH="allow"))
    proc = background(*fill)
    rid = next_request(u)
    client("approve", rid)
    finish(proc)
    wait_for(lambda: any(json.loads(g["body"]).get("data", {}).get("op") == "fill" for g in rx.got if g["path"] == "/hook"), 15)
    ces = [json.loads(g["body"]) for g in rx.got if g["path"] == "/hook"]
    names = {c["data"].get("op"): c.get("mcompevent") for c in ces if c["data"].get("request_id") == rid}
    check("webhook sink: the CloudEvents carry mcompevent login.request / login.authorize / login.fill", rc == 0 and rc2 == 0
          and names == {"request": "login.request", "authorize": "login.authorize", "fill": "login.fill"}, names)
    wait_for(lambda: any("login.fill" in g["body"] for g in rx.got if g["path"].startswith("/otlp")), 15)
    attrs = [{a["key"]: a["value"].get("stringValue") for a in lr["attributes"]} for g in rx.got if g["path"].startswith("/otlp")
             for rl in json.loads(g["body"])["resourceLogs"] for sl in rl["scopeLogs"] for lr in sl["logRecords"]]
    check("otlp sink: metacomputer.event, request_id and username_hint attributes",
          any(a.get("metacomputer.event") == "login.fill" and a.get("metacomputer.request_id") == rid
              and a.get("metacomputer.username_hint") == USERNAME[0] + "***" for a in attrs), attrs[-3:])
    # P1: a VM that has the value spells it in every field of its answer, for a fill and a sign-in, failed or "worked":
    # none of it reaches the result, the audit log, `audit --json`, the unified log or the sinks.
    since = time.time()
    hx_pw, hx_user = PW_VALUE.encode().hex(), USERNAME.encode().hex()
    two = str(int.from_bytes(PW_VALUE.encode()[:2], "big")), str(int.from_bytes(USERNAME.encode()[:2], "big"))
    outs_p1 = []
    for fl in ("all-fields-leak-next", "all-fields-leak-ok-next"):
        for cmd in (fill, ["login", "web-each", "--vm", "avm", "--password-selector", "#pw", "--username-selector", "#user"]):
            flag(fl)
            proc = background(*cmd)
            rid = next_request(u)
            client("approve", rid)
            rc, out, _ = finish(proc)
            outs_p1.append((rc, out))
    time.sleep(1)
    alog_text = open(alog).read()
    aj = client("audit", "--json")
    sink_text = " ".join(g["body"] for g in rx.got)
    ul = subprocess.run(["/usr/bin/log", "show", "--style", "compact", "--start", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since - 5)),
                         "--predicate", 'subsystem == "io.metavert.metacomputer.vault.test"'], capture_output=True, text=True, timeout=120)
    places = {"results": " ".join(o for _, o in outs_p1), "audit log": alog_text, "audit --json": aj.stdout, "sinks": sink_text,
              "unified log": ul.stdout}
    leaks = {k: [x for x in (hx_pw, hx_user) if x in v] for k, v in places.items()}
    p1_recs = [json.loads(l) for l in alog_text.splitlines()][-40:]
    counts = [x.get("chars") for x in p1_recs if x.get("op") in ("fill", "login") and x.get("result") != "ok"]
    check("P1: a VM spelling the value in every field it can answer with (fill and sign-in, failed or 'worked'): nothing of it "
          "in the results, the audit log, `audit --json`, the unified log or the sinks; no VM-chosen route or count either",
          all(rc != 0 for rc, _ in outs_p1) and not any(leaks.values()) and not [c for c in counts if str(c) in two]
          and not any(x.get("route") not in (None, "element", "keyboard") for x in p1_recs),
          (leaks, [o[-200:] for _, o in outs_p1], counts))
    check("...the unified log was read (it carries the vault's audit lines), so its check above means something",
          ul.returncode == 0 and '"op":"fill"' in ul.stdout, (ul.returncode, ul.stderr[-300:]))
    r = client("audit", "--verify")
    check("audit --verify: every record (the new ones included) has a valid creation MAC and the chain holds", r.returncode == 0
          and "checks out" in r.stdout, r.stdout + r.stderr)
    lock(u)
    r = subprocess.run([sys.executable, os.path.join(LAB, "scripts/verify-audit.py"), alog], capture_output=True, text=True)
    check("verify-audit.py: the local log still checks out", r.returncode == 0, r.stdout[-400:])

    # --- 9. the verbs other vaults use: lock, --passphrase, no --passphrase-stdin -------------------------------
    r = client("init", "--passphrase-stdin")
    r2 = client("unlock", "--passphrase-stdin")
    check("--passphrase-stdin is refused (the passphrase is typed on your terminal, never piped)", r.returncode == 64
          and r2.returncode == 64 and "never piped" in r.stderr + r2.stderr, (r.stderr, r2.stderr))
    u = start_unlock(["--for", "10m", "--passphrase", *allow], T)
    r = client("lock")
    gone = wait_for(lambda: not os.path.exists(sock), 10)
    lock_rec = [x for x in audit_recs() if x.get("op") == "lock"][-1]
    check("unlock --passphrase works (it always asks for it); `lock` from any program ends the unlock (it only restricts)",
          r.returncode == 0 and "Locked" in r.stdout and gone and u.wait(15) == 0 and "`lock`" in lock_rec.get("reason", ""), (r.stdout, lock_rec))
    r = client("lock")
    check("lock when already locked: says so, exit 0", r.returncode == 0 and "already locked" in r.stdout, r.stdout)
    u = start_unlock(["--for", "10m", *allow], T)
    proc = background(*fill)
    next_request(u)
    client("lock")
    rc, out, _ = finish(proc)
    check("lock while a question waits: the use is refused and the vault locks", rc in (2, 3) and wait_for(lambda: not os.path.exists(sock), 10), out)
    u.wait(15)

    # --- 10. a last audit record longer than the tail that's read: the next record still follows it ---------------
    adir = os.path.join(td, "a10")
    os.makedirs(adir, mode=0o700)

    def canon(o):
        return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    big = {"op": "set", "result": "ok", "name": "x", "origins": ["https://a%05d.example" % i for i in range(20000)], "seq": 1,
           "prev": "0" * 64, "ts": "2026-01-01T00:00:00.000000Z", "id": "00000000-0000-4000-8000-000000000001"}
    big["hash"] = hashlib.sha256(canon(big).encode()).hexdigest()
    open(os.path.join(adir, "audit.jsonl"), "w").write(canon(big) + "\n")
    client("fill", "x", "--vm", "avm", "--field", "password", extra={"MCOMP_VAULT_DIR": adir})
    lines = open(os.path.join(adir, "audit.jsonl")).read().splitlines()
    nxt = json.loads(lines[-1]) if len(lines) > 1 else {}
    vr = subprocess.run([sys.executable, os.path.join(LAB, "scripts/verify-audit.py"), os.path.join(adir, "audit.jsonl")], capture_output=True, text=True)
    check("a last record over 256 KiB: the next one still gets seq + 1 and links to it, and the chain verifies",
          len(canon(big)) > 1 << 18 and nxt.get("seq") == 2 and nxt.get("prev") == big["hash"] and vr.returncode == 0,
          (nxt.get("seq"), nxt.get("prev"), vr.stdout[-200:]))

    # --- 11. a request whose requester goes away stops, and the vault says so (the requester's confirmation) -----
    # The unlock process watches the requester's connection while it serves a fill, sign-in, session or token
    # request: when the requester closes its side (withdraws it: a signal, its parent gone, --while-pid, its own
    # deadline) or dies, the question is withdrawn, no further step starts and a step in progress (its ssh) is killed;
    # then it answers, and that answer is what a requester (the hub) waits for before it lets the computer go.
    u = start_unlock(["--for", "10m", *allow], dict(T, MCOMP_VAULT_TEST_PROMPT_TIMEOUT="30"))

    def cancel_recs(rid=None):
        return [x for x in audit_recs() if x.get("result") == "cancelled" and (rid is None or x.get("request_id") == rid)]

    def no_pending():
        return not js(pending().stdout).get("requests")

    def nreq(timeout=20):
        """next_request, without stopping the run when no question comes (an earlier build keeps an old one open)."""
        try:
            return next_request(u, timeout)
        except AssertionError:
            return None

    proc = background(*fill)
    rid = nreq()
    t0 = time.time()
    proc.send_signal(signal.SIGTERM)
    try:
        rc, out, err = finish(proc, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc, out, err = 999, "", "no exit"
    took = time.time() - t0
    check("R6-B: SIGTERM to a fill waiting for its question withdraws it: the unlock process withdraws the question and "
          "answers (the confirmation, within seconds), recorded as cancelled with its request id; nothing is left to approve",
          rc == 4 and "went away or withdrew" in js(out).get("reason", "") and took < 8 and cancel_recs(rid)
          and wait_for(no_pending, 5) and soft(u, f"request {rid} withdrawn"), (rc, out[-300:], err[-300:], took, cancel_recs()[-1:]))
    r = client("approve", rid or "00000000")
    check("...approving it afterwards finds nothing to approve", r.returncode != 0 and "no request" in (r.stdout + r.stderr), r.stdout + r.stderr)

    proc = background(*fill)
    rid = nreq()
    proc.kill()                                          # SIGKILL: no chance to withdraw it; the connection just ends
    proc.wait(10)
    check("R6-B: a client killed outright (SIGKILL) while its question waits: the unlock process sees the connection end "
          "and withdraws the question (recorded as cancelled), rather than waiting for an answer that has nobody to act for",
          bool(wait_for(lambda: cancel_recs(rid), 6)) and wait_for(no_pending, 5), (rid, cancel_recs()[-1:]))

    flag("slow-value-next")
    for f_ in ("slow-started", "slow-done"):
        flag(f_, on=False)
    n = len(calls())
    proc = background("fill", "web-sess", "--vm", "avm", "--field", "password")
    started = wait_for(lambda: os.path.exists(os.path.join(stubdir, "slow-started")), 15)
    t0 = time.time()
    proc.send_signal(signal.SIGTERM)
    try:
        rc, out, err = finish(proc, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc, out, err = 999, "", "no exit"
    took = time.time() - t0
    time.sleep(7)                                        # past the step's own 6 s: had it lived, it would have finished
    check("R6-B: a fill step in progress (its ssh to the VM) when the requester withdraws: its process group is killed at once, "
          "so the step never completes, and the answer says it was stopped", bool(started) and rc == 4 and took < 5
          and "went away or withdrew" in js(out).get("reason", "") and not os.path.exists(os.path.join(stubdir, "slow-done"))
          and cancel_recs(), (started, rc, out[-300:], took, os.path.exists(os.path.join(stubdir, "slow-done"))))
    flag("slow-value-next", on=False)

    flag("slow-value-next")
    for f_ in ("slow-started", "slow-done"):
        flag(f_, on=False)
    n = len(calls())
    proc = background("login", "web-sess", "--vm", "avm", "--password-selector", "#pw", "--username-selector", "#user")
    started = wait_for(lambda: os.path.exists(os.path.join(stubdir, "slow-started")), 15)
    proc.send_signal(signal.SIGTERM)
    try:
        rc, out, err = finish(proc, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc, out, err = 999, "", "no exit"
    time.sleep(7)
    later = [c for c in calls()[n:] if c.get("kind") == "fill:fill"]
    check("R6-B: a sign-in withdrawn while its username is being typed: that step is ended and the password is never sent "
          "(no further step after a withdrawal)", bool(started) and rc == 4 and not later
          and not os.path.exists(os.path.join(stubdir, "slow-done")), (started, rc, out[-300:], later))
    flag("slow-value-next", on=False)

    sh_out = os.path.join(td, "wrapped.out")
    wrapper = subprocess.Popen(["/bin/sh", "-c", f"{shlex.join([BIN, *fill])} >{shlex.quote(sh_out)} 2>&1; echo done"],
                               env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(wrapper)
    rid = nreq()
    wrapper.kill()                                       # the wrapper (the program that ran the client) is killed outright
    wrapper.wait(10)
    check("R6-B: killing the wrapper that ran the client (SIGKILL; the client itself lives on) withdraws the request too: "
          "the client sees its parent gone, and the unlock process withdraws the question", bool(wait_for(lambda: cancel_recs(rid), 8))
          and wait_for(no_pending, 5), (rid, open(sh_out).read()[-300:] if os.path.exists(sh_out) else ""))

    sleeper = subprocess.Popen(["/bin/sleep", "60"])
    procs.append(sleeper)
    proc = background(*fill, "--while-pid", str(sleeper.pid))
    rid = nreq(5)
    sleeper.kill()
    sleeper.wait()
    try:
        rc, out, err = finish(proc, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc, out, err = 999, "", "no exit"
    check("R6-B: --while-pid PID (the hub passes its own): the request is withdrawn when that process ends, and confirmed",
          rid is not None and rc == 4 and cancel_recs(rid) and wait_for(no_pending, 5), (rid, rc, out[-200:], err[-300:]))

    proc = background(*fill, extra={"MCOMP_VAULT_TEST_CLIENT_TIMEOUT": "2"})
    rid = nreq()
    try:
        rc, out, err = finish(proc, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc, out, err = 999, "", "no exit"
    check("R6-B: the client's own deadline (300 s; 2 s in this test build) withdraws the request instead of leaving it to run "
          "on: the unlock process stops it and confirms before the client exits", rc == 4 and cancel_recs(rid)
          and "went away or withdrew" in js(out).get("reason", "") and wait_for(no_pending, 5), (rc, out[-200:], err[-300:]))

    # settle: answered only once everything the unlock process got before it has finished
    first = background(*fill)
    rid_a = nreq()
    second = background("fill", "web-sess", "--vm", "avm", "--field", "password")   # queued behind the question
    time.sleep(1.0)
    second.kill()                                        # its requester gone while it waits in the queue
    second.wait(10)
    n = len(calls())
    st = subprocess.Popen([BIN, "settle", "--timeout", "60"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, stdin=subprocess.DEVNULL)
    time.sleep(1.5)
    waited = st.poll() is None
    client("deny", rid_a or "00000000")
    try:
        s_out, s_err = st.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        st.kill()
        s_out, s_err = "", "no answer"
    finish(first)
    withdrawn = [x for x in audit_recs() if "before it started" in x.get("reason", "")]
    check("R6-B: settle waits while a question is open, and answers once everything before it has finished; a request whose "
          "requester went away while it waited in the queue never starts (recorded as withdrawn before it started)",
          waited and st.returncode == 0 and js(s_out).get("settled") is True and js(s_out).get("unlock") == "running"
          and withdrawn and not [c for c in calls()[n:] if c.get("kind", "").startswith("fill:")], (waited, s_out, s_err[-200:], withdrawn[-1:]))
    lock(u)
    r = client("settle", "--timeout", "5")
    check("settle with no unlock process: settled (nothing can be running), exit 0", r.returncode == 0 and js(r.stdout).get("unlock") == "none",
          r.stdout + r.stderr)

    # --- every output ----------------------------------------------------------------------------------------
    text = open(alog).read()
    check("the audit log holds no values and no unmasked username", not any(v in text for v in VALUES + [USERNAME]))
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no value in the output of any of {len(outputs)} commands", not leaked, leaked)


if __name__ == "__main__":
    sys.exit(main())
