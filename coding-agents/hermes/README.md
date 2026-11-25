# Hermes Agent

metacomputer's code for Hermes Agent. Its page for people is [docs/hermes.md](../../docs/hermes.md) (what an install sets
up, how to use it, what's left to you); every agent is listed in [docs/agents.md](../../docs/agents.md), and
[coding-agents/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words: its install option (`--hermes`, `--no-hermes`, `--only-hermes`), its command (`hermes`), how it names metacomputer's tools (`mcp__metacomputer__*`), its docs; learn reads its session databases; `bench --agent hermes` |
| `recognition.json` | how its processes are told (`hermes`, `hermes-agent`, `hermes-acp`, its `hermes_cli` package and `python -m hermes_cli`); its gateway runs every Hermes conversation's tools (the vault treats it as shared) |
| `setup.py` | its install, uninstall and status steps (lib/agent_setup.py's `Agent`): the profile in use (`hermes config path`), `hermes config get/set/unset mcp_servers.metacomputer` with a 600 s timeout, the skills in `$HERMES_HOME/skills` |
| `setup.sh` | what install.sh's uninstall reads without Python: its configuration files (`ag_hermes_config_files`) |
| `sessions.py` | learn's reader of its SQLite session databases, every profile's (lib/session_dbs.py opens them, read only) |
| `bench.py` | its bench launcher: a Hermes home of the session's own, `hermes chat -q ... --format stream-json`; refused while Hermes would load a `.env` from outside its home |
| `channel.py` | its distribution files: `agent-plugin/`, the Agent Plugins package it installs (scripts/sync-plugin.py writes them) |
| `tests/recognition_cases.json` | its recognition cases, which lib/owners.py and the vault are both checked against |
| `claims.tsv` | its doc-claims rows (tests/test_doc_claims.py reads every agent's) |

Its tests: `tests/test_agent_setup.py` (its install steps against a stand-in `hermes`), `tests/test_learn.py` (its session
databases), `tests/test_session_guard.py` and `tests/test_vault_ops.py` (its recognition cases), `tests/test_plugin.py`
(the Agent Plugins package), `tests/test_bench_workdir.py` (its launcher).
