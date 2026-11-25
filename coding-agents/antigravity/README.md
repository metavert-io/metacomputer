# Antigravity CLI

metacomputer's code for Antigravity CLI (`agy`). Its page for people is [docs/antigravity.md](../../docs/antigravity.md)
(what an install sets up, how to use it, what's left to you); every agent is listed in
[docs/agents.md](../../docs/agents.md), and [coding-agents/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words: its install option (`--antigravity`, `--no-antigravity`, `--only-antigravity`), its command (`agy`), how it names metacomputer's tools (`call_mcp_tool` with server metacomputer), its docs; learn reads its conversations |
| `recognition.json` | how its processes are told: `agy` (and `antigravity`, its release name), and the Antigravity app's and IDE's own executables (their bundles, and the language server in a folder of Antigravity's own), a session kind of their own; `agy acp`, `agy remote-control` and the app run several conversations in one process (the vault treats them as shared) |
| `setup.py` | its install, uninstall and status steps (lib/agent_setup.py's `Agent`): `agy mcp add/remove`, a 600 s tool timeout written into its mcp_config.json, its permission rule in its settings.json (and the note that this install added it), the skills in its own folder |
| `setup.sh` | what install.sh's uninstall reads without Python: its configuration file (`ag_antigravity_config_files`) |
| `sessions.py` | learn's reader of its conversations' compact transcripts, as a format lib/transcripts.py tries; their projects from its conversation_summaries.db (lib/session_dbs.py opens it, read only) |
| `tests/recognition_cases.json` | its recognition cases, which lib/owners.py and the vault are both checked against |
| `claims.tsv` | its doc-claims rows (tests/test_doc_claims.py reads every agent's) |

It has no bench launcher and no distribution files of its own (an `agy plugin install` of the repository converts the
Gemini CLI extension). Its tests: `tests/test_agent_setup.py` (its install steps against a stand-in `agy`),
`tests/test_learn.py` (its conversations), `tests/test_session_guard.py` and `tests/test_vault_ops.py` (its recognition
cases).
