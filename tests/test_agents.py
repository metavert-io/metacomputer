#!/usr/bin/env python3
"""`metacomputer agents` (lib/agents.py): which coding agents are set up, and the check of what metacomputer relies
on in Codex. No VM. The real check runs against this Mac's codex when it's installed (in a throwaway CODEX_HOME:
your Codex settings aren't touched); a stand-in codex that behaves differently must fail it.

  python3 tests/test_agents.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AG = os.path.join(LAB, "lib", "agents.py")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def run(args, env):
    return subprocess.run([sys.executable, AG, *args], capture_output=True, text=True, env=env, timeout=300,
                          stdin=subprocess.DEVNULL)


# A codex that answers but has changed: its rules checker allows everything, its sandbox sets no marker, and
# `codex mcp` doesn't work.
FAKE_CODEX = """#!/bin/sh
case "$1" in
  --version) echo "codex-cli 99.0.0" ;;
  execpolicy) echo '{"decision":"allow","matchedRules":[]}' ;;
  sandbox) while [ "$1" != "--" ] && [ $# -gt 0 ]; do shift; done; shift; exec env -u CODEX_SANDBOX "$@" ;;
  mcp) echo "unknown subcommand" >&2; exit 2 ;;
  *) exit 2 ;;
esac
"""


def main():
    t = tempfile.mkdtemp(prefix="mcomp-agents-")
    try:
        home = os.path.join(t, "home")
        os.makedirs(os.path.join(home, ".codex"))
        base = {"HOME": home, "CODEX_HOME": os.path.join(home, ".codex"), "LANG": "en_US.UTF-8"}

        print("status")
        env = dict(base, PATH="/usr/bin:/bin")
        r = run(["--json"], env)
        st = json.loads(r.stdout or "{}")
        check("with neither agent on PATH: both reported as not installed", r.returncode == 0
              and st.get("claude_code", {}).get("installed") is False and st.get("codex", {}).get("installed") is False, r.stdout)
        r = run([], env)
        check("...and said plainly, with how to set one up later", "isn't installed" in r.stdout
              and "--only-agents" in r.stdout, r.stdout)
        os.makedirs(os.path.join(home, ".local", "share", "metacomputer"))
        json.dump({"claude": "auto", "codex": "off"}, open(os.path.join(home, ".local", "share", "metacomputer", "agents.json"), "w"))
        r = run(["--json"], env)
        check("your remembered choice (install.sh --no-codex) is reported", json.loads(r.stdout)["codex"].get("choice") == "off",
              r.stdout)

        print("check")
        r = run(["check"], env)
        check("check without Codex: nothing to check, exit 0", r.returncode == 0 and "isn't installed" in r.stdout, r.stdout)
        fake_bin = os.path.join(t, "fakebin")
        os.makedirs(fake_bin)
        with open(os.path.join(fake_bin, "codex"), "w") as f:
            f.write(FAKE_CODEX)
        os.chmod(os.path.join(fake_bin, "codex"), 0o755)
        r = run(["check", "--json"], dict(base, PATH=fake_bin + ":/usr/bin:/bin"))
        res = {c["name"]: c["ok"] for c in json.loads(r.stdout or "{}").get("checks", [])}
        check("a Codex that changed fails the check (exit 1): its rules decisions, its sandbox marker, codex mcp",
              r.returncode == 1 and res.get("rules") is False and res.get("sandbox marker") is False
              and res.get("mcp config") is False, (r.returncode, res, r.stderr[-300:]))
        r = run(["check"], dict(base, PATH=fake_bin + ":/usr/bin:/bin"))
        check("...and says what changed and where to look", "changed something metacomputer relies on" in r.stdout
              and "docs/codex.md" in r.stdout, r.stdout)
        codex = shutil.which("codex")
        if codex:
            env = dict(base, PATH=os.path.dirname(codex) + ":/usr/bin:/bin")
            before = sorted(os.listdir(base["CODEX_HOME"]))
            r = run(["check", "--json"], env)
            d = json.loads(r.stdout or "{}")
            check(f"this Mac's Codex ({d.get('codex')}) passes every check", r.returncode == 0
                  and d.get("checks") and all(c["ok"] for c in d["checks"]), r.stdout[-600:])
            check("...without touching its Codex settings (a throwaway CODEX_HOME)",
                  sorted(os.listdir(base["CODEX_HOME"])) == before, os.listdir(base["CODEX_HOME"]))
        else:
            print("SKIP the real Codex check: no codex command on this Mac")
    finally:
        shutil.rmtree(t, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
