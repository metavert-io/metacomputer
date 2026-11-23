"""A stand-in provider for tests (`fake`): computers held in memory, never a process, a file or a connection.

It does everything its manifest says it has and refuses the rest (NotSupported), so the contract tests can run the
interface end to end, and with other manifests (Fake(manifest=...)) check that what a provider lacks is refused with
its reason. Its rates are made up. It's test_only: it never counts as a provider (lib/cloud providers())."""
import copy

from . import capabilities
from .adapter import Adapter, AdapterError, NotSupported, check_name

MANIFEST = {
    "name": "fake",
    "place": "fake",
    "label": "the test stand-in",
    "beta": False,
    "root_disk": "ephemeral",
    "volumes": {"attach": "at create", "outlive_computer": False, "size_limit_gb": 10, "home": True,
                "mac_folders": False},
    "snapshots": {"keep": 3, "what": "the root disk"},
    "backup": {"to": "memory", "retention_days": None},
    "bucket_mount": None,
    "sleep_wake": {"kind": "auto-sleep", "keeps_processes": True},
    "egress": {"model": "dns-allowlist", "allowlist": True, "private_ranges_blocked": True},
    "outside_egress": {"enforced": True, "private_ranges": True, "metadata": True, "off": True},
    "billing": {"stopped": "storage", "guest_poweroff": "stops-billing", "note": "made up, for tests"},
    "port_forward": True,
    "sizes": ("2x4", "4x8"),
    "regions": ("here", "there"),
    "fresh_start": True,
}


class Fake(Adapter):
    name = "fake"
    manifest = MANIFEST
    test_only = True

    def __init__(self, manifest=None):
        self.manifest = copy.deepcopy(manifest or MANIFEST)
        self.computers = {}

    def _get(self, name):
        c = self.computers.get(check_name(name))
        if c is None:
            raise AdapterError(f"there's no computer {name} on {self.manifest['label']}")
        return c

    # lifecycle
    def create(self, name, region=None, size=None, persist="auto"):
        check_name(name)
        m = self.manifest
        if name in self.computers:
            raise AdapterError(f"{name} exists already on {m['label']}")
        if region is not None and region not in m["regions"]:
            raise NotSupported(self.name, "region", f"{m['label']} has no region {region!r}: {', '.join(m['regions'])}")
        if size is not None and size not in m["sizes"]:
            raise NotSupported(self.name, "size", f"{m['label']} has no size {size!r}: {', '.join(m['sizes'])}")
        try:
            mode = capabilities.choose_persist(m, persist)
        except capabilities.Unavailable as e:
            raise NotSupported(self.name, "PERSIST", str(e)) from None
        self.computers[name] = {"name": name, "place": m["place"], "state": "awake", "persist": mode,
                                "region": region or (m["regions"][0] if m["regions"] else None),
                                "size": size or (m["sizes"][0] if m["sizes"] else None),
                                "files": {}, "snapshots": [], "allow": []}
        return self.status(name)

    def wake(self, name):
        c = self._get(name)
        if c["state"] != "awake":
            if c["persist"] == "none":
                c["files"] = {}                 # a session computer starts fresh
            c["state"] = "awake"
        return self.status(name)

    def sleep(self, name):
        if self.manifest["sleep_wake"]["kind"] == "none":
            self._no("sleep and wake")
        c = self._get(name)
        c["state"] = "asleep" if self.manifest["sleep_wake"]["kind"] == "auto-sleep" else "stopped"
        return self.status(name)

    def delete(self, name):
        self._get(name)
        del self.computers[name]

    def status(self, name):
        c = self.computers.get(check_name(name))
        if c is None:
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        return {k: c[k] for k in ("name", "place", "state", "persist", "region", "size")}

    def list(self):
        return [self.status(n) for n in sorted(self.computers)]

    # what's on a computer's disk, for the persistence tests
    def put(self, name, path, data):
        self._get(name)["files"][path] = data

    def files(self, name):
        return dict(self._get(name)["files"])

    # transport
    def ssh_endpoint(self, name):
        self._get(name)
        return {"host": "fake-host", "port": 22, "alias": "mcomp-" + name, "proxy": self.ssh_proxy_command(name),
                "transport": "fake"}

    def ssh_proxy_command(self, name):
        self._get(name)
        return ["fake-proxy", "--computer", name]       # never run: the contract only checks its shape

    def port_forward(self, name, port):
        if not self.manifest["port_forward"]:
            self._no("port forwards")
        self._get(name)
        if not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"not a port: {port!r}")
        return {"host": "127.0.0.1", "port": 40000 + port % 20000}

    # snapshots
    def _snaps(self, name):
        if not self.manifest["snapshots"]:
            self._no("snapshots")
        return self._get(name)["snapshots"]

    def snapshot(self, name, label):
        snaps = self._snaps(name)
        check_name(label)
        if any(s["label"] == label for s in snaps):
            raise AdapterError(f"{name} has a snapshot {label} already")
        keep = self.manifest["snapshots"]["keep"]
        if keep is not None and len(snaps) >= keep:
            raise AdapterError(f"{self.manifest['label']} keeps {keep} snapshots; delete one first")
        snaps.append({"label": label, "files": dict(self._get(name)["files"])})
        return {"label": label}

    def snapshots(self, name):
        return [s["label"] for s in self._snaps(name)]

    def restore(self, name, label):
        for s in self._snaps(name):
            if s["label"] == label:
                self._get(name)["files"] = dict(s["files"])
                return {"label": label}
        raise AdapterError(f"{name} has no snapshot {label}")

    def delete_snapshot(self, name, label):
        snaps = self._snaps(name)
        before = len(snaps)
        snaps[:] = [s for s in snaps if s["label"] != label]
        if len(snaps) == before:
            raise AdapterError(f"{name} has no snapshot {label}")

    # egress
    def egress(self, name):
        eg = self.manifest["egress"]
        return {"model": eg["model"], "allow": list(self._get(name)["allow"])}

    def set_egress(self, name, allow):
        if not self.manifest["egress"]["allowlist"]:
            self._no("egress allowlist", f"{self.manifest['label']} has no allowlist: its computers reach the "
                                         "internet or nothing")
        names = [str(a) for a in allow]
        self._get(name)["allow"] = names
        return self.egress(name)

    # connection
    def setup(self):
        return {"ok": True, "note": "nothing to set up: the stand-in keeps its computers in memory"}

    def doctor(self):
        return [{"check": "in memory", "ok": True, "detail": f"{len(self.computers)} computer(s)"}]

    # cost
    def rates(self):
        return {"awake_per_hour": 0.25, "asleep_per_hour": 0.01, "currency": "USD", "note": "made up, for tests"}
