#!/usr/bin/env python3
"""ssh's ProxyCommand for a provider's tunnel (lib/cloud/transport.py): run the tunnel program on ssh's standard
input and output, and end it when ssh goes.

    proxywrap.py -- PROGRAM [ARGS...]

A tunnel program can outlive the ssh that started it: ssh sends its ProxyCommand SIGHUP on exit, and a program that
ignores SIGHUP (Fly's `sprite proxy -W` does) is left running with its connection open, which keeps a cloud computer
awake and billing. So the program runs as this process's child, in a process group of its own, and gets SIGTERM
(then SIGKILL after a grace period) when any of these happens: this process gets SIGHUP, SIGTERM or SIGINT; ssh, its
parent, is gone (checked every half second); the program itself exits (its status is this process's). The program
inherits only standard input, output and error. Standard library only."""
import os
import signal
import subprocess
import sys
import time

GRACE_S = 3.0
POLL_S = 0.5


def end(child):
    """SIGTERM to the program's process group, then SIGKILL if it's still there after the grace period."""
    for sig, wait in ((signal.SIGTERM, GRACE_S), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if child.poll() is not None:
                return
            time.sleep(0.05)


def run(argv, parent=None):
    parent = os.getppid() if parent is None else parent
    child = subprocess.Popen(argv, stdin=0, stdout=1, stderr=2, close_fds=True, start_new_session=True)
    stop = []

    def on_signal(signum, _frame):
        stop.append(signum)
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)
    try:
        while True:
            rc = child.poll()
            if rc is not None:
                return rc
            if stop or os.getppid() != parent:
                end(child)
                return 1
            time.sleep(POLL_S)
    finally:
        if child.poll() is None:
            end(child)


def main(argv):
    if len(argv) < 2 or argv[0] != "--":
        print("usage: proxywrap.py -- PROGRAM [ARGS...]", file=sys.stderr)
        return 2
    return run(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
