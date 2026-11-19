#!/usr/bin/env python3
"""Offline checks for the SSH arguments every connection to the VM uses
(scripts/lib.sh ssh_base: `metacomputer sh`, `ssh`, `mcp`, recordings, status...).

The VM is untrusted, so nothing of the Mac's may be forwarded to it whatever the
user's own ssh configuration says. No VM needed and nothing connects: the
arguments are built by lib.sh and resolved with `ssh -G`. It also resolves the
VM's own sshd config with this Mac's `sshd -T`: every account's keys come from a
root-owned list outside its home (SSH_AUTH=cert: none for cua). Run:
    python3 tests/test_ssh_options.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []

REQUIRED = ["BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null",
            "UpdateHostKeys=no", "ForwardAgent=no", "ForwardX11=no", "ForwardX11Trusted=no", "ClearAllForwardings=yes",
            "Tunnel=no", "ControlMaster=no", "ControlPath=none", "PermitLocalCommand=no", "ProxyCommand=none"]
FORBIDDEN_FLAGS = {"-A", "-X", "-Y", "-L", "-R", "-D", "-W", "-M", "-S", "-J", "-w", "-f", "-N"}
# What `ssh -G` must resolve, and what it must not print at all.
RESOLVED = {"forwardagent": "no", "forwardx11": "no", "forwardx11trusted": "no", "clearallforwardings": "yes",
            "controlmaster": "false", "permitlocalcommand": "no", "tunnel": "false", "updatehostkeys": "false",
            "batchmode": "yes", "stricthostkeychecking": "true", "port": "59999", "hostname": "127.0.0.1"}
ABSENT = ("proxycommand", "proxyjump", "controlpath", "localforward", "remoteforward", "dynamicforward",
          "localcommand", "remotecommand", "sendenv", "setenv")
HOSTILE = """Host *
  ForwardAgent yes
  ForwardX11 yes
  ForwardX11Trusted yes
  RemoteForward 9999 localhost:22
  LocalForward 9998 localhost:22
  DynamicForward 9997
  ControlMaster auto
  ControlPath /tmp/mcomp-test-cm-%r@%h:%p
  PermitLocalCommand yes
  LocalCommand touch /tmp/mcomp-test-should-not-run
  ProxyCommand nc %h %p
  Tunnel yes
  UpdateHostKeys yes
  SendEnv *
"""


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""), flush=True)


def config(td, auth):
    p = os.path.join(td, f"{auth}.env")
    open(p, "w").write(f"""VM_NAME=mcomp-no-such-vm
BASELINE_NAME=mcomp-no-such-vm-baseline
SSH_DIR="{td}/ssh"
SSH_PORT=59999
VNC_PORT=59998
RUN_DIR="{td}/run"
LOG_DIR="{td}/logs"
SSH_AUTH={auth}
""")
    return p


def ssh_args(cfg, role):
    r = subprocess.run(["bash", "-c", f'. "{LAB}/scripts/lib.sh" && ssh_base {role} && printf "%s\\0" "${{SSH_ARGS[@]}}"'],
                       capture_output=True, text=True, env=dict(os.environ, MCOMP_CONFIG=cfg))
    return [a for a in r.stdout.split("\0") if a], r.stderr


def resolve(args):
    r = subprocess.run(["ssh", "-G", *args], capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        out.setdefault(k, v)
    return r.returncode, out, r.stderr


def check_args(label, args):
    opts = {args[i + 1] for i, a in enumerate(args[:-1]) if a == "-o"}
    missing = [o for o in REQUIRED if o not in opts]
    check(f"{label}: no ssh config file applies (-F /dev/null first)", args[:2] == ["-F", "/dev/null"], args[:4])
    check(f"{label}: forwarding, multiplexing, proxies and local commands are off", not missing, missing)
    check(f"{label}: no option that forwards or multiplexes", not FORBIDDEN_FLAGS & set(args), FORBIDDEN_FLAGS & set(args))
    check(f"{label}: the loopback forward, by port", args[-1].endswith("@127.0.0.1") and args[-3:-1] == ["-p", "59999"],
          args[-3:])
    rc, got, err = resolve(args)
    wrong = {k: got.get(k) for k, v in RESOLVED.items() if got.get(k) != v}
    present = [k for k in ABSENT if k in got]
    check(f"{label}: ssh accepts them and resolves them safely", rc == 0 and not wrong and not present,
          (rc, wrong, present, err[-200:]))
    return rc


GUEST_SSHD = os.path.join(LAB, "provisioning", "guest", "rootfs", "etc", "ssh", "sshd_config.d")
KEYS_DIR = "/etc/ssh/metacomputer-keys"


def sshd_effective(td, user, cert=False):
    """What the guest's sshd config (its sshd_config.d files, plus the SSH_AUTH=cert file metacomputer-admin
    writes) gives USER, resolved by this Mac's own sshd -T. Nothing listens."""
    d = os.path.join(td, "cert" if cert else "key", "d")
    if not os.path.isdir(d):
        os.makedirs(d)
        for f in sorted(os.listdir(GUEST_SSHD)):
            shutil.copy(os.path.join(GUEST_SSHD, f), d)
        if cert:   # the file `metacomputer-admin ssh-auth cert` writes, as it writes it
            admin = open(os.path.join(LAB, "provisioning", "guest", "rootfs", "usr", "local", "sbin",
                                      "metacomputer-admin")).read()
            body = admin.split("cat > \"$CERT_CONF\" <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
            open(os.path.join(d, "20-metacomputer-cert.conf"), "w").write(body + "\n")
        hk = os.path.join(td, "hostkey")
        if not os.path.exists(hk):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", hk], check=True)
        open(os.path.join(os.path.dirname(d), "sshd_config"), "w").write(f"Include {d}/*.conf\nHostKey {hk}\n")
    r = subprocess.run(["/usr/sbin/sshd", "-T", "-f", os.path.join(os.path.dirname(d), "sshd_config"),
                        "-C", f"user={user},host=localhost,addr=127.0.0.1"], capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        out.setdefault(k, v)
    return out, r.stderr


def guest_key_checks(td):
    """The VM's key lists: root-owned files outside every home, so a VM that wipes or edits ~/.ssh can neither
    add a key nor cut the Mac off; SSH_AUTH=cert still turns the desktop user's static keys off."""
    if not os.access("/usr/sbin/sshd", os.X_OK):
        check("guest sshd config (needs /usr/sbin/sshd on this Mac)", False, "no /usr/sbin/sshd")
        return
    for user in ("cua", "cuaadmin"):
        got, err = sshd_effective(td, user)
        akf = got.get("authorizedkeysfile", "")
        check(f"guest sshd: {user}'s keys come only from {KEYS_DIR}/, never the home folder",
              akf == f"{KEYS_DIR}/%u", (akf, err[-200:]))
    for user, lst in (("mcfill", "/etc/ssh/metacomputer-fill/authorized_keys"),
                      ("mcproxy", "/etc/ssh/metacomputer-proxy/authorized_keys")):
        got, err = sshd_effective(td, user)
        check(f"guest sshd: {user} keeps its own root-owned list", got.get("authorizedkeysfile") == lst,
              (got.get("authorizedkeysfile"), err[-200:]))
    got, err = sshd_effective(td, "cua", cert=True)
    check("guest sshd, SSH_AUTH=cert: cua has no static keys at all (certificates only)",
          got.get("authorizedkeysfile") == "none" and got.get("trustedusercakeys") == "/etc/ssh/metacomputer-ca.pub",
          (got.get("authorizedkeysfile"), got.get("trustedusercakeys"), err[-200:]))
    got, err = sshd_effective(td, "cuaadmin", cert=True)
    check("guest sshd, SSH_AUTH=cert: the maintenance account keeps its root-owned list",
          got.get("authorizedkeysfile") == f"{KEYS_DIR}/%u", (got.get("authorizedkeysfile"), err[-200:]))
    got, _ = sshd_effective(td, "cua")
    check("guest sshd: StrictModes stays on (it checks the lists' owners and modes)", got.get("strictmodes") == "yes",
          got.get("strictmodes"))
    # Who writes the lists: the build puts the keys in the payload with cloud-init's options, provision.sh installs
    # them root-owned (0644 in a 0755 folder) before the sshd config that names them, finalize.sh removes the
    # bootstrap copies in the homes once sshd is shown to read the lists.
    build = open(os.path.join(LAB, "scripts", "build-vm.sh")).read()
    prov = open(os.path.join(LAB, "provisioning", "guest", "provision.sh")).read()
    fin = open(os.path.join(LAB, "provisioning", "guest", "finalize.sh")).read()
    check("build: the key lists carry the options cloud-init's copies do",
          "printf 'restrict,pty %s\\n' \"$(head -n 1 \"$SSH_DIR/ops.pub\")\"" in build
          and "printf 'restrict,command=\"/opt/metacomputer/bin/driver-mcp\" %s\\n' \"$(head -n 1 \"$SSH_DIR/mcp.pub\")\"" in build
          and "printf 'restrict,pty %s\\n' \"$(head -n 1 \"$SSH_DIR/admin.pub\")\" > \"$SEED/payload/keys-admin\"" in build
          and "'restrict,pty ' + k('ops.pub')" in build and "'restrict,command=\"/opt/metacomputer/bin/driver-mcp\" ' + k('mcp.pub')" in build)
    i_dir = prov.find(f"install -d -o root -g root -m 0755 {KEYS_DIR}")
    i_lst = prov.find('install -o root -g root -m 0644 "$PAYLOAD/${pair#*:}" "/etc/ssh/metacomputer-keys/${pair%%:*}"')
    i_cfg = prov.find('cp -r --preserve=mode,timestamps "$PAYLOAD/rootfs/." /')
    check("provision: root-owned lists (0644 in a 0755 folder), installed before the sshd config naming them",
          0 <= i_dir < i_lst < i_cfg and '"$GUSER:keys-guest" "$AUSER:keys-admin"' in prov, (i_dir, i_lst, i_cfg))
    i_chk = fin.find("grep -x 'authorizedkeysfile /etc/ssh/metacomputer-keys/%u' >/dev/null")
    i_rm = fin.find('rm -f "$home/.ssh/authorized_keys"')
    check("finalize: the homes' bootstrap copies go only after sshd is shown to read the lists", 0 <= i_chk < i_rm,
          (i_chk, i_rm))


def main():
    td = tempfile.mkdtemp(prefix="mcomp-sshopt-")
    try:
        hostile = os.path.join(td, "hostile_config")
        open(hostile, "w").write(HOSTILE)
        for auth in ("key", "cert"):
            cfg = config(td, auth)
            for role in ("admin", "ops", "mcp"):
                args, err = ssh_args(cfg, role)
                if not args:
                    check(f"{role} ({auth}): arguments built", False, err[-300:])
                    continue
                label = f"{role} ({auth})"
                check_args(label, args)
                if auth == "cert" and role != "admin":
                    check(f"{label}: a short-lived certificate first",
                          any(a.startswith("CertificateFile=") for a in args), args)
                # The second layer: even with a hostile file read in place of /dev/null, the
                # command line keeps forwarding, multiplexing and proxies off. (A config's
                # SendEnv adds to the command line's and its LocalCommand is still listed,
                # though PermitLocalCommand=no stops it: that is why -F /dev/null comes first.)
                rc, got, e = resolve(["-F", hostile, *args[2:]])
                wrong = {k: got.get(k) for k, v in RESOLVED.items() if got.get(k) != v}
                present = [k for k in ABSENT if k in got and k not in ("localcommand", "sendenv")]
                check(f"{label}: with a hostile ssh config read instead, forwarding, multiplexing and proxies stay off",
                      rc == 0 and not wrong and not present, (rc, wrong, present, e[-200:]))

        # What `metacomputer sh` actually hands over: PY is replaced by a script that
        # records its arguments instead of running vm_shell.py, so nothing connects.
        rec = os.path.join(td, "argv")
        fake_py = os.path.join(td, "fake-python")
        open(fake_py, "w").write(f'#!/bin/bash\ncase "$1" in\n  */vm_shell.py) printf "%s\\0" "$@" > "{rec}" ;;\n'
                                 f'  *) exec "{sys.executable}" "$@" ;;\nesac\n')
        os.chmod(fake_py, 0o755)
        subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "sh", "-c", "true"], capture_output=True, text=True,
                       env=dict(os.environ, MCOMP_CONFIG=config(td, "key"), PY=fake_py), timeout=60)
        argv = [a for a in open(rec).read().split("\0") if a] if os.path.exists(rec) else []
        if "--transport" in argv:
            i = argv.index("--transport")
            transport = argv[i + 2:i + 2 + int(argv[i + 1])]
            check("metacomputer sh: its transport is ssh -T with the same safe arguments",
                  transport[:2] == ["ssh", "-T"] and check_args("metacomputer sh transport", transport[2:]) == 0,
                  transport[:6])
        else:
            check("metacomputer sh: its transport is ssh -T with the same safe arguments", False, argv[:5])
        guest_key_checks(os.path.join(td, "sshd"))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
