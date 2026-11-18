#!/usr/bin/env python3
"""Shared and persistent storage for the virtual computer, on the Mac's side (docs/storage.md).

  disk volumes    ext4 disk images on the Mac, attached read-write to one VM at a time
                  (`lume run --disk`); formatted inside the VM on first use, only when blank
  folder volumes  a Mac folder shared over virtiofs (`--shared-dir`), read-only unless rw is
                  asked for; while a VM has one read-write, a watcher on the Mac quarantines what
                  the VM writes, removes links out of the folder and special files
  S3 volumes      a folder volume whose folder is a local copy of an S3 bucket (AWS S3, R2,
                  MinIO, any S3-compatible store), kept in step through a private sync copy that
                  `rclone bisync` (pinned; scripts/fetch-artifacts.sh rclone) syncs with the bucket,
                  at start, every S3_SYNC_INTERVAL while the VM runs, and at stop; keys from the
                  macOS Keychain, to rclone only by environment
  reference disks read-only ISO/UDF images packed from a Mac folder; any number of VMs
                  (`--mount` for the first, `--usb-storage` for the rest, both read-only)

Everything lives outside the VM, so it survives `reset`, `restore` and `build --replace`.

The registry (one JSON file per item) and the locks are on this Mac's internal disk, in
~/.local/share/metacomputer/storage/ ($MCOMP_STORAGE_HOME); the images go wherever
STORAGE_DIR (default ~/.local/share/metacomputer) or a per-item --dir says: an external
drive, a NAS mount, anything this user can reach.

  storage.py volume create NAME --size 20G [--dir PATH] | create NAME --folder PATH
             | create NAME --s3 --bucket B --endpoint URL [--region R] [--prefix P] [--dir PATH]
             | list [--json] | delete NAME [--yes] | resize NAME --size 40G
             | keys NAME | test NAME | sync NAME [--force]
  storage.py refdisk create NAME --from FOLDER [--dir PATH] | list [--json] | delete NAME [--yes]
  storage.py prepare --pid PID [--no-volumes]   before `lume run`: check, lock, write the run arguments
  storage.py started --pid PID                  after `lume run`: the VM's process holds the locks
  storage.py attached                           the guest's answer (stdin): report, record, event
  storage.py release                            after the VM stopped: let go of everything
  storage.py watch                              the folder watcher, with its supervisor (started by `started`)
  storage.py kill-vm [--grace S]                power this VM off from the Mac now (storage_abort)
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
import threading
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


def statfs_names(path):
    """(filesystem type, what it's mounted from) for the filesystem holding path (statfs(2))."""
    if sys.platform != "darwin":
        return ("unknown", str(os.stat(path).st_dev))
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    buf = _StatFS()
    if libc.statfs(path.encode(), ctypes.byref(buf)) != 0:
        return ("unknown", str(os.stat(path).st_dev))
    return (buf.f_fstypename.decode(errors="replace"), buf.f_mntfromname.decode(errors="replace"))


_POOLS = {}


def space_pool(path):
    """The pool a path's free space comes from. APFS volumes in one container share its free space whatever their
    device numbers, so for APFS it's the container (diskutil's APFSContainerReference for the volume statfs names,
    else the whole-disk part of its name: disk3s5 -> disk3); otherwise the filesystem itself."""
    t, src = statfs_names(path)
    if t.lower() != "apfs" or not src.startswith("/dev/disk"):
        return f"{t}:{src}"
    if src not in _POOLS:
        pool = None
        try:
            import plistlib
            out = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", src], capture_output=True, timeout=30).stdout
            pool = plistlib.loads(out).get("APFSContainerReference") if out else None
        except Exception:  # noqa: BLE001 - the name below is the fallback
            pool = None
        if not isinstance(pool, str) or not pool:
            m = re.match(r"^/dev/(disk[0-9]+)", src)
            pool = m.group(1) if m else src
        _POOLS[src] = "apfs:" + pool
    return _POOLS[src]


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


# --- walking a path without following links ------------------------------------------------------
# A registered path is never handed to open() whole: it's walked from / one component at a time, each opened
# O_NOFOLLOW | O_DIRECTORY relative to the one before (an openat chain), so a folder anywhere in it that was
# replaced by a link stops the walk (ELOOP / ENOTDIR) instead of leading somewhere else. Registered paths are
# recorded as the kernel spells them (no links in them), so a walk that meets one means something changed.
def _components(path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise OSError(errno.EINVAL, "not an absolute path", path)
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise OSError(errno.EINVAL, "a . or .. in the path", path)
    return parts


def walk_dir(path, create=False, mode=0o700):
    """The descriptor of the folder at path, reached from / through an openat chain of O_NOFOLLOW | O_DIRECTORY
    opens (a link anywhere in the path: OSError ELOOP or ENOTDIR). create: missing folders are made (mode)."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for c in _components(path):
            try:
                nfd = os.open(c, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(c, mode, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(c, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nfd
        out, fd = fd, None
        return out
    finally:
        if fd is not None:
            os.close(fd)


def walk_parent(path, create=False):
    """(the descriptor of the folder holding path, through walk_dir; path's last component)."""
    parent, leaf = os.path.split(path.rstrip("/"))
    if not leaf or leaf in (".", ".."):
        raise OSError(errno.EINVAL, "no file name", path)
    return walk_dir(parent or "/", create=create), leaf


def walk_file(path, flags=os.O_RDONLY):
    """The file at path: its folder through walk_parent, then the file itself opened O_NOFOLLOW."""
    dfd, leaf = walk_parent(path)
    try:
        return os.open(leaf, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd)
    finally:
        os.close(dfd)


def ident_at(path, folder):
    """[device, inode] of what path leads to through a walk (walk_dir / walk_file); OSError otherwise."""
    fd = walk_dir(path) if folder else walk_file(path)
    try:
        st = os.fstat(fd)
        return [st.st_dev, st.st_ino]
    finally:
        os.close(fd)


def recreate_hint(kind, name, path):
    """What to do with an item an earlier build registered without its identity: there's no adopting one in place
    (its path could be any spelling), only deleting it and creating it again. Says what happens to the data."""
    if kind == "refdisk":
        return (f"delete it and create it again: `metacomputer refdisk delete {name}` deletes the image {path} (the "
                f"folder it was packed from stays), then `metacomputer refdisk create {name} --from FOLDER`")
    if kind == "s3":
        return (f"delete it and create it again: `metacomputer volume delete {name}` removes its keys and sync state; "
                f"the bucket and the local copy {path} stay. Move the local copy aside, then `metacomputer volume "
                f"create {name} --s3 ...` and `metacomputer volume keys {name}`: the first sync brings the bucket's "
                f"files back; copy in anything from the old local copy that hadn't reached the bucket")
    return (f"delete it and create it again: `metacomputer volume delete {name}` deletes the image {path} and its "
            f"checkpoint snapshots, and the files on it with them, so copy out what you need first (or add "
            f"--keep-image: the image file stays where it is, no longer a volume and no longer protected); then "
            f"`metacomputer volume create {name} --size N` makes a new, blank image")


def legacy_msg(what, kind, name, path):
    return (f"{what} '{name}' was registered by an earlier build, before metacomputer recorded which "
            f"{'folder' if kind == 's3' else 'file'} it is; {recreate_hint(kind, name, path)}")


def open_image(rec, flags=os.O_RDONLY, kind="volume"):
    """The registered image (disk volume or reference disk) as a descriptor: its recorded path walked without
    following any link, and the file there checked to be the one recorded when it was created (device and
    inode). StorageError otherwise: a folder in the path replaced by a link, or the image itself replaced."""
    what = "reference disk" if kind == "refdisk" else "volume"
    name, path, want = rec.get("name", "?"), rec.get("path", ""), rec.get("ident")
    if not want:
        raise StorageError(legacy_msg(what, kind, name, path))
    try:
        fd = walk_file(path, flags)
    except FileNotFoundError:
        raise StorageError(f"{what} '{name}': {path} is missing (is its drive connected?)")
    except OSError as e:
        raise StorageError(f"{what} '{name}': {path} can't be reached without following a link ({e.strerror}): a "
                           "folder in its path was replaced; not using it")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino] != list(want):
        os.close(fd)
        raise StorageError(f"{what} '{name}': {path} isn't the image that was created (it was replaced); not using it")
    spelled = fd_path(fd)
    if spelled != path:
        # Registered paths are the kernel's own spelling (firmlinks mapped, its case and normalization), so every
        # comparison with another item's path sees the same names; any other form is refused, never used.
        os.close(fd)
        raise StorageError(f"{what} '{name}': {path} isn't recorded as the kernel names it ({spelled}); "
                           f"{recreate_hint(kind, name, path)}")
    return fd


def unlink_walked(path, ident=None):
    """Remove the file at path, its folder reached by walk_parent; with ident, only while it's still that file.
    Missing is fine. StorageError when the path can't be walked or the file was replaced."""
    try:
        dfd, leaf = walk_parent(path)
    except FileNotFoundError:
        return
    except OSError as e:
        raise StorageError(f"{path} can't be reached without following a link ({e.strerror}); not deleting through it")
    try:
        try:
            st = os.stat(leaf, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if ident and [st.st_dev, st.st_ino] != list(ident):
            raise StorageError(f"{path} isn't the file that was created (it was replaced); not deleting it")
        if stat.S_ISREG(st.st_mode) and not st.st_mode & stat.S_IWUSR:
            os.chmod(leaf, 0o600, dir_fd=dfd, follow_symlinks=False)
        os.unlink(leaf, dir_fd=dfd)
    finally:
        os.close(dfd)


def canon_file(path):
    """canon() of a file that must be the file at path itself: a link there (to anything) is refused
    (OSError ELOOP), so a swapped image can't lead the VM runner to another file."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a plain file", path)
        return fd_path(fd), (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def fd_path(fd):
    """The path the kernel knows an open file by (F_GETPATH)."""
    if sys.platform != "darwin":
        return os.readlink(f"/proc/self/fd/{fd}")
    raw = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1025)
    return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")


def canon_or_abs(path):
    try:
        return canon(path)[0]
    except OSError:
        return os.path.abspath(path)


def image_dirs():
    """The folders holding a registered disk volume's or reference disk's image (and its checkpoint
    snapshots, which sit beside it)."""
    out = []
    for kind in KINDS:
        for rec in reg_all(kind):
            if rec.get("kind", kind) in ("disk", "refdisk") and rec.get("path"):
                out.append(os.path.dirname(rec["path"]))
    return out


def protected_paths(home=None, lab=None, ssh_dir=None, data=None, state=None, images=None):
    """(home, data, exact, trees): folders that may not be shared, and folders nothing inside may be shared
    from, each as (canonical spelling, identity or None). Their parents are refused as well (sharing a
    parent shares them). The folders holding images are among them: a VM that could write there could swap
    an image (or a snapshot) for a link to any file, whatever the locks say at that moment."""
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
    exact += [c(p) for p in (image_dirs() if images is None else images)]
    trees = [c(p) for p in (j(home, "Library"), j(home, ".ssh"), ssh_dir, lab, j(home, ".lume"), state, "/System",
                            "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/var",
                            "/private/var", "/dev", "/cores", "/opt/homebrew")]
    # S3 staging folders (stage_folder), where files bound for a shared copy are made before they're quarantined
    trees += [c(stage_folder(r)) for r in reg_all("volume") if r.get("kind") == "s3" and r.get("path")]
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
                           "(personal folders, Library, keys, this checkout, system folders, the folders holding "
                           "volume images). Use a folder of its own.")
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


def guest_kind(kind):
    """The kind the VM's helper reports: an S3 volume is a folder to the VM."""
    return "folder" if kind == "s3" else kind


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


def strictly_inside(child, parent):
    return inside(child, parent) and fold(child.rstrip("/")) != fold(parent.rstrip("/"))


FOLDERISH = ("folder", "s3")
HELD_FILES = ("disk", "refdisk")


def writable_folder(x):
    return x.get("kind") in FOLDERISH and bool(x.get("rw"))


def _lock_of(x):
    """The lock an item or a live record names (records written before "lock" was kept: their file name)."""
    if x.get("lock"):
        return x["lock"]
    f = x.get("_file", "")
    return f[:-5] if f.endswith(".json") and x.get("exclusive", True) else ""


def pair_conflict(a, b):
    """Why a and b (planned items or live records) can't be held at once, said of a, or None. The rules are
    symmetric, so the answer never depends on which was taken first:
    - one lock, both exclusive (one VM per disk volume, per read-write folder);
    - a read-write folder and anything inside it that another holder relies on: another read-write folder
      overlapping it either way, a read-only folder below it (the writer could swap it for another), a
      disk image or reference disk anywhere in it (the writer could swap the image)."""
    la, lb = _lock_of(a), _lock_of(b)
    if la and la == lb and a.get("exclusive", True) and b.get("exclusive", True):
        return "is in use by"
    for x, y, x_is_a in ((a, b, True), (b, a, False)):
        # An S3 staging folder may be seen by no VM at all: no folder shared with any VM, read-only or read-write,
        # may hold it or sit inside it while a sync or a create uses it (and the refusals keep it out otherwise).
        if x.get("kind") == "stage" and y.get("kind") in FOLDERISH and host_overlapping(x.get("path", ""),
                                                                                         y.get("path", "")):
            return ("is an S3 staging folder no VM may see, overlapping a folder shared with" if x_is_a else
                    f"holds or is inside an S3 staging folder ({x.get('path')}), in use by")
    for x, y, x_is_a in ((a, b, True), (b, a, False)):
        if not writable_folder(x):
            continue
        xp, yp = x.get("path", ""), y.get("path", "")
        if writable_folder(y):
            hit = host_overlapping(xp, yp)
        elif y.get("kind") in FOLDERISH:
            hit = strictly_inside(yp, xp)
        else:
            hit = y.get("kind") in HELD_FILES and inside(yp, xp)
        if hit:
            if writable_folder(y):
                return "overlaps a folder shared read-write with"
            if x_is_a:
                return f"holds {y.get('kind')} '{y.get('name', '?')}' (inside it), held by"
            return "is inside a folder shared read-write with"
    return None


def item_conflicts(it, others):
    """Why item it can't be taken while the live records others (other VMs, other operations) exist."""
    out = []
    for rec in others:
        why = pair_conflict(it, rec)
        if why:
            out.append(f"{it['kind']} '{it['name']}' {why} {describe_holder(rec)}")
    return out


def plan_conflicts(items):
    """The same rules between the items of one plan (one start, one operation), each pair once."""
    out = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            why = pair_conflict(a, b)
            if why:
                out.append(f"{a['kind']} '{a['name']}' and {b['kind']} '{b['name']}' can't be attached together: "
                           + ("they're the same file or folder" if why == "is in use by" else
                              "one is a read-write folder that holds or overlaps the other"))
    return out


def record_name(it, vm, lab):
    """An exclusive item's record is one file for everyone; a shared one's is one per holder."""
    if it.get("exclusive", True):
        return it["lock"]
    import hashlib
    return it["lock"] + "-ro-" + hashlib.sha256(f"{vm}|{lab}".encode()).hexdigest()[:12]


def acquire(items, vm, lab, pid):
    """Take this VM's records for its items, or none: exclusive ones for what it writes (disk volumes,
    read-write folders), shared ones for read-only folders and reference disks. Every item is checked
    against the live records of other holders (item_conflicts) and against the plan's other items
    (plan_conflicts), by rules that don't depend on order. Returns the record names written; raises
    StorageError naming each conflict."""
    if not items:
        return []
    pstart = proc_start(pid)
    host = hostname()
    with GlobalLock():
        others = [r for r in all_locks() if not same_holder(r, vm, lab) and holder_alive(r)]
        conflicts = plan_conflicts(items)
        for it in items:
            conflicts += item_conflicts(it, others)
            if it["kind"] == "disk" and it.get("lock"):
                side = read_json(sidecar_path(it))
                if isinstance(side, dict) and side.get("host") and side.get("host") != host:
                    conflicts.append(f"disk '{it['name']}': its lock file says computer '{side['host']}' has it "
                                     f"(VM '{side.get('vm', '?')}', since {side.get('since', '?')}). If that computer "
                                     f"isn't using it, delete {sidecar_path(it)}")
        if conflicts:
            raise StorageError("; ".join(conflicts) + ". Stop that VM first, or start this one with --no-volumes")
        written = []
        for it in items:
            if not it.get("lock"):
                continue
            rec = {"kind": it["kind"], "name": it["name"], "path": it["path"], "vm": vm, "lab": lab,
                   "pid": int(pid), "pstart": pstart, "host": host, "since": now(), "state": "starting",
                   "rw": bool(it.get("rw", True)), "exclusive": bool(it.get("exclusive", True)), "lock": it["lock"]}
            name = record_name(it, vm, lab)
            atomic_json(os.path.join(locks_dir(), name + ".json"), rec)
            written.append(name)
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
def reserved(recs, op, extra=()):
    """Hold disk volumes (and extra items: a checkpoint, an S3 volume's local copy) for the length of an
    operation (checkpoint, restore, resize, delete, sync): taken before anything is checked or changed, through
    the same locks and rules a VM's start takes, so no VM can attach one, or a read-write folder around one,
    until every change the operation makes is done; refused while a VM (or another operation) holds one."""
    items = [{"kind": "disk", "name": r["name"], "path": r["path"], "lock": lock_key("disk", r["path"])} for r in recs]
    items += list(extra)
    holder = f"{OPERATION}{op}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
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
        try:
            os.close(open_image(rec, kind="refdisk"))     # walked without links, and the image that was made
        except StorageError as e:
            problems.append(str(e))
            continue
        ident = rec["ident"]
        # A shared record (any number of VMs), so a read-write folder around the image is refused while one
        # has it, whichever comes first.
        items.append({"kind": "refdisk", "name": r["name"], "path": path, "ident": list(ident), "mount": r["mount"],
                      "rw": False, "label": label_for(rec), "lock": lock_key("refdisk", path), "exclusive": False})
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
        try:
            fd = open_image(rec)                      # walked without links, and the image that was made
        except StorageError as e:
            problems.append(str(e))
            return None
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
        ident, size = rec["ident"], st.st_size
        if size % 512 or size < MIN_SIZE:
            problems.append(f"volume '{name}': {path} isn't a disk image metacomputer made")
            return None
        net = network_fs(path)
        if net:
            notes.append(f"volume '{name}' is on a network filesystem ({net[0]} at {net[1]}): if the connection drops "
                         "while the VM writes, the volume can be damaged, and locks don't hold across computers")
        return {"kind": "disk", "name": name, "path": path, "ident": list(ident), "mount": v["mount"], "rw": True,
                "size": size, "uuid": rec["uuid"], "lock": lock_key("disk", path)}
    if kind in FOLDER_KINDS:
        return plan_folder(rec, v, problems, notes)
    problems.append(f"volume '{name}': unknown kind {kind!r}")
    return None


FOLDER_KINDS = ("folder", "s3")
MAX_WATCH = 500_000          # entries a read-write folder may hold (the watcher keeps one record each)


def has_noindex(real):
    """Spotlight leaves out any folder whose name ends in .noindex, and everything in it (measured on
    macOS 27: .metadata_never_index files no longer keep a folder out)."""
    return any(c.endswith(".noindex") for c in real.split("/"))


def plan_folder(rec, v, problems, notes):
    name, path = rec["name"], rec.get("path", "")
    if not os.path.isdir(path):
        problems.append(f"volume '{name}': the folder {path} is missing (is its drive connected?)")
        return None
    try:
        real, ident = check_shareable_id(path)
    except StorageError as e:
        problems.append(f"volume '{name}': {e}")
        return None
    try:
        walked = ident_at(path, folder=True)            # the registered path itself, no link followed
    except OSError as e:
        problems.append(f"volume '{name}': {path} can't be reached without following a link ({e.strerror}): a folder "
                        "in its path was replaced")
        return None
    if walked != list(ident) or real.rstrip("/") != path.rstrip("/"):
        problems.append(f"volume '{name}': {path} now leads to another folder, or isn't recorded as the kernel names "
                        f"it ({real}); not sharing it")
        return None
    if ":" in real:
        problems.append(f"volume '{name}': {real} has a ':' in its path, which the VM runner can't share")
        return None
    if rec.get("kind") == "s3" and (list(ident) != list(rec.get("ident") or []) or fold(real) != fold(path)):
        problems.append(f"volume '{name}': the local copy {path} isn't the folder that was created (replaced, or "
                        "reached through a link now)" if rec.get("ident") else legacy_msg("volume", "s3", name, path))
        return None
    rw = v["mode"] == "rw"
    if rw and not has_noindex(real):
        problems.append(f"volume '{name}': a folder shared read-write must be kept out of Spotlight, so its name (or a "
                        f"parent's) must end in .noindex: rename {os.path.basename(real)} to "
                        f"{os.path.basename(real)}.noindex (Finder shows it like that) and run `metacomputer volume "
                        f"create {name} --folder NEWPATH` again after `volume delete {name}`; or attach it read-only")
        return None
    if rw:
        net = network_fs(real)
        if net:
            notes.append(f"volume '{name}' is on a network filesystem ({net[0]} at {net[1]}): locks don't hold across "
                         "computers, so another computer could write it at the same time")
    # Every folder takes a record: read-write ones exclusive, read-only ones shared, so a later start
    # can see what another VM has and refuse an overlap that would let one VM swap another's folder.
    return {"kind": rec["kind"], "name": name, "path": real, "ident": list(ident), "mount": v["mount"], "rw": rw,
            "lock": lock_key("folder", real), "exclusive": rw}


def share_names(paths):
    """The subfolder each shared folder gets in the VM's virtiofs share: the VM runner names them by
    their last path component, adding " (2)", " (3)" for repeats, in the order given."""
    taken, out = set(), []
    for p in paths:
        base = os.path.basename(p.rstrip("/")) or "Shared Folder"
        name, n = base, 2
        while name in taken:
            name = f"{base} ({n})"
            n += 1
        taken.add(name)
        out.append(name)
    return out


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
    """(key, rest) of "key: rest". A quoted key is its exact text between the quotes (YAML keeps every space of
    it), with no escapes; then any key that isn't plain printable text without spaces at either end is refused
    rather than read as something near it ("files " is not files)."""
    if text[:1] in "'\"":
        q = text[0]
        end = text.find(q, 1)
        if end < 0 or text[end + 1:end + 2] != ":":
            raise ManifestError(f"line {n}: not a key")
        key, rest = text[1:end], text[end + 2:]
        if "\\" in key:
            raise ManifestError(f"line {n}: an escape in a quoted key")
        if rest and not rest.startswith(" "):
            raise ManifestError(f"line {n}: not a key: value")
        _check_key(key, n)
        return key, rest.strip()
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
    key = key.rstrip(" ")                       # a plain key ends at its last non-space character
    _check_key(key, n)
    return key, rest.strip()


def _check_key(key, n):
    if not key or key != key.strip() or any(ord(c) < 32 or ord(c) == 127 for c in key) \
            or any(unicodedata.category(c) in ("Zs", "Cf") for c in key):
        raise ManifestError(f"line {n}: an unusual key {key!r} (spaces at an end, control or invisible characters)")


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
    for it in items:
        if it["kind"] in FOLDER_KINDS:
            args += ["--shared-dir", f"{it['path']}:{'rw' if it['rw'] else 'ro'}"]
    return args


def guest_plan(items):
    plan = {"version": PROTOCOL, "disks": [], "refdisks": [], "folders": []}
    for it in items:
        if it["kind"] == "disk":
            plan["disks"].append({"name": it["name"], "uuid": it["uuid"], "size": it["size"], "mount": it["mount"]})
        elif it["kind"] == "refdisk":
            plan["refdisks"].append({"name": it["name"], "label": it["label"], "mount": it["mount"]})
    folders = [it for it in items if it["kind"] in FOLDER_KINDS]
    for it, share in zip(folders, share_names([it["path"] for it in folders])):
        plan["folders"].append({"name": it["name"], "share": share, "mount": it["mount"], "rw": it["rw"]})
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
    try:
        for f in os.listdir(run_dir()):
            if f.startswith("storage-snap-") or f == "storage-watch.json":
                os.unlink(os.path.join(run_dir(), f))
    except OSError:
        pass


def snap_path(name):
    return os.path.join(run_dir(), f"storage-snap-{name}.json")


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
    snaps = {}
    for it in items:
        if it["kind"] in FOLDER_KINDS and it["rw"]:
            try:
                snaps[it["name"]] = snapshot(it["path"], it.get("ident"))
            except ProtectionFailure as e:
                raise StorageError(f"not starting: volume '{it['name']}': {e}; attach it read-only, or share a "
                                   "smaller folder")
    acquire(items, vm, lab, a.pid)
    for it in items:
        if it["kind"] == "s3":
            it["sync"] = take_sync_lock(it, vm, lab, a.pid)
            if not it["sync"]:
                print(f"  ! volume '{it['name']}': another VM keeps it in sync with its bucket; this one won't sync it",
                      file=sys.stderr)
            elif not keychain_get(it["name"], "access_key_id", probe=True):
                print(f"  ! volume '{it['name']}' has no keys yet (in your own terminal: metacomputer volume keys "
                      f"{it['name']}); it's attached as it is, without syncing", file=sys.stderr)
                it["sync"] = False
            else:
                r = s3_sync(reg_load("volume", it["name"]), reason="start")
                print(f"  {'✓' if r['ok'] else '!'} volume '{it['name']}': {esc(r['summary'])}", file=sys.stderr)
            if it["rw"]:
                try:
                    snaps[it["name"]] = snapshot(it["path"], it.get("ident"))
                except ProtectionFailure as e:
                    release_locks(vm, lab)
                    raise StorageError(f"not starting: volume '{it['name']}': {e}; attach it read-only, or sync it "
                                       "to a smaller prefix")
    for name, sigs in snaps.items():
        atomic_json(snap_path(name), {k: list(v) for k, v in sigs.items()})
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
    if needs_watcher(st.get("items", [])):
        # Protection starts with the VM, before the VM is asked anything: a read-write folder is writable from
        # the moment it boots. No watcher, no VM.
        pid = watcher_start()
        if not pid:
            print("metacomputer: the folder watcher didn't start (see storage-watcher.log), so a read-write folder "
                  "would be unprotected. Stopping the VM.", file=sys.stderr)
            return 1
        print(f"  ✓ folder watcher running (pid {pid}): what the VM writes is quarantined, links out of a folder are "
              "removed")
    return 0


def needs_watcher(items):
    return any((it["kind"] in FOLDER_KINDS and it["rw"]) or (it["kind"] == "s3" and it.get("sync")) for it in items)


def changed_items(items):
    """Items whose path no longer leads to the file or folder that was checked (same device and inode)."""
    out = []
    for it in items:
        want = it.get("ident")
        if not want:
            continue
        try:
            got = ident_at(it["path"], folder=it["kind"] in FOLDER_KINDS)    # walked: no link followed
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
    want = {(guest_kind(it["kind"]), it["name"]): it for it in (st or {}).get("items", [])}
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
    stats = watcher_stop()
    if stats and not a.quiet:
        for name, s_ in stats.get("folders", {}).items():
            print(f"  ✓ folder '{name}': {s_.get('quarantined', 0)} item(s) quarantined, {s_.get('links_removed', 0)} "
                  f"link(s) and {s_.get('special_removed', 0)} special file(s) removed, quarantine restored "
                  f"{s_.get('restored', 0)} time(s)")
    for it in (st or {}).get("items", []):
        if it.get("kind") == "s3" and it.get("sync"):
            rec = reg_load("volume", it["name"])
            if rec:
                r = s3_sync(rec, reason="stop")
                if not a.quiet or not r["ok"]:
                    print(f"  {'✓' if r['ok'] else '!'} volume '{it['name']}': {esc(r['summary'])}")
    n = release_locks(vm_name(), lab_root())
    clear_state()
    if st and st.get("items") and not a.quiet:
        print(f"  ✓ storage released ({n} lock{'s' if n != 1 else ''})")
    return 0


def emit(family, type_, message, severity=None, data=None, action=None, origin="cli"):
    try:
        import events
        events.emit(family, type_, message, severity, action, data, origin=origin)
    except Exception:  # noqa: BLE001 - events never fail a command
        pass


# --- disk volumes ------------------------------------------------------------------------
def canon_dir_path(p):
    """p as the kernel spells it (F_GETPATH of its deepest existing folder, then the missing names)."""
    p = os.path.abspath(p)
    rest = []
    while not os.path.isdir(p) and p != os.path.dirname(p):
        p, leaf = os.path.split(p)
        rest.insert(0, leaf)
    try:
        spelled = canon(p, directory=True)[0]
    except OSError:
        spelled = p
    return os.path.join(spelled, *rest) if rest else spelled


def destination(sub, given, what):
    """(canonical folder, whether it may be made) for a new item: what is "image" (a disk volume's or reference
    disk's) or "copy" (an S3 volume's local copy). The base the user named (--dir, else STORAGE_DIR) is resolved
    once, as the kernel spells it (links in it are the user's own choice); STORAGE_DIR/sub may be made. Nothing is
    made here. The same rules apply to --dir and to STORAGE_DIR: an image may not go inside a registered folder
    volume; and the caller reserves the new item (reserved(): refused when a folder any VM has read-write holds
    it, whichever came first) before open_destination() walks to the folder, making what's missing."""
    if given:
        base = os.path.abspath(os.path.expanduser(check_host_path(given, "--dir")))
        if not os.path.isdir(base):
            raise StorageError(f"--dir {base}: no such folder (is the drive connected?)")
        target, create = canon_dir_path(base), False
    else:
        if not os.path.isdir(os.path.dirname(data_dir())) and env("STORAGE_DIR"):
            raise StorageError(f"STORAGE_DIR {data_dir()}: its parent folder doesn't exist (is the drive connected?)")
        target, create = canon_dir_path(os.path.join(data_dir(), sub)), True
    if what == "image":
        for rec in reg_all("volume"):
            if rec.get("kind") in FOLDER_KINDS and rec.get("path") and inside(target, rec["path"]):
                raise StorageError(f"{target} is inside volume '{rec['name']}' ({rec['path']}), a folder a VM can be "
                                   "given; images go where no VM can reach")
    return target, create


def open_destination(target, create):
    """The destination folder's descriptor, reached by walk_dir (no link followed; missing folders made 0700 when
    create), once the new item is reserved. It must be the folder destination() named."""
    try:
        fd = walk_dir(target, create=create)
    except OSError as e:
        raise StorageError(f"{target} can't be reached without following a link ({e.strerror}); nothing was created")
    if fold(fd_path(fd)) != fold(target):
        os.close(fd)
        raise StorageError(f"{target} isn't where it was when it was checked; nothing was created")
    return fd


def cmd_volume_create(a):
    check_name(a.name)
    if reg_load("volume", a.name):
        raise StorageError(f"there's already a volume named {a.name} (metacomputer volume list)")
    if getattr(a, "folder", None):
        return create_folder(a)
    if getattr(a, "s3", False):
        return create_s3(a)
    if not a.size:
        raise StorageError("usage: metacomputer volume create NAME --size 20G [--dir PATH]")
    size = parse_size(a.size)
    d, create = destination("volumes", a.dir, "image")
    dfd = None
    try:
        leaf = a.name + ".img"
        path = os.path.join(d, leaf)
        rec = {"name": a.name, "kind": "disk", "path": path, "uuid": str(uuid.uuid4()), "created": now()}
        with reserved([rec], "create"):              # no VM may have a read-write folder around it, from now on
            dfd = open_destination(d, create)
            d = fd_path(dfd)                          # registered as the kernel names it (firmlinks, case)
            path = rec["path"] = os.path.join(d, leaf)
            try:
                fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                             dir_fd=dfd)
            except FileExistsError:
                raise StorageError(f"{path} already exists; not touching it")
            try:
                os.ftruncate(fd, size)
                st = os.fstat(fd)
            finally:
                os.close(fd)
            rec["ident"] = [st.st_dev, st.st_ino]       # every later use checks it's still this file
            reg_save("volume", rec)
        free = free_bytes_at(dfd)
    finally:
        if dfd is not None:
            os.close(dfd)
    print(f"volume '{a.name}': {human(size)} disk image at {esc(path)} (grows as it fills; {human(free)} free there)")
    net = network_fs(path)
    if net:
        print(f"  ! it's on a network filesystem ({net[0]} at {esc(net[1])}): if the connection drops while the VM "
              "writes, the volume can be damaged, and locks don't hold across computers")
    print(f"Attach it: VOLUMES={a.name} in config/local.env (in the VM at ~/data/{a.name}; "
          f"VOLUMES={a.name}:/volume for another place). It's formatted inside the VM on first use.")
    note_rebuild()
    return 0


def create_folder(a):
    if a.size or a.dir:
        raise StorageError("a folder volume is the folder itself: no --size or --dir")
    path = os.path.abspath(os.path.expanduser(check_host_path(a.folder, "--folder")))
    if not os.path.isdir(path):
        if not os.path.isdir(os.path.dirname(path)):
            raise StorageError(f"{os.path.dirname(path)}: no such folder (is the drive connected?)")
        os.mkdir(path, 0o755)
    real = check_shareable(path)
    if ":" in real:
        raise StorageError(f"{real} has a ':' in its path, which the VM runner can't share")
    reg_save("volume", {"name": a.name, "kind": "folder", "path": real, "created": now()})
    print(f"volume '{a.name}': the Mac folder {esc(real)}, shared into the VM (it stays visible in Finder)")
    print(f"Attach it read-only: VOLUMES={a.name} (in the VM at ~/data/{a.name}), or read-write: "
          f"VOLUMES={a.name}:rw (one VM at a time; what the VM writes is quarantined)")
    if not has_noindex(real):
        print(f"  ! read-write needs it kept out of Spotlight: its name (or a parent's) must end in .noindex. "
              f"Read-only works as it is.")
    net = network_fs(real)
    if net:
        print(f"  ! it's on a network filesystem ({net[0]} at {esc(net[1])}): locks don't hold across computers")
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
    if rec.get("kind") in FOLDER_KINDS:
        if not os.path.isdir(path):
            return "missing (is its drive connected?)"
        h = holder_of(path, "folder")
        return (f"read-write in {describe_holder(h)}" if h else "free") + ("" if has_noindex(_real(path))
                                                                           else "; read-only only (no .noindex)")
    return "?"


def cmd_volume_list(a):
    vols = reg_all("volume")
    rows = []
    for rec in vols:
        row = {"name": rec["name"], "kind": rec.get("kind"), "path": rec.get("path"), "state": volume_state(rec)}
        if rec.get("kind") == "s3":
            row["s3"] = dict(rec.get("s3") or {}, keys=bool(keychain_get(rec["name"], "access_key_id", probe=True)))
            row["last_sync"] = (read_json(os.path.join(s3_dir(rec["name"]), "last-sync.json")) or {})
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
        if r.get("s3"):
            b = r["s3"]
            ls = r.get("last_sync") or {}
            print(f"{'':<16} s3://{esc(b.get('bucket'))}/{esc(b.get('prefix') or '')} at {esc(b.get('endpoint'))}; keys "
                  f"{'in the Keychain' if b.get('keys') else 'not set (metacomputer volume keys ' + r['name'] + ')'}; last sync "
                  f"{ls.get('at', 'never')}{'' if ls.get('ok', True) else ' (failed: ' + esc(str(ls.get('error', ''))[:120]) + ')'}")
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
        with reserved([rec], "delete"):          # held until the registry entry is gone too
            # While the volume is held, no checkpoint can gain it (a save needs it too); the checkpoints that
            # have it are held as well, so their manifests are rewritten one operation at a time.
            snaps = snapshots_of(a.name)
            with reserved([], "delete-checkpoints", [ckpt_item_at(m) for m in sorted({m for m, _ in snaps})]):
                delete_disk(a, rec, snaps)
                reg_delete("volume", a.name)
    else:
        if rec.get("kind") in FOLDER_KINDS:
            h = holder_of(rec.get("path", ""), "folder")
            if h:
                raise StorageError(f"volume '{a.name}' is shared read-write with {describe_holder(h)}; stop that VM first")
        if rec.get("kind") == "s3":
            h = holder_of(rec.get("path", ""), "s3sync")
            if h:
                raise StorageError(f"volume '{a.name}' is kept in sync by {describe_holder(h)}; stop that VM first")
            d = s3_dir(a.name)
            lockf = None
            if os.path.isdir(d):
                lockf = os.open(os.path.join(d, "sync.lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                0o600)
                try:
                    fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(lockf)
                    raise StorageError(f"volume '{a.name}' is syncing right now; try again when it's done")
            try:
                remove_stage(rec)
                for what in ("access_key_id", "secret_access_key"):
                    subprocess.run([SECURITY, "delete-generic-password", "-s", keychain_service(a.name), "-a", what],
                                   capture_output=True)
                shutil.rmtree(d, ignore_errors=True)
            finally:
                if lockf is not None:
                    os.close(lockf)
        reg_delete("volume", a.name)
    if rec.get("kind") == "s3":
        print(f"  its keys are gone from the Keychain; the bucket and the local copy ({esc(rec.get('path', ''))}) are untouched")
    if rec.get("kind") in FOLDER_KINDS:
        print(f"volume '{a.name}' removed; the folder {esc(rec.get('path', ''))} and its files are untouched")
    elif getattr(a, "keep_image", False):
        print(f"volume '{a.name}' removed; its image stays at {esc(rec.get('path', ''))} (no longer a volume: nothing "
              "protects it now)")
    else:
        print(f"volume '{a.name}' deleted")
    if a.name in {e["name"] for e in safe_parse(env("VOLUMES"), "volume")}:
        print(f"  ! VOLUMES in your config still lists {a.name}: remove it, or start refuses")
    return 0


def delete_disk(a, rec, snaps):
    """With the volume and its checkpoints held (reserved). Everything is removed through walks (no link
    followed), the image only while it's still the file that was made."""
    keep = bool(getattr(a, "keep_image", False))
    extra = f" and its {len(snaps)} checkpoint snapshot(s)" if snaps else ""
    if keep:
        confirm(f"Remove volume '{a.name}'{extra}, keeping its image file {rec['path']} (no longer a volume, no longer "
                "protected)?", a.yes)
    else:
        confirm(f"Delete volume '{a.name}' ({rec['path']}){extra}? Its files are gone for good", a.yes)
    for m in sorted({m for m, _ in snaps}):
        man = read_json(m) or {}
        for v in man.get("volumes", []):
            if v.get("name") == a.name and v.get("snapshot"):
                unlink_walked(v["snapshot"])
        man["volumes"] = [v for v in man.get("volumes", []) if v.get("name") != a.name]
        atomic_json(m, man)
    if not keep:
        unlink_walked(rec["path"], rec.get("ident"))
    unlink_walked(rec["path"] + ".lock")


def cmd_volume_resize(a):
    rec = reg_load("volume", check_name(a.name))
    if not rec:
        raise StorageError(f"no volume named {a.name} (metacomputer volume list)")
    if rec.get("kind") != "disk":
        raise StorageError(f"volume '{a.name}' isn't a disk volume; only disk volumes have a size")
    if not a.size:
        raise StorageError("usage: metacomputer volume resize NAME --size 40G")
    size = parse_size(a.size)
    with reserved([rec], "resize"):
        return resize_volume(a, rec, size)


def resize_volume(a, rec, size):
    """With the volume held (reserved); the image reached by open_image (no link followed, the file made)."""
    fd = open_image(rec, os.O_RDWR)
    try:
        cur = os.fstat(fd).st_size
        if size < cur:
            raise StorageError(f"volume '{a.name}' is {human(cur)}; volumes only grow (shrinking could lose files)")
        if size == cur:
            print(f"volume '{a.name}' is already {human(cur)}")
            return 0
        os.ftruncate(fd, size)
    finally:
        os.close(fd)
    print(f"volume '{a.name}': {human(cur)} -> {human(size)}; its filesystem grows inside the VM at the next start")
    return 0


# --- reference disks -----------------------------------------------------------------------
def clone_or_copy(src, dst, expect=None):
    """dst (a new file) becomes a copy of the file at src. Both are reached through walks (no link anywhere in
    either path is followed); src must be a plain file, and expect's [device, inode] when given; only then is it
    cloned (fclonefileat: APFS, same volume) or copied from that descriptor, sparse-aware, bounded to the size it
    had and to the space above the reserve. Returns "clone" or "copy"."""
    try:
        sfd = walk_file(src)
    except OSError as e:
        raise StorageError(f"{src}: can't open it ({e.strerror}; no link in its path is followed)")
    try:
        st = os.fstat(sfd)
        if not stat.S_ISREG(st.st_mode):
            raise StorageError(f"{src} isn't a plain file")
        if expect and [st.st_dev, st.st_ino] != list(expect):
            raise StorageError(f"{src} isn't the image that was created (it was replaced); not copying it")
        try:
            dfd, leaf = walk_parent(dst)
        except OSError as e:
            raise StorageError(f"{dst}: its folder can't be reached without following a link ({e.strerror})")
        try:
            how, _ = copy_from_fd(sfd, dfd, leaf, st)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    return how


def data_ranges(fd, size):
    """(start, end) of the parts of the first size bytes of a file that hold data (SEEK_DATA/SEEK_HOLE), one at
    a time; the whole of it where the filesystem can't tell."""
    sd, sh = getattr(os, "SEEK_DATA", None), getattr(os, "SEEK_HOLE", None)
    if sd is None:
        yield 0, size
        return
    pos = 0
    while pos < size:
        try:
            start = os.lseek(fd, pos, sd)
        except OSError as e:
            if e.errno != errno.ENXIO:           # ENXIO: nothing but a hole from here
                yield pos, size
            return
        if start >= size:
            return
        end = min(os.lseek(fd, start, sh), size)
        yield start, end
        pos = end


def write_all(fd, data, offset):
    """pwrite until every byte is written (a short write isn't an error)."""
    view = memoryview(data)
    while view:
        n = os.pwrite(fd, view, offset)
        if n <= 0:
            raise OSError(errno.EIO, "a write wrote nothing")
        view, offset = view[n:], offset + n


RECHECK_EVERY = 64 * MIB      # a long copy checks the destination's real free space this often


def sparse_copy_fd(sfd, out, size, budget=None):
    """The first size bytes of sfd into out: its data parts only (holes stay holes), then out is exactly size
    long. A file that grew since size was read isn't copied past it; one that shrank ends early. At most budget
    bytes are written (StorageError past it), and every RECHECK_EVERY bytes the destination's real free space is
    checked against the reserve (something else may be filling the same disk). Returns the bytes written."""
    written, since = 0, 0
    for start, end in data_ranges(sfd, size):
        pos = start
        while pos < end:
            chunk = os.pread(sfd, min(8 * MIB, end - pos), pos)
            if not chunk:
                break
            if budget is not None and written + len(chunk) > budget:
                raise StorageError(f"copying it would write more than the {human(budget)} it may use")
            if since >= RECHECK_EVERY:
                since = 0
                if free_bytes_at(out) < reserve_bytes():
                    raise StorageError(f"the disk fell below the free-space reserve of {human(reserve_bytes())} "
                                       "(IMPORT_MIN_FREE_GB) during the copy; stopped")
            write_all(out, chunk, pos)
            pos += len(chunk)
            written += len(chunk)
            since += len(chunk)
    os.ftruncate(out, size)
    return written


def _fclone(sfd, dfd, name):
    """fclonefileat(2): an APFS clone of the open file sfd as name in the folder dfd. False where the filesystem
    can't (another volume, not APFS)."""
    if sys.platform != "darwin":
        return False
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.fclonefileat(sfd, dfd, name.encode("utf-8", "surrogateescape"), 0) == 0:
        return True
    e = ctypes.get_errno()
    if e in (errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS):
        return False
    raise OSError(e, os.strerror(e), name)


def sparse_copy(src, dst):
    """A sparse-aware copy by path, for files this program made (tests, fixtures)."""
    sfd = os.open(src, FILE_NOFOLLOW)
    try:
        out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            sparse_copy_fd(sfd, out, os.fstat(sfd).st_size)
        finally:
            os.close(out)
    finally:
        os.close(sfd)


DIR_NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_NOFOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
STAGE_MAX_DEPTH = 64


def copy_from_fd(sfd, dfd, name, st, budget=None, clone=True):
    """Copy the open file sfd to a new file name in the folder dfd: an APFS clone of the descriptor when it can
    (fclonefileat), else its data parts (sparse-aware), no more than the size st gave, only when the space it
    takes (its allocated blocks) leaves the reserve free there, and at most budget bytes. The source is never
    looked up by path again. Returns ("clone" or "copy", bytes written)."""
    if clone and _fclone(sfd, dfd, name):
        return "clone", 0
    need = min(st.st_blocks * 512, st.st_size)
    room = free_bytes_at(dfd) - reserve_bytes()
    if need > room:
        raise StorageError(f"not enough free space for a copy of {human(st.st_size)} ({human(max(room, 0))} above "
                           "the reserve, IMPORT_MIN_FREE_GB)")
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        n = sparse_copy_fd(sfd, out, st.st_size, room if budget is None else min(budget, room))
    except BaseException:
        os.close(out)
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError:
            pass
        raise
    os.close(out)
    return "copy", n


def stage_tree(src, dst, ident=None):
    """Copy a folder's plain files and folders into dst, leaving out links and special files; returns
    (files, bytes, left_out). Everything is reached through descriptors, from the folder's own (checked
    against ident when given) down: each entry is opened without following links and must still be the
    file or folder that was listed, so a file swapped for a link mid-copy is left out, not followed."""
    files = size = 0
    left = []
    sroot = os.open(src, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    droot = walk_dir(dst)
    # The staged copies, and then the image packed from them at full size (ISO/UDF keeps no holes), must
    # leave the reserve free: a sparse file the VM made huge for nothing is refused, not expanded.
    room = free_bytes_at(droot) - reserve_bytes()
    spent = [0]

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
                    if size + fst.st_size + spent[0] + min(fst.st_blocks * 512, fst.st_size) > room:
                        raise StorageError(f"packing {src} needs more than the {human(max(room, 0))} free above the "
                                           "reserve (IMPORT_MIN_FREE_GB): its files count at full size, as the image "
                                           "holds them; nothing was packed")
                    how, wrote = copy_from_fd(ffd, dfd, n, fst, budget=room - spent[0] - size - fst.st_size)
                    spent[0] += wrote
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
    d, create = destination("refdisks", a.dir, "image")
    dfd = None
    out = os.path.join(d, a.name + ".iso")
    rec = {"name": a.name, "kind": "refdisk", "path": out, "uuid": str(uuid.uuid4()), "source": src,
           "created": now()}
    label = label_for(rec)
    item = {"kind": "refdisk", "name": a.name, "path": out, "lock": lock_key("refdisk", out), "exclusive": True}
    stage_name = f".{a.name}.staging-{uuid.uuid4().hex[:8]}"
    tmp_name = f".{a.name}.{uuid.uuid4().hex[:8]}.iso"
    stage, tmp = os.path.join(d, stage_name), os.path.join(d, tmp_name)
    made = False
    try:
        with reserved([], "refdisk-create", [item]):  # no VM may have a read-write folder around it, from now on
            dfd = open_destination(d, create)
            d = fd_path(dfd)                          # registered as the kernel names it (firmlinks, case)
            out = rec["path"] = os.path.join(d, a.name + ".iso")
            try:
                os.stat(a.name + ".iso", dir_fd=dfd, follow_symlinks=False)
                raise StorageError(f"{out} already exists; not touching it")
            except FileNotFoundError:
                pass
            os.mkdir(stage_name, 0o700, dir_fd=dfd)
            made = True
            files, size, left = stage_tree(src, stage, src_ident)
            r = subprocess.run(["hdiutil", "makehybrid", "-quiet", "-o", tmp, stage, "-iso", "-joliet", "-udf",
                                "-udf-version", "1.50", "-default-volume-name", label, "-iso-volume-name", label,
                                "-joliet-volume-name", label, "-udf-volume-name", label],
                               capture_output=True, text=True)
            try:
                tfd = os.open(tmp_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
            except OSError:
                raise StorageError(f"hdiutil makehybrid failed: {esc(r.stderr.strip()[:300])}")
            try:
                if r.returncode != 0 or not stat.S_ISREG(os.fstat(tfd).st_mode):
                    raise StorageError(f"hdiutil makehybrid failed: {esc(r.stderr.strip()[:300])}")
                os.fchmod(tfd, 0o444)
                rename_excl(dfd, tmp_name, dfd, a.name + ".iso")
                st = os.fstat(tfd)
            finally:
                os.close(tfd)
            rec.update(files=files, bytes=size, left_out=len(left), ident=[st.st_dev, st.st_ino])
            reg_save("refdisk", rec)
    finally:
        if made:
            shutil.rmtree(stage, ignore_errors=True)
        if dfd is not None:
            try:
                os.unlink(tmp_name, dir_fd=dfd)
            except OSError:
                pass
            os.close(dfd)
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
    unlink_walked(rec["path"], rec.get("ident"))     # never through a link, never another file
    reg_delete("refdisk", a.name)
    print(f"reference disk '{a.name}' deleted (the folder it was packed from is untouched)")
    return 0


# --- checkpoints with volumes ----------------------------------------------------------------
def ckpt_manifest(vm, name):
    return os.path.join(state_home(), "checkpoints", vm, name + ".json")


def snapshot_path(vol_path, vm, name):
    d, base = os.path.split(vol_path)
    return os.path.join(d, f".{base}.ckpt-{vm}-{name}")


def ckpt_item(vm, name):
    """The lock on one checkpoint: save, restore and delete of it, and volume deletes that edit it, take turns."""
    return ckpt_item_at(ckpt_manifest(vm, name))


def ckpt_item_at(man):
    name = os.path.basename(os.path.dirname(man)) + "/" + os.path.basename(man)[:-5]
    return {"kind": "checkpoint", "name": name, "path": man, "lock": lock_key("checkpoint", man)}


def ckpt_volumes(man):
    return [v for v in (man or {}).get("volumes", []) if isinstance(v, dict) and v.get("name") and v.get("path")]


def cmd_checkpoint(a):
    vm = vm_name()
    man_path = ckpt_manifest(vm, a.name)
    if a.op == "list":
        man = read_json(man_path)
        print(" ".join(v["name"] for v in (man or {}).get("volumes", [])))
        return 0
    # Every operation takes the checkpoint's lock first, then reads its manifest (which nothing else can change
    # now), then reserves exactly the volumes that manifest names, and checks them, before changing anything.
    if a.op == "delete":
        with reserved([], "checkpoint-delete", [ckpt_item(vm, a.name)]):
            man = read_json(man_path) or {}
            with reserved(ckpt_volumes(man), "checkpoint-delete-volumes"):
                for v in man.get("volumes", []):
                    if v.get("snapshot"):
                        unlink_walked(v["snapshot"])
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
        with reserved([], "checkpoint", [ckpt_item(vm, a.name)]):
            old = read_json(man_path) or {}
            with reserved(vols + [v for v in ckpt_volumes(old) if v["name"] not in {r["name"] for r in vols}],
                          "checkpoint-volumes"):
                return save_checkpoint(vols, vm, a.name, man_path)
    if a.op == "restore":
        with reserved([], "restore", [ckpt_item(vm, a.name)]):
            man = read_json(man_path)
            if not man:
                return 0
            with reserved(ckpt_volumes(man), "restore-volumes"):
                return restore_checkpoint(man)
    raise StorageError("usage: storage.py checkpoint save|restore|delete|list NAME")


def save_checkpoint(vols, vm, name, man_path):
    """With the checkpoint and its volumes (old and new) held: no VM can attach one until it's done."""
    for rec in vols:
        os.close(open_image(rec))                   # each reachable without links, and the image that was made
    old = read_json(man_path) or {}
    for v in old.get("volumes", []):
        if v.get("snapshot"):
            unlink_walked(v["snapshot"])
    out = []
    for rec in vols:
        snap = snapshot_path(rec["path"], vm, name)
        unlink_walked(snap)
        how = clone_or_copy(rec["path"], snap, expect=rec["ident"])
        out.append({"name": rec["name"], "uuid": rec["uuid"], "path": rec["path"], "snapshot": snap, "how": how})
        print(f"  ✓ volume '{rec['name']}' {'cloned' if how == 'clone' else 'copied'} with the checkpoint")
    atomic_json(man_path, {"vm": vm, "checkpoint": name, "saved": now(), "volumes": out})
    return 0


def restore_checkpoint(man):
    """With the checkpoint and exactly the volumes its manifest names held. Checked before anything changes: each
    snapshot is there (reached without links), and a volume registered under a name the manifest uses is the same
    volume (same image path and UUID). Each image is then replaced by renaming a copy of its snapshot over it,
    in its own folder (walked), and the registry records the new file's identity."""
    vols = ckpt_volumes(man)
    for v in vols:
        try:
            os.close(walk_file(v.get("snapshot", "")))
        except OSError:
            raise StorageError(f"volume '{v['name']}': its snapshot {v.get('snapshot')} is missing or can't be reached "
                               "without following a link")
        rec = reg_load("volume", v["name"])
        if rec and (rec.get("kind") != "disk" or fold(rec.get("path", "")) != fold(v["path"])
                    or rec.get("uuid") != v.get("uuid")):
            raise StorageError(f"volume '{v['name']}' is now another volume than the one in this checkpoint; nothing "
                               "was rolled back")
    for v in vols:
        dfd, leaf = walk_parent(v["path"])
        try:
            tmp = leaf + ".restoring"
            try:
                os.unlink(tmp, dir_fd=dfd)
            except FileNotFoundError:
                pass
            clone_or_copy(v["snapshot"], os.path.join(os.path.dirname(v["path"]), tmp))
            os.chmod(tmp, 0o600, dir_fd=dfd, follow_symlinks=False)
            os.rename(tmp, leaf, src_dir_fd=dfd, dst_dir_fd=dfd)
            st = os.stat(leaf, dir_fd=dfd, follow_symlinks=False)
        finally:
            os.close(dfd)
        rec = reg_load("volume", v["name"]) or {"name": v["name"], "kind": "disk", "path": v["path"], "uuid": v["uuid"],
                                                "created": now()}
        rec["ident"] = [st.st_dev, st.st_ino]
        reg_save("volume", rec)
        print(f"  ✓ volume '{v['name']}' rolled back with the checkpoint")
    return 0


# --- S3-backed folder volumes ----------------------------------------------------------------
# The VM never sees the bucket or the keys: it sees a folder volume, a local copy on the Mac (the shared
# copy), kept in step with the bucket in two stages (exchange() with a private sync copy, which rclone
# bisync keeps in step with the bucket; see "S3: the two stages"). The keys live in the login Keychain (`volume keys`
# stores them through `security`'s own prompt); for each run they're read into rclone's environment
# only (an rclone remote defined by RCLONE_CONFIG_MCS3_*), never into argv, rclone.conf, logs or events.
SECURITY = "/usr/bin/security"
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
ENDPOINT_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?$")
REGION_RE = re.compile(r"^[a-z0-9-]{1,32}$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
REMOTE = "mcs3"
SMALL_FOLDER = 50            # bisync refuses a run where every file changed; below this many, that's normal


def keychain_service(name):
    return f"metacomputer-s3:{name}"


def keychain_get(name, what, probe=False, security=None):
    """A key from the login Keychain, or None. probe: only whether it exists (the value isn't read)."""
    cmd = [security or SECURITY, "find-generic-password", "-s", keychain_service(name), "-a", what]
    if not probe:
        cmd.append("-w")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return True if probe else r.stdout.rstrip("\n")


def s3_dir(name):
    return os.path.join(state_home(), "s3", name)


def s3_remote(b):
    return f"{REMOTE}:{b['bucket']}" + (f"/{b['prefix']}" if b.get("prefix") else "")


def provider_for(endpoint):
    host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0].lower()
    if host.endswith("amazonaws.com"):
        return "AWS"
    if host.endswith("r2.cloudflarestorage.com"):
        return "Cloudflare"
    return "Other"


def rclone_conf():
    conf = os.path.join(state_home(), "s3", "rclone.conf")
    os.makedirs(os.path.dirname(conf), mode=0o700, exist_ok=True)
    if not os.path.exists(conf):
        open(conf, "w").close()
    os.chmod(conf, 0o600)
    return conf


def rclone_env(b, keys, home=None):
    """The whole environment rclone runs with: nothing of ours but a path, a home (its own state folder) and
    the remote."""
    conf = rclone_conf()
    home = home or os.path.dirname(conf)
    tmp = os.path.join(home, "tmp")
    os.makedirs(tmp, mode=0o700, exist_ok=True)
    e = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "TMPDIR": tmp, "LANG": "C",
         "RCLONE_CONFIG": conf, f"RCLONE_CONFIG_{REMOTE.upper()}_TYPE": "s3",
         f"RCLONE_CONFIG_{REMOTE.upper()}_PROVIDER": b.get("provider") or provider_for(b["endpoint"]),
         f"RCLONE_CONFIG_{REMOTE.upper()}_ENDPOINT": b["endpoint"],
         f"RCLONE_CONFIG_{REMOTE.upper()}_ENV_AUTH": "false",
         f"RCLONE_CONFIG_{REMOTE.upper()}_NO_CHECK_BUCKET": "true",
         f"RCLONE_CONFIG_{REMOTE.upper()}_ACCESS_KEY_ID": keys[0],
         f"RCLONE_CONFIG_{REMOTE.upper()}_SECRET_ACCESS_KEY": keys[1]}
    if b.get("region"):
        e[f"RCLONE_CONFIG_{REMOTE.upper()}_REGION"] = b["region"]
    return e


def scrub(text, keys):
    for k in keys or ():
        if k and len(k) >= 4:
            text = text.replace(k, "[key]")
    return text


def rclone_pins():
    """(version, zip name, zip sha256, program sha256) from config/defaults.env; StorageError if one is malformed."""
    ver, zipname = env("RCLONE_VERSION", ""), env("RCLONE_ZIP", "")
    zsha, bsha = env("RCLONE_SHA256", "").lower(), env("RCLONE_BIN_SHA256", "").lower()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", ver or ""):
        raise StorageError("RCLONE_VERSION isn't set (config/defaults.env)")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", zipname or "") or ".." in zipname:
        raise StorageError("RCLONE_ZIP isn't set (config/defaults.env)")
    for name, v in (("RCLONE_SHA256", zsha), ("RCLONE_BIN_SHA256", bsha)):
        if not re.fullmatch(r"[0-9a-f]{64}", v or ""):
            raise StorageError(f"{name} isn't set (config/defaults.env)")
    return ver, zipname, zsha, bsha


def file_sha256(path):
    """SHA-256 of the file at path itself (a link there isn't followed), or None when there's none."""
    import hashlib
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 8 * MIB)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)
    finally:
        os.close(fd)


def unpack_rclone(d, zipname, zsha, bsha):
    """Unpack the rclone program from the downloaded zip in d, if the zip is the pinned one (RCLONE_SHA256) and the
    program in it is the pinned one (RCLONE_BIN_SHA256): written beside, then renamed over d/rclone. True when done."""
    import hashlib
    import zipfile
    z = os.path.join(d, zipname)
    if file_sha256(z) != zsha:
        return False
    try:
        with zipfile.ZipFile(z) as zf:
            members = [i for i in zf.infolist() if not i.is_dir() and i.filename.split("/")[-1] == "rclone"]
            if len(members) != 1:
                return False
            data = zf.read(members[0])
    except (OSError, zipfile.BadZipFile):
        return False
    if hashlib.sha256(data).hexdigest() != bsha:
        return False
    part = os.path.join(d, "rclone.part")
    try:
        os.unlink(part)
    except FileNotFoundError:
        pass
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o755)
    try:
        write_all(fd, data, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(part, 0o755)
    os.replace(part, os.path.join(d, "rclone"))
    try:
        os.unlink(os.path.join(d, "rclone.sha256"))      # an earlier version's digest file: never read, so gone
    except OSError:
        pass
    return True


def rclone_bin(fetch=True, cache=None):
    """The pinned rclone (CACHE/rclone-vVERSION/rclone, CACHE being the checkout's .cache), checked before every use
    against the program's pinned digest (RCLONE_BIN_SHA256 in config/defaults.env), never against a file on disk.
    Missing or different (a planted program included), it's unpacked again from the downloaded zip once that zip
    matches its own pin; failing that, downloaded and verified again (scripts/fetch-artifacts.sh rclone: the signed
    SHA256SUMS, the zip's pin, the program's pin)."""
    ver, zipname, zsha, bsha = rclone_pins()
    d = os.path.join(cache or os.path.join(LAB, ".cache"), f"rclone-v{ver}")
    b = os.path.join(d, "rclone")
    if file_sha256(b) != bsha and not (os.path.isdir(d) and unpack_rclone(d, zipname, zsha, bsha)):
        if not fetch:
            raise StorageError(f"{b} isn't the pinned rclone (RCLONE_BIN_SHA256) and the pinned zip isn't there to "
                               "unpack it again; scripts/fetch-artifacts.sh rclone downloads and verifies it")
        r = subprocess.run([os.path.join(LAB, "scripts", "fetch-artifacts.sh"), "rclone"], stdout=sys.stderr)
        if r.returncode != 0:
            raise StorageError("could not download and verify rclone (scripts/fetch-artifacts.sh rclone)")
    return verify_rclone(b, ver, bsha)


def verify_rclone(b, ver, want=None):
    """b is the pinned rclone: its SHA-256 is want (RCLONE_BIN_SHA256 unless given), checked before it's ever run,
    and only then does it have to report version ver."""
    want = (want or rclone_pins()[3]).lower()
    got = file_sha256(b)
    if got is None:
        raise StorageError(f"rclone isn't installed ({b}); scripts/fetch-artifacts.sh rclone installs it")
    if got != want:
        raise StorageError(f"{b} isn't the pinned rclone (its SHA-256 isn't RCLONE_BIN_SHA256); not running it")
    r = subprocess.run([b, "version"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    first = (r.stdout.splitlines() or [""])[0].strip()
    if first != f"rclone v{ver}":
        raise StorageError(f"{b} reports {first!r}, not rclone v{ver}")
    return b


def s3_keys(name, security=None):
    k = keychain_get(name, "access_key_id", security=security)
    s = keychain_get(name, "secret_access_key", security=security)
    if not k or not s:
        raise StorageError(f"volume '{name}' has no keys in the Keychain; in your own terminal: metacomputer volume keys {name}")
    return k, s


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# rclone never sees a folder the VM can reach (the two stages, below), so nothing the VM does can point it
# anywhere. The sandbox is a second line, against rclone itself: it may write only in the volume's own state
# folder (which holds the sync copy), nowhere else; and it may not read everyone's home folders, other drives
# or the shared temporary folders, except that state folder, its config and itself.
SANDBOX_DENY = ("/Users", "/System/Volumes/Data/Users", "/Volumes", "/private/tmp", "/private/var/folders",
                "/System/Volumes/Data/private/tmp", "/System/Volumes/Data/private/var/folders")


def sb_str(p):
    return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(rclone, state):
    """The sandbox-exec profile rclone runs under: writes only in state, reads denied in SANDBOX_DENY except
    state, its config and itself. The kernel checks each access against the path it actually reaches."""
    allow = sorted({state, canon_or_abs(state)})
    sub = " ".join(f"(subpath {sb_str(d)})" for d in allow)
    return ("(version 1)\n(allow default)\n"
            "(deny file-write*)\n"
            f'(allow file-write* {sub} (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") '
            '(literal "/dev/dtracehelper"))\n'
            "(deny file-read* " + " ".join(f"(subpath {sb_str(d)})" for d in SANDBOX_DENY) + ")\n"
            f"(allow file-read* {sub} (literal {sb_str(rclone)}) (literal {sb_str(canon_or_abs(rclone))}) "
            f"(literal {sb_str(rclone_conf())}))\n")


RCLONE_OUT_MAX = 4 << 20       # the tail of rclone's output kept for the log and for reading conflicts


def run_rclone(args, b, keys, rclone, timeout=1800, stdin=None, state=None, sandbox=True, watch_free=None):
    """rclone with the remote in its environment, confined by sandbox-exec to state (the volume's own folder,
    which holds its sync copy). watch_free: a path whose filesystem must keep the free-space reserve while
    rclone runs (checked twice a second; below it, rclone is stopped). Returns (rc, the tail of its output
    with any key scrubbed): 124 past timeout, 125 stopped for space, 126 no sandbox."""
    import signal
    state = state or os.path.join(state_home(), "s3", "_rclone")
    os.makedirs(state, mode=0o700, exist_ok=True)
    rclone = os.path.realpath(rclone)       # exec'd by its real path: the sandbox allows exactly that file
    cmd = [rclone, "--color", "NEVER", *args]
    if sandbox:
        if not os.access(SANDBOX_EXEC, os.X_OK):
            return 126, f"{SANDBOX_EXEC} is missing, so rclone can't be confined; not running it"
        cmd = [SANDBOX_EXEC, "-p", sandbox_profile(rclone, state), *cmd]
    outp = os.path.join(state, "rclone-run.out")
    fd = os.open(outp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    reserve = reserve_bytes()
    try:
        p = subprocess.Popen(cmd, stdout=fd, stderr=subprocess.STDOUT, env=rclone_env(b, keys, home=state), cwd=state,
                             stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, start_new_session=True)
    finally:
        os.close(fd)
    if stdin is not None:
        try:
            p.stdin.write(stdin.encode())
            p.stdin.close()
        except OSError:
            pass
    end, rc, why = time.monotonic() + timeout, None, ""
    while rc is None:
        try:
            rc = p.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() > end:
            why, rc = f"rclone took longer than {timeout} s", 124
        elif watch_free and free_bytes_path(watch_free) < reserve:
            why, rc = (f"rclone was stopped: the Mac's disk fell below the free-space reserve of {human(reserve)} "
                       "(IMPORT_MIN_FREE_GB)"), 125
        if why:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
    try:
        with open(outp, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - RCLONE_OUT_MAX))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    return rc, scrub(text + (("\n" + why) if why else ""), keys)


def free_bytes_path(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def backup_budget():
    try:
        gb = float(env("S3_BACKUP_MAX_GB", "5") or 5)
    except ValueError:
        gb = 5.0
    return int(max(gb, 0) * (1 << 30))


def prune_backups(folder, budget):
    """Keep the newest backups that fit in budget bytes (the folder is ours: the VM can't reach it)."""
    files = []
    for d, _, fs in os.walk(folder):
        for f in fs:
            p = os.path.join(d, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
    total = 0
    for mtime, size, p in sorted(files, reverse=True):
        total += size
        if total > budget:
            try:
                os.unlink(p)
            except OSError:
                pass


# --- free space: claims ------------------------------------------------------------------------
# Every operation that writes a lot (an S3 sync's stages and rclone) first claims part of the free space on
# each filesystem it writes to. What's free above the reserve, less what other live claims there hold, is
# what's left; an operation takes what it needs, or half of what's left when it can't know, so two
# operations never spend the same bytes and the reserve is never planned away. The real free space is also
# checked before each copy and while rclone runs.
def space_dir():
    d = os.path.join(state_home(), "space")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def claims_on(pool):
    """Bytes held by live claims on the pool (space_pool; stale claims are removed)."""
    held = 0
    for f in os.listdir(space_dir()):
        p = os.path.join(space_dir(), f)
        rec = read_json(p) if f.endswith(".json") else None
        if not isinstance(rec, dict):
            continue
        if not holder_alive(rec):
            try:
                os.unlink(p)
            except OSError:
                pass
        elif rec.get("pool") == pool:
            held += int(rec.get("bytes", 0))
    return held


@contextmanager
def space_claims(paths, what):
    """Claim free space in the pool of each path (space_pool: an APFS container, else the filesystem; one claim per
    pool). Yields {path: bytes held}, the same number for paths drawing on one pool; StorageError when one has
    nothing left above the reserve."""
    pool_of = {p: space_pool(p) for p in paths}
    pools = {}
    for p in paths:
        pools.setdefault(pool_of[p], p)
    files, grants = [], {}
    pstart = proc_start(os.getpid())
    try:
        with GlobalLock():
            for pool, p in pools.items():
                left = free_bytes_path(p) - reserve_bytes() - claims_on(pool)
                if left <= 0:
                    raise StorageError(f"the disk holding {p} has nothing free above the reserve of "
                                       f"{human(reserve_bytes())} (IMPORT_MIN_FREE_GB) that other operations haven't "
                                       "claimed; not syncing")
                grants[pool] = left // 2
                f = os.path.join(space_dir(), f"{what}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
                atomic_json(f, {"pool": pool, "bytes": grants[pool], "pid": os.getpid(), "pstart": pstart,
                                "what": what, "since": now()})
                files.append(f)
        yield {p: grants[pool_of[p]] for p in paths}
    finally:
        for f in files:
            try:
                os.unlink(f)
            except OSError:
                pass


# --- S3: the two stages ------------------------------------------------------------------------
# rclone never touches a folder the VM can reach or swap. Each S3 volume has two copies:
#   1. the shared copy, rec["path"]: the folder the VM sees (and Finder shows);
#   2. the sync copy, s3_dir(name)/sync: inside metacomputer's state folder, which can never be shared.
# rclone bisync runs between the sync copy and the bucket only, confined to the volume's state folder. Before
# and after it, exchange() brings the two copies into agreement, through descriptors: every component opened
# without following links from the shared copy's own descriptor (checked to be the folder that was created),
# only plain files copied (links, FIFOs, devices never), each file bound to what was listed, new files there
# written under a temporary name, given the quarantine attribute, and only then renamed into place, so nothing
# from the bucket is ever visible in the shared copy without it. Changes are found against what both copies
# held after the last exchange (stage.json): a change on one side goes to the other; a file changed on both
# keeps both (the older one renamed NAME.conflictN.EXT); a deletion goes across unless the other side changed.
# Empty folders aren't carried (like rclone's bisync, and buckets, which have no folders).
STAGE_TMP = ".metacomputer-tmp-"
PARTIAL = ".mcomp-partial"            # rclone's temporary files in the sync copy (--partial-suffix: 16 at most)
EXCHANGE_MAX = 1800.0                 # seconds one exchange may take
RENAME_EXCL = 0x00000004


def stage_excluded(name):
    return (name == ".DS_Store" or name.startswith("._") or name.startswith(".metacomputer-")
            or name.endswith(".link-removed.txt") or name.endswith(PARTIAL))


def _fsig(st):
    return ["f", st.st_size, st.st_mtime_ns, st.st_ino]


def rename_excl(fromfd, old, tofd, new):
    """renameatx_np(RENAME_EXCL): old in folder fromfd becomes new in folder tofd (one filesystem), atomically;
    new must not exist (FileExistsError)."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.renameatx_np(fromfd, old.encode("utf-8", "surrogateescape"), tofd, new.encode("utf-8", "surrogateescape"),
                         RENAME_EXCL) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), new)


def stage_scan(rootfd, deadline, limit=None, cleanup=()):
    """({relative path: ("f", size, mtime_ns, inode)} of the plain files, the set of folders) under rootfd,
    through descriptors (walk_fd: bounded in entries, depth and time; links and special files never followed
    or listed). Excluded names are left out. cleanup: name prefixes of temporary files this program made in an
    exchange that was interrupted (session_prefixes); only plain files with one of them are removed, nothing else
    (and the shared copy is never given any: nothing is made there under a temporary name)."""
    files, dirs = {}, set()
    cleanup = tuple(cleanup)

    def visit(dfd, n, r, st):
        if stage_excluded(n):
            if cleanup and n.startswith(cleanup) and stat.S_ISREG(st.st_mode):
                try:
                    os.unlink(n, dir_fd=dfd)
                except OSError:
                    pass
            return None
        if stat.S_ISDIR(st.st_mode):
            cfd = _open_at(dfd, n, st, DIR_NOFOLLOW)
            if cfd is not None:
                dirs.add(r)
            return cfd
        if stat.S_ISREG(st.st_mode):
            files[r] = _fsig(st)
        return None
    walk_fd(rootfd, visit, limit=limit, deadline=deadline)
    return files, dirs


def _dir_at(rootfd, parts, create=False, quarantine=False, stage=None, prefix=None):
    """The descriptor of the folder rootfd/parts..., every component opened without following links. With
    create, a missing one is made (0755), given the quarantine attribute when asked; with stage (a folder on the
    same filesystem that no VM sees), it's made and marked there first and then renamed into place, so it never
    appears without the attribute. None when a component is missing (without create), a link, or not a folder."""
    fd = os.dup(rootfd)
    try:
        for c in parts:
            try:
                nfd = os.open(c, DIR_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return None
                nfd = None
                if stage is not None:
                    tmp = (prefix or STAGE_TMP) + uuid.uuid4().hex[:12]
                    os.mkdir(tmp, 0o755, dir_fd=stage)
                    tfd = os.open(tmp, DIR_NOFOLLOW, dir_fd=stage)
                    try:
                        if quarantine:
                            ensure_quarantine_fd(tfd)
                        rename_excl(stage, tmp, fd, c)
                        nfd, tfd = tfd, None
                    except FileExistsError:
                        os.rmdir(tmp, dir_fd=stage)
                    finally:
                        if tfd is not None:
                            os.close(tfd)
                else:
                    try:
                        os.mkdir(c, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                if nfd is None:
                    try:
                        nfd = os.open(c, DIR_NOFOLLOW, dir_fd=fd)
                    except OSError:
                        return None
                    if quarantine and stage is None:
                        try:
                            ensure_quarantine_fd(nfd)
                        except BaseException:
                            os.close(nfd)
                            raise
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM):
                    return None
                raise
            os.close(fd)
            fd = nfd
        out, fd = fd, None
        return out
    finally:
        if fd is not None:
            os.close(fd)


def _lstat_sig(dfd, name):
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return _fsig(st) if stat.S_ISREG(st.st_mode) else ["other", stat.S_IFMT(st.st_mode)]


class Exchange:
    """One exchange between the shared copy (side 1) and the sync copy (side 2); see the comment above."""

    def __init__(self, roots, base, left, deadline, stage=None, session=None, cleanup=()):
        self.root = roots                     # {1: fd, 2: fd}
        self.stage = stage                    # side 1's staging folder (open_stage): where its new files are made
        self.prefix = STAGE_TMP + (f"{session}-" if session else "")   # this sync's temporary names (tracked)
        self.cleanup = tuple(cleanup)         # earlier sessions' prefixes: their leftovers in the sync copy go
        self.scanned = False
        self.base = {1: base.setdefault("one", {}), 2: base.setdefault("two", {})}
        self.left = left                      # {1: bytes, 2: bytes} each side may write ("_shared": one pool)
        self.deadline = deadline
        self.dev = {k: os.fstat(fd).st_dev for k, fd in roots.items()}
        self.copied = {1: 0, 2: 0}
        self.deleted = {1: 0, 2: 0}
        self.conflicts, self.skipped = [], []

    def spend(self, side, n):
        self.left[side] -= n
        if self.left.get("_shared"):
            self.left[3 - side] -= n

    def digest(self, side, rel, want):
        """SHA-256 of the file at rel on side, read from its descriptor while it's still want; None otherwise."""
        import hashlib
        parts = rel.split("/")
        d = _dir_at(self.root[side], parts[:-1])
        if d is None:
            return None
        try:
            fd = os.open(parts[-1], FILE_NOFOLLOW, dir_fd=d)
        except OSError:
            return None
        finally:
            os.close(d)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or _fsig(st) != list(want):
                return None
            h, pos = hashlib.sha256(), 0
            while pos < st.st_size:
                if time.monotonic() > self.deadline:
                    raise StorageError(f"the exchange took longer than {EXCHANGE_MAX:.0f} s; the rest waits for the "
                                       "next sync")
                chunk = os.pread(fd, min(8 * MIB, st.st_size - pos), pos)
                if not chunk:
                    break
                h.update(chunk)
                pos += len(chunk)
            return h.hexdigest()
        finally:
            os.close(fd)

    def copy(self, src, dst, rel, want, expect):
        """Copy the file at rel from side src (it must still be want) to side dst (whose entry must still be
        expect, None for none). A file bound for the shared copy is made in its staging folder (no VM sees it),
        given the quarantine attribute through the descriptor this process holds, and only then renamed into
        the shared copy in one step. Returns the new file's signature on dst, or None when it was left for the
        next exchange (something changed meanwhile, or the place is taken by a folder or a link)."""
        parts = rel.split("/")
        name = parts[-1]
        sdir = _dir_at(self.root[src], parts[:-1])
        if sdir is None:
            return None
        ddir = sfd = tfd = None
        tmp = None
        to_vm = dst == 1
        if to_vm and self.stage is None:
            raise StorageError("no staging folder for the shared copy")
        try:
            try:
                sfd = os.open(name, FILE_NOFOLLOW, dir_fd=sdir)
            except OSError:
                return None
            st = os.fstat(sfd)
            if not stat.S_ISREG(st.st_mode) or _fsig(st) != list(want):
                return None
            ddir = _dir_at(self.root[dst], parts[:-1], create=True, quarantine=to_vm,
                           stage=self.stage if to_vm else None, prefix=self.prefix)
            if ddir is None:
                self.skipped.append(rel)
                return None
            if _lstat_sig(ddir, name) != (list(expect) if expect else None):
                return None
            tdir = self.stage if to_vm else ddir
            tmp = self.prefix + uuid.uuid4().hex[:12]
            how, wrote = copy_from_fd(sfd, tdir, tmp, st, budget=max(self.left[dst], 0),
                                      clone=self.dev[src] == os.fstat(tdir).st_dev)
            self.spend(dst, wrote)
            tfd = os.open(tmp, FILE_NOFOLLOW, dir_fd=tdir)
            os.fchmod(tfd, (stat.S_IMODE(st.st_mode) & 0o755) | 0o600)
            if to_vm:
                ensure_quarantine_fd(tfd)            # on the file this process holds, before it has a name there
            os.utime(tfd, ns=(st.st_atime_ns, st.st_mtime_ns))
            new = _fsig(os.fstat(tfd))
            try:
                if expect is None:
                    rename_excl(tdir, tmp, ddir, name)
                else:
                    if _lstat_sig(ddir, name) != list(expect):
                        return None
                    os.rename(tmp, name, src_dir_fd=tdir, dst_dir_fd=ddir)
            except OSError as e:
                if e.errno in (errno.EEXIST, errno.EISDIR, errno.ENOTEMPTY, errno.ENOTDIR):
                    self.skipped.append(rel)
                    return None
                raise
            tmp = None
            self.copied[dst] += 1
            return new
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp, dir_fd=self.stage if to_vm else ddir)
                except (OSError, TypeError):
                    pass
            for fd in (tfd, sfd, ddir, sdir):
                if fd is not None:
                    os.close(fd)

    def delete(self, side, rel, want):
        """Remove the file at rel on side, if it's still want. True when it's gone."""
        parts = rel.split("/")
        d = _dir_at(self.root[side], parts[:-1])
        if d is None:
            return True
        try:
            cur = _lstat_sig(d, parts[-1])
            if cur is None:
                return True
            if cur != list(want):
                return False
            os.unlink(parts[-1], dir_fd=d)
            self.deleted[side] += 1
            return True
        finally:
            os.close(d)

    def set_aside(self, side, rel, want, taken):
        """Rename the file at rel on side to NAME.conflictN.EXT (a name neither copy has). Returns the new
        relative path, or None."""
        parts = rel.split("/")
        stem, ext = os.path.splitext(parts[-1])
        d = _dir_at(self.root[side], parts[:-1])
        if d is None:
            return None
        try:
            if _lstat_sig(d, parts[-1]) != list(want):
                return None
            for n in range(1, 1000):
                new = f"{stem}.conflict{n}{ext}"
                newrel = "/".join(parts[:-1] + [new])
                if newrel in taken:
                    continue
                try:
                    rename_excl(d, parts[-1], d, new)
                except FileExistsError:
                    continue
                return newrel
            return None
        finally:
            os.close(d)

    def prune(self, side, rels, keep):
        """Remove folders on side left empty by deletions, deepest first, unless the other side has them."""
        for rel in sorted(rels, key=lambda r: -r.count("/")):
            if not rel or rel in keep:
                continue
            parts = rel.split("/")
            d = _dir_at(self.root[side], parts[:-1])
            if d is None:
                continue
            try:
                os.rmdir(parts[-1], dir_fd=d)
            except OSError:
                pass
            finally:
                os.close(d)

    def run(self):
        f1, d1 = stage_scan(self.root[1], self.deadline)
        f2, d2 = stage_scan(self.root[2], self.deadline, cleanup=self.cleanup)
        self.scanned = True
        scan, dirs = {1: f1, 2: f2}, {1: d1, 2: d2}
        B = self.base
        taken = set(f1) | set(f2) | set(B[1]) | set(B[2])
        emptied = {1: set(), 2: set()}
        for rel in sorted(taken, key=lambda r: (r.count("/"), r)):
            if time.monotonic() > self.deadline:
                raise StorageError(f"the exchange took longer than {EXCHANGE_MAX:.0f} s; the rest waits for the next sync")
            s = {1: scan[1].get(rel), 2: scan[2].get(rel)}
            b = {1: B[1].get(rel), 2: B[2].get(rel)}
            c = {k: (list(s[k]) if s[k] else None) != (list(b[k]) if b[k] else None) for k in (1, 2)}
            if not c[1] and not c[2]:
                continue
            if c[1] and c[2]:
                if s[1] is None and s[2] is None:
                    B[1].pop(rel, None)
                    B[2].pop(rel, None)
                    continue
                if s[1] and s[2] and s[1][1:3] == s[2][1:3]:
                    # Same size and time aren't proof of the same contents: they're compared, each read from its
                    # checked descriptor. The same: recorded as in step. Different: a conflict (below; both kept).
                    h1, h2 = self.digest(1, rel, s[1]), self.digest(2, rel, s[2])
                    if h1 is None or h2 is None:
                        continue                       # one changed meanwhile: the next exchange sees it
                    if h1 == h2:
                        B[1][rel], B[2][rel] = s[1], s[2]
                        continue
                if s[1] and s[2]:
                    win = 1 if s[1][2] >= s[2][2] else 2
                    lose = 3 - win
                    newrel = self.set_aside(lose, rel, s[lose], taken)
                    if newrel is None:
                        self.skipped.append(rel)
                        continue
                    taken.add(newrel)
                    self.conflicts.append(rel)
                    B[lose].pop(rel, None)
                    new = self.copy(win, lose, rel, s[win], None)
                    if new:
                        B[win][rel], B[lose][rel] = s[win], new
                    continue
                src = 2 if s[1] is None else 1            # changed on one side, deleted on the other: the change
            else:
                src = 1 if c[1] else 2
            dst = 3 - src
            if s[src] is None:
                if self.delete(dst, rel, s[dst]):
                    B[1].pop(rel, None)
                    B[2].pop(rel, None)
                    emptied[dst].add(rel.rsplit("/", 1)[0] if "/" in rel else "")
            else:
                new = self.copy(src, dst, rel, s[src], s[dst])
                if new:
                    B[src][rel], B[dst][rel] = s[src], new
        for side in (1, 2):
            parents = set()
            for p in emptied[side]:
                while p:
                    parents.add(p)
                    p = p.rsplit("/", 1)[0] if "/" in p else ""
            self.prune(side, parents, dirs[3 - side])
        return {"to_vm": self.copied[1], "from_vm": self.copied[2], "deleted_in_vm": self.deleted[1],
                "deleted_from_vm": self.deleted[2], "conflicts": self.conflicts, "skipped": self.skipped[:20]}


def open_copy_root(rec):
    """The shared copy's folder descriptor, after checking that it may still be shared (the refusals, again)
    and that its path still leads to the folder that was created (same canonical path, device and inode),
    opened without following a link: a copy the VM swapped, or whose parent it swapped, is refused, never
    followed or allowed. StorageError otherwise."""
    path, want = rec.get("path", ""), rec.get("ident")
    if not want:
        raise StorageError(legacy_msg("volume", "s3", rec["name"], path))
    if not os.path.isdir(path):
        raise StorageError(f"the local copy {path} is missing (is its drive connected?)")
    real, ident = check_shareable_id(path)
    if list(ident) != list(want) or real.rstrip("/") != path.rstrip("/"):
        raise StorageError(f"the local copy {path} is no longer the folder that was created (replaced, or reached "
                           f"through a link now); not syncing")
    try:
        fd = os.open(path, DIR_NOFOLLOW)
    except OSError as e:
        raise StorageError(f"the local copy {path} can't be opened as itself ({e.strerror}); not syncing")
    st = os.fstat(fd)
    if [st.st_dev, st.st_ino] != list(want):
        os.close(fd)
        raise StorageError(f"the local copy {path} changed while it was checked; not syncing")
    return fd


def open_sync_root(name):
    """The sync copy (s3_dir(name)/sync, made 0700 if missing): rclone's side, never shared."""
    p = os.path.join(s3_dir(name), "sync")
    os.makedirs(p, mode=0o700, exist_ok=True)
    fd = os.open(p, DIR_NOFOLLOW)
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        os.close(fd)
        raise StorageError(f"{p} isn't this user's private folder")
    return fd, p


def stage_folder(rec):
    """The shared copy's staging folder: .NAME.mcstage.noindex beside it, on its filesystem, refused as a share
    (protected_paths), where files bound for the shared copy are made and quarantined before an atomic rename puts
    them in place."""
    return os.path.join(os.path.dirname(rec["path"].rstrip("/")), f".{rec['name']}.mcstage.noindex")


def stage_item(rec):
    """The lock on a volume's staging folder (pair_conflict: no folder shared with any VM may overlap it)."""
    p = stage_folder(rec)
    return {"kind": "stage", "name": rec["name"], "path": p, "lock": lock_key("stage", p)}


def stage_sessions_path(name):
    return os.path.join(s3_dir(name), "stage-sessions.json")


def recorded_sessions(name):
    rec = read_json(stage_sessions_path(name)) or {}
    return [s for s in rec.get("sessions", []) if isinstance(s, str) and re.fullmatch(r"[0-9a-f]{16}", s)]


def session_prefixes(sessions):
    return tuple(f"{STAGE_TMP}{s}-" for s in sessions)


def clean_stage(fd, sessions):
    """Remove from the staging folder only what this program made in the given sessions: entries named with one
    of their prefixes (session_prefixes), plain files, and folders only when empty. Nothing else is touched."""
    prefixes = session_prefixes(sessions)
    if not prefixes:
        return
    for n in os.listdir(fd):
        if not n.startswith(prefixes):
            continue
        try:
            st = os.stat(n, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                os.unlink(n, dir_fd=fd)
            elif stat.S_ISDIR(st.st_mode):
                os.rmdir(n, dir_fd=fd)
        except OSError:
            pass


def make_stage(rec, dfd=None):
    """Make a volume's staging folder, exclusively (it must not exist yet: an existing folder is never taken over),
    0700; returns its [device, inode] for the registry."""
    p = stage_folder(rec)
    own = dfd is None
    if own:
        dfd, leaf = walk_parent(p)
    else:
        leaf = os.path.basename(p)
    try:
        try:
            os.mkdir(leaf, 0o700, dir_fd=dfd)
        except FileExistsError:
            raise StorageError(f"{p} already exists; not touching it (move it aside)")
        st = os.stat(leaf, dir_fd=dfd, follow_symlinks=False)
        return [st.st_dev, st.st_ino]
    finally:
        if own:
            os.close(dfd)


def remove_stage(rec):
    """At volume delete: the staging folder, if it's the one this program made, emptied of what this program made
    (clean_stage) and removed when that leaves it empty. Anything else in it stays, and so does the folder."""
    p = stage_folder(rec)
    want = rec.get("stage_ident")
    try:
        fd = walk_dir(p)
    except OSError:
        return
    try:
        st = os.fstat(fd)
        if not want or [st.st_dev, st.st_ino] != list(want):
            print(f"  ! {esc(p)} isn't the staging folder metacomputer made; left as it is")
            return
        clean_stage(fd, recorded_sessions(rec["name"]))
    finally:
        os.close(fd)
    try:
        dfd, leaf = walk_parent(p)
        try:
            os.rmdir(leaf, dir_fd=dfd)
        finally:
            os.close(dfd)
    except OSError:
        print(f"  ! {esc(p)} holds files metacomputer didn't put there; left as it is")


def open_stage(rec, root1):
    """The staging folder's descriptor: reached without following links, and the folder this program made for
    the volume (device and inode recorded at creation); this user's, 0700, on the shared copy's filesystem. If it's
    gone, it's made again (exclusively) and recorded. A folder there that isn't the recorded one is refused, and
    nothing in it is touched. StorageError otherwise."""
    p = stage_folder(rec)
    want = rec.get("stage_ident")
    try:
        dfd, leaf = walk_parent(p)
    except OSError as e:
        raise StorageError(f"{os.path.dirname(p)} can't be reached without following a link ({e.strerror})")
    try:
        try:
            fd = os.open(leaf, DIR_NOFOLLOW, dir_fd=dfd)
        except FileNotFoundError:
            ident = make_stage(rec, dfd)
            cur = reg_load("volume", rec["name"]) or rec
            cur["stage_ident"] = rec["stage_ident"] = ident
            reg_save("volume", cur)
            want = ident
            fd = os.open(leaf, DIR_NOFOLLOW, dir_fd=dfd)
        except OSError as e:
            raise StorageError(f"the staging folder {p} can't be opened as itself ({e.strerror}); not syncing")
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if not want or [st.st_dev, st.st_ino] != list(want):
        os.close(fd)
        raise StorageError(f"{p} isn't the staging folder metacomputer made for this volume (replaced, or made by "
                           "someone else); not syncing, and nothing in it is touched. Move it aside: the next sync "
                           "makes a new one")
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077 or st.st_dev != os.fstat(root1).st_dev:
        os.close(fd)
        raise StorageError(f"{p} isn't this user's private folder on the local copy's disk")
    return fd


def stage_base_path(name):
    return os.path.join(s3_dir(name), "stage.json")


def folder_empty(path):
    try:
        with os.scandir(path) as it:
            return not any(not stage_excluded(e.name) for e in it)
    except OSError:
        return False


def s3_sync(rec, reason="manual", force=False, rclone=None, security=None, resync=False, sandbox=True):
    """One sync of an S3 volume, serialized per volume: exchange (shared copy -> sync copy), rclone bisync
    (sync copy <-> bucket), exchange again (sync copy -> shared copy). Never raises; returns {"ok", "summary",
    "conflicts", ...} and records it (last-sync.json, the log, events).
    - The shared copy is checked and reserved first (open_copy_root; a reservation, so no VM can attach a
      read-write folder around it until the sync is done); a copy that was replaced isn't synced.
    - Free space is claimed on each disk written to (space_claims); rclone's transfers are capped to its claim,
      and it's stopped if the disk falls below the reserve anyway.
    - A resync (which picks one side where both changed) runs only when asked for (resync=True), never on its
      own, and the first sync of a copy that already holds files counts as one.
    - Backups (older versions rclone replaced) are pruned to S3_BACKUP_MAX_GB before and after."""
    name, b = rec["name"], rec.get("s3") or {}
    d = s3_dir(name)
    os.makedirs(os.path.join(d, "bisync"), mode=0o700, exist_ok=True)
    out = {"ok": False, "at": now(), "reason": reason, "conflicts": [], "resynced": False, "forced": False}
    lockf = os.open(os.path.join(d, "sync.lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    root1 = root2 = stage1 = None
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        keys = None
        base = read_json(stage_base_path(name)) or {}
        if not isinstance(base, dict) or not all(isinstance(base.get(k, {}), dict) for k in ("one", "two")):
            base = {}                     # unreadable: every file counts as changed on both sides (kept, not lost)
        try:
            path = rec.get("path", "")
            copy_item = {"kind": "s3", "name": name, "path": path, "rw": False, "exclusive": False,
                         "lock": lock_key("s3copy", path)}
            with reserved([], "s3-sync", [copy_item, stage_item(rec)]):
                root1 = open_copy_root(rec)
                root2, sync = open_sync_root(name)
                stage1 = open_stage(rec, root1)
                prior = recorded_sessions(name)
                session = uuid.uuid4().hex[:16]
                atomic_json(stage_sessions_path(name), {"sessions": prior + [session]})   # before any is used
                clean_stage(stage1, prior)
                done = []
                prune_backups(os.path.join(d, "backup"), backup_budget())
                with space_claims([path, sync], f"s3-{name}") as grant:
                    shared_disk = space_pool(path) == space_pool(sync)       # one allocation pool
                    left = {1: grant[path], 2: grant[sync], "_shared": shared_disk}
                    deadline = time.monotonic() + EXCHANGE_MAX
                    ex = Exchange({1: root1, 2: root2}, base, left, deadline, stage1, session,
                                  session_prefixes(prior + [session]))
                    try:
                        out["before"] = ex.run()
                    finally:
                        atomic_json(stage_base_path(name), base)
                        if ex.scanned:
                            done.append(1)
                    rclone = rclone or rclone_bin()
                    keys = s3_keys(name, security)
                    first = not any(f.endswith(".lst") for f in os.listdir(os.path.join(d, "bisync")))
                    if first and not resync and not folder_empty(sync):
                        raise StorageError(f"the local copy already holds files, and a first sync would pick one side "
                                           f"wherever both have a file (the older copy goes to the backup folders). When "
                                           f"you're ready: metacomputer volume sync {name} --resync")
                    cap = max(left[2], 0)
                    if cap <= 0:
                        raise StorageError("no free space left in this sync's claim for rclone; not syncing")
                    base_args = ["bisync", sync, s3_remote(b), "--workdir", os.path.join(d, "bisync"),
                                 "--conflict-resolve", "newer", "--conflict-loser", "num", "--conflict-suffix", "conflict",
                                 "--suffix-keep-extension", "--resilient", "--recover", "--max-lock", "2m",
                                 "--backup-dir1", os.path.join(d, "backup"), "--max-transfer", f"{cap}B",
                                 "--partial-suffix", PARTIAL, "--exclude", ".DS_Store", "--exclude", "._*",
                                 "--exclude", "*.link-removed.txt", "--exclude", ".metacomputer-*",
                                 "--exclude", "*" + PARTIAL, "-v"]
                    extra = []
                    if first or resync:
                        extra = ["--resync", "--resync-mode", "newer"]
                        if b.get("prefix"):
                            extra += ["--backup-dir2", f"{REMOTE}:{b['bucket']}/{b['prefix']}.metacomputer-backup"]
                        out["resynced"] = True
                    free_before = free_bytes_path(sync)
                    run = lambda args: run_rclone(args, b, keys, rclone, state=d, sandbox=sandbox,  # noqa: E731
                                                  watch_free=sync)
                    rc, text = run(base_args + extra + (["--force"] if force else []))
                    if rc != 0 and "all files were changed" in text and not force and count_files(sync) < SMALL_FOLDER:
                        rc, text = run(base_args + extra + ["--force"])
                        out["forced"] = True
                    used = max(0, free_before - free_bytes_path(sync))
                    left[2] -= used
                    if shared_disk:
                        left[1] -= used
                    log = os.open(os.path.join(d, "rclone.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
                                  | os.O_CLOEXEC, 0o600)
                    try:
                        os.write(log, f"--- {out['at']} {reason} (exit {rc})\n{text[-200000:]}\n".encode())
                    finally:
                        os.close(log)
                    out["conflicts"] = conflicts_in(text)
                    out["ok"] = rc == 0
                    if rc == 0:
                        out["summary"] = ("first sync with" if first else "synced with") \
                            + f" s3://{b['bucket']}/{b.get('prefix') or ''}"
                    else:
                        why = [l for l in text.splitlines() if "ERROR" in l or "Failed" in l or "abort" in l.lower()
                               or "stopped" in l]
                        out["error"] = (why or text.strip().splitlines()[-1:] or ["rclone failed"])[-1][:300]
                        hint = ""
                        if "--resync" in text and "must run" in text.lower():
                            hint = (f"; rclone needs a resync, which picks one side wherever both changed: run "
                                    f"metacomputer volume sync {name} --resync when you're ready (older copies go to "
                                    "the backup folders; with bucket versioning on, nothing is lost)")
                        elif "all files were changed" in text:
                            hint = f"; if every file really changed, metacomputer volume sync {name} --force"
                        out["summary"] = f"sync failed ({out['error']})" + hint
                    # Whatever rclone managed (all of it, part, nothing) reaches the shared copy the same way.
                    try:
                        after = Exchange({1: root1, 2: root2}, base, left, time.monotonic() + EXCHANGE_MAX,
                                         stage1, session).run()
                    finally:
                        atomic_json(stage_base_path(name), base)
                    out["after"] = after
                    out["quarantined"] = after["to_vm"]
                    out["conflicts"] += [c for c in out["before"]["conflicts"] + after["conflicts"]
                                         if c not in out["conflicts"]]
                    if out["ok"] and out["conflicts"]:
                        out["summary"] += f"; {len(out['conflicts'])} conflict(s): both copies kept"
        except (StorageError, ProtectionFailure) as e:
            out["ok"] = False
            out["error"] = str(e)
            out["summary"] = f"not synced: {e}"
        except OSError as e:
            out["ok"] = False
            out["error"] = f"{e.strerror or e}"
            out["summary"] = f"not synced: {out['error']}"
        prune_backups(os.path.join(d, "backup"), backup_budget())
        try:
            atomic_json(os.path.join(d, "last-sync.json"), out)
        except OSError:
            pass
        if out["conflicts"]:
            emit("task", "s3_conflict", f"S3 volume '{name}': {len(out['conflicts'])} file(s) changed on both sides; both "
                 "copies kept (the older one with a .conflictN name)", "warn",
                 data={"volume": name, "files": out["conflicts"][:10]}, origin="watcher" if reason == "interval" else "cli")
        if not out["ok"] and reason != "manual":
            emit("task", "s3_sync_failed", f"S3 volume '{name}' didn't sync ({reason})", "error",
                 data={"volume": name, "reason": reason, "error": out.get("error", "")[:200]},
                 action=f"metacomputer volume sync {name}", origin="watcher" if reason == "interval" else "cli")
        return out
    finally:
        if stage1 is not None:
            try:
                clean_stage(stage1, prior + [session])
                if done:                  # the sync copy was scanned: earlier sessions' leftovers there are gone
                    atomic_json(stage_sessions_path(name), {"sessions": []})
            except (OSError, NameError):
                pass
        for fd in (root1, root2, stage1):
            if fd is not None:
                os.close(fd)
        fcntl.flock(lockf, fcntl.LOCK_UN)
        os.close(lockf)


def conflicts_in(text):
    """Files bisync kept twice in this run: changed on both sides and with a copy renamed .conflictN (it
    also warns about a file that changed on both sides but ended up identical, and keeps one copy)."""
    both = re.findall(r"New or changed in both paths\s*-\s*(\S.*)$", text, re.M)
    renamed = [os.path.basename(r.strip()) for r in re.findall(r"Renaming Path[12] copy\s*-\s*(\S.*)$", text, re.M)]
    out = []
    for n in both:
        stem = os.path.splitext(os.path.basename(n.strip()))[0]
        if any(r.startswith(stem + ".conflict") for r in renamed) and n.strip() not in out:
            out.append(n.strip())
    return out[:50]


def count_files(root):
    n = 0
    for _, _, fs in os.walk(root):
        n += len(fs)
        if n >= SMALL_FOLDER:
            break
    return n


def take_sync_lock(it, vm, lab, pid):
    """One VM at a time keeps a volume in sync (others attach the same local copy without syncing)."""
    lk = {"kind": "s3sync", "name": it["name"], "path": it["path"], "lock": lock_key("s3sync", it["path"])}
    try:
        acquire([lk], vm, lab, pid)
        return True
    except StorageError:
        return False


def create_s3(a):
    if a.size or a.folder:
        raise StorageError("an S3 volume takes --bucket and --endpoint (and --region, --prefix, --dir), not --size or --folder")
    if not a.bucket or not BUCKET_RE.match(a.bucket) or ".." in a.bucket:
        raise StorageError("--bucket: an S3 bucket name (3-63 lowercase letters, digits, dots and dashes)")
    if not a.endpoint or not ENDPOINT_RE.match(a.endpoint):
        raise StorageError("--endpoint: a URL like https://s3.us-east-1.amazonaws.com, "
                           "https://ACCOUNT.r2.cloudflarestorage.com or http://127.0.0.1:9000")
    if a.region and not REGION_RE.match(a.region):
        raise StorageError("--region: like us-east-1 (or auto for R2)")
    prefix = (a.prefix or "").strip("/")
    if prefix and (not PREFIX_RE.match(prefix) or ".." in prefix.split("/")):
        raise StorageError("--prefix: folder names of letters, digits, . _ - separated by /")
    d, create = destination("volumes", a.dir, "copy")
    dfd = None
    leaf = a.name + ".noindex"
    local = os.path.join(d, leaf)
    # A local copy is meant to be shared, so a registered folder above it is fine; what isn't is a folder a VM
    # has read-write around it right now (the reservation refuses that), or a refused place (check_shareable_id).
    copy_item = {"kind": "s3", "name": a.name, "path": local, "rw": False, "exclusive": False,
                 "lock": lock_key("s3copy", local)}
    endpoint = a.endpoint.rstrip("/")
    host = re.sub(r"^https?://", "", endpoint).split(":")[0]
    b = {"bucket": a.bucket, "endpoint": endpoint, "region": a.region or "", "prefix": prefix,
         "provider": provider_for(endpoint)}
    try:
        # The staging folder beside the copy is reserved too: no VM may have a folder (even read-only) holding it.
        with reserved([], "create", [copy_item, stage_item({"name": a.name, "path": local})]):
            dfd = open_destination(d, create)
            d = fd_path(dfd)
            local = os.path.join(d, leaf)
            try:
                os.mkdir(leaf, 0o755, dir_fd=dfd)
            except FileExistsError:
                raise StorageError(f"{local} already exists; not touching it")
            lfd = os.open(leaf, DIR_NOFOLLOW, dir_fd=dfd)
            made = [os.fstat(lfd).st_dev, os.fstat(lfd).st_ino]
            os.close(lfd)
            try:
                real, ident = check_shareable_id(local)
                if list(ident) != made:
                    raise StorageError(f"{local} changed while it was made")
                stage_ident = make_stage({"name": a.name, "path": real}, dfd)
            except StorageError:
                os.rmdir(leaf, dir_fd=dfd)
                raise
            # Identities recorded, inside the reservation: every sync and start checks the copy and its staging
            # folder are still these (open_copy_root, open_stage).
            reg_save("volume", {"name": a.name, "kind": "s3", "path": real, "ident": list(ident),
                                "stage_ident": stage_ident, "s3": b, "created": now()})
    finally:
        if dfd is not None:
            os.close(dfd)
    print(f"volume '{a.name}': s3://{a.bucket}/{prefix} at {endpoint}, kept in step with the local copy "
          f"{esc(real)} (visible in Finder; the VM sees only that folder; rclone works on a private copy in "
          f"{esc(os.path.join(s3_dir(a.name), 'sync'))}, which no VM ever sees)")
    if endpoint.startswith("http://") and host not in ("127.0.0.1", "localhost", "::1"):
        print("  ! the endpoint is plain http: keys sign each request, but the files travel unencrypted")
    print(f"Next, in your own terminal: metacomputer volume keys {a.name}   (asks for the access key ID and secret; "
          f"they go into the macOS Keychain)")
    print(f"Then: metacomputer volume test {a.name}, and VOLUMES={a.name} (read-only) or {a.name}:rw in the config")
    note_rebuild()
    return 0


def cmd_volume_keys(a):
    rec = reg_load("volume", check_name(a.name))
    if not rec or rec.get("kind") != "s3":
        raise StorageError(f"no S3 volume named {a.name} (metacomputer volume list)")
    if not sys.stdin.isatty():
        raise StorageError("volume keys asks for the keys at your terminal; run it in your own terminal, not through "
                           "Claude or a script (keys never go on a command line)")
    svc = keychain_service(a.name)
    for what, label in (("access_key_id", "access key ID"), ("secret_access_key", "secret access key")):
        print(f"The {label} for s3://{rec['s3']['bucket']} (macOS asks twice; nothing shows as you type):", flush=True)
        r = subprocess.run([SECURITY, "add-generic-password", "-U", "-s", svc, "-a", what,
                            "-l", f"metacomputer S3 volume {a.name}: {label}", "-w"])
        if r.returncode != 0:
            raise StorageError(f"the {label} wasn't stored (security exited {r.returncode})")
    print(f"keys for '{a.name}' are in your login Keychain (service {svc}); check them: metacomputer volume test {a.name}")
    return 0


def cmd_volume_test(a):
    rec = reg_load("volume", check_name(a.name))
    if not rec or rec.get("kind") != "s3":
        raise StorageError(f"no S3 volume named {a.name} (metacomputer volume list)")
    b = rec["s3"]
    rclone = rclone_bin()
    print(f"  ✓ rclone {env('RCLONE_VERSION')} (pinned, verified)")
    keys = s3_keys(a.name)
    print("  ✓ keys in the Keychain")
    rc, text = run_rclone(["lsf", s3_remote(b), "--max-depth", "1"], b, keys, rclone, timeout=60, state=s3_dir(a.name))
    if rc != 0:
        why = [l for l in text.splitlines() if l.strip()][-1:] or ["no answer"]
        raise StorageError(f"listing s3://{b['bucket']}/{b.get('prefix') or ''} failed: {esc(why[0][:300])}")
    print(f"  ✓ the endpoint answers and the keys may list s3://{b['bucket']}/{b.get('prefix') or ''}")
    probe = f".metacomputer-test-{uuid.uuid4().hex[:12]}"
    target = s3_remote(b) + "/" + probe
    rc, text = run_rclone(["rcat", target], b, keys, rclone, timeout=60, stdin="metacomputer volume test\n", state=s3_dir(a.name))
    rc2, _ = run_rclone(["deletefile", target], b, keys, rclone, timeout=60, state=s3_dir(a.name)) if rc == 0 else (1, "")
    if rc != 0:
        raise StorageError(f"the keys can't write to the bucket: {esc((text.strip().splitlines() or [''])[-1][:300])}")
    print("  ✓ the keys may write" + ("" if rc2 == 0 else " (but the test object couldn't be deleted: " + probe + ")"))
    rc, text = run_rclone(["backend", "versioning", f"{REMOTE}:{b['bucket']}"], b, keys, rclone, timeout=60, state=s3_dir(a.name))
    state = text.strip().splitlines()[-1].strip().strip("\"") if rc == 0 and text.strip() else ""
    if state == "Enabled":
        print("  ✓ bucket versioning is on: every change on the bucket's side keeps the earlier version")
    elif state:
        print(f"  ! bucket versioning is {state}: turn it on, so an overwrite or delete synced from the VM can be undone")
    else:
        print("  ! couldn't read the bucket's versioning (this store may not support it); with versioning on, an "
              "overwrite or delete synced from the VM can be undone")
    return 0


def cmd_volume_sync(a):
    rec = reg_load("volume", check_name(a.name))
    if not rec or rec.get("kind") != "s3":
        raise StorageError(f"no S3 volume named {a.name} (metacomputer volume list)")
    r = s3_sync(rec, reason="manual", force=a.force, resync=a.resync)
    print(("  ✓ " if r["ok"] else "  ! ") + esc(r["summary"]))
    for c in r["conflicts"][:20]:
        print(f"    conflict: {esc(c)} (both copies kept)")
    return 0 if r["ok"] else 1


# --- read-write folders: the watcher ---------------------------------------------------------
# While a VM has a Mac folder read-write, everything it writes there is on the Mac. Measured
# through Apple's virtiofs (docs/storage.md): the VM can create links to anywhere, FIFOs and hard
# links, and list, remove or rewrite any extended attribute, com.apple.quarantine included (a value
# with the "user approved" flag lets an unsigned binary run). So, every few seconds, the watcher
# compares the folder with what it saw before: a new or changed file or folder gets (back) the
# quarantine attribute, so Gatekeeper checks it like a download; a link whose target resolves
# outside the folder is replaced by a note; a FIFO, socket or device is removed; set-id bits go.
# What was there before the VM booted is left alone (that was the Mac's).
QUARANTINE = "com.apple.quarantine"
QUAR_OK = re.compile(rb"^0081;[0-9a-f]{8};metacomputer;$")
XATTR_NOFOLLOW = 0x0001
WATCH_EVERY = 2.0


class ProtectionFailure(Exception):
    """The watcher can't cover a read-write folder completely (too many entries or too deep, unreadable, an
    attribute that won't stick, the Mac's disk nearly full): protection fails loudly and the VM is stopped."""


MAX_DEPTH = 128


def watch_limit():
    """MAX_WATCH; MCOMP_WATCH_MAX can only lower it (the tests use that)."""
    v = os.environ.get("MCOMP_WATCH_MAX", "")
    return min(MAX_WATCH, int(v)) if v.isdigit() else MAX_WATCH


PASS_MAX = 300.0             # seconds one pass over the read-write folders may take
STALL_MAX = 60.0             # seconds the supervisor waits for the watcher to show progress
SUPERVISE_EVERY = 0.5        # how often the supervisor checks free space and progress


def _lower_only(var, ceiling):
    """A limit the environment can only lower (the tests use that)."""
    try:
        v = float(os.environ.get(var, ""))
    except ValueError:
        return ceiling
    return min(ceiling, v) if v > 0 else ceiling


def pass_limit():
    return _lower_only("MCOMP_WATCH_PASS_MAX", PASS_MAX)


def stall_limit():
    return _lower_only("MCOMP_WATCH_STALL_MAX", STALL_MAX)


_HEART = {"fd": None, "last": 0.0}


def beat():
    """Tell the supervisor the watcher is making progress (a byte down its pipe, at most four times a
    second). A supervisor that's gone is a ProtectionFailure: nothing would stop the VM if the watcher died."""
    fd = _HEART["fd"]
    if fd is None or threading.current_thread() is not threading.main_thread():
        return                                   # a sync thread's progress isn't the watcher's
    t = time.monotonic()
    if t - _HEART["last"] < 0.25:
        return
    _HEART["last"] = t
    try:
        os.write(fd, b".")
    except BlockingIOError:
        pass
    except OSError as e:
        raise ProtectionFailure(f"the watcher's supervisor is gone ({e.strerror})")


def reserve_bytes():
    try:
        gb = float(env("IMPORT_MIN_FREE_GB", "10") or 10)
    except ValueError:
        gb = 10.0
    return int(max(gb, 0) * (1 << 30))


def free_bytes_at(fd):
    st = os.fstatvfs(fd)
    return st.f_bavail * st.f_frsize


def _xlibc():
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    for fn, first in (("getxattr", ctypes.c_char_p), ("setxattr", ctypes.c_char_p),
                      ("fgetxattr", ctypes.c_int), ("fsetxattr", ctypes.c_int)):
        f = getattr(libc, fn)
        f.argtypes = [first, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        f.restype = ctypes.c_ssize_t if fn.endswith("getxattr") else ctypes.c_int
    return libc


def get_xattr(path, name):
    """An attribute of the file at path itself (links not followed). For reports and tests: the watcher uses
    descriptors."""
    buf = ctypes.create_string_buffer(256)
    n = _xlibc().getxattr(path.encode(), name.encode(), buf, 256, 0, XATTR_NOFOLLOW)
    return None if n < 0 else buf.raw[:n]


def set_xattr(path, name, value):
    if _xlibc().setxattr(path.encode(), name.encode(), value, len(value), 0, XATTR_NOFOLLOW) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e), path)


def fget_xattr(fd, name):
    buf = ctypes.create_string_buffer(256)
    n = _xlibc().fgetxattr(fd, name.encode(), buf, 256, 0, 0)
    return None if n < 0 else buf.raw[:n]


def fsetxattr_fd(fd, name, value):
    if _xlibc().fsetxattr(fd, name.encode(), value, len(value), 0, 0) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def quarantine_value():
    return f"0081;{int(time.time()):08x};metacomputer;".encode()


def ensure_quarantine_fd(fd):
    """The open file or folder carries our quarantine value, exactly; True when it had to be set. A file the
    VM made unwritable gets its owner's write bit back first (the owner may always change the mode)."""
    v = fget_xattr(fd, QUARANTINE)
    if v is not None and QUAR_OK.match(v):
        return False
    try:
        fsetxattr_fd(fd, QUARANTINE, quarantine_value())
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EPERM):
            raise ProtectionFailure(f"couldn't set the quarantine attribute ({e.strerror})")
        st = os.fstat(fd)
        os.fchmod(fd, stat.S_IMODE(st.st_mode) | (0o700 if stat.S_ISDIR(st.st_mode) else 0o600))
        try:
            fsetxattr_fd(fd, QUARANTINE, quarantine_value())
        except OSError as e2:
            raise ProtectionFailure(f"couldn't set the quarantine attribute ({e2.strerror})")
    return True


def ensure_quarantine(path):
    """By path, for a file this program made (a note); never through a folder the VM controls."""
    fd = os.open(path, FILE_NOFOLLOW)
    try:
        return ensure_quarantine_fd(fd)
    finally:
        os.close(fd)


def _sig(st):
    return (stat.S_IFMT(st.st_mode), st.st_mode, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _stat_at(dfd, name):
    """lstat relative to a folder descriptor; a folder the VM left without search permission gets it back."""
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except PermissionError:
        os.fchmod(dfd, stat.S_IMODE(os.fstat(dfd).st_mode) | 0o700)
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)


def _open_at(dfd, name, st, flags):
    """Open name in dfd without following links; None when it's no longer the entry st describes (it changed
    since it was listed: the next pass sees it). An entry the VM made unreadable gets its owner's bits back."""
    try:
        fd = os.open(name, flags, dir_fd=dfd)
    except PermissionError:
        os.chmod(name, stat.S_IMODE(st.st_mode) | (0o700 if flags & os.O_DIRECTORY else 0o600), dir_fd=dfd,
                 follow_symlinks=False)
        fd = os.open(name, flags, dir_fd=dfd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
            return None
        raise
    fst = os.fstat(fd)
    if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        return None
    return fd


def link_escapes(rel, target, rootcanon=None):
    """Whether a link at rel (inside the folder) with this target could lead out of it, from the text alone (no
    lookups, so nothing the VM changes meanwhile matters). Only one shape is kept: a relative target whose ".."
    steps all come first, no more of them than the link's own depth, then names going down. Why that holds for
    chains of links: the kernel resolves ".." from the real folder it has reached, and the link sits in a real
    folder at that depth, so its leading ".." steps stay inside; after them it only goes down, and each link met
    on the way down is held to the same rule from its own real folder. Anything else goes: an absolute target
    (the Mac's paths aren't the VM's; "ROOT/../.." names a way out), and a ".." after a name (with a -> ".",
    "a/../outside" climbs out of the folder however each link looks on its own)."""
    if not target or target.startswith("/") or "\0" in target:
        return True
    depth = rel.count("/")                      # folders between the share's root and the link
    ups, down = 0, False
    for c in target.split("/"):
        if c in ("", "."):
            continue
        if c == "..":
            if down:
                return True
            ups += 1
        else:
            down = True
    return ups > depth


def _note_at(dfd, name, target):
    shown = esc(target)[:500]
    try:
        fd = os.open(name + ".link-removed.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o644, dir_fd=dfd)
    except OSError:
        return
    try:
        os.write(fd, ("metacomputer removed a symbolic link the VM made here: it pointed outside this shared folder,\n"
                      f"to {shown!r}, where a program on the Mac following it would have left the folder.\n").encode())
        try:
            fsetxattr_fd(fd, QUARANTINE, quarantine_value())
        except OSError:
            pass
    finally:
        os.close(fd)


def open_root(path, ident=None):
    """The folder's descriptor, checked to be the folder that was checked (device and inode)."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    st = os.fstat(fd)
    if ident is not None and [st.st_dev, st.st_ino] != list(ident):
        os.close(fd)
        raise ProtectionFailure(f"{path} is no longer the folder that was checked")
    return fd


def walk_fd(rootfd, visit, limit=None, deadline=None):
    """Every entry under rootfd, through descriptors: visit(dirfd, name, rel, st) is called for each, and
    returns a folder descriptor to enter (which walk_fd closes) or None. Entries are counted as the folder is
    listed, so a folder with millions of entries fails at the limit instead of being read whole first. Over
    limit entries, MAX_DEPTH levels, or past deadline (time.monotonic()) is a ProtectionFailure."""
    limit = limit or watch_limit()
    count = [0]

    def check():
        if deadline is not None and time.monotonic() > deadline:
            raise ProtectionFailure("a pass over the folder ran past its time limit")
        beat()

    def listing(dfd):
        try:
            it = os.scandir(dfd)
        except PermissionError:
            os.fchmod(dfd, stat.S_IMODE(os.fstat(dfd).st_mode) | 0o700)
            it = os.scandir(dfd)
        names = []
        with it:
            for e in it:
                count[0] += 1
                if count[0] > limit:
                    raise ProtectionFailure(f"over {limit} files and folders, more than the watcher covers")
                check()
                names.append(e.name)
        return names

    def walk(dfd, rel, depth):
        if depth > MAX_DEPTH:
            raise ProtectionFailure(f"folders nested more than {MAX_DEPTH} deep")
        for n in listing(dfd):
            check()
            r = f"{rel}/{n}" if rel else n
            try:
                st = _stat_at(dfd, n)
            except FileNotFoundError:
                continue
            cfd = visit(dfd, n, r, st)
            if cfd is not None:
                try:
                    walk(cfd, r, depth + 1)
                finally:
                    os.close(cfd)
    walk(rootfd, "", 0)


def snapshot(path, ident=None):
    """What a read-write folder holds before the VM boots: {relative path: signature}."""
    fd = open_root(path, ident)
    out = {}
    try:
        def visit(dfd, n, r, st):
            out[r] = _sig(st)
            return _open_at(dfd, n, st, DIR_NOFOLLOW) if stat.S_ISDIR(st.st_mode) else None
        walk_fd(fd, visit)
    finally:
        os.close(fd)
    return out


def walk_sigs(root, limit=None):
    """snapshot() without the identity check; None past the limit (kept for callers that only report)."""
    try:
        return snapshot(root) if limit is None else _snapshot_limited(root, limit)
    except ProtectionFailure:
        return None


def _snapshot_limited(root, limit):
    fd = open_root(root)
    out = {}
    try:
        def visit(dfd, n, r, st):
            out[r] = _sig(st)
            return _open_at(dfd, n, st, DIR_NOFOLLOW) if stat.S_ISDIR(st.st_mode) else None
        walk_fd(fd, visit, limit)
    finally:
        os.close(fd)
    return out


def protect_fd(rootfd, rootcanon, prev, ours, stats, deadline=None):
    """One pass over a read-write folder, every action relative to a folder descriptor opened without
    following links, so a folder the VM swaps for a link mid-pass leads nowhere. Entries unchanged since the
    VM booted are left alone; everything else gets the quarantine attribute, verified through its own
    descriptor on every pass (so one the VM strips right after it was set is put back next time); links out
    of the folder become notes; FIFOs, sockets and devices go; set-id bits go. Returns (picture, links,
    special); raises ProtectionFailure when the pass can't cover the folder."""
    if free_bytes_at(rootfd) < reserve_bytes():
        raise ProtectionFailure(f"the Mac's disk has less than {reserve_bytes() >> 30} GB free (IMPORT_MIN_FREE_GB); "
                                "a read-write folder could fill it")
    cur, links, special = {}, [], []

    def quarantine(fd, r):
        if ensure_quarantine_fd(fd):
            stats["restored" if r in ours else "quarantined"] += 1
        ours.add(r)

    def visit(dfd, n, r, st):
        sig = _sig(st)
        untouched = r not in ours and prev.get(r) == sig
        t = stat.S_IFMT(st.st_mode)
        if t == stat.S_IFLNK:
            if not untouched:
                target = os.readlink(n, dir_fd=dfd)
                if link_escapes(r, target, rootcanon):
                    os.unlink(n, dir_fd=dfd)
                    _note_at(dfd, n, target)
                    links.append((r, target))
                    stats["links_removed"] += 1
                    return None
            cur[r] = sig
            return None
        if t == stat.S_IFDIR:
            cfd = _open_at(dfd, n, st, DIR_NOFOLLOW)
            if cfd is None:
                return None
            if not untouched:
                quarantine(cfd, r)
                cur[r] = _sig(os.fstat(cfd))
            else:
                cur[r] = sig
            return cfd
        if t == stat.S_IFREG:
            if untouched:
                cur[r] = sig
                return None
            ffd = _open_at(dfd, n, st, FILE_NOFOLLOW)
            if ffd is None:
                return None
            try:
                fst = os.fstat(ffd)
                if fst.st_mode & (stat.S_ISUID | stat.S_ISGID):
                    os.fchmod(ffd, stat.S_IMODE(fst.st_mode) & ~(stat.S_ISUID | stat.S_ISGID))
                quarantine(ffd, r)
                cur[r] = _sig(os.fstat(ffd))
            finally:
                os.close(ffd)
            return None
        os.unlink(n, dir_fd=dfd)                     # FIFO, socket, device
        special.append(r)
        stats["special_removed"] += 1
        return None
    walk_fd(rootfd, visit, deadline=deadline)
    return cur, links, special


def protect_path(root, prev, ours, stats, ident=None):
    """protect_fd on a folder given by path (opened and checked once)."""
    fd = open_root(root, ident)
    try:
        return protect_fd(fd, canon(root, directory=True)[0], prev, ours, stats)
    finally:
        os.close(fd)


protect = protect_path


def watcher_pid_path():
    return os.path.join(run_dir(), "storage-watcher.pid")


def watch_stats_path():
    return os.path.join(run_dir(), "storage-watch.json")


def lume_pid():
    try:
        return int(open(os.path.join(run_dir(), "lume.pid")).read().strip())
    except (OSError, ValueError):
        return 0


def _proc_start_or_none(pid):
    """proc_start(), or None when ps itself couldn't run."""
    try:
        return proc_start(pid)
    except (OSError, subprocess.SubprocessError):
        return None


def stop_vm(vm_pid, vm_start, grace=10.0):
    """Power the VM off from the Mac: end its `lume run` process (Virtualization.framework runs in it) with
    SIGTERM, then SIGKILL after grace seconds, if it's still that process. Never asks the guest anything. When
    ps can't run at all, the pid is ended anyway: protection that can't check fails closed."""
    import signal
    if not vm_pid or not vm_start:
        return False
    cur = _proc_start_or_none(vm_pid)
    if cur is not None and cur != vm_start:
        return False
    try:
        os.kill(vm_pid, signal.SIGTERM)
    except OSError:
        return False
    end = time.monotonic() + grace
    while time.monotonic() < end:
        cur = _proc_start_or_none(vm_pid)
        if cur is not None and cur != vm_start:
            return True
        time.sleep(0.2)
    try:
        os.kill(vm_pid, signal.SIGKILL)
    except OSError:
        pass
    return True


def fail_closed(reason, vm_pid, vm_start, stats, folders, origin="watcher"):
    """Protection failed: the VM is stopped first, from the Mac, before anything that could itself fail (a
    full disk can make writing the stats raise); then, each on its own and best effort, the stats, a line in
    the log and the event."""
    try:
        stop_vm(vm_pid, vm_start)
    except BaseException:  # noqa: BLE001 - nothing may keep the reports from running
        pass
    for report in (lambda: atomic_json(watch_stats_path(), dict(stats, failed=reason), mode=0o644),
                   lambda: print(f"metacomputer: storage protection failed: {esc(reason)}; the VM was stopped",
                                 file=sys.stderr, flush=True),
                   lambda: emit("security", "storage_protection_failed",
                                f"Protection of a read-write folder failed ({reason[:200]}); the VM {vm_name()} was "
                                "stopped", "error", data={"reason": reason[:300], "folders": list(folders)},
                                action="metacomputer stop, then look at the folder (and storage-watcher.log) before "
                                "starting again", origin=origin)):
        try:
            report()
        except BaseException:  # noqa: BLE001
            pass


def supervise(rfd, vm_pid, vm_start, root_fds):
    """The watcher's supervisor: a child process forked before the watcher's first pass, apart from it. It
    stops the VM from the Mac when the watcher ends without saying it finished (killed, crashed), shows no
    progress for STALL_MAX seconds (stuck in a pass), or when a read-write folder's disk drops below the
    reserve (checked every SUPERVISE_EVERY seconds, whatever the scans are doing). Returns the reason it
    stopped the VM, or None."""
    import select
    last, done, reserve, stall = time.monotonic(), None, reserve_bytes(), stall_limit()
    while True:
        ready, _, _ = select.select([rfd], [], [], SUPERVISE_EVERY)
        if ready:
            data = os.read(rfd, 4096)
            if not data:
                if done is None:
                    stop_vm(vm_pid, vm_start)
                    return "the watcher ended without finishing"
                if done == "F":
                    stop_vm(vm_pid, vm_start)      # it stopped the VM and reported; once more, to be sure
                return None
            if b"Q" in data:
                done = "Q"
            elif b"F" in data:
                done = "F"
            last = time.monotonic()
        if done:
            continue
        for fd in root_fds:
            try:
                free = free_bytes_at(fd)
            except OSError:
                continue
            if free < reserve:
                return (f"the Mac's disk has less than {reserve >> 30} GB free (IMPORT_MIN_FREE_GB); a read-write "
                        "folder could fill it")
        if time.monotonic() - last > stall:
            return f"the watcher showed no progress for {stall:.0f} s"


def start_supervisor(vm_pid, vm_start, root_fds, folders, stats):
    """Fork the supervisor; the watcher keeps the write end of its pipe (beat())."""
    import signal
    r, w = os.pipe()
    watcher = os.getpid()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(w)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)     # it ends when the watcher's pipe closes
            why = supervise(r, vm_pid, vm_start, root_fds)
            if why:
                fail_closed(why, vm_pid, vm_start, read_json(watch_stats_path()) or stats, folders, origin="watcher")
                try:
                    os.kill(watcher, signal.SIGKILL)      # stuck, or about to fail the same way: it's reported
                except OSError:
                    pass
        finally:
            os._exit(0)
    os.close(r)
    os.set_blocking(w, False)
    _HEART["fd"], _HEART["last"] = w, 0.0
    return pid


def finish_supervisor(how):
    """Tell the supervisor how the watcher ends (b"Q": finished; b"F": failed, VM stopped, reported)."""
    fd = _HEART["fd"]
    _HEART["fd"] = None
    if fd is None:
        return
    try:
        os.set_blocking(fd, True)
        os.write(fd, how)
    except OSError:
        pass
    os.close(fd)


def cmd_watch(a):
    import signal
    st = load_state() or {}
    folders = [it for it in st.get("items", []) if it["kind"] in FOLDER_KINDS and it["rw"]]
    syncs = [it for it in st.get("items", []) if it["kind"] == "s3" and it.get("sync")]
    if not folders and not syncs:
        return 0
    with open(watcher_pid_path(), "w") as f:
        f.write(str(os.getpid()))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    vm_pid = lume_pid()
    vm_start = proc_start(vm_pid)
    known, ours, roots = {}, {}, {}
    stats = {"pid": os.getpid(), "started": now(), "vm": vm_name(), "folders": {}}
    names = [it["name"] for it in folders]
    rc = 1
    try:
        for it in folders:
            roots[it["name"]] = (open_root(it["path"], it.get("ident")), canon(it["path"], directory=True)[0])
            snap = read_json(snap_path(it["name"])) or {}
            known[it["name"]] = {k: tuple(v) for k, v in snap.items()}
            ours[it["name"]] = set()
            stats["folders"][it["name"]] = {"path": it["path"], "quarantined": 0, "restored": 0, "links_removed": 0,
                                            "special_removed": 0, "errors": 0, "last_scan": None}
        if folders:
            stats["supervisor"] = start_supervisor(vm_pid, vm_start, [fd for fd, _ in roots.values()], names, stats)
        rc = watch_loop(folders, roots, known, ours, stats, stop, vm_pid, vm_start, syncs)
        finish_supervisor(b"Q")
    except BaseException as e:  # noqa: BLE001 - protection that can't run stops the VM, first
        reason = str(e) if isinstance(e, ProtectionFailure) else f"the watcher failed: {type(e).__name__}: {e}"
        fail_closed(reason, vm_pid, vm_start, stats, names)
        finish_supervisor(b"F")
        rc = 1
    finally:
        for fd, _ in roots.values():
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(watcher_pid_path())
        except OSError:
            pass
    return rc


def cmd_kill_vm(a):
    """Power this VM off from the Mac, now (bin/metacomputer's storage_abort: a storage-security failure at
    start). Its `lume run` process, checked to be this VM's by its command line, gets SIGTERM, then SIGKILL.
    The guest is never asked anything, so a guest that stalls can't keep itself running."""
    pid = lume_pid()
    if not pid:
        return 0
    try:
        cmdline = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        cmdline = ""
    if f" run {vm_name()} " not in f" {cmdline} ":
        return 0
    stop_vm(pid, proc_start(pid), grace=a.grace)
    return 0


def watch_loop(folders, roots, known, ours, stats, stop, vm_pid, vm_start, syncs=()):
    last_write = 0
    if syncs:
        import threading

        def sync_loop():
            every = max(15, int(float(env("S3_SYNC_INTERVAL", "60") or 60)))
            while not stop["now"]:
                end = time.monotonic() + every
                while time.monotonic() < end and not stop["now"]:
                    time.sleep(0.5)
                if stop["now"]:
                    break
                for it in syncs:
                    rec = reg_load("volume", it["name"])
                    if rec:
                        s3_sync(rec, reason="interval")
        threading.Thread(target=sync_loop, daemon=True).start()
    while True:
        t0 = time.monotonic()
        deadline = t0 + pass_limit()
        final = stop["now"] or (vm_pid and proc_start(vm_pid) != vm_start)
        for it in folders:
            fs = stats["folders"][it["name"]]
            before = dict(fs)
            rootfd, rootcanon = roots[it["name"]]
            known[it["name"]], links, special = protect_fd(rootfd, rootcanon, known[it["name"]], ours[it["name"]], fs,
                                                           deadline)
            fs["last_scan"] = now()
            if links:
                emit("security", "symlink_neutralized",
                     f"The VM made {len(links)} link(s) leading out of the shared folder '{it['name']}'; removed",
                     data={"volume": it["name"], "folder": it["path"],
                           "links": [{"path": r, "target": t} for r, t in links[:10]]}, origin="watcher")
            if special:
                emit("security", "special_file_removed",
                     f"The VM made {len(special)} FIFO(s), socket(s) or device(s) in the shared folder '{it['name']}'; removed",
                     data={"volume": it["name"], "folder": it["path"], "paths": special[:10]}, origin="watcher")
            if fs["restored"] > before["restored"]:
                emit("security", "quarantine_restored",
                     f"The VM removed or changed the quarantine attribute on {fs['restored'] - before['restored']} "
                     f"item(s) in the shared folder '{it['name']}'; put back", data={"volume": it["name"],
                                                                                      "folder": it["path"]},
                     origin="watcher")
        elapsed = time.monotonic() - t0
        if final or time.monotonic() - last_write > 5:
            stats["interval_s"] = round(max(WATCH_EVERY, 5 * elapsed), 2)
            atomic_json(watch_stats_path(), stats, mode=0o644)
            last_write = time.monotonic()
        if final:
            return 0
        end = time.monotonic() + max(WATCH_EVERY, 5 * elapsed)
        while time.monotonic() < end and not stop["now"]:
            beat()
            time.sleep(0.2)


def watcher_alive():
    try:
        pid = int(open(watcher_pid_path()).read().strip())
    except (OSError, ValueError):
        return 0
    r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return pid if "storage.py watch" in r.stdout else 0


def watcher_start():
    if watcher_alive():
        return watcher_alive()
    log = os.path.join(os.environ.get("MCOMP_LOG_DIR") or os.path.join(LAB, "logs"), "storage-watcher.log")
    with open(log, "a") as out:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "watch"], stdin=subprocess.DEVNULL,
                             stdout=out, stderr=out, start_new_session=True)
    for _ in range(40):
        if watcher_alive() == p.pid:
            return p.pid
        if p.poll() is not None:
            return 0
        time.sleep(0.1)
    return p.pid if p.poll() is None else 0


def watcher_stop(timeout=30):
    """Stop the watcher after its last pass (the VM is off by now); returns its final stats."""
    pid = watcher_alive()
    if pid:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        end = time.time() + timeout
        while time.time() < end and watcher_alive() == pid:
            time.sleep(0.2)
        if watcher_alive() == pid:
            os.kill(pid, signal.SIGKILL)
    return read_json(watch_stats_path())


def watcher_status():
    st = read_json(watch_stats_path()) or {}
    pid = watcher_alive()
    return dict(st, running=bool(pid), pid=pid or st.get("pid")) if (st or pid) else None


# --- status ----------------------------------------------------------------------------------
def status():
    st = load_state() or {}
    configured = bool((env("VOLUMES") or "").strip() or (env("REFDISKS") or "").strip())
    return {"configured": configured, "volumes": env("VOLUMES"), "refdisks": env("REFDISKS"),
            "guest_features": guest_features().get("storage") == "1", "items": st.get("items", []),
            "attached": st.get("attached"), "watcher": watcher_status()}


def describe_status(host, guest, running):
    """Lines for `metacomputer status` (host: status(); guest: guest-status's "storage", or None). What the VM
    reports is untrusted: used only where it's an object with a list of objects, and only to look things up."""
    lines = []
    if not isinstance(guest, dict):
        guest = None
    elif not isinstance(guest.get("items"), list):
        guest = dict(guest, items=[])
    else:
        guest = dict(guest, items=[g for g in guest["items"] if isinstance(g, dict)
                                   and isinstance(g.get("kind"), str) and isinstance(g.get("name"), str)])
    supported = guest.get("supported") if guest is not None else host.get("guest_features")
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
        m = mounted.get((guest_kind(it["kind"]), it["name"]))
        mark = "\033[32m✓\033[0m" if m else ("\033[31m✗\033[0m" if running else "-")
        lines.append(f"  {mark} {it['kind']:<8} {it['name']:<14} {it['mount']:<28} {'rw' if it['rw'] else 'ro'}  "
                     f"{esc(it['path'])}")
    if any(it["kind"] in FOLDER_KINDS and it["rw"] for it in host["items"]):
        w = host.get("watcher") or {}
        if w.get("running"):
            tot = lambda k: sum(f.get(k, 0) for f in (w.get("folders") or {}).values())  # noqa: E731
            lines.append(f"  watcher  running: {tot('quarantined')} quarantined, {tot('links_removed')} links and "
                         f"{tot('special_removed')} special files removed (metacomputer security)")
        elif running:
            lines.append("  \033[31m✗\033[0m watcher  not running: what the VM writes isn't quarantined "
                         "(metacomputer restart)")
    return lines


def security_summary(host):
    """What `metacomputer security` shows about storage (host: status())."""
    host = host or {}
    return {"configured": bool(host.get("configured")), "volumes": host.get("volumes") or "",
            "refdisks": host.get("refdisks") or "", "guest_features": bool(host.get("guest_features")),
            "attached": [{k: it.get(k) for k in ("kind", "name", "mount", "rw", "path")}
                         for it in host.get("items") or []], "watcher": host.get("watcher")}


PROTECTION = {
    "disk": "read-write, this VM only (locked while it runs); the VM can read and change everything on it",
    "refdisk": "read-only at the hypervisor; any number of VMs",
    "folder-ro": "a Mac folder, read-only: the Mac refuses writes (checked as root at each start)",
    "folder-rw": "a Mac folder, read-write, this VM only: what it writes is quarantined, links out of the folder and "
                 "special files are removed, Spotlight is kept out (.noindex)",
}


def protection(it):
    k = it["kind"]
    extra = "; a local copy of an S3 bucket, synced on the Mac (the VM never sees the keys)" if k == "s3" else ""
    if k in FOLDER_KINDS:
        k = "folder-rw" if it.get("rw") else "folder-ro"
    return PROTECTION.get(k, "") + extra


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
        lines.append(f"  {it['kind']:<8} {it['name']:<14} at {it['mount']:<24} {protection(it)}")
    w = s.get("watcher")
    if any(it["kind"] in FOLDER_KINDS and it.get("rw") for it in s["attached"]):
        if w and w.get("running"):
            lines.append(f"  watcher  running (pid {w.get('pid')})" + "".join(
                f"; {n}: {f.get('quarantined', 0)} quarantined, {f.get('links_removed', 0)} links and "
                f"{f.get('special_removed', 0)} special files removed, quarantine restored {f.get('restored', 0)}x"
                for n, f in (w.get("folders") or {}).items()))
        else:
            lines.append("  \033[33m! the folder watcher isn't running\033[0m: nothing quarantines what the VM writes "
                         "(metacomputer restart starts it)")
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
    p.add_argument("--folder")
    p.add_argument("--s3", action="store_true")
    p.add_argument("--bucket")
    p.add_argument("--endpoint")
    p.add_argument("--region")
    p.add_argument("--prefix")
    p.set_defaults(fn=cmd_volume_create)
    p = vol.add_parser("keys")
    p.add_argument("name")
    p.set_defaults(fn=cmd_volume_keys)
    p = vol.add_parser("test")
    p.add_argument("name")
    p.set_defaults(fn=cmd_volume_test)
    p = vol.add_parser("sync")
    p.add_argument("name")
    p.add_argument("--force", action="store_true")
    p.add_argument("--resync", action="store_true")
    p.set_defaults(fn=cmd_volume_sync)
    p = vol.add_parser("list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_volume_list)
    p = vol.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--keep-image", action="store_true")
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
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    p = sub.add_parser("kill-vm")
    p.add_argument("--grace", type=float, default=10.0)
    p.set_defaults(fn=cmd_kill_vm)
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
