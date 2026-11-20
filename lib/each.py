#!/usr/bin/env python3
"""`metacomputer each VM,VM,... -- COMMAND [ARGS...]`: the same metacomputer command on several VMs at once.

Each VM runs `metacomputer --vm NAME COMMAND ARGS` (`main` is this checkout's own VM, with no --vm), at the same
time, with no terminal to answer on (so a command that asks first refuses, as it would for a script), and their
output is shown VM by VM when each finishes, escaped for the terminal. The exit status is the highest of theirs.
It's for work that fans out (the same check in several browsers or profiles, a page tested from several VMs, a
batch split between them); every VM keeps its own one-controller rule, guards, vault scope and network allowlist.
"""
import concurrent.futures
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from termsafe import escape_for_terminal  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(LAB, "bin", "metacomputer")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
MAX_VMS = 16
NOT_HERE = {"each", "setup", "build", "mcp", "mcp-lazy", "ssh", "view", "handoff"}   # interactive, or one at a time


def child_env():
    """The environment without a VM already picked (MCOMP_VM, MCOMP_CONFIG): each child names its own, and `main` is
    this checkout's config/local.env, whatever the caller had selected."""
    return {k: v for k, v in os.environ.items() if k not in ("MCOMP_VM", "MCOMP_CONFIG")}


def run_one(vm, cmd, timeout):
    args = [CLI] + ([] if vm == "main" else ["--vm", vm]) + cmd
    t0 = time.monotonic()
    try:
        r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                           errors="replace", env=child_env())
        code = r.returncode if r.returncode >= 0 else 128 - r.returncode   # killed by a signal: 128 + its number
        return vm, code, r.stdout, r.stderr, time.monotonic() - t0
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return vm, 124, out, f"metacomputer each: stopped after {timeout} s\n", time.monotonic() - t0


def main(argv):
    timeout = 3600
    if argv and argv[0].startswith("--timeout"):
        try:
            timeout = int(argv[0].split("=", 1)[1]) if "=" in argv[0] else int(argv[1])
        except (ValueError, IndexError):
            print("metacomputer each: --timeout SECONDS", file=sys.stderr)
            return 2
        argv = argv[1:] if "=" in argv[0] else argv[2:]
    if len(argv) < 3 or argv[1] != "--":
        print("usage: metacomputer each [--timeout S] VM,VM,... -- COMMAND [ARGS...]   (main = this checkout's VM)",
              file=sys.stderr)
        return 2
    vms = [v.strip() for v in argv[0].split(",") if v.strip()]
    cmd = argv[2:]
    if not vms or len(vms) > MAX_VMS or len(set(vms)) != len(vms) or not all(NAME_RE.match(v) for v in vms):
        print(f"metacomputer each: give 1 to {MAX_VMS} different VM names, separated by commas", file=sys.stderr)
        return 2
    if cmd[0] in NOT_HERE or cmd[0].startswith("-"):
        print(f"metacomputer each: `{escape_for_terminal(cmd[0])}` isn't for running on several VMs at once",
              file=sys.stderr)
        return 2
    rc = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as ex:
        futs = [ex.submit(run_one, v, cmd, timeout) for v in vms]
        for f in concurrent.futures.as_completed(futs):
            vm, code, out, err, secs = f.result()
            rc = max(rc, code)
            print(f"=== {vm}: exit {code} ({secs:.0f} s)")
            for text, stream in ((out, sys.stdout), (err, sys.stdout)):
                if text:
                    stream.write(escape_for_terminal(text) if sys.stdout.isatty() else text)
                    if not text.endswith("\n"):
                        stream.write("\n")
            sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
