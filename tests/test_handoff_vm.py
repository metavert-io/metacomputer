#!/usr/bin/env python3
"""`metacomputer handoff` against a running test VM, the way Claude uses it (skills/metacomputer/SKILL.md):

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_handoff_vm.py

Claude's side opens a handoff (no terminal, as an agent's shell runs it) and waits. While it's open, an acting
call through the hub (`metacomputer call`) is refused with "Paused: the user has the computer" and reaches nothing,
reads still work, `metacomputer sh` is refused (with or without --override, and --read-only too: the user's own
--read-only at a pseudo-terminal runs), `put`, `ssh` and `rename` are refused (the user's own ssh runs), `secrets fill`,
a fill naming another VM, `input-reset` (but `--check`) and `stop` are refused, `release` leaves it, and the
session that asked can't say "done" for the user. Then the
user's "done" (scripted at a pseudo-terminal, standing in for the user's own terminal) ends it: the waiting
command returns 0 with the user's note, and the same acting call goes through.

The driver's leftover input devices (lib/input_reset.py): a background scroll in Mousepad leaves a "CUA v1.…" pair;
a handoff opened while slow background typing still runs removes nothing (the typing finishes) and says the reset is
pending, and its waiting command removes the pair once that call has ended, the handoff still open; opening the main
handoff (after another background scroll) removes the new pair and says so; the user's own `input-reset` runs during the handoff (an agent's is
refused); after "done" the same driver session's next background input works at once (the hub has a fresh driver
connection; without one, Cua Driver 0.53.0 ends at its next pointer action through the removed pair) and leaves a
new pair, which `input-reset` removes again at the end.

It starts the VM if it's off (and stops it again afterwards), uses the VM's own settings file only, and sends its
events to a temporary folder, so no event sink of yours hears it. A test VM only: it acts on the desktop (the
driver's agent-cursor setting).
"""
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import userterm  # noqa: E402

MC = os.path.join(LAB, "bin", "metacomputer")
FAILS = []
PASSES = [0]
ACT = ("set_agent_cursor_enabled", '{"session": "handoff-vm-test", "enabled": false}')
SESSION = "handoff-vm-test"


def check(name, cond, detail=""):
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:500]})" if detail and not cond else ""), flush=True)


def mc(env, *args, stdin=subprocess.DEVNULL, timeout=180):
    return subprocess.run([MC] + list(args), env=env, stdin=stdin, capture_output=True, text=True, timeout=timeout)


def as_user(env, *args):
    """A command at the user's own terminal: a pseudo-terminal, in a process no agent session is above
    (tests/userterm.py), so this test can be run by an agent too. (returncode, stdout, stderr)"""
    m, s = pty.openpty()
    p = userterm.spawn([MC] + list(args), env=env, stdin=s, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(s)
    try:
        o, e = p.communicate(timeout=180)
    finally:
        os.close(m)
    return p.returncode, o.decode(errors="replace"), e.decode(errors="replace")


def pairs(env):
    r = mc(env, "input-reset", "--check")
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def mousepad(env):
    """Mousepad's window (pid, window_id), launched when it isn't running."""
    for attempt in range(2):
        r = mc(env, "call", "--json", "list_windows", "{}")
        try:
            ws = json.loads(r.stdout).get("structuredContent", {}).get("windows") or []
        except ValueError:
            ws = []
        w = next((w for w in ws if "mousepad" in str(w.get("app_name", "")).lower()), None)
        if w:
            return w["pid"], w["window_id"]
        if attempt == 0:
            mc(env, "call", "launch_app", '{"name": "mousepad"}')
            time.sleep(4)
    return None


def scroll(env, target):
    """A background scroll in Mousepad: Cua Driver sends it through its own virtual pointer (a "CUA v1.…" pair)."""
    t0 = time.time()
    r = mc(env, "call", "--session", SESSION, "--timeout", "60", "scroll",
           json.dumps({"pid": target[0], "window_id": target[1], "direction": "down", "amount": 2}), timeout=120)
    return r, time.time() - t0


def run_dir_of(vm):
    """The test VM's RUN_DIR: its settings file's, else a config/vms/ VM's own folder, else the checkout's .run."""
    cfg = os.environ["MCOMP_CONFIG"]
    for line in open(cfg):
        if line.startswith("RUN_DIR="):
            return os.path.expanduser(line.split("=", 1)[1].strip().strip('"').strip("'"))
    if os.path.basename(os.path.dirname(os.path.abspath(cfg))) == "vms":
        return os.path.join(LAB, ".run-vms", vm)
    return os.path.join(LAB, ".run")


def lock_held(path):
    """Whether something holds the dispatch lock (an exclusive try fails)."""
    try:
        f = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(f)


def ssh_connected(pid):
    """Whether PID is ssh by now (metacomputer ssh exec'd it) with a connection of its own."""
    r = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    return r.stdout.strip().endswith("ssh")


def main():
    if not os.environ.get("MCOMP_CONFIG"):
        print("set MCOMP_CONFIG to a test VM's settings file (this test acts on that VM's desktop)")
        return 2
    td = tempfile.mkdtemp(prefix="mchandoff-")
    env = dict(os.environ, MCOMP_EVENTS_DIR=os.path.join(td, "events"), MCOMP_HANDBACK_PANEL="off")   # no panel on the Mac
    try:
        names_spool = any(line.startswith("EVENTS_SPOOL=") for line in open(os.environ["MCOMP_CONFIG"]))
    except OSError:
        names_spool = False
    if not names_spool and os.path.basename(os.path.dirname(os.path.abspath(os.environ["MCOMP_CONFIG"]))) != "vms":
        # a VM's own settings file that names its spool keeps it (two would refuse); a config/vms/ VM's is fixed
        env["MCOMP_EVENTS_SPOOL"] = os.path.join(td, "spool")
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
        # `metacomputer ssh` with the real OpenSSH (it closes the descriptors it inherits): the dispatch lock is held
        # while it runs (a helper keeps it until ssh's pid exits), its exit status is ssh's, and TERM to its pid ends
        # ssh itself (it's exec'd), the lock going only after.
        dispatch = os.path.join(run_dir_of(vm), "owners", ".dispatch")
        sp = subprocess.Popen([MC, "ssh", "ops", "sleep 30"], env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        held = False
        end = time.time() + 20
        while time.time() < end and not held and sp.poll() is None:
            time.sleep(0.5)
            held = lock_held(dispatch) and ssh_connected(sp.pid)
        seen = {}

        def watch():
            end_w = time.time() + 20
            while time.time() < end_w:
                if not lock_held(dispatch):
                    sp.poll()
                    seen["ssh_alive_at_release"] = sp.returncode is None
                    return
                time.sleep(0.01)
        wt = threading.Thread(target=watch, daemon=True)
        wt.start()
        os.kill(sp.pid, signal.SIGTERM)
        wt.join(25)
        try:
            sp.wait(10)
        except subprocess.TimeoutExpired:
            sp.kill()
        r7 = mc(env, "ssh", "ops", "exit 7")
        check("metacomputer ssh (real OpenSSH): the dispatch lock is held while ssh is connected, TERM to its pid ends ssh "
              "(the lock let go only after), and ssh's exit status is the command's (7)", held
              and seen.get("ssh_alive_at_release") is False and sp.returncode in (-signal.SIGTERM, 255)   # (OpenSSH
              and r7.returncode == 7,                                          # catches TERM and exits 255 itself)
              (held, seen, sp.returncode, r7.returncode, r7.stderr[-200:]))
        target = mousepad(env)
        check("Mousepad runs (the window the background input goes to)", target is not None)
        if target:
            r, took = scroll(env, target)
            check("a background scroll leaves the driver's virtual pointer pair behind", r.returncode == 0
                  and (pairs(env) or 0) >= 1, (r.stdout[-200:], r.stderr[-200:], pairs(env)))
            # A handoff opened while an agent call still runs in the driver (slow background typing) waits 10 s for it,
            # then leaves the devices (removing them could cut the call off) and says the reset is pending.
            box = {}
            typing = threading.Thread(target=lambda: box.update(r=mc(env, "call", "--session", SESSION, "--timeout", "120",
                                                                    "type_text", json.dumps({
                                                                        "pid": target[0], "window_id": target[1],
                                                                        "text": "x" * 300, "delay_ms": 60}))))
            typing.start()
            time.sleep(1.5)
            plog = open(os.path.join(td, "pending.out"), "w+")
            popen = subprocess.Popen([MC, "handoff", "--why", "other", "--say", "pending check", "--timeout", "1m"],
                                     env=env, stdin=subprocess.DEVNULL, stdout=plog, stderr=subprocess.STDOUT)
            end = time.time() + 60
            while time.time() < end:
                plog.seek(0)
                if "Waiting up to" in plog.read():
                    break
                time.sleep(0.25)
            still_pairs = pairs(env)
            typing.join(120)
            # Once that call has ended, the handoff's waiting command makes the pending reset, the handoff still open
            # (with a pair left, Chrome would ignore the live view's keyboard for the whole handoff).
            end = time.time() + 20
            while time.time() < end and pairs(env) != 0:
                time.sleep(0.5)
            after_pairs, still_open = pairs(env), popen.poll() is None
            as_user(env, "handoff", "cancel")
            popen.wait(30)
            plog.seek(0)
            said = plog.read()
            check("a handoff opened while an agent call is still running: nothing removed (the call isn't cut off and "
                  "finishes), and it says the reset is pending and how to make it", (still_pairs or 0) >= 1
                  and "weren't removed" in said and "metacomputer input-reset" in said and box.get("r") is not None
                  and box["r"].returncode == 0, (still_pairs, said[:600], box.get("r") and box["r"].stderr[-200:]))
            check("...and once that call has ended, the waiting handoff removes them, the handoff still open, and says so",
                  after_pairs == 0 and still_open and "leftover virtual input devices were removed" in said,
                  (after_pairs, still_open, said[-400:]))
            r, took = scroll(env, target)            # a pair again, for the main handoff to remove

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
        end = time.time() + 60
        while time.time() < end:
            log.seek(0)
            if "Waiting up to" in log.read():
                break
            time.sleep(0.25)
        log.seek(0)
        said = log.read()
        check("opening it removed the driver's leftover input devices, and says so", pairs(env) == 0
              and "Removed Claude's leftover virtual input devices" in said, (pairs(env), said[:600]))

        r = mc(env, "call", *ACT)
        check("while it's open, an acting call through the hub is refused: the user has the computer",
              r.returncode != 0 and "Paused: the user has the computer" in (r.stdout + r.stderr),
              (r.returncode, r.stdout[-300:], r.stderr[-300:]))
        r = mc(env, "call", "list_windows", "{}")
        check("reads still work", r.returncode == 0, r.stderr[-300:])
        r = mc(env, "sh", "-c", "echo acting")
        r2 = mc(env, "sh", "--override", "-c", "echo acting")
        r3 = mc(env, "sh", "--read-only", "-c", "echo reading")
        check("sh is refused (125) with or without --override, and --read-only too (an agent session's script could "
              "still reach the desktop)", r.returncode == 125 and r2.returncode == 125 and r3.returncode == 125
              and "the user has the computer" in r.stderr + r.stdout and "reading" not in r3.stdout,
              (r.returncode, r2.returncode, r3.returncode, r.stderr[-200:]))
        rc, o, e = as_user(env, "sh", "--read-only", "-c", "echo reading")
        check("...but the user's own --read-only run at their terminal goes ahead", rc == 0 and "reading" in o, (rc, o, e[-200:]))
        upf = os.path.join(td, "held.txt")
        open(upf, "w").write("x")
        r = mc(env, "put", upf)
        r2 = mc(env, "ssh", "ops", "touch /tmp/held-by-handoff")
        r3 = mc(env, "rename", "~/nothing-here.txt", "x.txt")
        rc, o, e = as_user(env, "ssh", "ops", "echo mine; test -e /tmp/held-by-handoff && echo reached || echo held")
        check("put, ssh and rename are refused for the agent (exit 4, the guest isn't reached); the user's own ssh at "
              "their terminal goes ahead", r.returncode == 4 and r2.returncode == 4 and r3.returncode == 4
              and rc == 0 and "mine" in o and "held" in o, (r.returncode, r2.returncode, r3.returncode, rc, o[-200:]))
        # Recording: an agent's record start is held; the user's own at their terminal streams through their handoff,
        # and their record stop saves it (the stream's Mac half is record start's own: nothing asks again).
        r = mc(env, "record", "start", "held-rec", "10")
        rc, o, e = as_user(env, "record", "start", "mine-rec", "10")
        time.sleep(4)
        rc2, o2, e2 = as_user(env, "record", "stop")
        m_dir = re.search(r"streaming to (/\S+)/ ", o + e)
        rec_dir = m_dir.group(1) if m_dir else ""
        saved = sorted(f for f in os.listdir(rec_dir) if f.startswith(("mine-rec.", "held-rec."))) if rec_dir else []
        check("an agent's record start is held (exit 4); the user's own at their terminal streams, and their stop "
              "saves it", r.returncode == 4 and rc == 0 and rc2 == 0 and "mine-rec.mp4" in saved
              and not any(f.startswith("held-rec") for f in saved), (r.returncode, rc, e[-300:], rc2, e2[-300:], saved))
        for f in saved:
            os.remove(os.path.join(rec_dir, f))
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
        rc, o, e = as_user(env, "input-reset")
        check("...but the user's own input-reset runs during their handoff (exit 0, it says why)", rc == 0
              and o.strip().isdigit() and "your handoff is open" in e, (rc, o, e[-300:]))
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

        # The user's "done", from their own terminal (a pseudo-terminal no agent session is above).
        rc, o, e = as_user(env, "handoff", "done", "--note", "ticked it")
        check("the user's done (at a terminal) is taken", rc == 0 and "Handed the virtual computer back"
              in o, (rc, o, e))
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
        if target:
            r, took = scroll(env, target)
            check("the same driver session's next background input works at once and makes a new pair (the hub "
                  "reconnected the driver after the reset)", r.returncode == 0 and took < 30 and (pairs(env) or 0) >= 1,
                  (r.returncode, round(took, 1), r.stdout[-200:], r.stderr[-200:]))
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
            as_user(env, "handoff", "cancel")        # the user calls it off (their terminal)
        mc(env, "call", "end_session", json.dumps({"session": SESSION}))
        mc(env, "input-reset")                       # leave no pair behind
        mc(env, "release")
        if started:
            mc(env, "stop", timeout=300)
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{PASSES[0]}/{PASSES[0] + len(FAILS)} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
