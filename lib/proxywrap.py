#!/usr/bin/env python3
"""ssh's ProxyCommand for a provider's tunnel (lib/cloud/transport.py): run the tunnel program on ssh's standard
input and output, and end it, and everything it started, when ssh goes.

    proxywrap.py [--id ID --check N CHECK-PROGRAM [CHECK-ARGS...]] -- PROGRAM [ARGS...]

A tunnel program can outlive the ssh that started it: ssh sends its ProxyCommand SIGHUP on exit, and a program that
ignores SIGHUP (one provider's tunnel program can) is left running with its connection open, which keeps a cloud computer
awake and billing. So the program runs in a process group of its own, which this process owns: when ssh goes (this
process gets SIGHUP, SIGTERM or SIGINT, or its parent is gone, checked every half second) or the program itself
exits, the whole group gets SIGTERM, then SIGKILL after a grace period, and this process waits until the group is
empty, whatever the program's own children do. The handlers are in place before the program starts. The program
inherits only standard input, output and error; its exit status is this process's. Standard library only.

It notes its parent (ssh) first thing, and a parent that's gone already (launchd, pid 1) gets no tunnel: an ssh that
exited while this process was starting can't leave one behind. It checks the parent again before the tunnel starts,
and from then on every half second. It leaves the process group (and session) ssh started it in right after: whoever ends ssh's connection
may SIGKILL that whole group at once (the hub does, after a grace period), and a SIGKILL would end this process before
it could end the tunnel, which isn't in that group. Out of it, this process outlives ssh by the half second it takes
to notice, then ends the tunnel (found live: `hub stop` left a provider's tunnel running, and its computer awake).

With --id, the provider is asked first, on every connection: the N words after --check are a program and its
arguments whose answer (JSON, at most a megabyte, within 30 s; run with HOME, PATH and LANG only and nothing on its
standard input) must name the computer by the provider's own id for it, the one recorded when it was made ("id", or
"data"."id"). Any other answer, or none, and the tunnel never starts (lib/remote.py target gives these for a
computer behind a provider's tunnel, from its transport's hook: one deleted and made again under its name is another
computer, whatever arguments a caller kept).

`ssh -f` doesn't fit: it forks after signing in and the process that started this one exits, so the tunnel would end
at once. A caller that wants ssh in the background runs it there itself (bin/metacomputer view_tunnel)."""
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time

GRACE_S = 3.0
POLL_S = 0.5
CHECK_S = 30.0
CHECK_MAX = 1 << 20
ID = re.compile(r"[A-Za-z0-9_-]{4,80}")


def group_alive(pgid):
    """Whether any process is left in the group (signal 0 only checks)."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def end_group(pgid, leader=None):
    """SIGTERM to the whole group, then SIGKILL to whatever is left after the grace period; returns once the group
    is empty (or after the SIGKILL's second). The leader is reaped as it goes, so its zombie doesn't count."""
    for sig, wait in ((signal.SIGTERM, GRACE_S), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if leader is not None:
                leader.poll()
            if not group_alive(pgid):
                return
            time.sleep(0.05)


def leave_group():
    """Out of the caller's process group and session (setsid), so a group kill aimed at ssh's connection never ends
    this process before it has ended the tunnel. A process that leads its group already can't (EPERM): it stays."""
    try:
        os.setsid()
        return True
    except OSError:
        return False


def provider_says(check, want, stop=(), timeout=CHECK_S):
    """None when the provider's answer to `check` (a program and its arguments) names the computer by the id `want`;
    else why not. The answer is read as it comes, capped, and the check ended (its whole group) past the cap, the
    timeout, or a stop."""
    env = {"HOME": os.environ.get("HOME") or os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
           "LANG": "C.UTF-8"}
    try:
        p = subprocess.Popen(check, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=env, close_fds=True, start_new_session=True)
    except OSError:
        return "the provider couldn't be asked whether this is still the computer it was made as"
    out, over = bytearray(), None
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        sel.register(p.stdout, selectors.EVENT_READ)
        while over is None:
            if stop:
                over = "stopped"
                break
            left = deadline - time.monotonic()
            if left <= 0:
                over = "no answer in time"
                break
            if not sel.select(min(left, 0.2)):
                continue
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            out += chunk
            if len(out) > CHECK_MAX:
                over = "too long an answer"
    if over is not None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    p.stdout.close()
    try:
        rc = p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        rc = p.wait()
    if over is not None or rc != 0:
        return "the provider couldn't be asked whether this is still the computer it was made as"
    try:
        obj = json.loads(bytes(out).decode())
    except ValueError:
        return "the provider couldn't be asked whether this is still the computer it was made as"
    have = obj.get("id") if isinstance(obj, dict) else None
    if have is None and isinstance(obj, dict) and isinstance(obj.get("data"), dict):
        have = obj["data"].get("id")
    if have != want:
        return ("the provider's computer of that name isn't the one this computer was made as (another id: deleted and "
                "made again?)")
    return None


def run(argv, parent=None, check=None, want=None):
    parent = os.getppid() if parent is None else parent
    stop = []

    def on_signal(signum, _frame):
        stop.append(signum)
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)          # before the program starts: no window where a signal is missed
    # The parent this process was started by (ssh), seen before anything else ran: one that's gone already (this process
    # was orphaned before it could look: its parent is launchd now) never gets a tunnel.
    if parent <= 1 or os.getppid() != parent or stop:
        return 1
    if check is not None:
        why = provider_says(check, want, stop)
        if why is not None:
            print(f"metacomputer: {why}: no connection was made", file=sys.stderr, flush=True)
            return 1
        if os.getppid() != parent or stop:
            return 1
    child = subprocess.Popen(argv, stdin=0, stdout=1, stderr=2, close_fds=True, start_new_session=True)
    pgid = child.pid                          # its own session and group, which only it and its children are in
    try:
        while True:
            rc = child.poll()
            if rc is not None:
                return rc
            if stop or os.getppid() != parent:
                return 1
            time.sleep(POLL_S)
    finally:
        end_group(pgid, child)               # always: the group, not only the program, whichever way this ends


def main(argv):
    parent = os.getppid()        # first: ssh, before anything else can happen (an orphan sees launchd here: refused)
    usage = "usage: proxywrap.py [--id ID --check N CHECK-PROGRAM [ARGS...]] -- PROGRAM [ARGS...]"
    check = want = None
    if argv[:1] == ["--id"]:
        n = argv[3] if len(argv) > 4 and argv[2] == "--check" else ""
        if not ID.fullmatch(argv[1]) or not n.isdigit() or not 1 <= int(n) <= 64 or len(argv) < 4 + int(n) \
                or not argv[4].startswith("/"):
            print(usage, file=sys.stderr)
            return 2
        want, check, argv = argv[1], argv[4:4 + int(n)], argv[4 + int(n):]
    if len(argv) < 2 or argv[0] != "--":
        print(usage, file=sys.stderr)
        return 2
    if os.environ.get("MCOMP_PARSE_ONLY") == "1":   # tests/test_help.py's valid forms: taken, nothing done
        print("parse-only: proxywrap.py: taken " + json.dumps({"flags": [], "values": ["--id"], "words": 1, "max_words": None, "passthrough": True}))
        sys.exit(99)        # parse only: nothing was done (never 0: a check run this way refuses)
    leave_group()
    return run(argv[1:], parent=parent, check=check, want=want)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
