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
                    "downloading": ["/x" + HOSTILE]},
    }
    r = subprocess.run([sys.executable, INSPECT, "--render"], input=json.dumps(snap), capture_output=True, text=True)
    check("inspect: titles, commands, names and paths from the VM reach the terminal escaped", r.returncode == 0
          and raw_controls(r.stdout) == [] and "\\x1b]52;c;SGk=\\x07" in r.stdout and "\\u202e" in r.stdout
          and r.stdout.count("\\x1b]52") >= 10, (r.returncode, r.stderr, raw_controls(r.stdout)[:5]))
    r = subprocess.run([sys.executable, INSPECT, "--render-profiles"], input=json.dumps(snap), capture_output=True, text=True)
    check("browser (profiles): names escaped", r.returncode == 0 and raw_controls(r.stdout) == [] and "\\x1b]52" in r.stdout,
          (r.stdout, r.stderr))
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

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
