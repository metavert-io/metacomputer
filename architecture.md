# metacomputer architecture

A disposable Ubuntu 24.04 ARM64 XFCE desktop, running as a Lume virtual machine
on your Mac, that Claude Code operates through Cua Driver's MCP server.
The controller (Claude Code, lifecycle scripts, test harness, evidence) stays on
macOS; every graphical action happens inside the guest.

```text
Your Mac (Apple silicon, macOS 14+) - trusted controller
│
├── Claude Code ──stdio──▶ bin/metacomputer mcp-lazy ─▶ bin/metacomputer mcp ──ssh──┐
│     (metacomputer skill; one controller at a time: .run/owners/)     │
├── tests/run_tests.py (MCP client, direct bridge `metacomputer mcp`)            │
├── bin/metacomputer (start/stop/status/reset/test/record/recordings/view)        │
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
            ├── Firefox (Mozilla .deb), LibreOffice Writer (gtk3 VCL),
            │   GNOME Calculator, Thunar, Mousepad, XFCE Terminal
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
| lume-secure (default) | Lume 0.7.2 (`lume-v0.7.2`, `f30fbc0`) + one patch: `lume run --no-share-clipboard / --no-share-audio / --no-share-rosetta / --no-host-sharing` (Lume's default sharing is unchanged); built from source, ad-hoc signed with Lume's virtualization entitlement | `~/.local/share/lume-secure/`, wrapper `~/.local/bin/lume-secure` | VM lifecycle on Virtualization.framework; telemetry disabled; no LaunchAgent |
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
each live connection), so the tools are listed even while the VM is off. On the
first `tools/call` it starts the direct bridge, `bin/metacomputer mcp`, and relays
from then on. While the VM is off, a call returns "start it, then retry". A
dropped connection is reopened on the next call. Its claims in `.run/owners/`
(`lib/owners.py`) let only one session act at a time: acting calls are
refused while another session acted within 10 minutes, reads are always
allowed, and `metacomputer stop/reset/restore/test` refuse too unless given
`--override`.

The bridge also guards Google Chrome (docs/chrome-investigation.md). When
the driver delivers background input through its own X input devices (a
"CUA v1..." master pointer/keyboard pair, created by XInput2 multi-pointer),
Chrome ignores that input, and while such a pair exists Chrome ignores all
keyboard input, for any app's background input, until the pair is removed.
So the bridge:
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

The direct bridge, `bin/metacomputer mcp`, is `ssh -T` with the
`mcp` key to `cua@127.0.0.1:2277`. In the guest that key is
`restrict,command="/opt/metacomputer/bin/driver-mcp"`: no shell, no PTY, no
forwarding. The wrapper loads the XFCE session's environment (published at
login to `$XDG_RUNTIME_DIR/metacomputer/session.env`: `DISPLAY`, `XAUTHORITY`,
`DBUS_SESSION_BUS_ADDRESS`) and execs `cua-driver mcp`. MCP's stdio stream is
carried end to end by SSH; each MCP connection owns one driver runtime.
Nothing on the Mac drives the Mac's own desktop: Cua Driver is not
installed on macOS at all.

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
| `$SSH_DIR/ops` | host, 0600 | `cua@guest` shell (no sudo, no forwarding) for status, recordings, artifact checks, `metacomputer inspect` (read-only snapshot piped from `lib/guest_inspect.py`) |
| `$SSH_DIR/admin` | host, 0600 | `cuaadmin@guest`; after build `sudo` only `/usr/local/sbin/metacomputer-admin` (fixed verbs) |
| `$SSH_DIR/hostkey` | host, 0600 | the guest's SSH host key, generated on the host and pinned in `known_hosts` (strict checking from the first connection) |
| VNC password | `$SSH_DIR/vnc-pass` (0600), or your own secret store (`VNC_PASSWORD_STORE=command`); guest `~cua/.config/metacomputer/vnc-pass` 0600 | live view / takeover |

Guest accounts have locked passwords; SSH is key-only with `AllowUsers cua cuaadmin`.
No macOS credentials are used anywhere.

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

Blocked attempts are logged to `logs/netgw-blocked.jsonl`. Inbound: there is
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
| `stop` | `lume stop` (ACPI power button → systemd poweroff; forced after 90 s), stop cua-netgw |
| `status` | Lume state, gateway + forwards, DHCP lease, SSH, `guest-status` (Xorg, XFCE, AT-SPI, resolution, active window, driver doctor), MCP probe |
| `reset` | sync guest recordings → stop → `lume delete` working VM → `lume clone` baseline → start |
| `baseline` | re-snapshot the working VM (previous baseline kept as `-prev`) |

The VMs are not started at login and no LaunchAgent or daemon was added.

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
  every VM. With lume-secure (the default), `metacomputer start` leaves each out
  unless its `SHARE_*` setting is `on`; `metacomputer status` reports what the
  guest actually has (virtio console ports, a 0x0019 sound device, a virtiofs
  Rosetta tag). With standard Lume they are always attached: the guest has no
  `spice-vdagent` and `virtio_snd` is blacklisted, but a root-compromised
  guest could start an agent and read or set the Mac's clipboard, so don't
  keep secrets on the clipboard while it runs untrusted tasks.
- **Guest egress is open to the public internet** (by design, for browsing);
  everything private (LAN, tailnet, the Mac) is refused on the host.
- **Unattended upgrades** patch the working VM after each reset; refresh the
  baseline occasionally with `metacomputer baseline` (or rebuild) so resets start
  current.
