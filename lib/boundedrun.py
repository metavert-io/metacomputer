"""Run a command whose output comes from the VM, without trusting how much or how slowly it answers.

    rc, out, err = run(cmd, stdin=b"..." | file object | None, max_out=N, max_err=M, timeout=S)

stdout and stderr are read as they arrive, never more than max_out / max_err bytes, all within
one deadline. Past a cap it raises Overflow, past the deadline Timeout; either way the command
(started in a process group of its own) is ended first, SIGTERM and then SIGKILL. Stdlib only.
"""
import os
import selectors
import signal
import subprocess
import threading
import time


class Overflow(Exception):
    pass


class Timeout(Exception):
    pass


def _end(p):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass
        try:
            p.wait(2)
            return
        except subprocess.TimeoutExpired:
            continue


def run(cmd, stdin=None, max_out=1 << 20, max_err=64 << 10, timeout=60):
    feed = isinstance(stdin, (bytes, bytearray))
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if feed else (stdin if stdin is not None else subprocess.DEVNULL),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    if feed:
        def writer():
            try:
                p.stdin.write(stdin)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass
        threading.Thread(target=writer, daemon=True).start()
    deadline = time.monotonic() + timeout
    out_fd, err_fd = p.stdout.fileno(), p.stderr.fileno()
    bufs = {out_fd: bytearray(), err_fd: bytearray()}
    caps = {out_fd: max_out, err_fd: max_err}
    sel = selectors.DefaultSelector()
    for f in (p.stdout, p.stderr):
        sel.register(f, selectors.EVENT_READ)
    try:
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                _end(p)
                raise Timeout(f"no complete answer within {timeout} s")
            for key, _ in sel.select(min(left, 1.0)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                bufs[key.fd] += chunk
                if len(bufs[key.fd]) > caps[key.fd]:
                    _end(p)
                    raise Overflow(f"more than {caps[key.fd]} bytes")
        left = max(0.1, deadline - time.monotonic())
        try:
            rc = p.wait(left)
        except subprocess.TimeoutExpired:
            _end(p)
            raise Timeout(f"no complete answer within {timeout} s")
    finally:
        sel.close()
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except OSError:
                pass
    return rc, bytes(bufs[out_fd]), bytes(bufs[err_fd])
