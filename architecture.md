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
├── scripts (lib/mcomp_sdk.py) ───────────────────────┤ RUN_DIR/hub/sock     │
│                                                     ▼ (0600, uid-checked)  │
│   the hub, lib/mcp_hub.py: one per VM; every guard, every client           │
│     one controller (.run/owners/) · Chrome guard · bounded mode ·         │
│     coordinate guard · action-log notes · events                          │
│     └── bin/metacomputer mcp (MCOMP_DIRECT_BRIDGE=1) ──ssh─────────────────┐
├── tests/run_tests.py, web (MCP client, direct bridge `metacomputer mcp`)    │
├── bin/metacomputer (start/stop/status/reset/test/record/recordings/view)    │
│                                                                            │
├── cua-netgw (user-mode gateway, host-enforced egress policy)               │
│     127.0.0.1:2277 ─▶ guest :22      127.0.0.1:5977 ─▶ guest :5900 (VNC)   │
│     unixgram socket ◀──▶ Lume VM network device                           │
└── Lume 0.7.2 (Apple Virtualization.framework, native arm64)               │
      └── VM "metacomputer" (4 vCPU, 8 GB, 50 GB sparse, 1920x1080 virtio-gpu)  │
            Ubuntu 24.04.x LTS arm64 (official cloud image, cloud-init)      │
            LightDM autologin ─▶ Xorg (modesetting) ─▶ XFCE 4.18             │
            user `cua` (no sudo)                                             │
            ├── /opt/metacomputer/bin/driver-mcp ◀────────────────────────────┘
            │     └── cua-driver 0.52.1 mcp (stdio; X11/EWMH, XTest, uinput,
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
| cua-netgw | built from trycua/cua `ca6a643` (`libs/cua/netgw`) | `vendor/bin/cua-netgw` | user-mode network for the VM (Lume `gateway:` mode); egress rules; loopback forwards |
| Cua Driver | 0.52.1 linux-arm64 (sha256 `ea8229f4…`) | guest `/opt/cua-driver/0.52.1` | desktop observation/control, MCP server, recording |
| Ubuntu | 24.04 LTS arm64 cloud image, release 20261017 (sha256 `50e24e1f…`, SHA256SUMS signed by `D2EB4462…7C81`) | VM disk | guest OS |
| qcow2raw | `tools/qcow2raw` (Lima's go-qcow2reader v0.8.0) | `vendor/bin/qcow2raw` | converts the qcow2 cloud image to the raw disk Lume boots |
| Firefox | Mozilla APT repo (arm64 .deb; key `35BAA0B3…15A3`) | guest | default browser (no snaps in the guest) |
| Google Chrome | Google's APT repo (arm64 .deb; key `EB4C1BFD…4796`), optional (`BROWSERS`) | guest | second browser; the driver's page tools |

Exact package versions installed in the guest are in `/etc/metacomputer/versions`
and `/var/lib/metacomputer/package-versions.txt` (copied to `logs/` at build).

## Provisioning (unattended)

`metacomputer build` → `scripts/build-vm.sh`:

1. `build-tools.sh` builds cua-netgw (pinned commit) and qcow2raw.
2. `fetch-artifacts.sh` downloads and verifies the Ubuntu image (GPG-signed
   SHA256SUMS, pinned key fingerprint, pinned digest), the Cua Driver release
   tarball (pinned digest, cross-checked against the release SHA256SUMS) and
   Mozilla's APT key (pinned fingerprint).
3. `lume create --os linux`, then the cloud image is converted into the VM's
   50 GiB sparse `disk.img` (cloud-init grows the root partition).
4. A NoCloud seed ISO (label `cidata`) carries `user-data` (users, SSH keys,
   a pre-generated pinned SSH host key), `network-config` and `payload.tgz`
   (provision script, guest file tree, the verified driver tarball, the
   Mozilla key). The guest never downloads Cua components itself.
5. First boot: cloud-init creates `cua` and `cuaadmin`, runs
   `provisioning/guest/provision.sh` (purges snapd, adds Mozilla's repo,
   full-upgrade, installs Xorg/LightDM/XFCE/AT-SPI/apps/ffmpeg/x11vnc,
   installs the driver, configures autologin, accessibility, no screen
   blanking/locking, uinput, sshd, ufw, unattended-upgrades) and reboots.
6. The host waits for the XFCE session, installs the VNC password, checks
   readiness (`guest-status`), runs `finalize.sh` (narrow sudo,
   cloud-init disabled, cached user-data removed), stops the VM and clones
   the baseline (`BASELINE_NAME`, default `metacomputer-baseline`).

## Desktop-control transport

Claude Code sessions use `metacomputer` → `bin/metacomputer mcp-lazy`
(`lib/mcp_lazy.py`), registered at user scope so it exists in every
project. It answers `initialize` and `tools/list` from a saved copy of the
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
Code's bridge, `metacomputer call` and scripts (`lib/mcomp_sdk.py`). The idea
comes from Microsoft's Hybrid Computer Use study: one daemon per desktop that
the MCP server, the CLI and an SDK share, so references and state carry over.

- **Where it listens.** A Unix socket, `RUN_DIR/hub/sock`: the folder is 0700 and
  the socket 0600. When that path is too long for a Unix socket (about 104 bytes
  on macOS), the socket goes in a private folder in `/tmp` named after RUN_DIR
  (`/tmp/mcomp-hub-UID-HASH/`), checked the same way. Each connection's peer is
  identified by the kernel (`LOCAL_PEERCRED` and `LOCAL_PEERPID` on macOS,
  `SO_PEERCRED` on Linux, `getpeereid` as a fallback), and anything that isn't
  this user is closed before a byte is read. The first message must be a
  `metacomputer/hello` naming the client's kind (Claude Code, `call`, a script),
  folder, optional driver session and settings.
- **One per VM.** `RUN_DIR/hub/hub.pid` is held under `flock` while the hub runs;
  a second hub for the same VM exits (code 3). A client that finds no socket
  starts one (`mcp_hub.py serve`, detached, logging to `LOG_DIR/hub.log`) and
  waits for its socket. A client whose settings differ (another `ACTION_LOG`,
  `DRIVER_MODE`, `COORD_GUARD`, …) is refused while others use the hub; when
  it's alone the hub restarts with that client's settings.
- **When it ends.** After `HUB_IDLE` (10 minutes) with no client and no
  driver recording (it asks the driver before exiting while one was started
  through it), on `metacomputer stop`, or on `metacomputer hub stop`.
- **Multiplexing.** Each client's JSON-RPC ids are rewritten on the way to the
  driver (`c<client>.<n>`) and restored on the way back, so each answer reaches
  only its client. `initialize`, `tools/list`, `ping` and `logging/setLevel` are
  answered by the hub from the driver's handshake. The driver's
  `tools/list_changed` goes to every client; progress notifications go to the
  client whose token they carry; other notifications aren't any one client's
  and are dropped. Requests the driver makes of its client (`ping`) are answered
  by the hub. A client's cancellation is passed on with the rewritten id.
- **Driver sessions.** Every client shares one driver runtime, so element
  tokens, driver sessions, recordings and a Chrome that `browser_prepare`
  started outlive any one call. Screenshot frames are per driver session (Cua
  Driver 0.52.1 refuses pixels against "a screenshot that belongs to another
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
  call forever.
- **Reconnecting.** When the driver connection drops (the VM stopped or
  restarted), every pending request fails with a plain message, everything the
  hub learned about that runtime is forgotten, and the next call reconnects.

With `MCP_HUB=off` the same core runs inside each client instead, over a
socketpair: Claude Code's bridge and `metacomputer call` each have their own
connection, as before the hub, with the same guards.

### Guards (in the hub, for every client)

- **One controller at a time** (`lib/owners.py`). Acting calls are refused
  while another session acted within 10 minutes; reads are always allowed.
  The check and the claim are one step under this VM's owners lock, shared with
  `metacomputer sh`. Claims are written per client in `.run/owners/`: a Claude
  Code session's under its bridge's pid (dropped when it disconnects), a
  one-shot `metacomputer call`'s under its Claude Code process, or else the shell
  that ran it, like `metacomputer sh`. `metacomputer stop/reset/restore/test`
  refuse too unless given `--override`. The hub's own SSH connection isn't
  counted as a controller; any other direct connection is.
- **Chrome** (docs/chrome-investigation.md). When the driver delivers
  background input through its own X input devices (a "CUA v1..." master
  pointer/keyboard pair, created by XInput2 multi-pointer), Chrome ignores that
  input, and while such a pair exists Chrome ignores all keyboard input, for any
  app's background input, until the pair is removed. So the hub:
  - learns which guest pids are Chrome from results it relays, asking
    `list_windows` when needed;
  - rewrites input tools and `run_steps` aimed at Chrome to
    `delivery_mode: "foreground"`;
  - refuses `set_value` and `run_script` aimed at Chrome;
  - notices virtual-device routes in results (`path=mpx_*`), and before the
    next Chrome keyboard input runs `metacomputer input-reset` (the guest's
    `input-reset`, `xinput remove-master`), noting it in that result.

  A driver session whose pair was removed can't use the background keyboard
  route again until it reconnects; its next such call returns an error.
- **Bounded mode** (SECURITY.md): `launch_app` by name is rewritten to the
  absolute path the manifest allows; a refusal that repeats tells the user
  (events); when the bounded session ends, every client attached to it is told
  to ask the user to reconnect, and stays ended until it does, while a client
  that connects later gets a new driver connection (each connection used to
  have its own session).
- **The coordinate guard** (`COORD_GUARD=on`, below).
- **The action log** (`ACTION_LOG=on`): one log file per driver connection, so
  the hub's connection is one file; before each `tools/call` the hub sends the
  relay a note naming the client (kind, pid, Claude Code pid, folder, driver
  session), which the relay records in the call's record and never passes on.
  Calls the hub refuses are recorded with their client too.
- **Events** (docs/events.md): `in_use`, `chrome_input_refused`, `refused`
  (bounded) and the controller events, with the client's own folder.

### The coordinate guard

Pixel input is aimed from a screenshot. Measured on Cua Driver 0.52.1: it maps
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
`right_click`, `drag`, `scroll`, `type_text`, `mouse_button_down`/`drag`/`up`
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
checks decide. Not covered: popup menus (`list_windows` doesn't list them),
windows named only by title in a batch step, and `run_script`'s `[x, y]`
targets. The active window isn't reported by `list_windows`, and on its own it
doesn't change where a point lands; a window raised over the point is caught
by the stacking check.

The direct bridge, `bin/metacomputer mcp` with `MCOMP_DIRECT_BRIDGE=1`, is
`ssh -T` with the `mcp` key to `cua@127.0.0.1:2277`. It's the hub's own
connection, and the GUI tests and `web` use it for theirs (they check for other
controllers and claim the computer themselves). Without that variable, and with
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
- checks that the VM's port is served by the pinned cua-netgw binary (its path and contents);
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
  blanked, and audits each request. Tokens and tunnels end with the unlock.
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
- **Needs-you events**: while a question waits, the unlock process drops a
  `needs_you.approval_waiting` file into the event spool
  (`~/Library/Application Support/metacomputer/events/spool/`, under the "Event spool"
  contract below); on exit 3 (locked), the event stream's exit hook in
  `bin/metacomputer` reports `needs_you.vault_locked`.

Threat model and limits: [SECURITY.md](SECURITY.md#secrets-and-logins-metacomputer-secrets).

## Network

Lume runs the VM in `gateway:` mode: its network device is a datagram socket
to cua-netgw, a user-mode gateway (gvisor-tap-vsock) on the host. The guest
gets `10.213.77.2/24` by static DHCP lease; DNS goes to the gateway, which
resolves through 1.1.1.1 / 9.9.9.9 (not the Mac's resolvers or MagicDNS).
Every connection the guest opens is checked on the host:

- always refused (cua-netgw built-ins): the Mac's loopback, every address of
  the Mac's own interfaces (LAN and tailnet IPs), the gateway itself, cloud
  metadata, multicast/broadcast
- refused by `config/netgw-rules.json`: RFC 1918 (your LAN, its router and
  devices), 100.64.0.0/10 (Tailscale/CGNAT), link-local, ULA (tailnet v6),
  benchmark/reserved ranges, outbound SMTP
- everything else (the public internet): allowed

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

Two recorders, both writing MP4 inside the guest and copied to the host:

1. **Driver-owned (per automation run):** `start_recording` with
   `record_video: true` on the agent's MCP connection: ffmpeg `x11grab` (1920×1080,
   30 fps, H.264) of the
   X display plus a trajectory folder per action (`turn-NNNNN/` with
   before/after accessibility state, before/after window images, click
   markers, `action.json`). The test harness uses this for every test and
   copies everything to `recordings/<run>/` and `results/<run>/<test>/guest/`.
2. **Whole-desktop (any task, any driver):** `metacomputer record start|stop` runs
   `desktop-record` (ffmpeg x11grab, visible pointer, 20 fps, H.264) and
   saves to `recordings/manual/`.

`metacomputer reset` syncs `~/metacomputer-runs` from the guest before replacing the VM.

## Lifecycle

| Command | Implementation |
| --- | --- |
| `start` | start cua-netgw (fresh per run: a gateway serves one VM process), `lume run --display none --vnc disabled --network gateway:…` plus `--no-share-*` for each `SHARE_*` that isn't `on` (when the Lume has the flags), wait for SSH, the XFCE session and an MCP handshake |
| `stop` | stop the hub, `lume stop` (ACPI power button → systemd poweroff; forced after 90 s), stop cua-netgw |
| `status` | Lume state, gateway + forwards, DHCP lease, SSH, `guest-status` (Xorg, XFCE, AT-SPI, resolution, active window, driver doctor), MCP probe, the hub (pid, clients, idle time) |
| `reset` | sync guest recordings → stop → `lume delete` working VM → `lume clone` baseline → start |
| `baseline` | re-snapshot the working VM (previous baseline kept as `-prev`) |

The VMs are not started at login and no LaunchAgent or daemon was added.
With an event sink configured (or a sink-change announcement still waiting),
`start` also starts the event watcher (a detached process, like cua-netgw) and
`stop` stops it; see below.

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
events watcher (30 s): spool, backlogs, netgw-blocked.jsonl, health every 5 min
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
  (destination, port, protocol, reason; batched to one event a minute, slowing
  to once an hour for the same destinations). Every 5 min: `metacomputer
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
  others.
- **Guarantees:** at least once to each sink, in order with everything else.
  Files wait in the spool while nothing runs (no command, no watcher). With no
  sink configured, taken events reach only the local log.

## Known limitations

- **Lume VNC is not loopback-only.** `_VZVNCServer` listens on every interface;
  `metacomputer` always runs `--vnc disabled` and uses guest x11vnc behind the
  gateway's loopback forward instead.
- **`lume stop` is a hard power-off for detached Linux VMs.** `metacomputer stop`
  shuts down from inside the guest and uses Lume's stop only as a fallback.
- **One cua-netgw per VM run.** A gateway keeps talking to the Lume process it
  first heard from, so `start` always launches a fresh one (and kills it on
  stop: it ignores SIGTERM while a forwarded connection is stuck).
- **No compositor.** With xfwm4 compositing on, client-side-decorated windows
  (Firefox, GTK4) draw shadows that skew the driver's pixel mapping by ~20 px;
  so compositing is off, Firefox uses the system titlebar, and the driver's
  cursor halo/float are off (alpha would render black).
- **Firefox text entry needs foreground input** (AT-SPI insertion into its
  fields and synthetic background keys are ignored). Foreground input moves the
  real pointer/focus; harmless in a dedicated VM, but one controller at a time.
- **Focused element** is not reported by the driver on Linux (0.52.1).
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
