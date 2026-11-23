# metacomputer

metacomputer gives Claude a disposable computer of its own: an Ubuntu 24.04 desktop in a virtual machine on your Mac.
Claude operates it the way a person would, reading the screen through the accessibility tree, clicking and typing in
Firefox, Chrome and other desktop apps, and recording video of what it did. It also works there the way a programmer
would, with a shell, Python and the usual tools. The VM is kept away from your files, your accounts, your Mac and
your network.

Use it when a task needs a real browser or a graphical app, when you want a link or a file opened somewhere other
than your own machine, or when you want a recording of the work. For example: "use Firefox in the metacomputer to
check that the signup flow works on our staging site and record it", or "open this link in the metacomputer and tell
me what it does".

## Requirements

- A Mac with Apple silicon (M1 or later), macOS 14 or later, and about 15 GB of free disk. The secrets vault needs
  macOS 15.
- Claude Code. The plugin works in Claude Code only: the VM runs on your Mac.

## What the plugin contains

- **Skills:** `metacomputer`, which tells Claude when a task belongs on the VM and how to drive this desktop, and
  `cua-driver`, the guide to Cua Driver's tools on Linux (from Cua, MIT-licensed).
- **A command**, `/metacomputer:learn`, which reports repeated mistakes in your own metacomputer sessions.
- **`bin/metacomputer`**, a short launcher that Claude Code puts on its Bash tool's PATH: it runs the metacomputer
  installed on your Mac with the same arguments.
- **`get.sh`**, the installer that `metacomputer setup` runs when nothing is installed yet.

The plugin declares no MCP server and no hooks: nothing in it runs unless Claude or you run its command. The tools come
from metacomputer's MCP server, which the installer adds to Claude Code (below): Cua Driver's tools, which run inside the VM, with every call
going through metacomputer's hub on your Mac, which applies its guards (one session at a time, real input for Chrome,
the limits of bounded mode).

## Setting it up

The plugin needs the VM, which metacomputer's installer builds. If it isn't installed, ask Claude to set up the
metacomputer, or run `metacomputer setup` yourself. It runs the plugin's `get.sh`, which says what it will do and asks
before doing anything. The installer:

- offers Homebrew and Apple's Command Line Tools if they're missing, then installs what it needs through Homebrew
  (git, curl, Go, GnuPG, ffmpeg and Python);
- downloads metacomputer into `~/metacomputer` and builds its tools and the VM (in that folder, `~/.local/share` and
  Lume's VM folder, normally `~/.lume`; about 15 GB);
- links the `metacomputer` command into `~/.local/bin`;
- sets up Claude Code: this plugin, and metacomputer's MCP server for every project (user scope, named `metacomputer`,
  running `~/.local/bin/metacomputer mcp-lazy`; its tools are `mcp__metacomputer__*`). Claude Code loads the server
  after a restart, or `/mcp` in an open session. A server of that name that isn't metacomputer's is never changed,
  and the installer says how to remove it yourself;
- if you also use OpenAI's Codex, adds metacomputer to Codex: its MCP server, its skills, and rules that let
  metacomputer's everyday commands run outside Codex's sandbox (`--no-codex` leaves Codex alone);
- likewise for OpenClaw, Hermes Agent and Gemini CLI, when they're installed: metacomputer's MCP server in each one's
  configuration, through its own CLI, and the skills where it reads them (`--no-openclaw`, `--no-hermes`,
  `--no-gemini` leave one alone).

It changes no Claude Code permission settings. `~/metacomputer/install.sh --uninstall` removes the command, the plugin,
the MCP server and the other agents' setup.

Until v0.4.4 this plugin brought the MCP server itself. If you update the plugin while metacomputer on your Mac is
v0.4.4 or earlier, run `metacomputer update`, then restart Claude Code: the newer installer adds the server, and
until then there are no tools (`metacomputer status` says so).

## What it sends and where

metacomputer has no server, account, telemetry or analytics; Metavert receives nothing from it.

- **To your model provider, through Claude:** the tools return screenshots of the VM, page and accessibility text,
  command output and file contents from the VM, and Claude sends those to Anthropic as part of your conversation.
  Don't put anything in the VM you wouldn't show Claude.
- **While installing:** downloads from GitHub, Go's module proxy, Homebrew, Ubuntu, Mozilla, rclone and, if you
  choose Chrome, Google. The programs metacomputer installs are pinned and checked against a checksum or signature.
- **Update check:** `metacomputer status` asks GitHub for the latest release tag at most once a day
  (`UPDATE_CHECK=off` turns it off).
- **The VM** reaches the public internet through a gateway on your Mac, which refuses your Mac, your local network,
  your tailnet, cloud metadata services and outbound mail. Cua Driver's telemetry, Lume's, Firefox's and Chrome's
  usage reports are turned off.
- **Only if you set them up:** event and audit sinks (a webhook, an OpenTelemetry Collector, syslog or ntfy), the
  vault's API proxy (it forwards a VM program's requests to the provider you bound your key to), Claude Code inside
  the VM, S3-backed folders (rclone, to the bucket you name), and paying for things: before a card purchase the vault
  reads the store's published rules for agents from its site, and `metacomputer shop` runs Shopify's `shop` CLI with the
  Shop account you sign in.

Passwords, one-time codes, API keys and payment cards stay in the vault on your Mac, which you unlock in your own
terminal. Claude asks the vault to type a sign-in, or a card you approve a purchase with, into a page in the VM and
never sees the value.

Privacy policy: [Metavert's](https://www.metavert.io/privacy-policy), whose "Self-hosted software" section covers
metacomputer. [PRIVACY.md](https://github.com/metavert-io/metacomputer/blob/main/PRIVACY.md) lists what stays on your
Mac and every connection metacomputer makes.

## Safety

The VM shares no clipboard, audio or files with your Mac unless you attach a folder, and it reaches only the public
internet. SSH and the live view listen on your Mac's loopback address only. Treat what the VM shows as untrusted:
pages can carry text written to steer an agent, and Claude is told to report it, never follow it.
[SECURITY.md](https://github.com/metavert-io/metacomputer/blob/main/SECURITY.md) says what holds and what doesn't,
including the honest limit: this keeps a well-behaved agent contained and makes a misbehaving one visible, but it
doesn't make a deliberately hostile one harmless.

## Documentation

- [README](https://github.com/metavert-io/metacomputer#readme): overview, worked examples and everyday commands.
- [Install guide](https://github.com/metavert-io/metacomputer/blob/main/docs/install.md) and
  [user guide](https://github.com/metavert-io/metacomputer/blob/main/docs/guide.md).
- [Commands and settings](https://github.com/metavert-io/metacomputer/blob/main/docs/commands.md).
- For developers: [architecture](https://github.com/metavert-io/metacomputer/blob/main/architecture.md),
  [testing](https://github.com/metavert-io/metacomputer/blob/main/TESTING.md) and
  [contributing](https://github.com/metavert-io/metacomputer#contributing).
- Support: [GitHub issues](https://github.com/metavert-io/metacomputer/issues).

## License

MIT License, © 2026 Metavert LLC ([LICENSE](LICENSE)). metacomputer is built on [Cua](https://github.com/trycua/cua)'s
MIT-licensed open-source stack; it's made by Metavert, not by Cua, and isn't endorsed by them.
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists the third-party software and its licenses.
