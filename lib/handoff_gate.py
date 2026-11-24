"""The handoff gate of the programs that change a computer by themselves, however they're run.

While the user has the computer (a handoff: lib/handoff.py, the record lib/owners.py keeps), every program that would
change it waits: its power state, its disks, snapshots or checkpoints, its network rules, what's inside it (files,
processes, mounts), or whether it exists. bin/metacomputer's commands hold this check themselves (handoff_hold), and
so do our own scripts (scripts/lib.sh script_handoff_hold); a program run by itself goes through it here, right after
its command line is taken and before it calls anything, runs anything in the guest or changes any state:

    import handoff_gate
    handoff_gate.wait_or_hold(owners_dir)       # exits 4 (and says why) while that computer's handoff is open

It goes ahead:
  - when no handoff is open: this computer's dispatch lock is then held shared for the rest of the run, as the bash
    side holds it on its descriptor, so no handoff opens while the program acts (opening one takes it exclusively);
  - at the user's own terminal (owners.at_user_terminal, as `handoff.py guard --user-ok`): their own hands;
  - under its caller's hold (inherited_hold): a descriptor this process was handed on that very lock file, checked
    to be it (device and inode, a regular file) and to hold a shared lock (never one being taken exclusively), as a
    command that already looked passes it on to what it runs (bin/metacomputer's descriptor 6, a script's 5). The lock
    isn't taken again then. An environment variable alone never counts.

It refuses (exit 4) when the lock can't be had in time (a handoff being opened) or the record can't be looked at: a
check that can't be made never lets the action through. Under MCOMP_PARSE_ONLY nothing gets here (the program's
parser ends the run with 99 first); if something did, it ends here the same way.

Like every check against your own account, this stops an agent from acting by accident while the user has the
computer, not a program set on it (it can take the lock itself, or run the provider's tool or ssh directly:
SECURITY.md). Shared code: no provider, agent, guest system or browser is named here; the programs that call it are.
"""
import fcntl
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flockwait  # noqa: E402
import owners  # noqa: E402
import strictargs  # noqa: E402

EXIT_WAIT = 4                  # as handoff.py guard and bin/metacomputer: the user has the computer
_HELD = {}                     # dispatch lock path -> the descriptor this process holds it on, for the rest of its run


def use(owners_dir):
    """Look at the computer whose owners folder is OWNERS_DIR (its RUN_DIR/owners): this process acts on that one."""
    if owners_dir:
        owners.OWNERS_DIR = os.path.abspath(owners_dir)


def _open_fds():
    try:
        return sorted(int(n) for n in os.listdir("/dev/fd") if n.isdigit())
    except OSError:
        return list(range(3, 256))


def inherited_hold(exclude=()):
    """The descriptor this process was handed (not one it opened: EXCLUDE) that holds this computer's dispatch lock, or
    None. It must be that very file (device and inode, a regular file, never a link), someone must hold the lock shared
    and nobody exclusively (two probes on a description of this process's own), and a shared lock on the descriptor
    must be had at once without waiting (held already, it's left as it is)."""
    path = owners.dispatch_path()
    try:
        want = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(want.st_mode):
        return None
    found = []
    for fd in _open_fds():
        if fd < 3 or fd in exclude or fd in _HELD.values():
            continue
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino):
            found.append(fd)
    if not found:
        return None
    try:
        probe = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None                 # nobody holds it: what was handed on isn't a hold
        except BlockingIOError:
            pass
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return None                 # held exclusively: a handoff being opened, or an input reset
    except OSError:
        return None
    finally:
        os.close(probe)
    for fd in found:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return fd
        except OSError:
            continue
    return None


def _refuse(text):
    print("metacomputer: " + text, file=sys.stderr, flush=True)
    sys.exit(EXIT_WAIT)


def wait_or_hold(owners_dir=None):
    """The gate (see the module's notes): returns "inherited", "held" or "user" when the program may go on, holding
    this computer's dispatch lock for the rest of its run; exits 4, saying why, when it must wait."""
    if strictargs.PARSE_ONLY or strictargs.DESCRIBE:
        print("parse only: nothing was done", file=sys.stderr)
        sys.exit(strictargs.PARSE_ONLY_EXIT)
    use(owners_dir)
    path = owners.dispatch_path()
    if path in _HELD:
        return "held"                   # this process looked already, and still holds it
    fd = inherited_hold()
    if fd is not None:
        _HELD[path] = fd
        return "inherited"
    try:
        os.makedirs(owners.OWNERS_DIR, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        _refuse(f"couldn't open this computer's dispatch lock ({e.strerror or e}); nothing was done")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        flockwait.take(fd, owners.DISPATCH_WAIT_S, 0.02, shared=True)
    except BlockingIOError:
        os.close(fd)
        _refuse("a handoff is being opened, so the user may have the computer now; nothing was done "
                "(`metacomputer handoff status`)")
    except OSError as e:
        os.close(fd)
        _refuse(f"couldn't take this computer's dispatch lock ({e.strerror or e}); nothing was done")
    try:
        h = owners.open_handoff()
    except Exception as e:      # noqa: BLE001  (a check that can't be made never lets the action through)
        os.close(fd)
        _refuse(f"couldn't check whether the user has the computer ({e}); nothing was done")
    if h:
        if owners.at_user_terminal((0,)):
            print("metacomputer: your handoff is open, and this runs at your own terminal: going ahead.",
                  file=sys.stderr, flush=True)
            _HELD[path] = fd
            return "user"
        os.close(fd)
        _refuse(owners.handoff_refusal(h))
    _HELD[path] = fd
    return "held"
