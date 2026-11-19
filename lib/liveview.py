#!/usr/bin/env python3
"""The live view on this Mac's own screen: `metacomputer view --open` (bin/metacomputer, do_view).

  liveview.py gui                       exit 0 when this command can use the Mac's own screen: it didn't come in
                                        over SSH, and the console is this user's screen session; else exit 1 and
                                        say why on stdout
  liveview.py clip [--clear-after 45]   the live-view password, one line on stdin -> the clipboard (pbcopy's
                                        stdin). A detached helper clears the clipboard after that many seconds,
                                        but only while it still holds that password: it keeps a SHA-256 of it,
                                        never the password, and compares the clipboard's text with it.

The password comes in on stdin and leaves on pbcopy's stdin: never in an argument, the environment, a file, a
log or an event, and nothing here prints it. Stdlib only.
"""
import hashlib
import hmac
import os
import resource
import subprocess
import sys
import time

MAX_PASSWORD = 256
CLEAR_DEFAULT_S = 45


def gui_reason():
    """None when this process can put a window on the Mac's own screen; else why not."""
    for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        if os.environ.get(k):
            return "this terminal came in over SSH, so it isn't on the Mac's own screen"
    try:
        if os.stat("/dev/console").st_uid != os.getuid():
            return "the Mac's screen isn't yours right now (another user, or the login window)"
    except OSError:
        return "this Mac has no screen session"
    try:
        r = subprocess.run(["launchctl", "managername"], capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
        manager = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        manager = ""
    if manager != "Aqua":
        return "this process isn't part of the Mac's screen session"
    return None


def read_password():
    """One line from stdin: 1-256 bytes, no control characters (VNC itself uses the first 8)."""
    data = sys.stdin.buffer.readline(MAX_PASSWORD + 2)
    pw = data.rstrip(b"\r\n")
    if not pw or len(pw) > MAX_PASSWORD or any(c < 32 or c == 127 for c in pw):
        return None
    return pw


def _max_fd():
    try:
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (OSError, ValueError):
        soft = 1024
    return min(soft if soft > 0 else 1024, 65536)


def clear_later(digest, after):
    """A detached helper (its own session, no inherited descriptors: not the caller's output pipe, nor the VM
    locks on 7 and 8) that empties the clipboard after `after` seconds if it still holds the password."""
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, _max_fd())
        time.sleep(after)
        cur = subprocess.run(["pbpaste"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL).stdout
        if hmac.compare_digest(hashlib.sha256(cur).digest(), digest):
            subprocess.run(["pbcopy"], input=b"", timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except BaseException:
        pass
    finally:
        os._exit(0)


def clip(clear_after):
    pw = read_password()
    if pw is None:
        print("couldn't read the live-view password from its store", file=sys.stderr)
        return 1
    try:
        r = subprocess.run(["pbcopy"], input=pw, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        ok = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    digest = hashlib.sha256(pw).digest()
    del pw
    if not ok:
        print("couldn't put the live-view password on the clipboard (pbcopy failed)", file=sys.stderr)
        return 1
    if clear_after > 0:
        clear_later(digest, clear_after)
    return 0


def main(argv):
    if argv[:1] == ["gui"]:
        why = gui_reason()
        if why:
            print(why)
            return 1
        return 0
    if argv[:1] == ["clip"]:
        after = CLEAR_DEFAULT_S
        if len(argv) == 3 and argv[1] == "--clear-after":
            try:
                after = max(0, min(int(argv[2]), 3600))
            except ValueError:
                print("--clear-after takes seconds", file=sys.stderr)
                return 2
        elif len(argv) != 1:
            print("usage: liveview.py clip [--clear-after SECONDS]", file=sys.stderr)
            return 2
        return clip(after)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
