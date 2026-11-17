#!/usr/bin/env python3
"""Checks for the optional hardening (SECURITY.md) against a running VM.

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_options.py [A] [B] [C] [D] [F]

Use a test VM: this switches the VM's SSH and driver settings (through the
maintenance account) and puts them back to the defaults at the end. Variants
of the given config are written to a temporary folder.

A  ACTION_LOG=on: a real driver session is logged, signed and verifies.
B  SSH_AUTH=cert: certificates work; the static key, expired, wrong-role,
   other-CA and wrong-address certificates are refused; the VM forces the
   driver command for the mcp role whatever the certificate says; the key ID
   reaches the VM's auth log; switching back restores the static keys.
C  DRIVER_MODE=bounded: readonly refuses input; desktop launches listed apps
   (by name, mapped to their path) and refuses the terminal and page tools;
   browser opens only listed origins; an expired or tampered manifest and a
   standard request (the VM's floor) are refused; back to standard at the end.
D  The secrets fill account (mcfill; changes nothing): the fill key runs only
   secret-fill whatever is asked, with no terminal and no forwarding; the fill key
   doesn't work for the desktop user nor the ops key for mcfill; the account, its
   program and its key list are root-owned and its home is empty.
F  metacomputer sh: runs in the VM as the desktop user with the desktop's
   environment; exit codes, stdin/--file/--cwd/arguments, a detached GUI app
   the driver can read, background programs, --timeout, an interrupted run,
   the output cap; VM_SHELL=off and the bounded driver refuse; another
   session's claim refuses (--read-only and --override still run); the
   action log records runs (SHELL_LOG), redacted unless ACTION_LOG_TEXT=keep,
   and verifies.
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(LAB, "bin", "metacomputer")
sys.path.insert(0, os.path.join(LAB, "lib"))
from mcp_client import MCPClient, MCPError  # noqa: E402

BASE = os.environ.get("MCOMP_CONFIG") or os.path.join(LAB, "config", "local.env")
TMP = tempfile.mkdtemp(prefix="mcomp-opt-")
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""), flush=True)


def variant(name, *lines):
    p = os.path.join(TMP, name + ".env")
    with open(p, "w") as f:
        f.write(open(BASE).read().rstrip("\n") + "\n" + "\n".join(lines) + "\n")
    return p


def mcomp(cfg, *args, stdin=None, timeout=180):
    return subprocess.run([BIN, *args], input=stdin, capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, MCOMP_CONFIG=cfg))


def settings(cfg):
    """SSH_DIR, SSH_PORT, GUEST_USER, NET_GATEWAY_IP, LOG_DIR, as scripts/lib.sh resolves them."""
    out = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; printf "%s\\n" "$SSH_DIR" "$SSH_PORT" "$GUEST_USER" '
                          '"$NET_GATEWAY_IP" "$LOG_DIR" "$KNOWN_HOSTS"'],
                         capture_output=True, text=True, env=dict(os.environ, MCOMP_CONFIG=cfg)).stdout.split("\n")
    return dict(zip(["ssh_dir", "port", "user", "gw", "logs", "known"], out))


def client(cfg):
    old = os.environ.get("MCOMP_CONFIG")
    os.environ["MCOMP_CONFIG"] = cfg
    try:
        c = MCPClient(cmd=[BIN, "mcp"], stderr_path=os.path.join(TMP, "mcp-stderr.log"))
        c.start(timeout=60)
        return c
    finally:
        if old is None:
            os.environ.pop("MCOMP_CONFIG", None)
        else:
            os.environ["MCOMP_CONFIG"] = old


def raw_ssh(s, key, cert=None, cmd="true"):
    args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", f"IdentityFile={key}",
            "-o", f"UserKnownHostsFile={s['known']}", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
            "-o", "LogLevel=ERROR", "-p", s["port"]]
    if cert:
        args += ["-o", f"CertificateFile={cert}"]
    return subprocess.run(args + [f"{s['user']}@127.0.0.1", cmd], capture_output=True, text=True, timeout=60)


def mint(s, role, ca=None, validity="-2m:+5m", source=None, force=None, name="t"):
    key = os.path.join(TMP, f"{name}-{role}")
    for p in (key, key + ".pub", key + "-cert.pub"):
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], check=True)
    opts = ["-O", "clear", "-O", f"source-address={source or s['gw'] + '/32'}"]
    if force:
        opts += ["-O", f"force-command={force}"]
    subprocess.run(["ssh-keygen", "-q", "-s", ca or os.path.join(s["ssh_dir"], "ca"), "-I", f"mcomp:test:{name}",
                    "-n", role, "-V", validity, *opts, key + ".pub"], check=True)
    return key, key + "-cert.pub"


def text_of(r):
    return (r.text or "").strip()


# --- A ---------------------------------------------------------------------------
def test_a():
    print("A  signed action log (ACTION_LOG=on)")
    cfg = variant("alog", "ACTION_LOG=on")
    s = settings(cfg)
    before = set()
    for d, _, fs in os.walk(os.path.join(s["logs"], "actions")):
        before |= {os.path.join(d, f) for f in fs}
    c = client(cfg)
    c.call("list_windows", {})
    c.call("get_screen_size", {})
    c.call("type_text", {"text": "not-a-real-password", "pid": 1, "window_id": 1})   # fails; logged with the text redacted
    c.close()
    time.sleep(1)
    new = []
    for d, _, fs in os.walk(os.path.join(s["logs"], "actions")):
        new += [os.path.join(d, f) for f in fs if os.path.join(d, f) not in before]
    check("one new log file for the session", len(new) == 1, new)
    if not new:
        return
    recs = [json.loads(x) for x in open(new[0])]
    tools = [r.get("tool") for r in recs if r.get("type") == "call"]
    check("calls recorded in order", tools == ["list_windows", "get_screen_size", "type_text"], tools)
    start = recs[0].get("session") or {}
    check("start record names the connection", start.get("via") in ("tests", "direct", "call") and start.get("vm"), start)
    check("typed text is redacted", "not-a-real-password" not in open(new[0]).read())
    check("log file is private", oct(os.stat(new[0]).st_mode & 0o777) == "0o600")
    rel = os.path.relpath(new[0], os.path.join(s["logs"], "actions"))
    v = mcomp(cfg, "actions", "verify", rel)
    check("metacomputer actions verify: OK", v.returncode == 0 and v.stdout.startswith("OK "), v.stdout + v.stderr)


# --- B ---------------------------------------------------------------------------
def test_b():
    print("B  SSH certificates (SSH_AUTH=cert)")
    cert_cfg = variant("cert", "SSH_AUTH=cert")
    s = settings(cert_cfg)
    try:
        r = mcomp(cert_cfg, "security", "apply")
        r2 = mcomp(cert_cfg, "security", "--json")
        st = json.loads(r2.stdout or "{}")
        check("security apply switches the VM to certificates", (st.get("ssh_auth") or {}).get("vm") == "cert", r.stdout + r.stderr + r2.stderr)
        r = mcomp(cert_cfg, "ssh", "ops", "echo cert-ok")
        check("ops shell works with a certificate", r.stdout.strip() == "cert-ok", r.stderr)
        r = raw_ssh(s, os.path.join(s["ssh_dir"], "ops"), cmd="echo static")
        check("the static ops key is refused", r.returncode != 0 and "static" not in r.stdout, r.stdout)
        key, cert = mint(s, "ops", validity="-30m:-20m", name="expired")
        r = raw_ssh(s, key, cert, "echo x")
        check("an expired certificate is refused", r.returncode != 0 and "x" not in r.stdout)
        key, cert = mint(s, "admin", name="wrongrole")
        r = raw_ssh(s, key, cert, "echo x")
        check("a certificate for another role is refused", r.returncode != 0 and "x" not in r.stdout)
        other = os.path.join(TMP, "other-ca")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other], check=True)
        key, cert = mint(s, "ops", ca=other, name="otherca")
        r = raw_ssh(s, key, cert, "echo x")
        check("a certificate from another CA is refused", r.returncode != 0 and "x" not in r.stdout)
        key, cert = mint(s, "ops", source="10.9.9.9/32", name="wrongsrc")
        r = raw_ssh(s, key, cert, "echo x")
        check("a certificate for another source address is refused", r.returncode != 0 and "x" not in r.stdout)
        key, cert = mint(s, "mcp", name="noforce")   # no force-command in the certificate
        r = raw_ssh(s, key, cert, "id")
        check("the VM forces the driver command for mcp whatever the certificate says",
              "uid=" not in r.stdout and "unknown driver request" in r.stderr, (r.stdout + r.stderr)[:200])
        c = client(cert_cfg)
        n = len(c.tools())
        c.close()
        check("the driver connection works with a certificate", n > 50, n)
        r = mcomp(cert_cfg, "ssh", "admin", "sudo /usr/local/sbin/metacomputer-admin logs 400")
        check("certificate key IDs reach the VM's auth log", "ID mcomp:ops:" in r.stdout and "ID mcomp:mcp:" in r.stdout,
              [line for line in r.stdout.splitlines() if "ID mcomp:" in line][-2:])
    finally:
        r = mcomp(BASE, "security", "apply")
        r2 = mcomp(BASE, "security", "--json")
        st = json.loads(r2.stdout or "{}")
        check("back to static keys", (st.get("ssh_auth") or {}).get("vm") == "key", r.stdout + r.stderr)
        r = raw_ssh(s, os.path.join(s["ssh_dir"], "ops"), cmd="echo static")
        check("the static ops key works again", r.stdout.strip() == "static", r.stderr)


# --- C ---------------------------------------------------------------------------
def denied(r):
    t = text_of(r)
    return r.is_error and ("outside the capability manifest" in t or "Permission denied" in t)


def test_c():
    print("C  bounded driver (DRIVER_MODE=bounded)")
    ro = variant("ro", "DRIVER_MODE=bounded", "DRIVER_PROFILE=readonly")
    desk = variant("desk", "DRIVER_MODE=bounded", "DRIVER_PROFILE=desktop")
    s = settings(ro)
    guest_runs = f"/home/{s['user']}/metacomputer-runs"
    try:
        r = mcomp(ro, "security", "apply")
        st = json.loads(mcomp(ro, "security", "--json").stdout or "{}")
        check("security apply sets the VM's floor to bounded", (st.get("driver") or {}).get("vm_policy") == "bounded", r.stdout + r.stderr)

        c = client(ro)
        check("readonly: list_windows works", not c.call("list_windows", {}).is_error)
        check("readonly: a click is refused", denied(c.call("click", {"x": 10, "y": 10})))
        check("readonly: typing is refused", denied(c.call("type_text", {"text": "x"})))
        check("readonly: launching an app is refused", denied(c.call("launch_app", {"name": "mousepad"})))
        c.close()

        try:
            c = client(BASE)
            c.close()
            check("the VM refuses a standard driver session while bounded", False, "connected")
        except MCPError:
            err = open(os.path.join(TMP, "mcp-stderr.log")).read()
            check("the VM refuses a standard driver session while bounded", "accepts only bounded" in err, err[-200:])

        c = client(desk)
        c.call("start_session", {"session": "opt"})
        r = c.call("launch_app", {"name": "mousepad", "session": "opt"})
        pid = (r.structured or {}).get("pid")
        check("desktop: launch by name works (mapped to /usr/bin/mousepad)", not r.is_error and pid, text_of(r))
        time.sleep(2)
        wins = ((c.call("list_windows", {"pid": pid}).structured or {}).get("windows") or []) if pid else []
        if wins:
            w = wins[0]["window_id"]
            r = c.call("type_text", {"pid": pid, "window_id": w, "text": "bounded", "delivery_mode": "foreground"})
            check("desktop: typing into a listed app works", not r.is_error, text_of(r))
        else:
            check("desktop: typing into a listed app works", False, "no window")
        check("desktop: the terminal is refused", denied(c.call("launch_app", {"name": "xfce4-terminal", "launch_path": "/usr/bin/xfce4-terminal", "session": "opt"})))
        check("desktop: browser page tools are refused",
              denied(c.call("browser_prepare", {"session": "opt", "allow_launch": True, "profile": {"mode": "isolated_new"}})))
        r = c.call("start_recording", {"session": "opt", "output_dir": "/tmp/elsewhere", "record_video": False})
        check("desktop: writing outside the task folders is refused", denied(r), text_of(r))
        r = c.call("start_recording", {"session": "opt", "output_dir": f"{guest_runs}/opt-test", "record_video": False})
        check("desktop: recording into the task folder works", not r.is_error, text_of(r))
        c.call("stop_recording", {"session": "opt"})
        c.call("end_session", {"session": "opt"})
        c.close()
        mcomp(BASE, "ssh", "ops", f"pkill -x mousepad; rm -rf {guest_runs}/opt-test; true")

        # browser profile on a page served inside the VM
        mcomp(BASE, "ssh", "ops", "mkdir -p /tmp/mcomp-opt-site && echo '<title>allowed</title><a href=x>x</a>' > /tmp/mcomp-opt-site/index.html"
                               " && (nohup python3 -m http.server 8791 --bind 127.0.0.1 --directory /tmp/mcomp-opt-site >/dev/null 2>&1 &)")
        time.sleep(1)
        br = variant("br", "DRIVER_MODE=bounded", "DRIVER_PROFILE=browser", 'DRIVER_ORIGINS="http://127.0.0.1:8791"')
        c = client(br)
        c.call("start_session", {"session": "optb"})
        r = c.call("browser_prepare", {"session": "optb", "allow_launch": True, "profile": {"mode": "isolated_new"}})
        check("browser: a throwaway browser starts", not r.is_error, text_of(r))
        pid = (r.structured or {}).get("prepared_pid")
        tgt = {"session": "optb"}
        for _ in range(30):
            ws = (c.call("list_windows", {"pid": pid}).structured or {}).get("windows", []) if pid else []
            if ws:
                b = c.call("get_browser_state", {"pid": pid, "window_id": ws[0]["window_id"], "session": "optb"})
                st = b.structured or {}
                if st.get("target_id"):
                    tgt.update(target_id=st["target_id"], tab_id=(st.get("tabs") or [{}])[0].get("tab_id"))
                break
            time.sleep(0.3)
        r = c.call("browser_navigate", {**tgt, "url": "http://127.0.0.1:8791/"})
        check("browser: a listed origin opens", not r.is_error, text_of(r))
        r = c.call("browser_navigate", {**tgt, "url": "https://example.org/"})
        check("browser: another origin is refused", r.is_error and "outside the capability manifest" in text_of(r), text_of(r))
        check("browser: desktop input is refused", denied(c.call("click", {"x": 10, "y": 10})))
        c.call("end_session", {"session": "optb"})
        c.close()
        mcomp(BASE, "ssh", "ops", "pkill -f '[h]ttp.server 8791'; rm -rf /tmp/mcomp-opt-site; true")

        # expiry
        man = os.path.join(TMP, "short.yaml")
        open(man, "w").write("version: 3\nexpires_after: 4s\nidle_timeout: 4s\nallow:\n  tools: [list_windows]\n"
                             "resources:\n  desktop:\n    display: true\n")
        ex = variant("ex", "DRIVER_MODE=bounded", f'DRIVER_MANIFEST="{man}"')
        c = client(ex)
        check("expiry: works at first", not c.call("list_windows", {}).is_error)
        time.sleep(6)
        r = c.call("list_windows", {})
        check("expiry: refused after expires_after", r.is_error and "expired" in text_of(r), text_of(r))
        c.close()

        # a manifest changed inside the VM is refused
        mcomp(BASE, "ssh", "ops", "for f in ~/.config/metacomputer/manifests/*.yaml; do echo '# changed' >> \"$f\"; done")
        try:
            c = client(ro)
            c.close()
            check("a manifest changed in the VM is refused", False, "connected")
        except MCPError:
            err = open(os.path.join(TMP, "mcp-stderr.log")).read()
            check("a manifest changed in the VM is refused", "doesn't match its hash" in err, err[-200:])
        mcomp(BASE, "ssh", "ops", "rm -rf ~/.config/metacomputer/manifests")
    finally:
        r = mcomp(BASE, "security", "apply")
        st = json.loads(mcomp(BASE, "security", "--json").stdout or "{}")
        check("back to standard", (st.get("driver") or {}).get("vm_policy") == "standard", r.stdout + r.stderr)
        try:
            c = client(BASE)
            n = len(c.tools())
            c.close()
            check("a standard driver session works again", n > 50, n)
        except MCPError as e:
            check("a standard driver session works again", False, e)


# --- F ---------------------------------------------------------------------------
def run_dir(cfg):
    return subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; printf "%s" "$RUN_DIR"'], capture_output=True,
                          text=True, env=dict(os.environ, MCOMP_CONFIG=cfg)).stdout


def sh(cfg, *args, stdin=None, timeout=180):
    r = mcomp(cfg, "sh", *args, stdin=stdin, timeout=timeout)
    try:
        j = json.loads(r.stdout) if "--json" in args else {}
    except ValueError:
        j = {}
    return r, j


def action_logs(logs):
    out = set()
    for d, _, fs in os.walk(os.path.join(logs, "actions")):
        out |= {os.path.join(d, f) for f in fs if f.endswith(".jsonl")}
    return out


def test_f():
    print("F  shell channel (metacomputer sh)")
    s = settings(BASE)
    home = f"/home/{s['user']}"
    marker = "/tmp/mcomp-optf-marker"
    other = None
    claim = None
    try:
        r, _ = sh(BASE, "-c", "uname -s; id -un; . /etc/os-release; echo $ID; test -e /Users && echo mac || echo no-mac; "
                              "sudo -n true 2>/dev/null && echo sudo || echo no-sudo")
        check("runs in the VM as the desktop user, without sudo", r.stdout.split() == ["Linux", s["user"], "ubuntu", "no-mac", "no-sudo"],
              r.stdout + r.stderr)
        r, _ = sh(BASE, "-c", "findmnt -rn -t virtiofs,9p,nfs,nfs4,cifs,fuse.sshfs | wc -l; "
                              f"timeout 3 bash -c '</dev/tcp/{s['gw']}/22' 2>/dev/null && echo open || echo closed")
        check("nothing of the Mac's is mounted, and the Mac's side of the network refuses it", r.stdout.split() == ["0", "closed"],
              r.stdout + r.stderr)
        r, _ = sh(BASE, "-c", "pwd; echo ~")
        check("~ and the default folder are the VM's home", r.stdout.split() == [home, home], r.stdout)
        r, _ = sh(BASE, "-c", "echo out; echo err >&2; exit 7")
        check("exit status is the script's; stdout and stderr kept apart", r.returncode == 7 and r.stdout == "out\n" and "err" in r.stderr,
              (r.returncode, r.stdout, r.stderr))
        r, _ = sh(BASE, "-", "--cwd", "/tmp", "--", "a", "b c", stdin='echo "$(pwd) $1|$2"\n')
        check("script on stdin, --cwd, arguments after --", r.stdout == "/tmp a|b c\n", r.stdout + r.stderr)
        f = os.path.join(TMP, "check-f.sh")
        open(f, "w").write('echo "$(basename "$0") ran"\n')
        r, _ = sh(BASE, "--file", f)
        check("--file sends a script from the Mac (run from a fixed path)", r.stdout == "script ran\n", r.stdout + r.stderr)
        r, _ = sh(BASE, "-c", 'echo "$DISPLAY"; echo "$GTK_MODULES"; xset q >/dev/null 2>&1 && echo x-ok; '
                              'dbus-send --session --print-reply --dest=org.freedesktop.DBus / org.freedesktop.DBus.GetId >/dev/null && echo bus-ok')
        out = r.stdout.split("\n")
        check("the desktop session's environment: display, session bus, accessibility settings",
              out[0].startswith(":") and "atk-bridge" in out[1] and "x-ok" in out and "bus-ok" in out, r.stdout + r.stderr)
        r, _ = sh(BASE, "--read-only", "-c", 'echo "${DISPLAY:-none} ${DBUS_SESSION_BUS_ADDRESS:-none}"')
        check("--read-only runs without it", r.stdout == "none none\n", r.stdout)

        # A GUI app started detached outlives the run, and the driver can read it.
        r, _ = sh(BASE, "-c", "pkill -x mousepad; setsid -f mousepad </dev/null >/tmp/mcomp-optf-mousepad.log 2>&1; echo started")
        check("setsid -f starts a GUI app and returns", r.returncode == 0 and r.stdout == "started\n", r.stderr)
        c = client(BASE)
        wins = []
        for _ in range(40):
            wins = (c.call("list_windows", {"app": "mousepad"}).structured or {}).get("windows") or []
            if wins:
                break
            time.sleep(0.5)
        state = c.call("get_window_state", {"pid": wins[0]["pid"], "window_id": wins[0]["window_id"],
                                            "include_screenshot": False}) if wins else None
        c.close()
        check("... it's still running, on the desktop, with an accessibility tree", wins and state and not state.is_error
              and len(state.text) > 200, (wins, state.text[:200] if state else None))
        sh(BASE, "-c", "pkill -x mousepad; rm -f /tmp/mcomp-optf-mousepad.log; true")

        t = time.time()
        r, _ = sh(BASE, "-c", "sleep 61.5 & echo started")
        el = time.time() - t
        r2, _ = sh(BASE, "-c", "pgrep -f 'sleep 61.5' >/dev/null && echo alive; pkill -f 'sleep 61.5'; true")
        check("a program left running with & doesn't hold the result back, and keeps running", r.stdout == "started\n"
              and el < 8 and r2.stdout == "alive\n", (el, r.stdout, r2.stdout))
        t = time.time()
        r, j = sh(BASE, "--json", "--timeout", "2", "-c", "sleep 62.5 & sleep 63.5; echo never")
        el = time.time() - t
        r2, _ = sh(BASE, "-c", "pgrep -f 'sleep 6[23].5' || echo none")
        check("--timeout: exit 124 and the script's group is ended", r.returncode == 124 and j.get("status") == "timeout"
              and "never" not in j.get("stdout", "") and el < 12 and r2.stdout == "none\n", (r.returncode, el, r2.stdout))
        p = subprocess.Popen([BIN, "sh", "-c", "sleep 64.5; echo never"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=dict(os.environ, MCOMP_CONFIG=BASE))
        time.sleep(2.5)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        time.sleep(1)
        r2, _ = sh(BASE, "-c", "pgrep -f 'sleep 64.5' || echo none")
        check("interrupted on the Mac: exit 130, and the VM stops the script", p.returncode == 130 and r2.stdout == "none\n",
              (p.returncode, r2.stdout))
        r, j = sh(BASE, "--json", "-c", "head -c 200000 /dev/zero | tr '\\0' a; printf '\\nEND'")
        whole = b"a" * 200000 + b"\nEND"
        check("output over the cap: start and end kept, size and hash of the whole stream",
              j.get("stdout_truncated") and j.get("stdout_bytes") == len(whole)
              and j.get("stdout_sha256") == hashlib.sha256(whole).hexdigest() and j.get("stdout", "").endswith("\nEND")
              and len(j.get("stdout", "")) < 70000, {k: j.get(k) for k in ("stdout_bytes", "stdout_truncated")})

        # settings
        sh(BASE, "-c", f"rm -f {marker}")
        r, _ = sh(variant("shoff", "VM_SHELL=off"), "-c", f"touch {marker}")
        r2, _ = sh(variant("bnd", "DRIVER_MODE=bounded"), "-c", f"touch {marker}")
        r3, _ = sh(BASE, "-c", f"test -e {marker} && echo ran || echo nothing-ran")
        check("VM_SHELL=off and the bounded driver (VM_SHELL=auto) refuse (125); nothing runs",
              r.returncode == 125 and r2.returncode == 125 and r3.stdout == "nothing-ran\n", (r.returncode, r2.returncode, r3.stdout))
        r, _ = sh(variant("bndon", "DRIVER_MODE=bounded", "VM_SHELL=on"), "-c", "echo ok")
        check("the bounded driver with VM_SHELL=on runs", r.returncode == 0 and r.stdout == "ok\n", r.stderr)
        st = json.loads(mcomp(variant("bnd", "DRIVER_MODE=bounded"), "security", "--json").stdout or "{}").get("shell") or {}
        check("metacomputer security shows the shell setting", st.get("effective") == "off" and "bounded" in st.get("why", ""), st)

        # one controller at a time
        owners_dir = os.path.join(run_dir(BASE), "owners")
        os.makedirs(owners_dir, exist_ok=True)
        other = subprocess.Popen(["sleep", "300"])
        claim = os.path.join(owners_dir, f"{other.pid}.json")
        now = time.time()
        json.dump({"bridge_pid": other.pid, "claude_pid": None, "cwd": "/another/project", "started": now,
                   "last_call": now, "last_tool": "click"}, open(claim, "w"))
        r, _ = sh(BASE, "-c", f"touch {marker}")
        r3, _ = sh(BASE, "--read-only", "-c", f"test -e {marker} && echo ran || echo nothing-ran")
        check("another session acting: refused (125); --read-only still runs", r.returncode == 125 and "/another/project" in r.stderr
              and r3.returncode == 0 and r3.stdout == "nothing-ran\n", (r.returncode, r3.stdout, r3.stderr[-200:]))
        t = time.time()
        r, _ = sh(BASE, "--override", "-c", "echo took-over")
        who = json.loads(mcomp(BASE, "who", "--json").stdout or "{}")
        mine = [x for x in who.get("sessions", []) if x.get("bridge_pid") != other.pid and x.get("last_tool") == "vm_shell"
                and x.get("last_call", 0) >= t - 1]
        check("--override runs, and the run claims the computer (metacomputer who)", r.stdout == "took-over\n" and mine,
              (r.stdout, who.get("sessions")))

        # the action log
        before = action_logs(s["logs"])
        sh(variant("alogoff", "ACTION_LOG=off"), "-c", "echo not-logged")   # the test VM's own config may log
        sh(variant("slogoff", "ACTION_LOG=on", "SHELL_LOG=off"), "-c", "echo not-logged")
        check("no log file with ACTION_LOG=off, nor with SHELL_LOG=off", action_logs(s["logs"]) == before)
        alog = variant("slog", "ACTION_LOG=on")
        body = "echo optf-secret-out; echo optf-secret-err >&2"
        r, j = sh(alog, "--json", "--override", "-c", body, "--", "optf-secret-arg")
        new = sorted(action_logs(s["logs"]) - before)
        check("ACTION_LOG=on (SHELL_LOG=auto): one log file for the run", len(new) == 1 and j.get("logged"), new)
        if new:
            raw = open(new[0]).read()
            recs = [json.loads(x) for x in raw.splitlines()]
            call = next((x for x in recs if x["type"] == "call"), {})
            res = next((x for x in recs if x["type"] == "result"), {}).get("result") or {}
            check("it records a vm_shell call: script hash and size, cwd", call.get("tool") == "vm_shell"
                  and (call.get("args") or {}).get("script_sha256") == hashlib.sha256(body.encode()).hexdigest()
                  and (call.get("args") or {}).get("cwd") == home, call.get("args"))
            check("... and its result: exit code, output sizes and hashes", res.get("exit_code") == 0
                  and (res.get("stdout") or {}).get("sha256") == hashlib.sha256(b"optf-secret-out\n").hexdigest()
                  and (res.get("stderr") or {}).get("bytes") == len("optf-secret-err\n"), res)
            check("script, arguments and output text are redacted", "optf-secret" not in raw)
            v = mcomp(alog, "actions", "verify", os.path.relpath(new[0], os.path.join(s["logs"], "actions")))
            check("metacomputer actions verify: OK", v.returncode == 0 and v.stdout.startswith("OK "), v.stdout + v.stderr)
        before = action_logs(s["logs"])
        sh(variant("slogkeep", "ACTION_LOG=on", "ACTION_LOG_TEXT=keep"), "--override", "-c", body, "--", "optf-secret-arg")
        new = sorted(action_logs(s["logs"]) - before)
        raw = open(new[0]).read() if new else ""
        check("ACTION_LOG_TEXT=keep keeps the script, arguments and output", all(
            m in raw for m in ("echo optf-secret-out;", "optf-secret-arg", "optf-secret-out\\n", "optf-secret-err\\n")))
    finally:
        if other:
            other.kill()
            other.wait()
        if claim and os.path.exists(claim):
            os.remove(claim)
        sh(BASE, "--override", "-c", f"pkill -x mousepad; pkill -f 'sleep 6[1-4].5'; rm -f {marker} /tmp/mcomp-optf-mousepad.log; true")


# --- D ---------------------------------------------------------------------------
def as_user(s, key, user, stdin=b"", cmd="id", extra=()):
    args = ["ssh", "-T", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", f"IdentityFile={key}",
            "-o", f"UserKnownHostsFile={s['known']}", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
            "-o", "LogLevel=ERROR", "-p", s["port"], *extra, f"{user}@127.0.0.1", cmd]
    return subprocess.run(args, input=stdin, capture_output=True, timeout=60)


def last_json(r):
    try:
        return json.loads(r.stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def test_d():
    print("D  secrets fill account (mcfill)")
    s = settings(BASE)
    fill, ops = os.path.join(s["ssh_dir"], "fill"), os.path.join(s["ssh_dir"], "ops")
    if not os.path.exists(fill):
        check("the fill key exists (a VM built with secrets support)", False, fill)
        return
    r = as_user(s, fill, "mcfill", stdin=b"not json\n", cmd="id; cat /etc/shadow")
    check("the fill key runs only secret-fill, whatever command is asked for",
          r.returncode == 0 and last_json(r).get("reason") == "bad request" and b"uid=" not in r.stdout, r.stdout + r.stderr)
    req = json.dumps({"mode": "check", "field": "password", "origins": ["https://example.com"]}).encode() + b"\n"
    res = last_json(as_user(s, fill, "mcfill", stdin=req))
    check("a check answers in JSON (no Chrome, or no tab on the site)", res.get("ok") is False
          and any(t in res.get("reason", "") for t in ("no Chrome", "no open tab")), res)
    r = as_user(s, fill, "mcfill", stdin=b"x\n", extra=("-tt",))
    check("no terminal for the fill account", b"PTY allocation request failed" in r.stderr, r.stderr)
    r = as_user(s, fill, "mcfill", extra=("-W", "127.0.0.1:22"))
    check("no forwarding for the fill account", r.returncode != 0, r.stderr)
    check("the ops key doesn't sign in as mcfill", as_user(s, ops, "mcfill").returncode == 255)
    check("the fill key doesn't sign in as the desktop user", as_user(s, fill, s["user"]).returncode == 255)
    r = raw_ssh(s, ops, cmd="getent passwd mcfill | cut -d: -f6,7; "
                "stat -c '%U %a %n' /opt/metacomputer/bin/secret-fill /etc/ssh/metacomputer-fill/authorized_keys "
                "/var/lib/metacomputer/fill-home /etc/metacomputer/fill.conf; ls -A /var/lib/metacomputer/fill-home | wc -l; "
                "test -w /opt/metacomputer/bin/secret-fill && echo WRITABLE; true")
    out = r.stdout
    check("mcfill: root-owned empty home, dash, no password shell", "/var/lib/metacomputer/fill-home:/bin/sh" in out, out)
    check("secret-fill, the key list and fill.conf are root-owned and not writable by the desktop user",
          "root 755 /opt/metacomputer/bin/secret-fill" in out and "root 644 /etc/ssh/metacomputer-fill/authorized_keys" in out
          and "root 755 /var/lib/metacomputer/fill-home" in out and "root 644 /etc/metacomputer/fill.conf" in out
          and "WRITABLE" not in out and out.strip().splitlines()[-1].strip() == "0", out)


def main(argv):
    which = [a.upper() for a in argv] or ["A", "B", "C", "D", "F"]
    try:
        for k in which:
            {"A": test_a, "B": test_b, "C": test_c, "D": test_d, "E": test_e, "F": test_f}[k]()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if results and all(results) else 1


# --- E ---------------------------------------------------------------------------
class Receiver:
    """A local webhook receiver for test E (the events go nowhere else)."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.events, rx = [], self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    rx.events.append(json.loads(body)["data"])
                except (ValueError, KeyError):
                    pass
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def wait(self, family, type_, timeout=10, since=0):
        end = time.time() + timeout
        while time.time() < end:
            hit = [e for e in self.events[since:] if e.get("family") == family and e.get("type") == type_]
            if hit:
                return hit[0]
            time.sleep(0.25)
        return None


def test_e():
    """E: the event stream (docs/events.md), run by name (python3 tests/test_options.py E); not in the default
    set. With a local webhook receiver in a temporary events folder: `record stop` sends task.recording_stopped,
    `notify` sends needs_you, a blocked connection from the VM reaches security within one watcher cycle, and
    `web open-untrusted` on a sign-in page sends security.untrusted_link with origins only. Changes nothing in
    the VM."""
    print("E  event stream (a local receiver in a temporary events folder; the VM's settings don't change)")
    env = {"MCOMP_EVENTS_DIR": os.path.join(TMP, "events"), "MCOMP_EVENTS_SPOOL": os.path.join(TMP, "events-spool")}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    rx = Receiver()
    s = settings(BASE)
    started_watcher = False
    rec_files = []
    try:
        import pty
        m, s_ = pty.openpty()              # adding a sink needs a terminal
        try:
            r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "events.py"), "sink", "add", "etest", "--kind",
                                "webhook", "--url", f"http://127.0.0.1:{rx.port}/e", "--yes"], capture_output=True,
                               text=True, stdin=s_)
        finally:
            os.close(s_)
            os.close(m)
        check("a webhook sink in a temporary events folder", r.returncode == 0, r.stdout + r.stderr)
        h = json.loads(mcomp(BASE, "_health").stdout or "{}")
        check("_health (what the watcher probes): running, SSH, desktop ready, no MCP",
              h.get("vm") == "running" and h.get("ssh") and (h.get("guest") or {}).get("ready"), h)

        # record stop -> task.recording_stopped
        name = "events-test-" + time.strftime("%H%M%S")
        r = mcomp(BASE, "record", "start", name)
        time.sleep(4)
        r2 = mcomp(BASE, "record", "stop")
        ev = rx.wait("task", "recording_stopped")
        d = (ev or {}).get("data") or {}
        if d.get("path"):
            rec_files = [d["path"], d["path"][:-4] + ".json"]
        check("record stop -> task.recording_stopped with name, duration, path, description, key moments, audio",
              r.returncode == 0 and r2.returncode == 0 and d.get("name") == name and (d.get("duration_s") or 0) > 2
              and str(d.get("path", "")).endswith(".mp4") and str(d.get("description_file", "")).endswith(".json")
              and isinstance(d.get("key_moments"), int) and isinstance(d.get("audio"), bool), (r.stderr, r2.stderr, ev))

        # notify -> needs_you
        r = mcomp(BASE, "notify", "Test E: a sign-in would be needed here", "--why", "login",
                  "--action", "Nothing to do: this is test E")
        ev = rx.wait("needs_you", "login")
        check("notify -> needs_you.login with the action and the VM's name", r.returncode == 0 and ev
              and ev.get("action") == "Nothing to do: this is test E" and ev.get("vm"), (r.stdout, r.stderr, ev))

        # a blocked connection from the VM -> security event within one watcher cycle
        if mcomp(BASE, "events", "watcher", "status").stdout.startswith("event watcher running"):
            print("  (a watcher already runs for this VM: driving one cycle with 'events watch --once' instead)")
            mcomp(BASE, "events", "watch", "--once")          # sets where reading starts
            since = len(rx.events)
            mcomp(BASE, "ssh", "ops", "curl -s -m 3 -o /dev/null http://10.255.255.254:9/; true")
            time.sleep(1)
            mcomp(BASE, "events", "watch", "--once")
            ev = rx.wait("security", "blocked_connections", timeout=5, since=since)
        else:
            r = mcomp(BASE, "events", "watcher", "start")
            started_watcher = True
            time.sleep(3)                                      # its first cycle notes where the log ends
            since = len(rx.events)
            mcomp(BASE, "ssh", "ops", "curl -s -m 3 -o /dev/null http://10.255.255.254:9/; true")
            ev = rx.wait("security", "blocked_connections", timeout=45, since=since)
        dests = ((ev or {}).get("data") or {}).get("destinations") or []
        check("a blocked connection from the VM -> security.blocked_connections within one cycle (30 s)",
              any(x.get("host") == "10.255.255.254" and x.get("port") == 9 for x in dests), ev)

        # open-untrusted on a local page with a password field -> security event
        mcomp(BASE, "put", os.path.join(LAB, "tests", "fixtures", "site"), "--to", "/tmp/mcomp-events-site")
        mcomp(BASE, "ssh", "ops", "cd /tmp/mcomp-events-site/site && (nohup python3 -m http.server 8793 --bind 127.0.0.1 "
                                  ">/dev/null 2>&1 &)")
        time.sleep(1)
        r = mcomp(BASE, "web", "open-untrusted", "http://127.0.0.1:8793/signin.html?ref=abc", "--watch", "4", timeout=240)
        ev = rx.wait("security", "untrusted_link", timeout=10)
        d = (ev or {}).get("data") or {}
        check("open-untrusted on a page with a password field -> security.untrusted_link, origins only",
              r.returncode == 0 and d.get("password_fields", 0) >= 1 and d.get("start_origin") == "http://127.0.0.1:8793"
              and "ref=abc" not in json.dumps(ev), (r.stdout[-300:], r.stderr[-300:], ev))
        vp = os.path.join(s["ssh_dir"], "vnc-pass")
        vnc = open(vp).read().strip() if os.path.exists(vp) else ""
        blob = json.dumps(rx.events)
        check("nothing secret in what the receiver got: no URL queries, no live-view password",
              "ref=abc" not in blob and not (vnc and vnc in blob))
    finally:
        if started_watcher:
            mcomp(BASE, "events", "watcher", "stop")
        mcomp(BASE, "ssh", "ops", "pkill -f '[h]ttp.server 8793'; rm -rf /tmp/mcomp-events-site; true")
        for f in rec_files:
            if os.path.exists(f):
                os.remove(f)
        rx.srv.shutdown()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
