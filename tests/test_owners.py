#!/usr/bin/env python3
"""Claims on the virtual computer made outside Claude Code (lib/owners.py). No VM.

    python3 tests/test_owners.py

`metacomputer sh` run outside Claude Code holds its claim in the name of the shell
that ran it, which is often short-lived (bash -c, a script, another agent's tool
runner). It checks that such a claim:
- outlives that shell for the activity window (here 3 s instead of ten minutes);
- counts as the same controller for later runs from the same terminal session,
  and as another, active controller for a different session;
- is gone once the window has passed;
while a Claude Code session's claim and a bridge's claim still go with their process.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# Each step runs in a fresh Python with these settings: owners reads them at import. claude_of is
# replaced so the test behaves as outside Claude Code even when Claude Code runs it.
PRELUDE = ("import json, os, sys; sys.path.insert(0, os.path.join(%r, 'lib')); import owners; "
           "owners.claude_of = lambda *a: None; ") % LAB


def py(code, env, new_session=False):
    r = subprocess.run([sys.executable, "-c", PRELUDE + code], env=env, capture_output=True, text=True,
                       start_new_session=new_session, timeout=60)
    return r


def main():
    td = tempfile.mkdtemp(prefix="mcomp-owners-")
    # MCOMP_SSH_PORT=1: no real connection to any VM (a running hub, a test, `web`) counts as a controller here.
    env = dict(os.environ, MCOMP_RUN_DIR=td, MCOMP_ACTIVE_S="3", MCOMP_SSH_PORT="1")
    odir = os.path.join(td, "owners")

    # A run of metacomputer sh from a short-lived shell (bash -c), as another agent's tool runner does.
    r = subprocess.run(["/bin/bash", "-c", f"{sys.executable} -c \"{PRELUDE}print(json.dumps(owners.claim_for_shell('vm_shell')))\"; exit 0"],
                       env=env, capture_output=True, text=True, timeout=60)
    who = json.loads(r.stdout or "[0, 0]")
    holder = who[0]
    time.sleep(0.2)
    files = [f for f in os.listdir(odir) if f.endswith(".json")] if os.path.isdir(odir) else []
    rec = json.load(open(os.path.join(odir, files[0]))) if files else {}
    check("a shell claim outside Claude Code: held for the shell that ran it, marked as a shell claim with its session",
          r.returncode == 0 and rec.get("bridge_pid") == holder and rec.get("kind") == "shell" and rec.get("sid") == os.getsid(0)
          and rec.get("claude_pid") is None, (r.stderr[-300:], rec))
    alive = subprocess.run(["/bin/kill", "-0", str(holder)], capture_output=True).returncode == 0
    check("...whose shell has already exited", not alive)

    r = py("print(json.dumps(owners.claims()))", env)
    cl = json.loads(r.stdout or "[]")
    check("the claim outlives its shell within the activity window: listed, active", len(cl) == 1 and cl[0].get("active")
          and cl[0].get("holder_gone"), (r.stderr[-300:], cl))
    r = py("print(json.dumps(owners.others()))", env)
    check("...a later run from the same terminal session is the same controller (not refused)", json.loads(r.stdout or "[1]") == [],
          (r.stdout, r.stderr[-300:]))
    r = py("print(json.dumps(owners.others()))", env, new_session=True)
    oth = json.loads(r.stdout or "[]")
    check("...a run from another session sees another, active controller (refused unless --override)", len(oth) == 1
          and oth[0].get("active") and oth[0].get("kind") == "session" and oth[0].get("claim") == "shell", (r.stdout, r.stderr[-300:]))
    r = py("print(owners.describe(owners.others()[0]))", env, new_session=True)
    check("...described as a shell outside Claude Code", "metacomputer sh outside Claude Code" in r.stdout, r.stdout)

    # A Claude Code session's claim and a bridge's claim still go with their process.
    dead = holder
    py(f"owners.write_claim({dead + 100000}, 4242, '/x', 't', 0)", env)              # a claude session claim, pid not alive
    py(f"owners.write_claim({dead + 100001}, None, '/x', 't', 0)", env)              # a bridge claim, pid not alive
    r = py("print(json.dumps(sorted(c['bridge_pid'] for c in owners.claims())))", env)
    check("a dead Claude Code claim and a dead bridge's claim are removed at once (only the shell claim stays)",
          json.loads(r.stdout or "[]") == [holder], r.stdout)

    time.sleep(3.3)
    r = py("print(json.dumps(owners.claims()))", env)
    check("once the activity window has passed, the shell claim is gone", json.loads(r.stdout or "[1]") == []
          and not [f for f in os.listdir(odir) if f.endswith(".json")], r.stdout)

    # A shell claim whose shell is still alive stays (idle after the window), as before.
    p = subprocess.Popen(["/bin/bash", "-c", f"{sys.executable} -c \"{PRELUDE}owners.claim_for_shell('vm_shell')\"; sleep 30"], env=env)
    try:
        time.sleep(1.0)
        time.sleep(3.3)
        r = py("print(json.dumps(owners.claims()))", env)
        cl = json.loads(r.stdout or "[]")
        check("a shell claim whose shell is still running stays, idle after the window", len(cl) == 1 and not cl[0].get("active")
              and cl[0].get("bridge_pid") == p.pid, cl)
    finally:
        p.kill()
        p.wait()

    # Which process is Claude Code: every way it's installed, from a synthetic process table.
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import owners
    native = "/Users/someone/.local/share/claude/versions/2.1.325"
    check("Claude Code is recognized however it's installed (npm/Homebrew, IDE native binary, native installer)",
          all(owners.is_claude(c) for c in ("claude", "/opt/homebrew/bin/claude", native,
              "/Users/someone/.vscode/extensions/anthropic.claude-code-2.1.322-darwin-arm64/resources/native-binary/claude")))
    check("...and other processes aren't", not any(owners.is_claude(c) for c in (
        "/usr/bin/python3", "/bin/zsh", "", "/x/claude/versions/notes.txt", "/x/claude/versions/2.1.325/helper", "claude-helper")))
    table = {300: (200, "/bin/bash"), 200: (100, native), 100: (1, "/bin/zsh")}
    check("claude_of finds a natively installed Claude Code above a tool's shell (its claim goes with that session)",
          owners.claude_of(300, table) == 200, owners.claude_of(300, table))

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
