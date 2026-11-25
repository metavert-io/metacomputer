"""Claude Code's part of `metacomputer agents` (its own install steps are in setup.sh): what's set up in this Mac's Claude
Code (status, line): the skills (the plugin, or a skill link with --no-plugin) and the tools (the user-scope MCP server
metacomputer, read as install.sh reads it, claude_mcp.py: its command and exact arguments checked; the plugin declares
none, and an older copy of it that still brings its own is reported), and this checkout's command that adds them.

Python 3.11 or later, as lib/agents.py; stdlib only."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from agents import LAB, SAFE_PATH, choices, link_target, run, trusted, version  # noqa: E402  (lib/agents.py's helpers)


def _claude_mcp():
    """claude_mcp.py, beside this file: Claude Code's user-scope MCP servers, read exactly."""
    from agentkit import registry
    return registry.module("claude-code", "claude_mcp")


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
    """Claude Code's user-scope MCP server metacomputer, read as install.sh reads it (claude_mcp.py, beside this file:
    the entry in Claude Code's configuration, checked against `claude mcp get` from an empty folder; its exact arguments
    compared):
    {"owner", "kind", "words"}. The words are claude_mcp's: metacomputer's own command and arguments, or for anything
    else only its program's name and how many arguments it has, or its transport; never an argument, a variable, a URL
    or the entry (they can carry credentials)."""
    claude_mcp = _claude_mcp()
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


def line(s):
    """Its line in `metacomputer agents` (after its name)."""
    if not s["installed"]:
        return "isn't installed" + ("; you chose to leave it alone" if s.get("choice") == "off" else
                                    "; once it is: ./install.sh --only-agents (or metacomputer update)")
    if s.get("choice") == "off":
        return f"{s['version'] or 'installed'}: left alone (--no-claude; --claude sets it up)"
    if s.get("set_up"):
        return f"{s['version'] or 'installed'}: set up ({claude_way(s)})"
    if s.get("plugin") == "on" or s.get("skill_link") or s.get("user_mcp_server") or s.get("mcp_error"):
        return f"{s['version'] or 'installed'}: {claude_missing(s)}"
    return f"{s['version'] or 'installed'}: not set up; ./install.sh --only-agents sets it up"


status = claude_status
