# metacomputer with Antigravity CLI

metacomputer works with Google's Antigravity CLI (`agy`) as well as Claude Code, Codex, OpenClaw, Hermes Agent and Gemini
CLI. Antigravity CLI drives the virtual computer through the same MCP tools, the same hub and the same guards; the vault,
the network allowlist, handoffs, recordings and lessons work the same way. This page covers what's set up for
Antigravity CLI, what it changes, and where it differs.

**What was checked, and how.** Antigravity CLI 1.5.15 (2026-11-23), its release download (signed by Google, checksum from
Google's release manifest) in a throwaway folder and a throwaway home, with no Google account and no model: install.sh's
registration (`agy mcp list` shows it), Antigravity CLI starting metacomputer's MCP server and calling its tools through
its `call_mcp_tool` (its 68 tools listed; the calls answered that the computer was off, as it was), the permission rule
the install adds (with it the calls ran; without it a headless run refused them), the skills reaching the model's
instructions, `--uninstall`, its processes recognized as an agent session, and `metacomputer learn` reading
its transcripts. For the sessions, Antigravity CLI's own model provider setting (`"modelProvider": "gemini"`) pointed at
a stand-in for Google's model API on this Mac (`GOOGLE_GEMINI_BASE_URL`), with every other network connection blocked.
**Not checked**, because each needs a Google account: a session with a real model, its `auto` mode (the default for
personal accounts since 1.5.7) and the terminal sandbox that mode turns on, `agy acp` and `agy remote-control` (what's
said about them below comes from Google's documentation), and the Antigravity app and IDE.

Antigravity CLI is Google's successor to Gemini CLI for free and Google AI Pro/Ultra users (since 2026-06-18); Gemini CLI
goes on for Gemini Code Assist and paid Gemini API keys ([gemini.md](gemini.md)). It isn't open source: its use is
under Google's terms for Antigravity.

## Set it up

Every run of `install.sh` (the `curl ... | bash` line, `metacomputer update`, the install line again) sets metacomputer
up for each coding agent installed on the Mac, Antigravity CLI included (an `agy` command on your PATH), and for one
installed since the last run. Installed Antigravity CLI after metacomputer? `metacomputer update`, or right away:

```bash
cd ~/metacomputer && ./install.sh --only-antigravity    # or the folder `metacomputer where` shows
```

`--no-antigravity` leaves Antigravity CLI alone, and later runs remember it until `--antigravity` (it doesn't remove what
an earlier run added: `--uninstall` does). `get.sh` passes `--no-antigravity` on. `metacomputer agents` shows what's set
up for each agent. Start a new Antigravity CLI conversation afterwards.

What it adds:

| What | Where | For |
| --- | --- | --- |
| The MCP server `metacomputer` (`~/.local/bin/metacomputer mcp-lazy`), through `agy mcp add`, with `"timeoutSeconds": 600` | `~/.gemini/config/mcp_config.json` | The tools, in every Antigravity CLI conversation. Antigravity CLI ends a tool call after 3 minutes by default, shorter than a long `run_steps` or text typed at a person's pace; its `mcp add` has no option for it, so the install adds `timeoutSeconds` to metacomputer's own entry itself (below). |
| metacomputer's permission rule, `mcp(metacomputer/*)`, in `permissions.allow`, when the run adds the server | `~/.gemini/antigravity-cli/settings.json` | Its tools without a question before each one (every click), as Gemini CLI's server gets `trust`: they act only on the virtual computer, through the hub's guards (below). |
| The skills `metacomputer` and `cua-driver` (links into the checkout) | `~/.gemini/antigravity-cli/skills/` | When and how to use the virtual computer. Antigravity CLI reads that folder (its own); it doesn't read `~/.agents/skills`. |

By hand, the same is:

```bash
agy mcp add metacomputer ~/.local/bin/metacomputer mcp-lazy
# then, in ~/.gemini/config/mcp_config.json, "timeoutSeconds": 600 in metacomputer's entry,
# and in ~/.gemini/antigravity-cli/settings.json: { "permissions": { "allow": ["mcp(metacomputer/*)"] } }
ln -s ~/metacomputer/skills/metacomputer ~/.gemini/antigravity-cli/skills/metacomputer
ln -s ~/metacomputer/.claude/skills/cua-driver ~/.gemini/antigravity-cli/skills/cua-driver
```

`~/.gemini/config/mcp_config.json` is shared: the Antigravity app and IDE read it too, so they get metacomputer's MCP
server as well (not checked; the skills folder above is the CLI's alone).

`agy mcp add` adds or replaces a server of the same name without asking, and `agy mcp list` prints only a table, so the
install reads the entry from `mcp_config.json` itself, as Antigravity CLI reads it (comments and trailing commas
allowed), first and again right before it adds one. It changes Antigravity CLI's MCP server `metacomputer` only when
there's none, by the rule it uses for the other agents: a server that is a local command, exactly
`~/.local/bin/metacomputer` or the checkout's `bin/metacomputer`, with exactly the argument `mcp-lazy`, is
metacomputer's own. Anything else is never changed or removed: the install names it by its program and how many
arguments it has (never its arguments or values), says to run `agy mcp remove metacomputer` and then `./install.sh
--only-antigravity`, and records the step as unfinished (exit status 1; `metacomputer update` runs it again). A
`mcp_config.json` it can't read, as Antigravity CLI reads it, is never taken for one without the server.

Antigravity CLI's own `agy mcp` command rewrites the whole file as plain JSON and refuses one with comments or trailing
commas. When yours has them, the install changes nothing and says how to add the entry by hand. The timeout: when
metacomputer's own entry has no `timeoutSeconds` (one you set stays as you set it), the install writes the file again as
`agy mcp add` itself writes it (plain JSON, sorted keys) with `"timeoutSeconds": 600` added to that entry, to a new file
beside it that replaces it only while the file is still exactly what was read; otherwise it leaves the file and says how
to add it by hand. A `mcp_config.json` that is a link (a file you keep elsewhere) is never replaced: the run leaves the
link and its file as they are and says what to add. `--uninstall` removes only metacomputer's own (`agy mcp remove`), and keeps `~/.local/bin/metacomputer`
and `mcomp` while an entry that stays runs them.

### Approving its tools

Antigravity CLI asks before every MCP tool call unless a permission rule allows it (MCP tools are **Ask** by default,
in `auto` mode too; only `--dangerously-skip-permissions` skips that; in a headless run, `agy -p`, a call it would ask
about is refused). Its CLI has no command for permission rules, so the run that adds metacomputer's MCP server also adds
metacomputer's own rule, exactly `mcp(metacomputer/*)`, to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`
(as Gemini CLI's server gets `"trust": true`): metacomputer's tools act only on the virtual computer, through the hub's
guards. Nothing else in that file changes. The rules for it:

- Only plain JSON is written (Antigravity CLI keeps the file so): one with comments or trailing commas is left as it is,
  and the run says which line to add yourself. The file is written to a new file beside it, with its mode, which
  replaces it only while it's still exactly what was read, and is read back. A `settings.json` that is a link is never
  replaced: it's left as it is, with the line to add.
- An `ask` or `deny` rule of yours for metacomputer's tools (`mcp(metacomputer/...)`) or for every MCP tool (`mcp(*)`)
  wins: no allow rule is added next to it.
- Only a run that adds the server adds the rule: take it out if you'd rather be asked before each call, and later runs
  leave it out.
- `--uninstall` takes out exactly that rule once metacomputer's server is gone (or there's no server named
  `metacomputer`), and only when this install added it: the install notes that it did in
  `~/.local/share/metacomputer/antigravity-rule.json`. The same rule written by you (before the install, or after you
  took it out) stays, and someone else's server of that name keeps it.

A running Antigravity CLI may write its settings again from what it loaded: install with it closed, or start it again
afterwards. The check that a file is still what was read is made right before it's replaced (its content read again,
and the same file: device, inode, size and times), but a program writing it in that very instant can still lose its
change, since a file can't be compared and swapped against any writer ([install.md](install.md#what-goes-where)).

### Through a plugin instead

Antigravity CLI converts Gemini CLI extensions, and this repository is one (`gemini-extension.json`, [gemini.md](gemini.md)):
`agy plugin install https://github.com/metavert-io/metacomputer` (or `agy plugin import gemini`, for the extension
installed in Gemini CLI) makes it a plugin. Checked with a local checkout: it copies the whole repository (about 15 MB)
into `~/.gemini/config/plugins/metacomputer`, with one skill (`metacomputer`, not `cua-driver`) and the MCP server run by
the package's launcher (`agent-plugin/bin/metacomputer`, which runs the metacomputer installed on your Mac and nothing
else), named `metacomputer_metacomputer` (Antigravity CLI prefixes a plugin's servers with its name). install.sh's setup
is the one to use; with both, Antigravity CLI has two servers with the same tools.

## Its tools

By default Antigravity CLI doesn't put MCP tools in the model's tool list: the model calls them through its own
`call_mcp_tool` tool, with `ServerName` `metacomputer` (`metacomputer_metacomputer` through the plugin), `ToolName` the
tool (`click`, `run_steps`, ...) and `Arguments`. The tools' descriptions are in `~/.gemini/antigravity-cli/mcp/metacomputer/`.
A server can have its tools loaded up front, as `mcp_metacomputer_<tool>`; the skill tells the agent to use whichever its
tool list shows.

Antigravity CLI loads the first 24,000 bytes of a skill's text, and metacomputer's `SKILL.md` is longer. Its rules
(refusals and the commands only you run, secrets and one-time codes, payments, untrusted content and hidden text,
isolation), handing a step to you and the safety check before the first action come first, within those bytes
(`tests/test_agent_setup.py` fails when an edit pushes one of them past); the driving guidance, the other commands and
the references follow, and reach the model when it reads the whole file, which the skill tells it to do.

## Its shell and sandbox

Antigravity CLI runs commands (its `run_command`) with your shell (zsh), in the environment it started with, plus
`ANTIGRAVITY_AGENT=1`, `ANTIGRAVITY_CONVERSATION_ID` and a few others of its own; the skill runs `metacomputer` by its
absolute path. In `auto` mode, the default for personal accounts since 1.5.7, it runs commands in its terminal sandbox
(macOS `sandbox-exec`), which sees only the workspace and temporary folders and has no network: there, a `metacomputer`
command can't reach its state folders, the virtual computer's SSH port or the hub's and the vault's sockets, so it
fails. Antigravity CLI runs a command outside the sandbox when it judges it safe, or asks; a `command` rule with the
command's full path (`"command(/Users/you/.local/bin/metacomputer)"` in `permissions.allow`) lets it through without
asking. (Not checked: the sandbox needs an account's `auto` mode.) The MCP server isn't run as one of its commands:
Antigravity CLI starts it itself, as its own child.

## Commands only you run

Some commands are yours alone: setting up and unlocking the vault, widening the network allowlist, approving a lesson
with your own text, saving the live-view password, saying you're done with a handoff. They need your own terminal, and
they refuse when an agent session, Antigravity CLI's included, is above them in the process tree. The Antigravity app
and IDE count as one too (below), so they refuse in those apps' own terminals as well. Run them in Terminal, iTerm or an
SSH session, not through an agent.

## Where Antigravity CLI differs

- **One controller at a time** counts each Antigravity CLI process as one session: its executable `agy` (the
  installer's `~/.local/bin/agy`, or `antigravity`, its name in Google's release and in Homebrew's cask), told by the
  executable the process runs (`lib/owners.py`, and the vault's `vault/Util.swift`, with the same shared cases).
- **The Antigravity app and IDE** run their agent inside the app (its language server), so their own processes are an
  agent session too, recognized by the executable's realpath: one in the `Contents/MacOS` of an Antigravity app bundle
  (the app's `Antigravity.app/Contents/MacOS/Antigravity`, the IDE's `Antigravity IDE.app/Contents/MacOS/Electron`, their
  helpers) or a language server (`language_server`, the app's `Contents/Resources/bin/language_server`, or one named for
  its platform, such as `language_server_macos_arm`) in a folder of Antigravity's own (a path component that is or
  starts with `antigravity`, case aside: `Antigravity.app`, `.antigravity`, `antigravity-ide`; never the word inside
  another name, such as `not-antigravity`). Everything they run (the agent's
  commands, the MCP servers they start, a terminal of theirs) is under that session, so the commands only you run
  refuse there. They run every conversation in one process, so the vault treats them like Codex's app server: no "for
  this session" answers, project rules or session tokens; each use asks. Checked against the bundles of Antigravity
  2.34.0 and Antigravity IDE 2.8.0 (Google's macOS downloads; neither was run).
- **The vault** recognizes Antigravity CLI sessions as it does Claude Code's: "for this session" answers, unattended rules
  for a project folder, and tokens tied to the session that asked for them. Antigravity CLI serving an editor (`agy acp`)
  or remote devices (`agy remote-control`) runs several conversations in one process, so requests from under those get
  none of these, as with Codex's app server: each use asks (recognized by their arguments, from Google's documentation).
- **`metacomputer learn`** reads Antigravity CLI's conversations: the transcript it keeps of each one
  (`~/.gemini/antigravity-cli/brain/<conversation>/.system_generated/logs/transcript.jsonl`, its project folder from
  `conversation_summaries.db` beside it, read without a write or a lock), with the same rules: only classes, counts, tool
  names and app names from a fixed list, never text. Its own conversation databases (`conversations/*.db`) hold Google's
  binary records, which learn doesn't read.
- **The lessons skill** (`/metacomputer:learn`) comes with Claude Code's plugin and Codex's; in Antigravity CLI, run
  `metacomputer learn report`.
- **No benchmark runs:** `bench/bench.py` has no `--agent antigravity`. A headless run (`agy -p`) needs a Google sign-in
  or a Gemini API key, and Google's FAQ says wrapping `agy` in third-party tools isn't supported on consumer plans.

## If something doesn't work

- **No metacomputer tools:** `agy mcp list` should show `metacomputer` enabled; start a new conversation after setting it
  up (`/mcp` in a conversation shows the servers).
- **Every tool call asks:** the `mcp(metacomputer/*)` rule isn't in `permissions.allow` (a settings.json with comments,
  an `ask` or `deny` rule of yours, or you took it out): add it (above).
- **A tool call ends after 3 minutes:** `timeoutSeconds` is missing from metacomputer's entry in `mcp_config.json`.
- **`metacomputer` commands fail with permission errors in `auto` mode:** they ran inside Antigravity CLI's sandbox; let
  them run outside it (above).
- **`agy mcp add` says it can't parse `mcp_config.json`:** the file has comments or trailing commas; add the entry by hand.
