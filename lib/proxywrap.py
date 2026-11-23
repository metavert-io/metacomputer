#!/usr/bin/env python3
"""ssh's ProxyCommand for a provider's tunnel (lib/cloud/transport.py): run the tunnel program on ssh's standard
input and output, and end it, and everything it started, when ssh goes.

    proxywrap.py -- PROGRAM [ARGS...]

A tunnel program can outlive the ssh that started it: ssh sends its ProxyCommand SIGHUP on exit, and a program that
ignores SIGHUP (Fly's `sprite proxy -W` can) is left running with its connection open, which keeps a cloud computer
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
to notice, then ends the tunnel (found live: `hub stop` left `sprite proxy` running, and the Sprite awake).

`ssh -f` doesn't fit: it forks after signing in and the process that started this one exits, so the tunnel would end
at once. A caller that wants ssh in the background runs it there itself (bin/metacomputer view_tunnel)."""
import os
import signal
import subprocess
import sys
import time

GRACE_S = 3.0
POLL_S = 0.5


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


def run(argv, parent=None):
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
    if len(argv) < 2 or argv[0] != "--":
        print("usage: proxywrap.py -- PROGRAM [ARGS...]", file=sys.stderr)
        return 2
    parent = os.getppid()        # first: ssh, before anything else can happen (an orphan sees launchd here: refused)
    leave_group()
    return run(argv[1:], parent=parent)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
