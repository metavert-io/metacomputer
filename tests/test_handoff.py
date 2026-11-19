#!/usr/bin/env python3
"""`metacomputer handoff` (lib/handoff.py, owners.py's handoff record) and `metacomputer view --open`
(lib/liveview.py, bin/metacomputer). No VM.

    python3 tests/test_handoff.py

The handoff: open, done, cancel, the time limit, a stopped waiter and `wait`, Enter and Ctrl-C at a terminal,
the session that asked can't say "done" for the user, a record that can't be read fails closed, the
one-controller rule when opening, and the events (the needs_you notice through the event stream's cleaning,
lifecycle.handoff_*; the user's note never in an event). The pause of the hub's acting calls is in
tests/test_hub.py (handoff_checks), the shell's here.

The live view: with stand-ins for pbcopy, pbpaste, open, launchctl, security, tailscale and the VM runner, the
password reaches the clipboard and never an argument, the output, the logs or the vnc:// URL; the clipboard is
cleared after the delay only while it still holds that password; SSH sessions get instructions instead;
--tailnet prints the commands and runs none of them.
"""
import json
import os
import pty
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD = tempfile.mkdtemp(prefix="mcomp-handoff-")
ENV = {"MCOMP_RUN_DIR": os.path.join(TD, "run"), "MCOMP_LOG_DIR": os.path.join(TD, "logs"),
       "MCOMP_EVENTS_DIR": os.path.join(TD, "events"), "MCOMP_EVENTS_SPOOL": os.path.join(TD, "spool"),
       "MCOMP_VM_NAME": "hotest", "MCOMP_SSH_PORT": "1", "MCOMP_SSH_DIR": os.path.join(TD, "ssh"),
       "MCOMP_ACTIVE_S": "600"}
os.environ.update(ENV)
for k in ("MCOMP_CONFIG", "MCOMP_VM_STATE"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(LAB, "lib"))
import owners  # noqa: E402
import handoff  # noqa: E402

FAILS = []
PASSES = [0]
VNC_PW = "Hv7Kp2Qz"               # a stand-in live-view password: it must never show up anywhere but the clipboard
os.makedirs(ENV["MCOMP_SSH_DIR"], mode=0o700)
with open(os.path.join(ENV["MCOMP_SSH_DIR"], "vnc-pass"), "w") as _f:
    _f.write(VNC_PW + "\n")
os.chmod(os.path.join(ENV["MCOMP_SSH_DIR"], "vnc-pass"), 0o600)

# handoff.py in a fresh Python, as a given Claude Code session (FAKE_CLAUDE: its pid; unset: outside Claude Code,
# whatever runs this test), with a 1-second minimum time limit so the limit can be tested quickly.
PRELUDE = ("import os, sys; sys.path.insert(0, os.path.join(%r, 'lib')); import owners; "
           "_c = os.environ.get('FAKE_CLAUDE'); owners.claude_of = (lambda *a: int(_c)) if _c else (lambda *a: None); "
           "import handoff; handoff.MIN_TIMEOUT_S = 1; sys.exit(handoff.main(sys.argv[1:]))") % LAB
SESSION_A = 424242
SAY = "Tick the 'Verify you are human' box on example.com"


def check(name, cond, detail=""):
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)


def env(**extra):
    e = dict(os.environ)
    for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "FAKE_CLAUDE"):
        e.pop(k, None)
    e.update({k: str(v) for k, v in extra.items() if v is not None})
    return e


def hcmd(*args):
    return [sys.executable, "-c", PRELUDE] + list(args)


def run(*args, claude=None, stdin=subprocess.DEVNULL, **extra):
    return subprocess.run(hcmd(*args), env=env(FAKE_CLAUDE=claude, **extra), stdin=stdin, capture_output=True,
                          text=True, timeout=60)


def start(*args, claude=SESSION_A, stdin=subprocess.DEVNULL, **extra):
    return subprocess.Popen(hcmd(*args), env=env(FAKE_CLAUDE=claude, **extra), stdin=stdin, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def wait_open(timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if owners.open_handoff():
            return owners.open_handoff()
        time.sleep(0.05)
    return None


def finish(p, timeout=15):
    try:
        o, e = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        o, e = p.communicate()
        return None, o, e
    return p.returncode, o, e


def events_log():
    try:
        return [json.loads(x) for x in open(os.path.join(ENV["MCOMP_LOG_DIR"], "events.jsonl"))]
    except OSError:
        return []


def clear():
    try:
        os.remove(owners.handoff_path())
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        shutil.rmtree(owners.handoff_path())


def foreign_claim():
    other = subprocess.Popen(["sleep", "120"])
    now = time.time()
    os.makedirs(owners.OWNERS_DIR, exist_ok=True)
    json.dump({"bridge_pid": other.pid, "claude_pid": 999999, "cwd": "/another/project", "started": now,
               "last_call": now, "last_tool": "click"}, open(owners.claim_path(other.pid), "w"))
    return other


# --- the handoff ------------------------------------------------------------------------------------
def basic_checks():
    print("open, the pause everywhere, done from the user's terminal")
    clear()
    n0 = len(events_log())
    p = start("--why", "captcha", "--say", SAY)
    h = wait_open()
    check("open: the record is written, status open, with its time limit", h and h["why"] == "captcha"
          and h["say"] == SAY and 890 <= h["deadline"] - h["opened"] <= 900 and h["claude_pid"] == SESSION_A, h)
    mode = stat.S_IMODE(os.stat(owners.handoff_path()).st_mode)
    check("the record is a 0600 file beside the claims, not a claim", mode == 0o600 and not any(
        c.get("kind") == "handoff" for c in owners.claims()), oct(mode))
    found = owners.others(self_pid=os.getpid(), table={}, mine=set(), my_claude=SESSION_A, direct=[])
    check("others() lists it for every caller, the session that asked included", any(
        o["kind"] == "handoff" and o["active"] for o in found), found)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "owners.py"), "check"], env=env(),
                       capture_output=True, text=True, timeout=30)
    check("owners.py check: exit 4 while the user has it, and says so", r.returncode == 4
          and "the user has the computer" in r.stderr, (r.returncode, r.stderr))
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "owners.py"), "release"], env=env(),
                       capture_output=True, text=True, timeout=30)
    check("release never drops it", "not released" in r.stdout and owners.open_handoff(), r.stdout)
    r = run("guard")
    check("guard: exit 4 with the refusal", r.returncode == 4 and "Paused: the user has the computer" in r.stderr,
          (r.returncode, r.stderr))
    r = run("status", "--json")
    st = json.loads(r.stdout or "{}")
    check("status --json: open, with the instructions", st.get("open") and st["handoff"]["say"] == SAY, r.stdout)
    r = run("--why", "login", "--say", "another", claude=SESSION_A)
    check("a second handoff while one is open is refused (exit 3)", r.returncode == 3 and "already open" in r.stderr,
          (r.returncode, r.stderr))
    r = run("done", claude=SESSION_A)
    check("the session that asked can't say done for the user (no terminal): exit 3, still open",
          r.returncode == 3 and "can't say the user is done" in r.stderr and owners.open_handoff(), (r.returncode, r.stderr))
    note = "solved it\x1b[31m, ‮all good"
    r = run("done", "--note", note, claude=None)
    rc, o, e = finish(p)
    check("done from elsewhere: exit 0, and the waiting handoff returns 0", r.returncode == 0 and rc == 0
          and "Handed the virtual computer back" in r.stdout, (r.returncode, r.stderr, rc, e))
    check("its output tells Claude to read the screen again, with the note escaped", "handoff done" in o
          and "Read the screen again" in o and "Their note: solved it" in o and "\x1b" not in o and "‮" not in o, o)
    check("its output carries the instructions: view --open, the clipboard, handoff done/cancel",
          "metacomputer view --open" in o and "clipboard" in o and "Cmd+V" in o and "metacomputer handoff done" in o
          and "metacomputer handoff cancel" in o and "For Claude" in o, o)
    rec = owners.handoff_record()
    check("the record says done, from where, with the note", rec["status"] == "done" and rec["ended_from"] == "process"
          and rec["note"].startswith("solved it"), rec)
    check("nothing is paused any more", owners.open_handoff() is None and run("guard").returncode == 0)
    evs = events_log()[n0:]
    types = [(e["family"], e["type"]) for e in evs]
    check("events: needs_you.captcha, lifecycle.handoff_opened, lifecycle.handoff_done",
          ("needs_you", "captcha") in types and ("lifecycle", "handoff_opened") in types
          and ("lifecycle", "handoff_done") in types, types)
    ny = next(e for e in evs if e["type"] == "captcha")
    check("the needs_you notice is Claude's (origin claude), with the live-view command as its action",
          ny["origin"] == "claude" and ny["message"] == SAY and "metacomputer view --open" in ny["action"]
          and "clipboard" in ny["action"] and "metacomputer handoff done" in ny["action"]
          and ny["data"].get("handoff_id") == h["id"], ny)
    dn = next(e for e in evs if e["type"] == "handoff_done")
    check("handoff_done: fixed words, the id and timing, has_note, never the note itself",
          dn["origin"] == "cli" and dn["data"]["has_note"] is True and dn["data"]["handoff_id"] == h["id"]
          and "solved it" not in json.dumps(evs), dn)


def cancel_checks():
    print("cancel by the session that asked; a stopped waiter and wait")
    clear()
    n0 = len(events_log())
    p = start("--why", "login", "--say", "Sign in to example.com yourself")
    h = wait_open()
    p.send_signal(signal.SIGTERM)
    rc, o, e = finish(p)
    check("a signal stops only the waiting (exit 5); the handoff stays open", rc == 5 and "stays open" in o
          and owners.open_handoff() and owners.open_handoff()["id"] == h["id"], (rc, o, e))
    w = start("wait", "--json")
    time.sleep(0.5)
    r = run("cancel", "--note", "use the other account", claude=SESSION_A)
    rc, o, e = finish(w)
    res = json.loads(o.strip().splitlines()[-1]) if o.strip() else {}
    check("the session that asked may cancel its own handoff", r.returncode == 0 and "Cancelled" in r.stdout,
          (r.returncode, r.stderr))
    check("wait --json returns 4 with status cancelled and the note", rc == 4 and res.get("status") == "cancelled"
          and res.get("note") == "use the other account" and res.get("ended_from") == "claude_session", (rc, o, e))
    r = run("wait")
    check("wait after it ended reports how it ended", r.returncode == 4 and "cancelled" in r.stdout, r.stdout)
    evs = events_log()[n0:]
    check("handoff_cancelled is sent once", [e["type"] for e in evs].count("handoff_cancelled") == 1,
          [e["type"] for e in evs])
    r = run("done", claude=None)
    check("done with nothing open: exit 1, says so", r.returncode == 1 and "no handoff is open" in r.stderr, r.stderr)


def limit_checks():
    print("the time limit")
    clear()
    n0 = len(events_log())
    t0 = time.time()
    p = start("--why", "2fa", "--say", "Approve the sign-in on your phone", "--timeout", "2s")
    wait_open()
    rc, o, e = finish(p, 20)
    took = time.time() - t0
    rec = owners.handoff_record()
    check("at the limit the handoff ends: exit 124, the computer is Claude's again", rc == 124 and 1.5 < took < 10
          and rec["status"] == "timed_out" and owners.open_handoff() is None, (rc, took, o, e))
    check("its output says don't try the check yourself", "time limit" in o and "Don't try the check yourself" in o, o)
    check("handoff_timed_out is sent (warn)", any(e["type"] == "handoff_timed_out" and e["severity"] == "warn"
                                                  for e in events_log()[n0:]))
    # Nobody waiting: a record past its deadline doesn't pause anything, and the next command records it once.
    clear()
    now = time.time()
    handoff.write_record({"v": 1, "id": "h-0123456789ab", "status": "open", "why": "other", "say": "x",
                          "opened": now - 100, "deadline": now - 1, "timeout_s": 99, "cwd": TD, "claude_pid": None})
    check("an open record past its deadline doesn't count", owners.open_handoff() is None
          and run("guard").returncode == 0)
    n1 = len(events_log())
    run("status")
    run("status")
    rec = owners.handoff_record()
    check("status records the time limit once (one event)", rec["status"] == "timed_out" and [
        e["type"] for e in events_log()[n1:]].count("handoff_timed_out") == 1, rec)
    r = run("done", claude=None)
    check("a late done finds nothing open", r.returncode == 1 and "timed out" in r.stderr, r.stderr)
    r = run("--why", "captcha", "--say", "x", "--timeout", "3h")
    r2 = run("--why", "captcha", "--say", "x", "--timeout", "soon")
    r3 = run("--why", "captcha")
    r4 = run("--why", "bribe", "--say", "x")
    check("usage: time limits from 30 s to 2 h, --say required, --why from the list", r.returncode == 2
          and r2.returncode == 2 and r3.returncode == 2 and r4.returncode == 2,
          (r.returncode, r2.returncode, r3.returncode, r4.returncode))
    r = run("--why", "captcha", "--say", "x", MCOMP_VM_STATE="off")
    check("with the VM off there's nothing to hand over (exit 1)", r.returncode == 1 and "off" in r.stderr
          and owners.open_handoff() is None, r.stderr)


def terminal_checks():
    print("at a terminal: Enter, c, Ctrl-C")
    for keys, want, name in ((b"\n", 0, "Enter is done"), (b"c\n", 4, "c and Enter cancels")):
        clear()
        n0 = len(events_log())
        m, s = pty.openpty()
        p = start("--why", "captcha", "--say", SAY, stdin=s, claude=None)
        os.close(s)
        wait_open()
        time.sleep(0.3)
        os.write(m, keys)
        rc, o, e = finish(p)
        os.close(m)
        rec = owners.handoff_record()
        check(f"{name} (exit {want}), recorded as from the terminal", rc == want and rec["ended_from"] == "enter",
              (rc, rec, e))
        check(f"{name}: no phone ping when the person at the terminal opened it",
              not any(e["family"] == "needs_you" for e in events_log()[n0:]) and "press Enter" in o, o)
    clear()
    m, s = pty.openpty()
    p = start("--why", "captcha", "--say", SAY, stdin=s, claude=None)
    os.close(s)
    wait_open()
    time.sleep(0.3)
    p.send_signal(signal.SIGINT)
    rc, o, e = finish(p)
    os.close(m)
    check("Ctrl-C at that terminal cancels (exit 4)", rc == 4 and owners.handoff_record()["ended_from"] == "ctrl_c",
          (rc, o, e))
    # done at a terminal is the user's, even inside the session that asked (their own `!` command at a pty)
    clear()
    p = start("--why", "captcha", "--say", SAY)
    wait_open()
    m, s = pty.openpty()
    r = subprocess.run(hcmd("done"), env=env(FAKE_CLAUDE=SESSION_A), stdin=s, capture_output=True, text=True,
                       timeout=30)
    os.close(s)
    os.close(m)
    rc, o, e = finish(p)
    check("done at a terminal works in the session that asked too", r.returncode == 0 and rc == 0
          and owners.handoff_record()["ended_from"] == "terminal", (r.returncode, r.stderr, rc))


def controller_checks():
    print("opening one: the one-controller rule")
    clear()
    other = foreign_claim()
    try:
        r = run("--why", "captcha", "--say", SAY, claude=SESSION_A)
        check("Claude doesn't pause another session's work: refused (exit 3)", r.returncode == 3
              and "in use by another controller" in r.stderr and "/another/project" in r.stderr
              and owners.open_handoff() is None, (r.returncode, r.stderr))
        p = start("--why", "captcha", "--say", SAY, "--override")
        ok = wait_open() is not None
        run("cancel", claude=None)
        finish(p)
        check("--override (after the user agreed) opens it", ok)
        clear()
        m, s = pty.openpty()
        p = start("--why", "other", "--say", "Take the wheel", stdin=s, claude=None)
        os.close(s)
        ok = wait_open() is not None
        os.write(m, b"\n")
        finish(p)
        os.close(m)
        check("a person at a terminal can always take the computer", ok)
    finally:
        owners.drop_claim(other.pid)
        other.kill()
        other.wait()


def unreadable_checks():
    print("a record that can't be read fails closed, for a bounded time")
    clear()
    os.makedirs(owners.OWNERS_DIR, exist_ok=True)
    open(owners.handoff_path(), "w").write("{not json")
    h = owners.open_handoff()
    check("garbage reads as an open handoff (it fails closed)", h and h.get("unreadable")
          and abs(h["deadline"] - time.time() - owners.HANDOFF_MAX_S) < 30, h)
    check("guard refuses, and says how to remove it", run("guard").returncode == 4 and "can't be read" in
          run("guard").stderr)
    r = run("done", claude=None)
    check("done can't hand back what can't be read", r.returncode == 1, r.stderr)
    r = run("cancel", claude=None)
    check("cancel removes it", r.returncode == 0 and not os.path.lexists(owners.handoff_path()), r.stderr)
    # an old garbage file: counts only for HANDOFF_MAX_S after it was written
    open(owners.handoff_path(), "w").write("x")
    old = time.time() - owners.HANDOFF_MAX_S - 5
    os.utime(owners.handoff_path(), (old, old))
    check("garbage older than the longest time limit doesn't count", owners.open_handoff() is None)
    clear()
    target = os.path.join(TD, "elsewhere.json")
    now = time.time()
    json.dump({"v": 1, "id": "h-aaaaaaaaaaaa", "status": "done", "why": "other", "opened": now, "deadline": now + 60},
              open(target, "w"))
    os.symlink(target, owners.handoff_path())
    check("a link in its place isn't followed (it reads as unreadable: closed)", owners.open_handoff()
          and owners.open_handoff().get("unreadable"))
    r = run("cancel", claude=None)
    check("cancel removes the link, not what it points to", r.returncode == 0
          and not os.path.lexists(owners.handoff_path()) and os.path.exists(target), r.stderr)
    for what, bad in (("a time limit past 2 h", {"v": 1, "id": "h-aaaaaaaaaaaa", "status": "open", "why": "other",
                                                 "opened": now, "deadline": now + 9e9}),
                      ("an id that isn't one", {"v": 1, "id": "../../x", "status": "open", "why": "other",
                                                "opened": now, "deadline": now + 60}),
                      ("a deadline that isn't a number", {"v": 1, "id": "h-aaaaaaaaaaaa", "status": "open",
                                                          "why": "other", "opened": now, "deadline": float("nan")})):
        json.dump(bad, open(owners.handoff_path(), "w"))
        h = owners.open_handoff()
        check(f"a record with {what} reads as unreadable: open, for at most 2 h",
              h and h.get("unreadable") and h["deadline"] - time.time() <= owners.HANDOFF_MAX_S + 1, h)
    clear()


def event_checks():
    print("events: cleaning, reserved types")
    clear()
    n0 = len(events_log())
    hostile = (f"Solve it \x1b]52;c;aGk=\x07 at https://user:pw@x.example/login?next=/a&t=secret#f, the password is "
               f"{VNC_PW}")
    p = start("--why", "captcha", "--say", hostile)
    wait_open()
    run("cancel", claude=None)
    rc, o, e = finish(p)
    ny = next((e for e in events_log()[n0:] if e["family"] == "needs_you"), {})
    blob = json.dumps(events_log()[n0:])
    check("the notice is cleaned: no escapes, no URL query or credentials, the live-view password redacted",
          ny and "\x1b" not in ny["message"] and "t=secret" not in blob and "user:pw" not in blob
          and VNC_PW not in blob and "[redacted]" in ny["message"], ny)
    check("on the terminal Claude's words carry no control characters", "\x1b" not in o and "\x07" not in o
          and "Solve it" in o, o[:300])
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "events.py"), "emit", "lifecycle", "handoff_done",
                        "--message", "fake"], env=env(), capture_output=True, text=True, timeout=30)
    check("events emit can't fake a handoff event (reserved)", r.returncode == 2 and "reserved" in r.stderr, r.stderr)
    clear()
    p = start("--why", "captcha", "--say", SAY)
    wait_open()
    run("cancel", claude=None)
    finish(p)
    p = start("--why", "captcha", "--say", SAY)
    wait_open()
    run("cancel", claude=None)
    rc, o, e = finish(p)
    check("the same notice again within 5 minutes isn't sent again (notify's rule), and it says so",
          "Not sent again" in o, o)


def shell_checks():
    print("metacomputer sh while the user has the computer")
    clear()
    now = time.time()
    handoff.write_record({"v": 1, "id": "h-bbbbbbbbbbbb", "status": "open", "why": "captcha", "say": "x",
                          "opened": now, "deadline": now + 120, "timeout_s": 120, "cwd": TD, "claude_pid": None})
    e = env(MCOMP_VM_STATE="running", MCOMP_VM_SHELL="on", MCOMP_ACTION_LOG="off", MCOMP_DRIVER_MODE="standard")
    for extra in ([], ["--override"]):
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "vm_shell.py"), "--transport", "1", "false",
                            "-c", "true"] + extra + ["--json"], env=e, capture_output=True, text=True, timeout=30)
        res = json.loads(r.stdout or "{}")
        check(f"sh {' '.join(extra) or '(plain)'}: refused (125) while the user has the computer",
              r.returncode == 125 and "the user has the computer" in res.get("message", ""), (r.returncode, r.stdout,
                                                                                               r.stderr))
    clear()


# --- the live view ----------------------------------------------------------------------------------
STUBS = {
    "pbcopy": 'echo "pbcopy $*" >> "$STUB/argv.log"; cat > "$STUB/clip"',
    "pbpaste": 'echo "pbpaste $*" >> "$STUB/argv.log"; cat "$STUB/clip" 2>/dev/null',
    "open": 'echo "open $*" >> "$STUB/argv.log"',
    "launchctl": 'echo "launchctl $*" >> "$STUB/argv.log"; cat "$STUB/manager" 2>/dev/null || echo Aqua',
    "security": ('echo "security $*" >> "$STUB/argv.log"; case " $* " in *" -w "*) cat "$STUB/keychain" ;; esac'),
    "tailscale": ('echo "tailscale $*" >> "$STUB/argv.log"; '
                  'case "$1" in status) echo \'{"Self": {"DNSName": "box.example-tailnet.ts.net."}}\' ;; esac'),
}


def view_setup():
    vd = os.path.join(TD, "view")
    stub = os.path.join(vd, "stub")
    os.makedirs(stub)
    for name, body in STUBS.items():
        p = os.path.join(stub, name)
        open(p, "w").write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)
    lume = os.path.join(stub, "lume")
    open(lume, "w").write('#!/bin/sh\ncase "$1" in get) echo \'{"name": "hvtest", "status": "running"}\' ;;\n'
                          'ls) echo \'[{"name": "hvtest", "status": "running"}]\' ;; esac\n')
    os.chmod(lume, 0o755)
    ssh = os.path.join(vd, "ssh")
    os.makedirs(ssh, mode=0o700)
    return vd, stub, lume, ssh


def view(vd, stub, lume, ssh, *args, store="file", extra_cfg="", **extra):
    cfg = os.path.join(vd, "test.env")
    open(cfg, "w").write(f'VM_NAME=hvtest\nBASELINE_NAME=hvtest-baseline\nSSH_DIR="{ssh}"\nSSH_PORT=59872\n'
                         f'VNC_PORT=59871\nRUN_DIR="{vd}/run"\nLOG_DIR="{vd}/logs"\nLUME="{lume}"\n'
                         f'VNC_PASSWORD_STORE={store}\n{extra_cfg}')
    e = env(MCOMP_CONFIG=cfg, STUB=stub, PATH=stub + os.pathsep + os.environ.get("PATH", ""),
            MCOMP_VIEW_CLIPBOARD_S=extra.pop("clear", 1), **extra)
    for k in ("MCOMP_RUN_DIR", "MCOMP_LOG_DIR", "MCOMP_SSH_DIR", "MCOMP_VM_NAME"):
        e.pop(k, None)
    return subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "view"] + list(args), env=e,
                          capture_output=True, text=True, timeout=60)


def read(p):
    try:
        return open(p).read()
    except OSError:
        return ""


def everything_but_clip(vd):
    out = []
    for root, _, files in os.walk(vd):
        for f in files:
            if f not in ("clip", "keychain", "vnc-pass", "secret.txt"):
                out.append(read(os.path.join(root, f)))
    return "\n".join(out)


def wait_file(p, want, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if read(p) == want:
            return True
        time.sleep(0.1)
    return False


def view_checks():
    print("view --open: the clipboard, never an argument or the output")
    vd, stub, lume, ssh = view_setup()
    clip, argv = os.path.join(stub, "clip"), os.path.join(stub, "argv.log")
    if os.stat("/dev/console").st_uid != os.getuid():
        print("SKIP view --open's screen checks: this Mac's screen isn't this user's session right now")
        return
    open(os.path.join(ssh, "vnc-pass"), "w").write(VNC_PW + "\n")
    os.chmod(os.path.join(ssh, "vnc-pass"), 0o600)
    r = view(vd, stub, lume, ssh, "--open")
    check("the file store: the password reaches the clipboard", r.returncode == 0 and read(clip) == VNC_PW,
          (r.returncode, r.stdout, r.stderr))
    check("it says so: on the clipboard, paste with Cmd+V, not the Mac's password, cleared in 1 s",
          "clipboard" in r.stdout and "Cmd+V" in r.stdout and "isn't" in r.stdout and "cleared" in r.stdout, r.stdout)
    opened = [l for l in read(argv).splitlines() if l.startswith("open ")]
    check("Screen Sharing is opened at vnc://127.0.0.1:PORT, with no password in the URL",
          opened == ["open vnc://127.0.0.1:59871"], opened)
    check("the password is in no argument, no output and no log", VNC_PW not in read(argv)
          and VNC_PW not in r.stdout + r.stderr and VNC_PW not in everything_but_clip(vd), read(argv))
    check("after the delay the clipboard is cleared (it still held the password)", wait_file(clip, ""), read(clip))
    r = view(vd, stub, lume, ssh, "--open", clear=3)
    time.sleep(0.5)
    open(clip, "w").write("something the user copied")
    time.sleep(4)
    check("a clipboard the user changed meanwhile is left alone", r.returncode == 0
          and read(clip) == "something the user copied", read(clip))
    # The Keychain store: `security ... -w` prints it into the pipe; its arguments name the item, never the password.
    os.remove(os.path.join(ssh, "vnc-pass"))
    open(os.path.join(stub, "keychain"), "w").write("Kc9Pw4Lm\n")
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open", store="keychain", clear=0)
    sec = [l for l in read(argv).splitlines() if l.startswith("security ")]
    check("the Keychain store: read with security -w into the pipe, onto the clipboard", r.returncode == 0
          and read(clip) == "Kc9Pw4Lm" and sec and "-s metacomputer-vnc -a hvtest -w" in sec[0]
          and "Kc9Pw4Lm" not in read(argv) + r.stdout + r.stderr, (sec, r.stdout, r.stderr))
    check("--clear-after 0 says nothing about clearing, and leaves it", "cleared" not in r.stdout
          and read(clip) == "Kc9Pw4Lm", r.stdout)
    secret = os.path.join(vd, "secret.txt")
    open(secret, "w").write("Cm5Store\n")
    r = view(vd, stub, lume, ssh, "--open", store="command", clear=0,
             extra_cfg=f'VNC_PASSWORD_GET="cat {secret}"\nVNC_PASSWORD_SET="cat >/dev/null"\n')
    check("your own command's store: onto the clipboard, not the output", r.returncode == 0
          and read(clip) == "Cm5Store" and "Cm5Store" not in r.stdout + r.stderr, (r.stdout, r.stderr))
    # Over SSH, or outside the screen session: instructions instead, nothing touched.
    open(clip, "w").write("untouched")
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open", store="keychain", SSH_CONNECTION="10.0.0.2 5000 10.0.0.1 22")
    check("over SSH: exit 1 with how to do it instead (the Mac's screen, the tunnel, --tailnet)", r.returncode == 1
          and "came in over SSH" in r.stdout and "ssh -N -L 59871:127.0.0.1:59871" in r.stdout
          and "view --tailnet" in r.stdout and "metacomputer view --open" in r.stdout, (r.stdout, r.stderr))
    check("over SSH: no clipboard, no Screen Sharing, no password read", read(clip) == "untouched"
          and not any(l.split()[0] in ("pbcopy", "open", "security") for l in read(argv).splitlines()), read(argv))
    open(os.path.join(stub, "manager"), "w").write("Background\n")
    r = view(vd, stub, lume, ssh, "--open", store="keychain")
    os.remove(os.path.join(stub, "manager"))
    check("outside the screen session (launchctl: Background): the same instructions", r.returncode == 1
          and "screen session" in r.stdout and read(clip) == "untouched", (r.stdout, r.stderr))
    open(lume, "w").write('#!/bin/sh\ncase "$1" in get) echo \'{"name": "hvtest", "status": "stopped"}\' ;; esac\n')
    r = view(vd, stub, lume, ssh, "--open")
    check("with the VM off: says to start it, opens nothing", r.returncode != 0 and "off" in r.stderr
          and read(clip) == "untouched", r.stderr)

    print("view and view --tailnet")
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh)
    check("view: points at --open and --tailnet, and runs neither", r.returncode == 0 and "view --open" in r.stdout
          and "view --tailnet" in r.stdout and "tailscale serve" not in r.stdout and read(argv) == "", r.stdout)
    r = view(vd, stub, lume, ssh, "--tailnet")
    ts = [l for l in read(argv).splitlines() if l.startswith("tailscale ")]
    check("--tailnet prints the serve command, its undo and the tailnet name", r.returncode == 0
          and "tailscale serve --tcp 59871 tcp://127.0.0.1:59871" in r.stdout
          and "tailscale serve --tcp 59871 off" in r.stdout and "box.example-tailnet.ts.net:59871" in r.stdout
          and "your choice" in r.stdout, r.stdout)
    check("--tailnet runs nothing but `tailscale status`", ts == ["tailscale status --json"], ts)
    r = view(vd, stub, lume, ssh, "--bogus")
    check("an unknown option is a usage error", r.returncode != 0 and "usage" in r.stderr, r.stderr)

    print("liveview.py clip: what it takes")
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "liveview.py"), "clip", "--clear-after", "0"],
                       input="bad\x1bpass\n", env=env(STUB=stub, PATH=stub + os.pathsep + os.environ["PATH"]),
                       capture_output=True, text=True, timeout=30)
    check("a password with control characters is refused, nothing copied", r.returncode == 1
          and read(clip) != "bad\x1bpass", r.stderr)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "liveview.py"), "clip"], input="",
                       env=env(STUB=stub, PATH=stub + os.pathsep + os.environ["PATH"]), capture_output=True, text=True,
                       timeout=30)
    check("no password: exit 1", r.returncode == 1)


def main():
    try:
        basic_checks()
        cancel_checks()
        limit_checks()
        terminal_checks()
        controller_checks()
        unreadable_checks()
        event_checks()
        shell_checks()
        view_checks()
    finally:
        clear()
        shutil.rmtree(TD, ignore_errors=True)
    print(f"\n{PASSES[0]}/{PASSES[0] + len(FAILS)} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
