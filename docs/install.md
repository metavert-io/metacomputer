# Installing metacomputer

metacomputer runs on a Mac with Apple silicon (M1 or later) and macOS 14 or
later. The secrets vault (`metacomputer secrets`) needs macOS 15. You need about
15 GB of free disk (10 GB where Lume keeps VMs, normally `~/.lume`, 2 GB for
the checkout, and 1 GB each in your home folder and the temporary folders;
the installers check each disk they write to), and Claude Code if you want
Claude to use the virtual computer (you almost certainly do).

- [The 5-minute install](#the-5-minute-install)
- [From Claude Code first (the plugin)](#from-claude-code-first-the-plugin)
- [By hand: clone and ./install.sh](#by-hand-clone-and-installsh)
- [Options](#options)
- [What goes where](#what-goes-where)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)

## The 5-minute install

Paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/metavert-io/metacomputer/main/get.sh | bash
```

You can read the script before you run it: download it, read it, then run
`bash get.sh`. The top of the file says what it does. It never asks for or
stores your password, and it never runs `sudo` itself.

### What happens

| Step | What it does | What you do | How long |
| --- | --- | --- | --- |
| 1. Check this Mac | Apple silicon, macOS version, free disk, memory, network | nothing | seconds |
| 2. Prerequisites | Homebrew and Apple's Command Line Tools, only if they're missing (see below) | answer one question, then Homebrew asks for your password | none if you have them; 5-15 minutes on a new Mac |
| 3. Download | `git clone` into `~/metacomputer` (about 3 MB), at the latest release, or `main` before the first release | nothing | seconds |
| 4. Install | `./install.sh --yes`: Go, GnuPG and ffmpeg from Homebrew if they're missing, the VM runner (mcomp-vm) and the secrets vault built from source, this Mac's settings, the Claude Code plugin, then the VM | nothing | about 5 minutes on a fast connection |
| 5. Check | a VM it just built: starts it, runs `metacomputer status` and a short self-test. A VM you already had: only reads (`metacomputer status` and a read-only look at its windows; started only if it's stopped), so nothing open in it is closed or typed into | nothing | under half a minute |

Then it prints three prompts to try in Claude Code. The VM is left running;
`metacomputer stop` stops it, and Claude starts it again when it needs it.

### What you click or answer

On a Mac that already has Homebrew and the Command Line Tools: nothing. You
paste one line and wait.

On a new Mac:

1. **Homebrew** (if it's missing): get.sh asks `Run Homebrew's installer now? [Y/n]`.
   Press Enter. get.sh downloads Homebrew's official installer completely
   before running it. Homebrew's installer then shows what it will do, asks you
   to press Return, and asks for your Mac password. It needs an administrator
   account. It also installs Apple's Command Line Tools if they're missing, so
   you don't see Apple's dialog.
2. **Apple's Command Line Tools** (if they're still missing): a macOS dialog
   asks to install the "command line developer tools". Click **Install**, then
   **Agree**. get.sh waits, checking every 10 seconds, and carries on by itself.

Google Chrome goes into the VM next to Firefox. Installing it means accepting
[Google's terms](https://www.google.com/chrome/terms/); add `--no-chrome` to
leave it out (`curl … | bash -s -- --no-chrome`).

### How long it really takes

Measured on 2026-11-18 by running get.sh as a new user would, in an empty home
folder with its own Claude Code settings, so nothing was cached: no Go or Swift
build caches, no downloads, no VM. The Mac already had Homebrew, its packages
and the Command Line Tools (see below for a Mac without them). The connection
was fast: the Ubuntu image (about 600 MB) arrived in about 15 seconds. Three runs
took 5 min 5 s, about 5 min 15 s (not counting a 30-second wait that was fixed
after it) and 5 min 40 s. The stages of the 5 min 5 s run:

| Stage | Time |
| --- | --- |
| Checks (this Mac, prerequisites) | about 1 s |
| Download metacomputer (git clone) | about 1 s |
| Homebrew packages | 0 (already installed) |
| VM runner: download and build mcomp-vm | about 1 min 40 s |
| Settings and the Claude Code plugin | about 2 s |
| Secrets vault build | about 15 s |
| VM build: Go tools, downloads, Ubuntu's image and packages, provisioning, clean snapshot | about 2 min 45 s (provisioning inside the VM: about 2 min 10 s) |
| Health check: start, status, self-test | about 20 s |
| **Total** | **about 5 minutes**, with no questions |

So on a Mac with the prerequisites in place it is a 5-minute install, and about
a minute of that is yours (pasting the command and reading the result); the
other four run unattended.

The downloads add up to about 1.5 GB: Ubuntu's cloud image (about 600 MB), the
Ubuntu packages installed inside the VM (about 800 MB), and the Go and Swift
code the build needs (about 200 MB). On a slower connection the VM build takes
longer; at about 50 Mbit/s, expect 10-15 minutes in all.

On a new Mac, add these (estimates, not measured here):

- Homebrew's installer, with the Command Line Tools: 5-15 minutes, most of it
  Apple's download.
- Go, GnuPG and ffmpeg from Homebrew (about 35 packages with their libraries):
  2-5 minutes on a fast connection.

That makes 15-30 minutes on a new Mac, of which about 5 minutes need you: the
Homebrew question, Return and your password, and maybe Apple's dialog.

The install uses about 6 GB of disk: about 4-5 GB for the VM and its clean
snapshot, which share most of their blocks, and about 1 GB of downloads and
build caches. The Command
Line Tools and the Homebrew packages take up to 4 GB more if they're new. The
VM's disk grows as you use it, up to 50 GB.

### If it stops

Every failure says what went wrong and what to do. Fix that and run the same
command again. Every finished step is skipped: the checkout is kept, the
mcomp-vm and vault builds are reused, and the Ubuntu image download resumes.

Running it again never deletes a VM. If a VM build was interrupted, or a VM is
there without its clean snapshot, the installer stops and says so (with
`--no-build` it just says so and succeeds). A VM without its snapshot may hold
your work. It's replaced only when you add `--replace-vm` (`curl … | bash -s --
--replace-vm`, or `./install.sh --replace-vm`); `--yes` never implies it.

One install runs at a time for your user account, whatever checkout or home
folder it uses. Changes to a VM run one at a time too: a build, `reset`,
`restore`, `baseline` and `checkpoint` (saving or deleting one) all take the
same per-VM lock. A second one waits 10 seconds, then stops without changing
anything and says what's running. A lock lasts as long as the last process
doing the work, and nothing stale survives once they've all exited. Every
install also rebuilds the host tools (`cua-netgw`, `qcow2raw`) when they don't
match their pinned sources, whether or not it builds the VM. See
[Troubleshooting](#troubleshooting).

## From Claude Code first (the plugin)

If you install the plugin before anything else, you can finish the setup
without leaving Claude Code:

```text
/plugin marketplace add metavert-io/metacomputer
/plugin install metacomputer@metacomputer
```

The plugin brings the skills and the MCP tools, but not the VM. Ask Claude to
use its virtual computer, or to set up metacomputer. Its skill sees that
nothing is set up, says what setup does and asks you. When you say yes, it
runs the plugin's own `metacomputer setup --yes`, which is the same get.sh as
above, shipped inside the plugin. Nothing is downloaded and run from
elsewhere. You can also run it yourself in Terminal; the plugin's command
prints its full path when it finds nothing set up:

```bash
~/.claude/plugins/cache/metacomputer/metacomputer/<version>/bin/metacomputer setup
```

Run that way, it shows the plan and asks `Set up metacomputer now? [Y/n]`.
With no terminal to ask on (Claude's shell), it only explains, unless it's
given `--yes`.

Two limits:

- **Homebrew** needs your password, so if Homebrew is missing, run the setup
  command in Terminal rather than through Claude.
- `setup` takes only `--dir DIR`, `--no-chrome`, `--no-build`, `--yes` and
  `--dry-run`. It can't be pointed at another repository or script, and it
  runs only the project's own code, with settings that only set values. It
  never deletes anything in an existing checkout. A checkout in that folder is
  used only if it's a clean checkout of github.com/metavert-io/metacomputer on
  its default branch that holds nothing but its tracked files, logs, run state,
  verified downloads (`.cache/ubuntu-*`, `cua-driver-*`, `mozilla`, `google`,
  and in `.cache/rclone-v*` rclone's signed sums, its keys and the pinned zip)
  and a `config/local.env` whose settings only set values (storage's
  `STORAGE_DIR`, `VOLUMES`, `REFDISKS`, `S3_SYNC_INTERVAL` and
  `S3_BACKUP_MAX_GB` among them). Refused: git settings beyond a plain
  clone's, git hooks, files git doesn't know, build outputs (`vendor/`,
  `.cache/cua-src`, `.cache/mcomp-vm`, `__pycache__`, and the rclone program
  unpacked from its zip, which is checked only against a digest kept beside
  it: delete `.cache/rclone-v*/rclone`, and storage unpacks it again from the
  checked zip when it needs it), any other `.cache` entry, a Lume storage
  location inside the checkout,
  and settings that choose a command, a source or a pin (`*_GET`, `*_SET`,
  repositories, tags, commits, URLs, checksums, `RCLONE_*`, `LUME` pointing
  anywhere but the runners install.sh installs). Then you run that checkout's
  `./install.sh` yourself if you trust it, or set up a fresh copy with
  `--dir` and a new folder. From the plugin, install.sh builds the host tools
  and mcomp-vm from new temporary clones (`--fresh-sources`), leaving the
  checkout's caches alone.
- Run directly (not from the plugin), get.sh uses a checkout with local
  changes, from another origin or on a branch of its own as it is, without
  updating it or checking it against the repository, and says so: that's
  trusting the code in that folder. Only a clean checkout is updated and
  checked against the commit the repository names.
- `$MCOMP_HOME` and `~/.local/bin/metacomputer` are trusted: once either names
  a checkout, the plugin hands every command to it, `setup` included (there,
  `setup` is that checkout's `./install.sh`, which asks first, or needs
  `--yes` when there's no terminal).

Once a checkout exists, the plugin's copy of `bin/metacomputer` hands every
command to it (through `~/.local/bin/metacomputer`, or `$MCOMP_HOME`).

## By hand: clone and ./install.sh

```bash
git clone https://github.com/metavert-io/metacomputer.git ~/metacomputer
cd ~/metacomputer
./install.sh
```

This is what get.sh does after its prerequisite step. `./install.sh` needs
Homebrew if any of git, curl, Go, GnuPG, ffmpeg or Python 3 is missing. It
needs Apple's Command Line Tools to build mcomp-vm and the vault. It shows what
it will do and asks once, `Go ahead? [Y/n]` (Enter means yes). `--yes` skips
the question.

```text
==> [1/7] Checking this Mac  (a few seconds)
  ✓ macOS 15.1 on Apple silicon
  ✓ 212 GB free, 16 GB of memory
  ✓ network: the download sites are reachable
  This run will:
    - build mcomp-vm, the VM runner, from source  (about 1 minute; --lume=standard: Cua's release instead)
    - write this Mac's settings to ~/metacomputer/config/local.env: Firefox and Google Chrome in the VM (…)
    - link the metacomputer command and add the Claude Code plugin (skills and tools in every project)
    - build the secrets vault (metacomputer secrets)  (about 1 minute; skipped if it's up to date)
    - build the VM metacomputer: about 1.5 GB of downloads, then a few minutes unattended
  Already done: tools.
  Go ahead? [Y/n]
```

Each stage then prints its number, an estimate, and how long it took. At the
end, a summary lists the time of each longer stage.

## Options

**get.sh** (after `| bash -s --`, or `bash get.sh …`):

| Option | Effect |
| --- | --- |
| `--dir DIR` | Where the checkout goes. Default: the checkout `~/.local/bin/metacomputer` already runs, else `~/metacomputer` |
| `--ref REF` | A tag or branch instead of the latest release |
| `--repo URL` | Clone from somewhere else: a fork, a mirror, a local clone |
| `--no-chrome` | Firefox only in the VM |
| `--no-build` | Everything but the VM (later: `metacomputer build`) |
| `--replace-vm` | Delete the VM and its clean snapshot and build them again: for a build that didn't finish, or a VM without its snapshot. Never implied by `--yes`, and not available from the plugin's `setup` |
| `--confirm` | Explain the plan and ask before changing anything (what `metacomputer setup` uses) |
| `-y`, `--yes` | Answer yes, including running Homebrew's installer (which still asks for your password itself) |
| `--dry-run` | Check this Mac and print what it would do; change nothing |
| `-h`, `--help` | The options |

**install.sh**:

| Option | Effect |
| --- | --- |
| `--yes` | No question: the defaults (mcomp-vm, Chrome included, build now) |
| `--no-chrome` | Firefox only in the VM |
| `--lume=mcomp-vm` / `--lume=standard` | The VM runner: mcomp-vm (the default; built from source, can keep your clipboard, audio and Rosetta away from the VM) or Cua's notarized Lume release (always shares them) |
| `--no-build` | Set everything up but don't build the VM yet |
| `--replace-vm` | Delete the VM and its clean snapshot and build them again (otherwise a VM is never replaced) |
| `--fresh-sources` | Build the host tools and mcomp-vm from new temporary clones (the tools even if they're up to date), leaving the checkout's `.cache` alone; what setup from the plugin uses |
| `--no-claude` | Don't touch Claude Code |
| `--no-plugin` | Claude Code without the plugin: a skill link and a user-scope MCP server (tools `mcp__metacomputer__*`) |
| `--only-claude` | Only the Claude Code step |
| `--uninstall` | Remove the command, the plugin and the vault program (the VM stays) |

`MCOMP_CONFIG=FILE ./install.sh` writes and reads that settings file instead of
`config/local.env`, as every other command does. That's how you set up a
second VM beside the first.

**metacomputer setup**: in a checkout, the same as `./install.sh` (with its
options). From the plugin with no checkout yet, get.sh with `--confirm` (see
above).

## What goes where

| What | Where |
| --- | --- |
| The checkout: scripts, settings, logs, build caches | `~/metacomputer` (or `--dir`); settings in `config/local.env` |
| The commands | `~/.local/bin/metacomputer` and `~/.local/bin/mcomp` (links into the checkout) |
| The VM runner | `~/.local/bin/mcomp-vm`, `~/.local/share/mcomp-vm` (or `~/.local/bin/lume` for standard Lume) |
| The VM and its clean snapshot | `~/.lume/metacomputer`, `~/.lume/metacomputer-baseline` (Lume's storage location) |
| The VM's SSH keys | `~/.ssh/metacomputer` |
| The live-view (VNC) password | your login Keychain, item `metacomputer-vnc` (account: the VM's name). Read it with `security find-generic-password -s metacomputer-vnc -a metacomputer -w`. It's written with `security -i` fed on stdin, so it's on no command line, and it reaches the VM on ssh's stdin, never through an environment. If the Keychain is locked or missing during the build (some SSH sessions), or doesn't answer within 20 seconds, it goes to `~/.ssh/metacomputer/vnc-pass` (0600, written to a new file and renamed into place, never through a link) instead, and the build says so |
| The secrets vault program | `~/.local/share/metacomputer/bin/mcomp-vault`; your vault in `~/Library/Application Support/metacomputer` |
| The Claude Code plugin | Claude Code's plugin settings (marketplace `metacomputer`: the checkout) |
| Go's build cache and modules | `~/go` and `~/Library/Caches/go-build` (Go's own defaults) |
| Homebrew packages | Homebrew's prefix (`/opt/homebrew`) |
| The install and build locks | `metacomputer-locks` in your per-user temporary folder (`getconf DARWIN_USER_TEMP_DIR`) |
| Volumes and reference disks, if you make any ([storage.md](storage.md)) | `STORAGE_DIR` (default `~/.local/share/metacomputer`), their records in `~/.local/share/metacomputer/storage`; rclone for S3 volumes in the checkout's `.cache/rclone-v*`. The installer neither needs nor touches them |

## Updating

```bash
cd ~/metacomputer && git pull && ./install.sh
```

Or run get.sh again: it updates the checkout to the latest release, then runs
`./install.sh`, which rebuilds only what changed (a new mcomp-vm pin, a changed
vault). An update doesn't rebuild the VM, and its check only reads from the VM
(no self-test), so windows and unsaved work in it are left alone. When the
CHANGELOG says a change takes effect on the next build, rebuild with
`metacomputer build --replace` when you're ready (it replaces the VM and its
clean snapshot). get.sh leaves a checkout
alone if it has local changes or is on a branch of your own. A plugin installed
from GitHub updates with `claude plugin update metacomputer@metacomputer`.

## Uninstalling

```bash
metacomputer stop
~/metacomputer/install.sh --uninstall   # the command, the alias, the plugin and the vault program
```

`--uninstall` then prints the commands that remove the rest for the runner you
use. For the default mcomp-vm and VM name:

```bash
mcomp-vm delete metacomputer
mcomp-vm delete metacomputer-baseline      # and any checkpoints `mcomp-vm ls` shows
security delete-generic-password -s metacomputer-vnc -a metacomputer   # the live-view password
rm -r ~/.ssh/metacomputer
rm -rf ~/.local/share/mcomp-vm ~/.local/bin/mcomp-vm   # last: the deletes above need it
rm -rf ~/metacomputer                      # the checkout, with its recordings and results
```

Your secrets vault (`~/Library/Application Support/metacomputer`), event
sinks (`~/.config/metacomputer`) and any volumes and reference disks
(`metacomputer volume list`, `refdisk list`; delete them with `volume delete`
and `refdisk delete` before removing the checkout) stay until you delete them. Go's module cache
is read-only on purpose; `go clean -modcache` removes it. Homebrew packages stay
installed (`brew uninstall ffmpeg go gnupg` if nothing else uses them).

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `needs a Mac with Apple silicon` | The VM is an ARM64 Ubuntu desktop run by Apple's Virtualization framework; Intel Macs can't run it. |
| `this terminal runs under Rosetta` | Your Terminal is set to open with Rosetta. In Finder, Get Info on Terminal and untick "Open using Rosetta", then run it again. |
| `needs macOS 14 or later` | Update macOS (System Settings > General > Software Update). |
| `only N GB free on the disk that holds …` | It checks every disk it writes to: where Lume keeps VMs (10 GB), the checkout (2 GB), your home folder and the temporary folders (1 GB each), and Homebrew's prefix when tools are missing (2 GB), adding them up when they share a disk. Free some space there. `--no-build` sets up everything else for now; `metacomputer build` builds the VM later. |
| `couldn't tell where Lume will keep the VM` | Lume's settings didn't name one default storage location (`mcomp-vm config get`, or `~/.config/lume/config.yaml` before the runner is installed). The installer doesn't guess; fix the settings, then run it again. |
| `another metacomputer install is running` or `another build of the VM … is running` | Only one install runs at a time, and one build of each VM. Wait for the other one to finish (the message names it), then run it again. Nothing was changed. |
| `… can be changed by someone else; refusing to keep the live-view password there` | The folder for the live-view password (`~/.ssh/metacomputer`, or `SSH_DIR`), or a folder above it, can be written by other users. Use a folder only you can change. |
| `couldn't list the VMs` | The VM runner's `ls` failed, so the installer can't tell whether your VM exists, and it changes nothing. Check that `mcomp-vm ls` works, then run it again. |
| `an earlier build of … didn't finish` | The VM build was interrupted. Everything else is set up. `./install.sh --replace-vm` (or `metacomputer build --replace`) deletes the unfinished VM and builds it again; finished downloads are kept. |
| `… exists without its clean snapshot` | The VM is there, but not its baseline, and nothing says a build was under way, so it may hold your work and is left alone. To keep it, make its current state the baseline: `metacomputer baseline`. To start over: `./install.sh --replace-vm`. |
| `… is missing, but its clean snapshot … is there` | Restore the VM from the snapshot: `metacomputer reset`. Or build both again: `./install.sh --replace-vm`. |
| `isn't a plain checkout of the project` | You ran setup from the plugin with a folder that holds another checkout, local changes, a branch of its own, build outputs, other files, or settings that choose a command, a source or a pin. Nothing in it was changed. Use a new folder (`setup --dir …`), or run that checkout's `./install.sh` yourself. |
| `another build or change of the VM … is running` | A build, `reset`, `restore`, `baseline` or `checkpoint` of the same VM is running (the message names it). Wait for it to finish, then run the command again. |
| `couldn't list … releases` or `not the commit … named` | get.sh couldn't ask the repository which release to install, or the checkout didn't match the commit the repository named (a push or a moved tag in between). Nothing is installed from it; run get.sh again. |
| `can't reach github.com` or `Ubuntu's image server` | Check the network, and any proxy, VPN or firewall that blocks github.com or cloud-images.ubuntu.com. |
| `couldn't download metacomputer` | Check the network, as above. Cloning a private fork or mirror (`--repo`) needs a login: `gh auth login`, and get.sh then uses the GitHub CLI's credentials. |
| Homebrew's installer fails | It needs an administrator account and the internet. Install it from [brew.sh](https://brew.sh), then run get.sh again. |
| `Homebrew couldn't install …` | Run `brew update`, then `brew doctor`, and fix what it names. Then run it again. Installing the tools another way also works; they only need to be on `PATH`. |
| The Command Line Tools dialog never appeared, or you're on SSH | See below. |
| `mcomp-vm didn't build` | Check `xcode-select -p` (the Command Line Tools) and the network, then run it again. Or use Cua's notarized Lume: `./install.sh --lume=standard` (it shares your clipboard, audio and Rosetta with the VM). |
| `the vault didn't build` | Everything but `metacomputer secrets` works. Rebuild later with `scripts/build-vault.sh` (macOS 15 and the Command Line Tools). |
| The VM build stopped | The newest `logs/build-*.log` has the details. `./install.sh --replace-vm` deletes the unfinished VM and builds it again; finished downloads are kept. If provisioning failed inside the VM, it's left running for you to look at: `metacomputer ssh admin`. |
| `the login Keychain didn't take the live-view password` | The build ran where the Keychain was locked or missing (some SSH sessions). The password is in `~/.ssh/metacomputer/vnc-pass` instead, readable only by you; `metacomputer view` says how to read it. |
| `metacomputer status` isn't ready after the install | `metacomputer stop`, then `metacomputer start`, then `metacomputer status` again. `metacomputer logs` shows the VM's and the gateway's logs. |
| Claude Code has no metacomputer tools | Restart Claude Code, or run `/reload-plugins`. `claude plugin list` should show `metacomputer@metacomputer`; `./install.sh --only-claude` sets it up again. |
| `~/.local/bin isn't on your PATH` | Add it: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zprofile`, then open a new Terminal. Claude always uses the full path. |

### Command Line Tools without the dialog

Apple's dialog needs someone at the screen. Over SSH, or if the dialog never
shows up, install the tools from Terminal with Apple's software update tool.
This needs an administrator's password, which `sudo` asks for:

```bash
touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
softwareupdate --list | grep -o 'Label: Command Line Tools.*' | sed 's/^Label: //'
sudo softwareupdate --install "Command Line Tools for Xcode-NN.N"   # the label printed above
rm /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
```

Homebrew's installer does the same when the tools are missing, so installing
Homebrew first also works. Then run get.sh again.
