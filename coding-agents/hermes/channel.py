"""Hermes Agent's distribution files: agent-plugin/, the portable Agent Plugins 1.0.0 package
(https://agent-plugins.org/specification) that Hermes Agent installs. scripts/sync-plugin.py keeps it (called with
itself as S): plugin.json and mcp.json written here, the two skills one level under skills/ as the format wants (the
driver pack's links to ../../../skills/metacomputer/ become ../metacomputer/ in its copy: S.relink), the license and
the notices. Its own files are README.md and bin/metacomputer (the launcher its MCP server runs; gemini-extension.json
runs it too)."""
import json

COPIES = [  # (source in the repository, copy, how the copy's bytes come from the source's: None = as they are)
    ("skills/metacomputer", "agent-plugin/skills/metacomputer", None),
    (".claude/skills/cua-driver", "agent-plugin/skills/cua-driver", "relink"),
    ("LICENSE", "agent-plugin/LICENSE", None),
    ("THIRD_PARTY_NOTICES.md", "agent-plugin/THIRD_PARTY_NOTICES.md", None),
]
LINK_TOPS = ("agent-plugin",)           # its folder holds regular files only
SERVER = "metacomputer"
TOOL_TIMEOUT = 600          # seconds, as install.sh gives the MCP server in Codex, OpenClaw and Hermes Agent


def plugin_json(S):
    """agent-plugin/plugin.json: the plugin's listing fields (S.listing()), with words for any agent."""
    claude = S.listing()
    d = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": claude["name"],
        "description": ("Gives your agent a disposable computer of its own: an isolated Ubuntu 24.04 desktop VM on your "
                        "Mac that it operates like a person, through Cua Driver's MCP tools, with skills for when and how "
                        "to use it. Needs macOS on Apple silicon and metacomputer installed (its install script builds "
                        "the VM)."),
        "author": claude["author"],
        "homepage": claude["homepage"],
        "repository": claude["repository"],
        "license": claude["license"],
        "keywords": claude["keywords"],
        # Hermes Agent's settings for the server: a tool-call timeout as Codex's (its default is 300 s)
        "extensions": {"com.nousresearch.hermes": {"servers": {SERVER: {"timeout": TOOL_TIMEOUT}}}},
    }
    return (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def mcp_json(S):
    """agent-plugin/mcp.json: the MCP server runs the package's launcher (the format takes a command in the package or
    a bare name, never an absolute path), which runs the metacomputer installed on this Mac."""
    d = {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
         "mcpServers": {SERVER: {"type": "stdio", "command": "./bin/metacomputer", "args": ["mcp-lazy"]}}}
    return (json.dumps(d, indent=2) + "\n").encode("utf-8")


WRITTEN = [("agent-plugin/plugin.json", plugin_json), ("agent-plugin/mcp.json", mcp_json)]
