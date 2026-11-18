"""The VM's half of `metacomputer sh` (lib/vm_shell.py is the Mac's half).

Runs INSIDE the virtual computer as the desktop user. The Mac sends it over
the ops SSH connection's stdin, so nothing is quoted on either side:

  1. its length and this file (read by the one-line bootstrap in vm_shell.py);
  2. one JSON line: cwd, timeout, max_output, desktop, args, name, script_bytes;
  3. the script itself (script_bytes bytes).

The Mac then keeps stdin open until the answer arrives. The script runs with
bash in its own process group, with stdout and stderr going to files in a
private folder, so a program it leaves running can't hold the connection
open. When the script ends, this prints one JSON line: how it ended, its exit
code and duration, and for each stream its size, SHA-256 and its start and
end (up to max_output bytes, base64).

- Timeout: the script's process group gets SIGTERM, then SIGKILL 3 s later.
- Stdin closes early (the Mac's command was interrupted or lost the
  connection): the same, reported as "cancelled".
- Programs started with `setsid -f` have their own session and keep running.
- desktop: load the XFCE session's environment (DISPLAY, XAUTHORITY, the
  session bus, XDG variables) and its accessibility settings, so programs the
  script starts open on the desktop and Cua Driver can read them.

Python 3 standard library only.
"""
import base64
import hashlib
import json
import os
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
KILL_AFTER = 3.0


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
            lines = f.read().splitlines()
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


def summarize(path, cap):
    """Size, SHA-256 and the kept part of a captured stream: all of it up to cap
    bytes, else its first cap/2 and last cap/2 bytes (base64)."""
    h, total, head, tail = hashlib.sha256(), 0, bytearray(), bytearray()
    keep_tail = cap - cap // 2
    try:
        f = open(path, "rb")
    except OSError:
        return {"bytes": 0, "sha256": h.hexdigest(), "truncated": False, "head": ""}
    with f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            total += len(b)
            if len(head) < cap:
                need = cap - len(head)
                head += b[:need]
                b = b[need:]
            if b:
                tail += b
                if len(tail) > keep_tail:
                    del tail[:len(tail) - keep_tail]
    if total <= cap:
        return {"bytes": total, "sha256": h.hexdigest(), "truncated": False,
                "head": base64.b64encode(bytes(head)).decode()}
    end = (head + tail)[-keep_tail:]   # head + tail is a contiguous suffix of the stream
    return {"bytes": total, "sha256": h.hexdigest(), "truncated": True,
            "head": base64.b64encode(bytes(head[:cap // 2])).decode(),
            "tail": base64.b64encode(bytes(end)).decode()}


def stop(proc):
    """SIGTERM to the script's process group, SIGKILL after KILL_AFTER seconds."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + KILL_AFTER
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
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


def main():
    inp = sys.stdin.buffer
    try:
        hdr = json.loads(inp.readline())
        n = int(hdr["script_bytes"])
        script = inp.read(n)
        if len(script) != n:
            raise ValueError(f"expected {n} bytes of script, got {len(script)}")
    except (ValueError, KeyError, TypeError) as e:
        emit({"status": "error", "message": f"bad request from the Mac: {e}"})
        return 2
    cwd = hdr.get("cwd") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        emit({"status": "error", "message": f"no such folder in the virtual computer: {cwd}"})
        return 2
    env, desktop = build_env(bool(hdr.get("desktop", True)))
    base = tempfile.gettempdir()
    sweep(base)
    d = tempfile.mkdtemp(prefix="mcsh.", dir=base)
    try:
        name = "".join(c for c in str(hdr.get("name") or "") if c.isalnum() or c in "._-")[:64].lstrip(".") or "metacomputer-sh"
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(script)
        cap = int(hdr.get("max_output") or 65536)
        out_p, err_p = os.path.join(d, "stdout"), os.path.join(d, "stderr")
        with open(out_p, "wb") as fo, open(err_p, "wb") as fe:
            t0 = time.monotonic()
            proc = subprocess.Popen(["bash", "--noprofile", "--norc", path, *[str(a) for a in hdr.get("args") or []]],
                                    cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                    start_new_session=True)
        state = {"done": False, "cancelled": False}

        def watch():   # stdin closes before the answer only if the Mac's side is gone
            # The raw fd, not sys.stdin.buffer: a thread blocked in the buffered reader
            # holds its lock, and interpreter shutdown waits a second for it. The Mac
            # sends nothing after the script, so the buffer holds nothing more.
            try:
                while os.read(0, 4096):
                    pass
            except OSError:
                pass
            if not state["done"]:
                state["cancelled"] = True
                stop(proc)

        threading.Thread(target=watch, daemon=True).start()
        status = "exited"
        try:
            rc = proc.wait(timeout=float(hdr.get("timeout") or 120))
        except subprocess.TimeoutExpired:
            status = "timeout"
            stop(proc)
            rc = proc.wait()
        if state["cancelled"]:
            status = "cancelled"
        state["done"] = True
        duration = time.monotonic() - t0
        code = 128 - rc if rc < 0 else rc
        emit({"status": status, "exit_code": code if status == "exited" else None, "raw_exit": code,
              "duration_s": round(duration, 3), "cwd": cwd, "desktop": desktop,
              "stdout": summarize(out_p, cap), "stderr": summarize(err_p, cap)})
        return 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    try:
        sys.stdout.flush()
    except OSError:
        pass
    os._exit(rc)   # don't wait for the stdin watcher
