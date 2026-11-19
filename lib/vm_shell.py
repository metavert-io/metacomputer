#!/usr/bin/env python3
"""`metacomputer sh`: run a bash script in the virtual computer and get its result.

bin/metacomputer runs this as

  vm_shell.py --transport N WORD... [user arguments]

where the N words are the command that reaches the VM (`ssh -T ...`, the
same path, key and account as `metacomputer ssh ops`) and the rest are the
user's. The script goes over SSH's stdin together with the VM's half
(vm_shell_guest.py), so nothing is quoted or expanded on the Mac. See
docs/shell.md for the design and SECURITY.md for what it can reach.

The VM is untrusted, including other programs running as its desktop user.
Whatever comes back, this side:
- reads at most a fixed number of bytes per stream (set from --max-output),
  under one deadline that starts before ssh does, and closes the connection
  past either;
- accepts only a reply that fits a strict schema, and shows nothing of one
  that doesn't;
- escapes control characters before they reach a terminal (--raw opts out);
- logs only hashes and sizes of the VM's free text unless ACTION_LOG_TEXT=keep.

Settings (config/defaults.env, passed in by bin/metacomputer):
  VM_SHELL   auto | on | off     auto = on, except off while DRIVER_MODE=bounded
  SHELL_LOG  auto | off          auto = every run goes in the signed action log
                                 when ACTION_LOG=on; off = never logged
MCOMP_SH_GRACE_S (tests only) shortens how long the Mac waits past --timeout.
"""
import base64
import binascii
import contextlib
import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import owners  # noqa: E402
from termsafe import UNSAFE, escape_for_terminal  # noqa: E402,F401

LAB = owners.LAB
GUEST_PART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vm_shell_guest.py")
# The fixed remote command: read a length, then that much Python (the VM's half), and run it.
BOOTSTRAP = ("python3 -I -c 'import sys;b=sys.stdin.buffer;n=int(b.readline());"
             "exec(compile(b.read(n),\"metacomputer-sh\",\"exec\"))'")

TOOL = "vm_shell"
DEFAULT_TIMEOUT, MAX_TIMEOUT = 120, 3600
DEFAULT_OUTPUT, MIN_OUTPUT, MAX_OUTPUT = 65536, 1024, 1048576
MAX_SCRIPT = 1048576


def _grace():
    try:
        return min(300, max(1, int(os.environ.get("MCOMP_SH_GRACE_S") or 30)))
    except ValueError:
        return 30


GRACE_S = _grace()      # the Mac waits this much longer than the VM's own timeout
CLAIM_EVERY_S = 60      # a long run keeps its claim on the computer fresh
CHUNK = 1 << 16
STDERR_WIRE = 65536     # ssh's and the VM's own diagnostics
REPLY_OVERHEAD = 16384  # the reply's JSON around the two streams
MAX_MESSAGE = 1000      # the VM's error text, in characters
REMOTE_SHOWN = 400      # how much of the VM's diagnostics a terminal shows

# This command's exit codes when the script's own can't be given.
EXIT_USAGE, EXIT_TIMEOUT, EXIT_REFUSED, EXIT_FAILED, EXIT_INTERRUPTED = 2, 124, 125, 255, 130

USAGE = """usage: metacomputer sh -c 'COMMANDS' | --file SCRIPT | -   [options] [-- ARGS...]

Runs bash in the virtual computer as its desktop user, with the desktop
session's environment (DISPLAY, the session bus, XDG variables). The script
goes over SSH's stdin: nothing is quoted or expanded on the Mac (~ is the
VM's home). Output comes back when the script ends.

  -c 'COMMANDS'        the script as one argument
  --file SCRIPT        a script file on the Mac
  -                    the script on stdin (the script's own stdin is empty)
  --cwd DIR            folder in the VM to run in (default: the desktop user's home)
  --timeout SECONDS    stop the script after this long (default 120, at most 3600)
  --max-output BYTES   keep at most this much of stdout and of stderr
                       (default 65536, at most 1048576; the start and end are kept)
  --read-only          for checks that change nothing: runs without the desktop's
                       environment and doesn't take over the computer, so it works
                       while another session uses it. A promise, not a sandbox.
  --override           run even though another session is using the computer
                       (ask the user first)
  --json               the result as one JSON object
  --raw                print the output exactly as it came, even to a terminal
                       (otherwise control characters are shown escaped there)
  -- ARGS              $1, $2, ... for the script

To leave a program running (a GUI app), start it detached:
  setsid -f APP ARGS </dev/null >/tmp/APP.log 2>&1

Exit status: the script's own; 124 timed out; 125 refused (VM_SHELL, the
bounded driver, another session, the action log); 255 couldn't run or finish
(VM off, SSH, no such folder, a malformed answer, a log that couldn't be
signed); 130 interrupted; 2 usage.
Settings: VM_SHELL=auto|on|off, SHELL_LOG=auto|off (metacomputer security)."""


class Usage(Exception):
    pass


class Protocol(Exception):
    """The VM's answer doesn't fit the schema. Messages are fixed text, never the VM's."""


# --- settings ------------------------------------------------------------------
def settings(env=None):
    env = os.environ if env is None else env
    return {"vm_shell": env.get("MCOMP_VM_SHELL", "auto") or "auto",
            "shell_log": env.get("MCOMP_SHELL_LOG", "auto") or "auto",
            "action_log": env.get("MCOMP_ACTION_LOG", "off") or "off",
            "action_log_text": env.get("MCOMP_ACTION_LOG_TEXT", "redact") or "redact",
            "driver_mode": env.get("MCOMP_DRIVER_MODE", "standard") or "standard"}


def shell_allowed(vm_shell, driver_mode):
    """(True/False/None for an invalid value, explanation)."""
    if vm_shell == "on":
        return True, "VM_SHELL=on"
    if vm_shell == "off":
        return False, "VM_SHELL=off"
    if vm_shell == "auto":
        if driver_mode == "bounded":
            return False, "VM_SHELL=auto and DRIVER_MODE=bounded"
        return True, "VM_SHELL=auto"
    return None, f"VM_SHELL={vm_shell!r} isn't one of auto, on, off"


def logging_on(shell_log, action_log):
    """True/False, or None for an invalid SHELL_LOG."""
    if shell_log not in ("auto", "off"):
        return None
    return shell_log == "auto" and action_log == "on"


def describe_settings(s=None):
    """What `metacomputer security` shows: the effective shell and logging settings."""
    s = s or settings()
    allowed, why = shell_allowed(s["vm_shell"], s["driver_mode"])
    logged = logging_on(s["shell_log"], s["action_log"])
    problems = []
    if allowed is None:
        problems.append(why + "; metacomputer sh refuses to run")
    if logged is None:
        problems.append(f"SHELL_LOG={s['shell_log']!r} isn't one of auto, off; metacomputer sh refuses to run")
    return {"setting": s["vm_shell"], "effective": "on" if allowed else "off", "why": why,
            "log": s["shell_log"], "logged": bool(logged),
            "text": s["action_log_text"] if logged else None, "problems": problems}


# --- arguments -----------------------------------------------------------------
def _int(opt, v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise Usage(f"{opt} needs a whole number")
    if not lo <= n <= hi:
        raise Usage(f"{opt} must be between {lo} and {hi}")
    return n


def parse(argv):
    o = {"source": None, "command": None, "file": None, "cwd": None, "timeout": DEFAULT_TIMEOUT,
         "max_output": DEFAULT_OUTPUT, "json": False, "raw": False, "read_only": False, "override": False,
         "args": [], "help": False}

    def source(kind):
        if o["source"]:
            raise Usage("give the script one way: -c, --file or -")
        o["source"] = kind

    i = 0
    while i < len(argv):
        a = argv[i]
        opt, eq, val = a.partition("=") if a.startswith("--") else (a, "", "")

        def value():
            nonlocal i
            if eq:
                return val
            if i + 1 >= len(argv):
                raise Usage(f"{opt} needs a value")
            i += 1
            return argv[i]

        if a == "--":
            o["args"] = argv[i + 1:]
            break
        elif opt in ("-c", "--command"):
            source("command")
            o["command"] = value()
        elif opt == "--file":
            source("file")
            o["file"] = value()
        elif a == "-":
            source("stdin")
        elif opt == "--cwd":
            o["cwd"] = value()
        elif opt == "--timeout":
            o["timeout"] = _int("--timeout", value(), 1, MAX_TIMEOUT)
        elif opt == "--max-output":
            o["max_output"] = _int("--max-output", value(), MIN_OUTPUT, MAX_OUTPUT)
        elif a == "--json":
            o["json"] = True
        elif a == "--raw":
            o["raw"] = True
        elif a == "--read-only":
            o["read_only"] = True
        elif a == "--override":
            o["override"] = True
        elif a in ("-h", "--help"):
            o["help"] = True
        else:
            raise Usage(f"unknown argument {a!r}")
        i += 1
    if not o["help"] and not o["source"]:
        raise Usage("no script: give -c 'COMMANDS', --file SCRIPT or - (stdin)")
    if o["read_only"] and o["override"]:
        raise Usage("--read-only doesn't take over the computer, so --override has nothing to do")
    return o


def guest_cwd(p, user):
    home = f"/home/{user}"
    if not p or p == "~":
        return home
    if p.startswith("~/"):
        return home + "/" + p[2:]
    if p.startswith("/"):
        return p
    return home + "/" + p


def read_script(o, stdin=None):
    if o["source"] == "command":
        data = o["command"].encode("utf-8", "surrogateescape")
    elif o["source"] == "file":
        try:
            with open(o["file"], "rb") as f:
                data = f.read(MAX_SCRIPT + 1)
        except OSError as e:
            raise Usage(f"can't read {o['file']}: {e.strerror}")
    else:
        data = (stdin or sys.stdin.buffer).read(MAX_SCRIPT + 1)
    if len(data) > MAX_SCRIPT:
        raise Usage(f"the script is over {MAX_SCRIPT // 1024} KB; put it in the VM (metacomputer put) and run it from there")
    return data


# --- the VM's answer -------------------------------------------------------------
GUEST_ERRORS = {"bad_request": "the virtual computer's side couldn't read the request",
                "no_such_folder": "no such folder in the virtual computer: {cwd}",
                "internal": "the virtual computer's side failed"}
STATUSES = ("exited", "timeout", "cancelled")
OK_KEYS = {"metacomputer_sh", "status", "exit_code", "duration_s", "desktop", "held_open", "stdout", "stderr"}
STREAM_KEYS = {"bytes", "sha256", "truncated", "head"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_STREAM_BYTES = 1 << 50


def b64len(n):
    return 4 * ((n + 2) // 3)


def wire_limit(cap):
    """The most a well-formed reply can be: each stream's kept bytes as base64, plus the JSON around them."""
    return 2 * (b64len(cap // 2) + b64len(cap - cap // 2)) + REPLY_OVERHEAD


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise Protocol("a key appears twice")
        d[k] = v
    return d


def _no_constant(_c):
    raise Protocol("NaN or Infinity in the answer")


def _b64(s, n, what):
    """Exactly n bytes from base64 text; the length is checked before anything is decoded."""
    if not isinstance(s, str) or len(s) != b64len(n):
        raise Protocol(f"{what}: wrong length")
    try:
        b = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise Protocol(f"{what}: not base64")
    if len(b) != n:
        raise Protocol(f"{what}: wrong length")
    return b


def check_stream(d, cap, what):
    if not isinstance(d, dict) or type(d.get("truncated")) is not bool:
        raise Protocol(f"{what}: not a stream summary")
    t = d["truncated"]
    if set(d) != STREAM_KEYS | ({"tail"} if t else set()):
        raise Protocol(f"{what}: unexpected fields")
    n, sha = d["bytes"], d["sha256"]
    if type(n) is not int or not 0 <= n <= MAX_STREAM_BYTES:
        raise Protocol(f"{what}: bad size")
    if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
        raise Protocol(f"{what}: bad sha256")
    if t:
        if n <= cap:
            raise Protocol(f"{what}: marked truncated but within the cap")
        head, tail = _b64(d["head"], cap // 2, what), _b64(d["tail"], cap - cap // 2, what)
    else:
        if n > cap:
            raise Protocol(f"{what}: over the cap but not marked truncated")
        head, tail = _b64(d["head"], n, what), b""
    return {"bytes": n, "sha256": sha, "truncated": t, "head": head, "tail": tail}


def parse_reply(raw, cap, timeout):
    """The VM's answer as checked data, or Protocol. raw is already bounded by wire_limit."""
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise Protocol("not exactly one line")
    try:
        d = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Protocol("not JSON")
    if not isinstance(d, dict) or type(d.get("metacomputer_sh")) is not int or d["metacomputer_sh"] != 1:
        raise Protocol("not a metacomputer sh answer")
    st = d.get("status")
    if st == "error":
        if not {"metacomputer_sh", "status", "code"} <= set(d) <= {"metacomputer_sh", "status", "code", "message"}:
            raise Protocol("unexpected fields")
        if not isinstance(d["code"], str) or d["code"] not in GUEST_ERRORS:
            raise Protocol("unknown error code")
        msg = d.get("message", "")
        if not isinstance(msg, str) or len(msg) > MAX_MESSAGE:
            raise Protocol("bad error message")
        return {"status": "error", "code": d["code"], "message": msg}
    if not isinstance(st, str) or st not in STATUSES:
        raise Protocol("unknown status")
    if set(d) != OK_KEYS:
        raise Protocol("unexpected fields")
    ec = d["exit_code"]
    if st == "exited":
        if type(ec) is not int or not 0 <= ec <= 255:
            raise Protocol("exit code out of range")
    elif ec is not None:
        raise Protocol("an exit code without an exit")
    dur = d["duration_s"]
    if type(dur) not in (int, float) or not math.isfinite(dur) or not 0 <= dur <= timeout + 600:
        raise Protocol("bad duration")
    for k in ("desktop", "held_open"):
        if type(d[k]) is not bool:
            raise Protocol(f"{k}: not true or false")
    return {"status": st, "exit_code": ec, "duration_s": float(dur), "desktop": d["desktop"],
            "held_open": d["held_open"], "stdout": check_stream(d["stdout"], cap, "stdout"),
            "stderr": check_stream(d["stderr"], cap, "stderr")}


# --- output ----------------------------------------------------------------------
def utf8_cut_end(b):
    """b without a partial UTF-8 character at its end."""
    for k in range(1, min(4, len(b)) + 1):
        c = b[-k]
        if c & 0xC0 == 0x80:
            continue
        need = 2 if c & 0xE0 == 0xC0 else 3 if c & 0xF0 == 0xE0 else 4 if c & 0xF8 == 0xF0 else 1
        return b if need <= k else b[:-k]
    return b


def utf8_cut_start(b):
    """b without the continuation bytes of a character cut off at its start."""
    i = 0
    while i < min(3, len(b)) and b[i] & 0xC0 == 0x80:
        i += 1
    return b[i:]


def shown_bytes(st, label):
    """What a terminal or --json shows of one checked stream."""
    if not st["truncated"]:
        return st["head"]
    head, tail = utf8_cut_end(st["head"]), utf8_cut_start(st["tail"])
    left_out = st["bytes"] - len(head) - len(tail)
    note = (f"\n[metacomputer sh: {left_out:,} bytes of {label} left out here; it was {st['bytes']:,} bytes in all. "
            f"Use --max-output, or write it to a file in the VM and copy it with metacomputer get.]\n").encode()
    return head + note + tail


# Terminal escaping lives in termsafe.py (shared with inspect and web): control
# characters but newline and tab, DEL, C1 controls and bidi controls are shown escaped.
clean = escape_for_terminal


def clean_bytes(b):
    return clean(b.decode("utf-8", "backslashreplace")).encode("utf-8")


# --- the action log --------------------------------------------------------------
def session_info(s):
    table = owners.ps_table()
    e = os.environ.get
    return {"bridge_pid": os.getpid(), "claude_pid": owners.claude_of(os.getpid(), table), "cwd": os.getcwd(),
            "via": "shell", "vm": e("MCOMP_VM_NAME", ""), "driver_mode": s["driver_mode"],
            "driver_profile": e("MCOMP_DRIVER_PROFILE", ""), "ssh_auth": e("MCOMP_SSH_AUTH", "key"),
            "vm_shell": s["vm_shell"]}


def call_args(o, script, cwd, keep_text):
    """The call record's arguments. The script and its arguments are kept only
    with ACTION_LOG_TEXT=keep, like typed text; their hash and size always. The
    guest folder and the --file name are paths, kept like a driver call's paths."""
    text = script.decode("utf-8", "replace")
    args = {"script": text if keep_text else {"redacted": True, "chars": len(text)},
            "script_bytes": len(script), "script_sha256": hashlib.sha256(script).hexdigest(),
            "source": o["source"], "cwd": cwd, "timeout_s": o["timeout"], "max_output": o["max_output"],
            "read_only": o["read_only"], "override": o["override"]}
    if o["source"] == "file":
        args["file"] = os.path.basename(o["file"])
    if o["args"]:
        args["script_args"] = list(o["args"]) if keep_text else {
            "redacted": True, "count": len(o["args"]), "chars": sum(len(a) for a in o["args"])}
    return args


def result_fields(res, keep_text):
    """The result record. Only fixed text written here and checked values; what the
    VM said in its own words (output, diagnostics) only as size and hash unless keep."""
    out = {"status": res["status"], "kind": res.get("kind"), "exit_code": res.get("exit_code"),
           "error": res["status"] != "exited" or res.get("exit_code") != 0 or bool(res.get("kind")),
           "desktop": res.get("desktop"), "held_open": res.get("held_open")}
    if res.get("message"):
        out["message"] = res["message"][:300]
    for k in ("stdout", "stderr"):
        st = (res.get("_streams") or {}).get(k)
        if st:
            out[k] = {"bytes": st["bytes"], "sha256": st["sha256"], "truncated": st["truncated"]}
            if keep_text:
                out[k]["text"] = shown_bytes(st, k).decode("utf-8", "replace")
    remote = res.get("_remote") or b""
    if remote:
        if keep_text:
            out["remote_text"] = remote[-2000:].decode("utf-8", "replace")
        else:
            out["remote"] = {"bytes": len(remote), "sha256": hashlib.sha256(remote).hexdigest()}
    return out


class Log:
    """One action-log file per run, when logging applies (else every method does nothing)."""

    def __init__(self, s, tool=TOOL):
        self.log = None
        self.keep = s["action_log_text"] == "keep"
        self.s = s
        self.tool = tool

    def open(self):
        import actionlog
        cfg = actionlog.settings()
        cfg["keep_text"] = self.keep
        self.log = actionlog.ActionLog(session_info(self.s), cfg)
        return self.log

    def name(self):
        return self.log.name if self.log else None

    def refused(self, args, reason):
        if self.log:
            self.log.refused(self.tool, args, reason)

    def call(self, args):
        if self.log:
            self.log.call(1, self.tool, args)

    def sign_now(self):
        return self.log.sign_now() if self.log else True

    def result(self, ms, fields):
        if self.log:
            self.log.event("result", id=1, tool=self.tool, ms=ms, result=fields)

    def close(self):
        """True when the log (if any) ended with a good final signature."""
        if not self.log:
            return True
        self.log.close()
        return self.log.sign_error is None


# --- the connection --------------------------------------------------------------
@contextlib.contextmanager
def deferred_signals():
    """While cleanup runs, SIGINT, SIGTERM and SIGHUP are only noted (the list it
    yields), so a repeated Ctrl-C can't cut short killing, reaping and closing."""
    late = []

    def note(signum, _frame):
        late.append(signum)

    old = {sig: signal.signal(sig, note) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield late
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def run_remote(transport, payload, deadline, limits, on_tick=None):
    """Send the payload and read the answer, never past the deadline or a stream's limit.
    The transport's stdin stays open until the end: the VM's half stops the script if it
    closes first. Returns (stdout, stderr, problem), problem being None, "timeout",
    "overflow:stdout", "overflow:stderr" or "spawn"."""
    out = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        p = subprocess.Popen(transport + [BOOTSTRAP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    except OSError as e:
        return b"", str(e).encode(), "spawn"
    problem = None
    sel = selectors.DefaultSelector()
    try:
        fin = p.stdin.fileno()
        for f in (p.stdin, p.stdout, p.stderr):
            os.set_blocking(f.fileno(), False)
        sel.register(fin, selectors.EVENT_WRITE, "stdin")
        sel.register(p.stdout.fileno(), selectors.EVENT_READ, "stdout")
        sel.register(p.stderr.fileno(), selectors.EVENT_READ, "stderr")
        view, sent, reading = memoryview(payload), 0, 2
        next_tick = time.monotonic() + CLAIM_EVERY_S
        while reading and not problem:
            now = time.monotonic()
            if on_tick and now >= next_tick:
                on_tick()                    # a claim refresh: a file write, no process table
                now = time.monotonic()       # it took time: check the deadline from now
                next_tick = now + CLAIM_EVERY_S
            if now >= deadline:
                problem = "timeout"
                break
            wait = min(1.0, deadline - now, (next_tick - now) if on_tick else 1.0)
            for key, _ in sel.select(max(0.0, wait)):
                if key.data == "stdin":
                    try:
                        sent += os.write(fin, view[sent:sent + CHUNK])
                    except BlockingIOError:
                        continue
                    except OSError:          # the VM's side closed it
                        sel.unregister(fin)
                        continue
                    if sent >= len(payload):
                        sel.unregister(fin)  # all sent; it stays open
                    continue
                try:
                    data = os.read(key.fd, CHUNK)
                except BlockingIOError:
                    continue
                if not data:
                    sel.unregister(key.fd)
                    reading -= 1
                    continue
                buf = out[key.data]
                if len(buf) + len(data) > limits[key.data]:
                    problem = "overflow:" + key.data
                    break
                buf += data
        if not problem:
            try:
                p.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                problem = "timeout"
    finally:
        # Closing stdin tells the VM's half to stop the script. Give the connection a moment
        # to end by itself (none for a VM that misbehaved), then end it, reap it and close
        # its pipes; interrupts meanwhile are noted, not acted on, so nothing is skipped.
        with deferred_signals() as late:
            sel.close()
            try:
                try:
                    p.stdin.close()
                except OSError:
                    pass
                if p.poll() is None:
                    try:
                        p.wait(timeout=0.5 if problem else 5)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
                for f in (p.stdout, p.stderr):
                    try:
                        f.close()
                    except OSError:
                        pass
        if late:
            raise KeyboardInterrupt
    return bytes(out["stdout"]), bytes(out["stderr"]), problem


def execute(o, transport, header, script, deadline, base, on_tick):
    """Run it and turn whatever came back into (result, exit code)."""
    guest = open(GUEST_PART, "rb").read()
    payload = str(len(guest)).encode() + b"\n" + guest + json.dumps(header).encode() + b"\n" + script
    cap = o["max_output"]
    limits = {"stdout": wire_limit(cap), "stderr": STDERR_WIRE}
    raw, remote, problem = run_remote(transport, payload, deadline, limits, on_tick)
    err = {**base, "status": "error", "exit_code": None, "_remote": remote}
    if problem == "timeout":
        return {**err, "status": "timeout", "kind": "timeout", "message":
                f"no complete answer from the virtual computer within {o['timeout'] + GRACE_S} s; the connection was "
                "closed, which stops the script"}, EXIT_TIMEOUT
    if problem and problem.startswith("overflow:"):
        which = problem.split(":", 1)[1]
        return {**err, "_remote": b"", "kind": "protocol", "message":
                f"the virtual computer sent more on {which} than a reply can hold ({limits[which]:,} bytes); "
                "the connection was closed and nothing from it is shown"}, EXIT_FAILED
    if problem == "spawn":
        return {**err, "kind": "connection", "message": "couldn't start ssh (metacomputer status)"}, EXIT_FAILED
    if not raw:
        return {**err, "kind": "connection", "message": "no answer from the virtual computer (metacomputer status)"}, EXIT_FAILED
    try:
        rep = parse_reply(raw, cap, o["timeout"])
    except Protocol as e:
        return {**err, "kind": "protocol", "message":
                f"the virtual computer's answer was malformed ({e}); nothing from it is shown"}, EXIT_FAILED
    if rep["status"] == "error":
        return {**err, "kind": "guest", "message": GUEST_ERRORS[rep["code"]].format(cwd=base["cwd"]),
                "_remote": rep["message"].encode() + (b"\n" + remote if remote else b"")}, EXIT_FAILED
    res = {**base, "status": rep["status"], "exit_code": rep["exit_code"], "duration_s": rep["duration_s"],
           "desktop": rep["desktop"], "held_open": rep["held_open"],
           "_streams": {k: rep[k] for k in ("stdout", "stderr")}, "notes": []}
    if rep["status"] == "exited":
        code = rep["exit_code"]
        if not o["read_only"] and not rep["desktop"]:
            res["notes"].append("the desktop session isn't up, so this ran without DISPLAY and the session bus "
                                "(metacomputer status)")
    elif rep["status"] == "timeout":
        code = EXIT_TIMEOUT
        res["message"] = (f"stopped after {o['timeout']} s (--timeout); the script and the processes it started in "
                          "its group were ended")
    else:
        code = EXIT_FAILED
        res["message"] = "the connection closed before the script finished; it was stopped"
    if rep["held_open"]:
        res["notes"].append("a program the script left running still had its output open; anything more it prints "
                            "is discarded (start it with: setsid -f APP ... </dev/null >/tmp/APP.log 2>&1)")
    return res, code


# --- printing ----------------------------------------------------------------------
def finish(o, res, exit_code):
    """Print the result (plain or JSON) and return the exit code."""
    streams = res.get("_streams") or {}
    remote = (res.get("_remote") or b"")[-2000:].decode("utf-8", "replace")
    if o.get("json"):
        out = {k: v for k, v in res.items() if not k.startswith("_")}
        out["exit"] = exit_code
        for k in ("stdout", "stderr"):
            st = streams.get(k)
            out[k] = shown_bytes(st, k).decode("utf-8", "replace") if st else ""
            if st:
                out[k + "_bytes"], out[k + "_sha256"], out[k + "_truncated"] = st["bytes"], st["sha256"], st["truncated"]
        if remote:
            out["remote"] = remote
        print(json.dumps(out, indent=1, ensure_ascii=sys.stdout.isatty()))
        return exit_code
    for k, f in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        st = streams.get(k)
        if st:
            data = shown_bytes(st, k)
            f.buffer.write(data if o.get("raw") or not f.isatty() else clean_bytes(data))
            f.buffer.flush()
    lines = ([res["message"]] if res.get("message") else []) + list(res.get("notes") or [])
    if remote and res.get("status") != "exited":
        lines.append("the virtual computer said: " + remote.strip()[-REMOTE_SHOWN:])
    for k in ("stdout", "stderr"):
        st = streams.get(k)
        if st and st["truncated"]:
            lines.append(f"{k} was {st['bytes']:,} bytes; kept its first and last {o['max_output'] // 2:,} (--max-output)")
    lines += [f"warning: {w}" for w in res.get("warnings") or []]
    for line in lines:   # diagnostics are always escaped: they can carry the VM's words
        sys.stderr.write(f"metacomputer sh: {clean(line)}\n")
    return exit_code


# --- main ----------------------------------------------------------------------------
def main(argv):
    transport = []
    if argv[:1] == ["--transport"]:
        try:
            n = int(argv[1])
        except (IndexError, ValueError):
            print("vm_shell.py: --transport N WORD...", file=sys.stderr)
            return EXIT_USAGE
        transport, argv = argv[2:2 + n], argv[2 + n:]
    want_json = "--json" in argv
    try:
        o = parse(argv)
        if o["help"]:
            print(USAGE)
            return 0
        script = read_script(o)
    except Usage as e:
        if want_json:
            print(json.dumps({"status": "usage", "exit": EXIT_USAGE, "message": str(e)}))
        else:
            print(f"metacomputer sh: {clean(str(e))}\n\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    if not transport:
        return finish(o, {"status": "error", "message": "no way to reach the VM (run it as metacomputer sh)"}, EXIT_FAILED)

    res, code = run_guarded(o, transport, script)
    return finish(o, res, code)


def run_guarded(o, transport, script, tool=TOOL, log_args=None, header_extra=None):
    """Run a script in the VM under every rule `metacomputer sh` keeps, and return (result, exit code)
    without printing anything: VM_SHELL and the bounded driver, the one-controller check and claim (unless
    o["read_only"]), the action log (the call signed before anything runs, the result after), the deadline
    and the reply's schema. o needs cwd, timeout, max_output, read_only, override and args (and source and
    file for the default log record). vm_claude.py runs its steps through this too, under its own tool name:
    - log_args(keep_text) gives the call record's arguments, for a caller whose script and arguments
      the default record (call_args) shouldn't hold;
    - header_extra is merged into the request header the VM's half reads. It is never logged or shown:
      vm_claude.py passes a token there ("env"), which the VM's half puts in the script's environment."""
    s = settings()
    user = os.environ.get("MCOMP_GUEST_USER") or "cua"
    cwd = guest_cwd(o["cwd"], user)
    base = {"cwd": cwd, "read_only": o["read_only"], "max_output": o["max_output"], "logged": None}
    warnings = []

    log_on = logging_on(s["shell_log"], s["action_log"])
    if log_on is None:
        return {**base, "status": "refused", "message":
                f"SHELL_LOG={s['shell_log']!r} isn't one of auto, off; refusing to run without knowing "
                "whether it should be logged (fix it in the config)"}, EXIT_REFUSED
    log = Log(s, tool)
    args = log_args(log.keep) if log_args else call_args(o, script, cwd, log.keep)

    def open_log():
        """None, or why the log (required here) couldn't be opened."""
        if not log_on or log.log:
            return None
        try:
            log.open()
        except Exception as e:  # never run unlogged by accident
            return f"the action log is on but couldn't be opened ({type(e).__name__}); not running unlogged"
        base["logged"] = log.name()
        return None

    def refuse(msg):
        if open_log() is None:
            log.refused(args, msg)
            log.close()
        return {**base, "status": "refused", "message": msg}, EXIT_REFUSED

    allowed, why = shell_allowed(s["vm_shell"], s["driver_mode"])
    if allowed is None:
        return refuse(f"{why}; refusing (fix it in the config)")
    if not allowed:
        if s["vm_shell"] == "off":
            return refuse("the shell channel is off (VM_SHELL=off in this virtual computer's config). That's the "
                          "user's setting: use the desktop tools, or ask the user to turn it on. Don't use "
                          "`metacomputer ssh` to get around it.")
        return refuse("the driver is in bounded mode (DRIVER_MODE=bounded), which turns the shell channel off unless "
                      "VM_SHELL=on: the shell isn't limited by the driver's manifest. That's the user's setting; "
                      "ask them if you need it, and don't use `metacomputer ssh` to get around it.")
    if os.environ.get("MCOMP_VM_STATE", "running") != "running":
        return {**base, "status": "error", "kind": "vm_off",
                "message": "the virtual computer is off (metacomputer start)"}, EXIT_FAILED

    # One controller at a time: look for others and claim the computer as one step.
    who = None
    if not o["read_only"]:
        busy = []
        try:
            with owners.lock():
                if not o["override"]:
                    busy = [x for x in owners.others() if x["active"]]
                if not busy:
                    who = owners.claim_for_shell(tool)
        except (OSError, TimeoutError) as e:
            return refuse(f"couldn't record that this session is using the virtual computer ({e}); not running")
        if busy:
            return refuse("the virtual computer is in use by another session:\n" +
                          "\n".join("  - " + owners.describe(x) for x in busy) +
                          "\nOnly one session should control it at a time. Ask the user before taking over; if they "
                          "agree, run again with --override (or run `metacomputer release`). --read-only runs checks "
                          "that change nothing without taking over.")

    def refresh():   # the holder found above: no process table, so it can't stall the run
        try:
            owners.claim_for_shell(tool, who)
        except OSError as e:
            w = f"couldn't refresh this session's claim on the virtual computer ({e.strerror or e})"
            if w not in warnings:
                warnings.append(w)

    err = open_log()
    if err:
        return {**base, "status": "refused", "message": err}, EXIT_REFUSED
    log.call(args)
    if not log.sign_now():
        msg = "the action log couldn't be signed (ACTION_LOG_KEY); not running unsigned"
        log.result(0, {"status": "refused", "kind": "log", "error": True, "message": msg})
        log.close()
        return {**base, "status": "refused", "kind": "log", "message": msg}, EXIT_REFUSED

    header = {"cwd": cwd, "timeout": o["timeout"], "max_output": o["max_output"], "desktop": not o["read_only"],
              "args": o["args"], "script_bytes": len(script)}
    header.update(header_extra or {})
    t0 = time.monotonic()
    deadline = t0 + o["timeout"] + GRACE_S    # starts before ssh does

    # Interrupts (Ctrl-C, SIGTERM, SIGHUP) stop the run while it's in flight; from the moment
    # it ends they're only noted, so logging, signing and printing the result always finish.
    sig_state = {"defer": False}

    def on_signal(_signum, _frame):
        if not sig_state["defer"]:
            raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    res, code = None, EXIT_FAILED
    try:
        try:
            res, code = execute(o, transport, header, script, deadline, base,
                                None if o["read_only"] else refresh)
        finally:
            sig_state["defer"] = True
    except KeyboardInterrupt:
        sig_state["defer"] = True
        res, code = {**base, "status": "cancelled", "kind": "interrupted", "exit_code": None,
                     "message": "interrupted; the script in the VM is stopped too"}, EXIT_INTERRUPTED
    except Exception as e:
        sig_state["defer"] = True
        res, code = {**base, "status": "error", "kind": "internal", "exit_code": None,
                     "message": f"metacomputer sh failed on the Mac ({type(e).__name__})"}, EXIT_FAILED
    finally:
        sig_state["defer"] = True   # the result is logged and signed whatever arrives meanwhile
        if res is None:
            res = {**base, "status": "error", "kind": "internal", "exit_code": None,
                   "message": "metacomputer sh stopped unexpectedly"}
        if log.log:
            log.result(int((time.monotonic() - t0) * 1000), result_fields(res, log.keep))
            if not log.close():
                res = {**res, "status": "error", "kind": "log", "message":
                       (f"the script ended ({res.get('status')}, exit code {res.get('exit_code')}) but its "
                        "action-log record couldn't be signed (ACTION_LOG_KEY); treat this run as unlogged")}
                code = EXIT_FAILED
    if not o["read_only"]:
        refresh()
    if warnings:
        res["warnings"] = warnings
    return res, code

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
