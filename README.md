# metacomputer

**Agentic coding plus a real desktop, with the controls you'd want around it.**

metacomputer gives Claude Code a disposable Ubuntu computer of its own: a
virtual machine on your Mac. Claude operates it the way a person would: it
reads the screen through the accessibility tree, clicks and types, uses
Firefox and Chrome, and records video of what it did. It also works in it the
way a programmer would, with a shell, Python, LibreOffice, ImageMagick and
ffmpeg. The VM is kept away from your files, your accounts, your Mac and your
network, and what Claude does there can be logged, limited and approved.

https://github.com/user-attachments/assets/9465c66c-88fd-4e58-845e-a430c61ddf1a

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
work, `metacomputer view --open` opens the live desktop in Screen Sharing, with
its password on your clipboard. More ideas:
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

## What makes metacomputer awesome?

**Security: walled off from your Mac.** Out of the box the VM shares no
clipboard, audio or files with your Mac, and its only network is a gateway that
reaches the public internet and nothing of yours: not your Mac, your LAN or your
tailnet. Page text that's likely hidden from you, or that addresses AI agents, is
marked in what Claude reads, so it can point it out to you; and at a CAPTCHA or a
sign-in that's yours, Claude hands you the computer and waits. A signed log of
every action, a restricted mode that holds Cua Driver to the apps and sites you
list, and recordings saved on your Mac as they're made are there when you want
them ([Security in brief](#security-in-brief)).

**Key management: Claude signs in without seeing your passwords.** The vault on
your Mac types a password or the current 2FA code into the page and tells Claude
only the outcome, and API keys go through a proxy on your Mac, so they never enter
the VM, not even for Claude Code running inside it. Uses can wait for your
approval, by Touch ID where the Mac has it, and each one lands in an audit trail
you can send off the Mac. Bring your logins from Chrome, the Passwords app,
1Password or Bitwarden
([the vault, step by step](docs/guide.md#use-saved-passwords-and-2fa-codes)).

**Shared storage: files that outlive the VM.** Disk volumes survive resets and
rebuilds, Mac folders are shared read-only unless you say otherwise (and show in
Finder), folders can stay in step with an S3 bucket, and reference disks give any
number of VMs the same read-only files. Nothing is attached until you list it
([docs/storage.md](docs/storage.md)).

**Self-sovereign: no cloud services that you need.** Beyond Claude Code itself,
which is Anthropic's service, and the install's downloads from GitHub, Ubuntu and
the projects it builds on, it needs nothing else in the cloud: no account, no
subscription. It's the self-hosted way to run Cua's open-source stack (Lume, Cua
Driver, cua-netgw), at pinned versions you can fork. Your keys stay on your Mac,
in the vault and your own Keychain; logs and events go only where you send them;
and moving from Cua Spaces' Keyvault is [documented](docs/moving-from-spaces.md).

**Built on Microsoft Research's Hybrid Computer Use (HCU) study.** HCU found that
an agent with a shell beside the screen is more accurate and much cheaper than one
that drives everything through the GUI. So the skill routes work by where the
state lives: files and data to the shell, which is what `metacomputer sh` is for,
and GUI apps and signed-in sites to the screen ([docs/shell.md](docs/shell.md)).
The hub and the benchmark follow the same study.

**`metacomputer learn`: it learns your apps without being told.** It reads
Claude's own past sessions, finds where it stumbled (refused calls, pixel clicks
without a fresh screenshot, repeated mistakes, the apps where the time went) and
proposes short lessons with the evidence. You approve the ones you want, and every
new session starts with them, as part of the instructions sent to the model: you
don't write the instructions, but you decide what's kept
([docs/learn.md](docs/learn.md)). The skill's own habits came from mining sessions
the same way: on the 20-task benchmark, invalid-argument calls fell from 14 to 1
in a full run, and to 0 once the three tasks a final change to the notes touched
were run again ([records](bench/runs/)).

**Contents:**
[What it can do](#what-it-can-do) ·
[Worked examples](#worked-examples) ·
[Prompts](#prompt-a-computer-into-existence) ·
[How it works](#how-it-works) ·
[Security in brief](#security-in-brief) ·
[Requirements](#requirements) ·
[Everyday commands](#everyday-commands) ·
[Configuration](#configuration) ·
[Troubleshooting](#troubleshooting) ·
[Updating and uninstalling](#updating-and-uninstalling) ·
[Documentation](#documentation) ·
[Contributing](#contributing) ·
[Credits](#credits)

## What it can do

Ask in plain words. "A computer", "your computer", a real browser or a GUI app
means the VM; "my screen", "my Mac" or "my Chrome" means yours, and the VM never
stands in for it. Code, files on your Mac and pages a plain fetch can read stay
with Claude Code's normal tools.

**On the desktop**
- **Desktop apps:** LibreOffice Writer and Calc, a calculator, a file manager, a
  text editor and a terminal, driven from the accessibility tree, with every
  result checked.
- **More apps:** any Ubuntu package through `EXTRA_PACKAGES`, with verified rules
  for Inkscape, Audacity, Kdenlive and pygame
  ([EXTRA-APPS.md](skills/metacomputer/EXTRA-APPS.md)); `VIRTUAL_AUDIO=on` adds a
  sound card that plays into nothing.
- **Firefox and Chrome:** *human* mode (clicks and typing like a person, at a
  person's pace: the default, and the one for sign-ins, checkout and posting),
  *page* mode (Chrome's page elements: bulk forms, uploads, downloads, dialogs)
  and *hybrid* (page reads, real clicks and typing for a recording). Kept Firefox
  profiles keep a sign-in until a reset.
- **Web checks that need no model:** `web compare` (Firefox against Chrome),
  `web responsive` (several widths, with difference images against a past run)
  and `web a11y`.
- **Your own web app:** its static build served on the VM's localhost and clicked
  through in both browsers.

**Files, sign-ins and storage**
- **Files and data through the shell:** `metacomputer sh` with Python,
  LibreOffice headless, pdftotext, sqlite3, ImageMagick and ffmpeg; `put` and
  `get` move files.
- **Sign-ins:** saved passwords and 2FA codes typed by the vault into page-mode
  Chrome, signed-in profiles sealed and restored, or you sign in yourself through
  the live view, in a handoff.
- **API keys:** short-lived tokens for a proxy on your Mac that adds the real key;
  a long job renews its token (`secrets token renew`) instead of getting a new one.
- **Claude inside the metacomputer:** `metacomputer claude` installs and starts
  Claude Code in the VM, signed in through that proxy (your subscription token or
  API key stays on the Mac), and Claude drives it with `send` and `read`; `--show`
  puts it on the VM's desktop for the live view and recordings.
- **Storage:** disk volumes, shared Mac folders, S3-backed folders and reference
  disks.

**Evidence and safety**
- **Recordings:** per task, with screenshots before and after each action and a
  contact sheet, or the whole desktop, with sound and key moments, streamed to
  your Mac as it records so a run that wipes the VM can't take it back.
- **The signed action log** (opt-in), checked by `metacomputer actions verify`.
- **Untrusted links and files:** `web open-untrusted` follows a link in a
  throwaway profile without clicking anything; suspicious files are read inside
  the VM.
- **Watching and taking over:** the live view (`metacomputer view --open` opens
  Screen Sharing with the password on your clipboard; `view` shows the SSH tunnel
  from another device), `inspect`, checkpoints and `reset`.
- **Handing it to you:** at a CAPTCHA or bot check, or a sign-in or 2FA prompt
  that's yours to do, Claude runs `metacomputer handoff` instead of trying to get
  past it. You get a notice, do that step in the live view and run
  `metacomputer handoff done`; Claude's actions wait until then.
- **Notifications and events:** `metacomputer notify` when Claude is blocked on
  you, and events to your phone (ntfy), Slack, an OpenTelemetry Collector or a
  SIEM, once you add a sink. The first blocked connection after a quiet spell
  reaches them within seconds.

**Getting better**
- **Lessons** from your own sessions (`metacomputer learn`, or
  `/metacomputer:learn` in a chat).
- **The benchmark:** 20 desktop tasks scored from the VM's real state, to measure
  one change at a time ([bench/README.md](bench/README.md)).
- **More than one computer:** `metacomputer vm create NAME` makes another VM
  beside the first, with its own settings, keys, ports and vault; `--vm NAME`
  picks it ([More than one computer](#more-than-one-computer)).

The [guide](docs/guide.md) covers each one with what to ask and the commands
behind it.

## Worked examples

Each one shows what you ask, what Claude does, and what you get back.

<details open>
<summary><b>A spreadsheet with a chart</b></summary>

**You ask:** "Here's sales.csv. Build a Calc workbook with totals by region and a
column chart, and save it as sales.ods and sales.xlsx."

**Claude:**
1. Says it's using the virtual computer, checks that no other session is using
   it (`metacomputer who`), and starts it if it's off.
2. Copies the file in with `metacomputer put sales.csv` (it lands in `~/Inbox`).
3. Opens it in LibreOffice Calc and adds the totals with formulas, working from
   the accessibility tree.
4. Inserts the chart through Calc's chart wizard, with real pointer clicks where
   the wizard needs them.
5. Saves both formats through the Save As dialog, typing the full path.
6. Checks the saved files through the shell: both exist, and the totals read back
   from the .xlsx match.

**You get:** both files copied to your Mac, the totals in the reply, and what it
checked.

</details>

<details>
<summary><b>Signing in with a saved password and a 2FA code</b></summary>

**Once, in your own terminal** (Claude can't run these, and never asks for a
password or code in the chat):

```bash
metacomputer secrets set demo --kind password --origin https://dashboard.example.com --username you@example.com --approval each
metacomputer secrets set demo-otp --kind totp --origin https://dashboard.example.com   # the 2FA seed
metacomputer secrets bind                 # pin the VM: check its fingerprint, type yes
metacomputer secrets unlock --for 1h      # keep this terminal open while Claude works
```

**You ask:** "Sign in to dashboard.example.com with my `demo` login and its 2FA
code, then download this month's report."

**Claude:**
1. Lists what's stored (`metacomputer secrets list`: names, sites and usernames,
   never values).
2. Opens the sign-in page in page-mode Chrome and finds the two fields.
3. Runs `metacomputer secrets login demo --username-selector '#email'
   --password-selector '#password'`. The vault checks, from the browser's own
   view, that the page is on the login's site and each field is the right kind,
   then types both values through a locked account in the VM. Claude gets back
   the fields filled, a masked username and the character counts.
4. Because the login asks first (`--approval each`; imported logins do too), your
   unlock terminal shows the request and who asked, and on a Mac with Touch ID you
   approve with your finger. Without it, uses go ahead once the vault is unlocked.
5. On the 2FA page, clicks into the code field and runs `metacomputer secrets
   fill demo-otp --field otp`; the vault works out the current code.
6. Checks the signed-in page with a fresh read, then downloads the report in page
   mode.

**You get:** the report on your Mac, and a line per use in the vault's audit log.
The password and code never pass through the chat, the transcript or the logs.
On the page, the password shows as dots; the username and the code are ordinary
text, visible in screenshots and recordings.

</details>

<details>
<summary><b>Claude inside the metacomputer</b></summary>

**Once, in your own terminal:** run `claude setup-token` for a subscription token (or
use an API key), and store it in the vault for `api.anthropic.com`:

```bash
metacomputer secrets set claude-sub --kind api --api-host api.anthropic.com   # paste the token
metacomputer secrets unlock --for 2h
```

**You ask:** "Give me Claude inside the metacomputer, show it on the desktop, and have
it write and test a small CSV parser in its own folder."

**Claude:**
1. Runs `metacomputer claude --token claude-sub --show`. That installs Claude Code in
   the VM if it's missing, gets a 1-hour token from the vault, sets Claude Code's
   first-run choices, and starts it in tmux, in a terminal on the VM's desktop. The
   token goes in `CLAUDE_CODE_OAUTH_TOKEN` (an API key stored with `--header 'x-api-key:
   {value}'` goes in `ANTHROPIC_API_KEY`), and `ANTHROPIC_BASE_URL` points at the
   vault's proxy inside the VM.
2. Sends the task with `metacomputer claude send "..." --wait`, which pastes it whole and
   returns once the inner Claude's screen holds still. It reads the screen with `claude
   read` and answers the inner Claude's questions with `claude keys`, within what you
   asked for.
3. Treats what the inner Claude says as the VM's text, and checks the result itself: the
   files through `metacomputer sh`, the tests by running them.
4. Ends with `metacomputer claude stop --revoke`, which closes the session and its token.

**You get:** the code and the test run, a terminal you could watch in the live view, and
a line per token in the vault's audit log. Your subscription token never entered the VM:
the inner Claude had a proxy token that worked only through your Mac, only for
api.anthropic.com, and only until it was revoked.

</details>

<details>
<summary><b>Sharing a Mac folder and processing its files</b></summary>

**You ask:** "Share my ~/work/invoices folder with the VM, read-only. Pull the
date, invoice number and total out of every PDF into a spreadsheet, and put it in
a folder I can open in Finder."

**Claude** (storage changes only because you asked for them):
1. Runs `metacomputer volume create invoices --folder ~/work/invoices` (read-only)
   and `metacomputer volume create handoff --folder ~/vm-handoff.noindex` (a
   read-write folder needs `.noindex` in its path, which keeps Spotlight out).
2. Adds `VOLUMES="invoices:/mnt/invoices,handoff:/data/handoff:rw"` to
   `config/local.env` and restarts the VM. At start, the VM proves it can't write
   to the read-only folder.
3. Runs one `metacomputer sh` script: `pdftotext` on each PDF, Python to pull the
   fields, openpyxl to write `/data/handoff/invoices.xlsx`.
4. Opens the workbook in Calc to check the layout, and checks a few rows against
   the PDFs.

**You get:** `invoices.xlsx` in `~/vm-handoff.noindex`, in Finder. A watcher on
your Mac gave it macOS's quarantine attribute, as it does everything the VM
writes there. Your invoices folder was never writable from the VM.

</details>

<details>
<summary><b>Checking a page in both browsers</b></summary>

**You ask:** "Does our pricing page look the same in Firefox and Chrome? Check it
at phone width too, and run an accessibility check."

**Claude:**
1. Runs `metacomputer web compare https://example.com/pricing`: the page in both
   browsers side by side, with differences in title, headings, links, controls
   and text.
2. Runs `metacomputer web responsive https://example.com/pricing --widths
   1920,1280,768,500` and looks at each screenshot for overflow and broken
   layout.
3. Runs `metacomputer web a11y https://example.com/pricing`: unlabelled controls,
   images without text alternatives, missing headings, vague link text.

**You get:** for each check, `report.md`, `report.json` and the images in a
results folder, and a summary of what differs and what breaks. Next time,
`web responsive --baseline` against this run adds similarity scores and
difference images. These checks need no model, so you can run them yourself too.

</details>

<details>
<summary><b>Opening a suspicious link</b></summary>

**You ask:** "Someone sent me this link: https://example.com/invoice. What does it
do, and is it safe to open?"

**Claude:**
1. Runs `metacomputer web open-untrusted https://example.com/invoice`. Inside the
   VM, `curl` follows the server's redirects.
2. The link then opens in a throwaway Chrome profile, watched for in-page
   redirects, new tabs and the final page. Nothing on the page is clicked or
   typed into.
3. The report notes any password fields (a sign the page wants your credentials)
   and any downloads it started, which stay in the VM. Claude treats all of it as
   untrusted data, never as instructions.

**You get:** where the link really goes, a screenshot of the final page, and
whether it asks for a password or starts a download. With an event sink set up, a
password prompt, a download or a redirect to another site is also sent as a
security event. Your Mac never opened the link.

</details>

<details>
<summary><b>Recording a task</b></summary>

**You ask:** "Record yourself filling in the sign-up form on our staging site in
Chrome, with the typing visible, so I can attach it to the issue."

**Claude:**
1. Runs `metacomputer task start signup-demo`, which makes a task folder in the VM
   and one on your Mac.
2. Starts Cua Driver's recorder into that folder before the first action.
3. Works in hybrid mode: it reads and checks the form through the page, but
   clicks and types key by key, so the video shows it.
4. Stops the recording and runs `metacomputer task finish`, which copies
   everything to your Mac and makes a contact sheet.

**You get:** a 1920×1080 MP4, a contact sheet of 12 frames, the screenshots and
click markers before and after each action, and `artifacts.json` listing it all.
For a video of the whole desktop instead, `metacomputer record start NAME` and
`record stop` capture it with ffmpeg, with the VM's sound if it has a sound card.

**If the task might turn on the VM** (a script from a repository you don't trust,
say), the whole-desktop recording is the one to run: it streams to your Mac as it
records (`RECORD_TO=host`, the default), so a script that wipes the VM's home
folder can't take it back. Cua Driver's per-task recording is written inside the
VM; while the task folder is open, your Mac copies each finished step every 15
seconds, and the newest step and the video at `task finish`.
[SECURITY.md](SECURITY.md#recordings) says what a hostile VM can and can't erase.

</details>

<details>
<summary><b>A game studio: STARBREAK, with art, sound and a trailer made in the VM's apps</b></summary>

**Once:** add the apps and a sound card in `config/local.env`, then rebuild the VM
with `metacomputer build --replace` (it replaces everything in the VM, so Claude
runs it only with your OK):

```bash
EXTRA_PACKAGES="inkscape audacity kdenlive frei0r-plugins mediainfo python3-pygame"
VIRTUAL_AUDIO=on
```

**You ask** (a chapter at a time, as STARBREAK was made): "Make a small space
shooter called STARBREAK in Python with pygame. Draw the ships and the planet in
Inkscape, make the sound effects and level them in Audacity, balance the waves in
a Calc sheet, playtest it, and cut a short trailer in Kdenlive. Record all of it."

**Claude:**
1. Writes the game's code on your Mac with its normal tools, and copies it into
   the VM.
2. **Art:** draws the ships, hazards and a ringed planet in Inkscape, with real
   pointer clicks on the canvas and keyboard shortcuts for its dialogs, and
   exports each PNG by typing its path.
3. **Sound:** makes the raw effects with a sound generator in Firefox, then trims,
   fades and levels them in Audacity. It can't hear, so it judges the sound from
   the waveform.
4. **Balance:** an autopilot plays many games headless through `metacomputer sh`;
   the numbers per wave go into a Calc sheet with a chart, and Claude tunes the
   waves table until its goals hold.
5. **Playtest:** plays the game on the desktop from screenshots and key presses
   (a pygame window isn't in the accessibility tree), notes what's wrong, and
   fixes each thing where it lives: code on the Mac, art in Inkscape, numbers in
   Calc.
6. **Trailer:** puts clips of play and the logo on Kdenlive's timeline, by pixel,
   since the timeline isn't in the accessibility tree, and exports the cut.
7. Records the whole desktop throughout (`metacomputer record`), with the VM's
   sound.

**You get:** the game with its art, sounds and balance sheet, a trailer, and a
recording of every step. STARBREAK, the game in the video above, was made this
way, over a series of recorded sessions: art, sound, balance, playtests and the
trailer.

</details>

## Prompt a computer into existence

A few more to try:

- "Convert every .docx in this folder to PDF and give me a zip."
- "Use my OpenAI key from the VM without the VM seeing it: run this script against the API there."
- "Give me Claude inside the metacomputer, signed in with my subscription, and show it on the desktop."
- "Build the site, serve it inside the VM, and click through sign-up and checkout in both browsers. Tell me what breaks."
- "Work through these 20 forms. Ping my phone if you hit a CAPTCHA or a sign-in, and when you're done."
- "Draw a flat two-colour logo for a coffee shop in Inkscape and give me the SVG and a 512 px PNG." (needs `inkscape`)
- "What do you keep getting wrong on the virtual computer? Show me the evidence."

[The full gallery](docs/guide.md#prompt-a-computer-into-existence) has more than
fifty, from office work and games to research and teaching it a new app.

## How it works

```text
Your Mac                                                         The VM: Ubuntu 24.04, XFCE
Claude Code
 └─ the metacomputer plugin: the skill + MCP tools
     └─ the hub: one per VM, every guard ───────────── SSH ──▶ Cua Driver ──▶ the desktop's apps
 metacomputer sh ───────────────────────────────────── SSH ──▶ bash, as the desktop user
 the secrets vault, unlocked in your terminal ──────── SSH ──▶ locked fill and proxy accounts
 volumes and folders you attach ─────────────────────────────▶ mounted at start
 cua-netgw: the VM's only network, to the public internet
 mcomp-vm: runs the VM on Apple's Virtualization framework
```

The plugin gives Claude Code the `metacomputer` skill, which says when a task
belongs on the VM and how to drive this desktop, and an MCP server whose tools
are Cua Driver's, running inside the VM. Every call goes through the hub, which
holds the only connection to the driver and applies the same guards to Claude
Code, `metacomputer call` and scripts. Files go over the shell channel, and
sign-ins and API keys through the vault. Each part exists on its own, mostly from
Cua; metacomputer builds them from pinned, verified sources with one command and
teaches Claude this particular desktop. [architecture.md](architecture.md) has
the design, and [docs/commands.md](docs/commands.md#the-claude-code-plugin) the
plugin's tool names and updates.

## Security in brief

An agent that uses a computer reads whatever is on the screen, and some of it is
written to steer it. No model is immune. So the VM holds none of your data unless
you attach some, reaches none of your machines, and gets your passwords and keys
through the vault rather than the chat.

**Always on:**
- **Walled off.** mcomp-vm, metacomputer's build of Lume, gives the VM no
  clipboard, audio or Rosetta from your Mac, and nothing is shared unless you
  attach it; `metacomputer status` checks from inside the VM.
- **Public internet only.** The gateway on your Mac refuses connections to your
  Mac, your LAN, your tailnet, cloud metadata and outbound mail, and logs them.
- **Locked down.** SSH and the live view listen on `127.0.0.1` only. SSH is
  key-only, a key per role, with a pinned host key, and the VM reads the keys from
  root-owned files the desktop user can't change; the desktop user has no sudo;
  and what the build downloads is pinned and checked.
- **The VM is treated as hostile.** What comes back is bounded and checked, files
  arrive as plain files, and its text is escaped before your terminal shows it.
- **Every guard on every path.** One session at a time, real input for Chrome, and
  bounded mode's limits, for Claude Code, `metacomputer call` and scripts alike.
  The coordinate guard refuses a pixel click after certain window-layout changes
  since its screenshot (the window resized, or covered by a dialog or another
  app); it compares window geometry, not content. A refusal is reported to you,
  never worked around.
- **Hidden text marked.** Page text a person likely can't see (tiny, off the page,
  no size on screen) or that addresses AI agents is marked in what Claude reads,
  so it tells you about it; a label, not a filter: colour and opacity aren't
  detected ([SECURITY.md](SECURITY.md#hidden-page-text)).

**The vault.** Nothing is used until you unlock it in your own terminal, which
shows every request. Imported secrets then ask before every use; secrets you add
with `set` don't (`--approval session`, the default) unless you give them
`--approval each` or unlock with `--confirm each`. In presence mode, the default
on a Mac with Touch ID, an approval takes Touch ID, your login password or the
passphrase, so a program can't approve by typing into the unlock terminal; with
typed approvals, one that can type there could. Passwords show as dots on the
page, but usernames and one-time codes are visible, in recordings too. Programs in
the VM that need an API key, Claude Code inside the VM included, get a short-lived
token for a proxy on your Mac instead; anything in the VM can use that token until
it ends or you revoke it, never the key, and renewing it can ask you first.

**Opt-in,** so out of the box metacomputer behaves the way Cua Driver does on its
own (`metacomputer security` shows what's on):

| Setting | What it adds |
| --- | --- |
| `ACTION_LOG=on` | A signed, hash-chained log of every tool call and shell run |
| `DRIVER_MODE=bounded` | Cua Driver does only what a manifest of apps, folders and sites allows; the shell turns itself off |
| `SSH_AUTH=cert` | A fresh key and a 5-minute certificate for every connection |
| An event sink | Events sent as they happen; with no sink, nothing leaves the Mac |

**The honest limit.** This keeps a well-behaved agent contained and makes a
misbehaving one visible. It doesn't make a deliberately hostile agent harmless:
everything runs as your macOS user, and an agent with a shell in the VM can get
at a password once it has been typed into a page. Someone who already controls
your Mac account can change the config, read the keys and drive the VM directly.
And the VM reaches the public internet by design, so anything you put in it could
be sent out. [SECURITY.md](SECURITY.md#what-holds-by-default) says what holds and
[what doesn't](SECURITY.md#what-doesnt-hold), and
[the guide](docs/guide.md#isolation-and-safety-in-detail) has every protection in
detail.

## Requirements

- A Mac with Apple silicon and macOS 14 or later; the secrets vault needs macOS 15.
- About 15 GB of free disk. The VM gets 4 vCPUs and 8 GB of memory (4 GB on a Mac
  with less than 16 GB).
- Homebrew and Apple's Command Line Tools; the install offers both when they're missing.
- Python 3.11 or later on the Mac. Apple's own `python3` is 3.9, so where that's the only one, the install
  adds Homebrew's.
- [Claude Code](https://claude.com/claude-code), for the plugin.
- Optional: Google Chrome in the VM, which means accepting
  [Google's terms](https://www.google.com/chrome/terms/).

## Everyday commands

`metacomputer` (alias `mcomp`, in `~/.local/bin`). Claude runs these for you;
they're here for when you want to yourself.

| Command | What it does |
| --- | --- |
| `start`, `stop`, `status` | Boot the VM (about 12 s), shut it down, check every layer |
| `who`, `release` | Which session is using the VM; hand it over |
| `view [--open]` | Watch or take over the live desktop (`--open`: Screen Sharing, password on the clipboard) |
| `handoff done`, `handoff cancel` | Hand the computer back to Claude after a step it handed you, or call it off |
| `sh -c 'CMD'`, `put FILE`, `get PATH` | Bash in the VM; copy files in and out |
| `web compare\|responsive\|a11y\|open-untrusted URL` | Browser checks that need no model |
| `record start NAME`, `record stop` | Record the whole desktop |
| `checkpoint NAME`, `restore NAME`, `reset` | Save the stopped VM, go back to a save or to the clean baseline |
| `vm list`, `vm create NAME`, `vm delete NAME`, `--vm NAME` | Your VMs; another one beside the first; delete one; act on one |
| `volume create\|list`, `refdisk create\|list` | Storage that outlives the VM |
| `secrets unlock`, `secrets list` | Unlock the vault, in your own terminal; what it holds, never values |
| `claude --token NAME`, `claude send\|read\|stop` | Claude Code inside the VM, signed in through the vault's proxy |
| `security`, `actions verify` | The optional hardening; check the signed action log |
| `events status`, `notify "TEXT"` | The event stream; tell your sinks Claude is blocked |
| `learn report\|review\|list` | Lessons from your own sessions |
| `test`, `help` | Six recorded GUI checks; every command |

`reset`, `restore`, `baseline` and deleting a checkpoint ask first, `vm delete`
wants the VM's name typed, and Claude runs them only with your OK. [docs/commands.md](docs/commands.md) has every
command and option.

## Configuration

`config/defaults.env` holds the pinned versions and defaults, and
`config/local.env` this Mac's settings (never committed; the installer writes it).
The settings people change most:

| Setting | For |
| --- | --- |
| `BROWSERS`, `EXTRA_PACKAGES`, `VIRTUAL_AUDIO`, `VM_CPU`, `VM_MEMORY` | What the VM has (next build) |
| `VOLUMES`, `REFDISKS` | Storage to attach (next start) |
| `RECORD_QUALITY`, `RECORD_TO`, `RECORD_DIR` | Desktop recordings |
| `ACTION_LOG`, `DRIVER_MODE`, `SSH_AUTH` | The optional hardening |
| `BROWSER`, `MODE` | The browser and mode Claude starts with (`metacomputer browser` sets them) |

Event sinks and the vault keep their own settings and change only from your
terminal. [docs/commands.md](docs/commands.md#configuration) lists every setting.

### More than one computer

```bash
metacomputer vm create demo          # config/vms/demo.env, then the build (asks first)
metacomputer --vm demo start         # or: export MCOMP_VM=demo
metacomputer --vm demo secrets init  # demo's own vault (in your own terminal)
metacomputer vm list                 # every VM: state, disk, ports, baseline
metacomputer vm delete demo          # the VM, its baseline and its settings, after you type its name
```

Each extra VM has one settings file, `config/vms/NAME.env`, with its own names and
ports; its SSH keys, runtime folders, vault folder and event spool are fixed by its
name, beside the main VM's, so `--vm NAME` needs nothing else. A bare `metacomputer` still means `config/local.env`'s VM,
and `MCOMP_CONFIG=FILE` still works. Commands that change a VM say which one first,
and anything unclear is refused rather than guessed: a checkout with no settings of
its own, with other VMs in the runner, makes you say which with `--vm`. `vm delete`
never takes the VM in use without `--force`, and keeps volumes and the vault folder
unless a flag asks ([guide](docs/guide.md#run-a-second-vm-side-by-side)).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `metacomputer status` shows a ✗ | Read that line. `metacomputer logs` shows the logs; `metacomputer restart` stops the VM and starts it again. |
| A Claude Code session has no metacomputer tools | Run `/mcp` and reconnect `plugin:metacomputer:metacomputer`, or restart Claude Code. `./install.sh --only-claude` sets it up again. |
| "Another session is using the virtual computer" | `metacomputer who` shows which. Ask before taking over, then `metacomputer release`. |
| "Paused: the user has the computer" | Claude handed you a step (`metacomputer handoff status`). Do it in the live view, then run `metacomputer handoff done` (or `handoff cancel`). |
| Screen Sharing rejects your password | It wants the live-view password, not your Mac's: `metacomputer view --open` puts it on the clipboard; paste it with Cmd+V. |
| A site's "Verify you are human" check keeps failing | Sites with bot checks may refuse the VM's automated browser, even when you tick the box yourself: page-mode Chrome is driven through DevTools, which they detect. metacomputer doesn't disguise it, and Claude hands such checks to you rather than trying to get past them. If one keeps failing, do that step on your own computer. |
| A sign-in looks different from usual (no code box, "check your email" but nothing arrives) | The site has likely flagged an automated browser: page-mode Chrome is driven through DevTools. Account pages start in human mode (the VM's Firefox, real clicks and keys); when a flow looks degraded anyway, Claude switches to human mode and tries once more. |
| Chrome ignores typing | `metacomputer input-reset`. |
| The vault says it's locked | Run `metacomputer secrets unlock` in your own terminal, and keep it open. |
| The install stops | Fix what it names and run it again; finished steps are skipped and it never deletes a VM. |

The guide has [the full table](docs/guide.md#troubleshooting) and
[known limitations](docs/guide.md#known-limitations), and
[docs/install.md](docs/install.md#troubleshooting) covers the install.

## Updating and uninstalling

```bash
cd ~/metacomputer && git pull && ./install.sh   # or run the install line again
```

An update rebuilds only what changed and leaves the VM alone. When the
[CHANGELOG](CHANGELOG.md) says a change takes effect on the next build, run
`metacomputer build --replace` when you're ready. A plugin installed from GitHub
updates with `claude plugin update metacomputer@metacomputer`.

```bash
metacomputer stop
~/metacomputer/install.sh --uninstall   # the command, the plugin and the vault program
```

It then prints the commands that remove the VM and its keys. Your vault, event
sinks and volumes stay until you delete them
([docs/install.md](docs/install.md#uninstalling)).

## Documentation

- [docs/install.md](docs/install.md): installing, options, what goes where, updating, uninstalling.
- [docs/guide.md](docs/guide.md): what to ask Claude Code, task by task, with the commands; the prompt gallery; isolation in detail; known limitations; troubleshooting.
- [docs/commands.md](docs/commands.md): every command and setting, and the plugin.
- [SECURITY.md](SECURITY.md): every control, what it protects against, where it ends, the vault in full, and how to report a vulnerability.
- [architecture.md](architecture.md): the design, pinned versions, the hub, the network, recording and the event spool.
- [docs/storage.md](docs/storage.md): volumes, folders, S3 and reference disks.
- [docs/shell.md](docs/shell.md): `metacomputer sh` and how it treats the VM.
- [docs/events.md](docs/events.md): the event stream, every event, wire formats and receivers.
- [docs/learn.md](docs/learn.md): lessons from your own sessions.
- [docs/moving-from-spaces.md](docs/moving-from-spaces.md): coming from Cua Spaces' Keyvault.
- [bench/README.md](bench/README.md): the benchmark, with each cited run's records in [bench/runs/](bench/runs/).
- [TESTING.md](TESTING.md): every test and how to run it.
- [skills/metacomputer/](skills/metacomputer/): the skill itself, with the desktop and browser rules verified on this VM in [DESKTOP.md](skills/metacomputer/DESKTOP.md) and [BROWSER.md](skills/metacomputer/BROWSER.md).
- [docs/chrome-investigation.md](docs/chrome-investigation.md): how Chrome was added and why the input guard exists.
- [CHANGELOG.md](CHANGELOG.md): what changed.

## Contributing

Issues and pull requests are welcome. Before you change code:
- Read [architecture.md](architecture.md) and [CLAUDE.md](CLAUDE.md), which describes the repository's conventions.
- Settings belong in `config/defaults.env` and `config/local.env`. Don't hard-code a machine's names, paths or hosts.
- The optional hardening stays off by default, so metacomputer behaves like Cua Driver out of the box.
- Run the tests in [TESTING.md](TESTING.md) that cover your change, and `scripts/check-public.sh -a`, which checks the tree and history for anything personal. For a change to how Claude works the desktop, the benchmark shows whether it helps.

Report security problems privately, as [SECURITY.md](SECURITY.md#reporting-a-vulnerability)
describes. Bugs in Cua Driver or Lume themselves belong upstream at
[trycua/cua](https://github.com/trycua/cua).

## Credits

metacomputer is built on [Cua](https://github.com/trycua/cua) by Cua AI, Inc.
(MIT): Cua Driver runs the desktop, Lume runs the VM, and `cua-netgw`, built from
Cua's source, is the network gateway (it uses gvisor-tap-vsock, Apache-2.0).
[mcomp-vm](https://github.com/metavert-io/metacomputer-vm), metacomputer's build
of Lume, adds the host-sharing flags; like metacomputer, it's made by Metavert,
not by Cua, and isn't endorsed by them. The shell channel's routing, the hub and
the benchmark's method come from Microsoft Research's
[Hybrid Computer Use](https://github.com/microsoft/Hybrid-Computer-Use) work.
`tools/qcow2raw` uses Lima's
[go-qcow2reader](https://github.com/lima-vm/go-qcow2reader) (Apache-2.0). The VM
runs Ubuntu, Firefox, LibreOffice and, optionally, Google Chrome.
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists them with their licenses;
the Claude Code plugin carries the same notices and license.

## License

MIT License, © 2026 Metavert LLC. See [LICENSE](LICENSE).
