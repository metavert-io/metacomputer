#!/usr/bin/env python3
"""Offline checks for `metacomputer sh` (lib/vm_shell.py, lib/vm_shell_guest.py).

No VM needed: the "transport" is `bash -c` on this Mac instead of SSH, so the
VM's half runs locally. Run:
    python3 tests/test_vm_shell.py
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
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import actionlog  # noqa: E402
import vm_shell  # noqa: E402
import vm_shell_guest  # noqa: E402

SH = os.path.join(LAB, "lib", "vm_shell.py")
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""), flush=True)


def usage_error(argv):
    try:
        vm_shell.parse(argv)
    except vm_shell.Usage as e:
        return str(e)
    return None


def unit_checks(td):
    p = vm_shell.parse
    o = p(["-c", "echo hi"])
    check("parse: -c", o["source"] == "command" and o["command"] == "echo hi" and o["timeout"] == 120
          and o["max_output"] == 65536 and not o["json"])
    o = p(["--file", "x.sh", "--cwd", "/tmp", "--timeout=5", "--max-output", "2048", "--json", "--", "a", "-c", "--json"])
    check("parse: --file, options (both forms), arguments after --",
          o["source"] == "file" and o["file"] == "x.sh" and o["cwd"] == "/tmp" and o["timeout"] == 5
          and o["max_output"] == 2048 and o["json"] and o["args"] == ["a", "-c", "--json"], o)
    o = p(["-", "--read-only"])
    check("parse: - and --read-only", o["source"] == "stdin" and o["read_only"])
    o = p(["-c", "-x"])
    check("parse: a -c script may start with a dash", o["command"] == "-x")
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
    check("VM_SHELL: on is on even when bounded; off is off",
          a("on", "bounded")[0] is True and a("off", "standard")[0] is False)
    check("VM_SHELL: an unknown value is neither", a("yes", "standard")[0] is None)
    lo = vm_shell.logging_on
    check("SHELL_LOG: auto follows ACTION_LOG; off never logs; unknown is neither",
          (lo("auto", "on"), lo("auto", "off"), lo("off", "on"), lo("always", "on")) == (True, False, False, None))
    d = vm_shell.describe_settings({"vm_shell": "auto", "shell_log": "auto", "action_log": "on",
                                    "action_log_text": "keep", "driver_mode": "standard"})
    check("security summary", d["effective"] == "on" and d["logged"] and d["text"] == "keep" and not d["problems"], d)

    # output caps: the VM's summary and the Mac's rendering
    def summary(data, cap):
        path = os.path.join(td, "stream")
        open(path, "wb").write(data)
        return vm_shell_guest.summarize(path, cap)

    data = bytes(range(256)) * 10
    s = summary(data, 4096)
    check("cap: output under the cap comes back whole", not s["truncated"] and base64.b64decode(s["head"]) == data
          and s["bytes"] == len(data) and s["sha256"] == hashlib.sha256(data).hexdigest())
    s = summary(b"x" * 4096, 4096)
    check("cap: exactly the cap is not truncated", not s["truncated"] and s["bytes"] == 4096)
    data = os.urandom(3000) + b"MIDDLE" + os.urandom(3000)
    s = summary(data, 2048)
    head, tail = base64.b64decode(s["head"]), base64.b64decode(s["tail"])
    check("cap: over the cap keeps the first and last halves", s["truncated"] and head == data[:1024]
          and tail == data[-1024:] and s["bytes"] == len(data) and s["sha256"] == hashlib.sha256(data).hexdigest())
    data = b"a" * 2050
    s = summary(data, 2048)
    check("cap: just over the cap", s["truncated"] and base64.b64decode(s["tail"]) == data[-1024:])
    shown, info = vm_shell.stream(summary(("é" * 3000).encode(), 2048), "stdout")
    try:
        text = shown.decode("utf-8")
        ok = "left out here" in text and info["bytes"] == 6000
    except UnicodeDecodeError as e:
        ok, text = False, str(e)
    check("cap: cuts fall on UTF-8 character boundaries", ok, text[:80])


def env_for(td, **extra):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MCOMP_"):
            env.pop(k)
    env.update(MCOMP_RUN_DIR=os.path.join(td, "run"), MCOMP_ACTION_LOG_DIR=os.path.join(td, "actions"),
               MCOMP_ACTION_LOG_KEY=os.path.join(td, "keys", "action-log"), MCOMP_VM_NAME="testvm",
               MCOMP_GUEST_USER="cua", XDG_RUNTIME_DIR=os.path.join(td, "xdg"),
               MCOMP_SSH_PORT="1")   # a port no VM uses: real driver connections on this Mac aren't this "VM"'s
    env.pop("DISPLAY", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def sh(td, *args, stdin=None, timeout=60, **env):
    work = os.path.join(td, "work")
    return subprocess.run([sys.executable, SH, "--transport", "2", "bash", "-c", "--cwd", work, *args],
                          input=stdin, capture_output=True, text=True, timeout=timeout, env=env_for(td, **env))


def js(r):
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def log_files(td):
    return actionlog.files(os.path.join(td, "actions"))


def pgrep(pattern):
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()


def run_checks(td):
    os.makedirs(os.path.join(td, "work"))
    r = sh(td, "-c", 'echo out; echo err >&2; exit 7')
    check("exit status is the script's, streams kept apart", r.returncode == 7 and r.stdout == "out\n"
          and "err\n" in r.stderr, (r.returncode, r.stdout, r.stderr))
    r = sh(td, "-c", 'echo "$0|$1|$2|$(pwd)"', "--", "one", "two words")
    check("arguments after -- are $1, $2; --cwd is the folder", r.stdout.strip().endswith(
        "|one|two words|" + os.path.realpath(os.path.join(td, "work"))), r.stdout)
    r = sh(td, "-", stdin='x=$((6*7))\necho "$x"\nread -r line || echo "stdin empty"\n')
    check("script on stdin; its own stdin is empty", r.stdout == "42\nstdin empty\n", r.stdout + r.stderr)
    script = os.path.join(td, "fix-things.sh")
    open(script, "w").write('echo "running $(basename "$0")"\n')
    r = sh(td, "--file", script)
    check("--file sends a Mac file and keeps its name", r.stdout == "running fix-things.sh\n", r.stdout + r.stderr)
    r = sh(td, "--json", "-c", 'printf "%s" "$1"', "--", "data")
    j = js(r)
    check("--json gives the result as data", j.get("status") == "exited" and j.get("exit") == 0 and j.get("exit_code") == 0
          and j.get("stdout") == "data" and j.get("stdout_bytes") == 4
          and j.get("stdout_sha256") == hashlib.sha256(b"data").hexdigest() and j.get("stdout_truncated") is False
          and isinstance(j.get("duration_s"), (int, float)), j)
    r = sh(td, "--json", "--max-output", "1024", "-c", "head -c 5000 /dev/zero | tr '\\0' a")
    j = js(r)
    check("--max-output caps a stream and says so", j.get("stdout_truncated") and j.get("stdout_bytes") == 5000
          and "left out here" in j.get("stdout", "") and j.get("exit") == 0, {k: j.get(k) for k in ("stdout_bytes", "exit")})
    r = sh(td, "-c", "head -c 5000 /dev/zero | tr '\\0' a", "--max-output", "1024")
    check("plain output notes the cap on stderr", "stdout was 5,000 bytes" in r.stderr, r.stderr)

    # the desktop's environment comes from the session file in the VM
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

    # a program left running doesn't hold the result; a timeout ends the script's group
    t = time.monotonic()
    r = sh(td, "-c", "sleep 31.7 & echo started")
    el = time.monotonic() - t
    check("a background program doesn't hold the result back", r.returncode == 0 and el < 5 and r.stdout == "started\n", el)
    check("... and keeps running after a normal end", bool(pgrep("sleep 31.7")))
    subprocess.run(["pkill", "-f", "sleep 31.7"])
    t = time.monotonic()
    r = sh(td, "--timeout", "1", "-c", "sleep 32.1 & sleep 32.2; echo never")
    el = time.monotonic() - t
    check("--timeout: exit 124, the script's group is ended", r.returncode == 124 and "never" not in r.stdout and el < 8
          and not pgrep("sleep 32.1") and not pgrep("sleep 32.2"), (r.returncode, el, r.stderr))
    r = sh(td, "--json", "--timeout", "1", "-c", "sleep 32.3")
    check("--timeout in JSON", js(r).get("status") == "timeout" and js(r).get("exit") == 124 and js(r).get("exit_code") is None)
    p = subprocess.Popen([sys.executable, SH, "--transport", "2", "bash", "-c", "--cwd", os.path.join(td, "work"),
                          "-c", "sleep 33.4; echo never"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env_for(td))
    time.sleep(1.5)
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=20)
    time.sleep(0.5)
    check("interrupted on the Mac: exit 130, the script is stopped too", p.returncode == 130 and not pgrep("sleep 33.4"),
          (p.returncode, err[-200:]))

    # settings
    marker = os.path.join(td, "ran")
    r = sh(td, "-c", f"touch {marker}", MCOMP_VM_SHELL="off")
    check("VM_SHELL=off: refused (125), nothing runs", r.returncode == 125 and not os.path.exists(marker)
          and "VM_SHELL=off" in r.stderr, r.stderr)
    r = sh(td, "-c", f"touch {marker}", MCOMP_DRIVER_MODE="bounded")
    check("bounded driver with VM_SHELL=auto: refused", r.returncode == 125 and not os.path.exists(marker)
          and "bounded" in r.stderr, r.stderr)
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

    # one controller at a time
    run_dir = os.path.join(td, "run", "owners")
    os.makedirs(run_dir, exist_ok=True)
    other = subprocess.Popen(["sleep", "60"])
    try:
        now = time.time()
        json.dump({"bridge_pid": other.pid, "claude_pid": None, "cwd": "/another/project", "started": now,
                   "last_call": now, "last_tool": "click"}, open(os.path.join(run_dir, f"{other.pid}.json"), "w"))
        r = sh(td, "-c", f"touch {marker}")
        check("another session acting: refused (125), nothing runs", r.returncode == 125 and not os.path.exists(marker)
              and "/another/project" in r.stderr, r.stderr)
        before = set(os.listdir(run_dir))
        r = sh(td, "--read-only", "-c", "echo looked")
        check("--read-only runs anyway and doesn't claim the computer", r.returncode == 0
              and set(os.listdir(run_dir)) == before, sorted(os.listdir(run_dir)))
        r = sh(td, "--override", "-c", "echo took over")
        mine = [json.load(open(os.path.join(run_dir, f))) for f in os.listdir(run_dir) if f != f"{other.pid}.json"]
        check("--override runs, and the run claims the computer for this session", r.returncode == 0 and len(mine) == 1
              and mine[0].get("last_tool") == "vm_shell", mine)
    finally:
        other.kill()
        other.wait()

    # the action log
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
        check("records: start, a vm_shell call and its result, end, final signature",
              kinds == ["start", "call", "result", "end", "checkpoint"] and call.get("tool") == "vm_shell"
              and recs[-1].get("final") and (recs[0].get("session") or {}).get("via") == "shell", kinds)
        a = call.get("args") or {}
        check("call: script hash and size, cwd; script and its arguments redacted",
              a.get("script_sha256") == hashlib.sha256(body.encode()).hexdigest() and a.get("script_bytes") == len(body)
              and (a.get("script") or {}).get("redacted") and (a.get("script_args") or {}).get("count") == 1
              and a.get("cwd"), a)
        check("result: exit code, output sizes and hashes",
              res.get("exit_code") == 3 and res.get("error") is True
              and (res.get("stdout") or {}).get("sha256") == hashlib.sha256(b"secret-out-marker\n").hexdigest()
              and (res.get("stderr") or {}).get("bytes") == len("secret-err-marker\n"), res)
        check("no script, argument or output text in the file", "secret" not in raw)
        v = actionlog.verify_file(files[-1], os.path.join(td, "actions"), os.path.join(td, "keys", "action-log.pub"))
        check("the log verifies", v["ok"] and v["final"] and v["calls"] == 1, v)
    r = sh(td, "-c", body, "--", "secret-arg-marker", MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_TEXT="keep")
    raw = open(log_files(td)[-1]).read()
    check("ACTION_LOG_TEXT=keep: the script, its arguments and its output are kept",
          all(m in raw for m in ("secret-out-marker; echo", "secret-arg-marker", "secret-out-marker\\n", "secret-err-marker\\n")))
    r = sh(td, "-c", f"touch {marker}", MCOMP_ACTION_LOG="on", MCOMP_VM_SHELL="off")
    recs = [json.loads(x) for x in open(log_files(td)[-1])]
    check("a refused run is logged as refused", r.returncode == 125 and any(
        x["type"] == "refused" and x.get("tool") == "vm_shell" for x in recs), [x["type"] for x in recs])
    r = sh(td, "-c", "sleep 34.5", "--timeout", "1", MCOMP_ACTION_LOG="on")
    res = next((x for x in map(json.loads, open(log_files(td)[-1])) if x["type"] == "result"), {}).get("result") or {}
    check("a timeout is logged as one", res.get("status") == "timeout" and res.get("error") is True, res)
    env = env_for(td)
    v = subprocess.run([sys.executable, os.path.join(LAB, "lib", "actionlog.py"), "verify", "--all"],
                       capture_output=True, text=True, env=env)
    check("metacomputer actions verify --all passes", v.returncode == 0 and "TAMPERED" not in v.stdout, v.stdout[-300:])
    s = subprocess.run([sys.executable, os.path.join(LAB, "lib", "actionlog.py"), "show", "latest"],
                       capture_output=True, text=True, env=env).stdout
    check("actions show reads a shell run", "-> vm_shell" in s and "<- vm_shell timeout" in s, s[-300:])


def main():
    td = tempfile.mkdtemp(prefix="mcomp-sh-")
    try:
        unit_checks(td)
        run_checks(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
