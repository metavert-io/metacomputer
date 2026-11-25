"""What metacomputer reads about the Mac's processes, in one place: ps where it can run, else the kernel.

/bin/ps is setuid root on macOS, and macOS's sandbox (sandbox-exec) runs no setuid program, under any profile. So inside
one (an agent session confined that way) ps can't start at all, and every check that asked it failed with it: a
session's claim, `metacomputer sh`, the gateway in `status`. Here ps is still asked first, exactly as before; only when
it can't be started (OSError: EPERM in a sandbox, or no ps at all) do the same facts come from the kernel, which any
account can read without a program:
  - every process, its parent, its process group and when it started: sysctl kern.proc.all and kern.proc.pid (struct
    kinfo_proc, the record ps reads them from), so always ps's answer;
  - a process's arguments and environment: KERN_PROCARGS2, readable for this account's processes only (ps, setuid
    root, reads every account's);
  - its executable (proc_pidpath) and its working folder (proc_pidinfo), which never came from ps.

ps prints a process's name (comm=, its first argument) and its command line (command=, its arguments joined by spaces)
vis(3)-encoded: tab and newline as \\011 and \\012, the other control characters as ^X, DEL as ^?, and anything beyond
ASCII as ps's locale has it. The kernel's answer stands in for ps's only where it is exactly ps's: the arguments could
be read and are ASCII (or the process is a zombie, which ps shows as <defunct>). Anything else can't be told: another
account's process, arguments beyond ASCII, a process that ended between two reads. Then the field comes back as None
(a ps that couldn't say), and table() lists the process as inexact, which lib/owners.py counts as an unidentified agent
(fail closed, as when ps can't be read). tests/test_owners.py compares the two on this Mac's processes.

CLI (scripts/lib.sh proc_field, where ps can't start):
  procinfo.py command|comm|pgid|lstart PID   that field from the kernel, as `ps -ww -o FIELD= -p PID` prints it; exit
                                             1 when there's no such process, 3 when it can't be told exactly (nothing
                                             printed)
  procinfo.py commands                       `ps -axww -o command=`: every process's command line whose rendering is
                                             exactly ps's (another account's can't be read, and is left out)
"""
import os
import subprocess
import sys

PS = "/bin/ps"
FIELDS = ("command", "comm", "pgid", "lstart")

_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL, _KERN_PROC_PID, _KERN_PROCARGS2 = 1, 14, 0, 1, 49    # <sys/sysctl.h>
# struct kinfo_proc on 64-bit macOS (648 bytes; tests/test_owners.py checks every field against ps): kp_proc.p_starttime
# (a struct timeval) at 0, p_stat at 36, p_pid at 40, p_comm (MAXCOMLEN + 1) at 243; kp_eproc.e_ucred.cr_uid at 420,
# e_ppid at 560, e_pgid at 564.
_KINFO_SIZE, _KP_STAT, _KP_PID, _KP_COMM, _KP_UID, _KP_PPID, _KP_PGID = 648, 36, 40, 243, 420, 560, 564
_SZOMB = 5                          # p_stat of a zombie
_ENOMEM = 12


class Proc(tuple):
    """One process as the kernel lists it: pid, ppid, pgid, uid, zombie, kname (the kernel's own short name, p_comm:
    at most 16 bytes of its executable's file name) and start (seconds, microseconds)."""
    __slots__ = ()
    pid = property(lambda s: s[0])
    ppid = property(lambda s: s[1])
    pgid = property(lambda s: s[2])
    uid = property(lambda s: s[3])
    zombie = property(lambda s: s[4])
    kname = property(lambda s: s[5])
    start = property(lambda s: s[6])


def _libc():
    import ctypes
    import ctypes.util
    return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _parse_kinfo(raw):
    import struct
    sec, usec = struct.unpack_from("<qi", raw, 0)
    pid, = struct.unpack_from("<i", raw, _KP_PID)
    uid, = struct.unpack_from("<I", raw, _KP_UID)
    ppid, pgid = struct.unpack_from("<ii", raw, _KP_PPID)
    kname = raw[_KP_COMM:_KP_COMM + 17].split(b"\0", 1)[0].decode("utf-8", "replace")
    return Proc((pid, ppid, pgid, uid, raw[_KP_STAT] == _SZOMB, kname, (sec, usec)))


def kernel_procs():
    """Every process (pid 0, the kernel's own, included), from sysctl kern.proc.all; None when it can't be read (not
    macOS, or an answer that isn't whole records)."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        libc = _libc()
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL)
        for _ in range(8):
            size = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
                return None
            size = ctypes.c_size_t(size.value + 64 * _KINFO_SIZE)     # room for processes started meanwhile
            buf = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) == 0:
                break
            if ctypes.get_errno() != _ENOMEM:
                return None
        else:
            return None
        raw = buf.raw[:size.value]
    except (OSError, AttributeError, ValueError):
        return None
    if not raw or len(raw) % _KINFO_SIZE:
        return None
    procs = [_parse_kinfo(raw[o:o + _KINFO_SIZE]) for o in range(0, len(raw), _KINFO_SIZE)]
    return procs if any(p.pid == 1 for p in procs) else None


def kernel_proc(pid):
    """One process from sysctl kern.proc.pid, or None when there's no such process (or it can't be read)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or sys.platform != "darwin":
        return None
    try:
        import ctypes
        mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid)
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(len(buf))
        if _libc().sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value != _KINFO_SIZE:
            return None                                  # no such process
        p = _parse_kinfo(buf.raw[:_KINFO_SIZE])
    except (OSError, AttributeError, ValueError):
        return None
    return p if p.pid == pid else None


def _procargs(pid):
    """(arguments, environment) exactly as the kernel keeps them (KERN_PROCARGS2: what the process left there, so a
    program that renamed itself shows its new name first), or None when they can't be read (another account's
    process, a zombie, no such process). The kernel's record has no mark where the arguments end, only their count, so
    the environment is what follows them."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or sys.platform != "darwin":
        return None
    try:
        import ctypes
        import struct
        libc = _libc()
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 8:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0 or size.value < 8:
            return None
        raw = buf.raw[:size.value]
        argc = struct.unpack_from("<i", raw, 0)[0]
    except (OSError, AttributeError, ValueError):
        return None
    i = raw.find(b"\0", 4)                    # the executable's path, then padding
    if i < 0 or argc <= 0:
        return None
    while i < len(raw) and raw[i] == 0:
        i += 1
    args, env = [], []
    while i < len(raw):
        j = raw.find(b"\0", i)
        j = len(raw) if j < 0 else j
        word = raw[i:j].decode("utf-8", "replace")
        if len(args) < argc:
            args.append(word)
        elif not word:
            break                             # the environment ends at its first empty string
        else:
            env.append(word)
        i = j + 1
    return (args, env) if args else None


def argv(pid):
    """A process's arguments, exactly (one string each: spaces inside an argument stay inside it), or None when they
    can't be read. macOS: KERN_PROCARGS2; Linux: /proc/PID/cmdline."""
    if sys.platform == "darwin":
        got = _procargs(pid)
        return got[0] if got else None
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = f.read().split(b"\0")
    except (OSError, TypeError, ValueError):
        return None
    if parts and parts[-1] == b"":
        parts.pop()
    return [x.decode("utf-8", "replace") for x in parts] or None


def environ(pid):
    """A process's environment as it started ({NAME: value}; what it set later isn't there), or None when it can't be
    read (another account's process, a zombie, no such process; not macOS)."""
    got = _procargs(pid)
    if not got:
        return None
    out = {}
    for item in got[1]:
        name, eq, value = item.partition("=")
        if eq and name and name not in out:
            out[name] = value
    return out


def _libproc():
    import ctypes
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def exe(pid):
    """The realpath of a process's executable (its text image: macOS proc_pidpath, Linux /proc/PID/exe), or None."""
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        if sys.platform == "darwin":
            import ctypes
            buf = ctypes.create_string_buffer(4096)
            n = _libproc().proc_pidpath(pid, buf, 4096)
            return buf.value.decode("utf-8", "replace") if n > 0 else None
        return os.readlink(f"/proc/{pid}/exe")
    except (OSError, ValueError, AttributeError, TypeError):
        return None


_PROC_PIDVNODEPATHINFO, _VNODE_INFO_SIZE, _MAXPATHLEN = 9, 152, 1024    # <sys/proc_info.h>


def cwd(pid):
    """A process's working folder (macOS proc_pidinfo PROC_PIDVNODEPATHINFO, Linux /proc/PID/cwd), or None when it
    can't be read (another account's process, no such process)."""
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        if sys.platform == "darwin":
            import ctypes
            size = 2 * (_VNODE_INFO_SIZE + _MAXPATHLEN)        # struct proc_vnodepathinfo: pvi_cdir, pvi_rdir
            buf = ctypes.create_string_buffer(size)
            n = _libproc().proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, ctypes.c_uint64(0), buf, size)
            if n != size:
                return None
            path = buf.raw[_VNODE_INFO_SIZE:_VNODE_INFO_SIZE + _MAXPATHLEN].split(b"\0", 1)[0]
            return path.decode("utf-8", "replace") or None
        return os.readlink(f"/proc/{pid}/cwd")
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def vis(text):
    """text as ps prints it (vis(3) with VIS_TAB | VIS_NL | VIS_NOSLASH), or None when that depends on ps's locale: a
    character beyond ASCII (or one that wasn't valid UTF-8)."""
    out = []
    for ch in text:
        o = ord(ch)
        if o > 0x7F:
            return None
        if ch == "\t":
            out.append("\\011")
        elif ch == "\n":
            out.append("\\012")
        elif o < 0x20:
            out.append("^" + chr(o + 0x40))
        elif o == 0x7F:
            out.append("^?")
        else:
            out.append(ch)
    return "".join(out)


def _render(p, field):
    """The kernel's answer for one process's FIELD exactly as ps prints it, or None when it can't be told."""
    if field == "pgid":
        return str(p.pgid)
    if field == "lstart":
        return lstart_of(p.start[0])
    if p.zombie:
        return "<defunct>"
    args = argv(p.pid)
    if not args:
        return None             # another account's, or one that ended meanwhile: ps (setuid root) may read it
    return vis(args[0] if field == "comm" else " ".join(args))


# ps's lstart is strftime's %c in ps's own LC_TIME: the C locale's "%a %b %e %H:%M:%S %Y" only where that locale is
# one of these (checked on macOS: en_GB, fr_FR, de_DE and ja_JP each write it their own way).
C_TIME_LOCALES = ("", "C", "POSIX")
C_TIME_PREFIXES = ("C.", "en_US")


def lstart_of(seconds, environ_=None):
    """A start time (seconds) as `ps -o lstart=` prints it, or None when ps would print it in another locale's words."""
    e = os.environ if environ_ is None else environ_
    loc = e.get("LC_ALL") or e.get("LC_TIME") or e.get("LANG") or ""
    if loc not in C_TIME_LOCALES and not loc.startswith(C_TIME_PREFIXES):
        return None
    import time
    return time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(seconds))


def start(pid):
    """When a process started, as the kernel has it ("SECONDS.MICROSECONDS": macOS's kern.proc.pid, Linux's
    /proc/PID/stat), or None when there's no such process. With the pid, it names one process even after the pid is
    reused."""
    if sys.platform == "darwin":
        p = kernel_proc(pid)
        return f"{p.start[0]}.{p.start[1]:06d}" if p else None
    try:
        pid = int(pid)
        with open(f"/proc/{pid}/stat") as f:
            return "jiffies." + f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, TypeError, ValueError):
        return None


def kernel_field(pid, field):
    """FIELD of one process from the kernel, as ps prints it: "" when there's no such process (ps prints nothing),
    None when it can't be told exactly."""
    p = kernel_proc(pid)
    return "" if p is None else _render(p, field)


def field(pid, name, ps=None, timeout=None):
    """`ps -ww -o NAME= -p PID`'s answer (NAME: command, comm, pgid or lstart; "" when ps lists no such process, or
    fails), from ps itself when it can run (PS, or the `ps` on PATH where a caller always ran that one). Only when ps
    can't be started: the kernel's answer when it is exactly ps's, else None (can't be told). timeout: as the caller
    gave ps one (subprocess.TimeoutExpired)."""
    if name not in FIELDS:
        raise ValueError(f"no field {name!r}")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""               # as ps answers one: nothing
    try:
        return subprocess.run([ps or PS, "-ww", "-o", f"{name}=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=timeout).stdout.rstrip("\n")
    except OSError:
        pass                    # ps can't start: a sandbox runs no setuid program
    return kernel_field(pid, name)


def table(ps=None):
    """`ps -axo pid=,ppid=,comm=`: (its output, its exit code, the pids whose line can't be told exactly). From ps when
    it can run; when it can't be started, the same lines from the kernel (the kernel's own process, pid 0, left out as
    ps leaves it out), with the processes whose name can't be told exactly listed as inexact, their name shown as ps
    shows one it can't read, "(kernel name)". Exit code 1 and no lines when the kernel can't be read either."""
    try:
        r = subprocess.run([ps or PS, "-axo", "pid=,ppid=,comm="], capture_output=True, text=True)
        return r.stdout, r.returncode, frozenset()
    except OSError:
        pass
    procs = kernel_procs()
    if procs is None:
        return "", 1, frozenset()
    lines, inexact = [], set()
    for p in sorted(procs):
        if p.pid <= 0:
            continue
        name = _render(p, "comm")
        if name is None:
            inexact.add(p.pid)
            name = f"({p.kname})"
        lines.append(f"{p.pid:5d} {p.ppid:5d} {name}")
    return "\n".join(lines) + "\n", 0, frozenset(inexact)


def commands():
    """Every process's command line whose rendering is exactly ps's (`ps -axww -o command=` where ps can't start); None
    when the kernel can't be read."""
    procs = kernel_procs()
    if procs is None:
        return None
    out = []
    for p in sorted(procs):
        if p.pid > 0:
            got = _render(p, "command")
            if got is not None:
                out.append(got)
    return out


def main(argv_):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import strictargs           # its command line, by metacomputer's rules, before anything is read
    got = strictargs.expect(argv_, ["command|comm|pgid|lstart PID", "commands"], prog="procinfo.py", usage=__doc__)
    if argv_[0] == "commands":
        lines = commands()
        if lines is None:
            return 1
        for line in lines:
            print(line)
        return 0
    pid = got.words[1] if len(got.words) > 1 else ""
    if not pid.isdigit():
        print("procinfo.py: PID is a number", file=sys.stderr)
        return 2
    out = kernel_field(int(pid), argv_[0])
    if out is None:
        return 3
    if out == "":
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
