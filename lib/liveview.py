#!/usr/bin/env python3
"""The live view on this Mac's own screen: `metacomputer view --open` (bin/metacomputer, do_view).

  liveview.py gui                       exit 0 when this command can use the Mac's own screen: it didn't come in
                                        over SSH, and the console is this user's screen session; else exit 1 and
                                        say why on stdout
  liveview.py clip [--clear-after 45]   the live-view password, one line on stdin -> the clipboard. A detached
                                        helper empties the clipboard after that many seconds, but only if nothing
                                        was copied since: it keeps the change count the copy's own clearContents
                                        returned (never the password), and compares and empties in one run.

The clipboard is macOS's general pasteboard, through osascript's JavaScript bridge to AppKit (NSPasteboard), so
the copy and its change count, and later the comparison and the clearing, each happen inside one process. The
password is marked concealed and transient (nspasteboard.org's markers), which clipboard managers that follow
them don't keep. It comes in on stdin and leaves on osascript's stdin: never in an argument, the environment,
a file, a log or an event, and nothing here prints it. Stdlib only.
"""
import os
import re
import resource
import subprocess
import sys
import time

MAX_PASSWORD = 256
CLEAR_DEFAULT_S = 45
# run(argv): "copy" (the text on stdin -> the pasteboard; prints the change count its clearContents returned, so
# it is this copy's own, or "raced" when the count moved before the text was in place: something else was copied
# in between) or "clear N" (empties the pasteboard only if its change count is still N; "cleared" or "kept").
PASTEBOARD_JS = r"""
ObjC.import('AppKit');
function run(argv) {
  var pb = $.NSPasteboard.generalPasteboard;
  if (argv[0] === 'copy') {
    var data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    var s = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding);
    if (s.isNil() || !ObjC.unwrap(s)) { return 'error'; }
    var n = pb.clearContents;
    var ok = pb.setStringForType(s, 'public.utf8-plain-text');
    pb.setStringForType($(''), 'org.nspasteboard.ConcealedType');
    pb.setStringForType($(''), 'org.nspasteboard.TransientType');
    if (!ok) { return 'error'; }
    return String(pb.changeCount) === String(n) ? String(n) : 'raced';
  }
  if (argv[0] === 'clear' && argv.length === 2) {
    if (String(pb.changeCount) !== argv[1]) { return 'kept'; }
    pb.clearContents;
    return 'cleared';
  }
  return 'error';
}
"""
COUNT_RE = re.compile(r"-?\d{1,20}")


def pasteboard(args, data=None):
    """osascript with PASTEBOARD_JS: its one line of output, or None."""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", PASTEBOARD_JS] + args, input=data,
                           capture_output=True, timeout=15, stdin=None if data is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


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


def clear_later(count, after):
    """A detached helper (its own session, no inherited descriptors: not the caller's output pipe, nor the VM
    locks on 7 and 8) that empties the clipboard after `after` seconds if its change count is still `count`."""
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
        pasteboard(["clear", count])
    except BaseException:
        pass
    finally:
        os._exit(0)


def clip(clear_after):
    pw = read_password()
    if pw is None:
        print("couldn't read the live-view password from its store", file=sys.stderr)
        return 1
    count = pasteboard(["copy"], pw)
    del pw
    if count == "raced":
        # Something else was copied while the password went on: which is on the clipboard now is unclear, so
        # nothing is cleared later (it could be someone else's).
        print("something else was copied at the same moment, so the clipboard won't be cleared by itself; copy "
              "something else when you're done", file=sys.stderr)
        return 0
    if count is None or not COUNT_RE.fullmatch(count):
        print("couldn't put the live-view password on the clipboard (osascript failed)", file=sys.stderr)
        return 1
    if clear_after > 0:
        clear_later(count, clear_after)
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
