#!/usr/bin/env python3
"""Two fixes found while recording on a fresh VM. No VM.

    python3 tests/test_reset_sync.py

1. `metacomputer reset` (and stop and checkpoint) first copies the guest's recordings with
   `do_recordings --sync ... || true`. do_recordings used `die` when the copy failed, which exits the
   whole command, so `|| true` never ran: a VM whose home had been wiped (no ~/metacomputer-runs)
   couldn't be reset at all. It now warns and returns non-zero. This runs the real function from
   bin/metacomputer under /bin/bash with stand-ins for the VM.
2. `web compare|responsive|a11y|open-untrusted` named their Chrome session with a fixed string. The
   hub keeps one driver connection, and Cua Driver keeps an ended session's name ended for that
   connection's lifetime, so a second run in the same hub failed ("session ... has ended"). Each run
   now gets a name of its own.
"""
import os
import re
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def function(src, name):
    m = re.search(r"^%s\(\) \{.*?^\}\n" % re.escape(name), src, re.S | re.M)
    assert m, name
    return m.group(0)


def run_sync(running, gssh_ok, import_ok):
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    td = tempfile.mkdtemp(prefix="mcomp-resetsync-")
    script = f'''
set -Eeuo pipefail
LAB_ROOT={td}; VM_NAME=t; PY=/usr/bin/true; RECORD_DIR=
warn() {{ echo "WARN $*" >&2; }}
ok() {{ echo "OK $*"; }}
die() {{ echo "DIE $*" >&2; exit 9; }}
vm_running() {{ {"true" if running else "false"}; }}
gssh() {{ {"printf x" if gssh_ok else "return 1"}; }}
guest_import() {{ cat >/dev/null; {"true" if import_ok else "false"}; }}
{function(src, "do_recordings")}
do_recordings --sync >/dev/null || true
echo "WENT-ON"
'''
    p = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    return p


# 1. A failed sync must not end the caller (reset goes on).
p = run_sync(running=True, gssh_ok=False, import_ok=False)
check("reset goes on when the guest has no recordings folder", "WENT-ON" in p.stdout,
      f"rc={p.returncode} out={p.stdout!r} err={p.stderr!r}")
check("the failed sync is reported as a warning, not an exit", "WARN could not sync" in p.stderr
      and "DIE" not in p.stderr, p.stderr)
p = run_sync(running=False, gssh_ok=True, import_ok=True)
check("a stopped VM: warns and goes on", "WENT-ON" in p.stdout and "WARN VM is not running" in p.stderr,
      f"{p.stdout!r} {p.stderr!r}")
p = run_sync(running=True, gssh_ok=True, import_ok=True)
check("a sync that works still reports it", "WENT-ON" in p.stdout and "DIE" not in p.stderr, p.stderr)

# `metacomputer recordings --sync` on its own still fails with a non-zero status when the copy fails.
src = open(os.path.join(LAB, "bin", "metacomputer")).read()
check("do_recordings no longer calls die for the sync", "die \"could not sync" not in function(src, "do_recordings")
      and "die \"VM is not running; nothing to sync\"" not in function(src, "do_recordings"))

# 2. Every browser run gets its own session name.
sys.path.insert(0, os.path.join(LAB, "lib"))
import browser_tools  # noqa: E402

calls = []


class FakeDriver(browser_tools.Driver):
    def __init__(self):
        self.sessions = set()

    def call(self, tool, args=None, ok=True, timeout=120):
        calls.append((tool, dict(args or {})))

        class R:
            structured = {"prepared_pid": 7, "target_id": "t", "tabs": [{"tab_id": "a"}]}
            is_error = False
            text = ""
        return R()

    def wait_new_window(self, before, pid=None):
        return {"pid": 7, "window_id": 1}

    def frame(self, w, width):
        pass


d = FakeDriver()
w1 = d.open_chrome(None, "web-untrusted")
w2 = d.open_chrome(None, "web-untrusted")
names = [a["session"] for t, a in calls if t == "browser_prepare"]
check("two runs, two session names", len(names) == 2 and names[0] != names[1], str(names))
check("names keep their readable prefix", all(n.startswith("web-untrusted-") for n in names), str(names))
check("the names are what gets ended at close", d.sessions == set(names), f"{d.sessions} vs {names}")
check("names are plain (letters, digits, hyphens)", all(re.fullmatch(r"[a-z0-9-]+", n) for n in names), str(names))

print(("FAILED: " + ", ".join(FAILS)) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
