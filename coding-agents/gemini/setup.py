"""Gemini CLI's part of lib/agent_setup.py: its install, uninstall and status steps (an Agent: what's different about its
MCP server, read from its settings.json, and its skills). install.sh runs them through lib/agent_setup.py
install|uninstall|status gemini."""
import os

import claude_mcp      # MCP_ARGS: the arguments metacomputer's server is registered with
from agent_setup import Agent, manifest_words, NAME, SETTINGS_CAP, _distinct, home, _entry_res, jsonc_loads  # the framework


class Gemini(Agent):
    """Gemini CLI: user-scope MCP servers in ~/.gemini/settings.json (JSON with comments; GEMINI_CLI_HOME moves it), set
    with `gemini mcp add -s user`, which replaces an entry of the same name without asking, so the entry is read (from
    the file: the CLI has no lookup) right before. `--trust`: the tools act only on the virtual computer, through the
    hub's guards (Codex's `default_tools_approval_mode = "approve"` likewise). Its default tool timeout is 600 s."""
    name, label, prog, tools, doc = manifest_words("gemini")
    note = ("Gemini CLI starts MCP servers only in folders you trust: it asks the first time you start it in a folder "
            "(docs/gemini.md)")

    def env(self, env):
        return dict(env, GEMINI_CLI_NO_RELAUNCH="true")      # one process for a quick command, not two

    def base(self, env):
        return env.get("GEMINI_CLI_HOME") or home(env)

    def settings(self, env):
        return os.path.join(self.base(env), ".gemini", "settings.json")

    reads_files = True

    def skills_dir(self, env):
        return os.path.join(self.base(env), ".agents", "skills")

    def config_files(self, env):
        return _distinct([self.settings(env), os.path.join(home(env), ".gemini", "settings.json")])

    def read(self, prog, env):
        path = self.settings(env)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read(SETTINGS_CAP + 1)
        except (FileNotFoundError, NotADirectoryError):
            return {"kind": "absent"}
        except (OSError, UnicodeDecodeError):
            return {"kind": "error", "why": "its settings.json couldn't be read"}
        if len(text) > SETTINGS_CAP:          # what's past the read could be anything, metacomputer's entry included
            return {"kind": "error", "why": "its settings.json is larger than 4 MB, so it wasn't read in full"}
        if not text.strip():
            return {"kind": "absent"}
        try:
            d = jsonc_loads(text)
        except (ValueError, RecursionError):
            return {"kind": "error", "why": "its settings.json isn't JSON this can read"}
        servers = d.get("mcpServers") if isinstance(d, dict) else None
        if servers is None:
            return {"kind": "absent"}
        if not isinstance(servers, dict):
            return {"kind": "error", "why": "its mcpServers isn't a table"}
        if NAME not in servers:
            return {"kind": "absent"}
        res = _entry_res(servers[NAME])
        res["raw"] = servers[NAME]
        return res

    def add_argv(self, prog, link):
        return [prog, "mcp", "add", "-s", "user", "--trust", NAME, link, *claude_mcp.MCP_ARGS]

    def remove_argv(self, prog):
        return [prog, "mcp", "remove", "-s", "user", NAME]

    def add_line(self, link_shown):
        return f"gemini mcp add -s user --trust {NAME} {link_shown} mcp-lazy"

    def remove_line(self):
        return f"gemini mcp remove -s user {NAME}"


AGENT = Gemini()
