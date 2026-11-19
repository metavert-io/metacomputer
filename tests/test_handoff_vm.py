#!/usr/bin/env python3
"""`metacomputer handoff` against a running test VM, the way Claude uses it (skills/metacomputer/SKILL.md):

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_handoff_vm.py

Claude's side opens a handoff (no terminal, as an agent's shell runs it) and waits. While it's open, an acting
call through the hub (`metacomputer call`) is refused with "Paused: the user has the computer" and reaches nothing,
reads still work, `metacomputer sh` is refused (with or without --override; --read-only runs), `secrets fill`,
a fill naming another VM, `input-reset` (but `--check`) and `stop` are refused, `release` leaves it, and the
session that asked can't say "done" for the user. Then the
user's "done" (scripted at a pseudo-terminal, standing in for the user's own terminal) ends it: the waiting
command returns 0 with the user's note, and the same acting call goes through.

It starts the VM if it's off (and stops it again afterwards), uses the VM's own settings file only, and sends its
events to a temporary folder, so no event sink of yours hears it. A test VM only: it acts on the desktop (the
driver's agent-cursor setting).
"""
import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MC = os.path.join(LAB, "bin", "metacomputer")
FAILS = []
PASSES = [0]
ACT = ("set_agent_cursor_enabled", '{"session": "handoff-vm-test", "enabled": false}')


def check(name, cond, detail=""):
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:500]})" if detail and not cond else ""), flush=True)


def mc(env, *args, stdin=subprocess.DEVNULL, timeout=180):
    return subprocess.run([MC] + list(args), env=env, stdin=stdin, capture_output=True, text=True, timeout=timeout)


def main():
    if not os.environ.get("MCOMP_CONFIG"):
        print("set MCOMP_CONFIG to a test VM's settings file (this test acts on that VM's desktop)")
        return 2
    td = tempfile.mkdtemp(prefix="mchandoff-")
    env = dict(os.environ, MCOMP_EVENTS_DIR=os.path.join(td, "events"), MCOMP_EVENTS_SPOOL=os.path.join(td, "spool"))
    r = mc(env, "where")
    vm = next((line.split()[1] for line in r.stdout.splitlines() if line.startswith("vm ")), "")
    if vm in ("", "metacomputer"):
        print(f"refusing: MCOMP_CONFIG names the VM '{vm}'; use a test VM")
        return 2
    print(f"test VM: {vm}")
    started = False
    opener = None
    try:
        if mc(env, "status").returncode != 0:
            r = mc(env, "start", timeout=600)
            started = True
            check("the test VM starts", r.returncode == 0, r.stderr[-400:])
        mc(env, "release")
        r = mc(env, "call", *ACT)
        check("before the handoff, an acting call goes through", r.returncode == 0, (r.stdout[-300:], r.stderr[-300:]))

        log = open(os.path.join(td, "opener.out"), "w+")
        opener = subprocess.Popen([MC, "handoff", "--why", "captcha", "--say",
                                   "Tick the 'Verify you are human' box on example.com in Chrome", "--timeout", "5m",
                                   "--json"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        h = None
        for _ in range(100):
            st = json.loads(mc(env, "handoff", "status", "--json").stdout or "{}")
            if st.get("open"):
                h = st["handoff"]
                break
            time.sleep(0.2)
        check("Claude's handoff opens (no terminal) and waits", h and h["why"] == "captcha" and opener.poll() is None, h)

        r = mc(env, "call", *ACT)
        check("while it's open, an acting call through the hub is refused: the user has the computer",
              r.returncode != 0 and "Paused: the user has the computer" in (r.stdout + r.stderr),
              (r.returncode, r.stdout[-300:], r.stderr[-300:]))
        r = mc(env, "call", "list_windows", "{}")
        check("reads still work", r.returncode == 0, r.stderr[-300:])
        r = mc(env, "sh", "-c", "echo acting")
        r2 = mc(env, "sh", "--override", "-c", "echo acting")
        r3 = mc(env, "sh", "--read-only", "-c", "echo reading")
        check("sh is refused (125) with or without --override; --read-only runs",
              r.returncode == 125 and r2.returncode == 125 and "the user has the computer" in r.stderr + r.stdout
              and r3.returncode == 0 and "reading" in r3.stdout, (r.returncode, r2.returncode, r3.returncode, r.stderr[-200:]))
        r = mc(env, "secrets", "fill", "nothing-here", "--field", "password")
        check("secrets fill is refused before the vault is asked (exit 4)", r.returncode == 4
              and "the user has the computer" in r.stderr, (r.returncode, r.stderr[-300:]))
        r = mc(env, "secrets", "fill", "nothing-here", "--vm", vm + "-other", "--field", "password")
        check("a fill naming another VM is refused (its handoff isn't this one's)", r.returncode != 0
              and "acts on this VM" in r.stderr, (r.returncode, r.stderr[-300:]))
        r = mc(env, "input-reset")
        r2 = mc(env, "input-reset", "--check")
        check("input-reset is refused (exit 4); input-reset --check only reads and runs",
              r.returncode == 4 and "the user has the computer" in r.stderr and r2.returncode == 0,
              (r.returncode, r.stderr[-200:], r2.returncode, r2.stderr[-200:]))
        r = mc(env, "stop")
        check("stop is refused while the user has the computer", r.returncode != 0
              and "the user has the computer" in r.stderr, (r.returncode, r.stderr[-300:]))
        r = mc(env, "release")
        r2 = mc(env, "who")
        check("release leaves it; who shows it", "not released" in r.stdout and "the user has the computer" in r2.stdout,
              (r.stdout[-300:], r2.stdout[-300:]))
        r = mc(env, "handoff", "done")
        check("the session that asked can't say done for the user (exit 3)", r.returncode == 3
              and opener.poll() is None, (r.returncode, r.stderr[-300:]))

        # The user's "done", from a terminal: a pseudo-terminal stands in for the user's own.
        m, s = pty.openpty()
        r = subprocess.run([MC, "handoff", "done", "--note", "ticked it"], env=env, stdin=s, capture_output=True,
                           text=True, timeout=60)
        os.close(s)
        os.close(m)
        check("the user's done (at a terminal) is taken", r.returncode == 0 and "Handed the virtual computer back"
              in r.stdout, (r.returncode, r.stdout, r.stderr))
        rc = opener.wait(30)
        log.seek(0)
        out = log.read()
        res = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        check("the waiting handoff returns 0 with status done and the user's note", rc == 0
              and res.get("status") == "done" and res.get("note") == "ticked it" and res.get("ended_from") == "terminal",
              (rc, out[-500:]))
        check("its output gave the user's steps: view --open, the clipboard, handoff done",
              "metacomputer view --open" in out and "clipboard" in out and "metacomputer handoff done" in out, out[:600])
        r = mc(env, "call", *ACT)
        check("after done, the same acting call goes through", r.returncode == 0, (r.stdout[-300:], r.stderr[-300:]))
        evs = []
        try:
            evs = [json.loads(x) for x in open(os.path.join(env.get("MCOMP_LOG_DIR") or "", "events.jsonl"))]
        except (OSError, ValueError):
            pass
        if not evs:
            lg = mc(env, "events", "tail", "20", "--json")
            evs = [json.loads(x) for x in lg.stdout.splitlines() if x.startswith("{")]
        types = [e.get("type") for e in evs]
        check("events: needs_you.captcha, handoff_opened, handoff_done", "captcha" in types and "handoff_opened" in types
              and "handoff_done" in types, types[-10:])
        check("the note is in no event", "ticked it" not in json.dumps(evs))
    finally:
        if opener and opener.poll() is None:
            opener.terminate()
            subprocess.run([MC, "handoff", "cancel"], env=env, capture_output=True, timeout=60)
        mc(env, "release")
        if started:
            mc(env, "stop", timeout=300)
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{PASSES[0]}/{PASSES[0] + len(FAILS)} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
