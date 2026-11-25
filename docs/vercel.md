# A computer on Vercel Sandbox

A metacomputer can be a [Vercel sandbox](https://vercel.com/docs/sandbox): a persistent Firecracker VM whose disk Vercel
snapshots at every stop. It gets the same desktop as the VM on your Mac (XFCE, Firefox, Chrome if `BROWSERS` has it,
LibreOffice, Cua Driver's Linux build), the same accounts and keys, and your Mac stays the controller: the hub and all
its guards, the vault, recordings, the action log and the CLI run here. It's a computer elsewhere like the others in
[docs/remote.md](remote.md); this page says what's different on Vercel.

| | Vercel Sandbox (`--on vercel`) |
|---|---|
| Who makes it | metacomputer, in a Vercel team's project you name, through Vercel's REST API |
| Stop and start | a stop snapshots the disk and powers the VM off: **files are kept, programs aren't** (open apps close); each start boots that snapshot |
| Idle stop | the session's timeout: `IDLE_STOP` minutes (15) after the last use, at most the plan's cap (45 minutes on Hobby, 24 hours on Pro) |
| What it costs | running: the CPU time it uses and all of its memory; stopped: its snapshot only ([below](#what-it-costs)) |
| Egress | Vercel's network policy, outside the VM: the internet without private ranges, nothing, or an allowlist of names ([below](#the-network)) |
| Snapshots, `reset`, checkpoints | yes: Vercel snapshots of the whole disk; taking or restoring one restarts the computer |
| Input | foreground only (no `/dev/uinput`): keys, typing and pixel clicks take the focus |
| Architecture | x86_64 (Ubuntu 26.04, Vercel's image) |
| Volumes and Mac folders | no (not yet) |

## Setting it up

1. **A Vercel team and a project for metacomputer.** A Vercel access token is **team-wide**: it can do anything in
   the team (deployments, domains, environment variables, every project), not only sandboxes. Make a team of its own
   for metacomputer if you have other work on Vercel, and a project in it (metacomputer only makes sandboxes there).
2. **An access token** of that team (vercel.com, Account Settings, Tokens; scope: that team), and the team's and the
   project's IDs (`team_...`, `prj_...`, in their settings). Keep all three in your secrets manager and hand them to the
   commands that need Vercel, for that command only:

```bash
secret exec metacomputer-cloud/VERCEL_TOKEN metacomputer-cloud/VERCEL_TEAM_ID metacomputer-cloud/VERCEL_PROJECT_ID -- \
  metacomputer vm create work --on vercel [--size 2x4|4x8|8x16] [--region iad1] [--sandbox NAME] [--egress internet|off] [--idle-stop MINUTES|off]
```

   They're needed to make, start, stop, snapshot, delete and change the network policy of the computer. Using it (the
   MCP tools, `sh`, `put`/`get`, the live view) doesn't need them once it's started: the computer's broker (below)
   holds them.
3. **The plan.** On **Hobby** (free), every session ends 45 minutes after it starts, in use or not: open apps close
   then, and `metacomputer start` starts a new one. Sandboxes are free within Hobby's monthly quota; past it, Vercel
   stops making sandboxes until the next month. **Pro** ($20 a month, with $20 of usage) raises the session to 24 hours
   and allows 8x16. `vm create` and `start` read the team's plan and say what it means; metacomputer never changes it.

`vm create` writes `config/vms/work.env`, says the rate, the plan's session cap and what's kept, and asks before
anything is made (`--yes` agrees). The sandbox is named after the computer unless `--sandbox` names it; one that exists
already in the project is refused (metacomputer never takes over a sandbox it didn't make). The size (2 GB of memory per
vCPU) and the region are fixed when it's made; a region's snapshots stay in it.

The build (`metacomputer --vm work build`, about 6 minutes):

1. **The sandbox**, through Vercel's API, journaled first and tagged with the computer and the build's operation: a
   build that fails or is interrupted deletes what it made and nothing else (a launch whose answer was lost is found by
   its name and that tag, looked for again until two minutes after the launch, Vercel's visibility delay; a sandbox
   of that name without it is never touched). `build --keep-on-failure` keeps it
   instead (stopped). Its network policy is `internet` while it's set up, then `EGRESS`, read back. Its snapshots never
   expire by themselves (`snapshotExpiration` 0).
2. **Its broker** starts on your Mac (below), and the first step goes through Vercel's own command API, as root:
   `providers/vercel/guest/bootstrap.sh` makes the accounts `cua` (the desktop, no sudo) and `cuaadmin`
   (maintenance), each reached only with keys made on your Mac for this computer, installs the boot script and runs it:
   Vercel's agent port is refused to every account but root, and sshd listens on the sandbox's own loopback. Its host
   key comes back over that authenticated API and is pinned here as `mcomp-work`: never learned from a first
   connection.
3. **The same setup as every computer elsewhere**, over SSH through the broker: the payload (Cua Driver's
   `linux-x86_64` build, pinned by SHA-256; the APT keys, pinned by fingerprint) and `provision.sh` with
   `MCOMP_TARGET=vercel`. One SSH connection stays open while it runs, so the session doesn't end under it.
4. **The desktop**, started by the boot script (no systemd, cloud-init or display manager): Xvfb (the X server in
   memory), then XFCE on its own D-Bus session, accessibility on. Then the live-view password, the desktop checked,
   `finalize.sh` (`cuaadmin`'s sudo narrowed to `metacomputer-admin`), `EGRESS` set and read back, and a snapshot at
   Vercel as its **baseline** (which stops it; it's started again).

## Using it

Everything works as on any computer: `metacomputer --vm work status`, the MCP tools through the hub, `sh`,
`put`/`get`, `shot`, `record`, `view --open` (the live view through an SSH tunnel to the sandbox's `127.0.0.1:5900`),
`secrets fill` and `token`. `list`, `--on vercel` and `default` know it.

- **Start, and what a stop closes.** `metacomputer --vm work start` (with the credentials) starts a session from the
  latest snapshot (a few seconds), runs the boot script in it (sshd, the X server, the desktop, about 10 s) and the
  broker on your Mac. **A stop closes every program**: files and installed packages are kept, but open apps are closed
  (LibreOffice and Firefox start clean: the boot script clears the locks they left; Firefox offers to restore its
  tabs), and the sandbox's hostname changes. `start` says so.
- **The idle stop is the session's timeout.** Vercel ends a session at its timeout, whatever's running. metacomputer
  keeps it `IDLE_STOP` minutes (15) ahead while anything of yours is connected (the hub, a command, the live view),
  renewing it once a minute of it is used (half of it for `IDLE_STOP=1`): the
  hub lets go after `HUB_IDLE` (10 minutes) with no client, and the session ends at most `IDLE_STOP` minutes later,
  even if your Mac is off. `IDLE_STOP=off` keeps it to the plan's cap. Nothing extends it past the cap: on Hobby, 45
  minutes after it started the computer stops, apps and all; `status` says when the session ends.
- **`metacomputer stop`** closes this Mac's connections, then Vercel snapshots the disk and powers it off (a few
  seconds to about 15 s with a desktop's disk). The snapshot the previous stop made is deleted then (unless a checkpoint
  is that snapshot), so stopped it keeps one snapshot plus your checkpoints.
- **Checkpoints.** `metacomputer --vm work checkpoint NAME [--replace]`, `checkpoints`, `restore NAME`,
  `checkpoint --delete NAME`, `baseline` (the current state becomes the baseline) and `reset` (back to the baseline)
  are Vercel snapshots of the whole disk, named in the computer's record on your Mac. **Taking one stops the computer**
  (Vercel's snapshot ends the session): metacomputer starts it again, but open apps close and connections are cut. **A
  restore** stops it, then starts it from the checkpoint: **starting from a snapshot that isn't the latest took Vercel 4
  to 5 minutes** (measured twice), against seconds for a start after a stop. Each change waits while the user has the
  computer (`metacomputer handoff`) or another session is using it (`--override` goes ahead).
- **What's lost, and when.** metacomputer makes its snapshots never expire. A sandbox whose snapshots expire (one
  changed by hand to `snapshotExpiration` above 0) loses its latest one 30 days after it was last used, and Vercel then
  removes a sandbox that can't resume after 14 days of inactivity: about six weeks stopped and it's gone. `status`
  says when a stopped computer's snapshot expires, or that it's gone; `cloud doctor vercel` checks each computer's
  expiry setting.
- **Input is foreground only.** The sandbox's kernel has no `/dev/uinput`, so the hub sends keys, typing and pixel
  clicks in the foreground (the desktop's own pointer and keyboard, XTEST) and says so in the result; clicks on an
  element (`element_token`) still go through accessibility.
- **Logs.** `metacomputer --vm work logs` reads the setup's log and the boot script's through Vercel's API.
- **Updates.** Vercel's image isn't upgraded whole: for a newer desktop, make a new computer (`vm delete`, then
  `vm create`).
- **`desktop-restart`** ends the desktop's processes and runs the boot script again (every open app closes).

## The broker: how SSH gets in

There's no TCP into a sandbox: Vercel's command API runs a command with its standard input and output streamed, and
its output is text, not bytes. metacomputer carries SSH over it:

- **The broker** (`providers/vercel/broker.py`) runs on your Mac, one per computer, started by `start` (and the build)
  with the credentials on its standard input. It's the only program besides `lib/remote.py` that holds them: ssh, the
  hub, the vault and every helper never do. It listens on a private socket (`/tmp/mcomp-UID-vercel/HASH.sock`, 0600 in
  a 0700 folder; another account's process is refused) and does three things only: carry an SSH connection to this
  computer's sandbox, say whether the sandbox is still this computer's own, and keep the session going while a
  connection is open. It ends once the session has ended and nothing is connected.
- **Each connection**: ssh runs `providers/vercel/tunnel.py` as its ProxyCommand through `lib/proxywrap.py`, which first
  asks the broker for the sandbox's identity as Vercel has it now (its name and the time Vercel made it, with the
  computer's and the build's tags) and refuses unless it's the one recorded when the computer was made. The broker
  checks again, then runs `/opt/metacomputer/vercel/relay` in the sandbox, which connects to sshd on `127.0.0.1:22`;
  bytes go in as base64 in the API's JSON (each write at its offset, so a retry can't repeat them), and come back as
  base64 lines. A stopped computer stays stopped: the broker never starts a session.
- **Measured**: a new connection and a command, about 1.0 s; a keystroke's echo, about 60 ms; 20 MB out in 1.5 s, 5 MB
  in in 1.9 s.

## The network

Vercel's network policy is enforced outside the VM: nothing in the sandbox, root included, can change it. It matches
names by the TLS SNI a connection asks for, and addresses by range. There's no egress firewall in the guest: this is
the one layer, and the manifest's `outside_egress` says so.

- **`EGRESS=internet`** (the default): every name and address but private, shared, link-local and reserved IPv4 ranges
  (10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16 with the metadata address, and the rest of
  `lib/cloud/ubuntu_vm.py`'s list), which Vercel refuses outright, even through a name that resolves to one (checked
  live: a reset in 15 ms, where Vercel's own allow-all lets them out and they time out; the metadata address is refused).
  Vercel's policy takes IPv4 ranges only; a sandbox has no IPv6 route out (link-local only).
- **`EGRESS=off`**: Vercel's deny-all: no name resolves, no address answers. SSH keeps working (it comes in through the
  API, not out).
- **An allowlist**: `net allow example.com other.org [--exact]` lists names (each with its subdomains unless
  `--exact`; `*.name` is the subdomains only), with the same private ranges refused; `net remove NAME` takes names off
  (the last one gone: nothing at all); `net clear` goes back to `EGRESS`. A list stays until it's cleared. A change
  whose resulting policy lets out anything the one in force doesn't runs only at your own terminal, however it's split
  into steps; narrowing it runs anywhere. Addresses can't be listed: only names. HTTPS reaches a listed name; plain HTTP
  and bare addresses are reset (a name can only be matched in TLS), and an unlisted name fails at DNS.
- **What Vercel says**: names are matched by SNI, so a listed name can carry traffic for another site on the same
  servers (domain fronting); a list constrains which name a connection asks for, not where the request ends up.
- **The browsers' own DNS over HTTPS is locked off** (Firefox's and Chrome's policies), as on a Sprite.

## What keeps it apart

- **Vercel's agent port is refused to the desktop.** Every sandbox has a listener on tcp 23456, on every address
  (IPv4 and IPv6), run as root by Vercel's own agent outside the sandbox's process namespace (nothing in the sandbox
  owns it). It speaks HTTP and answered "404 page not found" to every path metacomputer tried, Vercel's command API's
  included; that API is on another socket (`/run/vercel/share/init.sock`, Vercel's `ubuntu` account only) and refuses
  even root without Vercel's signature. Nothing found lets an account in the sandbox use the port to change the policy,
  read credentials, act on other sandboxes or reach your Mac, but what it serves isn't documented, so metacomputer
  refuses it to every account but root: an nftables rule by uid (`tcp dport 23456 meta skuid != 0 reject`), put back
  by the boot script at every start (a stop clears it) before anything of the desktop's runs, replaced in one
  transaction so it never lapses (`desktop-restart` runs the boot script with the desktop still up); if no rule can be
  put in force, the processes of every account but root's and Vercel's own are ended, sshd too, and the boot stops.
  Checked live: the desktop's account, Vercel's `ubuntu` account and an account in a user namespace of its own
  are refused, root still gets its 404, and no account but root can remove the rule.
- **SSH only through Vercel's API.** sshd listens on the sandbox's own loopback, reached from your Mac only through the
  broker (Vercel's token, then metacomputer's own keys and the pinned host key). metacomputer publishes no port (a
  published port is a public URL that Vercel doesn't authenticate). The live view's x11vnc listens on `127.0.0.1:5900`.
- **Its own sandbox, and nothing else.** Every operation (the policy, snapshots, restore, a command, each SSH
  connection) checks that the sandbox of that name was made when its record says, by the computer's build; one deleted
  and made again under the name is another, and nothing is done to it.
- **The token is team-wide.** Anyone with it can do anything in the team. It's in your secrets manager, in
  `lib/remote.py` for one command, and in the broker's memory while the computer is in use; never in a file, an
  argument, a log or another program's environment, and never in the sandbox.
- **Vercel holds what's in the VM.** Its screen, disk and whatever is typed into it are on Vercel's machines, and
  Vercel's own `ubuntu` account in it has sudo (that's the controller's channel, as the Mac's owner is for a VM there).
  Use a VM on your Mac for work you wouldn't put there. No file, secret or key of your Mac's goes to it.

## What it costs

At Vercel's Pro list prices (iad1, read 2026-11): memory $0.0212 per GB-hour, **all of it while it runs** (2 GB per
vCPU), CPU $0.128 per CPU-hour **actually used**, snapshot storage $0.08 per GB-month. A 2x4 desktop driven now and
then is about $0.10 an hour running (a 4x8 about $0.18); all CPUs busy, about $0.34 (2x4) or $0.68 (4x8). Stopped, its
snapshot (a set-up desktop's is about 2 GB) is about half a cent a day. Pro's $20 a month includes $20 of usage. On
Hobby it's all free within the monthly quota (5 active CPU-hours, 420 GB-hours of memory, 15 GB of snapshots), after
which sandboxes can't be made or started until the next month. `vm create` says the rate before anything is made;
`vm delete` deletes the sandbox with every snapshot of it (Vercel would otherwise keep them, and bill them).

## Measured (2026-11-25, one 2x4 sandbox on Hobby, iad1)

| Step | Time |
|---|---|
| The sandbox made through the API | 1.2 s |
| SSH answers (bootstrap through Vercel's API, sshd installed) | 28 s after the launch began |
| Ready (desktop, driver, checks, baseline snapshot) | 220 s after the launch began |
| `status` (the hub connects) / `health_report` through the hub (no credentials in the command) | 6.4 s / 0.4 s |
| Firefox launched on a page / its title read / `shot` | 1.9 s / 0.4 s / 1.5 s |
| `sh` / `put` 3 MB / `get` 3 MB | 1.8 s / 6.8 s / 2.1 s |
| `net allow` / `net egress off` (set and read back) | 1.5 s / 1.1 s |
| `stop` (the disk snapshotted, about 1.5 GB) / the broker gone after it | 9.5 s / 12 s |
| `start` after a stop (a session 1.8 s, the boot script 4.2 s, the desktop ready) | 11 s |
| `checkpoint` (it stops and starts again) | 21 s |
| `restore` (Vercel starting from a snapshot that isn't the latest: 304 s) | 322 s |
| `vm delete` (the sandbox and its snapshots) | 2.6 s |

## Settings (`config/vms/NAME.env`)

`PLACE=vercel`, `PROVIDER=vercel`, `TRANSPORT=vercel`, `VERCEL_SANDBOX` (the sandbox; the computer's name by default),
`SIZE` (2x4, 4x8, 8x16), `REGION` (none: the project's default), `IDLE_STOP` (minutes, or off), `EGRESS`.
`metacomputer cloud status vercel` lists the project's sandboxes metacomputer made and the team's plan, marking the
ones that are computers here; `cloud doctor vercel` checks the credentials, each computer's sandbox, its policy and its
snapshots' expiry.
