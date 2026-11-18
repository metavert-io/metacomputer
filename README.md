# metacomputer

**A computer of its own for Claude Code, running on your Mac.**

metacomputer gives Claude Code a full Ubuntu desktop: a virtual machine on your
Mac that Claude Code installs and then uses to automate any task a computer can
do. It works desktop apps and real browsers the way a person would, reading the
screen through the accessibility tree, clicking and typing. It works the way a
programmer would too, with a shell, Python and command-line tools. It records
video of what it did, signs in to sites from a vault without seeing your
passwords, and keeps files on volumes that outlive the VM.

The VM is kept away from your files, your accounts, your Mac and your network,
and what Claude does there can be approved, limited and logged.

metacomputer is open source (MIT). It's built on [Cua](https://github.com/trycua/cua)'s
open-source stack: Lume runs the VM, Cua Driver operates the desktop, and
`cua-netgw` is the network gateway. metacomputer is made by Metavert, not by
Cua, and not endorsed by them.

<!-- VIDEO: drag metacomputer-deep-dive.mp4 here on github.com -->

*The deep dive: how metacomputer works under the hood.*

## The 5-minute install

Paste this into Terminal on a Mac with Apple silicon:

```bash
curl -fsSL https://raw.githubusercontent.com/metavert-io/metacomputer/main/get.sh | bash
```

There's no VM for you to set up. The script builds it, with everything around
it, and checks that it works. Then, in any Claude Code session:

> Get yourself a virtual computer and tell me what's on its screen.

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
is yours; the rest runs unattended. It takes longer when:
- Homebrew still has to install Go, GnuPG and ffmpeg: add 2-5 minutes;
- the connection is slower: at about 50 Mbit/s, 10-15 minutes in all;
- the Mac is brand new: 15-30 minutes, mostly Apple's and Homebrew's downloads,
  of which about 5 minutes need you.

It needs about 15 GB of free disk and refuses to start with less (it checks each
disk it writes to). The install itself uses about 6 GB. Running it again is safe:
finished steps are skipped, and it never deletes a VM.

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

## What it can do

You ask in plain words, in any Claude Code session. "A computer", "your
computer", a real browser or a GUI app means the VM; "my screen", "my Mac" or
"my Chrome" means yours, and the VM is never used in its place. Claude says
which computer it's using before it acts, checks the result, and tells you
where the evidence is.

- **Desktop apps.** LibreOffice Writer and Calc, a calculator, a file manager,
  a text editor and a terminal come installed. Any other Ubuntu package
  (Inkscape, Audacity, Kdenlive, GIMP) is one setting away.
- **Real browsers.** Firefox and Chrome, driven like a person, or in Chrome
  through the page's own elements for exact forms, uploads and downloads.
- **Files and data.** Spreadsheets, documents, PDFs, images and archives, worked
  on with scripts in the VM (Python with openpyxl, python-docx and Pillow,
  poppler, sqlite3, ImageMagick, ffmpeg) or in the apps.
- **Checks of a web app.** Two browsers side by side, layouts at several widths,
  difference images against an earlier run, an accessibility check.
- **Untrusted links and files,** opened in a throwaway browser profile, away from your Mac.
- **Sign-ins and API keys** from a vault, without the values passing through the chat.
- **Files that outlive the VM:** disk volumes, Mac folders you see in Finder,
  S3 buckets and read-only reference disks.
- **Evidence:** an MP4 of a run, a contact sheet, per-action traces, and a signed
  log of every action.
- **Notifications** when it needs you or a task is done, on your phone, in Slack
  or in a SIEM.
- **Lessons from your own sessions:** it finds the mistakes it keeps making and
  proposes lessons, which you approve.

Code, files on your Mac, APIs and pages a plain fetch can read stay with Claude
Code's normal tools; the skill doesn't boot a VM for those.
[Prompt interesting computers into existence](#prompt-interesting-computers-into-existence)
has more than fifty things to ask, and the [guide](docs/guide.md) goes through
each capability with its commands.

## Security

An agent that uses a computer reads whatever is on the screen, and some of it is
written by people who want to steer the agent. No model is immune to that. So
the virtual computer holds none of your data unless you attach some and reaches
none of your machines, your credentials never pass through the agent's context,
and every protection is one you can check.

This keeps a well-behaved agent contained and makes a misbehaving one visible.
It doesn't make a deliberately hostile agent harmless, as
[Where it ends](#where-it-ends) explains. [SECURITY.md](SECURITY.md) has each
control and where it stops.

### Isolation and safety

These are always on:

| Protection | What it does |
| --- | --- |
| **No host sharing** | Lume attaches your Mac's clipboard, its speakers and microphone, and Rosetta to every VM. [mcomp-vm](https://github.com/metavert-io/metacomputer-vm), metacomputer's build of Lume 0.7.2, can leave each out, and metacomputer starts the VM without all three. `metacomputer status` reads what the VM actually has from inside it. |
| **Public internet only** | The VM's only network is a gateway on the Mac (Cua's `cua-netgw`) that checks every connection the VM opens. It refuses your Mac, your LAN and router, Tailscale and CGNAT addresses, link-local and cloud-metadata addresses, and outbound mail ports, and logs each attempt. The VM never appears on your LAN. |
| **Nothing listens off loopback** | SSH and the live view are forwarded only to `127.0.0.1` on your Mac. Lume's own VNC server, which listens on every interface, is never started. |
| **SSH by role** | Keys only, with the VM's host key made on your Mac and pinned before the first boot. A key per role: `mcp` can only start Cua Driver's server, `ops` is the desktop user's shell, `admin` is for maintenance, and `fill` and `proxy` serve only the vault. The Mac's ssh ignores your SSH config and forwards nothing. |
| **Narrow accounts** | The desktop user has no sudo. After the build, the maintenance account can run only one root script with fixed verbs, cloud-init is off, and every account's password is locked. |
| **Verified inputs** | The Ubuntu image (signed checksums, a pinned key and digest), the Cua Driver release (a pinned SHA-256, checked again inside the VM), Lume, the Mozilla and Google repository keys, and rclone for S3 volumes are all pinned and checked. The VM never downloads Cua components itself. |
| **Less in the guest** | No snaps, no clipboard agent, the virtio sound driver blacklisted, telemetry and update checks off, and browser policies that turn off password saving, Chrome sign-in and sync. Security updates install automatically. |
| **Page text is data** | The skill and the MCP server's instructions tell Claude that pages, documents, window titles and shell output from the VM are data, never instructions. Claude never asks you for a password. |
| **The VM is treated as hostile** | What comes back from the VM is bounded and checked: capped messages, strict reply formats, escaped terminal output. Files copied to the Mac arrive as plain files only (no links or devices), and a copy stops before it can fill your disk. |
| **Your Mac stays yours** | Nothing here can see or control your Mac's screen, and no LaunchAgent or daemon is added. The VM's wallpaper says VIRTUAL COMPUTER, so every screenshot shows which machine it is. |

The [guide](docs/guide.md#isolation-and-safety-in-detail) has the network rules,
every SSH credential and what each one grants.

### The secrets vault

`metacomputer secrets` lets Claude sign in to sites and call APIs inside the VM
without a password, code or key passing through the conversation. It ships with
metacomputer, needs no password manager, and is built from source at install
(macOS 15 or later).

- **Signing in without Claude being handed the password.** You store a login once,
  in your own terminal, or import it. In the VM, Claude opens the sign-in page in
  page-mode Chrome and runs `metacomputer secrets login NAME` (username and
  password under one approval) or `fill NAME --field password|username|otp`, as
  the skill teaches it. The vault checks, from the browser's own view, that the
  page and the field's frame are on one of the login's sites and that the field is
  the right kind, then types the value through a locked account in the VM that
  Claude can't change. Claude gets back only the outcome, such as "filled, 16
  characters". For 2FA, the vault works out the current code from the stored seed.
- **You approve, with Touch ID on Macs that have it.** You unlock the vault in your
  own terminal, which shows every request and who asked ("Asked by Claude Code in
  ~/Projects/foo"). A use that asks waits for you. On a Mac with Touch ID, every
  approval takes your fingerprint (or your login password in macOS's dialog), since
  any program that can type into a terminal could type an answer there. On a Mac
  without it you approve by typing the secret's name, or turn the same mode on and
  confirm with your login password. `metacomputer secrets approve ID` answers from
  any terminal. Nothing Claude runs can approve a use.
- **Google, Microsoft, Apple and Okta sign-ins always ask.** One sign-in at an
  identity provider opens every site that trusts it, so a fill on their sign-in
  pages asks every time, whatever a secret's settings, an earlier answer or a rule
  says. So does an API token for one of their hosts, and loading a whole saved
  browser profile.
- **Bring the logins you have.** `import-passwords --browser chrome` reads Chrome's
  saved passwords (macOS asks you before the vault may use Chrome's key), and
  `--csv` takes an export from Chrome, the macOS Passwords app, 1Password or
  Bitwarden, with their one-time-code columns as 2FA secrets. `import-session`
  brings one site's cookies from your Chrome into a VM profile. Imported logins ask
  before every use until you relax them, and the import shows only sites, counts
  and masked usernames.
- **API keys never enter the VM.** A program in the VM gets a short-lived token (15
  minutes by default, 8 hours at most) for a proxy that appears inside the VM. The
  proxy on your Mac puts the real key into each request and sends it only to that
  API's host, over HTTPS, and blanks the key out if the API echoes it back. Tokens
  end when the vault locks.
- **An audit trail you can send away.** Every use, refusal and change is a line in a
  hash-chained log, the records the vault writes carry a MAC under a key only it
  holds, and a copy goes to macOS's unified log. Audit sinks send each record off
  the Mac as it happens (signed webhooks, OpenTelemetry, syslog over TLS, ntfy),
  and a sink marked `--required` must acknowledge each use before it goes ahead.
  Records carry the site-login event names other password vaults use
  (`login.request`, `login.fill`, `login.denied`).
- **Keys.** The vault file is encrypted under your passphrase together with a key in
  this Mac's Secure Enclave, so a copy can't be opened elsewhere with the passphrase
  alone. A recovery key, shown once, opens it on another Mac.

In your own terminal, once:

```bash
metacomputer secrets init                                          # a passphrase; prints a recovery key once
metacomputer secrets import-passwords --browser chrome --dry-run   # what would come in (then without --dry-run)
metacomputer secrets bind                                          # pin the VM: check its fingerprint, type yes
metacomputer secrets unlock --for 1h                               # keep this terminal open while Claude works
```

Or add one login by hand:
`metacomputer secrets set example --kind password --origin https://example.com --username you@example.com`.

**Coming from Cua Spaces?** If you used its Keyvault, the names you know work here
today: `metacomputer secrets requests`, `approve`, `deny`, `lock` and
`import-passwords`, and the audit log uses its event names. An MCP tool in the
style of its `request_site_login` is coming in a point release; until then Claude
signs in with `secrets login` and `fill`.
[docs/moving-from-spaces.md](docs/moving-from-spaces.md) maps the rest.

**Its limit.** The vault keeps values out of Claude's view in normal operation. It
isn't a wall against a deliberately hostile agent: with a shell in the VM, code can
pose as Chrome's developer tools to receive a fill, and driving the browser can
reveal a field (a site's "show password" button, for one). SECURITY.md has the
[full model](SECURITY.md#secrets-and-logins-metacomputer-secrets) and
[what doesn't hold](SECURITY.md#what-doesnt-hold). The
[guide](docs/guide.md#use-saved-passwords-and-2fa-codes) covers approvals,
unattended rules, tokens, rotation and audit sinks step by step.

### Every guard on every path: the hub

One process per VM on your Mac, the hub, holds the only connection to Cua Driver.
Claude Code's tools, `metacomputer call`, `metacomputer web`, the GUI tests and
scripts all go through it, over a socket only your user can use, so every guard
applies to each of them:
- **One controller at a time.** While one Claude Code session is working the VM,
  another session's actions and shell runs are refused (reads still work). Claude
  asks you before taking over.
- **The Chrome guard.** Chrome ignores background input, so input aimed at it is
  sent in the foreground, and `set_value` and `run_script` aimed at it are refused.
- **The coordinate guard.** A pixel click is refused when the windows have changed
  since the screenshot it was aimed from (resized, or covered by a dialog or
  another app), so a click can't land somewhere Claude didn't look.
- **Bounded mode's limits,** when it's on (below).

A refusal is reported to you, never worked around.
[SECURITY.md](SECURITY.md#the-hub-and-its-guards-mcp_hub-coord_guard) has the
details.

### Opt-in hardening

These are off by default, so out of the box metacomputer behaves the way Cua
Driver does on its own. Turn any of them on in `config/local.env`;
`metacomputer security` shows what's on.

| Setting | What it adds |
| --- | --- |
| `ACTION_LOG=on` | A signed, hash-chained log of every tool call and shell run ([below](#recordings-and-the-signed-action-log)) |
| `DRIVER_MODE=bounded` | Cua Driver does only what a manifest lists ([below](#bounded-mode)) |
| `SSH_AUTH=cert` | A fresh key and a 5-minute SSH certificate for every connection, named in the VM's auth log (sealed browser sessions need the default, `key`, for now) |
| An event sink | Events sent as they happen ([below](#events-hear-from-it)); with no sink, nothing leaves the Mac |

### Bounded mode

`DRIVER_MODE=bounded` turns on Cua Driver's bounded mode: every call must fit a
capability manifest of tools, apps, folders and websites, with a time limit (8
hours, or 30 minutes idle, by default). Use a profile (`readonly`, `desktop` with
no terminal, or `browser` with only the sites you list) or your own manifest. The
Mac asks for the manifest by its SHA-256, and the VM refuses one that doesn't
match. While bounded, the shell channel turns itself off, since a shell isn't held
to the manifest, and storage attaches only what the manifest lists. Page tools and
desktop tools can't share one bounded session, so hybrid browsing needs standard
mode. [SECURITY.md](SECURITY.md#bounded-driver-driver_modebounded) has the rest.

### Recordings and the signed action log

- **Per task, through Cua Driver:** a 1920×1080, 30 fps MP4 and a folder per action
  with the state, screenshots and click markers before and after.
  `metacomputer task finish` copies it to the Mac with a contact sheet.
- **The whole desktop, whoever drives it:** `metacomputer record start NAME` and
  `record stop` capture the screen with ffmpeg, with the VM's own sound when it has
  a sound card, and a description file with the video's SHA-256.
  `RECORD_QUALITY=high` is sharp enough to edit into videos.
- **The signed action log** (`ACTION_LOG=on`): every tool call and `metacomputer sh`
  run, each record chained to the one before by its hash, and the chain's head
  signed with an SSH key as it goes. By default typed text is recorded as its
  length, and scripts and output as sizes and hashes. `metacomputer actions verify`
  finds edited, inserted, reordered or removed records. With it on, a desktop
  recording also lists its key moments (`0:42 launch_app inkscape`).

Recordings show the whole screen. Passwords filled by the vault show as dots, but
usernames and codes are visible.

### Events: hear from it

`metacomputer events` sends what matters as it happens: Claude blocked on you (a
sign-in, a CAPTCHA, a 2FA code), a task or recording finished, connections the
gateway blocked, refusals by the Chrome guard or bounded mode, a failed
`actions verify`, what `web open-untrusted` found, and the VM's health. Sinks are
webhooks (CloudEvents, signed), OpenTelemetry (OTLP/HTTP), syslog over TLS and
ntfy for your phone.
- **Off by default.** With no sink, nothing is sent; events go only to a local log.
- **What's sent:** tool names, counts, paths, the VM's and the Mac's short names,
  and the scheme, host and port of web pages. Never typed text, passwords, tokens,
  page contents, screenshots or URL queries.
- **Sinks change only from your own terminal,** and every change is announced to
  the sinks you already had. Floods are capped per family and per phone.

It's best effort, not an audit trail: the signed action log and the vault's audit
log are the tamper-evident records. [docs/events.md](docs/events.md) has every
event, the wire formats and a Slack relay.

### Where it ends

- **Your own account.** None of this protects against someone who already controls
  your Mac account: they can change the config, read the keys and drive the VM
  directly.
- **A deliberately hostile agent.** Claude Code runs as you. The controls above keep
  a well-behaved agent away from your machines and secrets and make a misbehaving
  one visible; they don't stop one that sets out to work around them as your user.
  An agent with a shell in the VM can get at a password once it's typed into a page.
- **The public internet.** The VM can reach it by design, so anything you put in the
  VM could be sent out.

[SECURITY.md](SECURITY.md#what-these-protect-against) explains each control and
where it ends.

## Features

### Storage that outlives the VM

The VM is disposable: `reset` puts back the clean baseline. Storage is how files
survive that, travel between VMs or come from your Mac, while the VM stays
untrusted. Nothing is attached until you list it in `VOLUMES` or `REFDISKS`.

| Kind | What it is | In the VM |
| --- | --- | --- |
| **Disk volume** | An ext4 disk image on your Mac, any drive | Read-write, one VM at a time |
| **Folder volume** | A Mac folder, shared live and visible in Finder | Read-only unless you add `:rw` |
| **S3 volume** | A folder kept in step with a bucket on AWS S3, R2, MinIO or any S3-compatible store | Like a folder volume |
| **Reference disk** | A read-only image packed from a Mac folder | Read-only, any number of VMs |

```bash
metacomputer volume create work --size 20G    # a sparse disk image on the Mac
echo 'VOLUMES=work' >> config/local.env       # in the VM at ~/data/work
metacomputer restart                          # attached at start, formatted inside the VM on first use
```

- **They survive** `reset`, `restore` and rebuilds, and `checkpoint NAME
  --with-volumes` snapshots them with the VM.
- **Read-only means read-only:** the Mac holds the share read-only, and the VM
  proves it can't write at every start.
- **A read-write folder is watched.** Its name must include `.noindex` (to keep
  Spotlight out), and while the VM runs a watcher on the Mac puts macOS's quarantine
  attribute on everything the VM writes, replaces links that lead out of the folder,
  removes devices and FIFOs, and stops the VM if it can't keep up.
- **Private places are refused:** your home folder, Documents, Desktop, Downloads,
  Library and iCloud Drive, dot-folders such as `.ssh`, and the checkout.
- **S3 keys never enter the VM.** You type them at macOS's own prompt into the
  Keychain (`volume keys`), and rclone, pinned and sandboxed, syncs on the Mac.
- Mount points such as `/volume` and `/mnt/shared` match Cua Spaces', so scripts
  written for it keep working.

[docs/storage.md](docs/storage.md) has the details, and
[SECURITY.md](SECURITY.md#storage) what each kind lets a hostile VM do.

### It learns from its own sessions

Claude makes some of the same mistakes again and again on a desktop: clicking by
pixel without a fresh screenshot, or trying a third way to do something without
checking why the first two failed. `metacomputer learn report` (or `/metacomputer:learn`
in a chat) reads your own Claude Code sessions that used the virtual computer,
counts what went wrong and where the time went, and proposes short lessons with
the evidence: "34 pixel clicks refused for lack of a screenshot, in 17 of 33
sessions", say, and the rule that avoids them.

You decide which lessons to keep (`metacomputer learn review`, or tell Claude in a
chat which ones to approve). Approved lessons go to every later session as advice,
after the skill's own habits. They can't switch off a safety check, override a
refusal or overrule you. Reading your sessions and storing the lessons happen on
your Mac, but an approved lesson becomes part of the instructions each Claude Code
session sends to the model, like the skill itself, so don't put anything in one you
wouldn't send to Claude. `metacomputer learn bench` runs the benchmark on a test VM
with and without your lessons, so you can see whether they help.

Nothing a web page or document in the VM says can become a lesson: proposals are
built only from counts and fixed names, and lessons in your own words need your
own terminal. [docs/learn.md](docs/learn.md) has the details.

### The shell channel: `metacomputer sh`

`metacomputer sh` runs bash in the VM as the desktop user and brings back its
output, exit code and duration. Claude routes each piece of work by where its
state lives, following Microsoft Research's
[Hybrid Computer Use](https://github.com/microsoft/Hybrid-Computer-Use) work:
files no app holds open go to the shell (converting, extracting, counting), while
open documents, settings, signed-in sites and canvases go to their apps, because
the app's copy is what gets saved. Claude never writes a file behind an app's back
to fake a GUI result, and says when part of a task went through the shell.

```bash
metacomputer sh -c 'pdftotext -layout ~/Inbox/invoice.pdf - | head -40'
metacomputer sh --file ./convert.sh -- ~/Inbox/reports    # a script from your Mac, with arguments
```

- It can do what the desktop user can, and can't reach your Mac or get root.
- It treats the VM as hostile: capped output, one deadline, a strict reply format,
  and escaped text before anything reaches your terminal.
- It counts as acting, so it's refused while another session controls the VM.
- Bounded mode turns it off, and the signed action log records each run when it's on.

[docs/shell.md](docs/shell.md) has the design.

### Browsers, and checks that need no model

Firefox is the default; Chrome is installed beside it. Claude can work a browser
three ways: **human** (reads the accessibility tree and clicks and types in the
window, for demos and recordings), **page** (Chrome only: works on the page's own
elements, for exact forms, uploads and downloads) and **hybrid** (reads through the
page, acts with real clicks and typing that show in a recording).

Four scripted checks need no model and write a report and images:

```bash
metacomputer web compare URL                   # Firefox and Chrome side by side, with the differences
metacomputer web responsive URL --widths 1920,1280,768,500   # --baseline DIR adds difference images
metacomputer web a11y URL                      # unlabelled controls, missing alt text and headings, vague links
metacomputer web open-untrusted URL            # redirects, the final page, password prompts, downloads; nothing clicked
```

### Add apps

The desktop user has no sudo, so system packages go in `EXTRA_PACKAGES` in
`config/local.env`, from Ubuntu's archive, and take effect on the next build:

```bash
echo 'EXTRA_PACKAGES="inkscape audacity"' >> config/local.env
metacomputer build --replace     # rebuilds the VM and its baseline: everything in the VM is lost, volumes are kept
```

Rules for Inkscape, Audacity, Kdenlive and pygame windows, verified in a VM built
this way, are in [EXTRA-APPS.md](skills/metacomputer/EXTRA-APPS.md), and the
[guide](docs/guide.md#add-apps) lists the packages. `VIRTUAL_AUDIO=on` gives the VM
a sound card that plays into nothing, so editors and games have sound and
recordings can include it; nothing reaches your Mac's speakers.

### The Claude Code plugin

The `metacomputer` plugin brings everything Claude Code needs, to every project:
- **the `metacomputer` skill:** when a task belongs on the virtual computer, a routine
  for working it (check who else is using it, start it, act from the accessibility
  tree, verify the outcome, hand back the evidence), and desktop and browser rules
  verified on this VM;
- **Cua Driver's own guide,** as `metacomputer:cua-driver`;
- **`/metacomputer:learn`,** which runs the lessons report in a chat;
- **the MCP server,** which lists the tools even while the VM is off.

The installer adds it from your checkout, so a `git pull` updates the skills at the
next session start. Installed from GitHub with `/plugin install`, it updates with
`claude plugin update metacomputer@metacomputer` and runs your checkout's command.
The tools are named `mcp__plugin_metacomputer_metacomputer__*` (or
`mcp__metacomputer__*` with `./install.sh --no-plugin`); permission rules and
scripts of your own need that prefix.

### The benchmark

[bench/](bench/README.md) is a fixed set of 20 desktop tasks across Calculator,
Mousepad, Writer, Calc, Thunar, Firefox, Chrome and the shell. Each runs in a fresh
headless Claude Code session on a test VM, and a check reads the VM's real state
afterwards (saved files, what the browser really sent to a local server, app
settings) rather than trusting what the agent says. It reports pass rate, a partial
score, cost, turns, time and the mix of tool calls, so you can change one thing
(the skill, the shell channel, the driver, the model) and see what moves. The method
follows Microsoft Research's Hybrid Computer Use study. Tightening the skill from
recorded sessions, for this release, kept all 20 tasks passing and took calls
rejected for invalid arguments from 14 to 0.

It runs on a test VM and refuses your main one unless you pass `--allow-main-vm`.
Each run spends Claude usage, within per-task and per-run budget caps.

## Prompt interesting computers into existence

Each of these is something you can type into Claude Code with the plugin
installed. Claude decides what runs on the desktop and what goes through the
shell, says which computer it's using, and ends with a check and the paths of what
it made. Where a prompt needs an app that isn't in the standard VM, it says which
package to add to `EXTRA_PACKAGES` (and the VM then needs a rebuild; see
[Add apps](#add-apps)).

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

**Sign-ins and keys**

| You say | What happens |
| --- | --- |
| "Log me into github.com on the virtual computer with my saved login, then open my notifications." | Claude opens the page in page-mode Chrome and runs `metacomputer secrets login`; the vault types the username and password under one approval, with Touch ID on a Mac that has it. Claude gets back "filled", never the password. |
| "Sign in to the demo dashboard with my `demo` login and its 2FA code from the vault, then download this month's report." | The vault types the username, password and current code; Claude never sees them. You approve in your terminal if you asked to. |
| "Bring my Chrome passwords into the vault." | Claude can't do this one for you: it gives you `metacomputer secrets import-passwords --browser chrome` for your own terminal. macOS asks whether the vault may use Chrome's key, and imported logins ask before every use. |
| "Use my GitHub session from my own Chrome on the virtual computer, so it doesn't have to sign in again." | You bring that one site's cookies in from your terminal (`import-session`); Claude loads them into a VM profile, with your approval. Cookies only. |
| "Let this project use the staging login without asking me for the next 8 hours." | Claude gives you the `metacomputer secrets rule add ... --for 8h` command; making the rule takes Touch ID or the passphrase. Google, Microsoft, Apple and Okta sign-ins still ask every time. |
| "Use my OpenAI key from the VM without the VM seeing it: run this script against the API there." | Claude gets a 15-minute token; the script calls the proxy inside the VM, and the proxy on your Mac adds the real key. |
| "Keep me signed in to the admin site between runs." | Claude seals the signed-in Chrome profile into the vault and loads it next time; each load asks you, since a whole profile can hold any sign-in. (Sealed sessions need `SSH_AUTH=key`, the default, for now.) |

**Files that outlive the computer**

| You say | What happens |
| --- | --- |
| "Keep my project files on a volume so they survive a reset." | `metacomputer volume create work --size 20G` and `VOLUMES=work`: a disk image on your Mac, attached at the next start, that outlives resets and rebuilds. |
| "Put your results in a folder I can open in Finder." | A folder volume, read-write because you asked. The folder's name needs `.noindex`, and a watcher on your Mac quarantines everything the VM writes there. |
| "Give the VM a read-only copy of this folder of reference papers." | `metacomputer refdisk create`: a read-only disk image that any number of VMs can attach. |
| "Use the project files in our team bucket, and put your results there." | An S3 volume: rclone on your Mac keeps a folder in step with the bucket. You type the keys into the Keychain yourself (`volume keys`); they never enter the VM. |
| "Checkpoint the computer and its work volume before you try the migration." | `metacomputer checkpoint NAME --with-volumes` while the VM is stopped (APFS clones where it can); `restore` rolls both back, after asking you. |

**Web**

| You say | What happens |
| --- | --- |
| "Does our landing page look the same in Firefox and Chrome? Show me the differences." | `metacomputer web compare`: both browsers side by side, with differences in headings, links, controls and text. |
| "Show me the pricing page at desktop, tablet and phone widths, and tell me where the layout breaks." | `metacomputer web responsive` at 1920, 1280, 768 and 500 px, then a look at each screenshot. |
| "Fill in the contact form on our public staging site with test data and confirm the thank-you page appears." | Chrome in page mode fills each field exactly, submits, and checks the next page. |
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
| "What did you do on the virtual computer in the last session?" | With `ACTION_LOG=on`, `metacomputer actions show latest` lists every tool call and shell run, and `actions verify` checks nothing was changed. |

**Getting better at the job**

| You say | What happens |
| --- | --- |
| "What do you keep getting wrong on the virtual computer? Show me the evidence." | `metacomputer learn report`: failed calls by class, repeated mistakes and time per app from your own sessions, with proposed lessons and their counts. |
| "Approve the no-frame lesson." | Claude approves only the IDs you name. The lesson reaches later sessions as advice that can't switch off a guard; lessons Claude drafted need your own terminal. |
| "Do my lessons actually help? Measure it on the test computer." | `metacomputer learn bench`: the benchmark with and without them, on a test VM. It says how many sessions it will run and asks first, since it spends your Claude usage. |

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
| "Set up a second virtual computer called `staging`, Firefox only, beside the main one." | Claude writes a settings file with its own names, ports and folders, and builds it with `MCOMP_CONFIG`. About 5 GB more disk to start, growing as it's used. |
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

**Teach it a new app**

| You say | What happens |
| --- | --- |
| "Add GIMP to the computer, then find out which of its menus and dialogs work through accessibility and which need real clicks. Write the rules down like DESKTOP.md." | After the rebuild (which needs your OK), Claude explores and writes notes you can add to the skill. GIMP has no verified rules yet. Needs `gimp`. |
| "Learn our web app's admin screens and write a short guide, with screenshots, that the next session can follow." | Claude maps each screen from its accessibility tree and saves a Markdown guide with screenshots. |
| "Try Inkscape's export with each method you have and tell me which works best here." | Claude compares the shortcut, the menu and the command line, and reports which hung and which worked. Needs `inkscape`. |

## How it works

```text
Your Mac                                                                  In the VM
├── Claude Code + the metacomputer plugin
│     ├── MCP tools ─────────────┐
│     ├── metacomputer call, web ┴▶ hub: one per VM, every guard ───SSH─▶ Cua Driver
│     └── metacomputer sh ──────────────────────────────────────────SSH─▶ bash, as the desktop user
├── secrets vault, unlocked in your terminal ───────────────────────SSH─▶ locked fill and proxy accounts
├── volumes, folders and reference disks you attach ────────────────────▶ mounted at start
├── cua-netgw: the VM's only network; the public internet, nothing of yours
└── mcomp-vm (Lume) runs the VM: Ubuntu 24.04 ARM64, XFCE, Firefox, Chrome, LibreOffice
```

| Part | What it is |
| --- | --- |
| The virtual computer | An Ubuntu 24.04 ARM64 desktop (XFCE on Xorg) in a Lume VM on Apple's Virtualization framework: 4 vCPUs, 8 GB of memory and a 50 GB sparse disk by default. Firefox, LibreOffice Writer and Calc, GNOME Calculator, Thunar, Mousepad and a terminal come installed, Google Chrome optionally, and tools for scripts: Python with openpyxl, python-docx, odfpy and Pillow, poppler-utils, sqlite3, zip, ImageMagick, ffmpeg, and venv and pip for anything else. |
| Cua Driver, inside the VM | [Cua Driver](https://github.com/trycua/cua) 0.53.0, an MCP server for the desktop: accessibility trees, mouse and keyboard input, screenshots, recordings, and page-level tools for Chrome. It runs only in the VM and is never installed on your Mac. |
| The hub | One process per VM on your Mac that holds the only connection to Cua Driver and applies every guard to every client. |
| The shell channel | `metacomputer sh`: bash in the VM as the desktop user, for files no app holds open. |
| The secrets vault | `metacomputer secrets`: a small program on your Mac, built from `vault/`, with a locked fill account and a proxy account in the VM. |
| The network gateway | `cua-netgw`, built from Cua's source at a pinned commit: the VM's only network, allowing the public internet and nothing of yours. |
| mcomp-vm | metacomputer's build of Lume, which can keep your clipboard, audio and Rosetta away from the VM. `./install.sh --lume=standard` uses Cua's release instead, which always shares them. |
| The plugin | The skills and the MCP server, in every Claude Code project ([plugin/](plugin/)). |
| The `metacomputer` command | `~/.local/bin/metacomputer` (alias `mcomp`): starts, stops and checks the VM, runs scripts in it, moves files, records, runs browser checks, keeps snapshots and opens the live view. |

You could put this together yourself. You would need a VM runner and an Ubuntu
ARM64 image converted into a disk it boots; unattended provisioning of a desktop
with accessibility turned on, the apps, Cua Driver, SSH and a firewall; a bridge
that carries the driver's MCP stream from the VM to Claude Code; a network that
keeps the VM off your LAN; a live view, recordings, a shell and a way to move
files; and instructions that tell the model when to use the VM, how to drive this
particular desktop reliably, and how to check its own work. metacomputer turns
that into one command and a plugin, built from pinned, verified sources.

[architecture.md](architecture.md) has the design, the pinned versions and the
workarounds for Lume.

## Requirements

- A Mac with Apple silicon (M1 or later) and macOS 14 or later. The secrets vault
  needs macOS 15; everything else works without it.
- About 15 GB of free disk; the installer refuses to start with less. It checks
  each disk it writes to: 10 GB where Lume keeps VMs, 2 GB for the checkout and 1 GB
  each for your home folder and the temporary folders. The install uses about 6 GB,
  and the VM's disk grows as it's used, up to 50 GB. Each extra VM needs its own space.
- Memory for the VM: 8 GB by default, or 4 GB on a Mac with less than 16 GB.
- Homebrew and Apple's Command Line Tools. get.sh offers both when they're missing.
- [Claude Code](https://claude.com/claude-code), for the plugin.
- About 1.5 GB of downloads for the VM.
- Optional: Google Chrome in the VM. Installing it means accepting
  [Google's terms](https://www.google.com/chrome/terms/).

## Commands

`metacomputer` (alias `mcomp`). [docs/commands.md](docs/commands.md) has every
command, option and setting, and the [guide](docs/guide.md) shows each in use.

| For | Commands |
| --- | --- |
| Running it | `start`, `stop`, `restart`, `status`, `who`, `release`, `view` |
| Working in it | `sh`, `inspect`, `call`, `browser`, `web compare\|responsive\|a11y\|open-untrusted` |
| Files | `put`, `get`, `shot`, `task start\|finish` |
| Storage | `volume create\|list\|resize\|delete\|keys\|test\|sync`, `refdisk create\|list\|delete` |
| Saving and resetting | `checkpoint`, `checkpoints`, `restore`, `reset`, `baseline`, `build` |
| Evidence | `record start\|stop\|moments`, `recordings`, `contact-sheet`, `actions list\|show\|verify` |
| Security | `security [apply]`, `secrets ...` (the vault) |
| Events | `notify`, `events sink\|status\|tail` |
| Lessons | `learn report\|review\|approve\|list\|bench` |
| Setup and upkeep | `setup`, `where`, `logs`, `test`, `hub`, `help` |

Commands that change the VM (`reset`, `restore`, `baseline`, deleting a
checkpoint) ask first, and Claude runs them only with your OK.

## Documentation

- [docs/install.md](docs/install.md): installing, options, what goes where, updating, uninstalling, troubleshooting the install.
- [docs/guide.md](docs/guide.md): what to ask Claude Code, task by task, with the commands; isolation in detail; known limitations; troubleshooting.
- [docs/commands.md](docs/commands.md): every command and setting.
- [SECURITY.md](SECURITY.md): every control, what it protects against, where it ends, the secrets vault in full, and how to report a vulnerability.
- [architecture.md](architecture.md): the design, components and pinned versions, provisioning, the hub, network, recording, the event spool, and Lume's limits.
- [docs/storage.md](docs/storage.md): volumes, folders, S3 and reference disks, and a table for anyone coming from Cua Volume.
- [docs/moving-from-spaces.md](docs/moving-from-spaces.md): coming from Cua Spaces' Keyvault.
- [docs/learn.md](docs/learn.md): lessons from your own sessions.
- [docs/events.md](docs/events.md): the event stream, every event, wire formats and receivers.
- [docs/shell.md](docs/shell.md): `metacomputer sh`, its design and how it treats the VM.
- [bench/README.md](bench/README.md): the benchmark.
- [TESTING.md](TESTING.md): every test and how to run it.
- [skills/metacomputer/](skills/metacomputer/): the skill itself, with the verified desktop and browser rules in [DESKTOP.md](skills/metacomputer/DESKTOP.md) and [BROWSER.md](skills/metacomputer/BROWSER.md).
- [docs/chrome-investigation.md](docs/chrome-investigation.md): how Chrome was added and why the input guard exists.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release.

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
(MIT): Cua Driver runs the desktop, Lume runs the VM, and `cua-netgw`, built from
Cua's source, is the network gateway (it uses gvisor-tap-vsock, Apache-2.0).
[mcomp-vm](https://github.com/metavert-io/metacomputer-vm), metacomputer's build
of Lume, adds the host-sharing flags; like metacomputer, it's made by Metavert,
not by Cua, and not endorsed by them. The shell channel's routing and the
benchmark's method come from Microsoft Research's
[Hybrid Computer Use](https://github.com/microsoft/Hybrid-Computer-Use) work.
`tools/qcow2raw` uses Lima's
[go-qcow2reader](https://github.com/lima-vm/go-qcow2reader) (Apache-2.0). The VM
runs Ubuntu, Firefox, LibreOffice and, optionally, Google Chrome.
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists them with their licenses.

## License

MIT License, © 2026 Metavert LLC. See [LICENSE](LICENSE).
