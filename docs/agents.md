# Coding agents

<!-- Made from coding-agents/<name>/manifest.json by scripts/gen-agents.py: don't edit by hand (its --check, run by tests/test_agents.py, fails a copy that isn't current). -->

The coding agents metacomputer sets itself up for: every install (and `metacomputer update`) does each one installed on this Mac, unless you said `--no-...` (`./install.sh --help`). Each agent is a folder of its own, `coding-agents/<name>/` ([coding-agents/README.md](../coding-agents/README.md)), listed in `lib/agentkit/registry.py`; `metacomputer agents` shows how each is set up here.

| Agent | Command | install.sh | Tools | `learn` reads it | `bench` | Docs | Code |
|---|---|---|---|---|---|---|---|
| Claude Code | `claude` | `--no-claude`, `--only-claude`, `--no-plugin` | `mcp__metacomputer__*` | yes (its transcript files) | `--agent claude` | [docs/install.md](install.md) | [coding-agents/claude-code/](../coding-agents/claude-code/) |
| Codex | `codex` | `--no-codex`, `--only-codex` | `mcp__metacomputer__*` | yes (its transcript files) | `--agent codex` | [docs/codex.md](codex.md) | [coding-agents/codex/](../coding-agents/codex/) |
| OpenClaw | `openclaw` | `--no-openclaw`, `--only-openclaw` | `metacomputer__*` | yes (its session databases) | `--agent openclaw` | [docs/openclaw.md](openclaw.md) | [coding-agents/openclaw/](../coding-agents/openclaw/) |
| Hermes Agent | `hermes` | `--no-hermes`, `--only-hermes` | `mcp__metacomputer__*` | yes (its session databases) | `--agent hermes` | [docs/hermes.md](hermes.md) | [coding-agents/hermes/](../coding-agents/hermes/) |
| Gemini CLI | `gemini` | `--no-gemini`, `--only-gemini` | `mcp_metacomputer_*` | yes (its transcript files) | `--agent gemini` | [docs/gemini.md](gemini.md) | [coding-agents/gemini/](../coding-agents/gemini/) |
| Antigravity CLI | `agy` | `--no-antigravity`, `--only-antigravity` | `call_mcp_tool with server metacomputer` | yes (its transcript files) | no | [docs/antigravity.md](antigravity.md) | [coding-agents/antigravity/](../coding-agents/antigravity/) |
