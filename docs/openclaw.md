# metacomputer with OpenClaw

metacomputer works with OpenClaw as well as Claude Code and Codex: give OpenClaw a computer of its own instead of your
Mac. OpenClaw drives the virtual computer through the same MCP tools, the same hub and the same guards; the vault, the
network allowlist, handoffs and recordings work the same way. This page covers what's set up for OpenClaw, what it
changes, and where it differs.

Checked with OpenClaw 2026.11.7 in a throwaway home: install.sh's registration (`openclaw mcp status`), OpenClaw
connecting to metacomputer's MCP server (`openclaw mcp probe metacomputer`: its tools, as `metacomputer__*`), the skills
(`openclaw skills info`), the Agent Plugins package below (`openclaw plugins install`), and that its processes are
recognized as an agent session. No model was run: a benchmark run needs a model provider's key.

## Set it up

Every run of `install.sh` (the `curl ... | bash` line, `metacomputer update`, the install line again) sets metacomputer
up for each coding agent installed on the Mac, OpenClaw included, and for one installed since the last run. Installed
OpenClaw after metacomputer? `metacomputer update`, or right away:

```bash
cd ~/metacomputer && ./install.sh --only-openclaw    # or the folder `metacomputer where` shows
```

`--no-openclaw` leaves OpenClaw alone, and later runs remember it until `--openclaw` (it doesn't remove what an earlier
run added: `--uninstall` does). `metacomputer agents` shows what's set up for each agent. A running gateway picks up the
new server by itself.

What it adds:

| What | Where | For |
| --- | --- | --- |
| The MCP server `metacomputer` (`~/.local/bin/metacomputer mcp-lazy`) with a request timeout of 600 s | `mcp.servers` in `~/.openclaw/openclaw.json`, through `openclaw mcp add --no-probe` | The tools, `metacomputer__*`, in every OpenClaw session. Its default request timeout (60 s) is shorter than a long `run_steps` or text typed at a person's pace. |
| The skills `metacomputer` and `cua-driver` (links into the checkout) | `~/.agents/skills/` (which Codex and Gemini CLI read too) | When and how to use the virtual computer. The install checks with `openclaw skills info` that OpenClaw offers both. |

By hand, the same is:

```bash
openclaw mcp add metacomputer --command ~/.local/bin/metacomputer --arg mcp-lazy --timeout 600 --no-probe
ln -s ~/metacomputer/skills/metacomputer ~/.agents/skills/metacomputer
ln -s ~/metacomputer/.claude/skills/cua-driver ~/.agents/skills/cua-driver
```

OpenClaw reads `~/.agents/skills` only with its default state folder: with `OPENCLAW_STATE_DIR` (or a profile) of your
own, link the two skills into that folder's `skills/` instead; the install says when OpenClaw doesn't list them.

The install changes OpenClaw's MCP server `metacomputer` only when there's none (`openclaw mcp add` refuses a name
that's there), by the rule it uses for Claude Code's and Codex's: a server that is a local command, exactly
`~/.local/bin/metacomputer` or the checkout's `bin/metacomputer`, with exactly the argument `mcp-lazy`, is metacomputer's
own (read with `openclaw mcp show metacomputer --json`, again right before any change). Anything else is never changed or
removed: the install names it by its program and how many arguments it has (never its arguments or values), says to run
`openclaw mcp unset metacomputer` and then `./install.sh --only-openclaw`, and records the step as unfinished (exit
status 1; `metacomputer update` runs it again). `--uninstall` removes only metacomputer's own, and keeps
`~/.local/bin/metacomputer` and `mcomp` while an entry that stays runs them. OpenClaw refuses a write that leaves its
configuration much smaller than it was; when it refuses the removal, the uninstall says so and gives the command.

By default OpenClaw hides MCP tools behind its tool search (`dispatch_action`); the skill tells the agent to search for
`metacomputer`. `tools.toolSearch: false` in its configuration shows them directly.

## The Agent Plugins package

`agent-plugin/` in the repository is metacomputer as a portable [Agent Plugins](https://agent-plugins.org/specification)
1.0.0 package (docs/hermes.md has the details), and OpenClaw installs it as one: the MCP server `metacomputer` (the
package's launcher, which runs the metacomputer installed on your Mac and nothing else) and both skills. From a
checkout:

```bash
openclaw plugins install ~/metacomputer/agent-plugin --force
```

(`--force` confirms a source that isn't ClawHub.) With install.sh's registration as well, your own `mcp.servers` entry
wins over the package's. ClawHub's package listings take OpenClaw's own plugins and Codex, Claude and Cursor bundles,
not this format yet; the `metacomputer` skill can be listed there on its own.

## Its shell

OpenClaw runs shell commands (its `exec` tool) with your `$SHELL` (`zsh -f -c`, not a login shell) after a snapshot of
your shell's startup files, with PATH from a login shell, so `metacomputer` commands run as from your terminal; the skill
runs them as `~/.local/bin/metacomputer` by its absolute path. It marks them with `OPENCLAW_SHELL=exec`. Its sandbox
backends (Docker and the like; off by default) run commands elsewhere, where there's no metacomputer. Its MCP servers
get only `HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL` and `TERM` plus the entry's own `env`: give the server `MCOMP_CONFIG`
or `MCOMP_VM` there to point it at another computer (or use `select_computer`).

## Commands only you run

Some commands are yours alone: setting up and unlocking the vault, widening the network allowlist, approving a lesson
with your own text, saving the live-view password, saying you're done with a handoff. They need your own terminal, and
they refuse when an agent session, OpenClaw's included, is above them in the process tree. Run them in Terminal, iTerm
or an SSH session, not through an agent.

## Where OpenClaw differs

- **One controller at a time:** OpenClaw's gateway (its process is named `openclaw-gateway`) runs every conversation, so
  they count as one session and don't wait for each other; a one-off `openclaw agent exec` runs in a process of its own
  (named `openclaw`). As with Codex's app server, when two of the gateway's conversations have their tools on different
  computers, a command from either that names no computer refuses: name it with `--vm`.
- **The vault** treats requests from under the gateway like those from Codex's app server: "for this session" answers,
  project rules and session tokens don't apply, and each use asks. A one-off `openclaw agent exec` is a session.
- **`metacomputer learn`** doesn't read OpenClaw's sessions yet: they're kept in SQLite databases
  (`~/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite`, partly compressed), and with tool search on, the MCP calls
  are recorded as `dispatch_action` calls. The hub's action log (`ACTION_LOG=on`) records every client's calls, and
  learn reads that.
- **The tools' names** are `metacomputer__<tool>` (no `mcp__` prefix).

## A benchmark run

`bench/bench.py run --agent` runs Claude Code and Codex. Until it runs OpenClaw, one benchmark task at a time can be run
by hand on a test VM with your own provider key: the task's setup, a one-off `openclaw agent exec` (no gateway; its
configuration for that run only, tool search off), then its check:

```bash
cd ~/metacomputer && export MCOMP_CONFIG=/path/to/test-vm.env
bench/bench.py check mousepad-new --setup
prompt="$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["prompt"])' bench/tasks/mousepad-new.toml)"
openclaw agent exec "Use your virtual computer (the metacomputer) for this. $prompt" --json --model PROVIDER/MODEL \
  --config-override '{"mcp": {"servers": {"metacomputer": {"command": "'"$HOME"'/.local/bin/metacomputer", "args": ["mcp-lazy"], "requestTimeoutMs": 600000, "env": {"MCOMP_CONFIG": "'"$MCOMP_CONFIG"'"}}}}, "tools": {"toolSearch": false}}' > run.json
bench/bench.py check mousepad-new
```

## If something doesn't work

- **No metacomputer tools:** `openclaw mcp status` should show `metacomputer` configured and enabled; `openclaw mcp
  probe metacomputer` starts it and lists its tools.
- **A tool call times out after 60 s:** `requestTimeoutMs` is missing from the entry.
- **The skills aren't offered:** `openclaw skills info metacomputer` says why (a state folder of its own: see above).
