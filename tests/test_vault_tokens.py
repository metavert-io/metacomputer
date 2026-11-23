#!/usr/bin/env python3
"""Offline checks for renewable API tokens and the per-secret answer time limit (vault/Proxy.swift,
vault/Server.swift, `metacomputer secrets token renew`, `set|policy --answer-timeout`).

No VM needed; the same approach as tests/test_vault_ops.py, whose helpers this reuses: a TEST build of
mcomp-vault, an ssh stub standing in for the VM's proxy account, a local plain-HTTP API behind the proxy,
a throwaway vault in a temporary folder (MCOMP_VAULT_DIR), every passphrase, key and answer typed into a
pseudo-terminal, every value a dummy. Covered:
  - `token renew ID`: the same token in the VM keeps working past its first expiry; `tokens` counts renewals;
  - an expired, revoked or unknown token isn't renewed; a shorter --ttl never shortens a token;
  - who renews: the token's own Claude Code session and you at a terminal (the client in a pseudo-terminal of its
    own, started by an orphaned helper, so no Claude Code process is among its parents whoever runs this test)
    follow the secret's policy; another session or a background program is asked about, even for `session`;
  - approval: an `each` secret's renewal asks again (no -> unchanged, yes -> renewed), `NAME session` covers
    the next renewal from that Claude Code session; a `session` secret doesn't ask, unless the unlock says
    --confirm each; --project applies to whoever renews; a required audit sink gets the renewal's record first;
    a renewal answered late gets the expiry its question named;
  - revocation during a renewal's question: `revoke` answers at once, the renewal is then refused; `lock` during
    a `renew --all` batch stops it and ends every token at once; a locked renewal's refusal isn't a forgery;
  - an answer in flight ends with its token (revoked, expired without a renewal, its secret replaced); a renewal
    keeps it going; a VM that stops reading can't hold one open past a revoke;
  - the longest life (MCOMP_VAULT_TEST_TOKEN_MAX_LIFETIME here) and the unlock's end are never passed;
  - a vault the unlock can't open any more (damaged, or rekeyed while a question waits) ends every token and answer
    and locks, with no other request;
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
PASS2 = "dummy passphrase for tokens two 61"
# Invented keys, put together here so no line looks like a real credential to a secret scanner.
KEY1, KEY2, KEY3, KEY4, KEY5 = ("-".join(("dummy", "api", "key", w, "not", "real")) for w in ("renew", "each", "long", "proj", "new"))
VALUES = [PASS, PASS2, KEY1, KEY2, KEY3, KEY4, KEY5]
outputs = []
RENEW_FIELDS = {"id", "seq", "ts", "prev", "hash", "mac", "op", "result", "name", "vm", "token_id", "ttl_s", "approval",
                "requester", "reason", "request_id", "requester_left", "renewer"}


class Up(tvo.Upstream):
    """The API behind the proxy, plus /v1/drip: an answer that streams a line every 0.25 s for 10 s."""

    def handle_any(self):
        if self.path.startswith("/v1/big"):         # 32 MB as fast as the proxy takes it
            self.server.got.append({"method": self.command, "path": self.path})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                chunk = b"x" * 65536
                for _ in range(512):
                    self.wfile.write(chunk)
            except OSError:
                pass
            return
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
           "MCOMP_VAULT_TEST_IGNORE_AGENT": "1",
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

    # The user at a terminal: the client in a pseudo-terminal of its own (its stdin and stderr a terminal, in the
    # terminal's foreground), started by an orphaned helper so no Claude Code process is among its parents, whoever
    # runs this test.
    term_py = os.path.join(td, "term.py")
    open(term_py, "w").write(
        "import os, pty, sys\n"
        "out, argv = sys.argv[1], sys.argv[2:]\n"
        "pid, fd = pty.fork()\n"
        "if pid == 0:\n"
        "    os.execve(argv[0], argv, os.environ)\n"
        "buf = b''\n"
        "while True:\n"
        "    try:\n"
        "        d = os.read(fd, 65536)\n"
        "    except OSError:\n"
        "        break\n"
        "    if not d:\n"
        "        break\n"
        "    buf += d\n"
        "_, st = os.waitpid(pid, 0)\n"
        "open(out, 'wb').write(buf)\n"
        "open(out + '.rc', 'w').write(str(os.waitstatus_to_exitcode(st)))\n")

    def term_start(*args, extra=None):
        counter[0] += 1
        out = os.path.join(td, f"term{counter[0]}.out")
        script = f"({shlex.join([sys.executable, '-I', term_py, out, *args])} </dev/null >/dev/null 2>&1) &"
        subprocess.run(["/bin/sh", "-c", script], env=dict(env, **(extra or {})), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out

    def term(*args, extra=None, timeout=60):
        return detached_wait(term_start(*args, extra=extra), timeout)

    def as_claude(project, *commands):
        """Client commands one after another under one stand-in Claude Code process in `project`."""
        counter[0] += 1
        outs = [os.path.join(td, f"cl{counter[0]}-{i}.out") for i in range(len(commands))]
        script = "; ".join(f"{shlex.join([BIN, *c])} >{shlex.quote(o)} 2>&1; echo $? >{shlex.quote(o + '.rc')}"
                           for c, o in zip(commands, outs))
        p = subprocess.Popen([fake, project, "/bin/sh", "-c", script], env=env, stdin=subprocess.DEVNULL)
        procs.append(p)
        return p, outs

    def as_claude_token_renew(project, token_args, renew_args):
        """One stand-in Claude Code session that gets a token and then renews it (the token's id read in between)."""
        counter[0] += 1
        o1, o2 = (os.path.join(td, f"cc{counter[0]}-{i}.out") for i in (1, 2))
        get_id = (f"{shlex.quote(sys.executable)} -I -c 'import json,sys; print(json.loads(open(sys.argv[1]).read().strip()"
                  f".splitlines()[-1]).get(\"token_id\", \"00000000\"))' {shlex.quote(o1)}")
        script = (f"{shlex.join([BIN, 'token', *token_args])} >{shlex.quote(o1)} 2>&1; echo $? >{shlex.quote(o1 + '.rc')}; "
                  f"id=$({get_id}); {shlex.join([BIN, 'token', 'renew'])} \"$id\" {shlex.join(renew_args)} "
                  f">{shlex.quote(o2)} 2>&1; echo $? >{shlex.quote(o2 + '.rc')}")
        p = subprocess.Popen([fake, project, "/bin/sh", "-c", script], env=env, stdin=subprocess.DEVNULL)
        procs.append(p)
        return p, [o1, o2]

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
        """`token renew` typed by the user at a terminal: the secret's own policy applies, whoever the token was for."""
        rc, text = term(BIN, "token", "renew", *args, "--vm", "opsvm", extra=extra)
        return rc, js(text), text

    def last_renew():
        recs = [x for x in audit_recs() if x.get("op") == "renew"]
        return recs[-1] if recs else {}

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
    rc, res, text = renew(tok.get("token_id", "00000000"), "--ttl", "20s")
    check("token renew ID (typed at a terminal): ok, the expiry moved (about 20 s from now), one renewal; the answer "
          "carries no token", st0 == 200 and rc == 0 and res.get("extended") is True and 15 <= res.get("expires_in_s", 0) <= 20
          and res.get("renewals") == 1 and res.get("token_id") == tok.get("token_id") and "token" not in res
          and tok.get("token", "x") not in text, (st0, rc, text))
    time.sleep(4)
    st, data = vm_request("GET", "/api1/v1/echo", bearer(tok))
    check("past its first expiry, the very same token still works in the VM (no new token, nothing to restart)",
          st == 200 and up.got[-1].get("headers", {}).get("authorization") == "Bearer " + KEY1, (st, data[:200]))
    tj = tokens_json().get(tok.get("token_id"), {})
    check("tokens lists its renewals and the latest it may ever live until", tj.get("renewals") == 1 and tj.get("longest_until"), tj)
    rec = last_renew()
    check("the renewal's audit record: op renew, the token's id, its new lifetime in the vault's own seconds, the approval, "
          "who renewed it (you at a terminal), the requester; nothing the VM chose", rec.get("result") == "ok"
          and rec.get("token_id") == tok.get("token_id") and isinstance(rec.get("ttl_s"), int) and rec.get("approval") == "session"
          and rec.get("renewer") == "terminal" and "requester" in rec and set(rec) <= RENEW_FIELDS and "mac" in rec, rec)
    rc, res, text = renew(tok.get("token_id", "00000000"), "--ttl", "5s")
    tj2 = tokens_json().get(tok.get("token_id"), {})
    check("a renewal asking for less than the token has left changes nothing (never shortened), and asks nothing",
          rc == 0 and res.get("extended") is False and tj2.get("expires") == tj.get("expires"), (text, tj2))
    rc, res, text = renew("0badc0de")
    check("an unknown token id: refused (exit 2), get a new one", rc == 2 and "no live token" in res.get("reason", ""), text)
    tokE = js(client("token", "api1", "--vm", "opsvm", "--ttl", "2s").stdout)
    time.sleep(3)
    rc, res, text = renew(tokE.get("token_id", "00000000"))
    check("an expired token isn't renewed (exit 2): it stays ended", rc == 2 and "can't be renewed" in res.get("reason", ""), text)
    tokR = js(client("token", "api1", "--vm", "opsvm").stdout)
    client("revoke", tokR.get("token_id", "00000000"))
    rc, res, text = renew(tokR.get("token_id", "00000000"))
    st, _ = vm_request("GET", "/api1/v1/echo", bearer(tokR))
    check("a revoked token isn't renewed (exit 2), and stays refused in the VM (401)", rc == 2 and st == 401, (text, st))
    r = client("token", "renew", "--vm", "opsvm")
    check("token renew with neither an id nor --all is a token for a secret named renew (none here): no renewal happens",
          r.returncode != 0 and "renew" in r.stdout + r.stderr, r.stdout + r.stderr)

    # --- 2. who renews: the token's own session and you at a terminal follow the policy; anyone else asks -----------
    n_keep = u.text().count("Keep token")
    p, outs = as_claude_token_renew(proj1, ["api1", "--vm", "opsvm", "--ttl", "5m"], ["--vm", "opsvm", "--ttl", "20m"])
    (rc_t, out_t), (rc_r, out_r) = claude_results(p, outs)
    tokS = js(out_t)
    rec = last_renew()
    check("the Claude Code session a `session` secret's token was made for renews it without a question (renewer same)",
          rc_t == 0 and rc_r == 0 and js(out_r).get("extended") is True and u.text().count("Keep token") == n_keep
          and rec.get("token_id") == tokS.get("token_id") and rec.get("renewer") == "same" and rec.get("approval") == "session",
          (out_t[-200:], out_r[-300:], rec))
    exp_s = tokens_json().get(tokS.get("token_id"), {}).get("expires")
    p, outs = as_claude(proj2, ["token", "renew", tokS.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "30m"])
    q = (seen(u, f"Keep token {tokS.get('token_id')} for api1") and seen(u, f"It was made for Claude Code in {proj1}")
         and seen(u, "Type api1 to allow"))
    u.send("no\n")
    rp = claude_results(p, outs)[0]
    exp_s2 = tokens_json().get(tokS.get("token_id"), {}).get("expires")
    check("another Claude Code session renewing it is asked at the unlock terminal, even for a `session` secret, and told whose "
          "token it is; 'no' refuses it (exit 2) and the expiry stays", q and rp[0] == 2 and exp_s and exp_s2 == exp_s, (q, rp, exp_s, exp_s2))
    p, outs = as_claude(proj2, ["token", "renew", tokS.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "30m"])
    q = seen(u, f"Keep token {tokS.get('token_id')}") and seen(u, "Type api1 to allow")
    u.send("api1\n")
    rp = claude_results(p, outs)[0]
    rec = last_renew()
    check("...and allowed there, it's renewed, recorded as another requester's (renewer other, each:yes)",
          q and rp[0] == 0 and rec.get("renewer") == "other" and rec.get("approval") == "each:yes", (q, rp, rec))
    h = detached_start("token", "renew", tokS.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "40m")
    q = seen(u, "not from Claude Code") and seen(u, "Type api1 to allow")
    u.send("no\n")
    rc_d, out_d = detached_wait(h)
    check("a program that isn't Claude Code and isn't run at a terminal counts as another requester: asked, refused with 'no'",
          q and rc_d == 2, (q, rc_d, out_d))
    n_keep = u.text().count("Keep token")
    rc, res, text = renew(tokS.get("token_id", "00000000"), "--ttl", "50m")
    rec = last_renew()
    check("you at a terminal renew the session's token without a question (renewer terminal): your own policy applies",
          rc == 0 and res.get("extended") is True and u.text().count("Keep token") == n_keep and rec.get("renewer") == "terminal",
          (text, rec))

    # --- 3. approval: an `each` secret asks again; `session` grants; the expiry shown is the one given; revocation wins
    h = detached_start("token", "api-each", "--vm", "opsvm", "--ttl", "10m")
    asked = seen(u, "Type api-each to allow")
    u.send("api-each\n")
    rc, out = detached_wait(h)
    tokA = js(out)
    exp0 = tokens_json().get(tokA.get("token_id"), {}).get("expires")
    rc_n, res_n, text_n = renew(tokA.get("token_id", "00000000"), "--ttl", "2h")
    q = seen(u, f"Keep token {tokA.get('token_id')} for api-each") and seen(u, "Type api-each to allow")
    exp1 = tokens_json().get(tokA.get("token_id"), {}).get("expires")
    check("an `each` secret: its renewal asks again even from you at a terminal, naming the token, the secret and how long; "
          "no answer in time refuses it (exit 2) and the token keeps its expiry",
          asked and rc == 0 and q and rc_n == 2 and exp0 and exp1 == exp0, (rc, q, rc_n, text_n, exp0, exp1))
    h = term_start(BIN, "token", "renew", tokA.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "2h")
    q = seen(u, "Type api-each to allow")
    u.send("api-each\n")
    rc_yes, out_yes = detached_wait(h)
    res = js(out_yes)
    rec = last_renew()
    check("...and allowed, it's renewed: the audit log has the request, the approval and the renewal (each:yes)",
          q and rc_yes == 0 and res.get("extended") is True and res.get("expires_in_s", 0) > 7000
          and rec.get("approval") == "each:yes" and rec.get("request_id")
          and any(x.get("op") == "authorize" and x.get("use") == "renew" for x in audit_recs()), (rc_yes, out_yes, rec))
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
    # The expiry the question names is the one given, however long the answer takes.
    h = detached_start("token", "api-each", "--vm", "opsvm", "--ttl", "30s")
    seen(u, "Type api-each to allow")
    u.send("api-each\n")
    rc, out = detached_wait(h)
    tokW = js(out)
    h = detached_start("token", "renew", tokW.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "90s")
    q = seen(u, f"Keep token {tokW.get('token_id')}") and seen(u, "Type api-each to allow")
    time.sleep(4)                                    # the person takes a few seconds to answer
    u.send("api-each\n")
    rc, out = detached_wait(h)
    res = js(out)
    check("a renewal answered 4 s late gets the expiry its question named (90 s from the question), not 90 s from the answer",
          q and rc == 0 and 0 < res.get("expires_in_s", 99) <= 87, (q, rc, out))

    # Revocation during a question: revoke is answered at once (it doesn't wait behind the question), and a renewal,
    # once allowed, finds its token gone.
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
    p, outs = as_claude_token_renew(proj1, ["api-proj", "--vm", "opsvm", "--ttl", "10m"], ["--vm", "opsvm", "--ttl", "1h"])
    (rc_t, out_t), rp1 = claude_results(p, outs)
    tokP = js(out_t)
    p, outs = as_claude(proj2, ["token", "renew", tokP.get("token_id", "00000000"), "--vm", "opsvm"])
    rp2 = claude_results(p, outs)[0]
    check("--project: a renewal from a Claude Code session in another project is refused before anything is asked; the session "
          "in the secret's project renews its own token", tokP.get("token_id") and rp2[0] == 2
          and "limited to Claude Code sessions in" in js(rp2[1]).get("reason", "") and rp1[0] == 0, (tokP, rp2, rp1))

    # --- 4. answers in flight end with their token; a renewal keeps them going -------------------------------------
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
    rc, res, text = renew(tokY.get("token_id", "00000000"), "--ttl", "1m")
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
    # A VM that stops reading can't hold an answer (and its token's access) open: the write is cut with the token.
    tokV = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "10m").stdout)
    sv = socket.create_connection(("127.0.0.1", mport[0]), timeout=10)
    sv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sv.sendall(f"GET /api-long/v1/big HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {tokV.get('token', 'x')}\r\n\r\n".encode())
    head = sv.recv(64)
    time.sleep(2.5)                                   # not reading: the proxy's writes to it block
    t0 = time.time()
    client("revoke", tokV.get("token_id", "00000000"))
    done = tvo.wait_for(lambda: [x for x in audit_recs() if x.get("op") == "api" and x.get("token_id") == tokV.get("token_id")], 45, 0.2)
    took = time.time() - t0
    sv.close()
    check("an answer the VM stops reading (its writes blocked) ends within seconds of a revoke, not when a write times out",
          head.startswith(b"HTTP/1.1 200") and done and took < 5, (head[:20], round(took, 1), done[-1:] if done else None))
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
    # Replacing the secret ends its tokens, and an answer streaming on one of them, without any other request.
    tokG = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "10m").stdout)
    hold = {}
    thg = threading.Thread(target=stream, args=("/api-long/v1/drip", tokG, hold, "g"))
    thg.start()
    time.sleep(1.0)
    rc, out = tty([BIN, "set", "api-long", *api, "--answer-timeout", "30s", "--replace"],
                  [("Vault passphrase", PASS + "\n"), ("API key for api-long", KEY5 + "\n")])
    t_set = time.time()
    thg.join(30)
    ended_after = time.time() - t_set
    gt, ggot = hold.get("g", (0, b""))
    check("set --replace cuts an answer streaming on one of its tokens within a second or two (no other request needed)",
          rc == 0 and b"tick" in ggot and b"DRIP-END" not in ggot and ended_after < 3, (rc, round(ended_after, 1), ggot[-60:]))

    # --- 5. renew --all, a required audit sink, the wrapper, and a lock in the middle of a batch ---------------------
    for t in (tokA, tokP, tokS, tokW):  # `each` secrets' (they'd ask), a project-limited one's, and the session's
        client("revoke", t.get("token_id", "00000000"))
    tokB2 = js(client("token", "api1", "--vm", "opsvm", "--ttl", "10m").stdout)
    live = [t for t in tokens_json().values() if t.get("expires_in_s", 0) > 20]
    n_ren = len([x for x in audit_recs() if x.get("op") == "renew"])
    rc, res, text = renew("--all", "--ttl", "30m")
    ids = {x.get("token_id") for x in res.get("renewed", [])}
    check("token renew --all: every live token on the VM is renewed, one audit record each",
          rc == 0 and len(live) >= 2 and {t["id"] for t in live} <= ids and not res.get("not_renewed")
          and len([x for x in audit_recs() if x.get("op") == "renew"]) - n_ren == len(ids), (text, live))
    rx = tvo.http_server(tvo.Receiver)
    rc, out = tty([BIN, "audit-sink", "add", "req", "--kind", "webhook", "--url", f"http://127.0.0.1:{rx.server_address[1]}/req", "--required"],
                  [("Vault passphrase", PASS + "\n")])
    check("a required audit sink (webhook)", rc == 0, out[-300:])
    rx.down.add("/req")
    rc, res, text = renew(tokB.get("token_id", "00000000"), "--ttl", "1h")
    rx.down.discard("/req")
    rc2, res2, text2 = renew(tokB.get("token_id", "00000000"), "--ttl", "2h")
    begins = [json.loads(x["body"]).get("data", {}) for x in rx.got if x["path"].startswith("/req")]
    begins = [b for b in begins if b.get("op") == "begin" and b.get("use") == "renew"]
    check("with a required audit sink, a renewal is refused while the sink is down (exit 2), and goes ahead once its own "
          "begin record has reached the sink", rc == 2 and "required audit sink" in res.get("reason", "") and rc2 == 0
          and begins and begins[-1].get("token_id") == tokB.get("token_id"), (text, text2, begins[-1:]))
    rc, out = tty([BIN, "audit-sink", "remove", "req"], [("Vault passphrase", PASS + "\n")])
    conf = os.path.join(td, "ops-vm.env")
    open(conf, "w").write(f'VM_NAME=opsvm\nSSH_PORT={port}\nSSH_DIR="{sshdir}"\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n'
                          f'ACTION_LOG=on\nACTION_LOG_KEY="{sshdir}/action-log"\nGUEST_USER=cua\n')
    wrapper = {"MCOMP_CONFIG": conf, "MCOMP_VAULT_BIN": BIN, "MCOMP_VAULT_ALLOW_TEST_BUILD": "1", "MCOMP_EVENTS_SPOOL": spool,
               "MCOMP_EVENTS_DIR": os.path.join(td, "events-cfg")}
    mc = os.path.join(LAB, "bin/metacomputer")
    rc, text = term(mc, "secrets", "token", "renew", tokB.get("token_id", "00000000"), "--ttl", "3h", extra=wrapper)
    adir = os.path.join(td, "logs", "actions")
    afiles = [os.path.join(d, f) for d, _, fs in os.walk(adir) for f in fs if f.endswith(".jsonl")]
    arecs = [json.loads(l) for f in afiles for l in open(f)]
    check("metacomputer secrets token renew ID (this VM filled in): renewed, and the signed action log records it as "
          "secrets_token_renew with the token's id", rc == 0 and js(text).get("extended") is True
          and any(x.get("type") == "secrets_token_renew" and x.get("token_id") == tokB.get("token_id") for x in arecs),
          text[-400:])
    # A lock while a batch waits on one token's question stops the batch: nothing after it is renewed, and every token
    # ends when the lock is answered, not when the unlock process gets round to stopping.
    h = detached_start("token", "api-each", "--vm", "opsvm", "--ttl", "5m")
    seen(u, "Type api-each to allow")
    u.send("api-each\n")
    rc, out = detached_wait(h)
    tokQ = js(out)                                    # an `each` token: its renewal asks
    tokT = js(client("token", "api1", "--vm", "opsvm", "--ttl", "5m").stdout)   # made after it: renewed after it
    h = term_start(BIN, "token", "renew", "--all", "--vm", "opsvm", "--ttl", "1h")
    q = seen(u, f"Keep token {tokQ.get('token_id')}")
    rl = client("lock")
    stT, _ = vm_request("GET", "/api1/v1/echo", bearer(tokT), timeout=5)
    rc, out = detached_wait(h)
    res = js(out)
    tvo.wait_for(lambda: not os.path.exists(sock), 15, 0.1)
    try:
        u.wait(15)
    except AssertionError:
        pass
    check("lock while renew --all waits on a question: the batch stops (the token after it isn't renewed) and its tokens stop "
          "working as soon as the lock is answered", q and rl.returncode == 0 and stT != 200
          and tokT.get("token_id") not in {x.get("token_id") for x in res.get("renewed", [])}
          and any(x.get("token_id") == tokT.get("token_id") and "locking" in x.get("reason", "") for x in res.get("not_renewed", [])),
          (q, rl.stdout, stT, out))
    st, _ = vm_request("GET", "/api1/v1/echo", bearer(tokB), timeout=5)
    rc, res, text = renew(tokB.get("token_id", "00000000"))
    check("lock ends every token, renewed or not (the proxy is gone), and a renewal while locked says so (exit 3)",
          st != 200 and rc == 3, (st, text))

    # --- 6. the longest life: never past its limit after issue (8 s in this test build), never past the unlock -------
    u = start_unlock(["--for", "20m"], {"MCOMP_VAULT_TEST_TOKEN_MAX_LIFETIME": "8"})
    r = client("audit", "--verify")
    check("the refusal a renewal wrote while the vault was locked is a client's own record, not a forgery: audit --verify "
          "checks out", r.returncode == 0 and "checks out" in r.stdout, (r.stdout + r.stderr)[-400:])
    tokM = js(client("token", "api1", "--vm", "opsvm", "--ttl", "3s").stdout)
    proxy_port()
    rc, res, text = renew(tokM.get("token_id", "00000000"), "--ttl", "1h")
    rc2, res2, text2 = renew(tokM.get("token_id", "00000000"), "--ttl", "1h")
    check("renewing past the longest life: it reaches its limit (8 s after it was made here), no further; the next renewal "
          "is refused and says why", rc == 0 and res.get("extended") is True and res.get("expires_in_s", 99) <= 8 and rc2 == 2
          and "can't live any longer" in res2.get("reason", "") and "after it was made" in res2.get("reason", ""), (text, text2))
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
    h = term_start(BIN, "token", "renew", tokU.get("token_id", "00000000"), "--vm", "opsvm", "--ttl", "1h")
    q = seen(u, f"Keep token {tokU.get('token_id')}") and seen(u, "Type api1 to allow")
    u.send("api1\n")
    rc, out = detached_wait(h)
    res = js(out)
    rc2, res2, text2 = renew(tokU.get("token_id", "00000000"), "--ttl", "1h")
    check("unlock --confirm each: a `session` secret's renewal asks too, even from you at a terminal; it never reaches past "
          "the unlock's end, and a renewal with nothing left to give is refused, naming the unlock", q and rc == 0
          and res.get("expires_in_s", 99) <= 15 and rc2 == 2 and "unlock ends" in res2.get("reason", ""), (q, out, text2))
    lock(u)

    # --- 6b. a computer elsewhere: a token is for the computer it was made for, not the name --------------------------
    # Its binding names its identity (lib/remote.py identity: beside its keys); deleted, or made again under the same
    # name, it's another computer: the token stops at once, its tunnel is closed, and a new binding gets a new tunnel.
    ident = os.path.join(sshdir, "computer-id")
    open(ident, "w").write("c" * 64 + "\n")
    far = ["--port", "22", "--host", "203.0.113.7", "--host-key-alias", "mcomp-farvm", "--host-key-line", hostkey,
           "--fill-key", sshdir + "/fill", "--fill-user", "mcfill", "--ops-key", sshdir + "/ops", "--ops-user", "cua",
           "--proxy-key", sshdir + "/proxy", "--proxy-user", "mcproxy", "--proxy-port", "18766"]
    rc, out = tty([BIN, "bind", "farvm", *far], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("bind a computer elsewhere with its proxy account and identity", rc == 0, out[-300:])
    u = start_unlock(["--for", "20m"])
    tokF = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    tun_far = [c for c in calls() if c.get("kind") == "proxy:tunnel" and "mcproxy@203.0.113.7" in c["argv"]]
    proxy_port()
    st_ok, _ = vm_request("GET", "/api1/v1/echo", bearer(tokF))
    check("a token for the computer elsewhere works through its tunnel (to its address)", st_ok == 200 and len(tun_far) == 1,
          (st_ok, len(tun_far)))
    open(ident, "w").write("d" * 64 + "\n")             # deleted and made again under the same name
    st_gone, body = vm_request("GET", "/api1/v1/echo", bearer(tokF))
    alive = lambda pid: subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
    tunnel_closed = tvo.wait_for(lambda: not alive(tun_far[0]["pid"]), 10, 0.1) if tun_far else False
    st_again, _ = vm_request("GET", "/api1/v1/echo", bearer(tokF))
    check("...the computer made again (another identity beside its keys): the token is refused at once (401, nothing "
          "sent), revoked, and the tunnel to the old one closed", st_gone == 401 and "made again" in body
          and tunnel_closed and st_again in (0, 401), (st_gone, body[:200], tunnel_closed, st_again))
    tokF2 = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    check("...and no new token for it until it's bound again", not tokF2.get("token") and "bind" in tokF2.get("reason", ""),
          tokF2)
    rc, out = tty([BIN, "bind", "farvm", *far], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    n_tun = len([c for c in calls() if c.get("kind") == "proxy:tunnel"])
    tokF3 = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    tun_new = [c for c in calls() if c.get("kind") == "proxy:tunnel"][n_tun:]
    proxy_port()
    st_new, _ = vm_request("GET", "/api1/v1/echo", bearer(tokF3))
    st_old, _ = vm_request("GET", "/api1/v1/echo", bearer(tokF))
    check("bound again: a new tunnel for the new computer (never the old one's), its new token works, the old one doesn't",
          rc == 0 and len(tun_new) == 1 and st_new == 200 and st_old == 401, (rc, len(tun_new), st_new, st_old))
    # Made again and bound again before anything used the old tunnel: the next token gets a new tunnel, the old one ends.
    open(ident, "w").write("e" * 64 + "\n")
    rc, out = tty([BIN, "bind", "farvm", *far], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    n_tun = len([c for c in calls() if c.get("kind") == "proxy:tunnel"])
    tokF4 = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    tun_4 = [c for c in calls() if c.get("kind") == "proxy:tunnel"][n_tun:]
    old_closed = tvo.wait_for(lambda: not alive(tun_new[0]["pid"]), 10, 0.1) if tun_new else False
    proxy_port()
    st_4, _ = vm_request("GET", "/api1/v1/echo", bearer(tokF4))
    check("re-bound to another computer while the old tunnel was still open: a new tunnel, the old one closed, the new "
          "token works", rc == 0 and len(tun_4) == 1 and old_closed and st_4 == 200, (rc, len(tun_4), old_closed, st_4))

    def rebind_as(ch):
        open(ident, "w").write(ch * 64 + "\n")
        rc, out = tty([BIN, "bind", "farvm", *far], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
        check(f"(bound again as computer {ch * 4}...)", rc == 0, out[-400:])
        return rc

    def token_ids():
        return {t["id"] for t in tokens_json().values() if t.get("vm") == "farvm"}

    # A request whose body is still coming when the computer changes: checked again just before it's forwarded.
    rebind_as("f")
    tokS1 = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    proxy_port()
    n_up = len(up.got)
    sk = socket.create_connection(("127.0.0.1", mport[0]), timeout=30)
    sk.sendall(("POST /api1/v1/echo HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + tokS1.get("token", "x")
                + "\r\nContent-Length: 10\r\n\r\nabc").encode())
    time.sleep(1.5)
    open(ident, "w").write("5" * 64 + "\n")             # deleted and made again while the body was on its way
    sk.sendall(b"defghij")
    got = b""
    try:
        while True:
            d = sk.recv(4096)
            if not d:
                break
            got += d
    except OSError:
        pass
    sk.close()
    first = got.split(b"\r\n", 1)[0].decode(errors="replace")
    check("a request whose body was still coming when its computer was made again: refused just before it would be "
          "forwarded (401), nothing reaches the API (found by review: the check came before the body only)",
          " 401 " in first and b"while this request waited" in got and len(up.got) == n_up, (first, got[-200:], len(up.got) - n_up))
    # An answer streaming when the computer changes: cut within about a second.
    rebind_as("7")
    tokS2 = js(client("token", "api-long", "--vm", "farvm", "--ttl", "10m").stdout)
    proxy_port()
    hold = {}
    th = threading.Thread(target=stream, args=("/api-long/v1/drip", tokS2, hold, "s"))
    th.start()
    time.sleep(1.5)
    t_change = time.time()
    open(ident, "w").write("6" * 64 + "\n")
    th.join(30)
    secs, body = hold.get("s", (99, b""))
    check("an answer streaming when its computer is made again: cut within seconds (no DRIP-END), the token revoked "
          "(found by review: streams went on)", b"tick" in body and b"DRIP-END" not in body and secs < 5.5
          and tokS2.get("token_id") not in token_ids(), (secs, body[-80:], round(time.time() - t_change, 1), tokS2))
    # Bound again to another computer: the old binding's tokens are pruned even if nothing uses them.
    rebind_as("8")
    tokS3 = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    alive_before = tokS3.get("token_id") in token_ids()
    rebind_as("9")
    check("bound again to another computer: the tokens made for the one before are pruned at once (tokens lists none)",
          alive_before and tokS3.get("token_id") not in token_ids(), (alive_before, token_ids(), tokS3))
    # vm delete revokes the VM's tokens in whatever vault it uses.
    tokS4 = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    r = client("revoke", "--vm", "farvm")
    check("revoke --vm NAME (what vm delete runs): that VM's tokens end, others stay", r.returncode == 0
          and tokS4.get("token_id") not in token_ids() and "Revoked" in r.stdout, (r.stdout, r.stderr))
    tokF4 = js(client("token", "api1", "--vm", "farvm", "--ttl", "10m").stdout)
    proxy_port()
    os.remove(ident)                                   # vm delete removes the identity
    st_del, _ = vm_request("GET", "/api1/v1/echo", bearer(tokF4))
    check("...deleted (no identity beside its keys): its token stops too", st_del == 401, st_del)
    lock(u)

    # --- 7. a vault this unlock can't open any more fails closed: tokens and answers end, and it locks ---------------
    vfile = os.path.join(vdir, "vault.json")
    u = start_unlock(["--for", "20m"])
    tokK = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "10m").stdout)
    proxy_port()
    hold = {}
    thk = threading.Thread(target=stream, args=("/api-long/v1/drip", tokK, hold, "k"))
    thk.start()
    time.sleep(1.5)
    good = open(vfile, "rb").read()
    open(vfile + ".tmp", "wb").write(b'{"damaged": true}\n')
    os.replace(vfile + ".tmp", vfile)                 # damaged, as an atomic write would leave it
    t_bad = time.time()
    thk.join(30)
    cut_after = time.time() - t_bad
    locked = tvo.wait_for(lambda: not os.path.exists(sock), 10, 0.1)
    lock_after = time.time() - t_bad
    kt, kgot = hold.get("k", (0, b""))
    try:
        u.wait(15)
    except AssertionError:
        pass
    open(vfile + ".tmp", "wb").write(good)
    os.replace(vfile + ".tmp", vfile)
    check("a vault file the unlock can no longer open and verify ends the answers streaming on its tokens at once and locks "
          "the vault, with no other request needed", b"tick" in kgot and b"DRIP-END" not in kgot and cut_after < 3 and locked
          and lock_after < 5 and "can't be read and verified" in u.text(), (round(cut_after, 1), locked, round(lock_after, 1),
                                                                          u.text()[-300:]))
    u = start_unlock(["--for", "20m"])
    tokK2 = js(client("token", "api-long", "--vm", "opsvm", "--ttl", "10m").stdout)
    proxy_port()
    hold = {}
    thk = threading.Thread(target=stream, args=("/api-long/v1/drip", tokK2, hold, "k2"))
    thk.start()
    h = detached_start("token", "api-each", "--vm", "opsvm", "--ttl", "10m")
    q = seen(u, "Type api-each to allow")
    rc, out = tty([BIN, "rekey"], [("Vault passphrase", PASS + "\n"), ("New vault passphrase", PASS2 + "\n"),
                                   ("same passphrase again", PASS2 + "\n")])
    t_rk = time.time()
    rc_q, out_q = detached_wait(h, 30)
    q_after = time.time() - t_rk
    thk.join(30)
    cut_after = time.time() - t_rk
    locked = tvo.wait_for(lambda: not os.path.exists(sock), 10, 0.1)
    lock_after = time.time() - t_rk
    k2t, k2got = hold.get("k2", (0, b""))
    try:
        u.wait(15)
    except AssertionError:
        pass
    check("a rekey while a question waits: the question ends (refused), the answer streaming on a token ends, and the unlock "
          "locks within seconds, without waiting for the question's time or another request", q and rc == 0 and rc_q != 0
          and q_after < 4 and b"DRIP-END" not in k2got and cut_after < 4 and locked and lock_after < 5
          and "key changed" in u.text(), (q, rc, rc_q, round(q_after, 1), round(cut_after, 1), locked, round(lock_after, 1)))

    # --- the guarantees that hold throughout -------------------------------------------------------------------
    recs = audit_recs()
    ren = [x for x in recs if x.get("op") == "renew"]
    check(f"every renewal record ({len(ren)}) holds only the vault's own fields: no request count, path, size or VM text",
          ren and all(set(x) <= RENEW_FIELDS for x in ren) and all(isinstance(x.get("ttl_s", 0), int) for x in ren)
          and all(x.get("renewer") in (None, "same", "terminal", "other") for x in ren),
          [sorted(set(x) - RENEW_FIELDS) for x in ren if not set(x) <= RENEW_FIELDS][:3])
    toks = [t.get("token") for t in (tok, tokE, tokR, tokS, tokA, tokW, tokZ, tokB, tokB2, tokC, tokP, tokD, tokX, tokY, tokV, tokL, tokG,
                                     tokQ, tokT, tokM, tokU, tokK, tokK2) if t.get("token")]
    raw = open(alog).read()
    term_text = "".join(p.text() for p in unlocks)
    check("the audit log holds no key and no token", not any(v in raw for v in VALUES) and not any(t in raw for t in toks))
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no key or passphrase in the output of any of {len(outputs)} commands", not leaked, leaked)
    check("the unlock terminal never showed a key or a token", not any(v in term_text for v in VALUES) and not any(t in term_text for t in toks))
    listener.close()


if __name__ == "__main__":
    sys.exit(main())
