#!/usr/bin/env python3
"""Offline checks for `metacomputer sh` (lib/vm_shell.py, lib/vm_shell_guest.py).

No VM needed. Two stand-ins for SSH:
- `bash -c` on this Mac, so the real VM half runs locally;
- a fake, hostile VM (FAKE_VM below) that floods stdout or stderr, sends one
  endless line, stops reading stdin, holds the connection without answering,
  or sends crafted answers, malformed or not.
Run:
    python3 tests/test_vm_shell.py
"""
import hashlib
import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import actionlog  # noqa: E402
import owners  # noqa: E402
import vm_shell  # noqa: E402
import vm_shell_guest  # noqa: E402

SH = os.path.join(LAB, "lib", "vm_shell.py")
results = []

# A VM that misbehaves. The transport's last argument (the bootstrap) is ignored.
FAKE_VM = r'''
import json, os, sys, time
mode = sys.argv[1]
if os.environ.get("FAKE_PIDFILE"):
    open(os.environ["FAKE_PIDFILE"], "w").write(str(os.getpid()))
def read_request():
    b = sys.stdin.buffer
    n = int(b.readline()); b.read(n)
    hdr = json.loads(b.readline()); b.read(hdr["script_bytes"])
try:
    if mode == "flood-stdout":
        while True:
            sys.stdout.buffer.write(b"A" * 65536); sys.stdout.flush()
    elif mode == "flood-stderr":
        while True:
            sys.stderr.buffer.write(b"E" * 65536); sys.stderr.flush()
    elif mode == "huge-line":
        sys.stdout.buffer.write(b'{"metacomputer_sh":1,"status":"exited","pad":"')
        while True:
            sys.stdout.buffer.write(b"A" * 65536); sys.stdout.flush()
    elif mode == "deaf":
        time.sleep(3600)
    elif mode == "stubborn":   # deaf, and ignores SIGTERM and SIGINT: only SIGKILL ends it
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        time.sleep(3600)
    elif mode == "silent":
        read_request(); time.sleep(3600)
    elif mode == "reply":
        read_request()
        if os.environ.get("FAKE_STDERR"):
            sys.stderr.buffer.write(os.environ["FAKE_STDERR"].encode()); sys.stderr.flush()
        for p in filter(None, os.environ.get("FAKE_DELETE", "").split(":")):
            os.remove(p)
        if os.environ.get("FAKE_CHMOD"):
            os.chmod(os.environ["FAKE_CHMOD"], 0o500)
        sys.stdout.buffer.write(open(os.environ["FAKE_REPLY"], "rb").read()); sys.stdout.flush()
except BrokenPipeError:
    pass
'''


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""), flush=True)


def usage_error(argv):
    try:
        vm_shell.parse(argv)
    except vm_shell.Usage as e:
        return str(e)
    return None


def capture(data, cap):
    c = vm_shell_guest.Capture(cap)
    for i in range(0, len(data), 7000):   # in uneven pieces, as a pipe delivers them
        c.add(data[i:i + 7000])
    return c.summary()


def reply(cap=65536, out=b"hello\n", err=b"", **over):
    d = {"metacomputer_sh": 1, "status": "exited", "exit_code": 0, "duration_s": 0.25, "desktop": True,
         "held_open": False, "stdout": capture(out, cap), "stderr": capture(err, cap)}
    d.update(over)
    return (json.dumps(d) + "\n").encode()


def protocol_error(raw, cap=65536):
    try:
        vm_shell.parse_reply(raw, cap, 120)
    except vm_shell.Protocol as e:
        return str(e)
    return None


# --- pure functions ---------------------------------------------------------------
def unit_checks():
    p = vm_shell.parse
    o = p(["-c", "echo hi"])
    check("parse: -c", o["source"] == "command" and o["command"] == "echo hi" and o["timeout"] == 120
          and o["max_output"] == 65536 and not o["json"] and not o["raw"])
    o = p(["--file", "x.sh", "--cwd", "/tmp", "--timeout=5", "--max-output", "2048", "--json", "--raw", "--", "a", "-c", "--json"])
    check("parse: --file, options (both forms), --raw, arguments after --",
          o["source"] == "file" and o["file"] == "x.sh" and o["cwd"] == "/tmp" and o["timeout"] == 5
          and o["max_output"] == 2048 and o["json"] and o["raw"] and o["args"] == ["a", "-c", "--json"], o)
    o = p(["-", "--read-only"])
    check("parse: - and --read-only", o["source"] == "stdin" and o["read_only"])
    check("parse: a -c script may start with a dash", p(["-c", "-x"])["command"] == "-x")
    bad = {"no script": [], "two scripts": ["-c", "a", "-"], "unknown option": ["-c", "a", "--frob"],
           "timeout 0": ["-c", "a", "--timeout", "0"], "timeout too long": ["-c", "a", "--timeout", "3601"],
           "timeout not a number": ["-c", "a", "--timeout", "soon"], "output cap too small": ["-c", "a", "--max-output", "10"],
           "output cap too big": ["-c", "a", "--max-output", "9999999"], "-c without a value": ["-c"],
           "--read-only with --override": ["-c", "a", "--read-only", "--override"]}
    errs = {k: usage_error(v) for k, v in bad.items()}
    check("parse: usage errors", all(errs.values()), [k for k, v in errs.items() if not v])

    g = vm_shell.guest_cwd
    check("cwd: ~, ~/x, relative and absolute are guest paths",
          (g(None, "user"), g("~", "user"), g("~/a b", "user"), g("x/y", "user"), g("/tmp", "user"))
          == ("/home/user", "/home/user", "/home/user/a b", "/home/user/x/y", "/tmp"))

    a = vm_shell.shell_allowed
    check("VM_SHELL: auto is on in standard mode, off in bounded mode",
          a("auto", "standard")[0] is True and a("auto", "bounded")[0] is False)
    check("VM_SHELL: on is on even when bounded; off is off", a("on", "bounded")[0] is True and a("off", "standard")[0] is False)
    check("VM_SHELL: an unknown value is neither", a("yes", "standard")[0] is None)
    lo = vm_shell.logging_on
    check("SHELL_LOG: auto follows ACTION_LOG; off never logs; unknown is neither",
          (lo("auto", "on"), lo("auto", "off"), lo("off", "on"), lo("always", "on")) == (True, False, False, None))
    d = vm_shell.describe_settings({"vm_shell": "auto", "shell_log": "auto", "action_log": "on",
                                    "action_log_text": "keep", "driver_mode": "standard"})
    check("security summary", d["effective"] == "on" and d["logged"] and d["text"] == "keep" and not d["problems"], d)

    # the VM's capture of a stream, and the Mac's check of it
    data = bytes(range(256)) * 10
    s = capture(data, 4096)
    check("capture: under the cap comes back whole", not s["truncated"] and s["bytes"] == len(data)
          and s["sha256"] == hashlib.sha256(data).hexdigest())
    check("capture: exactly the cap is not truncated", not capture(b"x" * 4096, 4096)["truncated"])
    data = os.urandom(3000) + b"MIDDLE" + os.urandom(300000)
    s = capture(data, 2048)
    st = vm_shell.check_stream(s, 2048, "stdout")
    check("capture: over the cap keeps the first and last halves, hashes all of it",
          st["truncated"] and st["head"] == data[:1024] and st["tail"] == data[-1024:] and st["bytes"] == len(data)
          and st["sha256"] == hashlib.sha256(data).hexdigest())
    st = vm_shell.check_stream(capture(b"a" * 2050, 2048), 2048, "stdout")
    check("capture: just over the cap", st["truncated"] and st["tail"] == b"a" * 1024)
    c = vm_shell_guest.Capture(65536)
    for _ in range(200):
        c.add(os.urandom(65536))
    check("capture: memory stays bounded (13 MB in, at most 1.5 times the cap held)",
          len(c.head) + len(c.tail) <= 65536 + 32768 and c.total == 200 * 65536)
    shown = vm_shell.shown_bytes(vm_shell.check_stream(capture(("é" * 3000).encode(), 2048), 2048, "stdout"), "stdout")
    try:
        ok = "left out here" in shown.decode("utf-8")
    except UnicodeDecodeError:
        ok = False
    check("display: cuts fall on UTF-8 character boundaries", ok)
    t = vm_shell.clean("a\tb\nc\x1b]52;c;SGk=\x07\r‮⁦\x9b\x7f")
    check("display: control characters, C1, DEL and bidi controls are escaped; tab and newline kept",
          t == "a\tb\nc\\x1b]52;c;SGk=\\x07\\x0d\\u202e\\u2066\\x9b\\x7f", repr(t))

    # the reply schema
    check("schema: a good answer passes", protocol_error(reply()) is None)
    long_one = json.loads(reply(cap=2048, out=b"x" * 5000))
    check("schema: a truncated answer passes", protocol_error((json.dumps(long_one) + "\n").encode(), cap=2048) is None)
    good = json.loads(reply())
    nan = reply(duration_s=float("nan"))
    variants = {
        "a stream that's a number": reply(stdout=1),
        "exit code 256": reply(exit_code=256),
        "exit code -1": reply(exit_code=-1),
        "exit code true": reply(exit_code=True),
        "exit code as text": reply(exit_code="0"),
        "an exit code on a timeout": reply(status="timeout", exit_code=0),
        "an unknown status": reply(status="fine"),
        "NaN duration": nan,
        "a huge duration": reply(duration_s=1e12),
        "desktop as text": reply(desktop="yes"),
        "held_open missing": (json.dumps({k: v for k, v in good.items() if k != "held_open"}) + "\n").encode(),
        "an extra field": reply(note="hi"),
        "metacomputer_sh true": reply(metacomputer_sh=True),
        "sha256 not hex": reply(stdout={**good["stdout"], "sha256": "\x1b[2J" + "0" * 60}),
        "head not base64": reply(stdout={**good["stdout"], "head": "!!!!!!!!"}),
        "head longer than its size": reply(stdout={**good["stdout"], "bytes": 2}),
        "over the cap, not truncated": reply(stdout={**good["stdout"], "bytes": 70000}),
        "truncated within the cap": reply(stdout={**good["stdout"], "truncated": True, "tail": ""}),
        "a truncated stream's head of the wrong size": reply(cap=2048, out=b"x" * 5000, stdout={
            **long_one["stdout"], "head": long_one["stdout"]["head"][:-8]}),
        "a key twice": reply().replace(b'"desktop": true', b'"desktop": true, "desktop": false'),
        "two lines": reply() + reply(),
        "no final newline": reply()[:-1],
        "not JSON": b"hello\n",
        "a JSON list": b"[1, 2]\n",
        "deep nesting": b"[" * 200000 + b"\n",
        "not UTF-8": b'{"metacomputer_sh": 1, "status": "\xff"}\n',
        "an unknown error code": b'{"metacomputer_sh": 1, "status": "error", "code": "oops"}\n',
        "an over-long error message": (json.dumps({"metacomputer_sh": 1, "status": "error", "code": "internal",
                                                   "message": "x" * 5000}) + "\n").encode(),
    }
    errs = {k: protocol_error(v, cap=2048 if "truncated stream" in k else 65536) for k, v in variants.items()}
    check(f"schema: {len(variants)} malformed answers are all rejected", all(errs.values()),
          [k for k, v in errs.items() if not v])
    leaked = [k for k, v in errs.items() if v and ("\x1b" in v or "secret" in v or "oops" in v)]
    check("schema: rejection messages are fixed text, never the VM's", not leaked, leaked)
    check("wire limit: a full answer fits, with room only for the JSON around it",
          len(reply(cap=2048, out=os.urandom(5000), err=os.urandom(5000))) <= vm_shell.wire_limit(2048) < 2 * 2048 * 4 / 3 + 20000)


# --- runs ----------------------------------------------------------------------------
def env_for(td, **extra):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MCOMP_") or k.startswith("FAKE_"):
            env.pop(k)
    env.update(MCOMP_RUN_DIR=os.path.join(td, "run"), MCOMP_ACTION_LOG_DIR=os.path.join(td, "actions"),
               MCOMP_ACTION_LOG_KEY=os.path.join(td, "keys", "action-log"), MCOMP_VM_NAME="testvm",
               MCOMP_GUEST_USER="cua", XDG_RUNTIME_DIR=os.path.join(td, "xdg"),
               MCOMP_SSH_PORT="1",   # a port no VM uses: real driver connections on this Mac aren't this "VM"'s
               MCOMP_SESSIONS_DIR=os.path.join(td, "sessions"))   # lib/sessionvm.py's records: never the account's
    env.pop("DISPLAY", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def cmd(td, args, fake=None):
    work = os.path.join(td, "work")
    transport = ["2", "bash", "-c"] if fake is None else ["3", sys.executable, os.path.join(td, "fake_vm.py"), fake]
    return [sys.executable, SH, "--transport", *transport, "--cwd", work, *args]


def sh(td, *args, stdin=None, timeout=90, fake=None, **env):
    return subprocess.run(cmd(td, args, fake), input=stdin, capture_output=True, text=True, timeout=timeout,
                          env=env_for(td, **env))


def shb(td, *args, fake=None, **env):
    return subprocess.run(cmd(td, args, fake), capture_output=True, timeout=90, env=env_for(td, **env))


def js(r):
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def log_files(td):
    return actionlog.files(os.path.join(td, "actions"))


def records(path):
    return [json.loads(x) for x in open(path)]


def verifies(td, path, key=None):
    return actionlog.verify_file(path, os.path.join(td, "actions"), (key or os.path.join(td, "keys", "action-log")) + ".pub")


def pgrep(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()


def gone(pidfile):
    try:
        os.kill(int(open(pidfile).read()), 0)
        return False
    except (ProcessLookupError, ValueError, OSError):
        return True


def run_checks(td):
    os.makedirs(os.path.join(td, "work"))
    r = sh(td, "-c", 'echo out; echo err >&2; exit 7')
    check("exit status is the script's, streams kept apart", r.returncode == 7 and r.stdout == "out\n"
          and "err\n" in r.stderr, (r.returncode, r.stdout, r.stderr))
    r = sh(td, "-c", 'echo "$1|$2|$(pwd)"', "--", "one", "two words")
    check("arguments after -- are $1, $2; --cwd is the folder", r.stdout.strip() ==
          "one|two words|" + os.path.realpath(os.path.join(td, "work")), r.stdout)
    r = sh(td, "-", stdin='x=$((6*7))\necho "$x"\nread -r line || echo "stdin empty"\n')
    check("script on stdin; its own stdin is empty", r.stdout == "42\nstdin empty\n", r.stdout + r.stderr)
    for name in ("stdout", ".stdout", "stderr", "script"):
        path = os.path.join(td, name)
        open(path, "w").write(f'echo "ran {name} as $(basename "$0"), folder holds: $(ls -A "$(dirname "$0")")"\n')
        r = sh(td, "--file", path)
        ok = r.returncode == 0 and r.stdout == f"ran {name} as script, folder holds: script\n"
        if not ok:
            break
    check("--file: any name runs from one fixed path, alone in its folder (stdout, .stdout, stderr, script)", ok,
          r.stdout + r.stderr)
    r = sh(td, "--json", "-c", 'printf "%s" "$1"', "--", "data")
    j = js(r)
    check("--json gives the result as data", j.get("status") == "exited" and j.get("exit") == 0 and j.get("exit_code") == 0
          and j.get("stdout") == "data" and j.get("stdout_bytes") == 4
          and j.get("stdout_sha256") == hashlib.sha256(b"data").hexdigest() and j.get("stdout_truncated") is False
          and isinstance(j.get("duration_s"), float), j)
    r = sh(td, "--json", "--max-output", "1024", "-c", "head -c 5000 /dev/zero | tr '\\0' a")
    j = js(r)
    check("--max-output caps a stream and says so", j.get("stdout_truncated") and j.get("stdout_bytes") == 5000
          and "left out here" in j.get("stdout", "") and j.get("exit") == 0, {k: j.get(k) for k in ("stdout_bytes", "exit")})
    r = sh(td, "-c", "head -c 5000 /dev/zero | tr '\\0' a", "--max-output", "1024")
    check("plain output notes the cap on stderr", "stdout was 5,000 bytes" in r.stderr, r.stderr)

    xdg = os.path.join(td, "xdg", "metacomputer")
    os.makedirs(xdg)
    open(os.path.join(xdg, "session.env"), "w").write("DISPLAY=:77\nDBUS_SESSION_BUS_ADDRESS=unix:path=/nowhere\n")
    r = sh(td, "--json", "-c", 'echo "$DISPLAY $DBUS_SESSION_BUS_ADDRESS"')
    check("the desktop session's environment is loaded", js(r).get("stdout") == ":77 unix:path=/nowhere\n"
          and js(r).get("desktop") is True, r.stdout)
    r = sh(td, "--json", "--read-only", "-c", 'echo "${DISPLAY:-none} ${DBUS_SESSION_BUS_ADDRESS:-none}"')
    check("--read-only runs without it", js(r).get("stdout") == "none none\n" and js(r).get("desktop") is False, r.stdout)
    os.remove(os.path.join(xdg, "session.env"))
    r = sh(td, "-c", "true")
    check("without a desktop session it runs and says so", r.returncode == 0 and "desktop session isn't up" in r.stderr, r.stderr)

    # programs left running, timeouts, interruptions (the real VM half)
    t = time.monotonic()
    r = sh(td, "--json", "-c", "sleep 31.7 & echo started")
    el = time.monotonic() - t
    check("a program left running with & doesn't hold the result back (the output drain is short)",
          r.returncode == 0 and el < 4 and js(r).get("stdout") == "started\n" and js(r).get("held_open") is True, el)
    check("... and keeps running after a normal end", bool(pgrep("sleep 31.7")))
    subprocess.run(["pkill", "-f", "sleep 31.7"])
    ticker = os.path.join(td, "ticker.pid")
    r = sh(td, "-c", f"sh -c 'echo $$ > {ticker}; while :; do echo tick; sleep 0.1; done' & echo started")
    time.sleep(1.5)
    alive = not gone(ticker)
    check("a program that keeps printing after the run isn't killed by a closed pipe", r.returncode == 0 and alive
          and "still had its output open" in r.stderr, (alive, r.stderr[-200:]))
    try:
        os.kill(int(open(ticker).read()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    t = time.monotonic()
    r = sh(td, "--json", "-c", "head -c 30000000 /dev/zero; echo done >&2")
    j, el = js(r), time.monotonic() - t
    check("30 MB of output: counted and hashed in full, only 64 KB kept, nothing written to disk",
          j.get("stdout_bytes") == 30000000 and j.get("stdout_sha256") == hashlib.sha256(b"\0" * 30000000).hexdigest()
          and j.get("stdout_truncated") and len(j.get("stdout", "")) < 70000 and j.get("stderr") == "done\n", el)
    r = sh(td, "-c", 'd=$(dirname "$0"); rm -f "$d/stdout" "$d/stderr"; ln -s /dev/zero "$d/stdout"; '
                     'mkfifo "$d/stderr"; echo swapped', timeout=30)
    check("planting a FIFO or a /dev/zero link in the run folder changes nothing", r.returncode == 0 and r.stdout == "swapped\n",
          r.stdout + r.stderr)
    t = time.monotonic()
    r = sh(td, "--timeout", "1", "-c", "sleep 32.1 & sleep 32.2; echo never")
    el = time.monotonic() - t
    check("--timeout: exit 124, the script's group is ended", r.returncode == 124 and "never" not in r.stdout and el < 8
          and not pgrep("sleep 32.1") and not pgrep("sleep 32.2"), (r.returncode, el, r.stderr))
    t = time.monotonic()
    r = sh(td, "--json", "--timeout", "1", "-c", "yes")
    j, el = js(r), time.monotonic() - t
    check("--timeout on a command that floods its output: 124, bounded, quick", j.get("status") == "timeout"
          and j.get("exit") == 124 and j.get("exit_code") is None and j.get("stdout_bytes", 0) > 65536 and el < 8, el)
    marker = os.path.join(td, "cleaned-up")
    r = sh(td, "--timeout", "1", "-c", f"(trap 'sleep 1; echo cleaned > {marker}; exit' TERM; "
                                       "while :; do sleep 0.1; done) & wait")
    check("SIGTERM grace lasts until the whole group is gone, not just bash", r.returncode == 124 and os.path.exists(marker),
          r.stderr[-200:])
    p = subprocess.Popen(cmd(td, ["-c", "sleep 33.4; echo never"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env_for(td))
    time.sleep(1.5)
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=20)
    time.sleep(0.5)
    check("interrupted on the Mac: exit 130, the script is stopped too", p.returncode == 130 and not pgrep("sleep 33.4"),
          p.returncode)

    # settings
    marker = os.path.join(td, "ran")
    r = sh(td, "-c", f"touch {marker}", MCOMP_VM_SHELL="off")
    check("VM_SHELL=off: refused (125), nothing runs", r.returncode == 125 and not os.path.exists(marker)
          and "VM_SHELL=off" in r.stderr, r.stderr)
    r = sh(td, "-c", f"touch {marker}", MCOMP_DRIVER_MODE="bounded")
    check("bounded driver with VM_SHELL=auto: refused", r.returncode == 125 and not os.path.exists(marker), r.stderr)
    r = sh(td, "-c", "echo ok", MCOMP_DRIVER_MODE="bounded", MCOMP_VM_SHELL="on")
    check("bounded driver with VM_SHELL=on: runs", r.returncode == 0 and r.stdout == "ok\n", r.stderr)
    r = sh(td, "-c", "echo ok", MCOMP_VM_SHELL="yes")
    check("an unknown VM_SHELL value is refused", r.returncode == 125, r.stderr)
    r = sh(td, "-c", "echo ok", MCOMP_SHELL_LOG="always", MCOMP_ACTION_LOG="on")
    check("an unknown SHELL_LOG value is refused", r.returncode == 125 and "SHELL_LOG" in r.stderr, r.stderr)
    r = sh(td, "-c", "echo ok", MCOMP_VM_STATE="off")
    check("VM off: exit 255 with a hint", r.returncode == 255 and "metacomputer start" in r.stderr, r.stderr)
    r = sh(td, "-c", "echo ok", "--cwd", "/no/such/folder")
    check("a missing --cwd: exit 255", r.returncode == 255 and "no such folder" in r.stderr, r.stderr)
    r = sh(td, "--frob")
    check("usage error: exit 2", r.returncode == 2 and "usage:" in r.stderr)
    r = sh(td, "--json", "--frob")
    check("usage error in JSON", js(r).get("status") == "usage" and js(r).get("exit") == 2)


def hostile_checks(td):
    open(os.path.join(td, "fake_vm.py"), "w").write(FAKE_VM)
    pidfile = os.path.join(td, "fake.pid")
    big = os.path.join(td, "big.sh")
    open(big, "w").write(("# padding so the request can't fit in a pipe's buffer\n" * 8000) + "echo hi\n")

    for mode, what in (("flood-stdout", "endless stdout"), ("flood-stderr", "endless stderr"), ("huge-line", "one endless line")):
        t = time.monotonic()
        r = sh(td, "--json", "-c", "true", fake=mode, FAKE_PIDFILE=pidfile)
        j, el = js(r), time.monotonic() - t
        check(f"hostile VM, {what}: cut off at the wire limit, exit 255, the connection closed",
              r.returncode == 255 and j.get("kind") == "protocol" and el < 10 and gone(pidfile)
              and j.get("stdout") == "" and len(r.stdout) < 20000, (r.returncode, j.get("kind"), el))
    for mode, what in (("deaf", "stops reading stdin"), ("silent", "never answers")):
        t = time.monotonic()
        r = sh(td, "--json", "--timeout", "1", "--file", big, fake=mode, FAKE_PIDFILE=pidfile, MCOMP_SH_GRACE_S="2")
        j, el = js(r), time.monotonic() - t
        check(f"hostile VM {what}: given up at the deadline (124), the connection closed",
              r.returncode == 124 and j.get("status") == "timeout" and 2.5 < el < 9 and gone(pidfile), (r.returncode, el))
    p = subprocess.Popen(cmd(td, ["--file", big], fake="deaf"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env_for(td, FAKE_PIDFILE=pidfile))
    time.sleep(1.5)
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=20)
    check("interrupted while the VM won't read: exit 130, the connection closed", p.returncode == 130 and gone(pidfile),
          p.returncode)

    def fake_reply(raw, *args, **env):
        path = os.path.join(td, "reply.bin")
        open(path, "wb").write(raw)
        return sh(td, "--json", "-c", "true", *args, fake="reply", FAKE_REPLY=path, **env)

    r = fake_reply(reply(out=b"faked\n", exit_code=3))
    check("a well-formed answer from the fake VM is shown", r.returncode == 3 and js(r).get("stdout") == "faked\n", r.stdout)
    n0 = len(log_files(td))
    bad = {"a stream that's a number": reply(stdout=1), "exit code 256": reply(exit_code=256),
           "not JSON": b"\x1b[2J secret\n", "two lines": reply() + reply(),
           "a key twice": reply().replace(b'"desktop": true', b'"desktop": true, "desktop": false')}
    fails = {}
    for name, raw in bad.items():
        r = fake_reply(raw, MCOMP_ACTION_LOG="on")
        j = js(r)
        recs = records(log_files(td)[-1]) if len(log_files(td)) > n0 else []
        n0 = len(log_files(td))
        res = next((x for x in recs if x["type"] == "result"), {}).get("result") or {}
        ok = (r.returncode == 255 and j.get("kind") == "protocol" and j.get("stdout") == "" and "secret" not in r.stdout
              and res.get("kind") == "protocol" and recs and recs[-1].get("final") and verifies(td, log_files(td)[-1])["ok"])
        if not ok:
            fails[name] = (r.returncode, j.get("kind"), res)
    check("malformed answers end to end: exit 255, nothing shown, logged as a protocol error and signed", not fails, fails)
    r = fake_reply(b"")
    check("no answer at all: a connection error (255)", r.returncode == 255 and js(r).get("kind") == "connection", r.stdout)

    # control characters reach a terminal only escaped
    path = os.path.join(td, "reply.bin")
    nasty = b"\x1b]52;c;SGk=\x07\x1b[2Jforged\r\n\xe2\x80\xae"
    open(path, "wb").write(reply(out=nasty))

    def on_tty(*args):
        m, s = pty.openpty()
        p = subprocess.Popen(cmd(td, args, "reply"), stdout=s, stderr=subprocess.PIPE, env=env_for(td, FAKE_REPLY=path))
        os.close(s)
        out = b""
        while True:
            try:
                chunk = os.read(m, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        p.wait()
        os.close(m)
        return out

    out = on_tty("-c", "true")
    check("on a terminal, control characters in the output are shown escaped", b"\x1b" not in out and b"\x07" not in out
          and b"\xe2\x80\xae" not in out and b"\\x1b]52;c;SGk=\\x07" in out and b"\\u202e" in out, out[:200])
    out = on_tty("--raw", "-c", "true")
    check("--raw prints them as they came", b"\x1b]52;c;SGk=\x07" in out, out[:100])
    r = shb(td, "-c", "true", fake="reply", FAKE_REPLY=path)
    check("to a pipe (not a terminal) the output is exact", r.stdout == nasty, r.stdout[:100])
    err_reply = (json.dumps({"metacomputer_sh": 1, "status": "error", "code": "internal",
                             "message": "secret-guest-diag \x1b[31m"}) + "\n").encode()
    open(path, "wb").write(err_reply)
    r = shb(td, "-c", "true", fake="reply", FAKE_REPLY=path, FAKE_STDERR="secret-ssh-diag \x1b]0;title\x07")
    check("the VM's diagnostics are always escaped in messages", r.returncode == 255 and b"\x1b" not in r.stderr
          and b"secret-guest-diag \\x1b[31m" in r.stderr, r.stderr[-300:])

    # what the log keeps of the VM's own words
    r = fake_reply(err_reply, MCOMP_ACTION_LOG="on", FAKE_STDERR="secret-ssh-diag")
    raw = open(log_files(td)[-1]).read()
    res = next((x for x in map(json.loads, raw.splitlines()) if x["type"] == "result"), {}).get("result") or {}
    check("ACTION_LOG_TEXT=redact: the VM's diagnostics only as size and hash; the message is the Mac's",
          "secret" not in raw and res.get("kind") == "guest" and (res.get("remote") or {}).get("bytes", 0) > 30
          and len((res.get("remote") or {}).get("sha256", "")) == 64 and res.get("message") == "the virtual computer's side failed",
          res)
    r = fake_reply(err_reply, MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_TEXT="keep", FAKE_STDERR="secret-ssh-diag")
    raw = open(log_files(td)[-1]).read()
    check("ACTION_LOG_TEXT=keep: they're kept", "secret-guest-diag" in raw and "secret-ssh-diag" in raw)

    # signing
    nokey = os.path.join(td, "agentkey")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", nokey], check=True)
    os.remove(nokey)    # only the .pub is left, and no agent holds the key
    marker = os.path.join(td, "ran-unsigned")
    sign_logs = os.path.join(td, "sign-actions")   # signed with other keys: kept apart from the main check
    r = sh(td, "-c", f"touch {marker}", MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_KEY=nokey + ".pub",
           MCOMP_ACTION_LOG_DIR=sign_logs, SSH_AUTH_SOCK=os.path.join(td, "no-agent"))
    check("a log that can't be signed at the start: refused (125), nothing runs", r.returncode == 125
          and not os.path.exists(marker) and "couldn't be signed" in r.stderr, r.stderr[-300:])
    key2 = os.path.join(td, "keys2", "action-log")
    open(path, "wb").write(reply(out=b"ran\n"))
    r = sh(td, "--json", "-c", "true", fake="reply", FAKE_REPLY=path, FAKE_DELETE=key2,
           MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_KEY=key2, MCOMP_ACTION_LOG_DIR=sign_logs)
    j = js(r)
    v = actionlog.verify_file(actionlog.files(sign_logs)[-1], sign_logs, key2 + ".pub")
    check("the final signature fails: exit 255 and says the run is unlogged; the output is still shown",
          r.returncode == 255 and j.get("kind") == "log" and j.get("stdout") == "ran\n" and "unlogged" in j.get("message", "")
          and v["ok"] and v["signed_through"] >= 1 and not v["final"], (r.returncode, j.get("kind"), j.get("message"), v))


def owner_checks(td):
    run_dir = os.path.join(td, "run", "owners")
    os.makedirs(run_dir, exist_ok=True)
    marker = os.path.join(td, "ran2")
    other = subprocess.Popen(["sleep", "60"])
    claim = os.path.join(run_dir, f"{other.pid}.json")
    try:
        now = time.time()
        json.dump({"bridge_pid": other.pid, "claude_pid": None, "cwd": "/another/project", "started": now,
                   "last_call": now, "last_tool": "click"}, open(claim, "w"))
        r = sh(td, "-c", f"touch {marker}")
        check("another session acting: refused (125), nothing runs", r.returncode == 125 and not os.path.exists(marker)
              and "/another/project" in r.stderr, r.stderr)
        before = set(os.listdir(run_dir))
        r = sh(td, "--read-only", "-c", "echo looked")
        check("--read-only runs anyway and doesn't claim the computer", r.returncode == 0
              and set(os.listdir(run_dir)) == before, sorted(os.listdir(run_dir)))
        r = sh(td, "--override", "-c", "echo took over")
        mine = [json.load(open(os.path.join(run_dir, f))) for f in os.listdir(run_dir)
                if f.endswith(".json") and f != f"{other.pid}.json"]
        check("--override runs, and the run claims the computer for this session", r.returncode == 0 and len(mine) == 1
              and mine[0].get("last_tool") == "vm_shell", mine)
    finally:
        other.kill()
        other.wait()
        if os.path.exists(claim):
            os.remove(claim)

    holder = ("import os, sys, time; sys.path.insert(0, sys.argv[1]); import owners\n"
              "with owners.lock():\n    open(sys.argv[2], 'w').close(); time.sleep(1.5)\n")
    flag = os.path.join(td, "locked")
    p = subprocess.Popen([sys.executable, "-c", holder, os.path.join(LAB, "lib"), flag], env=env_for(td))
    while not os.path.exists(flag):
        time.sleep(0.02)
    t = time.monotonic()
    r = sh(td, "-c", "true")
    el = time.monotonic() - t
    p.wait()
    check("the check-and-claim waits for the owners lock (one step for concurrent starts)", r.returncode == 0 and el > 1.0, el)

    errors = []

    def writer(i):
        try:
            for _ in range(25):
                owners.write_claim(4242, None, "/x", f"t{i}", 0)
        except Exception as e:
            errors.append(repr(e))

    os.environ["MCOMP_RUN_DIR"] = os.path.join(td, "run2")
    try:
        import importlib
        importlib.reload(owners)
        ts = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t_ in ts:
            t_.start()
        for t_ in ts:
            t_.join()
        d = os.path.join(td, "run2", "owners")
        leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        check("concurrent claim writes for one holder don't collide", not errors and not leftovers
              and json.load(open(os.path.join(d, "4242.json"))).get("bridge_pid") == 4242, errors[:2] + leftovers)
    finally:
        os.environ.pop("MCOMP_RUN_DIR", None)
        importlib.reload(owners)

    open(os.path.join(run_dir, ".lock"), "a").close()
    os.chmod(run_dir, 0o500)
    try:
        r = sh(td, "-c", f"touch {marker}")
        check("the claim can't be written: refused (125), nothing runs", r.returncode == 125 and not os.path.exists(marker)
              and "couldn't record" in r.stderr, r.stderr[-300:])
    finally:
        os.chmod(run_dir, 0o700)
    path = os.path.join(td, "reply.bin")
    open(path, "wb").write(reply(out=b"ok\n"))
    try:
        r = sh(td, "--json", "-c", "true", fake="reply", FAKE_REPLY=path, FAKE_CHMOD=run_dir)
        w = js(r).get("warnings") or []
        check("the claim can't be refreshed: reported as a warning", r.returncode == 0 and any("refresh" in x for x in w), w)
    finally:
        os.chmod(run_dir, 0o700)


def log_checks(td):
    n0 = len(log_files(td))
    sh(td, "-c", "echo x", MCOMP_ACTION_LOG="off")
    sh(td, "-c", "echo x", MCOMP_ACTION_LOG="on", MCOMP_SHELL_LOG="off")
    check("no log with ACTION_LOG=off, nor with SHELL_LOG=off", len(log_files(td)) == n0)
    body = 'echo secret-out-marker; echo secret-err-marker >&2; exit 3'
    r = sh(td, "--json", "-c", body, "--", "secret-arg-marker", MCOMP_ACTION_LOG="on")
    files = log_files(td)
    check("SHELL_LOG=auto with ACTION_LOG=on: one log file per run", len(files) == n0 + 1 and js(r).get("logged"), js(r).get("logged"))
    if len(files) == n0 + 1:
        raw = open(files[-1]).read()
        recs = [json.loads(x) for x in raw.splitlines()]
        kinds = [x["type"] for x in recs]
        call = next((x for x in recs if x["type"] == "call"), {})
        res = (next((x for x in recs if x["type"] == "result"), {}).get("result")) or {}
        check("records: start, a vm_shell call, a signature before running, its result, end, final signature",
              kinds == ["start", "call", "checkpoint", "result", "end", "checkpoint"] and call.get("tool") == "vm_shell"
              and recs[-1].get("final") and not recs[2].get("final") and (recs[0].get("session") or {}).get("via") == "shell", kinds)
        a = call.get("args") or {}
        check("call: script hash and size, cwd; script and its arguments redacted",
              a.get("script_sha256") == hashlib.sha256(body.encode()).hexdigest() and a.get("script_bytes") == len(body)
              and (a.get("script") or {}).get("redacted") and (a.get("script_args") or {}).get("count") == 1 and a.get("cwd"), a)
        check("result: exit code, output sizes and hashes",
              res.get("exit_code") == 3 and res.get("error") is True
              and (res.get("stdout") or {}).get("sha256") == hashlib.sha256(b"secret-out-marker\n").hexdigest()
              and (res.get("stderr") or {}).get("bytes") == len("secret-err-marker\n"), res)
        check("no script, argument or output text in the file", "secret" not in raw)
        v = verifies(td, files[-1])
        check("the log verifies", v["ok"] and v["final"] and v["calls"] == 1, v)
    sh(td, "-c", body, "--", "secret-arg-marker", MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_TEXT="keep")
    raw = open(log_files(td)[-1]).read()
    check("ACTION_LOG_TEXT=keep: the script, its arguments and its output are kept",
          all(m in raw for m in ("secret-out-marker; echo", "secret-arg-marker", "secret-out-marker\\n", "secret-err-marker\\n")))
    r = sh(td, "-c", "touch /nonexistent/x", MCOMP_ACTION_LOG="on", MCOMP_VM_SHELL="off")
    recs = records(log_files(td)[-1])
    check("a refused run is logged as refused", r.returncode == 125 and any(
        x["type"] == "refused" and x.get("tool") == "vm_shell" for x in recs), [x["type"] for x in recs])
    sh(td, "-c", "sleep 34.5", "--timeout", "1", MCOMP_ACTION_LOG="on")
    res = next((x for x in records(log_files(td)[-1]) if x["type"] == "result"), {}).get("result") or {}
    check("a timeout is logged as one", res.get("status") == "timeout" and res.get("error") is True, res)
    env = env_for(td)
    v = subprocess.run([sys.executable, os.path.join(LAB, "lib", "actionlog.py"), "verify", "--all"],
                       capture_output=True, text=True, env=env)
    check("metacomputer actions verify --all passes", v.returncode == 0 and "TAMPERED" not in v.stdout, v.stdout[-300:])
    s = subprocess.run([sys.executable, os.path.join(LAB, "lib", "actionlog.py"), "show", "latest"],
                       capture_output=True, text=True, env=env).stdout
    check("actions show reads a shell run", "-> vm_shell" in s and "<- vm_shell timeout" in s, s[-300:])


FAKE_KEYGEN = """#!/bin/bash
# ssh-keygen that hangs on signing after FAKE_SIGN_OK good signatures; anything else is the real one.
if [ "$2" = "-Y" ] && [ "$3" = "sign" ]; then
  n=$(cat "$FAKE_SIGN_COUNT" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$FAKE_SIGN_COUNT"
  if [ "$n" -ge "${FAKE_SIGN_OK:-0}" ]; then exec sleep 61.3; fi
fi
exec REAL "$@"
"""


def lifecycle_checks(td):
    """The VM half's lifecycle (round 2): the deadline without open output, cancellation
    that waits for the whole group, and draining while a TERM handler runs."""
    t = time.monotonic()
    r = sh(td, "--json", "--timeout", "1", "-c", "exec >/dev/null 2>&1; sleep 20.6")
    el = time.monotonic() - t
    check("a script that closes its output still times out (124) on time", r.returncode == 124
          and js(r).get("status") == "timeout" and el < 6 and not pgrep("sleep 20.6"), (r.returncode, el))

    marker = os.path.join(td, "saved-after-term")
    r = sh(td, "--json", "--timeout", "1", "--max-output", "1024", "-c",
           f"(trap 'head -c 300000 /dev/zero; echo saved > {marker}; exit' TERM; while :; do sleep 0.1; done) & wait")
    j = js(r)
    check("output is still read during the SIGTERM grace: a handler printing 300 KB finishes",
          r.returncode == 124 and os.path.exists(marker) and j.get("stdout_bytes", 0) >= 300000, (r.returncode, j.get("stdout_bytes")))

    p = subprocess.Popen(cmd(td, ["-c", "(trap '' TERM; sleep 35.1) & wait"]), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env_for(td))
    time.sleep(1.5)
    t = time.monotonic()
    p.send_signal(signal.SIGTERM)
    p.communicate(timeout=30)
    el = time.monotonic() - t
    left = pgrep("sleep 35.1")
    check("cancelled: a child that ignores SIGTERM is killed before the VM half exits", p.returncode == 130 and not left
          and el >= 2.5, (p.returncode, left, el))
    for pid in left:
        subprocess.run(["kill", "-9", pid])


def signal_checks(td):
    """Repeated interrupts while the Mac ends the connection can't skip the kill."""
    pidfile = os.path.join(td, "stubborn.pid")
    p = subprocess.Popen(cmd(td, ["-c", "true"], "stubborn"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env_for(td, FAKE_PIDFILE=pidfile))
    time.sleep(1.0)
    t = time.monotonic()
    while p.poll() is None and time.monotonic() - t < 20:
        p.send_signal(signal.SIGTERM if int((time.monotonic() - t) * 4) % 2 else signal.SIGINT)
        time.sleep(0.25)
    p.communicate(timeout=30)
    el = time.monotonic() - t
    check("interrupts arriving all through cleanup: the transport is still killed and reaped (130)",
          p.returncode == 130 and gone(pidfile) and el < 15, (p.returncode, gone(pidfile), el))


def signer_checks(td):
    """A signer that never answers counts as a failed signature, within a bound."""
    import shutil as _sh
    fakebin = os.path.join(td, "fakebin")
    os.makedirs(fakebin)
    keygen = os.path.join(fakebin, "ssh-keygen")
    open(keygen, "w").write(FAKE_KEYGEN.replace("REAL", _sh.which("ssh-keygen")))
    os.chmod(keygen, 0o755)
    path = fakebin + ":" + os.environ["PATH"]
    marker = os.path.join(td, "ran-hung-signer")
    count = os.path.join(td, "sign-count")
    logs = os.path.join(td, "hung-actions")
    t = time.monotonic()
    r = sh(td, "-c", f"touch {marker}", PATH=path, FAKE_SIGN_COUNT=count, FAKE_SIGN_OK="0", MCOMP_SIGN_TIMEOUT_S="1",
           MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_DIR=logs, MCOMP_ACTION_LOG_KEY=os.path.join(td, "keys3", "k"))
    el = time.monotonic() - t
    check("a signer that hangs at the start: refused (125) within the bound, nothing runs, the signer killed",
          r.returncode == 125 and not os.path.exists(marker) and el < 8 and not pgrep("sleep 61.3"),
          (r.returncode, el, r.stderr[-200:]))
    os.remove(count)
    open(os.path.join(td, "reply.bin"), "wb").write(reply(out=b"ran\n"))
    t = time.monotonic()
    r = sh(td, "--json", "-c", "true", fake="reply", FAKE_REPLY=os.path.join(td, "reply.bin"), PATH=path,
           FAKE_SIGN_COUNT=count, FAKE_SIGN_OK="1", MCOMP_SIGN_TIMEOUT_S="1", MCOMP_ACTION_LOG="on",
           MCOMP_ACTION_LOG_DIR=logs, MCOMP_ACTION_LOG_KEY=os.path.join(td, "keys3", "k"))
    el = time.monotonic() - t
    check("a signer that hangs at the end: exit 255 (unlogged) within the bound, the signer killed",
          r.returncode == 255 and js(r).get("kind") == "log" and el < 8 and not pgrep("sleep 61.3"), (r.returncode, el))

    old_path, old_timeout = os.environ["PATH"], actionlog.SIGN_TIMEOUT_S
    os.environ.update(PATH=path, FAKE_SIGN_COUNT=count, FAKE_SIGN_OK="0")
    os.remove(count)
    actionlog.SIGN_TIMEOUT_S = 1
    try:
        log = actionlog.ActionLog({"bridge_pid": 1}, {"on": True, "keep_text": False, "dir": os.path.join(td, "sticky"),
                                                      "key": os.path.join(td, "keys4", "k")})
        log.call(1, "vm_shell", {})
        t = time.monotonic()
        first = log.sign_now()
        el = time.monotonic() - t
        os.environ["FAKE_SIGN_OK"] = "100"   # the signer works again
        log.call(2, "vm_shell", {})
        second = log.sign_now()
        log.close()
        kinds = [x["type"] for x in records(log.path)]
        check("ActionLog: a hung signer fails within SIGN_TIMEOUT_S and the failure is sticky",
              first is False and second is False and el < 3 and "sign_failed" in kinds and log.sign_error, (first, second, el, kinds))
    finally:
        os.environ["PATH"] = old_path
        for k in ("FAKE_SIGN_COUNT", "FAKE_SIGN_OK"):
            os.environ.pop(k, None)
        actionlog.SIGN_TIMEOUT_S = old_timeout


def refresh_checks(td):
    """The claim refresh during a run can't stall the deadline."""
    os.environ["MCOMP_RUN_DIR"] = os.path.join(td, "run3")
    import importlib
    importlib.reload(owners)
    real_ps = owners.ps_table
    try:
        def no_ps(*_a, **_k):
            raise AssertionError("ps called")
        owners.ps_table = no_ps
        who = owners.claim_for_shell("vm_shell", (424242, None))
        rec = json.load(open(os.path.join(td, "run3", "owners", "424242.json")))
        check("a refresh reuses the holder found at the start: no process table", who == (424242, None)
              and rec.get("last_tool") == "vm_shell")
    except AssertionError as e:
        check("a refresh reuses the holder found at the start: no process table", False, e)
    finally:
        owners.ps_table = real_ps
        os.environ.pop("MCOMP_RUN_DIR", None)
        importlib.reload(owners)
    old = vm_shell.CLAIM_EVERY_S
    vm_shell.CLAIM_EVERY_S = 0.2
    ticks = []
    try:
        t = time.monotonic()
        _, _, problem = vm_shell.run_remote([sys.executable, os.path.join(td, "fake_vm.py"), "deaf"], b"x" * 10,
                                            t + 1.0, {"stdout": 1000, "stderr": 1000},
                                            lambda: (ticks.append(time.monotonic() - t), time.sleep(1.5)))
        el = time.monotonic() - t
        check("the deadline is checked again right after a slow refresh", problem == "timeout" and len(ticks) == 1
              and ticks[0] < 0.5 and 1.5 < el < 2.7, (problem, ticks, el))
    finally:
        vm_shell.CLAIM_EVERY_S = old


def relay_owner_checks(td):
    """With ACTION_LOG=on a bridge's connection runs through mcp_relay.py; it's the
    bridge's own, not a second controller. A relay under anything else still counts."""
    tree = os.path.join(td, "tree")
    os.makedirs(os.path.join(tree, "k"))
    spawner = "import subprocess, sys\nsubprocess.Popen(sys.argv[1:]).wait()\n"
    for name in ("mcp_lazy.py", "mcp_relay.py", "mcp_client.py"):
        open(os.path.join(tree, name), "w").write(spawner)
    ssh = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ProxyCommand=sleep 40",
           "-o", f"IdentityFile={tree}/k/mcp", "-p", "59997", "cua@127.0.0.1", "true"]
    py = sys.executable
    bridge = subprocess.Popen([py, os.path.join(tree, "mcp_lazy.py"), py, os.path.join(tree, "mcp_relay.py"), *ssh],
                              start_new_session=True, stdin=subprocess.DEVNULL)
    client = subprocess.Popen([py, os.path.join(tree, "mcp_client.py"), py, os.path.join(tree, "mcp_relay.py"), *ssh],
                              start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        r = subprocess.run([py, "-c", "import json, sys; sys.path.insert(0, sys.argv[1]); import owners; "
                                      "print(json.dumps(owners.direct_connections(owners.ps_table())))",
                            os.path.join(LAB, "lib")], capture_output=True, text=True,
                           env=dict(os.environ, MCOMP_SSH_PORT="59997"))
        found = json.loads(r.stdout or "[]")
        chains = [set(d.get("chain") or []) for d in found]
        check("who: a bridge's connection through the action-log relay isn't another controller; a relay under a "
              "client is", len(found) == 1 and client.pid in chains[0] and bridge.pid not in chains[0],
              (len(found), r.stderr[-200:]))
    finally:
        for proc in (bridge, client):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()


def main():
    td = tempfile.mkdtemp(prefix="mcomp-sh-")
    try:
        unit_checks()
        run_checks(td)
        hostile_checks(td)
        owner_checks(td)
        log_checks(td)
        lifecycle_checks(td)
        signal_checks(td)
        signer_checks(td)
        refresh_checks(td)
        relay_owner_checks(td)
    finally:
        for d, _, _ in os.walk(td):
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
