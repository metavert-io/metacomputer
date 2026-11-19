#!/usr/bin/env python3
"""Offline checks for renewable API tokens and the per-secret answer time limit (vault/Proxy.swift,
vault/Server.swift, `metacomputer secrets token renew`, `set|policy --answer-timeout`).

No VM needed; the same approach as tests/test_vault_ops.py, whose helpers this reuses: a TEST build of
mcomp-vault, an ssh stub standing in for the VM's proxy account, a local plain-HTTP API behind the proxy,
a throwaway vault in a temporary folder (MCOMP_VAULT_DIR), every passphrase, key and answer typed into a
pseudo-terminal, every value a dummy. Covered:
  - `token renew ID`: the same token in the VM keeps working past its first expiry; `tokens` counts renewals;
  - an expired, revoked or unknown token isn't renewed; a shorter --ttl never shortens a token;
  - approval: an `each` secret's renewal asks again (no -> unchanged, yes -> renewed), `NAME session` covers
    the next renewal from that Claude Code session; a `session` secret doesn't ask, unless the unlock says
    --confirm each; --project applies to whoever renews; a required audit sink gets the renewal's record first;
  - revocation during a renewal's question: `revoke` answers at once, the renewal is then refused;
  - an answer in flight ends with its token (revoked, or expired without a renewal); a renewal keeps it going;
  - the longest life (MCOMP_VAULT_TEST_TOKEN_MAX_LIFETIME here) and the unlock's end are never passed;
  - `set|policy --answer-timeout`: its range, `list`, and the proxy honouring it per secret;
  - `renew --all`, and `metacomputer secrets token renew` (the action log's kind);
  - the audit records of renewals hold only the vault's own words and numbers; no value or token anywhere.

Run: python3 tests/test_vault_tokens.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build python3 tests/test_vault_tokens.py   (skip building)
"""
import http.client
import json
import os
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
PASS = "dummy passphrase for tokens 58"
# Invented keys, put together here so no line looks like a real credential to a secret scanner.
KEY1, KEY2, KEY3, KEY4 = ("-".join(("dummy", "api", "key", w, "not", "real")) for w in ("renew", "each", "long", "proj"))
VALUES = [PASS, KEY1, KEY2, KEY3, KEY4]
outputs = []
RENEW_FIELDS = {"id", "seq", "ts", "prev", "hash", "mac", "op", "result", "name", "vm", "token_id", "ttl_s", "approval",
                "requester", "reason", "request_id", "requester_left"}


class Up(tvo.Upstream):
    """The API behind the proxy, plus /v1/drip: an answer that streams a line every 0.25 s for 10 s."""

    def handle_any(self):
        if self.path.startswith("/v1/drip"):
            self.server.got.append({"method": self.command, "path": self.path})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()           # HTTP/1.0: the body ends when the connection does
            try:
                for i in range(40):
                    self.wfile.write(b"tick %d\n" % i)
                    self.wfile.flush()
                    time.sleep(0.25)
                self.wfile.write(b"DRIP-END\n")
                self.wfile.flush()
            except OSError:
                pass
            return
        super().handle_any()

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


def main():
    td = tempfile.mkdtemp(prefix="mcvt-", dir="/tmp")
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
    return 0 if ts.results and all(ts.results) else 1


def run(td, procs):
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []

    # --- a stand-in VM: keys, the gateway's listener (this process), the ssh stub, a stand-in Claude Code ---------
    home, vdir, sshdir = os.path.join(td, "home"), os.path.join(td, "v"), os.path.join(td, "ssh")
    spool = os.path.join(td, "spool")
    os.makedirs(home)
    os.makedirs(sshdir, mode=0o700)
    os.makedirs(spool, mode=0o700)
    for k in ("ops", "fill", "proxy", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    os.remove(os.path.join(sshdir, "hostkey"))
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
    fake = os.path.join(td, "fake", "claude")
    os.makedirs(os.path.dirname(fake))
    open(fake + ".c", "w").write(tvo.FAKE_CLAUDE_C)
    r = subprocess.run(["cc", "-o", fake, fake + ".c"], capture_output=True, text=True)
    check("a stand-in process named claude builds", r.returncode == 0, r.stderr)
    proj1, proj2 = os.path.realpath(os.path.join(td, "proj1")), os.path.realpath(os.path.join(td, "proj2"))
    os.makedirs(proj1)
    os.makedirs(proj2)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
           "MCOMP_VAULT_TEST_SSH": stub}
    sock = os.path.join(vdir, "vault.sock")
    alog = os.path.join(vdir, "audit.jsonl")

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    def client(*args, extra=None, timeout=120):
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=dict(env, **(extra or {})),
                           stdin=subprocess.DEVNULL, timeout=timeout)
        outputs.append(r.stdout + r.stderr)
        return r

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
        except AssertionError:          # it didn't ask what it should have: a failed check, not the end of the run
            rc = 999
            try:
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
        finally:
            p.close()
        outputs.append(p.text())
        return rc, p.text()

    counter = [0]

    def detached_start(*args):
        """The client with no Claude Code process among its ancestors, in the background."""
        counter[0] += 1
        out = os.path.join(td, f"det{counter[0]}.out")
        script = f"({shlex.join([BIN, *args])} >{shlex.quote(out)} 2>&1; echo $? >{shlex.quote(out + '.rc')}) &"
        subprocess.run(["/bin/sh", "-c", script], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return out

    def detached_wait(out, timeout=60):
        tvo.wait_for(lambda: os.path.exists(out + ".rc") and open(out + ".rc").read().strip(), timeout)
        text = open(out).read() if os.path.exists(out) else ""
        outputs.append(text)
        rc = open(out + ".rc").read().strip() if os.path.exists(out + ".rc") else "999"
        return int(rc or 999), text

    def as_claude(project, *commands):
        """Client commands one after another under one stand-in Claude Code process in `project`."""
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

    def seen(u, text, timeout=20):
        try:
            u.expect(text, timeout)
            return True
        except AssertionError:
            return False

    T = {"MCOMP_VAULT_TEST_LISTENERS": my_name, "MCOMP_VAULT_TEST_PROMPT_TIMEOUT": "8", "MCOMP_VAULT_TEST_API_HTTP": "1",
         "MCOMP_VAULT_TEST_ANSWER_TIMEOUT": "3", "MCOMP_EVENTS_SPOOL": spool}

    unlocks = []

    def start_unlock(args=(), extra=None):
        p = ts.Pty([BIN, "unlock", *args, *allow], dict(env, **T, **(extra or {})))
        procs.append(p)
        unlocks.append(p)
        p.expect("Vault passphrase: ")
        p.send(PASS + "\n")
        p.expect("unlocked until")
        tvo.wait_for(lambda: os.path.exists(sock), 5, 0.05)
        return p

    def lock(u):
        client("lock")
        tvo.wait_for(lambda: not os.path.exists(sock), 15, 0.1)
        try:
            u.wait(15)
        except AssertionError:
            pass

    mport = [9]

    def proxy_port():
        tun = [c for c in calls() if c.get("kind") == "proxy:tunnel"]
        fwd = tun[-1]["argv"][tun[-1]["argv"].index("-R") + 1] if tun else ""
        mport[0] = int(fwd.rsplit(":", 1)[1]) if fwd else 9
        return mport[0]

    def vm_request(method, path, headers=None, body=None, timeout=30):
        """What a program in the VM does: a request to the proxy, with a token."""
        c = http.client.HTTPConnection("127.0.0.1", mport[0], timeout=timeout)
        try:
            c.request(method, path, body=body, headers=headers or {})
            resp = c.getresponse()
            data = resp.read().decode(errors="replace")
            return resp.status, data
        except (OSError, http.client.HTTPException) as e:
            return 0, repr(e)
        finally:
            c.close()

    def bearer(tok):
        return {"Authorization": "Bearer " + tok.get("token", "x")}

    def stream(path, tok, holder, key):
        """A long answer read to its end (or until it's cut): (seconds, bytes)."""
        t1, got = time.time(), b""
        try:
            s = socket.create_connection(("127.0.0.1", mport[0]), timeout=30)
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {tok.get('token', 'x')}\r\n\r\n".encode())
            while True:
                d = s.recv(4096)
                if not d:
                    break
                got += d
            s.close()
        except OSError:
            pass
        holder[key] = (round(time.time() - t1, 1), got)

    def tokens_json():
        r = client("tokens", "--json")
        try:
            return {t["id"]: t for t in json.loads(r.stdout).get("tokens", [])}
        except ValueError:
            return {}

    def renew(*args, extra=None):
        r = client("token", "renew", *args, "--vm", "opsvm", extra=extra)
        return r.returncode, js(r.stdout), r

    # --- the vault, its secrets, the VM ----------------------------------------------------------------------
    up = tvo.http_server(Up)
    upstream = f"127.0.0.1:{up.server_address[1]}"
    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    check("init a throwaway vault (passphrase only) in a temporary folder", rc == 0 and vdir.startswith(td), out[-300:])
    api = ["--kind", "api", "--api-host", upstream]
    rc, out = tty([BIN, "set", "api1", *api, "--api-path-prefix", "/v1"], [("Vault passphrase", PASS + "\n"), ("API key for api1", KEY1 + "\n")])
    check("set api1 (approval session)", rc == 0, out[-300:])
    rc, out = tty([BIN, "set", "api-each", *api, "--approval", "each"], [("Vault passphrase", PASS + "\n"), ("API key for api-each", KEY2 + "\n")])
    check("set api-each (approval each)", rc == 0, out[-300:])
    for t, why in (("2h", "at most 1h"), ("5s", "at least 10s"), ("soon", "like 90s")):
        r = client("set", "api-long", *api, "--answer-timeout", t)
        check(f"set --answer-timeout {t}: refused before anything is asked ({why})", r.returncode == 64 and why in r.stderr, r.stderr)
    r = client("set", "pw", "--kind", "password", "--origin", ts.SITE, "--answer-timeout", "30m")
    check("set --answer-timeout on a password secret: refused (api secrets only)", r.returncode == 64 and "go with --kind api" in r.stderr,
          r.stderr)
    rc, out = tty([BIN, "set", "api-long", *api, "--answer-timeout", "30s"], [("Vault passphrase", PASS + "\n"), ("API key for api-long", KEY3 + "\n")])
    check("set api-long --answer-timeout 30s: saved, and said", rc == 0 and "each answer within 30 s" in out, out[-300:])
    rec = [x for x in audit_recs() if x.get("op") == "set" and x.get("name") == "api-long"]
    check("the audit record of set names the answer time limit (the user's own setting)", rec and rec[-1].get("answer_timeout_s") == 30, rec[-1:])
    rc, out = tty([BIN, "set", "api-proj", *api, "--project", proj1], [("Vault passphrase", PASS + "\n"), ("API key for api-proj", KEY4 + "\n")])
    check("set api-proj --project (one project only)", rc == 0, out[-300:])
    common = ["--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill", "--fill-user", "mcfill",
              "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe,
              "--proxy-key", sshdir + "/proxy", "--proxy-user", "mcproxy", "--proxy-port", "18765"]
    rc, out = tty([BIN, "bind", "opsvm", *common], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("bind the stand-in VM with its proxy account", rc == 0, out[-300:])

    # --- 1. renewing: the same token keeps working ---------------------------------------------------------------
    u = start_unlock(["--for", "6h"])            # longer than every renewal asked for below
    r = client("list", "--json")
    lst = {s["name"]: s for s in json.loads(r.stdout or "{}").get("secrets", [])} if r.returncode == 0 else {}
    check("list: each api secret's answer time limit (its own, else the default: 3 s in this test build)",
          lst.get("api-long", {}).get("answer_timeout_s") == 30 and lst.get("api1", {}).get("answer_timeout_s") == 3, r.stdout[-400:])
    tok = js(client("token", "api1", "--vm", "opsvm", "--ttl", "3s").stdout)
    proxy_port()
    st0, _ = vm_request("GET", "/api1/v1/echo", bearer(tok))
    rc, res, r = renew(tok.get("token_id", "00000000"), "--ttl", "20s")
    check("token renew ID: ok, the expiry moved (about 20 s from now), one renewal; the answer carries no token",
          st0 == 200 and rc == 0 and res.get("extended") is True and 15 <= res.get("expires_in_s", 0) <= 20
          and res.get("renewals") == 1 and res.get("token_id") == tok.get("token_id") and "token" not in res
          and tok.get("token", "x") not in r.stdout, (st0, rc, r.stdout, r.stderr))
    time.sleep(4)
    st, data = vm_request("GET", "/api1/v1/echo", bearer(tok))
    check("past its first expiry, the very same token still works in the VM (no new token, nothing to restart)",
          st == 200 and up.got[-1].get("headers", {}).get("authorization") == "Bearer " + KEY1, (st, data[:200]))
    tj = tokens_json().get(tok.get("token_id"), {})
    check("tokens lists its renewals and the latest it may ever live until", tj.get("renewals") == 1 and tj.get("longest_until"), tj)
    rec = [x for x in audit_recs() if x.get("op") == "renew"]
    check("the renewal's audit record: op renew, the token's id, its new lifetime in the vault's own seconds, the approval, "
          "the requester; nothing the VM chose", rec and rec[-1].get("result") == "ok" and rec[-1].get("token_id") == tok.get("token_id")
          and isinstance(rec[-1].get("ttl_s"), int) and rec[-1].get("approval") == "session" and "requester" in rec[-1]
          and set(rec[-1]) <= RENEW_FIELDS and "mac" in rec[-1], rec[-1:])
    rc, res, r = renew(tok.get("token_id", "00000000"), "--ttl", "5s")
    tj2 = tokens_json().get(tok.get("token_id"), {})
    check("a renewal asking for less than the token has left changes nothing (never shortened), and asks nothing",
          rc == 0 and res.get("extended") is False and tj2.get("expires") == tj.get("expires"), (r.stdout, tj2))
    rc, res, r = renew("0badc0de")
    check("an unknown token id: refused (exit 2), get a new one", rc == 2 and "no live token" in res.get("reason", ""), r.stdout)
    tokE = js(client("token", "api1", "--vm", "opsvm", "--ttl", "2s").stdout)
    time.sleep(3)
    rc, res, r = renew(tokE.get("token_id", "00000000"))
    check("an expired token isn't renewed (exit 2): it stays ended", rc == 2 and "can't be renewed" in res.get("reason", ""), r.stdout)
    tokR = js(client("token", "api1", "--vm", "opsvm").stdout)
    client("revoke", tokR.get("token_id", "00000000"))
    rc, res, r = renew(tokR.get("token_id", "00000000"))
    st, _ = vm_request("GET", "/api1/v1/echo", bearer(tokR))
    check("a revoked token isn't renewed (exit 2), and stays refused in the VM (401)", rc == 2 and st == 401, (r.stdout, st))
    r = client("token", "renew", "--vm", "opsvm")
    check("token renew with neither an id nor --all is a token for a secret named renew (none here): no renewal happens",
          r.returncode != 0 and "renew" in r.stdout + r.stderr, r.stdout + r.stderr)

    # --- 2. approval: an `each` secret asks again; `session` grants; revocation wins ------------------------------
    h = detached_start("token", "api-each", "--vm", "opsvm", "--ttl", "10m")
    asked = seen(u, "Type api-each to allow")
    u.send("api-each\n")
    rc, out = detached_wait(h)
    tokA = js(out)
    exp0 = tokens_json().get(tokA.get("token_id"), {}).get("expires")
    h = detached_start("token", "renew", tokA.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "2h")
    q = seen(u, f"Keep token {tokA.get('token_id')} for api-each") and seen(u, "Type api-each to allow")
    u.send("no\n")
    rc_no, out_no = detached_wait(h)
    exp1 = tokens_json().get(tokA.get("token_id"), {}).get("expires")
    check("an `each` secret: its renewal asks again, naming the token, the secret and how long; 'no' refuses it (exit 2) and "
          "the token keeps its expiry", asked and rc == 0 and q and rc_no == 2 and exp0 and exp1 == exp0, (rc, q, rc_no, out_no, exp0, exp1))
    h = detached_start("token", "renew", tokA.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "2h")
    q = seen(u, "Type api-each to allow")
    u.send("api-each\n")
    rc_yes, out_yes = detached_wait(h)
    res = js(out_yes)
    rec = [x for x in audit_recs() if x.get("op") == "renew"]
    check("...and allowed, it's renewed: the audit log has the request, the approval and the renewal (each:yes)",
          q and rc_yes == 0 and res.get("extended") is True and res.get("expires_in_s", 0) > 7000
          and rec[-1].get("approval") == "each:yes" and rec[-1].get("request_id")
          and any(x.get("op") == "authorize" and x.get("use") == "renew" for x in audit_recs()), (rc_yes, out_yes, rec[-1:]))
    n_q = u.text().count("Type api-each to allow")
    p, outs = as_claude(proj1, ["token", "renew", tokA.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "3h"],
                        ["token", "renew", tokA.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "4h"])
    q = seen(u, "Type api-each to allow")
    u.send("api-each session\n")
    res2 = claude_results(p, outs)
    rec = [x for x in audit_recs() if x.get("op") == "renew"][-2:]
    check("'api-each session' at a renewal: the next renewal from that Claude Code session doesn't ask (grant:session)",
          q and [x[0] for x in res2] == [0, 0] and u.text().count("Type api-each to allow") == n_q + 1
          and [x.get("approval") for x in rec] == ["each:session", "grant:session"], (res2, rec))

    # Revocation during a renewal's question: revoke is answered at once (it doesn't wait behind the question), and the
    # renewal, once allowed, finds its token gone.
    tokZ = js(client("token", "api1", "--vm", "opsvm", "--ttl", "10m").stdout)
    h = detached_start("token", "api-each", "--vm", "opsvm", "--ttl", "10m")
    q = seen(u, "Type api-each to allow")
    t0 = time.time()
    rv = client("revoke", tokZ.get("token_id", "00000000"), timeout=30)
    took = time.time() - t0
    stZ, _ = vm_request("GET", "/api1/v1/echo", bearer(tokZ))
    seen(u, "(still waiting) Type api-each to allow")   # the revoke's line moved the question down: asked again below it
    u.send("no\n")
    detached_wait(h)
    check("revoke while any question waits (here a new token's) is answered at once, and the token is refused at once (401)",
          q and rv.returncode == 0 and "Revoked" in rv.stdout and took < 3 and stZ == 401, (q, rv.stdout, round(took, 1), stZ))
    tokB = js(client("token", "api1", "--vm", "opsvm", "--ttl", "10m").stdout)
    h0 = detached_start("token", "api-each", "--vm", "opsvm", "--ttl", "10m")
    seen(u, "Type api-each to allow")
    u.send("api-each\n")
    rc, out = detached_wait(h0)
    tokC = js(out)
    h = detached_start("token", "renew", tokC.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "2h")
    q = seen(u, f"Keep token {tokC.get('token_id')}")
    t0 = time.time()
    rv = client("revoke", tokC.get("token_id", "00000000"), timeout=30)
    took = time.time() - t0
    st, _ = vm_request("GET", "/api-each/x", bearer(tokC))
    seen(u, "(still waiting) Type api-each to allow")
    u.send("api-each\n")
    rc, out = detached_wait(h)
    stB, _ = vm_request("GET", "/api1/v1/echo", bearer(tokB))
    check("revoke while a renewal's question waits: answered at once (not after the question), the token refused in the VM "
          "at once (401)", q and rv.returncode == 0 and "Revoked" in rv.stdout and took < 3 and st == 401, (q, rv.stdout, round(took, 1), st))
    check("...and the renewal, allowed afterwards, is refused: the revoke wins (exit 2); other tokens are untouched",
          rc == 2 and "revoked before it could be renewed" in js(out).get("reason", "") and stB == 200, (rc, out, stB))

    # --project applies to whoever renews.
    p, outs = as_claude(proj1, ["token", "api-proj", "--vm", "opsvm", "--ttl", "10m"])
    tokP = js(claude_results(p, outs)[0][1])
    p, outs = as_claude(proj2, ["token", "renew", tokP.get("token_id", "00000000"), "--vm", "opsvm"])
    rp2 = claude_results(p, outs)[0]
    p, outs = as_claude(proj1, ["token", "renew", tokP.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "1h"])
    rp1 = claude_results(p, outs)[0]
    check("--project: a renewal from a Claude Code session in another project is refused; from the secret's project it's renewed",
          tokP.get("token_id") and rp2[0] == 2 and "limited to Claude Code sessions in" in js(rp2[1]).get("reason", "") and rp1[0] == 0,
          (tokP, rp2, rp1))

    # --- 3. answers in flight end with their token; a renewal keeps them going -------------------------------------
    tokD = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "10m").stdout)
    hold = {}
    th = threading.Thread(target=stream, args=("/api-long/v1/drip", tokD, hold, "d"))
    th.start()
    time.sleep(1.5)
    client("revoke", tokD.get("token_id", "00000000"))
    th.join(30)
    dt, dgot = hold.get("d", (0, b""))
    check("revoke cuts an answer in flight at once (it doesn't stream on to its end)", b"tick 1" in dgot and b"DRIP-END" not in dgot
          and dt < 4, (dt, dgot[-60:]))
    tokX = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "3s").stdout)
    tokY = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "4s").stdout)
    thx = threading.Thread(target=stream, args=("/api-long/v1/drip", tokX, hold, "x"))
    thy = threading.Thread(target=stream, args=("/api-long/v1/drip", tokY, hold, "y"))
    thx.start()
    thy.start()
    time.sleep(1.5)
    rc, res, r = renew(tokY.get("token_id", "00000000"), "--ttl", "1m")
    thx.join(30)
    thy.join(30)
    xt, xgot = hold.get("x", (0, b""))
    yt, ygot = hold.get("y", (0, b""))
    check("a token that expires without a renewal ends its answer in flight there (about 3 s, not the 10 s stream)",
          b"tick 1" in xgot and b"DRIP-END" not in xgot and xt < 6, (xt, xgot[-60:]))
    check("...while one renewed in time streams to the end, past its first expiry (and past the default 3 s answer limit: "
          "api-long's own is 30 s)", rc == 0 and b"DRIP-END" in ygot and yt >= 9, (rc, yt, ygot[-60:]))
    rec = [x for x in audit_recs() if x.get("op") == "api" and x.get("token_id") == tokX.get("token_id")]
    check("the cut answer's audit record says why in the vault's own words", rec and "the token ended during the answer" in rec[-1].get("reason", ""),
          rec[-1:])
    tokL = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "10m").stdout)
    hold = {}
    th1 = threading.Thread(target=stream, args=("/api-long/v1/late", tokL, hold, "long"))
    th2 = threading.Thread(target=stream, args=("/api1/v1/late", tokB, hold, "short"))
    th1.start()
    th2.start()
    th1.join(30)
    th2.join(30)
    lt, lgot = hold.get("long", (0, b""))
    st_, sgot = hold.get("short", (0, b""))
    check("--answer-timeout is per secret: a 5 s answer passes whole for api-long (30 s), and is cut near 3 s for api1 (the default here)",
          b"LATE-DATA" in lgot and b"LATE-DATA" not in sgot and b"first " in sgot and st_ < 5, (lt, lgot[-40:], st_, sgot[-40:]))
    rc, out = tty([BIN, "policy", "api1", "--answer-timeout", "20s"], [("Vault passphrase", PASS + "\n")])
    lst1 = {s["name"]: s for s in json.loads(client("list", "--json").stdout or "{}").get("secrets", [])}
    rc2, out2 = tty([BIN, "policy", "api1", "--answer-timeout", "default"], [("Vault passphrase", PASS + "\n")])
    lst2 = {s["name"]: s for s in json.loads(client("list", "--json").stdout or "{}").get("secrets", [])}
    r3 = client("policy", "api-each", "--answer-timeout", "2h")
    check("policy NAME --answer-timeout D|default changes it without typing the key again (range checked)",
          rc == 0 and "up to 20 s" in out and lst1.get("api1", {}).get("answer_timeout_s") == 20 and rc2 == 0
          and lst2.get("api1", {}).get("answer_timeout_s") == 3 and r3.returncode == 64, (out[-200:], out2[-200:], r3.stderr))
    rc, out = tty([BIN, "policy", "api-each", "--approval", "each"], [("Vault passphrase", PASS + "\n")])
    check("policy NAME --approval still works on its own", rc == 0 and "asks first" in out, out[-200:])

    # --- 4. renew --all, a required audit sink, the wrapper -------------------------------------------------------
    for t in (tokA, tokP):              # an `each` secret's (it would ask) and a project-limited one's (not from here)
        client("revoke", t.get("token_id", "00000000"))
    live = [t for t in tokens_json().values() if t.get("expires_in_s", 0) > 20]
    n_ren = len([x for x in audit_recs() if x.get("op") == "renew"])
    rc, res, r = renew("--all", "--ttl", "30m")
    ids = {x.get("token_id") for x in res.get("renewed", [])}
    check("token renew --all: every live token on the VM is renewed, one audit record each",
          rc == 0 and len(live) >= 2 and {t["id"] for t in live} <= ids and not res.get("not_renewed")
          and len([x for x in audit_recs() if x.get("op") == "renew"]) - n_ren == len(ids), (r.stdout, live))
    rx = tvo.http_server(tvo.Receiver)
    rc, out = tty([BIN, "audit-sink", "add", "req", "--kind", "webhook", "--url", f"http://127.0.0.1:{rx.server_address[1]}/req", "--required"],
                  [("Vault passphrase", PASS + "\n")])
    check("a required audit sink (webhook)", rc == 0, out[-300:])
    rx.down.add("/req")
    rc, res, r = renew(tokB.get("token_id", "00000000"), "--ttl", "1h")
    rx.down.discard("/req")
    rc2, res2, r2 = renew(tokB.get("token_id", "00000000"), "--ttl", "2h")
    begins = [json.loads(x["body"]).get("data", {}) for x in rx.got if x["path"].startswith("/req")]
    begins = [b for b in begins if b.get("op") == "begin" and b.get("use") == "renew"]
    check("with a required audit sink, a renewal is refused while the sink is down (exit 2), and goes ahead once its own "
          "begin record has reached the sink", rc == 2 and "required audit sink" in res.get("reason", "") and rc2 == 0
          and begins and begins[-1].get("token_id") == tokB.get("token_id"), (r.stdout, r2.stdout, begins[-1:]))
    rc, out = tty([BIN, "audit-sink", "remove", "req"], [("Vault passphrase", PASS + "\n")])
    conf = os.path.join(td, "ops-vm.env")
    open(conf, "w").write(f'VM_NAME=opsvm\nSSH_PORT={port}\nSSH_DIR="{sshdir}"\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n'
                          f'ACTION_LOG=on\nACTION_LOG_KEY="{sshdir}/action-log"\nGUEST_USER=cua\n')
    wrapper = {"MCOMP_CONFIG": conf, "MCOMP_VAULT_BIN": BIN, "MCOMP_VAULT_ALLOW_TEST_BUILD": "1", "MCOMP_EVENTS_SPOOL": spool,
               "MCOMP_EVENTS_DIR": os.path.join(td, "events-cfg")}
    mc = os.path.join(LAB, "bin/metacomputer")
    r = subprocess.run([mc, "secrets", "token", "renew", tokB.get("token_id", "00000000"), "--ttl", "3h"], capture_output=True,
                       text=True, env=dict(env, **wrapper), timeout=120, stdin=subprocess.DEVNULL)
    outputs.append(r.stdout + r.stderr)
    adir = os.path.join(td, "logs", "actions")
    afiles = [os.path.join(d, f) for d, _, fs in os.walk(adir) for f in fs if f.endswith(".jsonl")]
    arecs = [json.loads(l) for f in afiles for l in open(f)]
    check("metacomputer secrets token renew ID (this VM filled in): renewed, and the signed action log records it as "
          "secrets_token_renew with the token's id", r.returncode == 0 and js(r.stdout).get("extended") is True
          and any(x.get("type") == "secrets_token_renew" and x.get("token_id") == tokB.get("token_id") for x in arecs),
          (r.stdout + r.stderr)[-400:])
    lock(u)
    st, _ = vm_request("GET", "/api1/v1/echo", bearer(tokB), timeout=5)
    rc, res, r = renew(tokB.get("token_id", "00000000"))
    check("lock ends every token, renewed or not (the proxy is gone), and a renewal while locked says so (exit 3)",
          st != 200 and rc == 3, (st, r.stdout, r.stderr))

    # --- 5. the longest life: never past its limit after issue (8 s in this test build), never past the unlock -------
    u = start_unlock(["--for", "20m"], {"MCOMP_VAULT_TEST_TOKEN_MAX_LIFETIME": "8"})
    tokM = js(client("token", "api1", "--vm", "opsvm", "--ttl", "3s").stdout)
    proxy_port()
    rc, res, r = renew(tokM.get("token_id", "00000000"), "--ttl", "1h")
    rc2, res2, r2 = renew(tokM.get("token_id", "00000000"), "--ttl", "1h")
    check("renewing past the longest life: it reaches its limit (8 s after it was made here), no further; the next renewal "
          "is refused and says why", rc == 0 and res.get("extended") is True and res.get("expires_in_s", 99) <= 8 and rc2 == 2
          and "can't live any longer" in res2.get("reason", "") and "after it was made" in res2.get("reason", ""), (r.stdout, r2.stdout))
    time.sleep(5)
    st1, _ = vm_request("GET", "/api1/v1/echo", bearer(tokM))
    time.sleep(4)
    st2, _ = vm_request("GET", "/api1/v1/echo", bearer(tokM))
    check("...the token works past its 3 s and stops at its limit (401)", st1 == 200 and st2 == 401, (st1, st2))
    lock(u)
    u = start_unlock(["--for", "15s", "--confirm", "each"])
    h = detached_start("token", "api1", "--vm", "opsvm", "--ttl", "5s")
    seen(u, "Type api1 to allow")
    u.send("api1\n")
    rc, out = detached_wait(h)
    tokU = js(out)
    proxy_port()
    h = detached_start("token", "renew", tokU.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "1h")
    q = seen(u, f"Keep token {tokU.get('token_id')}") and seen(u, "Type api1 to allow")
    u.send("api1\n")
    rc, out = detached_wait(h)
    res = js(out)
    rc2, res2, r2 = renew(tokU.get("token_id", "00000000"), "--ttl", "1h")
    check("unlock --confirm each: a `session` secret's renewal asks too; it never reaches past the unlock's end, and a "
          "renewal with nothing left to give is refused, naming the unlock", q and rc == 0 and res.get("expires_in_s", 99) <= 15
          and rc2 == 2 and "unlock ends" in res2.get("reason", ""), (q, out, r2.stdout))
    lock(u)

    # --- the guarantees that hold throughout -------------------------------------------------------------------
    recs = audit_recs()
    ren = [x for x in recs if x.get("op") == "renew"]
    check(f"every renewal record ({len(ren)}) holds only the vault's own fields: no request count, path, size or VM text",
          ren and all(set(x) <= RENEW_FIELDS for x in ren) and all(isinstance(x.get("ttl_s", 0), int) for x in ren),
          [sorted(set(x) - RENEW_FIELDS) for x in ren if not set(x) <= RENEW_FIELDS][:3])
    toks = [t.get("token") for t in (tok, tokE, tokR, tokA, tokB, tokC, tokP, tokD, tokX, tokY, tokL, tokM, tokU) if t.get("token")]
    raw = open(alog).read()
    term = "".join(p.text() for p in unlocks)
    check("the audit log holds no key and no token", not any(v in raw for v in VALUES) and not any(t in raw for t in toks))
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no key or passphrase in the output of any of {len(outputs)} commands", not leaked, leaked)
    check("the unlock terminal never showed a key or a token", not any(v in term for v in VALUES) and not any(t in term for t in toks))
    listener.close()


if __name__ == "__main__":
    sys.exit(main())
