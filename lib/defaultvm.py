#!/usr/bin/env python3
"""config/default-vm, the default computer's name, read the one way both sides read it: scripts/lib.sh
(mc_read_default runs this) and lib/labconfig.py (imports it).

The file is opened once, without following a link and without waiting (O_NOFOLLOW, O_NONBLOCK: a link is refused
and a FIFO can't hang the reader), checked through that descriptor to be a regular file, and at most 66 bytes are
read from it; exactly those bytes are checked: one VM name (scripts/lib.sh mc_valid_vm_name: letters, digits, '.',
'-', '_', not starting with '-' or '.', at most 64) on one line, a newline at its end or not, nothing else. So what
is checked is what is used, whatever replaces the file meanwhile. Standard library only, for any python3 (the
resolution's own Python may be an older one).

    defaultvm.py read [FILE]    the name on stdout (nothing when there's no file), exit 0; the refusal, exit 1
"""
import errno
import os
import re
import stat
import sys

NAME = re.compile(rb"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
MAX = 65
PLAIN = "config/default-vm (your default computer) isn't a plain file this account can read"
SHAPE = "config/default-vm (your default computer) must hold one VM name on one line, and nothing else"


class Refused(ValueError):
    pass


def read(path):
    """The name config/default-vm holds, or None when there's no file; Refused for anything else."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        raise Refused(PLAIN) from None          # a link (ELOOP), no permission, ...
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(PLAIN)
        data = b""
        while len(data) <= MAX:
            chunk = os.read(fd, MAX + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        raise Refused(PLAIN) from None
    finally:
        os.close(fd)
    body = data[:-1] if data.endswith(b"\n") else data
    if len(data) > MAX or not NAME.fullmatch(body):
        raise Refused(SHAPE)
    return body.decode("ascii")


def main(argv):
    if len(argv) not in (1, 2) or argv[0] != "read":
        print("usage: defaultvm.py read [FILE]", file=sys.stderr)
        return 2
    path = argv[1] if len(argv) == 2 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         "config", "default-vm")
    try:
        name = read(path)
    except Refused as e:
        sys.stdout.write(str(e))
        return 1
    sys.stdout.write(name or "")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
