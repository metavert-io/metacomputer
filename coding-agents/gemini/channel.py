"""Gemini CLI's distribution files. gemini-extension.json, at the repository's root (Gemini CLI installs the repository
as its extension: `gemini extensions install` of it), is kept by hand: its MCP server runs agent-plugin/bin/metacomputer
(Hermes Agent's package's launcher) and its context file is agent-plugin/GEMINI.md, and scripts/release.sh sets its
version to the release's (its manifest's "release_file"). So it has nothing for scripts/sync-plugin.py to copy or write."""

COPIES = []
WRITTEN = []
LINK_TOPS = ()
KEPT = ("gemini-extension.json",)       # its files kept by hand, for the index (docs/agents.md)
