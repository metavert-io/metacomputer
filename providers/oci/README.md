# Oracle Cloud (`oci`)

For people: [docs/remote.md](../../docs/remote.md#oracle-cloud-oci) (setup, token scopes, what keeps it apart, what it costs). This page is for
contributors: what's in this folder ([providers/README.md](../README.md) says what every provider's folder holds).

Metacomputer makes, starts, stops and deletes Ubuntu 24.04 VMs in one compartment of your tenancy, through Oracle's
Python SDK (`OCI_PYTHON`: a Python that has it). Everything else (SSH, the desktop, its own firewall) is the generic
Ubuntu VM's (`lib/cloud/ubuntu_vm.py`).

| File | What |
|---|---|
| `adapter.py` | the driver: the SDK facade (`SDK`), the network, each computer's network security group, launches journaled by operation, the console read for a guest that powered itself off, rates |
| `manifest.py` | its capabilities, sizes and credentials; `CLI`: its words and settings for the command line |
| `cli.sh` | its own steps in `vm create`, `vm delete` and `stop` (their words) |
| `settings.env` | what a computer's settings file says for it |
| `claims.tsv` | its doc-claims rows |

**Credentials** (only from the environment of the command that needs them; only `lib/remote.py` gets them):
`OCI_USER`, `OCI_TENANCY`, `OCI_FINGERPRINT`, `OCI_REGION`, `OCI_API_KEY_PEM`, `OCI_COMPARTMENT`.

**What keeps it apart**: its own firewall inside, and a network security group of its own outside that lets out public
addresses only (or nothing); OCI applies no security rule to 169.254.0.0/16, so its metadata service can't be refused
from outside (`outside_egress.metadata` is False). **Billing**: stopped through the API, only the boot volume; a guest
that powered itself off is billed as running until it's stopped there. SECURITY.md has the details.

## Tests

`python3 tests/test_cloud_adapters.py` runs the contract every adapter passes against it (no VM, no network), checks
its manifest and that no shared code names it. Its own: `python3 tests/test_remote.py` (a stand-in SDK; no VM, no network).

## What stays shared

The hub's guards, the vault, the handoff gate (every command that changes one of its computers gates it the same
way), the network isolation and gateway rules, and the secrets flow are the framework's, not this folder's.
