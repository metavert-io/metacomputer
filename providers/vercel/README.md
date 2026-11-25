# Vercel Sandbox (`vercel`)

For people: [docs/vercel.md](../../docs/vercel.md) (setup, token scope, plan limits, what keeps it apart, what it costs).
This page is for contributors: what's in this folder ([providers/README.md](../README.md) says what every provider's
folder holds).

Persistent Vercel sandboxes made, started, stopped, snapshotted and deleted through Vercel's REST API (the endpoints
Vercel's own SDK uses), reached through Vercel's command API: there's no TCP into a sandbox. A stop snapshots the disk
and powers the VM off, so every start boots the disk afresh: the boot script starts sshd and the desktop each time.

| File | What |
|---|---|
| `adapter.py` | the adapter: sandboxes tagged with their computer and their build's operation, every operation checked against the sandbox's creation time and tags first (`_live`), the network policy, snapshots as checkpoints (their names in the computer's record), the boot script run at every start, the snapshots a stop leaves behind deleted, rates; it registers the `vercel` transport |
| `api.py` | the REST client (stdlib, on `lib/cloud/vps.py`'s API base): its HTTP given by `lib/remote.py` (`lib/provider_http.py`), errors scrubbed of the token and the team's and project's IDs |
| `broker.py` | the transport's broker: the only process besides `lib/remote.py` that holds the token; a 0600 socket per computer that carries SSH connections (the guest's relay, through the command API) and answers the identity check; it keeps the session going while a connection is open |
| `tunnel.py` | ssh's ProxyCommand and the identity check, asking the broker; stdlib and Python 3.9 only (the vault stages a copy of it) |
| `manifest.py` | its capabilities; `CLI`: its credentials, transport, options (`--sandbox`), words |
| `remote.py` | its part of `lib/remote.py`: create, abort, wake (a session, the boot script, the broker), delete (with every snapshot), plan, cloud, the transport's checks, the policy's narrowing rule, `exec`, `checkpoint`, `allow` |
| `cli.sh` | its own steps in `vm create`, `stop`, `net`, checkpoints and `reset`, `desktop-restart`, `logs` |
| `build.sh` | its own build steps: the first step through Vercel's API, the desktop by the boot script, the baseline snapshot |
| `guest/` | its bootstrap, the boot script, the relay and sshd's listen settings (`/opt/metacomputer/vercel/`), copied into the payload as `vercel/` |
| `settings.env`, `claims.tsv` | its settings; its doc-claims rows |

**The credential rule.** `VERCEL_TOKEN`, `VERCEL_TEAM_ID` and `VERCEL_PROJECT_ID` reach `lib/remote.py` (taken off
every other program's environment first thing, as every provider's are) and, on its standard input, the computer's
broker. ssh, the hub, the vault and the tunnel never get them: they reach the sandbox through the broker's socket.

**What keeps it apart**: Vercel's network policy, outside the VM, is the one egress layer; the guest's nftables keep
Vercel's agent port (tcp 23456) from every account but root, put back by the boot script at every start, before
anything of the desktop's runs. **Input**: no `/dev/uinput`, so the hub sends keys, typing and pixel clicks in the
foreground. **Billing**: stopped, only its snapshot. SECURITY.md ("Vercel Sandbox") has the details.

## Tests

`python3 tests/test_cloud_adapters.py` runs the contract every adapter passes against it (no VM, no network), checks
its manifest and that no shared code names it. Its own: `python3 tests/test_vercel.py` (a stand-in for Vercel's API; no
VM, no network).
