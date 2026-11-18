"""Waiting for an exclusive flock: the loop shared by owners.lock() and events.Lock.

Only the wait lives here. Each caller keeps its own lock file, folder checks,
error and release: owners.lock() raises TimeoutError and releases by closing the
file; events.Lock raises Busy and unlocks before closing. Stdlib only.
"""
import fcntl
import time


def exclusive(fd, wait, poll):
    """Take an exclusive flock on the open file `fd`, trying every `poll` seconds for
    up to `wait` seconds. Returns once the lock is held. When the time runs out it
    raises the last BlockingIOError and leaves `fd` open: the caller closes it and
    raises its own error. Any other error from flock goes to the caller as it is."""
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll)
