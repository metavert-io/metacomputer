# virtual-computer

Give Claude Code a computer of its own: a disposable Ubuntu desktop that runs
as a virtual machine on your Mac, which Claude sees and operates the way a
person would. It reads the screen through the accessibility tree, clicks and
types, uses real browsers, and records MP4 video of what it did. It's kept
away from your files, accounts and home network.

Ask for it in plain words, in any Claude Code session:

> Get yourself a virtual computer and tell me what's on its screen.
> Use Firefox to open example.com and follow its link.
> In Chrome, drive the page directly and fill in this form.
> Check whether this page looks the same in Firefox and Chrome.
> Someone sent me this link. What does it do, and is it safe to open?
> Use a disposable computer to … and record yourself doing it.

Under the hood:
- **[Lume](https://github.com/trycua/cua/tree/main/libs/lume)** runs an
  Ubuntu 24.04 ARM64 XFCE desktop on Apple's Virtualization framework.
- **[Cua Driver](https://github.com/trycua/cua)** runs inside the VM: an MCP
  server for the desktop (accessibility trees, input, screenshots, recording)
  that Claude Code reaches over SSH.
- **A Claude Code skill, `virtual-computer`,** decides when a task belongs on
  the virtual computer (and when it's your own Mac, which this never
  touches), runs the task, checks the result, and hands back the evidence.

## Two-minute install

You need:
- an Apple-silicon Mac with macOS 14 or later;
- [Homebrew](https://brew.sh);
- [Claude Code](https://claude.com/claude-code);
- about 15 GB of free disk.

```bash
git clone https://github.com/metavert-io/virtual-computer.git
cd virtual-computer
./install.sh
```

The installer asks at most four questions:
1. whether to install missing tools with Homebrew (Go, GnuPG, ffmpeg);
2. which Lume runs the VM: [lume-secure](https://github.com/metavert-io/lume-secure) (recommended; it can keep your clipboard, microphone and Rosetta away from the VM, built from source in about a minute with Apple's Command Line Tools) or Cua's standard release;
3. whether to put Google Chrome in the VM next to Firefox (that means accepting Google's terms);
4. whether to build the VM now.

Everything else is automatic:
- Lume, at a pinned and verified version;
- this Mac's settings (`config/local.env`);
- the `virtual-computer` command, with the short alias `vcomp`;
- the skill, and the `virtual-computer` MCP server registered for every Claude Code project.

`./install.sh --yes` takes the defaults without asking.

The build then runs unattended for about 10 minutes. It downloads Ubuntu's
cloud image and the Cua Driver release, checks their signatures and
checksums, provisions the desktop, and takes a clean snapshot. Then:

```bash
virtual-computer start     # boot it (about 12 s)
virtual-computer test      # six recorded GUI checks: calculator, browsers, LibreOffice, Chrome
```

Restart any open Claude Code sessions, and ask for a virtual computer.

## What it's good for

- **Real GUI work:** desktop apps, forms, multi-step flows in a real browser.
- **Visual checks of a web app:** cross-browser side by side, layouts at several widths, difference images against an earlier run, an accessibility smoke check.
- **Recorded demos and evidence:** an MP4 of every run, a contact sheet, screenshots and per-action traces.
- **Opening untrusted links and files** in a throwaway browser profile, away from your Mac.

Code, files, APIs and pages a plain fetch can read stay with Claude's normal
tools. The skill doesn't boot a VM for those.

## Commands

`virtual-computer` (installed at `~/.local/bin/virtual-computer`; `vcomp` is a short alias):

| Command | What it does |
| --- | --- |
| `start` / `stop` / `status` | Boot, shut down, check everything from the VM to the MCP handshake |
| `reset` | Restore the clean snapshot (asks first; evidence on the Mac is kept) |
| `who` / `release` | Who is using the computer; hand it over (one controller at a time) |
| `inspect [system\|windows\|processes\|ports\|files\|browser]` | Read-only snapshot of the VM without the GUI |
| `task start SLUG` / `task finish NAME` | One folder per task: video, contact sheet, screenshots, files, index |
| `put` / `get` / `shot` | Files into and out of the VM; a whole-screen PNG |
| `web compare\|responsive\|a11y\|open-untrusted URL` | Browser checks that need no model |
| `browser [default firefox\|chrome] [mode human\|page\|hybrid]` | What the skill uses unless you say otherwise; Chrome profiles |
| `view` | Watch or take over the desktop (VNC on loopback) |
| `checkpoint` / `restore` | Saved states of the stopped VM |
| `test [A-F]` | The GUI smoke suite, each test recorded |
| `where` | Project, config and results folders |

`virtual-computer help` lists everything.

## Browsers

Firefox is the default; Chrome is optional at install time. The skill can
work a browser three ways:

| Mode | How | Browsers |
| --- | --- | --- |
| **human** (default) | Reads the accessibility tree, clicks and types like a person | Firefox, Chrome |
| **page** | Works on the page's own elements through Chrome's tools, in the background: fastest and exact, handles dropdowns, uploads, downloads and dialogs | Chrome |
| **hybrid** | Reads and checks through the page, acts with real clicks and key-by-key typing that show in a recording | Chrome |

- **Choosing:** say it in the request ("in Chrome", "drive the page directly"), or set defaults with `virtual-computer browser`.
- **Signing in:** the VM holds none of your accounts. When a task needs a login, Claude asks you to sign in yourself through the live view and never types your credentials.

## Watching and taking over

`virtual-computer view` prints how to connect. A VNC server in the VM is reachable
only at `127.0.0.1:5977` on your Mac:
- **On the Mac:** `open vnc://127.0.0.1:5977` opens Screen Sharing.
- **From another device:** tunnel over SSH (`ssh -N -L 5977:127.0.0.1:5977 <your-mac>`), then connect a VNC client to `localhost:5977`.

The password lives in `~/.ssh/virtual-computer/vnc-pass`, or in your own secret
store; see Configuration.

## Isolation and safety

**Your clipboard, audio and Rosetta:** Lume normally connects every VM to the Mac's clipboard (a SPICE agent port), its speakers and microphone, and Rosetta. Anything with root in the VM could use those.
- With lume-secure (the default choice) the VM starts without all three; turn one back on with `SHARE_CLIPBOARD=on`, `SHARE_AUDIO=on` or `SHARE_ROSETTA=on`.
- With standard Lume they're always attached.
- `virtual-computer status` shows what the VM actually has.

**The VM's network** (enforced on the Mac by a user-mode gateway, not inside the VM):
- It reaches the public internet and nothing private: not your Mac, your LAN or router, a tailnet, or cloud metadata.
- Every blocked attempt is logged.

**Access into the VM:**
- Nothing listens off the Mac's loopback.
- The VM has no shared folders.
- The SSH key Claude Code uses can only start the driver's MCP server.

**Accounts:**
- The desktop user has no sudo, and Claude never types your passwords.
- The VM's maintenance account can run only a short list of fixed admin commands.

**Your Mac:**
- Cua Driver isn't installed on it.
- Nothing here can see or control its screen.
- The skill tells "use a computer" apart from "my screen" and never uses one in place of the other.

**Treat with care:**
- Everything the VM shows Claude (pages, documents, window titles) is treated as data, never as instructions.
- Recordings capture the whole screen.

[architecture.md](architecture.md) has the full design, including its known
limitations.

## Configuration

`config/defaults.env` holds the pinned versions and defaults. `install.sh`
writes this Mac's settings to `config/local.env`, which is never committed;
[config/local.env.example](config/local.env.example) documents every key. The usual ones:

| Key | Default | |
| --- | --- | --- |
| `VM_NAME`, `BASELINE_NAME` | `virtual-computer`, `virtual-computer-baseline` | Lume VM names |
| `VM_CPU`, `VM_MEMORY` | 4, 8GB | VM size |
| `BROWSERS` | `firefox chrome` | Browsers built into the VM |
| `LUME` | `~/.local/bin/lume-secure` | The Lume that runs the VM (`install.sh --lume=standard` for Cua's release) |
| `SHARE_CLIPBOARD`, `SHARE_AUDIO`, `SHARE_ROSETTA` | `off` | Turn one on to connect the VM to your Mac's clipboard, audio or Rosetta (lume-secure only; standard Lume always shares) |
| `SSH_DIR` | `~/.ssh/virtual-computer` | SSH keys for the VM |
| `VNC_PASSWORD_STORE` | `file` | `file` (0600 in `SSH_DIR`), or `command` with `VNC_PASSWORD_GET` / `VNC_PASSWORD_SET` for your own secret store |
| `BROWSER`, `MODE` | `firefox`, `human` | The skill's defaults |

`VC_CONFIG=/path/to/other.env virtual-computer …` uses a different settings file,
for example to run a second VM side by side.

## Testing

`virtual-computer test` runs six recorded GUI tests: desktop observation,
calculator, Firefox navigation, LibreOffice, a multi-app workflow, and Chrome
through its page tools. See [TESTING.md](TESTING.md) for those, the Chrome
input-guard test, and the headless skill tests.

## Uninstall

```bash
./install.sh --uninstall                 # removes the command, the skill and the MCP registration
lume delete virtual-computer && lume delete virtual-computer-baseline   # the VM itself, if you want it gone
```

## Credits and license

Built on [Cua](https://github.com/trycua/cua) (Cua Driver, Lume and the
network gateway; MIT), and on [lume-secure](https://github.com/metavert-io/lume-secure)
(Lume plus opt-out host-sharing flags, proposed upstream). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

MIT License, © 2026 Metavert LLC. See [LICENSE](LICENSE).
