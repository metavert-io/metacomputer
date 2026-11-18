#!/usr/bin/env python3
"""A refused `metacomputer build` changes nothing (scripts/build-vm.sh). No VM: a fake Lume
reports that the VM exists, in a temporary SSH folder laid out as after a finished build
(the host key's private half deleted, known_hosts pinning the VM's key).

    python3 tests/test_build_refusals.py

Each refusal (the VM exists without --replace; a bad EXTRA_PACKAGES or VIRTUAL_AUDIO,
with or without --replace) must leave the host key, every other key and known_hosts
exactly as they were, so SSH to the existing VM keeps working.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(LAB, "scripts", "build-vm.sh")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def snapshot(d):
    return {n: hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest() for n in sorted(os.listdir(d))}


def main():
    td = tempfile.mkdtemp(prefix="mcomp-build-")
    try:
        return run(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run(td):
    ssh = os.path.join(td, "ssh")
    os.makedirs(ssh, mode=0o700)
    for k in ("admin", "ops", "mcp", "fill", "proxy", "hostkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"test-{k}", "-f", os.path.join(ssh, k)], check=True)
    os.remove(os.path.join(ssh, "hostkey"))          # a finished build deletes the private half
    with open(os.path.join(ssh, "known_hosts"), "w") as f:
        f.write("[127.0.0.1]:2399 " + " ".join(open(os.path.join(ssh, "hostkey.pub")).read().split()[:2]) + "\n")
    before = snapshot(ssh)
    bindir = os.path.join(td, "bin")
    os.makedirs(bindir)
    calls = os.path.join(td, "lume-calls")
    with open(os.path.join(bindir, "lume"), "w") as f:
        f.write("#!/bin/sh\n"
                f"echo \"$*\" >> '{calls}'\n"
                "case \"$1\" in\n"
                "  ls) echo '[{\"name\": \"fake-build-vm\"}, {\"name\": \"fake-build-vm-baseline\"}]' ;;\n"
                "  get) echo '{\"status\": \"stopped\"}' ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n")
    os.chmod(os.path.join(bindir, "lume"), 0o755)

    def build(extra_cfg="", replace=False):
        cfg = os.path.join(td, "test.env")
        with open(cfg, "w") as f:
            f.write(f'VM_NAME=fake-build-vm\nBASELINE_NAME=fake-build-vm-baseline\nLUME="{bindir}/lume"\n'
                    f'SSH_DIR="{ssh}"\nSSH_PORT=2399\nRUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\n{extra_cfg}\n')
        env = dict(os.environ, MCOMP_CONFIG=cfg, MCOMP_EVENTS_DIR=os.path.join(td, "events"),
                   MCOMP_LOCK_DIR=os.path.join(td, "locks"),   # the build's VM lock: never the real per-user folder
                   PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        if os.path.exists(calls):
            os.remove(calls)
        return subprocess.run([BUILD] + (["--replace"] if replace else []), env=env, capture_output=True, text=True,
                              timeout=60)

    cases = [
        ("the VM exists, no --replace", "", False, "already exists"),
        ("EXTRA_PACKAGES with a name that isn't a package", 'EXTRA_PACKAGES="gimp ;rm"', False, "EXTRA_PACKAGES"),
        ("...also with --replace", 'EXTRA_PACKAGES="gimp ;rm"', True, "EXTRA_PACKAGES"),
        ("VIRTUAL_AUDIO other than on or off", "VIRTUAL_AUDIO=yes", True, "VIRTUAL_AUDIO"),
    ]
    for what, extra, replace, msg in cases:
        r = build(extra, replace)
        out = r.stdout + r.stderr
        lume_calls = open(calls).read() if os.path.exists(calls) else ""
        check(f"refused: {what}", r.returncode != 0 and msg in out, (r.returncode, out[-400:]))
        check("...the host key, the other keys and known_hosts are unchanged", snapshot(ssh) == before,
              (sorted(set(snapshot(ssh)) ^ set(before)), [n for n in before if snapshot(ssh).get(n) != before[n]]))
        check("...and nothing was stopped or deleted", "delete" not in lume_calls and "stop" not in lume_calls, lume_calls)

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
