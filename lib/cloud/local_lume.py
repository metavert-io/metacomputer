"""The adapter for this Mac: computers that are Lume VMs here (`local-lume`).

It describes what this Mac offers, and hands every action to the metacomputer command that already does it
(HandledByCLI): the lifecycle, checkpoints, the network allowlist and the checks stay in bin/metacomputer and
scripts/, unchanged. SSH needs no proxy (each VM's SSH port is on this Mac's loopback), and nothing costs money."""
from .adapter import Adapter, HandledByCLI, check_name

MANIFEST = {
    "name": "local-lume",
    "place": "mac",
    "label": "this Mac",
    "beta": False,
    "root_disk": "persistent",
    # Disk volumes and Mac folders (VOLUMES), attached when the VM starts and kept when it's deleted.
    "volumes": {"attach": "at start", "outlive_computer": True, "size_limit_gb": None, "home": False,
                "mac_folders": True},
    # The stopped VM's disk, copied by the runner: the baseline (reset) and named checkpoints, as many as fit.
    "snapshots": {"keep": None, "what": "the stopped VM's disk: its baseline and named checkpoints"},
    "backup": None,
    # S3 volumes: a folder kept in step with the bucket by rclone on this Mac, not a live mount.
    "bucket_mount": {"how": "a folder kept in step with the bucket (rclone bisync on this Mac)", "home": False},
    "sleep_wake": {"kind": "stop-start", "keeps_processes": False},
    # The gateway on this Mac (mcomp-netgw): this Mac, the LAN, the tailnet and cloud metadata refused; an
    # allowlist with metacomputer net allow.
    "egress": {"model": "mac-gateway", "allowlist": True, "private_ranges_blocked": True},
    # The gateway runs on this Mac, outside the VM: nothing in the VM can change it.
    "outside_egress": {"enforced": True, "private_ranges": True, "metadata": True, "off": True},
    "billing": {"stopped": "none", "guest_poweroff": "n/a", "note": "runs on this Mac: nothing is billed"},
    "input": {"background": True, "note": "the driver's virtual pointer and keyboard (/dev/uinput)"},
    "port_forward": True,
    "sizes": (),      # VM_CPU and VM_MEMORY in the VM's settings
    "regions": (),
    "fresh_start": False,
}


def _vm(name):
    return ["metacomputer", "--vm", check_name(name)]


class LocalLume(Adapter):
    name = "local-lume"
    manifest = MANIFEST

    def _cli(self, what, command):
        raise HandledByCLI(self.name, what, command)

    def create(self, name, region=None, size=None, persist="auto"):
        self._cli("creating a computer", ["metacomputer", "vm", "create", check_name(name)])

    def wake(self, name):
        self._cli("starting a computer", _vm(name) + ["start"])

    def sleep(self, name):
        self._cli("stopping a computer", _vm(name) + ["stop"])

    def delete(self, name):
        self._cli("deleting a computer", ["metacomputer", "vm", "delete", check_name(name)])

    def status(self, name):
        self._cli("a computer's status", _vm(name) + ["status", "--json"])

    def list(self):
        self._cli("listing the computers", ["metacomputer", "vm", "list", "--json"])

    def ssh_endpoint(self, name):
        check_name(name)
        return None                  # the VM's own port on 127.0.0.1 (scripts/lib.sh ssh_base), not an endpoint

    def ssh_proxy_command(self, name):
        check_name(name)
        return None                  # plain SSH to the VM's own port on 127.0.0.1 (scripts/lib.sh ssh_base)

    def port_forward(self, name, port):
        self._cli("reaching the live view", _vm(name) + ["view", "--print"])

    def snapshot(self, name, label):
        self._cli("saving a checkpoint", _vm(name) + ["checkpoint", check_name(label)])

    def snapshots(self, name):
        self._cli("listing checkpoints", _vm(name) + ["checkpoints"])

    def restore(self, name, label):
        self._cli("restoring a checkpoint", _vm(name) + ["restore", check_name(label)])

    def delete_snapshot(self, name, label):
        self._cli("deleting a checkpoint", _vm(name) + ["checkpoint", "--delete", check_name(label)])

    def egress(self, name):
        self._cli("the network allowlist", _vm(name) + ["net", "status"])

    def set_egress(self, name, allow):
        self._cli("changing the network allowlist", _vm(name) + ["net", "allow", *[str(a) for a in allow]])

    def setup(self):
        self._cli("setting this Mac up", ["metacomputer", "setup"])

    def doctor(self):
        self._cli("checking a computer", ["metacomputer", "status"])

    def rates(self):
        return {"awake_per_hour": 0.0, "asleep_per_hour": 0.0, "currency": "USD",
                "note": "runs on this Mac: no provider charges"}
