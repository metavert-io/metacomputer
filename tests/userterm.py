"""Run a command as at the user's own terminal, for tests of commands only the user may run.

Those commands want a terminal with no agent session (Claude Code, Codex, OpenClaw, Hermes Agent, Gemini CLI) above them (lib/owners.py,
at_user_terminal), and the test suites are often run by an agent. `spawn` runs the command in a process that launchd
(pid 1) has adopted, so whatever runs the suite isn't above it; its standard streams are whatever the test passes
(usually a pty's slave end). No setting in the code under test is involved: the process tree is the real one.

    p = userterm.spawn([cmd, ...], env=e, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(timeout=60)    # or p.poll() / p.wait(timeout); p.returncode
"""
import os
import select
import subprocess
import tempfile
import time

_AGENTS = {}


def fake_agent(name="codex"):
    """A small program named `name` that runs its arguments as its child and exits with their status: stands in for an
    agent session (Codex, which can give its commands a terminal) above a command. Built here with cc, because copies
    of Apple's own binaries don't run; None without a C compiler."""
    if name not in _AGENTS:
        d = tempfile.mkdtemp(prefix="mcomp-agent-")
        src, out = os.path.join(d, name + ".c"), os.path.join(d, name)
        with open(src, "w") as f:
            f.write("#include <unistd.h>\n#include <sys/wait.h>\nint main(int c, char **v) { pid_t p = fork(); "
                    "if (p == 0) { execv(v[1], v + 1); _exit(127); } int st = 0; waitpid(p, &st, 0); "
                    "return WIFEXITED(st) ? WEXITSTATUS(st) : 126; }\n")
        subprocess.run(["/usr/bin/cc", "-o", out, src], capture_output=True)
        _AGENTS[name] = out if os.path.exists(out) else None
    return _AGENTS[name]


def fake_interpreter(name="node"):
    """A small program named `name` (node, python3) that runs what follows `--` in its arguments as its child and exits
    with its status: stands in for an agent that runs on an interpreter, whose arguments before `--` say which program
    it runs (Gemini CLI's `node .../bin/gemini`, Hermes Agent's `python3 -m hermes_cli.main gateway run`). Started
    with another argv[0] (subprocess's executable=), it stands in for a process that names itself (OpenClaw's
    `openclaw-gateway`). None without a C compiler."""
    key = "interp:" + name
    if key not in _AGENTS:
        d = tempfile.mkdtemp(prefix="mcomp-interp-")
        src, out = os.path.join(d, name + ".c"), os.path.join(d, name)
        with open(src, "w") as f:
            f.write("#include <string.h>\n#include <unistd.h>\n#include <sys/wait.h>\nint main(int c, char **v) { int i = 1; "
                    "while (i < c && strcmp(v[i], \"--\") != 0) i++; if (i + 1 >= c) return 111; pid_t p = fork(); "
                    "if (p == 0) { execv(v[i + 1], v + i + 1); _exit(127); } int st = 0; waitpid(p, &st, 0); "
                    "return WIFEXITED(st) ? WEXITSTATUS(st) : 126; }\n")
        subprocess.run(["/usr/bin/cc", "-o", out, src], capture_output=True)
        _AGENTS[key] = out if os.path.exists(out) else None
    return _AGENTS[key]


class Detached:
    """A command started by spawn(): its exit status arrives on a pipe from the adopted process that waits for it."""

    def __init__(self, status_fd, stdout=None, stderr=None, args=None):
        self.args = args
        self._fd = status_fd
        self._buf = b""
        self.returncode = None
        self.stdout = stdout          # file objects for the ends the test reads (stdout/stderr=subprocess.PIPE)
        self.stderr = stderr

    def _read(self, timeout):
        if self.returncode is not None or self._fd is None:
            return
        end = time.time() + timeout
        while True:
            left = max(0.0, end - time.time())
            if not select.select([self._fd], [], [], left)[0]:
                return
            chunk = os.read(self._fd, 64)
            if not chunk:
                os.close(self._fd)
                self._fd = None
                try:
                    self.returncode = int(self._buf)
                except ValueError:
                    self.returncode = -1          # the adopted process ended without saying
                return
            self._buf += chunk

    def poll(self):
        self._read(0)
        return self.returncode

    def wait(self, timeout=None):
        self._read(3600 if timeout is None else timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("detached command", timeout)
        return self.returncode

    def communicate(self, timeout=60):
        """Read what the command wrote to the PIPE ends until it ends; (stdout bytes, stderr bytes)."""
        out, err = b"", b""
        streams = [s for s in (self.stdout, self.stderr) if s is not None]
        end = time.time() + timeout
        while streams and time.time() < end:
            ready = select.select(streams, [], [], 0.2)[0]
            for s in ready:
                chunk = os.read(s.fileno(), 65536)
                if not chunk:
                    streams.remove(s)
                elif s is self.stdout:
                    out += chunk
                else:
                    err += chunk
        try:
            self.wait(max(1.0, end - time.time()))
        except subprocess.TimeoutExpired as e:
            e.output, e.stderr = out, err
            raise
        for s in (self.stdout, self.stderr):
            if s is not None:
                s.close()
        return out, err


def spawn(argv, env=None, cwd=None, stdin=None, stdout=None, stderr=None):
    """Start argv in a process adopted by launchd, with the given standard streams (a file descriptor, a file, None,
    or subprocess.PIPE for stdout/stderr)."""
    pipes = {}
    for name, v in (("stdout", stdout), ("stderr", stderr)):
        if v == subprocess.PIPE:
            r, w = os.pipe()
            pipes[name] = (r, w)
    out_fd = pipes["stdout"][1] if "stdout" in pipes else stdout
    err_fd = pipes["stderr"][1] if "stderr" in pipes else stderr
    status_r, status_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(status_r)
            for r, _ in pipes.values():
                os.close(r)
            if os.fork() != 0:
                os._exit(0)                  # the middle process exits, so launchd adopts its child
            for _ in range(500):
                if os.getppid() == 1:
                    break
                time.sleep(0.01)
            # In the test's own session, as subprocess.Popen started it before: a session leader holding a terminal
            # can't finish exiting while output it never had read (the echo of what the test typed) waits there.
            p = subprocess.Popen(argv, env=env, cwd=cwd, stdin=stdin, stdout=out_fd, stderr=err_fd)
            # Keep nothing of the test's open: its pty and pipes see their end when the command's copies close.
            for fd in range(3, 4096):
                if fd != status_w:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            rc = p.wait()
            os.write(status_w, str(rc).encode())
        finally:
            os._exit(0)
    os.close(status_w)
    for _, w in pipes.values():
        os.close(w)
    os.waitpid(pid, 0)
    return Detached(status_r, args=list(argv),
                    stdout=os.fdopen(pipes["stdout"][0], "rb", buffering=0) if "stdout" in pipes else None,
                    stderr=os.fdopen(pipes["stderr"][0], "rb", buffering=0) if "stderr" in pipes else None)
