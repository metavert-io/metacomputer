# Vultr (`vultr`)

For people: [docs/remote.md](../../docs/remote.md#hetzner-cloud-digitalocean-and-vultr) (setup, token scopes, what keeps it apart, what it costs). This page is for
contributors: what's in this folder ([providers/README.md](../README.md) says what every provider's folder holds).

A thin driver on `lib/cloud/vps.py` (servers billed by the hour, through the provider's REST API with Python's
standard library).

| File | What |
|---|---|
| `adapter.py` | its REST calls (`VultrAPI`), its firewall group (what comes in only), the guest asked to shut down over SSH (`SOFT_STOP = "guest"`) |
| `manifest.py` | its capabilities, sizes, regions and credential; `CLI`: its API host, its idle stop off by default |
| `settings.env`, `claims.tsv` | its settings; its doc-claims rows (none of its own yet: its promises' checks read shared code too) |

**Credential**: `VULTR_API_KEY`, an API key whose access control list has this Mac's public address. **What keeps it
apart**: its own firewall only (Vultr's firewall groups filter only what comes in: `outside_egress.enforced` is False).
**Billing**: a stopped instance is billed in full: only deleting it stops the charges.

## Tests

`python3 tests/test_cloud_adapters.py` runs the contract every adapter passes against it (no VM, no network), checks
its manifest and that no shared code names it. Its own: `python3 tests/test_remote.py` (a stand-in API; no VM, no network).

## What stays shared

The hub's guards, the vault, the handoff gate (every command that changes one of its computers gates it the same
way), the network isolation and gateway rules, and the secrets flow are the framework's, not this folder's.
