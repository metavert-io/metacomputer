#!/usr/bin/env python3
"""Offline checks for the SSH arguments every connection to the VM uses
(scripts/lib.sh ssh_base: `metacomputer sh`, `ssh`, `mcp`, recordings, status...).

The VM is untrusted, so nothing of the Mac's may be forwarded to it whatever the
user's own ssh configuration says. No VM needed and nothing connects: the
arguments are built by lib.sh and resolved with `ssh -G`. Run:
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
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
