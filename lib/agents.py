#!/usr/bin/env python3
"""The coding agents metacomputer is set up for on this Mac: Claude Code, Codex (docs/codex.md), OpenClaw
(docs/openclaw.md), Hermes Agent (docs/hermes.md), Gemini CLI (docs/gemini.md) and Antigravity CLI
(docs/antigravity.md).

  metacomputer agents [--json]          each agent: installed (and its version), how it's set up, your choice
                                        (install.sh --no-claude, --no-codex, ..., remembered for later runs). For
                                        Claude Code: the skills (the plugin, or a skill link with --no-plugin) and
                                        the tools (the user-scope MCP server metacomputer, read as install.sh reads
                                        it, lib/claude_mcp.py: its command and exact arguments checked; the plugin
                                        declares none, and an older copy of it that still brings its own is
                                        reported), and this checkout's command that adds them. For OpenClaw,
                                        Hermes Agent, Gemini CLI and Antigravity CLI: the MCP server metacomputer as
                                        each keeps it (lib/agent_setup.py, read by the agent's own CLI, or Gemini
                                        CLI's settings.json and Antigravity CLI's mcp_config.json) and the skill links
  metacomputer agents check [--json]    what metacomputer relies on in this Mac's Codex, checked without a model and
                                        without touching your Codex settings (a throwaway CODEX_HOME): its rules
                                        checker, the sandbox's marker, `codex mcp` and the two settings install.sh
                                        adds (by Codex's strict config loading), and the format of your session
                                        files, tool calls included (for `learn`). Only a `codex` from an absolute
                                        PATH entry, yours or root's and not writable by others, is run

install.sh runs the check after setting Codex up, so an update to Codex that changes one of these is reported
there, and `metacomputer update` runs install.sh. Exit status of `check`: 0 all passed (or no Codex), 1 a check
failed. Stdlib only; Python 3.11 or later (tomllib).
"""
import argparse
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
    """(absolute path, None) of the `claude` or `codex` command to run, or (None, why not). Only absolute PATH entries
    are searched (a `.` on PATH would let a downloaded folder's own `codex` run here), and the file must be yours or
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
        return {k: d.get(k) for k in ("claude", "codex", "openclaw", "hermes", "gemini", "antigravity")
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


def claude_status():
    prog, why = trusted("claude")
    st = {"installed": bool(prog or why), "version": version(prog) if prog else None, "choice": choices().get("claude")}
    if why:
        st["untrusted"] = why
    if not prog:
        return st
    rc, out, _ = run([prog, "plugin", "list", "--json"])
    plugin = None
    plugin_server = False      # an older copy of the plugin that still declares its own MCP server
    if rc == 0:
        try:
            for p in json.loads(out):
                if p.get("id") == "metacomputer@metacomputer" and p.get("scope") == "user":
                    plugin = "on" if p.get("enabled") else "off"
                    plugin_server = bool(p.get("mcpServers")) and plugin == "on"
        except (ValueError, AttributeError, TypeError):
            pass
    st["plugin"] = plugin
    st["plugin_mcp_server"] = plugin_server
    srv = user_server(prog)
    st["user_mcp_server"] = srv["kind"] not in ("absent", "error")
    st["mcp_server"] = srv["words"] if st["user_mcp_server"] else None
    st["mcp_ok"] = srv["owner"] == "ok"
    if srv["kind"] == "error":
        st["mcp_error"] = srv["words"]
    st["skill_link"] = link_target(os.path.expanduser("~/.claude/skills/metacomputer"))
    st["set_up"] = st["mcp_ok"] or plugin_server
    return st


def user_server(prog):
    """Claude Code's user-scope MCP server metacomputer, read as install.sh reads it (lib/claude_mcp.py: the entry in
    Claude Code's configuration, checked against `claude mcp get` from an empty folder; its exact arguments compared):
    {"owner", "kind", "words"}. The words are claude_mcp's: metacomputer's own command and arguments, or for anything
    else only its program's name and how many arguments it has, or its transport; never an argument, a variable, a URL
    or the entry (they can carry credentials)."""
    import claude_mcp
    env = dict(os.environ, PATH=SAFE_PATH)
    res = claude_mcp.user_entry(prog, "metacomputer", env)
    own = claude_mcp.owner(res, LAB, os.path.expanduser("~/.local/bin/metacomputer"))
    return {"owner": own, "kind": res["kind"], "words": claude_mcp.words(res, env, own)}


INSTALL_CLAUDE = f"{os.path.join(LAB, 'install.sh')} --only-claude"


def claude_skills(s):
    return ("the plugin's skills" if s.get("plugin") == "on" else
            "a skill link" if s.get("skill_link") else "no metacomputer skill")


def claude_way(s):
    """How Claude Code is set up, in words: where the skills come from and where the tools come from."""
    skills = claude_skills(s)
    if s.get("mcp_ok") and s.get("plugin_mcp_server"):
        return (f"{skills} and the MCP server metacomputer; the plugin is an older copy that brings its own too, so the "
                "tools are listed twice: claude plugin update metacomputer@metacomputer")
    if s.get("plugin_mcp_server"):
        return f"{skills}, and the tools from an older copy of the plugin; {INSTALL_CLAUDE} moves them to the " \
               "MCP server metacomputer"
    return f"{skills} and the MCP server metacomputer"


def claude_missing(s):
    """Claude Code installed, but without metacomputer's tools: why, and what this checkout runs to add them."""
    skills = claude_skills(s)
    if s.get("mcp_error"):
        return f"{skills}; the MCP server metacomputer couldn't be read ({s['mcp_error']})"
    if s.get("user_mcp_server"):
        return (f"{skills}, but the user-scope MCP server metacomputer runs {s['mcp_server']}, not "
                f"~/.local/bin/metacomputer mcp-lazy (no metacomputer tools); {INSTALL_CLAUDE} says how to replace it")
    return f"{skills}, but no MCP server metacomputer (no tools); {INSTALL_CLAUDE} adds it"


def hooks():
    """[(name, its setup.py)] of every registered agent with a part of its own in this command (coding-agents/<name>/
    setup.py's status and line; check), in the registry's order."""
    from agentkit import registry
    return [(n, registry.module(n, "setup")) for n in registry.AGENTS if registry.has(n, "setup.py")]


def main(argv):
    ap = strictargs.Parser(prog="metacomputer agents", description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="?", default="status", choices=("status", "check"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="check: print only what failed")
    a = ap.parse_args(argv)
    if a.what == "check":                  # what metacomputer relies on in each agent that has a check of its own
        rc = 0
        for _n, h in hooks():
            if hasattr(h, "check"):
                rc = max(rc, h.check(a.json, a.quiet))
        return rc
    import agent_setup
    from agentkit import manifests
    own = dict(hooks())
    st = {"claude_code": claude_status()}
    st[manifests.manifest("codex")["json_key"]] = own["codex"].status()
    more = agent_setup.AGENTS
    for name, ag in more.items():
        st[name] = agent_setup.status(ag, LAB, os.path.expanduser("~/.local/bin/metacomputer"), dict(os.environ))
    if a.json:
        print(json.dumps(st, indent=2))
        return 0
    for label, s in (("Claude Code", st["claude_code"]),):
        if s.get("untrusted"):
            print(f"{label:<15} {s['untrusted']}")
            continue
        if not s["installed"]:
            how = "isn't installed" + ("; you chose to leave it alone" if s.get("choice") == "off" else
                                       "; once it is: ./install.sh --only-agents (or metacomputer update)")
        elif s.get("choice") == "off":
            how = f"{s['version'] or 'installed'}: left alone (--no-claude; --claude sets it up)"
        elif s.get("set_up"):
            how = f"{s['version'] or 'installed'}: set up ({claude_way(s)})"
        elif s.get("plugin") == "on" or s.get("skill_link") or s.get("user_mcp_server") or s.get("mcp_error"):
            how = f"{s['version'] or 'installed'}: {claude_missing(s)}"
        else:
            how = f"{s['version'] or 'installed'}: not set up; ./install.sh --only-agents sets it up"
        print(f"{label:<15} {how}")
    cs = st[manifests.manifest("codex")["json_key"]]
    print(f"{manifests.label('codex'):<15} {cs['untrusted'] if cs.get('untrusted') else own['codex'].line(cs)}")
    for name, ag in more.items():
        print(f"{ag.label:<15} {more_line(name, ag, st[name])}")
    return 0


def more_line(name, ag, s):
    """One line for OpenClaw, Hermes Agent, Gemini CLI or Antigravity CLI (lib/agent_setup.py status)."""
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
