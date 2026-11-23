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
import secrets
import select
import shutil
import signal
import stat
import termios
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD = tempfile.mkdtemp(prefix="mcomp-handoff-")
ENV = {"MCOMP_RUN_DIR": os.path.join(TD, "run"), "MCOMP_LOG_DIR": os.path.join(TD, "logs"),
       "MCOMP_EVENTS_DIR": os.path.join(TD, "events"), "MCOMP_EVENTS_SPOOL": os.path.join(TD, "spool"),
       "MCOMP_VM_NAME": "hotest", "MCOMP_SSH_PORT": "1", "MCOMP_SSH_DIR": os.path.join(TD, "ssh"),
       "MCOMP_ACTIVE_S": "600", "MCOMP_SESSIONS_DIR": os.path.join(TD, "sessions")}
os.environ.update(ENV)
for k in ("MCOMP_CONFIG", "MCOMP_VM_STATE"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(LAB, "lib"))
import owners  # noqa: E402
import handoff  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import userterm  # noqa: E402

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
           "_real_ps = owners.ps_table; "
           "owners.ps_table = ((lambda strict=False: (_ for _ in ()).throw(OSError('ps failed')) if strict else {}) "
           "if os.environ.get('FAKE_PS_FAIL') else _real_ps); "
           "import handoff; handoff.MIN_TIMEOUT_S = 1; "
           "handoff.OPEN_WAIT_S = float(os.environ.get('TEST_OPEN_WAIT', '40')); "
           "sys.exit(handoff.main(sys.argv[1:]))") % LAB
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
    r = run("done", claude=SESSION_A + 1)
    check("...nor can another agent session (another Claude Code or Codex, or one started from the first)",
          r.returncode == 3 and "an agent session" in r.stderr and owners.open_handoff(), (r.returncode, r.stderr))
    r = run("done", claude=None, FAKE_PS_FAIL="1")
    check("...nor anything when the process list can't be read (unknown is never the user)",
          r.returncode == 3 and "couldn't tell whether" in r.stderr and owners.open_handoff(), (r.returncode, r.stderr))
    note = "solved it\x1b[31m, ‮all good"
    r = run("done", "--note", note, claude=None)
    rc, o, e = finish(p)
    check("done from elsewhere: exit 0, and the waiting handoff returns 0", r.returncode == 0 and rc == 0
          and "Handed the virtual computer back" in r.stdout, (r.returncode, r.stderr, rc, e))
    check("its output tells Claude to read the screen again, with the note escaped", "handoff done" in o
          and "Read the screen again" in o and "Their note: solved it" in o and "\x1b" not in o and "‮" not in o, o)
    check("its output carries the instructions: view --open, the clipboard, handoff done/cancel",
          "metacomputer view --open" in o and "clipboard" in o and "Cmd+V" in o and "metacomputer handoff done" in o
          and "metacomputer handoff cancel" in o and "For you, the agent" in o, o)
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
          and "Cmd+V" in ny["action"] and "metacomputer handoff done" in ny["action"]
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
    clear()
    p = start("--why", "captcha", "--say", SAY, "--timeout", "2h")
    h = wait_open()
    run("cancel", claude=None)
    finish(p)
    check("the longest time limit (2h) makes a valid record, not an unreadable one", h and not h.get("unreadable")
          and h.get("id"), h)
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
    # A terminal inside the session that asked isn't the user's: Codex can give its commands a pseudo-terminal, so
    # there `done` would be the agent saying the user is done (owners.at_user_terminal).
    clear()
    p = start("--why", "captcha", "--say", SAY)
    wait_open()
    m, s = pty.openpty()
    r = subprocess.run(hcmd("done"), env=env(FAKE_CLAUDE=SESSION_A), stdin=s, capture_output=True, text=True,
                       timeout=30)
    os.close(s)
    os.close(m)
    check("done at a terminal inside the session that asked is refused (an agent's own terminal isn't the user's)",
          r.returncode == 3 and "can't say the user is done" in r.stderr and owners.open_handoff(), (r.returncode, r.stderr))
    m, s = pty.openpty()
    r = subprocess.run(hcmd("done"), env=env(FAKE_CLAUDE=None), stdin=s, capture_output=True, text=True, timeout=30)
    os.close(s)
    os.close(m)
    rc, o, e = finish(p)
    check("...and done at the user's own terminal works", r.returncode == 0 and rc == 0
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
    # garbage counts for HANDOFF_MAX_S from when it last changed, and no longer
    open(owners.handoff_path(), "w").write("x")
    check("garbage counts for at most 2 h from when it last changed", owners.open_handoff()
          and owners.open_handoff(now=time.time() + owners.HANDOFF_MAX_S + 5) is None)
    future = time.time() + 10 * 365 * 86400
    os.utime(owners.handoff_path(), (future, future))
    check("a modification time in the future doesn't stretch it (the kernel's change time counts)",
          owners.open_handoff(now=time.time() + owners.HANDOFF_MAX_S + 5) is None,
          owners.handoff_record())
    # nothing about the file can be had (not even its metadata): 2 h from the first time that was noticed,
    # whoever looks and however often
    real_open, real_lstat, real_time = os.open, os.lstat, time.time
    clock = [real_time()]

    def deny(path, *a, **kw):
        if path == owners.handoff_path():
            raise PermissionError(13, "Permission denied", path)
        return (real_open if deny.which == "open" else real_lstat)(path, *a, **kw)

    def deny_open(path, *a, **kw):
        deny.which = "open"
        return deny(path, *a, **kw)

    def deny_lstat(path, *a, **kw):
        deny.which = "lstat"
        return deny(path, *a, **kw)
    try:
        os.open, os.lstat, time.time = deny_open, deny_lstat, (lambda: clock[0])
        first = owners.open_handoff()
        clock[0] += 3600
        still = owners.open_handoff()
        clock[0] += owners.HANDOFF_MAX_S
        later = owners.open_handoff()
    finally:
        os.open, os.lstat, time.time = real_open, real_lstat, real_time
    check("no metadata at all: open from the first time that was noticed, for at most 2 h, not from each look",
          first and first.get("unreadable") and still and later is None, (first, still, later))
    clear()
    real_path = owners._unreadable_path
    try:
        os.open, os.lstat = deny_open, deny_lstat
        owners._unreadable_path = lambda: os.path.join(TD, "no-such-folder", "handoff-unreadable")
        unbounded = owners.open_handoff()
    finally:
        os.open, os.lstat = real_open, real_lstat
        owners._unreadable_path = real_path
    check("...and when even that first failure can't be recorded, it doesn't count (nothing would bound it)",
          unbounded is None, unbounded)
    clear()
    check("once the record is gone, that first failure is forgotten", owners.handoff_record() is None
          and not os.path.lexists(os.path.join(os.path.dirname(owners.OWNERS_DIR), "handoff-unreadable")))
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
                                                          "why": "other", "opened": now, "deadline": float("nan")}),
                      ("an id with a newline after it", {"v": 1, "id": "h-aaaaaaaaaaaa\n", "status": "open",
                                                         "why": "other", "opened": now, "deadline": now + 60}),
                      ("a folder that isn't text", {"v": 1, "id": "h-aaaaaaaaaaaa", "status": "open", "why": "other",
                                                    "opened": now, "deadline": now + 60, "cwd": 7}),
                      ("a session that isn't a pid", {"v": 1, "id": "h-aaaaaaaaaaaa", "status": "open",
                                                      "why": "other", "opened": now, "deadline": now + 60,
                                                      "claude_pid": "424242"})):
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
    # Admitted with no handoff open; one opens right after (here: as the claim is written), before the run starts.
    mark = os.path.join(TD, "sh-ran")
    rec = {"v": 1, "id": "h-cccccccccccc", "status": "open", "why": "captcha", "say": "x", "opened": now,
           "deadline": now + 120, "timeout_s": 120, "cwd": TD, "claude_pid": None}
    code = ("import json, os, sys; sys.path.insert(0, os.path.join(%r, 'lib')); import owners, handoff, vm_shell\n"
            "claim = owners.claim_for_shell\n"
            "def opening(*a, **kw):\n"
            "    r = claim(*a, **kw); handoff.write_record(json.loads(os.environ['REC'])); return r\n"
            "owners.claim_for_shell = opening\n"
            "sys.exit(vm_shell.main(sys.argv[1:]))\n") % LAB
    r = subprocess.run([sys.executable, "-c", code, "--transport", "4", "/bin/sh", "-c", f"touch {mark}; cat >/dev/null",
                        "x", "-c", "true", "--timeout", "5", "--json"], env=dict(e, REC=json.dumps(rec)), capture_output=True, text=True,
                       timeout=60)
    res = json.loads(r.stdout or "{}")
    check("sh: a handoff that opens after the admission check, before the run starts, still stops it (nothing ran)",
          r.returncode == 125 and "the user has the computer" in res.get("message", "") and not os.path.exists(mark),
          (r.returncode, r.stdout[-300:], r.stderr[-300:], os.path.exists(mark)))
    clear()


def shell_write_checks():
    print("metacomputer sh: the dispatch lock is held until the script is written")
    code = ("import contextlib, fcntl, json, os, sys, time; sys.path.insert(0, os.path.join(%r, 'lib'))\n"
            "import owners, vm_shell\n"
            "seen = []\n"
            "real = os.write\n"
            "def write(fd, data):\n"
            "    if len(data) > 200:\n"
            "        probe = os.open(owners.dispatch_path(), os.O_RDWR)\n"
            "        try:\n"
            "            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(probe, fcntl.LOCK_UN); seen.append(False)\n"
            "        except BlockingIOError:\n"
            "            seen.append(True)\n"
            "        finally:\n"
            "            os.close(probe)\n"
            "    return real(fd, data)\n"
            "os.write = write\n"
            "@contextlib.contextmanager\n"
            "def gate():\n"
            "    with owners.acting():\n"
            "        yield\n"
            "raw, remote, problem = vm_shell.run_remote(['/bin/sh', '-c', 'head -c 5000 >/dev/null; echo done'], "
            "b'y' * 5000, time.monotonic() + 10, {'stdout': 1 << 20, 'stderr': 1 << 20}, None, gate)\n"
            "print(json.dumps({'held': seen, 'out': raw.decode(), 'problem': problem}))\n") % LAB
    r = subprocess.run([sys.executable, "-c", code], env=env(), capture_output=True, text=True, timeout=60)
    res = json.loads(r.stdout or "{}")
    check("the script is written to SSH while the dispatch lock is held (a handoff can't open in between)",
          res.get("held") and all(res["held"]) and "done" in res.get("out", ""), (res, r.stderr[-300:]))


def harness_checks():
    print("the GUI tests' own commands in the VM")
    clear()
    code = ("import os, sys, json; sys.path.insert(0, os.path.join(%r, 'lib')); import harness\n"
            "ran = []\n"
            "harness.subprocess.run = lambda *a, **k: ran.append(a) or harness.subprocess.CompletedProcess(a, 0, '', '')\n"
            "try:\n"
            "    harness.guest(\"pkill -u $(id -u) -f 'mousepad' 2>/dev/null; true\")\n"
            "    print(json.dumps({'step': None, 'ran': len(ran)}))\n"
            "except harness.Step as e:\n"
            "    print(json.dumps({'step': str(e), 'ran': len(ran)}))\n") % LAB
    now = time.time()
    handoff.write_record({"v": 1, "id": "h-333333333333", "status": "open", "why": "captcha", "say": "x",
                          "opened": now, "deadline": now + 120, "timeout_s": 120, "cwd": TD, "claude_pid": None})
    r = subprocess.run([sys.executable, "-c", code], env=env(), capture_output=True, text=True, timeout=60)
    during = json.loads(r.stdout or "{}")
    clear()
    r = subprocess.run([sys.executable, "-c", code], env=env(), capture_output=True, text=True, timeout=60)
    after = json.loads(r.stdout or "{}")
    check("a GUI test's preparation in the VM (pkill, rm) stops while the user has the computer, and runs after",
          during.get("step") and "the user has the computer" in during["step"] and during.get("ran") == 0
          and after.get("step") is None and after.get("ran") == 1, (during, after, r.stderr[-200:]))


LIFECYCLE_LUME = """#!/bin/sh
echo "lume $*" >> "$LUME_LOG"
case "$1" in
  ls) echo '[{"name": "lctest", "status": "running"}, {"name": "lctest-baseline", "status": "stopped"}]' ;;
  get) case "$2" in lctest) echo '{"name": "lctest", "status": "running"}' ;;
                     *) echo "{\\"name\\": \\"$2\\", \\"status\\": \\"stopped\\"}" ;; esac ;;
  config) printf 'Configured VM storage locations:\\n  - home: %s/vms (default)\\n' "$(dirname "$0")" ;;   # where it keeps VMs
esac
exit 0
"""


def lifecycle_checks():
    print("stop, reset, restore, baseline, desktop-restart: one more look after the question")
    clear()
    ld = os.path.join(TD, "lifecycle")
    os.makedirs(ld)
    lume, llog = os.path.join(ld, "lume"), os.path.join(ld, "lume.log")
    open(lume, "w").write(LIFECYCLE_LUME)
    os.chmod(lume, 0o755)
    cfg = os.path.join(ld, "test.env")
    open(cfg, "w").write(f'VM_NAME=lctest\nBASELINE_NAME=lctest-baseline\nSSH_DIR="{ld}/ssh"\nSSH_PORT=59875\n'
                         f'VNC_PORT=59876\nRUN_DIR="{ENV["MCOMP_RUN_DIR"]}"\nLOG_DIR="{ld}/logs"\nLUME="{lume}"\n')
    e = env(MCOMP_CONFIG=cfg, LUME_LOG=llog, MCOMP_LOCK_DIR=os.path.join(ld, "locks"), MCOMP_STOP_DEADLINE="3")
    for k in ("MCOMP_RUN_DIR", "MCOMP_LOG_DIR", "MCOMP_SSH_DIR", "MCOMP_VM_NAME"):
        e.pop(k, None)
    m, sl = pty.openpty()
    p = subprocess.Popen([os.path.join(LAB, "bin", "metacomputer"), "reset"], env=e, stdin=sl,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(sl)
    os.set_blocking(p.stdout.fileno(), False)
    seen, end = b"", time.time() + 20
    while b"[y/N]" not in seen and time.time() < end and p.poll() is None:
        try:
            seen += os.read(p.stdout.fileno(), 4096) or b""
        except BlockingIOError:
            time.sleep(0.05)
    asked = b"[y/N]" in seen
    now = time.time()
    handoff.write_record({"v": 1, "id": "h-444444444444", "status": "open", "why": "login", "say": "x",
                          "opened": now, "deadline": now + 120, "timeout_s": 120, "cwd": TD, "claude_pid": None})
    open(llog, "w").close()
    os.write(m, b"y\n")
    try:
        p.wait(60)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    err = p.stderr.read().decode("utf-8", "replace")
    os.close(m)
    acted = [x for x in read(llog).splitlines() if x.split()[1:2] and x.split()[1] not in ("ls", "get")]
    check("reset: a handoff that opens while it asks stops it after the answer (exit 4), before it changes anything",
          asked and p.returncode == 4 and "the user has the computer" in err and not acted,
          (asked, p.returncode, err[-300:], acted))
    clear()


def web_cleanup_checks():
    print("metacomputer web's cleanup while the user has the computer")
    clear()
    import browser_tools
    sent = []
    real_guest = browser_tools.guest
    browser_tools.guest = lambda cmd: sent.append(cmd) or ""
    d = object.__new__(browser_tools.Driver)
    d.windows = lambda **kw: []
    now = time.time()
    try:
        handoff.write_record({"v": 1, "id": "h-dddddddddddd", "status": "open", "why": "login", "say": "x",
                              "opened": now, "deadline": now + 120, "timeout_s": 120, "cwd": TD, "claude_pid": None})
        kept = d.close_firefox({"window_id": 0x1a00003, "opened": [0x1a00003]})
        during = list(sent)
        handoff.end("h-dddddddddddd", "done", "process")
        closed = d.close_firefox({"window_id": 0x1a00003, "opened": [0x1a00003]})
    finally:
        browser_tools.guest = real_guest
    check("a web run's Firefox windows aren't closed over SSH while the user has the computer, and are after",
          kept is False and not during and closed is True and len(sent) == 1 and "wmctrl -ic" in sent[0],
          (kept, during, closed, sent))
    clear()


# A stand-in for the guest's input-reset at the end of the SSH command (lib/input_reset.py): it notes whether the
# handoff was open and the dispatch lock held exclusively while it ran, and answers as FAKE_RESET says.
FAKE_GUEST = r'''
import fcntl, json, os, sys, time
sys.path.insert(0, os.path.join(%r, "lib"))
import owners
os.makedirs(owners.OWNERS_DIR, exist_ok=True)
fd = os.open(owners.dispatch_path(), os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    held = False
except BlockingIOError:
    held = True
with open(os.environ["FAKE_RESET_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "dispatch_held": held, "handoff_open": bool(owners.open_handoff()),
                        "stamp": owners.input_reset_at()}) + "\n")
mode = os.environ.get("FAKE_RESET", "2")
if mode == "ssh_fail":
    sys.exit(255)
if mode == "slow":
    time.sleep(30)
if mode == "flood":
    sys.stdout.write("9" * 100000)
    sys.exit(0)
print(mode)
'''


def input_reset_checks():
    print("opening a handoff removes the driver's leftover input devices; how every reset is made; view --open's")
    import input_reset
    clear()
    d = os.path.join(TD, "reset")
    os.makedirs(d, exist_ok=True)
    fake, log = os.path.join(d, "fake_guest.py"), os.path.join(d, "reset.log")
    open(fake, "w").write(FAKE_GUEST % LAB)
    transport = [sys.executable, fake]
    T = ["--transport", "2"] + transport

    def entries():
        return [json.loads(x) for x in read(log).splitlines() if x.strip()]

    def unstamp():
        if os.path.lexists(owners.input_reset_path()):
            os.remove(owners.input_reset_path())
    check("the guest's answer: one count and nothing else",
          [input_reset.parse_count(x) for x in (b"0\n", b" 2 ", b"x", b"1 2", b"", b"12345", b"-1")]
          == [0, 2, None, None, None, None, None])
    os.environ["FAKE_RESET_LOG"] = log
    try:
        unstamp()
        os.environ["FAKE_RESET"] = "2"
        got = input_reset.run_guest(transport, check=True)
        got2 = input_reset.run_guest(transport)
        check("run_guest only asks: --check and a plain run record nothing themselves", got == (2, None)
              and got2 == (2, None) and owners.input_reset_at() == 0.0, (got, got2))
        outcomes = []
        for mode in ("ssh_fail", "nope", "flood"):
            os.environ["FAKE_RESET"] = mode
            outcomes.append(input_reset.run_guest(transport))
        os.environ["FAKE_RESET"] = "slow"
        t0 = time.time()
        outcomes.append(input_reset.run_guest(transport, timeout=1.5))
        took = time.time() - t0
        check("what the VM answers is read with limits: SSH failing, not a count, too long, too slow (stopped at its "
              "deadline)", [n for n, _ in outcomes] == [None] * 4 and "SSH" in outcomes[0][1]
              and "count" in outcomes[1][1] and "too long" in outcomes[2][1] and "within 1 s" in outcomes[3][1]
              and took < 8, (outcomes, took))
        check("nothing to run it over: no reset", input_reset.run_guest([]) == (None, "no connection to the virtual "
                                                                                 "computer was given"))
        # reset_now: recorded before the guest is asked; only a confirmed 0 puts the earlier record back.
        unstamp()
        os.environ["FAKE_RESET"] = "2"
        open(log, "w").close()
        got = input_reset.reset_now(transport)
        at_run = entries()[-1].get("stamp") if entries() else None
        check("reset_now records the reset before the guest is asked, and keeps it when pairs were removed",
              got == (2, None) and at_run and at_run > 0 and owners.input_reset_at() == at_run, (got, at_run))
        prev = owners.input_reset_at()
        os.environ["FAKE_RESET"] = "ssh_fail"
        got = input_reset.reset_now(transport)
        check("…an answer that's lost (the reset may have happened) leaves it recorded: the hub renews",
              got[0] is None and owners.input_reset_at() > prev, (got, prev, owners.input_reset_at()))
        prev = owners.input_reset_at()
        os.environ["FAKE_RESET"] = "0"
        got = input_reset.reset_now(transport)
        check("…a confirmed 0 puts the earlier record back (nothing to renew)", got == (0, None)
              and owners.input_reset_at() == prev, (got, prev, owners.input_reset_at()))
        unstamp()
        os.makedirs(owners.input_reset_path())          # can't be written: no reset
        open(log, "w").close()
        got = input_reset.reset_now(transport)
        os.rmdir(owners.input_reset_path())
        check("…when the record can't be written, the guest isn't asked", got[0] is None and "record" in got[1]
              and entries() == [], (got, entries()))
        # MCP_HUB=off: nothing reports what each bridge runs, so in-flight is unknown (never 0).
        os.environ["MCOMP_MCP_HUB"] = "off"
        try:
            off = input_reset.in_flight()
        finally:
            os.environ.pop("MCOMP_MCP_HUB", None)
        check("in_flight with MCP_HUB=off is unknown (None), never 0; with no hub at all it's 0",
              off is None and input_reset.in_flight() == 0, off)
        # A driver connection the shared hub doesn't own (a script or bridge running a hub of its own, whatever this
        # command's MCP_HUB says): its calls can't be seen, so in-flight is unknown, not 0.
        fd_ = os.path.join(TD, "fakessh")
        os.makedirs(fd_, exist_ok=True)
        src, exe = os.path.join(fd_, "ssh.c"), os.path.join(fd_, "ssh")
        open(src, "w").write("#include <unistd.h>\nint main(void) { sleep(60); return 0; }\n")
        subprocess.run(["/usr/bin/cc", "-o", exe, src], capture_output=True)
        if os.path.exists(exe):
            conn = subprocess.Popen([exe, "-T", "-o", f"IdentityFile={TD}/ssh/mcp", "-p", owners.SSH_PORT or "1",
                                     "cua@127.0.0.1"])
            time.sleep(0.3)
            try:
                seen, unknown = input_reset.outside_connections(None), input_reset.in_flight()
            finally:
                conn.kill()
                conn.wait()
            check("a driver connection no shared hub owns (a local hub's): in_flight is unknown, never 0",
                  seen is True and unknown is None and input_reset.in_flight() == 0, (seen, unknown))
        else:
            print("SKIP the local-hub connection check: no C compiler for the stand-in ssh")
    finally:
        os.environ.pop("FAKE_RESET", None)

    # Opening a handoff: under the dispatch lock held exclusively, after the record is written, recorded first.
    unstamp()
    open(log, "w").close()
    p = start(*T, "--why", "captcha", "--say", SAY, FAKE_RESET="2")
    h = wait_open()
    end = time.time() + 15
    while not entries() and time.time() < end:
        time.sleep(0.05)
    run("cancel", claude=SESSION_A)
    rc, o, e = finish(p)
    ent = entries()
    check("opening a handoff runs the guest's input-reset once, with the record written, the dispatch lock held "
          "exclusively (no agent call can be sent meanwhile) and the reset already recorded",
          h and len(ent) == 1 and ent[0]["handoff_open"] and ent[0]["dispatch_held"] and (ent[0].get("stamp") or 0) > 0
          and ent[0]["argv"] == [input_reset.GUEST], ent)
    check("its message says so", "Removed Claude's leftover virtual input devices (2 pair(s))" in o, o[:800])
    for mode, want in (("ssh_fail", "couldn't reach the VM over SSH"), ("not-a-count", "wasn't a count")):
        open(log, "w").close()
        p = start(*T, "--why", "login", "--say", SAY, FAKE_RESET=mode)
        h = wait_open()
        end = time.time() + 15
        while not entries() and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.5)
        still = owners.open_handoff()
        run("cancel", claude=SESSION_A)
        rc, o, e = finish(p)
        check(f"a reset that fails ({mode}) doesn't stop the handoff: it opens, and its message says to end it and run "
              "`metacomputer input-reset`", h and still and want in o and "metacomputer handoff cancel" in o
              and "metacomputer input-reset" in o and "Removed" not in o, o[:900])
    # When calls may still be running (MCP_HUB=off: that can't be known), nothing is removed: pending.
    open(log, "w").close()
    p = start(*T, "--why", "login", "--say", SAY, FAKE_RESET="2", MCOMP_MCP_HUB="off", TEST_OPEN_WAIT="40")
    h = wait_open()
    time.sleep(0.8)
    run("cancel", claude=SESSION_A)
    rc, o, e = finish(p, 40)
    check("when it can't be known whether an agent call is still running (MCP_HUB=off), the handoff opens without "
          "removing anything, and says the reset is pending and how to make it", h and entries() == []
          and "weren't removed" in o and "metacomputer input-reset" in o and "Removed" not in o, (entries(), o[:900]))
    p = start("--transport", "0", "--why", "login", "--say", SAY)
    h = wait_open()
    time.sleep(0.5)
    run("cancel", claude=SESSION_A)
    rc, o, e = finish(p)
    check("when bin/metacomputer couldn't set SSH up (--transport 0) the handoff still opens, and says what to run",
          h and "couldn't set up SSH to the VM" in o and "metacomputer input-reset" in o, o[:900])
    open(log, "w").close()
    p = start(*T, "--why", "captcha", "--say", SAY, FAKE_RESET="0")
    wait_open()
    end = time.time() + 15
    while not entries() and time.time() < end:
        time.sleep(0.05)
    time.sleep(0.3)
    run("cancel", claude=SESSION_A)
    rc, o, e = finish(p)
    check("nothing to remove: nothing said", entries() and "virtual input devices" not in o, o[:600])
    open(log, "w").close()
    p = start("--why", "captcha", "--say", SAY)
    wait_open()
    time.sleep(0.5)
    run("cancel", claude=SESSION_A)
    finish(p)
    check("handoff.py without bin/metacomputer's transport: nothing is run", entries() == [])

    # In flight: opening waits a little for acting calls already in the driver; when one is still running, or that
    # can't be known, it removes nothing (removing them could cut that call off).
    seen, flights = [], [1, 1, 0]
    real = (input_reset.in_flight, input_reset.run_guest, input_reset.INFLIGHT_WAIT_S, input_reset.POLL_S)
    try:
        input_reset.in_flight = lambda: flights.pop(0) if flights else 0
        input_reset.run_guest = lambda t, check=False, **kw: (seen.append(t) or (1, None))
        input_reset.POLL_S = 0.01
        a = input_reset.at_handoff(["ssh"])
        check("opening waits for the acting calls in flight to end, then resets", a == ("removed", 1, False)
              and not flights and seen == [["ssh"]], (a, flights))
        input_reset.INFLIGHT_WAIT_S = 0.2
        for stuck in (1, None):
            seen.clear()
            input_reset.in_flight = lambda: stuck
            a = input_reset.at_handoff(["ssh"])
            line = handoff.reset_line(a, "Claude")
            check(f"…when one is still running after the wait ({stuck}): nothing removed, the message says it's pending "
                  "and how to make it", a[0] == "pending" and seen == [] and "weren't removed" in line
                  and "metacomputer input-reset" in line, (a, seen, line))
        input_reset.in_flight = lambda: 0
        input_reset.run_guest = lambda t, check=False, **kw: 1 / 0
        a = input_reset.at_handoff(["ssh"])
        check("an unexpected error is a failed reset, never a failed handoff", a[0] == "failed", a)
        check("without a transport (handoff.py run by itself) nothing is tried", input_reset.at_handoff([])[0] == "skipped")
    finally:
        input_reset.in_flight, input_reset.run_guest, input_reset.INFLIGHT_WAIT_S, input_reset.POLL_S = real
    clear()

    # view --open's reset (for_view): the policy, with the hub, the guest and who's at the terminal stood in for.
    real = (owners.at_user_terminal, owners.others, input_reset.in_flight, input_reset.run_guest)
    said, ran = [], []

    def guest(t, check=False, **kw):
        fdl = os.open(owners.dispatch_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl
            fcntl.flock(fdl, fcntl.LOCK_SH | fcntl.LOCK_NB)
            held = False
        except BlockingIOError:
            held = True
        finally:
            os.close(fdl)
        ran.append(held)
        return guest.answer

    def case(user, busy, flight, answer=(2, None), handoff_open=False):
        said.clear(), ran.clear()
        owners.at_user_terminal = lambda fds=(0,): user
        owners.others = lambda *a, **k: [{"kind": "session", "active": True}] if busy else []
        input_reset.in_flight = lambda: flight
        guest.answer = answer
        clear()
        if handoff_open:
            t = time.time()
            handoff.write_record({"v": 1, "id": "h-eeeeeeeeeeee", "status": "open", "why": "login", "say": "x",
                                  "opened": t, "deadline": t + 60, "timeout_s": 60, "cwd": TD, "claude_pid": None})
        rc = input_reset.for_view(["ssh"], out=said.append)
        clear()
        return rc, list(said), list(ran)
    try:
        input_reset.run_guest = guest
        got = case(False, False, 0)
        check("view --open with nobody else active and nothing in flight: resets under the dispatch lock held "
              "exclusively and says so", got[0] == 0 and got[2] == [True] and "Removed 2" in got[1][0], got)
        got = case(False, True, 0)
        check("...another controller active (one controller at a time): left as they are, and says what to run",
              got[2] == [] and "another session" in got[1][0] and "metacomputer input-reset" in got[1][0], got)
        got = case(True, True, 0)
        check("...but a person at their own terminal takes the computer whatever session is active", got[2] == [True], got)
        for flight, word in ((1, "still running"), (None, "couldn't tell")):
            for user in (False, True):
                got = case(user, False, flight)
                check(f"...an agent call in flight ({flight}): never, not even for the person at the terminal", got[2] == []
                      and word in got[1][0], got)
        got = case(False, False, 0, handoff_open=True)
        check("...a handoff open, not the user's terminal (the agent, the dialog's button): nothing run, nothing said "
              "(opening it reset them)", got[2] == [] and got[1] == [], got)
        got = case(True, False, 0, handoff_open=True)
        check("...a handoff open, at the user's own terminal: their reset runs", got[2] == [True], got)
        got = case(False, False, 0, answer=(0, None))
        check("...nothing to remove: nothing said", got[2] == [True] and got[1] == [], got)
        got = case(False, False, 0, answer=(None, "couldn't reach the VM over SSH"))
        check("...a failed reset says so and what to run; view --open goes on (exit 0)", got[0] == 0
              and "Couldn't remove" in got[1][0] and "metacomputer input-reset" in got[1][0], got)
        input_reset.in_flight = real[2]
        os.environ["MCOMP_MCP_HUB"] = "off"
        try:
            owners.at_user_terminal = lambda fds=(0,): True
            owners.others = lambda *a, **k: []
            said.clear(), ran.clear()
            input_reset.for_view(["ssh"], out=said.append)
        finally:
            os.environ.pop("MCOMP_MCP_HUB", None)
        check("...with MCP_HUB=off (the bridges' calls can't be seen): never, and says so", ran == []
              and said and "couldn't tell" in said[0], (ran, said))
    finally:
        (owners.at_user_terminal, owners.others, input_reset.in_flight, input_reset.run_guest) = real
        os.environ.pop("FAKE_RESET_LOG", None)
        clear()


VAULT_STUB = """#!/bin/sh
case "$1" in
  version) echo '{"build":"test"}'; exit 0 ;;
  requests) [ "$STUB_REQUESTS" = fail ] && { echo 'not json'; exit 3; }
            [ -n "$STUB_REQUESTS" ] && printf '%s\\n' "$STUB_REQUESTS" || echo '{"requests": []}'; exit 0 ;;
  settle) echo "settle $*" >> "$VAULT_LOG"; sleep "${STUB_SETTLE_S:-0}"
          if [ -n "$STUB_SETTLE_FAIL" ] && [ -f "$STUB_SETTLE_FAIL" ]; then echo '{"ok":false,"settled":false}'; exit 1; fi
          echo "settled" >> "$VAULT_LOG"; echo '{"ok":true,"settled":true}'; exit 0 ;;
esac
echo "start $*" >> "$VAULT_LOG"
sleep "${STUB_FILL_S:-0}"
if [ -n "$STUB_FILL_RC" ]; then echo "withdrawn $*" >> "$VAULT_LOG"; exit "$STUB_FILL_RC"; fi
echo "sent $*" >> "$VAULT_LOG"
echo '{"ok":true,"chars":8}'
"""


def fill_checks():
    print("secrets fill, login and session load; input-reset")
    clear()
    fd = os.path.join(TD, "fill")
    os.makedirs(fd)
    vault, log = os.path.join(fd, "vault"), os.path.join(fd, "vault.log")
    open(vault, "w").write(VAULT_STUB)
    os.chmod(vault, 0o755)
    lume = os.path.join(fd, "lume")
    open(lume, "w").write('#!/bin/sh\ncase "$1" in get) echo \'{"name": "fltest", "status": "running"}\' ;; esac\n')
    os.chmod(lume, 0o755)
    cfg = os.path.join(fd, "test.env")
    open(cfg, "w").write(f'VM_NAME=fltest\nBASELINE_NAME=fltest-baseline\nSSH_DIR="{fd}/ssh"\nSSH_PORT=59873\n'
                         f'VNC_PORT=59874\nRUN_DIR="{ENV["MCOMP_RUN_DIR"]}"\nLOG_DIR="{fd}/logs"\nLUME="{lume}"\n')
    e = env(MCOMP_CONFIG=cfg, MCOMP_VAULT_BIN=vault, MCOMP_VAULT_ALLOW_TEST_BUILD="1", VAULT_LOG=log)
    for k in ("MCOMP_RUN_DIR", "MCOMP_LOG_DIR", "MCOMP_SSH_DIR", "MCOMP_VM_NAME"):
        e.pop(k, None)
    mc = os.path.join(LAB, "bin", "metacomputer")

    def lines():
        return read(log).splitlines()
    r = subprocess.run([mc, "secrets", "fill", "site", "--vm", "othervm", "--field", "password"], env=e,
                       capture_output=True, text=True, timeout=60)
    check("a fill naming another VM (--vm) is refused before the vault: its handoff isn't this one's",
          r.returncode != 0 and "acts on this VM (fltest)" in r.stderr and not lines(), (r.returncode, r.stderr, lines()))
    r = subprocess.run([mc, "secrets", "fill", "site", "--vm=fltest", "--field", "password"], env=e,
                       capture_output=True, text=True, timeout=60)
    check("--vm naming this VM goes ahead", r.returncode == 0 and any("--vm=fltest" in x for x in lines()),
          (r.returncode, r.stderr, lines()))
    # Paying (R30): only exactly `pay --purchase KEY --done` (with at most --vm naming this VM), read word by word, skips
    # the checks; every other form, whatever its words hold, is checked like a fill.
    key = "ab" * 16
    open(log, "w").close()
    rs = {n: subprocess.run([mc, "secrets", "pay"] + a, env=e, capture_output=True, text=True, timeout=60) for n, a in (
        ("a new purchase with --done added", ["card", "--vm", "othervm", "--origin", "https://shop.example", "--total-selector", "#total",
                                              "--fill", "number=#cc", "--done"]),
        ("a selector holding --done", ["--purchase", key, "--vm", "othervm", "--field", "number", "--selector", "#cc /* --done */"]),
        ("--done naming another VM", ["--purchase", key, "--done", "--vm", "othervm"]),
        ("--done with a fill", ["--purchase", key, "--done", "--field", "number", "--vm", "othervm"]))}
    check("R30: secrets pay with --done somewhere in it (a new purchase, a selector, another VM, a fill) gets every check: "
          "refused before the vault when it names another VM", all(x.returncode != 0 and "acts on this VM (fltest)" in x.stderr
                                                                   for x in rs.values()) and not lines(),
          ({n: (x.returncode, x.stderr[-160:]) for n, x in rs.items()}, lines()))
    r1 = subprocess.run([mc, "secrets", "pay", "--purchase", key, "--done"], env=e, capture_output=True, text=True, timeout=60)
    r2 = subprocess.run([mc, "secrets", "pay", "--vm", "fltest", "--done", "--purchase", key], env=e, capture_output=True, text=True,
                        timeout=60)
    check("R30: ...and exactly `pay --purchase KEY --done` (at most with --vm naming this VM) ends it straight away",
          r1.returncode == 0 and r2.returncode == 0 and [x for x in lines() if x.startswith("start")]
          == [f"start pay --purchase {key} --done", f"start pay --vm fltest --done --purchase {key}"],
          (r1.returncode, r2.returncode, r1.stderr[-200:], lines()))
    open(log, "w").close()
    # A fill that waits (for an approval, say) keeps the computer: a handoff can't open until it's done.
    open(log, "w").close()
    p = subprocess.Popen([mc, "secrets", "fill", "site", "--field", "password"], env=dict(e, STUB_FILL_S="3"),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.time() + 15
    while not any(x.startswith("start") for x in lines()) and time.time() < end:
        time.sleep(0.05)
    h = start("--why", "captcha", "--say", SAY, TEST_OPEN_WAIT="1")
    time.sleep(1.8)
    opened_during = owners.open_handoff() is not None and not any(x.startswith("sent") for x in lines())
    rc_h, o_h, e_h = finish(h, 5) if h.poll() is not None else (None, "", "")
    p.wait(30)
    if h.poll() is None:
        run("cancel", claude=None)
        finish(h)
    check("a handoff can't open while a fill is on its way (it waits for the dispatch lock, then refuses)",
          not opened_during and rc_h == 3 and "still on its way" in e_h and p.returncode == 0,
          (opened_during, rc_h, e_h[-300:], p.returncode))
    clear()
    now = time.time()
    handoff.write_record({"v": 1, "id": "h-eeeeeeeeeeee", "status": "open", "why": "login", "say": "x",
                          "opened": now, "deadline": now + 120, "timeout_s": 120, "cwd": TD, "claude_pid": None})
    open(log, "w").close()
    rs = [subprocess.run([mc, "secrets"] + a, env=e, capture_output=True, text=True, timeout=60)
          for a in (["fill", "site", "--field", "password"], ["login", "site", "--password-selector", "#p"],
                    ["session", "load", "s"])]
    check("fill, login and session load stop (exit 4) while the user has the computer; the vault isn't asked",
          all(x.returncode == 4 and "the user has the computer" in x.stderr for x in rs) and not lines(),
          ([x.returncode for x in rs], rs[0].stderr[-200:], lines()))
    r = subprocess.run([mc, "input-reset"], env=e, capture_output=True, text=True, timeout=60)
    check("input-reset (it acts on the desktop) stops too", r.returncode == 4 and "the user has the computer" in r.stderr,
          (r.returncode, r.stderr[-300:]))
    # ...except from the user's own terminal: it gives back their own mouse and keyboard during their handoff. A
    # stand-in ssh answers the guest's input-reset with a count (the VM's part is tests/test_handoff_vm.py's).
    sd = os.path.join(fd, "sshstub")
    os.makedirs(sd)
    sshlog = os.path.join(fd, "ssh.log")
    open(os.path.join(sd, "ssh"), "w").write('#!/bin/sh\necho "$*" >> "%s"\ncase "$*" in *input-reset*) echo 1 ;; esac\n'
                                             % sshlog)
    os.chmod(os.path.join(sd, "ssh"), 0o755)
    eu = dict(e, PATH=sd + os.pathsep + os.environ.get("PATH", ""))
    stamp = os.path.join(ENV["MCOMP_RUN_DIR"], "owners", owners.INPUT_RESET_FILE)
    if os.path.lexists(stamp):
        os.remove(stamp)

    def at_pty(argv):
        m, s = pty.openpty()
        p = userterm.spawn(argv, env=eu, stdin=s, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.close(s)
        try:
            o, er = p.communicate(timeout=60)
        finally:
            os.close(m)
        return p.returncode, o.decode(errors="replace"), er.decode(errors="replace")
    rc, o, er = at_pty([mc, "input-reset"])
    check("input-reset from the user's own terminal goes ahead while their handoff is open: the guest's reset runs, "
          "its count is printed and the reset is recorded for the hub", rc == 0 and o.strip() == "1"
          and "your handoff is open" in er and "input-reset" in read(sshlog) and os.path.exists(stamp)
          and owners.open_handoff(), (rc, o, er[-300:], read(sshlog)[-200:]))
    open(sshlog, "w").close()
    agent = userterm.fake_agent("codex")
    if agent:
        rc, o, er = at_pty([agent, mc, "input-reset"])
        check("...but not from an agent session, even with a terminal of its own (exit 4, the guest isn't asked)",
              rc == 4 and "the user has the computer" in er and "input-reset" not in read(sshlog), (rc, er[-300:]))
        # The Antigravity app and IDE run their agent inside the app: under its main executable or its language server
        # (the stand-in copied into an app bundle's places), the user's own commands refuse as under any agent session.
        bundle = os.path.join(fd, "apps", "Antigravity.app", "Contents")
        for dst in (os.path.join(bundle, "MacOS", "Antigravity"), os.path.join(bundle, "Resources", "bin", "language_server"),
                    os.path.join(fd, "apps", "Antigravity IDE.app", "Contents", "MacOS", "Electron")):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(agent, dst)
            rc, o, er = at_pty([dst, mc, "input-reset"])
            check(f"...nor under the Antigravity app ({os.path.basename(dst)}), with a terminal of its own (exit 4)",
                  rc == 4 and "the user has the computer" in er and "input-reset" not in read(sshlog), (rc, er[-300:]))
            rc, o, er = at_pty([dst, mc, "handoff", "done"])
            check(f"...and `handoff done` under it doesn't end the user's handoff ({os.path.basename(dst)})",
                  rc != 0 and owners.open_handoff(), (rc, o[-200:], er[-300:]))
    else:
        print("SKIP input-reset from an agent with a terminal: no C compiler for the stand-in agent")
    r = subprocess.run([mc, "input-reset"], env=eu, capture_output=True, text=True, timeout=60)
    check("...nor without a terminal (exit 4)", r.returncode == 4 and "input-reset" not in read(sshlog),
          (r.returncode, r.stderr[-200:]))
    t0 = owners.input_reset_at()
    r = subprocess.run([mc, "input-reset", "--check"], env=eu, capture_output=True, text=True, timeout=60)
    check("--check only reads: it runs for anyone, and records nothing", r.returncode == 0 and "--check" in read(sshlog)
          and owners.input_reset_at() == t0, (r.returncode, r.stderr[-200:]))
    # Not while an agent call may still be running in the VM: with MCP_HUB=off nothing says, so it refuses (exit 3).
    open(sshlog, "w").close()
    eu_off = dict(eu, MCOMP_MCP_HUB="off")
    m, s = pty.openpty()
    p = userterm.spawn([mc, "input-reset"], env=eu_off, stdin=s, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(s)
    try:
        o, er = p.communicate(timeout=60)
    finally:
        os.close(m)
    check("input-reset refuses (exit 3) when it can't be known whether an agent call is still running (MCP_HUB=off), "
          "even at the user's own terminal; the guest isn't asked", p.returncode == 3
          and "couldn't tell" in er.decode() and "input-reset" not in read(sshlog), (p.returncode, er[-300:]))
    # `secrets approve` of a waiting one-time code request fills once allowed: it stops too, however its arguments are
    # ordered, and when the vault's list can't be read (it may fill). Only an approval the list shows as another kind
    # (one that answers a fill already on its way) goes straight on.
    code_req = json.dumps({"requests": [{"id": "1a2b3c4d", "kind": "code", "needs_approval": True}]})
    plain_req = json.dumps({"requests": [{"id": "5e6f7a8b", "use": "fill", "name": "site"}]})
    cases = [(["approve", "1a2b3c4d"], code_req), (["approve", "--json", "1a2b3c4d"], code_req), (["approve", "1a2b3c4d", "--json"], code_req),
             (["approve", "--scope", "use", "1a2b3c4d"], code_req), (["approve", "--json", "1a2b3c4d"], "fail"),
             (["approve", "--json", "9f9f9f9f"], code_req)]
    rs = []
    for args, listing in cases:
        open(log, "w").close()
        x = subprocess.run([mc, "secrets"] + args, env=dict(e, STUB_REQUESTS=listing), capture_output=True, text=True, timeout=60)
        rs.append((args, listing == "fail", x.returncode, "the user has the computer" in x.stderr, any(y.startswith("start approve") for y in lines())))
    check("approve of a waiting code request stops (exit 4) while the user has the computer, with --json before or after the "
          "id, with --scope, when the list can't be read, and for an id the list doesn't show; the vault isn't asked",
          all(rc == 4 and said and not asked for _, _, rc, said, asked in rs), rs)
    open(log, "w").close()
    x = subprocess.run([mc, "secrets", "approve", "--json", "5e6f7a8b"], env=dict(e, STUB_REQUESTS=plain_req), capture_output=True,
                       text=True, timeout=60)
    check("...while an approval the list shows as another kind (answering a fill on its way) goes straight to the vault",
          any(y.startswith("start approve --json 5e6f7a8b") for y in lines()), (x.returncode, x.stderr[-200:], lines()))
    # The commands that act on a one-time code request name this VM (whose handoff they checked) to the vault, which
    # refuses another VM's request; an approve naming another VM is refused here.
    clear()
    got = {}
    for args, stdin in ((["give", "1a2b3c4d"], "123456\n"), (["fill", "--request", "1a2b3c4d"], ""), (["approve", "1a2b3c4d"], "")):
        open(log, "w").close()
        subprocess.run([mc, "secrets"] + args, env=dict(e, STUB_REQUESTS=code_req), input=stdin, capture_output=True, text=True, timeout=60)
        got[args[0]] = [y for y in lines() if y.startswith("start " + args[0])]
    open(log, "w").close()
    x = subprocess.run([mc, "secrets", "approve", "1a2b3c4d", "--vm", "othervm"], env=dict(e, STUB_REQUESTS=code_req), capture_output=True,
                       text=True, timeout=60)
    check("give, fill --request and approve of a code request pass this VM (--vm fltest) to the vault; approve naming another "
          "VM is refused before the vault", all(v and v[-1].endswith("--vm fltest") for v in got.values())
          and x.returncode != 0 and "acts on this VM (fltest)" in x.stderr and not lines(), (got, x.returncode, x.stderr[-200:], lines()))
    clear()

    # The vault withdrew the request but its unlock process didn't confirm it stopped (exit 5): the wrapper keeps the
    # dispatch lock while `settle` waits for the unlock process, so no handoff opens meanwhile.
    marks = lambda: [f for f in os.listdir(owners.OWNERS_DIR) if f.startswith("vault-inflight.")]  # noqa: E731
    open(log, "w").close()
    p = subprocess.Popen([mc, "secrets", "fill", "site", "--field", "password"],
                         env=dict(e, STUB_FILL_RC="5", STUB_SETTLE_S="3"), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    end = time.time() + 15
    while not any(x.startswith("settle") for x in lines()) and time.time() < end:
        time.sleep(0.05)
    h = start("--why", "captcha", "--say", SAY, TEST_OPEN_WAIT="1")
    rc_h, o_h, e_h = finish(h, 6)
    opened_during = owners.open_handoff() is not None
    clear()
    p.wait(30)
    check("an unconfirmed vault request keeps the lock while it settles: a handoff can't open meanwhile",
          rc_h == 3 and "still on its way" in e_h and not opened_during
          and p.returncode == 5 and "settled" in lines() and not marks(),
          (rc_h, e_h[-200:], opened_during, p.returncode, lines(), marks()))
    clear()
    # ...and when it can't confirm even then, a marker stays: opening a handoff settles first, and refuses until it can.
    fail = os.path.join(fd, "settle-fails")
    open(fail, "w").close()
    open(log, "w").close()
    r = subprocess.run([mc, "secrets", "fill", "site", "--field", "password"],
                       env=dict(e, STUB_FILL_RC="5", STUB_SETTLE_FAIL=fail), capture_output=True, text=True, timeout=60)
    left = marks()
    he = dict(e, STUB_SETTLE_FAIL=fail)
    h = start("--why", "captcha", "--say", SAY, TEST_OPEN_WAIT="5", **{k: v for k, v in he.items()
                                                                     if k.startswith(("MCOMP_", "STUB_", "VAULT_"))})
    rc_h, o_h, e_h = finish(h, 20)
    refused = rc_h == 3 and "hasn't confirmed" in e_h and owners.open_handoff() is None
    clear()
    os.remove(fail)
    p = start("--why", "captcha", "--say", SAY, **{k: v for k, v in e.items() if k.startswith(("MCOMP_", "VAULT_"))})
    opened = wait_open() is not None
    run("cancel", claude=None)
    finish(p)
    check("an unconfirmed request whose settle fails leaves a marker; a handoff settles first and refuses until "
          "it can, then opens", r.returncode == 5 and left and refused and opened and not marks(),
          (r.returncode, left, rc_h, e_h[-200:], opened, marks()))
    clear()
    # A vault client that dies after sending (a crash, a signal: here exit 139) is no answer either: settle, then
    # the marker goes; a clean refusal (exit 2, denied) needs none.
    open(log, "w").close()
    r = subprocess.run([mc, "secrets", "fill", "site", "--field", "password"], env=dict(e, STUB_FILL_RC="139"),
                       capture_output=True, text=True, timeout=60)
    crashed = (r.returncode, "settled" in lines(), marks())
    open(log, "w").close()
    r2 = subprocess.run([mc, "secrets", "fill", "site", "--field", "password"], env=dict(e, STUB_FILL_RC="2"),
                        capture_output=True, text=True, timeout=60)
    check("a vault client that died (exit 139) is settled before the lock goes; a clean refusal (exit 2) isn't",
          crashed == (139, True, []) and r2.returncode == 2 and not any(x.startswith("settle") for x in lines())
          and not marks(), (crashed, r2.returncode, lines(), marks()))
    clear()


# --- the live view ----------------------------------------------------------------------------------
# osascript stands in for the pasteboard (lib/liveview.py's PASTEBOARD_JS): "copy" puts stdin on it and prints the
# new change count; "clear N" empties it only if the count is still N. Every argument is logged (on one line).
OSASCRIPT = r"""#!/bin/sh
printf 'osascript %s\n' "$(printf '%s ' "$@" | tr '\n' ' ')" >> "$STUB/argv.log"
if [ "$1" = "-e" ]; then
  # The handoff's dialog (AppleScript: -e SCRIPT MODE TEXT SECONDS WHO): its arguments exactly, then it "waits for
  # the user" (dialog.answer) for up to 30 s, the way a dialog blocks.
  printf '%s' "$2" > "$STUB/dialog.script"; printf '%s' "$4" > "$STUB/dialog.text"
  printf '%s %s %s %s\n' "$3" "$5" "$#" "$6" > "$STUB/dialog.args"; echo $$ > "$STUB/dialog.pid"
  i=0; while [ ! -f "$STUB/dialog.answer" ] && [ $i -lt 300 ]; do sleep 0.1; i=$((i + 1)); done
  cat "$STUB/dialog.answer" 2>/dev/null; echo done >> "$STUB/dialog.ended"; exit 0
fi
n=$(cat "$STUB/count" 2>/dev/null || echo 7)
case "$5" in
  # Screen Sharing's keychain item (lib/liveview.py's REMEMBER_JS: op server keychain label [tag app]); the password
  # arrives on stdin and is kept in kc.pw (the stand-in's keychain).
  save) cat > "$STUB/kc.pw"; echo "$7" > "$STUB/kc.used"
        if [ -f "$STUB/kc.fail" ]; then echo "error created -25308"; exit 0; fi
        if [ -f "$STUB/kc.item" ]; then how=updated; else how=created; fi
        printf '%s\n%s\n%s\n%s\n' "$6" "$8" "$9" "${10}" > "$STUB/kc.item"; echo "$how" ;;
  refresh) cat > "$STUB/kc.pw"; echo "$7" > "$STUB/kc.used"
        if [ -f "$STUB/kc.fail" ]; then echo "error updated -25308"; exit 0; fi
        if [ ! -f "$STUB/kc.item" ]; then echo none; exit 0; fi
        printf '%s\n%s\n%s\n%s\n' "$6" "$8" "$9" "${10}" > "$STUB/kc.item"; echo updated ;;
  show) if [ -f "$STUB/kc.item" ]; then
          printf '{"server": "%s", "port": "0", "label": "%s"}\n' "$(sed -n 1p "$STUB/kc.item")" "$(sed -n 2p "$STUB/kc.item")"
        else echo null; fi ;;
  forget) if [ -f "$STUB/kc.item" ]; then rm -f "$STUB/kc.item"; echo removed; else echo none; fi ;;
  copy) cat > "$STUB/clip"; n=$((n + 1)); echo "$n" > "$STUB/count"
        if [ -f "$STUB/race" ]; then echo raced; else echo "$n"; fi ;;
  clear) echo "clear $6 at $n" >> "$STUB/clears.log"
         if [ "$6" = "$n" ]; then : > "$STUB/clip"; echo $((n + 1)) > "$STUB/count"; echo cleared; else echo kept; fi ;;
esac
"""
STUBS = {
    "pbcopy": 'echo "pbcopy $*" >> "$STUB/argv.log"; cat > "$STUB/clip"',
    "pbpaste": 'echo "pbpaste $*" >> "$STUB/argv.log"; cat "$STUB/clip" 2>/dev/null',
    "open": 'echo "open $*" >> "$STUB/argv.log"',
    "launchctl": 'echo "launchctl $*" >> "$STUB/argv.log"; cat "$STUB/manager" 2>/dev/null || echo Aqua',
    "security": ('echo "security $*" >> "$STUB/argv.log"; case " $* " in *" -w "*) cat "$STUB/keychain" ;; esac; '
                 'case "$1" in login-keychain) [ -f "$STUB/kcpath" ] && printf \'    "%s"\\n\' "$(cat "$STUB/kcpath")" ;; esac'),
    "tailscale": ('echo "tailscale $*" >> "$STUB/argv.log"; '
                  'case "$1" in status) echo \'{"Self": {"DNSName": "box.example-tailnet.ts.net."}}\' ;; esac'),
    # the VM's input reset over SSH (view --open, lib/input_reset.py): the count in $STUB/reset_out, else 0
    "ssh": ('case "$*" in *input-reset*) echo "ssh input-reset" >> "$STUB/argv.log"; '
            'cat "$STUB/reset_out" 2>/dev/null || echo 0 ;; *) echo "ssh $*" >> "$STUB/argv.log" ;; esac'),
}


def view_setup(name="view"):
    vd = os.path.join(TD, name)
    stub = os.path.join(vd, "stub")
    os.makedirs(stub)
    for name, body in STUBS.items():
        p = os.path.join(stub, name)
        open(p, "w").write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)
    open(os.path.join(stub, "osascript"), "w").write(OSASCRIPT)
    os.chmod(os.path.join(stub, "osascript"), 0o755)
    lume = os.path.join(stub, "lume")
    open(lume, "w").write('#!/bin/sh\ncase "$1" in get) echo \'{"name": "hvtest", "status": "running"}\' ;;\n'
                          'ls) echo \'[{"name": "hvtest", "status": "running"}]\' ;; esac\n')
    os.chmod(lume, 0o755)
    ssh = os.path.join(vd, "ssh")
    os.makedirs(ssh, mode=0o700)
    return vd, stub, lume, ssh


def view(vd, stub, lume, ssh, *args, store="file", extra_cfg="", stdin=None, **extra):
    cfg = os.path.join(vd, "test.env")
    open(cfg, "w").write(f'VM_NAME=hvtest\nBASELINE_NAME=hvtest-baseline\nSSH_DIR="{ssh}"\nSSH_PORT=59872\n'
                         f'VNC_PORT=59871\nRUN_DIR="{vd}/run"\nLOG_DIR="{vd}/logs"\nLUME="{lume}"\n'
                         f'VNC_PASSWORD_STORE={store}\n{extra_cfg}')
    e = env(MCOMP_CONFIG=cfg, STUB=stub, PATH=stub + os.pathsep + os.environ.get("PATH", ""),
            MCOMP_VIEW_CLIPBOARD_S=extra.pop("clear", 1), **extra)
    for k in ("MCOMP_RUN_DIR", "MCOMP_LOG_DIR", "MCOMP_SSH_DIR", "MCOMP_VM_NAME"):
        e.pop(k, None)
    return subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "view"] + list(args), env=e, stdin=stdin,
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
            if f not in ("clip", "keychain", "vnc-pass", "secret.txt", "count", "kc.pw"):
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
    check("the clipboard is set and cleared through the pasteboard's change count (one step each), never "
          "pbcopy or pbpaste", not any(l.split()[0] in ("pbcopy", "pbpaste") for l in read(argv).splitlines())
          and "clear 8 at 8" in read(os.path.join(stub, "clears.log")), (read(argv)[-300:],
                                                                         read(os.path.join(stub, "clears.log"))))

    def user_copies(text):                   # what another app's copy does to the pasteboard
        n = int(read(os.path.join(stub, "count")) or 7)
        open(clip, "w").write(text)
        open(os.path.join(stub, "count"), "w").write(str(n + 1))
    r = view(vd, stub, lume, ssh, "--open", clear=3)
    time.sleep(0.5)
    user_copies("something the user copied")
    time.sleep(4)
    check("a clipboard the user changed meanwhile is left alone", r.returncode == 0
          and read(clip) == "something the user copied", read(clip))
    r = view(vd, stub, lume, ssh, "--open", clear=3)
    time.sleep(0.5)
    user_copies(VNC_PW)
    time.sleep(4)
    check("the same text copied again by the user is left alone too (the change count decides, not the text)",
          r.returncode == 0 and read(clip) == VNC_PW, read(clip))
    # The Keychain store: `security ... -w` prints it into the pipe; its arguments name the item, never the password.
    os.remove(os.path.join(ssh, "vnc-pass"))
    open(os.path.join(stub, "keychain"), "w").write("Kc9Pw4Lm\n")
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open", store="keychain", clear=0)
    sec = [l for l in read(argv).splitlines() if l.startswith("security ")]
    check("the Keychain store: read with security -w into the pipe, onto the clipboard", r.returncode == 0
          and read(clip) == "Kc9Pw4Lm" and any("-s metacomputer-vnc -a hvtest -w" in x for x in sec)
          and "Kc9Pw4Lm" not in read(argv) + r.stdout + r.stderr, (sec, r.stdout, r.stderr))
    check("--clear-after 0 says nothing about clearing, and leaves it", "cleared" not in r.stdout
          and read(clip) == "Kc9Pw4Lm", r.stdout)
    secret = os.path.join(vd, "secret.txt")
    open(secret, "w").write("Cm5Store\n")
    r = view(vd, stub, lume, ssh, "--open", store="command", clear=0,
             extra_cfg=f'VNC_PASSWORD_GET="cat {secret}"\nVNC_PASSWORD_SET="cat >/dev/null"\n')
    check("your own command's store: onto the clipboard, not the output", r.returncode == 0
          and read(clip) == "Cm5Store" and "Cm5Store" not in r.stdout + r.stderr, (r.stdout, r.stderr))
    # The driver's leftover input devices go before Screen Sharing opens (lib/input_reset.py; its rules are
    # input_reset_checks'): the guest's input-reset over SSH (the stand-in answers $STUB/reset_out).
    open(os.path.join(stub, "reset_out"), "w").write("1\n")
    open(argv, "w").close()
    stamp = os.path.join(vd, "run", "owners", owners.INPUT_RESET_FILE)
    r = view(vd, stub, lume, ssh, "--open", stdin=subprocess.DEVNULL)
    order = [l.split()[0] + (" " + l.split()[1] if l.startswith("ssh") else "") for l in read(argv).splitlines()
             if l.startswith(("ssh", "open"))]
    check("view --open removes the driver's leftover input devices before Screen Sharing opens, says so, and records "
          "it for the hub", r.returncode == 0 and "Removed 1 leftover virtual input device pair(s)" in r.stdout
          and order == ["ssh input-reset", "open"] and os.path.exists(stamp), (r.stdout, order))
    now = time.time()
    os.makedirs(os.path.join(vd, "run", "owners"), exist_ok=True)
    with open(os.path.join(vd, "run", "owners", owners.HANDOFF_FILE), "w") as f:
        json.dump({"v": 1, "id": "h-ffffffffffff", "status": "open", "why": "login", "say": "x", "opened": now,
                   "deadline": now + 60, "timeout_s": 60, "cwd": vd, "claude_pid": None}, f)
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open", stdin=subprocess.DEVNULL)
    os.remove(os.path.join(vd, "run", "owners", owners.HANDOFF_FILE))
    check("...not while a handoff is open, from anything but the user's own terminal (the dialog's button, an "
          "agent): opening the handoff did it; Screen Sharing still opens", r.returncode == 0
          and "ssh input-reset" not in read(argv) and "open vnc://127.0.0.1:59871" in read(argv)
          and "virtual input" not in r.stdout, (r.stdout, read(argv)))
    open(os.path.join(stub, "reset_out"), "w").write("0\n")

    # Over SSH, or outside the screen session: instructions instead, nothing touched.
    open(clip, "w").write("untouched")
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open", store="keychain", SSH_CONNECTION="10.0.0.2 5000 10.0.0.1 22")
    check("over SSH: exit 1 with how to do it instead (the Mac's screen, the tunnel, --tailnet)", r.returncode == 1
          and "came in over SSH" in r.stdout and "ssh -N -L 59871:127.0.0.1:59871" in r.stdout
          and "view --tailnet" in r.stdout and "metacomputer view --open" in r.stdout, (r.stdout, r.stderr))
    check("over SSH: no clipboard, no Screen Sharing, no password read", read(clip) == "untouched"
          and not any(l.split()[0] in ("pbcopy", "open", "security", "osascript") for l in read(argv).splitlines()),
          read(argv))
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

    open(os.path.join(stub, "race"), "w").close()
    open(os.path.join(stub, "clears.log"), "w").close()
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "liveview.py"), "clip", "--clear-after", "1"],
                       input=VNC_PW + "\n", env=env(STUB=stub, PATH=stub + os.pathsep + os.environ["PATH"]),
                       capture_output=True, text=True, timeout=30)
    time.sleep(2)
    os.remove(os.path.join(stub, "race"))
    check("a copy that raced another one isn't cleared later (the clipboard may hold someone else's copy), and it "
          "says so", r.returncode == 0 and "won't be cleared" in r.stderr
          and read(os.path.join(stub, "clears.log")) == "", (r.returncode, r.stderr))

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


# --- Screen Sharing's remembered password (view, lib/liveview.py remember) -----------------------------------
def view_tty(vd, stub, lume, ssh, *args, answer=None, store="file", extra_cfg="", **extra):
    """bin/metacomputer view at a terminal (stdin and stdout a pty): answers the question with `answer` when it's
    asked. Returns (exit status, everything the terminal showed, whether the question was asked)."""
    cfg = os.path.join(vd, "test.env")
    open(cfg, "w").write(f'VM_NAME=hvtest\nBASELINE_NAME=hvtest-baseline\nSSH_DIR="{ssh}"\nSSH_PORT=59872\n'
                         f'VNC_PORT=59871\nRUN_DIR="{vd}/run"\nLOG_DIR="{vd}/logs"\nLUME="{lume}"\n'
                         f'VNC_PASSWORD_STORE={store}\n{extra_cfg}')
    e = env(MCOMP_CONFIG=cfg, STUB=stub, PATH=stub + os.pathsep + os.environ.get("PATH", ""),
            MCOMP_VIEW_CLIPBOARD_S="0", **extra)
    for k in ("MCOMP_RUN_DIR", "MCOMP_LOG_DIR", "MCOMP_SSH_DIR", "MCOMP_VM_NAME"):
        e.pop(k, None)
    m, s = pty.openpty()
    # as the user at their own terminal: no agent session running this test is above it (tests/userterm.py)
    p = userterm.spawn([os.path.join(LAB, "bin", "metacomputer"), "view"] + list(args), env=e, stdin=s, stdout=s,
                       stderr=s)
    os.close(s)
    buf, asked, end = b"", False, time.time() + 60
    while time.time() < end:
        try:
            ready, _, _ = select.select([m], [], [], 0.2)
        except (OSError, ValueError):
            break
        if ready:
            try:
                chunk = os.read(m, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buf += chunk
            if not asked and b"Save it? [Y/n]" in buf:
                asked = True
                os.write(m, (answer if answer is not None else "\n").encode())
        elif p.poll() is not None:
            break
    p.wait(timeout=30)
    os.close(m)
    return p.returncode, buf.decode("utf-8", "replace"), asked


def remember_checks():
    print("view --open: Screen Sharing's remembered password (stand-ins for osascript and security)")
    if os.stat("/dev/console").st_uid != os.getuid():
        print("SKIP the remembered password's checks: this Mac's screen isn't this user's session right now")
        return
    vd, stub, lume, ssh = view_setup("remember")
    clip, argv, item = os.path.join(stub, "clip"), os.path.join(stub, "argv.log"), os.path.join(stub, "kc.item")
    open(os.path.join(ssh, "vnc-pass"), "w").write(VNC_PW + "\n")
    os.chmod(os.path.join(ssh, "vnc-pass"), 0o600)
    fake_kc = os.path.join(vd, "login.keychain-db")
    open(fake_kc, "w").close()
    open(os.path.join(stub, "kcpath"), "w").write(fake_kc)
    label = "metacomputer live view (127.0.0.1:59871)"

    def lines(prefix):
        return [x for x in read(argv).splitlines() if x.startswith(prefix)]

    def clipped():
        return any(" copy" in x and "JavaScript" in x for x in lines("osascript "))

    open(argv, "w").close()
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--open", answer="\n")
    saved = read(item).splitlines()
    check("the first time, at a terminal: it asks; Enter is yes", asked and rc == 0, (rc, out))
    check("…the password goes to the keychain item on osascript's stdin, for Screen Sharing's address",
          read(os.path.join(stub, "kc.pw")) == VNC_PW and saved[:2] == ["127.0.0.1:59871", label]
          and saved[2].startswith("mcomp-") and saved[3] == "/System/Applications/Utilities/Screen Sharing.app"
          and read(os.path.join(stub, "kc.used")).strip() == fake_kc, (saved, read(os.path.join(stub, "kc.used"))))
    check("…the login keychain's path comes from security login-keychain, and nothing else is asked of security",
          lines("security ") == ["security login-keychain"] * len(lines("security ")) and lines("security "),
          lines("security "))
    check("…then Screen Sharing opens with nothing to paste: no clipboard", "Saved the live-view password" in out
          and "nothing to paste" in out and not clipped() and lines("open ") == ["open vnc://127.0.0.1:59871"],
          (out, lines("open ")))
    check("…and the password is in no argument, no output and no log", VNC_PW not in read(argv) and VNC_PW not in out
          and VNC_PW not in everything_but_clip(vd), out)
    tag1 = saved[2]
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open")
    check("once saved, view --open without a terminal brings it up to date in place (a new tag), no question, "
          "through the helper's refresh (which never makes one)",
          r.returncode == 0 and read(item).splitlines()[2] != tag1 and "Save it?" not in r.stdout
          and any(" refresh " in x for x in lines("osascript ")) and not any(" save " in x for x in lines("osascript "))
          and "nothing to paste" in r.stdout and not clipped() and read(os.path.join(stub, "kc.pw")) == VNC_PW,
          (r.stdout, r.stderr))
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open", "--no-remember")
    check("--no-remember: the clipboard this once, the keychain untouched", r.returncode == 0 and clipped()
          and not any(" save " in x for x in lines("osascript ")) and "clipboard" in r.stdout, (r.stdout, r.stderr))
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open", extra_cfg="VIEW_REMEMBER=off\n")
    check("VIEW_REMEMBER=off with one saved earlier: not touched, says --forget removes it, the clipboard instead",
          r.returncode == 0 and clipped() and not any(" save " in x for x in lines("osascript "))
          and "view --forget" in r.stdout, r.stdout)
    tag = read(item).splitlines()[2]
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--remember", extra_cfg="VIEW_REMEMBER=off\n")
    check("an item already there, VIEW_REMEMBER=off, view --remember without a terminal: refused, the item untouched",
          r.returncode != 0 and "yours to run" in r.stderr and read(item).splitlines()[2] == tag
          and not any(" save " in x for x in lines("osascript ")), (r.returncode, r.stderr))
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--remember", extra_cfg="VIEW_REMEMBER=off\n")
    check("…and at a terminal too: VIEW_REMEMBER=off means no writes at all, and it says how to change that",
          rc != 0 and "VIEW_REMEMBER=off" in out and "VIEW_REMEMBER=ask" in out and read(item).splitlines()[2] == tag,
          (rc, out))
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--open", "--remember", extra_cfg="VIEW_REMEMBER=off\n")
    check("…and view --open --remember with it off: refused before anything opens", rc != 0
          and read(item).splitlines()[2] == tag and not lines("open "), (rc, out))
    r = view(vd, stub, lume, ssh, "--remember")
    check("an item already there, view --remember without a terminal (VIEW_REMEMBER=ask): refused, untouched",
          r.returncode != 0 and "yours to run" in r.stderr and read(item).splitlines()[2] == tag, r.stderr)
    r = view(vd, stub, lume, ssh, "--forget")
    check("--forget without a terminal: refused, the item stays", r.returncode != 0 and "yours to run" in r.stderr
          and os.path.exists(item), (r.returncode, r.stderr))
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--forget")
    check("--forget at a terminal removes it (and says so)", rc == 0 and not os.path.exists(item)
          and "Removed the live-view password" in out, (rc, out))
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--forget")
    check("--forget with none: says there's none", rc == 0 and "has no live-view password" in out, out)
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open")
    check("no terminal and nothing saved (Claude, a script, the dialog's button): nothing saved, the clipboard, and "
          "a hint", r.returncode == 0 and not os.path.exists(item) and clipped()
          and "metacomputer view --remember" in r.stdout and "Save it?" not in r.stdout, r.stdout)
    r = view(vd, stub, lume, ssh, "--remember")
    check("view --remember without a terminal is refused: it's the user's to run", r.returncode != 0
          and "yours to run" in r.stderr and not os.path.exists(item), (r.returncode, r.stderr))
    open(argv, "w").close()
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--open", answer="n\n")
    cfg_now = read(os.path.join(vd, "test.env"))
    check("a no: nothing saved, VIEW_REMEMBER=off written to the settings, the clipboard", asked and rc == 0
          and not os.path.exists(item) and "VIEW_REMEMBER=off" in cfg_now and "not asked again" in out and clipped(),
          (out, cfg_now))
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--open", extra_cfg="VIEW_REMEMBER=off\n")
    check("…and with VIEW_REMEMBER=off it isn't asked again", rc == 0 and not asked and not os.path.exists(item), out)
    r = view(vd, stub, lume, ssh, "--open", extra_cfg="VIEW_REMEMBER=on\n")
    check("VIEW_REMEMBER=on: saved without a question, even without a terminal", r.returncode == 0
          and read(item).splitlines()[:2] == ["127.0.0.1:59871", label] and "nothing to paste" in r.stdout, r.stdout)
    os.remove(item)
    open(argv, "w").close()
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--remember")
    check("view --remember at a terminal: saved, no question, nothing opened", rc == 0 and not asked
          and os.path.exists(item) and not lines("open ") and "view --open" in out, (rc, out))
    tag = read(item).splitlines()[2]
    open(os.path.join(stub, "kc.fail"), "w").close()
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--remember")
    check("an explicit --remember the keychain refuses (locked): an error, with the reason", rc != 0
          and "locked" in out and "wasn't saved" in out and read(item).splitlines()[2] == tag, (rc, out))
    open(argv, "w").close()
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--open", "--remember")
    check("view --open --remember that can't save: the live view opens with the clipboard, and the command fails",
          rc != 0 and "locked" in out and clipped() and lines("open ") == ["open vnc://127.0.0.1:59871"], (rc, out))
    os.remove(os.path.join(stub, "kc.fail"))
    os.rename(os.path.join(stub, "kcpath"), os.path.join(stub, "kcpath.off"))
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--remember")
    os.rename(os.path.join(stub, "kcpath.off"), os.path.join(stub, "kcpath"))
    check("an explicit --remember when the login keychain can't be found: an error, with the reason", rc != 0
          and "login keychain" in out and "wasn't saved" in out, (rc, out))
    open(os.path.join(stub, "kc.fail"), "w").close()
    open(argv, "w").close()
    r = view(vd, stub, lume, ssh, "--open")
    os.remove(os.path.join(stub, "kc.fail"))
    check("a keychain that won't take it (locked): says why, and the clipboard instead", r.returncode == 0
          and clipped() and "locked" in r.stderr and "clipboard" in r.stdout, (r.stdout, r.stderr))
    os.remove(item)
    open(argv, "w").close()
    rc, out, asked = view_tty(vd, stub, lume, ssh, answer="\n")
    check("plain view at a terminal on the Mac's screen opens the live view (asking first)", rc == 0 and asked
          and lines("open ") == ["open vnc://127.0.0.1:59871"] and os.path.exists(item), (rc, out))
    open(argv, "w").close()
    rc, out, asked = view_tty(vd, stub, lume, ssh, SSH_CONNECTION="10.0.0.2 5000 10.0.0.1 22")
    check("plain view over SSH, even at a terminal: how to connect, nothing opened or asked", rc == 0 and not asked
          and "ssh -N -L 59871:127.0.0.1:59871" in out and not lines("open ") and not lines("osascript "), out)
    r = view(vd, stub, lume, ssh, "--print")
    check("view --print: how to connect, nothing opened", r.returncode == 0 and "ssh -N -L 59871" in r.stdout
          and not lines("open "), r.stdout)
    rc, out, asked = view_tty(vd, stub, lume, ssh, "--open", extra_cfg="VIEW_REMEMBER=maybe\n")
    check("view --open with a VIEW_REMEMBER that isn't ask, on or off: counts as off (says so), no question, no write",
          rc == 0 and not asked and "counts as off" in out and not any(" save " in x or " refresh " in x
                                                                        for x in lines("osascript ")), out)
    for bad in (["--open", "--tailnet"], ["--remember", "--no-remember"], ["--tailnet", "--remember"]):
        r = view(vd, stub, lume, ssh, *bad)
        check(f"view {' '.join(bad)}: a usage error", r.returncode != 0 and "usage" in r.stderr, r.stderr)


def remember_helper_checks():
    """lib/liveview.py's remember, refresh and forget called directly (not through bin/metacomputer view): the same
    consent and VIEW_REMEMBER rules, applied by the helper itself."""
    print("lib/liveview.py remember | refresh | forget called directly: its own consent and VIEW_REMEMBER checks")
    vd, stub, lume, ssh = view_setup("helper")
    item = os.path.join(stub, "kc.item")
    fake_kc = os.path.join(vd, "login.keychain-db")
    open(fake_kc, "w").close()
    open(os.path.join(stub, "kcpath"), "w").write(fake_kc)
    cfg = os.path.join(vd, "test.env")
    lv = [sys.executable, os.path.join(LAB, "lib", "liveview.py")]

    def call(*args, setting=None, conf="", tty=False):
        open(cfg, "w").write(f"VM_NAME=hvtest\nVNC_PORT=59871\n{conf}")
        e = env(MCOMP_CONFIG=cfg, STUB=stub, PATH=stub + os.pathsep + os.environ.get("PATH", ""),
                MCOMP_VIEW_REMEMBER=setting)
        open(os.path.join(stub, "argv.log"), "w").close()
        if not tty:
            return subprocess.run(lv + list(args), input=VNC_PW + "\n", env=e, capture_output=True, text=True,
                                  timeout=60)
        # At a terminal: wait for the prompt (echo is turned off before it's written), look at the terminal's echo
        # flag, type the password, then collect what the terminal showed: with echo on, the password would be there.
        m, s = pty.openpty()
        p = userterm.spawn(lv + list(args), env=e, stdin=s, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        err, end = b"", time.time() + 30
        while time.time() < end and b"isn't shown" not in err:
            ready, _, _ = select.select([p.stderr], [], [], 0.2)
            if ready:
                chunk = os.read(p.stderr.fileno(), 4096)
                if not chunk:
                    break
                err += chunk
        echo_off = b"isn't shown" in err and not termios.tcgetattr(s)[3] & termios.ECHO
        os.write(m, (VNC_PW + "\n").encode())
        o, rest = p.communicate(timeout=60)
        shown = b""
        while True:
            ready, _, _ = select.select([m], [], [], 0.3)
            if not ready:
                break
            try:
                chunk = os.read(m, 4096)
            except OSError:
                break
            if not chunk:
                break
            shown += chunk
        echo_back = bool(termios.tcgetattr(s)[3] & termios.ECHO)
        os.close(s)
        os.close(m)
        res = subprocess.CompletedProcess(p.args, p.returncode, o.decode(), (err + rest).decode())
        res.echo_off, res.shown, res.echo_back = echo_off, shown.decode("utf-8", "replace"), echo_back
        return res

    def wrote():
        return any(f" {op} " in x for x in read(os.path.join(stub, "argv.log")).splitlines()
                   for op in ("save", "refresh", "forget"))

    r = call("remember", "--port", "59871")
    check("remember with the password piped in and no --from-view: refused, the keychain never reached",
          r.returncode == 1 and "needs your yes" in r.stderr and not wrote() and not os.path.exists(item), r.stderr)
    r = call("remember", "--port", "59871", "--from-view", setting="off")
    check("remember --from-view with MCOMP_VIEW_REMEMBER=off: refused", r.returncode == 1
          and "VIEW_REMEMBER is off" in r.stderr and not wrote(), r.stderr)
    r = call("remember", "--port", "59871", "--from-view", setting="on", conf="VIEW_REMEMBER=off\n")
    check("…and with the settings file's VIEW_REMEMBER=off, an environment that says on can't loosen it",
          r.returncode == 1 and "VIEW_REMEMBER is off" in r.stderr and not wrote(), r.stderr)
    r = call("remember", "--port", "59871", "--from-view", setting="yes please")
    check("an unknown MCOMP_VIEW_REMEMBER counts as off (fails closed)", r.returncode == 1
          and "VIEW_REMEMBER is off" in r.stderr and not wrote(), r.stderr)
    r = call("refresh", "--port", "59871", setting="ask")
    check("refresh with nothing saved: makes nothing (exit 1, says so)", r.returncode == 1
          and "no saved live-view password" in r.stderr and not os.path.exists(item), (r.returncode, r.stderr))
    r = call("refresh", "--port", "59871", conf="VIEW_REMEMBER=maybe\n")
    r2 = call("remember", "--port", "59871", "--from-view", conf="VIEW_REMEMBER=maybe\n")
    check("a VIEW_REMEMBER in the settings file that isn't ask, on or off counts as off (fails closed)",
          r.returncode == 1 and r2.returncode == 1 and "VIEW_REMEMBER is off" in r.stderr + r2.stderr
          and r.stderr.count("VIEW_REMEMBER is off") == 1 and not wrote(), (r.stderr, r2.stderr))
    r = call("refresh", "--port", "59871", conf="VIEW_REMEMBER=\n")
    check("…while an empty one means ask, as unset does", r.returncode == 1 and "VIEW_REMEMBER is off" not in r.stderr
          and "no saved live-view password" in r.stderr, r.stderr)
    r = call("remember", "--port", "59871", tty=True)
    check("remember with the password typed at a terminal (stdin is one): allowed", r.returncode == 0
          and r.stdout.strip() == "created" and os.path.exists(item), (r.returncode, r.stdout, r.stderr))
    check("…typed with echo off on that terminal, so the password never shows there, and echo back on afterwards",
          r.echo_off and VNC_PW not in r.shown and r.echo_back and read(os.path.join(stub, "kc.pw")) == VNC_PW,
          (r.echo_off, r.shown, r.echo_back, r.stderr))
    tag = read(item).splitlines()[2]
    r = call("refresh", "--port", "59871", setting="ask")
    check("refresh with metacomputer's item there: updated in place, no terminal needed", r.returncode == 0
          and r.stdout.strip() == "updated" and read(item).splitlines()[2] != tag, (r.returncode, r.stderr))
    tag = read(item).splitlines()[2]
    r = call("refresh", "--port", "59871", conf="VIEW_REMEMBER=off\n")
    r2 = call("refresh", "--port", "59871", setting="off")
    check("refresh while VIEW_REMEMBER is off (settings file or environment): refused, the item untouched",
          r.returncode == 1 and r2.returncode == 1 and read(item).splitlines()[2] == tag and not wrote(),
          (r.stderr, r2.stderr))
    r = call("forget", "--port", "59871")
    check("forget with no terminal and no --from-view: refused, the item stays", r.returncode == 1
          and "needs your yes" in r.stderr and os.path.exists(item) and not wrote(), r.stderr)
    r = call("forget", "--port", "59871", "--from-view")
    check("forget --from-view (what bin/metacomputer passes after its own check): removed", r.returncode == 0
          and r.stdout.strip() == "removed" and not os.path.exists(item), (r.returncode, r.stderr))
    for bad in (["remember", "--port", "59871", "--json"], ["refresh", "--port", "59871", "--from-view"],
                ["remembered", "--port", "59871", "--from-view"]):
        r = call(*bad)
        check(f"liveview.py {' '.join(bad)}: a usage error", r.returncode == 2 and "usage" in r.stderr, r.stderr)


def keychain_checks():
    """lib/liveview.py's REMEMBER_JS with the real osascript and Security framework, in a keychain made for this test
    inside its temporary folder (HOME too, so not even the keychain search list changes). The login keychain is
    never touched: `security` is a stand-in that answers login-keychain with the test keychain and refuses the rest,
    and nothing here reads a password back."""
    print("Screen Sharing's item in a throwaway keychain (real osascript and Security framework)")
    real = "/usr/bin/security"
    if sys.platform != "darwin" or not os.path.exists(real) or not shutil.which("osascript"):
        print("SKIP: needs macOS's security and osascript")
        return
    kd = os.path.join(TD, "kc")
    home, stub = os.path.join(kd, "home"), os.path.join(kd, "stub")
    for d in (home, stub):
        os.makedirs(d, mode=0o700)
    kc = os.path.join(kd, "test.keychain")
    pwfile = os.path.join(kd, "kc-pass")
    fd = os.open(pwfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, secrets.token_hex(16).encode())
    os.close(fd)
    kenv = env(HOME=home)

    def search_list():
        r = subprocess.run([real, "list-keychains", "-d", "user"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL, env=env())
        return r.stdout
    before = search_list()
    r = subprocess.run([real, "-i"], input=f'create-keychain -p {read(pwfile)} "{kc}"\n', capture_output=True,
                       text=True, timeout=60, env=kenv)
    if not os.path.isfile(kc):
        print(f"SKIP: couldn't make a throwaway keychain ({r.stderr.strip()[:200]})")
        return
    try:
        subprocess.run([real, "set-keychain-settings", kc], capture_output=True, timeout=30, env=kenv,
                       stdin=subprocess.DEVNULL)
        with open(os.path.join(stub, "security"), "w") as f:
            f.write('#!/bin/sh\necho "$*" >> "%s/calls"\ncase "$1" in login-keychain) printf \'    "%%s"\\n\' "%s" ;; '
                    '*) echo "the test\'s security stand-in refuses: $1" >&2; exit 99 ;; esac\n' % (stub, kc))
        os.chmod(os.path.join(stub, "security"), 0o755)
        senv = env(HOME=home, PATH=stub + os.pathsep + os.environ.get("PATH", ""))
        lv = [sys.executable, os.path.join(LAB, "lib", "liveview.py")]
        fake = "Tk" + secrets.token_hex(3)

        def lvrun(*a, data=None):
            return subprocess.run(lv + list(a), input=data, capture_output=True, text=True, timeout=60, env=senv,
                                  stdin=None if data is not None else subprocess.DEVNULL)
        r = lvrun("refresh", "--port", "59871", data=fake + "\n")
        r0 = lvrun("remembered", "--port", "59871")
        check("refresh with nothing there: makes nothing (exit 1)", r.returncode == 1 and r0.returncode == 1,
              (r.returncode, r.stdout, r.stderr, r0.returncode))
        r = lvrun("remember", "--port", "59871", data=fake + "\n")
        check("remember without a terminal or --from-view: refused, nothing made", r.returncode == 1
              and "needs your yes" in r.stderr and lvrun("remembered", "--port", "59871").returncode == 1, r.stderr)
        r = lvrun("remember", "--port", "59871", "--from-view", data=fake + "\n")
        check("remember: the item is created in the keychain it was given", r.returncode == 0
              and r.stdout.strip() == "created", (r.returncode, r.stdout, r.stderr))
        r = lvrun("remembered", "--port", "59871", "--json")
        a = json.loads(r.stdout or "{}") if r.stdout.strip().startswith("{") else {}
        check("its attributes are Screen Sharing's key for vnc://127.0.0.1:59871: server 127.0.0.1:59871, port 0, "
              "type 'vncl', no account; metacomputer's label and a one-time tag", r.returncode == 0
              and a.get("server") == "127.0.0.1:59871" and a.get("port") == "0" and a.get("type") == "1986945900"
              and a.get("account") is None and a.get("label") == "metacomputer live view (127.0.0.1:59871)"
              and str(a.get("comment", "")).startswith("mcomp-"), (r.returncode, r.stdout, r.stderr))
        tag1 = a.get("comment")
        r = lvrun("refresh", "--port", "59871", data="Ot" + secrets.token_hex(3) + "\n")
        a2 = json.loads(lvrun("remembered", "--port", "59871", "--json").stdout or "null") or {}
        check("refresh: updated in place (the same key, a new tag)", r.returncode == 0
              and r.stdout.strip() == "updated" and a2.get("comment") not in (None, tag1), (r.stdout, r.stderr, a2))
        r = lvrun("remember", "--port", "59871", "--from-view", data="Ot" + secrets.token_hex(3) + "\n")
        a3 = json.loads(lvrun("remembered", "--port", "59871", "--json").stdout or "null") or {}
        check("remember again: updated in place too", r.returncode == 0 and r.stdout.strip() == "updated"
              and a3.get("comment") not in (None, tag1, a2.get("comment")), (r.stdout, r.stderr, a3))
        r = lvrun("remembered", "--port", "59872")
        check("another port isn't that item (exit 1)", r.returncode == 1, (r.returncode, r.stderr))
        r = lvrun("forget", "--port", "59871")
        check("forget without a terminal or --from-view: refused, the item stays", r.returncode == 1
              and lvrun("remembered", "--port", "59871").returncode == 0, r.stderr)
        r = lvrun("forget", "--port", "59871", "--from-view")
        r2 = lvrun("remembered", "--port", "59871")
        r3 = lvrun("forget", "--port", "59871", "--from-view")
        check("forget removes it, so there was exactly one; a second forget finds none", r.stdout.strip() == "removed"
              and r2.returncode == 1 and r3.stdout.strip() == "none", (r.stdout, r.stderr, r2.returncode, r3.stdout))
        calls = read(os.path.join(stub, "calls")).split("\n")
        check("security was asked only for the login keychain's path (no find, dump or -w)",
              set(c for c in calls if c) == {"login-keychain"}, calls)
        check("the password never reached an argument or the output", fake not in json.dumps(calls) + r.stdout)
    finally:
        subprocess.run([real, "delete-keychain", kc], capture_output=True, timeout=30, env=kenv,
                       stdin=subprocess.DEVNULL)
    check("the throwaway keychain is gone and the user's keychain search list is as it was",
          not os.path.exists(kc) and search_list() == before and kc not in before, search_list())


# --- the handoff's dialog (INTERACTIVE_PROMPTS) --------------------------------------------------------------
def dialog_checks():
    print("handoff's dialog: INTERACTIVE_PROMPTS, the text as data, Open live view (a stand-in osascript)")
    if os.stat("/dev/console").st_uid != os.getuid():
        print("SKIP the dialog's checks: this Mac's screen isn't this user's session right now")
        return
    vd, stub, lume, ssh = view_setup("dialog")
    open(os.path.join(ssh, "vnc-pass"), "w").write(VNC_PW + "\n")
    os.chmod(os.path.join(ssh, "vnc-pass"), 0o600)
    cfg = os.path.join(vd, "test.env")
    open(cfg, "w").write(f'VM_NAME=hvtest\nBASELINE_NAME=hvtest-baseline\nSSH_DIR="{ssh}"\nSSH_PORT=59872\n'
                         f'VNC_PORT=59871\nRUN_DIR="{vd}/run"\nLOG_DIR="{vd}/logs"\nLUME="{lume}"\n'
                         f'VNC_PASSWORD_STORE=file\n')
    path = stub + os.pathsep + os.environ.get("PATH", "")
    argv = os.path.join(stub, "argv.log")

    def dfile(name):
        return read(os.path.join(stub, name))

    def reset_stub():
        for f in ("dialog.script", "dialog.text", "dialog.args", "dialog.pid", "dialog.answer", "dialog.ended"):
            try:
                os.remove(os.path.join(stub, f))
            except FileNotFoundError:
                pass
        open(argv, "w").close()

    def opened(say, prompts="on", **extra):
        clear()
        reset_stub()
        p = start("--why", "captcha", "--say", say, "--timeout", "120", MCOMP_INTERACTIVE_PROMPTS=prompts,
                  MCOMP_CONFIG=cfg, STUB=stub, PATH=path, MCOMP_VIEW_CLIPBOARD_S="0", **extra)
        wait_open()
        return p

    def wait_for(fn, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.1)
        return False

    def end_it(p):
        run("cancel", claude=None)
        return finish(p)

    p = opened(SAY, prompts=None)
    time.sleep(1.5)
    rc, o, e = end_it(p)
    check("INTERACTIVE_PROMPTS unset (off): no dialog, and the output says how to open the live view",
          not dfile("dialog.args") and "metacomputer view --open" in o and "A dialog" not in o, o)
    p = opened(SAY, prompts="off")
    time.sleep(1.5)
    rc, o, e = end_it(p)
    check("INTERACTIVE_PROMPTS=off: no dialog either", not dfile("dialog.args") and "A dialog" not in o, o)

    nasty = ('Tick "Verify" \\ then " & (do shell script "touch ' + os.path.join(vd, "canary") + '") & " '
             'end run tell application "Finder" to quit -e x')
    p = opened(nasty)
    shown = wait_for(lambda: dfile("dialog.args"))
    t0 = time.time()
    rc, o, e = end_it(p)
    check("on: a dialog is shown, and handoff says so", shown and "A dialog on the Mac's screen" in o, (o, e))
    check("…handoff didn't wait for it: it ended as soon as the handoff did, while the dialog was still up",
          rc == 4 and time.time() - t0 < 10, rc)
    check("…osascript got the fixed script, then show, the --say text exactly as data, the seconds left and who asks",
          dfile("dialog.script") == handoff.DIALOG_SCRIPT and dfile("dialog.text") == handoff.dialog_text(nasty)
          and dfile("dialog.args").split()[0] == "show" and dfile("dialog.args").split()[2:] == ["6", "Claude"]
          and 1 <= int(dfile("dialog.args").split()[1]) <= 120, (dfile("dialog.args"), dfile("dialog.text")))
    check("…and the dialog is taken down when the handoff ends", wait_for(lambda: dfile("dialog.pid") and not
                                                                           pid_alive(int(dfile("dialog.pid"))), 8))
    r = subprocess.run(["osascript", "-e", handoff.DIALOG_SCRIPT, "compose", handoff.dialog_text(nasty), "5", "Claude"],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    check("the real osascript, in the script's compose mode (no dialog): the text comes back verbatim after "
          "\"Claude needs you: \", and nothing in it ran (quotes, backslashes, do shell script, end run, tell)",
          r.returncode == 0 and r.stdout.rstrip("\n") == "Claude needs you: " + handoff.dialog_text(nasty)
          and not os.path.exists(os.path.join(vd, "canary")), (r.returncode, r.stdout, r.stderr))
    r = subprocess.run(["osascript", "-e", handoff.DIALOG_SCRIPT, "compose", "Tick the box", "5", "Codex"],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    check("...and a handoff a Codex session opened says Codex", r.stdout.rstrip("\n") == "Codex needs you: Tick the box",
          (r.returncode, r.stdout, r.stderr))
    check("...which the record decides (agent_word)", handoff.agent_word({"agent": "Codex"}) == "Codex"
          and handoff.agent_word({"agent": "Claude Code"}) == "Claude" and handoff.agent_word({}) == "Claude"
          and handoff.agent_word({"agent": "Evil\nname"}) == "Claude")
    long_say = "x" * 600
    check("the dialog's text is cleaned and capped (240 characters)", len(handoff.dialog_text(long_say)) == 240
          and handoff.dialog_text("a\x1b[31mb\nc") == "a [31mb c", handoff.dialog_text("a\x1b[31mb\nc"))

    p = opened(SAY)
    wait_for(lambda: dfile("dialog.args"))
    open(os.path.join(stub, "dialog.answer"), "w").write("Open live view\n")
    got = wait_for(lambda: "open vnc://127.0.0.1:59871" in read(argv), 30)
    rc, o, e = end_it(p)
    check("Open live view runs metacomputer view --open (Screen Sharing at the live view; no terminal, so the "
          "clipboard here, nothing saved)", got and not os.path.exists(os.path.join(stub, "kc.item"))
          and read(os.path.join(stub, "clip")) == VNC_PW, read(argv)[-400:])
    check("…and the password stays out of every argument and log", VNC_PW not in read(argv)
          and VNC_PW not in everything_but_clip(vd))
    p = opened(SAY)
    wait_for(lambda: dfile("dialog.args"))
    open(os.path.join(stub, "dialog.answer"), "w").write("Later\n")
    wait_for(lambda: dfile("dialog.ended"))
    time.sleep(1)
    rc, o, e = end_it(p)
    check("Later: nothing is opened", "open vnc://" not in read(argv), read(argv)[-300:])

    p = opened(SAY, SSH_CONNECTION="10.0.0.2 5000 10.0.0.1 22")
    time.sleep(1.5)
    rc, o, e = end_it(p)
    check("over SSH: no dialog", not dfile("dialog.args") and "A dialog" not in o, o)
    open(os.path.join(stub, "manager"), "w").write("Background\n")
    p = opened(SAY)
    time.sleep(1.5)
    rc, o, e = end_it(p)
    os.remove(os.path.join(stub, "manager"))
    check("outside the screen session (launchctl: Background): no dialog", not dfile("dialog.args"), o)
    clear()
    reset_stub()
    m, s = pty.openpty()
    p = start("--why", "captcha", "--say", SAY, stdin=s, claude=None, MCOMP_INTERACTIVE_PROMPTS="on",
              MCOMP_CONFIG=cfg, STUB=stub, PATH=path)
    os.close(s)
    wait_open()
    time.sleep(1.5)
    os.write(m, b"c\n")
    rc, o, e = finish(p)
    os.close(m)
    check("a handoff the person at this terminal opened: no dialog", not dfile("dialog.args") and rc == 4, (rc, o))

    # handoff.py _dialog ID (also reachable as `metacomputer handoff _dialog ID`) applies the same rules itself.
    p = opened(SAY, prompts="off")
    hid = owners.open_handoff()["id"]
    common = dict(MCOMP_CONFIG=cfg, STUB=stub, PATH=path)
    r1 = run("_dialog", hid, MCOMP_INTERACTIVE_PROMPTS="off", **common)
    r2 = run("_dialog", hid, MCOMP_INTERACTIVE_PROMPTS=None, **common)
    r3 = run("_dialog", hid, MCOMP_INTERACTIVE_PROMPTS="on", SSH_CONNECTION="10.0.0.2 5000 10.0.0.1 22", **common)
    open(os.path.join(stub, "manager"), "w").write("Background\n")
    r4 = run("_dialog", hid, MCOMP_INTERACTIVE_PROMPTS="on", **common)
    os.remove(os.path.join(stub, "manager"))
    r5 = run("_dialog", "h-000000000000", MCOMP_INTERACTIVE_PROMPTS="on", **common)
    r6 = run("_dialog", "../" + hid, MCOMP_INTERACTIVE_PROMPTS="on", **common)
    check("_dialog run directly shows nothing when INTERACTIVE_PROMPTS is off or unset, over SSH, outside the screen "
          "session, or for an id that isn't the open handoff's", not dfile("dialog.args")
          and all(x.returncode == 0 for x in (r1, r2, r3, r4, r5, r6)),
          [(x.returncode, x.stderr[-200:]) for x in (r1, r2, r3, r4, r5, r6)])
    d = start("_dialog", hid, claude=None, MCOMP_INTERACTIVE_PROMPTS="on", **common)
    shown = wait_for(lambda: dfile("dialog.args"))
    rc, o, e = end_it(p)
    finish(d)
    check("…and shows it when the rules allow (on, the screen session, a handoff Claude opened), until it ends",
          shown and d.returncode == 0, (shown, d.returncode))
    clear()
    reset_stub()
    m, s = pty.openpty()
    p = start("--why", "captcha", "--say", SAY, stdin=s, claude=None, MCOMP_INTERACTIVE_PROMPTS="off", **common)
    os.close(s)
    rec = wait_open()
    r = run("_dialog", rec["id"], MCOMP_INTERACTIVE_PROMPTS="on", **common)
    time.sleep(0.5)
    os.write(m, b"c\n")
    finish(p)
    os.close(m)
    check("…and nothing for a handoff the record says was opened at a terminal, whatever runs _dialog",
          rec.get("interactive") is True and r.returncode == 0 and not dfile("dialog.args"), (rec, r.returncode))


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True
    return bool(st.strip()) and not st.strip().startswith("Z")



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
        shell_write_checks()
        harness_checks()
        lifecycle_checks()
        web_cleanup_checks()
        fill_checks()
        input_reset_checks()
        view_checks()
        remember_checks()
        remember_helper_checks()
        keychain_checks()
        dialog_checks()
    finally:
        clear()
        shutil.rmtree(TD, ignore_errors=True)
    print(f"\n{PASSES[0]}/{PASSES[0] + len(FAILS)} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
