#!/usr/bin/env python3
"""The coding agents metacomputer is set up for on this Mac: Claude Code, Codex (docs/codex.md), OpenClaw
(docs/openclaw.md), Hermes Agent (docs/hermes.md), Gemini CLI (docs/gemini.md) and Antigravity CLI
(docs/antigravity.md).

  metacomputer agents [--json]          each agent: installed (and its version), how it's set up, your choice
                                        (install.sh --no-FLAG, remembered for later runs): an agent with steps of
                                        its own (its manifest's install steps) says how it's set up in its folder's
                                        setup.py (status, line); a generic one, lib/agent_setup.py's status (the
                                        MCP server metacomputer as it keeps it, read by its own CLI or from its
                                        settings, and the skill links)
  metacomputer agents check [--json]    what metacomputer relies on in an agent, checked without a model and
                                        without touching its settings: each agent's setup.py check, where it
                                        defines one (its folder's README says what it checks; with --json, one
                                        agent's document as it prints it, several under their JSON keys). Only a
                                        command from an absolute PATH entry, yours or root's and not writable by
                                        others, is run

install.sh runs an agent's check after setting it up, so an update to the agent that changes one of these is
reported there, and `metacomputer update` runs install.sh. Exit status of `check`: 0 all passed (or nothing to
check), 1 a check failed. Stdlib only; Python 3.11 or later (tomllib).
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
AGENTS_FILE = os.path.expanduser("~/.local/share/metacomputer/agents.json")
CLI = os.path.join(LAB, "bin", "metacomputer")
TIMEOUT = 60


SAFE_PATH = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(d))


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strictargs  # noqa: E402  (argparse that refuses what a command wouldn't use)


def trusted(name):
    """(absolute path, None) of an agent's command to run, or (None, why not). Only absolute PATH entries
    are searched (a `.` on PATH would let a downloaded folder's own copy run here), and the file must be yours or
    root's and not writable by other accounts. Everything this runs gets the same PATH."""
    p = shutil.which(name, path=SAFE_PATH)
    if not p:
        return None, None
    real = os.path.realpath(p)
    try:
        st = os.stat(real)
    except OSError as e:
        return None, f"{real}: {e}"
    if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
        return None, f"{real} is owned by another account or writable by others: not run"
    return real, None


def run(argv, env=None, timeout=TIMEOUT, cwd=None):
    """(exit status, stdout, stderr); 127 when the program can't be run. PATH is SAFE_PATH."""
    env = dict(env if env is not None else os.environ, PATH=SAFE_PATH)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
                           cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def choices():
    try:
        d = json.load(open(AGENTS_FILE))
        from agentkit import manifests, registry
        return {k: d.get(k) for k in (manifests.manifest(n)["flag"] for n in registry.AGENTS)
                if d.get(k) in ("on", "off")}
    except (OSError, ValueError):
        return {}


def version(prog, env=None):
    rc, out, _ = run([prog, "--version"], env=env, timeout=20)
    return out.strip().splitlines()[0][:80] if rc == 0 and out.strip() else None


def link_target(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def hooks():
    """[(name, its setup.py)] of every registered agent with a part of its own in this command (coding-agents/<name>/
    setup.py's status and line; check), in the registry's order."""
    from agentkit import registry
    return [(n, registry.module(n, "setup")) for n in registry.AGENTS if registry.has(n, "setup.py")]


def own(mod, name):
    """mod's function `name` when its file defines it (not one it imported from the framework), else None."""
    f = getattr(mod, name, None)
    return f if callable(f) and getattr(f, "__module__", None) == mod.__name__ else None


def check_all(checks, as_json, quiet):
    """Each agent's check [(name, its function)]: its exit status, the highest. With as_json, one JSON document: one
    agent's as it prints it, several under their JSON keys (each one's printed document)."""
    if not (as_json and len(checks) > 1):
        return max([0] + [f(as_json, quiet) for _n, f in checks])
    from agentkit import manifests
    rc, docs = 0, {}
    for n, f in checks:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = max(rc, f(True, quiet))
        try:
            docs[manifests.manifest(n)["json_key"]] = json.loads(buf.getvalue())
        except ValueError:
            rc, docs[manifests.manifest(n)["json_key"]] = 1, {"error": "its check didn't print a JSON document"}
    print(json.dumps(docs, indent=2))
    return rc


def main(argv):
    ap = strictargs.Parser(prog="metacomputer agents", description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="?", default="status", choices=("status", "check"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="check: print only what failed")
    a = ap.parse_args(argv)
    if a.what == "check":                  # what metacomputer relies on in each agent that has a check of its own
        return check_all([(n, own(h, "check")) for n, h in hooks() if own(h, "check")], a.json, a.quiet)
    import agent_setup
    from agentkit import manifests, registry
    setups = dict(hooks())
    generic = agent_setup.AGENTS                # {flag: Agent}: the agents whose steps are lib/agent_setup.py's
    st, rows = {}, []
    for n in registry.AGENTS:
        m = manifests.manifest(n)
        if m["install"]["steps"] == "hooks":    # an agent with steps of its own: its part here (setup.py status, line)
            st[m["json_key"]] = setups[n].status()
            rows.append((m["label"], m["json_key"], lambda s, h=setups[n]: h.line(s)))
        else:
            ag = generic[m["flag"]]
            st[m["json_key"]] = agent_setup.status(ag, LAB, os.path.expanduser("~/.local/bin/metacomputer"),
                                                   dict(os.environ))
            rows.append((ag.label, m["json_key"], lambda s, ag=ag: more_line(ag.name, ag, s)))
    if a.json:
        print(json.dumps(st, indent=2))
        return 0
    for label, key, line in rows:
        s = st[key]
        print(f"{label:<15} {s['untrusted'] if s.get('untrusted') else line(s)}")
    return 0


def more_line(name, ag, s):
    """One line for an agent whose steps are lib/agent_setup.py's (its status)."""
    if s.get("untrusted"):
        return s["untrusted"]
    if not s["installed"]:
        return "isn't installed" + ("; you chose to leave it alone" if s.get("choice") == "off" else
                                    "; once it is: ./install.sh --only-agents (or metacomputer update)")
    ver = s.get("version") or "installed"
    if s.get("choice") == "off":
        return f"{ver}: left alone (--no-{name}; --{name} sets it up)"
    skills = all(s.get("skills", {}).values())
    if s.get("set_up"):
        return f"{ver}: set up (MCP server, {'skills' if skills else 'skills missing'}; tools {ag.tools})"
    if s.get("mcp_error"):
        return f"{ver}: its MCP server metacomputer couldn't be read ({s['mcp_error']})"
    if s.get("mcp_server"):
        return (f"{ver}: its MCP server metacomputer runs {s['mcp_server']}, not ~/.local/bin/metacomputer mcp-lazy (no "
                f"metacomputer tools); {os.path.join(LAB, 'install.sh')} --only-{name} says how to replace it")
    return f"{ver}: not set up; ./install.sh --only-{name} sets it up"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
