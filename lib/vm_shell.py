#!/usr/bin/env python3
"""`metacomputer sh`: run a bash script in the virtual computer and get its result.

bin/metacomputer runs this as

  vm_shell.py --transport N WORD... [user arguments]

where the N words are the command that reaches the VM (`ssh -T ...`, the
same path, key and account as `metacomputer ssh ops`) and the rest are the
user's. The script goes over SSH's stdin together with the VM's half
(vm_shell_guest.py), so nothing is quoted or expanded on the Mac. See
docs/shell.md for the design and SECURITY.md for what it can reach.

Settings (config/defaults.env, passed in by bin/metacomputer):
  VM_SHELL   auto | on | off     auto = on, except off while DRIVER_MODE=bounded
  SHELL_LOG  auto | off          auto = every run goes in the signed action log
                                 when ACTION_LOG=on; off = never logged
"""
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import owners  # noqa: E402

LAB = owners.LAB
GUEST_PART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vm_shell_guest.py")
# The fixed remote command: read a length, then that much Python (the VM's half), and run it.
BOOTSTRAP = ("python3 -I -c 'import sys;b=sys.stdin.buffer;n=int(b.readline());"
             "exec(compile(b.read(n),\"metacomputer-sh\",\"exec\"))'")

TOOL = "vm_shell"
DEFAULT_TIMEOUT, MAX_TIMEOUT = 120, 3600
DEFAULT_OUTPUT, MIN_OUTPUT, MAX_OUTPUT = 65536, 1024, 1048576
MAX_SCRIPT = 1048576
GRACE_S = 30            # the Mac waits this much longer than the VM's own timeout
CLAIM_EVERY_S = 60      # a long run keeps its claim on the computer fresh

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
  -- ARGS              $1, $2, ... for the script

To leave a program running (a GUI app), start it detached:
  setsid -f APP ARGS </dev/null >/tmp/APP.log 2>&1

Exit status: the script's own; 124 timed out; 125 refused (VM_SHELL, the
bounded driver, another session); 255 couldn't run or finish (VM off, SSH,
no such folder); 2 usage.
Settings: VM_SHELL=auto|on|off, SHELL_LOG=auto|off (metacomputer security)."""


class Usage(Exception):
    pass


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
         "max_output": DEFAULT_OUTPUT, "json": False, "read_only": False, "override": False,
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


def stream(summary, label):
    """(bytes shown, info) for one stream from the VM's summary."""
    head = base64.b64decode(summary.get("head") or "")
    info = {"bytes": summary.get("bytes", 0), "sha256": summary.get("sha256"), "truncated": bool(summary.get("truncated"))}
    if not info["truncated"]:
        return head, info
    head = utf8_cut_end(head)
    tail = utf8_cut_start(base64.b64decode(summary.get("tail") or ""))
    left_out = info["bytes"] - len(head) - len(tail)
    note = (f"\n[metacomputer sh: {left_out:,} bytes of {label} left out here; it was {info['bytes']:,} bytes in all. "
            f"Use --max-output, or write it to a file in the VM and copy it with metacomputer get.]\n").encode()
    return head + note + tail, info


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
    with ACTION_LOG_TEXT=keep, like typed text; the hash and size always."""
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


def result_fields(res, shown, keep_text):
    """The result record's summary of a run (sizes and hashes of the whole streams)."""
    out = {"status": res["status"], "exit_code": res.get("exit_code"), "error": res["status"] != "exited" or res.get("exit_code") != 0,
           "desktop": res.get("desktop")}
    if res.get("message"):
        out["message"] = res["message"][:300]
    for k in ("stdout", "stderr"):
        if k in shown:
            info = dict(shown[k][1])
            if keep_text:
                info["text"] = shown[k][0].decode("utf-8", "replace")
            out[k] = info
    return out


class Log:
    """One action-log file per run, when logging applies (else every method does nothing)."""

    def __init__(self, s):
        self.log = None
        self.keep = s["action_log_text"] == "keep"
        self.s = s

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
            self.log.refused(TOOL, args, reason)

    def call(self, args):
        if self.log:
            self.log.call(1, TOOL, args)

    def result(self, ms, fields):
        if self.log:
            self.log.event("result", id=1, tool=TOOL, ms=ms, result=fields)

    def close(self):
        if self.log:
            self.log.close()


# --- running -----------------------------------------------------------------------
def run_remote(transport, header, script, timeout, on_tick=None):
    """Send the VM's half, the header and the script; wait for the one-line answer.
    Returns (answer dict or None, ssh stderr text, host_timeout)."""
    guest = open(GUEST_PART, "rb").read()
    payload = (str(len(guest)).encode() + b"\n" + guest
               + json.dumps(header).encode() + b"\n" + script)
    try:
        p = subprocess.Popen(transport + [BOOTSTRAP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    except OSError as e:
        return None, str(e), False
    got = {}

    def reader(name, f):
        got[name] = f.read()

    threads = [threading.Thread(target=reader, args=(n, f), daemon=True)
               for n, f in (("out", p.stdout), ("err", p.stderr))]
    for t in threads:
        t.start()
    try:
        p.stdin.write(payload)
        p.stdin.flush()     # stdin stays open: the VM's half stops the script if it closes early
    except (BrokenPipeError, OSError):
        pass
    deadline = time.monotonic() + timeout + GRACE_S
    host_timeout = False
    try:
        while True:
            try:
                p.wait(timeout=max(0.1, min(CLAIM_EVERY_S, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    host_timeout = True
                    break
                if on_tick:
                    on_tick()
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        if p.poll() is None:
            try:
                p.wait(timeout=5 if not host_timeout else 0.5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    for t in threads:
        t.join(timeout=5)
    answer = None
    for line in reversed((got.get("out") or b"").splitlines()):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("metacomputer_sh") == 1:
            answer = d
            break
    return answer, (got.get("err") or b"").decode("utf-8", "replace"), host_timeout


def claim():
    """This session (its Claude Code process, else the calling shell) is acting on the computer."""
    try:
        owners.claim_for_shell(TOOL)
    except OSError:
        pass


def finish(o, res, exit_code):
    """Print the result (plain or JSON) and return the exit code."""
    shown = res.get("_shown") or {}
    if o.get("json"):
        out = {k: v for k, v in res.items() if not k.startswith("_")}
        out["exit"] = exit_code
        for k in ("stdout", "stderr"):
            data, info = shown.get(k, (b"", None))
            out[k] = data.decode("utf-8", "replace")
            if info:
                out[k + "_bytes"] = info["bytes"]
                out[k + "_sha256"] = info["sha256"]
                out[k + "_truncated"] = info["truncated"]
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return exit_code
    for k, f in (("stdout", sys.stdout.buffer), ("stderr", sys.stderr.buffer)):
        data = shown.get(k, (b"", None))[0]
        if data:
            f.write(data)
            f.flush()
    if res.get("message"):
        sys.stderr.write(f"metacomputer sh: {res['message']}\n")
    for k in ("stdout", "stderr"):
        info = shown.get(k, (b"", None))[1]
        if info and info["truncated"]:
            sys.stderr.write(f"metacomputer sh: {k} was {info['bytes']:,} bytes; kept its first and last "
                             f"{o['max_output'] // 2:,} (--max-output)\n")
    return exit_code


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
            print(f"metacomputer sh: {e}\n\n{USAGE}", file=sys.stderr)
        return EXIT_USAGE
    if not transport:
        return finish(o, {"status": "error", "message": "no way to reach the VM (run it as metacomputer sh)"}, EXIT_FAILED)

    s = settings()
    user = os.environ.get("MCOMP_GUEST_USER") or "cua"
    cwd = guest_cwd(o["cwd"], user)
    base = {"cwd": cwd, "read_only": o["read_only"], "max_output": o["max_output"], "logged": None}

    log_on = logging_on(s["shell_log"], s["action_log"])
    if log_on is None:
        return finish(o, {**base, "status": "refused", "message":
                          f"SHELL_LOG={s['shell_log']!r} isn't one of auto, off; refusing to run without knowing "
                          "whether it should be logged (fix it in the config)"}, EXIT_REFUSED)
    log = Log(s)
    args = call_args(o, script, cwd, log.keep)

    def open_log():
        """None, or why the log (required here) couldn't be opened."""
        if not log_on or log.log:
            return None
        try:
            log.open()
        except Exception as e:  # never run unlogged by accident
            return f"the action log is on but couldn't be opened ({e}); not running unlogged"
        base["logged"] = log.name()
        return None

    def refuse(msg):
        if open_log() is None:
            log.refused(args, msg)
            log.close()
        return finish(o, {**base, "status": "refused", "message": msg}, EXIT_REFUSED)

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
        return finish(o, {**base, "status": "error", "message": "the virtual computer is off (metacomputer start)"}, EXIT_FAILED)
    if not o["read_only"] and not o["override"]:
        busy = [x for x in owners.others() if x["active"]]
        if busy:
            return refuse("the virtual computer is in use by another session:\n" +
                          "\n".join("  - " + owners.describe(x) for x in busy) +
                          "\nOnly one session should control it at a time. Ask the user before taking over; if they "
                          "agree, run again with --override (or run `metacomputer release`). --read-only runs checks "
                          "that change nothing without taking over.")

    header = {"cwd": cwd, "timeout": o["timeout"], "max_output": o["max_output"], "desktop": not o["read_only"],
              "args": o["args"], "script_bytes": len(script),
              "name": os.path.basename(o["file"]) if o["source"] == "file" else "metacomputer-sh"}
    err = open_log()
    if err:
        return finish(o, {**base, "status": "refused", "message": err}, EXIT_REFUSED)
    log.call(args)
    if not o["read_only"]:
        claim()
    t0 = time.monotonic()
    interrupted = []

    def on_signal(signum, _frame):
        interrupted.append(signum)
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)
    try:
        answer, ssh_err, host_timeout = run_remote(transport, header, script, o["timeout"],
                                                   None if o["read_only"] else claim)
    except KeyboardInterrupt:
        ms = int((time.monotonic() - t0) * 1000)
        log.result(ms, {"status": "cancelled", "exit_code": None, "error": True,
                        "message": "interrupted on the Mac; the VM stops the script when the connection closes"})
        log.close()
        return finish(o, {**base, "status": "cancelled", "message": "interrupted; the script in the VM is stopped too"},
                      EXIT_INTERRUPTED)
    ms = int((time.monotonic() - t0) * 1000)
    if not o["read_only"]:
        claim()

    if answer is None:
        tail = ssh_err.strip()[-400:]
        if host_timeout:
            res = {**base, "status": "timeout", "exit_code": None, "message":
                   f"no answer from the virtual computer within {o['timeout'] + GRACE_S} s; the connection was closed, "
                   "which stops the script"}
            code = EXIT_TIMEOUT
        else:
            res = {**base, "status": "error", "exit_code": None, "message":
                   "couldn't run it in the virtual computer" + (f": {tail}" if tail else "") +
                   " (metacomputer status)"}
            code = EXIT_FAILED
        log.result(ms, result_fields(res, {}, log.keep))
        log.close()
        return finish(o, res, code)

    shown = {k: stream(answer.get(k) or {}, k) for k in ("stdout", "stderr")} if answer.get("status") != "error" else {}
    res = {**base, "status": answer.get("status"), "exit_code": answer.get("exit_code"),
           "duration_s": answer.get("duration_s"), "desktop": answer.get("desktop"), "_shown": shown}
    if answer.get("cwd"):
        res["cwd"] = answer["cwd"]
    if res["status"] == "exited":
        code = res["exit_code"] if isinstance(res["exit_code"], int) else EXIT_FAILED
        if not o["read_only"] and not answer.get("desktop"):
            res["message"] = ("the desktop session isn't up, so this ran without DISPLAY and the session bus "
                              "(metacomputer status)")
    elif res["status"] == "timeout":
        code = EXIT_TIMEOUT
        res["message"] = (f"stopped after {o['timeout']} s (--timeout); the script and the processes it started in "
                          "its group were ended")
    elif res["status"] == "cancelled":
        code = EXIT_FAILED
        res["message"] = "the connection closed before the script finished; it was stopped"
    else:
        code = EXIT_FAILED
        res["status"] = "error"
        res["message"] = answer.get("message") or "the virtual computer's side failed"
    log.result(ms, result_fields(res, shown, log.keep))
    log.close()
    return finish(o, res, code)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
