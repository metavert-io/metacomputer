#!/usr/bin/env python3
"""Checks for the optional hardening (SECURITY.md) against a running VM.

    VC_CONFIG=path/to/test-vm.env python3 tests/test_options.py [A] [B] [C]

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
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(LAB, "bin", "virtual-computer")
sys.path.insert(0, os.path.join(LAB, "lib"))
from mcp_client import MCPClient, MCPError  # noqa: E402

BASE = os.environ.get("VC_CONFIG") or os.path.join(LAB, "config", "local.env")
TMP = tempfile.mkdtemp(prefix="vc-opt-")
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""), flush=True)


def variant(name, *lines):
    p = os.path.join(TMP, name + ".env")
    with open(p, "w") as f:
        f.write(open(BASE).read().rstrip("\n") + "\n" + "\n".join(lines) + "\n")
    return p


def vc(cfg, *args, stdin=None, timeout=180):
    return subprocess.run([BIN, *args], input=stdin, capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, VC_CONFIG=cfg))


def settings(cfg):
    """SSH_DIR, SSH_PORT, GUEST_USER, NET_GATEWAY_IP, LOG_DIR, as scripts/lib.sh resolves them."""
    out = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh"; printf "%s\\n" "$SSH_DIR" "$SSH_PORT" "$GUEST_USER" '
                          '"$NET_GATEWAY_IP" "$LOG_DIR" "$KNOWN_HOSTS"'],
                         capture_output=True, text=True, env=dict(os.environ, VC_CONFIG=cfg)).stdout.split("\n")
    return dict(zip(["ssh_dir", "port", "user", "gw", "logs", "known"], out))


def client(cfg):
    old = os.environ.get("VC_CONFIG")
    os.environ["VC_CONFIG"] = cfg
    try:
        c = MCPClient(cmd=[BIN, "mcp"], stderr_path=os.path.join(TMP, "mcp-stderr.log"))
        c.start(timeout=60)
        return c
    finally:
        if old is None:
            os.environ.pop("VC_CONFIG", None)
        else:
            os.environ["VC_CONFIG"] = old


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
    subprocess.run(["ssh-keygen", "-q", "-s", ca or os.path.join(s["ssh_dir"], "ca"), "-I", f"vc:test:{name}",
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
    v = vc(cfg, "actions", "verify", rel)
    check("virtual-computer actions verify: OK", v.returncode == 0 and v.stdout.startswith("OK "), v.stdout + v.stderr)


# --- B ---------------------------------------------------------------------------
def test_b():
    print("B  SSH certificates (SSH_AUTH=cert)")
    cert_cfg = variant("cert", "SSH_AUTH=cert")
    s = settings(cert_cfg)
    try:
        r = vc(cert_cfg, "security", "apply")
        r2 = vc(cert_cfg, "security", "--json")
        st = json.loads(r2.stdout or "{}")
        check("security apply switches the VM to certificates", (st.get("ssh_auth") or {}).get("vm") == "cert", r.stdout + r.stderr + r2.stderr)
        r = vc(cert_cfg, "ssh", "ops", "echo cert-ok")
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
        r = vc(cert_cfg, "ssh", "admin", "sudo /usr/local/sbin/virtual-computer-admin logs 400")
        check("certificate key IDs reach the VM's auth log", "ID vc:ops:" in r.stdout and "ID vc:mcp:" in r.stdout,
              [line for line in r.stdout.splitlines() if "ID vc:" in line][-2:])
    finally:
        r = vc(BASE, "security", "apply")
        r2 = vc(BASE, "security", "--json")
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
    guest_runs = f"/home/{s['user']}/virtual-computer-runs"
    try:
        r = vc(ro, "security", "apply")
        st = json.loads(vc(ro, "security", "--json").stdout or "{}")
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
        vc(BASE, "ssh", "ops", f"pkill -x mousepad; rm -rf {guest_runs}/opt-test; true")

        # browser profile on a page served inside the VM
        vc(BASE, "ssh", "ops", "mkdir -p /tmp/vc-opt-site && echo '<title>allowed</title><a href=x>x</a>' > /tmp/vc-opt-site/index.html"
                               " && (nohup python3 -m http.server 8791 --bind 127.0.0.1 --directory /tmp/vc-opt-site >/dev/null 2>&1 &)")
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
        vc(BASE, "ssh", "ops", "pkill -f '[h]ttp.server 8791'; rm -rf /tmp/vc-opt-site; true")

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
        vc(BASE, "ssh", "ops", "for f in ~/.config/virtual-computer/manifests/*.yaml; do echo '# changed' >> \"$f\"; done")
        try:
            c = client(ro)
            c.close()
            check("a manifest changed in the VM is refused", False, "connected")
        except MCPError:
            err = open(os.path.join(TMP, "mcp-stderr.log")).read()
            check("a manifest changed in the VM is refused", "doesn't match its hash" in err, err[-200:])
        vc(BASE, "ssh", "ops", "rm -rf ~/.config/virtual-computer/manifests")
    finally:
        r = vc(BASE, "security", "apply")
        st = json.loads(vc(BASE, "security", "--json").stdout or "{}")
        check("back to standard", (st.get("driver") or {}).get("vm_policy") == "standard", r.stdout + r.stderr)
        try:
            c = client(BASE)
            n = len(c.tools())
            c.close()
            check("a standard driver session works again", n > 50, n)
        except MCPError as e:
            check("a standard driver session works again", False, e)


def main(argv):
    which = [a.upper() for a in argv] or ["A", "B", "C"]
    try:
        for k in which:
            {"A": test_a, "B": test_b, "C": test_c}[k]()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
