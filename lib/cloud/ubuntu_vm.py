"""The generic adapter for an Ubuntu 24.04 VM reached over SSH: what every such computer has in common, and the
bring-your-own-server case (`ubuntu-vm`, place `server`), which needs no provider code at all.

A computer like this is a full VM (systemd, its own kernel, a disk that keeps everything) with an address SSH
reaches. metacomputer sets it up the same way wherever it is (lib/remote.py: bootstrap.sh, then provision.sh over
SSH): XFCE on Xorg in memory, Cua Driver's Linux build for its architecture (arm64 or x86_64, each pinned by its
SHA-256), the desktop user `cua` without sudo, and the computer's own firewall (guest-firewall): SSH in, key only;
out, the internet with the cloud's metadata services, link-local and private ranges refused (EGRESS=internet), or
nothing (EGRESS=off). The live view is x11vnc on the computer's loopback, reached through an SSH tunnel. The Mac
stays the controller: the hub, the vault, recordings and every guard run here.

Providers that make the VM (OCI: lib/cloud/oci.py) add a thin driver for create, start, stop and delete on top of
RemoteVM; your own server has none: you make it, keep it on, and delete it."""
import ipaddress

from .adapter import Adapter, AdapterError, HandledByCLI, NotSupported, check_name
from . import transport

EGRESS = ("internet", "off")
# What a computer elsewhere never reaches (the guest firewall's private4 set, guest-firewall; a provider's outside
# rules, where it has them): this host, private, shared (CGNAT, tailnets), link-local (cloud metadata), documentation,
# benchmarking, multicast and reserved ranges.
PRIVATE_V4 = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
              "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
              "224.0.0.0/3")


def public_v4():
    """IPv4 without PRIVATE_V4, as the fewest CIDR blocks: what a provider's allow-only egress rules list."""
    left = [ipaddress.ip_network("0.0.0.0/0")]
    for p in PRIVATE_V4:
        net = ipaddress.ip_network(p)
        nxt = []
        for block in left:
            if net.subnet_of(block):
                nxt.extend(block.address_exclude(net))
            elif not block.subnet_of(net):
                nxt.append(block)
        left = nxt
    return tuple(str(n) for n in ipaddress.collapse_addresses(left))
ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}

# What every Ubuntu VM reached over SSH has; each adapter adds its name, place, label, sleep and sizes.
BASE = {
    "beta": False,
    "root_disk": "persistent",
    "volumes": None,             # not yet: the Mac's volumes and folders reach a VM here through Lume only
    "snapshots": None,
    "backup": None,
    "bucket_mount": None,
    # Its own firewall (guest-firewall): the internet, with metadata, link-local and private ranges refused; no
    # allowlist of names (EGRESS=internet or off). That firewall is in the guest, so root in the guest can undo it:
    # what a provider enforces outside the computer is `outside_egress`, each adapter's own.
    "egress": {"model": "guest-firewall", "allowlist": False, "private_ranges_blocked": True},
    "port_forward": True,        # the live view, through an SSH tunnel to this Mac's loopback
    "regions": (),
    "fresh_start": False,
}

MANIFEST = dict(BASE, name="ubuntu-vm", place="server", label="your own server",
                sleep_wake={"kind": "none", "keeps_processes": False}, sizes=(),
                # metacomputer sets nothing up outside your server: only its own firewall holds, and root on the
                # server can undo it. Your host's firewall is yours to add.
                outside_egress={"enforced": False, "private_ranges": False, "metadata": False, "off": False},
                billing={"stopped": "unknown", "guest_poweroff": "n/a",
                         "note": "your own server: metacomputer never stops it; what it costs is your host's terms"})


def check_egress(v):
    v = (v or "internet").strip().lower()
    if v not in EGRESS:
        raise AdapterError(f"EGRESS={v} isn't a setting: internet (the default: the internet, with metadata, "
                           "link-local and private ranges refused) or off (nothing out, package updates included)")
    return v


def check_arch(v):
    a = ARCHES.get((v or "").strip().lower())
    if a is None:
        raise AdapterError(f"{v!r} isn't an architecture metacomputer sets up (arm64 or x86_64)")
    return a


class RemoteVM(Adapter):
    """An Ubuntu 24.04 VM reached over SSH. `computers` is {name: its settings (config/vms/NAME.env, as a dict)};
    `states` is {name: what its provider made (RUN_DIR/remote.json)}: both come from lib/remote.py, which reads
    them; an adapter made with neither knows no computer."""
    manifest = {}

    def __init__(self, computers=None, states=None):
        self.computers = dict(computers or {})
        self.states = dict(states or {})

    def _settings(self, name):
        check_name(name)
        s = self.computers.get(name)
        if s is None or s.get("PROVIDER") != self.name:
            raise AdapterError(f"there's no computer {name} on {self.manifest['label']} here (metacomputer list)")
        return s

    def _vm(self, name):
        return ["metacomputer", "--vm", check_name(name)]

    # transport
    def ssh_endpoint(self, name):
        return transport.endpoint(self._settings(name), self.states.get(name))

    def ssh_proxy_command(self, name):
        return self.ssh_endpoint(name)["proxy"]

    def port_forward(self, name, port):
        check_name(name)
        if port != 5900:
            raise NotSupported(self.name, "port forward", f"only the live view (port 5900) is forwarded from "
                                                         f"{self.manifest['label']}, through SSH")
        raise HandledByCLI(self.name, "reaching the live view", self._vm(name) + ["view", "--print"])

    # egress
    def egress(self, name):
        s = self._settings(name)
        return {"model": "guest-firewall", "setting": check_egress(s.get("EGRESS")), "allow": []}

    def set_egress(self, name, allow):
        raise NotSupported(self.name, "egress allowlist", f"{self.manifest['label']} has no allowlist of names: its "
                           "own firewall lets the internet through with metadata and private ranges refused "
                           "(EGRESS=internet), or nothing (EGRESS=off)")

    def doctor(self):
        return [{"check": "settings", "ok": True, "detail": f"{len(self.computers)} computer(s) here"}]


class UbuntuVM(RemoteVM):
    """Your own server: an Ubuntu 24.04 host you can SSH into, with sudo. metacomputer sets it up over your own
    connection once (bootstrap.sh, then the same setup as every VM here), and from then on signs in only with the
    keys it made for it. It never powers it on or off, and deleting it here leaves the server alone."""
    name = "ubuntu-vm"
    manifest = MANIFEST

    def create(self, name, region=None, size=None, persist="auto"):
        check_name(name)
        raise HandledByCLI(self.name, "setting up your own server",
                           ["metacomputer", "vm", "create", name, "--on", "server", "--host", "ADDRESS"])

    def wake(self, name):
        check_name(name)
        raise NotSupported(self.name, "way to start a computer", "your own server is on when you keep it on: "
                           "metacomputer doesn't power it on or off")

    def sleep(self, name):
        check_name(name)
        raise NotSupported(self.name, "way to stop a computer", "your own server is on when you keep it on: "
                           "metacomputer doesn't power it on or off")

    def delete(self, name):
        raise HandledByCLI(self.name, "forgetting your own server (the server itself is left as it is)",
                           ["metacomputer", "vm", "delete", check_name(name)])

    def status(self, name):
        raise HandledByCLI(self.name, "a computer's status", self._vm(name) + ["status", "--json"])

    def list(self):
        return [{"name": n, "place": self.manifest["place"], "state": "unknown"}
                for n, s in sorted(self.computers.items()) if s.get("PROVIDER") == self.name]

    def setup(self):
        return {"ok": True, "note": "nothing to connect: point metacomputer at the server with "
                                    "metacomputer vm create NAME --on server --host ADDRESS --user YOUR_ACCOUNT"}

    def rates(self, size=None):
        return {"awake_per_hour": 0.0, "asleep_per_hour": 0.0, "currency": "USD",
                "note": "your own server: what it costs is between you and its host"}
