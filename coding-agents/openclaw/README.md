# OpenClaw

metacomputer's code for OpenClaw. Its page for people is [docs/openclaw.md](../../docs/openclaw.md)
(what an install sets up, how to use it, what's left to you); every agent is listed in
[docs/agents.md](../../docs/agents.md), and [coding-agents/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words: its install option (`--openclaw`, `--no-openclaw`, `--only-openclaw`), its command (`openclaw`), how it names metacomputer's tools (`metacomputer__*`), its docs; learn reads its session databases; `bench --agent openclaw` |
| `recognition.json` | how its processes are told (`openclaw`, `openclaw.mjs` and the `openclaw-gateway` it names itself, its package, and its own helpers under `dist/process/supervisor/`, which aren't a session); its gateway runs every conversation in one process (the vault treats it as shared) |
| `setup.py` | its install, uninstall and status steps (lib/agent_setup.py's `Agent`): `openclaw mcp show/add/unset` with a 600 s timeout, its configuration files and profiles for an uninstall, `openclaw skills info` |
| `setup.sh` | what install.sh's uninstall reads without Python: its configuration files (`ag_openclaw_config_files`) |
| `sessions.py` | learn's reader of its SQLite session databases (lib/session_dbs.py opens them, read only) |
| `bench.py` | its bench launcher: a throwaway HOME with a state folder of its own, `openclaw agent exec --json` |
| `tests/recognition_cases.json` | its recognition cases, which lib/owners.py and the vault are both checked against |
| `claims.tsv` | its doc-claims rows (tests/test_doc_claims.py reads every agent's) |

Its tests: `tests/test_agent_setup.py` (its install steps against a stand-in `openclaw`), `tests/test_learn.py` (its
session databases), `tests/test_session_guard.py` and `tests/test_vault_ops.py` (its recognition cases),
`tests/test_bench_workdir.py` (its launcher).
