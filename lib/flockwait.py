"""Waiting for a flock: the loop shared by owners.lock(), owners.dispatch() and events.Lock.

Only the wait lives here. Each caller keeps its own lock file, folder checks,
error and release: owners.lock() and owners.dispatch() raise TimeoutError and
release by closing the file; events.Lock raises Busy and unlocks before closing.
Stdlib only.
"""
import fcntl
import time


def take(fd, wait, poll, shared=False):
    """Take a flock on the open file `fd` (shared, or exclusive), trying every `poll`
    seconds for up to `wait` seconds. Returns once the lock is held. When the time runs
    out it raises the last BlockingIOError and leaves `fd` open: the caller closes it and
    raises its own error. Any other error from flock goes to the caller as it is."""
    deadline = time.monotonic() + wait
    op = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    while True:
        try:
            fcntl.flock(fd, op)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll)


def exclusive(fd, wait, poll):
    """take(), exclusive."""
    take(fd, wait, poll)
