# A computer elsewhere: an Ubuntu VM reached over SSH

metacomputer's computers normally run on your Mac (Lume VMs). A computer can also be an **Ubuntu 24.04 VM somewhere
else**, reached over SSH: **your own server**, or one a **provider** makes for you (Oracle Cloud first). It gets the
same desktop (XFCE, Firefox, optionally Chrome, LibreOffice), the same Cua Driver, the same accounts, and your Mac
stays the controller: the hub and all its guards, the vault, recordings, the action log and the CLI run here, as for
a VM on your Mac. The difference is where the VM runs, and what keeps it apart from everything else (its own firewall
instead of the Mac's gateway).

| | Your own server (`--on server`) | Oracle Cloud (`--on oci`) |
|---|---|---|
| Who makes it | you | metacomputer, in your OCI compartment |
| Who powers it on and off | you (metacomputer never does) | `start` / `stop`; the guest powers itself off after `IDLE_STOP` minutes unused (OCI bills a billed size until `stop`) |
| Architecture | arm64 or x86_64 (found out) | from `--size` |
| What it costs | between you and its host | `vm create` says the rate first; Arm within the Always Free allowance costs nothing |
| What's kept | its disk | its disk (`PERSIST=disk`), also while stopped |
| Snapshots, `reset`, checkpoints | no | no (not yet) |
| Volumes and Mac folders | no (not yet) | no (not yet) |

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
     the prompt comes from your server, and the password isn't echoed). It makes the accounts `cua` (the desktop,
     no sudo) and `cuaadmin` (maintenance), each reached only with keys made on your Mac for this computer, and
     reports the server's host key, which is pinned on your Mac as `mcomp-work`;
  3. from then on uses only those keys: uploads the payload and runs the same setup as every VM (below).
- Your account keeps signing in as before, with the keys in its home (`AllowUsers` lists it; a `Match` block keeps its
  own `authorized_keys`), and keeps its own `sudo`.
- metacomputer never powers your server on or off; `metacomputer --vm work stop` only closes this Mac's connection.
- `metacomputer vm delete work` removes it here (settings, runtime folder) and leaves the server as it is; it says
  what to remove there (the four accounts, the `sshd_config.d` files, the firewall service), or delete the keys folder
  on your Mac so it can't sign in again.

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
- one VCN with one public subnet and an internet gateway, made with the first computer and deleted with the last;
  its security list lets in **SSH (port 22) and ICMP type 3 code 4** (what TCP needs to size its packets) and nothing
  else;
- the instance: Canonical's newest Ubuntu 24.04 image for the shape's architecture, a 50 GB boot volume, a public
  address, the IMDS v2 endpoints only, the Oracle Cloud Agent's plugins off, and cloud-init user-data that pins the
  host key made on your Mac, makes **no default account**, and runs `bootstrap.sh` (`lib/cloud/guest.py`).

**Sizes**: `arm-2x12` (VM.Standard.A1.Flex, 2 OCPU, 12 GB: the whole Always Free Arm allowance, so it costs nothing
there), `arm-1x6`, `x86-1x8` and `x86-2x8` (VM.Standard.E5.Flex, billed by the hour), and `x86-micro`
(VM.Standard.E2.1.Micro: Always Free, but 1 GB and a fraction of a core, so slow; a 2 GB swap file is added). When
an availability domain has no room for the shape ("out of host capacity", common for Arm), `vm create` tries each of
the region's other domains once and then stops, saying so: run `metacomputer --vm NAME build` again later. Nothing
is made until you agree (`vm create` shows the rate and what's kept first; `--yes` agrees).

**Lifecycle**: `start` starts a stopped instance and waits for the desktop; `stop` shuts the guest down, then OCI
stops the instance (its disk stays; OCI bills a stopped standard instance for its disk only). With nothing using it
for `IDLE_STOP` minutes (15 by default: no SSH connection, which includes the hub's, a recording or the live view's
tunnel, and no inner Claude session), the guest powers itself off. **OCI doesn't notice that**: an instance whose
guest powered itself off still counts as running, and a billed size is still billed, until it's stopped through the
API. `metacomputer stop` does that (a plain stop, since the guest is already off), and `start` does it before
starting the instance again; `status` says when it runs at OCI but doesn't answer. Within the Always Free allowance
this costs nothing either way; for a billed size, stop it when you're done rather than rely on the idle stop. `vm delete NAME` terminates the instance with its
boot volume, and the network once no computer of yours uses it. **A monthly limit**: OCI's budgets (with an alert to
your email) are set by the tenancy's administrator in the console; metacomputer's API user can't set or enforce one,
so keep a budget alert there.

`metacomputer cloud status oci` (instances, network, boot volumes metacomputer made), `cloud doctor oci` (the
credentials, the network's rules), `cloud setup oci` (makes the network) and `cloud teardown oci` (deletes the
network when no instance is left).

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
   (on a provider's computer; your own server keeps yours) and turns cloud-init off. A provider's computer is set up
   once (delete it and make a new one to start over); your own server can be set up again with `build`, which runs
   `bootstrap.sh` with your sudo again.

## What keeps it apart

- **In**: SSH only, key only (no passwords, no root, `AllowUsers`), and the provider's firewall says the same for a
  computer it makes. The live view's VNC server listens on the computer's own `127.0.0.1:5900` only; `metacomputer
  view --open` (or `view --tunnel`, for another VNC client) opens an SSH tunnel to it from `127.0.0.1:VNC_PORT` on
  your Mac with the `view` key, which may open that one forward and nothing else; the tunnel closes when the viewer
  does. There's no public port for it, and `view --tailnet` is refused.
- **Out** (`guest-firewall`, nftables, loaded before the network comes up): every account, root included, is refused
  the cloud's metadata services and every link-local address (169.254.0.0/16, which holds 169.254.169.254 on every
  major cloud, and fe80::/10), and private, shared and reserved ranges (10/8, 172.16/12, 192.168/16, 100.64/10, fc00::/7,
  which holds OCI's IPv6 metadata address, multicast, ...): no provider-internal network, no home network, no tailnet.
  The exceptions are the system services that need the cloud's own addresses, each to its one port and matched by its
  account: the DNS resolver (53), the DHCP client (67, 547) and the clock (123); programs in the desktop resolve names
  through the local resolver. **`EGRESS`** (per computer): `internet` (the default) lets the rest of the internet
  through; `off` lets nothing else out except package downloads, so security updates keep coming. There's no
  allowlist of names here; `metacomputer net status` shows the setting, and `net allow` is refused.
- **Your Mac's things stay on your Mac**: no file of yours, no secret and no key but the computer's own goes to it;
  ssh forwards nothing of the Mac's (no agent, no X11, no ports, no config file); the vault types a value into a page
  over its fill account's SSH connection and never stores it there; API keys stay on the Mac behind the vault's
  proxy (an SSH reverse forward to the computer's `127.0.0.1`).
- **What still differs from the Mac's VM**: the provider runs the hardware, so its screen, disk and what's typed into
  it are on the provider's machines (choose a VM on your Mac for sensitive work); `SSH_AUTH=cert` isn't available
  (its certificates name the Mac's gateway address), and neither are snapshots, volumes or the event watcher's health
  probe.

## How it's reached: the transport

Every command that talks to the computer (the hub's driver connection, `sh`, `put`/`get`, `shot`, `record`, the live
view's tunnel, the vault's fills and tokens) uses one transport (`lib/cloud/transport.py`, through `scripts/lib.sh`'s
`ssh_base` and the vault's binding): SSH straight to the computer's address, its host key pinned under `mcomp-NAME`
(never under the address). A provider whose computers have no address of their own adds its tunnel there
(`transport.register`, run as ssh's ProxyCommand through `lib/proxywrap.py`, which ends the tunnel when ssh goes, so
it never keeps a computer awake and billing); the vault takes a computer reached that way once its binding learns
the tunnel too.

## Settings (`config/vms/NAME.env`)

`PLACE` (`server` or `oci`), `PROVIDER` (`ubuntu-vm` or `oci`), `TRANSPORT` (`ssh`), `SSH_HOST` and `SSH_HOST_PORT`
(your server's address; a provider's computer gets its address from what the provider made, kept in
`.run-vms/NAME/remote.json`), `SETUP_USER`, `SIZE`, `EGRESS`, `IDLE_STOP`; `SSH_PORT` is kept only so no two computers
here share a port, and `VNC_PORT` is where the live view's tunnel listens on your Mac. `PLACE` other than `mac`
is refused in `config/local.env`: only a computer of its own can be elsewhere.
