"""The interface every provider adapter implements: what a computer's provider can do, in six groups.

    lifecycle   create, wake, sleep, delete, status, list
    transport   ssh_endpoint (where SSH reaches the computer: None for this Mac's own VMs, a port on its loopback
                (scripts/lib.sh ssh_base); else lib/cloud/transport.py's endpoint), ssh_proxy_command (the program that
                carries the connection; None: ssh connects straight to the endpoint), port_forward (one of the
                computer's ports, on this Mac's loopback)
    snapshots   snapshot, snapshots, restore, delete_snapshot
    egress      egress (what the computer may reach), set_egress
    connection  setup, doctor
    cost        rates, estimate

A method for something the provider doesn't have raises NotSupported, with a plain reason. A method that this Mac's
existing commands already carry out raises HandledByCLI naming the command (the local adapter: metacomputer's own
lifecycle code is what runs, unchanged). Each adapter has a capability manifest (lib/cloud/capabilities.py) saying
what it has, which is checked before anything starts, so a request it can't meet gets its reason up front.
Nothing here makes a network call."""
import re

NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
STATES = ("absent", "awake", "asleep", "stopped")


class AdapterError(Exception):
    """Something an adapter refuses; str() is the reason, for the person."""


class NotSupported(AdapterError):
    """The provider doesn't have this (its manifest says so)."""

    def __init__(self, adapter, what, reason=None):
        self.adapter, self.what = adapter, what
        super().__init__(reason or f"{adapter} has no {what}")


class HandledByCLI(AdapterError):
    """This Mac's existing commands do this: `command` (an argument list for bin/metacomputer)."""

    def __init__(self, adapter, what, command):
        self.adapter, self.what, self.command = adapter, what, list(command)
        super().__init__(f"{what} on {adapter} is done by the metacomputer command: {' '.join(self.command)}")


def check_name(name):
    """A computer's name: what --vm takes (scripts/lib.sh mc_valid_vm_name). ValueError otherwise."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"{name!r} isn't a computer's name (letters, digits, '.', '-' and '_')")
    return name


class Adapter:
    """The base: every method refuses with NotSupported until an adapter provides it. `name` is the adapter's
    registered name, `manifest` its capabilities (capabilities.check_manifest), `test_only` marks a stand-in that
    never counts as a provider."""

    name = "adapter"
    manifest = {}
    test_only = False
    # Whether making a computer here, or the provider's setup, rewrites something its other computers use too (a shared
    # network's rules): lib/remote.py then holds the handoff check of every computer of this provider's, not only the
    # one being made (lib/handoff_gate.py wait_or_hold_all).
    SHARED_NETWORK_CHANGES = False

    def _no(self, what, reason=None):
        raise NotSupported(self.name, what, reason)

    # lifecycle
    def create(self, name, region=None, size=None, persist="auto"):
        self._no("way to create a computer")

    def wake(self, name):
        self._no("way to wake a computer")

    def sleep(self, name):
        self._no("way to put a computer to sleep")

    def delete(self, name):
        self._no("way to delete a computer")

    def status(self, name):
        self._no("status")

    def list(self):
        self._no("list of computers")

    # transport
    def ssh_endpoint(self, name):
        self._no("SSH transport")

    def ssh_proxy_command(self, name):
        self._no("SSH transport")

    def port_forward(self, name, port):
        self._no("port forwards")

    # snapshots
    def snapshot(self, name, label):
        self._no("snapshots")

    def snapshots(self, name):
        self._no("snapshots")

    def restore(self, name, label):
        self._no("snapshots")

    def delete_snapshot(self, name, label):
        self._no("snapshots")

    # egress
    def egress(self, name):
        self._no("egress control")

    def set_egress(self, name, allow):
        self._no("egress control")

    # connection
    def setup(self):
        self._no("setup")

    def doctor(self):
        self._no("checks")

    # cost
    def rates(self):
        self._no("rates")

    def estimate(self, hours_awake, hours_asleep=0.0):
        """What that much use costs at rates(), in its currency."""
        r = self.rates()
        if hours_awake < 0 or hours_asleep < 0:
            raise ValueError("hours can't be negative")
        return round(r["awake_per_hour"] * hours_awake + r["asleep_per_hour"] * hours_asleep, 4)


# Every method of the interface, by group (the contract test checks each adapter answers every one).
INTERFACE = {
    "lifecycle": ("create", "wake", "sleep", "delete", "status", "list"),
    "transport": ("ssh_endpoint", "ssh_proxy_command", "port_forward"),
    "snapshots": ("snapshot", "snapshots", "restore", "delete_snapshot"),
    "egress": ("egress", "set_egress"),
    "connection": ("setup", "doctor"),
    "cost": ("rates", "estimate"),
}
