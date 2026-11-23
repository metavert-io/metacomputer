# metacomputer (Agent Plugins package)

metacomputer gives your agent a disposable computer of its own: an Ubuntu 24.04 desktop in a virtual machine on your
Mac. The agent operates it the way a person would, reading the screen through the accessibility tree, clicking and
typing in Firefox, Chrome and other desktop apps, and recording video of what it did. The VM is kept away from your
files, your accounts, your Mac and your network.

This folder is metacomputer packaged in the portable [Agent Plugins](https://agent-plugins.org/specification) format
(1.0.0), the one Hermes Agent installs:

```bash
hermes plugins install metavert-io/metacomputer/agent-plugin --enable --force
```

(`--force`: from outside its catalog, Hermes Agent's install scan stops at a "caution" verdict, here from the skill's
safety notes; it lists them first.)

## What it contains

- **Skills:** `metacomputer`, which tells the agent when a task belongs on the VM and how to drive its desktop, and
  `cua-driver`, the guide to Cua Driver's tools on Linux (from Cua, MIT-licensed; its license is in the skill's folder).
- **An MCP server,** `metacomputer` (`mcp.json`): `./bin/metacomputer mcp-lazy`. Its tools are Cua Driver's, which run
  inside the VM, with every call going through metacomputer's hub on your Mac and its guards (one session at a time,
  real input for Chrome, the limits of bounded mode).
- **`bin/metacomputer`,** a short launcher: it runs the metacomputer installed on your Mac (`$MCOMP_HOME`, else
  `~/.local/bin/metacomputer`) with the same arguments. With nothing installed it says how to install, and runs nothing.

The package needs metacomputer installed on the Mac, which builds the VM: see
[the 5-minute install](https://github.com/metavert-io/metacomputer#the-5-minute-install) (macOS on Apple silicon; the
installer says what it will do and asks first).

The installer also sets metacomputer up for each coding agent it finds (Claude Code, Codex, OpenClaw, Hermes Agent,
Gemini CLI), so with it you don't need this package; an MCP server named `metacomputer` in your own configuration takes
precedence over the package's.

Its skills, `plugin.json` and `mcp.json` are made from the repository's own files by `scripts/sync-plugin.py`; edit
those instead. More:
[docs/hermes.md](https://github.com/metavert-io/metacomputer/blob/main/docs/hermes.md), and the
[privacy policy](https://github.com/metavert-io/metacomputer/blob/main/PRIVACY.md).
