# metacomputer with Hermes Agent

metacomputer works with Nous Research's Hermes Agent as well as Claude Code and Codex. Hermes Agent drives the virtual
computer through the same MCP tools, the same hub and the same guards; the vault, the network allowlist, handoffs and
recordings work the same way. This page covers what's set up for Hermes Agent, what it changes, and where it differs.

Checked with Hermes Agent 0.23.3 (2026-11-20) in a throwaway home: install.sh's registration (`hermes mcp list` shows
it), Hermes Agent connecting to metacomputer's MCP server (`hermes mcp test metacomputer` lists its tools), the skills,
the Agent Plugins package below (`hermes plugins validate`, and installed from a git source), and that its processes
are recognized as an agent session. No model was run: a benchmark run needs a model provider's key (Nous Portal, OpenRouter and so on).

## Set it up

Every run of `install.sh` (the `curl ... | bash` line, `metacomputer update`, the install line again) sets metacomputer
up for each coding agent installed on the Mac, Hermes Agent included, and for one installed since the last run.
Installed Hermes Agent after metacomputer? `metacomputer update`, or right away:

```bash
cd ~/metacomputer && ./install.sh --only-hermes    # or the folder `metacomputer where` shows
```

`--no-hermes` leaves Hermes Agent alone, and later runs remember it until `--hermes` (it doesn't remove what an earlier
run added: `--uninstall` does). `metacomputer agents` shows what's set up for each agent. A running Hermes gateway picks
up the new server within about a minute (or `/reload-mcp` in a session).

What it adds:

| What | Where | For |
| --- | --- | --- |
| The MCP server `metacomputer` (`~/.local/bin/metacomputer mcp-lazy`) with `timeout: 600` | `mcp_servers` in `$HERMES_HOME/config.yaml` (`~/.hermes`; the active profile's), through `hermes config set` | The tools, `mcp__metacomputer__*`, in every Hermes Agent session. Its default tool timeout (300 s) is shorter than a long `run_steps` or text typed at a person's pace. |
| The skills `metacomputer` and `cua-driver` (links into the checkout) | `$HERMES_HOME/skills/` | When and how to use the virtual computer (Hermes Agent doesn't read `~/.agents/skills` unless you add it to `skills.external_dirs`) |

By hand, the same is:

```bash
hermes config set mcp_servers.metacomputer '{"command": "'"$HOME"'/.local/bin/metacomputer", "args": ["mcp-lazy"], "timeout": 600}'
ln -s ~/metacomputer/skills/metacomputer ~/.hermes/skills/metacomputer
ln -s ~/metacomputer/.claude/skills/cua-driver ~/.hermes/skills/cua-driver
```

(`hermes mcp add` starts the server to test it and then asks questions, so the install uses `hermes config set`, which
keeps your comments and every other setting.) `hermes config set` replaces an entry of the same name, so the install
reads it first (`hermes config get mcp_servers.metacomputer --json`) and again right before it sets it. It changes
Hermes Agent's MCP server `metacomputer` only when there's none, by the rule it uses for Claude Code's and Codex's: a
server that is a local command, exactly `~/.local/bin/metacomputer` or the checkout's `bin/metacomputer`, with exactly
the argument `mcp-lazy`, is metacomputer's own. Anything else is never changed or removed: the install names it by its
program and how many arguments it has (never its arguments or values), says to run `hermes config unset
mcp_servers.metacomputer` and then `./install.sh --only-hermes`, and records the step as unfinished (exit status 1;
`metacomputer update` runs it again). `--uninstall` removes only metacomputer's own, and keeps
`~/.local/bin/metacomputer` and `mcomp` while an entry that stays runs them.

Hermes Agent approves a configured server's tools without asking (its `trust: full`, the default). By default it also
hides MCP tools behind its tool search (`tool_search`, `tool_call`); the skill tells the agent to search for
`metacomputer`. `tools.tool_search.enabled: off` in its config shows them directly.

## The Agent Plugins package

`agent-plugin/` in the repository is metacomputer as a portable [Agent Plugins](https://agent-plugins.org/specification)
1.0.0 package: `plugin.json`, the skills under `skills/`, and `mcp.json`, whose server `metacomputer` runs the package's
launcher (`bin/metacomputer`), which runs the metacomputer installed on your Mac and nothing else. It needs metacomputer
installed; then:

```bash
hermes plugins install metavert-io/metacomputer/agent-plugin --enable --force
```

Hermes Agent scans a package before it installs it, and from a source outside its catalog it stops at a "caution"
verdict until you pass `--force`: its patterns flag the skill's safety notes (that the agent never asks for the vault's
passphrase, that the VM's user has no sudo, the vault's local proxy address). Read the findings it lists first. Hermes
Agent installs packages turned off: `--enable` (or `hermes plugins enable metacomputer`) turns it on. With
install.sh's registration as well, the server in `config.yaml` wins over the package's; either one is enough. Its skills
are named `metacomputer:metacomputer` and `metacomputer:cua-driver` there. `scripts/sync-plugin.py` makes the package
from the repository's own files.

## Its shell

Hermes Agent runs shell commands with `bash -c` after a login shell's environment, captured once per session, so
`metacomputer` commands run as from your terminal; the skill runs them as `~/.local/bin/metacomputer` by its absolute
path. It marks them with `HERMES_AGENT=true`. Its other terminal backends (Docker, SSH, Modal, ...) run commands
elsewhere, where there's no metacomputer: use the local one. Its MCP servers get only a few variables (`HOME`, `PATH`,
`HERMES_HOME` and the like) plus the entry's own `env`: give a server `MCOMP_CONFIG` or `MCOMP_VM` there, not in the
shell, to point it at another computer (or use `select_computer`).

## Commands only you run

Some commands are yours alone: setting up and unlocking the vault, widening the network allowlist, approving a lesson
with your own text, saving the live-view password, saying you're done with a handoff. They need your own terminal, and
they refuse when an agent session, Hermes Agent's included, is above them in the process tree. Run them in Terminal,
iTerm or an SSH session, not through an agent.

## Where Hermes Agent differs

- **One controller at a time:** every Hermes Agent conversation runs in its gateway (`hermes chat`, `-z` and the TUI
  are its clients), one process per profile, so its conversations count as one session and don't wait for each other.
  `lib/owners.py` recognizes it as `python` running `hermes_cli`. As with Codex's app server, when two conversations
  have their tools on different computers, a command from either that names no computer refuses: name it with `--vm`.
- **The vault** treats requests from under the gateway like those from Codex's app server: its conversations share one
  process, so nothing tells one from another, and "for this session" answers, project rules and session tokens don't
  apply: each use asks.
- **`metacomputer learn`** doesn't read Hermes Agent's sessions yet: they're kept in a SQLite database
  (`$HERMES_HOME/state.db`), and with tool search on, the MCP calls are recorded inside batched `tool_call` calls.
  The hub's action log (`ACTION_LOG=on`) records every client's calls, and learn reads that.
- **The tools' names** are `mcp__metacomputer__<tool>`, as in Claude Code and Codex.

## A benchmark run

`bench/bench.py run --agent` runs Claude Code and Codex. Until it runs Hermes Agent, one benchmark task at a time can be
run by hand on a test VM with your own provider key: the task's setup, Hermes Agent headless on its prompt in a
profile whose server points at the test VM (the server doesn't inherit `MCOMP_CONFIG`), then its check:

```bash
cd ~/metacomputer && export MCOMP_CONFIG=/path/to/test-vm.env
hermes profile create bench && hermes -p bench config set mcp_servers.metacomputer \
  '{"command": "'"$HOME"'/.local/bin/metacomputer", "args": ["mcp-lazy"], "timeout": 600, "env": {"MCOMP_CONFIG": "'"$MCOMP_CONFIG"'"}}'
ln -s ~/metacomputer/skills/metacomputer ~/metacomputer/.claude/skills/cua-driver ~/.hermes/profiles/bench/skills/
bench/bench.py check mousepad-new --setup
prompt="$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["prompt"])' bench/tasks/mousepad-new.toml)"
hermes -p bench chat -q "Use your virtual computer (the metacomputer) for this. $prompt" -Q --yolo --format stream-json > run.jsonl
bench/bench.py check mousepad-new
```

## If something doesn't work

- **No metacomputer tools:** `hermes mcp list` should show `metacomputer` enabled; `/reload-mcp` in a session, or wait a
  minute for a running gateway. With tool search on, the tools appear through `tool_search`.
- **`hermes mcp test metacomputer`** starts the server and lists its tools (exit status 0 when it connects).
- **A tool call times out after 300 s:** `timeout` is missing from the entry.
