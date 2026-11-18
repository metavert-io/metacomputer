# Changelog

All notable changes to metacomputer are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- A Claude Code plugin, `metacomputer`, with a marketplace in the repository (`.claude-plugin/marketplace.json`, the plugin in `plugin/`). It brings the `metacomputer` skill, Cua Driver's guide as `metacomputer:cua-driver`, and the MCP server, to every project. Install it in Claude Code with `/plugin marketplace add metavert-io/metacomputer` and `/plugin install metacomputer@metacomputer`; the VM still comes from a checkout and `./install.sh`, which also installs the plugin. A plugin installed from GitHub runs your checkout's `bin/metacomputer` (through `~/.local/bin/metacomputer`, or `MCOMP_HOME`).
- `./install.sh --only-claude` runs just the Claude Code step.
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
- mcomp-vm 0.7.2-mcomp.2: the runner now introduces itself as mcomp-vm (banner, usage, `--version`), from metacomputer-vm's fresh public history. `install.sh` rebuilds an installed runner that isn't the pinned release.
- `./install.sh` installs the plugin by default and removes the skill link and user-scope MCP server that earlier versions registered for the same checkout. The tools are then named `mcp__plugin_metacomputer_metacomputer__*` instead of `mcp__metacomputer__*`; update permission rules or scripts of your own that name the old prefix. `./install.sh --no-plugin` keeps the earlier setup and names. The skill, the benchmark and the tests work with both.
- Cua Driver 0.53.0 in the VM (was 0.52.1); rebuild a VM to get it. The same 65 tools and arguments; on Linux it no longer re-sends input that may already have landed, joins the accessibility bus `AT_SPI_BUS_ADDRESS` names, presses Enter for a trailing newline in `browser_type` on a single-line field, and refuses a trusted `browser_click` whose point another element covers (`browser_input_trust_unavailable`). The driver's skill pack in `.claude/skills/cua-driver/` and the cached tool list follow it.
- The coordinate guard also checks `press_key` and `hotkey` when they carry x,y (they click that point to focus it first), as it already did `type_text`.
- The skill, tightened from recorded sessions: a one-round start (`who`, one ToolSearch, only the notes the task needs), core habits in SKILL.md (input sent isn't proof, two tries then check the premise, reads chosen by the next action, batch shape, file dialogs, `metacomputer sh -c`), an error table in DESKTOP.md, and the notes for Inkscape, Audacity, Kdenlive and games moved to EXTRA-APPS.md. The bridge's MCP instructions repeat the core habits. On the benchmark: 20/20 still pass, cost $11.83 → $10.38, turns 20.2 → 18.0 per task, invalid-argument calls 14 → 0.
- After a successful build, the VM's private SSH host key is deleted from the Mac; only the public key and `known_hosts` are kept.
- `metacomputer secrets set` on a name that already exists now refuses unless you add `--replace`.
- Claude Code installed with the native installer (`~/.local/share/claude/versions/X.Y.Z`) is now recognized as Claude Code, so a session's `metacomputer sh` and `call` claims go with that session instead of a parent process that outlives it.
- The desktop user's display name, shown in the VM's panel, is now `metacomputer` instead of "Cua automation" (new builds).
- The VM runner is now mcomp-vm, formerly lume-secure, built from github.com/metavert-io/metacomputer-vm (tag v0.7.2-mcomp.1, commit pinned), because Cua's trademark policy asks a modified version to take another name. It's the same Lume build with the same flags, made by Metavert, not by Cua. `install.sh` installs it as `~/.local/bin/mcomp-vm` and takes `--lume=mcomp-vm` (`--lume=secure` still works); `LUME` defaults to it, and its pins are `MCOMP_VM_REPO`, `MCOMP_VM_TAG` and `MCOMP_VM_COMMIT` (`LUME_SECURE_*` in a local.env still count). Existing installs keep working: while `LUME`'s runner isn't installed and `~/.local/bin/lume-secure` is, metacomputer uses that one and prints a notice; `./install.sh` installs mcomp-vm beside it, points `LUME` there and leaves lume-secure in place. `install.sh --uninstall` names the runner in use.

### Security
- Every SSH connection the Mac makes to the VM now runs with no config file (`-F /dev/null`) and with agent, X11, port and tunnel forwarding and connection sharing turned off, so nothing in your SSH config can forward anything to the VM.
- Files copied from the VM (`task finish`, `get`, `recordings --sync`, the test harness) arrive as plain files and folders only: links, hard links, FIFOs and devices are left out and reported, and nothing is written through a link, so a VM can no longer plant a link named like a contact sheet and have `task finish` overwrite a Mac file through it.
- Those copies also stop, keeping the files that arrived whole, once they pass the VM's disk size (`VM_DISK_BYTES`), when the Mac would have less than `IMPORT_MIN_FREE_GB` free (default 10), or after 200,000 entries, so a VM can't fill the Mac's disk or memory.
- Google's APT repo stays limited to the pinned key. Chrome's package rewrote the repo entry we wrote (it rewrites `google-chrome.sources` whenever that file exists) to trust a keyring its own scripts write; the entry is now `metacomputer-google-chrome.sources`, and the build fails if any other entry for Google's repo appears, even after a run of Chrome's daily job. Rebuild a VM with Chrome to get it.
- Text from the VM or a web page is shown escaped by `inspect`, `browser`, `web` and `record moments`, as `metacomputer sh` already did (`--json` stays exact).
- The API proxy matches `--api-path-prefix` by whole path segments (`/v1` no longer allows `/v10` or `/v1-admin`); a request's head must arrive within 10 seconds and its token is checked before any body is read; each VM has its own 16 connection slots.
- `actions verify` requires each checkpoint to sign the record right before it and the final one to end the file, so forged records followed by a replayed final checkpoint no longer verify.
- The VM's Mozilla and Google package keyrings hold only the pinned key, even if the downloaded key file carries others.
- The benchmark runs in a fresh private folder and runs `claude` by an absolute path found before its shim folder goes on PATH.
- The hub's socket, when RUN_DIR's path is too long for one, lives in your own temporary folder (`/var/folders/.../T`) instead of the shared `/tmp`, and clients check before they send anything that the socket's folder is yours and private and that the process serving it runs as you, so another account on the Mac can't stand in for the hub.
- `metacomputer web`, the GUI test harness and the status probe go through the hub like every other client, so its guards apply to them; `web` and the tests claim the computer through it before they start (`web --override` releases other sessions' claims first, as `metacomputer release` does). A status connection only reads: the hub refuses its claims. `lib/mcp_client.py` opens the raw driver connection (`MCOMP_DIRECT_BRIDGE=1`) only for transport tests that ask for it.
- A client of any kind that disconnects while one of its acting calls is still running in the driver (a script that timed out, a session that closed mid-call) no longer frees the computer at once: the hub holds it for that call in a lease it keeps fresh until the call ends or the driver connection closes, and `who` shows it. Past the activity window the hub closes the driver connection first and only then lets go, so the call can't act after the computer is free. If the lease can't be written, the client's claim stays. An acting call another controller still has running counts as busy even when its claim file is gone.
- The Chrome guard knows which app an element token came from, so `set_value` or background input aimed at Chrome by a token alone is refused or sent in the foreground like any other. A token from a read the hub doesn't have is refused whatever else the call names (`element_token_unknown`), and so are a window id the driver doesn't list (`window_unknown`) and a token whose app differs from the pid or window the call names (`target_conflict`): hints the hub can't resolve, or that disagree, never count as "not Chrome".
- In a `run_steps` batch, the coordinate guard refuses a pixel step that comes after a step able to move, resize, open or close a window or a dialog (a double click or a click with count 2, a click with Super, Alt or Meta held, a middle click, a drag, a press on the desktop frame, a click on a dialog's button named by role and name, Escape, or keys with Super, Alt or Meta, a function key, or Ctrl+N/W/Q/O/S/P), since it can't look between steps (`pixels_after_window_change`). Plain clicks, typing, Return and Ctrl+A between pixel steps still pass; none of the 373 recorded benchmark batches is refused.
- What a hostile VM can make the Mac's side keep or do is capped beyond the size of each message: a repeated `tools/list_changed` starts one refresh at a time, a refresh stops after 50 pages, 2,000 tools, 16 MB, 60 s or a repeated cursor, and what the hub learns from results is capped; the driver connection's stderr keeps a 64 KB tail and at most 1 MB in its log; JSON nested more than 100 levels deep, or with more than a million values, is dropped as a bad line instead of stopping a reader or filling memory (hub, relay, SDK, direct client); the direct client's backlog has a byte budget; screenshot layouts keep only checked geometry, at most 256 windows each and 32,768 in all. A driver that keeps sending messages the hub can't use (not JSON, or of the wrong shape) is hung up on after 50 in a minute, `hub.log` is rotated at 5 MB while the hub runs, and the same kind of note is written at most five times a minute.
- Copies from the VM refuse tar extension headers over 64 KB, more than 8 in a row or more than 64 MB in all (before Python's `tarfile` reads them into memory), GNU sparse entries (also those a pax header marks sparse, before their map is read) and compressed streams, and don't keep the entries they've passed; count every file and folder they make, including folders a path implies, against a 200,000 limit and the free-space floor; and cap the whole stream. The GUI test harness streams its copy into the importer with the same limits as `get` instead of holding it in memory, and stops the SSH command when a limit is reached; a command that exits nonzero or a copy that fails is recorded in the run's errors and marks its evidence incomplete.
- On a terminal, the VM's words are escaped in the stderr of SSH commands, `inspect --json` (written again as ASCII-only JSON), `record start`, `record status`, `status`, recorder messages and logs, `logs`, `who` and `input-reset`; `metacomputer ssh` stays raw. The host-mode recording's stream log is capped at 1 MB.
- `./install.sh` uses an already registered marketplace named `metacomputer` only when it's this checkout or github.com/metavert-io/metacomputer; from any other source its plugin isn't installed (it would run that source's code on your Mac), the installer says how to remove it, and it sets up the skill link and MCP server instead.

### Fixed
- Benchmark: `bold_runs` reports what Writer shows in bold. A bold paragraph style with the rest of its text in a not-bold span no longer counts as the whole paragraph bold, which failed `writer-format` runs whose documents were right (`tests/test_bench_checks.py`).
- `metacomputer who` failed with a traceback when a `metacomputer sh` claim from outside Claude Code was listed.
- `metacomputer status` failed with a JSON error when the MCP probe failed part-way.
- SECURITY.md's vault command table: `audit-sink test NAME` needs your terminal (and the passphrase) while the vault is locked.
- `./install.sh --help` shows its whole description again (the last step was cut off).
- Two VMs side by side: a session on one VM no longer makes the other refuse every tool call ("in use by another controller").
- A refused `build` (the VM exists, or a bad setting) no longer replaces the host key and `known_hosts`, which broke SSH to the existing VM.
- `metacomputer sh` outside Claude Code: its claim on the computer lasts the activity window even when the shell that ran it exits.
- The API proxy: a same-host redirect keeps the API's path encoding and trailing slash; after an answer times out, nothing more is written to that connection.
- Sealed browser sessions refuse at once, with a clear message, under `SSH_AUTH=cert` (they need `SSH_AUTH=key` for now).
- The event stream: an OTLP collector's partial success counts the rejected events as dropped; every delivery attempt has a wall-clock deadline, so a sink that trickles bytes can't hold `start`, `stop` or `sink add`; the relay reads `events.json` only when it changes.
- The README now says that `checkpoint --delete` asks first, like `reset`, `restore` and `baseline`, and that with no terminal to ask on it needs `--yes`.

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
