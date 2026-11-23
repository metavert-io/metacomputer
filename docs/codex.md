# metacomputer with Codex

metacomputer works with OpenAI's Codex as well as Claude Code. Codex drives the virtual computer through the same
MCP tools, the same hub and the same guards; the vault, the network allowlist, handoffs, recordings and lessons work
the same way. This page covers what's set up for Codex, what its sandbox changes, and where the two differ.

Checked with Codex CLI 0.167.0 through `codex exec` (`bench/bench.py run --agent codex`), on the same Cua Driver
(0.53.0) and settings (coordinate guard off, the hub on) as the Claude Code runs beside it:

| Task set | Codex | Claude Code |
| --- | --- | --- |
| Standard: 20 desktop tasks in Calc, Writer, Mousepad, Thunar, Firefox and Chrome | 20/20 ([record](../bench/runs/codex-std-2026-11-21.json)) | 19/20 ([record](../bench/runs/learn-2026-11-18-without.json)) |
| Lessons: 8 tasks built to bring out known mistakes | 8/8; 1.5 min a task; 1 failed driver call ([record](../bench/runs/codex-lessons-2026-11-21.json)) | 8/8; 1.1 min a task; 7 failed driver calls ([record](../bench/runs/learn-lessons-2026-11-18-without.json)) |

The six browser tasks of the standard set, run again on their own, passed 6/6 too
([record](../bench/runs/codex-browser-2026-11-21.json)). These are single runs, days apart, each agent with its default
model, so read them as "Codex works as well here", not as a ranking. Turn counts don't compare: the benchmark counts
each of Codex's tool calls as a turn.

## Set it up

Every run of `install.sh` (the `curl ... | bash` line, `metacomputer update`, the install line again) sets
metacomputer up for each coding agent installed on the Mac, Claude Code and Codex, and for one installed since the last
run. Installed Codex after metacomputer? `metacomputer update`, or right away:

```bash
cd ~/metacomputer && ./install.sh --only-agents    # or the folder `metacomputer where` shows
```

`--no-codex` leaves Codex alone, and later runs remember it until `--codex` (it doesn't remove what an earlier run
added: `--uninstall` does). `metacomputer agents` shows what's set up for each agent. Start a new Codex session
afterwards.

What it adds:

| What | Where | For |
| --- | --- | --- |
| The MCP server `metacomputer` (`<checkout>/bin/metacomputer mcp-lazy`) with `tool_timeout_sec = 600` and `default_tools_approval_mode = "approve"` | `$CODEX_HOME/config.toml` (`~/.codex`) | The tools, `mcp__metacomputer__*`, in every Codex session. Codex's default tool timeout (60 s) is shorter than a long `run_steps` or text typed at a person's pace. Codex otherwise asks before every tool that isn't marked read-only (every click), and `codex exec` refuses them; these tools act only on the virtual computer, through the hub's guards. |
| The skills `metacomputer` and `cua-driver` (links into the checkout) | `~/.agents/skills/` | When and how to use the virtual computer, as Claude Code gets them from the plugin. A link there is metacomputer's only when it points into this checkout's own skill folders; anything else of that name (a broken link included) is left, named by its path, and the step counts as unfinished |
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

The install changes Codex's MCP server `metacomputer` only when it's metacomputer's own, by the rule it uses for Claude
Code's: a local command that is exactly the checkout's `bin/metacomputer`, a path earlier versions registered in the
checkout's `bin/` (pointed at `bin/metacomputer`, everything else you set on it kept) or `~/.local/bin/metacomputer`,
with exactly the argument `mcp-lazy`. It reads the whole entry again right before a change and leaves it as it is when
it changed meanwhile. Anything else, a program elsewhere in the checkout or a moved checkout's launcher included, is
never changed or removed: the install names it by its program and how many arguments it has (never its arguments or
values), says to run `codex mcp remove metacomputer` and then `./install.sh --only-codex`, and records the Codex step as
unfinished (exit status 1; `metacomputer update` runs it again). `--uninstall` removes only metacomputer's own, and
keeps `~/.local/bin/metacomputer` and `mcomp` while an entry that stays runs them.

## Codex's plugin marketplace

The repository is also a Codex plugin marketplace (`.agents/plugins/marketplace.json`), with one plugin, `metacomputer`:
the same `plugin/` folder Claude Code installs, with a manifest of Codex's own (`plugin/.codex-plugin/plugin.json`).
It brings the skills, `metacomputer`, `cua-driver` and `learn` (`$metacomputer:learn`, or `/skills`), and no MCP server:

```bash
codex plugin marketplace add metavert-io/metacomputer
codex plugin add metacomputer@metacomputer
```

The tools, Codex's rules and the command still come from metacomputer installed on your Mac (`install.sh`, above): the
plugin's skills run `~/.local/bin/metacomputer`, and without it they say how to set it up (the plugin's launcher runs its
own copy's `get.sh`, which says what it will do and asks first). With install.sh's skill links in `~/.agents/skills` as
well, Codex has the skills twice (the plugin's are named `metacomputer:...`); either is enough. Checked with Codex CLI
0.167.0 in a throwaway `CODEX_HOME`, with a checkout as the marketplace (`codex plugin marketplace add
~/metacomputer`): the three skills in the model's instructions (`codex debug prompt-input`). Adding it from GitHub, as
above, works once this is on the repository's main branch.

## OpenAI's plugin directory

`scripts/build-openai-plugin.py` packages the same skills for OpenAI's plugin directory (ChatGPT and Codex): a ZIP with
one folder, `metacomputer/`, holding the skills (`metacomputer`, `learn`, `cua-driver`), the icon, the license and the
notices, and a manifest (`.codex-plugin/plugin.json`: its listing's website, support, privacy policy and terms of
service URLs) with the release's version and `"requires_local_executor": true`,
which makes it "Desktop only": installed from the ChatGPT desktop app and used in threads that run on your Mac. It
carries no MCP server, launcher or script: its skills run the metacomputer installed on your Mac, and say how to
install it when it isn't. `--check` builds it in memory and checks it against the directory's documented rules (regular
files only, each skill a folder of its own with its name and a description of at most 1,024 characters, every link
in a skill leading to a file in the package, the icon); `tests/test_plugin.py` runs that. Its sources are regular
files only: a skill folder, the icon, the license or the notices that is a link, or sits in a linked folder, stops the
build (it would bring in what the link points to), and `scripts/sync-plugin.py --check` refuses any link in `plugin/`,
`agent-plugin/` or `.agents/`. Nothing is uploaded or
submitted from here: that's the publisher's step, in OpenAI's portal. Not checked: whether the ChatGPT desktop app's
local threads use the MCP server install.sh gives Codex.

```bash
python3 scripts/build-openai-plugin.py            # results/openai-plugin/metacomputer-VERSION.zip
python3 scripts/build-openai-plugin.py --check
```

## When Codex changes

metacomputer relies on parts of Codex that aren't a fixed interface: its rules and their checker, the variable its
sandbox sets, `codex mcp` and two of its settings, and the format of its session files. `metacomputer agents check`
checks each of them against the Codex on the Mac, without a model and without touching your Codex settings: the
rules' decisions and the commands' exit statuses, a marker the sandbox sets itself (not one passed down to it), the two
settings by Codex's own strict config loading (with a model provider that doesn't exist, so no session starts), and
that `learn` finds the tool calls in your newest session that has some. It runs only a `codex` found through an
absolute PATH entry, yours or root's and not writable by others. install.sh runs it after setting Codex up, so
`metacomputer update` reports a Codex update that changed one of them.

## Codex's sandbox

Codex runs shell commands in a sandbox that blocks what `metacomputer` needs on your Mac: its state folders, the
virtual computer's SSH port, and the hub's and the vault's sockets. Inside it, a `metacomputer` command stops at once
with exit status 77 and a message saying so (Codex marks sandboxed commands with `CODEX_SANDBOX`), instead of failing
in ways that look like a broken VM; one that only asks for its usage (`--help` or `-h`) prints it, since it runs
nothing. The MCP tools aren't affected: Codex starts its MCP servers outside the sandbox.

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
  Codex IDE extension, the desktop app and the terminal app run all of their conversations in one process (`codex
  app-server`), so two conversations there count as one session and don't wait for each other. For the terminal app
  that's its shared background server (checked with Codex 0.167: `codex` starts it on first use, as
  `codex app-server --managed-daemon`, and the commands of every conversation run under it); `codex --no-daemon`
  starts the terminal app without it. `codex exec` runs as a process of its own. For the same reason, when two of the
  app server's conversations have their tools on different computers, a command from either that names no computer
  refuses: name it with `--vm` ([commands.md](commands.md#an-agent-sessions-computer)).
- **The vault** recognizes Codex sessions as it does Claude Code's: "for this session" answers, unattended rules for
  a project folder, and tokens tied to the session that asked for them. Requests from under Codex's app server (the
  IDE extension, the desktop app and the terminal app's shared background server) get none of these: its
  conversations share one process, so nothing tells one conversation from another, and each use asks.
- **`metacomputer learn`** reads Codex's sessions (`$CODEX_HOME/sessions`) as well as Claude Code's, with the same
  rules: only classes, counts, tool names and app names from a fixed list, never text. Approved lessons reach Codex
  in the MCP server's instructions. `--draft-with-claude`, `learn bench` and the benchmark run Claude Code
  (`claude -p`).
- **`metacomputer claude`** starts Claude Code inside the virtual computer; it works the same when Codex asked for it.
- **The lessons skill:** with the plugin from the marketplace (above), Codex has `learn` (`$metacomputer:learn`), the
  same steps as Claude Code's `/metacomputer:learn`; without it, run `metacomputer learn report`.

## Login shells and PATH

Codex runs shell commands through your login shell (`zsh -lc` on a Mac): its shell tool has a `login` option, and its
`allow_login_shell` setting turns that off (checked with Codex CLI 0.167.0). A login shell builds PATH again from your
profile. On macOS, `/etc/zprofile` runs `path_helper`, which puts the folders listed in `/etc/paths` and
`/etc/paths.d` (`/usr/local/bin`, `/usr/bin`, `/bin`, `/usr/sbin`, `/sbin`, Homebrew's) in front of the PATH the shell
was given, and a profile that adds `~/.local/bin` puts that in front of them all. So a folder put first on PATH before
Codex started (a wrapper `metacomputer` for a test VM, another checkout's `bin/`) comes after `~/.local/bin/metacomputer`
in Codex's commands. Variables such as `MCOMP_CONFIG` and `MCOMP_VM` pass through a login shell unchanged; only PATH is
rebuilt.

In metacomputer's own setup that doesn't change which checkout runs: the skill runs `~/.local/bin/metacomputer` by its
absolute path, and Codex's MCP server is the same checkout's `bin/metacomputer`. What can still differ is the computer:
the MCP server keeps the computer it started on, while each command takes the default computer as it is when it runs.
A command that names no computer and would act on another computer than the session's refuses
([commands.md](commands.md#an-agent-sessions-computer)), and `select_computer` moves the session's tools to another
computer. For a harness of your own: run commands by absolute path, name the VM (`--vm NAME`, or `MCOMP_CONFIG` in the
environment, which a login shell keeps), and give the run a throwaway `HOME`.

## If something doesn't work

- **No `mcp__metacomputer__*` tools:** `codex mcp list` should show `metacomputer`; start a new session after setting it up.
- **A command exits 77:** it ran inside the sandbox; approve it outside, or check the rules file is there
  (`codex execpolicy check -r ~/.codex/rules/metacomputer.rules metacomputer status` says `allow`).
- **A tool call times out after 60 s:** `tool_timeout_sec` is missing under `[mcp_servers.metacomputer]`.
- **"MCP tool call requires approval":** `default_tools_approval_mode = "approve"` is missing there (or you chose to be
  asked; in `codex exec` nobody can answer).
