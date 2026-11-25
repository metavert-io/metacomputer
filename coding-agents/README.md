# Coding agents

Each coding agent metacomputer sets itself up for is a folder here. A pull request for one agent should change that
folder and little else: shared code never names an agent (a test fails when it does: tests/test_agents.py), and each
folder has its own reviewers (`.github/CODEOWNERS`).

The list of agents, their install options, what learn and bench do with them and where their docs are:
[docs/agents.md](../docs/agents.md) (made from the manifests). The folder is `coding-agents/`, not `agents/`: Gemini
CLI installs the repository's root as its extension (gemini-extension.json), and would load a root folder named
`agents/` as the extension's own agents.

## What an agent's folder holds

```
coding-agents/<name>/
  manifest.json        its words (lib/agentkit/manifests.py): label, flag (install.sh's --FLAG, --no-FLAG, --only-FLAG),
                       prog (its command), short (a handoff's name for it), json_key, docs (its page for people),
                       owners (CODEOWNERS), tools; install (options of its own, how a missing one is said, "generic" or
                       "hooks" steps, only_note); sessions (learn reads it: files or databases, its count's key, its
                       Transcript kinds); bench (its --agent word); release_file (a channel file carrying the
                       release's version)
  recognition.json     how its processes are told (lib/agentkit/recognition.py says each table; an app's folders in
                       lowercase): read by lib/owners.py and compiled into the vault by scripts/build-vault.sh, one
                       copy for both
  setup.py             its install, uninstall and status steps: for a "generic" agent, lib/agent_setup.py's Agent
                       (AGENT = its instance: how its MCP server is read, added and removed, where its skills go);
                       for one with hooks, its part of `metacomputer agents` (status, line, check)
  setup.sh             optional: its part of install.sh (functions only, ag_FLAG_HOOK; see Hooks)
  cli.sh               optional: its part of bin/metacomputer (ag_FLAG_cli_start, before any command runs)
  sessions.py          optional: its session reader for `metacomputer learn` (see Session readers)
  bench.py             optional: its bench launcher (bench/bench.py; see the launchers there)
  channel.py           optional: its distribution files (scripts/sync-plugin.py: COPIES, WRITTEN, LINK_TOPS; and
                       OPENAI for scripts/build-openai-plugin.py)
  README.md            its page for contributors: what's where, its tests (people's docs stay in docs/)
  claims.tsv           its doc-claims rows (tests/test_doc_claims.py reads every registered agent's)
  tests/recognition_cases.json   optional: its recognition cases (lib/owners.py and the vault are both checked
                       against them: tests/agent_argv_cases.py)
```

The framework is `lib/agentkit/` (the registry, the manifests, the recognition tables, the rule every agent's MCP
server entry is read by, and agents.sh, what bash reads) and the shared programs that call into the folders:
`lib/agent_setup.py` (the install and uninstall steps, the ownership of an entry and of a skill link, the locks,
replace-if-unchanged), `lib/agents.py` (`metacomputer agents`), `lib/owners.py` (whose session a process is),
`lib/transcripts.py` and `lib/session_dbs.py` (the session readers' interface), `lib/learn_mine.py` (learn's sources),
`bench/bench.py` (the bench harness), `scripts/sync-plugin.py` and `scripts/build-openai-plugin.py` (the channels),
and install.sh and bin/metacomputer.

## Adding an agent

1. Its folder, as above. Start from the closest one: an agent with an MCP CLI of its own is a "generic" one, an Agent
   subclass in setup.py over lib/agent_setup.py (`openclaw`, `hermes`, `gemini`, `antigravity`); one whose install is
   bigger brings its own install.sh hooks (`codex`, `claude-code`).
2. One line in `lib/agentkit/registry.py` (`AGENTS`: its order is the order of listings and steps, nothing else;
   `DEFAULT` names the agent a record without an agent field is, Claude Code: claims, handoffs, the vault's records
   and bench runs are older than the other agents' support).
3. `python3 scripts/gen-agents.py`: docs/agents.md, lib/agentkit/agents.sh (what bash reads) and CODEOWNERS follow from
   the manifests.
4. A line in the CHANGELOG.

The checks: `python3 tests/test_agents.py` (every folder's shape, the manifests and recognition tables, its bash only
setting variables and defining functions of its own, the generated files being current, and no agent named in shared
code), then the agent's own tests (its README says which: they're in tests/, its recognition cases in its folder's
tests/).

## Hooks

Shared code finds an agent's own behaviour by what its folder brings, never by its name:

- **install.sh** (from setup.sh, by its flag): `ag_FLAG_option` (an install option of its own), `ag_FLAG_wanted` (set it
  up this run), `ag_FLAG_stage` and `ag_FLAG_step` (its part of the agents' step, and of --only-FLAG), `ag_FLAG_plan`
  and `ag_FLAG_plan_link` (its line in the plan), `ag_FLAG_only_note`, `ag_FLAG_agents_note` and `ag_FLAG_done` (what
  a run says last), `ag_FLAG_uninstall_server`, `ag_FLAG_uninstall_after_links` and `ag_FLAG_uninstall_rest` (its part
  of --uninstall, in that order), `ag_FLAG_config_files` (a generic agent's configuration files, for an uninstall
  without Python). A generic agent needs none but the last: lib/agent_setup.py runs its steps.
- **bin/metacomputer** (from cli.sh): `ag_FLAG_cli_start`, before any command runs.
- **lib/agents.py** (from setup.py): an agent whose manifest's install steps are "hooks" gives `status()` and
  `line(s)`; any agent may give `check(as_json, quiet)`, defined in its setup.py itself (one it imports isn't taken).
  `agents check --json` prints one document: one agent's as it prints it, several under their JSON keys.
- **learn** (from sessions.py): an agent whose sessions are files gives `roots(env)` (folders to walk) or `files(env)`,
  `MARKER` (how a file that used metacomputer's tools is spotted) and `FORMAT = (ORDER, take)`, which
  lib/transcripts.py's parse() tries in ORDER (Antigravity CLI's 20, Gemini CLI's 30, Codex's 40); one whose sessions
  are SQLite databases gives `KIND`, `databases(env)` and `read(...)` (lib/session_dbs.py opens them, read only). A
  reader that can't be loaded or fails is noted by the agent's name in learn's report, and the others are still read.
- **bench** (from bench.py, with the harness as B): `SKILL_HOME`, `ISOLATED`, `TOKENS_ONLY`, `args` or `isolate`,
  `events` (or `ENVELOPE` and `envelope_events`), `model`, `prepare`, `before`/`after`, `refuse`, `end`.

Everything the guards decide stays shared and unchanged: the hub's guards, the vault's rules (only its recognition
tables and the records' default agent come from here), the handoff gate, the network isolation rules and the secrets
flow.
