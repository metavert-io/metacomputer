# metacomputer

A computer of its own for Claude Code. metacomputer runs a disposable Ubuntu
desktop as a virtual machine on your Mac. Claude Code operates it the way a
person would: it reads the screen through the accessibility tree, clicks and
types, uses real browsers, and records video of what it did. The VM is kept
away from your files, your accounts, your Mac and your local network.

You ask for it in plain words, in any Claude Code session:

> Get yourself a virtual computer and tell me what's on its screen.
>
> Check whether this page looks the same in Firefox and Chrome.
>
> Someone sent me this link. What does it do, and is it safe to open?

**Contents:**
[What it is](#what-it-is) ·
[Why I made it](#why-i-made-it) ·
[Requirements](#requirements) ·
[Install](#install) ·
[Quick start](#quick-start) ·
[How-to guide](#how-to-guide-what-to-ask-claude-code) ·
[Commands](#commands) ·
[Configuration](#configuration) ·
[Isolation and safety](#isolation-and-safety) ·
[Known limitations](#known-limitations) ·
[Troubleshooting](#troubleshooting) ·
[Testing](#testing) ·
[Uninstall](#uninstall) ·
[Contributing](#contributing) ·
[Credits](#acknowledgements) ·
[License](#license)

## What it is

`install.sh` sets up four parts:

| Part | What it is |
| --- | --- |
| The virtual computer | An Ubuntu 24.04 ARM64 desktop (XFCE on Xorg) in a [Lume](https://github.com/trycua/cua/tree/main/libs/lume) VM on Apple's Virtualization framework. Firefox, LibreOffice Writer, GNOME Calculator, the Thunar file manager, the Mousepad text editor and a terminal come installed. Google Chrome is optional. |
| Cua Driver, inside the VM | [Cua Driver](https://github.com/trycua/cua) 0.52.1, an MCP server for the desktop: accessibility trees, mouse and keyboard input, screenshots, recordings, and page-level tools for Chrome. It runs only in the VM and is never installed on your Mac. |
| The `metacomputer` skill and MCP server | The skill ([skills/metacomputer/](skills/metacomputer/): SKILL.md, DESKTOP.md, BROWSER.md) tells Claude Code when a task belongs on the virtual computer and how to work it. The MCP server, `metacomputer mcp-lazy`, is registered for every project, so the tools (`mcp__metacomputer__*`) are listed even while the VM is off. |
| The `metacomputer` command | `bin/metacomputer`, installed as `~/.local/bin/metacomputer` with the short alias `mcomp`. It starts and stops the VM, checks it, moves files, records, runs browser checks, keeps snapshots and opens the live view. |

```text
Your Mac
├── Claude Code + the metacomputer skill
│     └── MCP server (metacomputer mcp-lazy) ──SSH to 127.0.0.1, forced command──┐
├── metacomputer command                                                         │
├── network gateway (cua-netgw): the VM reaches the public internet only        │
└── Lume ── VM: Ubuntu 24.04 ARM64, XFCE                                         │
             └── Cua Driver MCP server ◀─────────────────────────────────────────┘
                 Firefox, Chrome, LibreOffice, Calculator, Thunar, Mousepad
```

What it's good for:
- **Real GUI work:** desktop apps, forms, multi-step flows in a real browser.
- **Visual checks of a web app:** two browsers side by side, layouts at several widths, difference images against an earlier run, an accessibility smoke check.
- **Recorded demos and evidence:** an MP4 of a run, a contact sheet, screenshots and per-action traces.
- **Untrusted links and files,** opened in a throwaway browser profile, away from your Mac.

Code, files, APIs and pages a plain fetch can read stay with Claude Code's
normal tools. The skill doesn't boot a VM for those.

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
- a live view, recordings, and a way to get files in and out;
- instructions that tell the model when to use the VM, how to drive this particular desktop reliably, and how to check its own work.

metacomputer turns that into `./install.sh` and a skill. The installer builds
the VM from pinned, verified sources and registers everything with Claude
Code. The skill gives Claude Code what it needs from its first session:
- **Which computer.** "Use a computer" means the VM. "My screen" or "my Chrome" means yours, and the VM is never used in its place.
- **A routine.** Check who else is using it, start it, keep a task folder, act from the accessibility tree rather than from screenshots, verify the outcome, and hand back the evidence.
- **Rules verified on this desktop.** For example: which inputs Firefox ignores, which dialogs freeze which apps, and how to verify a result from structure.

### Security: kept away from your Mac, your network and your accounts

An agent that uses a computer reads whatever is on the screen, and some of it
is written by people who want to steer the agent. No model is immune to that.
So the virtual computer should hold none of your data and reach none of your
machines, and every protection should be something you can check.

Compared with running Lume and Cua Driver as they come, metacomputer sets up
the following. These are **always on**:

| Protection | What it does |
| --- | --- |
| **lume-secure** (the installer's default) | Lume attaches your Mac's clipboard, its speakers and microphone, and Rosetta to every VM. [lume-secure](https://github.com/metavert-io/lume-secure) is Lume 0.7.2 plus `--no-share-clipboard`, `--no-share-audio`, `--no-share-rosetta` and `--no-host-sharing`, and metacomputer starts the VM without all three unless you turn one on. `metacomputer status` reports what the VM actually has, read from inside it. |
| **Public internet only** | The VM's network is a user-mode gateway on the Mac (Cua's `cua-netgw`) that checks every connection the VM opens. It refuses your Mac, your LAN and router, Tailscale and CGNAT addresses, link-local and cloud-metadata addresses, and outbound mail ports. Blocked attempts are logged. |
| **Nothing listens off loopback** | SSH and the live view are forwarded only to `127.0.0.1` on your Mac. Lume's own VNC server, which listens on every interface, is never started. The live view is a password-protected VNC server inside the VM, and the VM's firewall admits it only from the gateway. |
| **SSH by role** | Keys only, with the VM's host key generated on your Mac and pinned before the first boot. Three keys for three roles: `mcp` can only start Cua Driver's MCP server (a forced command, no shell), `ops` is a shell for the desktop user, `admin` is for maintenance. No forwarding of any kind. |
| **Narrow accounts** | The desktop user has no sudo. After the build, the maintenance account can run only one root script with a fixed list of verbs, cloud-init is turned off, and its cached user-data (which held the host key) is deleted. Both accounts have locked passwords. |
| **Verified inputs** | The Ubuntu image (signed `SHA256SUMS`, pinned signing key and image digest), the Cua Driver release (pinned SHA-256, cross-checked against the release's checksums and checked again inside the VM), Lume (pinned checksum, or lume-secure's pinned tag and commit), and the Mozilla and Google repository keys (pinned fingerprints). The VM never downloads Cua components itself. |
| **Less in the guest** | No snaps. No clipboard agent (`spice-vdagent` is removed) and the virtio sound driver is blacklisted, a second layer for when standard Lume attaches those devices. No shared folders. Cua Driver's telemetry and update checks are off, and so is Lume's telemetry. Browser policies turn off telemetry and password saving in both browsers, and sign-in and sync in Chrome. Security updates install automatically, without automatic reboots. |
| **Page text is data** | The skill and the MCP server's own instructions tell Claude that pages, documents, window titles and accessibility text are data, never instructions. Claude never types your passwords; it asks you to sign in through the live view. `metacomputer web open-untrusted` opens a link in a throwaway Chrome profile without clicking anything on it. |
| **Your Mac stays yours** | Nothing here can see or control your Mac's screen. The VM's wallpaper and menu say VIRTUAL COMPUTER, so every screenshot and recording shows which machine it is. |

Three more protections are **opt-in**. They're off by default, so out of the
box metacomputer behaves the way Cua Driver does on its own. Turn on any of
them in `config/local.env`; `metacomputer security` shows what's on.

| Setting | What it adds |
| --- | --- |
| `ACTION_LOG=on` | A signed, hash-chained log of every tool call sent to the VM. Each record carries the hash of the one before it, and the chain's head is signed with an SSH key (`ssh-keygen -Y sign`) every 20 records, every minute with new activity, and at the end. By default, typed text is recorded as its length only. `metacomputer actions verify` finds edited, inserted, reordered or removed records. |
| `SSH_AUTH=cert` | A fresh key and a 5-minute SSH certificate for every connection, limited to one role and to the Mac's side of the VM network. Its key ID (`mcomp:ROLE:USER:PROJECT:pidN`) appears in the VM's auth log. The static `ops` and `mcp` keys stop working. |
| `DRIVER_MODE=bounded` | Cua Driver's bounded mode: every call must fit a capability manifest of tools, apps, folders and websites, with a time limit. Profiles `readonly`, `desktop` (no terminal) and `browser` (only the sites you list), or your own manifest. The Mac asks for the manifest by its SHA-256, and the VM refuses a manifest that doesn't match and, while bounded, any standard session. |

None of this protects against someone who already controls your Mac account:
they can change the config, read the keys and drive the VM directly.
[SECURITY.md](SECURITY.md) explains each opt-in protection and where it ends,
and [Isolation and safety](#isolation-and-safety) has the specifics of the
rest.

## Requirements

- A Mac with Apple silicon and macOS 14 or later.
- About 15 GB of free disk for the VM and its clean snapshot (the installer warns below 20 GB). The build downloads about 600 MB.
- Memory for the VM: it gets 4 vCPUs and 8 GB by default, or 4 GB on a Mac with less than 16 GB.
- [Homebrew](https://brew.sh), to install any missing tools: git, curl, Go, GnuPG, ffmpeg and Python 3.
- Apple's Command Line Tools (`xcode-select --install`), to build lume-secure. Without them, use `--lume=standard`.
- [Claude Code](https://claude.com/claude-code), for the skill and the MCP server.
- Optional: Google Chrome in the VM. Installing it means accepting [Google's terms](https://www.google.com/chrome/terms/).

## Install

```bash
git clone https://github.com/metavert-io/metacomputer.git
cd metacomputer
./install.sh
```

The installer asks at most four questions:
1. whether to install missing tools with Homebrew;
2. which Lume runs the VM: lume-secure (recommended; built from source in about a minute) or Cua's standard release, which always shares your clipboard, audio and Rosetta with the VM;
3. whether to put Google Chrome in the VM next to Firefox;
4. whether to build the VM now.

| Option | Effect |
| --- | --- |
| `--yes` | Take the defaults: lume-secure, Chrome included, build now |
| `--no-chrome` | Firefox only in the VM |
| `--lume=secure` / `--lume=standard` | Choose the Lume without being asked |
| `--no-build` | Set everything up but don't build the VM yet |
| `--no-claude` | Don't touch Claude Code (no command links, skill or MCP server) |
| `--uninstall` | Remove the command, the skill and the MCP registration (the VM stays) |

Everything else is automatic. Each step is skipped when it's already done, so
running `./install.sh` again is safe.
- **Lume**, at a pinned and verified version: `~/.local/bin/lume-secure`, or `~/.local/bin/lume` for the standard release. Telemetry off, no background service.
- **This Mac's settings** in `config/local.env`, never committed.
- **The command** `~/.local/bin/metacomputer` and its alias `mcomp`.
- **The skill**, linked at `~/.claude/skills/metacomputer`.
- **The MCP server** `metacomputer`, registered at user scope (`claude mcp add -s user metacomputer …/bin/metacomputer mcp-lazy`).

The build then runs unattended for about 10 minutes, sometimes longer. It
downloads Ubuntu's cloud image and the Cua Driver release, checks their
signatures and checksums, provisions the desktop, and takes a clean snapshot
(the baseline). Its log goes to `logs/build-*.log`.

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
acted in the last 10 minutes, a second session's actions are refused (reads
still work), and `stop`, `reset`, `restore`, `baseline` and `test` refuse
unless given `--override`. Claude asks you before taking over;
`metacomputer release` hands the VM over.

### Use desktop apps

- "Use the calculator on your computer to work out 137 × 29."
- "On the virtual computer, write a short note in the text editor, save it as notes.txt and give me the file."
- "Open LibreOffice Writer, write a one-page memo with a Heading 1 title, save it as memo.odt and copy it to my Mac."
- "Open the file manager on your computer and tell me what's in Downloads."
- "Copy the first paragraph of this page in Firefox into a Writer document and save it."
- "Open a terminal on the virtual computer and tell me which Python version it has."

Claude works from the accessibility tree: it finds controls by role and name,
batches the steps it has decided on, and verifies the outcome (a value in the
app, a file on disk) rather than trusting a screenshot. It takes a screenshot
only when the tree can't answer, such as for canvases, images or layout.

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

The VM holds none of your accounts, and Claude never types your credentials.
It opens the window and asks you to sign in yourself through the live view
(`metacomputer view`). Later sessions with the same profile name reuse its
cookies until the VM is reset.

```bash
metacomputer browser profiles                    # named Chrome profiles in the VM
metacomputer browser profiles --delete shopping  # asks first
```

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
metacomputer ssh ops 'cd ~/Inbox/dist && nohup python3 -m http.server 8000 >/dev/null 2>&1 &'
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
untrusted.

### Record a task

- "Record yourself filling in this form in Chrome."
- "Use a disposable computer to write this memo in LibreOffice, and record the whole desktop while you do it."

There are two recorders:
- **Per task, through Cua Driver.** Claude makes a task folder and calls `start_recording`. It produces a 1920×1080, 30 fps MP4 and a folder per action with the state, screenshots and click markers before and after. `metacomputer task finish` copies it all to the Mac and makes a contact sheet.
- **The whole desktop, whoever drives it.** `metacomputer record start NAME` and `record stop` capture the screen with ffmpeg, at 20 fps by default. A description file (`NAME.json`) beside the MP4 lists its times, settings, length, size, sound track and SHA-256. With `ACTION_LOG=on` it also lists the key moments: every tool call made on this VM while it recorded, with its time in the video (for example `0:42 launch_app inkscape`).

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
an external drive.

Recordings capture the whole screen. Claude never types your passwords, and
with the driver's recorder it passes any other sensitive value it types to
`redact_values`, or leaves those steps out of the recording.

### Move files in and out

| Ask | Or run |
| --- | --- |
| "Here's data.csv. Open it on your computer." | `metacomputer put data.csv` (to `~/Inbox` in the VM; `--to '~/Documents'` for elsewhere) |
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
- `reset`, `restore` and `baseline` ask first (`--yes` skips the question). Claude never resets, restores, rebuilds or re-baselines the VM without your explicit OK.

### Add apps

The desktop user has no sudo, so system packages can't be installed from
inside the VM. Add them to `EXTRA_PACKAGES` in `config/local.env` instead.
They come from Ubuntu's archive, without "recommended" extras, and take
effect on the next build.

| App | `EXTRA_PACKAGES` |
| --- | --- |
| Inkscape (vector drawing) | `inkscape` |
| LibreOffice Calc (spreadsheets) | `libreoffice-calc` |
| Audacity (audio editing) | `audacity` |
| Kdenlive (video editing) | `kdenlive frei0r-plugins mediainfo` |
| Ristretto (image viewer) | `ristretto` |

```bash
# config/local.env
EXTRA_PACKAGES="inkscape libreoffice-calc audacity"
```

```bash
metacomputer build --replace     # rebuilds the VM AND its baseline; everything in the VM is lost
```

Then:
- "Draw a simple logo in Inkscape and export it as a PNG."
- "Make a bar chart from this CSV in LibreOffice Calc and save the spreadsheet."
- "Trim the silence from the start of this recording in Audacity."

[DESKTOP.md](skills/metacomputer/DESKTOP.md) has rules for these apps,
verified in a VM built this way. Some of their dialogs, canvases and
timelines need real pointer clicks instead of accessibility actions.

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
MCOMP_CONFIG=~/second.env metacomputer status
```

The `metacomputer` MCP server that `install.sh` registers drives the VM named
in `config/local.env`.

### Turn on the optional hardening

- "Which hardening options are on for your computer?"
- "Show me exactly what you sent to the virtual computer in the last session."
- "Check that the action logs haven't been changed."

```bash
metacomputer security                 # what's on, in the config and in the VM; --json
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
| `start` / `stop [--force]` / `restart` | Boot (about 12 s); shut down from inside the VM, forced after 90 s |
| `status [--json]` | Every layer: VM, gateway, SSH, desktop, accessibility, driver, live view, host sharing, MCP handshake. Exit 0 = ready |
| `who` / `release` | Who is using the VM; hand it over (one controller at a time) |
| `reset [--yes]` | Restore the clean baseline (asks first; evidence on the Mac is kept) |
| `checkpoint NAME [--replace]` / `checkpoint --delete NAME` / `checkpoints` / `restore NAME` | Saved states of the stopped VM |
| `baseline [--yes]` | Make the current VM the new baseline (the old one is kept as `-prev`) |
| `build [--replace]` | Build the VM and its baseline from scratch |
| `inspect [SECTION...] [--minutes N] [--top N] [--json]` | Read-only snapshot of the VM: `system`, `windows`, `processes`, `ports`, `files`, `browser` |
| `task start SLUG` / `task finish NAME` | One folder per task: video, contact sheet, screenshots, files, `artifacts.json` |
| `put HOST_PATH... [--to GUEST_DIR]` | Copy files into the VM (default `~/Inbox`) |
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
| `security [apply] [--json]` | The optional hardening, as configured and as the VM has it |
| `actions list\|show\|verify` | The signed action log (`ACTION_LOG=on`) |
| `test [A-F]` | The GUI smoke suite, each test recorded |
| `call TOOL ['JSON']` | One Cua Driver tool call, on a new connection |
| `ssh [ops\|admin] [CMD...]` | A shell in the VM (`ops` is the desktop user) |
| `logs [N]` | The last lines of the Lume, gateway and VM logs |
| `where` | Project, config, skill, docs and results folders; VM names |
| `mcp` / `mcp-lazy` | The direct MCP bridge (tests, scripts) and the on-demand one Claude Code uses |

`stop`, `reset`, `restore`, `baseline`, `test` and `desktop-restart` refuse
while another session is using the VM; `--override` proceeds anyway.

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
| `EXTRA_PACKAGES` | (none) | More Ubuntu packages, e.g. `inkscape libreoffice-calc` |
| `VIRTUAL_AUDIO` | `off` | `on`: a sound card inside the VM that plays into nothing |

**Lume and host sharing:**

| Key | Default | |
| --- | --- | --- |
| `LUME` | `~/.local/bin/lume-secure` | The Lume that runs the VM (`install.sh --lume=standard` for Cua's release) |
| `SHARE_CLIPBOARD`, `SHARE_AUDIO`, `SHARE_ROSETTA` | `off` | `on` connects the VM to your Mac's clipboard, audio or Rosetta (lume-secure only; standard Lume always shares) |

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
| `ACTION_LOG` | `off` | `on`: signed log of every tool call, in `logs/actions/` |
| `ACTION_LOG_TEXT` | `redact` | `keep`: record typed text, values and scripts in full |
| `ACTION_LOG_KEY` | `$SSH_DIR/action-log` | The signing key, or a `.pub` whose private key is in ssh-agent (for example hardware-backed) |
| `SSH_AUTH` | `key` | `cert`: a fresh key and a short-lived certificate per connection |
| `SSH_CERT_TTL`, `SSH_CA_KEY` | `5m`, `$SSH_DIR/ca` | Certificate lifetime; the CA key, or a `.pub` in ssh-agent |
| `DRIVER_MODE` | `standard` | `bounded`: Cua Driver does only what a manifest lists |
| `DRIVER_PROFILE` | `desktop` | `readonly`, `desktop` or `browser` ([config/driver-profiles/](config/driver-profiles/)) |
| `DRIVER_ORIGINS` | (none) | The sites the `browser` profile may open, e.g. `"https://example.com https://app.example.com"` |
| `DRIVER_MANIFEST` | (none) | Your own Cua capability manifest instead of a profile |
| `DRIVER_EXPIRES`, `DRIVER_IDLE` | `8h`, `30m` | A bounded session's lifetime (at most 24h) and idle limit |

## Isolation and safety

**Host sharing.** Lume attaches a SPICE clipboard port (your Mac's clipboard),
a virtio sound device (its speakers and microphone) and a Rosetta share to
every VM.
- With lume-secure, `metacomputer start` leaves out each one whose `SHARE_*` setting isn't `on`.
- With standard Lume all three are always attached. The VM has no `spice-vdagent` and `virtio_snd` is blacklisted, but a process with root in the VM could start an agent and read or set your clipboard. Don't keep secrets on the clipboard while it runs untrusted tasks.
- `metacomputer status` shows what the VM actually has, read from inside it (virtio console ports, a sound device, a Rosetta tag). With lume-secure and the defaults it reads `clipboard off  audio off  rosetta off`.

**Network.** The VM's network device talks to `cua-netgw`, a user-mode gateway
on your Mac built from Cua's source at a pinned commit. Every connection the
VM opens is checked on the Mac, not inside the VM:
- **Always refused:** your Mac's loopback and every address of its own interfaces (LAN and tailnet), the gateway itself, cloud metadata, multicast and broadcast.
- **Refused by [config/netgw-rules.json](config/netgw-rules.json):** private LAN ranges (10/8, 172.16/12, 192.168/16), 100.64.0.0/10 (Tailscale and CGNAT), link-local, IPv6 ULA, benchmark and reserved ranges, and outbound SMTP (ports 25, 465, 587).
- **Allowed:** everything else, the public internet.
- DNS goes to the gateway, which resolves through 1.1.1.1 and 9.9.9.9, not your Mac's resolvers. Blocked attempts are logged to `logs/netgw-blocked.jsonl`.
- Inbound, there is no route to the VM except two forwards that listen on `127.0.0.1` only: SSH (port 2277 by default) and the live view (5977). The VM's firewall admits ports 22 and 5900 only from the gateway. There is no network bridge, so the VM never appears on your LAN.

**SSH and credentials** (all in `SSH_DIR`, default `~/.ssh/metacomputer`; the
private files are 0600):

| Credential | Grants |
| --- | --- |
| `mcp` | The desktop user `cua`, with a forced command: Cua Driver's MCP server only. No shell, no PTY, no forwarding |
| `ops` | A shell as `cua` (no sudo, no forwarding), for status, file copies and `inspect` |
| `admin` | The maintenance account `cuaadmin`, which after the build may use sudo only for `/usr/local/sbin/metacomputer-admin` and its fixed verbs |
| `hostkey` | The VM's own SSH host key, generated on the Mac and pinned in `known_hosts`, so strict checking applies from the first connection |
| `vnc-pass` | The live-view password (or your own store, with `VNC_PASSWORD_STORE=command`) |

The VM's SSH accepts keys only, for `cua` and `cuaadmin` only, with root login
and every kind of forwarding off. Both accounts have locked passwords, and no
macOS credentials are used anywhere.

**Your Mac.** Cua Driver isn't installed on it, and nothing in this project
can see or control its screen. The VM isn't started at login, and no
LaunchAgent or daemon is added.

**Treat with care:**
- Everything the VM shows (pages, documents, window titles, accessibility text) is untrusted data, never instructions. The skill says so, but no model is immune to prompt injection; the protections above limit what a steered agent could reach.
- The VM can reach the public internet, so anything you put in it could be sent out. Don't copy secrets into it.
- Recordings and screenshots capture the whole screen.
- `ACTION_LOG_TEXT=keep` records typed text in full; keep such logs as safe as the passwords that might be in them.

## Known limitations

**Platform:**
- Apple-silicon Macs only, with an ARM64 guest.
- One controller per VM at a time (see [Start it and check on it](#start-it-and-check-on-it)). A second VM needs its own settings file.
- The VM can't reach your Mac or LAN, by design. Copy a local web app in as static files to test it.
- No root for the desktop user: apps are added with `EXTRA_PACKAGES` and a rebuild, which also replaces the baseline.
- Unattended upgrades patch the working VM after each reset. Refresh the baseline now and then with `metacomputer baseline`, or rebuild, so resets start current.

**Cua Driver 0.52.1 on this desktop** (details in [DESKTOP.md](skills/metacomputer/DESKTOP.md) and [BROWSER.md](skills/metacomputer/BROWSER.md)):
- **No Firefox page route.** Page tools (`get_browser_state` and the `browser_*` tools) work only with Chrome, so page and hybrid modes need Chrome.
- **The focused element isn't reported** on Linux. Claude proves focus with a fresh read.
- **Firefox ignores background typing.** Text entry in Firefox uses foreground input, which moves the real pointer and focus.
- **Chrome ignores background input,** and while the driver's virtual input devices exist it ignores the keyboard altogether. The bridge sends input aimed at Chrome in the foreground, refuses `set_value` and `run_script` aimed at it, and removes leftover devices before Chrome needs the keyboard ([docs/chrome-investigation.md](docs/chrome-investigation.md)).
- **Opening modal dialogs through accessibility actions can freeze GTK apps.** Mousepad's Save As through its menu, and Inkscape's dialogs, stopped answering; keyboard shortcuts work.
- **Some content isn't in the accessibility tree:** Thunar's icon view (use List View), Kdenlive's and Audacity's timelines, canvases and SDL games. These need pixel clicks; SDL window screenshots come back black.
- **A driver-launched Chrome closes** after 300 s without driver activity.
- **No compositor.** With compositing on, window shadows shift the driver's pixel mapping by about 20 px, so it stays off.

**Bounded mode** (`DRIVER_MODE=bounded`, [SECURITY.md](SECURITY.md)):
- Cua runs page tools and desktop tools in separate runtimes, so one bounded session can't use both, and hybrid browsing needs standard mode.
- Manifests in driver 0.52.1 can't grant the clipboard tools, so they are always refused.
- `kill_app` refused to close apps the driver had launched; close them from their own window or with `metacomputer desktop-restart`.
- The `desktop` profile lists the standard apps only. Apps added with `EXTRA_PACKAGES` need your own `DRIVER_MANIFEST`.

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
| A Claude Code session has no `mcp__metacomputer__*` tools | Run `/mcp` and reconnect `metacomputer`, or restart Claude Code. `claude mcp get metacomputer` should show `…/bin/metacomputer mcp-lazy`; `./install.sh --no-build` registers it again. |
| "The virtual computer is off" | `metacomputer start`, then try again. |
| "Another session is using the virtual computer" | `metacomputer who` shows which. Ask before taking over, then `metacomputer release` (or `--override` on the command). |
| Chrome ignores typing | `metacomputer input-reset`. |
| An app stops answering (`atspi_app_unresponsive`) | It has to be closed (`kill_app`). `metacomputer desktop-restart` logs the desktop out and in, which closes every app. |
| A call is refused as "outside the capability manifest" | Bounded mode is on. Choose another `DRIVER_PROFILE`, or allow it in your own `DRIVER_MANIFEST`. |
| "Cua Driver's bounded session has ended" | Reconnect the MCP server with `/mcp` (`DRIVER_EXPIRES`, `DRIVER_IDLE`). |
| `status` says the VM was built by an earlier version | Rebuild it: `metacomputer build --replace`. |
| `status` says REBOOT REQUIRED | Security updates are waiting for a reboot: `metacomputer restart`. |
| The installer stops | Fix what it names and run `./install.sh` again; finished steps are skipped. A failed build leaves its log in `logs/build-*.log`. If provisioning failed inside the VM, it is left running so you can look: `metacomputer ssh admin`. |
| lume-secure won't build | It needs Apple's Command Line Tools (`xcode-select --install`). Or use `./install.sh --lume=standard`. |
| `metacomputer: command not found` | Add `~/.local/bin` to your `PATH`, or use the full path. |
| `record start` refuses `RECORD_DIR` | Its parent folder doesn't exist, usually a drive that isn't connected. |

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
| `MCOMP_CONFIG=test-vm.env python3 tests/test_options.py` | a separate test VM | The action log, SSH certificates and bounded mode; it switches that VM's SSH and driver settings |
| `tests/skill/run_headless.sh` | the VM, API usage | The skill end to end in a headless Claude Code session |

## Uninstall

```bash
metacomputer stop
./install.sh --uninstall      # removes the command, the alias, the skill and the MCP registration
```

That leaves the VM, its keys and Lume in place. To remove them too (with
standard Lume, use `lume` in place of `lume-secure`, and remove
`~/.local/share/lume` and `~/.local/bin/lume` instead):

```bash
lume-secure ls                                  # also shows checkpoints (metacomputer-ckpt-*) and metacomputer-baseline-prev
lume-secure delete metacomputer
lume-secure delete metacomputer-baseline
rm -r ~/.ssh/metacomputer
rm -rf ~/.local/share/lume-secure ~/.local/bin/lume-secure
```

Recordings and results stay in the checkout until you delete it.

## Documentation

- [SECURITY.md](SECURITY.md): the optional hardening, what it protects against, where it ends, and how to report a vulnerability.
- [architecture.md](architecture.md): the design, components and pinned versions, provisioning, the MCP transport, network, recording, and Lume's limits.
- [TESTING.md](TESTING.md): every test and how to run it.
- [skills/metacomputer/](skills/metacomputer/): the skill itself; [DESKTOP.md](skills/metacomputer/DESKTOP.md) and [BROWSER.md](skills/metacomputer/BROWSER.md) hold the verified desktop and browser rules.
- [docs/chrome-investigation.md](docs/chrome-investigation.md): how Chrome was added and why the input guard exists.
- `.claude/skills/cua-driver/`: Cua Driver's own skill pack, trimmed to its Linux guide.

## Contributing

Issues and pull requests are welcome. Before you change code:
- Read [architecture.md](architecture.md) and [CLAUDE.md](CLAUDE.md), which describes the repository's conventions.
- Settings belong in `config/defaults.env` and `config/local.env`. Don't hard-code a machine's names, paths or hosts.
- The optional hardening stays off by default, so metacomputer behaves like Cua Driver out of the box.
- Run the tests in [TESTING.md](TESTING.md) that cover your change, and `scripts/check-public.sh -a`, which checks the tree and history for anything personal.

Report security problems privately, as [SECURITY.md](SECURITY.md#reporting-a-vulnerability)
describes. Bugs in Cua Driver or Lume themselves belong upstream at
[trycua/cua](https://github.com/trycua/cua).

## Acknowledgements

metacomputer is built on [Cua](https://github.com/trycua/cua) by Cua AI, Inc.
(MIT): Cua Driver runs the desktop, Lume runs the VM, and `cua-netgw`, built
from Cua's source, is the network gateway (it uses gvisor-tap-vsock,
Apache-2.0). [lume-secure](https://github.com/metavert-io/lume-secure) adds
the host-sharing flags to Lume. `tools/qcow2raw` uses Lima's
[go-qcow2reader](https://github.com/lima-vm/go-qcow2reader) (Apache-2.0). The
VM runs Ubuntu, Firefox and, optionally, Google Chrome.
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists them with their
licenses.

## License

MIT License, © 2026 Metavert LLC. See [LICENSE](LICENSE).
