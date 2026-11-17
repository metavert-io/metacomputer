#!/usr/bin/env python3
"""Offline checks for `metacomputer secrets` (the mcomp-vault program in vault/).

No VM needed. A TEST build of mcomp-vault (scripts/build-vault.sh --test) runs a stub
instead of /usr/bin/ssh (MCOMP_VAULT_TEST_SSH) and accepts this test's own listener in
place of the VM's gateway (MCOMP_VAULT_TEST_LISTENERS); release builds don't contain
those switches, which this test also checks. The passphrase, values and y/N answers are
typed into a pseudo-terminal, as a person would. Every value here is a dummy.

Run: python3 tests/test_secrets.py
     MCOMP_VAULT_TEST_BIN=/path/to/test-build python3 tests/test_secrets.py   (skip building;
     MCOMP_VAULT_SKIP_RELEASE=1 also skips the release build and its check)
"""
import base64
import gzip
import hmac
import io
import json
import os
import pty
import select
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
VALUES = [PASS, PW_VALUE, SEED, TEXT]          # must never appear in any output, log or the vault file
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
        self.buf, self.pos, self.status = b"", 0, None
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

    eof = False

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


STUB = r'''#!{python}
# Stands in for /usr/bin/ssh in a TEST build of mcomp-vault: records what it got, answers like the VM.
import json, os, sys
LOG = {log!r}
argv = sys.argv[1:]
remote = argv[-1] if argv else ""
rec = {{"argv": argv, "env": dict(os.environ)}}
kh = next((a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile=")), None)
rec["known_hosts"] = open(kh).read() if kh and os.path.exists(kh) else None
d = os.path.dirname(LOG)
if remote.startswith("python3 -I -c "):
    raw = sys.stdin.buffer.read()
    rec["stdin"] = raw.decode()
    req = json.loads(raw)
    rec["kind"] = "fill:" + req.get("mode", "?")
    if os.path.exists(os.path.join(d, "deny-next")):
        os.remove(os.path.join(d, "deny-next"))
        out = {{"ok": False, "denied": True, "reason": "the focused element is <input type=text>, not a password field"}}
    elif req.get("mode") == "check":
        out = {{"ok": True, "mode": "check", "field": req["field"], "origin": req["origins"][0]}}
    else:
        out = {{"ok": True, "mode": "fill", "field": req["field"], "origin": req["origins"][0], "chars": len(req["value"])}}
    print(json.dumps(out))
elif "-czf -" in remote:
    rec["kind"] = "session:save"
    sys.stdout.buffer.write(open(os.path.join(d, "archive.tgz"), "rb").read())
elif "-xzf -" in remote:
    rec["kind"] = "session:load"
    open(os.path.join(d, "loaded.tgz"), "wb").write(sys.stdin.buffer.read())
else:
    rec["kind"] = "other"
with open(LOG, "a") as f:
    f.write(json.dumps(rec) + "\n")
'''


def main():
    td = tempfile.mkdtemp(prefix="mcv-")
    if len(td) > 60:              # Unix socket paths are limited to 104 bytes
        shutil.rmtree(td)
        td = tempfile.mkdtemp(prefix="mcv-", dir="/tmp")
    unlock_procs = []
    try:
        run(td, unlock_procs)
    finally:
        for p in unlock_procs:
            try:
                os.kill(p.pid, signal.SIGKILL)
                os.waitpid(p.pid, 0)
            except (OSError, ChildProcessError):
                pass
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


def run(td, unlock_procs):
    # --- build ---------------------------------------------------------------------
    bins = os.path.join(td, "bin")
    BIN = os.environ.get("MCOMP_VAULT_TEST_BIN")
    if not BIN:
        BIN = os.path.join(bins, "mcomp-vault-test")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--test", BIN], capture_output=True, text=True)
        check("build: test binary", r.returncode == 0, r.stderr[-800:])
    if not os.environ.get("MCOMP_VAULT_SKIP_RELEASE"):
        rel = os.path.join(bins, "mcomp-vault")
        r = subprocess.run([os.path.join(LAB, "scripts/build-vault.sh"), "--out", rel], capture_output=True, text=True)
        check("build: release binary", r.returncode == 0, r.stderr[-800:])
        if r.returncode == 0:
            blob = open(rel, "rb").read()
            check("release build has no test switches", b"MCOMP_VAULT_TEST_" not in blob)
            check("release build: selftest", subprocess.run([rel, "selftest"], capture_output=True).returncode == 0)
            v = json.loads(subprocess.run([rel, "version", "--json"], capture_output=True, text=True).stdout)
            check("release build says so", v.get("build") == "release")
            sig = subprocess.run(["codesign", "-dv", rel], capture_output=True, text=True).stderr
            check("release build: ad hoc signature with the hardened runtime", "adhoc,runtime" in sig, sig)
    check("test build has its test switches", b"MCOMP_VAULT_TEST_SSH" in open(BIN, "rb").read())
    st = subprocess.run([BIN, "selftest"], capture_output=True, text=True)
    check("selftest (TOTP RFC 6238, PBKDF2, AES-GCM, base32, origins)", st.returncode == 0 and "all passed" in st.stdout, st.stdout[-500:])
    check("test build says so", json.loads(subprocess.run([BIN, "version", "--json"], capture_output=True, text=True).stdout)["build"] == "test")

    # --- a fake VM endpoint: a listener, keys, the ssh stub ---------------------------
    home = os.path.join(td, "home")
    vdir = os.path.join(td, "v")
    sshdir = os.path.join(td, "ssh")
    os.makedirs(home)
    os.makedirs(sshdir, mode=0o700)
    for k in ("ops", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "dummy", "-f", os.path.join(sshdir, k)], check=True)
    hostkey = " ".join(open(os.path.join(sshdir, "hostkey.pub")).read().split()[:2])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    open(os.path.join(sshdir, "known_hosts"), "w").write(f"[127.0.0.1]:{port} {hostkey}\n")
    lsof = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True).stdout
    my_name = next((l[1:] for l in lsof.splitlines() if l.startswith("c")), "")
    check("this test's listener is visible to lsof", bool(my_name), lsof)
    stubdir = os.path.join(td, "stub")
    os.makedirs(stubdir)
    stub_log = os.path.join(stubdir, "calls.jsonl")
    stub = os.path.join(stubdir, "ssh")
    open(stub, "w").write(STUB.format(python=sys.executable, log=stub_log))
    os.chmod(stub, 0o755)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"dummy-cookie-jar-5150" * 50
        ti = tarfile.TarInfo("prof1/Default/Cookies")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    archive = buf.getvalue()
    open(os.path.join(stubdir, "archive.tgz"), "wb").write(archive)

    def calls():
        return [json.loads(l) for l in open(stub_log)] if os.path.exists(stub_log) else []

    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "MCOMP_VAULT_DIR": vdir, "LANG": "en_US.UTF-8",
           "MCOMP_VAULT_TEST_SSH": stub}
    sock = os.path.join(vdir, "vault.sock")

    def client(*args, extra=None):
        e = dict(env, **(extra or {}))
        r = subprocess.run([BIN, *args], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL, timeout=120)
        outputs.append(r.stdout + r.stderr)
        return r

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

    def fill_json(r):
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
    check("init creates the vault", rc == 0 and "Created the vault" in out, out)
    check("init: the passphrase isn't echoed", PASS not in out)
    check("vault folder 0700, vault file 0600", (os.stat(vdir).st_mode & 0o777) == 0o700
          and (os.stat(vdir + "/vault.json").st_mode & 0o777) == 0o600)
    hdr = json.load(open(vdir + "/vault.json"))
    check("vault header: PBKDF2-SHA256 with at least 600k rounds, AES-256-GCM",
          hdr["kdf"]["name"] == "pbkdf2-sha256" and hdr["kdf"]["iterations"] >= 600000 and hdr["cipher"] == "aes-256-gcm"
          and len(base64.b64decode(hdr["kdf"]["salt"])) == 16)
    rc, out = tty([BIN, "init"], [("already exists", None)])
    check("init refuses to replace a vault", rc != 0)

    # --- storing secrets -----------------------------------------------------------------
    r = client("set", "web-login", "--kind", "password", "--origin", SITE, "--username", USERNAME)
    check("set without a terminal refused", r.returncode != 0 and "own terminal" in r.stderr)
    for bad in ("https://example.com/login", "http://example.com", "ftp://example.com", "example.com", "https://a b.com"):
        r = client("set", "x", "--kind", "password", "--origin", bad)
        check(f"set refuses the origin {bad!r} before asking anything", r.returncode == 64 and "--origin" in r.stderr)
    r = client("set", "x", "--kind", "password")
    check("set refuses a secret without an origin", r.returncode == 64 and "at least one --origin" in r.stderr)
    r = client("set", "a/b", "--kind", "password", "--origin", SITE)
    check("set refuses a bad name", r.returncode == 64)
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE, "--username", USERNAME],
                  [("Vault passphrase", "wrong passphrase 123\n")])
    check("set with the wrong passphrase refused", rc != 0 and "wrong passphrase" in out)
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE, "--username", USERNAME],
                  [("Vault passphrase", PASS + "\n"), ("Password for web-login", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set a password with a username", rc == 0 and "Saved web-login" in out, out)
    check("set: the password isn't echoed", PW_VALUE not in out and PASS not in out)
    rc, out = tty([BIN, "set", "web-otp", "--kind", "totp", "--origin", SITE],
                  [("Vault passphrase", PASS + "\n"), ("TOTP seed", SEED + "\n")])
    check("set a TOTP seed", rc == 0 and "Saved web-otp" in out and SEED not in out, out)
    rc, out = tty([BIN, "set", "web-otp2", "--kind", "totp", "--origin", SITE],
                  [("Vault passphrase", PASS + "\n"), ("TOTP seed", "not*base32\n")])
    check("set refuses a seed that isn't base32", rc != 0 and "base32" in out)
    rc, out = tty([BIN, "set", "web-text", "--kind", "text", "--origin", SITE],
                  [("Vault passphrase", PASS + "\n"), ("Value for web-text", TEXT + "\n")])
    check("set a text value", rc == 0 and TEXT not in out, out)
    rc, out = tty([BIN, "set", "web-each", "--kind", "password", "--origin", SITE, "--approval", "each"],
                  [("Vault passphrase", PASS + "\n"), ("Password for web-each", PW_VALUE + "\n"), ("same password again", PW_VALUE + "\n")])
    check("set a password that asks before each use", rc == 0 and "every use asks" in out, out)
    rc, out = tty([BIN, "set", "web-login", "--kind", "password", "--origin", SITE],
                  [("Vault passphrase", PASS + "\n"), ("Replace it?", "n\n")])
    check("set asks before replacing, and 'n' keeps it", rc != 0 and "left unchanged" in out)

    # --- binding the VM (through bin/metacomputer, which fills in this config's VM) -------
    conf = os.path.join(td, "test-vm.env")
    open(conf, "w").write(f'VM_NAME=testvm\nSSH_PORT={port}\nSSH_DIR="{sshdir}"\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n'
                          f'ACTION_LOG=on\nACTION_LOG_KEY="{sshdir}/action-log"\nGUEST_USER=cua\n')
    wrapper = {"MCOMP_CONFIG": conf, "MCOMP_VAULT_BIN": BIN}
    r = client("bind", "testvm", "--port", str(port), "--host-key-line", hostkey, "--key", sshdir + "/ops", "--user", "cua")
    check("bind without a terminal refused", r.returncode != 0 and "own terminal" in r.stderr)
    rc, out = tty([os.path.join(LAB, "bin/metacomputer"), "secrets", "bind"], [("Vault passphrase", PASS + "\n")], extra=wrapper)
    fp = subprocess.run(["ssh-keygen", "-l", "-f", sshdir + "/hostkey.pub"], capture_output=True, text=True).stdout.split()[1]
    check("metacomputer secrets bind pins this VM from the config", rc == 0 and f"Bound VM testvm: cua@127.0.0.1:{port}" in out
          and fp in out, out)
    rc, out = tty([BIN, "bind", "deadvm", "--port", "9", "--host-key-line", hostkey, "--key", sshdir + "/ops", "--user", "cua"],
                  [("Vault passphrase", PASS + "\n")])
    check("bind a second VM", rc == 0)
    raw = open(vdir + "/vault.json").read()
    check("the vault file holds no names or values in the clear",
          not any(v in raw for v in VALUES + [USERNAME, "web-login", "testvm", SITE]))

    # --- locked --------------------------------------------------------------------
    r = client("list")
    check("list while locked: exit 3, says to ask the user to unlock", r.returncode == 3 and "metacomputer secrets unlock" in r.stderr)
    check("list while locked shows no names", "web-login" not in r.stdout + r.stderr)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("fill while locked: exit 3", r.returncode == 3 and fill_json(r).get("reason") == "locked"
          and "own terminal" in r.stderr, r.stdout + r.stderr)
    r = client("session", "load", "s1", "--vm", "testvm", "--profile", "prof1")
    check("session load while locked: exit 3", r.returncode == 3)
    r = client("status")
    check("status while locked", r.returncode == 0 and "locked" in r.stdout)
    r = client("unlock")
    check("unlock without a terminal refused", r.returncode != 0 and "own terminal" in r.stderr)
    check("nothing reached the VM while locked", calls() == [])

    def start_unlock(args=(), extra=None):
        p = Pty([BIN, "unlock", *args], dict(env, **(extra or {})))
        unlock_procs.append(p)
        p.expect("Vault passphrase: ")
        p.send(PASS + "\n")
        p.expect("unlocked until")
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        return p

    # --- unlock 1: the listener isn't the VM's gateway ------------------------------------
    u1 = start_unlock()
    check("socket is 0600", (os.stat(sock).st_mode & 0o777) == 0o600)
    rc, out = tty([BIN, "unlock"], [("already unlocked", None)])
    check("a second unlock is refused while one runs", rc != 0)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("fill refused when the port isn't served by cua-netgw", r.returncode == 2 and "not the VM's gateway" in fill_json(r).get("reason", ""),
          r.stdout)
    r = client("fill", "web-login", "--vm", "deadvm", "--field", "password")
    check("fill with nothing listening on the VM's port: exit 4", r.returncode == 4 and "nothing listens" in fill_json(r).get("reason", ""))
    check("...and nothing was sent", calls() == [])
    u1.send("\x03")
    u1.expect("locked (Ctrl-C)")
    check("Ctrl-C locks the vault", u1.wait(15) == 0 and not os.path.exists(sock))
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("fill after Ctrl-C: exit 3", r.returncode == 3)

    # --- unlock 2: the stub stands in for the VM ---------------------------------------------
    u2 = start_unlock(("--for", "10m"), {"MCOMP_VAULT_TEST_LISTENERS": my_name, "MCOMP_VAULT_TEST_PROMPT_TIMEOUT": "3"})
    r = client("status")
    check("status while unlocked", r.returncode == 0 and "unlocked until" in r.stdout)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    res = fill_json(r)
    check("fill a password: exit 0, chars reported", r.returncode == 0 and res.get("ok") is True and res.get("chars") == len(PW_VALUE)
          and res.get("origin") == SITE and res.get("field") == "password", r.stdout + r.stderr)
    c = calls()[-1]
    sent = json.loads(c["stdin"])
    check("the value reached the VM side on ssh's stdin", sent.get("value") == PW_VALUE and sent.get("mode") == "fill"
          and sent.get("origins") == [SITE] and sent.get("field") == "password")
    check("the value is on no command line", PW_VALUE not in " ".join(c["argv"]))
    check("the value is in no environment variable", not any(PW_VALUE in v for v in c["env"].values()))
    check("ssh gets no agent and no caller environment", "SSH_AUTH_SOCK" not in c["env"] and "MCOMP_VAULT_DIR" not in c["env"])
    a = c["argv"]
    want = ["-F", "/dev/null", "BatchMode=yes", "IdentitiesOnly=yes", f"IdentityFile={sshdir}/ops", "StrictHostKeyChecking=yes",
            "GlobalKnownHostsFile=/dev/null", "ControlPath=none", "ForwardAgent=no", "-p", str(port), "cua@127.0.0.1"]
    check("ssh runs with the pinned key, port and user, and strict host checking", all(w in a for w in want), a)
    check("ssh trusts only the pinned host key", c["known_hosts"] == f"[127.0.0.1]:{port} {hostkey}\n", c["known_hosts"])
    check("ssh runs the built-in guest script with python3 -I", a[-1].startswith("python3 -I -c '") and "def main()" in a[-1])
    check("the pinned known_hosts file is removed afterwards", not os.path.exists(next(x.split("=", 1)[1] for x in a if x.startswith("UserKnownHostsFile="))))

    r = client("fill", "web-login", "--vm", "testvm", "--field", "username")
    check("fill the username", r.returncode == 0 and json.loads(calls()[-1]["stdin"])["value"] == USERNAME, r.stdout)
    check("the username isn't in the fill's output", USERNAME not in r.stdout + r.stderr)
    t0 = int(time.time())
    r = client("fill", "web-otp", "--vm", "testvm", "--field", "otp")
    code = json.loads(calls()[-1]["stdin"])["value"]
    check("fill a TOTP code: the current 6-digit code", r.returncode == 0 and code in {totp(SEED, t0 + d) for d in (-30, 0, 30)}, code)
    check("the code isn't in the fill's output", code not in r.stdout + r.stderr)
    r = client("fill", "web-text", "--vm", "testvm", "--field", "text", "--profile", "prof1", "--selector", "#notes")
    sent = json.loads(calls()[-1]["stdin"])
    check("fill text, with the profile and selector passed on", r.returncode == 0 and sent["value"] == TEXT
          and sent.get("profile") == "prof1" and sent.get("selector") == "#notes")

    n = len(calls())
    for name, field in (("web-login", "otp"), ("web-login", "text"), ("web-otp", "password"), ("web-otp", "username"),
                        ("web-text", "password")):
        r = client("fill", name, "--vm", "testvm", "--field", field)
        check(f"{name} --field {field} refused (exit 2)", r.returncode == 2 and "fills" in fill_json(r).get("reason", ""), r.stdout)
    r = client("fill", "nosuch", "--vm", "testvm", "--field", "password")
    check("an unknown secret: exit 4", r.returncode == 4 and "no secret named nosuch" in fill_json(r).get("reason", ""))
    r = client("fill", "web-login", "--vm", "othervm", "--field", "password")
    check("a VM that isn't bound: exit 4", r.returncode == 4 and "isn't bound" in fill_json(r).get("reason", ""))
    r = client("fill", "web-login", "--vm", "testvm", "--field", "secret")
    check("an unknown field: usage error", r.returncode == 64)
    check("refusals happen before anything reaches the VM", len(calls()) == n)

    open(os.path.join(stubdir, "deny-next"), "w").close()
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("a refusal from the VM side: exit 2 with its reason", r.returncode == 2 and "not a password field" in fill_json(r).get("reason", ""))

    # each-use approval at the unlock terminal
    n = len(calls())
    proc = subprocess.Popen([BIN, "fill", "web-each", "--vm", "testvm", "--field", "password"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    u2.expect(f"Allow fill of web-each (password) into {SITE} on VM testvm? [y/N]")
    u2.send("n\n")
    out, err = proc.communicate(timeout=60)
    outputs.append(out + err)
    check("approval 'each', answered n: exit 2", proc.returncode == 2 and "not allowed" in out, out)
    new = calls()[n:]
    check("...only the check reached the VM, without the value", [x["kind"] for x in new] == ["fill:check"]
          and "value" not in json.loads(new[0]["stdin"]))
    proc = subprocess.Popen([BIN, "fill", "web-each", "--vm", "testvm", "--field", "password"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    u2.expect("Allow fill of web-each")
    u2.send("y\n")
    out, err = proc.communicate(timeout=60)
    outputs.append(out + err)
    check("approval 'each', answered y: exit 0", proc.returncode == 0 and json.loads(out)["ok"] is True, out)
    check("...the value went only with the approved fill", json.loads(calls()[-1]["stdin"]).get("value") == PW_VALUE
          and calls()[-2]["kind"] == "fill:check")
    t = time.time()
    r = client("fill", "web-each", "--vm", "testvm", "--field", "password")
    check("approval 'each' with no answer: refused after the timeout", r.returncode == 2 and time.time() - t >= 2.5)

    r = client("list")
    check("list while unlocked: names, kinds, sites, approval, username", r.returncode == 0 and all(
        s in r.stdout for s in ("web-login", "web-otp", "web-text", "web-each", SITE, USERNAME, "approval each", "testvm")), r.stdout)
    r = client("list", "--json")
    lj = json.loads(r.stdout)
    login = next(s for s in lj["secrets"] if s["name"] == "web-login")
    check("list --json: metadata and use counts, no value field", r.returncode == 0 and login["uses"] == 2
          and "last_used" in login and not any("value" in s for s in lj["secrets"]))
    check("list --json: the VM's pinned host key fingerprint", any(v["name"] == "testvm" and v["host_key"] == fp for v in lj["vms"]))

    # sealed browser sessions
    r = client("session", "save", "s1", "--vm", "testvm", "--profile", "prof1")
    sealed = os.path.join(vdir, "sessions", "s1.sealed")
    check("session save seals the profile", r.returncode == 0 and fill_json(r).get("bytes") == len(archive)
          and os.path.exists(sealed), r.stdout + r.stderr)
    blob = open(sealed, "rb").read() if os.path.exists(sealed) else b""
    check("the sealed file is encrypted", archive not in blob and b"dummy-cookie-jar" not in blob and len(blob) == len(archive) + 28
          and (os.stat(sealed).st_mode & 0o777) == 0o600)
    save_cmd = calls()[-1]["argv"][-1]
    check("save refuses while Chrome uses the profile and skips caches", "[-]-user-data-dir=" in save_cmd and "exit 4" in save_cmd
          and "Cache" in save_cmd and "/home/" + "cua" + "/.local/state/cua-driver/browser-profiles" in save_cmd)
    r = client("session", "save", "s1", "--vm", "testvm", "--profile", "prof1")
    check("saving over an existing session needs --replace", r.returncode == 2 and "--replace" in fill_json(r).get("reason", ""))
    r = client("session", "load", "s1", "--vm", "testvm", "--profile", "prof2")
    loaded = open(os.path.join(stubdir, "loaded.tgz"), "rb").read() if os.path.exists(os.path.join(stubdir, "loaded.tgz")) else b""
    check("session load puts the same archive back", r.returncode == 0 and loaded == archive, r.stdout + r.stderr)
    load_cmd = calls()[-1]["argv"][-1]
    check("load replaces the target profile from the saved folder", "/browser-profiles/prof2" in load_cmd and '"$t/prof1"' in load_cmd)
    with open(sealed, "r+b") as f:
        f.seek(40)
        b = f.read(1)
        f.seek(40)
        f.write(bytes([b[0] ^ 1]))
    r = client("session", "load", "s1", "--vm", "testvm", "--profile", "prof2")
    check("a changed sealed file is refused", r.returncode == 4 and "changed" in fill_json(r).get("reason", ""))

    # changes while unlocked: delete needs the passphrase, and the server sees it at once
    rc, out = tty([BIN, "delete", "web-text"], [("Vault passphrase", PASS + "\n")])
    check("delete (with the passphrase) while unlocked", rc == 0)
    r = client("list")
    check("...the unlocked vault sees it at once", r.returncode == 0 and "web-text" not in r.stdout)

    # through bin/metacomputer, with the signed action log on
    r = subprocess.run([os.path.join(LAB, "bin/metacomputer"), "secrets", "fill", "web-login", "--field", "password"],
                       capture_output=True, text=True, env=dict(env, **wrapper), timeout=120)
    outputs.append(r.stdout + r.stderr)
    check("metacomputer secrets fill (the VM from the config)", r.returncode == 0 and fill_json(r).get("vm") == "testvm", r.stdout + r.stderr)
    adir = os.path.join(td, "logs", "actions")
    files = [os.path.join(d, f) for d, _, fs in os.walk(adir) for f in fs if f.endswith(".jsonl")]
    recs = [json.loads(l) for f in files for l in open(f)]
    ev = [x for x in recs if x.get("type") == "secrets_fill"]
    check("the fill is in the signed action log", len(ev) == 1 and ev[0].get("name") == "web-login" and ev[0].get("chars") == len(PW_VALUE)
          and ev[0].get("origin") == SITE, recs)
    check("...without the value", not any(v in open(f).read() for f in files for v in VALUES))
    check("...and it verifies", files and all(actionlog.verify_file(f, adir, sshdir + "/action-log.pub")["ok"] for f in files))
    r = subprocess.run([os.path.join(LAB, "bin/metacomputer"), "secrets", "fill", "web-login", "--field", "otp"],
                       capture_output=True, text=True, env=dict(env, **wrapper), timeout=120)
    outputs.append(r.stdout + r.stderr)
    check("metacomputer secrets passes the exit code on", r.returncode == 2)

    # the terminal closing locks it
    u2.close()
    st2 = u2.wait(15)
    check("closing the terminal locks the vault", not os.path.exists(sock), st2)
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("fill after the terminal closed: exit 3", r.returncode == 3)
    term = u2.text()
    check("the unlock terminal showed each request and outcome", "fill web-login (password) on VM testvm" in term
          and "-> ok: " + SITE in term and "-> denied" in term)
    check("the unlock terminal never showed a value", not any(v in term for v in VALUES + [USERNAME, code]))

    # --- unlock 3: the time runs out ------------------------------------------------------
    u3 = start_unlock(("--for", "2s"))
    time.sleep(3)
    check("the vault locks when its time is up", u3.wait(15) == 0 and not os.path.exists(sock) and "time is up" in u3.text())
    r = client("fill", "web-login", "--vm", "testvm", "--field", "password")
    check("fill after the timeout: exit 3", r.returncode == 3)

    # --- unlock 4: --confirm each asks for every use --------------------------------------
    u4 = start_unlock(("--confirm", "each"), {"MCOMP_VAULT_TEST_LISTENERS": my_name})
    proc = subprocess.Popen([BIN, "fill", "web-login", "--vm", "testvm", "--field", "password"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    u4.expect("Allow fill of web-login (password)")
    u4.send("y\n")
    out, err = proc.communicate(timeout=60)
    outputs.append(out + err)
    check("--confirm each asks even for a 'session' secret", proc.returncode == 0)
    proc = subprocess.Popen([BIN, "session", "load", "s1", "--vm", "testvm", "--profile", "prof2"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    u4.expect("Allow session load of s1 into profile prof2")
    u4.send("n\n")
    out, err = proc.communicate(timeout=60)
    outputs.append(out + err)
    check("--confirm each: a session load answered n is refused", proc.returncode == 2)
    u4.send("\x03")
    u4.wait(15)

    # --- the audit log and every output -------------------------------------------------
    alog = os.path.join(vdir, "audit.jsonl")
    text = open(alog).read()
    recs = [json.loads(l) for l in text.splitlines()]
    check("audit log is 0600", (os.stat(alog).st_mode & 0o777) == 0o600)
    check("audit log records fills with their outcome", any(x["op"] == "fill" and x["result"] == "ok" and x.get("chars") == len(PW_VALUE)
                                                            for x in recs)
          and any(x["op"] == "fill" and x["result"] == "denied" for x in recs)
          and any(x["op"] == "fill" and x["result"] == "locked" for x in recs)
          and any(x["op"] == "fill" and x.get("approval") == "each:no" for x in recs))
    check("audit log records unlock and lock", any(x["op"] == "unlock" for x in recs)
          and {x.get("reason") for x in recs if x["op"] == "lock"} >= {"Ctrl-C", "time is up", "the terminal closed"})
    check("audit log holds no values", not any(v in text for v in VALUES + [USERNAME]))
    r = client("audit", "--tail", "50")
    check("audit --tail shows records, no values", r.returncode == 0 and "fill" in r.stdout and not any(v in r.stdout for v in VALUES))
    leaked = [v for v in VALUES if any(v in o for o in outputs)]
    check(f"no value in the output of any of {len(outputs)} client commands", not leaked, leaked)
    check("no username or code in the output of fills", not any((USERNAME in o or code in o) for o in outputs
                                                                 if '"secrets"' not in o and "Secrets:" not in o))


if __name__ == "__main__":
    sys.exit(main())
