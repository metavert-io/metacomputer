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
It also checks the owners lock: TimeoutError when another process holds it past the
timeout, with no file left open, and free again once the holder leaves.
"""
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runports  # noqa: E402  (this run's own port numbers: tests/runports.py)
SSH_PORT = runports.port()
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


def detached_pty(argv, timeout=30):
    """Run argv on a new pseudo-terminal in a process whose parent is launchd (pid 1): detached from whatever runs
    this test, so no agent session that started the suite is above it. Returns its exit code (None if it didn't
    report within the timeout)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            if os.fork() != 0:
                os._exit(0)                       # the middle process exits: its child goes to launchd
            for _ in range(500):
                if os.getppid() == 1:
                    break
                time.sleep(0.01)
            cpid, fd = pty.fork()
            if cpid == 0:
                os.execv(argv[0], argv)
            while True:                           # drain the terminal until the command closes it
                try:
                    if not os.read(fd, 4096):
                        break
                except OSError:
                    break
            _, st = os.waitpid(cpid, 0)
            os.write(w, str(os.waitstatus_to_exitcode(st)).encode())
        finally:
            os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)
    data, end = b"", time.time() + timeout
    while time.time() < end:
        if select.select([r], [], [], 1)[0]:
            chunk = os.read(r, 64)
            if not chunk:
                break
            data += chunk
    os.close(r)
    try:
        return int(data)
    except ValueError:
        return None


def main():
    td = tempfile.mkdtemp(prefix="mcomp-owners-")
    # A port of this run's own: no real connection to any VM (a running hub, a test, `web`), nor another run's
    # stand-in, counts as a controller here.
    env = dict(os.environ, MCOMP_RUN_DIR=td, MCOMP_ACTIVE_S="3", MCOMP_SSH_PORT=str(SSH_PORT),
               MCOMP_SESSIONS_DIR=os.path.join(td, "sessions"))   # lib/sessionvm.py's records: never the account's
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
    check("...described as a shell outside an agent session", "metacomputer sh outside an agent session" in r.stdout, r.stdout)

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

    # The owners lock (the hub and metacomputer sh check and claim under it): while another process holds it,
    # TimeoutError after the timeout with its file closed; free again as soon as the holder leaves.
    lib = os.path.join(LAB, "lib")
    probe = ("import json, os, sys, time; sys.path.insert(0, sys.argv[1]); import owners\n"
             "n, t = len(os.listdir('/dev/fd')), time.monotonic()\n"
             "try:\n    with owners.lock(timeout=0.3):\n        print(json.dumps(['acquired']))\n"
             "except Exception as e:\n"
             "    print(json.dumps([type(e).__name__, str(e), time.monotonic() - t, len(os.listdir('/dev/fd')) - n]))\n")
    hold = subprocess.Popen([sys.executable, "-c", "import sys, time; sys.path.insert(0, sys.argv[1]); import owners\n"
                             "with owners.lock():\n    print('held', flush=True); time.sleep(2.0)\n", lib],
                            env=env, stdout=subprocess.PIPE, text=True)
    try:
        held = hold.stdout.readline().strip() == "held"
        r = subprocess.run([sys.executable, "-c", probe, lib], env=env, capture_output=True, text=True, timeout=60)
        got = json.loads(r.stdout or "[]")
        check("while another process holds the owners lock: TimeoutError after the timeout, its file closed",
              held and len(got) == 4 and got[0] == "TimeoutError" and got[1] == "another process holds the owners lock"
              and 0.3 <= got[2] < 1.5 and got[3] == 0, (got, r.stderr[-300:]))
    finally:
        hold.wait(timeout=30)
        hold.stdout.close()
    r = subprocess.run([sys.executable, "-c", probe, lib], env=env, capture_output=True, text=True, timeout=60)
    check("...and free again once its holder has left", json.loads(r.stdout or "[]") == ["acquired"], (r.stdout, r.stderr[-300:]))

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

    # Codex: its native binary, however it's installed (npm/Homebrew start it from a node launcher; the IDE extension and
    # the desktop app run `codex app-server`)
    codex_native = "/opt/homebrew/lib/node_modules/@openai/codex/vendor/aarch64-apple-darwin/codex/codex"
    check("Codex is an agent session however it's installed (npm/Homebrew native binary, IDE extension, desktop app)",
          all(owners.agent_kind(c) == "Codex" for c in ("codex", codex_native,
              "/Users/someone/.vscode/extensions/openai.chatgpt-26.1106.51428-darwin-arm64/bin/macos-aarch64/codex",
              "/Applications/Codex.app/Contents/Resources/codex")))
    check("...and its launcher and look-alikes aren't", not any(owners.agent_kind(c) for c in (
        "node", "/opt/homebrew/bin/node", "/x/bin/codex.js", "codex-helper", "/x/codexify", "/x/codex/notes")))
    check("...and Claude Code is still Claude Code", owners.agent_kind(native) == "Claude Code")
    table = {300: (250, "/bin/zsh"), 250: (200, codex_native), 200: (150, "/opt/homebrew/bin/node"), 150: (1, "/bin/zsh")}
    check("claude_of finds Codex above a command's shell, below its node launcher (its claims go with that session)",
          owners.claude_of(300, table) == 250, owners.claude_of(300, table))
    table = {400: (300, "/bin/zsh"), 300: (250, codex_native), 250: (200, "/bin/zsh"), 200: (1, native)}
    check("the nearest agent session counts: Codex run from Claude Code is a session of its own",
          owners.claude_of(400, table) == 300, owners.claude_of(400, table))

    check("an ancestry that doesn't reach launchd is unknown, never the user's (a process ps didn't list, a loop)",
          owners.ancestry_complete(300, {300: (250, "zsh"), 250: (1, "zsh")})
          and not owners.ancestry_complete(300, {300: (250, "zsh")})
          and not owners.ancestry_complete(300, {300: (250, "zsh"), 250: (300, "zsh")}))
    real_ps = owners.ps_table
    try:
        owners.ps_table = lambda strict=False: (_ for _ in ()).throw(OSError("ps failed")) if strict else {}
        tty_r, tty_w = os.openpty()
        fails_closed = not owners.at_user_terminal((tty_w,))
        os.close(tty_r)
        os.close(tty_w)
    finally:
        owners.ps_table = real_ps
    check("...and at_user_terminal fails closed when ps fails", fails_closed)

    # The user's own terminal, end to end with real processes: a terminal with no agent session above it. The runs
    # below are detached from whatever runs this test (re-parented to launchd), so an agent running the suite isn't
    # above them; a small program named `codex` (built here: copies of Apple's binaries don't run) stands in for
    # Codex giving a command a terminal of its own, running it as its child.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import userterm
    fake = userterm.fake_agent("codex")
    at = [sys.executable, os.path.join(LAB, "lib", "owners.py"), "at-terminal"]
    rc_plain = detached_pty(at)
    rc_codex = detached_pty([fake, *at]) if fake else "no C compiler to build the stand-in"
    check("at-terminal: a terminal with no agent session above it is the user's (exit 0)", rc_plain == 0, rc_plain)
    check("...a terminal Codex gave its command isn't (exit 1)", rc_codex == 1, rc_codex)
    r = subprocess.run(at, stdin=subprocess.DEVNULL, capture_output=True)
    check("...nor is no terminal at all", r.returncode == 1, r.returncode)

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
