# metacomputer with Codex

metacomputer works with OpenAI's Codex as well as Claude Code. Codex drives the virtual computer through the same
MCP tools, the same hub and the same guards; the vault, the network allowlist, handoffs, recordings and lessons work
the same way. This page covers what's set up for Codex, what its sandbox changes, and where the two differ.

Checked with Codex CLI 0.167.0: the benchmark's six browser tasks (Firefox and Chrome forms, reading a table, a
canvas, an unlabeled tile, link navigation) passed 6/6 through `codex exec`
([bench/runs/codex-browser-2026-11-21.json](../bench/runs/codex-browser-2026-11-21.json); `bench/bench.py run --agent codex`).

## Set it up

On a first install, `install.sh` (and the `curl ... | bash` line) sets Codex up too when the `codex` command is
installed. On a Mac where metacomputer is already installed:

```bash
cd ~/metacomputer && ./install.sh --only-codex     # or the folder `metacomputer where` shows
```

Later runs of `install.sh` and `metacomputer update` refresh it once it's there, and never add it to a Codex that
doesn't have it yet. `--no-codex` leaves Codex alone; `--uninstall` removes what it added. Start a new Codex session
afterwards.

What it adds:

| What | Where | For |
| --- | --- | --- |
| The MCP server `metacomputer` (`<checkout>/bin/metacomputer mcp-lazy`) with `tool_timeout_sec = 600` and `default_tools_approval_mode = "approve"` | `$CODEX_HOME/config.toml` (`~/.codex`) | The tools, `mcp__metacomputer__*`, in every Codex session. Codex's default tool timeout (60 s) is shorter than a long `run_steps` or text typed at a person's pace. Codex otherwise asks before every tool that isn't marked read-only (every click), and `codex exec` refuses them; these tools act only on the virtual computer, through the hub's guards. |
| The skills `metacomputer` and `cua-driver` (links into the checkout) | `~/.agents/skills/` | When and how to use the virtual computer, as Claude Code gets them from the plugin |
| metacomputer's rules | `$CODEX_HOME/rules/metacomputer.rules` | Running `metacomputer` commands outside Codex's sandbox (below) |

By hand, the same is:

```bash
codex mcp add metacomputer -- ~/metacomputer/bin/metacomputer mcp-lazy
# then, under [mcp_servers.metacomputer] in ~/.codex/config.toml:
#   tool_timeout_sec = 600
#   default_tools_approval_mode = "approve"
ln -s ~/metacomputer/skills/metacomputer ~/.agents/skills/metacomputer
ln -s ~/metacomputer/.claude/skills/cua-driver ~/.agents/skills/cua-driver
```

and a rules file like `config/codex/metacomputer.rules.in`, with `@COMMANDS@` replaced by the ways you write the
command (`["metacomputer", "mcomp", "/Users/you/.local/bin/metacomputer"]`).

A `default_tools_approval_mode` or `tool_timeout_sec` you set yourself stays as you set it; `"writes"` (or
`"prompt"`) makes Codex ask you before the tools that act.

## Codex's sandbox

Codex runs shell commands in a sandbox that blocks what `metacomputer` needs on your Mac: its state folders, the
virtual computer's SSH port, and the hub's and the vault's sockets. Inside it, a `metacomputer` command stops at once
with exit status 77 and a message saying so (Codex marks sandboxed commands with `CODEX_SANDBOX`), instead of failing
in ways that look like a broken VM. The MCP tools aren't affected: Codex starts its MCP servers outside the sandbox.

metacomputer's rules let the commands through:

| Commands | Rule |
| --- | --- |
| Everyday ones, by name: `status`, `start`, `stop` (it refuses while another session uses the VM), `who`, `web`, `browser`, `net`, `trace`, `handoff`, `notify`, `events`, `inspect`, `call`, `sh`, `ssh`, `put`, `get`, `shot`, `task`, `record`, `recordings`, `view`, `secrets`, `learn`, `claude`, `hub` ... | `allow`: they run outside the sandbox without asking |
| Ones that replace, delete or stop the VM, its storage, apps or settings, or take it from another session: `reset`, `restore`, `baseline`, `checkpoint`, `build`, `vm`, `volume`, `refdisk`, `update`, `setup`, `release`, `restart`, `desktop-restart`, `test`, `security`, `each` (it runs commands on other VMs), `hub stop`, `events sink`, `learn approve/add/edit/remove/reject/reseal/bench`, `browser profiles --delete`, and any command with `--vm NAME` | `prompt`: Codex asks you each time |
| The ones only you run: `secrets init/set/policy/bind/unlock/rekey/recover/approve/rule/give/import-passwords/import-session/audit-sink`, `volume keys`, `view --remember/--forget`, `handoff done` | `forbidden` |

Rules match the words in order, so they see a flag like `--override` only right after the command (`metacomputer
stop --override` asks; `metacomputer sh -c '...' --override` doesn't): as in Claude Code, the skill has the agent ask you
before taking the computer over, and metacomputer's own checks still apply. A command no rule names (a new one, say)
stays in the sandbox: it stops with exit 77 and Codex asks you before running
it outside. The rules are a convenience, not the protection: metacomputer's own checks apply whoever runs a command, and a
command written another way (`MCOMP_VM=x metacomputer reset`, say) may not match a rule. Without the rules, approve
`metacomputer` commands when Codex asks to run them outside the sandbox. A Codex setup of your own that gives the
sandbox everything above can set `MCOMP_CODEX_SANDBOX=allow`.

## Commands only you run

Some commands are yours alone: setting up and unlocking the vault, widening the network allowlist, approving a lesson
with your own text, saving the live-view password, saying you're done with a handoff. They need your own terminal.
Codex's command tool can give a command a terminal of its own, so a terminal isn't enough: these commands also refuse
when an agent session, Claude Code or Codex, is above them in the process tree, or when the process tree can't be
read all the way up. Run them in Terminal, iTerm or an SSH session, not through an agent. Like every check against your own account, this stops an agent from passing by
accident, not a program set on it (SECURITY.md).

## Where Codex differs

- **One controller at a time** counts each Codex process as one session, as it counts each Claude Code process. The
  Codex IDE extension and the desktop app run all of their conversations in one process (`codex app-server`), so two
  conversations there count as one session and don't wait for each other.
- **The vault** recognizes Codex sessions as it does Claude Code's: "for this session" answers, unattended rules for
  a project folder, and tokens tied to the session that asked for them. Requests from under Codex's app server (the
  IDE extension and the desktop app) get none of these: its conversations share one process, so nothing tells one
  conversation from another, and each use asks.
- **`metacomputer learn`** reads Codex's sessions (`$CODEX_HOME/sessions`) as well as Claude Code's, with the same
  rules: only classes, counts, tool names and app names from a fixed list, never text. Approved lessons reach Codex
  in the MCP server's instructions. `--draft-with-claude`, `learn bench` and the benchmark run Claude Code
  (`claude -p`).
- **`metacomputer claude`** starts Claude Code inside the virtual computer; it works the same when Codex asked for it.
- **Slash commands:** the plugin's `/learn` command is Claude Code's; in Codex, run `metacomputer learn report`.

## If something doesn't work

- **No `mcp__metacomputer__*` tools:** `codex mcp list` should show `metacomputer`; start a new session after setting it up.
- **A command exits 77:** it ran inside the sandbox; approve it outside, or check the rules file is there
  (`codex execpolicy check -r ~/.codex/rules/metacomputer.rules metacomputer status` says `allow`).
- **A tool call times out after 60 s:** `tool_timeout_sec` is missing under `[mcp_servers.metacomputer]`.
- **"MCP tool call requires approval":** `default_tools_approval_mode = "approve"` is missing there (or you chose to be
  asked; in `codex exec` nobody can answer).
