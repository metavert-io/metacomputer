"""The handoff gate of the programs that change a computer by themselves, however they're run.

While the user has the computer (a handoff: lib/handoff.py, the record lib/owners.py keeps), every program that would
change it waits: its power state, its disks, snapshots or checkpoints, its network rules, what's inside it (files,
processes, mounts), or whether it exists. bin/metacomputer's commands hold this check themselves (handoff_hold), and
so do our own scripts (scripts/lib.sh script_handoff_hold); a program run by itself goes through it here, right after
its command line is taken and before it calls anything, runs anything in the guest or changes any state:

    import handoff_gate
    d = handoff_gate.owners_dir_for(name=..., run_dir=..., transport=...)   # the computer it acts on, or exit 4
    handoff_gate.wait_or_hold(d)                # exits 4 (and says why) while that computer's handoff is open
    handoff_gate.wait_or_hold_all([d1, d2])     # several computers (what one volume changes): each, in path order

The computer is the one the program acts on, told from the same settings it acts with (owners_dir_for): its name and
its run folder must be the same computer's, and a transport that names one of this checkout's computers (by its SSH
folder's known-hosts or key file) must name that one; what can't be told, or doesn't agree, refuses.

It goes ahead:
  - when no handoff is open: this computer's dispatch lock is then held shared for the rest of the run, as the bash
    side holds it on its descriptor, so no handoff opens while the program acts (opening one takes it exclusively);
  - at the user's own terminal (owners.at_user_terminal, as `handoff.py guard --user-ok`): their own hands;
  - under its caller's hold (inherited_hold): a descriptor this process was handed on that very lock file, checked
    to be it (device and inode, a regular file), to be a description that has taken a flock (the kernel's FHASLOCK
    on it, read with proc_pidfdinfo: a description that never locked doesn't have it), that someone holds the lock
    shared and nobody exclusively, as a command that already looked passes it on to what it runs (bin/metacomputer's
    descriptor 6, a script's 5). The lock isn't taken again then. An environment variable alone never counts.
    FHASLOCK stays set after LOCK_UN: so nothing lets go of a dispatch hold while keeping its descriptor open; it
    closes it (recording.py's let_go, the ssh holder's exit).

It refuses (exit 4) when the lock can't be had in time (a handoff being opened) or the record can't be looked at: a
check that can't be made never lets the action through. Under MCOMP_PARSE_ONLY nothing gets here (the program's
parser ends the run with 99 first); if something did, it ends here the same way.

Like every check against your own account, this stops an agent from acting by accident while the user has the
computer, not a program set on it (it can take the lock itself, or run the provider's tool or ssh directly:
SECURITY.md). Shared code: no provider, agent, guest system or browser is named here; the programs that call it are.
"""
import ctypes
import fcntl
import os
import stat
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flockwait  # noqa: E402
import owners  # noqa: E402
import strictargs  # noqa: E402

EXIT_WAIT = 4                  # as handoff.py guard and bin/metacomputer: the user has the computer
_HELD = {}                     # dispatch lock path -> the descriptor this process holds it on, for the rest of its run
PROC_PIDFDVNODEINFO = 1        # proc_pidfdinfo's flavor: struct vnode_fdinfo, whose first word is fi_openflags
FHASLOCK = 0x4000              # in fi_openflags: this open file description has taken a flock


def use(owners_dir):
    """Look at the computer whose owners folder is OWNERS_DIR (its RUN_DIR/owners): this process acts on that one."""
    if owners_dir:
        owners.OWNERS_DIR = os.path.abspath(owners_dir)


def _open_fds():
    try:
        return sorted(int(n) for n in os.listdir("/dev/fd") if n.isdigit())
    except OSError:
        return list(range(3, 256))


def has_flocked(fd):
    """Whether descriptor FD's open file description has taken a flock (FHASLOCK, through libproc's proc_pidfdinfo);
    False when that can't be read: no inherited hold then."""
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidfdinfo.restype = ctypes.c_int
        buf = ctypes.create_string_buffer(2048)
        n = lib.proc_pidfdinfo(os.getpid(), int(fd), PROC_PIDFDVNODEINFO, buf, 2048)
        if n < 4:
            return False
        return bool(struct.unpack_from("=I", buf.raw, 0)[0] & FHASLOCK)
    except Exception:       # noqa: BLE001  (no libproc, or the call failed: fail closed)
        return False


def inherited_hold(exclude=(), only=None):
    """The descriptor this process was handed (not one it opened: EXCLUDE; ONLY: just these numbers) that holds this
    computer's dispatch lock, or None. It must be that very file (device and inode, a regular file, never a link), a
    description that has taken a flock (has_flocked), someone must hold the lock shared and nobody exclusively (two
    probes on a description of this process's own), and a shared lock on it must be had at once without waiting
    (held already, it's left as it is)."""
    path = owners.dispatch_path()
    try:
        want = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(want.st_mode):
        return None
    found = []
    for fd in (sorted(only) if only is not None else _open_fds()):
        if fd < 3 or fd in exclude or fd in _HELD.values():
            continue
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino) and has_flocked(fd):
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


def refuse(text):
    """Exit 4 saying TEXT: a computer whose handoff can't be checked (a program's own refusals of that kind)."""
    _refuse(text)


def _same(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError, TypeError):
        return False


def _transport_dirs(transport):
    """The SSH folders a transport names (its UserKnownHostsFile=, IdentityFile= and CertificateFile= files' folders)."""
    out = []
    for i, w in enumerate(transport):
        for key in ("UserKnownHostsFile=", "IdentityFile=", "CertificateFile="):
            if w.startswith(key):
                out.append(os.path.dirname(w[len(key):]))
            elif w == "-i" and i + 1 < len(transport):
                out.append(os.path.dirname(transport[i + 1]))
    return out


def owners_dir_for(name=None, run_dir=None, transport=None, root=None):
    """The owners folder of the computer a program acts on, from what it acts with: NAME (its VM name), RUN_DIR (its
    run folder, as given) and TRANSPORT (the command that reaches it). A computer of this checkout's (its settings,
    labconfig.computers) is told by its name or by the SSH folder its transport names, and its run folder must be its
    own; a computer this checkout has no settings for (another checkout's, a test's) needs its run folder given, one
    that is no computer of this checkout's. Anything else (a name and a folder of different computers, a transport
    to one and a name of another, nothing to tell it by) refuses with exit 4: its handoff can't be checked."""
    try:
        import labconfig                # (its own settings are read on import: only when a computer must be told)
        comps = labconfig.computers(root)
    except Exception as e:      # noqa: BLE001  (settings that can't be read: the computer can't be told)
        _refuse(f"couldn't read this checkout's settings ({e}), so which computer's handoff to check can't be told; "
                "nothing was done")
    by_name = {c["name"]: c for c in comps if c["name"]}
    if transport is not None:
        dirs = _transport_dirs(transport)
        named = sorted({c["name"] for c in comps if c["ssh_dir"] and any(_same(d, c["ssh_dir"]) for d in dirs)})
        if len(named) > 1:
            _refuse(f"the command to reach the computer names the SSH folders of {', '.join(named)}: which computer it "
                    "acts on can't be told, so its handoff can't be checked; nothing was done")
        if named:
            if name and name != named[0]:
                _refuse(f"this acts on {name}, but the command to reach it is {named[0]}'s; nothing was done")
            name = named[0]
        elif not run_dir or any(c["run_dir"] and _same(run_dir, c["run_dir"]) for c in comps):
            _refuse("the command to reach the computer names none of this checkout's computers (by their SSH folders), "
                    "so which computer's handoff to check can't be told; nothing was done")
    owner = None
    if run_dir:
        owner = next((c["name"] for c in comps if c["run_dir"] and _same(run_dir, c["run_dir"])), None)
    if name and name in by_name:
        want = by_name[name]["run_dir"]
        if not want:
            _refuse(f"{name}'s run folder can't be told from its settings, so its handoff can't be checked; nothing was "
                    "done")
        if run_dir and not _same(run_dir, want):
            _refuse(f"the run folder {run_dir} isn't {name}'s ({want}), so its handoff can't be checked there; nothing "
                    "was done")
        return os.path.join(want, "owners")
    if run_dir:
        if owner and name and owner != name:
            _refuse(f"the run folder {run_dir} is {owner}'s, not {name}'s; nothing was done")
        if owner or not name or name not in by_name:
            return os.path.join(run_dir, "owners")
    _refuse(f"which computer this acts on ({name or 'no name'}) can't be told from this checkout's settings, and no run "
            "folder was given (MCOMP_RUN_DIR), so its handoff can't be checked; nothing was done")


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


def wait_or_hold_all(owners_dirs):
    """wait_or_hold for every computer in OWNERS_DIRS (what one action changes on several: a shared volume, a provider's
    shared network), each taken in the order of its folder's real path, so two such actions never wait on each other;
    the first that must wait ends it (exit 4) before anything is done. The owners folder looked at afterwards is the
    one that was current before."""
    before = owners.OWNERS_DIR
    seen = {}
    for d in owners_dirs:
        seen.setdefault(os.path.realpath(os.path.abspath(d)), d)
    try:
        for real in sorted(seen):
            wait_or_hold(seen[real])
    finally:
        owners.OWNERS_DIR = before
    return sorted(seen)
