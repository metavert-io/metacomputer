"""Copy files from the VM to the Mac without trusting what the VM sends.

    ... | python3 lib/guest_import.py DEST [--quiet]

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
  contact sheet, a re-sync) never meet a link the VM made.

Exit 0 when the stream was read (even with items left out), 1 when nothing could
be read (no stream, not a tar stream) or the copy failed.
"""
import os
import shutil
import stat
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from termsafe import UNSAFE, escape_for_terminal  # noqa: E402

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
MAX_DEPTH = 64
MAX_NAME = 1024
KINDS = {tarfile.SYMTYPE: "symbolic link", tarfile.LNKTYPE: "hard link", tarfile.FIFOTYPE: "FIFO",
         tarfile.CHRTYPE: "character device", tarfile.BLKTYPE: "block device"}


class Report:
    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.dropped = []      # (name, why)
        self.failure = None    # the error that cut the stream short, if any

    def drop(self, name, why):
        self.dropped.append((name, why))


def parts_of(name):
    """The path components of a member name, or None if the name can't be used."""
    if not name or len(name) > MAX_NAME or name.startswith("/") or "\n" in name or "\t" in name or UNSAFE.search(name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or len(parts) > MAX_DEPTH:
        return None
    return parts


def open_dir(parent_fd, parts, create):
    """A descriptor for parent/parts..., never through a link; folders made (0755 less the
    umask) when create is true. Raises OSError when a component isn't a real folder."""
    fd = os.dup(parent_fd)
    try:
        for p in parts:
            if create:
                try:
                    os.mkdir(p, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            nfd = os.open(p, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def unpack(stream, stage_fd, report):
    """Unpack the tar stream into the staging folder: regular files and folders only."""
    with tarfile.open(fileobj=stream, mode="r|*") as tf:
        for m in tf:
            parts = parts_of(m.name)
            if parts is None:
                report.drop(m.name, "a name that isn't a plain relative path")
                continue
            if not parts:            # "." itself
                continue
            if m.isdir():
                try:
                    os.close(open_dir(stage_fd, parts, True))
                    report.dirs += 1
                except OSError as e:
                    report.drop(m.name, f"folder not made ({e.strerror})")
                continue
            if not m.isreg():
                report.drop(m.name, KINDS.get(m.type, "not a regular file"))
                continue
            try:
                dfd = open_dir(stage_fd, parts[:-1], True)
            except OSError as e:
                report.drop(m.name, f"its folder couldn't be made ({e.strerror})")
                continue
            mode = 0o755 if m.mode & 0o111 else 0o644
            ffd = None
            try:
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
                            out.write(b)
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


def merge(src_fd, dst_fd, rel, report, skip=None):
    """Move everything under src_fd into dst_fd with rename(2): a link or a file in the way
    is replaced, never followed or written through; a folder is entered only if it's a
    real folder; a folder on the Mac isn't replaced by a file."""
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
                        merge(s2, d2, rel + name + "/", report)
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
            os.rename(name, name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


def import_tar(stream, dest):
    """Unpack a tar stream from the VM into dest (made if missing). Returns a Report;
    raises tarfile.TarError or OSError when the stream can't be read or written."""
    os.makedirs(dest, exist_ok=True)
    report = Report()
    dest_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    stage = tempfile.mkdtemp(prefix=".mcomp-import-", dir=dest)     # private: 0700, a new name
    name = os.path.basename(stage)
    try:
        stage_fd = os.open(name, DIR_FLAGS, dir_fd=dest_fd)
        try:
            failure = None
            try:
                unpack(stream, stage_fd, report)
            except (tarfile.TarError, OSError, EOFError) as e:
                failure = e         # what arrived whole before the stream broke is still kept
            sweep(stage_fd, "", report)
            merge(stage_fd, dest_fd, "", report, skip=name)
            if failure is not None:
                report.failure = failure
        finally:
            os.close(stage_fd)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        os.close(dest_fd)
    return report


def main(argv):
    quiet = "--quiet" in argv
    args = [a for a in argv if a != "--quiet"]
    if len(args) != 1:
        print("usage: ... | guest_import.py DEST [--quiet]", file=sys.stderr)
        return 2
    try:
        report = import_tar(sys.stdin.buffer, args[0])
    except OSError as e:
        print(f"metacomputer: copying from the VM failed: {escape_for_terminal(str(e))}", file=sys.stderr)
        return 1
    rc = 0
    if report.failure is not None:
        rc = 1
        if isinstance(report.failure, tarfile.ReadError) and not (report.files or report.dirs):
            if not quiet:
                print("metacomputer: nothing came from the VM (no files, or not a tar stream)", file=sys.stderr)
        else:
            print(f"metacomputer: the copy from the VM stopped part-way ({escape_for_terminal(str(report.failure))}); "
                  f"{report.files} file(s) arrived whole and were kept", file=sys.stderr)
    if report.dropped:
        print(f"metacomputer: left out {len(report.dropped)} item(s) from the VM that aren't plain files or folders:",
              file=sys.stderr)
        for n, why in report.dropped[:20]:
            print(f"  {escape_for_terminal(n)} ({why})", file=sys.stderr)
        if len(report.dropped) > 20:
            print(f"  ... and {len(report.dropped) - 20} more", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
