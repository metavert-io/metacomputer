"""metacomputer's provider framework: the places its computers can be (lib/placement.py) and the adapters for them.

    adapter.py        the interface (lifecycle, transport, snapshots, egress, connection, cost) and its refusals
    capabilities.py   the capability manifest, PERSIST and the feature map
    registry.py       the providers, an explicit list: each is a folder providers/<name>/ (its adapter, manifest,
                      guest parts, settings, docs, doc-claims and tests; providers/README.md)
    local_lume.py     this Mac (`local-lume`): what it offers; its actions are the existing metacomputer commands
    ubuntu_vm.py      an Ubuntu 24.04 VM reached over SSH: what all of them share, and your own server
                      (`ubuntu-vm`, place `server`), which needs no provider code
    vps.py            what the providers that bill servers by the hour share (their REST API, a firewall and a
                      first-boot key per computer, billing in full while stopped)
    transport.py      how SSH reaches a computer that isn't on this Mac: the hook a provider's tunnel plugs into
    guest.py          the cloud-init user-data and key lists a VM reached over SSH is given
    fake.py           an in-memory stand-in for the tests (`fake`), never a provider

Every adapter registered here passes the same contract tests (tests/test_cloud_adapters.py). providers() is the
places that make up `cloud`: the registered adapters that are neither this Mac's nor a test stand-in."""
from . import capabilities, registry
from .adapter import INTERFACE, Adapter, AdapterError, HandledByCLI, NotSupported
from .fake import Fake
from .local_lume import LocalLume
from .ubuntu_vm import UbuntuVM

# This Mac's, the tests' stand-in and your own server, then each registered provider's (providers/<name>/adapter.py),
# in the registry's order.
ADAPTERS = {LocalLume.name: LocalLume, Fake.name: Fake, UbuntuVM.name: UbuntuVM}
for _name in registry.PROVIDERS:
    _cls = registry.adapter_class(_name)
    if _cls.name != _name or _cls.name in ADAPTERS:
        raise ImportError(f"providers/{_name}/adapter.py registers {_cls.name!r}, not {_name!r}")
    ADAPTERS[_cls.name] = _cls
del _name, _cls

__all__ = ["ADAPTERS", "INTERFACE", "Adapter", "AdapterError", "HandledByCLI", "NotSupported", "adapter",
           "by_place", "capabilities", "providers", "registered", "registry"]


def registered():
    """The registered adapters' names."""
    return sorted(ADAPTERS)


def adapter(name, **kw):
    """A new instance of the adapter `name` (keyword arguments go to it: a remote adapter's computers, states, and
    for a provider its api or env); KeyError for one that isn't registered."""
    return ADAPTERS[name](**kw)


def by_place(place):
    """The adapter class whose computers are at `place` (mac, server, a provider's place); KeyError for none."""
    for cls in ADAPTERS.values():
        if cls.manifest["place"] == place and not cls.test_only:
            return cls
    raise KeyError(place)


def providers():
    """{place: label} for the providers' places (the group `cloud`): adapters that aren't this Mac's or a stand-in."""
    return {cls.manifest["place"]: cls.manifest["label"] for cls in ADAPTERS.values()
            if cls.manifest["place"] != "mac" and not cls.test_only}
