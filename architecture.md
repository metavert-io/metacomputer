# metacomputer architecture

A disposable Ubuntu 24.04 ARM64 XFCE desktop, running as a Lume virtual machine
on your Mac, that Claude Code operates through Cua Driver's MCP server.
The controller (Claude Code, lifecycle scripts, test harness, evidence) stays on
macOS; every graphical action happens inside the guest.

```text
Your Mac (Apple silicon, macOS 14+) - trusted controller
│
├── Claude Code ──stdio──▶ bin/metacomputer mcp-lazy ─┐                      │
├── metacomputer call [--session NAME] ───────────────┤ Unix socket          │
├── scripts, web, GUI tests (lib/mcomp_sdk.py) ───────┤ RUN_DIR/hub/sock     │
│                                                     ▼ (0600, uid-checked   │
│                                                        on both ends)       │
│   the hub, lib/mcp_hub.py: one per VM; every guard, every client           │
│     one controller (.run/owners/) · Chrome guard · bounded mode ·         │
│     coordinate guard · action-log notes · events                          │
│     └── bin/metacomputer mcp (MCOMP_DIRECT_BRIDGE=1) ──ssh─────────────────┐
├── bin/metacomputer (start/stop/status/reset/test/record/recordings/view)    │
│                                                                            │
├── mcomp-netgw (cua-netgw + patch: user-mode gateway, host egress policy)   │
│     127.0.0.1:2277 ─▶ guest :22      127.0.0.1:5977 ─▶ guest :5900 (VNC)   │
│     unixgram socket ◀──▶ Lume VM network device                           │
└── Lume 0.7.2 (Apple Virtualization.framework, native arm64)               │
      └── VM "metacomputer" (4 vCPU, 8 GB, 50 GB sparse, 1920x1080 virtio-gpu)  │
            Ubuntu 24.04.x LTS arm64 (official cloud image, cloud-init)      │
            LightDM autologin ─▶ Xorg (modesetting) ─▶ XFCE 4.18             │
            user `cua` (no sudo)                                             │
            ├── /opt/metacomputer/bin/driver-mcp ◀────────────────────────────┘
            │     └── cua-driver 0.53.0 mcp (stdio; X11/EWMH, XTest, uinput,
            │         AT-SPI over the session D-Bus, ffmpeg x11grab recording)
            ├── AT-SPI2 registry on the session bus (toolkit-accessibility on)
            ├── Firefox (Mozilla .deb), LibreOffice Writer + Calc (gtk3 VCL),
            │   GNOME Calculator, Thunar, Mousepad, XFCE Terminal
            ├── tools for scripts: python3 openpyxl, docx, odf, PIL; poppler-utils,
            │   sqlite3, zip/unzip, venv/pip, jq, ImageMagick, ffmpeg
            └── x11vnc :5900 (password) for the live view / takeover
```

## Why this shape (and not Cua Spaces)

Evaluated against the Cua source at `ca6a643`. On an Apple-silicon Mac a Linux Space
runs as a Docker/gVisor container, a QEMU VM, or a `cua-vz` microVM of Cua's
own image (XFCE on **Xvfb**); Lume is used by Spaces only for macOS guests.
Spaces reaches the desktop through the same Cua Driver (linked into
`cua-spacesd`), exposes driver tools indirectly (`call_tool`), and has no MP4
recording or action traces of its own. Its real advantage is a polished
live viewer with multiplayer takeover, which needs the Spaces app and, for
other devices, the cua.ai relay or a direct token connection. We use the
driver directly, Lume for the VM, and a loopback-only VNC view.

## Components and versions

| Component | Version / pin | Where | Role |
| --- | --- | --- | --- |
| mcomp-vm (default; formerly lume-secure) | metacomputer's build of Lume (made by Metavert, not by Cua): Lume 0.7.2 (`lume-v0.7.2`, `f30fbc0`) + one patch: `lume run --no-share-clipboard / --no-share-audio / --no-share-rosetta / --no-host-sharing` (Lume's default sharing is unchanged); built from source, ad-hoc signed with Lume's virtualization entitlement | `~/.local/share/mcomp-vm/`, wrapper `~/.local/bin/mcomp-vm`, from github.com/metavert-io/metacomputer-vm (`MCOMP_VM_TAG`, `MCOMP_VM_COMMIT`). While `LUME`'s runner isn't installed and `~/.local/bin/lume-secure` is, `scripts/lib.sh` uses that one and prints a notice | VM lifecycle on Virtualization.framework; telemetry disabled; no LaunchAgent |
| Lume (alternative) | 0.7.2 (Developer ID, notarized; sha256 `c96cf5a7…`) | `~/.local/share/lume/lume.app`, wrapper `~/.local/bin/lume` | the same, with clipboard, audio and Rosetta always attached |
| mcomp-netgw | cua-netgw, built from trycua/cua `ca6a643` (`libs/cua/netgw`) with `patches/netgw/` | `vendor/bin/mcomp-netgw` | user-mode network for the VM (Lume `gateway:` mode); egress rules; loopback forwards |
| rclone (only for S3 volumes) | 1.75.2 osx-arm64 (sha256 `610ab7d4…`; `SHA256SUMS` signed by `FBF737EC…54FA`) | `.cache/rclone-v1.75.2/rclone`, fetched on first use | two-way sync of S3 volumes' local copies with their buckets, on the Mac |
| Cua Driver | 0.53.0 linux-arm64 (sha256 `fd5e6a9d…`) | guest `/opt/cua-driver/0.53.0` | desktop observation/control, MCP server, recording |
| Ubuntu | 24.04 LTS arm64 cloud image, release 20261017 (sha256 `50e24e1f…`, SHA256SUMS signed by `D2EB4462…7C81`) | VM disk | guest OS |
| qcow2raw | `tools/qcow2raw` (Lima's go-qcow2reader v0.8.0) | `vendor/bin/qcow2raw` | converts the qcow2 cloud image to the raw disk Lume boots |
| Firefox | Mozilla APT repo (arm64 .deb; key `35BAA0B3…15A3`) | guest | default browser (no snaps in the guest) |
| Google Chrome | Google's APT repo (arm64 .deb; key `EB4C1BFD…4796`), optional (`BROWSERS`) | guest | second browser; the driver's page tools |

Exact package versions installed in the guest are in `/etc/metacomputer/versions`
and `/var/lib/metacomputer/package-versions.txt` (copied to `logs/` at build).

## Provisioning (unattended)

`metacomputer build` → `scripts/build-vm.sh`:

1. `build-tools.sh` builds mcomp-netgw (cua-netgw at the pinned commit, with `patches/netgw/` applied to a clean copy) and qcow2raw.
2. `fetch-artifacts.sh` downloads and verifies the Ubuntu image (GPG-signed
   SHA256SUMS, pinned key fingerprint, pinned digest), the Cua Driver release
   tarball (pinned digest, cross-checked against the release SHA256SUMS) and
   Mozilla's APT key (pinned fingerprint).
3. `lume create --os linux`, then the cloud image is converted into the VM's
   50 GiB sparse `disk.img` (cloud-init grows the root partition).
4. A NoCloud seed ISO (label `cidata`) carries `user-data` (users, bootstrap
   SSH keys, a pre-generated pinned SSH host key), `network-config` and
   `payload.tgz` (provision script, guest file tree, the verified driver
   tarball, the Mozilla key, and the accounts' key lists `keys-guest` and
   `keys-admin`). The guest never downloads Cua components itself.
5. First boot: cloud-init creates `cua` and `cuaadmin`, runs
   `provisioning/guest/provision.sh` (installs the key lists root-owned in
   `/etc/ssh/metacomputer-keys/` before the sshd config that reads only them
   (`AuthorizedKeysFile /etc/ssh/metacomputer-keys/%u`), purges snapd, adds Mozilla's repo,
   full-upgrade, installs Xorg/LightDM/XFCE/AT-SPI/apps/ffmpeg/x11vnc,
   installs the driver, configures autologin, accessibility, no screen
   blanking/locking, uinput, sshd, ufw, unattended-upgrades) and reboots.
6. The host waits for the XFCE session, installs the VNC password, checks
   readiness (`guest-status`), runs `finalize.sh` (narrow sudo,
   cloud-init disabled, cached user-data removed, cloud-init's key copies
   removed from the homes once `sshd -T` shows the lists in use), stops the VM and clones
   the baseline (`BASELINE_NAME`, default `metacomputer-baseline`).

## Desktop-control transport

Claude Code sessions use `metacomputer` → `~/.local/bin/metacomputer mcp-lazy`
(`bin/metacomputer`, `lib/mcp_lazy.py`), the user-scope MCP server `install.sh`
registers (`register_server`; it reads and judges the entry already there with
`lib/claude_mcp.py`, as `metacomputer agents` does), so it exists in every project; its tools are
`mcp__metacomputer__*`. The `metacomputer` plugin (`plugin/`) brings the skills, not
the server: it declares no MCP server and no hooks, so Anthropic's plugin directory
has no hook or server script to follow (an older copy declared the server, and its tools
were `mcp__plugin_metacomputer_metacomputer__*`). The plugin's own `bin/metacomputer`, the
`metacomputer` command on Claude Code's PATH, is a short launcher
(`plugin/bin/metacomputer`, in place or in a GitHub install's copy) that hands over to
the checkout `install.sh` set up (`MCOMP_HOME`, else `~/.local/bin/metacomputer`). The
bridge answers `initialize` and `tools/list` from a saved copy of the
driver's handshake (`config/driver-mcp-cache.json`, refreshed under `.run/` on
each live connection), so the tools are listed even while the VM is off, and
it starts instantly. On the first call that needs the driver it connects to this
VM's hub (below), starting it if it isn't running, and passes messages through it
from then on. While the VM is off, a call returns "start it, then retry". A
dropped connection (the VM or the hub stopped) fails the calls in flight with a
plain message and is reopened on the next call. If Claude Code goes away
without closing the bridge's stdin, the bridge notices within 5 s and exits.

### The hub

`lib/mcp_hub.py` is one process per VM on the Mac. It holds the single MCP
connection to Cua Driver in the VM (`bin/metacomputer mcp` with
`MCOMP_DIRECT_BRIDGE=1`, below) and serves every client on this Mac: Claude
Code's bridge, `metacomputer call`, `metacomputer web`, the GUI test harness,
the status probe and scripts (`lib/mcomp_sdk.py`). The idea comes from
Microsoft's Hybrid Computer Use study: one daemon per desktop that the MCP
server, the CLI and an SDK share, so references and state carry over.

- **Where it listens.** A Unix socket, `RUN_DIR/hub/sock`: the folder is 0700 and
  the socket 0600. When that path is too long for a Unix socket (about 104 bytes
  on macOS), the socket goes in a folder named after RUN_DIR inside this user's
  own temporary folder (`confstr(_CS_DARWIN_USER_TEMP_DIR)`, `/var/folders/.../T/`,
  0700; `$XDG_RUNTIME_DIR` on Linux; else `~/.cache/metacomputer`), never the
  shared `/tmp`, where another account could make the folder first. Each
  connection's peer is identified by the kernel (`LOCAL_PEERCRED` and
  `LOCAL_PEERPID` on macOS, `SO_PEERCRED` on Linux, `getpeereid` as a fallback),
  and anything that isn't this user is closed before a byte is read. Clients
  check the other way before they send anything: the socket's folder must be
  theirs and private, and the listening process must run as them (the same
  kernel call on the client's end). Clients look first where the running hub's
  record (`RUN_DIR/hub/hub.pid`) says it listens, so a hub started by an earlier
  version, with its socket elsewhere, is still found. The first message must be a
  `metacomputer/hello` naming the client's kind (Claude Code, `call`, a script,
  `web`, a test), folder, optional driver session and settings.
- **One per VM.** `RUN_DIR/hub/hub.pid` is held under `flock` while the hub runs;
  a second hub for the same VM exits (code 3). A client that finds no socket
  starts one (`mcp_hub.py serve`, detached, logging to `LOG_DIR/hub.log`,
  rotated at 5 MB while it runs, with one old copy; past five notes of one kind
  in a minute the rest are counted, with a few examples, in a summary line written
  when the minute ends, a driver connection closes or the hub stops) and waits for
  its socket. A client whose settings differ (another `ACTION_LOG`,
  `DRIVER_MODE`, `COORD_GUARD`, …) is refused while others use the hub; when
  it's alone the hub restarts with that client's settings.
- **When it ends.** After `HUB_IDLE` (10 minutes) with no client, no call in
  flight and no driver recording (it asks the driver before exiting while one
  was started through it), on `metacomputer stop`, or on `metacomputer hub stop`.
- **Multiplexing.** Each client's JSON-RPC ids are rewritten on the way to the
  driver (`c<client>.<n>`) and restored on the way back, so each answer reaches
  only its client. `initialize`, `tools/list`, `ping` and `logging/setLevel` are
  answered by the hub from the driver's handshake. The driver's
  `tools/list_changed` starts one refresh of the tool list on a single worker
  (repeats while it runs fold into one more pass, at most one every 2 s) and
  goes to every client once per refresh; progress notifications go to the
  client whose token they carry (the token is forgotten with its request);
  other notifications aren't any one client's and are dropped. Requests the
  driver makes of its client (`ping`) are answered by the hub. A client's
  cancellation is passed on with the rewritten id. `metacomputer/claim` checks
  for other controllers and claims the computer without a tool call, for
  clients that want to know before they start (`web`, the GUI tests).
- **Driver sessions.** Every client shares one driver runtime, so element
  tokens, driver sessions, recordings and a Chrome that `browser_prepare`
  started outlive any one call. Screenshot frames are per driver session (Cua
  Driver 0.52.1 and 0.53.0 refuse pixels against "a screenshot that belongs to another
  session"), so the hub gives each Claude Code session a driver session of its
  own (`mcp-<pid>-<n>`, added to calls that take a `session` and don't name
  one) and ends it when that session disconnects, as its own connection used to.
  `metacomputer call --session NAME` and `Hub(session=NAME)` name one that
  lasts until `end_session` or the hub's exit; without one, calls use the
  hub's default driver session, which also lasts while the hub runs.
- **Limits.** A client's message is at most 8 MB and a message from the VM at
  most 64 MB (read with a bounded `readline`, before any JSON parsing; the
  largest real results measured were under 1 MB at 1920x1080, and an
  incompressible 4096x2160 screenshot is about 35 MB in base64). A client
  gets at most 256 messages or 128 MB waiting for it; one that stops reading is
  disconnected, and each client has its own reader and writer threads, so a slow
  or hung client never holds up the others. A message over a limit closes that
  connection with a plain error: a client's own, or, for the VM, the driver
  connection, failing every call in flight. A driver that doesn't take a message
  within 30 s is dropped the same way, so a stuck VM can't hold every client's
  call forever. JSON nested more than 100 levels deep, or with more than a
  million values (counted before parsing, outside strings: the top value,
  containers, commas and colons; strings are skipped in one linear pass with
  `find()`, no regular expression), isn't parsed (`lib/wirelimits.py`'s
  `LimitError`; its depth check keeps one iterator per level; the readers'
  cleanup runs whatever stops them). When such a line's top-level `id` names
  one of the hub's requests (`c<client>.<n>`; `wirelimits.top_level_id`, one
  linear pass following nesting outside strings, so nested ids and ids inside
  strings never count, and two ids or anything but one complete object give
  none), that request is answered at once with an error (`answer_too_complex`);
  otherwise it's a bad line, skipped, and no request is touched. So is a
  message of the wrong shape (an id, method or params of the wrong type); past
  50 bad lines in a minute on one driver connection (each counted on its own)
  the hub closes it like an oversized one. What
  the driver can make the hub keep is capped regardless of message size: a tool
  list refresh stops after 50 pages, 2,000 tools, 16 MB, 60 s or a repeated
  cursor (the earlier list stays); pid, window and snapshot maps and screenshot
  layouts keep the newest few thousand entries, and a layout keeps only checked
  geometry, at most 256 windows each (a longer list is marked incomplete) and
  32,768 in all. The driver connection's stderr
  is drained on a thread into a 64 KB tail (what error messages quote) and at
  most 1 MB per connection in `RUN_DIR/hub/upstream.stderr`.
- **Reconnecting.** When the driver connection drops (the VM stopped or
  restarted), every pending request fails with a plain message, everything the
  hub learned about that runtime is forgotten, and the next call reconnects.
  Each driver connection runs in a process group of its own (the relay and its
  SSH, or SSH alone); the hub ends a connection by ending the group (SIGTERM,
  3 s for the relay to sign its log, then SIGKILL) before it fails that
  connection's calls, and keeps each connection's process by generation until its
  reader is done, so a connection it no longer uses for new calls (after a
  bounded session ended) can still be ended. Ending runs once per connection
  under a lock (the lease keeper, the reader and shutdown may all ask), waits
  for the leader without reaping it (`waitid(WNOWAIT)`, or kqueue), and reaps it
  only after the group's SIGKILL, so the group id can't be reused while it's
  signalled; nothing else reaps it (liveness comes from the reader seeing the end
  of the output), and a leader reaped elsewhere means no signal at all.

With `MCP_HUB=off` the same core runs inside each client instead, over a
socketpair: Claude Code's bridge and `metacomputer call` each have their own
connection, as before the hub, with the same guards. Both settings are on by default:
the hub closes the route around the guards that `metacomputer call` was, and lets
state carry over between calls; the coordinate guard catches pixels the driver
would misplace after certain window-layout changes (not content changes; "The
coordinate guard" below). Neither cost anything measurable in the benchmark
(bench/README.md, "Costs": 20/20 tasks either way, $11.80 against $11.83, 20.1
against 20.2 turns, no refusals; per-task records in
[bench/runs/](bench/runs/)), and the GUI and option tests pass with both on.

### Guards (in the hub, for every client)

- **One controller at a time** (`lib/owners.py`). Acting calls are refused
  while another session acted within 10 minutes; reads are always allowed.
  The check and the claim are one step under this VM's owners lock, shared with
  `metacomputer sh`. Claims are written per client in `.run/owners/`: a Claude
  Code session's under its bridge's pid (dropped when it disconnects), a
  one-shot `metacomputer call`'s under its Claude Code process, or else, like
  `metacomputer sh`'s, the shell that ran it (kept for the activity window, and
  the same controller from the same terminal session). `metacomputer web` and
  the GUI tests are clients too and claim before they start (`metacomputer/claim`;
  `web --override` first releases other sessions' claims, as `metacomputer
  release` does); a status connection can't claim. When a client of any kind
  disconnects while acting calls it made are still running in the driver, the
  hub holds the computer for them in a claim of its own (kind `lease`,
  `.run/owners/<hub pid>.json`), written before the client's claim is dropped,
  both under the owners lock (if it can't be written, the client's claim stays
  until a later write works, and meanwhile the hub's own record of the lease
  counts as another controller). A keeper thread rewrites it while those calls
  run, so it stays active; it's dropped when the last of them is answered or the
  driver connection closes, and past the activity window (`MCOMP_ACTIVE_S`) the
  keeper looks again at which of its calls still run and ends the connections
  they were sent on (their generations, not whichever is current), so nothing
  keeps acting after the computer is let go. Removing a request, assigning it to
  a lease and finishing it happen under one lock. An acting call another controller still has running
  counts as busy even without its claim file (`who` and the refusal describe
  it). A lease isn't another controller to the same client process or its Claude
  Code session. `metacomputer stop/reset/restore/test`
  refuse too unless given `--override`. The hub's own SSH connection isn't
  counted as a controller; any other direct connection is.
- **A handoff** (`lib/handoff.py`, `metacomputer handoff`). Claude gives the
  computer to the user for a step that's theirs (a CAPTCHA, a sign-in, a 2FA
  prompt). Its record, `.run/owners/handoff.state`, is written under the owners
  lock, and while it's open `owners.others()` lists it for every caller, so the
  hub refuses every client's acting calls (`user_has_computer`), the session that
  asked included, and `metacomputer sh`, the vault's fills and the lifecycle
  commands refuse too. Because a call's guards can wait on the VM after it was
  admitted, every action looks once more right before it leaves the Mac, under a
  per-VM dispatch lock (`.run/owners/.dispatch`, `owners.acting()`): held shared
  by the hub while it sends a call (and by its cleanup and input reset), by the
  action-log relay while it forwards one (it looks again after signing), by `sh`
  until its script is written, by the GUI tests' own SSH commands, by the
  lifecycle commands after their question, and by the fills for their whole
  command (and a `secrets settle` when the vault couldn't confirm a stop); opening
  a handoff takes it exclusively. Opening one also removes the driver's leftover
  virtual input devices (below; `lib/input_reset.py`), still under that lock, after
  the record and once acting calls already in the driver have ended (when one is
  still running after 10 s, or that can't be known, it leaves them and says the
  reset is pending), so the live view's mouse and keyboard work; `view --open` does
  the same when no agent call is in flight. The hub keeps its driver connection while one is
  open (no idle exit, restart or lease kill). It ends with `handoff done`, `handoff cancel` or
  its time limit; a record that can't be read counts as open, for at most 2 hours.
- **Chrome** (docs/chrome-investigation.md). When the driver delivers
  background input through its own X input devices (a "CUA v1..." master
  pointer/keyboard pair, created by XInput2 multi-pointer), Chrome ignores that
  input, and while such a pair exists Chrome ignores all keyboard input, for any
  app's background input, until the pair is removed. So the hub:
  - learns which guest pids are Chrome from results it relays, asking
    `list_windows` when needed, and which window and app each read's snapshot
    belongs to, so a call that names its target only by an element token
    (`<snapshot_id>:<row>`, which carries the pid to the driver but not to the
    hub) is resolved too. Every hint a call gives (pid, window id through the
    hub's window list or one `list_windows`, app or window name, token) is
    resolved: any that's Chrome makes the call Chrome's; a token from a read the
    hub doesn't have is refused whatever else the call names
    (`element_token_unknown`: read the window again), a window id the driver
    doesn't list is refused (`window_unknown`), and a pid, window id and token
    that don't all belong to one app are refused (`target_conflict`). A
    `run_steps` step is judged on its own target when it names one; one that
    names none runs, in the driver, in the window the last step that named one
    acted in (or the batch's), and the hub judges it the same way, the stricter
    verdict counting where both could apply;
  - rewrites input tools and `run_steps` aimed at Chrome to
    `delivery_mode: "foreground"`;
  - refuses `set_value` and `run_script` aimed at Chrome;
  - notices virtual-device routes in results (`path=mpx_*`), and before the
    next Chrome keyboard input removes the pairs (the guest's `input-reset`,
    `xinput remove-master`) as every reset is made, noting it in that result.

  The driver keeps a pair between actions (Cua Driver 0.53.0 never removes one), and
  a driver process whose pair was removed ends at its next pointer action through it
  (XI_BadDevice). So every reset (`lib/input_reset.py` reset_now) is made under the
  dispatch lock held exclusively, never while an agent call is still running in the
  driver (or that can't be known: `MCP_HUB=off`), and recorded before the guest is
  asked (`owners.mark_input_reset`). Right before a call that may use the driver's own
  devices (`may_use_devices`, each step by its effective mode) is sent, in the same
  critical section as the send (the dispatch lock and the hub's send gate,
  `before_send`), the hub compares that record with when its connection began using
  them (`reset_seen`) and, when it's later, starts a fresh driver connection first
  (`renew_after_reset`): only after everything sent on the old one has ended (else the
  call is refused, `input_reset_busy`), and, with every completion's bookkeeping
  published (`settle_lock`), not while a driver recording runs or a browser
  `browser_prepare` launched is open (`input_reset_recording`; foreground input goes
  on). The element tokens and snapshot ids clients see are the hub's own, one per (driver
  runtime, driver snapshot id), unique for the hub's lifetime and in the driver's shape
  (`tag_ids` on every answer, `untag_ids` on every call before any guard): an id of another
  runtime, or one the hub never handed out, is refused (`element_token_stale`), so a token
  kept across a reconnect can't name another element. Only snapshot ids are translated,
  in the driver's named fields (`snapshot_id`, `since`, `element_token`, `data` included)
  and its own text lines, never the envelope or other strings, at most `TAG_MAX_IDS` per
  answer (more: the answer is refused whole); in `run_script`'s script, only whole ids the
  hub issued (a token built from pieces, or an id a script returns as a plain value, stays
  the driver's and is refused when used); the mappings an answer uses are pinned while its
  new ones are made. Reads never renew or claim: their tokens are refused after the first
  renewing input, and the agent reads again. The final critical section also checks again
  that the calling client still holds the computer (`check_and_claim`) before any renewal
  or send, and a successful `stop_recording` clears every session's recording marker. A driver session the hub ends itself releases its browser,
  and a driver connection the shared hub doesn't own counts as calls that can't be seen
  (`input_reset.outside_connections`). Nothing else tears the connection down for a reset. One caveat from Cua's own notes: removing
  a master device crashes Qt 6.5+ clients on the display (QTBUG-146876), which is why the
  driver never removes one itself; Ubuntu 24.04's own Qt 6 is 6.4, but a Qt 6.5+ app added
  to the VM (a Flatpak or AppImage) could close at a reset.
- **Bounded mode** (SECURITY.md): `launch_app` by name is rewritten to the
  absolute path the manifest allows; a refusal that repeats tells the user
  (events); when the bounded session ends, every client attached to it is told
  to ask the user to reconnect, and stays ended until it does, while a client
  that connects later gets a new driver connection (each connection used to
  have its own session).
- **The coordinate guard** (`COORD_GUARD=on`, below).
- **Human pacing** (`BROWSER_PACING=human`; `lib/pacing.py`, SECURITY.md "Human mode and human pacing"): last,
  after every guard passed an acting call, a `type_text` or `run_steps` aimed at a Firefox or Chrome window (by the
  same target rules as the Chrome guard) gets the driver's own pauses (`delay_ms` per character and between steps,
  random within fixed ranges), a long text in a batch split into two or three runs, and `move_cursor` steps
  (desktop scope) toward a pixel click, whose screen point comes from the coordinate guard's layout of the
  screenshot it was aimed from and the live window list. At most 15 s per call; `hub.log` gets counts only.
- **Hidden page text** (`HIDDEN_TEXT=on`; `lib/hidden_text.py`, SECURITY.md "Hidden page text"): for a
  window read whose client wants markdown, the hub asks the driver for the element list too
  (`tree_format` `both`), reads hidden rows off their frames (and Chrome snapshots' visibility),
  matches wording aimed at AI agents, then takes the list out again: the client gets its own
  answer plus a note in front, a tag on each marked row and a `metacomputer_hidden_text` field,
  and the event stream gets `security.hidden_text` (counts and host only). It runs on the
  driver connection's reader thread, bounded per result (pieces of text, characters, 0.25 s
  to look and 0.15 s to tag, a line at a time).
- **The hub's names** (`lib/hub_notes.py`): on every answer, before anything else, one pass over all of
  it moves keys named `metacomputer_...` (at any depth) to `upstream_metacomputer_...` and marks every
  string that carries a hub note's opening anywhere as the VM's; the hub's own notes then go in the
  text and, under `metacomputer_note`, in the structured part, which is what Claude Code shows the
  model when there is one. Last, `fit` checks the final answer against a client's limits.
- **The action log** (`ACTION_LOG=on`): one log file per driver connection, so
  the hub's connection is one file; before each `tools/call` the hub sends the
  relay a note naming the client (kind, pid, Claude Code pid, folder, driver
  session), which the relay records in the call's record and never passes on.
  Calls the hub refuses are recorded with their client too.
- **Events** (docs/events.md): `in_use`, `chrome_input_refused`, `refused`
  (bounded) and the controller events, with the client's own folder.

### The coordinate guard

Pixel input is aimed from a screenshot. Measured on Cua Driver 0.52.1 (the
resize, covering-window, occluded and other-session cases again on 0.53.0): it maps
window-local x,y through the window's live position, so moving the window is
harmless; it refuses a point outside the window, a pixel action without a
screenshot of that window in that driver session, and a background press under
another app's window (`target_occluded`). It does **not** notice that the
window was resized since the screenshot (the content reflowed, and the click
lands on whatever is there now), that a window now covers the point for a
foreground click (it clicks the covering window) or that an app's own dialog
opened over it (it retargets to the dialog), and it can't check desktop pixels
at all.

So with `COORD_GUARD=on` (the default) the hub records the window layout
(`list_windows`) at every screenshot-bearing read: `get_window_state` with its
screenshot (the layout is asked for just before the read, without waiting),
`get_desktop_state` (its own window list), and a `run_steps` result that carries
one. Before it passes on a pixel-addressed action (`click`, `double_click`,
`right_click`, `drag`, `scroll`, `type_text`, `press_key`, `hotkey` (the three
keyboard tools click their x,y to focus it first), `mouse_button_down`/`drag`/`up`
and `move_cursor` with a target, with x/y, and `run_steps` steps that use x/y),
it compares that layout with the live one and refuses, before any input, when:
- the target window's size changed;
- the set of windows above the target that cover the point changed (one
  appeared, moved over it or went away), the point mapped through the
  screenshot's scale; after a `zoom`, the zoomed region;
- for desktop pixels, the windows stacked at that point (and their frames) changed.

The refusal's code is `windows_changed_since_screenshot`, with the hint "the
windows changed since your screenshot; take a fresh one" and the read to make
next. Non-pixel actions pay nothing; a pixel action pays one `list_windows`,
which a batch shares (it's reused until the next action is passed on, at most
1 s). Layouts are kept per driver session and window and forgotten when the
driver connection drops. When the hub saw no screenshot for that session and
window, or `list_windows` fails, the action is passed on and the driver's own
checks decide. When the layout at the screenshot or now had more windows than a
layout keeps (256), the action is refused instead (`too_many_windows`): a window
left out could be the target or what covers it. Not covered: popup menus (`list_windows` doesn't list them),
windows named only by title in a batch step, `run_script`'s `[x, y]`
targets, and `parallel_mouse_drag`'s paths. The active window isn't reported by `list_windows`, and on its own it
doesn't change where a point lands; a window raised over the point is caught
by the stacking check.

Inside a `run_steps` batch the comparison happens once, before the batch, and
the hub can't look between steps. So a pixel step that comes after a step able
to move, resize, open, close or raise a window is refused before anything runs
(code `pixels_after_window_change`, with `failed_step` and `window_step`):
- a `double_click` or a `click` with `count` 2 or more (a title bar
  maximizes), or a drag (`drag`, `mouse_*`);
- a click with Super, Meta, Alt or Hyper held (window-manager move and
  resize), or a middle click (raises or lowers a window);
- a press on the desktop frame (`scope: "desktop"`: a title bar, its buttons,
  a window edge);
- a click on a dialog's button named by role and name (OK, Cancel, Close,
  Save, Open, Apply, Yes, No, Don't Save, …), or on a window's own control
  (Maximize, Minimize, Restore, Full screen);
- `press_key` / `hotkey` with Super, Meta, Alt or Hyper (window-manager
  shortcuts), a function key (F11 is full screen), Ctrl with N, W, Q, O, S
  or P (new, close, quit; the open, save and print dialogs), or Escape (closes
  a dialog or menu).

The rule is narrow on purpose: a single plain click inside a window, typing
(newlines too), `set_value`, scroll and keys such as Return, Tab or Ctrl+A
don't count, although a click or Return can open or close a dialog, because
the benchmark's own batches put pixel clicks after them (click a field,
Ctrl+A, type, Return, click the next field). Checked against the 373 recorded
benchmark batches: the rule refuses none of them; counting single clicks would
refuse 10, Return 3 and typed newlines 2. So a plain pixel click on a title-bar
button an app draws inside its window, or Return accepting a dialog, can still
change what later pixels in the same batch hit; the driver's own frame checks
are what's left for those. The refusal says to send the steps up to
that one on their own, take a fresh screenshot and aim from it.

The direct bridge, `bin/metacomputer mcp` with `MCOMP_DIRECT_BRIDGE=1`, is
`ssh -T` with the `mcp` key to `cua@127.0.0.1:2277`. It's the hub's own
connection; apart from it only the transport tests open it (`lib/mcp_client.py`
refuses it unless a test passes `transport_test=True`), since it skips the
hub's guards. Everything else that drives the desktop, `web` and the GUI test
harness included, goes through the hub (`lib/mcomp_sdk.py`). Without that variable, and with
`MCP_HUB=on`, `metacomputer mcp` runs the same bridge as `mcp-lazy`, so an MCP
client registered with the older command gets the hub and its guards. In the
guest the key is `restrict,command="/opt/metacomputer/bin/driver-mcp"`: no
shell, no PTY, no forwarding. The wrapper loads the XFCE session's environment
(published at login to `$XDG_RUNTIME_DIR/metacomputer/session.env`: `DISPLAY`,
`XAUTHORITY`, `DBUS_SESSION_BUS_ADDRESS`) and execs `cua-driver mcp`. MCP's
stdio stream is carried end to end by SSH; each MCP connection owns one driver
runtime. Nothing on the Mac drives the Mac's own desktop: Cua Driver is not
installed on macOS at all.

Browsers: Firefox (Mozilla's APT repo) is the default. Google Chrome comes
from Google's APT repo (`BROWSERS`, key fingerprint pinned in
`config/defaults.env` and checked on the host). Its own repo management is turned
off (`/etc/default/google-chrome`), and managed policies
(`/etc/opt/chrome/policies/managed/metacomputer.json`) turn off sign-in, sync,
metrics, password saving and default-browser prompts. The driver's page
tools (`browser_prepare` with `allow_launch` + an isolated profile) launch
Chrome on driver-owned profiles under
`~/.local/state/cua-driver/browser-profiles/` in the guest. Attaching to the
normal profile (`existing_profile`) needs the driver's `--grant
existing-profile`, which this lab doesn't pass.

## Shell channel

`metacomputer sh` (`lib/vm_shell.py` on the Mac, `lib/vm_shell_guest.py` in
the VM; [docs/shell.md](docs/shell.md)) runs a bash script as `cua` over the
`ops` connection, the same path, key or certificate as `metacomputer ssh ops`.
The remote command is a fixed bootstrap: the VM's half of the program, a JSON
header and the script arrive on SSH's stdin, so nothing is quoted or run by a
shell on the Mac. In the VM:
- the script gets the desktop session's environment (`session.env`, plus the
  accessibility variables from `/etc/X11/Xsession.d/90metacomputer-env`), so
  apps it starts with `setsid -f` open on the desktop and the driver reads them;
- it runs from a fixed path (`/tmp/mcsh.*/script`) in its own process group;
  its stdout and stderr are pipes the VM's half reads as they come, hashing
  all of each and keeping only the first and last `max_output / 2` bytes (no
  disk, bounded memory);
- after the script's own process ends, its output is read for 0.5 s more; a
  program it left running that still holds the output is handed to a `cat`
  that discards it;
- one JSON line comes back: status, exit code, duration, and each stream's
  size, SHA-256 and kept bytes;
- a timeout, or stdin closing early (the Mac's side interrupted), ends the
  process group from the same loop that reads the output: SIGTERM, then
  SIGKILL after 3 s to whatever is left; the answer waits until the group is
  gone.

On the Mac, `VM_SHELL` (`auto` is off while `DRIVER_MODE=bounded`) is checked
first; then, under the owners lock, the one-controller rule, and the run
claims the computer in `.run/owners/` like an acting tool call (held by the
caller's Claude Code process). With `ACTION_LOG=on` and `SHELL_LOG=auto` each
run is its own signed action-log file with a `vm_shell` call, signed before
anything runs, and its result. The VM is untrusted: the Mac reads each stream
up to a fixed limit under one deadline, accepts only an answer that fits a
strict schema, and escapes control characters before they reach a terminal
([docs/shell.md](docs/shell.md#a-vm-that-misbehaves)).

### Claude inside the VM

`metacomputer claude` (`lib/vm_claude.py`, with `lib/vm_claude_guest.sh` as the
script it sends) is built on the shell channel: each step (`start`, `send`, `read`,
`wait`, `keys`, `show`, `status`, `stop`) is one run through `vm_shell.run_guarded`,
logged as a `vm_claude` call.
- `start` gets a token from the vault (`metacomputer secrets token`, on a pipe) and
  passes it in the request header's `env`. The script takes it out of its environment
  at once and writes it to a 0600 file that Claude Code's launcher reads and deletes.
- Claude Code then runs in a tmux server on its own socket (`tmux -L
  metacomputer-claude`), with `ANTHROPIC_BASE_URL` at the vault's proxy in the VM.
- `send` pastes the prompt with `load-buffer` and `paste-buffer -p` (a bracketed
  paste), then sends Enter; `read` is `capture-pane`; `wait` polls the pane until it
  holds still.
- `show` attaches an `xfce4-terminal` on the desktop to the same session.
- While it runs, a keeper (`vm_claude.py _keeper`, detached) holds an owners claim of
  kind `inner` for the session that started it. It probes the pane once a minute
  (read-only, unlogged; each probe a new `metacomputer claude _probe`, so its SSH
  credentials are made for that connection), renews the claim under the owners lock
  when the screen changed, and ends with `stop`, the inner Claude's exit,
  `metacomputer release` (which takes the same lock) or the VM going away.
- The Mac keeps a record per session in `RUN_DIR/claude/` (the token's id, never the
  token).
- SECURITY.md, "Claude inside the VM", has what it can reach.

## Observation and accessibility

AT-SPI2 runs on the session bus; `toolkit-accessibility` is on (dconf system
db) and the session exports `GTK_MODULES=gail:atk-bridge`,
`QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`, `SAL_USE_VCLPLUGIN=gtk3` (LibreOffice's
accessible VCL backend), `MOZ_ENABLE_WAYLAND=0`. Firefox's
`accessibility.force_disabled` is locked to 0 by policy. Cua Driver reads
windows through X11/EWMH (+ XRes for pids) and trees through AT-SPI.

## Authentication

| Credential | Where | Grants |
| --- | --- | --- |
| `$SSH_DIR/mcp` (default `~/.ssh/metacomputer`) | host, 0600 | `cua@guest`, forced command = Cua Driver MCP only |
| `$SSH_DIR/ops` | host, 0600 | `cua@guest` shell (no sudo, no forwarding) for status, recordings, artifact checks, `metacomputer inspect` (read-only snapshot piped from `lib/guest_inspect.py`) and the shell channel, `metacomputer sh` |
| `$SSH_DIR/admin` | host, 0600 | `cuaadmin@guest`; after build `sudo` only `/usr/local/sbin/metacomputer-admin` (fixed verbs) |
| `$SSH_DIR/fill` | host, 0600; used by the secrets vault | `mcfill@guest`, the fill account: forced command `/opt/metacomputer/bin/secret-fill`, no TTY, no forwarding; key list root-owned in `/etc/ssh/metacomputer-fill/` |
| `$SSH_DIR/proxy` | host, 0600; used by the secrets vault | `mcproxy@guest`, the proxy account: one reverse forward to the guest's `127.0.0.1:SECRETS_PROXY_PORT` (18765) and nothing else (`nologin`, no TTY, no local forwarding); key list root-owned in `/etc/ssh/metacomputer-proxy/` with `restrict,port-forwarding,permitlisten=...` |
| `$SSH_DIR/hostkey` | host, 0600, during the build only | the guest's SSH host key, generated on the host and pinned in `known_hosts` (strict checking from the first connection); a finished build deletes the private half from the Mac (`hostkey.pub` stays), and a rebuild makes a new key |
| VNC password | `$SSH_DIR/vnc-pass` (0600), or your own secret store (`VNC_PASSWORD_STORE=command`); guest `~cua/.config/metacomputer/vnc-pass` 0600 | live view / takeover |

With `SSH_AUTH=cert` (optional; [SECURITY.md](SECURITY.md)), `ops` and `mcp` sign in with a fresh key and a short-lived
certificate from `$SSH_DIR/ca` per connection (`scripts/lib.sh` `cert_for`):
- principal = role, key ID `mcomp:ROLE:USER:PROJECT:pidN`;
- `source-address` = the gateway, and the `mcp` certificate carries a `force-command`;
- the guest trusts that CA through `/etc/ssh/sshd_config.d/20-metacomputer-cert.conf`;
- the guest sets each role's options in `/etc/ssh/metacomputer-principals/cua`, and turns off `cua`'s
  authorized keys.

`metacomputer-admin ssh-auth cert|key` switches it, and `start` and `security apply` call it.

Guest accounts have locked passwords; SSH is key-only with `AllowUsers cua cuaadmin mcfill mcproxy`.
No macOS credentials are used anywhere. On the Mac, every connection to the VM
(`scripts/lib.sh` `ssh_base`) runs with `-F /dev/null`, so no `~/.ssh/config`
`Host` block applies, and with agent, X11, port and tunnel forwarding,
connection sharing, local commands and proxies turned off
(`tests/test_ssh_options.py`). The secrets vault starts its own ssh (`/usr/bin/ssh`
with `-F none` and the pinned host key as `KnownHostsCommand` arguments, never a file).

## Secrets (`metacomputer secrets`)

Website passwords, TOTP seeds, API keys and sealed Chrome profiles live in a vault of
their own, `mcomp-vault`: a Swift program in `vault/`, installed to
`~/.local/share/metacomputer/bin/` by `scripts/build-vault.sh`, with its data in
`~/Library/Application Support/metacomputer/`. Claude's side (`metacomputer
secrets fill ...`) talks only to `mcomp-vault unlock`, which runs in the user's own
terminal and holds the decrypted vault. The unlock process:
- checks the request;
- checks that the VM's port is served by the pinned gateway binary (mcomp-netgw; its path and contents);
- asks the user when the secret says so (they type its name);
- runs `/usr/bin/ssh` with the pinned endpoint as the VM's fill account, `mcfill`,
  writing the value to ssh's standard input.

In the VM, sshd forces `/opt/metacomputer/bin/secret-fill` (root-owned) for that
account. The program:
- finds the driver's Chrome among the desktop user's DevTools listeners on loopback;
- checks the page from the browser's view: frame origins from `Page.getFrameTree`,
  the focused element through isolated worlds;
- types the value into that element.

Sealed browser sessions are copied to and from the VM as the desktop user.
Only the outcome comes back.

The rest of the vault, briefly (details in SECURITY.md):
- **Keys** (`vault/Store.swift`, `KeyCommands.swift`): the contents are sealed under a
  random data key, wrapped under the passphrase (PBKDF2) combined through HKDF with an
  ECDH secret from a Secure Enclave P-256 key, and again under a one-time recovery key.
  `rekey`, `recover` and `rotate-session-key` rewrite them.
- **API proxy** (`vault/Proxy.swift`): `token NAME` makes the unlock process listen on
  the Mac's 127.0.0.1 (a random port) and run `ssh -N -R 127.0.0.1:18765:127.0.0.1:PORT`
  as `mcproxy`, so the VM reaches it at `http://127.0.0.1:18765/NAME/`. It checks the
  short-lived token, puts the real key in the secret's header, calls `https://<api-host>`
  (no redirects followed, no proxies), streams the answer back with any copy of the key
  blanked, and audits each request. An answer ends with its token, or at its time limit
  (10 minutes, or the secret's `--answer-timeout`). `token renew ID` (`Server.swift`,
  `opRenew`) moves a live token's expiry under the same checks and approval as a new
  token, never past the unlock or 12 hours after issue; the token in the VM is unchanged.
  Tokens and tunnels end with the unlock.
- **Requesters** (`vault/Util.swift`, `Requester`): the client's pid
  (`LOCAL_PEERPID`) and its parents up to the nearest Claude Code process name the
  session and project, for the prompt, the audit log, `NAME session` grants and
  `--project` limits. Attribution, not isolation.
- **Audit** (`vault/Util.swift`, `Sinks.swift`, `SinkCommands.swift`): `audit.jsonl`
  is hash-chained (`seq`, `prev`, `hash`, `id`), each record the vault writes carries a
  creation MAC under the vault's audit key (inside the encrypted vault), and every
  record goes to the unified log too. The unlock process delivers it, in order and at
  least once, to any number of webhook (CloudEvents), OTLP/HTTP JSON, syslog-over-TLS
  and ntfy sinks, each with its own HMAC key. It checks each record's hash, MAC and the
  hash it wrote before signing it for a sink (a failure goes as an `audit_tamper`
  notice), keeps each sink's place in memory (the saved `sinks/NAME.state` carries a
  MAC), and, for a required sink, waits for that sink to acknowledge the `begin` record
  of each use. `scripts/verify-audit.py` checks the log and sink captures;
  `audit --verify` checks the MACs.
- **Withdrawn requests** (`vault/Server.swift`, `requesterGone`; `Commands.swift`,
  `request`): while it serves a fill, sign-in, session or token request, the unlock
  process watches the requester's connection.
  - When the requester closes its side or dies, the vault stops the request. It
    withdraws a question still waiting. It starts no further step: `runProcess` checks
    `requesterProbe`. It kills a step in progress, the ssh's process group.
  - It then answers `cancelled`, which is the requester's confirmation.
  - The client withdraws on SIGINT, SIGTERM or SIGHUP, when its parent ends, or at its
    deadline. With no confirmation it exits 5.
  - `mcomp-vault settle` is answered only after everything sent before it has finished.
- **Needs-you events**: while a question waits, the unlock process drops a
  `needs_you.approval_waiting` file into the event spool
  (`~/Library/Application Support/metacomputer/events/spool/`, under the "Event spool"
  contract below); on exit 3 (locked), the event stream's exit hook in
  `bin/metacomputer` reports `needs_you.vault_locked`.

Threat model and limits: [SECURITY.md](SECURITY.md#secrets-and-logins-metacomputer-secrets).

## Storage

[docs/storage.md](docs/storage.md). Disks the VM keeps across `reset`, attached when it
starts (Lume has no hot-plug), all of it outside the VM:

```text
Mac                                                         VM
registry + locks  ~/.local/share/metacomputer/storage/      storage-helper (root; only via
images            STORAGE_DIR/volumes/NAME.img  --disk  ─▶     metacomputer-admin storage attach)
                  STORAGE_DIR/refdisks/NAME.iso --mount ─▶   ext4 nodev,nosuid at ~/data/NAME
                                         (more: --usb-storage)  UDF read-only at ~/ref/NAME
```

- **`start`** runs `storage.py prepare` before `lume run`: it reads `VOLUMES` and
  `REFDISKS`, refuses missing images, unknown names, disallowed or overlapping mount
  points and a VM without storage support (`SSH_DIR/guest-features`, written by a
  finished build), leaves out what a bounded manifest doesn't list, takes a lock per
  read-write volume (held by the start command, then by the `lume run` process), and
  writes the extra `lume run` arguments and the VM's plan to `RUN_DIR`.
- Once SSH is up, the plan goes to `sudo metacomputer-admin storage attach`. The
  helper finds each volume among the extra read-write virtio disks in order and checks
  its size, formats only a blank disk (ext4 with the volume's UUID), runs `e2fsck -p`,
  grows the filesystem after `volume resize`, finds reference disks by volume label,
  and mounts through descriptors opened without following links. It mounts all of it
  or nothing; on failure, `start` stops the VM again.
- **Folder volumes** go to `lume run --shared-dir PATH:ro|rw`. Lume puts every shared
  folder in one virtiofs device (tag `com.apple.virtio-fs.automount`), a subfolder each,
  named after its last path component; the helper mounts it at `/run/metacomputer/vfs`
  (0700), proves each read-only one is read-only at the Mac (it tries to create a file as
  root), and bind-mounts each at its mount point. For a read-write folder, `start` records
  its contents before the VM boots, and a watcher process (`storage.py watch`, detached,
  pid in `RUN_DIR/storage-watcher.pid`) compares the folder with that every 2 s or more:
  quarantine on what's new or changed (exact value enforced), links whose text could lead
  out of the folder replaced by a note (only relative targets with their `..` steps first,
  within the link's depth, are kept), FIFOs, sockets and devices removed, set-id bits
  dropped, `security` events. Entries are counted as each folder is listed and a pass has a
  time limit; past either, or on any failure, it ends the VM's process first (SIGTERM, then
  SIGKILL) and reports after. It forks a supervisor before its first pass, holding the other
  end of a pipe the watcher writes a byte to as it makes progress: the supervisor ends the
  VM's process when the pipe closes without the watcher's "finished" byte, when no byte comes
  for 60 s, or when a read-write folder's disk drops below the reserve (it checks twice a
  second). The watcher does a last pass and exits when `stop` signals it or the VM's
  process is gone.
- **S3 volumes** are folder volumes over a local copy (the shared copy). `start` takes a
  per-volume sync lock (one VM syncs a volume), syncs once before the VM boots, and the
  watcher syncs every `S3_SYNC_INTERVAL` on a thread of its own; `stop` syncs once more after
  the VM is off. A sync (`s3_sync`), serialized per volume by an flock: reserve the shared copy
  (a lock record, so no read-write folder around it can be attached meanwhile), open it after
  checking it's still shareable and still the folder recorded at creation (path, device,
  inode), open the private sync copy `storage/s3/NAME/sync/`, claim free space on both disks
  (`storage/space/`, one record per claim, dropped with its holder), then: exchange (shared to
  sync), `rclone bisync` between the sync copy and the bucket, exchange (sync to shared). The
  exchange (`Exchange`) works against `storage/s3/NAME/stage.json`, both copies' file
  signatures after the last exchange, through descriptors from the two roots: plain files
  only, each copied from the descriptor that was checked (`fclonefileat` on one disk, a
  sparse copy bounded to the checked size and the claim, re-checking free space every 64 MiB,
  otherwise), written as `.metacomputer-tmp-*`; for the shared copy that's in its staging
  folder `.NAME.mcstage.noindex` beside it (same filesystem, refused as a share), quarantined
  through the held descriptor, then moved in with a cross-folder `renameatx_np`
  (`RENAME_EXCL` for a new name); folders it makes there are made, marked and renamed the
  same way. A file changed on both sides with the same size and time is compared by SHA-256
  before it's recorded as in step; a conflict renames the older copy `NAME.conflictN.EXT` in
  place. Free-space claims are keyed by allocation pool (`space_pool`: an APFS volume's
  container from `diskutil info`, else the filesystem). Each rclone run reads the keys from the login Keychain
  (`security find-generic-password -w`, output captured) into rclone's environment as an
  env-defined remote (`RCLONE_CONFIG_MCS3_*`), with an empty `rclone.conf`, its output
  (written to a file in its state folder, the tail kept) scrubbed of the keys before it reaches
  `storage/s3/NAME/rclone.log`; bisync's state lives in `storage/s3/NAME/bisync/`. It runs
  through `/usr/bin/sandbox-exec -p` with a profile that denies every write outside
  `storage/s3/NAME/` and reads of the home folders, `/Volumes` and the shared temporary
  folders except that folder, its config and itself (rclone's HOME and TMPDIR are in there
  too), capped with `--max-transfer` to its claim and killed if the disk falls below the
  reserve while it runs. Backups are pruned to `S3_BACKUP_MAX_GB` before and after. A resync
  runs only on `volume sync NAME --resync` (or for the first sync of an empty copy). rclone
  itself is `.cache/rclone-vVERSION/rclone`, from `scripts/fetch-artifacts.sh rclone` (signed
  `SHA256SUMS`, pinned key and zip digest), checked before each use against the program's own
  pinned digest (`RCLONE_BIN_SHA256`; `rclone_bin`), and unpacked again from the verified zip
  (`unpack_rclone`) when it's missing or different; no digest file is written or read.
- **Registered paths are walked** (`walk_dir`, `walk_file`): from `/`, one component at a time,
  each `openat(..., O_NOFOLLOW | O_DIRECTORY)` relative to the last, so no link anywhere in a
  path is followed. Paths are registered as the kernel spells them (`fd_path` of the checked
  descriptor), and a record in any other spelling is refused. Images (disk volumes, reference
  disks), S3 local copies and their staging folders record their device and inode when made
  (there's no re-binding: an item without one is deleted and created again), and
  `open_image` checks them at every start, checkpoint, restore, resize and delete. A staging
  folder is a `stage` item for the lock rules: no folder record of any VM may overlap it, while
  a create or a sync holds it. Its temporary names carry a session prefix recorded in
  `storage/s3/NAME/stage-sessions.json` before use; cleanup removes only those. A new
  item's folder (`destination`: `--dir` or `STORAGE_DIR`, resolved once, then walked) is
  reserved before anything is made in it.
- **Locks between items** follow one symmetric rule (`pair_conflict`), checked against other
  holders' live records and between a plan's own items: one exclusive lock per disk volume and
  read-write folder; a read-write folder may not overlap another, hold a folder someone else
  has, or hold an image anything uses (reference disks take shared records). Operations
  (checkpoint save, restore and delete, resize, delete, S3 sync) take records too, held until
  their last change. The folders holding images are refused as shares outright.
- **A storage failure after `lume run`** (a changed path, no watcher, a failed mount) ends
  the VM from the Mac: `vm_kill` runs `storage.py kill-vm` (the `lume run` pid from
  `RUN_DIR/lume.pid`, checked by its command line; SIGTERM, then SIGKILL) and then Lume's
  stop, each with a host deadline; the guest is never asked. An ordinary `vm_stop` sends the
  guest's poweroff in the background, so its 90 s deadline holds.
- **`stop`**, `reset`, `restore` and `baseline` stop the watcher, sync S3 volumes a last
  time and release the locks once the VM is off.
  `restore NAME` of a checkpoint taken `--with-volumes` first puts back each volume
  from its snapshot (an APFS clone beside the image, or a sparse copy).
- `guest-status` reports what the helper mounted (`/run/metacomputer/storage.json`,
  checked against the live mount table); `status` and `security` show it.

## Network

Lume runs the VM in `gateway:` mode: its network device is a datagram socket
to mcomp-netgw (Cua's cua-netgw with metacomputer's patch, below), a user-mode gateway (gvisor-tap-vsock) on the host. The guest
gets `10.213.77.2/24` by static DHCP lease; DNS goes to the gateway, which
resolves through 1.1.1.1 / 9.9.9.9 (not the Mac's resolvers or MagicDNS).
Every connection the guest opens is checked on the host:

- always refused (the gateway's built-ins): the Mac's loopback, every address of
  the Mac's own interfaces (LAN and tailnet IPs), the gateway itself, cloud
  metadata, multicast/broadcast
- refused by `config/netgw-rules.json`: RFC 1918 (your LAN, its router and
  devices), 100.64.0.0/10 (Tailscale/CGNAT), link-local, ULA (tailnet v6),
  benchmark/reserved ranges, outbound SMTP
- everything else (the public internet): allowed, unless a network allowlist
  is in force (`metacomputer net allow NAME... --for D`): then only the names on it
  (SECURITY.md, "Network allowlists")

The gateway reads `RUN_DIR/netgw-rules.json`, which `metacomputer start` (and
`metacomputer net`) writes from `config/netgw-rules.json` plus the list in
`RUN_DIR/net-allow.json` (lib/netpolicy.py), checked with `mcomp-netgw -check-rules`
first; the gateway reloads it within a second of a change.

The patch (`patches/netgw/`, applied by `scripts/build-tools.sh` to a clean export of the pinned
source; the gateway's own tests plus the patch's run against it) changes these things, each needed for
an allowlist to hold: the addresses a name on the list resolves to are kept (before, a default deny
removed every answer, so an allowed name resolved to nothing); a PTR question follows the same name
rules as any other (before, any PTR question went upstream, which carries data out in its name); with
name rules in use, a name with escaped characters is refused (the rules compare presentation text, and
an escaped dot would let `a\.b.example.com` pass for a subdomain of `b.example.com`); a question
forwarded upstream carries nothing but itself (not the guest's other sections or EDNS options); a
connection is checked again once it's registered (a rule change while it was being dialled was missed);
every answer is bound to its name, allowed or not (so an answer the guest cached before a list
started still leads to its name; a binding grants nothing to a name the rules don't allow);
and the control socket answers `rules` with the sha256 of the rules file in force, reported only once
the connections the new rules refuse have been closed, which `metacomputer net` compares with what the
saved list makes. With the stock kind of rules file (everything
public accepted), the gateway behaves as cua-netgw does.

Blocked attempts are logged to `logs/netgw-blocked.jsonl` (with an event
sink configured, the event watcher reports new ones; see "Event stream"). Inbound: there is
no route to the guest at all except the two forwards, which listen on
127.0.0.1 only. The guest's ufw additionally admits only 22 and 5900 from the
gateway address. There is no vmnet bridge, so the guest never appears on
your LAN.

Verified from inside the guest: example.com and wikipedia.org reachable; the
LAN's router and other LAN devices, the Mac's LAN and tailnet addresses,
100.100.100.100, the gateway and 169.254.169.254 refused, each with a logged
reason.

## Recording

Two recorders. Where each keeps its file while it records decides whether a VM
that turns hostile mid-run can erase it (SECURITY.md, "Recordings"):

1. **Driver-owned (per automation run):** `start_recording` with
   `record_video: true` on the agent's MCP connection: ffmpeg `x11grab` (1920×1080,
   30 fps, H.264) of the
   X display plus a trajectory folder per action (`turn-NNNNN/` with
   before/after accessibility state, before/after window images, click
   markers, `action.json`), all written inside the guest. The test harness uses this for every test (in a
   driver session of its own, through the hub) and copies everything to
   `recordings/<run>/` and `results/<run>/<test>/guest/`, unpacked as it streams
   in, with the same limits as `metacomputer get` (`guest_import.import_from_command`).
   For a task folder (`metacomputer task start`), a copier on the Mac (`_task-sync`,
   detached; pid file `RUN_DIR/task-sync.NAME.pid`) lists the folder's recording every
   `TASK_SYNC_INTERVAL` seconds (`vm_answer`: at most 4 MB within 60 s) and asks for each
   turn but the newest that hasn't come through whole yet, at most 500 at a time: names
   other than `turn-<digits>` are never used. The copy runs `guest_import --only-top
   LIST --keep-existing turn- --timeout 300 -- ssh ...`: only the folders asked for are
   taken from the VM's answer, no file already on the Mac is replaced (by a file or a
   folder, under any spelling), and the copy has a deadline. A turn is marked done
   (`RUN_DIR/task-sync.NAME.done`) only after a whole copy, so one cut short is asked
   for again. It ends when
   `task finish` removes its pid file (killed with its process group after 30 s),
   after the folder has been missing four times in a row, after eight failed
   listings, after an hour without a new turn, or after a day. `task start
   --no-sync` starts none (`web` checks without `--record`, which never call
   `task finish`). `task finish` then copies the folder with
   `guest_import --keep-existing recording/turn-` (`vm_tar_copy`: it ends only once the VM has sent
   nothing for 120 s): a turn file the Mac already has
   stays as it is, and one the VM changed since is named and counted in the
   `task.finished` event. The video is only whole at `stop_recording`, so it
   arrives at `task finish`.
2. **Whole-desktop (any task, any driver):** `metacomputer record start|stop` runs
   `desktop-record` (ffmpeg x11grab, visible pointer, 20 fps, H.264) and
   saves to `recordings/manual/` (or `RECORD_DIR`). With `RECORD_TO=host`, the
   default, `desktop-record stream` writes Matroska to stdout over SSH, and the
   Mac's half (`_record-pipe`, detached) is `lib/recording.py save`: it runs that
   ssh, writes what arrives to a new `.mkv` (exclusive, no-follow), and stops the
   stream before the Mac's free space falls below `IMPORT_MIN_FREE_GB`. `record
   stop` asks the VM's recorder to stop (its answer through `vm_answer`: at most 4 KB
   within 60 s), waits up to 30 s for the stream to end (none if the VM can't be
   reached), then `recording.py finalize` remuxes to MP4 without re-encoding, only if a
   copy of the `.mkv`'s size (plus two 512 MiB margins) fits above the reserve and what
   other operations have claimed. It claims that space (`lib/storage.py`'s free-space
   claims, which S3 syncs use too, and which `guest_import` and `recording.py save`
   count against the reserve) until ffmpeg ends, and watches ffmpeg every 0.2 s: free
   space within 512 MiB of the reserve, an MP4 past its limit (`-fs` is only advice),
   a failure or a signal stops it, deletes the partial MP4 and keeps the `.mkv`. A
   stream whose Mac side ended before `record stop` is marked `stream_ended_early` in
   the description file. With `RECORD_TO=guest` the MP4 is written in
   `~/metacomputer-runs/manual/` and copied at stop (`vm_copy_video`: `recording.py
   save` with the reserve, `VM_DISK_BYTES` and deadlines: 60 s without a byte, an hour
   in all); `record start` and `record status` in that mode run under `run_until` (60
   s, `MCOMP_RECORD_WAIT`).
   `stream`, `stop` and `status` keep their state in `/run/user/UID/metacomputer`,
   not the home folder.

`metacomputer reset` (and `restore`) finishes a desktop recording, whether or not the
VM is still running, then syncs `~/metacomputer-runs` from the guest, before replacing
the VM; `stop` does the same with a VM that's already off, and `record start` saves a
streamed recording whose stream had ended before it starts a new one.

## Lifecycle

| Command | Implementation |
| --- | --- |
| `start` | check and lock the storage (`VOLUMES`, `REFDISKS`; none with `--no-volumes`), start mcomp-netgw (fresh per run: a gateway serves one VM process), `lume run --display none --vnc disabled --network gateway:…` plus `--no-share-*` for each `SHARE_*` that isn't `on` (when the Lume has the flags) and the storage's `--disk`/`--mount`/`--usb-storage`, wait for SSH, mount the storage, wait for the XFCE session and an MCP handshake |
| `stop` | stop the hub, `lume stop` (ACPI power button → systemd poweroff; forced after 90 s), stop the gateway, release the storage locks |
| `status` | Lume state, gateway + forwards, DHCP lease, SSH, `guest-status` (Xorg, XFCE, AT-SPI, resolution, active window, driver doctor), MCP probe (through the hub, as a status client), the hub (pid, clients, idle time) |
| `reset` | finish a desktop recording → sync guest recordings (`sync_or_stop`: stop here, deleting nothing, unless the copy is whole or `--discard-unsynced`) → stop → `lume delete` working VM → `lume clone` baseline → start |
| `baseline` | sync guest recordings (`sync_or_stop`, as for `reset`) and remove them from the VM, then re-snapshot the working VM (previous baseline kept as `-prev`) |

The VMs are not started at login and no LaunchAgent or daemon was added.
With an event sink configured (or a sink-change announcement still waiting),
`start` also starts the event watcher (a detached process, like the gateway) and
`stop` stops it; see below.

## Placement and providers

Every computer has a name and a place. Every one is on this Mac (`mac`); `cloud`
is the group of providers' places, and has none.

- **The default computer.** `config/default-vm` (not committed; written by
  `metacomputer default NAME`) names the VM a command acts on when it names none: with
  no `--vm`, `MCOMP_VM` or `MCOMP_CONFIG`, `scripts/lib.sh` chooses it exactly as `--vm
  NAME` would, and passes it on to what the command starts as its settings file (the main
  VM, chosen for want of a pointer, as `MCOMP_NO_DEFAULT_VM=1`), so a pointer written
  meanwhile never moves what a command started. One reader, `lib/defaultvm.py`, reads it
  for both sides: opened once without following a link or waiting on a FIFO, checked
  through that descriptor to be a regular file, exactly the bytes read checked (one name on
  one line). `lib/labconfig.py` resolves it with `scripts/lib.sh`'s own `mc_load_settings`,
  in a bash that reads no startup file and imports no function, which hands the loaded
  settings back as values in one strict record on a pipe of its own; Python uses them as they
  are, never reading the file again. That pipe is a named one in a private folder, opened only
  by the step that writes the record (nothing the resolution started holds it), and the bash
  runs in a session of its own, killed with its whole process group on a timeout or an oversized record.
  A pointer that doesn't resolve refuses, never another
  VM, and never the defaults; `vm delete` refuses (without `--force`) while it can't be read,
  and a lookup of it that fails (no permission, an I/O error) counts as that: only the reader's
  "no such file" means there's no pointer. `default NAME` holds NAME's
  lock (as `vm delete` does) from the check to the write. Without a pointer, the main VM
  (`config/local.env`) is the default, as before. `MCOMP_NO_DEFAULT_VM=1` leaves it
  out; `setup`, `update`, `version`, `each`'s `main` and `install.sh`'s build leave it out
  too, so they always mean the main VM.
- **Which computer.** `lib/placement.py` resolves what was said: a computer's name;
  else a provider's name (its one computer, or the default if it's among several there);
  else a place (`mac`, `cloud`: the default if it's there, else the one computer
  there); else the default. More than one candidate, or none, refuses with a list or a
  reason; so does a word that is both a computer's name and a place's. `bin/metacomputer
  --on PLACE` matches places only (with `--vm` too, the VM must be at that place), and
  `vm create` refuses places' names for VMs. `metacomputer list` (= `vm list`, `lib/vms.py`) shows each
  computer's place and marks the default.
- **A session's computer** (`lib/sessionvm.py`). An agent session's MCP tools act on the computer its bridge
  (`lib/mcp_lazy.py`) started on, the default computer then, until `select_computer` chooses another. The bridge answers
  its two tools itself: `list_computers` runs `metacomputer list --json` and marks the session's computer;
  `select_computer` resolves the name with `metacomputer --vm NAME hub env --json` from the environment the bridge
  started with (bin/metacomputer names its MCOMP_ variables in `MCOMP_BRIDGE_KEYS`; any computer's settings and choice
  left out), then connects to that computer's own hub, one hub per computer, and closes the connection to the one before
  (whose hub lets go of the session as when it ends; the bridge's exit waits for that). Each claim a hub writes carries
  the client connection that wrote it (`conn`: the hub's pid and the client's number), and a client that goes drops only
  a claim it wrote (`owners.drop_claim(pid, conn)`), never one a later connection in the same name wrote. The shell
  command the result gives is bare only when `metacomputer hub env --json` with no computer chosen reaches the same
  computer (name, SSH port and folder), else `sessionvm.naming`'s (`--vm`, or its own `MCOMP_CONFIG` file). A hub says
  so in its hello (`features: ["conn_claims"]`), and the bridge switches away from a computer only through a hub that
  does: through an earlier one it refuses and changes nothing (its connection stays, so that hub sees no disconnect),
  remembers that hub (pid and start time) so every later switch away from its computer is refused while it runs, and
  says replacing it is the user's call (`hub stop`). Messages are handled on a thread of their own; a signal handler
  only sets a flag, the agent going away and the end of input set the same one, and the main loop makes the shutdown
  (`finish`) once no switch or close is under way (`begin_switch`/`end_switch` around each), so every connection being
  let go of is one the shutdown waits for; from then on `connect` opens nothing (it looks at `stopping` under the lock
  the shutdown takes the current connection with) and the message thread answers new requests with an error. A computer
  with `MCP_HUB=off` other than the start one is refused: an in-process core keeps its claims where the process's owners
  module looks. The bridge leaves a record of the session's computer (from its start, and at each selection), and so
  does every claim made in an agent session's name (`owners.write_claim`), in a private per-account folder that every
  checkout reads (the account's own temporary folder, or `MCOMP_SESSIONS_DIR`). Before a command that names no computer
  (no `--vm`, `MCOMP_VM`, `MCOMP_CONFIG` or pin), `bin/metacomputer`'s `session_guard` runs `sessionvm.py guard`: the
  session above it (`owners.claude_of`), its records that stand (each names its processes by pid and start time: a
  bridge still running under it; a claim of its in that computer's owners folder, in that holder's name), and the
  computer they make the session's (its bridges', else its claims'). Another computer refuses an acting command, naming
  both and the ways on (`--vm`, or `select_computer`); a read gets a note. A command that passed passes on
  `MCOMP_GUARD_VM`, so what it starts for the same VM isn't asked again. Any program running as the user can write a
  record: it guards against accidents, not a program set on getting past it.
- **Providers** (`lib/cloud/`). `adapter.py` is the interface every provider adapter
  implements: lifecycle (create, wake, sleep, delete, status, list), transport (an SSH
  proxy command, port forwards), snapshots, egress, connection (setup, doctor) and cost
  (rates, estimate). A method for something the provider lacks raises `NotSupported`
  with the reason. `capabilities.py` has the capability manifest each adapter carries
  (the storage capabilities apart: `root_disk`, `volumes`, `snapshots`, `backup`,
  `bucket_mount`; then sleep and wake, the egress model, port forwards, sizes,
  regions), `PERSIST=auto|disk|volume|backup|none` (`auto`: the first the manifest
  offers, in that order; another mode must be one it offers) and the feature map
  (each feature, the capabilities it needs, and the reason when one is missing).
  Two adapters are registered: `local-lume`, which describes this Mac (a persistent
  disk, volumes and Mac folders, checkpoints, the gateway's allowlist, no cost) and
  hands every action to the existing command (`HandledByCLI`: the lifecycle code in
  `bin/metacomputer` and `scripts/` is what runs, unchanged), and `fake`, an in-memory
  stand-in for the tests that never counts as a provider. `tests/test_cloud_adapters.py`
  runs the same contract against each registered adapter.

## Event stream (optional)

`lib/events.py` ([docs/events.md](docs/events.md)) sends events to sinks the
user adds with `metacomputer events sink add`: a CloudEvents webhook signed
with HMAC-SHA256, OTLP/HTTP JSON logs, RFC 5424 syslog over TLS (RFC 5425
framing) or ntfy. With no sink, nothing is sent and events only reach the
local log, `$LOG_DIR/events.jsonl`. Its scope: hostile text from the guest
and the web, floods and mistakes in metacomputer's own code, and telling the
user about sink changes. It doesn't defend against a program running as the
user, which can read `events.json` and post to the sinks directly (SECURITY.md).

```text
emitters                                  lib/events.py                          sinks
bin/metacomputer (start, stop, reset,  ┐   clean → number (seq, under a lock)   ┌ webhook (CloudEvents)
  record stop, task finish; build-vm)  │   → local log                          │ otlp (Collector)
lib/mcp_hub.py (in use, Chrome guard,  ├─▶ → each matching sink's backlog ──────┤ syslog+tls
  bounded refusals, controllers)       │   → send now, 3 s per sink;            └ ntfy
lib/mcp_relay.py (action-log records)  │     else retry with backoff, in order
actionlog verify, web open-untrusted   │
metacomputer notify (Claude)           ┘
spool files (the secrets vault, …)  ──▶ taken on every emission and by the watcher
events watcher (30 s): spool, backlogs, netgw-blocked.jsonl (checked every 2 s), health every 5 min
```

- **Where it runs.** In the process that emits: commands emit before they
  return (at most about 3 s, sinks in parallel); the hub and the relay emit on a
  background thread so a tool call never waits. A sink in its backoff isn't
  tried at all.
- **State.** `~/.config/metacomputer/events.json` (sinks, 0600) and
  `events-state/` beside it: the sequence counter and a global lock; per sink
  and generation an append-only backlog file, a count of what was queued and
  its delivery status; one lock per sink name; debounce marks; the family and
  phone windows; pending sink-change announcements; the last configuration
  `events sink` wrote; and the watcher's place in the gateway log. Events are
  appended to backlogs under the global lock (which also re-reads the
  configuration), in sequence order; one process at a time sends a sink's
  backlog, under that sink's lock, after checking its generation is still the
  current one. `MCOMP_EVENTS_DIR` moves all of it.
- **Origin and limits.** Each event records the code path that made it
  (`cli` for the commands' own Python code, `bridge`, `watcher`, `spool`,
  `claude` for `notify`, `script` for `events emit`, which the shell hooks in
  `bin/metacomputer` and `scripts/lib.sh` use too); `events emit` and the spool
  can't use the types only metacomputer's own code makes. Origins tell code
  paths apart; they don't authenticate producers. A strict sliding window per
  family lets at most 10 events in any 10 minutes reach the sinks; the rest are
  logged locally and summed up in one `suppressed` event (only code paths can
  exempt an event: sink tests, the relay's `actions`). Each phone (ntfy) sink
  also has its own cap, 10 in any 10 minutes and 30 in any hour, taken right
  before each network attempt (retries and sink-change announcements included);
  a message without a slot waits in order, it isn't dropped.
- **Sink changes.** `events sink add` and `remove` need a terminal on stdin.
  The change is one transaction under the sink's lock and then the global lock:
  it re-reads the configuration, refuses if the sink's generation isn't the one
  the user confirmed, captures the sinks that existed before it, writes the
  announcement (`security.sink_added`/`sink_removed`) to a journal in
  `events-state/admin/` as *prepared* with the hash of the configuration it's
  about to write, writes the configuration, and marks the record *committed*.
  On the next run, a prepared record whose hash matches the file is committed
  and one that doesn't is dropped. Replacing or removing a sink makes a new
  generation (the retired one's files go). Announcements are retried from the
  journal until delivered (or given up after 20 attempts), outside the backlogs;
  several waiting for one phone go as one `sink_changes` message. A change to
  `events.json` made any other way is noticed against the hash of the last
  configuration `events sink` wrote, journaled before it's accepted, and
  announced the same way (`security.sink_config_changed`) to that
  configuration's sinks. An unreadable `events.json` is used as no sinks
  (events still reach the local log), reported by `events status` and the
  error log, and announced once. Best effort: a program running as the user
  can also edit `events-state/`.
- **The watcher.** `events.py watch`, started by `start` when a sink exists
  or an announcement is still pending (`events.py needs-watcher`; it exits when
  neither holds), pid in `$RUN_DIR/events-watcher.pid`. Every 30 s: the
  configuration check, the spool, pending announcements, due
  backlogs, held-back summaries, and new lines of `$LOG_DIR/netgw-blocked.jsonl`
  (destination, port, protocol, reason). A thread of its own stats that log every
  2 s, whatever the cycle is doing: the first blocked connection after 5 quiet minutes is sent at once (at
  most 6 an hour); the follow-ups are batched to one event a minute, slowing
  to once an hour for the same destinations. Every 5 min: `metacomputer
  _health` (Lume state, gateway, SSH, `guest-status`, the desktop recording),
  with one event per change and a 30-minute quiet period for a condition that
  comes back. Each step runs on its own, so one failing doesn't stop the others.
  Its pidfile stays locked while it runs (one watcher per VM). It never opens
  an MCP connection.
- **Cleaning.** Every string loses control and invisible characters, escape
  sequences, and links' credentials, path parameters, queries and fragments (to
  the next space, also without a scheme); sink header values, signing keys,
  secret URL parts and the VNC password are redacted; secret-looking keys are
  dropped from `data`; sizes are capped. Text from inside the VM goes only into
  `data`, never into a message.

### Event spool

The contract for programs that produce events without calling metacomputer,
such as the secrets vault ("approval waiting", "vault locked"):

- **Folder:** `~/Library/Application Support/metacomputer/events/spool/`, or
  `$MCOMP_EVENTS_SPOOL`. The producer creates it if missing, 0700. Files
  in it must be regular files owned by the same user; anything else is
  skipped.
- **One file per event,** named `<time>-<random>.json`, where `<time>` is UTC
  `YYYYMMDDTHHMMSS.ffffffZ` (fixed width, so names sort in time order) and
  `<random>` is at least 8 letters or digits.
- **Written atomically:** write the content to a name that starts with `.` or
  doesn't end in `.json` (for example `.<name>.tmp`) in the same folder, then
  rename it to the final name. Such temporary names are never read.
- **Content:** one UTF-8 JSON object of at most 64 KB:

  | Field | Required | |
  | --- | --- | --- |
  | `family` | yes | `needs_you`, `task`, `security`, `lifecycle` or `actions` |
  | `type` | yes | lowercase letters, digits and `_`, at most 40 (`^[a-z0-9][a-z0-9_]{0,39}$`) |
  | `message` | yes | one line for a person |
  | `severity` | no | `info`, `warn` or `error`; default by family (`needs_you` and `security` warn, the others info) |
  | `vm` | no | the VM's name; default: the VM of the process that takes the file |
  | `action` | no | what the person should do |
  | `data` | no | a small object |
  | `project` | no | a folder path |

  Other fields are ignored (an `origin` in the file too: these events always
  say `spool`). The event's time is the file's modification time.
- **Reserved types:** a file can't use the types only metacomputer's own code
  makes (`suppressed`, the `actions` family, the bridge's, the watcher's, sink
  changes and tests; docs/events.md lists them).
- **Taking them:** on every emission and every watcher cycle, under the
  global lock, oldest name first (up to 500 per pass, counted after skipping
  temporary names, folders, links and files that aren't yours). The folder is
  pinned as a descriptor and everything is relative to it; each file is opened
  with `O_NOFOLLOW|O_NONBLOCK`, checked again with `fstat` and read only up to
  the size limit plus one byte; quarantine moves are relative to the same
  descriptor. Spool events
  count toward their family's rate limit. A valid file is
  cleaned like any event (so a producer can't leak a URL query or a
  secret-looking key through it), numbered, logged, queued for each matching
  sink, then deleted; delivery then follows the sinks' backlogs. An invalid
  (any parse failure, too deep included), oversized or reserved file, or one
  that can't be recorded, is moved to `spool/.quarantine/` (the newest 50 kept)
  and counted (`events status --json`, `spool_rejected`) without stopping the
  others. One that can't be moved there (`.quarantine` isn't a folder of yours)
  is left in the spool, counted (`left`) and reported once: nothing is deleted
  that couldn't be quarantined. Before a pass takes any file, and when the
  watcher starts, the spool is checked with `lib/safedelete.py` (`check_spool`):
  one that is, holds or reaches another protected folder isn't consumed.
- **Guarantees:** at least once to each sink, in order with everything else.
  Files wait in the spool while nothing runs (no command, no watcher). With no
  sink configured, taken events reach only the local log.

## Learning from sessions (`metacomputer learn`)

`metacomputer learn` ([docs/learn.md](docs/learn.md)) closes a loop on the Mac:
mine the user's own sessions, propose lessons, have the user approve them, give
them to later sessions, and measure the difference.

```text
~/.claude/projects/**/*.jsonl ─┐                     ┌─▶ built-in templates (lib/learn.py)
saved stream-json transcripts ─┤                     │   └─ --draft-with-claude: claude -p, counts only
results/bench/*/transcript ────┼─▶ lib/learn_mine.py ┤
LOG_DIR/actions/, events.jsonl ┘   (structured only)  └─▶ proposals.json ──approve──▶ lessons.json
                                                                                       (0700/0600, private)
mcp_lazy / hub: initialize ─▶ IDENTITY + HABITS + learned_notes() + the driver's text
```

- **Reading** (`lib/transcripts.py`, shared with `tests/skill/analyze.py`): both
  transcript formats, Claude Code's session files and headless stream-json, into
  an ordered list of tool calls with results and times. A file is parsed only if
  it holds a tool_use of a metacomputer tool (either prefix); the others are only
  scanned for that marker. A session saved both ways is counted once.
- **Mining** (`lib/learn_mine.py`): each failed driver call gets a class from fixed
  patterns over the driver's and the hub's messages (for a `run_steps` report, its
  failing step's line), each action a route (tool, target kind, delivery mode,
  scope), each call an app from a fixed list (learned from `list_windows`,
  `launch_app` and read results by window id and pid), each tool name the driver's
  own or `unknown_tool`. It keeps only those, counts and times; the VM's text is
  never copied. Transcripts are mined one at a time, read with size, event and time
  bounds, and a file that fails is counted as unreadable.
- **Proposals** (`lib/learn.py`): templates keyed by pattern, filled with counts and
  fixed names; optional drafts from one tool-less `claude -p` session that gets the
  structured summary only, checked before they are proposed.
- **Store** (`lib/learn_store.py`): `~/.local/share/metacomputer/lessons/`
  (`LESSONS_DIR`), written atomically under a lock, read only when private. Each
  change through `learn` appends the store's hash to `approvals.jsonl` (and to its
  event); lessons are sent only while `lessons.json` matches the last one, and a
  mismatch with the event log is reported. This makes tampering visible; a program
  running as the user can still rewrite both (SECURITY.md).
- **Delivery**: `mcp_hub.initialize_result()` appends `learned_notes()` after the
  habits. Claude Code's bridge (`mcp-lazy`) answers `initialize` itself, from its
  own environment, so the lessons and `LESSONS` follow each client (the benchmark's
  `--lessons` switch relies on this); for the clients the hub answers, each hello
  carries the client's `LESSONS` and store (`lessons_of()`), and the hub uses them.
  The hub's status reports the store's hash. No guard reads the store, and lessons aren't part of the hub's settings.
- **Measuring**: `learn bench` runs `bench/bench.py run --lessons off`, then `on`,
  on a test VM, and compares the two with `bench/report.py`. The VM, baseline and
  settings hash it showed are checked again before every reset (`BENCH_EXPECT_*`),
  and `MCOMP_EXPECT_VM` makes `bin/metacomputer` refuse any other VM.

## Known limitations

- **Lume VNC is not loopback-only.** `_VZVNCServer` listens on every interface;
  `metacomputer` always runs `--vnc disabled` and uses guest x11vnc behind the
  gateway's loopback forward instead.
- **`lume stop` is a hard power-off for detached Linux VMs.** `metacomputer stop`
  shuts down from inside the guest and uses Lume's stop only as a fallback.
- **One gateway per VM run.** A gateway keeps talking to the Lume process it
  first heard from, so `start` always launches a fresh one (and kills it on
  stop: it ignores SIGTERM while a forwarded connection is stuck).
- **No compositor.** With xfwm4 compositing on, client-side-decorated windows
  (Firefox, GTK4) draw shadows that skew the driver's pixel mapping by ~20 px;
  so compositing is off, Firefox uses the system titlebar, and the driver's
  cursor halo/float are off (alpha would render black).
- **Firefox text entry needs foreground input** (AT-SPI insertion into its
  fields and synthetic background keys are ignored). Foreground input moves the
  real pointer/focus; harmless in a dedicated VM, but one controller at a time.
- **Focused element** is not reported by the driver on Linux (0.52.1, 0.53.0).
- **Thunar's icon view** has no accessible items; use List View.
- **Host-sharing devices.** Lume attaches a SPICE clipboard channel
  (`VZSpiceAgentPortAttachment`, shares the Mac's clipboard), a virtio sound
  device wired to the host's speakers and microphone, and a Rosetta share to
  every VM. With mcomp-vm (the default), `metacomputer start` leaves each out
  unless its `SHARE_*` setting is `on`; `metacomputer status` reports what the
  guest actually has (virtio console ports, a 0x0019 sound device, a virtiofs
  Rosetta tag). With standard Lume they are always attached: the guest has no
  `spice-vdagent` and `virtio_snd` is blacklisted, but a root-compromised
  guest could start an agent and read or set the Mac's clipboard, so don't
  keep secrets on the clipboard while it runs untrusted tasks.
  `VIRTUAL_AUDIO=on` doesn't change this: it adds PulseAudio inside the guest
  with a single null sink (apps play into it, `desktop-record` captures its
  monitor), and `virtio_snd` stays blacklisted.
- **Guest egress is open to the public internet** (by design, for browsing);
  everything private (LAN, tailnet, the Mac) is refused on the host.
- **Unattended upgrades** patch the working VM after each reset; refresh the
  baseline occasionally with `metacomputer baseline` (or rebuild) so resets start
  current.
