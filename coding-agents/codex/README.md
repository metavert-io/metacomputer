# Codex

metacomputer's code for OpenAI's Codex. Its page for people is [docs/codex.md](../../docs/codex.md) (what an install sets
up, how to use it, what's left to you); every agent is listed in [docs/agents.md](../../docs/agents.md), and
[coding-agents/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words: its install option (`--codex`, `--no-codex`, `--only-codex`), its command (`codex`), how it names metacomputer's tools (`mcp__metacomputer__*`), its docs; learn reads its session files; `bench --agent codex` |
| `recognition.json` | how its processes are told (its native `codex`); its app server (`codex app-server`: the IDE extensions, the desktop app, the terminal app's background server) runs every conversation in one process (the vault treats it as shared) |
| `setup.sh` | its install step and its part of --uninstall (install.sh's hooks `ag_codex_step`, `ag_codex_plan`, `ag_codex_uninstall_server`, `ag_codex_uninstall_rest`): `codex mcp add` with a 600 s timeout and its tools approved (config.toml changed only in metacomputer's own table, read again and compared), the skills in ~/.agents/skills, metacomputer's rules for its sandbox (from config/codex/metacomputer.rules.in) |
| `setup.py` | its part of `metacomputer agents` (status, its line) and `metacomputer agents check` (what metacomputer relies on in this Mac's Codex, in a throwaway CODEX_HOME), and `codex-entry`, its MCP server read whole for setup.sh |
| `cli.sh` | its part of bin/metacomputer: inside Codex's sandbox, a command stops at once (exit 77) and says how to run it outside |
| `sessions.py` | learn's reader of its session files (`$CODEX_HOME/sessions/.../rollout-*.jsonl`), as a format lib/transcripts.py tries |
| `bench.py` | its bench launcher: `codex exec --json` with the work folder trusted, its trust table put back after |
| `channel.py` | its distribution files: the repository's Codex marketplace (.agents/plugins/marketplace.json) and the plugin's Codex manifest (plugin/.codex-plugin/plugin.json); scripts/build-openai-plugin.py packages the same for OpenAI's plugin directory |
| `claims.tsv` | its doc-claims rows (tests/test_doc_claims.py reads every agent's) |

Its tests: `tests/test_install.py` and `tests/test_agents.py` (its install step and `metacomputer agents` against a
stand-in `codex`), `tests/test_learn.py` (its session files), `tests/test_vault_ops.py` (its app server),
`tests/test_plugin.py` (the marketplace, the manifest and the OpenAI package), `tests/test_bench_workdir.py` (its
launcher).
