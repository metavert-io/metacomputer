"""Copy files from the VM to the Mac without trusting what the VM sends.

    ... | python3 lib/guest_import.py DEST [--quiet] [--keep-existing PREFIX]

reads a tar stream (made in the VM: `metacomputer get`, `task finish`,
`recordings --sync`, the test harness) on stdin and puts its plain files and
folders under DEST. The VM is untrusted, so:

- the stream is unpacked into a fresh private folder (0700) inside DEST, by this
  program rather than tar, and only regular files and folders are created there:
  symbolic and hard links, FIFOs, devices and sockets are left out, and so are
  names that are absolute, hold `..`, or hold control or bidi characters;
- anything in that folder that isn't a regular file or a folder (there should be
  none) is deleted before it's moved;
- each file and folder is then moved into DEST with rename(2), which replaces a
  link already there instead of writing through it, and no folder in DEST is
  entered through a link; a folder on the Mac is never replaced by a file;
- what was left out is reported on stderr (escaped), so later writers (the
  contact sheet, a re-sync) never meet a link the VM made;
- with `--keep-existing PREFIX`, a file under PREFIX (a path relative to DEST)
  that the Mac already has as a regular file is kept, not replaced: evidence
  copied earlier (`task finish` keeps the recording turns copied while the task
  ran) can't be swapped by the VM later, and copies that now differ in the VM
  are counted and named;
- the copy stops, keeping the files that arrived whole, when the files pass
  MCOMP_IMPORT_MAX_BYTES (bin/metacomputer passes VM_DISK_BYTES: a VM can't hold
  more real files than its disk), when the Mac's free space at DEST would fall
  below MCOMP_IMPORT_MIN_FREE_GB (IMPORT_MIN_FREE_GB, default 10), after
  MAX_MEMBERS entries, or after MAX_OBJECTS files and folders made (folders a
  path implies count too), so a VM can't fill the Mac's disk or memory;
- the stream is read as plain tar only (the VM's `tar -cf -`; nothing is
  decompressed), at most max_bytes plus room for headers in all, and tar's
  extension headers (long names, pax records) are refused past MAX_EXT_HEADER
  bytes each or MAX_EXT_CHAIN in a row, before Python's tarfile reads them into
  memory; GNU sparse entries (never made by `tar -cf`) are refused.

Exit 0 when the stream was read (even with items left out), 1 when nothing could
be read (no stream, not a tar stream) or the copy failed.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from termsafe import UNSAFE, escape_for_terminal  # noqa: E402

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
MAX_DEPTH = 64
MAX_NAME = 1024
MAX_MEMBERS = 200_000            # tar entries, including ones left out
MAX_OBJECTS = 200_000            # files and folders made on the Mac, including folders a path implies
MAX_DROPPED_KEPT = 1000          # left-out names kept for the report (the count is exact)
MAX_EXT_HEADER = 64 << 10        # one GNU long name / pax header (a name is used only up to MAX_NAME)
MAX_EXT_CHAIN = 8                # extension headers in a row before an entry
MAX_EXT_TOTAL = 64 << 20         # every extension header in a stream together (the metadata budget)
OBJECT_COST = 4096               # what a file or folder costs the disk before any data, for the free-space check
FREE_CHECK_EVERY = 64 << 20      # bytes written (objects at OBJECT_COST) between free-space checks
STREAM_SLACK = 64 << 20          # with max_bytes: what headers and padding may add to the stream
GIB = 1 << 30


class ImportLimit(Exception):
    """The copy passed one of the limits; what arrived whole is kept."""


EXT_TYPES = (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE,
             tarfile.SOLARIS_XHDTYPE)


class _TarInfo(tarfile.TarInfo):
    """tarfile reads a long-name or pax header's whole payload into memory, and follows a chain of them by
    recursion, before it yields the entry they belong to; GNU sparse headers (old style, or pax's GNU.sparse.*
    records, whose map is read from the stream into a list) can grow without end. So sizes are checked here
    first, sparse maps are refused before their parsers run, and every extension header counts against one
    budget for the stream (ImportLimit)."""

    def _proc_member(self, tf):
        if not isinstance(self.size, int) or self.size < 0:
            # base-256 sizes can be negative, and some tarfile versions (3.9) read such a payload as empty
            # without moving on, while it would subtract from the budgets below: refused on every version
            raise ImportLimit(f"a tar header with a negative size ({self.size})")
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise ImportLimit("a GNU sparse entry (`tar -cf` makes none)")
        if self.type not in EXT_TYPES:
            tf._mcomp_chain = 0
            return super()._proc_member(tf)
        if self.size > MAX_EXT_HEADER:
            raise ImportLimit(f"a tar extension header of {self.size:,} bytes (at most {MAX_EXT_HEADER:,})")
        chain = getattr(tf, "_mcomp_chain", 0) + 1
        if chain > MAX_EXT_CHAIN:
            raise ImportLimit(f"more than {MAX_EXT_CHAIN} tar extension headers in a row")
        tf._mcomp_ext = getattr(tf, "_mcomp_ext", 0) + self.size
        if tf._mcomp_ext > MAX_EXT_TOTAL:
            raise ImportLimit(f"more than {MAX_EXT_TOTAL >> 20} MB of tar extension headers (the metadata budget)")
        if self.type == tarfile.XGLTYPE:    # a global pax header stays for the rest of the stream: capped in all
            tf._mcomp_global = getattr(tf, "_mcomp_global", 0) + self.size
            if tf._mcomp_global > MAX_EXT_HEADER:
                raise ImportLimit(f"more than {MAX_EXT_HEADER:,} bytes of global pax headers")
        tf._mcomp_chain = chain
        return super()._proc_member(tf)

    # pax's GNU sparse formats (0.0, 0.1, 1.0): tarfile calls these after reading the next header; 1.0 then
    # reads a map of any length from the stream. Refused before any of them runs.
    def _proc_gnusparse_00(self, *_a, **_k):
        raise ImportLimit("a pax GNU sparse entry (`tar -cf` makes none)")

    _proc_gnusparse_01 = _proc_gnusparse_00
    _proc_gnusparse_10 = _proc_gnusparse_00


def _open_stream(stream):
    """The tar stream, read as plain tar, with no TarInfo kept per member (`stream=True` where tarfile has it;
    unpack() also empties the member list as it goes)."""
    try:
        return tarfile.open(fileobj=stream, mode="r|", tarinfo=_TarInfo, stream=True)
    except TypeError:                     # an older tarfile without the stream argument
        return tarfile.open(fileobj=stream, mode="r|", tarinfo=_TarInfo)


class _Capped:
    """The stream from the VM, read with a total limit (None = none)."""

    def __init__(self, stream, limit):
        self.stream, self.limit, self.read_bytes = stream, limit, 0

    def read(self, n=-1):
        b = self.stream.read(n)
        self.read_bytes += len(b)
        if self.limit is not None and self.read_bytes > self.limit:
            raise ImportLimit(f"the stream passed {self.limit / GIB:.1f} GiB")
        return b


def limits_from_env(env=None):
    """(max_bytes or None, min_free_bytes) from MCOMP_IMPORT_MAX_BYTES and MCOMP_IMPORT_MIN_FREE_GB."""
    env = os.environ if env is None else env
    def num(name, default):
        v = (env.get(name) or "").strip()
        if not v:
            return default
        try:
            n = float(v)
        except ValueError:
            return default
        return n if n >= 0 else default
    mx = num("MCOMP_IMPORT_MAX_BYTES", 0)
    return (int(mx) if mx > 0 else None), int(num("MCOMP_IMPORT_MIN_FREE_GB", 10) * GIB)
KINDS = {tarfile.SYMTYPE: "symbolic link", tarfile.LNKTYPE: "hard link", tarfile.FIFOTYPE: "FIFO",
         tarfile.CHRTYPE: "character device", tarfile.BLKTYPE: "block device"}


class Report:
    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.dropped = []      # (name, why), the first MAX_DROPPED_KEPT
        self.dropped_count = 0
        self.bytes = 0         # file bytes written
        self.objects = 0       # files and folders made, including folders a path implied
        self.failure = None    # the error that cut the stream short, if any
        self.kept = 0          # files under --keep-existing that the Mac already had: kept
        self.kept_changed = [] # ...of those, the first MAX_DROPPED_KEPT whose copy in the VM differs now
        self.kept_changed_count = 0

    def drop(self, name, why):
        self.dropped_count += 1
        if len(self.dropped) < MAX_DROPPED_KEPT:
            self.dropped.append((name, why))


def parts_of(name):
    """The path components of a member name, or None if the name can't be used."""
    if not name or len(name) > MAX_NAME or name.startswith("/") or "\n" in name or "\t" in name or UNSAFE.search(name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or len(parts) > MAX_DEPTH:
        return None
    return parts


def open_dir(parent_fd, parts, create, made=None):
    """A descriptor for parent/parts..., never through a link; folders made (0755 less the
    umask) when create is true, each one counted by made() (which may raise ImportLimit).
    Raises OSError when a component isn't a real folder."""
    fd = os.dup(parent_fd)
    try:
        for p in parts:
            if create:
                try:
                    os.mkdir(p, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    if made is not None:
                        try:
                            made()
                        except BaseException:      # past a limit: the folder just made goes again
                            try:
                                os.rmdir(p, dir_fd=fd)
                            except OSError:
                                pass
                            raise
            nfd = os.open(p, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def free_bytes(fd):
    st = os.fstatvfs(fd)
    return st.f_bavail * st.f_frsize


def unpack(stream, stage_fd, report, max_bytes=None, min_free=0):
    """Unpack the tar stream into the staging folder: regular files and folders only.
    Raises ImportLimit when the stream passes max_bytes, MAX_MEMBERS or MAX_OBJECTS, or
    when the free space would fall below min_free."""
    members = 0
    budget = {"since_check": FREE_CHECK_EVERY}       # check before the first byte or folder

    def spend(n):
        """n more bytes of the Mac's disk (written, or OBJECT_COST for a new file or folder)."""
        budget["since_check"] += n
        if budget["since_check"] >= FREE_CHECK_EVERY:
            budget["since_check"] = 0
            if free_bytes(stage_fd) - FREE_CHECK_EVERY < min_free:
                raise ImportLimit(f"the Mac would have less than {min_free / GIB:.0f} GiB free (IMPORT_MIN_FREE_GB)")

    def made():
        report.objects += 1
        if report.objects > MAX_OBJECTS:
            raise ImportLimit(f"more than {MAX_OBJECTS:,} files and folders")
        spend(OBJECT_COST)

    with _open_stream(stream) as tf:
        for m in tf:
            del tf.members[:]            # nothing per member stays in memory (a stream reads each once)
            members += 1
            if members > MAX_MEMBERS:
                raise ImportLimit(f"more than {MAX_MEMBERS:,} entries")
            parts = parts_of(m.name)
            if parts is None:
                report.drop(m.name, "a name that isn't a plain relative path")
                continue
            if not parts:            # "." itself
                continue
            if m.isdir():
                try:
                    os.close(open_dir(stage_fd, parts, True, made))
                    report.dirs += 1
                except OSError as e:
                    report.drop(m.name, f"folder not made ({e.strerror})")
                continue
            if not m.isreg():
                report.drop(m.name, KINDS.get(m.type, "not a regular file"))
                continue
            try:
                dfd = open_dir(stage_fd, parts[:-1], True, made)
            except OSError as e:
                report.drop(m.name, f"its folder couldn't be made ({e.strerror})")
                continue
            mode = 0o755 if m.mode & 0o111 else 0o644
            ffd = None
            try:
                made()                   # before the file exists: a limit leaves no empty file behind
                try:
                    ffd = os.open(parts[-1], WRITE_FLAGS, mode, dir_fd=dfd)
                except FileExistsError:
                    if stat.S_ISDIR(os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False).st_mode):
                        report.drop(m.name, "a folder of the same name came first")
                        continue
                    os.unlink(parts[-1], dir_fd=dfd)      # named twice in the stream: the later copy wins, as with tar
                    ffd = os.open(parts[-1], WRITE_FLAGS, mode, dir_fd=dfd)
                done = False
                try:
                    src = tf.extractfile(m)
                    with os.fdopen(ffd, "wb", closefd=False) as out:
                        while True:
                            b = src.read(1 << 20)
                            if not b:
                                break
                            if max_bytes is not None and report.bytes + len(b) > max_bytes:
                                raise ImportLimit(f"more than {max_bytes / GIB:.1f} GiB, the most a VM's disk holds")
                            spend(len(b))
                            out.write(b)
                            report.bytes += len(b)
                    try:
                        os.utime(ffd, (m.mtime, m.mtime))
                    except (OSError, OverflowError, ValueError, TypeError):
                        pass
                    done = True
                    report.files += 1
                finally:
                    if not done:       # cut off part-way: no half file is kept
                        try:
                            os.unlink(parts[-1], dir_fd=dfd)
                        except OSError:
                            pass
            finally:
                if ffd is not None:
                    os.close(ffd)
                os.close(dfd)


def sweep(fd, rel, report):
    """Delete anything under fd that isn't a regular file (with one link) or a folder."""
    for name in os.listdir(fd):
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            sub = os.open(name, DIR_FLAGS, dir_fd=fd)
            try:
                sweep(sub, rel + name + "/", report)
            finally:
                os.close(sub)
        elif not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.unlink(name, dir_fd=fd)
            report.drop(rel + name, "not a regular file")


def same_content(src_fd, dst_fd, name):
    """Do the staged file and the Mac's file of this name hold the same bytes? (Neither through a link.)"""
    a = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
    try:
        b = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dst_fd)
    except OSError:
        os.close(a)
        return False
    try:
        if os.fstat(a).st_size != os.fstat(b).st_size:
            return False
        while True:
            x, y = os.read(a, 1 << 20), os.read(b, 1 << 20)
            if x != y:
                return False
            if not x:
                return True
    finally:
        os.close(a)
        os.close(b)


def merge(src_fd, dst_fd, rel, report, skip=None, keep=None):
    """Move everything under src_fd into dst_fd with rename(2): a link or a file in the way
    is replaced, never followed or written through; a folder is entered only if it's a
    real folder; a folder on the Mac isn't replaced by a file. A file whose path starts
    with keep (relative to DEST) that the Mac already has as a regular file stays as it is."""
    for name in sorted(os.listdir(src_fd)):
        if name == skip:
            continue
        st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        try:
            dt = os.stat(name, dir_fd=dst_fd, follow_symlinks=False)
        except FileNotFoundError:
            dt = None
        if stat.S_ISDIR(st.st_mode):
            if dt is not None and stat.S_ISDIR(dt.st_mode):
                s2 = os.open(name, DIR_FLAGS, dir_fd=src_fd)
                try:
                    d2 = os.open(name, DIR_FLAGS, dir_fd=dst_fd)
                    try:
                        merge(s2, d2, rel + name + "/", report, keep=keep)
                    finally:
                        os.close(d2)
                finally:
                    os.close(s2)
                continue
            if dt is not None:
                os.unlink(name, dir_fd=dst_fd)     # a file or link where the VM has a folder: the entry goes, never its target
            os.rename(name, name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        else:
            if dt is not None and stat.S_ISDIR(dt.st_mode):
                report.drop(rel + name, "a folder of that name is already on the Mac")
                continue
            if keep and dt is not None and stat.S_ISREG(dt.st_mode) and (rel + name).startswith(keep):
                report.kept += 1
                if not same_content(src_fd, dst_fd, name):
                    report.kept_changed_count += 1
                    if len(report.kept_changed) < MAX_DROPPED_KEPT:
                        report.kept_changed.append(rel + name)
                continue                 # the staged copy goes with the staging folder
            os.rename(name, name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


def import_tar(stream, dest, max_bytes=None, min_free=0, keep=None):
    """Unpack a tar stream from the VM into dest (made if missing). Returns a Report;
    raises tarfile.TarError or OSError when the stream can't be read or written.
    A limit (ImportLimit) ends the copy like a broken stream: report.failure.
    keep: a path prefix (relative to dest) whose files already on the Mac are kept (merge)."""
    os.makedirs(dest, exist_ok=True)
    report = Report()
    dest_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    stage = tempfile.mkdtemp(prefix=".mcomp-import-", dir=dest)     # private: 0700, a new name
    name = os.path.basename(stage)
    # Everything read counts, including entries left out (tar skips over their data by reading it).
    capped = _Capped(stream, max_bytes + MAX_MEMBERS * 2560 + STREAM_SLACK if max_bytes is not None else None)
    try:
        stage_fd = os.open(name, DIR_FLAGS, dir_fd=dest_fd)
        try:
            failure = None
            try:
                unpack(capped, stage_fd, report, max_bytes, min_free)
            except (tarfile.TarError, OSError, EOFError, ImportLimit, ValueError, RecursionError) as e:
                failure = e         # what arrived whole before the stream broke is still kept
            sweep(stage_fd, "", report)
            merge(stage_fd, dest_fd, "", report, skip=name, keep=keep)
            if failure is not None:
                report.failure = failure
        finally:
            os.close(stage_fd)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        os.close(dest_fd)
    return report


def limits_from_config(config=None):
    """The same limits bin/metacomputer passes to `get`, from the settings files (VM_DISK_BYTES,
    IMPORT_MIN_FREE_GB), for Python callers (the GUI test harness); MCOMP_IMPORT_* still win."""
    if config is None:
        import labconfig
        config = labconfig.load()
    env = {"MCOMP_IMPORT_MAX_BYTES": os.environ.get("MCOMP_IMPORT_MAX_BYTES") or config.get("VM_DISK_BYTES") or "",
           "MCOMP_IMPORT_MIN_FREE_GB": os.environ.get("MCOMP_IMPORT_MIN_FREE_GB") or config.get("IMPORT_MIN_FREE_GB") or ""}
    return limits_from_env(env)


def import_from_command(cmd, dest, max_bytes=None, min_free=0, timeout=600, env=None):
    """Run cmd (an SSH command that writes a tar stream from the VM) and unpack its output into dest as it
    arrives, never holding the stream in memory. Its stderr is kept to a short tail (wirelimits.StderrTail).
    The command is stopped and reaped when a limit is reached, the stream ends or breaks, or after timeout
    seconds. Returns (Report, returncode, stderr tail); raises OSError when the command can't start."""
    import wirelimits
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    tail = wirelimits.StderrTail(proc.stderr)
    timer = threading.Timer(timeout, proc.kill)
    timer.daemon = True
    timer.start()
    report = None
    try:
        report = import_tar(proc.stdout, dest, max_bytes, min_free)
    finally:
        timer.cancel()
        whole = report is not None and report.failure is None
        try:
            rc = proc.wait(timeout=5 if whole else 0.05)
        except subprocess.TimeoutExpired:
            proc.kill()                  # a limit or a broken stream: the VM doesn't get to send the rest
            rc = proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass
    return report, rc, tail.text(wait=2.0)


def main(argv):
    quiet = "--quiet" in argv
    args = [a for a in argv if a != "--quiet"]
    keep = None
    if "--keep-existing" in args:
        i = args.index("--keep-existing")
        keep = args[i + 1] if i + 1 < len(args) else ""
        del args[i:i + 2]
        if not keep or keep.startswith("/") or parts_of(keep.rstrip("/") or ".") is None:
            print("guest_import.py: --keep-existing takes a relative path prefix", file=sys.stderr)
            return 2
    if len(args) != 1:
        print("usage: ... | guest_import.py DEST [--quiet] [--keep-existing PREFIX]", file=sys.stderr)
        return 2
    max_bytes, min_free = limits_from_env()
    try:
        report = import_tar(sys.stdin.buffer, args[0], max_bytes, min_free, keep)
    except OSError as e:
        print(f"metacomputer: copying from the VM failed: {escape_for_terminal(str(e))}", file=sys.stderr)
        return 1
    rc = 0
    if report.failure is not None:
        rc = 1
        if isinstance(report.failure, ImportLimit):
            print(f"metacomputer: stopped copying from the VM: {report.failure}; "
                  f"{report.files} file(s) arrived whole and were kept", file=sys.stderr)
        elif isinstance(report.failure, tarfile.ReadError) and not (report.files or report.dirs):
            if not quiet:
                print("metacomputer: nothing came from the VM (no files, or not a tar stream)", file=sys.stderr)
        else:
            print(f"metacomputer: the copy from the VM stopped part-way ({escape_for_terminal(str(report.failure))}); "
                  f"{report.files} file(s) arrived whole and were kept", file=sys.stderr)
    if report.dropped_count:
        print(f"metacomputer: left out {report.dropped_count} item(s) from the VM that aren't plain files or folders:",
              file=sys.stderr)
        for n, why in report.dropped[:20]:
            print(f"  {escape_for_terminal(n)} ({why})", file=sys.stderr)
        if report.dropped_count > 20:
            print(f"  ... and {report.dropped_count - 20} more", file=sys.stderr)
    if report.kept_changed_count:
        # Copied earlier, kept as they were then; the VM's copies have changed since.
        print(f"metacomputer: kept {report.kept} file(s) the Mac already had; {report.kept_changed_count} of them "
              f"differ in the VM now (changed after they were copied; the Mac keeps the earlier copies):",
              file=sys.stderr)
        for n in report.kept_changed[:20]:
            print(f"  {escape_for_terminal(n)}", file=sys.stderr)
        if report.kept_changed_count > 20:
            print(f"  ... and {report.kept_changed_count - 20} more", file=sys.stderr)
    out = os.environ.get("MCOMP_IMPORT_KEEP_REPORT")
    if keep is not None and out:
        # For the caller's event (task finish): counts, and only folder names of a fixed shape, never the VM's text.
        folders = sorted({n[len(keep):].split("/")[0] for n in report.kept_changed if n.startswith(keep)})
        folders = [keep.rsplit("/", 1)[-1] + f for f in folders if f.isdigit()][:20]
        try:
            with open(out, "w") as f:
                json.dump({"kept": report.kept, "changed": report.kept_changed_count, "changed_in": folders}, f)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
