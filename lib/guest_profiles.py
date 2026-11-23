"""Browser profile folders in the VM, made and deleted inside the VM (`metacomputer browser profiles --new|--delete`).

Runs in the guest as the desktop user, read from standard input (`python3 -I - new|check|delete KIND NAME`); standard
library only. KIND is `firefox` (kept profiles for human mode, ~/.local/state/metacomputer/firefox-profiles) or
`chrome` (the driver's page-mode profiles, ~/.local/state/cua-driver/browser-profiles).

What it keeps, since anything in the VM may have changed these folders:
- The profiles folder is reached from the home folder one name at a time without following a link (O_NOFOLLOW):
  a link anywhere on the way (`firefox-profiles` pointing at ~, say) is refused, never followed.
- A profile is only ever a direct child of that folder, by a plain name (letters, digits, `.`, `_`, `-`; not `.` or
  `..`), and must be a real folder, not a link.
- It's deleted through the profiles folder's descriptor with an rmtree that doesn't follow links inside it.
- `check` and `delete` refuse a profile a running browser uses: Firefox with `--profile DIR`, `-profile DIR` or
  `--profile=DIR`, Chrome with `--user-data-dir=DIR` or `--user-data-dir DIR`, compared as the folders they are (a
  relative DIR against that browser's own working folder, /proc/PID/cwd; one that can't be read counts as in use).

Exit status: 0 done, 2 bad usage or name, 3 no such profile, 4 a running browser uses it, 5 refused (a link or
something other than a folder on the way, or this Python can't delete without following links).
The Mac side prints its own messages from the status; nothing this prints is shown as it is.

MCOMP_GUEST_PROC names another /proc (tests only).
"""
import errno
import os
import re
import shutil
import stat
import sys

ROOTS = {"firefox": (".local", "state", "metacomputer", "firefox-profiles"),
         "chrome": (".local", "state", "cua-driver", "browser-profiles")}
NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class Refused(Exception):
    pass


def home():
    return os.environ.get("HOME") or os.path.expanduser("~")


def open_root(kind, create=False):
    """A descriptor of the profiles folder, reached from the home folder without following a link."""
    fd = os.open(home(), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for part in ROOTS[kind]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nfd = os.open(part, FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    raise Refused(f"{part} on the way to the profiles folder is a link or not a folder")
                raise
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def profile_path(kind, name):
    return os.path.join(home(), *ROOTS[kind], name)


def in_use(kind, path):
    """Does a running process of this user's use the profile folder at path?"""
    proc = os.environ.get("MCOMP_GUEST_PROC") or "/proc"
    want = os.path.realpath(path)
    flags = ("--profile", "-profile") if kind == "firefox" else ("--user-data-dir",)
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return True                                  # can't tell: refuse rather than delete a profile in use
    for pid in pids:
        try:
            if os.stat(os.path.join(proc, pid)).st_uid != os.getuid():
                continue
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                args = f.read(1 << 20).decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        cwd = None
        for i, a in enumerate(args):
            val = None
            for fl in flags:
                if a == fl and i + 1 < len(args):
                    val = args[i + 1]
                elif a.startswith(fl + "="):
                    val = a[len(fl) + 1:]
            if not val:
                continue
            if not os.path.isabs(val):                  # relative to the browser's own folder, not this one's
                if cwd is None:
                    try:
                        cwd = os.readlink(os.path.join(proc, pid, "cwd"))
                    except OSError:
                        return True                      # can't tell where it points: refuse
                val = os.path.join(cwd, val)
            if val == path or os.path.realpath(val) == want:
                return True
    return False


def _help_shaped(s):
    """A request for usage, or a word shaped like one (lib/strictargs.py's rule; this runs in the VM, on its own)."""
    return s in ("-h", "--help", "--h", "--he", "--hel") or s.startswith("--help=") or (s.startswith("-h") and not s.startswith("--"))


def main(argv):
    if argv in (["-h"], ["--help"]):
        print("usage: guest_profiles.py new|check|delete firefox|chrome NAME")
        return 0
    if len(argv) != 3 or argv[0] not in ("new", "check", "delete") or argv[1] not in ROOTS or _help_shaped(argv[2]) \
            or argv[2].startswith("-"):
        print("usage: guest_profiles.py new|check|delete firefox|chrome NAME", file=sys.stderr)
        return 2
    import json
    if os.environ.get("MCOMP_PARSE_ONLY") == "1":   # tests/test_help.py's valid forms: taken, nothing done
        print("parse-only: guest_profiles.py: taken " + json.dumps({"flags": [], "values": [], "words": 3, "max_words": 3}))
        sys.exit(0)
    action, kind, name = argv
    if not NAME_RE.match(name) or name in (".", ".."):
        print("bad profile name", file=sys.stderr)
        return 2
    try:
        root = open_root(kind, create=action == "new")
    except FileNotFoundError:
        return 3
    except Refused as e:
        print(e, file=sys.stderr)
        return 5
    try:
        if action == "new":
            try:
                os.mkdir(name, 0o700, dir_fd=root)
            except FileExistsError:
                pass
            st = os.stat(name, dir_fd=root, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                print("the profile's name is taken by something that isn't a folder", file=sys.stderr)
                return 5
            return 0
        try:
            st = os.stat(name, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            return 3
        if not stat.S_ISDIR(st.st_mode):
            print("the profile is a link or not a folder", file=sys.stderr)
            return 5
        if in_use(kind, profile_path(kind, name)):
            return 4
        if action == "check":
            return 0
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            print("this Python can't delete a folder without following links in it", file=sys.stderr)
            return 5
        shutil.rmtree(name, dir_fd=root)
        return 0
    finally:
        os.close(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
