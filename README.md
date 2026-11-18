# metacomputer

**Agentic coding plus a real desktop, with the controls you'd want around it.**

metacomputer gives Claude Code a disposable Ubuntu computer of its own: a
virtual machine on your Mac. Claude operates it the way a person would: it
reads the screen through the accessibility tree, clicks and types, uses
Firefox and Chrome, and records video of what it did. It also works in it the
way a programmer would, with a shell, Python, LibreOffice, ImageMagick and
ffmpeg. The VM is kept away from your files, your accounts, your Mac and your
network, and what Claude does there can be logged, limited and approved.

## The 5-minute install

Paste this into Terminal on a Mac with Apple silicon (macOS 14 or later, about
15 GB free):

```bash
curl -fsSL https://raw.githubusercontent.com/metavert-io/metacomputer/main/get.sh | bash
```

That's the whole install. There's no VM for you to set up: the script builds
it, installs the Claude Code plugin, and checks that everything works. On a Mac
that already has Homebrew and Apple's Command Line Tools it takes about 5 minutes
and asks you nothing; on a new Mac it offers to install those first.

## Try it

Start a new Claude Code session (or restart the ones you have open, so they load
the plugin), then ask:

> Get yourself a virtual computer and tell me what's on its screen.

> Make a LibreOffice Calc spreadsheet of the planets' diameters, add a column
> chart, and save it as planets.xlsx.

> Does wikipedia.org look the same in Firefox and Chrome? Show me the differences.

> Someone sent me this link: https://example.com. What does it do, and is it safe to open?

Claude says which computer it's using before it acts, starts the VM if it isn't
running, checks its own results and tells you where the evidence is. To watch it
work, `metacomputer view` shows how to open the live desktop. More ideas:
[Prompt a computer into existence](#prompt-a-computer-into-existence).

<details>
<summary><b>What the install does, what you click, and how long it takes</b></summary>

**What happens:**
1. It checks the Mac: Apple silicon, macOS 14 or later, free disk, memory, the network.
2. It offers Homebrew and Apple's Command Line Tools, only if they're missing.
3. It clones metacomputer into `~/metacomputer` and runs `./install.sh --yes`, which
   adds Go, GnuPG and ffmpeg from Homebrew if they're missing, builds the VM runner
   (mcomp-vm) and the secrets vault from source, writes this Mac's settings, installs
   the Claude Code plugin, and builds the VM: about 1.5 GB of downloads (Ubuntu's
   cloud image and packages, Cua Driver, the Go and Swift code the build needs),
   unattended provisioning, then a clean snapshot.
4. It starts the VM, runs `metacomputer status` and a short self-test, and prints
   three prompts to try. The VM is left running; `metacomputer stop` stops it, and
   Claude starts it again when it needs it.

**What you click or answer.** On a Mac that already has Homebrew and Apple's
Command Line Tools: nothing. On a new Mac, press Enter when get.sh offers to run
Homebrew's installer, then Return and your Mac password in Homebrew's own
installer (it needs an administrator account); if the Command Line Tools are
still missing, click **Install** and **Agree** in Apple's dialog. get.sh itself
never asks for a password and never runs `sudo`.

Google Chrome goes into the VM next to Firefox, which means accepting
[Google's terms](https://www.google.com/chrome/terms/). To leave it out:
`curl … | bash -s -- --no-chrome`.

**How long it takes.** Measured from scratch (nothing cached, no VM) on a Mac that
already had Homebrew, its packages and the Command Line Tools, on a fast
connection: 5 min 5 s, about 5 min 15 s, and 5 min 40 s. About a minute of that
is yours (pasting the command and reading what it printed); the rest runs
unattended. Estimates, not yet measured, for when it takes longer:
- Homebrew still has to install Go, GnuPG and ffmpeg: add 2-5 minutes;
- the connection is slower: at about 50 Mbit/s, 10-15 minutes in all;
- the Mac is brand new: 15-30 minutes, mostly Apple's and Homebrew's downloads,
  of which about 5 minutes need you.

It needs about 15 GB of free disk and refuses to start with less (it checks each
disk it writes to). The install itself uses about 6 GB. Running it again is safe:
finished steps are skipped, and it never deletes a VM.

</details>

**From Claude Code first.** If you'd rather start in Claude Code, add the plugin:

```text
/plugin marketplace add metavert-io/metacomputer
/plugin install metacomputer@metacomputer
```

Then ask Claude to set up metacomputer. The plugin brings the skills and tools
but not the VM. Its skill sees that nothing is set up, says what setup does and
asks you; on a yes it runs the plugin's own `metacomputer setup --yes`, the same
get.sh shipped inside the plugin, so nothing is downloaded and run from
elsewhere. If Homebrew is missing, run that setup command in Terminal instead,
since Homebrew asks for your password.

Or by hand: `git clone https://github.com/metavert-io/metacomputer.git ~/metacomputer && cd ~/metacomputer && ./install.sh`.

[docs/install.md](docs/install.md) has every option, the stage-by-stage timings,
what goes where, updating, uninstalling and troubleshooting.

## What you get

| Control | What it gives you |
| --- | --- |
| **Isolation, always on** | No clipboard, audio or Rosetta sharing with your Mac ([mcomp-vm](https://github.com/metavert-io/metacomputer-vm), metacomputer's build of Lume). A network gateway on the Mac lets the VM reach the public internet and nothing else. A firewall in the VM, SSH by role with a pinned host key, and every download checked against a pinned checksum or signing key. |
| **A secrets vault** | Passwords, 2FA codes, browser logins and API keys that Claude uses without the values passing through the chat. You unlock it in your own terminal; imported secrets ask before each use (Touch ID, or the secret's name typed there), and ones you add can too (`--approval each`). The vault is bound to your Mac's Secure Enclave. API keys never enter the VM: a proxy on the Mac adds them. Every use goes into a hash-chained audit log you can send off the Mac. |
| **A signed record (opt-in)** | Every tool call and shell run in a hash-chained log, signed with an SSH key. `metacomputer actions verify` finds any edit. |
| **Restricted mode (opt-in)** | Cua Driver's bounded mode: every call must fit a manifest of apps, folders and sites. The shell channel turns itself off. |
| **Events (opt-in)** | Blocked connections, guard refusals, "Claude needs you" and finished tasks, sent to your phone, a webhook, an OpenTelemetry Collector or a SIEM. |

**The honest limit.** This keeps a well-behaved agent contained and makes a
misbehaving one visible. It doesn't make a deliberately hostile agent
harmless: everything runs as your macOS user, and an agent with a shell in the
VM can get at a password once it has been typed into a page.
[SECURITY.md](SECURITY.md#what-holds-by-default) says what holds, and
[what doesn't](SECURITY.md#what-doesnt-hold).

You ask for it in plain words, in any Claude Code session:

> Get yourself a virtual computer and tell me what's on its screen.
>
> Check whether this page looks the same in Firefox and Chrome.
>
> Someone sent me this link. What does it do, and is it safe to open?

[Prompt a computer into existence](#prompt-a-computer-into-existence) has more
than forty more.

**Contents:**
[The 5-minute install](#the-5-minute-install) ·
[Try it](#try-it) ·
[What it is](#what-it-is) ·
[Why I made it](#why-i-made-it) ·
[Security](#security) ·
[It learns](#it-learns-from-its-own-sessions) ·
[Requirements](#requirements) ·
[Install](#install) ·
[Quick start](#quick-start) ·
[Prompt gallery](#prompt-a-computer-into-existence) ·
[How-to guide](#how-to-guide-what-to-ask-claude-code) ·
[Commands](#commands) ·
[Configuration](#configuration) ·
[Isolation and safety](#isolation-and-safety) ·
[Known limitations](#known-limitations) ·
[Troubleshooting](#troubleshooting) ·
[Testing and the benchmark](#testing) ·
[Uninstall](#uninstall) ·
[Contributing](#contributing) ·
[Credits](#acknowledgements) ·
[License](#license)

## What it is

`install.sh` sets up these parts:

| Part | What it is |
| --- | --- |
| The virtual computer | An Ubuntu 24.04 ARM64 desktop (XFCE on Xorg) in a [Lume](https://github.com/trycua/cua/tree/main/libs/lume) VM on Apple's Virtualization framework. Firefox, LibreOffice Writer and Calc, GNOME Calculator, the Thunar file manager, the Mousepad text editor and a terminal come installed, and Google Chrome is optional. So do tools for scripts: Python with openpyxl, python-docx, odfpy and Pillow, poppler-utils for PDFs, sqlite3, zip, ImageMagick, ffmpeg, and venv and pip for anything else. |
| Cua Driver, inside the VM | [Cua Driver](https://github.com/trycua/cua) 0.53.0, an MCP server for the desktop: accessibility trees, mouse and keyboard input, screenshots, recordings, and page-level tools for Chrome. It runs only in the VM and is never installed on your Mac. |
| The shell channel | `metacomputer sh` runs a bash script in the VM as the desktop user and brings back its output and exit code. Claude uses it for files no app holds open, and the desktop for everything an app holds. |
| The secrets vault | `metacomputer secrets`, a small vault on your Mac for passwords, 2FA seeds, sealed browser logins and API keys. It ships here and depends on no password manager. |
| The `metacomputer` plugin for Claude Code | A Claude Code plugin ([plugin/](plugin/), listed by this repository's marketplace) with two skills and an MCP server, in every project. The `metacomputer` skill ([skills/metacomputer/](skills/metacomputer/)) tells Claude Code when a task belongs on the virtual computer and how to work it; `cua-driver` is Cua Driver's own guide. The MCP server, `metacomputer mcp-lazy`, lists the tools (`mcp__plugin_metacomputer_metacomputer__*`) even while the VM is off. It reaches the VM through the hub, one process per VM that `metacomputer call` and scripts share, and that applies every guard to each of them. Without the plugin (`./install.sh --no-plugin`) the same skill and server are a skill link and a user-scope MCP server, and the tools are `mcp__metacomputer__*`. |
| The `metacomputer` command | `bin/metacomputer`, installed as `~/.local/bin/metacomputer` with the short alias `mcomp`. It starts and stops the VM, checks it, runs scripts in it, moves files, records, runs browser checks, keeps snapshots and opens the live view. |

```text
Your Mac
├── Claude Code + the metacomputer skill
│     ├── MCP server (metacomputer mcp-lazy) ──┐
│     ├── metacomputer call, scripts ──────────┴▶ hub: one per VM, every guard ─SSH──┐
│     └── metacomputer sh ─────────────────────────SSH to 127.0.0.1, desktop user──────┤
├── secrets vault (unlocked in your terminal) ──fill account / API proxy over SSH─────┤
├── network gateway (cua-netgw): the VM reaches the public internet only              │
└── mcomp-vm (Lume) ── VM: Ubuntu 24.04 ARM64, XFCE                                    │
                        ├── Cua Driver MCP server ◀────────────────────────────────────┘
                        └── Firefox, Chrome, LibreOffice, Calculator, Thunar, Mousepad, Python
```

What it's good for:
- **Real GUI work:** desktop apps, forms, multi-step flows in a real browser.
- **Files and data:** spreadsheets, documents, PDFs and images, worked on with scripts or in the apps.
- **Visual checks of a web app:** two browsers side by side, layouts at several widths, difference images against an earlier run, an accessibility smoke check.
- **Recorded demos and evidence:** an MP4 of a run, a contact sheet, screenshots and per-action traces.
- **Untrusted links and files,** opened in a throwaway browser profile, away from your Mac.
- **Signing in without sharing credentials in the chat,** and calling APIs without the VM ever holding the key.

Code, files on your Mac, APIs and pages a plain fetch can read stay with Claude
Code's normal tools. The skill doesn't boot a VM for those.

## Why I made it

I wanted Claude Code to have a computer of its own: a real desktop where it
can use apps and browsers the way I would, show me what it did, and open
things I'd rather not open on my own Mac. The parts to build one already
exist, mostly from the [Cua](https://github.com/trycua/cua) project. What I
wanted on top of them was two things: something simple to set up, and
something I'd be comfortable leaving an agent alone with.

### Simplicity: one install, and Claude Code knows what to do

You could put this together yourself. You would need:
- a VM runner (Lume) and an Ubuntu ARM64 image converted into a disk it boots;
- unattended provisioning: a desktop session with accessibility turned on, the apps, Cua Driver, SSH and a firewall;
- a bridge that carries Cua Driver's MCP stream from the VM to Claude Code;
- a network that keeps the VM off your LAN;
- a live view, recordings, a shell, and a way to get files in and out;
- instructions that tell the model when to use the VM, how to drive this particular desktop reliably, and how to check its own work.

metacomputer turns that into `./install.sh` and a skill. The installer builds
the VM from pinned, verified sources and registers everything with Claude
Code. The skill gives Claude Code what it needs from its first session:
- **Which computer.** "Use a computer" means the VM. "My screen" or "my Chrome" means yours, and the VM is never used in its place.
- **A routine.** Check who else is using it, start it, keep a task folder, act from the accessibility tree rather than from screenshots, verify the outcome, and hand back the evidence.
- **Where the work goes.** Following Microsoft Research's [Hybrid Computer Use](https://github.com/microsoft/Hybrid-Computer-Use) work, Claude routes each piece of work by where its state lives: files no app holds open go to the shell, while open documents, settings, signed-in sites and canvases go to their apps.
- **Rules verified on this desktop.** For example: which inputs Firefox ignores, which dialogs freeze which apps, and how to verify a result from structure.

### Security: kept away from your Mac, your network and your accounts

An agent that uses a computer reads whatever is on the screen, and some of it
is written by people who want to steer the agent. No model is immune to that.
So the virtual computer should hold none of your data and reach none of your
machines, the agent's credentials should never pass through its own context,
and every protection should be something you can check. The next section
lists each control and where it ends.

## Security

Compared with running Lume and Cua Driver as they come, metacomputer sets up
the following. These are **always on**:

| Protection | What it does |
| --- | --- |
| **mcomp-vm** (the installer's default) | Lume attaches your Mac's clipboard, its speakers and microphone, and Rosetta to every VM. [mcomp-vm](https://github.com/metavert-io/metacomputer-vm), formerly lume-secure, is metacomputer's build of Lume, made by Metavert, not by Cua and not endorsed by them. It's Lume 0.7.2 plus `--no-share-clipboard`, `--no-share-audio`, `--no-share-rosetta` and `--no-host-sharing`, and metacomputer starts the VM without all three unless you turn one on. `metacomputer status` reports what the VM actually has, read from inside it. |
| **Public internet only** | The VM's network is a user-mode gateway on the Mac (Cua's `cua-netgw`) that checks every connection the VM opens. It refuses your Mac, your LAN and router, Tailscale and CGNAT addresses, link-local and cloud-metadata addresses, and outbound mail ports. Blocked attempts are logged. |
| **Nothing listens off loopback** | SSH and the live view are forwarded only to `127.0.0.1` on your Mac. Lume's own VNC server, which listens on every interface, is never started. The live view is a password-protected VNC server inside the VM, and the VM's firewall admits it, and SSH, only from the gateway. |
| **SSH by role** | Keys only, with the VM's host key generated on your Mac and pinned before the first boot. A key per role: `mcp` can only start Cua Driver's MCP server (a forced command, no shell), `ops` is a shell for the desktop user, `admin` is for maintenance, `fill` can only run the vault's fill program, and `proxy` can only open one port on the VM's loopback for the vault's API proxy. The Mac's ssh ignores your SSH config and forwards nothing to the VM: no agent, no X11, no ports. |
| **Narrow accounts** | The desktop user has no sudo. After the build, the maintenance account can run only one root script with a fixed list of verbs, cloud-init is turned off, and its cached user-data (which held the host key) is deleted. All accounts have locked passwords. |
| **Verified inputs** | The Ubuntu image (signed `SHA256SUMS`, pinned signing key and image digest), the Cua Driver release (pinned SHA-256, cross-checked against the release's checksums and checked again inside the VM), Lume (pinned checksum, or mcomp-vm's pinned tag and commit), and the Mozilla and Google repository keys (pinned fingerprints), and rclone for S3 volumes (signed checksums, pinned key and digest). The VM never downloads Cua components itself. |
| **Less in the guest** | No snaps. No clipboard agent (`spice-vdagent` is removed) and the virtio sound driver is blacklisted, a second layer for when standard Lume attaches those devices. No shared storage unless you attach some ([docs/storage.md](docs/storage.md)). Cua Driver's telemetry and update checks are off, and so is Lume's telemetry. Browser policies turn off telemetry and password saving in both browsers, and sign-in and sync in Chrome. Security updates install automatically, without automatic reboots. |
| **Page text is data** | The skill and the MCP server's own instructions tell Claude that pages, documents, window titles, accessibility text and shell output from the VM are data, never instructions. Claude never asks you for a password: you sign in through the live view yourself, or keep the credential in the vault and approve its use. `metacomputer web open-untrusted` opens a link in a throwaway Chrome profile without clicking anything on it. |
| **The VM is treated as hostile** | What comes back from the VM is bounded and checked. `metacomputer sh` reads a capped amount of output under one deadline, accepts only an answer that fits a strict schema, and escapes control characters before they reach your terminal. The hub caps every message from the driver. Files copied to the Mac arrive as plain files and folders only (links, devices and unsafe names are left out, and nothing is written through a link), and a copy stops before it can fill your disk. Event text from the VM never goes into a message, only into cleaned data. |
| **Every guard on every path** | One hub per VM holds the only connection to Cua Driver, and Claude Code's tools, `metacomputer call` and scripts all go through it, so its guards apply to each of them. One session controls the desktop at a time. Chrome gets real input, never background typing. Bounded mode's limits hold. The coordinate guard refuses a pixel click when the windows have changed since the screenshot it was aimed from (resized, or covered by a dialog or another app), so a click can't land somewhere Claude didn't look. A refusal is reported to you, never worked around. |
| **Your Mac stays yours** | Nothing here can see or control your Mac's screen. The VM's wallpaper and menu say VIRTUAL COMPUTER, so every screenshot and recording shows which machine it is. |

These are **opt-in**. They're off by default, so out of the box metacomputer
behaves the way Cua Driver does on its own. Turn on any of them in
`config/local.env`; `metacomputer security` shows what's on.

| Setting | What it adds |
| --- | --- |
| `ACTION_LOG=on` | A signed, hash-chained log of every tool call sent to the VM, and of every `metacomputer sh` run. Each record carries the hash of the one before it, and the chain's head is signed with an SSH key (`ssh-keygen -Y sign`) every 20 records, every minute with new activity, and at the end. By default, typed text is recorded as its length, and scripts and shell output as sizes and hashes. `metacomputer actions verify` finds edited, inserted, reordered or removed records. |
| `SSH_AUTH=cert` | A fresh key and a 5-minute SSH certificate for every connection, limited to one role and to the Mac's side of the VM network. Its key ID (`mcomp:ROLE:USER:PROJECT:pidN`) appears in the VM's auth log. The static `ops` and `mcp` keys stop working, and with them sealed browser sessions (`secrets session save` and `load`), for now. |
| `DRIVER_MODE=bounded` | Cua Driver's bounded mode: every call must fit a capability manifest of tools, apps, folders and websites, with a time limit. Profiles `readonly`, `desktop` (no terminal) and `browser` (only the sites you list), or your own manifest. The Mac asks for the manifest by its SHA-256, and the VM refuses a manifest that doesn't match and, while bounded, any standard session. The shell channel is off while bounded (`VM_SHELL=auto`), since a shell isn't held to the manifest. |
| An event sink | `metacomputer events sink add` sends events as they happen; with no sink, nothing leaves the Mac. See [The event stream](#the-event-stream) below. |

### The secrets vault

`metacomputer secrets` keeps credentials out of the conversation. It ships with
metacomputer and depends on nothing else: no password manager, no Keychain
entries, no other tools. You store a password, a 2FA (TOTP) seed, a saved
browser login or an API key once, in your own terminal. To use one, you unlock
the vault in your terminal, and you can choose to approve each use by typing
its name.
- **Logins and 2FA codes.** Claude asks the vault to type the value into the field it has focused, and gets back only "filled, N characters". The request and its answer never carry the value, so it doesn't pass through Claude's context, the transcript or the logs on the way. It goes only to the sites you allowed, only into the right kind of field, and in the VM through a separate, locked account Claude can't change. Once typed, it's on the page like any other text: a password field shows dots, but a 2FA code in an ordinary field can be seen in screenshots, accessibility reads and recordings until the page moves on.
- **API keys.** The key never enters the VM. A program there gets a short-lived token for a proxy on your Mac, which puts the real key into requests to that one API host. If the API sends the key back in an answer, the proxy blanks it out.
- **Keys.** By default the vault file is encrypted under your passphrase together with a key held in this Mac's Secure Enclave, so a copy taken elsewhere can't be opened with the passphrase alone. A recovery key, shown once, opens it on another Mac.
- **An audit trail.** Every use, refusal and change goes into a hash-chained log, where the records the vault writes carry a MAC under a key only it holds, and into macOS's unified log. It can be sent off the Mac as it happens; a sink you mark as required stops every use while it can't be reached.
- **Who asked.** Your terminal and the log name the Claude Code session and project behind each request. That's attribution, not isolation: everything runs as you, so a hostile program of yours could pose as another session.

It keeps credentials out of Claude's hands in normal operation. It doesn't make
a deliberately hostile agent harmless: one with a shell in the VM, or one that
clicks a site's "show password" button, can still get at a value once it's in
the browser.
[SECURITY.md](SECURITY.md#secrets-and-logins-metacomputer-secrets) has the full
model, and [compares it with AWS KMS, Secrets Manager and HashiCorp Vault](SECURITY.md#if-you-know-aws-kms-secrets-manager-or-hashicorp-vault).

### The shell channel

`metacomputer sh` runs bash in the VM as the desktop user. It can do what that
user can: read and write their files, start apps on the desktop, reach the
public internet. It can't reach your Mac (the same SSH path, with nothing
forwarded) or get root.
- **Bounded mode turns it off** (`VM_SHELL=auto`), because a shell would get around the driver's manifest. `VM_SHELL=on` keeps it on anyway; `VM_SHELL=off` turns it off everywhere.
- **It counts as acting:** it's refused while another session is using the computer, unless you agree to `--override`.
- **It's logged when the action log is on** (`SHELL_LOG=auto`): the script's hash and size, the exit code and duration, and each output's size and hash. The script and output text are kept only with `ACTION_LOG_TEXT=keep`.

Details in [SECURITY.md](SECURITY.md#the-shell-channel-metacomputer-sh) and
[docs/shell.md](docs/shell.md).

### The event stream

`metacomputer events` sends what matters as it happens: Claude blocked on you
(a sign-in, a CAPTCHA, a 2FA code), a task or recording finished, connections
the gateway blocked, refusals by the Chrome guard or bounded mode, a failed
`actions verify`, what `web open-untrusted` found, and the VM's health. Sinks
are webhooks (CloudEvents, signed), OpenTelemetry (OTLP/HTTP), syslog over TLS
and ntfy for your phone.
- **Off by default.** With no sink, nothing is sent; events go only to a local log.
- **What's sent:** tool names, counts, paths, the VM's and the Mac's short names, and the scheme, host and port of web pages. **Never** typed text, passwords, tokens, page contents, screenshots or URL queries. Every string is cleaned first, and text from inside the VM goes only into cleaned data, never into a message.
- **Floods are capped:** at most 10 events per family in any 10 minutes, and a phone gets at most 10 in 10 minutes and 30 an hour. The rest are summed up.
- **Its threat model is scoped on purpose.** It defends against hostile text from the VM and the web, and against floods. Against a hostile program running as you, it offers detection, not prevention: adding or removing a sink needs your terminal and is announced to your existing sinks, and an edit to the configuration file made any other way is noticed and announced too.

It's best effort, not an audit trail; the signed action log and the vault's
audit are the tamper-evident records. Details in
[SECURITY.md](SECURITY.md#event-stream-metacomputer-events-optional) and
[docs/events.md](docs/events.md).

### Where it ends

- **Your own account.** None of this protects against someone who already controls your Mac account: they can change the config, read the keys and drive the VM directly.
- **A deliberately hostile agent.** Claude Code runs as you. The controls above keep a well-behaved agent away from your machines and secrets and make a misbehaving one visible; they don't stop one that sets out to work around them as your user.
- **The public internet.** The VM can reach it by design, so anything you put in the VM could be sent out.

[SECURITY.md](SECURITY.md) explains each control and where it ends, and
[Isolation and safety](#isolation-and-safety) has the specifics.

## It learns from its own sessions

Claude makes some of the same mistakes again and again on a desktop: clicking
by pixel without a fresh screenshot, or trying a third way to do something
without checking why the first two failed. `metacomputer learn report` reads
your own Claude Code sessions that used the virtual computer, counts what went
wrong and where the time went, and proposes short lessons with the evidence:
"34 pixel clicks refused for lack of a screenshot, in 17 of 33 sessions", say,
and the rule that avoids them.

You decide which lessons to keep (`metacomputer learn review`, or tell Claude
in a chat which ones to approve). Approved lessons go to every later session
as advice, after the skill's own habits. They can't switch off a safety check,
override a refusal or overrule you. Reading your sessions and storing the
lessons happen on your Mac, but an approved lesson becomes part of the
instructions each Claude Code session sends to the model, like the skill
itself, so don't put anything in one you wouldn't send to Claude.
`metacomputer learn bench` runs the benchmark on a test VM with and without
them, so you can see whether they help.

Nothing a web page or document in the VM says can become a lesson: proposals
are built only from counts and fixed names, and lessons in your own words need
your own terminal. A lessons file changed some other way isn't sent until you
look at it and accept it. [docs/learn.md](docs/learn.md) has the details, and
SECURITY.md the protections and their limits.

## Requirements

- A Mac with Apple silicon and macOS 14 or later.
- About 15 GB of free disk for the VM and its clean snapshot (the installer warns below 20 GB), and as much again for each extra VM. The install downloads about 1.5 GB.
- Memory for the VM: it gets 4 vCPUs and 8 GB by default, or 4 GB on a Mac with less than 16 GB.
- [Homebrew](https://brew.sh), to install any missing tools: git, curl, Go, GnuPG, ffmpeg and Python 3.
- Apple's Command Line Tools (`xcode-select --install`), to build mcomp-vm and the secrets vault. Without them, use `--lume=standard`.
- For `metacomputer secrets` only: macOS 15 or later. Everything else works without it.
- [Claude Code](https://claude.com/claude-code), for the plugin (the skills and the MCP server). `claude plugin` needs a recent version; with an older one, `install.sh` links the skill and registers the server instead.
- Optional: Google Chrome in the VM. Installing it means accepting [Google's terms](https://www.google.com/chrome/terms/).

## Install

metacomputer has two parts, and you need both:

| Part | Where you get it | What it gives you |
| --- | --- | --- |
| The Claude Code plugin | A Claude Code session, or `./install.sh` | The skills and the MCP tools, in every project |
| The checkout and `./install.sh` | Your Mac's terminal | The VM, the `metacomputer` command, this Mac's settings and the secrets vault. The plugin runs this checkout's `bin/metacomputer` |

**The plugin, from Claude Code** (in a session):

```text
/plugin marketplace add metavert-io/metacomputer
/plugin install metacomputer@metacomputer
```

On Claude Code 2.1.275 or later, one step does both:
`/plugin install metacomputer --marketplace metavert-io/metacomputer` (or, in a
shell, `claude plugin install metacomputer --marketplace metavert-io/metacomputer`).
The plugin alone has no VM: until the next step is done, its MCP server stops
with a message saying to set it up.

**The VM and the command, on your Mac**, in one command (it checks the Mac,
offers Homebrew and Apple's Command Line Tools if they're missing, clones into
`~/metacomputer` and runs `./install.sh --yes`; see [The 5-minute install](#the-5-minute-install)
above). [docs/install.md](docs/install.md) has the details,
measured times, the plugin-first path, updating, uninstalling and
troubleshooting:

```bash
curl -fsSL https://raw.githubusercontent.com/metavert-io/metacomputer/main/get.sh | bash
```

Or by hand: `git clone https://github.com/metavert-io/metacomputer.git && cd metacomputer && ./install.sh`.

`./install.sh` installs the plugin too, from the checkout, so you can skip the
`/plugin` commands. If you added the plugin from GitHub first, it keeps that one.
The two sources update differently:
- **From the checkout** (what `install.sh` does), the plugin loads in place: after
  a `git pull`, Claude Code picks up the new skills at the next session start.
- **From GitHub**, Claude Code keeps a copy that follows the repository's commits.
  Update it with `/plugin marketplace update metacomputer` or
  `claude plugin update metacomputer@metacomputer`. Its copy of `bin/metacomputer`
  runs your checkout's, through `~/.local/bin/metacomputer` (or the checkout that
  `MCOMP_HOME` names).

`./install.sh` shows what it will do and asks once (Enter means yes; `--yes`
skips it). Its defaults: mcomp-vm runs the VM (`--lume=standard` for Cua's
release, which always shares your clipboard, audio and Rosetta with the VM),
Google Chrome goes in next to Firefox (`--no-chrome`), and the VM is built now
(`--no-build`).

| Option | Effect |
| --- | --- |
| `--yes` | Take the defaults: mcomp-vm, Chrome included, build now |
| `--no-chrome` | Firefox only in the VM |
| `--lume=mcomp-vm` / `--lume=standard` | mcomp-vm (the default) or Cua's standard Lume (`--lume=secure`, from before the rename, still means mcomp-vm) |
| `--no-build` | Set everything up but don't build the VM yet |
| `--no-claude` | Don't touch Claude Code (no command links, plugin, skill or MCP server) |
| `--no-plugin` | Claude Code without the plugin: the skill link and a user-scope MCP server, as before the plugin (tools: `mcp__metacomputer__*`). It removes this checkout's plugin |
| `--only-claude` | Only the Claude Code step: the command links and the plugin (or, with `--no-plugin`, the skill and server) |
| `--uninstall` | Remove the command, the plugin (or the skill and the MCP registration) and the vault program (the VM and your vault file stay) |

Everything else is automatic. Each step is skipped when it's already done, so
running `./install.sh` again is safe.

The tools' names depend on how the server is registered:
`mcp__plugin_metacomputer_metacomputer__*` from the plugin, `mcp__metacomputer__*`
without it. The skill works with either. Permission rules, scripts and MCP
configurations of your own that name `mcp__metacomputer__` need the plugin's
prefix too (for example `"permissions": {"allow": ["mcp__plugin_metacomputer_metacomputer__*"]}`).
- **mcomp-vm** (or standard Lume), at a pinned and verified version. Telemetry off, no background service.
- **This Mac's settings** in `config/local.env`, never committed.
- **The command** `~/.local/bin/metacomputer` and its alias `mcomp`.
- **The plugin** `metacomputer@metacomputer`, at user scope, from the marketplace `metacomputer` (this checkout): the skills `metacomputer` and `cua-driver` and the MCP server `metacomputer` (`…/bin/metacomputer mcp-lazy`). An earlier install's skill link and user-scope MCP server are removed when they point at this checkout, so the tools aren't listed twice. A marketplace named `metacomputer` that's already registered is used only when it's this checkout or github.com/metavert-io/metacomputer; from anywhere else its plugin isn't installed (it would run that source's code on your Mac), the installer says how to remove it, and sets up the skill link and server instead.
- **Without the plugin** (`--no-plugin`, or a Claude Code without `claude plugin`): the skill, linked at `~/.claude/skills/metacomputer`, and the MCP server `metacomputer` at user scope (`claude mcp add -s user metacomputer …/bin/metacomputer mcp-lazy`).
- **The secrets vault program**, `~/.local/share/metacomputer/bin/mcomp-vault`, built from `vault/` by `scripts/build-vault.sh` (skipped, with a note, before macOS 15 or without `swiftc`).

mcomp-vm goes to `~/.local/bin/mcomp-vm` (its files in
`~/.local/share/mcomp-vm`) and standard Lume to `~/.local/bin/lume`. A Mac set
up before the rename has the same build as `~/.local/bin/lume-secure`, and
metacomputer keeps using it until you run `./install.sh` again (when `LUME`
names a runner that isn't installed, it uses lume-secure and prints a notice).
The installer then builds mcomp-vm beside it and points `LUME` there. Both see
the same VMs, and lume-secure stays until you remove it.

The VM build then runs unattended, about 2-3 minutes on a fast connection
(longer on a slow one). It downloads Ubuntu's cloud image and the Cua Driver
release, checks their signatures and checksums, provisions the desktop, and
takes a clean snapshot (the baseline). Its log goes to `logs/build-*.log`.

## Quick start

```bash
metacomputer start     # boot it (about 12 s)
metacomputer status    # every layer from the VM to the MCP handshake; exit code 0 means ready
metacomputer test      # six recorded GUI checks: desktop, calculator, browsers, LibreOffice, Chrome
metacomputer view      # how to watch the desktop
```

Restart any open Claude Code sessions, then ask:

> Get yourself a virtual computer and tell me what's on its screen.

Claude says which computer it's using before it acts ("Using the virtual
computer, an Ubuntu VM, not your Mac"). It checks whether another session is
using the VM, starts it if needed, does the work, checks the result, and
reports what it checked and where the evidence is.

If `~/.local/bin` isn't on your `PATH`, use the full path
`~/.local/bin/metacomputer` (Claude always does).

## Prompt a computer into existence

Each of these is something you can type into Claude Code with the skill
installed. Claude decides what runs on the desktop and what goes through the
shell, says which computer it's using, and ends with a check and the paths of
what it made. Where a prompt needs an app that isn't in the standard VM, it
says which package to add to `EXTRA_PACKAGES` (and the VM then needs a
rebuild; see [Add apps](#add-apps)).

**Creative**

| You say | What happens |
| --- | --- |
| "Draw a flat two-colour logo for a coffee shop called North Star in Inkscape, export a 512 px PNG, and give me the SVG and the PNG." | Claude draws on the canvas with real pointer clicks, opens dialogs with their shortcuts, saves early, and copies both files to your Mac. Needs `inkscape`. |
| "Trim the silence from these nine sound effects in Audacity, fade each one out, level them, and export them as WAV." | Claude selects on the waveform by pixel, works the menus from the keyboard, and exports each file with a typed path. It can't hear, so it judges the sound from the waveform. Needs `audacity`. |
| "Put these three clips on a timeline in Kdenlive, in order, and save the project." | Claude adds the clips and arranges them by pixel, since the timeline isn't in the accessibility tree, then saves with Save As. Needs `kdenlive frei0r-plugins mediainfo`. |
| "Resize these 40 photos to 1600 px wide and make me a contact sheet of all of them." | A script with ImageMagick in the VM does the resizing and the sheet; you get a zip back. |
| "Record yourself editing this clip in Kdenlive, with the sound." | A whole-desktop recording with the VM's own sound card. Needs `kdenlive` and `VIRTUAL_AUDIO=on`. |

**Office and data**

| You say | What happens |
| --- | --- |
| "Here's last quarter's sales CSV. Build a Calc workbook with totals by region and a column chart, and save it as .ods and .xlsx." | Claude works in LibreOffice Calc on the desktop, using pixel clicks for the chart wizard, and checks the saved files. |
| "Convert every .docx in this folder to PDF and give me a zip." | One `metacomputer sh` script: LibreOffice headless with its own profile, then `zip`. |
| "Pull the date, invoice number and total out of these 30 PDF invoices into one spreadsheet." | `pdftotext` and Python in the VM, written out with openpyxl, then a look at the result in Calc. |
| "Fill in this Word template once for each row of the CSV, one document per customer." | python-docx in the VM fills the template; Claude opens one result in Writer to check the layout. |
| "Load these CSVs into SQLite and tell me which customer ordered the most in March." | sqlite3 in the VM, with the query and its answer in the reply. |
| "Fix the broken formulas in column F of this spreadsheet, and show me what you changed." | Calc on the desktop, because the open document is what gets saved; Claude lists the cells it changed. |
| "Write my notes up as a memo in Writer: a Heading 1 title, a bulleted list, page numbers. Save it as .odt and .docx." | Writer on the desktop, styles from its menus, and both files read back to check. |

**Web**

| You say | What happens |
| --- | --- |
| "Does our landing page look the same in Firefox and Chrome? Show me the differences." | `metacomputer web compare`: both browsers side by side, with differences in headings, links, controls and text. |
| "Show me the pricing page at desktop, tablet and phone widths, and tell me where the layout breaks." | `metacomputer web responsive` at 1920, 1280, 768 and 500 px, then a look at each screenshot. |
| "Fill in the contact form on our public staging site with test data and confirm the thank-you page appears." | Chrome in page mode fills each field exactly, submits, and checks the next page. |
| "Sign in to the demo dashboard with my `demo` login and its 2FA code from the vault, then download this month's report." | The vault types the username, password and current code; Claude never sees them. You approve in your terminal if you asked to. |
| "Use my OpenAI key from the VM without the VM seeing it: run this script against the API there." | Claude gets a 15-minute token; the script calls the proxy inside the VM, and the proxy on your Mac adds the real key. |
| "Keep me signed in to the admin site between runs." | Claude seals the signed-in Chrome profile into the vault and loads it next time, both with your OK. (Sealed sessions need `SSH_AUTH=key`, the default, for now.) |
| "Run an accessibility check on our sign-up page." | `metacomputer web a11y`: unlabelled controls, images without text alternatives, missing headings, vague links. |

**Testing and QA**

| You say | What happens |
| --- | --- |
| "Build the site, serve it inside the VM, and click through sign-up, checkout and the account page in both browsers. Tell me what breaks." | Claude copies the build in, serves it on the VM's own localhost, and works each flow like a user. |
| "Record a repro of the bug where Save stays disabled after editing the title, with a contact sheet I can attach to the issue." | A task folder, a recording with per-action traces, and a contact sheet copied to your Mac. |
| "Compare today's pages with last week's run and list the ones that changed." | `metacomputer web responsive --baseline` with similarity scores and difference images. |
| "Run the GUI smoke tests and tell me if anything fails." | `metacomputer test`: six recorded tests, each checked from the app's state and the files on disk. |
| "Check every link in the footer and tell me which ones fail." | A script in the VM, then a browser look at the ones that did. |
| "Upload this CSV through the import page in Chrome and check that the row count matches." | Page mode handles the file picker; Claude reads the count back from the page. |

**Safety**

| You say | What happens |
| --- | --- |
| "Someone sent me this link. What does it do, and is it safe to open?" | `metacomputer web open-untrusted`: redirects, the final page, any password prompts or downloads, with nothing on the page clicked. |
| "This PDF came from an unknown sender. Open it on the virtual computer, not my Mac, and tell me what's in it." | The file goes into the VM only; `pdftotext` and `pdfinfo` read it there, and Claude reports what it says and any web addresses in it. |
| "Download the installer from this site in the VM and tell me its checksum and what's in the archive, without running it." | `curl`, `sha256sum` and `unzip -l` in the VM. Nothing reaches your Mac. |
| "Where do these three shortened links really go?" | One `open-untrusted` run per link, each in a throwaway profile. |
| "Did the VM try to reach anything on my network today?" | Claude reads the gateway's log of blocked connections on your Mac and sums it up. |

**Games**

| You say | What happens |
| --- | --- |
| "Write a small arcade game in Python with pygame, run it on the virtual computer and play a round to check the controls." | Claude writes the game, starts it on the desktop through the shell, and plays from desktop screenshots and key presses. Needs `python3-pygame`. |
| "Give the game an autopilot that plays it, run 100 autopilot games, and put the scores per level in a Calc sheet with a chart." | The games run headless through `metacomputer sh`; the numbers go into a Calc workbook with a chart. Needs `python3-pygame`. |
| "Tune the difficulty table until a beginner autopilot clears level 1 at least 80% of the time. Show me the numbers before and after." | Claude changes the table, reruns the autopilot, and repeats until the goal holds. |
| "Playtest a level and write down every bug and confusing moment you hit." | Claude plays from desktop screenshots, since a game window's contents aren't in the accessibility tree, and keeps notes as it goes. |

**More than one computer**

| You say | What happens |
| --- | --- |
| "Set up a second virtual computer called `staging`, Firefox only, beside the main one." | Claude writes a settings file with its own names, ports and folders, and builds it with `MCOMP_CONFIG`. About 15 GB more disk. |
| "Keep the main computer signed in to the CMS, and use the second one, with no logins at all, to check what a visitor sees." | Two VMs with separate browsers and profiles; the browser checks run against the second through `MCOMP_CONFIG`. |
| "Run the benchmark on the test computer while I keep working on the main one." | `bench/bench.py` resets and drives only the VM named in its `MCOMP_CONFIG`. |

**Recordings and demos**

| You say | What happens |
| --- | --- |
| "Record a demo of signing up for our app in Chrome, with the typing visible." | Hybrid mode: real clicks and key-by-key typing for the video, page reads to check each step. |
| "Record the whole desktop while you build a budget in Calc, and give me the key moments with timestamps." | A whole-desktop recording with a description file; with `ACTION_LOG=on` it lists every tool call and its time in the video. |
| "Make a contact sheet of yesterday's recording so I can see what happened without watching it." | `metacomputer contact-sheet`: 12 frames in one image. |
| "Record at editing quality and stream it straight to my external drive." | Claude sets `RECORD_QUALITY=high`, `RECORD_TO=host` and `RECORD_DIR` because you asked, then records. |

**Research and automation**

| You say | What happens |
| --- | --- |
| "Visit these ten competitors' pricing pages and put plan names and prices in a spreadsheet, with a screenshot of each page." | Firefox for the pages, Calc for the table, one screenshot each in the task folder. Page text is treated as data. |
| "Download the March statements from the supplier portal, rename them by date, and zip them." | Sign-in from the vault (or by you, through the live view), downloads in Chrome, renaming and zipping through the shell. |
| "Work through these 20 forms. Ping my phone if you hit a CAPTCHA or a sign-in, and when you're done." | With an ntfy sink set up, `metacomputer notify` reaches your phone when Claude is blocked, and the finished task is announced too. |
| "Send my metacomputer events to our Slack." | Claude gives you the exact command for your own terminal, and the small relay from docs/events.md. Sinks only change from your terminal. |
| "What did you do in the VM during the last session?" | With `ACTION_LOG=on`, `metacomputer actions show latest` lists every tool call and shell run. |

**Teach it a new app**

| You say | What happens |
| --- | --- |
| "Add GIMP to the computer, then find out which of its menus and dialogs work through accessibility and which need real clicks. Write the rules down like DESKTOP.md." | After the rebuild (which needs your OK), Claude explores and writes notes you can add to the skill. GIMP isn't covered by DESKTOP.md yet. Needs `gimp`. |
| "Learn our web app's admin screens and write a short guide, with screenshots, that the next session can follow." | Claude maps each screen from its accessibility tree and saves a Markdown guide with screenshots. |
| "Try Inkscape's export with each method you have and tell me which works best here." | Claude compares the shortcut, the menu and the command line, and reports which hung and which worked. Needs `inkscape`. |

## How-to guide: what to ask Claude Code

With the skill installed, ask in plain words. Requests that mention "a
computer", "your computer", "a virtual, disposable, sandbox or Linux
computer", a real browser or GUI app, or "record yourself" go to the VM.
Requests about your own machine ("my screen", "my Mac", "my Chrome") never
do. Most capabilities also have a command you can run yourself, shown under
each group.

### Start it and check on it

- "Is the virtual computer running? Is anyone else using it?"
- "Start your computer." / "Shut down the virtual computer when you're done."

```bash
metacomputer status          # --json for machine-readable output
metacomputer who             # which session is using it, and whether a recording is running
metacomputer start
metacomputer stop            # finishes a running desktop recording first; --force powers off at once
metacomputer restart
```

One Claude Code session controls the VM at a time. While another session has
acted in the last 10 minutes, a second session's actions and shell runs are
refused (reads still work), and `stop`, `reset`, `restore`, `baseline` and
`test` refuse unless given `--override`. Claude asks you before taking over;
`metacomputer release` hands the VM over.

### Use desktop apps

- "Use the calculator on your computer to work out 137 × 29."
- "On the virtual computer, write a short note in the text editor, save it as notes.txt and give me the file."
- "Open LibreOffice Writer, write a one-page memo with a Heading 1 title, save it as memo.odt and copy it to my Mac."
- "Make a small budget in LibreOffice Calc with a SUM row and save it as budget.ods."
- "Open the file manager on your computer and tell me what's in Downloads."
- "Copy the first paragraph of this page in Firefox into a Writer document and save it."

Claude works from the accessibility tree: it finds controls by role and name,
batches the steps it has decided on, and verifies the outcome (a value in the
app, a file on disk) rather than trusting a screenshot. It takes a screenshot
only when the tree can't answer, such as for canvases, images or layout.

### Work with files through the shell

- "Convert these Word documents to PDF and zip them."
- "Count the rows per category in this CSV and give me the totals."
- "What's in this archive? Don't extract anything."

`metacomputer sh` runs a script in the VM as the desktop user, with the
desktop's environment, and returns its output, exit code and duration. Claude
routes work by where its state lives: files no app holds open go to the shell;
a document open in an app is changed in that app, because the app's copy is
what gets saved. It says when part of a task went through the shell, and never
writes a file behind an app's back to fake a GUI result.

```bash
metacomputer sh -c 'ls -la ~/Downloads'
metacomputer sh --file ./convert.sh -- ~/Inbox/reports        # a script from your Mac, with arguments
metacomputer sh --json -c 'pdftotext -layout ~/Inbox/invoice.pdf - | head -40'
metacomputer sh --read-only -c 'ls ~/Documents'                # a check that changes nothing
```

- `--timeout SECONDS` (default 120, at most 3600) stops the script and everything in its process group. `--max-output BYTES` (default 64 KB per stream, at most 1 MB) keeps the first and last part; the full size and SHA-256 are reported.
- Exit status is the script's own; 124 means it timed out, 125 that it was refused (`VM_SHELL`, bounded mode, another session), 255 that it couldn't run (VM off, SSH, a malformed answer from the VM).
- `~` and `$HOME` are the desktop user's home in the VM; nothing is expanded on the Mac.
- To start an app on the desktop from a script, detach it: `setsid -f mousepad FILE </dev/null >/tmp/mousepad.log 2>&1`.
- The VM has Python 3.12 with openpyxl, python-docx, odfpy, Pillow, PyYAML and requests; LibreOffice for headless conversion (`soffice -env:UserInstallation=file:///tmp/lo-convert --headless --convert-to pdf FILE`); `pdftotext` and friends, `sqlite3`, `zip` and `unzip`, `jq`, ImageMagick, ffmpeg, git and curl. `python3 -m venv` gives you pip for anything else, at user level.

[docs/shell.md](docs/shell.md) has the design and
[skills/metacomputer/SHELL.md](skills/metacomputer/SHELL.md) the routing Claude follows.

### Browse the web

- "Use Firefox to open example.com and follow its link."
- "Search Wikipedia for Ada Lovelace in Firefox on your computer and tell me the first sentence of the article."

Firefox is the default browser. Claude works it like a person: it reads the
page through the accessibility tree and clicks and types in the window.

### Use Chrome in page or hybrid mode

With Chrome installed, Claude can work a browser three ways:

| Mode | How | Browsers | Good for |
| --- | --- | --- | --- |
| **human** (default) | Reads the accessibility tree; clicks and types in the window like a person | Firefox, Chrome | Demos, recordings, UX checks |
| **page** | Works on the page's own elements through Chrome's tools, in the background; fastest and exact | Chrome | Forms, dropdowns, uploads, downloads, dialogs, reading a site exactly |
| **hybrid** | Reads and checks through the page; acts with real clicks and key-by-key typing that show in a recording | Chrome | Realistic recordings with exact verification |

- "In Chrome, drive the page directly and fill in the form at this URL with these details: …"
- "Upload report.pdf through the form on this page." (Claude copies the file into the VM first.)
- "Download the CSV linked on this page and give it to me."
- "Fill in the sign-up form in Chrome and show it typing, so it looks right in a recording." (hybrid)
- "Use Chrome like a person would." (human)

Page mode uses a throwaway Chrome profile unless you ask for a named one.
Asking for page or hybrid mode in Firefox gets an offer to use Chrome instead,
because Cua Driver has no page tools for Firefox.

```bash
metacomputer browser                    # the current defaults
metacomputer browser default chrome     # firefox | chrome
metacomputer browser mode page          # human | page | hybrid (page and hybrid need Chrome)
```

### Sign in to a site

- "Open a Chrome profile called shopping and wait while I sign in."

The VM holds none of your accounts, and Claude never types your credentials
itself. It opens the window and asks you to sign in through the live view
(`metacomputer view`), or uses the vault (next section). Later sessions with
the same profile name reuse its cookies until the VM is reset.

```bash
metacomputer browser profiles                    # named Chrome profiles in the VM
metacomputer browser profiles --delete shopping  # asks first
```

### Use saved passwords and 2FA codes

- "Sign in to the demo site as me with the `demo` login, then do X."
- "Log in to example.com; the code for its 2FA is in the vault as `example-otp`."
- "Keep this signed-in session so tomorrow's run doesn't have to log in again."

You set this up once, in your own terminal. Claude can't do these steps, and never
asks you for a passphrase, password or code in the chat:

```bash
./scripts/build-vault.sh                 # or ./install.sh, which builds it too
metacomputer secrets init                # creates the vault; asks for a passphrase; prints a recovery key once
metacomputer secrets set example --kind password --origin https://example.com --username you@example.com
metacomputer secrets set example-otp --kind totp --origin https://example.com    # the base32 seed
metacomputer secrets bind                # pins the VM; compare the fingerprint, then type yes
metacomputer secrets unlock --for 1h     # keep this terminal open while Claude works
```

Values are typed with the echo off, never on a command line. Write the recovery key
down and keep it away from this Mac. `set` on a name that already exists refuses unless
you add `--replace` (see [Rotate keys and keep an audit trail](#rotate-keys-and-keep-an-audit-trail)).

Or bring the logins you already have, in your own terminal:

```bash
metacomputer secrets import-passwords --browser chrome --dry-run   # what would come in: sites, masked usernames
metacomputer secrets import-passwords --browser chrome             # macOS asks about Chrome's key: Allow (once)
metacomputer secrets import-passwords --csv ~/Downloads/passwords.csv   # Chrome, Passwords app, 1Password or Bitwarden export
metacomputer secrets import-session --browser chrome --site github.com --name gh --to-profile work   # one site's cookies
```

Imported logins ask before every use until you relax them (`policy NAME --approval session`).
A Passwords app or 1Password export's one-time-code column comes in as a 2FA secret too.
`import-session` brings a site's cookies, not its localStorage, sealed for a page-mode Chrome
profile in the VM; `session load NAME` puts them there (`--site HOST` sends only the cookies
that site gets). It needs Chrome 130 or later, whose cookie values name their own domain, and
stops if any value names another one. Delete a CSV export once it's in.

Then Claude:
1. opens the site in page-mode Chrome;
2. clicks into the field;
3. runs `metacomputer secrets fill example --field username`, then `--field password` and `--field otp`.

How it works:
- **The unlock terminal:** it prints each request, who asked and the outcome, never the values. Close it, or press Ctrl-C, and the vault locks.
- **Approving each use:** add `--approval each` to `set` (or `--confirm each` to `unlock`), and every use waits for you in that terminal. It shows the secret, the site and who asked (for example `Asked by Claude Code in ~/Projects/foo`). What you type decides how far the answer reaches:
  - `NAME`: this use only;
  - `NAME session`: this use and later uses of that secret from the same Claude Code session;
  - `NAME unlock`: every use of that secret until the vault locks.
- **Approving from elsewhere:** each waiting use has a request id. `metacomputer secrets requests` lists them (who asked, the site, a masked username, how long it has waited); `metacomputer secrets approve ID` allows one with Touch ID or your login password, `approve ID --passphrase` with the vault passphrase, and `deny ID` refuses it. `unlock --touch-id` shows the Touch ID dialog for every question by itself. In presence mode (below) nothing Claude can run approves: only your finger, your password or your passphrase does. With typed approvals, a program that can type into the unlock terminal could answer for you ([SECURITY.md](SECURITY.md#what-doesnt-hold), "Keystroke injection into the unlock terminal").
- **Presence mode:** on a Mac with Touch ID (and a finger enrolled), every approval and every `NAME session` / `NAME unlock` grant needs Touch ID (or your login password in macOS's dialog) or the passphrase; what you type at the unlock terminal only chooses how far it reaches, since any program that can type into that terminal could type it too, and the dialog or the passphrase prompt names that scope (`approve ID --scope session|unlock` asks for a wider one explicitly). It stays on when Touch ID can't be checked, and once the vault has seen Touch ID enrolled, until you turn it off. On a Mac without Touch ID, typed answers stay the default and `unlock` warns once. Switch it for one unlock with `--require-presence` / `--no-require-presence`, or keep a setting with `metacomputer secrets presence on|off|auto`; `secrets status` and `metacomputer security` show which is in force. A program that controls your terminal still sees the passphrase as you type it: only the system dialog defends against that ([SECURITY.md](SECURITY.md#approving-from-elsewhere-request-ids-touch-id-the-passphrase)).
- **Unattended rules:** `metacomputer secrets rule add --secret NAME --project DIR --for 8h` (or `--site https://...`, or `--session NAME`) lets that use happen without asking for Claude Code sessions in that folder, until it expires (at most 30 days). A rule only decides whether to ask: the value still goes only to the site checked in the VM. Making one takes Touch ID or the passphrase; `rule list` and `rule remove ID` manage them. `policy NAME --approval session|each` changes whether a secret asks at all.
- **Identity providers always ask:** a fill on Google's, Microsoft's, Apple's or Okta's sign-in pages asks every time, whatever the secret, the unlock or a rule says (the list is in [SECURITY.md](SECURITY.md#identity-providers-always-ask)).
- **One approval per sign-in:** `metacomputer secrets login NAME --username-selector CSS --password-selector CSS` fills both fields under one question.
- **Coming from Cua Spaces?** The same verbs work (`status`, `init`, `unlock`, `lock`, `import-passwords`, `requests`, `approve`, `deny`), and the audit log carries its event names. Claude signs in with `metacomputer secrets login` (or `fill`), under the same approvals. [docs/moving-from-spaces.md](docs/moving-from-spaces.md) maps everything and says what differs.
- **Limiting a secret to a project:** `set ... --project DIR` refuses requests from Claude Code sessions working outside that folder. Like the "who asked" line, this keeps honest sessions apart; it isn't a wall, since any program of yours can pose as a Claude Code session.
- **Keeping a login:** `metacomputer secrets session save NAME --profile P` seals a signed-in Chrome profile into the vault (it asks first), and `session load NAME` brings it back after a reset. A whole profile may hold any sign-in, so loading one asks every time and no rule covers it. For now this needs `SSH_AUTH=key` (the default): the vault signs in with the VM's static desktop key, which `SSH_AUTH=cert` turns off, so both commands refuse at once under `cert`. Fills and API tokens use their own accounts and work either way.
- **What Claude can see:** `metacomputer secrets list` shows names, kinds, sites, usernames, approval, project limits, ages and use counts, never values. `metacomputer secrets audit` shows the history.

Limits to know:
- Fills need the page-mode Chrome that Cua Driver starts, not Firefox.
- The VM needs to be built with this version, which adds the locked fill account. Older VMs say so and refuse.
- Usernames and one-time codes are visible on the page once typed; passwords show as dots.
- Unlock in a plain terminal window. Inside tmux or screen, another program could answer for you, so uses that need your answer are refused there unless you unlock with `--touch-id` (answer with Touch ID instead) or `--allow-multiplexer`.

The full model is in [SECURITY.md](SECURITY.md#secrets-and-logins-metacomputer-secrets).

### Use an API key from the VM without the VM seeing it

- "Use my OpenAI key from the VM without the VM seeing it: run this script against the API there."
- "Test the app in the VM against the real API, using the `openai` key from my vault."

You store the key once, in your own terminal. It goes in a request header (by default
`Authorization: Bearer <key>`):

```bash
metacomputer secrets set openai --kind api --api-host api.openai.com
metacomputer secrets set example-api --kind api --api-host api.example.com --header 'x-api-key: {value}' --api-path-prefix /v1
```

Then Claude asks for a token and points the program in the VM at the proxy:

```bash
metacomputer secrets token openai --ttl 15m   # prints the token, its id and base_url http://127.0.0.1:18765/openai/ (inside the VM)
metacomputer secrets tokens                   # the live tokens
metacomputer secrets revoke ID                # end one now (--all for every one)
```

The program in the VM sends its requests to that base URL with the token where the key
would go. The proxy on your Mac takes only well-formed HTTP/1.1 (or 1.0) requests, checks the
token and the path (a prefix such as `/v1` covers whole segments: `/v1` and `/v1/...`,
never `/v10` or `/v1-admin`), puts the real key in, and sends the request on to that API's host
only, over HTTPS. It never follows a redirect. If an answer contains the key, even
compressed, the proxy replaces it with `[key withheld by metacomputer secrets]`.

- A token lasts 15 minutes by default and at most 8 hours, and every token ends when the vault locks. Changing or deleting the secret ends its tokens too, so a token never picks up a new value or a wider scope.
- A token is a bearer credential: whatever holds it can call that API as you until it ends. It can't get the key or reach another host.
- The key is withheld only as an exact copy; an API that sent it back transformed (in base64, say) would hand it over.
- The VM needs the proxy account, which VMs built before API tokens don't have. `token` says so; rebuild the VM.

### Rotate keys and keep an audit trail

- "Which of my secrets haven't been rotated in a while?" (`metacomputer secrets list` shows each one's age)
- "Are the vault's audit sinks delivering?" (`metacomputer secrets audit-sink list`)
- "Check that the vault's audit log hasn't been changed." (`metacomputer secrets audit --verify`)

Rotating is up to you; nothing rotates on a schedule. These run in your own terminal
and ask for the passphrase:

```bash
metacomputer secrets set openai --kind api --api-host api.openai.com --replace   # a new value, recorded as a rotation
metacomputer secrets rekey                 # new passphrase, data key, Enclave key and recovery key
metacomputer secrets rotate-session-key    # re-encrypt every sealed browser login under a new key
```

**The Secure Enclave and the recovery key.** By default `init` binds the vault to
this Mac's Secure Enclave (when it has one) and prints a recovery key once. The
Enclave key never leaves the chip, so a copy of the vault file can't be opened
elsewhere with the passphrase alone.
- `metacomputer secrets recover` opens the vault with the recovery key, on another Mac or after this Mac's Enclave is reset, and sets a new passphrase and keys.
- `rekey --enclave` moves a passphrase-only vault onto this Mac's Enclave; `rekey --no-enclave` moves it off.
- `init --no-enclave` makes a passphrase-only vault, which opens on any Mac with the passphrase.

**The audit log.** Every unlock, lock, use, refusal, proxied API request and change is
one line in `audit.jsonl` in the vault folder, with names, sites, outcomes and who asked,
never values or tokens. Each line carries the hash of the one before it and a MAC
under the vault's own audit key, and each event also goes to macOS's unified log:

```bash
metacomputer secrets audit --tail 20
metacomputer secrets audit --verify        # every record's MAC and the chain, checked by the vault (while unlocked)
metacomputer secrets audit --head          # the current head, SEQ:HASH, to note at a moment you trust
python3 scripts/verify-audit.py ~/Library/Application\ Support/metacomputer/audit.jsonl
log show --predicate 'subsystem == "io.metavert.metacomputer.vault"' --last 1d
```

`verify-audit.py` recomputes every hash and checks the chain and the sequence
(`--expect-start` and `--expect-head` pin the ends). Your own account could
still rewrite the file and its hashes, but not the MACs: `audit --verify` names
any record the vault didn't write. Records that were deleted, or never
delivered before a rewrite, are simply missing, so copies sent off the Mac as
events happen are the record to rely on. There are four kinds of these
**audit sinks**, all optional:

| Kind | Sends to |
| --- | --- |
| `webhook` | Any HTTPS endpoint, one signed POST per event |
| `otlp` | An OpenTelemetry collector or service (OTLP/HTTP logs), signed |
| `syslog` | A SIEM or rsyslog, over TLS |
| `ntfy` | Your phone, through ntfy |

```bash
metacomputer secrets audit-sink add otel --kind otlp --url http://127.0.0.1:4318 --required   # uses wait for it to accept their record
metacomputer secrets audit-sink add siem --kind syslog --url syslog+tls://siem.example:6514     # best effort: syslog can't acknowledge a record
metacomputer secrets audit-sink add phone --kind ntfy --url ask     # the topic is typed, not echoed
```

- `--required` (webhook and OTLP sinks only; syslog and ntfy can't acknowledge a record) fails closed: each use is first recorded and must reach every required sink before it goes ahead. While one can't be reached, fills, tokens and session saves and loads are refused, and proxied API requests get 503.
- The vault signs a record for a sink only after it passes its own checks; a record that fails reaches the sinks as an `audit_tamper` notice instead, and the unlock terminal says `AUDIT LOG CHANGED`.
- `scripts/verify-audit.py --key KEY FILE...` checks what a sink received: signatures, hashes, chain and gaps (`--require-verified` also fails on records a locked client wrote).
- Adding or removing a sink needs your terminal and the passphrase. `audit-sink list` works from anywhere, though while the vault is locked it shows only each sink's delivery state, not its settings. `audit-sink test NAME` works from anywhere while the vault is unlocked; while it's locked, it asks for the passphrase in your terminal.

[SECURITY.md](SECURITY.md#the-audit-log-and-sending-it-off-this-mac) has the event
fields, each sink's format, what each check proves, an OCSF mapping and collector
examples.

### Check a page in both browsers, at several widths, for accessibility

These checks are scripted: they need no model and write `report.md`,
`report.json` and images to a results folder.

| Ask | Or run |
| --- | --- |
| "Does this page look the same in Firefox and Chrome?" | `metacomputer web compare URL` |
| "Show me this page at desktop, tablet and phone widths." | `metacomputer web responsive URL --widths 1920,1280,768,500` |
| "What changed on this page since the last run?" | `metacomputer web responsive URL --baseline results/<earlier-run-folder>` |
| "Run an accessibility check on this page." | `metacomputer web a11y URL` |

- `compare` shows the two browsers side by side and lists differences in title, headings, links, controls and text. It needs Chrome.
- `responsive` and `a11y` use Chrome unless given `--browser firefox`. `responsive --baseline` adds similarity scores and difference images. `a11y` looks for unlabelled controls, images without text alternatives, missing headings and vague link text; Chrome finds more.
- Every `web` command takes `--task NAME` (write into that task's folder), `--record` (record the screen), `--json` and `--override`.

### Test a web app you're building

- "Build the site and click through the checkout flow on the virtual computer, in both browsers."

The VM can't reach your Mac, by design, so a dev server on your Mac isn't
visible to it. Claude builds the app as static files, copies the folder in,
and serves it inside the VM:

```bash
metacomputer put dist
metacomputer sh -c 'cd ~/Inbox/dist && setsid -f python3 -m http.server 8000 </dev/null >/tmp/http.log 2>&1'
# then open http://localhost:8000 in the VM's browser
```

### Open an untrusted link or file

- "Someone sent me this link. What does it do, and is it safe to open?"
- "Open this Word document on the virtual computer, not on my Mac, and tell me what it says."

```bash
metacomputer web open-untrusted URL              # --watch 10: seconds to watch for redirects
```

`open-untrusted` follows the server's redirects with `curl` inside the VM,
then opens the link in a throwaway Chrome profile and watches it. It reports
in-page redirects and tab changes, the final page with a screenshot, any
password fields (a sign that the page wants your credentials), and any
downloads it started, which stay in the VM. Nothing on the page is clicked or
typed into. Everything in the report came from the page, so it is treated as
untrusted. With an event sink, a password prompt, a download or a redirect to
another site is also sent as a security event.

### Record a task

- "Record yourself filling in this form in Chrome."
- "Use a disposable computer to write this memo in LibreOffice, and record the whole desktop while you do it."

There are two recorders:
- **Per task, through Cua Driver.** Claude makes a task folder and calls `start_recording`. It produces a 1920×1080, 30 fps MP4 and a folder per action with the state, screenshots and click markers before and after. `metacomputer task finish` copies it all to the Mac and makes a contact sheet.
- **The whole desktop, whoever drives it.** `metacomputer record start NAME` and `record stop` capture the screen with ffmpeg, at 20 fps by default. A description file (`NAME.json`) beside the MP4 lists its times, settings, length, size, sound track and SHA-256. With `ACTION_LOG=on` it also lists the key moments: every tool call and shell run made on this VM while it recorded, with its time in the video (for example `0:42 launch_app inkscape`).

```bash
metacomputer record start demo          # name and, optionally, frames per second
metacomputer record stop                # saves recordings/manual/demo.mp4 and demo.json
metacomputer record moments recordings/manual/demo.mp4
metacomputer recordings                 # recent recordings; --sync copies the VM's task runs to the Mac
metacomputer contact-sheet FILE.mp4     # 12 frames in one image
```

`RECORD_QUALITY=high` is sharp enough to edit into videos (several times
larger). `RECORD_TO=host` streams the recording to the Mac while it runs, so
nothing builds up on the VM's disk. `RECORD_DIR` can be any folder, such as
an external drive. Shell steps don't show in a recording, so for a recorded
demo Claude uses the apps.

Recordings capture the whole screen. Passwords filled by `metacomputer secrets`
show as dots, but usernames and codes are visible. With the driver's recorder,
Claude passes any other sensitive value it types to `redact_values`, or leaves
those steps out of the recording.

### Move files in and out

| Ask | Or run |
| --- | --- |
| "Here's data.csv. Open it on your computer." | `metacomputer put data.csv` (to `~/Inbox` in the VM; `--to '~/Documents'` for elsewhere). Every file is checked by SHA-256, and a name that's taken lands as `data (1).csv` unless you add `--overwrite` |
| "Give me the file you saved." | `metacomputer get '~/Documents/memo.odt'` (to `results/files/<time>/`, or `--task NAME`) |
| "Take a screenshot of the virtual computer." | `metacomputer shot` (to `screenshots/manual/`, or `--task NAME`) |
| "Rename that download to report.pdf." | `metacomputer rename GUEST_PATH report.pdf` |

Quote a VM path that starts with `~`, or your Mac's shell expands it to your
Mac's home folder.

For a task, Claude keeps one folder in the VM and one on the Mac:

```bash
metacomputer task start invoice-form    # prints NAME, RECORDING_DIR, FILES_DIR (in the VM) and HOST_DIR (on the Mac)
metacomputer task finish NAME           # copies everything to HOST_DIR, makes contact sheets, writes artifacts.json
```

With `ACTION_LOG=on`, `task finish` also copies the action logs written
during the task; the copies verify like the originals.

### Keep files across resets: volumes and reference disks

- "Keep my project files on a volume so they survive a reset."
- "Give the VM a read-only copy of this folder of reference papers."

```bash
metacomputer volume create work --size 20G        # a disk image on the Mac, sparse
metacomputer refdisk create papers --from ~/papers-for-vm   # a read-only image of a folder
# in config/local.env:
VOLUMES=work                 # in the VM at ~/data/work, read-write
REFDISKS=papers:/mnt/papers  # read-only, at a mount point of your choice
metacomputer restart         # attached at start
```

- A **disk volume** is read-write, belongs to one VM at a time, and is formatted inside the VM on first use (only if it's blank). It survives `reset`, `restore` and `build --replace`; `checkpoint NAME --with-volumes` snapshots it with the VM.
- A **reference disk** is read-only and any number of VMs can attach it.
- Mount points: `/volume`, `/mnt`, `/data`, `/srv`, `/workspace` or a folder in the VM user's home, so scripts written for Cua Spaces' `/volume` keep working.
- Images go in `STORAGE_DIR` (default `~/.local/share/metacomputer`) or `--dir`: an external drive or a NAS mount works too (read-write volumes on a network drive get a warning). A missing image refuses the start; `start --no-volumes` starts with nothing attached.

[docs/storage.md](docs/storage.md) has the details and a table for anyone coming from Cua Volume.

### Share a folder with the VM (visible in Finder)

- "Put your results in a folder I can open in Finder."
- "Read the files in my project's `assets` folder, but don't change them."

```bash
metacomputer volume create assets --folder ~/work/assets            # read-only unless you say :rw
metacomputer volume create handoff --folder ~/vm-handoff.noindex    # read-write needs .noindex in the path
# in config/local.env:
VOLUMES="assets:/mnt/assets,handoff:/data/handoff:rw"
metacomputer restart
```

- **Read-only by default.** The Mac holds the share read-only, and the VM proves it at every start.
- **Read-write (`:rw`) is opt-in** and one VM at a time. While the VM runs, a watcher on the Mac puts macOS's quarantine attribute on everything the VM writes (so Gatekeeper checks it like a download, and puts it back if the VM removes it), replaces links that lead out of the folder with a note, and removes FIFOs and devices. The folder must be kept out of Spotlight with a `.noindex` name, because `.metadata_never_index` no longer works.
- Your personal folders (home, Documents, Desktop, Downloads, Library and iCloud Drive, `.ssh` and other dot-folders) and this checkout are refused, links resolved.

Details and what the watcher can't do: [docs/storage.md](docs/storage.md#folder-volumes).

### Keep a folder in step with an S3 bucket

- "Use the project files in our team bucket, and put your results there."

```bash
metacomputer volume create team --s3 --bucket my-bucket --endpoint https://s3.us-east-1.amazonaws.com --prefix projects/x
metacomputer volume keys team      # in your own terminal: macOS asks for the keys (into the Keychain)
metacomputer volume test team      # endpoint, keys, bucket versioning
# in config/local.env:
VOLUMES="team:/mnt/team:rw"
metacomputer restart
```

The VM sees a folder; rclone (pinned and verified, on the Mac) syncs it with the bucket at start, every minute (`S3_SYNC_INTERVAL`) and at stop. A file changed on both sides is kept twice (`name.conflict1.ext`). The keys never enter the VM, a command line or a config file. Works with AWS S3, R2, MinIO or any S3-compatible store. Details: [docs/storage.md](docs/storage.md#s3-volumes).

### Look inside the VM without the GUI

- "What's running on your computer right now? Is anything still downloading?"

```bash
metacomputer inspect                     # everything
metacomputer inspect windows ports       # sections: system windows processes ports files browser
metacomputer inspect files --minutes 30  # files changed in ~ and /tmp in the last 30 minutes
metacomputer inspect --json
```

`inspect` is read-only, so it works even while another session controls the
VM. It shows windows (with the ids the driver tools use), apps with their
helper processes, CPU and memory, listening ports, recent files, downloads
(`.part` means still running), the default browser and whether a recording is
running. Command lines are masked.

### Watch or take over

- "Let me watch what you're doing." / "I'll take over for a minute."

`metacomputer view` prints how to connect (`--open` opens Screen Sharing):
- **On the Mac:** `open vnc://127.0.0.1:5977`.
- **From another device:** tunnel over SSH (`ssh -N -L 5977:127.0.0.1:5977 <your-mac>`), then connect a VNC client to `localhost:5977`. If Tailscale is installed, `view` also prints the `tailscale serve` line that would publish it to your tailnet instead.

The password is in `~/.ssh/metacomputer/vnc-pass`, or in your own secret
store (see `VNC_PASSWORD_STORE` under [Configuration](#configuration)).

### Hear from it: notifications and events

- "Ping my phone if you get stuck at a sign-in, and when the task is done."
- "Send my metacomputer events to our Slack." (Claude gives you the command and the relay from docs/events.md.)
- "Is anything waiting to be delivered?"

Sinks decide what leaves your Mac, so you add them in your own terminal;
`events sink add` and `remove` refuse without one, and Claude doesn't change
them.

```bash
# in your own terminal: your phone, through ntfy (a long random topic; anyone who knows it can read it)
metacomputer events sink add phone --kind ntfy --url https://ntfy.sh/<long-random-topic> --families needs_you,task
metacomputer events sink test phone      # one test event, now
metacomputer events status               # sinks, the watcher, backlogs, failing sinks
metacomputer events tail                 # the local log
```

- When Claude is blocked on you (a sign-in, a CAPTCHA, a 2FA code, a dialog it won't accept, an approval) it runs `metacomputer notify "what it needs" --why login --action "what to do"` and says the same in the chat.
- The rest is sent automatically: finished tasks and recordings, security signals (blocked connections, the Chrome guard, bounded-mode refusals, a failed `actions verify`, `open-untrusted` findings), the VM's lifecycle, and a health probe every 5 minutes.
- Families: `needs_you`, `task`, `security`, `lifecycle`, and `actions` (every action-log record, for a SIEM; only when you name it). Filter per sink with `--families` and `--min-severity`.
- Kinds: `webhook` (CloudEvents, signed with HMAC-SHA256), `otlp` (to an OpenTelemetry Collector, which can forward to S3, CloudWatch, Datadog, Splunk, Loki and others), `syslog` (RFC 5424 over TLS) and `ntfy`. Use `--header-env 'Authorization=Bearer $TOKEN'` so a token stays out of your shell history, and give sinks ingest-only tokens: the sink file is readable by anything running as you.
- While a sink exists, `metacomputer start` also starts a small watcher process, and `metacomputer status` shows an `events` line.

[docs/events.md](docs/events.md) has every event, the wire formats, a Slack
relay, a Collector configuration and an OCSF mapping.

### Save, restore and reset

| Ask | Or run |
| --- | --- |
| "Reset the virtual computer to a clean state." | `metacomputer reset` |
| "Save a checkpoint called before-upgrade." | `metacomputer stop`, then `metacomputer checkpoint before-upgrade` |
| "Go back to the before-upgrade checkpoint." | `metacomputer restore before-upgrade` |
| "Make the current state the new clean baseline." | `metacomputer baseline` |

- `reset` replaces the working VM with a fresh copy of the baseline. It copies the VM's task runs to the Mac first; recordings and results on the Mac are kept.
- Checkpoints are taken while the VM is stopped. `metacomputer checkpoints` lists them; `checkpoint NAME --replace` overwrites one; `checkpoint --delete NAME` deletes one.
- `baseline` keeps the previous baseline as `<BASELINE_NAME>-prev`.
- `reset`, `restore`, `baseline` and `checkpoint --delete` ask first (`--yes` skips the question). From a script or another program, with no terminal to ask on, they refuse unless given `--yes`. Claude never resets, restores, rebuilds or re-baselines the VM without your explicit OK.

### Add apps

The desktop user has no sudo, so system packages can't be installed from
inside the VM (a Python venv works for user-level packages). Add them to
`EXTRA_PACKAGES` in `config/local.env` instead. They come from Ubuntu's
archive, without "recommended" extras, and take effect on the next build.

| App | `EXTRA_PACKAGES` |
| --- | --- |
| Inkscape (vector drawing) | `inkscape` |
| Audacity (audio editing) | `audacity` |
| Kdenlive (video editing) | `kdenlive frei0r-plugins mediainfo` |
| pygame (games in Python) | `python3-pygame` |
| Ristretto (image viewer) | `ristretto` |
| GIMP (image editing; no verified rules yet) | `gimp` |

```bash
# config/local.env
EXTRA_PACKAGES="inkscape audacity"
```

```bash
metacomputer build --replace     # rebuilds the VM AND its baseline; everything in the VM is lost
```

A rebuild gives the VM a new host key. If you use the secrets vault, run
`metacomputer secrets bind` again in your own terminal afterwards: until you
do, fills, sessions and tokens refuse the rebuilt VM, since it no longer
matches the key the vault pinned.

Then:
- "Draw a simple logo in Inkscape and export it as a PNG."
- "Trim the silence from the start of this recording in Audacity."

[EXTRA-APPS.md](skills/metacomputer/EXTRA-APPS.md) has rules for Inkscape,
Audacity, Kdenlive and pygame windows, verified in a VM built this way. Some of their dialogs, canvases and timelines need real
pointer clicks instead of accessibility actions.

### Give the VM sound

`VIRTUAL_AUDIO=on` (next build) adds a sound card inside the VM that plays
into nothing: PulseAudio with a single null sink. Browsers, games and audio
or video editors get working sound output. Nothing reaches your Mac's
speakers or microphone: the virtio sound driver stays blacklisted, and Lume's
own audio sharing is the separate `SHARE_AUDIO` setting.

With the sound card, whole-desktop recordings include the VM's sound
(`RECORD_AUDIO=auto`, the default; `off` for video only). The sound is within
about 50 ms of the picture. `metacomputer status` shows the sound card when
the VM has one.

- "Record the whole desktop, with sound, while you edit this clip in Kdenlive." (with Kdenlive added through `EXTRA_PACKAGES`)

### Run a second VM side by side

`MCOMP_CONFIG` points the command at another settings file, used in place of
`config/local.env` (the defaults still apply). Give that VM its own
`VM_NAME`, `BASELINE_NAME`, `SSH_DIR`, `SSH_PORT`, `VNC_PORT`, `RUN_DIR` and
`LOG_DIR`, so the two never share a gateway, a lock or logs.
[config/local.env.example](config/local.env.example) has a commented
example.

```bash
MCOMP_CONFIG=~/second.env metacomputer build
MCOMP_CONFIG=~/second.env metacomputer start
MCOMP_CONFIG=~/second.env metacomputer web compare https://example.com
```

The `metacomputer` MCP server that `install.sh` registers drives the VM named
in `config/local.env`. To give a Claude Code session the second VM instead,
start it with `MCOMP_CONFIG` set and an MCP configuration whose `metacomputer`
server passes the same variable. The benchmark runs its sessions this way.

```json
{"mcpServers": {"metacomputer": {"type": "stdio",
  "command": "/path/to/metacomputer/bin/metacomputer", "args": ["mcp-lazy"],
  "env": {"MCOMP_CONFIG": "/Users/you/second.env"}}}}
```

```bash
MCOMP_CONFIG=~/second.env claude --strict-mcp-config --mcp-config second-mcp.json
```

`--strict-mcp-config` leaves your other MCP servers out of that session.

### Turn on the optional hardening

- "Which hardening options are on for your computer?"
- "Show me exactly what you sent to the virtual computer in the last session."
- "Check that the action logs haven't been changed."

```bash
metacomputer security                 # what's on, in the config and in the VM, and the shell channel; --json
metacomputer security apply           # bring the running VM in line with the config (start does it too)
metacomputer actions list             # recent logged sessions; --days N
metacomputer actions show latest      # one session, readable; --json
metacomputer actions verify --all     # exit 1 if anything was changed
metacomputer actions verify FILE --key action-log.pub   # on another machine, with the public key
```

To turn one on, set it in `config/local.env` (for example `ACTION_LOG=on`,
`SSH_AUTH=cert`, or `DRIVER_MODE=bounded` with `DRIVER_PROFILE=browser` and
`DRIVER_ORIGINS="https://example.com"`), then run `metacomputer security
apply` or restart. Claude changes these settings only when you ask, never to
get around a limit. When bounded mode refuses a call, Claude tells you what
was refused and that you can allow it.

## Commands

`metacomputer` (installed at `~/.local/bin/metacomputer`; `mcomp` is a short
alias). `metacomputer help` prints the same list with every option.

| Command | What it does |
| --- | --- |
| `start [--no-volumes]` / `stop [--force]` / `restart` | Boot (about 12 s), attaching `VOLUMES` and `REFDISKS` (none with `--no-volumes`); shut down from inside the VM, forced after 90 s |
| `status [--json]` | Every layer: VM, gateway, SSH, desktop, accessibility, driver, live view, host sharing, the vault's accounts, events, MCP handshake. Exit 0 = ready |
| `who` / `release` | Who is using the VM; hand it over (one controller at a time) |
| `reset [--yes]` | Restore the clean baseline (asks first; evidence on the Mac is kept) |
| `checkpoint NAME [--replace] [--with-volumes]` / `checkpoint --delete NAME [--yes]` / `checkpoints` / `restore NAME [--yes]` | Saved states of the stopped VM, with its disk volumes if you ask (deleting and restoring ask first) |
| `volume create NAME --size 20G [--dir PATH]` / `volume create NAME --folder PATH` / `volume list [--json]` / `volume resize NAME --size 40G` / `volume delete NAME [--yes]` | Disk volumes (kept outside the VM, read-write to one VM) and folder volumes (a Mac folder, read-only unless `:rw`), attached with `VOLUMES` |
| `volume create NAME --s3 --bucket B --endpoint URL [--region R] [--prefix P]` / `volume keys NAME` / `volume test NAME` / `volume sync NAME [--force] [--resync]` | S3 volumes: a folder kept in step with a bucket; the keys (your own terminal, into the Keychain); a connection check; a sync now |
| `refdisk create NAME --from FOLDER [--dir PATH]` / `refdisk list [--json]` / `refdisk delete NAME [--yes]` | Read-only reference disks packed from a Mac folder, attached with `REFDISKS` |
| `baseline [--yes]` | Make the current VM the new baseline (the old one is kept as `-prev`) |
| `build [--replace]` | Build the VM and its baseline from scratch |
| `sh -c CMD \| --file SCRIPT \| - [--cwd DIR] [--timeout S] [--max-output B] [--read-only] [--json] [--raw] [-- ARGS]` | Run bash in the VM as the desktop user, with the desktop's environment |
| `inspect [SECTION...] [--minutes N] [--top N] [--json]` | Read-only snapshot of the VM: `system`, `windows`, `processes`, `ports`, `files`, `browser` |
| `task start SLUG` / `task finish NAME` | One folder per task: video, contact sheet, screenshots, files, `artifacts.json` |
| `put HOST_PATH... [--to GUEST_DIR] [--overwrite]` | Copy files into the VM (default `~/Inbox`), each checked by SHA-256; a taken name lands as `name (1).ext` unless `--overwrite` |
| `get GUEST_PATH [HOST_DIR] [--task NAME]` | Copy files out of the VM |
| `shot [HOST_PATH] [--task NAME]` | A PNG of the whole VM screen |
| `rename GUEST_PATH NEW_NAME` | Rename a file in the VM (a download saved under its id) |
| `record start [NAME] [FPS]` / `record stop` / `record status` / `record moments FILE` | Record the whole desktop; its key moments when the action log is on |
| `recordings [--sync] [N]` | List recent recordings; copy the VM's task runs to the Mac |
| `contact-sheet MP4 [OUT.png]` | 12 frames of a recording in one image |
| `web compare\|responsive\|a11y\|open-untrusted URL [...]` | Browser checks that need no model |
| `browser [default firefox\|chrome] [mode human\|page\|hybrid] [profiles [--delete NAME]]` | The skill's defaults; named Chrome profiles |
| `view [--open]` | Watch or take over the desktop (VNC on loopback) |
| `input-reset [--check]` | Remove the driver's leftover virtual input devices (when Chrome ignores typing) |
| `desktop-restart` | Log the desktop out and in again (closes every app) |
| `security [apply] [--json]` | The optional hardening and the shell channel, as configured and as the VM has them |
| `actions list\|show\|verify` | The signed action log (`ACTION_LOG=on`) |
| `secrets init\|set\|delete\|bind\|unlock` | The credentials vault: commands you run in your own terminal (they ask for the vault passphrase or a value); `set` on an existing name needs `--replace` |
| `secrets rekey\|recover\|rotate-session-key` | New keys, or opening the vault with its recovery key (your own terminal) |
| `secrets audit-sink add\|remove\|show NAME --key` | Off-Mac copies of the audit log, and a sink's signing key (your own terminal) |
| `secrets list\|fill\|session save\|session load` | What Claude uses while the vault is unlocked; values never come back |
| `secrets token NAME [--ttl 15m]`, `secrets tokens`, `secrets revoke ID\|--all` | A short-lived API token and the proxy's URL in the VM; the live tokens; end them |
| `secrets status\|audit\|selftest`, `secrets audit --verify\|--head`, `secrets audit-sink list\|test NAME` | State, the audit history (no values) and its checks, the vault's self-test; the sinks and their backlog |
| `notify "TEXT" [--why login\|captcha\|2fa\|dialog\|approval\|other] [--action TEXT]` | Tell your event sinks that Claude is blocked on you |
| `events sink add\|remove NAME ...` | Add or remove an event sink (your own terminal) |
| `events sink list\|show\|test`, `events status\|tail`, `events emit`, `events drain`, `events watcher start\|stop\|status` | The sinks, the stream's state and local log, an event from a script, a retry now, the watcher |
| `learn report [--since 7d] [--project PATH] [--json] [--draft-with-claude]` | Mine your sessions with the VM for repeated mistakes and time sinks; propose lessons with their evidence |
| `learn review`, `learn approve\|reject\|remove ID...`, `learn list [--instructions]` | Go through the proposals; keep or drop lessons; see what every new session gets |
| `learn edit ID`, `learn add` | Change a lesson's text, or write your own (your own terminal) |
| `learn bench --config TEST_ENV [--yes]` | The benchmark with and without your lessons, on a test VM (asks first: it spends your Claude usage) |
| `test [A-F]` | The GUI smoke suite, each test recorded |
| `ssh [ops\|admin] [CMD...]` | A raw SSH shell in the VM (`ops` is the desktop user); prefer `sh` |
| `logs [N]` | The last lines of the Lume, gateway and VM logs |
| `where` | Project, config, skill, docs and results folders; VM names |
| `mcp-lazy` / `mcp` | The MCP bridge Claude Code uses, through the hub (`mcp` with `MCOMP_DIRECT_BRIDGE=1` is the hub's own connection; only the transport tests open it otherwise) |
| `call [--session NAME] TOOL 'JSON'` / `hub [status\|stop]` | One tool call through the hub (with `--session`, tokens and driver state carry over between calls); the hub's pid, clients and idle time |

`stop`, `reset`, `restore`, `baseline`, `test`, `desktop-restart` and `sh`
refuse while another session is using the VM; `--override` proceeds anyway.

## Configuration

`config/defaults.env` holds the pinned versions and defaults. `install.sh`
writes this Mac's settings to `config/local.env`, which is never committed and
overrides any default. [config/local.env.example](config/local.env.example)
documents the common keys. `MCOMP_CONFIG=/path/to/file` uses another file
instead.

**The VM** (all but the names take effect on the next build):

| Key | Default | |
| --- | --- | --- |
| `VM_NAME`, `BASELINE_NAME` | `metacomputer`, `metacomputer-baseline` | Lume VM names |
| `VM_CPU`, `VM_MEMORY` | `4`, `8GB` | VM size |
| `BROWSERS` | `firefox chrome` | Browsers built in; `firefox` alone leaves Chrome out |
| `EXTRA_PACKAGES` | (none) | More Ubuntu packages, e.g. `inkscape gimp` |
| `VIRTUAL_AUDIO` | `off` | `on`: a sound card inside the VM that plays into nothing |
| `SECRETS_PROXY_PORT` | `18765` | The port inside the VM (on its loopback) where the secrets API proxy appears |

**Lume and host sharing:**

| Key | Default | |
| --- | --- | --- |
| `LUME` | `~/.local/bin/mcomp-vm` | The Lume that runs the VM: mcomp-vm, or `~/.local/bin/lume` for Cua's release (`install.sh --lume=standard`). If it isn't installed and `~/.local/bin/lume-secure`, mcomp-vm's former name, is, metacomputer uses that and says so |
| `SHARE_CLIPBOARD`, `SHARE_AUDIO`, `SHARE_ROSETTA` | `off` | `on` connects the VM to your Mac's clipboard, audio or Rosetta (mcomp-vm only; standard Lume always shares) |

**Mac side:**

| Key | Default | |
| --- | --- | --- |
| `SSH_DIR` | `~/.ssh/metacomputer` | SSH keys, the pinned host key and the live-view password |
| `SSH_PORT`, `VNC_PORT` | `2277`, `5977` | Loopback ports for the VM's SSH and live view |
| `RUN_DIR`, `LOG_DIR` | `.run/`, `logs/` in the checkout | Runtime files and logs; a second VM needs its own |
| `VNC_PASSWORD_STORE` | `file` | `file` (0600 in `SSH_DIR`), or `command` with `VNC_PASSWORD_GET` / `VNC_PASSWORD_SET` for your own secret store |

**Skill defaults** (`metacomputer browser` changes them):

| Key | Default | |
| --- | --- | --- |
| `BROWSER` | `firefox` | `firefox` or `chrome` |
| `MODE` | `human` | `human`, `page` or `hybrid` |

**The hub** (one connection per VM, shared by every client; [architecture.md](architecture.md#the-hub)):

| Key | Default | |
| --- | --- | --- |
| `MCP_HUB` | `on` | `on`: Claude Code's bridge, `metacomputer call` and scripts share one hub per VM, which applies every guard to each of them; `off`: each connects on its own, with the same guards |
| `HUB_IDLE` | `10m` | The hub exits after this long with no client and no driver recording |
| `COORD_GUARD` | `on` | `on`: pixel input is refused when the windows changed since the screenshot it was aimed from; `off`: only Cua Driver's own checks |

**Lessons** (`metacomputer learn`; [docs/learn.md](docs/learn.md)):

| Key | Default | |
| --- | --- | --- |
| `LESSONS` | `on` | `on`: the lessons you approved go in every new session's MCP instructions, as advice; `off`: kept, not sent |
| `LESSONS_DIR` | `~/.local/share/metacomputer/lessons` | Their private store (a 0700 folder, 0600 files, read only when private) |

**Shell channel** (`metacomputer sh`; [docs/shell.md](docs/shell.md)):

| Key | Default | |
| --- | --- | --- |
| `VM_SHELL` | `auto` | `auto`: on, except while `DRIVER_MODE=bounded`; `on`: always; `off`: every run is refused |
| `SHELL_LOG` | `auto` | `auto`: each run goes in the signed action log when `ACTION_LOG=on`; `off`: never logged |

**Storage** ([docs/storage.md](docs/storage.md); each takes effect at the next start):

| Key | Default | |
| --- | --- | --- |
| `STORAGE_DIR` | `~/.local/share/metacomputer` | Where new volumes and reference disks go: any folder you can reach, such as an external drive or a NAS mount (`--dir` per item) |
| `VOLUMES` | (none) | Volumes to attach: `name[:mountpoint][:rw]`, comma-separated; default mount point `~/data/NAME`. Disk volumes are read-write; folder volumes read-only unless `:rw` |
| `REFDISKS` | (none) | Read-only reference disks to attach: `name[:mountpoint]`; default `~/ref/NAME` |
| `S3_SYNC_INTERVAL` | `60` | Seconds between syncs of S3 volumes while the VM runs (they also sync at start and stop) |
| `S3_BACKUP_MAX_GB` | `5` | How much of the local files a sync overwrote or deleted is kept (newest first) |

**Desktop recordings** (`metacomputer record`):

| Key | Default | |
| --- | --- | --- |
| `RECORD_FPS` | `20` | Frames per second |
| `RECORD_QUALITY` | `standard` | `high`: sharp enough to edit into videos, several times larger |
| `RECORD_TO` | `guest` | `host`: stream to the Mac while recording, so nothing builds up in the VM |
| `RECORD_DIR` | `recordings/manual/` | Any folder on the Mac, such as an external drive; only its last part is created |
| `RECORD_AUDIO` | `auto` | `auto`: include the VM's sound when it has the virtual sound card; `off`: video only |

**Optional hardening** ([SECURITY.md](SECURITY.md)):

| Key | Default | |
| --- | --- | --- |
| `ACTION_LOG` | `off` | `on`: signed log of every tool call and shell run, in `logs/actions/` |
| `ACTION_LOG_TEXT` | `redact` | `keep`: record typed text, values, scripts and shell output in full |
| `ACTION_LOG_KEY` | `$SSH_DIR/action-log` | The signing key, or a `.pub` whose private key is in ssh-agent (for example hardware-backed) |
| `SSH_AUTH` | `key` | `cert`: a fresh key and a short-lived certificate per connection |
| `SSH_CERT_TTL`, `SSH_CA_KEY` | `5m`, `$SSH_DIR/ca` | Certificate lifetime; the CA key, or a `.pub` in ssh-agent |
| `DRIVER_MODE` | `standard` | `bounded`: Cua Driver does only what a manifest lists |
| `DRIVER_PROFILE` | `desktop` | `readonly`, `desktop` or `browser` ([config/driver-profiles/](config/driver-profiles/)) |
| `DRIVER_ORIGINS` | (none) | The sites the `browser` profile may open, e.g. `"https://example.com https://app.example.com"` |
| `DRIVER_MANIFEST` | (none) | Your own Cua capability manifest instead of a profile |
| `DRIVER_EXPIRES`, `DRIVER_IDLE` | `8h`, `30m` | A bounded session's lifetime (at most 24h) and idle limit |

**The event stream and the vault** keep their settings elsewhere: event sinks
in `~/.config/metacomputer/events.json` (changed only with `metacomputer events
sink`; `MCOMP_EVENTS_DIR` moves the folder, `MCOMP_EVENTS_SPOOL` the drop folder
for other programs' events), and the vault in
`~/Library/Application Support/metacomputer/` (`MCOMP_VAULT_DIR`).

## Isolation and safety

**Host sharing.** Lume attaches a SPICE clipboard port (your Mac's clipboard),
a virtio sound device (its speakers and microphone) and a Rosetta share to
every VM.
- With mcomp-vm, `metacomputer start` leaves out each one whose `SHARE_*` setting isn't `on`.
- With standard Lume all three are always attached. The VM has no `spice-vdagent` and `virtio_snd` is blacklisted, but a process with root in the VM could start an agent and read or set your clipboard. Don't keep secrets on the clipboard while it runs untrusted tasks.
- `metacomputer status` shows what the VM actually has, read from inside it (virtio console ports, a sound device, a Rosetta tag). With mcomp-vm and the defaults it reads `clipboard off  audio off  rosetta off`.

**Network.** The VM's network device talks to `cua-netgw`, a user-mode gateway
on your Mac built from Cua's source at a pinned commit. Every connection the
VM opens is checked on the Mac, not inside the VM:
- **Always refused:** your Mac's loopback and every address of its own interfaces (LAN and tailnet), the gateway itself, cloud metadata, multicast and broadcast.
- **Refused by [config/netgw-rules.json](config/netgw-rules.json):** private LAN ranges (10/8, 172.16/12, 192.168/16), 100.64.0.0/10 (Tailscale and CGNAT), link-local, IPv6 ULA, benchmark and reserved ranges, and outbound SMTP (ports 25, 465, 587).
- **Allowed:** everything else, the public internet.
- DNS goes to the gateway, which resolves through 1.1.1.1 and 9.9.9.9, not your Mac's resolvers. Blocked attempts are logged to `logs/netgw-blocked.jsonl` (and, with an event sink, sent as `security.blocked_connections`).
- Inbound, there is no route to the VM except two forwards that listen on `127.0.0.1` only: SSH (port 2277 by default) and the live view (5977). The VM's firewall admits ports 22 and 5900 only from the gateway. There is no network bridge, so the VM never appears on your LAN.

**SSH and credentials** (all in `SSH_DIR`, default `~/.ssh/metacomputer`; the
private files are 0600):

| Credential | Grants |
| --- | --- |
| `mcp` | The desktop user `cua`, with a forced command: Cua Driver's MCP server only. No shell, no PTY, no forwarding |
| `ops` | A shell as `cua` (no sudo, no forwarding), for `metacomputer sh`, status, file copies and `inspect` |
| `admin` | The maintenance account `cuaadmin`, which after the build may use sudo only for `/usr/local/sbin/metacomputer-admin` and its fixed verbs |
| `fill` | The locked account `mcfill`, for `metacomputer secrets` fills: sshd forces its one root-owned program, with no TTY and no forwarding |
| `proxy` | The locked account `mcproxy`, for API tokens: no command and no TTY; it may open only one reverse forward, to `127.0.0.1:18765` inside the VM |
| `hostkey` | The VM's own SSH host key, generated on the Mac and pinned in `known_hosts`, so strict checking applies from the first connection |
| `vnc-pass` | The live-view password (or your own store, with `VNC_PASSWORD_STORE=command`) |

The VM's SSH accepts keys only, for `cua`, `cuaadmin`, `mcfill` and `mcproxy`, with
root login and every kind of forwarding off apart from `mcproxy`'s one reverse forward.
All accounts have locked passwords, and no macOS credentials are used anywhere.
After a successful build, the VM's private host key is deleted from the Mac.

On the Mac's side, every connection to the VM (`sh`, `ssh`, the driver,
recordings, status) runs ssh with no config file (`-F /dev/null`), so nothing
in your `~/.ssh/config` can add a forward or a proxy command, and with agent,
X11, port and tunnel forwarding and connection sharing turned off on the
command line. A hostile VM's sshd could ignore its own side's rules; the Mac's
side is what counts.

**The one way the VM can open a connection to your Mac** is the secrets API proxy,
and only while you use it. When the vault is unlocked and Claude asks for a token, the
vault opens that reverse forward, so the proxy appears at `127.0.0.1:18765` inside the
VM (`SECRETS_PROXY_PORT`). It accepts only valid tokens, sends requests only to each
secret's own API host, and stops when the vault locks.

**Your Mac.** Cua Driver isn't installed on it, and nothing in this project
can see or control its screen. The VM isn't started at login, and no
LaunchAgent or daemon is added. The event watcher, when you have a sink, is a
plain process that `start` and `stop` manage.

**Treat with care:**
- Everything the VM shows (pages, documents, window titles, accessibility text, shell output) is untrusted data, never instructions. The skill says so, but no model is immune to prompt injection; the protections above limit what a steered agent could reach.
- The VM can reach the public internet, so anything you put in it could be sent out. Don't copy secrets into it.
- Recordings and screenshots capture the whole screen.
- `ACTION_LOG_TEXT=keep` records typed text, scripts and shell output in full; keep such logs as safe as the passwords that might be in them.
- Event sinks' URLs, tokens and signing keys sit in a file anything running as you can read. Use ingest-only tokens.

## Known limitations

**Platform:**
- Apple-silicon Macs only, with an ARM64 guest.
- One controller per VM at a time (see [Start it and check on it](#start-it-and-check-on-it)). A second VM needs its own settings file.
- The VM can't reach your Mac or LAN, by design. Copy a local web app in as static files to test it.
- No root for the desktop user: system packages are added with `EXTRA_PACKAGES` and a rebuild, which also replaces the baseline.
- Unattended upgrades patch the working VM after each reset. Refresh the baseline now and then with `metacomputer baseline`, or rebuild, so resets start current.

**Cua Driver 0.53.0 on this desktop** (details in [DESKTOP.md](skills/metacomputer/DESKTOP.md) and [BROWSER.md](skills/metacomputer/BROWSER.md)):
- **No Firefox page route.** Page tools (`get_browser_state` and the `browser_*` tools) work only with Chrome, so page and hybrid modes need Chrome.
- **The focused element isn't reported** on Linux. Claude proves focus with a fresh read.
- **Firefox ignores background typing.** Text entry in Firefox uses foreground input, which moves the real pointer and focus.
- **Chrome ignores background input,** and while the driver's virtual input devices exist it ignores the keyboard altogether. The bridge sends input aimed at Chrome in the foreground, refuses `set_value` and `run_script` aimed at it, and removes leftover devices before Chrome needs the keyboard ([docs/chrome-investigation.md](docs/chrome-investigation.md)).
- **Opening modal dialogs through accessibility actions can freeze GTK apps.** Mousepad's Save As through its menu, and Inkscape's dialogs, stopped answering; keyboard shortcuts work.
- **Some content isn't in the accessibility tree:** Thunar's icon view (use List View), Kdenlive's and Audacity's timelines, canvases and SDL games. These need pixel clicks; SDL window screenshots come back black.
- **A driver-launched Chrome closes** after 300 s without driver activity.
- **No compositor.** With compositing on, window shadows shift the driver's pixel mapping by about 20 px, so it stays off.

**The shell channel** ([docs/shell.md](docs/shell.md)):
- Output comes back when the script ends, capped per stream; write big outputs to a file and `get` it.
- `--read-only` is a promise, not a sandbox: it skips the one-controller check and leaves out the desktop's environment, but doesn't stop a script from changing things.
- The sizes and hashes in a result are the VM's own report; a compromised VM could report anything that fits the schema.
- One controller at a time is checked and claimed in one step under the owners lock, for tool calls (in the hub, for every client including `web` and the GUI tests) and shell runs alike. It keeps sessions from clashing; it isn't a barrier against a program running as you, which can use the raw driver connection (`MCOMP_DIRECT_BRIDGE=1`) the transport tests use.
- `VM_SHELL=off` turns off this command, not SSH: `metacomputer ssh ops` still works for the Mac's own plumbing. Deny it in Claude Code's permissions if you want a barrier.

**Bounded mode** (`DRIVER_MODE=bounded`, [SECURITY.md](SECURITY.md)):
- Cua runs page tools and desktop tools in separate runtimes, so one bounded session can't use both, and hybrid browsing needs standard mode.
- Manifests in driver 0.52.1 can't grant the clipboard tools, so they are always refused.
- `kill_app` refused to close apps the driver had launched; close them from their own window or with `metacomputer desktop-restart`.
- The `desktop` profile lists the standard apps only. Apps added with `EXTRA_PACKAGES` need your own `DRIVER_MANIFEST`.
- It limits the driver, not `metacomputer ssh ops` or programs already running in the VM; that's why it turns the shell channel off.

**Secrets** (`metacomputer secrets`; [SECURITY.md](SECURITY.md#what-doesnt-hold) has the full list):
- Fills need page-mode Chrome and a VM built with the locked fill account; API tokens need the proxy account too. Older VMs say so; rebuild them. See [Use saved passwords and 2FA codes](#use-saved-passwords-and-2fa-codes).
- It isn't a wall against a deliberately hostile agent. With a shell in the VM (the default), code can pose as Chrome's DevTools to receive a fill, or read a filled field back. Driving the browser can reveal a field too: a "show password" button, Chrome's developer tools, a `javascript:` address.
- One user and one operator. Everything runs as your macOS user, so who asked, `NAME session` and `--project` are attribution that any program of yours can fake, and the person who unlocks also approves.
- No hardware module holds the data key. The Secure Enclave key takes part only in opening the vault; while it's unlocked, the unlock process holds the data key.
- The API key behind a token is the one you stored, not a credential minted for each use.

**Events** ([docs/events.md](docs/events.md)):
- Best effort and at least once: a receiver that's down long enough can miss events, and a receiver should drop repeats by `id`.
- Against a program running as you, sink changes are detected and announced, not prevented.
- Rate limits hold back bursts (10 per family in 10 minutes; a phone also 30 an hour) and sum them up later.

**Recording:** the sound in whole-desktop recordings is within about 50 ms of
the picture. That's fine for watching; nudge it in an editor if a sound must
land on an exact frame.

[architecture.md](architecture.md) lists the workarounds for Lume itself: its
VNC server isn't loopback-only (so it's disabled), `lume stop` is a hard
power-off (so `stop` shuts down from inside the VM first), and a gateway
serves one VM process (so `start` launches a fresh one).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `metacomputer status` shows a ✗ | Read that line. `metacomputer logs` shows the Lume, gateway and VM logs; `metacomputer restart` stops the VM and starts it again. |
| A Claude Code session has no metacomputer tools | Run `/mcp` and reconnect `plugin:metacomputer:metacomputer` (or `metacomputer` without the plugin), or restart Claude Code. `claude plugin list` should show `metacomputer@metacomputer` enabled (without the plugin, `claude mcp get metacomputer` shows `…/bin/metacomputer mcp-lazy`); `./install.sh --only-claude` sets it up again. |
| The plugin's MCP server stops with "no metacomputer set up yet" | The plugin came from GitHub, and this Mac has no checkout set up: clone the repository and run `./install.sh`, or set `MCOMP_HOME` to a checkout. |
| The tools are listed twice (`mcp__metacomputer__*` and `mcp__plugin_metacomputer_metacomputer__*`) | Both the plugin and an older user-scope server are registered. `./install.sh --only-claude` removes the old one (or `--only-claude --no-plugin` removes the plugin). |
| "The virtual computer is off" | `metacomputer start`, then try again. |
| "Another session is using the virtual computer" | `metacomputer who` shows which. Ask before taking over, then `metacomputer release` (or `--override` on the command). |
| Chrome ignores typing | `metacomputer input-reset`. |
| An app stops answering (`atspi_app_unresponsive`) | It has to be closed (`kill_app`). `metacomputer desktop-restart` logs the desktop out and in, which closes every app. |
| A call is refused as "outside the capability manifest" | Bounded mode is on. Choose another `DRIVER_PROFILE`, or allow it in your own `DRIVER_MANIFEST`. |
| "Cua Driver's bounded session has ended" | Reconnect the MCP server with `/mcp` (`DRIVER_EXPIRES`, `DRIVER_IDLE`). |
| `metacomputer sh` exits 125 | Refused before anything ran: `VM_SHELL=off`, bounded mode with `VM_SHELL=auto`, another session using the VM, or an action log that couldn't be opened. The message says which. |
| `metacomputer sh` exits 255 | It couldn't run or finish: the VM is off, SSH failed, the `--cwd` folder doesn't exist, or the VM's answer was malformed. `--json` gives the `kind`. |
| `metacomputer sh` exits 124 | The script ran past `--timeout` (120 s by default) and was stopped. |
| `status` says the VM was built by an earlier version | Rebuild it: `metacomputer build --replace`. |
| `status` says REBOOT REQUIRED | Security updates are waiting for a reboot: `metacomputer restart`. |
| The installer stops | Fix what it names and run `./install.sh` again; finished steps are skipped. A failed build leaves its log in `logs/build-*.log`. If provisioning failed inside the VM, it is left running so you can look: `metacomputer ssh admin`. |
| mcomp-vm won't build | It needs Apple's Command Line Tools (`xcode-select --install`). Or use `./install.sh --lume=standard`. |
| `metacomputer: command not found` | Add `~/.local/bin` to your `PATH`, or use the full path. |
| `record start` refuses `RECORD_DIR` | Its parent folder doesn't exist, usually a drive that isn't connected. |
| `metacomputer secrets` says the vault is locked | Run `metacomputer secrets unlock` in your own terminal, and keep it open. |
| `secrets set` says a secret with that name exists | Add `--replace` to rotate its value, or delete it first. |
| Fills and tokens are refused because a required audit sink can't be reached | `metacomputer secrets audit-sink list` shows its last error. Fix the receiver, or remove the sink in your own terminal. |
| The unlock terminal says `AUDIT LOG CHANGED` | A record in `audit.jsonl` failed the vault's checks. `metacomputer secrets audit --verify` names it; the sinks got an `audit_tamper` notice in its place. |
| A program in the VM gets 401 from the proxy | The token expired, was revoked, or its secret changed: ask for a new one. |
| `secrets token` or `fill` says to rebuild the VM | The VM predates the proxy or fill account: `metacomputer build --replace`, then `metacomputer secrets bind` again. |
| `notify` says nobody was pinged | No event sink is set up. Add one in your own terminal (`metacomputer events sink add`). |
| Events aren't arriving | `metacomputer events status` shows failing sinks and backlogs; `events sink test NAME` tries one now. The watcher retries backlogs every 30 s once their backoff has passed. |
| `events sink add` refuses | It needs your own terminal (not an agent's shell, a script or a pipe). |

## Testing

Most tests run against your own VM, so start it first. Results and recordings
go to `results/`, `recordings/` and `screenshots/` (all ignored by git).
[TESTING.md](TESTING.md) has the details.

| Test | Needs | What it covers |
| --- | --- | --- |
| `metacomputer test [A-F]` | the VM | Six recorded GUI tests: desktop observation, Calculator, Firefox, LibreOffice Writer, several apps together, Chrome through its page tools |
| `python3 tests/test_chrome_guard.py` | the VM | The bridge's Chrome input guard |
| `python3 tests/test_recording.py` | nothing (`--vm` for a test VM) | Recording description files and key moments; both recording modes and sound |
| `python3 tests/test_actionlog.py` | nothing | Tamper detection in the action log; the relay passes messages through unchanged |
| `python3 tests/test_vm_shell.py`, `tests/test_ssh_options.py` | nothing | The shell channel against a fake, hostile VM that floods, stalls and sends malformed answers; the SSH arguments every connection to the VM uses (no config file, no forwarding) |
| `python3 tests/test_events.py` | nothing | The event stream: cleaning, sinks, delivery, limits, sink-change detection |
| `python3 tests/test_secrets.py`, `tests/test_vault_ops.py`, `tests/test_secret_fill.py` | nothing | The secrets vault: fills, keys and rotation, approvals and requesters, API tokens and the proxy, audit sinks and tamper checks, and the VM's fill program |
| `MCOMP_CONFIG=test-vm.env python3 tests/test_options.py` | a separate test VM | The action log, SSH certificates, bounded mode, events (E) and the shell channel (F); it switches that VM's settings |
| `python3 tests/test_learn.py` | nothing | `metacomputer learn`: mining synthetic sessions in both formats and both tool prefixes, injected text that must never reach a proposal, a lesson or the instructions, the approval rules, the store's permissions, events, and the benchmark's lessons switch |
| `python3 tests/test_plugin.py` | nothing (`--real-claude`: the `claude` CLI, in a temporary config) | The Claude Code plugin: its manifests and layout, `claude plugin validate`, the plugin's copy of `bin/metacomputer` handing over to the checkout, `install.sh`'s plugin mode, and both tool prefixes |
| `tests/skill/run_headless.sh` | the VM, API usage | The skill end to end in a headless Claude Code session |

### The benchmark

[bench/](bench/README.md) is a fixed set of 20 desktop tasks across Calculator,
Mousepad, Writer, Calc, Thunar, Firefox, Chrome and the shell. Each runs in a
fresh headless Claude Code session on a test VM, and a check reads the VM's
real state afterwards (saved files, what the browser really sent to a local
server, app settings) rather than trusting what the agent says. It reports
pass rate, a partial score, cost, turns, time and the mix of tool calls, so
you can change one thing (the skill, the shell channel, the model, the screen
size) and see what moves. The method follows Microsoft Research's Hybrid
Computer Use study.

```bash
export MCOMP_CONFIG=/path/to/test-vm.env   # a test VM: each task resets it to its baseline
bench/bench.py run --run-id baseline       # all 20 tasks
bench/bench.py compare baseline shell-forbid-1
```

It refuses to run without `MCOMP_CONFIG`, and refuses the VM named
`metacomputer` unless you pass `--allow-main-vm`. The first baseline
(2026-11-17, Opus 5.5, Cua Driver 0.52.1, the default skill) passed all 20
tasks for $11.95 in all, $0.20 to $1.31 a task; a full run takes about 35
minutes. Tasks are capped at $3 each and a run at $60 by default.

## Uninstall

```bash
metacomputer stop
./install.sh --uninstall      # removes the command, the alias, the plugin (or the skill and the MCP registration) and the vault program
```

Your secrets vault stays in `~/Library/Application Support/metacomputer/`, and
event sinks in `~/.config/metacomputer/`. Delete those folders only if you no
longer need anything in them.

The VM, its keys and Lume stay in place too. To remove them (with standard
Lume, use `lume` in place of `mcomp-vm`, and remove `~/.local/share/lume`
and `~/.local/bin/lume` instead; `./install.sh --uninstall` prints these for
the runner you use):

```bash
mcomp-vm ls                                  # also shows checkpoints (metacomputer-ckpt-*) and metacomputer-baseline-prev
mcomp-vm delete metacomputer
mcomp-vm delete metacomputer-baseline
mcomp-vm delete metacomputer-baseline-prev  # if ls showed it
mcomp-vm delete metacomputer-ckpt-NAME      # each checkpoint ls showed
rm -r ~/.ssh/metacomputer
rm -rf ~/.local/share/mcomp-vm ~/.local/bin/mcomp-vm         # last: the VMs above need it to be deleted
rm -rf ~/.local/share/lume-secure ~/.local/bin/lume-secure   # if an install from before the rename left it
```

Recordings and results stay in the checkout until you delete it.

## Documentation

- [SECURITY.md](SECURITY.md): every control, what it protects against, where it ends, the secrets vault in full, and how to report a vulnerability.
- [architecture.md](architecture.md): the design, components and pinned versions, provisioning, the MCP transport, network, recording, the event spool, and Lume's limits.
- [TESTING.md](TESTING.md): every test and how to run it.
- [docs/shell.md](docs/shell.md): `metacomputer sh`, its design and how it treats the VM.
- [docs/events.md](docs/events.md): the event stream, every event, wire formats and receivers.
- [docs/learn.md](docs/learn.md): `metacomputer learn`, lessons from your own sessions: what it reads and reports, proposals, approval, delivery and measuring.
- [docs/moving-from-spaces.md](docs/moving-from-spaces.md): coming from Cua Spaces' Keyvault: the command and event mapping, the sign-in flow, what differs, and how to bring your logins over.
- [bench/README.md](bench/README.md): the benchmark.
- [skills/metacomputer/](skills/metacomputer/): the skill itself. [DESKTOP.md](skills/metacomputer/DESKTOP.md) and [BROWSER.md](skills/metacomputer/BROWSER.md) hold the verified desktop and browser rules; [SHELL.md](skills/metacomputer/SHELL.md), [SECRETS.md](skills/metacomputer/SECRETS.md) and [EVENTS.md](skills/metacomputer/EVENTS.md) what Claude does with each.
- [docs/chrome-investigation.md](docs/chrome-investigation.md): how Chrome was added and why the input guard exists.
- `.claude/skills/cua-driver/`: Cua Driver's own skill pack, trimmed to its Linux guide.
- `.claude-plugin/marketplace.json` and [plugin/](plugin/): the Claude Code plugin, made of links to the skill, the driver pack and `bin/metacomputer` (the same layout as the repository, so their relative links work in both).

## Contributing

Issues and pull requests are welcome. Before you change code:
- Read [architecture.md](architecture.md) and [CLAUDE.md](CLAUDE.md), which describes the repository's conventions.
- Settings belong in `config/defaults.env` and `config/local.env`. Don't hard-code a machine's names, paths or hosts.
- The optional hardening stays off by default, so metacomputer behaves like Cua Driver out of the box.
- Run the tests in [TESTING.md](TESTING.md) that cover your change, and `scripts/check-public.sh -a`, which checks the tree and history for anything personal. For a change to how Claude works the desktop, the benchmark shows whether it helps.

Report security problems privately, as [SECURITY.md](SECURITY.md#reporting-a-vulnerability)
describes. Bugs in Cua Driver or Lume themselves belong upstream at
[trycua/cua](https://github.com/trycua/cua).

## Acknowledgements

metacomputer is built on [Cua](https://github.com/trycua/cua) by Cua AI, Inc.
(MIT): Cua Driver runs the desktop, Lume runs the VM, and `cua-netgw`, built
from Cua's source, is the network gateway (it uses gvisor-tap-vsock,
Apache-2.0). [mcomp-vm](https://github.com/metavert-io/metacomputer-vm),
metacomputer's build of Lume, adds the host-sharing flags; it's made by
Metavert, not by Cua, and isn't endorsed by them. The shell channel's routing
and the benchmark's method come from Microsoft Research's
[Hybrid Computer Use](https://github.com/microsoft/Hybrid-Computer-Use) work.
`tools/qcow2raw` uses Lima's
[go-qcow2reader](https://github.com/lima-vm/go-qcow2reader) (Apache-2.0). The
VM runs Ubuntu, Firefox, LibreOffice and, optionally, Google Chrome.
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists them with their
licenses.

## License

MIT License, © 2026 Metavert LLC. See [LICENSE](LICENSE).
