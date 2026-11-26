# A computer on Cloudflare Containers

A metacomputer can be a [Cloudflare container](https://developers.cloudflare.com/containers/): an Ubuntu 24.04 microVM
(x86_64) that a small control Worker of metacomputer's runs in your own Cloudflare account. It gets the same desktop as
the VM on your Mac (XFCE, Firefox, Chrome if `BROWSERS` has it, LibreOffice, Cua Driver's Linux build), the same
accounts and keys, and your Mac stays the controller: the hub and all its guards, the vault, recordings, the action log
and the CLI run here. It's a computer elsewhere like the others in [docs/remote.md](remote.md); this page says what's
different on Cloudflare.

| | Cloudflare Containers (`--on cloudflare`) |
|---|---|
| Who makes it | metacomputer, in your Cloudflare account, through Cloudflare's API: a control Worker per computer (below) |
| Stop and start | a stop snapshots the disk, then ends the container: **files are kept, programs aren't** (open apps close); each start restores that snapshot (about 0.2 s) |
| Idle stop | the control Worker stops it `IDLE_STOP` minutes (15) after its last connection closes, 350 at most |
| What it costs | running: its memory and disk as provisioned and the CPU it uses; stopped: its snapshots, free during Cloudflare's beta ([below](#what-it-costs)) |
| Egress | Cloudflare's outbound rules, outside the container: the internet without private ranges, nothing, or an allowlist of names through metacomputer's own handler ([below](#the-network)) |
| Snapshots, `reset`, checkpoints | yes: live snapshots of the whole disk (taking one doesn't interrupt it); each expires 30 days after its last use and **can't be deleted** ([below](#what-cloudflare-keeps)) |
| Input | foreground only (no `/dev/uinput`): keys, typing and pixel clicks take the focus |
| Architecture | x86_64 (Ubuntu 24.04, from Docker Hub's image, pinned by digest) |
| Sizes | `standard-2` (1 vCPU, 6 GiB, 12 GB), `standard-3` (2, 8, 16: the default), `standard-4` (4, 12, 20) |
| Volumes and Mac folders | no (not yet) |

Containers are on Cloudflare's **Workers Paid** plan ($5 a month); the Free plan doesn't have them. Running a container
from a Durable Object (the `durable_object` scheduling policy) and container snapshots are Cloudflare's **public betas**:
what's below is how they behaved when metacomputer was built against them (2026-11), and they may change.

## What metacomputer puts into your account

For each computer, named after its control Worker (`metacomputer-NAME` unless `vm create --worker NAME` names it):

- **A Worker** (`providers/cloudflare/worker/`, two small modules you can read), on **workers.dev only**: no route on
  any of your domains, no DNS record, nothing in your zones. Its secret is the computer's own key; its plain-text
  settings are the computer's name, the build's operation and its size.
- **Its Durable Object namespace** (class `Computer`): one object, the computer's, which starts, stops and snapshots
  the container and carries SSH.
- **Its container application**, with **Cloudflare's own SSH turned off** (it would give root in the container to any
  key listed for it), read back after it's made.
- **A repository in your account's container registry** holding Ubuntu's image (copied from Docker Hub without
  Docker, each part checked against the pinned digest) and, Cloudflare's doing, the computer's snapshots.

metacomputer never lists, reads or changes any other Worker, route, zone, DNS record or setting of the account. A Worker
of the same name that it didn't make is refused at `vm create` and left alone at `vm delete` (it must carry the
computer's name and the operation that made it).

## Setting it up

1. **An account with Workers Paid.** Best: a Cloudflare account of its own for metacomputer (see the token below).
2. **An API token** with, for that account: **Workers Scripts: Edit** and **Containers: Edit** (what the calls
   metacomputer makes need, by Cloudflare's docs; metacomputer was tested with a broader token, not yet with one limited
   to these two: if one is refused, Cloudflare's error names what's missing). Better still, make it
   an **account-owned token** (Manage Account, API Tokens) rather than one tied to your user, so it can do nothing
   outside that account. Workers Scripts Edit covers every Worker in the account: that's why an account of its own is
   the clean setup. Keep the token and the account's ID (32 hex digits) in your secrets manager and hand them to the
   two commands that need them, for that command only:

```bash
secret exec metacomputer-cloud/CLOUDFLARE_API_TOKEN metacomputer-cloud/CLOUDFLARE_ACCOUNT_ID -- \
  metacomputer vm create work --on cloudflare [--worker NAME] [--size standard-2|standard-3|standard-4] [--egress internet|off] [--idle-stop MINUTES]
```

   **Only `vm create` and `vm delete` need them.** Starting, stopping, checkpoints, the egress, the logs and every
   connection use the computer's own key instead, which `vm create` makes on your Mac (64 random hex digits, kept 0600
   beside its SSH keys in `~/.ssh/metacomputer-work/cloudflare-key`, like its SSH keys and its live-view password) and
   gives the Worker as its secret.

`vm create` writes `config/vms/work.env`, says the rate and what's kept, and asks before anything is made (`--yes`
agrees). The build (`metacomputer --vm work build`, about 5 minutes):

1. **The control Worker**, journaled first (each part recorded as it's made): Ubuntu's image into the registry,
   prepared to run by Cloudflare (it converts it once), the Worker uploaded (sent once: an upload whose answer is
   lost is found again only by its Worker's settings naming this computer and this build's operation, looked for again
   every 10 s until two minutes of lookups have found nothing), its workers.dev address, its container application.
   Cloudflare's upload replaces any Worker of the name and takes no condition, so the name is checked again right
   before it, under a lock on this Mac held through the upload, and the Worker read back after it must carry this
   build's operation: another create's Worker (made while the image was prepared, here or elsewhere) is left alone and
   the build stops. A build that fails deletes what it made and nothing else (`build --keep-on-failure` stops it instead). Then its
   first start, from the image, with the internet on for the setup's downloads.
2. **The first step through the Worker**, as root: `providers/cloudflare/guest/bootstrap.sh` makes the accounts `cua`
   (the desktop, no sudo) and `cuaadmin` (maintenance), each reached only with keys made on your Mac for this computer,
   and starts sshd on port 2222. Its host key comes back over the Worker's signed channel and is pinned here as
   `mcomp-work`: never learned from a first connection.
3. **The same setup as every computer elsewhere**, over SSH through the Worker: the payload (Cua Driver's
   `linux-x86_64` build, pinned by SHA-256; the APT keys, pinned by fingerprint) and `provision.sh` with
   `MCOMP_TARGET=cloudflare`. One SSH connection stays open while it runs, which keeps it from its idle stop.
4. **The desktop**, started by the boot script (no systemd): Xvfb, then XFCE on its own D-Bus session, accessibility
   on. Then the live-view password, the desktop checked, `finalize.sh`, `EGRESS` set and read back. From here the
   Worker **runs no command of yours** but the boot script and the logs: the key can't run anything else as root. Last,
   a snapshot as its **baseline** (it keeps running).

## Using it

Everything works as on any computer: `metacomputer --vm work status`, the MCP tools through the hub, `sh`,
`put`/`get`, `shot`, `record`, `view --open` (the live view through an SSH tunnel to the container's
`127.0.0.1:5900`), `secrets fill` and `token`. `list`, `--on cloudflare` and `default` know it.

- **Start, and what a stop closes.** `metacomputer --vm work start` restores the latest snapshot (about 0.2 s), runs
  the boot script (sshd, the X server, the desktop) and the broker on your Mac. **A stop closes every program**: the
  desktop's programs are ended first (so they save), the disk is snapshotted while the container still runs (about 20
  s for a desktop's 1-2 GB), and only then is the container ended. Files and installed packages are kept; open apps are
  closed (the boot script clears the locks they left). `start` and `stop` say so. If Cloudflare doesn't end the
  container after the snapshot, `stop` says that, as an error: the disk is saved, the container still runs (`status`:
  stopping), and the Worker tries to end it again every minute; a `start` then ends it and starts from the snapshot, or,
  if it still won't end, takes it up again as it is.
- **The idle stop is the Worker's.** It stops the computer (a snapshot, then the stop) `IDLE_STOP` minutes after its
  last connection closed (the hub's, a command's, the live view's), looking every minute (every 30 s for
  `IDLE_STOP=1`); while one is open it never does. Under it, the container's own inactivity timeout is `IDLE_STOP` plus
  10 minutes: if the Worker's stop ever fails three times in a row, Cloudflare's timeout ends it, without a snapshot.
  `IDLE_STOP` goes up to 350 minutes (Cloudflare's own timeout is 6 hours at most); there's no `off`.
- **Cloudflare can stop it without warning.** A container has no guaranteed runtime: a host event can end it. What
  changed since its last snapshot (its last stop, checkpoint or baseline) is then lost. The Worker sees it as a stop by
  Cloudflare, and `status` and `start` say so, with the date of the snapshot it went back to. Take a checkpoint before
  work you don't want to redo.
- **Checkpoints.** `metacomputer --vm work checkpoint NAME [--replace]`, `checkpoints`, `restore NAME`,
  `checkpoint --delete NAME`, `baseline` and `reset` are snapshots of the whole disk, named by the Worker. **Taking one
  doesn't interrupt the computer.** A restore discards what runs now and starts the computer from the checkpoint (open
  apps close). Each change waits while the user has the computer (`metacomputer handoff`) or another session is using
  it (`--override` goes ahead). Snapshots are tied to the image they came from: a computer made with a newer image
  can't use an older one's.
- **Input is foreground only.** The container's kernel has no `/dev/uinput`, so the hub sends keys, typing and pixel
  clicks in the foreground (the desktop's own pointer and keyboard, XTEST) and says so in the result; clicks on an
  element (`element_token`) still go through accessibility.
- **Logs.** `metacomputer --vm work logs` reads the setup's log and the boot script's through the Worker.
- **`desktop-restart`** ends the desktop's processes and runs the boot script again (every open app closes).
- **Updates.** Ubuntu's image isn't upgraded whole: for a newer desktop, make a new computer.

## What Cloudflare keeps

Cloudflare keeps every snapshot **30 days after it was made or last restored**, and **its API can't delete one**
(Cloudflare refuses: "snapshot manifests cannot be deleted through this API"). So:

- **While the computer exists**, its Worker restores each snapshot it keeps (its latest, its baseline, its checkpoints)
  before its 30 days run out (an alarm; a few seconds of runtime each, about every 25 days), so nothing it needs
  expires, whether it's stopped for months or runs for weeks: a second object of the Worker, the refresher, restores
  each in a container of its own and ends it at once, so a running computer isn't interrupted. A refresh that fails
  (no room at Cloudflare, say) is tried again 5 minutes later, then 10, 20 and so on, 6 hours at most between tries,
  until one works; it's never given up on before the snapshot expires. `status` says when its latest is kept until,
  and names any kept snapshot whose refresh keeps failing, with its tries, its last error and the date it expires;
  `cloud doctor cloudflare` checks every kept one and fails while one is failing.
- **A snapshot the computer lets go** (the one a stop replaced, a checkpoint deleted or replaced) isn't renewed: it
  expires 30 days after its last use. A computer stopped every day has about a month of them at Cloudflare.
- **`vm delete`** deletes the Worker, its container, its application and the image, and then **says plainly what
  Cloudflare keeps, with dates**: every snapshot that hasn't expired. Each holds the computer's whole disk as it was
  then: its files, its browser profiles (sign-ins, cookies) and anything typed into it. The list is also written to the
  computer's log folder (`logs-vms/NAME/cloudflare-residue.json`, which `vm delete` keeps). Cloudflare's conversion of
  the image (about 43 MB, no files of yours) can't be deleted either.
- Snapshots are free during Cloudflare's beta; what they'll cost after it isn't known. Whether they count toward the
  account's 50 GB registry limit isn't known either.

**Use a VM on your Mac for work whose traces you need gone on demand.** A later option (not built): keeping the home
folder on an encrypted volume whose key stays on the Mac, so deleting the key would shred what the snapshots hold. It
wasn't built because a live snapshot of a mounted encrypted volume isn't consistent, and its key would have to pass
through the Worker at every start.

## How SSH gets in: the control Worker and the broker

Nothing in a container is reachable from the internet. metacomputer reaches it through its own Worker:

- **Every request to the Worker is signed** with the computer's key: an HMAC-SHA256 of the method, the path, the query,
  the time, a nonce new to each request and the body's length and hash (sent in a header of their own), which the
  Worker checks in constant time and within 5 minutes of its own clock; the computer's Durable Object refuses a nonce
  it has seen, so a request can't be sent again, not even within those 5 minutes. The key itself never travels. A
  request without a valid signature is refused (401) before it reaches the computer, and **before a byte of its body is
  read**: the signature and the nonce are checked from the headers alone, and only then is the body read, to the length
  signed at most (64 KB, or 72 MB for the setup's commands; 413 past it), and refused (400) unless it's exactly the one
  signed. Once `vm delete` has told the Worker to forget the computer, it refuses every request for good (410), even
  if deleting the Worker then fails.
- **The broker** (`providers/cloudflare/broker.py`) runs on your Mac, one per computer, started by `start` (and the
  build) with the key on its standard input. It's the only program besides `lib/remote.py` that holds the key: ssh,
  the hub, the vault and every helper never do. It listens on a private socket (`/tmp/mcomp-UID-cloudflare/HASH.sock`,
  0600 in a 0700 folder; another account's process is refused), carries SSH connections, says whether the Worker still
  answers for this computer, and ends once the computer is stopped and nothing is connected. It never starts a stopped
  computer.
- **Each connection**: ssh runs `providers/cloudflare/tunnel.py` as its ProxyCommand through `lib/proxywrap.py`, which
  first asks the broker for the computer's identity as its Worker says it now (this computer, the operation that made
  it, its Durable Object) and refuses unless it's the one recorded. The broker opens a websocket to the Worker, whose
  Durable Object pipes it to the container's sshd; the answer that opens it names the Durable Object it reached, which
  must be the computer's before a byte goes through. Then metacomputer's own keys and the pinned host key, as anywhere.
- **The account's token never reaches the broker**, a connection or the container.

## The network

Cloudflare's outbound rules are enforced outside the container: nothing in it, root included, can change them. There's
no egress firewall in the guest.

- **`EGRESS=internet`** (the default): the container's internet as Cloudflare gives it, which refuses private, shared and
  link-local ranges outside the VM (checked live from inside: 10/8, 172.16/12, 192.168/16, 100.64/10, the gateway and
  IPv6 ULAs are reset at the first byte; the metadata address 169.254.169.254 doesn't answer).
- **`EGRESS=off`**: the container's internet off. Nothing leaves (names don't resolve), except names an allowlist's
  handler is registered for. SSH keeps working: it comes in through the Worker, not out.
- **An allowlist**: `net allow example.com other.org` (exact names: no wildcards; each subdomain is its own name;
  addresses can't be listed; 63 names at most). The internet is off, and each name is handed to metacomputer's own
  handler in the Worker, which **checks that the request asks for exactly that name** (the runtime builds the request
  from its Host header, not from the name it was intercepted on: a request for another site through an allowed name is
  refused, 403), fetches only that name, and **never follows a redirect** (the redirect goes back to the browser, whose
  next request is checked the same way). HTTP and HTTPS only; nothing else can leave. `net remove NAME` and `net clear`
  narrow it; a change whose resulting egress lets out anything the one in force doesn't runs only at your own terminal,
  however it's split into steps.
- **The allowlist's HTTPS is TLS interception.** Cloudflare's runtime terminates each allowed HTTPS connection with a
  certificate from a CA it makes at every start (in the container, valid a month), so the browser trusts it only
  because the boot script installs that CA (the system's store, Firefox through its policy, Chrome's store for the
  desktop's account) while an allowlist or `off` is in force, and takes it out when the internet is on. **Cloudflare's
  runtime, and metacomputer's handler in your own Worker, see that traffic decrypted** (sign-ins and cookies to the
  allowed sites included). The Worker logs nothing (its observability is off). With `EGRESS=internet` there's no
  interception at all.
- **Changing between the internet and off restarts the computer** (Cloudflare sets a container's internet when it
  starts): its disk is snapshotted first, and open apps close. Changes within off and allowlists apply at once.
  While it's stopped, a change is recorded and applies at its next start.
- **The browsers' own DNS over HTTPS is locked off** (Firefox's and Chrome's policies), as on a Sprite.

## What keeps it apart

- **No Cloudflare SSH.** The container application is made with Cloudflare's own SSH off (checked live: Cloudflare
  answers `SSH_NOT_ALLOWED`, and nothing listens on port 22 in the container).
- **The desktop has no privileges.** A command Cloudflare's runtime runs in the container keeps root's capabilities even
  when told to run as another user, so metacomputer never uses that: the boot script starts the desktop with `runuser`,
  which drops them, and after every start checks every process of every account but root's; if one holds a capability,
  all of them are ended (sshd first) and the boot fails. Checked live: 42 processes of accounts but root's, the
  desktop's and SSH sessions among them, none holding a capability.
- **The key's reach is narrow.** With the computer's key one can start, stop, snapshot and restore it, change its egress,
  run its boot script, read its logs and connect over SSH (with metacomputer's own keys too). After the setup it can't
  run any other command. The account's token can do much more (it could replace the Worker), which is why it's only in
  `vm create` and `vm delete`.
- **Its own Worker, and nothing else.** Every operation checks that the Worker answers, with the computer's key, for
  this computer, the operation that made it and the Durable Object recorded for it; `vm delete` and a lost launch check
  the Worker's settings with the token. One deleted and made again under the name is another, and nothing is done to it.
- **Cloudflare holds what's in the VM.** Its screen, disk, snapshots and whatever is typed into it are on Cloudflare's
  machines, and the container's root is the Worker's (the controller's channel, as the Mac's owner is for a VM there).
  Use a VM on your Mac for work you wouldn't put there. No file, secret or key of your Mac's goes to it.

## What it costs

Workers Paid is $5 a month and includes, each month, 25 GiB-hours of memory, 375 vCPU-minutes and 200 GB-hours of disk.
Past those, at Cloudflare's list prices (developers.cloudflare.com/containers/platform/pricing, read 2026-11): memory
$0.009 per GiB-hour and disk $0.00025 per GB-hour **as provisioned while it runs**, CPU $0.072 per vCPU-hour **as
used**, egress $0.025 per GB past 1 TB. A `standard-3` desktop driven now and then is about **$0.08 an hour** running
(`standard-2` about $0.06, `standard-4` about $0.12); all CPUs busy, about $0.22 (`standard-3`). The first few hours a
month fit in what the plan includes. **Stopped: nothing for the container**; its snapshots are free during Cloudflare's
beta (their price after it isn't known). The Worker and its Durable Object bill requests and time, well within the
plan's included amounts for one person. `vm create` says the rate before anything is made.

## Measured (2026-11-26, one standard-3 computer, Workers Paid)

| Step | Time |
|---|---|
| The control Worker deployed through the API (image copied and prepared, Worker, workers.dev, application) | 7 s |
| The container's first start, from the image | 0.3 s |
| SSH answers (bootstrap through the Worker, sshd installed) | 32 s after the launch began |
| Ready (desktop, driver, checks, baseline snapshot) | 314 s after the launch began |
| `status` (with the key only) / `health_report` through the hub | 4.3 s / 0.4 s |
| Firefox launched on a page / its title read / `shot` | 3.2 s / 0.3 s / 1.5 s |
| `sh` / `put` 3 MB / `get` 3 MB | 1.5 s / 3.9 s / 1.5 s |
| `stop` (programs ended, the disk snapshotted, about 1.6 GB) | 24 s |
| `start` (restore 0.25 s, the boot script 8 s, the desktop ready) | 12 s |
| `reset` (back to the baseline: a restore and the boot) | 9 s |
| `net allow` while stopped / `net egress off` from an allowlist (live) | 0.9 s / 0.6 s |
| The live view's tunnel / its first answer through it | 1.0 s / 0.13 s |
| `vm delete` (the Worker, application, container and image) | 11 s |

The spike measured the transport alone: a new SSH connection and a command in about 0.55 s, a keystroke's echo in about
40 ms, 20 MB out in 1.3 s, 5 MB in in 0.9 s.

## Settings (`config/vms/NAME.env`)

`PLACE=cloudflare`, `PROVIDER=cloudflare`, `TRANSPORT=cloudflare`, `CLOUDFLARE_WORKER` (its control Worker; fixed when
it's made), `SIZE` (`standard-2`, `standard-3`, `standard-4`), `IDLE_STOP` (minutes, 1-350), `EGRESS`.
`metacomputer cloud status cloudflare` lists this checkout's computers there, each asked through its own Worker;
`cloud doctor cloudflare` checks the token, each Worker (with its key), its egress, its snapshots' renewal and that
Cloudflare's SSH is off.
