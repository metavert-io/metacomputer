# Claude Code

metacomputer's code for Anthropic's Claude Code. Its pages for people are [docs/install.md](../../docs/install.md) and
[plugin/README.md](../../plugin/README.md) (what an install sets up: the plugin and the user-scope MCP server); every
agent is listed in [docs/agents.md](../../docs/agents.md), and [coding-agents/README.md](../README.md) says what a folder
holds. `metacomputer claude` (Claude Code inside the VM: lib/vm_claude.py) is a feature of its own, not this folder's.

| File | What it is |
|---|---|
| `manifest.json` | its words: its install options (`--claude`, `--no-claude`, `--only-claude`, `--no-plugin`), its command (`claude`), how it names metacomputer's tools (`mcp__metacomputer__*`), its docs; learn reads its session files; `bench --agent claude` (the default) |
| `recognition.json` | how its processes are told: its own `claude` executable or the native installer's version file (`.../claude/versions/2.1.325`); for the vault, node running a script named `claude*` too |
| `setup.sh` | its install step and its part of --uninstall (install.sh's hooks `ag_claude_*`): the plugin from this checkout's marketplace (or a skill link with --no-plugin), the user-scope MCP server `~/.local/bin/metacomputer mcp-lazy` (only metacomputer's own entry ever changed, read again right before), the old names retired |
| `claude_mcp.py` | its user-scope MCP servers, read exactly from its configuration and checked against `claude mcp get` (setup.sh's `user_server`, `user_recheck`) |
| `setup.py` | its part of `metacomputer agents` (status, its line) |
| `sessions.py` | where its session files are for learn (`$CLAUDE_CONFIG_DIR/projects`): lib/transcripts.py reads their format itself, which is also the stream-json bench writes |
| `bench.py` | its bench launcher: `claude -p ... --output-format stream-json`, the session's own MCP config, a budget per task |
| `channel.py` | its distribution files: plugin/'s copies of the repository's skills, get.sh, the license and the notices |
| `claims.tsv` | its doc-claims rows (tests/test_doc_claims.py reads every agent's) |

Its tests: `tests/test_plugin.py` (the plugin, the MCP server and its rule against a stand-in `claude`),
`tests/test_install.py` and `tests/test_agents.py` (its step and `metacomputer agents`), `tests/test_owners.py` and
`tests/test_vault_ops.py` (its recognition), `tests/test_learn.py` (its session files), `tests/test_bench_workdir.py`
(its launcher).
