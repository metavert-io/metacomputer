#!/usr/bin/env python3
"""Offline checks for the vault's key handling, audit sinks, requester identities and API tokens.

No VM needed; same approach as tests/test_secrets.py (whose stub, pseudo-terminal and helpers
this reuses): a TEST build of mcomp-vault, an ssh stub standing in for the VM, every passphrase
and value typed into a pseudo-terminal, every value a dummy. Covered here:
  - the Secure Enclave (when this Mac has one): init prints a recovery key; rekey; recover;
    --no-enclave; a vault whose Enclave key is gone opens only with the recovery key;
  - rotation: set --replace, ages in list, rotate-session-key;
  - the hash-chained audit log, scripts/verify-audit.py, the unified log;
  - audit sinks: webhook, OTLP, syslog over TLS (a self-signed CA passed with --ca) and ntfy,
    all at once: the same ordered events, required vs optional, removing one;
  - requester identities (a stand-in process named `claude`), approval scopes, --project;
  - API tokens through the proxy (a local plain-HTTP upstream in a test build): the real header
    swapped in, path limits, redirects, the key withheld from answers, expiry and revoke;
  - the events spool (needs_you approval_waiting from unlock, vault_locked from bin/metacomputer),
    following the event stream's spool contract.

Run: python3 tests/test_vault_ops.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build python3 tests/test_vault_ops.py   (skip building)
"""
import base64
import glob
import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import test_secrets as ts  # noqa: E402

LAB = ts.LAB
check = ts.check
PASS = ts.PASS
PASS2 = "dummy passphrase two 31"
PASS3 = "dummy passphrase three 47"
PW_VALUE = ts.PW_VALUE
UPSTREAM_VALUE = "dummy-api-key-7Hq2Zp9W"
SITE = ts.SITE
UPSTREAM_VALUE2 = "dummy-api-key-2nd-Qm4x"
RACE_VALUE = "dummy race value 5"
TEXT_O = "dummy old vault text 6"
VALUES = [PASS, PASS2, PASS3, PW_VALUE, UPSTREAM_VALUE, UPSTREAM_VALUE2, RACE_VALUE, TEXT_O]
RECOVERY_RE = re.compile(r"\b([A-Z2-7]{4}(?:-[A-Z2-7]{4}){7})\b")
KEY_RE = re.compile(r"signing key[^\n]*\n\s+([A-Za-z0-9+/]{43}=)")
SPOOL_NAME = re.compile(r"\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{16}\.json")
outputs = []

# Stands in for Codex's app server (the IDE extension, the desktop app): a process named "codex" run as `codex
# app-server`, in a project folder, running a command.
FAKE_CODEX_SERVER_C = r"""
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc < 4 || strcmp(argv[1], "app-server") != 0 || chdir(argv[2]) != 0) return 111;
    pid_t p = fork();
    if (p == 0) { execv(argv[3], argv + 3); _exit(127); }
    int st = 0;
    waitpid(p, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}
"""
FAKE_CLAUDE_C = r"""
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
/* Stands in for Claude Code: a process named "claude", in a project folder, running a command. */
int main(int argc, char **argv) {
    if (argc < 3 || chdir(argv[1]) != 0) return 111;
    pid_t p = fork();
    if (p == 0) { execv(argv[2], argv + 2); _exit(127); }
    int st = 0;
    waitpid(p, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}
"""

CERT_CNF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = {cn}
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[v3_srv]
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


# --- receivers ---------------------------------------------------------------------------------

class Receiver(http.server.BaseHTTPRequestHandler):
    """Webhook, OTLP and ntfy endpoints: records each POST; paths in server.down answer 503, paths in
    server.slow wait 4 s first, paths in server.partial answer 200 with an OTLP partial success."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if any(self.path.startswith(p) for p in self.server.slow):
            time.sleep(4)
        if any(self.path.startswith(p) for p in self.server.down):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.server.got.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                "signature": self.headers.get("X-Metacomputer-Signature"), "body": body})
        answer = b""
        if any(self.path.startswith(p) for p in self.server.partial):
            answer = json.dumps({"partialSuccess": {"rejectedLogRecords": "1", "errorMessage": "dummy rejection"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *a):
        pass


class Upstream(http.server.BaseHTTPRequestHandler):
    """The API behind the proxy: echoes what it got (as debug endpoints and httpbin do)."""

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        self.server.got.append({"method": self.command, "path": self.path, "body": body,
                                "headers": {k.lower(): v for k, v in self.headers.items()}})
        auth = self.headers.get("Authorization") or ""
        if self.path.startswith("/v1/redirect-other"):
            self.send_response(302)
            self.send_header("Location", "http://evil.example/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith("/v1/redirect-same"):
            self.send_response(302)
            self.send_header("Location", "/v1/echo?x=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith("/v1/redirect-encoded"):
            self.send_response(302)
            self.send_header("Location", "/v1/a%2Fb/c%20d/?q=%26x")   # an encoded slash and a trailing slash
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.startswith("/v1/late"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")   # no sniffing wait: data is passed on at once
            self.end_headers()           # HTTP/1.0: the body ends when the connection does
            try:
                self.wfile.write(b"first " + b"." * 2048)
                self.wfile.flush()
                time.sleep(5)            # past the proxy's answer timeout in this test build (3 s)
                self.wfile.write(b"LATE-DATA")
                self.wfile.flush()
            except OSError:
                pass
        elif self.path.startswith("/v1/slow"):
            time.sleep(2)            # within the answer timeout (3 s), but open while /v1/late would still write
            data = b'{"slow": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/v1/split"):
            self.send_response(200)
            self.end_headers()           # HTTP/1.0: the body ends when the connection does
            half = len(auth) // 2 + 3
            self.wfile.write(("start " + auth[:half]).encode())
            self.wfile.flush()
            time.sleep(0.3)
            self.wfile.write((auth[half:] + " end").encode())
            self.wfile.flush()
        else:
            data = json.dumps({"path": self.path, "authorization": auth, "body": body}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Echo", auth)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = handle_any

    def log_message(self, *a):
        pass


def http_server(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.got, srv.down, srv.slow, srv.partial = [], set(), set(), set()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class SyslogTLS:
    """An RFC 5425 receiver: TLS, octet-counted frames; one message per frame."""

    def __init__(self, cert, key):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.got, self.handshake_failures = [], 0
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.conn, args=(c,), daemon=True).start()

    def conn(self, c):
        try:
            t = self.ctx.wrap_socket(c, server_side=True)
        except (ssl.SSLError, OSError):
            self.handshake_failures += 1
            c.close()
            return
        buf = b""
        try:
            while True:
                d = t.recv(65536)
                if not d:
                    break
                buf += d
                while True:
                    sp = buf.find(b" ")
                    if sp <= 0 or not buf[:sp].isdigit():
                        break
                    n = int(buf[:sp])
                    if len(buf) < sp + 1 + n:
                        break
                    self.got.append(buf[sp + 1:sp + 1 + n].decode())
                    buf = buf[sp + 1 + n:]
        except (ssl.SSLError, OSError):
            pass
        finally:
            t.close()


def make_certs(d):
    """A throwaway CA and a server certificate for 127.0.0.1 signed by it."""
    o = shutil.which("openssl") or "/usr/bin/openssl"
    open(os.path.join(d, "ca.cnf"), "w").write(CERT_CNF.format(cn="mcomp test CA"))
    open(os.path.join(d, "srv.cnf"), "w").write(CERT_CNF.format(cn="127.0.0.1"))
    j = lambda f: os.path.join(d, f)  # noqa: E731
    steps = [[o, "req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", j("ca.key"), "-out", j("ca.pem"),
              "-days", "30", "-config", j("ca.cnf"), "-extensions", "v3_ca"],
             [o, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", j("srv.key"), "-out", j("srv.csr"), "-config", j("srv.cnf")],
             [o, "x509", "-req", "-in", j("srv.csr"), "-CA", j("ca.pem"), "-CAkey", j("ca.key"), "-CAcreateserial",
              "-out", j("srv.pem"), "-days", "30", "-extfile", j("srv.cnf"), "-extensions", "v3_srv"]]
    for s in steps:
        r = subprocess.run(s, capture_output=True, text=True)
        if r.returncode != 0:
            return None, r.stderr
    return (j("ca.pem"), j("srv.pem"), j("srv.key")), ""


def pwd_name():
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


def local_sshd(td):
    """An sshd of this user's own on 127.0.0.1 (no root needed): (port, host key line, client key, process) or None."""
    d = os.path.join(td, "sshd")
    os.makedirs(d, exist_ok=True)
    for k in ("hk", "ck"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(d, k)], check=True)
    shutil.copy(os.path.join(d, "ck.pub"), os.path.join(d, "ak"))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    open(os.path.join(d, "cfg"), "w").write(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d}/hk\nPidFile {d}/pid\nAuthorizedKeysFile {d}/ak\nStrictModes no\n"
        "UsePAM no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n")
    if not os.path.exists("/usr/sbin/sshd"):
        return None
    pr = subprocess.Popen(["/usr/sbin/sshd", "-D", "-f", os.path.join(d, "cfg"), "-E", os.path.join(d, "log")])
    for _ in range(50):
        with socket.socket() as c:
            if c.connect_ex(("127.0.0.1", port)) == 0:
                hk = " ".join(open(os.path.join(d, "hk.pub")).read().split()[:2])
                return port, hk, os.path.join(d, "ck"), pr
        time.sleep(0.1)
    pr.kill()
    return None


def wait_for(fn, timeout=20, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


def main():
    td = tempfile.mkdtemp(prefix="mcvo-", dir="/tmp")
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
        h = subprocess.run(["shasum"], input=f"{LAB}:opsvm".encode(), capture_output=True).stdout.decode()[:8]
        try:
            os.rmdir(f"/tmp/mcomp-{os.getuid()}-{h}")
        except OSError:
            pass
    print(f"\n{sum(ts.results)}/{len(ts.results)} passed")
    return 0 if all(ts.results) else 1


def run(td, procs):
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(td, "bin", "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    allow = ["--allow-multiplexer"] if ts.in_multiplexer() else []
    verify = os.path.join(LAB, "scripts/verify-audit.py")

    # --- the fake VM, keys, stub, as in test_secrets ------------------------------------------------
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
    open(os.path.join(stubdir, "archive.tgz"), "wb").write(
        ts.tarball([("prof1", "dir", None), ("prof1/Default", "dir", None), ("prof1/Default/Cookies", "file", b"dummy-jar-77" * 40)]))
    fake = os.path.join(td, "fake", "claude")
    os.makedirs(os.path.dirname(fake))
    open(fake + ".c", "w").write(FAKE_CLAUDE_C)
    r = subprocess.run(["cc", "-o", fake, fake + ".c"], capture_output=True, text=True)
    check("a stand-in process named claude builds", r.returncode == 0, r.stderr)
    proj1, proj2 = os.path.realpath(os.path.join(td, "proj1")), os.path.realpath(os.path.join(td, "proj2"))
    os.makedirs(os.path.join(proj1, "sub"))
    os.makedirs(proj2)

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
           "MCOMP_VAULT_TEST_IGNORE_AGENT": "1",
           "MCOMP_VAULT_TEST_SSH": stub}
    sock = os.path.join(vdir, "vault.sock")
    alog = os.path.join(vdir, "audit.jsonl")

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
        finally:
            p.close()
        return rc, p.text()

    counter = [0]

    def detached_start(*args):
        """The client with no Claude Code process among its ancestors (its parent shell is orphaned)."""
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

    def detached(*args, timeout=60):
        return detached_wait(detached_start(*args), timeout)

    def as_claude(project, *commands):
        """Run client commands one after another under one stand-in Claude Code process in `project`."""
        counter[0] += 1
        outs = [os.path.join(td, f"cl{counter[0]}-{i}.out") for i in range(len(commands))]
        script = "; ".join(f"{shlex.join([BIN, *c])} >{shlex.quote(o)} 2>&1; echo $? >{shlex.quote(o + '.rc')}"
                           for c, o in zip(commands, outs))
        p = subprocess.Popen([fake, project, "/bin/sh", "-c", script], env=env, stdin=subprocess.DEVNULL)
        procs.append(p)
        return p, outs

    def as_agent(argv0, project, *commands):
        """as_claude, under another stand-in: argv0 is the program and its leading arguments ([codex], or [codex,
        "app-server"]), then the project folder."""
        counter[0] += 1
        outs = [os.path.join(td, f"ag{counter[0]}-{i}.out") for i in range(len(commands))]
        script = "; ".join(f"{shlex.join([BIN, *c])} >{shlex.quote(o)} 2>&1; echo $? >{shlex.quote(o + '.rc')}"
                           for c, o in zip(commands, outs))
        p = subprocess.Popen([*argv0, project, "/bin/sh", "-c", script], env=env, stdin=subprocess.DEVNULL)
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

    def canon(o):
        return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def rehash(e):
        e.pop("hash", None)
        e["hash"] = hashlib.sha256(canon(e).encode()).hexdigest()
        return e

    def rewrite_last(change):
        """What an agent running as the user can do: change the newest record and recompute its hash."""
        lines = open(alog).read().splitlines()
        e = json.loads(lines[-1])
        change(e)
        rehash(e)
        lines[-1] = canon(e)
        with open(alog, "w") as f:
            f.write("\n".join(lines) + "\n")
        return e

    def append_forged(fields):
        """A whole new record, chained and hashed correctly, without the vault's key."""
        last = json.loads(open(alog).read().splitlines()[-1])
        e = dict(fields, seq=last["seq"] + 1, prev=last["hash"], ts="2026-01-01T00:00:00.000000Z",
                 id="00000000-0000-4000-8000-%012d" % (last["seq"] + 1))
        rehash(e)
        with open(alog, "a") as f:
            f.write(canon(e) + "\n")
        return e

    def start_unlock(args=(), extra=None, passphrase=PASS):
        p = ts.Pty([BIN, "unlock", *args], dict(env, **(extra or {})))
        procs.append(p)
        p.expect("Vault passphrase: ")
        p.send(passphrase + "\n")
        p.expect("unlocked until")
        wait_for(lambda: os.path.exists(sock), 5, 0.05)
        return p

    def spool_events():
        evs = []
        for f in sorted(glob.glob(os.path.join(spool, "*.json"))):
            try:
                evs.append(json.load(open(f)))
            except ValueError:
                evs.append({"bad": f})
        return evs

    # --- 1. the Secure Enclave and the recovery key -----------------------------------------------
    ver = json.loads(client("version", "--json").stdout)
    enclave = bool(ver.get("enclave_available"))
    print(f"(Secure Enclave available: {enclave})")
    nodir = os.path.join(td, "v-noenclave")
    rc, out = tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")],
                  extra={"MCOMP_VAULT_DIR": nodir})
    h0 = json.load(open(os.path.join(nodir, "vault.json"))) if os.path.exists(os.path.join(nodir, "vault.json")) else {}
    check("init --no-enclave: passphrase only, no recovery key", rc == 0 and "passphrase only" in out and "RECOVERY KEY" not in out
          and not h0.get("enclave") and h0.get("version") == 2, out)
    rc, out = tty([BIN, "init"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    hdr = json.load(open(os.path.join(vdir, "vault.json")))
    rk1 = (RECOVERY_RE.findall(out) or [""])[0]
    if enclave:
        check("init: bound to the Secure Enclave by default, with a one-time recovery key", rc == 0 and "Secure Enclave" in out
              and bool(rk1) and hdr.get("enclave") and {w["kind"] for w in hdr.get("wraps", [])} == {"passphrase", "recovery"}, out)
        check("the recovery key isn't stored in the vault folder", not any(rk1 in open(f, errors="replace").read()
                                                                           for f in glob.glob(vdir + "/*") if os.path.isfile(f)))
    else:
        check("init without a Secure Enclave: passphrase only", rc == 0 and "passphrase only" in out, out)
    olddir = os.path.join(td, "vold")
    oenv = {"MCOMP_VAULT_DIR": olddir}
    tty([BIN, "init", "--no-enclave"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")],
        extra=dict(oenv, MCOMP_VAULT_TEST_OLD_VAULT="1"))
    rc, out = tty([BIN, "set", "old1", "--kind", "text", "--origin", SITE], [("Vault passphrase", PASS + "\n"), ("Value for old1", TEXT_O + "\n")],
                  extra=oenv)
    orecs = [json.loads(l) for l in open(os.path.join(olddir, "audit.jsonl"))]
    check("a vault from before the audit key (test switch) gets one on its next change; its earlier record has no MAC, the new one has",
          rc == 0 and "mac" not in orecs[0] and "mac" in orecs[-1], [sorted(r) for r in orecs])
    po = ts.Pty([BIN, "unlock", *allow], dict(env, **oenv))
    procs.append(po)
    po.expect("Vault passphrase: ")
    po.send(PASS + "\n")
    po.expect("unlocked until")
    r = client("audit", "--verify", extra=oenv)
    check("audit --verify on the upgraded vault: its older record is legacy (from before the audit key), not a forgery",
          r.returncode == 0 and "1 from before the vault had an audit key" in r.stdout and "checks out" in r.stdout, r.stdout + r.stderr)
    po.send("\x03")
    po.wait(15)
    st = client("status")
    check("status says how the vault is keyed", "keys:" in st.stdout and (("Secure Enclave" in st.stdout) == enclave), st.stdout)

    # --- 2. secrets, rotation ---------------------------------------------------------------------------
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE, "--username", "dummy-user"],
                  [("Vault passphrase", PASS + "\n"), ("Password for web-login", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set web-login", rc == 0, out)
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE], [("Vault passphrase", PASS + "\n")])
    check("set over an existing secret is refused without --replace (before asking for a value)", rc != 0 and "--replace" in out
          and "Password for" not in out, out)
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE, "--username", "dummy-user", "--replace"],
                  [("Vault passphrase", PASS + "\n"), ("Password for web-login", PW_VALUE + "x\n"), ("same password again", PW_VALUE + "x\n")])
    check("set --replace rotates it", rc == 0, out)
    recs = audit_recs()
    check("the audit log records the rotation", any(x.get("op") == "rotate" and x.get("name") == "web-login" for x in recs))
    rc, out = tty([BIN, "set", "web-each", "--kind", "password", "--origin", SITE, "--approval", "each"],
                  [("Vault passphrase", PASS + "\n"), ("Password for web-each", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set web-each (asks each time)", rc == 0, out)
    rc, out = tty([BIN, "set", "scoped", "--kind", "password", "--origin", SITE, "--project", proj1],
                  [("Vault passphrase", PASS + "\n"), ("Password for scoped", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set scoped --project DIR", rc == 0 and "only for Claude Code sessions in" in out, out)

    up = http_server(Upstream)
    upstream = f"127.0.0.1:{up.server_address[1]}"
    r = client("set", "api1", "--kind", "api", "--api-host", "https://x.example")
    check("set --kind api refuses a URL as --api-host", r.returncode == 64, r.stderr)
    rc, out = tty([BIN, "set", "short", "--kind", "api", "--api-host", upstream], [("Vault passphrase", PASS + "\n"), ("API key for short", "abc\n")])
    check("an API key under 8 characters is refused", rc != 0 and "at least 8" in out, out)
    rc, out = tty([BIN, "set", "api1", "--kind", "api", "--api-host", upstream, "--header", "Authorization: Bearer {value}",
                   "--api-path-prefix", "/v1/"], [("Vault passphrase", PASS + "\n"), ("API key for api1", UPSTREAM_VALUE + "\n")])
    check("set an api secret (host, header template, path prefix)", rc == 0 and UPSTREAM_VALUE not in out, out)
    rc, out = tty([BIN, "set", "api2", "--kind", "api", "--api-host", upstream, "--header", "x-api-key: {value}"],
                  [("Vault passphrase", PASS + "\n"), ("API key for api2", UPSTREAM_VALUE + "2\n")])
    check("set an api secret with an x-api-key header", rc == 0, out)

    # --- 3. bind through bin/metacomputer: the proxy account is filled in --------------------------------
    conf = os.path.join(td, "ops-vm.env")
    open(conf, "w").write(f'VM_NAME=opsvm\nSSH_PORT={port}\nSSH_DIR="{sshdir}"\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n'
                          f'ACTION_LOG=on\nACTION_LOG_KEY="{sshdir}/action-log"\nGUEST_USER=cua\n')
    # MCOMP_EVENTS_DIR: the event stream's settings and state in a folder of the test's own (no sinks), so
    # nothing reaches the user's real event sinks.
    wrapper = {"MCOMP_CONFIG": conf, "MCOMP_VAULT_BIN": BIN, "MCOMP_VAULT_ALLOW_TEST_BUILD": "1", "MCOMP_EVENTS_SPOOL": spool,
               "MCOMP_EVENTS_DIR": os.path.join(td, "events-cfg")}
    mc = os.path.join(LAB, "bin/metacomputer")
    rc, out = tty([mc, "secrets", "bind", "--gateway", my_exe], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")],
                  extra=wrapper)
    check("metacomputer secrets bind pins the proxy account (mcproxy, port 18765) when the proxy key exists", rc == 0
          and "api tokens    as mcproxy" in out and "127.0.0.1:18765" in out and "Bound VM opsvm" in out, out)
    common = ["--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill", "--fill-user", "mcfill",
              "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe]
    rc, out = tty([BIN, "bind", "oldvm", *common], [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("bind without a proxy account says api tokens need one", rc == 0 and "no proxy account" in out, out)
    rc, out = tty([BIN, "bind", "refusevm", *common, "--proxy-key", sshdir + "/proxy", "--proxy-user", "mcproxy", "--proxy-port", "18765"],
                  [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
    check("bind a second VM with a proxy account", rc == 0, out)

    # --- 4. locked: exit 3, and one needs_you/vault_locked event from the event stream's exit hook -------------
    def locked_events():
        lp = os.path.join(td, "logs", "events.jsonl")
        evs = [json.loads(l) for l in open(lp)] if os.path.exists(lp) else []
        return [e for e in evs if e.get("family") == "needs_you" and e.get("type") == "vault_locked"]
    r = subprocess.run([mc, "secrets", "fill", "web-login", "--field", "password"], capture_output=True, text=True,
                       env=dict(env, **wrapper), timeout=120, stdin=subprocess.DEVNULL)
    outputs.append(r.stdout + r.stderr)
    evs = locked_events()
    check("metacomputer secrets fill while locked: exit 3 and one needs_you/vault_locked event (the event stream's hook)",
          r.returncode == 3 and len(evs) == 1 and evs[0].get("severity") == "warn" and "unlock" in (evs[0].get("action") or ""),
          (r.returncode, evs))
    r = subprocess.run([mc, "secrets", "token", "api1"], capture_output=True, text=True, env=dict(env, **wrapper), timeout=120,
                       stdin=subprocess.DEVNULL)
    check("metacomputer secrets token while locked: exit 3, and still just one vault_locked (no second report, no spool file "
          "of the wrapper's own)", r.returncode == 3 and len(locked_events()) == 1
          and not any(e.get("type") == "vault_locked" for e in spool_events()), (r.returncode, len(locked_events())))

    # --- 5. rekey and recover ----------------------------------------------------------------------------
    salt0 = hdr["kdf"].get("salt")
    rc, out = tty([BIN, "rekey"], [("Vault passphrase", PASS + "\n"), ("New vault passphrase", PASS2 + "\n"),
                                   ("same passphrase again", PASS2 + "\n")])
    hdr2 = json.load(open(os.path.join(vdir, "vault.json")))
    rk2 = (RECOVERY_RE.findall(out) or [""])[0]
    check("rekey: new passphrase, new salt" + (", new recovery key" if enclave else ""), rc == 0 and "Rekeyed" in out
          and hdr2["kdf"].get("salt") != salt0 and (not enclave or (rk2 and rk2 != rk1)), out)
    rc, out = tty([BIN, "unlock"], [("Vault passphrase", PASS + "\n")])
    check("after rekey the old passphrase is refused", rc != 0 and "wrong passphrase" in out, out)
    if enclave:
        rc, out = tty([BIN, "recover"], [("Recovery key", rk1 + "\n")])
        check("after rekey the old recovery key is refused", rc != 0 and "wrong recovery key" in out, out)
        saved = open(os.path.join(vdir, "vault.json")).read()
        h = json.loads(saved)
        blob = bytearray(__import__("base64").b64decode(h["enclave"]["key"]))
        blob[len(blob) // 2] ^= 0x55
        h["enclave"]["key"] = __import__("base64").b64encode(bytes(blob)).decode()
        open(os.path.join(vdir, "vault.json"), "w").write(json.dumps(h))
        rc, out = tty([BIN, "recover"], [("Recovery key", rk2 + "\n")])
        open(os.path.join(vdir, "vault.json"), "w").write(saved)
        check("a changed Enclave setting in the header breaks every key wrap, the recovery key's too (they're bound to it)",
              rc != 0 and "wrong recovery key" in out, out)
        gone = {"MCOMP_VAULT_TEST_NO_ENCLAVE": "1"}      # another Mac, or a reset Enclave
        rc, out = tty([BIN, "unlock"], [("Vault passphrase", PASS2 + "\n")], extra=gone)
        check("on a Mac whose Enclave can't use the vault's key, the passphrase alone can't open it; it says to recover", rc != 0
              and "recover" in out, out)
        rc, out = tty([BIN, "recover", "--no-enclave"], [("Recovery key", rk2.lower().replace("-", " ") + "\n"), ("Choose a passphrase", None),
                                                         ("New vault passphrase", PASS3 + "\n"), ("same passphrase again", PASS3 + "\n")],
                      extra=gone)
        hdr3 = json.load(open(os.path.join(vdir, "vault.json")))
        check("recover there: opens with the recovery key (any case, spaces for dashes), sets a new passphrase (here without an Enclave)",
              rc == 0 and "Recovered" in out and "passphrase only" in out and not hdr3.get("enclave"), out)
        rc, out = tty([BIN, "rekey", "--enclave"], [("Vault passphrase", PASS3 + "\n"), ("New vault passphrase", PASS3 + "\n"),
                                                    ("same passphrase again", PASS3 + "\n")])
        rk3 = (RECOVERY_RE.findall(out) or [""])[0]
        hdr3 = json.load(open(os.path.join(vdir, "vault.json")))
        check("rekey --enclave moves it back onto this Mac's Enclave, with a new recovery key", rc == 0 and hdr3.get("enclave")
              and rk3 and rk3 not in (rk1, rk2), out)
        current = PASS3
    else:
        current = PASS2
    rc, out = tty([BIN, "delete", "short"], [("Vault passphrase", current + "\n")])
    check("the current passphrase opens the vault", "wrong passphrase" not in out, out)
    pause = os.path.join(td, "rekey-pause")
    pr = ts.Pty([BIN, "rekey"], dict(env, MCOMP_VAULT_TEST_PAUSE_REKEY=pause))
    procs.append(pr)
    for want, say in (("Vault passphrase", current), ("New vault passphrase", current), ("same passphrase again", current)):
        pr.expect(want)
        pr.send(say + "\n")
    waited = wait_for(lambda: os.path.exists(pause + ".waiting"), 60)
    rc, out = tty([BIN, "set", "race-secret", "--kind", "text", "--origin", SITE],
                  [("Vault passphrase", current + "\n"), ("Value for race-secret", RACE_VALUE + "\n")])
    open(pause, "w").close()
    rrc = pr.wait(90)
    rc2, out2 = tty([BIN, "delete", "race-secret"], [("Vault passphrase", current + "\n")])
    check("rekey keeps a secret set while it was making its new keys (one lock across reload and save)",
          waited and rc == 0 and rrc == 0 and rc2 == 0 and "no secret named" not in out2, (waited, rc, rrc, out2[-300:]))

    # --- 6. audit sinks: four kinds at once ----------------------------------------------------------------
    rx = http_server(Receiver)
    hp = rx.server_address[1]
    certs, err = make_certs(td)
    check("a throwaway CA and server certificate", certs is not None, err)
    ca, srv_cert, srv_key = certs if certs else (None, None, None)
    sl = SyslogTLS(srv_cert, srv_key) if certs else None
    for kind, url, why in (("webhook", "http://example.com/x", "http only for loopback"),
                           ("syslog", f"https://127.0.0.1:{hp}/x", "syslog+tls"),
                           ("syslog", f"syslog://127.0.0.1:{hp}", "syslog+tls"),
                           ("otlp", "https://user:pw@example.com/", "plain URL")):
        r = client("audit-sink", "add", "x", "--kind", kind, "--url", url)
        check(f"audit-sink add refuses {kind} {url}", r.returncode == 64, r.stderr)
    r = client("audit-sink", "add", "x", "--kind", "webhook", "--url", f"http://127.0.0.1:{hp}/hook")
    check("audit-sink add without a terminal is refused", r.returncode != 0 and "own terminal" in r.stderr)
    keys = {}
    collector_tok = "dummy-collector-tok-9"
    sinks = [("hook", "webhook", f"http://127.0.0.1:{hp}/hook", ["--header", "X-Test: one"], []),
             ("otel", "otlp", f"http://127.0.0.1:{hp}/otlp", ["--header", "X-Collector-Token: {ask}"],
              [("Value of the X-Collector-Token header", collector_tok + "\n")]),
             ("sys", "syslog", f"syslog+tls://127.0.0.1:{sl.port if sl else 9}", ["--ca", ca or "/dev/null"], []),
             ("phone", "ntfy", "ask", [], [("URL for the audit sink phone", f"http://127.0.0.1:{hp}/ntfy/topic-dummy\n")])]
    for name, kind, url, extra_args, more in sinks:
        rc, out = tty([BIN, "audit-sink", "add", name, "--kind", kind, "--url", url, *extra_args],
                      [("Vault passphrase", current + "\n"), *more])
        m = KEY_RE.search(out)
        keys[name] = m.group(1) if m else None
        check(f"audit-sink add {name} ({kind})" + (" (typed with the echo off)" if more else ""), rc == 0
              and f"Added the audit sink {name}" in out and (kind == "ntfy" or keys[name]) and collector_tok not in out
              and "topic-dummy" not in out, out)
    rc, out = tty([BIN, "audit-sink", "show", "hook", "--key"], [("Vault passphrase", current + "\n")])
    check("audit-sink show --key (with the passphrase) shows the same key", rc == 0 and keys["hook"] and keys["hook"] in out, out)
    starts = {}
    for x in audit_recs():
        if x.get("op") == "audit_sink_add":
            starts[x["name"]] = x["seq"] - 1
    r = client("audit-sink", "list")
    check("audit-sink list while locked: no settings shown", r.returncode == 0 and "unlock to see" in r.stdout
          and "127.0.0.1" not in r.stdout, r.stdout)

    # The proxy's limits made small, so the tests needn't wait long: 6 connections per VM (one more waits 1.5 s,
    # then gets 503), the head within 3 s, the answer within 3 s.
    T = {"MCOMP_VAULT_TEST_LISTENERS": my_name, "MCOMP_VAULT_TEST_PROMPT_TIMEOUT": "8", "MCOMP_VAULT_TEST_API_HTTP": "1",
         "MCOMP_VAULT_TEST_PROXY_SLOTS": "6", "MCOMP_VAULT_TEST_SLOT_WAIT": "1.5", "MCOMP_VAULT_TEST_HEAD_DEADLINE": "3", "MCOMP_VAULT_TEST_ANSWER_TIMEOUT": "3",
         "MCOMP_EVENTS_SPOOL": spool}
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    locked_rec = audit_recs()[-1]
    check("a refusal while locked is recorded without a creation MAC (the client has no vault key)", r.returncode == 3
          and locked_rec.get("result") == "locked" and "mac" not in locked_rec, locked_rec)
    u = start_unlock(["--for", "20m", *allow], T, passphrase=current)
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a fill while four sinks are set", r.returncode == 0, r.stdout + r.stderr)
    check("records the vault writes carry a creation MAC", "mac" in audit_recs()[-1], audit_recs()[-1])
    open(os.path.join(stubdir, "nested-value-next"), "w").close()
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a VM answer hiding the value in a nested field is withheld (output and audit never see it)", r.returncode == 4
          and "withheld" in js(r.stdout).get("reason", "") and not any(v in r.stdout + r.stderr for v in VALUES)
          and not any(v in open(alog).read() for v in VALUES), r.stdout)
    open(os.path.join(stubdir, "badtype-next"), "w").close()
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a VM answer with a field of the wrong type is an error", r.returncode == 4 and "unexpected form" in js(r.stdout).get("reason", ""),
          r.stdout)
    open(os.path.join(stubdir, "extra-next"), "w").close()
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("fields a fill doesn't answer with are dropped", r.returncode == 0 and "extra" not in r.stdout and "debug" not in r.stdout
          and "dropped" not in json.dumps(audit_recs()[-1]), r.stdout)

    def last_seq():
        recs = audit_recs()
        return recs[-1]["seq"] if recs else 0

    def received(name):
        if name == "sys":
            return [json.loads(m[m.index("] {") + 2:]) for m in (sl.got if sl else [])]
        if name == "otel":
            evs = []
            for g in rx.got:
                if g["path"].startswith("/otlp"):
                    for rl in json.loads(g["body"])["resourceLogs"]:
                        for s in rl["scopeLogs"]:
                            evs += [json.loads(lr["body"]["stringValue"]) for lr in s["logRecords"]]
            return evs
        if name in ("hook", "req"):
            return [json.loads(g["body"])["data"] for g in rx.got if g["path"] == "/" + name]
        return [json.loads(g["body"])["message"].removeprefix("[unverified] ") for g in rx.got if g["path"].startswith("/ntfy")]

    def caught_up(names):
        last = last_seq()
        return all((received(n)[-1]["seq"] if n != "phone" else int(received(n)[-1].split()[0][1:])) == last
                   for n in names if received(n))  and all(received(n) for n in names)

    wait_for(lambda: caught_up(["hook", "otel", "sys", "phone"]), 30)
    local = {x["seq"]: x for x in audit_recs()}
    for name in ("hook", "otel", "sys"):
        got = received(name)
        want = [local[s] for s in range(starts[name] + 1, last_seq() + 1)]
        check(f"sink {name}: every event since it was added, in order, identical to the local log", got == want,
              ([g.get("seq") for g in got], starts[name], last_seq()))
    ph = received("phone")
    nt = [json.loads(g["body"]) for g in rx.got if g["path"].startswith("/ntfy")]
    check("sink phone (ntfy): JSON publishing to the server's root (topic, title, message, priority, tags), no values",
          ph and all(re.match(r"#\d+ ", p) for p in ph) and any("fill name=web-login" in p for p in ph)
          and all(g["path"] == "/ntfy/" for g in rx.got if g["path"].startswith("/ntfy"))
          and all(n.get("topic") == "topic-dummy" and n.get("priority") in (3, 4) and "vault:" in n.get("title", "")
                  and "lock" in n.get("tags", []) for n in nt)
          and any(n["message"].startswith(f"[unverified] #{locked_rec['seq']} ") for n in nt)
          and not any(v in p for p in ph for v in VALUES), nt[-2:])
    hooks = [g for g in rx.got if g["path"] == "/hook"]
    ce = json.loads(hooks[-1]["body"]) if hooks else {}
    check("webhook: CloudEvents 1.0 structured JSON, the user's header, a signature", hooks
          and hooks[-1]["headers"].get("content-type") == "application/cloudevents+json; charset=utf-8" and ce.get("specversion") == "1.0"
          and ce.get("type", "").startswith("io.metavert.metacomputer.vault.")
          and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", ce.get("id", ""))
          and ce.get("id") == ce.get("data", {}).get("id") and ce.get("source", "").startswith("metacomputer/")
          and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", ce.get("time", ""))
          and hooks[-1]["headers"].get("x-test") == "one"
          and hooks[-1]["signature"] == "sha256=" + hmac.new(base64.b64decode(keys["hook"]), hooks[-1]["body"].encode(), hashlib.sha256).hexdigest(),
          hooks[-1] if hooks else None)
    otl = [g for g in rx.got if g["path"].startswith("/otlp")]
    body = json.loads(otl[-1]["body"]) if otl else {}
    res_attrs = {a["key"]: a["value"].get("stringValue") for a in body.get("resourceLogs", [{}])[0].get("resource", {}).get("attributes", [])}
    lr = body.get("resourceLogs", [{}])[0].get("scopeLogs", [{}])[0].get("logRecords", [{}])[-1]
    check("otlp: POST <url>/v1/logs, resource service.name, records with attributes, int64 as strings, the typed header", otl
          and otl[-1]["path"] == "/otlp/v1/logs" and res_attrs.get("service.name") == "metacomputer-vault" and "host.name" in res_attrs
          and otl[-1]["headers"].get("x-collector-token") == collector_tok
          and isinstance(lr.get("timeUnixNano"), str) and lr.get("severityNumber") in (9, 13, 17)
          and any(a["key"] == "metacomputer.seq" and isinstance(a["value"].get("intValue"), str) for a in lr.get("attributes", [])),
          otl[-1]["path"] if otl else None)
    msg = sl.got[-1] if sl and sl.got else ""
    check("syslog: RFC 5424 over TLS, facility user, APP-NAME metacomputer-vault, PROCID -, MSGID = op, "
          "[metacomputer@32473 seq family vm id hash prev sig]",
          re.match(r"^<1[1-4]>1 \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z \S+ metacomputer-vault - \S+ "
                   r"\[metacomputer@32473 seq=\"\d+\" family=\"vault\"( vm=\"[^\"]+\")? id=\"[0-9a-f-]{36}\" hash=\"[0-9a-f]{64}\" "
                   r"prev=\"[0-9a-f]{64}\" verified=\"(true|false)\" sig=\"sha256=[0-9a-f]{64}\"\] \{", msg) is not None, msg[:300])
    hook_ces = [json.loads(g["body"]) for g in rx.got if g["path"] == "/hook"]
    lce = next((c for c in hook_ces if c["data"].get("seq") == locked_rec["seq"]), None)
    otl_recs = [lr for g in rx.got if g["path"].startswith("/otlp") for rl in json.loads(g["body"])["resourceLogs"]
                for sc in rl["scopeLogs"] for lr in sc["logRecords"]]
    lotl = next((lr for lr in otl_recs if json.loads(lr["body"]["stringValue"]).get("seq") == locked_rec["seq"]), None)
    lsys = next((m for m in (sl.got if sl else []) if f'seq="{locked_rec["seq"]}"' in m), "")
    check("sinks mark the locked client's record unverified, and the vault's own records verified",
          lce is not None and lce.get("mcompverified") is False and any(c.get("mcompverified") is True for c in hook_ces)
          and lotl is not None and {"key": "metacomputer.verified", "value": {"boolValue": False}} in lotl["attributes"]
          and 'verified="false"' in lsys, (lce, lsys[:200]))
    r = client("audit", "--verify")
    check("audit --verify (unlocked): every record's creation MAC checks out; locked refusals counted as unverified",
          r.returncode == 0 and "checks out" in r.stdout and "written without the vault key" in r.stdout, r.stdout + r.stderr)
    os.chmod(alog, 0)
    r = client("audit", "--verify")
    os.chmod(alog, 0o600)
    check("audit --verify when audit.jsonl can't be read: fails (exit 4) with the reason, never 'checks out'", r.returncode == 4
          and "cannot read audit.jsonl" in r.stdout + r.stderr and "checks out" not in r.stdout, r.stdout + r.stderr)
    saved_log = open(alog).read()
    with open(alog, "a") as f:
        f.write("not a record\n")
    r = client("audit", "--verify")
    open(alog, "w").write(saved_log)
    check("audit --verify: a line that isn't a record is a problem", r.returncode == 4 and "isn't a record" in r.stdout, r.stdout)
    open(alog, "w").write("".join(l + "\n" for l in saved_log.splitlines()[:-1]))
    r = client("audit", "--verify")
    open(alog, "w").write(saved_log)
    check("audit --verify: a record this unlock wrote that's gone from the end of the log is a problem", r.returncode == 4
          and "written by this unlock, is missing" in r.stdout, r.stdout)
    open(alog, "w").write("")
    r = client("audit", "--verify")
    open(alog, "w").write(saved_log)
    check("audit --verify: an empty log is a problem", r.returncode == 4 and "no records" in r.stdout, r.stdout)
    check("no sink received a value", not any(v in g["body"] for g in rx.got for v in VALUES)
          and not any(v in m for m in (sl.got if sl else []) for v in VALUES))

    # captures for verify-audit.py
    cap = {n: os.path.join(td, f"cap-{n}.jsonl") for n in ("hook", "otel", "sys")}
    with open(cap["hook"], "w") as f:
        for g in hooks:
            f.write(json.dumps({"signature": g["signature"], "body": g["body"]}) + "\n")
    with open(cap["otel"], "w") as f:
        for g in otl:
            f.write(json.dumps({"signature": g["signature"], "body": g["body"]}) + "\n")
    open(cap["sys"], "w").write("".join(m + "\n" for m in (sl.got if sl else [])))
    r = subprocess.run([sys.executable, "-I", verify, alog], capture_output=True, text=True)
    check("verify-audit.py: the local log's hashes and chain check out", r.returncode == 0 and r.stdout.startswith("OK"), r.stdout + r.stderr)
    for n in ("hook", "otel", "sys"):
        r = subprocess.run([sys.executable, "-I", verify, "--key", keys[n] or "00", cap[n]], capture_output=True, text=True)
        check(f"verify-audit.py: the {n} capture (signatures, hashes, chain, no gaps)", r.returncode == 0 and "every event signed with this sink's key" in r.stdout,
              r.stdout + r.stderr)
    r = subprocess.run([sys.executable, "-I", verify, "--key", keys["otel"] or "00", cap["hook"]], capture_output=True, text=True)
    check("verify-audit.py: another sink's key fails", r.returncode == 1 and "signature" in r.stdout, r.stdout)
    sys_lines = open(cap["sys"]).read().splitlines()
    flipped = os.path.join(td, "sys-flipped.txt")
    open(flipped, "w").write("\n".join(l.replace('verified="false"', 'verified="true"') for l in sys_lines) + "\n")
    r = subprocess.run([sys.executable, "-I", verify, "--key", keys["sys"] or "00", flipped], capture_output=True, text=True)
    check("syslog: flipping verified=\"false\" to \"true\" breaks the signature (it covers the status and MSG)",
          any('verified="false"' in l for l in sys_lines) and r.returncode == 1 and "signature" in r.stdout, r.stdout[:300])
    open(flipped, "w").write("\n".join(re.sub(r' verified="(true|false)"', "", l) for l in sys_lines) + "\n")
    r = subprocess.run([sys.executable, "-I", verify, "--key", keys["sys"] or "00", "--require-verified", flipped], capture_output=True, text=True)
    check("...and removing the marker doesn't pass --require-verified (absent counts as unverified)", r.returncode == 1, r.stdout[:300])
    stripped_hook = os.path.join(td, "hook-nomarker.jsonl")
    with open(stripped_hook, "w") as f:
        for l in open(cap["hook"]).read().splitlines():
            w = json.loads(l)
            body = json.loads(w["body"])
            body.pop("mcompverified", None)
            b = json.dumps(body, sort_keys=True, separators=(",", ":"))
            f.write(json.dumps({"signature": "sha256=" + hmac.new(base64.b64decode(keys["hook"]), b.encode(), hashlib.sha256).hexdigest(),
                                "body": b}) + "\n")
    r = subprocess.run([sys.executable, "-I", verify, "--key", keys["hook"], "--require-verified", stripped_hook], capture_output=True, text=True)
    check("a webhook capture without the status (an older one) fails --require-verified", r.returncode == 1
          and "not producer-verified" in r.stdout, r.stdout[:300])
    r = subprocess.run([sys.executable, "-I", verify, cap["hook"]], capture_output=True, text=True)
    check("verify-audit.py: a signed capture without --key fails", r.returncode == 1, r.stdout)
    lines = open(alog).read().splitlines()
    bad = os.path.join(td, "audit-changed.jsonl")
    ev = json.loads(lines[3])
    ev["result"] = "denied" if ev.get("result") != "denied" else "ok"
    open(bad, "w").write("\n".join(lines[:3] + [json.dumps(ev, sort_keys=True, separators=(",", ":"))] + lines[4:]) + "\n")
    r = subprocess.run([sys.executable, "-I", verify, bad], capture_output=True, text=True)
    check("verify-audit.py: a changed event fails (hash)", r.returncode == 1 and "hash doesn't match" in r.stdout, r.stdout)
    open(bad, "w").write("\n".join(lines[:3] + lines[4:]) + "\n")
    r = subprocess.run([sys.executable, "-I", verify, bad], capture_output=True, text=True)
    check("verify-audit.py: a removed event fails (gap)", r.returncode == 1 and "missing" in r.stdout, r.stdout)

    # M6: capture mode needs every event signed; local mode keeps order; checkpoints pin the ends
    def vrun(*args):
        return subprocess.run([sys.executable, "-I", verify, *args], capture_output=True, text=True)
    hook_lines = open(cap["hook"]).read().splitlines()
    raw_cap = os.path.join(td, "raw-as-capture.jsonl")
    open(raw_cap, "w").write("\n".join(canon(json.loads(json.loads(l)["body"])["data"]) for l in hook_lines) + "\n")
    r = vrun("--key", keys["hook"], raw_cap)
    check("verify-audit.py --key: raw events in place of the signed wrappers fail (capture mode needs every event signed)",
          r.returncode == 1 and "unsigned event" in r.stdout, r.stdout)
    mixed = os.path.join(td, "mixed.jsonl")
    open(mixed, "w").write("\n".join(hook_lines + [lines[-1]]) + "\n")
    r = vrun("--key", keys["hook"], mixed)
    check("...and so does a signed capture with one raw event added", r.returncode == 1 and "unsigned event" in r.stdout, r.stdout)
    for what, content in (("a repeated line", lines + [lines[-1]]), ("two lines swapped", lines[:-2] + [lines[-1], lines[-2]])):
        open(bad, "w").write("\n".join(content) + "\n")
        r = vrun(bad)
        check(f"verify-audit.py, local log: {what} fails", r.returncode == 1, r.stdout)
    rep_cap = os.path.join(td, "cap-repeat.jsonl")
    open(rep_cap, "w").write("\n".join(hook_lines + [hook_lines[-1]]) + "\n")
    r = vrun("--key", keys["hook"], rep_cap)
    check("verify-audit.py, capture: a repeated delivery is fine", r.returncode == 0 and "repeated" in r.stdout, r.stdout)
    open(rep_cap, "w").write("\n".join(hook_lines[:-2] + [hook_lines[-1], hook_lines[-2]]) + "\n")
    r = vrun("--key", keys["hook"], rep_cap)
    check("verify-audit.py, capture: events out of order fail", r.returncode == 1 and "out of order" in r.stdout, r.stdout)
    head = client("audit", "--head").stdout.strip()
    r = vrun("--expect-start", "1", "--expect-head", head, alog)
    check("verify-audit.py --expect-start/--expect-head with the right checkpoints: OK, and it says what that proves",
          r.returncode == 0 and "ends at the expected head" in r.stdout and "Doesn't prove: that nothing came before" not in r.stdout, r.stdout)
    r = vrun("--expect-head", "1:" + "0" * 64, alog)
    check("verify-audit.py --expect-head with another head fails", r.returncode == 1 and "expected head" in r.stdout, r.stdout)
    sliced = os.path.join(td, "slice.jsonl")
    open(sliced, "w").write("\n".join(lines[5:]) + "\n")
    r = vrun(sliced)
    check("verify-audit.py on a slice: OK as far as it goes, and says it can't prove nothing is missing at the ends",
          r.returncode == 0 and "Doesn't prove" in r.stdout and "nothing came before" in r.stdout, r.stdout)
    r = vrun("--expect-start", "1", sliced)
    check("...with --expect-start 1 the slice fails", r.returncode == 1, r.stdout)
    r = vrun("--key", keys["hook"], "--require-verified", cap["hook"])
    check("verify-audit.py --require-verified fails on events written without the vault key", r.returncode == 1
          and "not producer-verified" in r.stdout, r.stdout)
    ul = subprocess.run(["/usr/bin/log", "show", "--predicate", 'subsystem == "io.metavert.metacomputer.vault.test"', "--last", "5m",
                         "--style", "ndjson"], capture_output=True, text=True, timeout=120).stdout
    fill_hash = next((x["hash"] for x in audit_recs() if x.get("op") == "fill"), "none")
    check("every event also goes to the unified log (subsystem io.metavert.metacomputer.vault[.test])", fill_hash in ul)

    # optional down: uses go on; required down: uses stop
    rx.down.add("/hook")
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("an optional sink that's down doesn't stop a use", r.returncode == 0, r.stdout)
    sl_list = client("audit-sink", "list")
    check("audit-sink list (unlocked): kinds, masked URLs, backlog and the last error", "hook  webhook http://127.0.0.1:" in sl_list.stdout
          and "/..." in sl_list.stdout and "topic-dummy" not in sl_list.stdout and "waiting; last error" in sl_list.stdout, sl_list.stdout)
    rc, out = tty([BIN, "audit-sink", "add", "req", "--kind", "webhook", "--url", f"http://127.0.0.1:{hp}/req", "--required"],
                  [("Vault passphrase", current + "\n")])
    m = KEY_RE.search(out)
    keys["req"] = m.group(1) if m else None
    check("add a required sink while unlocked", rc == 0 and "uses are refused while it can't be reached" in out, out)
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a required sink that's up: uses work", r.returncode == 0, r.stdout)
    rx.down.add("/req")
    n = len(calls())
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a required sink that's down: the use is refused (exit 2) before anything reaches the VM", r.returncode == 2
          and "required audit sink req" in js(r.stdout).get("reason", "") and len(calls()) == n, r.stdout)
    begins = [x for x in audit_recs() if x.get("op") == "begin"]
    check("with a required sink, each use is first recorded as 'begin' (what, which secret, who)", begins
          and begins[-1].get("use") == "fill" and begins[-1].get("name") == "web-login" and "requester" in begins[-1], begins[-1:])
    rx.down.discard("/req")
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("...and works again once it's back", r.returncode == 0, r.stdout)
    rx.down.add("/req")
    st_path = os.path.join(vdir, "sinks", "req.state")
    stj = json.load(open(st_path))
    stj.update(delivered=999999, error=None, failures=0)
    json.dump(stj, open(st_path, "w"))
    n = len(calls())
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a forged sinks/req.state (its place far ahead, no error) doesn't let a use through while req is down", r.returncode == 2
          and "required audit sink req" in js(r.stdout).get("reason", "") and len(calls()) == n, r.stdout)
    rx.down.discard("/req")
    os.chmod(alog, 0o400)
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    os.chmod(alog, 0o600)
    check("an audit log that can't be written stops uses while a sink is required, and says so", r.returncode == 2
          and "audit log can't be written" in js(r.stdout).get("reason", "") and len(calls()) == n, r.stdout)
    check("...the unlock terminal warns about it", "the audit log couldn't be written" in u.text())
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("...and uses work again once it can be written", r.returncode == 0, r.stdout)
    r = client("audit-sink", "add", "sysreq", "--kind", "syslog", "--url", f"syslog+tls://127.0.0.1:{sl.port if sl else 9}", "--required")
    check("a syslog sink can't be --required (TLS delivery has no acknowledgement from the receiver)", r.returncode == 64
          and "acknowledges" in r.stderr, r.stderr)
    rx.partial.add("/otlpreq")
    rc, out = tty([BIN, "audit-sink", "add", "otlpreq", "--kind", "otlp", "--url", f"http://127.0.0.1:{hp}/otlpreq", "--required"],
                  [("Vault passphrase", current + "\n")])
    n = len(calls())
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a required OTLP sink answering 200 with rejected records (partial success) doesn't count as acceptance: the use is refused",
          rc == 0 and r.returncode == 2 and "rejected 1 record" in js(r.stdout).get("reason", "") and len(calls()) == n, r.stdout)
    rx.partial.discard("/otlpreq")
    tty([BIN, "audit-sink", "remove", "otlpreq"], [("Vault passphrase", current + "\n")])
    rx.down.discard("/hook")
    r = client("audit-sink", "test", "hook")
    check("audit-sink test: the optional sink catches up", r.returncode == 0 and "Sent: hook has every event" in r.stdout, r.stdout + r.stderr)
    rc, out = tty([BIN, "audit-sink", "remove", "phone"], [("Vault passphrase", current + "\n")])
    check("audit-sink remove phone", rc == 0 and "Removed" in out, out)
    removed_at = max(x["seq"] for x in audit_recs() if x.get("op") == "audit_sink_remove")
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    wait_for(lambda: caught_up(["hook", "otel", "sys", "req"]), 30)
    phone_seqs = [int(x.split()[0][1:]) for x in received("phone")]
    check("after removing one sink the others still get every event, and it gets none", caught_up(["hook", "otel", "sys", "req"])
          and r.returncode == 0 and max(phone_seqs) < removed_at, (last_seq(), removed_at, phone_seqs[-3:]))
    hooks = [g for g in rx.got if g["path"] == "/hook"]
    with open(cap["hook"], "w") as f:
        for g in hooks:
            f.write(json.dumps({"signature": g["signature"], "body": g["body"]}) + "\n")
    r = subprocess.run([sys.executable, "-I", verify, "--key", keys["hook"], cap["hook"]], capture_output=True, text=True)
    check("the webhook that was down got the missed events in order (spool and retry): no gaps", r.returncode == 0, r.stdout)
    if certs:
        rc, out = tty([BIN, "audit-sink", "add", "noca", "--kind", "syslog", "--url", f"syslog+tls://127.0.0.1:{sl.port}"],
                      [("Vault passphrase", current + "\n")])
        r = client("audit-sink", "test", "noca")
        check("a syslog sink without --ca refuses the self-signed server (system trust only)", r.returncode != 0, r.stdout + r.stderr)
        tty([BIN, "audit-sink", "remove", "noca"], [("Vault passphrase", current + "\n")])
        # A receiver of its own, so the other syslog sink's deliveries meanwhile don't count.
        sl_bad = SyslogTLS(srv_cert, srv_key)
        tty([BIN, "audit-sink", "add", "badhost", "--kind", "syslog", "--url", f"syslog+tls://localhost:{sl_bad.port}", "--ca", ca],
            [("Vault passphrase", current + "\n")])
        r = client("audit-sink", "test", "badhost")
        check("a syslog sink with --ca still checks the host name (the certificate is for 127.0.0.1, the URL says localhost)",
              r.returncode != 0 and sl_bad.handshake_failures > 0 and not sl_bad.got, (r.stdout + r.stderr, sl_bad.handshake_failures))
        tty([BIN, "audit-sink", "remove", "badhost"], [("Vault passphrase", current + "\n")])

    # H2: the dispatcher signs only what the vault wrote
    rx.down.add("/hook")
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    forged = rewrite_last(lambda e: e.update(result="denied", reason="forged by the agent"))
    rx.down.discard("/hook")
    r = client("audit-sink", "test", "hook")
    hook_data = [json.loads(g["body"])["data"] for g in rx.got if g["path"] == "/hook"]
    at_seq = [d for d in hook_data if d.get("seq") == forged["seq"]]
    check("a record changed after the vault wrote it (hash recomputed) is never signed for a sink: an audit_tamper notice goes instead",
          forged.get("op") == "fill" and at_seq and all(d.get("op") == "audit_tamper" for d in at_seq)
          and not any("forged by the agent" in json.dumps(d) for d in hook_data), at_seq)
    check("...the unlock terminal says so, and the local log records it", "AUDIT LOG CHANGED" in u.text()
          and any(x.get("op") == "audit_tamper" and x.get("target_seq") == forged["seq"] for x in audit_recs()))
    r = client("audit", "--verify")
    check("audit --verify finds the changed record (exit 4)", r.returncode == 4 and f"seq {forged['seq']}" in r.stdout, r.stdout)
    rc, out = tty([BIN, "audit-sink", "add", "req2", "--kind", "webhook", "--url", f"http://127.0.0.1:{hp}/second", "--required"],
                  [("Vault passphrase", current + "\n")])
    rx.down.add("/req")
    n = len(calls())
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    first = [x for x in audit_recs() if x.get("op") == "begin"][-1]
    acked_by_req2 = any(json.loads(g["body"])["data"].get("id") == first["id"] for g in rx.got if g["path"] == "/second")
    keep = [l for l in open(alog).read().splitlines() if json.loads(l)["seq"] < first["seq"]]
    open(alog, "w").write("".join(l + "\n" for l in keep))           # cut the log back before that begin
    rx.down.discard("/req")
    rx.down.add("/second")
    r2 = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    check("a stale acknowledgement can't authorize another use: with req down, req2 acked begin seq N; the log is cut back; "
          "with req2 down, a new use at the same seq N is refused", r.returncode == 2 and acked_by_req2 and r2.returncode == 2
          and len(calls()) == n, (r.stdout[:120], r2.stdout[:200]))
    check("...and the terminal says the log was cut back", "the log was cut back" in u.text(), u.text()[-300:])
    rx.down.discard("/second")
    for _ in range(3):
        r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
        if r.returncode == 0:
            break
    check("...once both sinks are back, uses go through again (after the refusals the cut-back log causes)", r.returncode == 0, r.stdout)
    tty([BIN, "audit-sink", "remove", "req2"], [("Vault passphrase", current + "\n")])
    tcap = os.path.join(td, "cap-tamper.jsonl")
    with open(tcap, "w") as f:
        for g in rx.got:
            if g["path"] == "/hook":
                f.write(json.dumps({"signature": g["signature"], "body": g["body"]}) + "\n")
    r = subprocess.run([sys.executable, "-I", verify, "--key", keys["hook"], tcap], capture_output=True, text=True)
    check("verify-audit.py reports the tamper notice in a capture", r.returncode == 1 and "tamper notice" in r.stdout, r.stdout)

    # --- 7. requester identities, approval scopes, projects ---------------------------------------------
    shutil.rmtree(spool, ignore_errors=True)       # the unlock process makes the spool folder itself when it's missing
    p, outs = as_claude(proj1, ["fill", "web-each", "--vm", "opsvm", "--field", "password"],
                        ["fill", "web-each", "--vm", "opsvm", "--field", "password"])
    u.expect("Type web-each to allow")
    asked = u.text()
    ev = wait_for(lambda: [e for e in spool_events() if e.get("type") == "approval_waiting"], 5)
    check("the prompt names the requester: Claude Code in its project, with its pid", f"Asked by Claude Code in {proj1} (pid {p.pid})" in asked,
          asked[-400:])
    check("while it waits, a needs_you/approval_waiting event is in the spool (which secret, the VM, the project; what to type)", ev
          and ev[0].get("family") == "needs_you" and ev[0].get("vm") == "opsvm" and "web-each" in ev[0].get("message", "")
          and ev[0].get("project") == proj1 and ev[0].get("action") == "Type web-each in the vault's terminal to allow"
          and all(SPOOL_NAME.fullmatch(n) for n in os.listdir(spool)) and (os.stat(spool).st_mode & 0o777) == 0o700
          and not any(v in json.dumps(ev) for v in VALUES), ev)
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import events as mcevents   # the merged event stream's own spool parser
    taken = mcevents.spool_event(ev[0], time.time()) if ev else None
    check("the event stream takes the vault's spool file as is (needs_you/approval_waiting isn't reserved or refused)",
          taken is not None and taken["family"] == "needs_you" and taken["type"] == "approval_waiting" and taken["origin"] == "spool",
          taken)
    u.send("web-each session\n")
    res = claude_results(p, outs)
    check("'NAME session': this use and the next from the same Claude Code process, without asking again",
          [x[0] for x in res] == [0, 0] and u.text().count("Type web-each to allow") == asked.count("Type web-each to allow"), res)
    recs = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"]
    check("the audit log records the requester and the approval scope", recs[-2].get("approval") == "each:session"
          and recs[-1].get("approval") == "grant:session" and recs[-1].get("requester", {}).get("project") == proj1
          and recs[-1].get("requester", {}).get("claude_pid") == p.pid, recs[-2:])
    p, outs = as_claude(proj1, ["fill", "web-each", "--vm", "opsvm", "--field", "password"])
    u.expect(f"(pid {p.pid})")
    u.expect("Type web-each to allow")
    u.send("web-each\n")
    res = claude_results(p, outs)
    check("another Claude Code process (a new session) is asked again", res[0][0] == 0, res)
    asked_lines = [l.strip("\r") for l in u.text().split("\n")]
    check("the requester is on a line of its own under the question", any(l.startswith(f"  Asked by Claude Code in {proj1} (pid ")
                                                                          for l in asked_lines), asked_lines[-6:])
    evil = os.path.join(td, "evil\n  Asked by Claude Code in /trusted-place (pid 1).")
    os.makedirs(evil)
    p, outs = as_claude(evil, ["fill", "web-each", "--vm", "opsvm", "--field", "password"])
    u.expect(f"(pid {p.pid})")
    u.expect("Type web-each to allow")
    u.send("no\n")
    res = claude_results(p, outs)
    asked_lines = [l.strip("\r") for l in u.text().split("\n")]
    check("a project folder whose name holds a newline can't add a line to the question", res[0][0] == 2
          and not any(l.startswith("  Asked by Claude Code in /trusted-place") for l in asked_lines), asked_lines[-6:])
    h = detached_start("fill", "web-each", "--vm", "opsvm", "--field", "password")
    u.expect("not from Claude Code")
    u.expect("Type web-each to allow")
    u.send("web-each session\n")
    rc, out = detached_wait(h)
    check("a request that isn't from Claude Code says so, and 'session' then allows only this use", rc == 0, out)
    # Codex: a session of its own, like Claude Code's; its app server (one process for every conversation) is none
    fcodex, fserver = os.path.join(td, "fakecodex", "codex"), os.path.join(td, "fakeserver", "codex")
    for path, src in ((fcodex, FAKE_CLAUDE_C), (fserver, FAKE_CODEX_SERVER_C)):
        os.makedirs(os.path.dirname(path))
        open(path + ".c", "w").write(src)
        subprocess.run(["cc", "-o", path, path + ".c"], capture_output=True, text=True)
    p, outs = as_agent([fcodex], proj1, ["fill", "web-each", "--vm", "opsvm", "--field", "password"],
                       ["fill", "web-each", "--vm", "opsvm", "--field", "password"])
    u.expect(f"Asked by Codex in {proj1} (pid {p.pid})")
    u.expect("Type web-each to allow")
    n0 = u.text().count("Type web-each to allow")
    u.send("web-each session\n")
    res = claude_results(p, outs)
    recs = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"]
    check("a Codex session is a session: named in the question, and 'NAME session' covers its next use",
          [x[0] for x in res] == [0, 0] and u.text().count("Type web-each to allow") == n0
          and recs[-1].get("approval") == "grant:session" and recs[-1].get("requester", {}).get("agent") == "Codex", (res, recs[-1:]))
    p, outs = as_agent([fserver, "app-server"], proj1, ["fill", "web-each", "--vm", "opsvm", "--field", "password"],
                       ["fill", "web-each", "--vm", "opsvm", "--field", "password"])
    u.expect("under Codex's app server")
    u.expect("Type web-each to allow")
    u.send("web-each session\n")
    u.expect("under Codex's app server")
    u.expect("Type web-each to allow")
    u.send("web-each\n")
    res = claude_results(p, outs)
    recs = [x for x in audit_recs() if x.get("op") == "fill" and x.get("name") == "web-each"]
    check("Codex's app server is no session (its conversations share the process): 'session' allows only that use, "
          "the next asks again", [x[0] for x in res] == [0, 0] and not recs[-1].get("requester", {}).get("claude_pid")
          and recs[-1].get("requester", {}).get("agent") == "Codex app server", (res, recs[-2:]))
    h = detached_start("fill", "web-each", "--vm", "opsvm", "--field", "password")
    u.expect("Type web-each to allow")
    u.send("web-each unlock\n")
    rc, out = detached_wait(h)
    n_prompts = u.text().count("Type web-each to allow")
    rc2, out2 = detached("fill", "web-each", "--vm", "opsvm", "--field", "password")
    p, outs = as_claude(proj2, ["fill", "web-each", "--vm", "opsvm", "--field", "password"])
    res = claude_results(p, outs)
    check("'NAME unlock': no more questions for that secret from anyone until the vault locks", rc == 0 and rc2 == 0 and res[0][0] == 0
          and u.text().count("Type web-each to allow") == n_prompts, (rc, rc2, res))
    p, outs = as_claude(proj2, ["fill", "scoped", "--vm", "opsvm", "--field", "password"])
    res = claude_results(p, outs)
    check("--project: a Claude Code session in another project is refused", res[0][0] == 2
          and "limited to Claude Code sessions in" in js(res[0][1]).get("reason", ""), res)
    rc, out = detached("fill", "scoped", "--vm", "opsvm", "--field", "password")
    check("--project: a request that isn't from Claude Code is refused", rc == 2, out)
    p, outs = as_claude(os.path.join(proj1, "sub"), ["fill", "scoped", "--vm", "opsvm", "--field", "password"])
    res = claude_results(p, outs)
    check("--project: a Claude Code session in that project (or below it) may use it", res[0][0] == 0, res)
    p, outs = as_agent([fcodex], os.path.join(proj1, "sub"), ["fill", "scoped", "--vm", "opsvm", "--field", "password"])
    res = claude_results(p, outs)
    check("--project: so may a Codex session there", res[0][0] == 0, res)
    p, outs = as_agent([fserver, "app-server"], proj1, ["fill", "scoped", "--vm", "opsvm", "--field", "password"])
    res = claude_results(p, outs)
    check("--project: Codex's app server, even started there, may not (it names no session or project)", res[0][0] == 2, res)
    r = client("list")
    check("list: ages (created, rotated / never rotated) and projects", "rotated" in r.stdout and "never rotated" in r.stdout
          and "only in " + proj1 in r.stdout, r.stdout)

    # --- 8. API tokens through the proxy ------------------------------------------------------------------
    r = client("token", "web-login", "--vm", "opsvm")
    check("a token for a password secret is refused", r.returncode == 2 and "tokens are for api secrets" in js(r.stdout).get("reason", ""))
    r = client("token", "api1", "--vm", "oldvm")
    check("a token for a VM bound without a proxy account: refused, says to rebuild", r.returncode != 0
          and "rebuild" in js(r.stdout).get("reason", ""), r.stdout)
    ts_flag = os.path.join(stubdir, "no-proxy-account")
    open(ts_flag, "w").close()
    r = client("token", "api1", "--vm", "refusevm")
    os.remove(ts_flag)
    check("a VM without the mcproxy account (refused login): fails closed, says to rebuild", r.returncode != 0
          and "rebuild" in js(r.stdout).get("reason", ""), r.stdout)
    r = subprocess.run([mc, "secrets", "token", "api1", "--ttl", "10m"], capture_output=True, text=True, env=dict(env, **wrapper), timeout=120)
    outputs.append(r.stdout + r.stderr)
    tok = js(r.stdout)
    check("metacomputer secrets token (the VM from the config): a short-lived token and the in-VM base URL", r.returncode == 0
          and tok.get("token", "").startswith("mct_") and tok.get("base_url") == "http://127.0.0.1:18765/api1/"
          and tok.get("header") == "Authorization: Bearer " + tok.get("token", "") and UPSTREAM_VALUE not in r.stdout, r.stdout + r.stderr)
    tun = [c for c in calls() if c.get("kind") == "proxy:tunnel"]
    fwd = tun[-1]["argv"][tun[-1]["argv"].index("-R") + 1] if tun else ""
    check("the tunnel: ssh as mcproxy with the proxy key, -N, one reverse forward to the VM's 127.0.0.1:18765", tun
          and tun[-1]["user"] == "mcproxy" and f"IdentityFile={sshdir}/proxy" in tun[-1]["argv"] and "-N" in tun[-1]["argv"]
          and "ExitOnForwardFailure=yes" in tun[-1]["argv"] and fwd.startswith("127.0.0.1:18765:127.0.0.1:"), tun[-1:] )
    mport = int(fwd.rsplit(":", 1)[1]) if fwd else 9
    adir = os.path.join(td, "logs", "actions")
    afiles = [os.path.join(d, f) for d, _, fs in os.walk(adir) for f in fs if f.endswith(".jsonl")]
    arecs = [json.loads(l) for f in afiles for l in open(f)]
    check("the signed action log records the token's id, never the token", any(x.get("type") == "secrets_token"
          and x.get("token_id") == tok.get("token_id") for x in arecs) and not any(tok.get("token", "x") in open(f).read() for f in afiles))

    def vm_request(method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", mport, timeout=30)
        c.request(method, path, body=body, headers=headers or {})
        resp = c.getresponse()
        data = resp.read().decode(errors="replace")
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        c.close()
        return resp.status, hdrs, data

    bearer = {"Authorization": "Bearer " + tok.get("token", "")}
    n_up = len(up.got)
    st, hd, data = vm_request("GET", "/api1/v1/echo?q=dummy-query-77", bearer)
    seen = up.got[-1] if len(up.got) > n_up else {}
    check("the API got the real key in the real header (the VM sent only the token)", st == 200
          and seen.get("headers", {}).get("authorization") == "Bearer " + UPSTREAM_VALUE and seen.get("path") == "/v1/echo?q=dummy-query-77",
          (st, seen))
    check("an API that echoes the key: it's withheld from the answer's body and headers", UPSTREAM_VALUE not in data
          and UPSTREAM_VALUE not in json.dumps(hd) and "[key withheld by metacomputer secrets]" in data
          and "[key withheld" in hd.get("x-echo", ""), (data[:300], hd))
    st, hd, data = vm_request("GET", "/api1/v1/split", bearer)
    check("...also when the key arrives split across reads", st == 200 and UPSTREAM_VALUE not in data and "key withheld" in data, data)
    st, _, data = vm_request("GET", "/api1/v1/echo", {"X-Metacomputer-Token": tok.get("token", "")})
    check("X-Metacomputer-Token works too", st == 200, (st, data))
    rx.down.add("/req")
    st, _, data = vm_request("GET", "/api1/v1/echo", bearer)
    check("a required audit sink that goes down: the very next proxied request is refused (503), no earlier failure needed",
          st == 503 and "required audit sink req" in data, (st, data))
    rx.down.discard("/req")
    st, _, _ = vm_request("GET", "/api1/v1/echo", bearer)
    req_begins = [d for d in received("req") if d.get("op") == "begin" and d.get("use") == "api"]
    check("...and passes again once it's back, each request's own begin record having reached the sink first", st == 200
          and req_begins and req_begins[-1].get("token_id") == tok.get("token_id") and "path" not in req_begins[-1],
          (st, req_begins[-1:]))

    rx.slow.add("/req")
    holder = {}
    n_up = len(up.got)
    th = threading.Thread(target=lambda: holder.update(r=vm_request("GET", "/api1/v1/echo", bearer)))
    th.start()
    time.sleep(1.5)
    rr = client("revoke", tok.get("token_id", "00000000"))
    th.join(60)
    rx.slow.discard("/req")
    check("a request whose token is revoked while it waits on the required sink isn't forwarded (401)", rr.returncode == 0
          and holder.get("r", (0,))[0] == 401 and "ended while this request waited" in holder["r"][2] and len(up.got) == n_up, holder.get("r"))
    tok = js(client("token", "api1", "--vm", "opsvm", "--ttl", "10m").stdout)
    bearer = {"Authorization": "Bearer " + tok.get("token", "")}

    # M2: the request parser, against malformed and hostile requests
    def raw_request(data):
        sk = socket.create_connection(("127.0.0.1", mport), timeout=15)
        resp = b""
        try:
            sk.sendall(data)
            sk.shutdown(socket.SHUT_WR)
            while True:
                d = sk.recv(4096)
                if not d:
                    break
                resp += d
        except OSError:
            pass
        finally:
            sk.close()
        m = re.match(rb"HTTP/1\.1 (\d{3})", resp)
        return int(m.group(1)) if m else None
    auth = b"Authorization: Bearer " + tok.get("token", "").encode() + b"\r\n"
    post = b"POST /api1/v1/echo HTTP/1.1\r\nHost: x\r\n" + auth
    cases = [
        ("a chunk size that would overflow after a 1-byte chunk", post + b"Transfer-Encoding: chunked\r\n\r\n1\r\nA\r\n7fffffffffffffff\r\n", 400),
        ("a 4 GB chunk", post + b"Transfer-Encoding: chunked\r\n\r\n1\r\nA\r\nFFFFFFFF\r\n", 413),
        ("a 20-digit Content-Length", post + b"Content-Length: 99999999999999999999\r\n\r\n", 400),
        ("a Content-Length over 32 MB", post + b"Content-Length: 4000000000\r\n\r\n", 413),
        ("Content-Length and Transfer-Encoding together", post + b"Content-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 400),
        ("two Content-Lengths", post + b"Content-Length: 1\r\nContent-Length: 2\r\n\r\nab", 400),
        ("a Transfer-Encoding other than chunked", post + b"Transfer-Encoding: gzip, chunked\r\n\r\n0\r\n\r\n", 501),
        ("a bare CR in a header", post + b"X-A: b\rc\r\n\r\n", 400),
        ("a bare LF in a header", post + b"X-A: b\nc\r\n\r\n", 400),
        ("a NUL in a header value", post + b"X-A: b\x00c\r\n\r\n", 400),
        ("a folded header", post + b"X-A: b\r\n c\r\n\r\n", 400),
        ("an absolute-URI request line", b"GET http://127.0.0.1:1/api1/v1/echo HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n", 400),
        ("CONNECT", b"CONNECT api.example:443 HTTP/1.1\r\nHost: api.example:443\r\n\r\n", 405),
        ("a request line with two spaces", b"GET  /api1/v1/echo HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n", 400),
        ("HTTP/2.0 in the request line", b"GET /api1/v1/echo HTTP/2.0\r\nHost: x\r\n" + auth + b"\r\n", 505),
    ]
    for what, data, want in cases:
        st = raw_request(data)
        check(f"request parser: {what} is refused ({want})", st == want, st)
    r = client("status")
    st, _, _ = vm_request("GET", "/api1/v1/echo", bearer)
    check("...and the unlock process is still serving afterwards", r.returncode == 0 and st == 200, (r.stdout[-200:], st))
    st, _, data = vm_request("POST", "/api1/v1/echo", dict(bearer, **{"Content-Type": "application/json", "Connection": "X-Drop",
                                                                      "X-Drop": "1", "Proxy-Authorization": "dummy", "X-Keep": "2"}),
                             body=b'{"a": 1}')
    seen = up.got[-1]
    check("POST bodies pass; hop-by-hop headers and those named in Connection are dropped; others pass", st == 200
          and seen["body"] == '{"a": 1}' and "x-drop" not in seen["headers"] and "proxy-authorization" not in seen["headers"]
          and seen["headers"].get("x-keep") == "2", seen)
    st, _, _ = vm_request("GET", "/api1/v1/echo")
    check("no token: 401", st == 401)
    st, _, _ = vm_request("GET", "/api1/v1/echo", {"Authorization": "Bearer mct_dummy-wrong-token"})
    check("a wrong token: 401", st == 401)
    st, _, _ = vm_request("GET", "/api2/echo", {"x-api-key": tok.get("token", "")})
    check("a token works only for its own secret", st == 401)
    for path in ("/api1/v2/echo", "/api1/v1/../admin", "/api1/v1/%2e%2e/admin", "/api1/v1/a%2Fb", "/api1/v1/.%2E/x"):
        st, _, _ = vm_request("GET", path, bearer)
        check(f"path {path}: refused (403)", st == 403, st)
    st, hd, data = vm_request("GET", "/api1/v1/redirect-other", bearer)
    check("a redirect to another host: refused (502), not followed", st == 502 and "another host" in data, (st, data))
    st, hd, _ = vm_request("GET", "/api1/v1/redirect-same", bearer)
    check("a redirect on the same host comes back through the proxy", st == 302
          and hd.get("location") == "http://127.0.0.1:18765/api1/v1/echo?x=1", (st, hd))
    # F6: the Location is rewritten from the encoded path: escapes and the trailing slash stay
    st, hd, _ = vm_request("GET", "/api1/v1/redirect-encoded", bearer)
    check("a same-host redirect keeps the API's encoding (%2F, %20, a trailing slash, the query's %26)", st == 302
          and hd.get("location") == "http://127.0.0.1:18765/api1/v1/a%2Fb/c%20d/?q=%26x", (st, hd))
    # F3: the path prefix by whole segments (api1 was set with --api-path-prefix /v1/, kept as /v1)
    for path in ("/api1/v10/echo", "/api1/v1-admin", "/api1/v1-admin/users", "/api1/v1%2Fadmin", "/api1/v1%2fadmin"):
        n_up = len(up.got)
        st, _, _ = vm_request("GET", path, bearer)
        check(f"path {path}: outside the prefix /v1 by segments, refused (403), nothing forwarded", st == 403 and len(up.got) == n_up, st)
    st, _, _ = vm_request("GET", "/api1/v1", bearer)
    check("path /api1/v1 (the prefix itself): allowed", st == 200 and up.got[-1]["path"] == "/v1", (st, up.got[-1:]))

    # A7: the token is checked right after the head, before any body is read
    sk = socket.create_connection(("127.0.0.1", mport), timeout=15)
    t0 = time.time()
    sk.sendall(b"POST /api1/v1/echo HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer mct_dummy-wrong-token\r\n"
               b"Content-Length: 100000\r\n\r\n")
    resp = b""
    try:
        while True:
            d = sk.recv(4096)
            if not d:
                break
            resp += d
    except OSError:
        pass
    sk.close()
    check("a wrong token with a body still to come: 401 at once, the body never waited for",
          resp.startswith(b"HTTP/1.1 401") and time.time() - t0 < 2, (resp[:60], round(time.time() - t0, 1)))

    # A7: a client that drips its head is cut off at the head deadline (3 s in this test build), however often a
    # byte arrives; and one VM's connections are limited (6 here): one more waits 1.5 s for a slot, then gets 503.
    def drip(out):
        s2 = socket.create_connection(("127.0.0.1", mport), timeout=30)
        s2.settimeout(0.5)
        t1, got = time.time(), b""
        for ch in b"GET /api1/v1/echo HTTP/1.1\r\nHost: x\r\nX-Pad: " + b"a" * 200:
            try:
                s2.send(bytes([ch]))
            except OSError:
                break
            try:
                d = s2.recv(4096)
                got += d
                if not d or got:
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        out.append((round(time.time() - t1, 1), got[:40]))
        s2.close()
    dripped = []
    drippers = [threading.Thread(target=drip, args=(dripped,)) for _ in range(6)]
    for th in drippers:
        th.start()
    time.sleep(1.0)
    t0 = time.time()
    st = raw_request(b"GET /api1/v1/echo HTTP/1.1\r\nHost: x\r\n" + b"Authorization: Bearer " + tok.get("token", "").encode()
                     + b"\r\n\r\n")
    check("with all 6 of this VM's slots held by dripping clients, one more connection waits about 1.5 s, then gets 503", st == 503
          and 1.2 <= time.time() - t0 < 3, (st, round(time.time() - t0, 1)))
    for th in drippers:
        th.join(30)
    check("each dripping client is cut off at the head deadline (408 at about 3 s), not kept for a byte every 0.5 s",
          len(dripped) == 6 and all(2.5 <= t <= 6 and g.startswith(b"HTTP/1.1 408") for t, g in dripped), dripped)
    st, _, _ = vm_request("GET", "/api1/v1/echo", bearer)
    check("...and the slots are free again: the next request passes", st == 200, st)

    # F4: an answer past the proxy's answer timeout (3 s here): cut off, and nothing of it written afterwards
    holder = {}

    def late():
        s3 = socket.create_connection(("127.0.0.1", mport), timeout=20)
        t1 = time.time()
        s3.sendall(b"GET /api1/v1/late HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + tok.get("token", "").encode() + b"\r\n\r\n")
        got = b""
        try:
            while True:
                d = s3.recv(4096)
                if not d:
                    break
                got += d
        except OSError:
            pass
        holder["late"] = (round(time.time() - t1, 1), got)
        s3.close()
    th = threading.Thread(target=late)
    th.start()
    time.sleep(3.6)
    # A request on a new connection (likely the same descriptor number on the Mac) while the late answer's
    # remaining data would arrive: it must get only its own answer.
    st, _, data = vm_request("GET", "/api1/v1/slow", bearer)
    th.join(30)
    lt, lgot = holder.get("late", (0, b""))
    check("an answer past the timeout is cut off near it, and its late data reaches no connection",
          b"first " in lgot and b"LATE-DATA" not in lgot and lt < 9 and st == 200 and "LATE-DATA" not in data
          and '"slow": true' in data, (lt, lgot[-80:], st, data[:80]))
    st, _, _ = vm_request("GET", "/api1/v1/echo", bearer)
    check("...and the proxy still serves", st == 200, st)
    apis = [x for x in audit_recs() if x.get("op") == "api"]
    check("each proxied request is audited: method, host, the path prefix it's within (never the path: it's the VM's), the "
          "API's status class, a size class, token id, whether the key was withheld",
          any(x.get("method") == "GET" and x.get("host") == upstream and "path" not in x and x.get("status") == "2xx"
              and x.get("bytes", 0) > 0 and x.get("token_id") == tok.get("token_id") and x.get("key_withheld") is True for x in apis)
          and not any("dummy-query-77" in json.dumps(x) for x in apis) and any(x.get("status") == 401 for x in apis), apis[-3:])
    r = client("token", "api2", "--vm", "opsvm", "--ttl", "2s")
    tok2 = js(r.stdout)
    st, _, _ = vm_request("GET", "/api2/anything", {"x-api-key": tok2.get("token", "")})
    seen = up.got[-1]
    check("an x-api-key secret: the real key in x-api-key", st == 200 and seen["headers"].get("x-api-key") == UPSTREAM_VALUE + "2", seen)
    time.sleep(3)
    st, _, _ = vm_request("GET", "/api2/anything", {"x-api-key": tok2.get("token", "")})
    check("a token stops working at its expiry", st == 401)
    r = client("tokens")
    check("tokens lists live tokens (id, secret, VM, time left)", tok.get("token_id", "?") in r.stdout and "api1 on VM opsvm" in r.stdout
          and tok.get("token", "x") not in r.stdout, r.stdout)
    r = client("revoke", tok.get("token_id", "00000000"))
    st, _, _ = vm_request("GET", "/api1/v1/echo", bearer)
    check("revoke ID: the token stops working", r.returncode == 0 and "Revoked" in r.stdout and st == 401, (r.stdout, st))
    r = client("token", "api1", "--vm", "opsvm")
    tok3 = js(r.stdout)
    r = client("revoke", "--all")
    st, _, _ = vm_request("GET", "/api1/v1/echo", {"Authorization": "Bearer " + tok3.get("token", "")})
    check("revoke --all", st == 401)
    tok5 = js(client("token", "api1", "--vm", "opsvm").stdout)
    st0, _, _ = vm_request("GET", "/api1/v1/echo", {"Authorization": "Bearer " + tok5.get("token", "")})
    rc, out = tty([BIN, "set", "api1", "--kind", "api", "--api-host", upstream, "--header", "Authorization: Bearer {value}",
                   "--api-path-prefix", "/v1/", "--replace"], [("Vault passphrase", current + "\n"), ("API key for api1", UPSTREAM_VALUE2 + "\n")])
    st, _, data = vm_request("GET", "/api1/v1/echo", {"Authorization": "Bearer " + tok5.get("token", "")})
    check("a token made before set --replace stops working: it's bound to the secret's generation", st0 == 200 and rc == 0
          and st == 401 and "changed since this token was made" in data, (st0, rc, st, data))
    tok6 = js(client("token", "api1", "--vm", "opsvm").stdout)
    st, _, _ = vm_request("GET", "/api1/v1/echo", {"Authorization": "Bearer " + tok6.get("token", "")})
    check("...a new token carries the new value", st == 200 and up.got[-1]["headers"].get("authorization") == "Bearer " + UPSTREAM_VALUE2,
          st)
    rc, out = tty([BIN, "set", "api1", "--kind", "api", "--api-host", upstream, "--header", "Authorization: Bearer {value}", "--replace"],
                  [("Vault passphrase", current + "\n"), ("API key for api1", UPSTREAM_VALUE2 + "\n")])
    n_up = len(up.got)
    st, _, _ = vm_request("GET", "/api1/v2/other", {"Authorization": "Bearer " + tok6.get("token", "")})
    check("a token can't widen with its secret: after a replace that drops the path prefix, the old token gets 401 everywhere",
          rc == 0 and st == 401 and len(up.got) == n_up, st)
    tok8 = js(client("token", "api2", "--vm", "opsvm").stdout)
    rc, out = tty([BIN, "delete", "api2"], [("Vault passphrase", current + "\n")])
    st, _, _ = vm_request("GET", "/api2/anything", {"x-api-key": tok8.get("token", "")})
    r = client("tokens")
    check("deleting a secret ends its tokens (refused, and gone from the list)", rc == 0 and st in (401, 404)
          and tok8.get("token_id", "?") not in r.stdout, (st, r.stdout))
    tok4 = js(client("token", "api1", "--vm", "opsvm").stdout)

    # --- 9. sealed sessions and rotate-session-key -------------------------------------------------------
    h = detached_start("session", "save", "s1", "--vm", "opsvm", "--profile", "prof1")
    u.expect("as the sealed session s1?")
    u.send("s1\n")
    rc, out = detached_wait(h)
    sealed = os.path.join(vdir, "sessions", "s1.sealed")
    before = open(sealed, "rb").read() if os.path.exists(sealed) else b""
    check("a sealed session to rotate", rc == 0 and before, out)
    rc, out = tty([BIN, "rotate-session-key"], [("Vault passphrase", current + "\n")])
    after = open(sealed, "rb").read() if os.path.exists(sealed) else b""
    check("rotate-session-key re-encrypts every sealed session", rc == 0 and "1 sealed session(s) re-encrypted" in out
          and after and after != before and not os.path.exists(sealed + ".new"), out)
    h = detached_start("session", "load", "s1", "--vm", "opsvm")
    u.expect("Load the sealed session s1")
    u.send("s1\n")
    rc, out = detached_wait(h)
    loaded = open(os.path.join(stubdir, "loaded.tgz"), "rb").read() if os.path.exists(os.path.join(stubdir, "loaded.tgz")) else b""
    check("...and it still loads, unchanged", rc == 0 and loaded == open(os.path.join(stubdir, "archive.tgz"), "rb").read(), out)
    # F12: sealed sessions sign in with the static ops key; a VM on SSH certificates refuses it
    open(os.path.join(stubdir, "ops-refused-next"), "w").close()
    h = detached_start("session", "load", "s1", "--vm", "opsvm")
    u.expect("Load the sealed session s1")
    u.send("s1\n")
    rc, out = detached_wait(h)
    check("a VM that refuses the static ops key (SSH_AUTH=cert): the load fails and names the cause", rc != 0
          and "sealed sessions need SSH_AUTH=key" in out, out)
    cert_conf = os.path.join(td, "cert.env")
    with open(cert_conf, "w") as f:
        f.write(f'. "{wrapper["MCOMP_CONFIG"]}"\nSSH_AUTH=cert\n')
    n_calls = len(calls())
    t0 = time.time()
    r = subprocess.run([mc, "secrets", "session", "save", "s9", "--profile", "prof1"], capture_output=True, text=True,
                       env=dict(env, **dict(wrapper, MCOMP_CONFIG=cert_conf)), timeout=60, stdin=subprocess.DEVNULL)
    check("with SSH_AUTH=cert, metacomputer secrets session save refuses at once, before any question or ssh", r.returncode == 1
          and "sealed sessions need SSH_AUTH=key" in r.stderr and len(calls()) == n_calls and time.time() - t0 < 10,
          (r.returncode, r.stderr[-300:]))
    check("list shows when the session key was rotated", "key rotated" in client("list").stdout)
    h = detached_start("session", "save", "s2", "--vm", "opsvm", "--profile", "prof1")
    u.expect("as the sealed session s2?")
    u.send("s2\n")
    rc, out = detached_wait(h)
    sealed2 = os.path.join(vdir, "sessions", "s2.sealed")

    def both():
        return (open(sealed, "rb").read(), open(sealed2, "rb").read())
    a1, a2 = both()
    stop1 = {"MCOMP_VAULT_TEST_STOP_ROTATE_AFTER": "1"}
    rc1, _ = tty([BIN, "rotate-session-key"], [("Vault passphrase", current + "\n")], extra=stop1)
    b1, b2 = both()
    rc2, _ = tty([BIN, "rotate-session-key"], [("Vault passphrase", current + "\n")], extra=stop1)
    c1, c2 = both()
    check("rotate-session-key cut short twice (each time after its first file): s1 moved each time, s2 still under the oldest key",
          rc == 0 and rc1 == 9 and rc2 == 9 and b1 != a1 and c1 != b1 and b2 == a2 and c2 == a2, (rc, rc1, rc2))
    ok_loads = []
    for name in ("s1", "s2"):
        h = detached_start("session", "load", name, "--vm", "opsvm")
        u.expect(f"Load the sealed session {name}")
        u.send(name + "\n")
        lrc, lout = detached_wait(h)
        ok_loads.append(lrc == 0 and open(os.path.join(stubdir, "loaded.tgz"), "rb").read() == open(os.path.join(stubdir, "archive.tgz"), "rb").read())
    check("...both sessions still load: every older key is kept until a rotation finishes", all(ok_loads), ok_loads)
    trace = os.path.join(td, "durable.trace")
    rc, out = tty([BIN, "rotate-session-key"], [("Vault passphrase", current + "\n")], extra={"MCOMP_VAULT_TEST_TRACE": trace})
    steps = [l.split("/")[-1] for l in open(trace).read().splitlines()] if os.path.exists(trace) else []
    check("rotation's durable writes, in order: the vault with every key, then each session file, then the vault without the old keys",
          steps == ["vault.json", "s1.sealed", "s2.sealed", "vault.json"], steps)
    d1, d2 = both()
    check("running it again finishes: both files under the new key, and it says the earlier rotation was cut short", rc == 0
          and "2 sealed session(s) re-encrypted" in out and "cut short" in out and d2 != a2, out)
    os.chmod(sealed2, 0)
    vault_before = open(os.path.join(vdir, "vault.json")).read()
    rc, out = tty([BIN, "rotate-session-key"], [("Vault passphrase", current + "\n")])
    os.chmod(sealed2, 0o600)
    check("a sealed session that can't be read stops rotate-session-key, with nothing changed", rc != 0 and "can't be read" in out
          and "nothing changed" in out and open(os.path.join(vdir, "vault.json")).read() == vault_before, out)

    # --- 9b. a VM that remembers a password it was given: nothing it says reaches any output, ever -----------------
    # It spells the last password, hex-encoded, into a later check's reason and every other field, onto its stderr with no
    # answer, into secret-fill's DevTools-error form, into a sealed-session save's and load's stderr, and into the path and
    # name of API proxy requests. None of it may reach the command's output, the audit log (`audit --json` too) or a sink.
    def flagp(name):
        open(os.path.join(stubdir, name), "w").close()
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    mem = os.path.join(stubdir, "remembered")
    remembered = open(mem).read().strip() if os.path.exists(mem) else ""
    leak_out = [r.stdout + r.stderr]
    login = ["login", "web-login", "--vm", "opsvm", "--password-selector", "#pw"]
    shapes = [("remember-reason-next", login), ("remember-stderr-next", login), ("remember-devtools-next", login),
              ("remember-reason-next", ["fill", "web-login", "--vm", "opsvm", "--field", "username"]),
              ("remember-stderr-next", ["fill", "web-login", "--vm", "opsvm", "--field", "password"]),
              ("remember-devtools-next", ["fill", "web-login", "--vm", "opsvm", "--field", "password"])]
    failed_ok = []
    for fl, args in shapes:
        flagp(fl)
        r = client(*args)
        leak_out.append(r.stdout + r.stderr)
        failed_ok.append(r.returncode in (2, 4))
    flagp("remember-session-next")
    h = detached_start("session", "load", "s1", "--vm", "opsvm")
    u.expect("Load the sealed session s1")
    u.send("s1\n")
    rc_l, out = detached_wait(h)
    leak_out.append(out)
    flagp("remember-session-next")
    h = detached_start("session", "save", "s9", "--vm", "opsvm", "--profile", "prof1")
    u.expect("as the sealed session s9?")
    u.send("s9\n")
    rc_s, out = detached_wait(h)
    leak_out.append(out)
    tokl = js(client("token", "api1", "--vm", "opsvm").stdout)
    vm_request("GET", "/api1/" + remembered, {"Authorization": "Bearer " + tokl.get("token", "")})
    vm_request("GET", "/" + remembered[:60] + "/x", {"Authorization": "Bearer " + tokl.get("token", "")})
    vm_request("PROPFIND", "/api1/x", {"Authorization": "Bearer " + tokl.get("token", "")})
    sinks_up = ["hook", "otel"] + (["sys"] if sl else [])
    wait_for(lambda: caught_up(sinks_up), 30)
    aj = client("audit", "--json", "--tail", "400")
    raw_audit = open(os.path.join(vdir, "audit.jsonl"), errors="replace").read()
    raw_sinks = " ".join(g.get("body", "") if isinstance(g.get("body"), str) else str(g.get("body")) for g in rx.got) \
        + " ".join(sl.got if sl else [])
    where = [w for w, text in (("outputs", " ".join(leak_out)), ("audit.jsonl", raw_audit), ("audit --json", aj.stdout),
                               ("sinks", raw_sinks)) if remembered and remembered in text]
    apis = [x for x in audit_recs() if x.get("op") == "api"][-3:]
    check("a VM spelling a password it was given earlier into a check's reason (and every other field), its stderr, "
          "secret-fill's DevTools-error form, a session save's and load's stderr, and an API path and name: it reaches no "
          "command output, audit record (`audit --json` included) or sink; each use fails in the vault's own words",
          len(remembered) >= 8 and all(failed_ok) and rc_l != 0 and rc_s != 0 and not where
          and not os.path.exists(os.path.join(vdir, "sessions", "s9.sealed")), (len(remembered), failed_ok, rc_l, rc_s, where))
    check("...the proxy records the method as one of a few words and no path: PROPFIND as 'other', the name only when it's "
          "a stored api secret's", apis and all("path" not in x for x in apis) and any(x.get("method") == "other" for x in apis)
          and any("name" not in x and x.get("status") == 404 for x in apis), apis)
    # Sizes and counts it can steer: an echo API answers with what it got, so N characters of padding give an answer of
    # C + N bytes. The audit keeps one of a few sizes, the API's status class and whether the key was withheld, never counts.
    sizes = (0, 1 << 10, 1 << 16, 1 << 20, 1 << 24, 1 << 28, 1 << 32, 1 << 40)
    n_api = len([x for x in audit_recs() if x.get("op") == "api"])
    for pad in (3000, 3001, 3077):
        vm_request("POST", "/api1/v1/echo", {"Authorization": "Bearer " + tokl.get("token", ""), "Content-Type": "text/plain"},
                   body=("x" * pad).encode())
    padded = [x for x in audit_recs() if x.get("op") == "api"][n_api:]
    lst = client("tokens", "--json")
    toks = (json.loads(lst.stdout or "{}").get("tokens") or []) if lst.returncode == 0 else []
    mine = [t for t in toks if t.get("id") == tokl.get("token_id")]
    check("...and nothing it can steer is recorded as a number it chose: answers 3000, 3001 and 3077 characters apart get the "
          "same size class, the API's status as its class, whether the key was withheld (not how often), and the token list "
          "counts requests in words", len(padded) == 3 and len({x.get("bytes") for x in padded}) == 1
          and all(x.get("bytes") in sizes and x.get("status") == "2xx" and x.get("key_withheld") in (True, None) for x in padded)
          and mine and mine[0].get("requests") in ("none", "1-9", "10-99", "100 or more"), (padded, mine))

    # --- 10. a rekey while unlocked locks the running unlock; tokens die with it ---------------------------
    rc, out = tty([BIN, "rekey"], [("Vault passphrase", current + "\n"), ("New vault passphrase", PASS + "\n"),
                                   ("same passphrase again", PASS + "\n")])
    check("rekey while unlocked", rc == 0, out)
    client("status")
    ended = u.wait(20)
    check("the running unlock locks itself when the vault's key changes", ended == 0 and "key changed" in u.text(), u.text()[-300:])
    try:
        st, _, _ = vm_request("GET", "/api1/v1/echo", {"Authorization": "Bearer " + tok4.get("token", "")})
    except OSError:
        st = "closed"
    check("after the lock the proxy is gone (tokens die with the unlock)", st == "closed", st)
    tunnel_pids = [c["pid"] for c in calls() if c.get("kind") == "proxy:tunnel" and c.get("pid")]

    def gone(pid):
        try:
            os.kill(pid, 0)
            return False
        except OSError:
            return True
    check("...and its ssh tunnels are stopped", wait_for(lambda: all(gone(x) for x in tunnel_pids), 10))
    r = subprocess.run([sys.executable, "-I", verify, alog], capture_output=True, text=True)
    check("verify-audit.py: the whole local log, after everything", r.returncode == 0, r.stdout)

    # Locked: a forged saved place for a sink isn't trusted
    hst = os.path.join(vdir, "sinks", "hook.state")
    stj = json.load(open(hst))
    stj["delivered"] = 999999
    json.dump(stj, open(hst, "w"))
    r = client("fill", "web-login", "--vm", "opsvm", "--field", "password")
    lock_seq = audit_recs()[-1]["seq"]
    n_hook = len([g for g in rx.got if g["path"] == "/hook"])
    rc, out = tty([BIN, "audit-sink", "test", "hook"], [("Vault passphrase", PASS + "\n")])
    new_hook = [json.loads(g["body"])["data"] for g in rx.got if g["path"] == "/hook"][n_hook:]
    check("a forged saved place (sinks/hook.state, MAC no longer matching) is ignored: delivery starts again from the sink's "
          "first event, and the new record arrives", rc == 0 and "didn't check out" in out and any(d.get("seq") == lock_seq for d in new_hook)
          and any(d.get("seq") == starts["hook"] + 1 for d in new_hook), out[-400:])
    # Locked: forged records appended with a correct chain, without the vault key
    f1 = append_forged({"op": "fill", "result": "ok", "name": "web-login", "vm": "opsvm", "field": "password", "chars": 9})
    f2 = append_forged({"op": "token", "result": "ok", "name": "api1", "vm": "opsvm", "mac": "0" * 64})
    f3 = append_forged({"op": "fill", "result": "locked", "name": "web-login", "vm": "opsvm", "field": "password", "mac": {}})
    n_hook = len([g for g in rx.got if g["path"] == "/hook"])
    rc, out = tty([BIN, "audit-sink", "test", "hook"], [("Vault passphrase", PASS + "\n")])
    new_hook = [json.loads(g["body"])["data"] for g in rx.got if g["path"] == "/hook"][n_hook:]
    d1 = [d for d in new_hook if d.get("seq") == f1["seq"]]
    d2 = [d for d in new_hook if d.get("seq") == f2["seq"]]
    d3 = [json.loads(g["body"]) for g in rx.got if g["path"] == "/hook"][n_hook:]
    d3 = [c for c in d3 if c["data"].get("seq") == f3["seq"]]
    check("a locked-client record with a malformed (non-string) MAC isn't signed as verified: a notice instead",
          d3 and all(c["data"].get("op") == "audit_tamper" for c in d3) and not any(c["data"].get("op") == "fill" for c in d3), d3)
    check("records appended without the vault key (a fill with no MAC, a token with a wrong one) aren't signed for a sink: notices instead",
          d1 and d2 and all(d.get("op") == "audit_tamper" for d in d1 + d2) and "no creation MAC" in d1[0].get("reason", "")
          and "fails its creation MAC" in d2[0].get("reason", "") and "AUDIT LOG CHANGED" in out, (d1, d2))
    fills = [c for c in calls() if c.get("user") == "mcfill"]
    pin = next((a for a in fills[-1]["argv"] if a.startswith("KnownHostsCommand=")), "") if fills else ""
    sshd = local_sshd(td)
    if sshd and pin:
        sport, shk, ckey, sproc = sshd
        procs.append(sproc)

        def real_ssh(hostkey_line):
            argv = [a for a in fills[-1]["argv"]]
            argv = [("KnownHostsCommand=/bin/echo [127.0.0.1]:%d %s" % (sport, hostkey_line)) if a.startswith("KnownHostsCommand=") else a
                    for a in argv]
            argv = [f"IdentityFile={ckey}" if a.startswith("IdentityFile=") else a for a in argv]
            i = argv.index("-p")
            argv[i + 1] = str(sport)
            argv[-2] = f"{os.environ.get('USER') or pwd_name()}@127.0.0.1"
            argv[-1] = "echo pinned-ok"
            return subprocess.run(["/usr/bin/ssh", *argv], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        r = real_ssh(shk)
        other = " ".join(open(ckey + ".pub").read().split()[:2])
        r2 = real_ssh(other)
        check("the real /usr/bin/ssh, with the vault's own options, connects when the pin is the server's key and refuses another",
              "pinned-ok" in r.stdout and r2.returncode != 0 and "verification failed" in r2.stderr, (r.stdout + r.stderr)[-200:])
    else:
        print("(no local sshd: skipped the real-ssh pin check)")
    term = u.text()
    check("the unlock terminal never showed a value or a token", not any(v in term for v in VALUES)
          and tok.get("token", "x") not in term)
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no value in the output of any of {len(outputs)} client commands", not leaked, leaked)
    raw = open(alog).read()
    check("the audit log holds no values or tokens", not any(v in raw for v in VALUES) and tok.get("token", "x") not in raw)
    listener.close()


if __name__ == "__main__":
    sys.exit(main())
