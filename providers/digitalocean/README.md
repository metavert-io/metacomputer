# DigitalOcean (`digitalocean`)

For people: [docs/remote.md](../../docs/remote.md#hetzner-cloud-digitalocean-and-vultr) (setup, token scopes, what keeps it apart, what it costs). This page is for
contributors: what's in this folder ([providers/README.md](../README.md) says what every provider's folder holds).

A thin driver on `lib/cloud/vps.py` (servers billed by the hour, through the provider's REST API with Python's
standard library).

| File | What |
|---|---|
| `adapter.py` | its REST calls (`DigitalOceanAPI`), the scope checks, the firewall applied right after the Droplet is made, its VPC resolver let through (pinned at the first boot) |
| `manifest.py` | its capabilities, sizes, regions and credential; `CLI`: its API host |
| `settings.env`, `claims.tsv` | its settings; its doc-claims rows (none of its own yet: its promises' checks read shared code too) |

**Credential**: `DIGITALOCEAN_TOKEN`, with the scopes docs/remote.md lists (`vpc:read` among them). **What keeps it
apart**: its own firewall inside, and a Cloud Firewall outside (public IPv4 addresses only, and DNS to its VPC's
resolver; or nothing). **Billing**: a powered-off Droplet is billed in full: only deleting it stops the charges.

## Tests

`python3 tests/test_cloud_adapters.py` runs the contract every adapter passes against it (no VM, no network), checks
its manifest and that no shared code names it. Its own: `python3 tests/test_remote.py` (a stand-in API; no VM, no network).

## What stays shared

The hub's guards, the vault, the handoff gate (every command that changes one of its computers gates it the same
way), the network isolation and gateway rules, and the secrets flow are the framework's, not this folder's.
