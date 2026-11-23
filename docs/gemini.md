# metacomputer with Gemini CLI

metacomputer works with Google's Gemini CLI as well as Claude Code and Codex. Gemini CLI drives the virtual computer
through the same MCP tools, the same hub and the same guards; the vault, the network allowlist, handoffs, recordings
and lessons work the same way. This page covers what's set up for Gemini CLI, what it changes, and where it differs.

Checked with Gemini CLI 0.70.0 (2026-11-19) in a throwaway home: install.sh's registration, Gemini CLI connecting to
metacomputer's MCP server (`gemini mcp list`: Connected), its skills and the extension below, and that its processes
are recognized as an agent session. No model was run: a benchmark run needs a Gemini API key or a Code Assist
subscription (below).

**Gemini CLI and Antigravity CLI.** Since 2026-06-18 Google serves free and Google AI Pro/Ultra users through its
successor, Antigravity CLI (`agy`); Gemini CLI goes on for Gemini Code Assist Standard and Enterprise and for paid
Gemini API keys, and is still released. **Antigravity CLI is not yet supported**: it keeps its own MCP configuration
(`~/.gemini/config/mcp_config.json`) and plugins, which this install doesn't set up.

## Set it up

Every run of `install.sh` (the `curl ... | bash` line, `metacomputer update`, the install line again) sets metacomputer
up for each coding agent installed on the Mac, Gemini CLI included, and for one installed since the last run. Installed
Gemini CLI after metacomputer? `metacomputer update`, or right away:

```bash
cd ~/metacomputer && ./install.sh --only-gemini    # or the folder `metacomputer where` shows
```

`--no-gemini` leaves Gemini CLI alone, and later runs remember it until `--gemini` (it doesn't remove what an earlier run
added: `--uninstall` does). `metacomputer agents` shows what's set up for each agent. Start a new Gemini CLI session
afterwards.

What it adds:

| What | Where | For |
| --- | --- | --- |
| The MCP server `metacomputer` (`~/.local/bin/metacomputer mcp-lazy`) with `"trust": true`, at user scope | `~/.gemini/settings.json` (`$GEMINI_CLI_HOME/.gemini`), through `gemini mcp add -s user` | The tools, `mcp_metacomputer_*`, in every Gemini CLI session. Gemini CLI otherwise asks before every tool call (every click); these tools act only on the virtual computer, through the hub's guards. Its own tool timeout (10 minutes) is long enough. |
| The skills `metacomputer` and `cua-driver` (links into the checkout) | `~/.agents/skills/` (which Codex and OpenClaw read too) | When and how to use the virtual computer |

By hand, the same is:

```bash
gemini mcp add -s user --trust metacomputer ~/.local/bin/metacomputer mcp-lazy
ln -s ~/metacomputer/skills/metacomputer ~/.agents/skills/metacomputer
ln -s ~/metacomputer/.claude/skills/cua-driver ~/.agents/skills/cua-driver
```

`gemini mcp add` replaces a server of the same name without asking, so the install reads the entry first (from
`settings.json`, which is JSON with comments; Gemini CLI has no lookup command) and again right before it adds one. It
changes Gemini CLI's MCP server `metacomputer` only when there's none, by the rule it uses for Claude Code's and Codex's:
a server that is a local command, exactly `~/.local/bin/metacomputer` or the checkout's `bin/metacomputer`, with exactly
the argument `mcp-lazy`, is metacomputer's own. Anything else is never changed or removed: the install names it by its
program and how many arguments it has (never its arguments or values), says to run `gemini mcp remove -s user
metacomputer` and then `./install.sh --only-gemini`, and records the step as unfinished (exit status 1; `metacomputer
update` runs it again). A `settings.json` it can't read, as Gemini CLI reads it, is never taken for one without the
server. `--uninstall` removes only metacomputer's own, and keeps `~/.local/bin/metacomputer` and `mcomp` while an entry
that stays runs them. Remove `"trust": true` from the entry yourself if you'd rather be asked before each tool call.

**Trusted folders.** Gemini CLI turns MCP servers off, user-scope ones included, in a folder you haven't trusted: it
asks the first time you start it in a folder, and `/mcp` shows the server as disabled until you trust the folder.
(Folder trust is on by default in Gemini CLI 0.70.0, though its documentation says otherwise.)

## The extension

The repository is also a Gemini CLI extension: `gemini-extension.json` at its root names the MCP server `metacomputer`
(the launcher in `agent-plugin/bin/metacomputer`, which runs the metacomputer installed on your Mac and nothing else),
the context file `agent-plugin/GEMINI.md`, and Gemini CLI loads the `metacomputer` skill from `skills/`. It needs
metacomputer installed; then:

```bash
gemini extensions install https://github.com/metavert-io/metacomputer
```

With install.sh's registration as well, the one in `settings.json` wins and the extension's server isn't started, and
Gemini CLI notes the two copies of the skill; either one is enough. Gemini CLI doesn't pass `trust` to an extension's
server, so through the extension alone it asks before each tool call. Its extensions gallery lists public repositories
with `gemini-extension.json` at the root and the `gemini-cli-extension` topic.

## Its shell and sandbox

Gemini CLI runs shell commands with `bash -c`, not a login shell, in the environment it started with (it adds
`GEMINI_CLI=1`), so `metacomputer` commands run as from your terminal. The skill runs them as
`~/.local/bin/metacomputer` by its absolute path. Gemini CLI's own sandbox (`--sandbox`, macOS Seatbelt; off by default)
blocks what metacomputer needs on the Mac (its state folders, the VM's SSH port, the hub's and the vault's sockets), and
it starts the MCP server inside it too: run Gemini CLI without its sandbox to use metacomputer.

## Commands only you run

Some commands are yours alone: setting up and unlocking the vault, widening the network allowlist, approving a lesson
with your own text, saving the live-view password, saying you're done with a handoff. They need your own terminal, and
they refuse when an agent session, Gemini CLI's included, is above them in the process tree. Gemini CLI can give a
command a terminal of its own (its shell tool runs commands in a pseudo-terminal), so a terminal isn't enough. Run them
in Terminal, iTerm or an SSH session, not through an agent.

## Where Gemini CLI differs

- **One controller at a time** counts each Gemini CLI session as one session: its process is `node` running its
  `gemini` script (it starts itself again as a second node process, the parent of its shell commands and MCP servers;
  `lib/owners.py` recognizes both by the script they run, a relative one from the process's own folder), or Gemini
  CLI's standalone `gemini` executable (by the executable the process runs).
- **The vault** recognizes Gemini CLI sessions as it does Claude Code's: "for this session" answers, unattended rules for
  a project folder, and tokens tied to the session that asked for them.
- **`metacomputer learn`** reads Gemini CLI's sessions (`~/.gemini/tmp/<project>/chats/session-*.jsonl`), with the same
  rules: only classes, counts, tool names and app names from a fixed list, never text. Gemini CLI wraps each MCP result
  in `<untrusted_context>` for the model; learn reads the text inside.
- **The tools' names** are `mcp_metacomputer_<tool>` (the skill writes `mcp__metacomputer__`; use the names your tool
  list shows).
- **Slash commands:** the plugin's `/learn` command is Claude Code's; in Gemini CLI, run `metacomputer learn report`.

## A benchmark run

`bench/bench.py run --agent gemini` runs each task in `gemini -p ... -o stream-json --approval-mode default --skip-trust
--allowed-mcp-server-names metacomputer`, in a throwaway HOME made for that session and deleted afterwards
(bench/README.md, "Isolated sessions"): none of your Gemini CLI settings, sign-in or environment, only the variables you
name with `--pass-env`. Its `~/.gemini/settings.json` holds only the server (with the run's settings, trusted), with
folder trust and telemetry off; the skill is in the work folder's `.agents/skills`.

**What it may do without asking:** a policy file (`~/.gemini/policies/`, Gemini CLI's policy engine) allows
metacomputer's tools and shell commands that start with metacomputer's command, and nothing else. Every other tool
keeps Gemini CLI's default rule; what that asks for (writing files, other shell commands, fetching pages) is refused in
a headless session. No yolo.

It needs a Gemini API key (your Code Assist sign-in is in your own `~/.gemini`, which the session doesn't see), given
to that command only and named with `--pass-env` (not run here; Google's README asks not to use Gemini CLI to benchmark
Gemini models, and this measures metacomputer, not the model):

```bash
export MCOMP_CONFIG=/path/to/test-vm.env
cd ~/metacomputer
# GEMINI_API_KEY set for this command only (from your secrets manager), and passed on by name:
bench/bench.py run --agent gemini --pass-env GEMINI_API_KEY --run-id gemini-std
```

Gemini CLI reports tokens, not a cost: `--budget` and `--cap` don't apply, and each task keeps its timeout.

## If something doesn't work

- **No `mcp_metacomputer_*` tools:** `/mcp` in the session; a disabled server means the folder isn't trusted.
  `gemini mcp list` from a trusted folder starts each server and should show `metacomputer` connected.
- **The server shows as disconnected:** `~/.local/bin/metacomputer status`; through the extension alone, its launcher
  says when metacomputer isn't installed.
- **Every tool call asks:** the entry has no `"trust": true` (or it comes from the extension).
