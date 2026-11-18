"""The VM's half of `metacomputer sh` (lib/vm_shell.py is the Mac's half).

Runs INSIDE the virtual computer as the desktop user. The Mac sends it over
the ops SSH connection's stdin, so nothing is quoted on either side:

  1. its length and this file (read by the one-line bootstrap in vm_shell.py);
  2. one JSON line: cwd, timeout, max_output, desktop, args, script_bytes;
  3. the script itself (script_bytes bytes).

The Mac then keeps stdin open until the answer arrives. The script runs with
bash, from a fixed path in a private folder, in its own process group. Its
stdout and stderr are pipes read here as they arrive: each stream is hashed
whole while only its first and last max_output/2 bytes are kept, so nothing
of it is written to disk and memory stays bounded. When the script ends this
prints one JSON line: how it ended, its exit code and duration, and for each
stream its size, SHA-256 and the kept bytes (base64).

- Timeout: the script's process group gets SIGTERM, then SIGKILL 3 s later if
  any of it is left, while its output is still read. The answer waits until the
  whole group is gone, not just bash.
- Stdin closes early (the Mac's command was interrupted or lost the
  connection): the same, reported as "cancelled", and this exits only after it.
- After the script's own process ends, its output is read for DRAIN_S more.
  A program it left running that still holds the output (started with `&`
  and not redirected) is then handed to a `cat >/dev/null`, so its later
  output is discarded instead of hitting a closed pipe; "held_open" says so.
- Programs started with `setsid -f` have their own session and keep running.
- desktop: load the XFCE session's environment (DISPLAY, XAUTHORITY, the
  session bus, XDG variables) and its accessibility settings, so programs the
  script starts open on the desktop and Cua Driver can read them.

The Mac treats everything this prints as untrusted (vm_shell.py validates
it). Python 3 standard library only.
"""
import base64
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ACCESS_ENV = "/etc/X11/Xsession.d/90metacomputer-env"
DESKTOP_KEYS = ("DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY")
CHUNK = 1 << 16
KILL_AFTER = 3.0   # SIGTERM grace for the script's process group
KILL_WAIT = 2.0    # after SIGKILL, how long to wait for the group to be gone
DRAIN_S = 0.5      # output still read after the script's own process ends


def build_env(desktop):
    """The script's environment and whether the desktop session's was loaded."""
    env = dict(os.environ)
    run = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    local_bin = os.path.join(env.get("HOME", ""), ".local", "bin")
    if os.path.isdir(local_bin) and local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = local_bin + ":" + env.get("PATH", "/usr/bin:/bin")
    for k in DESKTOP_KEYS:
        env.pop(k, None)
    if not desktop:
        return env, False
    try:
        with open(os.path.join(run, "metacomputer", "session.env")) as f:
            lines = f.read(65536).splitlines()
    except OSError:
        return env, False
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    if os.path.exists(ACCESS_ENV):
        try:
            r = subprocess.run(["bash", "--noprofile", "--norc", "-c", '. "$1" >/dev/null 2>&1; env -0', "_", ACCESS_ENV],
                               env=env, capture_output=True, timeout=10)
            if r.returncode == 0:
                env = dict(kv.split("=", 1) for kv in r.stdout.decode(errors="surrogateescape").split("\0") if "=" in kv)
        except (OSError, subprocess.SubprocessError):
            pass
    for k in ("_", "SHLVL", "PWD", "OLDPWD"):
        env.pop(k, None)
    return env, "DISPLAY" in env


class Capture:
    """One output stream: hashed whole, its first cap/2 and last cap/2 bytes kept."""

    def __init__(self, cap):
        self.cap, self.keep_tail = cap, cap - cap // 2
        self.h, self.total, self.head, self.tail = hashlib.sha256(), 0, bytearray(), bytearray()

    def add(self, b):
        self.h.update(b)
        self.total += len(b)
        if len(self.head) < self.cap:
            need = self.cap - len(self.head)
            self.head += b[:need]
            b = b[need:]
        if b:
            self.tail += b
            if len(self.tail) > self.keep_tail:
                del self.tail[:len(self.tail) - self.keep_tail]

    def summary(self):
        if self.total <= self.cap:
            return {"bytes": self.total, "sha256": self.h.hexdigest(), "truncated": False,
                    "head": base64.b64encode(bytes(self.head)).decode()}
        end = (self.head + self.tail)[-self.keep_tail:]   # head + tail is a contiguous suffix of the stream
        return {"bytes": self.total, "sha256": self.h.hexdigest(), "truncated": True,
                "head": base64.b64encode(bytes(self.head[:self.cap // 2])).decode(),
                "tail": base64.b64encode(bytes(end)).decode()}


def group_alive(pgid, proc):
    proc.poll()   # reap the script's own process: a zombie still counts as a member of its group
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def sweep(base):
    """Remove this user's run folders over a day old (left by a runner that was killed)."""
    try:
        names = os.listdir(base)
    except OSError:
        return
    for n in names:
        p = os.path.join(base, n)
        try:
            st = os.lstat(p)
            if n.startswith("mcsh.") and st.st_uid == os.getuid() and time.time() - st.st_mtime > 86400:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def emit(obj):
    try:
        sys.stdout.write(json.dumps({"metacomputer_sh": 1, **obj}) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass


def collect(proc, timeout, cap, cancel):
    """The script's whole lifecycle, in one loop that never blocks on its output:
    - stdout and stderr are read as they come, into bounded captures;
    - the deadline and cancel (set when the Mac's side is gone) are checked whether
      or not the script still has its output open;
    - to stop the script, its process group gets SIGTERM, then SIGKILL once
      KILL_AFTER has passed, and its output is still read meanwhile, so a TERM
      handler that prints a lot isn't blocked;
    - it returns once the script's own process has ended and its output is read
      (or DRAIN_S has passed), and, when it was being stopped, once its whole group
      is gone (or KILL_WAIT after SIGKILL): its own process ending isn't enough.
    Returns (captures, "timeout" | "cancelled" | None, held_open)."""
    caps = {"stdout": Capture(cap), "stderr": Capture(cap)}
    sel = selectors.DefaultSelector()
    for name, f in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        os.set_blocking(f.fileno(), False)
        sel.register(f.fileno(), selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    exited_at = term_at = kill_at = why = None
    try:
        while True:
            now = time.monotonic()
            if exited_at is None and proc.poll() is not None:
                exited_at = now
            if why is None and exited_at is None and (cancel.is_set() or now >= deadline):
                why = "cancelled" if cancel.is_set() else "timeout"
                signal_group(proc.pid, signal.SIGTERM)
                term_at = now
            group_gone = term_at is not None and not group_alive(proc.pid, proc)
            if exited_at is None and proc.returncode is not None:
                exited_at = now
            if term_at is not None and kill_at is None and not group_gone and now >= term_at + KILL_AFTER:
                signal_group(proc.pid, signal.SIGKILL)
                kill_at = now
            killed_long_ago = kill_at is not None and now >= kill_at + KILL_WAIT
            if exited_at is not None:
                drained = not sel.get_map() or now >= exited_at + DRAIN_S
                if drained and (term_at is None or group_gone or killed_long_ago):
                    break
            elif killed_long_ago:
                break   # its own process outlived SIGKILL (stuck in the kernel): stop waiting
            step = 0.05 if term_at is not None else 0.2
            if exited_at is None and term_at is None:
                step = max(0.0, min(step, deadline - now))
            if sel.get_map():
                for key, _ in sel.select(step):
                    try:
                        data = os.read(key.fd, CHUNK)
                    except BlockingIOError:
                        continue
                    if data:
                        caps[key.data].add(data)
                    else:
                        sel.unregister(key.fd)
            else:
                time.sleep(step)
        held = [key.fd for key in sel.get_map().values()]
    finally:
        sel.close()
    for fd in held:   # still held by a program the script left running: discard its later output
        try:
            os.set_blocking(fd, True)
            subprocess.Popen(["cat"], stdin=fd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass
    for f in (proc.stdout, proc.stderr):
        try:
            f.close()
        except OSError:
            pass
    return caps, why, bool(held)


def main():
    inp = sys.stdin.buffer
    try:
        hdr = json.loads(inp.readline())
        n = int(hdr["script_bytes"])
        script = inp.read(n)
        if len(script) != n:
            raise ValueError(f"expected {n} bytes of script, got {len(script)}")
        cap = int(hdr["max_output"])
        timeout = float(hdr["timeout"])
    except (ValueError, KeyError, TypeError) as e:
        emit({"status": "error", "code": "bad_request", "message": str(e)[:300]})
        return 2
    cwd = hdr.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        emit({"status": "error", "code": "no_such_folder", "message": cwd[:300]})
        return 2
    env, desktop = build_env(bool(hdr.get("desktop", True)))
    base = tempfile.gettempdir()
    sweep(base)
    d = tempfile.mkdtemp(prefix="mcsh.", dir=base)
    try:
        path = os.path.join(d, "script")   # fixed: nothing else lives in this folder
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(script)
        t0 = time.monotonic()
        proc = subprocess.Popen(["bash", "--noprofile", "--norc", path, *[str(a) for a in hdr.get("args") or []]],
                                cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        cancel, done = threading.Event(), threading.Event()

        def watch():   # stdin closes before the answer only if the Mac's side is gone
            # The raw fd, not sys.stdin.buffer: a thread blocked in the buffered reader
            # holds its lock, and interpreter shutdown waits a second for it. The Mac
            # sends nothing after the script, so the buffer holds nothing more.
            try:
                while os.read(0, 4096):
                    pass
            except OSError:
                pass
            if not done.is_set():
                cancel.set()   # collect() stops the script and waits until its group is gone

        threading.Thread(target=watch, daemon=True).start()
        caps, why, held = collect(proc, timeout, cap, cancel)
        done.set()
        rc = proc.poll()
        status = why or "exited"
        code = None if rc is None else 128 - rc if rc < 0 else rc
        emit({"status": status, "exit_code": code if status == "exited" else None,
              "duration_s": round(time.monotonic() - t0, 3), "desktop": desktop, "held_open": held,
              "stdout": caps["stdout"].summary(), "stderr": caps["stderr"].summary()})
        return 0
    except Exception as e:  # report it rather than leave the Mac without an answer
        emit({"status": "error", "code": "internal", "message": f"{type(e).__name__}: {e}"[:300]})
        return 2
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    try:
        sys.stdout.flush()
    except OSError:
        pass
    os._exit(rc)   # don't wait for the stdin watcher
