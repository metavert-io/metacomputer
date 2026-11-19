#!/usr/bin/env python3
"""Whether the VM may hold a value from the vault: the mark that decides what of the VM's own words may be kept.

The VM is untrusted, and it keeps every value it was ever given (a password, a one-time code, a session's
cookies, an API token). From then on it could spell one in anything it sends: an error message, a tool
description, its stderr, a size, an id. So from the moment a value may reach the VM until the VM is reset to its
baseline, nothing it chooses is recorded or kept on the Mac (SECURITY.md, "After a value reaches the VM"):

- the action log (lib/mcp_relay.py, lib/actionlog.py) records each call, answer and refusal as secret-adjacent:
  the tool, whose call, its argument names, sizes as size classes, whether it failed; no text, no message, no hash;
- the hub (lib/mcp_hub.py) withholds the connection's stderr, names exceptions by type only in hub.log, and neither
  asks the driver for its tool list again nor saves or serves one it sent;
- `metacomputer sh` (lib/vm_shell.py) records its output streams by size class only.

The mark is one file per VM's run folder (RUN_DIR/vm-may-hold-values). It's written BEFORE a value can be sent:
by the hub before a sign-in's or session load's vault call (lib/site_login.py), and by `metacomputer secrets
fill|login|session load|token` before it runs the vault (bin/metacomputer). Whatever the VM sends after the value
arrives is read after the mark exists, so every reader that looks at the mark before it records sees it. A mark
that can't be read counts as set (fail closed); one that can't be written stops the use before the vault is asked.

A copy of the VM carries the mark with it. Each saved image (the baseline, its -prev, a checkpoint) has a record,
RUN_DIR/vm-taint/IMAGE, written when it's made from the working VM (`baseline`, `checkpoint`: the working VM's
mark then) or made fresh (a build: clean). Replacing the working VM with an image (`reset`, `restore`) sets the mark
from that image's record. An image with no record (made before this) counts as holding a value if any VM of this
run folder was ever marked (RUN_DIR/vm-taint/.ever, written with every mark and never removed); a record that can't
be read counts as holding one.

  vmtaint.py status | mark [WHY] | clear      (RUN_DIR from MCOMP_RUN_DIR, as bin/metacomputer exports it)
  vmtaint.py save IMAGE | restore IMAGE | copy FROM TO | forget IMAGE | fresh BASELINE
"""
import os
import sys
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "vm-may-hold-values"
SIZES = (0, 1 << 10, 1 << 16, 1 << 20, 1 << 24, 1 << 28, 1 << 32)


def path(env=None):
    """The mark's file: MCOMP_VM_MARK when set (the hub gives its driver connection the very path it marks, so the
    action log's relay reads the same file whatever its own config says), else RUN_DIR/vm-may-hold-values."""
    env = os.environ if env is None else env
    if env.get("MCOMP_VM_MARK"):
        return env["MCOMP_VM_MARK"]
    rd = env.get("MCOMP_RUN_DIR") or os.path.join(LAB, ".run")
    return os.path.join(rd, NAME)


def tainted(env=None, mark_path=None):
    """True when the VM may hold a value (or that can't be told: an unreadable folder counts as yes)."""
    p = mark_path or path(env)
    try:
        os.lstat(p)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def mark(env=None, why="a value"):
    """Record that a value may reach the VM from now on. Raises OSError when it can't be recorded: the caller
    must then not send the value."""
    p = path(env)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _write(os.path.join(_state_dir(env), EVER), "marked\n")      # first: an image with no record counts from now
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, (time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + str(why)[:80].replace("\n", " ") + "\n").encode())
    finally:
        os.close(fd)
    if not tainted(mark_path=p):
        raise OSError("the mark didn't stay")


def clear(env=None):
    """The VM was replaced by a fresh copy of an image that holds no value (or rebuilt)."""
    try:
        os.remove(path(env))
    except FileNotFoundError:
        pass


# --- the mark travels with copies of the VM -------------------------------------------------------------------
STATE = "vm-taint"
EVER = ".ever"
IMAGE_RE = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _state_dir(env=None):
    env = os.environ if env is None else env
    rd = env.get("MCOMP_RUN_DIR") or os.path.join(LAB, ".run")
    return os.path.join(rd, STATE)


def _write(p, text):
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    tmp = p + f".{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.replace(tmp, p)


def _image_path(env, image):
    if not IMAGE_RE.fullmatch(image or ""):
        raise ValueError(f"not an image name: {image!r}")
    return os.path.join(_state_dir(env), image)


def ever(env=None):
    """Was any VM of this run folder ever marked (or can that not be told)?"""
    try:
        os.lstat(os.path.join(_state_dir(env), EVER))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def image_state(env, image):
    """'tainted' or 'clean': the image's record; with none, 'tainted' when a VM here was ever marked."""
    try:
        with open(_image_path(env, image)) as f:
            state = f.read().strip()
    except FileNotFoundError:
        return "tainted" if ever(env) else "clean"
    except (OSError, ValueError):
        return "tainted"
    return state if state in ("tainted", "clean") else "tainted"


def save_image(env, image):
    """IMAGE was just made from the working VM: it holds what the working VM holds."""
    _write(_image_path(env, image), ("tainted" if tainted(env) else "clean") + "\n")


def copy_image(env, src, dst):
    """DST was just made from SRC (the baseline kept as -prev)."""
    _write(_image_path(env, dst), image_state(env, src) + "\n")


def restore_image(env, image):
    """The working VM was just replaced by a copy of IMAGE: its mark follows that image. Returns the state."""
    state = image_state(env, image)
    if state == "tainted":
        mark(env, f"restored from {image}")
    else:
        clear(env)
    return state


def forget_image(env, image):
    try:
        os.remove(_image_path(env, image))
    except FileNotFoundError:
        pass


def fresh_image(env, baseline):
    """A build: the working VM and its baseline are new, holding no value. Other images keep their records."""
    clear(env)
    _write(_image_path(env, baseline), "clean\n")


def size_class(n):
    """A size the VM can steer, as one of eight values: the smallest of 0, 1 KiB, 64 KiB, 1 MiB, 16 MiB, 256 MiB,
    4 GiB and 1 TiB that holds it (as the vault records sizes). An exact count could spell a value a few bytes at a
    time; one of eight carries three bits at most."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 0
    for b in SIZES:
        if n <= b:
            return b
    return 1 << 40


def count_class(n):
    """A count the VM can steer, as one of a few words (as the vault records a token's requests)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    return "none" if n <= 0 else "1-9" if n < 10 else "10-99" if n < 100 else "100 or more"


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print("the VM may hold a value (until it's reset)" if tainted() else "no value has reached the VM since its reset")
        return 0
    if cmd == "mark":
        try:
            mark(why=" ".join(argv[1:]) or "a value")
        except OSError as e:
            print(f"metacomputer: couldn't record that a value may reach the VM ({e.strerror or e}); nothing was sent",
                  file=sys.stderr)
            return 4
        return 0
    if cmd == "clear":
        clear()
        return 0
    try:
        if cmd == "save" and len(argv) == 2:
            save_image(None, argv[1])
        elif cmd == "copy" and len(argv) == 3:
            copy_image(None, argv[1], argv[2])
        elif cmd == "forget" and len(argv) == 2:
            forget_image(None, argv[1])
        elif cmd == "fresh" and len(argv) == 2:
            fresh_image(None, argv[1])
        elif cmd == "restore" and len(argv) == 2:
            print(restore_image(None, argv[1]))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ValueError) as e:
        if cmd == "restore":
            try:                                 # can't tell what the image holds: the working VM counts as marked
                mark(None, f"restored from {argv[1]} (its record unreadable)")
            except OSError:
                pass
        print(f"metacomputer: couldn't record whether the VM may hold a value ({e})", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
