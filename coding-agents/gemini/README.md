# Gemini CLI

metacomputer's code for Gemini CLI. Its page for people is [docs/gemini.md](../../docs/gemini.md) (what an install sets
up, how to use it, what's left to you); every agent is listed in [docs/agents.md](../../docs/agents.md), and
[coding-agents/README.md](../README.md) says what a folder holds.

| File | What it is |
|---|---|
| `manifest.json` | its words: its install option (`--gemini`, `--no-gemini`, `--only-gemini`), its command (`gemini`), how it names metacomputer's tools (`mcp_metacomputer_*`), its docs; learn reads its session files; `bench --agent gemini`; gemini-extension.json carries the release's version |
| `recognition.json` | how its processes are told (its standalone `gemini`, `gemini.js`, its `@google/gemini-cli` package by name or by folder) |
| `setup.py` | its install, uninstall and status steps (lib/agent_setup.py's `Agent`): `gemini mcp add -s user --trust`, its settings.json read as Gemini CLI reads it (JSON with comments) |
| `setup.sh` | what install.sh's uninstall reads without Python: its configuration files (`ag_gemini_config_files`) |
| `sessions.py` | learn's reader of its session files (`~/.gemini/tmp/<project>/chats/`), as a format lib/transcripts.py tries |
| `bench.py` | its bench launcher: a throwaway HOME with a policy that allows metacomputer's tools and command, `gemini -p ... -o stream-json` |
| `channel.py` | its distribution files: gemini-extension.json, kept by hand at the repository's root |
| `tests/recognition_cases.json` | its recognition cases, which lib/owners.py and the vault are both checked against |
| `claims.tsv` | its doc-claims rows (tests/test_doc_claims.py reads every agent's) |

Its tests: `tests/test_agent_setup.py` (its install steps against a stand-in `gemini`, and gemini-extension.json),
`tests/test_learn.py` (its session files), `tests/test_session_guard.py` and `tests/test_vault_ops.py` (its recognition
cases), `tests/test_bench_workdir.py` (its launcher).
