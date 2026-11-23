#!/usr/bin/env python3
"""Text from the VM and from web pages on its way to a terminal (lib/termsafe.py and the
commands that print such text: `inspect`, `browser`, `web`, `record moments`). No VM.

    python3 tests/test_termsafe.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import termsafe  # noqa: E402

INSPECT = os.path.join(LAB, "lib", "guest_inspect.py")
RECORDING = os.path.join(LAB, "lib", "recording.py")
HOSTILE = "evil\x1b]52;c;SGk=\x07\x1b[2J\r\x9b31m‮⁦\x7f"
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def raw_controls(text):
    """Characters that must never reach a terminal unescaped."""
    return [c for c in text if c not in "\n\t" and termsafe.UNSAFE.match(c)]


def main():
    e = termsafe.escape_for_terminal
    t = e("a\tb\nc\x1b]52;c;SGk=\x07\r‮⁦\x9b\x7f")
    check("control characters, C1, DEL and bidi controls are escaped; tab and newline kept",
          t == "a\tb\nc\\x1b]52;c;SGk=\\x07\\x0d\\u202e\\u2066\\x9b\\x7f", repr(t))
    check("bytes that aren't UTF-8 are shown as \\xNN", e(b"ok \xff\xfe \xc3\xa9") == "ok \\xff\\xfe é", repr(e(b"ok \xff\xfe \xc3\xa9")))
    check("surrogate-escaped bytes too", e("x\udcff") == "x\\xff", repr(e("x\udcff")))
    check("plain text, accents and emoji pass unchanged", e("café ☃ \U0001F600 ok") == "café ☃ \U0001F600 ok")
    import vm_shell
    check("vm_shell uses the same helper", vm_shell.clean is termsafe.escape_for_terminal)

    # inspect: every field the VM controls
    snap = {
        "collected_at": "2026-11-18T00:00:00+0000", "note": "note" + HOSTILE,
        "system": {"hostname": "host" + HOSTILE, "uptime_s": 100, "load": [0.1, 0.2, 0.3],
                   "memory_mib": {"available": 1, "total": 2}, "disk_gib": {"free": 1, "total": 2},
                   "desktop_session": True, "recording": False,
                   "default_browser": {"xfce": "firefox" + HOSTILE, "links": "firefox"}, "virtual_input_pairs": 0},
        "windows": [{"class": "Firefox" + HOSTILE, "active": True, "window_id": 1, "pid": 2, "width": 3, "height": 4,
                     "x": 0, "y": 0, "title": "title" + HOSTILE}],
        "processes": {"count": 1, "apps": [{"has_window": True, "pid": 2, "cpu_pct": 1, "rss_mib": 1, "age_s": 5,
                                            "command": "cmd" + HOSTILE, "processes": 1}]},
        "ports": [{"proto": "tcp", "local": "127.0.0.1:1", "process": [{"name": "srv" + HOSTILE, "pid": 3}]}],
        "files": {"minutes": 60, "recent_total": 1, "recent": [{"age_s": 1, "bytes": 1, "path": "/f" + HOSTILE,
                                                                "downloading": False}],
                  "downloads": [{"age_s": 1, "bytes": 1, "name": "d" + HOSTILE}]},
        "browser": {"installed": ["firefox"], "running": [{"browser": "chrome", "pid": 4, "profile": "p" + HOSTILE,
                                                           "driver_owned": True, "page_tools": True,
                                                           "windows": ["w" + HOSTILE]}],
                    "named_profiles": [{"name": "n" + HOSTILE, "bytes": 1 << 20, "in_use": True}],
                    "firefox_profiles": [{"name": "f" + HOSTILE, "bytes": 1 << 20, "in_use": False}],
                    "downloading": ["/x" + HOSTILE]},
    }
    r = subprocess.run([sys.executable, INSPECT, "--render"], input=json.dumps(snap), capture_output=True, text=True)
    check("inspect: titles, commands, names and paths from the VM reach the terminal escaped", r.returncode == 0
          and raw_controls(r.stdout) == [] and "\\x1b]52;c;SGk=\\x07" in r.stdout and "\\u202e" in r.stdout
          and r.stdout.count("\\x1b]52") >= 10, (r.returncode, r.stderr, raw_controls(r.stdout)[:5]))
    r = subprocess.run([sys.executable, INSPECT, "--render-profiles"], input=json.dumps(snap), capture_output=True, text=True)
    check("browser (profiles): names escaped, kept Firefox profiles' too", r.returncode == 0
          and raw_controls(r.stdout) == [] and r.stdout.count("\\x1b]52") >= 2, (r.stdout, r.stderr))
    r = subprocess.run([sys.executable, INSPECT, "--render-profiles"], input=json.dumps({"browser": {"named_profiles": []}}),
                       capture_output=True, text=True)
    check("browser (profiles): none", r.returncode == 0 and "no driver-owned Chrome profiles" in r.stdout, r.stdout)
    r = subprocess.run([sys.executable, INSPECT, "--render"], input=json.dumps({"windows": [{"class": 5}]}),
                       capture_output=True, text=True)
    check("inspect: a snapshot of the wrong shape is a fixed message, exit 1, no traceback", r.returncode == 1
          and "expected shape" in r.stderr and "Traceback" not in r.stderr, r.stderr)

    # web: the report, progress messages and errors
    import browser_tools
    td = tempfile.mkdtemp(prefix="mcomp-termsafe-")
    page = {"title": "Page" + HOSTILE, "links": ["click" + HOSTILE]}

    def fake(a):
        with open(os.path.join(td, "report.md"), "w", newline="") as f:
            f.write(f"# Report\n\ntitle: {page['title']}\nlink: {page['links'][0]}\n")
        return {"folder": td, "page": page}

    def failing(a):
        raise browser_tools.Fail("the page said " + HOSTILE)

    old = browser_tools.cmd_a11y
    try:
        browser_tools.cmd_a11y = fake
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = browser_tools.main(["a11y", "https://example.com/"])
        check("web: the report printed to the terminal is escaped; report.md keeps the page's text", rc == 0
              and raw_controls(out.getvalue()) == [] and "\\x1b]52;c;SGk=\\x07" in out.getvalue()
              and HOSTILE in open(os.path.join(td, "report.md"), newline="").read(), (rc, out.getvalue()[:300]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = browser_tools.main(["a11y", "https://example.com/", "--json"])
        check("web --json stays exact (and JSON escapes control characters itself)", rc == 0
              and json.loads(out.getvalue())["page"] == page and "\x1b" not in out.getvalue(), out.getvalue()[:300])
        browser_tools.cmd_a11y = failing
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = browser_tools.main(["a11y", "https://example.com/"])
        check("web: an error carrying the page's words is escaped", rc == 1 and raw_controls(err.getvalue()) == []
              and "\\x1b]52" in err.getvalue(), err.getvalue())
    finally:
        browser_tools.cmd_a11y = old

    # record moments: a description file can come from the VM
    side = os.path.join(td, "clip.json")
    with open(side, "w") as f:
        json.dump({"moments": [{"t": 1.5, "tool": "click" + HOSTILE, "what": "w" + HOSTILE}]}, f)
    r = subprocess.run([sys.executable, RECORDING, "moments", side], capture_output=True, text=True)
    check("record moments: the description's words escaped", r.returncode == 0 and raw_controls(r.stdout) == []
          and "\\x1b]52" in r.stdout, (r.stdout, r.stderr))
    shutil.rmtree(td, ignore_errors=True)
    stream_checks()
    cli_checks()

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


def stream_checks():
    # termsafe.py as a filter (the shell's way to show the VM's stderr): escaped, line by line, capped
    data = ("ok line\n" + HOSTILE + "\n" + "z" * 5000 + "\n").encode() * 3
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "termsafe.py"), "--max", "2000"], input=data,
                       capture_output=True, timeout=30)
    out = r.stdout.decode()
    check("termsafe.py filter: escapes as it goes, and past --max says the rest was left out (and reads it all)",
          r.returncode == 0 and raw_controls(out) == [] and "\\x1b]52" in out and "the rest was left out" in out
          and len(out) < 4000, (r.returncode, len(out)))


def on_pty(argv, env, timeout=60, marker=None):
    """Run argv with stdout and stderr on a terminal; everything it printed. With marker: also the seconds after
    the start at which the marker text first appeared and at which the output ended."""
    import pty
    m, s = pty.openpty()
    t0 = time.time()
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=s, stderr=s, env=env, close_fds=True)
    os.close(s)
    got = b""
    seen = None
    while True:
        try:
            chunk = os.read(m, 65536)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
        if marker and seen is None and marker.encode() in got:
            seen = time.time() - t0
    ended = time.time() - t0
    p.wait(timeout)
    os.close(m)
    if marker:
        return p.returncode, got.decode("utf-8", "replace"), seen, ended
    return p.returncode, got.decode("utf-8", "replace")


FAKE_SSH = r'''#!/bin/sh
printf 'ssh says \033]52;c;SGk=\007\033[2J\n' >&2
case "$*" in
  *guest-status*) printf '%s\n' '{"ready": true, "resolution": "1920x1080\u001b]52;c;SGk=\u0007", "driver_version": "0.53\u001b[2J", "active_window_title": "t", "loadavg": {"\u001b]0;pwned\u0007": 1, "‮": 2}, "mem_available_mb": {"\u001b[5m": 1}, "disk_free_gb": 1'"$(printf '%0400d' 0)"', "uptime_s": 5}' ;;
  *desktop-record*)
    if [ "${FAKE_REC:-}" = slow ]; then    # a recorder that keeps talking: its words must stream, not pile up
      printf 'recording stream:y \033[2J\n'; sleep 3; printf 'still going\n'; exit "${FAKE_REC_RC:-0}"
    fi
    printf 'recording stream:x \033]52;c;SGk=\007\033[2J\n' ;;
  *python3*) cat >/dev/null; printf '{"note": "n\\u001b]52;c;SGk=\\u0007 \342\200\256"}\n' ;;
  *input-reset*) printf '0\033[2J\n' ;;
  *) exit 0 ;;
esac
'''


def cli_checks():
    # bin/metacomputer on a terminal: what the VM writes to SSH's stderr or stdout arrives escaped
    td = tempfile.mkdtemp(prefix="mcomp-termsafe-cli-")
    try:
        bindir = os.path.join(td, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "ssh"), "w") as f:
            f.write(FAKE_SSH)
        with open(os.path.join(bindir, "lume"), "w") as f:
            f.write("#!/bin/sh\ncase \"$1\" in get) echo '{\"status\": \"running\"}' ;; "
                    "ls) echo '[{\"name\": \"fake-termsafe-vm\", \"status\": \"running\"}]' ;; esac\n")
        for n in ("ssh", "lume"):
            os.chmod(os.path.join(bindir, n), 0o755)
        cfg = os.path.join(td, "test.env")
        with open(cfg, "w") as f:
            f.write(f'VM_NAME=fake-termsafe-vm\nLUME="{bindir}/lume"\nSSH_DIR="{td}/ssh"\nSSH_PORT=1\n'
                    f'RUN_DIR="{td}/run"\nLOG_DIR="{td}/logs"\nACTION_LOG=off\n'
                    # record start below reads what the VM's recorder answers, which only guest mode does
                    'RECORD_TO=guest\n')
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""), MCOMP_CONFIG=cfg,
                   MCOMP_EVENTS_DIR=os.path.join(td, "events"))
        cli = os.path.join(LAB, "bin", "metacomputer")
        rc, out = on_pty([cli, "record", "status"], env)
        check("record status on a terminal: the recorder's words and SSH's stderr are escaped",
              rc == 0 and "\x1b]52" not in out and "\x1b[2J" not in out and out.count("\\x1b]52") >= 2, (rc, out[-300:]))
        rc, out = on_pty([cli, "inspect", "--json"], env)
        check("inspect --json on a terminal: the VM's JSON written again as ASCII-only JSON; stderr escaped",
              rc == 0 and "\x1b" not in out and "‮" not in out and "\\u001b]52" in out and "\\u202e" in out
              and "\\x1b]52" in out, (rc, out[-300:]))
        rc, out = on_pty([cli, "input-reset"], env)
        check("input-reset takes only a count from the VM: an answer with escapes is refused, and none of it reaches "
              "the terminal", rc == 1 and "\x1b" not in out and "wasn't a count" in out, (rc, out[-200:]))
        r = subprocess.run([cli, "input-reset", "--check"], env=env, capture_output=True, text=True, timeout=60)
        check("…nor a pipe (--check too)", r.returncode == 1 and "\x1b" not in r.stdout + r.stderr, repr(r.stdout + r.stderr))
        rc, out = on_pty([cli, "record", "start", "clip1"], dict(env, RECORD_DIR=os.path.join(td, "rec")))
        check("record start on a terminal: the recorder's answer is escaped", "\x1b]52" not in out and "\x1b[2J" not in out
              and "\\x1b]52" in out and "recording the guest desktop" in out, (rc, out[-300:]))
        state = os.path.join(td, "run", "manual-recording")
        with contextlib.suppress(FileNotFoundError):
            os.remove(state)
        renv = dict(env, RECORD_DIR=os.path.join(td, "rec2"), FAKE_REC="slow")
        rc, out, seen, ended = on_pty([cli, "record", "start", "clip2"], renv, marker="recording stream:y")
        with contextlib.suppress(FileNotFoundError):
            os.remove(state)
        check("record start streams the recorder's words, escaped, as they come (not held until it ends)",
              seen is not None and ended - seen > 2 and "\x1b[2J" not in out and "\\x1b[2J" in out and rc == 0,
              (seen, ended, rc, out[-200:]))
        rc, out = on_pty([cli, "record", "start", "clip3"], dict(renv, RECORD_DIR=os.path.join(td, "rec3"),
                                                                  FAKE_REC_RC="1"))
        check("…and the recorder's own exit status still decides (a failed start says so)",
              rc != 0 and "could not start recording" in out, (rc, out[-200:]))
        # status: a gateway that looks alive, so the guest's own report is read and shown
        # (its command line names this VM's gw.json, as the real gateway's does: netgw_pid checks that)
        gw = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-config", f"{td}/run/gw.json"])
        try:
            os.makedirs(os.path.join(td, "run"), exist_ok=True)
            with open(os.path.join(td, "run", "netgw.pid"), "w") as f:
                f.write(str(gw.pid))
            with open(cfg, "a") as f:
                f.write("MCP_HUB=off\n")
            rc, out = on_pty([cli, "status"], env, timeout=120)
        finally:
            gw.kill()
            gw.wait()
        check("status on a terminal: the guest's own report (resolution, driver version) is escaped",
              "\x1b]52" not in out and "\x1b[2J" not in out and "resolution 1920x1080\\x1b]52" in out
              and "0.53\\x1b[2J" in out, out[-500:])
        check("…and a load average or memory figure that isn't numbers shows as '?', its keys never raw",
              "\x1b]0;pwned" not in out and "\x1b[5m" not in out and "‮" not in out and "load ?," in out
              and "? MB free" in out, out[-300:])
        check("…and a 401-digit disk figure is '?' too, not a crash", "? GB disk free" in out
              and "Traceback" not in out, out[-300:])
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
