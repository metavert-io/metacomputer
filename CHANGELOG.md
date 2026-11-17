# Changelog

All notable changes to metacomputer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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
