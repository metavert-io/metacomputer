#!/usr/bin/env python3
"""The live view's SSH tunnel to a computer elsewhere (bin/metacomputer view_tunnel), started in the background with
its output in a bounded, private log, and never on the caller's own output.

    tunnellog.py --log PATH [--cap BYTES] --fork -- ssh -f ARGS...
    tunnellog.py --log PATH [--cap BYTES] --listen PORT [--wait S] -- ssh ARGS...

--fork: ssh goes to the background itself once the forward is up (`ssh -f`): up when the ssh that was started exits 0.
--listen: ssh stays in the foreground (a provider's tunnel through lib/proxywrap.py, which ends it when this ssh goes):
up once that ssh listens on 127.0.0.1:PORT, within S seconds (else it's ended).

The tunnel's ssh writes to a pipe that a detached reader of its own drains for as long as the tunnel lasts (so ssh
never blocks on it), keeping at most --cap bytes in the log (a line says what was left out). The VM's end can make ssh
print without end; the log never grows past the cap. The log is made private: its folder 0700 (a folder of this
account's that isn't is made so; a link, or another account's folder, is refused), the file 0600, opened without
following a link, and refused when it's a link or another account's. What ssh said up to the moment the tunnel was up
(or failed), at most 64 KB, is printed on stdout for the caller to show; then this command exits with ssh's status (1
when it didn't come up). The reader holds none of the caller's descriptors, so a program reading this command's output
sees it end at once.
"""
import errno
import os
import select
import signal
import stat
import subprocess
import sys
import time

CAP = 1 << 20                 # what the log keeps of one tunnel's output
SHOWN = 64 << 10              # what the caller is given to show
INITIAL_DRAIN_S = 0.5         # how long what ssh wrote before it went to the background is read before the caller is told
CUT = b"\n[metacomputer: the rest of ssh's output was left out of this log (over its size limit)]\n"


class Refused(Exception):
    pass


def private_log(path):
    """The log file, open for writing (emptied), 0600 in a 0700 folder of this account's; Refused otherwise."""
    folder, uid = os.path.dirname(os.path.abspath(path)), os.getuid()
    try:
        os.mkdir(folder, 0o700)
    except FileExistsError:
        pass
    except OSError as e:
        raise Refused(f"couldn't make the log's folder {folder} ({e.strerror})")
    st = os.lstat(folder)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        raise Refused(f"the log's folder {folder} isn't a folder of this account's (a link, or another's)")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(folder, 0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused(f"the log {path} is a link; nothing was written to it")
        raise Refused(f"couldn't open the log {path} ({e.strerror})")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1:
        os.close(fd)
        raise Refused(f"the log {path} isn't a file of this account's alone; nothing was written to it")
    os.set_blocking(fd, True)
    os.fchmod(fd, 0o600)
    os.ftruncate(fd, 0)
    return fd


def listening(port, pid):
    """Whether process pid listens on TCP port (lsof)."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], capture_output=True,
                             text=True, timeout=5, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f"p{pid}" in out.split()


class Log:
    def __init__(self, fd, cap):
        if cap < len(CUT):      # the line saying the rest was left out must fit too
            raise ValueError(f"a log's cap is at least {len(CUT)} bytes")
        self.fd, self.cap, self.n, self.cut = fd, cap, 0, False

    def write(self, data):
        """At most cap bytes in the file, the line saying the rest was left out included (its room is kept)."""
        if self.cut or not data:
            return
        room = self.cap - len(CUT) - self.n
        part = data[:max(room, 0)]
        if part:
            os.write(self.fd, part)
            self.n += len(part)
        if len(data) > room:
            os.write(self.fd, CUT)
            self.n += len(CUT)
            self.cut = True


def reader(cmd, logfd, cap, mode, port, wait, r, w, sw):
    """The detached half: start ssh with its output on the pipe, say on sw when the tunnel is up (or isn't) and what it
    said so far, then drain the pipe into the log until the tunnel ends."""
    log, said, told = Log(logfd, cap), bytearray(), False

    def tell(rc):
        nonlocal told
        if not told:
            told = True
            try:
                os.write(sw, f"{rc}\n".encode() + bytes(said[:SHOWN]))
            finally:
                os.close(sw)
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=w, stderr=w, close_fds=True)
    except OSError as e:
        said += f"couldn't start ssh ({e.strerror})\n".encode()
        log.write(bytes(said))
        tell(255)
        return
    os.close(w)
    deadline = time.monotonic() + wait

    def take(timeout):
        """Read what's there (waiting up to timeout); False at the pipe's end."""
        if not select.select([r], [], [], timeout)[0]:
            return True
        chunk = os.read(r, 65536)
        if not chunk:
            return False
        log.write(chunk)
        if not told and len(said) < SHOWN:
            said.extend(chunk[:SHOWN - len(said)])
        return True
    open_ = True
    while not told:
        if open_:
            open_ = take(0.2)
        else:
            time.sleep(0.2)
        rc = p.poll()
        if rc is not None:
            # What it wrote before it exited, read for a moment at most (its background half can keep the pipe
            # readable without end): then the caller is told, and the rest is drained after.
            end = time.monotonic() + INITIAL_DRAIN_S
            while open_ and len(said) < SHOWN and time.monotonic() < end and select.select([r], [], [], 0)[0]:
                open_ = take(0)
            tell(rc if (mode == "fork" or rc != 0) else 1)
        elif mode == "listen" and listening(port, p.pid):
            tell(0)
        elif mode == "listen" and time.monotonic() > deadline:
            p.terminate()
            said.extend(b"the tunnel's forward didn't come up in time\n")
            tell(1)
    while open_:
        open_ = take(60)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def main(argv):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import strictargs           # its command line, by metacomputer's rules, before anything is opened or run
    strictargs.expect(argv, ["--log PATH [--cap BYTES] --fork -- ~COMMAND...",
                             "--log PATH [--cap BYTES] --listen PORT [--wait SECONDS] -- ~COMMAND..."],
                      prog="tunnellog.py", usage=__doc__)
    if "--" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    i = argv.index("--")
    opts, cmd = argv[:i], argv[i + 1:]
    log, cap, mode, port, wait = None, CAP, None, 0, 15.0
    seen = set()
    j = 0
    while j < len(opts):
        o = opts[j]
        # Each option once, and one of --fork or --listen: anything else is a usage error, before anything runs.
        if o in seen or (o in ("--fork", "--listen") and mode):
            print(f"tunnellog.py: {o} given twice, or with --fork/--listen (usage below)\n{__doc__}", file=sys.stderr)
            return 2
        seen.add(o)
        if o in ("--log", "--cap", "--listen", "--wait") and j + 1 < len(opts):
            v = opts[j + 1]
            try:
                if o == "--log":
                    log = v
                elif o == "--cap":
                    cap = int(v)
                    if cap < len(CUT):
                        raise ValueError
                elif o == "--listen":
                    mode, port = "listen", int(v)
                else:
                    wait = float(v)
            except ValueError:
                print(f"tunnellog.py: usage: {o} {v!r}: " + (f"a number of bytes, at least {len(CUT)} (the line saying the "
                      "rest was left out fits in it)" if o == "--cap" else "a number"), file=sys.stderr)
                return 2
            j += 2
        elif o == "--fork":
            mode = "fork"
            j += 1
        else:
            print(f"tunnellog.py: unknown option {o} (usage below)\n{__doc__}", file=sys.stderr)
            return 2
    if not log or not mode or not cmd:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        logfd = private_log(log)
    except Refused as e:
        print(f"metacomputer: {e}", file=sys.stderr)
        return 1
    r, w = os.pipe()
    sr, sw = os.pipe()
    pid = os.fork()
    if pid == 0:                      # the detached reader: its own session, none of the caller's descriptors
        try:
            os.setsid()
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            keep = {r, w, sw, logfd, 0, 1, 2}
            for fd in range(3, 4096):
                if fd not in keep:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            reader(cmd, logfd, cap, mode, port, wait, r, w, sw)
        finally:
            os._exit(0)
    os.close(w)
    os.close(r)
    os.close(sw)
    os.close(logfd)
    data = bytearray()
    while True:
        chunk = os.read(sr, 65536)
        if not chunk:
            break
        data += chunk
    os.close(sr)
    head, _, said = bytes(data).partition(b"\n")
    try:
        rc = int(head)
    except ValueError:
        rc, said = 1, b"the tunnel's helper ended without saying how it went\n"
    sys.stdout.buffer.write(said)
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
