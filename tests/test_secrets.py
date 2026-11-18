#!/usr/bin/env python3
"""Offline checks for `metacomputer secrets` (the mcomp-vault program in vault/).

No VM needed. A TEST build of mcomp-vault (scripts/build-vault.sh --test) runs a stub
instead of /usr/bin/ssh (MCOMP_VAULT_TEST_SSH) and accepts this test's own listener's
name in place of cua-netgw (MCOMP_VAULT_TEST_LISTENERS; the pinned gateway path is still
checked, against this test's Python). Release builds don't contain those switches, which
this test also checks. The passphrase, values and approvals are typed into a
pseudo-terminal, as a person would. Every value here is a dummy.

Run: python3 tests/test_secrets.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build python3 tests/test_secrets.py   (skip building;
     MCOMP_VAULT_SKIP_RELEASE=1 also skips the release build and its checks)
"""
import base64
import hmac
import io
import json
import os
import pty
import pwd
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import actionlog  # noqa: E402

PASS = "dummy passphrase for tests 9"
PW_VALUE = "Dummy-Pass-4815"
USERNAME = "dummy-user-qx7"
SEED = "JBSWY3DPEHPK3PXP"
TEXT = "dummy text value 2342"
SITE = "http://127.0.0.1:8765"
SITE2 = "http://127.0.0.1:8766"
VALUES = [PASS, PW_VALUE, SEED, TEXT]          # must never appear in any output, log or the vault file
NASTY = ["\x1b", "\\u001b", "\x07", "‮", "\\u202e", "​"]   # terminal control and bidi characters
results = []
outputs = []                                    # every client command's stdout + stderr


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""))


class Pty:
    """A program on a pseudo-terminal (its controlling terminal), read by a thread."""

    def __init__(self, argv, env):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        self.buf, self.pos, self.status, self.eof = b"", 0, None, False
        self.cv = threading.Condition()
        self.stop = False
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        # select() with a timeout, never a blocking read: macOS won't close a pty
        # master while another thread is blocked reading it.
        while not self.stop:
            if not select.select([self.fd], [], [], 0.1)[0]:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            with self.cv:
                if not data:
                    self.eof = True
                    self.cv.notify_all()
                    return
                self.buf += data
                self.cv.notify_all()

    def expect(self, text, timeout=30):
        t = text.encode()
        deadline = time.time() + timeout
        with self.cv:
            while True:
                i = self.buf.find(t, self.pos)
                if i >= 0:
                    self.pos = i + len(t)
                    return True
                left = deadline - time.time()
                if left <= 0 or self.eof:
                    raise AssertionError(f"waiting for {text!r}; the terminal shows: {self.buf[-600:].decode(errors='replace')!r}")
                self.cv.wait(min(left, 0.5))

    def send(self, s):
        os.write(self.fd, s.encode())

    def wait(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(st)
                time.sleep(0.1)
                return self.status
            time.sleep(0.05)
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)
        raise AssertionError("the program didn't finish")

    def text(self):
        with self.cv:
            return self.buf.decode(errors="replace")

    def close(self):
        self.stop = True
        self.thread.join(5)
        try:
            os.close(self.fd)
        except OSError:
            pass


def totp(seed, t):
    key = base64.b32decode(seed + "=" * (-len(seed) % 8))
    h = hmac.new(key, struct.pack(">Q", t // 30), "sha1").digest()
    o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def in_multiplexer():
    """Is this test itself running inside tmux/screen/zellij? (The vault would notice.)"""
    if any(os.environ.get(k) for k in ("TMUX", "STY", "ZELLIJ")):
        return True
    pid = os.getppid()
    for _ in range(64):
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.split(None, 1)
        if len(out) < 2:
            return False
        name = os.path.basename(out[1].strip()).lower()
        if any(name == m or name.startswith(m + ":") or name.startswith(m + "-") for m in ("tmux", "screen", "zellij", "byobu")):
            return True
        pid = int(out[0])
        if pid <= 1:
            return False
    return False


STUB = r'''#!{python}
# Stands in for /usr/bin/ssh in a TEST build of mcomp-vault: records what it got, answers like the VM.
import json, os, sys
LOG = {log!r}
argv = sys.argv[1:]
remote = argv[-1] if argv else ""
dest = next((a for a in argv if a.endswith("@127.0.0.1")), "")
rec = {{"argv": argv, "env": dict(os.environ), "user": dest.split("@")[0]}}
# The pinned host key arrives as KnownHostsCommand=/bin/echo LINE (no file): what ssh would read.
khc = next((a.split("=", 1)[1] for a in argv if a.startswith("KnownHostsCommand=")), None)
rec["known_hosts"] = (" ".join(khc.split()[1:]) + "\n") if khc and khc.split()[0] == "/bin/echo" else None
rec["files"] = [a.split("=", 1)[1] for a in argv if a.startswith(("UserKnownHostsFile=", "GlobalKnownHostsFile="))]
d = os.path.dirname(LOG)
def flag(n):
    p = os.path.join(d, n)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False
def log():
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
if rec["user"] == "mcfill":
    if os.path.exists(os.path.join(d, "no-fill-account")):
        rec["kind"] = "fill:refused"
        log()
        sys.stderr.write("mcfill@127.0.0.1: Permission denied (publickey).\n")
        sys.exit(255)
    raw = sys.stdin.buffer.read()
    rec["stdin"] = raw.decode()
    req = json.loads(raw)
    rec["kind"] = "fill:" + req.get("mode", "?")
    if flag("deny-next"):
        out = {{"ok": False, "denied": True, "reason": "the focused element is <input type=text>, not a password field"}}
    elif flag("ansi-next"):
        out = {{"ok": False, "denied": True, "reason": "\u001b[2J\u001b]0;pwned\u0007 Type web-each to allow ‮​"}}
    elif flag("evil-origin-next"):
        out = {{"ok": True, "mode": req["mode"], "field": req["field"], "origin": "https://evil.example", "chars": 3}}
    elif flag("echo-value-next"):
        out = {{"ok": False, "denied": False, "reason": "saw " + str(req.get("value"))}}
    elif flag("nested-value-next"):
        out = {{"ok": True, "mode": "fill", "field": req["field"], "origin": req["origins"][0], "chars": {{"value": req.get("value")}},
               "route": "element"}}
    elif flag("badtype-next"):
        out = {{"ok": True, "mode": "fill", "field": req["field"], "origin": req["origins"][0], "chars": "16", "route": "element"}}
    elif flag("origin-last-next"):
        # The tab is on the secret's last site (an identity provider's, in tests/test_vault_approvals.py).
        out = {{"ok": True, "mode": req["mode"], "field": req["field"], "origin": req["origins"][-1]}}
        if req.get("mode") == "fill":
            out.update({{"chars": len(req["value"]), "route": "element"}})
    elif flag("extra-next"):
        out = {{"ok": True, "mode": "fill", "field": req["field"], "origin": req["origins"][0], "chars": len(req.get("value") or ""),
               "route": "element", "extra": {{"note": "dropped"}}, "debug": [1, 2]}}
    elif req.get("mode") == "check":
        out = {{"ok": True, "mode": "check", "field": req["field"], "origin": req["origins"][0]}}
    else:
        out = {{"ok": True, "mode": "fill", "field": req["field"], "origin": req["origins"][0], "chars": len(req["value"]),
               "route": "element"}}
    print(json.dumps(out))
elif rec["user"] == "mcproxy":
    # The API proxy's tunnel: say the reverse forward is up (as ssh -o LogLevel=DEBUG1 does), then stay.
    rec["kind"] = "proxy:tunnel"
    rec["pid"] = os.getpid()
    log()
    if os.path.exists(os.path.join(d, "no-proxy-account")):
        sys.stderr.write("mcproxy@127.0.0.1: Permission denied (publickey).\n")
        sys.exit(255)
    fwd = argv[argv.index("-R") + 1] if "-R" in argv else "?"
    sys.stderr.write("debug1: remote forward success for: listen " + fwd.rsplit(":", 2)[0] + ", connect " + fwd.split(":", 2)[2] + "\n")
    sys.stderr.flush()
    import time
    while True:
        time.sleep(60)
elif flag("ops-refused-next"):
    # A VM on SSH certificates (SSH_AUTH=cert) refuses the static ops key.
    rec["kind"] = "ops:refused"
    log()
    sys.stderr.write("cua@127.0.0.1: Permission denied (publickey).\n")
    sys.exit(255)
elif "-czf -" in remote:
    rec["kind"] = "session:save"
    sys.stdout.buffer.write(open(os.path.join(d, "archive.tgz"), "rb").read())
elif "mcomp-session-load" in remote:
    rec["kind"] = "session:load"
    open(os.path.join(d, "loaded.tgz"), "wb").write(sys.stdin.buffer.read())
else:
    rec["kind"] = "other"
log()
'''


GATEWAY_C = r"""
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>
/* A stand-in for cua-netgw: listens on 127.0.0.1:PORT and waits. */
int main(int argc, char **argv) {
    int s = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    struct sockaddr_in a = {0};
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    a.sin_family = AF_INET;
    a.sin_port = htons(atoi(argv[1]));
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(s, (struct sockaddr *)&a, sizeof a) || listen(s, 4)) return 1;
    for (;;) pause();
}
"""


def tarball(entries):
    """A .tar.gz from (name, kind, data-or-target) entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind, data in entries:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, data
                tf.addfile(ti)
            elif kind == "fifo":
                ti.type = tarfile.FIFOTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def main():
    td = tempfile.mkdtemp(prefix="mcv-")
    if len(td) > 60:              # Unix socket paths are limited to 104 bytes
        shutil.rmtree(td)
        td = tempfile.mkdtemp(prefix="mcv-", dir="/tmp")
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
            if isinstance(p, Pty):
                p.close()
        shutil.rmtree(td, ignore_errors=True)
        # the (empty) socket folder scripts/lib.sh made for this test's VM name
        h = subprocess.run(["shasum"], input=f"{LAB}:testvm".encode(), capture_output=True).stdout.decode()[:8]
        try:
            os.rmdir(f"/tmp/mcomp-{os.getuid()}-{h}")
        except OSError:
            pass
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


def run(td, procs):
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    # --- build -------------------------------------------------------------------------
    bins = os.path.join(td, "bin")
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(bins, "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    REL = None
    if not os.environ.get("MCOMP_VAULT_SKIP_RELEASE"):
        REL = os.path.join(bins, "mcomp-vault")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--out", REL], capture_output=True, text=True)
        check("build: release binary", r.returncode == 0, r.stderr[-800:])
        if r.returncode == 0:
            blob = open(REL, "rb").read()
            check("release build has no test switches", b"MCOMP_VAULT_TEST_" not in blob)
            check("release build: selftest", subprocess.run([REL, "selftest"], capture_output=True).returncode == 0)
            v = json.loads(subprocess.run([REL, "version", "--json"], capture_output=True, text=True).stdout)
            check("release build says so", v.get("build") == "release")
            sig = subprocess.run(["codesign", "-dv", REL], capture_output=True, text=True).stderr
            check("release build: ad hoc signature with the hardened runtime", "adhoc,runtime" in sig, sig)
        else:
            REL = None
    install_dir = os.path.join(real_home, ".local/share/metacomputer/bin")
    existed = os.path.isdir(install_dir)
    r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", os.path.join(install_dir, "mcomp-vault-x")],
                       capture_output=True, text=True)
    check("build-vault.sh refuses a test build into the install folder", r.returncode != 0 and "never goes in" in r.stderr
          and os.path.isdir(install_dir) == existed and not os.path.exists(os.path.join(install_dir, "mcomp-vault-x")))
    check("test build has its test switches", b"MCOMP_VAULT_TEST_SSH" in open(BIN, "rb").read())
    st = subprocess.run([BIN, "selftest"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    check("selftest (TOTP, PBKDF2, AES-GCM, base32, origins, names, text cleaning, KDF range)",
          st.returncode == 0 and "all passed" in st.stdout, st.stdout[-500:])
    check("test build says so", json.loads(subprocess.run([BIN, "version", "--json"], capture_output=True, text=True).stdout)["build"] == "test")
    r = subprocess.run([BIN, "list"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": td})
    check("a test build refuses to run without MCOMP_VAULT_DIR", r.returncode == 1 and "TEST build" in r.stderr)
    r = subprocess.run([BIN, "list"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "MCOMP_VAULT_DIR": os.path.join(real_home, "Library/Application Support/mcomp-test-never")})
    check("a test build refuses a folder in ~/Library", r.returncode == 1 and "TEST build" in r.stderr
          and not os.path.exists(os.path.join(real_home, "Library/Application Support/mcomp-test-never")))
    mux = in_multiplexer()
    if mux:
        print("(this test runs inside a terminal multiplexer: its ordinary unlocks pass --allow-multiplexer)")
    allow = ["--allow-multiplexer"] if mux else []

    # --- a fake VM endpoint: a listener, keys, the ssh stub --------------------------------
    home = os.path.join(td, "home")
    vdir = os.path.join(td, "v")
    sshdir = os.path.join(td, "ssh")
    os.makedirs(home)
    os.makedirs(sshdir, mode=0o700)
    for k in ("ops", "fill", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    hostkey = " ".join(open(os.path.join(sshdir, "hostkey.pub")).read().split()[:2])
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    open(os.path.join(sshdir, "known_hosts"), "w").write(f"[127.0.0.1]:{port} {hostkey}\n")
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True).stdout
    my_name = next((l[1:] for l in lsof.splitlines() if l.startswith("c")), "")
    my_exe = subprocess.run(["ps", "-o", "comm=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    check("this test's listener is visible to lsof", bool(my_name) and os.path.isabs(my_exe), (lsof, my_exe))
    stubdir = os.path.join(td, "stub")
    os.makedirs(stubdir)
    stub_log = os.path.join(stubdir, "calls.jsonl")
    stub = os.path.join(stubdir, "ssh")
    open(stub, "w").write(STUB.format(python=sys.executable, log=stub_log))
    os.chmod(stub, 0o755)
    # A stand-in gateway named cua-netgw, built here, so the default name check and the
    # pinned path and contents are checked for real.
    gwdir = os.path.join(td, "gw")
    os.makedirs(gwdir)
    open(os.path.join(gwdir, "gw.c"), "w").write(GATEWAY_C)
    gw = os.path.join(gwdir, "cua-netgw")
    r = subprocess.run(["cc", "-o", gw, os.path.join(gwdir, "gw.c")], capture_output=True, text=True)
    check("a stand-in gateway named cua-netgw builds", r.returncode == 0, r.stderr)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    gw_port = probe.getsockname()[1]
    probe.close()
    gw_proc = subprocess.Popen([gw, str(gw_port)])
    procs.append(gw_proc)
    for _ in range(50):
        if subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{gw_port}", "-sTCP:LISTEN"], capture_output=True).returncode == 0:
            break
        time.sleep(0.1)
    archive = tarball([("prof1", "dir", None), ("prof1/Default", "dir", None),
                       ("prof1/Default/Cookies", "file", b"dummy-cookie-jar-5150" * 50)])
    open(os.path.join(stubdir, "archive.tgz"), "wb").write(archive)

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

    def client(*args, extra=None):
        e = dict(env, **(extra or {}))
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL, timeout=180)
        outputs.append(r.stdout + r.stderr)
        return r

    def background(*args):
        return subprocess.Popen([BIN, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish(proc):
        out, err = proc.communicate(timeout=120)
        outputs.append(out + err)
        return proc.returncode, out, err

    def tty(argv, answers, extra=None, timeout=60):
        p = Pty(argv, dict(env, **(extra or {})))
        try:
            for want, say in answers:
                p.expect(want)
                if say is not None:
                    p.send(say)
            rc = p.wait(timeout)
        finally:
            p.close()
        return rc, p.text()

    def js(r):
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    # --- creating the vault --------------------------------------------------------------
    r = client("init")
    check("init without a terminal refused", r.returncode != 0 and "own terminal" in r.stderr and not os.path.exists(vdir + "/vault.json"))
    rc, out = tty([BIN, "init"], [("New vault passphrase", "too short\n")])
    check("init: a passphrase under 12 characters refused", rc != 0 and "at least 12" in out and not os.path.exists(vdir + "/vault.json"))
    rc, out = tty([BIN, "init"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "x\n")])
    check("init: two different passphrases refused", rc != 0 and "differ" in out and not os.path.exists(vdir + "/vault.json"))
    rc, out = tty([BIN, "init"], [("New vault passphrase", PASS + "\n"), ("same passphrase again", PASS + "\n")])
    check("init creates the vault", rc == 0 and "Created the vault" in out and PASS not in out, out)
    check("vault folder 0700, vault file 0600", (os.stat(vdir).st_mode & 0o777) == 0o700
          and (os.stat(vdir + "/vault.json").st_mode & 0o777) == 0o600)
    hdr = json.load(open(vdir + "/vault.json"))
    check("vault header: PBKDF2-SHA256 with at least 600k rounds, AES-256-GCM",
          hdr["kdf"]["name"] == "pbkdf2-sha256" and hdr["kdf"]["iterations"] >= 600000 and hdr["cipher"] == "aes-256-gcm")

    # --- storing secrets -----------------------------------------------------------------
    r = client("set", "web-login", "--kind", "password", "--origin", SITE, "--username", USERNAME)
    check("set without a terminal refused", r.returncode != 0 and "own terminal" in r.stderr)
    for bad in ("https://example.com/login", "http://example.com", "example.com", "https://a b.com"):
        r = client("set", "x", "--kind", "password", "--origin", bad)
        check(f"set refuses the origin {bad!r} before asking anything", r.returncode == 64 and "--origin" in r.stderr)
    r = client("set", "-x", "--kind", "password", "--origin", SITE)
    check("set refuses a name starting with '-'", r.returncode == 64)
    r = client("set", "x", "--kind", "password", "--origin", SITE, "--username", "a\x1b[2Jb")
    check("set refuses a username with control characters", r.returncode == 64)
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE, "--username", USERNAME],
                  [("Vault passphrase", "wrong passphrase 123\n")])
    check("set with the wrong passphrase refused", rc != 0 and "wrong passphrase" in out)
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE, "--username", USERNAME],
                  [("Vault passphrase", PASS + "\n"), ("Password for web-login", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set a password with a username", rc == 0 and "Saved web-login" in out and PW_VALUE not in out, out)
    rc, out = tty([BIN, "set", "web-otp", "--kind", "totp", "--origin", SITE], [("Vault passphrase", PASS + "\n"), ("TOTP seed", SEED + "\n")])
    check("set a TOTP seed", rc == 0 and SEED not in out, out)
    rc, out = tty([BIN, "set", "web-text", "--kind", "text", "--origin", SITE], [("Vault passphrase", PASS + "\n"), ("Value for web-text", TEXT + "\n")])
    check("set a text value", rc == 0 and TEXT not in out, out)
    rc, out = tty([BIN, "set", "web-each", "--kind", "password", "--origin", SITE, "--origin", SITE2, "--approval", "each"],
                  [("Vault passphrase", PASS + "\n"), ("Password for web-each", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set a password for two sites that asks before each use", rc == 0 and "every use asks" in out, out)

    # --- binding the VM: through bin/metacomputer, confirmed by typing yes -----------------
    conf = os.path.join(td, "test-vm.env")
    open(conf, "w").write(f'VM_NAME=testvm\nSSH_PORT={port}\nSSH_DIR="{sshdir}"\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n'
                          f'ACTION_LOG=on\nACTION_LOG_KEY="{sshdir}/action-log"\nGUEST_USER=cua\n')
    wrapper = {"MCOMP_CONFIG": conf, "MCOMP_VAULT_BIN": BIN, "MCOMP_VAULT_ALLOW_TEST_BUILD": "1",
               "MCOMP_EVENTS_DIR": os.path.join(td, "events-cfg")}   # never the user's own event sinks
    mc = os.path.join(LAB, "bin/metacomputer")
    r = subprocess.run([mc, "secrets", "status"], capture_output=True, text=True,
                       env=dict(env, MCOMP_CONFIG=conf, MCOMP_VAULT_BIN=BIN), timeout=60)
    check("metacomputer secrets refuses a vault that isn't a release build", r.returncode != 0 and "isn't a release build" in r.stderr)
    if REL:
        r = subprocess.run([mc, "secrets", "status"], capture_output=True, text=True,
                           env=dict(env, MCOMP_CONFIG=conf, MCOMP_VAULT_BIN=REL), timeout=60)
        check("metacomputer secrets runs a release build", r.returncode == 0 and "locked" in r.stdout, r.stdout + r.stderr)
    r = client("bind", "testvm", "--port", str(port), "--host-key-line", hostkey, "--fill-key", sshdir + "/fill", "--fill-user", "mcfill",
               "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe)
    check("bind without a terminal refused", r.returncode != 0 and "own terminal" in r.stderr)
    fp = subprocess.run(["ssh-keygen", "-l", "-f", sshdir + "/hostkey.pub"], capture_output=True, text=True).stdout.split()[1]
    rc, out = tty([mc, "secrets", "bind", "--gateway", my_exe], [("Type yes to pin these settings", "y\n")], extra=wrapper)
    check("bind shows what it will pin, and anything but 'yes' pins nothing", rc != 0 and "not pinned" in out and fp in out
          and "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub" in out and os.path.basename(my_exe) in out and "mcfill" in out, out)
    check("metacomputer secrets bind warns while the VM's private host key is still on the Mac", "private host key is still in" in out, out)
    rc, out = tty([mc, "secrets", "bind", "--gateway", my_exe], [("Type yes to pin these settings", "yes\n"), ("Vault passphrase", PASS + "\n")],
                  extra=wrapper)
    check("metacomputer secrets bind pins this VM from the config after 'yes'", rc == 0 and f"Bound VM testvm: 127.0.0.1:{port}" in out
          and "fills as mcfill" in out and "sessions as cua" in out, out)
    common = ["--host-key-line", hostkey, "--fill-key", sshdir + "/fill", "--fill-user", "mcfill", "--ops-key", sshdir + "/ops", "--ops-user", "cua"]
    for vm, p, g in (("testvm2", port, my_exe), ("deadvm", 9, my_exe), ("wronggw", port, "/bin/sh"), ("gwvm", gw_port, gw)):
        rc, out = tty([BIN, "bind", vm, "--port", str(p), *common, "--gateway", g],
                      [("Type yes to pin", "yes\n"), ("Vault passphrase", PASS + "\n")])
        check(f"bind {vm}", rc == 0, out)
    odd = sshdir + "/fill‮x"
    shutil.copy(sshdir + "/fill", odd)
    os.chmod(odd, 0o600)
    r = client("bind", "oddvm", "--port", str(port), "--host-key-line", hostkey, "--fill-key", odd, "--fill-user", "mcfill",
               "--ops-key", sshdir + "/ops", "--ops-user", "cua", "--gateway", my_exe)
    check("bind refuses a path with invisible or control characters", r.returncode == 64 and "invisible" in r.stderr)
    raw = open(vdir + "/vault.json").read()
    check("the vault file holds no names or values in the clear",
          not any(v in raw for v in VALUES + [USERNAME, "web-login", "testvm", SITE]))

    # --- locked --------------------------------------------------------------------------
    r = client("list")
    check("list while locked: exit 3, says to ask the user to unlock", r.returncode == 3 and "metacomputer secrets unlock" in r.stderr)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("fill while locked: exit 3", r.returncode == 3 and js(r).get("reason") == "locked")
    r = client("unlock")
    check("unlock without a terminal refused", r.returncode != 0 and "own terminal" in r.stderr)
    check("nothing reached the VM while locked", calls() == [])

    def start_unlock(args=(), extra=None):
        p = Pty([BIN, "unlock", *args], dict(env, **(extra or {})))
        procs.append(p)
        p.expect("Vault passphrase: ")
        p.send(PASS + "\n")
        p.expect("unlocked until")
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        return p

    def lock(p, how="\x03"):
        p.send(how)
        return p.wait(15)

    # --- unlock 1: the listener isn't the VM's gateway ----------------------------------------
    u1 = start_unlock(allow)
    check("socket is 0600", (os.stat(sock).st_mode & 0o777) == 0o600)
    rc, out = tty([BIN, "unlock"], [("already unlocked", None)])
    check("a second unlock is refused while one runs", rc != 0)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("fill refused when the port isn't served by a program named cua-netgw", r.returncode == 2
          and "not the VM's gateway" in js(r).get("reason", ""), r.stdout)
    r = client("fill", "web-login", "--vm", "deadvm", "--field", "password")
    check("fill with nothing listening on the VM's port: exit 4", r.returncode == 4 and "nothing listens" in js(r).get("reason", ""))
    check("...and nothing was sent", calls() == [])
    check("Ctrl-C locks the vault", lock(u1) == 0 and not os.path.exists(sock))

    # --- unlock 2: the stub stands in for the VM -----------------------------------------------
    T = {"MCOMP_VAULT_TEST_LISTENERS": f"{my_name},cua-netgw", "MCOMP_VAULT_TEST_PROMPT_TIMEOUT": "3"}
    u2 = start_unlock(["--for", "10m", *allow], T)
    r = client("fill", "web-login", "--vm", "wronggw", "--field", "password")
    check("fill refused when the listener isn't the pinned gateway program (and says to bind again)", r.returncode == 2
          and "not the pinned gateway" in js(r).get("reason", "") and "bind` again" in js(r).get("reason", ""), r.stdout)
    r = client("fill", "web-login", "--vm", "gwvm", "--field", "password")
    check("fill through a listener named cua-netgw at the pinned path with the pinned contents", r.returncode == 0, r.stdout)
    changed = gw + ".new"
    shutil.copy(gw, changed)
    open(changed, "ab").write(b"\0changed")
    os.replace(changed, gw)          # the running gateway keeps its old file; the path now holds other contents
    r = client("fill", "web-login", "--vm", "gwvm", "--field", "password")
    check("fill refused when the gateway file's contents changed since bind", r.returncode == 2
          and "contents changed" in js(r).get("reason", "") and "bind` again" in js(r).get("reason", ""), r.stdout)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    res = js(r)
    check("fill a password: exit 0, chars and the typing route reported", r.returncode == 0 and res.get("ok") is True
          and res.get("chars") == len(PW_VALUE) and res.get("origin") == SITE and res.get("route") == "element", r.stdout + r.stderr)
    c = calls()[-1]
    sent = json.loads(c["stdin"])
    check("the fill goes to the fill account (mcfill) with the fill key", c["user"] == "mcfill"
          and f"IdentityFile={sshdir}/fill" in c["argv"] and c["argv"][-1] == "/opt/metacomputer/bin/secret-fill")
    check("the value reached the VM side on ssh's stdin", sent.get("value") == PW_VALUE and sent.get("origins") == [SITE])
    check("the value is on no command line and in no environment variable", PW_VALUE not in " ".join(c["argv"])
          and not any(PW_VALUE in v for v in c["env"].values()))
    check("ssh gets no agent and no caller environment", "SSH_AUTH_SOCK" not in c["env"] and "MCOMP_VAULT_DIR" not in c["env"])
    want = ["-F", "none", "BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
            "UserKnownHostsFile=/dev/null", "IdentityAgent=none", "ProxyCommand=none", "ProxyJump=none",
            "ControlPath=none", "ForwardAgent=no", "-p", str(port), "mcfill@127.0.0.1"]
    check("ssh runs with the pinned port and user, and strict host checking", all(w in c["argv"] for w in want), c["argv"])
    check("ssh trusts only the pinned host key, given as KnownHostsCommand=/bin/echo (no known_hosts file to swap)",
          c["known_hosts"] == f"[127.0.0.1]:{port} {hostkey}\n" and c["files"] == ["/dev/null", "/dev/null"], (c["known_hosts"], c["files"]))

    r = client("fill", "web-login", "--vm", "testvm", "--field", "username")
    check("fill the username", r.returncode == 0 and json.loads(calls()[-1]["stdin"])["value"] == USERNAME and USERNAME not in r.stdout)
    t0 = int(time.time())
    r = client("fill", "web-otp", "--vm", "testvm", "--field", "otp")
    code = json.loads(calls()[-1]["stdin"])["value"]
    check("fill a TOTP code: the current 6-digit code", r.returncode == 0 and code in {totp(SEED, t0 + d) for d in (-30, 0, 30)}, code)
    r = client("fill", "web-text", "--vm", "testvm", "--field", "text", "--profile", "prof1", "--selector", "#notes")
    sent = json.loads(calls()[-1]["stdin"])
    check("fill text, with the profile and selector passed on", r.returncode == 0 and sent["value"] == TEXT
          and sent.get("profile") == "prof1" and sent.get("selector") == "#notes")

    n = len(calls())
    for name, field in (("web-login", "otp"), ("web-otp", "password"), ("web-text", "password")):
        r = client("fill", name, "--vm", "testvm", "--field", field)
        check(f"{name} --field {field} refused (exit 2)", r.returncode == 2 and "fills" in js(r).get("reason", ""), r.stdout)
    r = client("fill", "nosuch", "--vm", "testvm", "--field", "password")
    check("an unknown secret: exit 4", r.returncode == 4)
    r = client("fill", "-x", "--vm", "testvm", "--field", "password")
    check("a name starting with '-': usage error", r.returncode == 64)
    check("refusals happen before anything reaches the VM", len(calls()) == n)

    # A raw request with a hostile name: refused, echoed short and printable.
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.sendall((json.dumps({"op": "fill", "name": "-bad\u001b[2J‮" + "x" * 500, "vm": "testvm", "field": "password"}) + "\n").encode())
    rep = s.makefile().readline()
    s.close()
    check("a hostile request is refused, its name echoed short and printable", json.loads(rep).get("reason") == "bad request"
          and len(json.loads(rep).get("name", "")) <= 70 and not any(x in rep for x in NASTY), rep[:200])

    flag("deny-next")
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("a refusal from the VM side: exit 2 with its reason", r.returncode == 2 and "not a password field" in js(r).get("reason", ""))
    flag("ansi-next")
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("control and bidi characters from the VM never reach the output", r.returncode == 2 and "pwned" in r.stdout
          and not any(x in r.stdout + r.stderr for x in NASTY), repr(r.stdout))
    flag("echo-value-next")
    r = client("fill", "web-otp", "--vm", "testvm", "--field", "otp")
    check("a VM answer holding the value is withheld, even a 6-digit code", r.returncode == 4
          and "withheld" in js(r).get("reason", "") and "saw" not in r.stdout, r.stdout)
    flag("no-fill-account")
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    flag("no-fill-account", on=False)
    check("a VM without the fill account: exit 4, rebuild the VM", r.returncode == 4 and "rebuild the VM" in js(r).get("reason", ""), r.stdout)

    # each-use approval: type the secret's name
    n = len(calls())
    proc = background("fill", "web-each", "--vm", "testvm", "--field", "password", "--profile", "prof1", "--selector",
                      "form   #pw\n" + "x" * 200)
    u2.expect(f"Fill web-each (password) into {SITE} on VM testvm, Chrome profile prof1, selector 'form #pw " + "x" * 71 + "...'?")
    u2.expect("Type web-each to allow")
    u2.send("y\n")
    rc, out, _ = finish(proc)
    check("approval: answering y no longer allows (the name must be typed)", rc == 2 and "not allowed" in out, out)
    new = calls()[n:]
    check("...only a check reached the VM, without the value", [x["kind"] for x in new] == ["fill:check"]
          and "value" not in json.loads(new[0]["stdin"]))
    proc = background("fill", "web-each", "--vm", "testvm", "--field", "password")
    u2.expect("Type web-each to allow")
    u2.send("web-each\n")
    rc, out, _ = finish(proc)
    fill_req = json.loads(calls()[-1]["stdin"])
    check("approval: typing the name allows it", rc == 0 and json.loads(out)["ok"] is True, out)
    check("...and the value goes only to the approved site", fill_req.get("value") == PW_VALUE and fill_req.get("origins") == [SITE]
          and json.loads(calls()[-2]["stdin"]).get("origins") == [SITE, SITE2])
    flag("evil-origin-next")
    n = len(calls())
    r = client("fill", "web-each", "--vm", "testvm", "--field", "password")
    check("a check naming a site outside the secret's is refused before asking", r.returncode == 4
          and "outside this secret's sites" in js(r).get("reason", "") and len(calls()) == n + 1)
    t = time.time()
    r = client("fill", "web-each", "--vm", "testvm", "--field", "password")
    check("approval with no answer: refused after the timeout", r.returncode == 2 and time.time() - t >= 2.5)
    u2.expect("(no answer in 3 s")
    # the gateway must still be there after the approval
    n = len(calls())
    proc = background("fill", "web-each", "--vm", "testvm", "--field", "password")
    u2.expect("Type web-each to allow")
    listener.close()
    u2.send("web-each\n")
    rc, out, _ = finish(proc)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(4)
    check("the listener is checked again after approval, before the value is sent", rc == 4 and "nothing listens" in out
          and [x["kind"] for x in calls()[n:]] == ["fill:check"], out)

    r = client("list")
    check("list while unlocked: names, sites, approval, username, VMs", r.returncode == 0 and all(
        s in r.stdout for s in ("web-login", "web-otp", "web-each", SITE, USERNAME, "approval each", "testvm", "fills as mcfill")), r.stdout)
    lj = json.loads(client("list", "--json").stdout)
    check("list --json: no value field", not any("value" in s for s in lj["secrets"]))

    # sealed browser sessions: saving always asks
    n = len(calls())
    proc = background("session", "save", "s1", "--vm", "testvm", "--profile", "prof1")
    u2.expect("Save Chrome profile prof1 from VM testvm as the sealed session s1?")
    u2.send("no\n")
    rc, out, _ = finish(proc)
    check("session save asks, and a wrong answer saves nothing", rc == 2 and len(calls()) == n
          and not os.path.exists(os.path.join(vdir, "sessions", "s1.sealed")), out)
    proc = background("session", "save", "s1", "--vm", "testvm", "--profile", "prof1")
    u2.expect("as the sealed session s1?")
    u2.send("s1\n")
    rc, out, _ = finish(proc)
    sealed = os.path.join(vdir, "sessions", "s1.sealed")
    check("session save seals the profile after the name is typed", rc == 0 and json.loads(out).get("bytes") == len(archive)
          and os.path.exists(sealed), out)
    blob = open(sealed, "rb").read() if os.path.exists(sealed) else b""
    check("the sealed file is encrypted", archive not in blob and b"dummy-cookie-jar" not in blob and (os.stat(sealed).st_mode & 0o777) == 0o600)
    save_cmd = calls()[-1]["argv"][-1]
    check("save runs as the desktop user, skips caches, refuses a running Chrome, and ends options with --",
          calls()[-1]["user"] == "cua" and "[-]-user-data-dir=" in save_cmd and "Cache" in save_cmd and "-czf - -- 'prof1'" in save_cmd)
    r = client("session", "save", "s1", "--vm", "testvm", "--profile", "prof1")
    check("saving over an existing session needs --replace", r.returncode == 2 and "--replace" in js(r).get("reason", ""))
    proc = background("session", "save", "s1", "--vm", "testvm", "--profile", "prof1", "--replace")
    u2.expect("Replace the sealed session s1")
    u2.send("s1\n")
    rc, out, _ = finish(proc)
    check("--replace asks too", rc == 0, out)
    r = client("session", "load", "s1", "--vm", "testvm", "--profile", "prof2")
    check("loading under another profile name is refused (the driver's marker names the profile)", r.returncode == 2
          and "saved from profile prof1" in js(r).get("reason", "") and ".cua-driver-owned-profile.json" in js(r).get("reason", ""))
    proc = background("session", "load", "s1", "--vm", "testvm")
    u2.expect("Load the sealed session s1 into profile prof1 on VM testvm")
    u2.send("s1\n")
    rc, out, _ = finish(proc)
    loaded = open(os.path.join(stubdir, "loaded.tgz"), "rb").read() if os.path.exists(os.path.join(stubdir, "loaded.tgz")) else b""
    check("session load (asks by default) puts the same archive back, under its own profile name", rc == 0 and loaded == archive, out)
    load_cmd = calls()[-1]["argv"][-1]
    check("load runs as the desktop user and unpacks with the safe loader", calls()[-1]["user"] == "cua"
          and "mcomp-session-load" in load_cmd and "/browser-profiles/prof1" in load_cmd and "[ ! -L " in load_cmd)

    # the loader itself, on archives that try to escape
    loader = shlex.split(load_cmd[load_cmd.index("exec python3"):])[4]
    root = os.path.join(td, "loadroot")

    def load_local(data):
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root)
        return subprocess.run([sys.executable, "-I", "-c", loader, root, "prof1"], input=data, capture_output=True, timeout=60)
    r = load_local(archive)
    check("loader: a good archive becomes the profile folder", r.returncode == 0
          and open(os.path.join(root, "prof1/Default/Cookies"), "rb").read().startswith(b"dummy-cookie-jar")
          and not [x for x in os.listdir(root) if x.startswith(".mcomp-load")], r.stderr)
    for what, entries in (("a '..' entry", [("prof1", "dir", None), ("prof1/../../escaped", "file", b"x")]),
                          ("an absolute path", [("/tmp/mcomp-escaped", "file", b"x")]),
                          ("an entry outside the profile folder", [("other/x", "file", b"x")]),
                          ("a link leading outside", [("prof1", "dir", None), ("prof1/link", "sym", "/etc/passwd")]),
                          ("a special file", [("prof1", "dir", None), ("prof1/fifo", "fifo", None)])):
        r = load_local(tarball(entries))
        check(f"loader refuses {what}", r.returncode == 5 and "refusing" in r.stderr.decode()
              and not os.path.exists(os.path.join(root, "prof1")) and not os.path.exists(os.path.join(td, "escaped")), r.stderr)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    os.symlink(td, os.path.join(root, "prof1"))
    r = subprocess.run([sys.executable, "-I", "-c", loader, root, "prof1"], input=archive, capture_output=True, timeout=60)
    check("loader refuses a profile folder that is a symbolic link", r.returncode == 5 and "symbolic link" in r.stderr.decode())

    # a passphrase-gated policy: same-VM loads stop asking; other VMs still ask
    rc, out = tty([BIN, "session", "policy", "s1", "--approval", "session"], [("Vault passphrase", PASS + "\n")])
    check("session policy (with the passphrase)", rc == 0 and "no longer asks" in out, out)
    r = client("session", "load", "s1", "--vm", "testvm")
    check("with policy 'session', loading into the same VM doesn't ask", r.returncode == 0, r.stdout)
    proc = background("session", "load", "s1", "--vm", "testvm2")
    u2.expect("Load the sealed session s1, saved from VM testvm, into a different VM, testvm2")
    u2.send("s1\n")
    rc, out, _ = finish(proc)
    check("loading into a different VM asks, naming both", rc == 0, out)
    with open(sealed, "r+b") as f:
        f.seek(40)
        b = f.read(1)
        f.seek(40)
        f.write(bytes([b[0] ^ 1]))
    r = client("session", "load", "s1", "--vm", "testvm")
    check("a changed sealed file is refused", r.returncode == 4 and "changed" in js(r).get("reason", ""))

    # changes while unlocked need the passphrase; the server sees them at once
    rc, out = tty([BIN, "delete", "web-text"], [("Vault passphrase", PASS + "\n")])
    check("delete (with the passphrase) while unlocked", rc == 0 and "web-text" not in client("list").stdout)

    # through bin/metacomputer, with the signed action log on
    r = subprocess.run([mc, "secrets", "fill", "web-login", "--field", "password"], capture_output=True, text=True,
                       env=dict(env, **wrapper), timeout=120)
    outputs.append(r.stdout + r.stderr)
    check("metacomputer secrets fill (the VM from the config)", r.returncode == 0 and js(r).get("vm") == "testvm", r.stdout + r.stderr)
    adir = os.path.join(td, "logs", "actions")
    files = [os.path.join(d, f) for d, _, fs in os.walk(adir) for f in fs if f.endswith(".jsonl")]
    recs = [json.loads(l) for f in files for l in open(f)]
    ev = [x for x in recs if x.get("type") == "secrets_fill"]
    check("the fill is in the signed action log, without the value, and it verifies", len(ev) == 1 and ev[0].get("chars") == len(PW_VALUE)
          and not any(v in open(f).read() for f in files for v in VALUES)
          and all(actionlog.verify_file(f, adir, sshdir + "/action-log.pub")["ok"] for f in files), recs)

    # the terminal closing locks it
    u2.close()
    u2.wait(15)
    check("closing the terminal locks the vault", not os.path.exists(sock))
    term = u2.text()
    check("the unlock terminal showed each request and outcome", "fill web-login (password) on VM testvm" in term
          and "-> ok: " + SITE in term and "-> denied" in term)
    check("the unlock terminal never showed a value or a control character from the VM",
          not any(v in term for v in VALUES + [USERNAME, code]) and not any(x in term for x in NASTY))

    # --- unlock 3: prompts end when the vault's time does ------------------------------------
    u3 = start_unlock(["--for", "4s", *allow], {"MCOMP_VAULT_TEST_LISTENERS": my_name})
    t = time.time()
    r = client("fill", "web-each", "--vm", "testvm", "--field", "password")
    check("a question can't outlast the unlock (no answer, 4 s left: refused within the time)", r.returncode in (2, 3)
          and time.time() - t < 8, (r.returncode, time.time() - t))
    check("the vault locks when its time is up", u3.wait(15) == 0 and not os.path.exists(sock) and "time is up" in u3.text())

    # --- a multiplexed terminal: confirmations refused unless allowed --------------------------
    M = {"TMUX": "/tmp/tmux-test/default,1,0", "MCOMP_VAULT_TEST_LISTENERS": my_name}
    rc, out = tty([BIN, "unlock", "--confirm", "each"], [("runs inside tmux", None)], extra=M)
    check("unlock --confirm each is refused inside tmux", rc != 0 and "--allow-multiplexer" in out)
    for var, name in (("WEZTERM_PANE", "WezTerm"), ("KITTY_LISTEN_ON", "kitty")):
        rc, out = tty([BIN, "unlock", "--confirm", "each"], [(f"runs inside {name}", None)], extra={var: "1"})
        check(f"...and inside {name} (${var})", rc != 0)
    u4 = start_unlock((), M)
    u4.expect("WARNING: this terminal runs inside tmux")
    n = len(calls())
    r = client("fill", "web-each", "--vm", "testvm", "--field", "password")
    check("inside tmux, a use that needs a confirmation is refused without asking", r.returncode == 2
          and "inside tmux" in js(r).get("reason", "") and [x["kind"] for x in calls()[n:]] == ["fill:check"], r.stdout)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("...uses that need none still work", r.returncode == 0)
    lock(u4)
    u5 = start_unlock(["--confirm", "each", "--allow-multiplexer"], M)
    proc = background("fill", "web-login", "--vm", "testvm", "--field", "password")
    u5.expect("Type web-login to allow")
    u5.send("web-login\n")
    rc, out, _ = finish(proc)
    check("--allow-multiplexer takes confirmations there anyway (--confirm each asks for every secret)", rc == 0, out)
    lock(u5)

    # --- a damaged header can't make unlock hang or crash ----------------------------------------
    original = open(vdir + "/vault.json", "rb").read()
    h = json.loads(original)
    h["kdf"]["iterations"] = 10 ** 12
    open(vdir + "/vault.json", "w").write(json.dumps(h))
    rc, out = tty([BIN, "delete", "web-login"], [("Vault passphrase", PASS + "\n")])
    open(vdir + "/vault.json", "wb").write(original)
    check("a vault header with absurd KDF rounds is refused, not run", rc == 1 and "out of range" in out, out)

    # --- the audit log and every output ---------------------------------------------------------
    alog = os.path.join(vdir, "audit.jsonl")
    text = open(alog).read()
    recs = [json.loads(l) for l in text.splitlines()]
    check("audit log is 0600 and records fills with their outcome and approval", (os.stat(alog).st_mode & 0o777) == 0o600
          and any(x["op"] == "fill" and x["result"] == "ok" and x.get("chars") == len(PW_VALUE) for x in recs)
          and any(x["op"] == "fill" and x.get("approval") == "each:no" for x in recs)
          and any(x["op"] == "fill" and x["result"] == "locked" for x in recs))
    check("audit log records unlock and lock", {x.get("reason") for x in recs if x["op"] == "lock"} >= {"Ctrl-C", "time is up", "the terminal closed"})
    check("audit log holds no values and no control characters", not any(v in text for v in VALUES + [USERNAME])
          and not any(x in text for x in NASTY))
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no value in the output of any of {len(outputs)} client commands", not leaked, leaked)
    check("no control character from the VM in any client output", not any(x in o for o in outputs for x in NASTY))


if __name__ == "__main__":
    sys.exit(main())
