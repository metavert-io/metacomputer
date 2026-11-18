#!/usr/bin/env python3
"""Shared and persistent storage for the virtual computer, on the Mac's side (docs/storage.md).

  disk volumes    ext4 disk images on the Mac, attached read-write to one VM at a time
                  (`lume run --disk`); formatted inside the VM on first use, only when blank
  reference disks read-only ISO/UDF images packed from a Mac folder; any number of VMs
                  (`--mount` for the first, `--usb-storage` for the rest, both read-only)

Everything lives outside the VM, so it survives `reset`, `restore` and `build --replace`.

The registry (one JSON file per item) and the locks are on this Mac's internal disk, in
~/.local/share/metacomputer/storage/ ($MCOMP_STORAGE_HOME); the images go wherever
STORAGE_DIR (default ~/.local/share/metacomputer) or a per-item --dir says: an external
drive, a NAS mount, anything this user can reach.

  storage.py volume create NAME --size 20G [--dir PATH] | list [--json] | delete NAME [--yes]
             | resize NAME --size 40G
  storage.py refdisk create NAME --from FOLDER [--dir PATH] | list [--json] | delete NAME [--yes]
  storage.py prepare --pid PID [--no-volumes]   before `lume run`: check, lock, write the run arguments
  storage.py started --pid PID                  after `lume run`: the VM's process holds the locks
  storage.py attached                           the guest's answer (stdin): report, record, event
  storage.py release                            after the VM stopped: let go of everything
  storage.py checkpoint save|restore|delete NAME | list   volume snapshots for --with-volumes
  storage.py status [--json]
Settings come from bin/metacomputer's environment (MCOMP_*), else the config files.
"""
import argparse
import ctypes
import ctypes.util
import errno
import fcntl
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labconfig  # noqa: E402
from termsafe import escape_for_terminal as esc  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
COMP_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
GUEST_ROOTS = ("/volume", "/mnt", "/data", "/srv", "/workspace")
MIB = 1 << 20
MIN_SIZE = 16 * MIB
MAX_SIZE = 64 << 40
PROTOCOL = 1
NETWORK_FS = ("smbfs", "nfs", "afpfs", "webdav", "cifs", "ftp", "cddafs", "osxfuse", "macfuse", "fusefs")
MNT_LOCAL = 0x00001000


class StorageError(Exception):
    """A refusal, with the message for the person."""


# --- settings ---------------------------------------------------------------------
def env(name, default=""):
    v = os.environ.get("MCOMP_" + name)
    return v if v is not None else labconfig.CONFIG.get(name, default)


def data_dir():
    """STORAGE_DIR: where new images go (each item records its own path)."""
    d = env("STORAGE_DIR") or os.path.join(HOME, ".local", "share", "metacomputer")
    return os.path.abspath(os.path.expanduser(d))


def state_home():
    """The registry and the locks: always on this Mac, whatever STORAGE_DIR says."""
    return os.path.abspath(os.environ.get("MCOMP_STORAGE_HOME")
                           or os.path.join(HOME, ".local", "share", "metacomputer", "storage"))


def vm_name():
    return env("VM_NAME", "metacomputer") or "metacomputer"


def run_dir():
    return os.environ.get("MCOMP_RUN_DIR") or os.path.join(LAB, ".run")


def guest_home():
    return "/home/" + (env("GUEST_USER", "cua") or "cua")


def lab_root():
    return os.path.realpath(LAB)


def hostname():
    return socket.gethostname().split(".")[0]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# --- small helpers ------------------------------------------------------------------
def check_name(name, what="volume"):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise StorageError(f"{what} names: lowercase letters, digits and dashes, starting with a letter or digit "
                           f"(at most 40): {name!r}")
    return name


def parse_size(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*", str(text), re.I)
    if not m:
        raise StorageError(f"size {text!r}: use a number with K, M, G or T (binary units), e.g. 20G")
    n = float(m.group(1)) * (1 << (10 * " KMGT".index(m.group(2).upper() or " ")))
    n = int(-(-n // MIB) * MIB)                    # whole MiB, rounded up
    if n < MIN_SIZE or n > MAX_SIZE:
        raise StorageError(f"size {text!r}: between 16M and 64T")
    return n


def check_host_path(p, what):
    if CTRL_RE.search(p):
        raise StorageError(f"{what}: control characters in the path")
    return p


def atomic_json(path, obj, mode=0o600):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def confirm(prompt, yes):
    if yes:
        return
    if not sys.stdin.isatty():
        raise StorageError(f"{prompt} — refusing without a terminal; re-run with --yes")
    sys.stdout.write(prompt + " [y/N] ")
    sys.stdout.flush()
    if sys.stdin.readline().strip() not in ("y", "Y"):
        raise StorageError("cancelled")


# --- the registry -------------------------------------------------------------------
KINDS = {"volume": "volumes", "refdisk": "refdisks"}


def reg_path(kind, name):
    return os.path.join(state_home(), "registry", KINDS[kind], name + ".json")


def reg_load(kind, name):
    rec = read_json(reg_path(kind, name))
    return rec if isinstance(rec, dict) and rec.get("name") == name else None


def reg_all(kind):
    d = os.path.join(state_home(), "registry", KINDS[kind])
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for f in names:
        if f.endswith(".json") and NAME_RE.match(f[:-5]):
            rec = reg_load(kind, f[:-5])
            if rec:
                out.append(rec)
    return out


def reg_save(kind, rec):
    atomic_json(reg_path(kind, rec["name"]), rec)


def reg_delete(kind, name):
    try:
        os.unlink(reg_path(kind, name))
    except FileNotFoundError:
        pass


# --- where things are: network filesystems, refused folders -------------------------
class _StatFS(ctypes.Structure):
    # struct statfs on macOS (64-bit inodes, the only layout on Apple silicon)
    _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
                ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2), ("f_owner", ctypes.c_uint32),
                ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7)]


def fs_info(path):
    """(filesystem type name, mount point, local?) for the filesystem holding path (statfs(2))."""
    if sys.platform != "darwin":
        return ("unknown", "/", True)
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    buf = _StatFS()
    p = path
    while not os.path.exists(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)
    if libc.statfs(p.encode(), ctypes.byref(buf)) != 0:
        return ("unknown", "/", True)
    return (buf.f_fstypename.decode(errors="replace"), buf.f_mntonname.decode(errors="replace"),
            bool(buf.f_flags & MNT_LOCAL))


def is_network(fstype, local):
    return fstype.lower() in NETWORK_FS or not local


def network_fs(path):
    t, mnt, local = fs_info(path)
    return (t, mnt) if is_network(t, local) else None


def _real(p):
    try:
        return os.path.realpath(p)
    except OSError:
        return os.path.abspath(p)


def fold(p):
    """How APFS compares names by default: case-insensitive, normalization-insensitive. Comparing folded
    spellings refuses a little more on a case-sensitive volume, never less."""
    return unicodedata.normalize("NFD", p).casefold()


def inside(child, parent):
    """child is parent or below it, compared as the filesystem compares names."""
    c, p = fold(child), fold(parent).rstrip("/")
    return c == p or c.startswith(p + "/")


F_GETPATH = 50                     # fcntl: the path the kernel knows an open file by


def canon(path, directory=False):
    """(canonical spelling, (device, inode)) of an existing file or folder, links resolved. The spelling is
    the one the kernel gives the opened file (F_GETPATH): its real case, firmlinks into the data volume
    mapped back (/system/volumes/data/Users/x/LIBRARY is /Users/x/Library). OSError if it can't be opened."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if sys.platform == "darwin":
            raw = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1025)
            spelled = raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        else:
            spelled = os.path.realpath(path)
        return spelled, (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def canon_or_abs(path):
    try:
        return canon(path)[0]
    except OSError:
        return os.path.abspath(path)


def protected_paths(home=None, lab=None, ssh_dir=None, data=None, state=None):
    """(home, data, exact, trees): folders that may not be shared, and folders nothing inside may be shared
    from, each as (canonical spelling, identity or None). Their parents are refused as well (sharing a
    parent shares them)."""
    def c(p):
        try:
            return canon(p, directory=True)
        except OSError:
            return (os.path.abspath(p), None)
    home = c(home or HOME)[0]
    lab = lab or lab_root()
    ssh_dir = ssh_dir or env("SSH_DIR") or os.path.join(HOME, ".ssh", "metacomputer")
    data = c(data or data_dir())[0]
    state = state or state_home()
    j = os.path.join
    exact = [c(p) for p in (home, j(home, "Documents"), j(home, "Desktop"), j(home, "Downloads"), j(home, "Pictures"),
                            j(home, "Movies"), j(home, "Music"), j(home, "Public"), "/", "/Users", "/Volumes", "/tmp",
                            data, j(data, "volumes"), j(data, "refdisks"))]
    trees = [c(p) for p in (j(home, "Library"), j(home, ".ssh"), ssh_dir, lab, j(home, ".lume"), state, "/System",
                            "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/var",
                            "/private/var", "/dev", "/cores", "/opt/homebrew")]
    return home, data, exact, trees


def check_shareable(path, **where):
    """The canonical path of a Mac folder that may go into the VM (shared or packed into a reference
    disk), or StorageError."""
    return check_shareable_id(path, **where)[0]


def check_shareable_id(path, **where):
    """(canonical path, (device, inode)) of a folder that may go into the VM, or StorageError. The folder is
    opened, links resolved, and judged by the spelling the kernel gives it and by its identity, so another
    spelling (case, Unicode normalization, the data volume's firmlinks) or a link to a refused folder is
    refused like the folder itself."""
    check_host_path(path, "folder")
    if not os.path.isdir(path):
        raise StorageError(f"{path}: no such folder")
    try:
        real, ident = canon(path, directory=True)
    except OSError as e:
        raise StorageError(f"{path}: can't open it ({e.strerror})")
    home, data, exact, trees = protected_paths(**where)
    why = None
    for p, pid in exact + trees:
        if pid == ident or inside(p, real):      # the folder itself, or one of its parents
            why = p
            break
    if why is None:
        for t, _ in trees:
            if inside(real, t):
                why = t
                break
    if why is None and inside(real, home) and fold(real) != fold(home):
        first = real[len(home.rstrip("/")) + 1:].split("/")[0]
        if first.startswith(".") and not inside(real, os.path.join(data, "volumes")) \
                and not inside(real, os.path.join(data, "refdisks")):
            why = os.path.join(home, first)
    tmp = canon_or_abs("/tmp")
    if why is None and inside(real, tmp) and fold(real) != fold(tmp):
        first = real[len(tmp.rstrip("/")) + 1:].split("/")[0]
        if fold(first).startswith("mcomp-"):
            why = os.path.join(tmp, first)     # the gateway's and the hub's sockets
    if why is not None:
        shown = path if real == path else f"{path} (that is {real})"
        raise StorageError(f"{shown} can't go into the VM: it is or holds {why}, which stays private "
                           "(personal folders, Library, keys, this checkout, system folders). Use a folder of its own.")
    return real, ident


# --- mount points in the VM, and the VOLUMES / REFDISKS settings ----------------------
def valid_mountpoint(p, home=None):
    home = home or guest_home()
    if not isinstance(p, str) or not p.startswith("/") or len(p) > 255 or p != os.path.normpath(p):
        return False
    parts = p.strip("/").split("/")
    if len(parts) > 16 or not all(COMP_RE.match(c) for c in parts):
        return False
    if any(p == r or p.startswith(r + "/") for r in GUEST_ROOTS):
        return True
    return p.startswith(home + "/") and not p[len(home) + 1:].startswith(".")


def expand_guest(p, home=None):
    home = home or guest_home()
    if p == "~":
        return home
    return home + p[1:] if p.startswith("~/") else p


def parse_list(text, kind, home=None):
    """VOLUMES / REFDISKS: name[:mountpoint][:ro|rw], separated by commas or spaces."""
    home = home or guest_home()
    out, names = [], set()
    setting = "VOLUMES" if kind == "volume" else "REFDISKS"
    for raw in [t for t in re.split(r"[,\s]+", text or "") if t]:
        parts = raw.split(":")
        name, mount, mode = parts[0], None, None
        if not NAME_RE.match(name):
            raise StorageError(f"{setting}: {name!r} isn't a {kind} name")
        if name in names:
            raise StorageError(f"{setting}: {name} is listed twice")
        names.add(name)
        for p in parts[1:]:
            if p in ("ro", "rw"):
                if mode:
                    raise StorageError(f"{setting}: {raw}: one ro or rw")
                mode = p
            elif p.startswith(("/", "~")):
                if mount:
                    raise StorageError(f"{setting}: {raw}: one mount point")
                mount = p
            else:
                raise StorageError(f"{setting}: {raw}: {p!r} is neither a mount point (/..., ~/...) nor ro/rw")
        mount = expand_guest(mount or (("~/data/" if kind == "volume" else "~/ref/") + name), home)
        if not valid_mountpoint(mount, home):
            raise StorageError(f"{setting}: {raw}: the mount point {mount} isn't allowed in the VM. Use /volume, /mnt, "
                               f"/data, /srv, /workspace (or a folder in them), or a folder in {home} (not a dot-folder)")
        out.append({"name": name, "mount": mount, "mode": mode})
    return out


def overlapping(a, b):
    """Two mount points in the VM (Linux paths: compared as they are)."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def host_overlapping(a, b):
    """Two folders on the Mac, compared as the filesystem compares names."""
    return inside(a, b) or inside(b, a)


# --- locks ------------------------------------------------------------------------------
def locks_dir():
    d = os.path.join(state_home(), "locks")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


class GlobalLock:
    """One process at a time changes the locks (an flock on locks/.lock)."""

    def __enter__(self):
        self.f = open(os.path.join(locks_dir(), ".lock"), "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def proc_start(pid):
    """The process's start time as ps reports it ("" when there's no such process): with the pid it
    names one process, even after the pid is reused."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def lock_key(kind, path):
    """A lock's file name: the item's canonical spelling, folded, so every spelling of one file or folder
    (case, firmlinks, links) is one lock."""
    import hashlib
    return f"{kind}-" + hashlib.sha256(fold(canon_or_abs(path)).encode()).hexdigest()[:24]


def holder_alive(rec):
    return bool(rec and rec.get("pstart") and proc_start(rec.get("pid")) == rec["pstart"])


def same_holder(rec, vm, lab):
    return bool(rec) and rec.get("vm") == vm and rec.get("lab") == lab


def all_locks():
    out = []
    try:
        names = os.listdir(locks_dir())
    except OSError:
        return out
    for f in names:
        if f.endswith(".json"):
            rec = read_json(os.path.join(locks_dir(), f))
            if isinstance(rec, dict):
                rec["_file"] = f
                out.append(rec)
    return out


OPERATION = "operation:"


def describe_holder(rec):
    vm = rec.get("vm", "?")
    if vm.startswith(OPERATION):
        return f"a metacomputer command still working on it ({vm[len(OPERATION):]}, pid {rec.get('pid')})"
    where = "" if rec.get("lab") == lab_root() else f" (from {rec.get('lab')})"
    return f"VM '{vm}'{where}, since {rec.get('since', '?')}"


def sidecar_path(item):
    return item["path"] + ".lock"


def acquire(items, vm, lab, pid):
    """Lock every read-write item for this VM, or none. items: plan items with "lock" set.
    Returns the list of lock files written; raises StorageError naming each conflict."""
    want = [it for it in items if it.get("lock")]
    if not want:
        return []
    pstart = proc_start(pid)
    host = hostname()
    with GlobalLock():
        existing = all_locks()
        conflicts = []
        for it in want:
            key = it["lock"]
            for rec in existing:
                if same_holder(rec, vm, lab) or not holder_alive(rec):
                    continue
                if rec["_file"] == key + ".json" or (
                        it.get("lock_overlap") and rec.get("overlap") and host_overlapping(rec.get("path", ""), it["path"])):
                    conflicts.append(f"{it['kind']} '{it['name']}' is in use by {describe_holder(rec)}")
            if it["kind"] == "disk":
                side = read_json(sidecar_path(it))
                if isinstance(side, dict) and side.get("host") and side.get("host") != host:
                    conflicts.append(f"disk '{it['name']}': its lock file says computer '{side['host']}' has it "
                                     f"(VM '{side.get('vm', '?')}', since {side.get('since', '?')}). If that computer "
                                     f"isn't using it, delete {sidecar_path(it)}")
        if conflicts:
            raise StorageError("; ".join(conflicts) + ". Stop that VM first, or start this one with --no-volumes")
        written = []
        for it in want:
            rec = {"kind": it["kind"], "name": it["name"], "path": it["path"], "vm": vm, "lab": lab,
                   "pid": int(pid), "pstart": pstart, "host": host, "since": now(), "state": "starting",
                   "overlap": bool(it.get("lock_overlap"))}
            atomic_json(os.path.join(locks_dir(), it["lock"] + ".json"), rec)
            written.append(it["lock"])
            if it["kind"] == "disk":
                try:
                    atomic_json(sidecar_path(it), {"host": host, "vm": vm, "since": rec["since"]}, mode=0o644)
                except OSError:
                    pass
        return written


def update_holder(vm, lab, pid):
    pstart = proc_start(pid)
    if not pstart:
        return 0
    n = 0
    with GlobalLock():
        for rec in all_locks():
            if same_holder(rec, vm, lab):
                f = rec.pop("_file")
                rec.update(pid=int(pid), pstart=pstart, state="running")
                atomic_json(os.path.join(locks_dir(), f), rec)
                n += 1
    return n


def release_locks(vm, lab):
    n = 0
    host = hostname()
    with GlobalLock():
        for rec in all_locks():
            if same_holder(rec, vm, lab):
                try:
                    os.unlink(os.path.join(locks_dir(), rec["_file"]))
                    n += 1
                except FileNotFoundError:
                    pass
                if rec.get("kind") == "disk":
                    side = read_json(rec["path"] + ".lock")
                    if isinstance(side, dict) and side.get("host") == host and side.get("vm") == vm:
                        try:
                            os.unlink(rec["path"] + ".lock")
                        except OSError:
                            pass
    return n


@contextmanager
def reserved(recs, op):
    """Hold disk volumes for the length of an operation (checkpoint, restore, resize, delete): taken before
    anything is checked or changed, through the same locks a VM's start takes, so no VM can attach one
    meanwhile, and refused while a VM (or another operation) holds one."""
    items = [{"kind": "disk", "name": r["name"], "path": r["path"], "lock": lock_key("disk", r["path"])} for r in recs]
    holder = f"{OPERATION}{op}:{os.getpid()}"
    acquire(items, holder, lab_root(), os.getpid())
    try:
        yield
    finally:
        release_locks(holder, lab_root())


def holder_of(path, kind):
    """The live lock on an item, or None."""
    rec = read_json(os.path.join(locks_dir(), lock_key(kind, path) + ".json"))
    return rec if isinstance(rec, dict) and holder_alive(rec) else None


# --- the plan for one start ------------------------------------------------------------
def guest_features():
    p = os.path.join(env("SSH_DIR") or os.path.join(HOME, ".ssh", "metacomputer"), "guest-features")
    try:
        return dict(l.strip().split("=", 1) for l in open(p) if "=" in l)
    except OSError:
        return {}


REBUILD = ("VM '{vm}' was built before storage support, so it can't mount volumes or reference disks. "
           "Rebuild it (metacomputer build --replace: the VM and its baseline are rebuilt; volumes, reference "
           "disks and their files are kept), or start without storage: metacomputer start --no-volumes")


def label_for(rec):
    return "MC" + rec["uuid"].replace("-", "")[:16].upper()


def build_plan(no_volumes=False):
    """Everything this start would attach: (items, problems, notes). Nothing is changed."""
    items, problems, notes = [], [], []
    if no_volumes:
        return items, problems, notes
    home = guest_home()
    try:
        vols = parse_list(env("VOLUMES"), "volume", home)
        refs = parse_list(env("REFDISKS"), "refdisk", home)
    except StorageError as e:
        return items, [str(e)], notes
    for v in vols:
        rec = reg_load("volume", v["name"])
        if not rec:
            problems.append(f"VOLUMES: there's no volume named {v['name']} (metacomputer volume list)")
            continue
        it = plan_volume(rec, v, problems, notes)
        if it:
            items.append(it)
    for r in refs:
        rec = reg_load("refdisk", r["name"])
        if not rec:
            problems.append(f"REFDISKS: there's no reference disk named {r['name']} (metacomputer refdisk list)")
            continue
        if r["mode"] == "rw":
            problems.append(f"REFDISKS: {r['name']}: reference disks are read-only")
            continue
        path = rec.get("path", "")
        if not os.path.isfile(path):
            problems.append(f"reference disk '{r['name']}': {path} is missing (is its drive connected?)")
            continue
        try:
            path, ident = canon(path)
        except OSError as e:
            problems.append(f"reference disk '{r['name']}': can't open {path} ({e.strerror})")
            continue
        items.append({"kind": "refdisk", "name": r["name"], "path": path, "ident": list(ident), "mount": r["mount"],
                      "rw": False,
                      "label": label_for(rec)})
    mounts = []
    for it in items:
        for m in mounts:
            if overlapping(m, it["mount"]):
                problems.append(f"{it['kind']} '{it['name']}': its mount point {it['mount']} overlaps {m}")
        mounts.append(it["mount"])
    items, skipped = bounded_filter(items)
    for it in skipped:
        notes.append(f"bounded mode: {it['kind']} '{it['name']}' isn't attached: the driver's manifest doesn't list "
                     f"{it['mount']} ({'write' if it['rw'] else 'read'})")
    if items and guest_features().get("storage") != "1":
        problems.append(REBUILD.format(vm=vm_name()))
    return items, problems, notes


def plan_volume(rec, v, problems, notes):
    name, kind, path = rec["name"], rec.get("kind"), rec.get("path", "")
    if kind == "disk":
        if v["mode"] == "ro":
            problems.append(f"VOLUMES: {name}: disk volumes attach read-write to one VM; for a read-only copy "
                            "that several VMs share, use a reference disk")
            return None
        if not os.path.isfile(path):
            problems.append(f"volume '{name}': {path} is missing (is its drive connected?)")
            return None
        try:
            path, ident = canon(path)
        except OSError as e:
            problems.append(f"volume '{name}': can't open {path} ({e.strerror})")
            return None
        size = os.stat(path).st_size
        if size % 512 or size < MIN_SIZE or not stat.S_ISREG(os.stat(path).st_mode):
            problems.append(f"volume '{name}': {path} isn't a disk image metacomputer made")
            return None
        net = network_fs(path)
        if net:
            notes.append(f"volume '{name}' is on a network filesystem ({net[0]} at {net[1]}): if the connection drops "
                         "while the VM writes, the volume can be damaged, and locks don't hold across computers")
        return {"kind": "disk", "name": name, "path": path, "ident": list(ident), "mount": v["mount"], "rw": True,
                "size": size, "uuid": rec["uuid"], "lock": lock_key("disk", path)}
    problems.append(f"volume '{name}': unknown kind {kind!r}")
    return None


def bounded_filter(items):
    """DRIVER_MODE=bounded: only what the manifest's files section lists is attached."""
    if env("DRIVER_MODE", "standard") != "bounded" or not items:
        return items, []
    try:
        import driver_profiles
        read, write = manifest_dirs(driver_profiles.render())
    except Exception:  # noqa: BLE001 - a manifest that can't be read exactly grants nothing
        read, write = [], []
    keep, skipped = [], []
    for it in items:
        allowed = write if it["rw"] else read + write
        if any(covered(it["mount"], d, rec) for d, rec in allowed):
            keep.append(it)
        else:
            skipped.append(it)
    return keep, skipped


def covered(mount, d, recursive):
    return mount == d or (recursive and mount.startswith(d.rstrip("/") + "/"))


class ManifestError(ValueError):
    pass


def yaml_subset(text):
    """Parse the YAML subset capability manifests use: block mappings and sequences by indentation, flow
    mappings {k: v} and sequences [a, b], plain and quoted scalars, comments. Anything else (anchors,
    aliases, tags, block scalars, several documents, tabs, duplicate keys) is a ManifestError: storage
    permissions are derived only from a manifest read exactly."""
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise ManifestError(f"line {n}: a tab in the indentation")
        body = _strip_comment(raw, n).rstrip()
        if not body.strip():
            continue
        if body.strip() in ("---", "..."):
            if lines:
                raise ManifestError(f"line {n}: more than one document")
            continue
        lines.append((len(body) - len(body.lstrip()), body.strip(), n))
    if not lines:
        return None
    val, i = _block(lines, 0, lines[0][0])
    if i != len(lines):
        raise ManifestError(f"line {lines[i][2]}: unexpected indentation")
    return val


def _strip_comment(line, n):
    out, q = [], None
    for k, ch in enumerate(line):
        if q:
            out.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "'\"":
            q = ch
        elif ch == "#" and (k == 0 or line[k - 1] in " \t"):
            break
        out.append(ch)
    if q:
        raise ManifestError(f"line {n}: an unterminated quote")
    return "".join(out)


def _block(lines, i, indent):
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        out = []
        while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
            rest, n = lines[i][1][1:].lstrip(), lines[i][2]
            if not rest:
                if i + 1 >= len(lines) or lines[i + 1][0] <= indent:
                    raise ManifestError(f"line {n}: an empty list item")
                v, i = _block(lines, i + 1, lines[i + 1][0])
                out.append(v)
                continue
            if _is_key(rest):
                # "- key: value" starts a mapping whose other keys sit under the key
                sub = [(indent + 2 + (len(lines[i][1]) - len(lines[i][1][1:].lstrip()) - 1), rest, n)]
                j = i + 1
                while j < len(lines) and lines[j][0] > indent:
                    sub.append(lines[j])
                    j += 1
                v, k = _block(sub, 0, sub[0][0])
                if k != len(sub):
                    raise ManifestError(f"line {sub[k][2]}: unexpected indentation")
                out.append(v)
                i = j
                continue
            out.append(_scalar_or_flow(rest, n))
            i += 1
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        body, n = lines[i][1], lines[i][2]
        if body.startswith("- "):
            raise ManifestError(f"line {n}: a list item where a key was expected")
        key, rest = _split_key(body, n)
        if key in out:
            raise ManifestError(f"line {n}: the key {key!r} twice")
        if rest:
            out[key] = _scalar_or_flow(rest, n)
            i += 1
        elif i + 1 < len(lines) and lines[i + 1][0] > indent:
            out[key], i = _block(lines, i + 1, lines[i + 1][0])
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            out[key], i = _block(lines, i + 1, indent)
        else:
            out[key] = None
            i += 1
    if i < len(lines) and lines[i][0] > indent:
        raise ManifestError(f"line {lines[i][2]}: unexpected indentation")
    return out, i


def _is_key(text):
    try:
        _split_key(text, 0)
        return True
    except ManifestError:
        return False


def _split_key(text, n):
    if text[:1] in "'\"":
        q = text[0]
        end = text.find(q, 1)
        if end < 0 or text[end + 1:end + 2] != ":":
            raise ManifestError(f"line {n}: not a key")
        key, rest = text[1:end], text[end + 2:]
    else:
        k = text.find(": ")
        if text.endswith(":") and (k < 0 or k > len(text) - 2):
            k = len(text) - 1
        if k <= 0:
            raise ManifestError(f"line {n}: not a key: value")
        key, rest = text[:k], text[k + 1:]
        if any(c in key for c in "{}[],&*!|>%@`"):
            raise ManifestError(f"line {n}: an unsupported key {key!r}")
    if rest and not rest.startswith(" "):
        raise ManifestError(f"line {n}: not a key: value")
    return key.strip(), rest.strip()


def _scalar_or_flow(text, n):
    if text[0] in "{[":
        v, k = _flow(text, 0, n)
        if text[k:].strip():
            raise ManifestError(f"line {n}: text after a closing bracket")
        return v
    return _scalar(text, n)


def _scalar(text, n):
    text = text.strip()
    if not text:
        raise ManifestError(f"line {n}: an empty value")
    if text[0] in "&*!|>%@`":
        raise ManifestError(f"line {n}: unsupported YAML ({text[0]})")
    if text[0] in "'\"":
        q = text[0]
        if len(text) < 2 or text[-1] != q or q in text[1:-1].replace("\\\\", "").replace('\\"', ""):
            raise ManifestError(f"line {n}: a bad quoted string")
        inner = text[1:-1]
        if q == '"':
            if "\\" in inner.replace("\\\\", "").replace('\\"', ""):
                raise ManifestError(f"line {n}: an unsupported escape")
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text in ("null", "Null", "NULL", "~"):
        return None
    if ": " in text or text.endswith(":"):
        raise ManifestError(f"line {n}: a mapping where a value was expected")
    return text


def _flow(text, k, n):
    """A flow mapping or sequence starting at text[k]; returns (value, index after it)."""
    opener = text[k]
    closer = "}" if opener == "{" else "]"
    k += 1
    out = {} if opener == "{" else []
    while True:
        while k < len(text) and text[k] == " ":
            k += 1
        if k >= len(text):
            raise ManifestError(f"line {n}: an unclosed {opener}")
        if text[k] == closer:
            return out, k + 1
        if text[k] in "{[":
            if opener == "{":
                raise ManifestError(f"line {n}: a nested key in a flow mapping")
            v, k = _flow(text, k, n)
            out.append(v)
        else:
            depth, q, j = 0, None, k
            while j < len(text):
                c = text[j]
                if q:
                    if c == q:
                        q = None
                elif c in "'\"":
                    q = c
                elif c in "{[":
                    depth += 1
                elif c in "}]":
                    if depth == 0:
                        break
                    depth -= 1
                elif c == "," and depth == 0:
                    break
                j += 1
            item = text[k:j].strip()
            if opener == "{":
                key, rest = _split_key(item, n) if ": " in item else (None, None)
                if key is None:
                    raise ManifestError(f"line {n}: {item!r} isn't key: value")
                if key in out:
                    raise ManifestError(f"line {n}: the key {key!r} twice")
                out[key] = _scalar_or_flow(rest, n) if rest else None
            else:
                out.append(_scalar_or_flow(item, n))
            k = j
        while k < len(text) and text[k] == " ":
            k += 1
        if k < len(text) and text[k] == ",":
            k += 1
        elif k < len(text) and text[k] != closer:
            raise ManifestError(f"line {n}: expected , or {closer}")


def manifest_dirs(text):
    """The read and write folders of a Cua capability manifest, exactly from resources.files.read and
    .write: lists of {dir: ABSOLUTE PATH, recursive: true|false}. A manifest that isn't in that form grants
    nothing (ManifestError)."""
    doc = yaml_subset(text)
    if not isinstance(doc, dict):
        raise ManifestError("the manifest isn't a mapping")
    res = doc.get("resources")
    if res is None:
        return [], []
    if not isinstance(res, dict):
        raise ManifestError("resources isn't a mapping")
    files = res.get("files")
    if files is None:
        return [], []
    if not isinstance(files, dict) or set(files) - {"read", "write"}:
        raise ManifestError("resources.files has keys other than read and write")
    out = []
    for section in ("read", "write"):
        entries = files.get(section) or []
        if not isinstance(entries, list):
            raise ManifestError(f"resources.files.{section} isn't a list")
        got = []
        for e in entries:
            if not isinstance(e, dict) or set(e) - {"dir", "recursive"} or not isinstance(e.get("dir"), str):
                raise ManifestError(f"resources.files.{section}: an entry that isn't {{dir: PATH, recursive: BOOL}}")
            rec = e.get("recursive", False)
            if not isinstance(rec, bool):
                raise ManifestError(f"resources.files.{section}: recursive must be true or false")
            d = e["dir"]
            if not d.startswith("/") or os.path.normpath(d) != d.rstrip("/") and d != "/":
                raise ManifestError(f"resources.files.{section}: {d!r} isn't an absolute, normal path")
            got.append((d.rstrip("/") or "/", rec))
        out.append(got)
    return out[0], out[1]


def lume_args(items):
    args, refs = [], 0
    for it in items:
        if it["kind"] == "disk":
            args += ["--disk", it["path"]]
    for it in items:
        if it["kind"] == "refdisk":
            args += ["--mount" if refs == 0 else "--usb-storage", it["path"]]
            refs += 1
    return args


def guest_plan(items):
    plan = {"version": PROTOCOL, "disks": [], "refdisks": [], "folders": []}
    for it in items:
        if it["kind"] == "disk":
            plan["disks"].append({"name": it["name"], "uuid": it["uuid"], "size": it["size"], "mount": it["mount"]})
        elif it["kind"] == "refdisk":
            plan["refdisks"].append({"name": it["name"], "label": it["label"], "mount": it["mount"]})
    return plan


def state_files():
    d = run_dir()
    return {"state": os.path.join(d, "storage-state.json"), "args": os.path.join(d, "storage-args"),
            "plan": os.path.join(d, "storage-plan.json")}


def clear_state():
    for p in state_files().values():
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


def cmd_prepare(a):
    clear_state()
    items, problems, notes = build_plan(a.no_volumes)
    for n in notes:
        print(f"  ! {esc(n)}", file=sys.stderr)
    if problems:
        raise StorageError("not starting: " + "\n  ".join(problems)
                           + "\n  (to start without any storage: metacomputer start --no-volumes)")
    if not items:
        return 0
    vm, lab = vm_name(), lab_root()
    acquire(items, vm, lab, a.pid)
    f = state_files()
    atomic_json(f["plan"], guest_plan(items))
    atomic_json(f["state"], {"vm": vm, "lab": lab, "prepared": now(), "items": items, "attached": None})
    with open(f["args"], "w") as out:
        out.write("".join(x + "\n" for x in lume_args(items)))
    return 0


def cmd_started(a):
    st = load_state()
    if not st:
        return 0
    update_holder(vm_name(), lab_root(), a.pid)
    changed = changed_items(st.get("items", []))
    if changed:
        print("metacomputer: the VM was started, but " + "; ".join(changed) + ": it's no longer what was checked. "
              "Stopping it.", file=sys.stderr)
        return 1
    return 0


def changed_items(items):
    """Items whose path no longer leads to the file or folder that was checked (same device and inode)."""
    out = []
    for it in items:
        want = it.get("ident")
        if not want:
            continue
        try:
            st = os.stat(it["path"])
            got = [st.st_dev, st.st_ino]
        except OSError:
            got = None
        if got != list(want):
            out.append(f"{it['kind']} '{it['name']}' at {it['path']} changed after it was checked")
    return out


def load_state():
    st = read_json(state_files()["state"])
    return st if isinstance(st, dict) else None


GUEST_REPLY_MAX = 64 * 1024
ADMIN = "sudo -n /usr/local/sbin/metacomputer-admin storage "


def guest_call(ssh, verb, stdin=b"", timeout=900, max_out=GUEST_REPLY_MAX):
    """Run `metacomputer-admin storage VERB` in the VM over the given ssh command. Its answer is untrusted:
    read as it comes, at most max_out bytes, within timeout seconds; past either the ssh is ended.
    Returns (exit code, stdout text, why it was cut short or "")."""
    import boundedrun
    try:
        rc, out, err = boundedrun.run(list(ssh) + [ADMIN + verb], stdin=stdin, max_out=max_out, max_err=16384,
                                      timeout=timeout)
    except boundedrun.Overflow:
        return 1, "", f"the VM's answer was over {max_out} bytes"
    except boundedrun.Timeout:
        return 1, "", f"the VM didn't answer within {timeout} s"
    return rc, out.decode("utf-8", "replace"), ""


def cmd_attach(a):
    """Mount this start's storage in the VM (bin/metacomputer gives the admin ssh command). Exit 3 when the VM
    can't (built before storage support), 1 when it didn't mount it all."""
    plan = state_files()["plan"]
    if not os.path.exists(plan):
        return 0
    n = a.transport
    ssh = a.rest[:n]
    rc, out, why = guest_call(ssh, "version", timeout=60, max_out=4096)
    if rc != 0 or out.strip().splitlines()[-1:] != [str(PROTOCOL)]:
        if why:
            print(f"metacomputer: {why}", file=sys.stderr)
        return 3
    rc, out, why = guest_call(ssh, "attach", stdin=open(plan, "rb").read())
    if why:
        print(f"metacomputer: the VM didn't mount its storage: {why}", file=sys.stderr)
        return 1
    return attached(out)


def cmd_attached(a):
    """The guest's answer to `storage attach`, on stdin (as cmd_attach reads it)."""
    return attached(sys.stdin.read(GUEST_REPLY_MAX + 1))


def attached(data):
    """The guest's answer to `storage attach` (untrusted: checked, bounded, escaped)."""
    st = load_state()
    data = data[:GUEST_REPLY_MAX + 1]
    try:
        res = json.loads(data.strip().splitlines()[-1]) if data.strip() else None
    except (ValueError, IndexError):
        res = None
    if not isinstance(res, dict) or res.get("ok") is not True or not isinstance(res.get("items"), list):
        why = res.get("error") if isinstance(res, dict) and isinstance(res.get("error"), str) else "no usable answer"
        print(f"metacomputer: the VM didn't mount its storage: {esc(why[:500])}", file=sys.stderr)
        return 1
    want = {(it["kind"], it["name"]): it for it in (st or {}).get("items", [])}
    got = {}
    for g in res["items"][:64]:
        if isinstance(g, dict) and g.get("ok") is True:
            got[(str(g.get("kind")), str(g.get("name")))] = g
    missing = [k for k in want if k not in got]
    if missing:
        print("metacomputer: the VM didn't report mounting: " + ", ".join(f"{k} '{n}'" for k, n in missing),
              file=sys.stderr)
        return 1
    for key, it in want.items():
        g = got[key]
        extra = []
        if g.get("formatted") is True:
            extra.append("formatted (first use)")
        if g.get("grown") is True:
            extra.append("grown to its new size")
        mode = "rw" if it["rw"] else "ro"
        print(f"  ✓ {it['kind']} '{it['name']}' at {it['mount']} ({mode})" + (f", {', '.join(extra)}" if extra else ""))
    st["attached"] = now()
    atomic_json(state_files()["state"], st)
    emit("lifecycle", "storage_attached",
         f"Storage attached to {vm_name()}: " + ", ".join(f"{it['kind']} {it['name']} ({'rw' if it['rw'] else 'ro'})"
                                                         for it in want.values()),
         data={"items": [{"kind": it["kind"], "name": it["name"], "mount": it["mount"], "rw": it["rw"],
                          "path": it["path"]} for it in want.values()]})
    return 0


def cmd_release(a):
    st = load_state()
    n = release_locks(vm_name(), lab_root())
    clear_state()
    if st and st.get("items") and not a.quiet:
        print(f"  ✓ storage released ({n} lock{'s' if n != 1 else ''})")
    return 0


def emit(family, type_, message, severity=None, data=None, action=None):
    try:
        import events
        events.emit(family, type_, message, severity, action, data)
    except Exception:  # noqa: BLE001 - events never fail a command
        pass


# --- disk volumes ------------------------------------------------------------------------
def target_dir(sub, given):
    """Where a new image goes: --dir (must exist; it may be a drive that isn't connected) or
    STORAGE_DIR/sub (made if missing)."""
    if given:
        d = os.path.abspath(os.path.expanduser(check_host_path(given, "--dir")))
        if not os.path.isdir(d):
            raise StorageError(f"--dir {d}: no such folder (is the drive connected?)")
        return d
    d = os.path.join(data_dir(), sub)
    if not os.path.isdir(os.path.dirname(data_dir())) and env("STORAGE_DIR"):
        raise StorageError(f"STORAGE_DIR {data_dir()}: its parent folder doesn't exist (is the drive connected?)")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def cmd_volume_create(a):
    check_name(a.name)
    if reg_load("volume", a.name):
        raise StorageError(f"there's already a volume named {a.name} (metacomputer volume list)")
    if not a.size:
        raise StorageError("usage: metacomputer volume create NAME --size 20G [--dir PATH]")
    size = parse_size(a.size)
    d = target_dir("volumes", a.dir)
    path = os.path.join(d, a.name + ".img")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise StorageError(f"{path} already exists; not touching it")
    try:
        os.ftruncate(fd, size)
    finally:
        os.close(fd)
    rec = {"name": a.name, "kind": "disk", "path": path, "uuid": str(uuid.uuid4()), "created": now()}
    reg_save("volume", rec)
    st = os.statvfs(d)
    free = st.f_bavail * st.f_frsize
    print(f"volume '{a.name}': {human(size)} disk image at {esc(path)} (grows as it fills; {human(free)} free there)")
    net = network_fs(path)
    if net:
        print(f"  ! it's on a network filesystem ({net[0]} at {esc(net[1])}): if the connection drops while the VM "
              "writes, the volume can be damaged, and locks don't hold across computers")
    print(f"Attach it: VOLUMES={a.name} in config/local.env (in the VM at ~/data/{a.name}; "
          f"VOLUMES={a.name}:/volume for another place). It's formatted inside the VM on first use.")
    note_rebuild()
    return 0


def note_rebuild():
    if guest_features().get("storage") != "1":
        print(f"  ! {REBUILD.format(vm=vm_name()).split('. Rebuild')[0]}: rebuild it before attaching "
              "(metacomputer build --replace)")


def volume_state(rec):
    path = rec.get("path", "")
    if rec.get("kind") == "disk":
        if not os.path.isfile(path):
            return "missing (is its drive connected?)"
        h = holder_of(path, "disk")
        return f"in use by {describe_holder(h)}" if h else "free"
    return "?"


def cmd_volume_list(a):
    vols = reg_all("volume")
    rows = []
    for rec in vols:
        row = {"name": rec["name"], "kind": rec.get("kind"), "path": rec.get("path"), "state": volume_state(rec)}
        if rec.get("kind") == "disk" and os.path.isfile(rec.get("path", "")):
            st = os.stat(rec["path"])
            row.update(size=st.st_size, used=st.st_blocks * 512)
        rows.append(row)
    if a.json:
        print(json.dumps({"volumes": rows, "storage_dir": data_dir(), "configured": env("VOLUMES")}, indent=2))
        return 0
    if not rows:
        print("no volumes (metacomputer volume create NAME --size 20G)")
        return 0
    configured = {e["name"]: e for e in safe_parse(env("VOLUMES"), "volume")}
    for r in rows:
        size = f"{human(r['size'])} ({human(r['used'])} used)" if "size" in r else "-"
        att = f"  attached here at {configured[r['name']]['mount']}" if r["name"] in configured else ""
        print(f"{r['name']:<16} {r['kind']:<7} {size:<26} {esc(r['state'])}{att}")
        print(f"{'':<16} {esc(r['path'] or '')}")
    return 0


def safe_parse(text, kind):
    try:
        return parse_list(text, kind)
    except StorageError:
        return []


def snapshots_of(name):
    """(checkpoint manifest path, snapshot file) for every checkpoint that holds this volume."""
    out = []
    root = os.path.join(state_home(), "checkpoints")
    for d, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(".json"):
                m = read_json(os.path.join(d, f))
                for v in (m or {}).get("volumes", []):
                    if v.get("name") == name:
                        out.append((os.path.join(d, f), v.get("snapshot")))
    return out


def cmd_volume_delete(a):
    rec = reg_load("volume", check_name(a.name))
    if not rec:
        raise StorageError(f"no volume named {a.name} (metacomputer volume list)")
    if rec.get("kind") == "disk":
        with reserved([rec], "delete"):
            delete_disk(a, rec)
    reg_delete("volume", a.name)
    print(f"volume '{a.name}' deleted")
    if a.name in {e["name"] for e in safe_parse(env("VOLUMES"), "volume")}:
        print(f"  ! VOLUMES in your config still lists {a.name}: remove it, or start refuses")
    return 0


def delete_disk(a, rec):
    """With the volume held (reserved)."""
    snaps = snapshots_of(a.name)
    extra = f" and its {len(snaps)} checkpoint snapshot(s)" if snaps else ""
    confirm(f"Delete volume '{a.name}' ({rec['path']}){extra}? Its files are gone for good", a.yes)
    for m, snap in snaps:
        if snap:
            try:
                os.unlink(snap)
            except FileNotFoundError:
                pass
        man = read_json(m) or {}
        man["volumes"] = [v for v in man.get("volumes", []) if v.get("name") != a.name]
        atomic_json(m, man)
    for path in (rec["path"], rec["path"] + ".lock"):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def cmd_volume_resize(a):
    rec = reg_load("volume", check_name(a.name))
    if not rec:
        raise StorageError(f"no volume named {a.name} (metacomputer volume list)")
    if rec.get("kind") != "disk":
        raise StorageError(f"volume '{a.name}' isn't a disk volume; only disk volumes have a size")
    if not a.size:
        raise StorageError("usage: metacomputer volume resize NAME --size 40G")
    size = parse_size(a.size)
    path = rec["path"]
    if not os.path.isfile(path):
        raise StorageError(f"{path} is missing (is its drive connected?)")
    with reserved([rec], "resize"):
        return resize_volume(a, rec, size)


def resize_volume(a, rec, size):
    """With the volume held (reserved)."""
    path = rec["path"]
    cur = os.stat(path).st_size
    if size < cur:
        raise StorageError(f"volume '{a.name}' is {human(cur)}; volumes only grow (shrinking could lose files)")
    if size == cur:
        print(f"volume '{a.name}' is already {human(cur)}")
        return 0
    with open(path, "r+b") as f:
        os.ftruncate(f.fileno(), size)
    print(f"volume '{a.name}': {human(cur)} -> {human(size)}; its filesystem grows inside the VM at the next start")
    return 0


# --- reference disks -----------------------------------------------------------------------
def clone_or_copy(src, dst):
    """APFS clonefile(2) when src and dst share an APFS volume, else a copy (sparse-aware for images)."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        if libc.clonefile(src.encode(), dst.encode(), 0) == 0:
            return "clone"
        e = ctypes.get_errno()
        if e not in (errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS):
            raise OSError(e, os.strerror(e), dst)
    sparse_copy(src, dst)
    return "copy"


def data_ranges(fd, size):
    """(start, end) of the parts of a file that hold data (SEEK_DATA/SEEK_HOLE); the whole file where
    the filesystem can't tell."""
    sd, sh = getattr(os, "SEEK_DATA", None), getattr(os, "SEEK_HOLE", None)
    if sd is None:
        return [(0, size)]
    out, pos = [], 0
    while pos < size:
        try:
            start = os.lseek(fd, pos, sd)
        except OSError as e:
            if e.errno == errno.ENXIO:          # nothing but a hole from here
                break
            return [(0, size)]
        end = os.lseek(fd, start, sh)
        out.append((start, end))
        pos = end
    return out


def sparse_copy(src, dst):
    with open(src, "rb") as fi, open(dst, "xb") as fo:
        size = os.fstat(fi.fileno()).st_size
        for start, end in data_ranges(fi.fileno(), size):
            fi.seek(start)
            fo.seek(start)
            pos = start
            while pos < end:
                chunk = fi.read(min(8 * MIB, end - pos))
                if not chunk:
                    break
                fo.write(chunk)
                pos += len(chunk)
        fo.truncate(size)
    shutil.copystat(src, dst)


DIR_NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
STAGE_MAX_DEPTH = 64


def copy_from_fd(sfd, dfd, name, st):
    """Copy the open file sfd to name in the folder dfd: an APFS clone of the descriptor when it can
    (fclonefileat), else its bytes. The source is never looked up by path again."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        if libc.fclonefileat(sfd, dfd, name.encode("utf-8", "surrogateescape"), 0) == 0:
            return
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        os.lseek(sfd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(sfd, 8 * MIB)
            if not chunk:
                break
            os.write(out, chunk)
    finally:
        os.close(out)


def stage_tree(src, dst, ident=None):
    """Copy a folder's plain files and folders into dst, leaving out links and special files; returns
    (files, bytes, left_out). Everything is reached through descriptors, from the folder's own (checked
    against ident when given) down: each entry is opened without following links and must still be the
    file or folder that was listed, so a file swapped for a link mid-copy is left out, not followed."""
    files = size = 0
    left = []
    sroot = os.open(src, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    droot = os.open(dst, DIR_NOFOLLOW)

    def walk(sfd, dfd, rel, depth):
        nonlocal files, size
        if depth > STAGE_MAX_DEPTH:
            left.append(rel)
            return
        with os.scandir(sfd) as it:
            names = [e.name for e in it]
        for n in names:
            r = f"{rel}/{n}" if rel else n
            if n == ".DS_Store":
                continue
            try:
                st = os.stat(n, dir_fd=sfd, follow_symlinks=False)
            except OSError:
                left.append(r)
                continue
            if stat.S_ISDIR(st.st_mode):
                try:
                    cfd = os.open(n, DIR_NOFOLLOW, dir_fd=sfd)
                except OSError:                       # replaced by a link (ELOOP), or gone
                    left.append(r)
                    continue
                try:
                    cst = os.fstat(cfd)
                    if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino):
                        left.append(r)
                        continue
                    os.mkdir(n, 0o755, dir_fd=dfd)
                    nfd = os.open(n, DIR_NOFOLLOW, dir_fd=dfd)
                    try:
                        walk(cfd, nfd, r, depth + 1)
                    finally:
                        os.close(nfd)
                finally:
                    os.close(cfd)
            elif stat.S_ISREG(st.st_mode):
                try:
                    ffd = os.open(n, FILE_NOFOLLOW, dir_fd=sfd)
                except OSError:
                    left.append(r)
                    continue
                try:
                    fst = os.fstat(ffd)
                    if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                        left.append(r)
                        continue
                    copy_from_fd(ffd, dfd, n, fst)
                    os.chmod(n, stat.S_IMODE(fst.st_mode) & 0o777, dir_fd=dfd, follow_symlinks=False)
                    files += 1
                    size += fst.st_size
                finally:
                    os.close(ffd)
            else:
                left.append(r)
    try:
        if ident is not None:
            rst = os.fstat(sroot)
            if (rst.st_dev, rst.st_ino) != tuple(ident):
                raise StorageError(f"{src} changed after it was checked; nothing was packed")
        walk(sroot, droot, "", 0)
    finally:
        os.close(sroot)
        os.close(droot)
    return files, size, left


def cmd_refdisk_create(a):
    check_name(a.name, "reference disk")
    if reg_load("refdisk", a.name):
        raise StorageError(f"there's already a reference disk named {a.name} (metacomputer refdisk list)")
    if not a.src:
        raise StorageError("usage: metacomputer refdisk create NAME --from FOLDER [--dir PATH]")
    src, src_ident = check_shareable_id(os.path.abspath(os.path.expanduser(a.src)))
    d = target_dir("refdisks", a.dir)
    out = os.path.join(d, a.name + ".iso")
    if os.path.lexists(out):
        raise StorageError(f"{out} already exists; not touching it")
    rec = {"name": a.name, "kind": "refdisk", "path": out, "uuid": str(uuid.uuid4()), "source": src,
           "created": now()}
    label = label_for(rec)
    stage = tempfile.mkdtemp(dir=d, prefix=f".{a.name}.staging-")
    tmp = os.path.join(d, f".{a.name}.{uuid.uuid4().hex[:8]}.iso")
    try:
        files, size, left = stage_tree(src, stage, src_ident)
        r = subprocess.run(["hdiutil", "makehybrid", "-quiet", "-o", tmp, stage, "-iso", "-joliet", "-udf",
                            "-udf-version", "1.50", "-default-volume-name", label, "-iso-volume-name", label,
                            "-joliet-volume-name", label, "-udf-volume-name", label],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(tmp):
            raise StorageError(f"hdiutil makehybrid failed: {esc(r.stderr.strip()[:300])}")
        os.chmod(tmp, 0o444)
        os.rename(tmp, out)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if os.path.exists(tmp):
            os.unlink(tmp)
    rec.update(files=files, bytes=size, left_out=len(left))
    reg_save("refdisk", rec)
    print(f"reference disk '{a.name}': {files} files, {human(size)}, packed from {esc(src)} into {esc(out)} (read-only)")
    if left:
        print(f"  ! left out {len(left)} link(s) or special file(s): " + ", ".join(esc(x) for x in left[:5])
              + (" ..." if len(left) > 5 else ""))
    print(f"Attach it: REFDISKS={a.name} in config/local.env (in the VM at ~/ref/{a.name}; "
          f"REFDISKS={a.name}:/mnt/{a.name} for another place). Any number of VMs can attach it.")
    note_rebuild()
    return 0


def cmd_refdisk_list(a):
    refs = reg_all("refdisk")
    if a.json:
        print(json.dumps({"refdisks": [dict(r, present=os.path.isfile(r.get("path", ""))) for r in refs],
                          "configured": env("REFDISKS")}, indent=2))
        return 0
    if not refs:
        print("no reference disks (metacomputer refdisk create NAME --from FOLDER)")
        return 0
    configured = {e["name"]: e for e in safe_parse(env("REFDISKS"), "refdisk")}
    for r in refs:
        present = os.path.isfile(r.get("path", ""))
        att = f"  attached here at {configured[r['name']]['mount']}" if r["name"] in configured else ""
        print(f"{r['name']:<16} {r.get('files', '?')} files, {human(r.get('bytes', 0)):<10} "
              f"{'' if present else 'missing (is its drive connected?)'}{att}")
        print(f"{'':<16} {esc(r.get('path', ''))}  (from {esc(r.get('source', '?'))})")
    return 0


def cmd_refdisk_delete(a):
    rec = reg_load("refdisk", check_name(a.name, "reference disk"))
    if not rec:
        raise StorageError(f"no reference disk named {a.name} (metacomputer refdisk list)")
    confirm(f"Delete reference disk '{a.name}' ({rec['path']})? A VM using it keeps it until it stops", a.yes)
    try:
        os.chmod(rec["path"], 0o600)
        os.unlink(rec["path"])
    except FileNotFoundError:
        pass
    reg_delete("refdisk", a.name)
    print(f"reference disk '{a.name}' deleted (the folder it was packed from is untouched)")
    return 0


# --- checkpoints with volumes ----------------------------------------------------------------
def ckpt_manifest(vm, name):
    return os.path.join(state_home(), "checkpoints", vm, name + ".json")


def snapshot_path(vol_path, vm, name):
    d, base = os.path.split(vol_path)
    return os.path.join(d, f".{base}.ckpt-{vm}-{name}")


def cmd_checkpoint(a):
    vm = vm_name()
    man_path = ckpt_manifest(vm, a.name)
    if a.op == "list":
        man = read_json(man_path)
        print(" ".join(v["name"] for v in (man or {}).get("volumes", [])))
        return 0
    if a.op == "delete":
        man = read_json(man_path) or {}
        for v in man.get("volumes", []):
            try:
                os.unlink(v["snapshot"])
            except (FileNotFoundError, KeyError):
                pass
        try:
            os.unlink(man_path)
        except FileNotFoundError:
            pass
        return 0
    if a.op == "save":
        vols = []
        for e in safe_parse(env("VOLUMES"), "volume"):
            rec = reg_load("volume", e["name"])
            if rec and rec.get("kind") == "disk":
                vols.append(rec)
            elif rec:
                print(f"  ! volume '{e['name']}' is a {rec.get('kind')} volume: checkpoints hold disk volumes only",
                      file=sys.stderr)
        if not vols:
            raise StorageError("--with-volumes: VOLUMES lists no disk volumes for this VM")
        with reserved(vols, "checkpoint"):
            return save_checkpoint(vols, vm, a.name, man_path)
    if a.op == "restore":
        man = read_json(man_path)
        if not man:
            return 0
        with reserved(man.get("volumes", []), "restore"):
            return restore_checkpoint(man)
    raise StorageError("usage: storage.py checkpoint save|restore|delete|list NAME")


def save_checkpoint(vols, vm, name, man_path):
    """With the volumes held (reserved): no VM can attach one until it's done."""
    for rec in vols:
        if not os.path.isfile(rec["path"]):
            raise StorageError(f"volume '{rec['name']}': {rec['path']} is missing (is its drive connected?)")
    old = read_json(man_path) or {}
    for v in old.get("volumes", []):
        try:
            os.unlink(v["snapshot"])
        except (FileNotFoundError, KeyError):
            pass
    out = []
    for rec in vols:
        snap = snapshot_path(rec["path"], vm, name)
        if os.path.lexists(snap):
            os.unlink(snap)
        how = clone_or_copy(rec["path"], snap)
        out.append({"name": rec["name"], "uuid": rec["uuid"], "path": rec["path"], "snapshot": snap, "how": how})
        print(f"  ✓ volume '{rec['name']}' {'cloned' if how == 'clone' else 'copied'} with the checkpoint")
    atomic_json(man_path, {"vm": vm, "checkpoint": name, "saved": now(), "volumes": out})
    return 0


def restore_checkpoint(man):
    """With the volumes held (reserved)."""
    for v in man.get("volumes", []):
        if not os.path.isfile(v.get("snapshot", "")):
            raise StorageError(f"volume '{v['name']}': its snapshot {v.get('snapshot')} is missing")
    for v in man.get("volumes", []):
        tmp = v["path"] + ".restoring"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        clone_or_copy(v["snapshot"], tmp)
        os.replace(tmp, v["path"])
        if not reg_load("volume", v["name"]):
            reg_save("volume", {"name": v["name"], "kind": "disk", "path": v["path"], "uuid": v["uuid"],
                                "created": now()})
        print(f"  ✓ volume '{v['name']}' rolled back with the checkpoint")
    return 0
def status():
    st = load_state() or {}
    configured = bool((env("VOLUMES") or "").strip() or (env("REFDISKS") or "").strip())
    return {"configured": configured, "volumes": env("VOLUMES"), "refdisks": env("REFDISKS"),
            "guest_features": guest_features().get("storage") == "1", "items": st.get("items", []),
            "attached": st.get("attached")}


def describe_status(host, guest, running):
    """Lines for `metacomputer status` (host: status(); guest: guest-status's "storage", or None)."""
    lines = []
    supported = (guest or {}).get("supported") if guest is not None else host.get("guest_features")
    if not host.get("configured") and not host.get("items"):
        return lines
    if not supported:
        lines.append("storage       \033[33mthis VM was built before storage support\033[0m: VOLUMES and REFDISKS need a "
                     "rebuild (metacomputer build --replace; volumes are kept)")
        return lines
    if not host.get("items"):
        lines.append("storage       nothing attached" + (" (started with --no-volumes, or before it was configured; "
                                                         "metacomputer restart attaches it)" if running else ""))
        return lines
    mounted = {(g.get("kind"), g.get("name")): g.get("mounted") for g in (guest or {}).get("items", [])}
    lines.append(f"storage       {len(host['items'])} attached   (metacomputer volume list)")
    for it in host["items"]:
        m = mounted.get((it["kind"], it["name"]))
        mark = "\033[32m✓\033[0m" if m else ("\033[31m✗\033[0m" if running else "-")
        lines.append(f"  {mark} {it['kind']:<8} {it['name']:<14} {it['mount']:<28} {'rw' if it['rw'] else 'ro'}  "
                     f"{esc(it['path'])}")
    return lines


def security_summary(host):
    """What `metacomputer security` shows about storage (host: status())."""
    host = host or {}
    return {"configured": bool(host.get("configured")), "volumes": host.get("volumes") or "",
            "refdisks": host.get("refdisks") or "", "guest_features": bool(host.get("guest_features")),
            "attached": [{k: it.get(k) for k in ("kind", "name", "mount", "rw", "path")}
                         for it in host.get("items") or []]}


PROTECTION = {
    "disk": "read-write, this VM only (locked while it runs); the VM can read and change everything on it",
    "refdisk": "read-only at the hypervisor; any number of VMs",
}


def describe_security(s):
    lines = ["Storage (docs/storage.md: what each kind lets the VM do)"]
    if not s.get("attached"):
        if s.get("configured"):
            lines.append(f"  nothing attached now; at the next start: VOLUMES={s['volumes'] or '-'}  "
                         f"REFDISKS={s['refdisks'] or '-'}")
        else:
            lines.append("  nothing attached: the VM shares no storage with this Mac or other VMs")
        return lines
    for it in s["attached"]:
        lines.append(f"  {it['kind']:<8} {it['name']:<14} at {it['mount']:<24} {PROTECTION.get(it['kind'], '')}")
    return lines


def cmd_status(a):
    print(json.dumps(status(), indent=2))
    return 0


# --- the command line ------------------------------------------------------------------------
def main(argv):
    ap = argparse.ArgumentParser(prog="metacomputer", add_help=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    vol = sub.add_parser("volume").add_subparsers(dest="op", required=True)
    p = vol.add_parser("create")
    p.add_argument("name")
    p.add_argument("--size")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_volume_create)
    p = vol.add_parser("list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_volume_list)
    p = vol.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_volume_delete)
    p = vol.add_parser("resize")
    p.add_argument("name")
    p.add_argument("--size")
    p.set_defaults(fn=cmd_volume_resize)
    ref = sub.add_parser("refdisk").add_subparsers(dest="op", required=True)
    p = ref.add_parser("create")
    p.add_argument("name")
    p.add_argument("--from", dest="src")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_refdisk_create)
    p = ref.add_parser("list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_refdisk_list)
    p = ref.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_refdisk_delete)
    p = sub.add_parser("prepare")
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--no-volumes", action="store_true")
    p.set_defaults(fn=cmd_prepare)
    p = sub.add_parser("started")
    p.add_argument("--pid", type=int, required=True)
    p.set_defaults(fn=cmd_started)
    sub.add_parser("attached").set_defaults(fn=cmd_attached)
    p = sub.add_parser("attach")
    p.add_argument("--transport", type=int, required=True)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_attach)
    p = sub.add_parser("release")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_release)
    p = sub.add_parser("checkpoint")
    p.add_argument("op", choices=["save", "restore", "delete", "list"])
    p.add_argument("name")
    p.set_defaults(fn=cmd_checkpoint)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except StorageError as e:
        print(f"metacomputer: {esc(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
