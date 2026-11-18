# Changelog

All notable changes to metacomputer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `metacomputer secrets`: a self-contained vault for website passwords, 2FA (TOTP) codes and sealed browser logins. Claude fills them into page-mode Chrome without the values reaching its context, the transcript, logs or recordings. You unlock it in your own terminal and can approve each use. VMs built with this version have a locked fill account that Claude can't modify. See SECURITY.md for exactly what holds and what doesn't.
- API keys through a proxy: `metacomputer secrets set NAME --kind api --api-host HOST` stores a key, and `secrets token NAME [--ttl 15m]` gives a program in the VM a short-lived token for a proxy at `127.0.0.1:18765` inside it. The proxy adds the real key on the Mac, sends requests only to that host, and withholds the key if the API echoes it back. Tokens last at most 8 hours and end when the vault locks; `secrets tokens` lists them and `secrets revoke ID|--all` ends them. VMs built with this version have a locked proxy account for it (rebuild older VMs to use tokens).
- Secure Enclave binding: `secrets init` binds the vault to this Mac's Secure Enclave when it has one and prints a one-time recovery key. `secrets recover` opens the vault with that key on another Mac; `rekey --enclave` and `rekey --no-enclave` move a vault onto or off the Enclave; `init --no-enclave` makes a passphrase-only vault.
- Rotation: `secrets set ... --replace` records a rotation and `secrets list` shows each secret's age; `secrets rekey` changes the passphrase and every key (the old recovery key stops working); `secrets rotate-session-key` re-encrypts sealed browser logins.
- The vault's audit log is hash-chained, and each record the vault writes carries a MAC under an audit key kept inside the vault (`secrets audit --verify`, `audit --head`); every event is copied to macOS's unified log, and `scripts/verify-audit.py` checks a log or a sink's capture. Audit sinks (`secrets audit-sink add`) send every event off the Mac as it happens, as signed webhooks, OTLP logs, syslog over TLS or ntfy messages; a `--required` sink must acknowledge each use before it goes ahead, and a record that fails the vault's checks reaches the sinks as an `audit_tamper` notice instead.
- Requesters and approval scopes: the unlock terminal and the audit log name the Claude Code session and project behind each request. When a use needs approval, typing `NAME session` or `NAME unlock` extends it to that session or until the vault locks, and `set --project DIR` limits a secret to sessions in that folder. This is attribution, not isolation.
- The skill's `SECRETS.md`: how Claude signs in, keeps a login and uses API tokens, and what it must never do.
- The event stream (`metacomputer events`, `metacomputer notify`; docs/events.md), off until you add a sink: Claude blocked on you, finished tasks and recordings, security signals (blocked connections, Chrome-guard and bounded-mode refusals, a failed `actions verify`, `web open-untrusted` findings) and the VM's lifecycle and health, sent to webhooks (CloudEvents, signed), OpenTelemetry (OTLP/HTTP), syslog over TLS or ntfy. Every string is cleaned and floods are rate-limited; sinks change only from your own terminal, and any change to them is announced to the existing sinks. The skill's `EVENTS.md` says when Claude notifies you.
- The shell channel, `metacomputer sh` (docs/shell.md): bash in the VM as the desktop user, with the desktop's environment, returning output, exit code and duration. It treats the VM as hostile (bounded output, one deadline, a strict reply schema, escaped terminal output), counts as an acting call for the one-controller rule, is off while `DRIVER_MODE=bounded` (`VM_SHELL`), and goes in the signed action log when it's on (`SHELL_LOG`). The skill's `SHELL.md` routes work by where its state lives, after Microsoft Research's Hybrid Computer Use work.
- File tools in the default VM: LibreOffice Calc, and for scripts python3-openpyxl, python3-docx, python3-odf, python3-pil, poppler-utils, sqlite3, zip and unzip, and python3-venv and pip. The bounded `desktop` profile launches Calc by name.
- A benchmark (`bench/`, bench/README.md): 20 desktop tasks run in fresh headless Claude Code sessions on a test VM, scored by checks that read the VM's real state, with switches to compare one change at a time. The first baseline passed all 20 for $11.95.

### Changed
- After a successful build, the VM's private SSH host key is deleted from the Mac; only the public key and `known_hosts` are kept.
- `metacomputer secrets set` on a name that already exists now refuses unless you add `--replace`.
- The VM runner is now mcomp-vm, formerly lume-secure, built from github.com/metavert-io/metacomputer-vm (tag v0.7.2-mcomp.1, commit pinned), because Cua's trademark policy asks a modified version to take another name. It's the same Lume build with the same flags, made by Metavert, not by Cua. `install.sh` installs it as `~/.local/bin/mcomp-vm` and takes `--lume=mcomp-vm` (`--lume=secure` still works); `LUME` defaults to it, and its pins are `MCOMP_VM_REPO`, `MCOMP_VM_TAG` and `MCOMP_VM_COMMIT` (`LUME_SECURE_*` in a local.env still count). Existing installs keep working: while `LUME`'s runner isn't installed and `~/.local/bin/lume-secure` is, metacomputer uses that one and prints a notice; `./install.sh` installs mcomp-vm beside it, points `LUME` there and leaves lume-secure in place. `install.sh --uninstall` names the runner in use.

### Security
- Every SSH connection the Mac makes to the VM now runs with no config file (`-F /dev/null`) and with agent, X11, port and tunnel forwarding and connection sharing turned off, so nothing in your SSH config can forward anything to the VM.
- Files copied from the VM (`task finish`, `get`, `recordings --sync`, the test harness) arrive as plain files and folders only: links, hard links, FIFOs and devices are left out and reported, and nothing is written through a link, so a VM can no longer plant a link named like a contact sheet and have `task finish` overwrite a Mac file through it.
- Text from the VM or a web page is shown escaped by `inspect`, `browser`, `web` and `record moments`, as `metacomputer sh` already did (`--json` stays exact).
- The API proxy matches `--api-path-prefix` by whole path segments (`/v1` no longer allows `/v10` or `/v1-admin`); a request's head must arrive within 10 seconds and its token is checked before any body is read; each VM has its own 16 connection slots.
- `actions verify` requires each checkpoint to sign the record right before it and the final one to end the file, so forged records followed by a replayed final checkpoint no longer verify.
- The VM's Mozilla and Google package keyrings hold only the pinned key, even if the downloaded key file carries others.
- The benchmark runs in a fresh private folder and runs `claude` by an absolute path found before its shim folder goes on PATH.

### Fixed
- `./install.sh --help` shows its whole description again (the last step was cut off).
- Two VMs side by side: a session on one VM no longer makes the other refuse every tool call ("in use by another controller").
- A refused `build` (the VM exists, or a bad setting) no longer replaces the host key and `known_hosts`, which broke SSH to the existing VM.
- `metacomputer sh` outside Claude Code: its claim on the computer lasts the activity window even when the shell that ran it exits.
- The API proxy: a same-host redirect keeps the API's path encoding and trailing slash; after an answer times out, nothing more is written to that connection.
- Sealed browser sessions refuse at once, with a clear message, under `SSH_AUTH=cert` (they need `SSH_AUTH=key` for now).
- The event stream: an OTLP collector's partial success counts the rejected events as dropped; every delivery attempt has a wall-clock deadline, so a sink that trickles bytes can't hold `start`, `stop` or `sink add`; the relay reads `events.json` only when it changes.

## [0.1.0] - 2026-11-17

The first public release. It covers everything built from 2026-11-16 to
2026-11-17, including pull requests #1 to #5.

### Added

- **The virtual computer.** `metacomputer build` (`scripts/build-vm.sh`)
  builds an Ubuntu 24.04 ARM64 XFCE desktop as a Lume VM, unattended: Ubuntu's
  official cloud image, a cloud-init seed, provisioning of Xorg, LightDM
  autologin, AT-SPI accessibility, Firefox, LibreOffice Writer, GNOME
  Calculator, Thunar, Mousepad, a terminal, ffmpeg and a VNC server, then a
  clean baseline snapshot. Versions and digests are pinned in
  `config/defaults.env`.
- **Cua Driver 0.52.1 inside the VM,** reached by Claude Code over SSH as the
  `metacomputer` MCP server. `metacomputer mcp-lazy` answers the MCP handshake
  from a saved copy, so the tools are listed in every project even while the
  VM is off, and connects on the first call.
- **The `metacomputer` skill** (`skills/metacomputer/`): which computer a
  request means, when a task needs the desktop at all, a routine for tasks,
  and desktop (DESKTOP.md) and browser (BROWSER.md) rules verified on this VM.
- **The `metacomputer` command,** with the short alias `mcomp`: `start`,
  `stop`, `restart`, `status`, `reset`, `checkpoint`/`restore`, `baseline`,
  `who`/`release`, `inspect`, `put`/`get`/`shot`/`rename`, `task start|finish`,
  `contact-sheet`, `view`, `input-reset`, `desktop-restart`, `call`, `ssh`,
  `logs` and `where`.
- **`install.sh`**: checks the Mac, installs missing tools with Homebrew,
  installs Lume, writes `config/local.env`, links the command, the skill and
  the MCP server for every Claude Code project, and builds the VM.
  `--uninstall` removes the links and the MCP registration.
- **Google Chrome** as an optional second browser (`BROWSERS`), with three
  browsing modes (human, page and hybrid), defaults set by
  `metacomputer browser`, and named Chrome profiles
  (`metacomputer browser profiles`).
- **Browser checks that need no model:** `metacomputer web compare`,
  `responsive` (with `--baseline` difference images), `a11y` and
  `open-untrusted`.
- **Recordings:** per-task MP4s and per-action traces through Cua Driver, and
  whole-desktop recordings with `metacomputer record start|stop`.
- **One controller at a time:** a second Claude Code session's actions are
  refused while another session acted in the last 10 minutes, and `stop`,
  `reset`, `restore`, `baseline` and `test` refuse unless given `--override`.
- **Chrome input guard** in the bridge: input aimed at Chrome is sent in the
  foreground, `set_value` and `run_script` aimed at Chrome are refused, and the
  driver's leftover virtual input devices are removed before Chrome needs the
  keyboard.
- **`EXTRA_PACKAGES`** (#4): more Ubuntu packages built into the VM, such as
  Inkscape, LibreOffice Calc, Audacity or Kdenlive. Package names are checked
  on the Mac and again in the VM.
- **Recording settings** (#4): `RECORD_FPS`, `RECORD_QUALITY` (`high` is sharp
  enough to edit into videos), `RECORD_TO=host` to stream a recording to the
  Mac while it runs, and `RECORD_DIR` for any folder, such as an external
  drive. A missing parent folder is refused rather than created.
- **A description file beside each desktop recording** (#4): times, settings,
  length, size and SHA-256. With the action log on, it lists the key moments,
  each tool call with its time in the video; `metacomputer record moments`
  prints them.
- **A second VM side by side** (#4): `RUN_DIR` and `LOG_DIR` keep each VM's
  gateway, lock, bridge files and action log apart; select the other VM with
  `MCOMP_CONFIG`.
- **`VIRTUAL_AUDIO=on`** (#5): a sound card inside the VM that plays into
  nothing (PulseAudio with one null sink). `RECORD_AUDIO=auto` includes its
  sound in desktop recordings, delayed 90 ms to land within about 50 ms of the
  picture. `metacomputer status` shows the sound card.
- **Desktop rules for extra apps** (#5) in DESKTOP.md: Inkscape, Kdenlive,
  Audacity, LibreOffice Calc and SDL/pygame windows, plus GTK file-dialog and
  window-sizing behavior.
- **Tests:** the recorded GUI suite (`metacomputer test`, A to F), the Chrome
  guard test, recording tests, action-log tests, option tests for a test VM,
  and headless skill tests. See TESTING.md.

### Security

- **lume-secure by default** (#1): Lume 0.7.2 plus flags that leave out the
  host-sharing devices. The VM starts without your Mac's clipboard, audio and
  Rosetta unless `SHARE_CLIPBOARD`, `SHARE_AUDIO` or `SHARE_ROSETTA` is `on`.
  `install.sh --lume=standard` installs Cua's release instead, with a warning
  that it always shares them. `metacomputer status` reports what the VM
  actually has, read from inside it.
- **A host-side network gateway** (`cua-netgw`, built from Cua's source at a
  pinned commit): the VM reaches the public internet only, never the Mac, the
  LAN, a tailnet, link-local or cloud-metadata addresses, or outbound mail
  ports. Blocked attempts are logged.
- **Nothing listens off the Mac's loopback.** SSH and the password-protected
  live view are forwarded to `127.0.0.1` only; Lume's own VNC server is never
  started.
- **SSH by role:** keys only, a host key generated on the Mac and pinned before
  the first boot, and separate `admin`, `ops` and `mcp` keys. The `mcp` key can
  only start Cua Driver's MCP server.
- **Narrow accounts:** the desktop user has no sudo; after the build the
  maintenance account may run only a fixed-verb admin script, cloud-init is
  turned off and its cached user-data is deleted.
- **Verified downloads:** the Ubuntu image (signed checksums, pinned key and
  digest), the Cua Driver release (pinned SHA-256, checked again inside the
  VM), Lume (pinned checksum, or lume-secure's pinned tag and commit), and the
  Mozilla and Google repository keys (pinned fingerprints).
- **A smaller guest:** no snaps, no clipboard agent, the virtio sound driver
  blacklisted, no shared folders, Cua Driver and Lume telemetry off, browser
  telemetry and password saving off by policy, and automatic security updates
  without automatic reboots.
- **Optional hardening** (#3), all off by default so metacomputer behaves like
  Cua Driver on its own; `metacomputer security [apply]` shows and applies it:
  - `ACTION_LOG=on`: a signed, hash-chained log of every tool call
    (`ssh-keygen -Y sign`), with typed text redacted by default, and
    `metacomputer actions list|show|verify`;
  - `SSH_AUTH=cert`: a fresh key and a short-lived SSH certificate for every
    connection, named in the VM's auth log; the static keys stop working;
  - `DRIVER_MODE=bounded`: Cua Driver's bounded mode with a capability
    manifest (profiles `readonly`, `desktop` and `browser`, or your own),
    requested by its SHA-256, with a VM-side policy that refuses standard
    sessions while bounded.

### Changed

- **The project is named metacomputer** (#1, #2). The command is
  `metacomputer` (alias `mcomp`), the MCP server and skill are `metacomputer`,
  environment settings use the `MCOMP_` prefix, and paths inside the VM are
  `/opt/metacomputer` and `~/metacomputer-runs`. For checkouts from before
  this release, `install.sh` removes command links, skill links and MCP
  registrations left by the earlier development names (`vc`, `cua-lab`,
  `virtual-computer`, `vcomp`, `cua-linux`), and `metacomputer status` points
  out VMs built with an older layout, which need a rebuild.
