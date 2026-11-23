# A computer on Fly Sprites

A metacomputer can be a [Sprite](https://docs.sprites.dev/) on Fly.io: a persistent Firecracker VM that sleeps by
itself when nothing is connected and wakes in a second or less. It gets the same desktop as the VM on your Mac (XFCE,
Firefox, Chrome if `BROWSERS` has it, LibreOffice, Cua Driver's Linux build), the same accounts and keys, and your Mac
stays the controller: the hub and all its guards, the vault, recordings, the action log and the CLI run here. It's a
computer elsewhere like the others in [docs/remote.md](remote.md); this page says what's different on a Sprite.

| | Fly Sprites (`--on sprites`) |
|---|---|
| Who makes it | metacomputer, in a Fly organization you name, through Fly's `sprite` command |
| Asleep and awake | it pauses by itself about 10 s after its last connection closes (its programs pause with it) and wakes at the next one |
| What it costs | awake: the CPU time it uses, the memory in use and its disk; asleep: its disk only ([below](#what-it-costs)) |
| Egress | Fly's network policy, outside the VM: every name, no name, or an allowlist of names ([below](#the-network)) |
| Snapshots, `reset`, checkpoints | yes: Fly's checkpoints of the whole disk (a restore restarts its programs) |
| Input | foreground only (no `/dev/uinput`): keys, typing and pixel clicks take the focus |
| Architecture | x86_64 (Ubuntu 26.04 on Fly's image) |
| Volumes and Mac folders | no (not yet) |

## Setting it up

1. Install Fly's `sprite` command and sign in once, in your own terminal: `sprite login` (it keeps its own sign-in in
   your login keychain; metacomputer never sees the token). A Fly organization of its own for metacomputer keeps its
   Sprites apart from your other work.
2. Make the computer:

```bash
metacomputer vm create work --on sprites --org YOUR-ORG [--sprite SPRITE-NAME] [--egress internet|off]
```

`vm create` writes `config/vms/work.env`, says the rate and what's kept, and asks before anything is made (`--yes`
agrees). The Sprite is named after the computer unless `--sprite` names it; one that exists already in the
organization is refused (metacomputer never takes over a Sprite it didn't make). `SPRITES_ORG` in
`config/local.env` saves typing `--org`; `SPRITE_CLI` names the command if it isn't on your PATH or in
`~/.local/bin`.

The build (`metacomputer --vm work build`, about 5 minutes):

1. **The Sprite**, through Fly's API, journaled first: a build that fails or is interrupted deletes what it made and
   nothing else (a launch whose answer was lost is found by its name and the time it was made; an older Sprite of that
   name is never touched). `build --keep-on-failure` keeps it instead.
2. **Its network policy**, every name allowed while it's set up (the packages it downloads), then `EGRESS` at the end,
   read back. metacomputer always sets one: Fly enforces nothing for a Sprite without one.
3. **The first step through Fly's own channel** (`sprite exec`, before metacomputer's own SSH exists):
   `provisioning/sprite/bootstrap.sh` makes the accounts `cua` (the desktop, no sudo) and `cuaadmin` (maintenance),
   each reached only with keys made on your Mac for this computer, and starts sshd as a Sprites service on the Sprite's
   loopback. Its host key comes back over that authenticated channel and is pinned here as `mcomp-work`: never learned
   from a first connection.
4. **The same setup as every computer elsewhere**, over SSH through Fly's tunnel: the payload (Cua Driver's
   `linux-x86_64` build, pinned by SHA-256; the APT keys, pinned by fingerprint) and `provision.sh` with
   `MCOMP_TARGET=sprite`. One SSH connection stays open while it runs, so the Sprite doesn't pause under it.
5. **The desktop as Sprites services** (a Sprite has no systemd, cloud-init or display manager): `xvfb` (the X server
   in memory), then `desktop` (XFCE on its own D-Bus session, accessibility on). Then the live-view password, the
   desktop checked, `finalize.sh` (`cuaadmin`'s sudo narrowed to `metacomputer-admin`), `EGRESS` set and read back,
   and a checkpoint at Fly as its **baseline**.

## Using it

Everything works as on any computer: `metacomputer --vm work status`, the MCP tools through the hub, `sh`,
`put`/`get`, `shot`, `record`, `view --open` (the live view through an SSH tunnel to the Sprite's `127.0.0.1:5900`),
`secrets fill` and `token` (below). `list`, `--on sprites` and `default` know it.

- **Sleep and wake.** The hub holds its SSH connection while it's used and lets go after `HUB_IDLE` (10 minutes) with
  no client; about 10 s later Fly pauses the Sprite. `metacomputer stop` closes this Mac's connections now (the hub, a
  recording, the live view's tunnel, the vault's API proxy tunnel), and says so if one is still open. The next command wakes it, with Firefox and everything else as they were. Every
  connection's tunnel runs through `lib/proxywrap.py` (the vault's through `mcomp-vault _tunnel`), which ends Fly's
  `sprite proxy` when ssh goes: a tunnel left open would keep the Sprite awake, and billing.
- **Checkpoints.** `metacomputer --vm work checkpoint NAME [--replace]`, `checkpoints`, `restore NAME`,
  `checkpoint --delete NAME`, `baseline` (the current state becomes the baseline) and `reset` (back to the baseline)
  use Fly's checkpoints, taken while it runs in a fraction of a second. **A restore restarts the Sprite's programs**:
  the desktop comes back as its services (about half a minute), but apps that were open are closed and every
  connection is cut, so launch them again afterwards. Files come back exactly as they were.
- **Input is foreground only.** A Sprite's kernel has no `/dev/uinput`, so the driver has no virtual devices for
  background input. The hub sends keys, typing and pixel clicks in the foreground (the desktop's own pointer and
  keyboard, XTEST) and says so in the result, keys and typing aimed at an element too; clicks on an element
  (`element_token`) still go through accessibility. Fine on a desktop nobody else uses; the live view shows the pointer move.
- **Typing into Firefox pages goes in the foreground**, as on every computer: text inserted through accessibility
  (`type_text` with an element token, `set_value`) reports success but doesn't land in a web page's fields
  ([DESKTOP.md](../skills/metacomputer/DESKTOP.md), Firefox).
- **Logs.** `metacomputer --vm work logs` reads the setup's log and the services' logs through Fly's channel.
- **Updates.** Fly's image isn't upgraded whole, and there are no update timers without systemd: for a newer desktop,
  make a new computer (`vm delete`, then `vm create`).
- **`desktop-restart`** makes the desktop's services again through Fly's channel (every open app closes).

## The network

Fly's network policy is a list of DNS names, enforced outside the VM: nothing in the Sprite, root included, can change
it. While a policy is in force, Fly refuses raw IP addresses (an address is let through only after an allowed name
resolved to it), private ranges (10/8, 172.16/12, 192.168/16, 100.64/10), the metadata service (169.254.169.254) and
Fly's own private network, even through a name that resolves to one of them (checked on a live Sprite: refused or
unanswered). There's no firewall in the guest: this is the one layer, and the manifest's `outside_egress` says so.

- **`EGRESS=internet`** (the default): every name. **`EGRESS=off`**: no name, so nothing goes out.
  `metacomputer --vm work net egress internet|off` sets it and reads it back; `net status` shows the setting and what
  Fly has in force, apart.
- **An allowlist**: `net allow example.com other.org [--exact]` lists names (each with its subdomains unless
  `--exact`; `*.name` is the subdomains only); `net remove NAME` takes names off (the last one gone: no name at all);
  `net clear` goes back to `EGRESS`. A list stays until it's cleared (`--for` isn't available: nothing on your Mac
  would be there to lift it). A change whose resulting policy lets out anything the one in force doesn't (adding
  names, a list where nothing goes out, every name where a list or nothing is in force) runs only at your own
  terminal, however it's split into steps; narrowing it runs anywhere. Addresses can't be listed: only names.
- **A blocked name looks like a DNS failure**: Firefox shows "Hmm. We're having trouble finding that site"
  (`dnsNotFound`), curl exit 6. That's the policy, not the site.
- **The browsers' own DNS over HTTPS is locked off** (Firefox's and Chrome's policies): names must go through the
  Sprite's resolver, where Fly's policy applies.

## What keeps it apart

- **Fly's management API is hidden from the desktop.** A Sprite's API socket (`/.sprite/api.sock`) is
  world-writable, and it can create services, which run as the Sprite's `sprite` account, which has sudo: so any
  account that reaches it has root, and can also keep the Sprite awake and billing, or make and restore checkpoints.
  metacomputer runs everything of the desktop's and SSH's in one **session**: a mount namespace where an empty tmpfs
  covers `/.sprite` (`hide-sprite-api`) and a PID namespace with its own `/proc`, made by the `session` service.
  sshd, the X server and the desktop join it before anything runs as the desktop's account (`in-session`), so every
  SSH session (the driver, `sh`, the vault's accounts, `cuaadmin`) and every desktop program is inside: the socket
  answers "connection refused", the tmpfs can't be unmounted (no root; a user namespace of their own can't either:
  the mount is locked there), and no process outside the session is visible, so no other process's `/proc/PID/root`
  leads to the real `/.sprite`. Fly's own channel (`sprite exec`, its services) keeps the real one: that's the
  controller's, as the Mac's owner is for a VM there. This was reported to Fly ([SECURITY.md](../SECURITY.md#fly-sprites)).
- **SSH only through Fly's tunnel.** sshd listens on the Sprite's own loopback, by number (on a Sprite, `localhost`
  is its network address), and is reached from your Mac only through `sprite proxy -W :22`, with Fly's sign-in, then
  metacomputer's own keys and the pinned host key. The live view's x11vnc listens on `127.0.0.1:5900` only. Fly gives
  every Sprite a URL (`*.sprites.app`); metacomputer leaves it as Fly makes it, private to the organization, and serves
  nothing on it.
- **The vault over the tunnel.** `secrets bind` pins Fly's `sprite` command (its path and contents), the tunnel's
  arguments and the Sprite's own id, as it pins the gateway on your Mac; fills and API tokens go through the vault's
  own copy of the command (checked again right before it runs), run by the vault itself (`mcomp-vault _tunnel`, which
  ends it when ssh goes), after Fly's API says the Sprite of that name still has that id. After the command changes
  (an update), bind again.
- **Its own Sprite, and nothing else.** Every operation (the policy, checkpoints, restore, `sprite exec`, each SSH
  connection) checks with Fly's API that the Sprite of that name has the id recorded when it was made; one deleted
  and made again under the name is another, and nothing is done to it. A build whose answer was lost deletes only a
  Sprite carrying its operation's label, set when Fly made it.
- **Fly holds what's in the VM.** Its screen, disk and whatever is typed into it are on Fly's machines. Use a VM on
  your Mac for work you wouldn't put there. No file, secret or key of your Mac's goes to it.
- **Fly's sign-in stays Fly's.** metacomputer runs the `sprite` command with only `HOME`, `PATH` and `LANG`, never
  sees its token, and runs `sprite exec` with `--no-port-forward` (it would otherwise forward ports a command opens
  in the Sprite to your Mac).

## What it costs

Fly bills a Sprite awake for the CPU time it uses ($0.0385 per CPU-hour), the memory in use ($0.021875 per GB-hour)
and its disk ($0.000683 per GB-hour), and asleep for its disk only ($0.000027 per GB-hour, about $0.02 per GB-month)
(Fly's list prices, read 2026-11). A desktop that's driven now and then uses about 1.5 GB and little CPU: a few cents
an hour awake; all 8 CPUs busy with all of its 8 GB in use would be about $0.49 an hour. A set-up computer writes about 5 GB.
`vm create` says the rate before anything is made; `vm delete` deletes the Sprite with its disk and checkpoints.

## Measured (2026-11-23, one Sprite)

| Step | Time |
|---|---|
| The Sprite made through the API | 0.6 s |
| SSH answers (bootstrap through `sprite exec`, sshd installed) | 47 s after the launch began |
| Ready (desktop, driver, checks, baseline checkpoint) | 325 s after the launch began |
| `status` (the hub connects) / `health_report` through the hub | 4.0 s / 0.3 s |
| Firefox launched / a page's tree read / an accessible click | 3.8 s / 0.6 s / 0.6 s |
| A foreground hotkey / `shot` / `get_desktop_state` | 1.2 s / 1.1 s / 0.6 s |
| `record start` / `stop` (11.6 s, h264 1920x1080, 20 fps, on the Mac) | 2.6 s / 1.9 s |
| `sh` / `put` / `get` | 1.6 s / 2.8 s / 1.1 s |
| `net egress off` (set and read back) | 0.8 s |
| Asleep after `stop` (its last connection closed) | within about 5 s |
| Woken after 1 minute asleep: `sprite exec` / a `sh` round trip | 0.2 s / 1.6 s (Firefox still open) |
| Woken after 11 minutes asleep: `sprite exec` / `sh` / the hub connected and `health_report` | 0.7 s / 2.1 s / 2.1 s (Firefox still open) |
| `checkpoint` (Fly's part / the whole command) | 0.6 s / 1.4 s |
| `restore`: Fly's part / the desktop back | 4 s / 12 s (open apps closed) |

## Settings (`config/vms/NAME.env`)

`PLACE=sprites`, `PROVIDER=sprites`, `TRANSPORT=sprite`, `SPRITE_ORG` (the organization), `SPRITE_NAME` (the Sprite;
the computer's name by default), `SPRITE_CLI` (Fly's command, when it isn't on your PATH or in `~/.local/bin`),
`EGRESS`. `IDLE_STOP` doesn't apply (it sleeps by itself), and neither do `SIZE` or a region (Fly picks them).
`metacomputer cloud status sprites [--org ORG]` lists the organization's Sprites, marking the ones that are computers
here; `cloud doctor sprites` checks the command, each computer's Sprite and its policy.
