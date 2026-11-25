# A computer elsewhere: an Ubuntu VM reached over SSH

metacomputer's computers normally run on your Mac (Lume VMs). A computer can also be an **Ubuntu 24.04 VM somewhere
else**, reached over SSH: **your own server**, or one a **provider** makes for you (Oracle Cloud, Hetzner Cloud,
DigitalOcean, Vultr). A **Fly Sprite** is a computer elsewhere too, reached through Fly's tunnel, with its own page:
[docs/sprites.md](sprites.md). It gets the
same desktop (XFCE, Firefox, optionally Chrome, LibreOffice), the same Cua Driver, the same accounts, and your Mac
stays the controller: the hub and all its guards, the vault, recordings, the action log and the CLI run here, as for
a VM on your Mac. The difference is where the VM runs, and what keeps it apart from everything else (its own firewall
and, where the provider enforces them, the provider's rules, instead of the Mac's gateway).

| | Your own server (`--on server`) | Oracle Cloud (`--on oci`) | Hetzner Cloud, DigitalOcean, Vultr (`--on hetzner\|digitalocean\|vultr`) |
|---|---|---|---|
| Who makes it | you | metacomputer, in your OCI compartment | metacomputer, in your account (Hetzner: the token's project) |
| Who powers it on and off | you (metacomputer never does) | `start` / `stop`, through OCI's API; the guest powers itself off after `IDLE_STOP` minutes unused, which OCI doesn't count as stopped | `start` / `stop`, through the provider's API; the guest powers itself off after `IDLE_STOP` minutes unused, which Hetzner and DigitalOcean see as off (Vultr doesn't: its idle stop is off by default) |
| What a stopped computer costs | between you and its host | its boot volume only, once stopped through the API; a guest that powered itself off is billed as running ([costs](#what-a-stopped-computer-costs)) | **everything**: a stopped server is billed in full until it's deleted ([costs](#what-a-stopped-computer-costs)) |
| Egress rules outside the VM | none of metacomputer's (only its own firewall) | its own network security group: public addresses only, or nothing; not the metadata service ([below](#what-keeps-it-apart)) | Hetzner, DigitalOcean: its own firewall, public addresses only, or nothing; not the metadata service. Vultr: none (its firewall filters only what comes in) |
| Architecture | arm64 or x86_64 (found out) | from `--size` | from `--size`: Hetzner's CAX sizes are Arm (the default), the rest x86_64 |
| What it costs | between you and its host | `vm create` says the rate first; Arm within the Always Free allowance costs nothing | `vm create` says the hourly rate first, and that only deleting stops it |
| What's kept | its disk | its disk (`PERSIST=disk`), also while stopped | its disk (`PERSIST=disk`), also while stopped |
| Snapshots, `reset`, checkpoints | no | no (not yet) | no (not yet) |
| Volumes and Mac folders | no (not yet) | no (not yet) | no (not yet) |

## Your own server

Any Ubuntu 24.04 host (arm64 or amd64) you can sign in to with an SSH **key**, as an account that may use `sudo`.
Give metacomputer a server of its own for this: setup takes it over (accounts, desktop, firewall, SSH settings).

```bash
metacomputer vm create work --on server --host 203.0.113.5 --user ubuntu [--port 22] [--egress internet|off]
```

- `vm create` writes `config/vms/work.env` and then runs the setup (`metacomputer --vm work build`), which:
  1. signs in **with your own SSH** (your `~/.ssh/config`, agent and `known_hosts`, so the host key you already trust
     is the one checked), with a key only: it refuses to go on if your account would need a password to sign in,
     because setup turns password sign-in off;
  2. runs `provisioning/remote/bootstrap.sh` with `sudo` (if `sudo` asks your password, run it in your own terminal:
     the prompt comes from your server, and the password isn't echoed). It loads the guest firewall first (SSH let in
     on every port `sshd -T` says sshd listens on, and the one you gave with `--port`), then makes the accounts `cua`
     (the desktop, no sudo) and `cuaadmin` (maintenance), each reached only with keys made on your Mac for this
     computer, and reports the server's host key, which is pinned on your Mac as `mcomp-work`;
  3. signs in again with a fresh connection of your own, to check your access survived, then uses only the new keys:
     uploads the payload and runs the same setup as every VM (below).
- **Your access is kept and checked.** Before any of its SSH settings can apply, the setup saves your server's own
  `sshd_config.d` (to `/var/lib/metacomputer/rollback`), writes your account's access (`AllowUsers` lists it; a
  `Match` block keeps its own `authorized_keys`) and checks it with `sshd -t` and `sshd -T`; if the new settings would
  lock your account out, it puts the server's own back and stops. After the setup and again after its reboot, a fresh
  connection of yours is checked; if it can't sign in, the setup's SSH settings and firewall are taken back off
  (`/opt/metacomputer/bin/remote-uninstall rollback`) and the build stops, saying so. A fresh connection is a new ssh
  process that never shares one you have open (`-o ControlMaster=no -o ControlPath=none` over your own ssh config), so
  a check can't pass through a connection made before the setup changed anything. None of these connections forwards
  anything of your Mac's to the server, whatever your ssh config says (`ForwardAgent=no`, `ForwardX11=no`,
  `ClearAllForwardings=yes`, `PermitLocalCommand=no`, no tunnel, remote command or delegated credentials;
  `scripts/lib.sh mc_owner_ssh_opts`): your agent signs them in from your Mac. Your account keeps its own
  `sudo`.
- metacomputer never powers your server on or off; `metacomputer --vm work stop` only closes this Mac's connection.
- `metacomputer vm delete work` removes it here (settings, runtime folder, its identity) and leaves the server as it
  is, saying how to take the setup off. `vm delete work --remove-from-server` does that first, over your own SSH with
  your `sudo`: `/opt/metacomputer/bin/remote-uninstall all` puts your SSH settings back, turns metacomputer's
  firewall off (the network's dependency on it first, so stopping it doesn't stop the network; its one nftables table
  and its service; other rules are untouched), turns the idle stop off, removes
  its sudo rules, and locks its four accounts and removes their key lists (their home folders and the desktop's
  packages stay); then a fresh connection of yours is checked before anything is removed here.

## Oracle Cloud (OCI)

```bash
metacomputer vm create arm1 --on oci [--size arm-2x12] [--egress internet|off] [--idle-stop 15]   # with OCI_* set
```

**Credentials** come only from the environment of the command that needs them (`vm create`, `build`, `start`,
`stop`, `status`'s provider line, `vm delete`, `cloud ...`), so hand them over from your secrets manager for that one
command (most can run a command with secrets in its environment); nothing writes them to disk, puts them on a command line or prints them, and only `lib/remote.py` (the one
program that talks to OCI) gets them: the hub, `ssh` and every other helper are started without them.

| Variable | What |
|---|---|
| `OCI_USER` | the API user's OCID |
| `OCI_TENANCY` | the tenancy's OCID |
| `OCI_FINGERPRINT` | the API key's fingerprint |
| `OCI_REGION` | the region, e.g. `us-ashburn-1` |
| `OCI_API_KEY_PEM` | the API key's private key **as PEM text** (not a path) |
| `OCI_COMPARTMENT` | the compartment metacomputer works in, and only in |

**Oracle's Python SDK**: `python3 -m venv ~/.local/share/metacomputer/oci && ~/.local/share/metacomputer/oci/bin/pip
install oci`, then `OCI_PYTHON=~/.local/share/metacomputer/oci/bin/python` in `config/local.env` (or the
environment). Only `lib/remote.py` runs with it.

**A policy for the API user** that keeps it to one compartment (replace the group and compartment names):

```
Allow group <group> to manage instance-family in compartment <compartment>
Allow group <group> to manage virtual-network-family in compartment <compartment>
Allow group <group> to manage volume-family in compartment <compartment>
Allow group <group> to read all-resources in compartment <compartment>
Allow group <group> to inspect compartments in tenancy
```

**What it makes**, all in that compartment and tagged `metacomputer=NAME` (the network `metacomputer=network`), so
`metacomputer cloud status oci` lists them even if this Mac lost their settings:
- one VCN (IPv4 only) with one public subnet and an internet gateway, made with the first computer and deleted with
  the last; its security list lets in **SSH (port 22) and ICMP type 3 code 4** (what TCP needs to size its packets)
  and lets **nothing** out. It's made under a lock that one process on this Mac holds at a time, so two `vm create`s
  at once share one network, and `vm delete` leaves it while another computer is being made in it (if two Macs each
  made one, the oldest is used and deleting deletes both);
- each computer's own **network security group** (`metacomputer-NAME-` and the first 12 digits of the build's
  operation, tagged with the computer and that operation), made new by each build before its instance (never one
  found by its name: a group of the same name another Mac or an earlier try made is left as it is) and deleted with
  it. Its OCID is recorded from the create's answer before anything configures it, and everything afterwards (its
  first rules, written after it's read again once the create's wait is over; `net egress`, `vm delete`, the clean-up)
  acts only on that recorded OCID, read again and still tagged for this computer and this operation; a group an earlier version made carries no operation, so `net egress` refuses to change it
  (delete the computer and make it again). With `EGRESS=internet` it lets out every public IPv4 address (103 blocks: everything except the private,
  shared, loopback, link-local, documentation, benchmarking, multicast and reserved ranges the guest firewall also
  refuses), with `EGRESS=off` nothing once the computer is set up (while it's set up, the public addresses, for the
  packages it installs: OCI's rules can't tell apt from anything else; the guest firewall can, below). OCI enforces it outside the VM, so nothing in the guest, root included, can
  change it;
- the instance: Canonical's newest Ubuntu 24.04 image for the shape's architecture, a 50 GB boot volume, a public
  address, its security group, the IMDS v2 endpoints only (the legacy v1 ones disabled), the Oracle Cloud Agent's
  plugins off, and cloud-init user-data that pins the host key made on your Mac, makes **no default account**, and
  runs `bootstrap.sh` (`lib/cloud/guest.py`), which loads the guest firewall before anything else.

**What OCI can't block**: it applies no security rule to link-local addresses (169.254.0.0/16), where the instance
metadata service (169.254.169.254), the VCN's DNS resolver and its clock answer, whatever the rules say. So the
metadata service, which keeps the instance's user-data (with the host key's private half), is refused only by the
guest firewall, a layer that root inside the guest could undo. The desktop's accounts have no root (`cua` has no
sudo; `cuaadmin` may run only `metacomputer-admin`). `metacomputer net status` and the provider's manifest say this
rather than claim more.

**Sizes**: `arm-2x12` (VM.Standard.A1.Flex, 2 OCPU, 12 GB: the whole Always Free Arm allowance, so it costs nothing
there), `arm-1x6`, `x86-1x8` and `x86-2x8` (VM.Standard.E5.Flex, billed by the hour), and `x86-micro`
(VM.Standard.E2.1.Micro: Always Free, but 1 GB and a fraction of a core, so slow; a 2 GB swap file is added). When
an availability domain has no room for the shape ("out of host capacity", common for Arm), `vm create` tries each of
the region's other domains once and then stops, saying so: run `metacomputer --vm NAME build` again later. Nothing
is made until you agree (`vm create` shows the rate and what's kept first; `--yes` agrees).

**Lifecycle**: `start` starts a stopped instance and waits for the desktop (refused, like `stop`, while a session or a
handoff has the computer; `--override` goes ahead). `stop` asks the guest to shut down, then OCI stops the instance (a
soft stop, which OCI turns into a hard one after 15 minutes; metacomputer waits that long and three minutes more; its
disk stays); `stop --force` stops it at once, like pulling the power. With nothing using it for `IDLE_STOP` minutes (15 by default: no SSH
connection, which includes the hub's, a recording or the live view's tunnel, and no inner Claude session), the guest
powers itself off. **OCI doesn't count that as stopped**: the instance stays RUNNING, and a billed size is billed as
running, until it's stopped through the API: `metacomputer stop` does that (a plain stop once the guest's console
shows it's off). `start` of an instance that is RUNNING but doesn't answer reads its serial console first (the whole
capture, through to its end): only when the guest's last words are its power-off (`reboot: Power down`) does it stop
the instance and start it again;
otherwise it refuses and stops nothing (the guest may be busy, or just unreachable from here), and `stop --force`
then `start` restarts it. `status` says when it's RUNNING at OCI but doesn't answer. `vm delete NAME` terminates the
instance with its boot volume and its security groups (the ones its record names: its own, and any an earlier build of
it left behind, found by that build's operation tag, never by the name), and the network once no computer of yours uses
it. **A monthly
limit**: OCI's budgets (with an alert to your email) are set by the tenancy's administrator in the console;
metacomputer's API user can't set or enforce one, so keep a budget alert there.

**A build that fails** (or is interrupted: Ctrl-C, the terminal closed, killed) after it made the instance deletes what
that build made, and nothing else: the build names an operation, its launch is tagged and journaled with it, and the
clean-up (`lib/remote.py abort --op`) deletes the instances tagged with it, then the security group and the network
only if that operation made them (their OCIDs, recorded under the network lock as it makes them: never whichever
network exists by then) and nothing else uses them. The journal is on the disk before the launch (`F_FULLFSYNC`, or
`fsync` on a file system without it), so a crash of this Mac never loses a launch it started; an error making it so
refuses the launch.
With `--keep-on-failure`, the instance is recorded as this computer's, kept, before it's stopped (a later build sets
it up again instead of taking it for a lost launch, and records the computer's identity, without which its setup
won't finish). An instance it found (say, one of the same name whose
record this Mac lost) is left alone. It says nothing of it is left billing, or exactly what could not be deleted.
`build --keep-on-failure` stops it through the API instead, so its disk can be looked at, and says what still bills
(its boot volume) and how to delete it. An instance whose record can't be written on the Mac is deleted at once; a
launch whose answer was lost (the connection dropped) is found by the operation tag it was launched with and deleted
when you build again.

## Hetzner Cloud, DigitalOcean and Vultr

```bash
metacomputer vm create arm2 --on hetzner [--size cax11] [--region fsn1] [--egress internet|off] [--idle-stop 15]
metacomputer vm create x86a --on digitalocean [--size s-2vcpu-4gb] [--region nyc3]
metacomputer vm create x86b --on vultr [--size vc2-2c-4gb] [--region ewr]
```

Three thin drivers on one base (`lib/cloud/vps.py`; `providers/hetzner/`, `digitalocean/`, `vultr/`), each through the
provider's REST API (Python's standard library; no SDK to install). **Everything a stopped server costs is still
billed at all three**: only deleting it stops the charges ([costs](#what-a-stopped-computer-costs)).

**Credentials**: one API token each, only from the environment of the command that needs it (`vm create`, `build`,
`start`, `stop`, `status`'s provider line, `net egress`, `vm delete`, `cloud ...`), handed over by your secrets
manager for that one command; nothing writes it to disk, puts it on a command line or prints it, and only
`lib/remote.py` gets it.

| Provider | Variable | What it needs |
|---|---|---|
| Hetzner Cloud | `HCLOUD_TOKEN` | an API token of one project, Read & Write (the project is the boundary: everything metacomputer makes is in it) |
| DigitalOcean | `DIGITALOCEAN_TOKEN` | a personal access token; a custom-scoped one needs `droplet`, `firewall` and `ssh_key` (create, read, update, delete) and read on `regions`, `sizes`, `image`, `actions` and `vpc` (`vpc:read`: the resolver's address is checked against the Droplet's VPC; DigitalOcean adds `snapshot:read`). `vm create` checks `vpc:read` before it makes anything, and `cloud doctor digitalocean` names each read scope the token lacks. No tag scope: metacomputer marks what it makes by name |
| Vultr | `VULTR_API_KEY` | an API key whose access control list has this Mac's public address; a user with a custom policy needs the instance, SSH key and firewall permissions |

**What it makes**, each marked with the computer's name and the build's operation, so `metacomputer cloud status
PROVIDER` lists it even if this Mac lost its settings, and a launch whose record was lost is found:
- **its own firewall**, made before the server (the outside layer, below);
- **an SSH key**: the public half of the computer's admin key, because these providers make a server without a root
  password only when they're given a key. It's for the first boot only: the user-data's `disable_root` keeps root
  from using it, `bootstrap.sh` removes it from root and locks root's password (Vultr sets one anyway, shown in its
  console), and it's deleted with the computer;
- **the server**: Ubuntu 24.04 for the size's architecture, IPv4 only (no IPv6 address), a public address, no
  backups, monitoring or provider agent, and the same cloud-init user-data as on Oracle Cloud (`lib/cloud/guest.py`:
  the host key made on your Mac, no default account, `bootstrap.sh`). Hetzner marks it with labels
  (`metacomputer=NAME`, `metacomputer-op=OPERATION`); DigitalOcean and Vultr with its name or label,
  `mc-OPERATION-NAME`. A server that isn't marked this way is never touched.

**The outside layer**, what each provider's firewall enforces outside the guest (root in the guest can't change it),
and no more is claimed (`outside_egress` in each manifest; `net status` shows it):

| | Hetzner Cloud | DigitalOcean | Vultr |
|---|---|---|---|
| In | SSH (TCP 22) only | SSH (TCP 22) only | SSH (TCP 22) only |
| Out, `EGRESS=internet` | every public IPv4 address (TCP and UDP on every port, ICMP: the 103 blocks the guest also lets through), nothing private, shared or link-local | the same, plus DNS (TCP and UDP port 53 only) to exactly DigitalOcean's own resolver for the Droplet's VPC, a private address (without it nothing resolves): read once from the new Droplet at its first boot (before anything but cloud-init and `bootstrap.sh` has run in it), refused unless it's that VPC's `resolver_ip` as DigitalOcean's API names it (its documented second-to-last address), and pinned in its record; never read from the computer again (`net egress` and a re-setup use the pinned one); nothing else on that network | **not filtered**: Vultr's firewall filters only what comes in |
| Out, `EGRESS=off` | nothing: one outbound rule that reaches no one (ICMP to 192.0.2.1, a documentation address), because Hetzner lets everything out of a firewall with no outbound rule | nothing (no outbound rule: DigitalOcean lets out only what a rule allows) | not filtered |
| The metadata service (169.254.169.254) | **not refused**: Hetzner answers it locally, past its firewalls (as it does DHCP and its DNS resolvers; tested: it answers with `EGRESS=off` too) | **not refused**: tested, it answers through DigitalOcean's firewall with `EGRESS=off` too | not filtered |
| When it applies | named in the server's create call | applied right after the Droplet is made (DigitalOcean takes no firewall in that call; a Droplet it can't be applied to is deleted at once) | named in the create call, with its SSH rule already in it (a group with no rule isn't enforced) |

In every case the computer's own firewall (below) refuses the metadata service, link-local and private ranges, and
holds `EGRESS`; on Vultr it's the only egress layer, and `net egress` changes only it (and says there's nothing to
set outside).

**Sizes and regions** (`--size`, `--region`; the rate is said before anything is made):

| Provider | Sizes | Default | Regions (default first) |
|---|---|---|---|
| Hetzner Cloud | Arm (Ampere): `cax11` (2 vCPU, 4 GB), `cax21` (4, 8 GB), `cax31` (8, 16 GB); x86: `cx23` (2, 4 GB), `cx33` (4, 8 GB) | `cax11` | `fsn1`, `nbg1`, `hel1` (Hetzner's Arm sizes are in its EU locations) |
| DigitalOcean | `s-2vcpu-4gb`, `s-4vcpu-8gb` (x86: DigitalOcean has no Arm Droplets) | `s-2vcpu-4gb` | `nyc3`, and any region that offers the size |
| Vultr | `vc2-2c-4gb`, `vc2-4c-8gb` (x86) | `vc2-2c-4gb` | `ewr`, and any region that offers the size |

Each size bills by the hour up to its monthly price (list prices in each driver, with the date they were read);
Hetzner also bills the server's IPv4 address. A create is sent once and never retried (a second try after a lost
answer could make a second server); a provider with no room for the size there says so, and you choose another
`--region` or size.

**Lifecycle**: `start` powers a stopped server on and waits for the desktop; a server the provider says runs but whose
SSH doesn't answer is never stopped or started by `start` (`stop --force`, then `start`, restarts it). `stop` asks the
guest to shut down and waits for the provider to see it off; `stop --force` powers it off at once. Hetzner's and
DigitalOcean's shutdown call presses the guest's power button, and both see a guest that powered itself off as off
(tested). **Vultr has no such call**, its halt being a power-off, **doesn't see a guest that powered itself off** (it
still says running), and **its start restarts an instance that runs** (all three found live). So on Vultr `stop`
asks the guest itself over SSH (it must answer: one that doesn't is refused) and then waits up to 2 minutes for Vultr's
own state to say stopped. SSH going quiet proves nothing (the guest may be stalled, cut off, or not obeying), and
Vultr's state didn't change when tested live, so `stop` then says the guest may still be on, and billed in full either
way, and halts nothing: `stop --force` halts it through Vultr, as your explicit choice. The idle stop powers the guest off after `IDLE_STOP` minutes unused; on Vultr it's off by default (it would
save nothing, and leave a guest Vultr thinks runs: `status` says so, and `stop --force`, then `start`, restarts it).
Whatever the provider, **a stopped server is billed in full**: `vm create`, `stop` and `status` say so. `vm delete NAME`
deletes the server with its disk, its firewall and its key, by the ids recorded when they were made (DigitalOcean's
listings can lag a few seconds behind what was just made), then anything else marked with its name.

**A build that fails** deletes what that build made, as on Oracle Cloud: the server, read by the id journaled the
moment its create returned (and found by its operation's mark), then its firewall and key by the ids journaled as
each create returned (and by their mark), and nothing else; `--keep-on-failure` records the server as kept and powers
it off, and says it's still billed in full until it's deleted. A create whose answer was lost (the connection
dropped, or the provider failed) leaves only its operation in the journal: the clean-up looks for its mark again for
about a minute, and if nothing shows, it says a server may exist that it can't name, keeps the journal (and the
firewall and key), and fails, naming the provider's console and API call that would find it; no new computer is made
under that name until a later look finds it (and deletes it). An instance read by a journaled id is this operation's
only if it still carries the operation's mark: one marked for another is refused, touched by nothing. `vm delete`
of such a computer refuses too, keeping everything, until you've looked and say so (`vm delete NAME
--checked-provider`): then this Mac's settings and records go, but the operation stays listed by `metacomputer cloud
status PROVIDER` with where to look, its firewall and key kept for it, until `cloud teardown PROVIDER` finds what it
made and deletes it (or you drop it: `cloud teardown PROVIDER --forget OPERATION`, refused while anything shows under
its mark). Those three run only in your own terminal: an agent tells you the command instead. Whatever deletes, stops,
starts or forgets a server (a failed build's clean-up, `vm delete`, `start`, `stop`, `cloud teardown`) first reads it
by its own id and acts only if it carries, now, the computer's name and the operation recorded for it: a listing's row
is only a candidate, and anything that doesn't match stops the command with nothing done and the record kept (a
server named for the computer that this Mac's record didn't make included: delete it at the provider if it's yours).
`net egress` changes the provider's firewall only after the same check, and only the firewall recorded for the
computer, read by its id and still marked for its operation. `stop` reads what it needs (the provider's plan, its
credentials) before it touches anything on this Mac: one it can't read leaves the hub and recordings as they were.
The list of unresolved launches is changed under one lock, so two commands at once never lose each other's entries.

**Vultr**: its API key works only from the addresses on the key's access control list. When this Mac's address isn't
on it (it changes when your public address does), Vultr answers "Unauthorized IP address", and metacomputer says
which address to add (as a `/32`) and where (the key's access control, in Vultr's console). When a user's policy
lacks a permission a call needs, metacomputer reports Vultr's own words for it and which call it was, rather than
guess.

## What a stopped computer costs

Each provider's manifest (`lib/cloud/`, `billing`) says what a stopped computer still bills, and `vm create` says it
before you agree. What the idle stop saves depends on the provider, because a guest powering itself off isn't the
provider stopping it:

| Provider | Stopped through its API (`stop`) | The guest powered itself off (`IDLE_STOP`) | What the idle stop saves |
|---|---|---|---|
| Oracle Cloud, standard shapes (every `--size`) | compute stops billing; the boot volume is still billed (nothing within the Always Free allowance) | billed as running: OCI still counts it RUNNING | nothing by itself: `metacomputer stop` (or `vm delete`) is what stops the bill |
| Hetzner Cloud, DigitalOcean, Vultr | billed in full: a stopped server bills like a running one until it's deleted (Hetzner: its IPv4 address too) | billed in full (Hetzner and DigitalOcean see it off; Vultr still counts it as running; all bill it the same) | nothing: only deleting it (`vm delete`) stops the bill (Vultr's idle stop is off by default) |
| Your own server | between you and its host | metacomputer doesn't power it off (`IDLE_STOP=off`) | nothing |

The idle stop still ends the desktop and everything running in it when nobody has used it for a while; for a billed
size, stop or delete it when you're done.

`metacomputer cloud status oci` (instances, network, boot volumes metacomputer made), `cloud doctor oci` (the
credentials, the network's rules), `cloud setup oci` (makes the network) and `cloud teardown oci` (deletes the
network when no instance is left and no computer is being made in it: its security group there without an instance
yet; a group no computer's record on this Mac names, another Mac's or one whose record is gone, is never deleted on
its name: teardown says so, and you delete it in OCI's console once you know it's no one's; one another Mac makes
after teardown looked stops the network's deletion too, with nothing deleted). Both change the network
under the same lock as `vm create`. For `hetzner`, `digitalocean` and `vultr`: `cloud
status` lists the servers, firewalls and keys metacomputer made there (Hetzner: and their IPv4 addresses), `cloud
doctor` checks the token and each computer's firewall (SSH only in; out, public addresses or nothing), `cloud setup`
checks the token (there's no shared network to make), and `cloud teardown` deletes first-boot keys left over, once no
server is left and no computer is being made (its firewall there without its server).

## The setup, wherever it runs

The same steps for both (`scripts/remote-build.sh`), and running them again on a computer that's set up changes
nothing that's already right:

1. **Keys**: one set per computer, `~/.ssh/metacomputer-NAME/` (0700; each key 0600): `admin`, `ops` (the desktop
   user's shell), `mcp` (Cua Driver's MCP server, forced), `view` (one forward, to the live view), `fill` and `proxy`
   (the vault's accounts). The host key is pinned in its `known_hosts` as `mcomp-NAME`; a provider's computer gets a
   host key made here (its private half is deleted from the Mac when the setup ends), your server keeps its own.
2. **The payload**, uploaded over SSH as `cuaadmin`: `provisioning/guest` (the same files as the Mac's VM),
   `provisioning/remote`, Cua Driver's Linux build for the computer's architecture (`linux-arm64` or `linux-x86_64`,
   each pinned by SHA-256 in `config/defaults.env` and checked against the release's checksums; the pins were checked
   against the release's Sigstore bundle when they were made), the APT keys pinned by fingerprint, and the key lists.
3. **`provision.sh` with `MCOMP_TARGET=remote`**: the desktop on Xorg's dummy driver (a display in memory: LightDM,
   XFCE, AT-SPI and `/dev/uinput` as on the Mac), Firefox from Mozilla's repository, Chrome if `BROWSERS` has it,
   LibreOffice, Cua Driver with its telemetry off, the vault's accounts (`mcfill`, `mcproxy`), the guest firewall and
   the idle stop. It reboots into the desktop, sets the live-view password, checks the desktop, then `finalize.sh`
   narrows `cuaadmin`'s sudo to `metacomputer-admin`, removes every other sudo rule the provider's image brought
   (on a provider's computer; your own server keeps yours) and turns cloud-init off. (It's already off before that
   reboot: with the metadata service refused, a provider's datasource that can't tell the computer is the same one
   without it, as Vultr's can't, would take it for a new one and make new host keys, which the pinned key refuses.)
   On a provider's computer, `bootstrap.sh` also points apt at Ubuntu's own archive when the image's mirror is on the
   provider's private network (DigitalOcean's is, inside DigitalOcean), which the firewalls refuse. A provider's computer is set up
   once (delete it and make a new one to start over); your own server can be set up again with `build`, which runs
   `bootstrap.sh` with your sudo again.

## What keeps it apart

- **In**: SSH only, key only (no passwords, no root, `AllowUsers`), and the provider's firewall says the same for a
  computer it makes. The live view's VNC server listens on the computer's own `127.0.0.1:5900` only; `metacomputer
  view --open` (or `view --tunnel`, for another VNC client) opens an SSH tunnel to it from `127.0.0.1:VNC_PORT` on
  your Mac with the `view` key, which may open that one forward and nothing else; the tunnel closes when the viewer
  does. The command returns once the tunnel is up (what SSH says goes to a private log (`tunnel/view-tunnel.log` in its log folder: 0600, in a
  0700 folder), at most 1 MB of it however much the VM's end makes SSH print, and what it said while it connected is
  shown), so a program that reads its output doesn't wait for the tunnel to close. There's no public port for it,
  and `view --tailnet` is refused.
- **Out, in two layers**, each said for what it is:
  - **Outside the VM, where the provider enforces it.** On Oracle Cloud, the computer's own network security group
    (above): public addresses only with `EGRESS=internet`, nothing with `EGRESS=off`. Nothing in the guest can change
    it, but it can't refuse link-local addresses, so the metadata service stays reachable at the network level. On
    Hetzner Cloud and DigitalOcean, the computer's own firewall does the same (public addresses only, or nothing), and
    the metadata service isn't refused by it either (Hetzner answers it past its firewalls; DigitalOcean doesn't say).
    On Vultr there's no outside egress layer: its firewall filters only what comes in. Your own server gets no outside
    layer from metacomputer: only its own firewall (your host's firewall is yours to set).
  - **Inside the VM** (`guest-firewall`, nftables): the rules apply to every account, root included, though root in
    the guest could remove them (the desktop's accounts can't). They refuse the cloud's metadata services and every
    link-local address (169.254.0.0/16, which holds 169.254.169.254 on every major cloud, and fe80::/10), and
    private, shared and reserved ranges (10/8, 172.16/12, 192.168/16, 100.64/10, fc00::/7, which holds OCI's IPv6
    metadata address, multicast, ...): no provider-internal network, no home network, no tailnet. The exceptions
    are the system services that need the cloud's own addresses, each to its one port and matched by its account:
    the DNS resolver (53), the DHCP client (67, 547) and the clock (123); programs in the desktop resolve names
    through the local resolver.
  - **`EGRESS`** (per computer): `internet` (the default) lets the rest of the internet through; `off` lets nothing
    else out, package downloads included (updates come again with `net egress internet`). A computer made with
    `EGRESS=off` is still set up: until its setup ends, its own firewall lets out apt's downloads too (the `_apt`
    account, TCP 80 and 443, public addresses only; `net status` says `setup`), and its provider's rules the public
    addresses; at the end of the setup both are set to `off` and read back, or the build fails. A setup that fails
    ends it too: `provision.sh`'s own failure handler, and the build's (its own firewall, then the provider's rules,
    for a computer it keeps, one it set up again or your own server). The build also closes it on its own deadline,
    3 hours on this Mac's clock, whatever the guest's clock says. Inside, it ends by itself after 3 hours of the
    guest's uptime: the marker (`guest-firewall mark-setup`) names the boot and the uptime it was made at, counts only
    in that boot and never from the future, and a timer on the monotonic clock ends it. The rule's own wall-clock end
    (nftables' `meta time`) is only a backstop. A computer set up again (kept after a failed setup) opens both layers
    again for its setup's downloads and closes them the same ways. There's no allowlist
    of names here, and `net allow` is refused.
  - **DHCP**, whichever network stack sends it (systemd-networkd as its own account, NetworkManager as root): let out
    by its privileged ports and a DHCP server's destinations (68 to 67: broadcast, private and link-local; 546 to 547:
    `ff02::1:2` and link-local), in the full rules and in the fallback, so the computer keeps its address after a
    reboot.
  - **Fail closed.** The firewall's service runs before the network, and the network requires it (a drop-in for
    `systemd-networkd` and one for `NetworkManager`, the two Ubuntu uses; the setup refuses a server whose network
    something else manages, putting its own settings back): if its rules don't load, an SSH-only fallback does (SSH
    in, nothing out but replies, and nothing of the full rules in it; `net status` says `fallback`), and if not even
    that loads, the network stays down. At a provider computer's
    first boot the network is up before cloud-init runs: `bootstrap.sh` loads the firewall before it makes any
    account, and the provider's rules are in force from the start.
  - **Open connections too.** A tightened rule applies to connections already open: the only packets let out as
    part of an existing connection are replies to connections made to the computer (SSH).
  - **`metacomputer net status`** says the setting, what's enforced outside the VM, and what's in force inside it,
    each apart. **`net egress internet|off`** changes it on a computer that's set up: the provider's rules first
    (read back), then the guest's firewall (read back), then the setting; if the computer doesn't answer or the
    provider can't be asked, nothing is changed.
- **Your Mac's things stay on your Mac**: no file of yours, no secret and no key but the computer's own goes to it;
  ssh forwards nothing of the Mac's (no agent, no X11, no ports, no config file); the vault types a value into a page
  over its fill account's SSH connection and never stores it there; API keys stay on the Mac behind the vault's
  proxy (an SSH reverse forward to the computer's `127.0.0.1`).
- **The vault binds the computer, not the name**: its binding of a computer elsewhere names the computer's identity
  (`~/.ssh/metacomputer-NAME/computer-id`: a hash of its provider instance, or your server's setup, and its pinned
  host key). A computer deleted and made again under the same name has another one, and the vault refuses it until
  it's bound again (`metacomputer secrets bind`). API tokens are for the computer they were made for: a request on a
  token whose computer isn't the bound one any more is refused (nothing sent), the token revoked and the tunnel
  closed, checked when a request arrives, again just before it's forwarded, and every second while an answer streams;
  a new binding gets a new tunnel and prunes the old computer's tokens. `vm delete` revokes the VM's tokens in
  whatever vault it uses and locks the computer's own vault.
- **Provider credentials reach one program.** `bin/metacomputer`, `scripts/lib.sh` and `scripts/remote-build.sh`
  take `OCI_*`, `HCLOUD_TOKEN`, `DIGITALOCEAN_TOKEN` and `VULTR_API_KEY` out of the environment before they do anything
  else; only `lib/remote.py` is started with them, and it takes them out of its own environment first, so nothing it
  starts gets them. The REST calls go only to the providers' own API addresses, over HTTPS, and never follow a
  redirect (`lib/provider_http.py`), which would carry the token elsewhere.
- **What still differs from the Mac's VM**: the provider runs the hardware, so its screen, disk and what's typed into
  it are on the provider's machines (choose a VM on your Mac for sensitive work); `SSH_AUTH=cert` isn't available
  (its certificates name the Mac's gateway address), and neither are snapshots, volumes or the event watcher's health
  probe.

## How it's reached: the transport

Every command that talks to the computer (the hub's driver connection, `sh`, `put`/`get`, `shot`, `record`, the live
view's tunnel, the vault's fills and tokens) uses one transport (`lib/cloud/transport.py`, through `scripts/lib.sh`'s
`ssh_base` and the vault's binding): SSH straight to the computer's address, its host key pinned under `mcomp-NAME`
(never under the address). A provider whose computers have no address of their own adds its tunnel there
(`transport.register`, run as ssh's ProxyCommand through `lib/proxywrap.py`, which leaves ssh's process group and ends
the tunnel when ssh goes, so it never keeps a computer awake and billing). Fly Sprites register `sprite proxy -W :22`
([docs/sprites.md](sprites.md)); the vault's binding pins that program and runs it through itself (`mcomp-vault
_tunnel`).

## Settings (`config/vms/NAME.env`)

`PLACE` (`server`, `oci`, `hetzner`, `digitalocean` or `vultr`), `PROVIDER` (`ubuntu-vm`, or the provider's own name:
`oci`, `hetzner`, `digitalocean`, `vultr`), `TRANSPORT` (`ssh`), `SSH_HOST` and `SSH_HOST_PORT`
(your server's address; a provider's computer gets its address from what the provider made, kept in
`.run-vms/NAME/remote.json`), `SETUP_USER`, `SIZE`, `REGION` (Hetzner, DigitalOcean, Vultr), `EGRESS`, `IDLE_STOP`; `SSH_PORT` is kept only so no two computers
here share a port, and `VNC_PORT` is where the live view's tunnel listens on your Mac. `PLACE` other than `mac`
is refused in `config/local.env`: only a computer of its own can be elsewhere.
