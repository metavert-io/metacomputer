"""metacomputer's provider adapters: one per place its computers can be (lib/placement.py).

    adapter.py        the interface (lifecycle, transport, snapshots, egress, connection, cost) and its refusals
    capabilities.py   the capability manifest, PERSIST and the feature map
    local_lume.py     this Mac (`local-lume`): what it offers; its actions are the existing metacomputer commands
    fake.py           an in-memory stand-in for the tests (`fake`), never a provider

Every adapter registered here passes the same contract tests (tests/test_cloud_adapters.py). providers() is the
places that make up `cloud`: the registered adapters that are neither this Mac's nor a test stand-in (none here)."""
from . import capabilities
from .adapter import INTERFACE, Adapter, AdapterError, HandledByCLI, NotSupported
from .fake import Fake
from .local_lume import LocalLume

ADAPTERS = {LocalLume.name: LocalLume, Fake.name: Fake}

__all__ = ["ADAPTERS", "INTERFACE", "Adapter", "AdapterError", "HandledByCLI", "NotSupported", "adapter",
           "capabilities", "providers", "registered"]


def registered():
    """The registered adapters' names."""
    return sorted(ADAPTERS)


def adapter(name):
    """A new instance of the adapter `name`; KeyError for one that isn't registered."""
    return ADAPTERS[name]()


def providers():
    """{place: label} for the providers' places (the group `cloud`): adapters that aren't this Mac's or a stand-in."""
    return {cls.manifest["place"]: cls.manifest["label"] for cls in ADAPTERS.values()
            if cls.manifest["place"] != "mac" and not cls.test_only}
