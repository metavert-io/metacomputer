# Cloudflare Containers (`cloudflare`)

For people: [docs/cloudflare.md](../../docs/cloudflare.md) (setup, token permissions, plans, persistence and its 30-day
limits, the allowlist's interception, costs). This page is for contributors: what's in this folder
([providers/README.md](../README.md) says what every provider's folder holds).

A computer here is a container (an Ubuntu 24.04 microVM, x86_64) run by a Durable Object of a control Worker that
metacomputer deploys into the user's own Cloudflare account, one Worker per computer, on workers.dev only (no route on
the account's domains). The Worker starts, stops and snapshots the container, carries SSH (a websocket piped to the
container's sshd), keeps it awake while a connection is open, stops it when idle, and restores each snapshot it keeps
before Cloudflare's 30 days run out. Everything else is the generic remote computer's (lib/cloud/ubuntu_vm.py).

| File | What |
|---|---|
| `worker/index.js` | the control Worker: its entry (every request's HMAC checked with the computer's key: logic.js verify, no body read before the header could be one, a nonce never taken twice), the `Computer` Durable Object (start, stop = snapshot then destroy, checkpoints, restore, egress, the boot script, logs, SSH over a websocket, the alarm: the idle stop, a stop by Cloudflare, the refresh, which a second object of the class, the refresher, runs in a container of its own), and the allowlist's outbound handlers (`Allow`, which checks the Host is its name and never follows a redirect; `Deny`) |
| `worker/logic.js` | its decisions with no runtime import (tested with node: tests/cloudflare_worker_test.mjs) |
| `adapter.py` | the adapter: the deploy (image, preparation, upload, workers.dev, container application with Cloudflare's SSH off), every operation checked against the Worker's own word (`_live`, `owns`) or, with the token, its bindings (`owns_script`), delete with what Cloudflare keeps said, the egress as policies (`policy_for`, `policy_kind`, `widens`), rates; it registers the `cloudflare` transport. No network or process import: lib/remote.py hands it its clients |
| `api.py` | the REST client (on `lib/cloud/vps.py`'s API base), the calls wrangler makes for a Durable-Object-run container, scrubbed of the token and the account's ID |
| `images.py` | Ubuntu's image from Docker Hub into the account's registry without Docker (OCI distribution API), each part checked by its digest; what a repository holds; the image's manifest deleted |
| `wire.py` | HTTPS to the Worker (signed requests, workers.dev only, no redirect) and the registries; the websocket client |
| `broker.py` | the transport's broker: the only process besides `lib/remote.py` that holds the computer's key; a 0600 socket per computer that carries SSH connections and answers the identity check |
| `tunnel.py` | ssh's ProxyCommand and the identity check, asking the broker; stdlib and Python 3.9 only (the vault stages a copy) |
| `manifest.py` | its capabilities and the pinned image; `CLI`: its credentials, transport, options (`--worker`), words |
| `remote.py` | its part of `lib/remote.py`: the key (made, read, removed), create, abort, wake, delete (journaled; residue said and noted), plan, cloud, the transport's checks, the egress's narrowing rule, `exec` (the boot script and logs as fixed forms), `checkpoint`, `allow` |
| `cli.sh` | its own steps in `vm create`, `stop`, `net`, checkpoints and `reset`, `desktop-restart`, `logs`, and `can_ask` (the token, or the computer's key) |
| `build.sh` | its own build steps: the deploy and first step through the Worker, the desktop by the boot script, the baseline snapshot |
| `guest/` | its bootstrap, the boot script (`/opt/metacomputer/cloudflare/boot`) and sshd's port (2222), copied into the payload as `cloudflare/` |
| `settings.env`, `claims.tsv` | its settings; its doc-claims rows |

**The credential rule.** `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` reach `lib/remote.py` only (taken off every
other program's environment first thing, as every provider's are), for `vm create` and `vm delete`. Daily use needs the
computer's own key instead: made by `vm create`, kept 0600 beside its SSH keys (`$SSH_DIR/cloudflare-key`), read only by
`lib/remote.py`, given to the broker on its standard input, uploaded once as the Worker's secret. It never travels:
each request carries an HMAC made with it. ssh, the hub, the vault, the tunnel and the guest never get either.

**Changing the Worker's protocol** (its signature, its routes, its answers) needs an upgrade path for computers already
made: a computer's Worker is uploaded once, by `vm create`, so a Mac speaking a newer protocol can't talk to an older
Worker. Nothing has been released before this protocol; a later change must redeploy existing Workers (with the token)
or accept both protocols for a release.

**What keeps it apart**: Cloudflare's outbound rules, outside the container (the internet with private ranges and the
metadata address refused, nothing, or metacomputer's handlers for an allowlist, which see that traffic decrypted);
Cloudflare's own SSH off on the application; the boot script starts the desktop with `runuser` (the platform's commands
keep root's capabilities even as another user) and ends every process of an account but root's that holds one; after
the setup the Worker runs no command but the boot script and the logs. **Input**: no `/dev/uinput`, so the hub sends
keys, typing and pixel clicks in the foreground. **Billing**: running, memory and disk as provisioned and the CPU used;
stopped, its snapshots (free during Cloudflare's beta). SECURITY.md ("Cloudflare Containers") has the details.

## Tests

`python3 tests/test_cloudflare.py` (stand-ins for Cloudflare's REST API, the registries and the Worker; it runs the
Worker's own checks, `tests/cloudflare_worker_test.mjs`, with node when it's there). `python3
tests/test_cloud_adapters.py` runs the contract every adapter passes against it, checks its manifest and that no shared
code names it.
