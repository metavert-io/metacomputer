#!/usr/bin/env python3
"""vm_stop and vm_force_stop (scripts/lib.sh) under the build's strict shell settings. No VM.

    python3 tests/test_vm_stop.py

scripts/build-vm.sh runs with `set -Eeuo pipefail` and an ERR trap. vm_stop and
vm_force_stop end a background helper with `kill` and `wait`; when that helper has
already exited, or exits non-zero, those commands fail, and without `|| true` the
ERR trap stopped every VM build at its last step ("stopping and cloning the clean
baseline"). This runs the real functions from lib.sh, with stand-ins for the VM,
under /bin/bash (macOS's bash 3.2, which the build uses).
"""
import os
import re
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def function(src, name):
    m = re.search(r"^%s\(\) \{.*?^\}\n" % re.escape(name), src, re.S | re.M)
    assert m, name
    return m.group(0)


def run(case):
    src = open(os.path.join(LAB, "scripts", "lib.sh")).read()
    funcs = function(src, "vm_stop") + function(src, "vm_force_stop")
    td = tempfile.mkdtemp(prefix="mcomp-vmstop-")
    # Stand-ins: the VM is "running" for the first few checks, then stops.
    stub = f'''
set -Eeuo pipefail
trap 'echo ERR-TRAP-FIRED; exit 97' ERR
STATE={td}/state; echo 0 > "$STATE"
vm_running() {{ n=$(cat "$STATE"); echo $((n+1)) > "$STATE"; [ "$n" -lt {case["running_checks"]} ]; }}
gssh() {{ {case["gssh"]}; }}
warn() {{ :; }}
netgw_stop() {{ :; }}
LUME={td}/lume
printf '#!/bin/sh\\n{case["lume"]}\\n' > "$LUME"; chmod +x "$LUME"
MCOMP_STOP_DEADLINE=3
{funcs}
{case["call"]}
echo DONE
'''
    script = os.path.join(td, "t.sh")
    open(script, "w").write(stub)
    r = subprocess.run(["/bin/bash", script], capture_output=True, text=True, timeout=120)
    return r


CASES = [
    ("vm_stop: the guest's poweroff helper has already exited (kill fails)",
     dict(running_checks=2, gssh="return 0", lume="exit 0", call="vm_stop mcvm")),
    ("vm_stop: the poweroff helper fails (wait returns non-zero)",
     dict(running_checks=2, gssh="return 255", lume="exit 0", call="vm_stop mcvm")),
    ("vm_stop: the guest never powers off, so it's forced",
     dict(running_checks=1000, gssh="sleep 30", lume="exit 1", call="vm_stop mcvm")),
    ("vm_force_stop: Lume's stop fails",
     dict(running_checks=0, gssh="return 0", lume="exit 1", call="vm_force_stop mcvm")),
    ("vm_stop: the VM isn't running",
     dict(running_checks=0, gssh="return 0", lume="exit 0", call="vm_stop mcvm")),
]


def main():
    for name, case in CASES:
        r = run(case)
        ok = r.returncode == 0 and "DONE" in r.stdout and "ERR-TRAP-FIRED" not in r.stdout
        check(name + ": finishes without tripping set -E / the ERR trap", ok, (r.returncode, r.stdout[-200:], r.stderr[-200:]))
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
