"""Vercel Sandbox (`vercel`, place `vercel`): metacomputer makes, starts, stops, snapshots and deletes persistent
Vercel sandboxes (Firecracker VMs whose disk is snapshotted at every stop); everything that talks to the desktop is the
generic remote computer's (lib/cloud/ubuntu_vm.py RemoteVM): SSH with metacomputer's own keys, the host key pinned as
mcomp-NAME.

A sandbox has no address and takes no raw TCP: SSH reaches it through Vercel's authenticated command API, which this
module registers as the `vercel` transport (lib/cloud/transport.py). ssh runs providers/vercel/tunnel.py as its
ProxyCommand (through lib/proxywrap.py, which first has it ask whether the sandbox is still the computer's own); the
tunnel asks the computer's broker (providers/vercel/broker.py), the one process that holds the token besides
lib/remote.py, which runs the guest's relay (/opt/metacomputer/vercel/relay: 127.0.0.1:22 to its standard input and
output, the output as base64 lines, since the API's output isn't binary-safe) and carries the bytes.

Everything else goes through Vercel's REST API (providers/vercel/api.py), given to the adapter by lib/remote.py: the
adapter alone never makes a network call or starts a program.

What a sandbox is, as metacomputer found it (docs/vercel.md):
- Ubuntu 26.04 on x86_64, 2 GB of memory per vCPU, a 64 GB disk; no systemd, cloud-init or kernel modules (no
  /dev/uinput: the driver's input goes in the foreground). Commands of Vercel's API run as `ubuntu`, which has sudo.
- A stop snapshots the disk and powers the VM off: files and packages are kept, no program is, and the hostname
  changes. The next start boots that snapshot (a few seconds); the boot script (/opt/metacomputer/vercel/boot, run
  through the API at every start) starts sshd, the X server and the desktop again and clears what the stop left.
- Each session ends at its timeout: metacomputer keeps it at IDLE_STOP minutes past the last use (the broker extends it
  while a connection is open), up to the plan's cap (45 minutes on Hobby, 24 hours on Pro).
- A snapshot taken on purpose (a checkpoint) stops the session too. Deleting a sandbox leaves its snapshots unless
  they're deleted with it: metacomputer deletes them. Its snapshots never expire by themselves (snapshotExpiration 0).
- Egress: Vercel's network policy, outside the VM: EGRESS=internet (every name and address but private and link-local
  ranges), off (deny-all), or an allowlist of names (net allow).

Identity: a sandbox is found by its name; one deleted and made again under the name is another sandbox. Its record
here keeps the time Vercel made it (createdAt) and the operation that made it (the tag mcop): every operation checks
both first (_live)."""
import re
import time

from cloud import transport
from cloud.adapter import AdapterError, NotSupported, check_name
from cloud.ubuntu_vm import PRIVATE_V4, RemoteVM, check_egress
from cloud.vps import static_scrub

from .manifest import DEFAULT_SIZE, MANIFEST, REGIONS, SIZES

SANDBOX_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
OP_RE = re.compile(r"[0-9a-f]{32}")
TAG = "metacomputer"                 # every sandbox metacomputer makes carries tag metacomputer=NAME...
OP_TAG = "mcop"                      # ...and mcop=<its build's operation>: what proves a lost launch was that build's
IMAGE = "vcr.vercel.com/vercel/sandbox/ubuntu"     # Vercel's Ubuntu 26.04 image (ubuntu:26.04 with sudo)
# What the policy refuses whatever else it allows: private, shared, link-local (the metadata service), documentation,
# benchmarking, multicast and reserved IPv4 ranges (lib/cloud/ubuntu_vm.py). Vercel's policy takes IPv4 ranges only
# ("Invalid CIDR" for fc00::/7, found live); a sandbox has no IPv6 route out (link-local only).
DENY = tuple(p for p in PRIVATE_V4 if p != "127.0.0.0/8")
DOMAIN = re.compile(r"(?:\*\.)?(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
                    r"\.(?=[a-z0-9-]*[a-z])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_NAMES = 64
CAP_MIN = {"hobby": 45, "pro": 24 * 60, "enterprise": 24 * 60}     # the longest session, by plan (minutes)
SETUP_MIN = 40                       # a new sandbox's first session (the setup), within Hobby's cap
STOP_KEEPS = "files, packages and settings are kept; every program closes (open apps are closed and the hostname changes)"
# Vercel's list prices for Pro (USD, vercel.com/docs/sandbox/pricing, iad1, read 2026-11-25): active CPU time, memory
# provisioned (all of it, used or not) and snapshot storage. Hobby bills nothing within its monthly quota.
CPU_HOUR = 0.128
MEM_GB_HOUR = 0.0212
SNAPSHOT_GB_MONTH = 0.08
DESKTOP_SNAPSHOT_GB = 2.0            # a set-up desktop's snapshot (measured: 1.5 GB after the spike's setup)
DESKTOP_CPU = 0.1                    # CPUs busy on average while it's driven (mostly idle between calls)
# The session's state, in metacomputer's words (lib/cloud/adapter.py STATES).
STATES = {"running": "awake", "pending": "starting", "stopping": "stopping", "snapshotting": "stopping",
          "stopped": "stopped", "failed": "stopped", "aborted": "stopped"}


def sandbox_of(settings):
    n = (settings.get("VERCEL_SANDBOX") or settings.get("VM_NAME") or "").strip()
    if not SANDBOX_NAME.fullmatch(n):
        raise AdapterError(f"VERCEL_SANDBOX {n!r} isn't a sandbox's name (lowercase letters, digits and dashes)")
    return n


def instance_id(sandbox, created_at):
    """The computer's identity at Vercel: its sandbox's name and the time Vercel made it (another sandbox made under the
    name has another). A word the tunnel's check and the vault's binding take ([A-Za-z0-9_-])."""
    if not isinstance(created_at, int) or created_at <= 0:
        raise AdapterError("Vercel didn't say when it made the sandbox")
    return f"{sandbox_of({'VERCEL_SANDBOX': sandbox})}-{created_at}"


def parse_instance(iid):
    """(sandbox, createdAt) of an instance id, or (None, None)."""
    m = re.fullmatch(r"([a-z0-9][a-z0-9-]{0,62})-([0-9]{10,16})", str(iid or ""))
    return (m.group(1), int(m.group(2))) if m else (None, None)


def broker_paths(lab, vm):
    """The computer's broker's socket (in a private folder of this account's: a Unix socket's path is limited to about
    104 bytes) for checkout `lab` and computer `vm`: the same for every program that asks."""
    import hashlib
    import os
    h = hashlib.sha256(f"{os.path.realpath(lab)}:{check_name(vm)}".encode()).hexdigest()[:16]
    d = f"/tmp/mcomp-{os.getuid()}-vercel"
    return d, os.path.join(d, h + ".sock")


def opener(settings, state=None):
    """The `vercel` transport: ssh through providers/vercel/tunnel.py, which asks the computer's broker (the one that
    holds the token) to carry the connection to the sandbox's sshd on its own loopback. ssh's host name is the
    sandbox's (the tunnel carries it; the host key is pinned under mcomp-NAME)."""
    import os
    sandbox = sandbox_of(settings)
    lab = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _d, sock = broker_paths(lab, settings["VM_NAME"])
    tunnel = os.path.join(lab, "providers", "vercel", "tunnel.py")
    return {"host": sandbox, "port": 22, "proxy": [tunnel, "relay", "--sock", sock, "--sandbox", sandbox]}


transport.register("vercel", opener)


# --- the network policy ---------------------------------------------------------------------------------------------
def policy_for(egress=None, allow=None, exact=False):
    """The network policy Vercel is given: EGRESS=internet (every name and address but DENY), off (nothing), or an
    allowlist of names (each with its subdomains unless exact; `*.name` is the subdomains only), DENY refused too.
    ValueError for a name that isn't one."""
    if allow is not None:
        names = []
        for a in allow:
            d = str(a).strip().lower().rstrip(".")
            if not DOMAIN.fullmatch(d):
                raise ValueError(f"{a!r} isn't a DNS name (an address can't be allowed by name: the policy matches the "
                                 "name a connection asks for)")
            names.append(d)
            if not exact and not d.startswith("*."):
                names.append("*." + d)
        names = list(dict.fromkeys(names))
        if not names or len(names) > MAX_NAMES:
            raise ValueError(f"an allowlist has 1 to {MAX_NAMES} names (a name counts twice with its subdomains)")
        return {"allow": names, "subnets": {"deny": list(DENY)}}
    if check_egress(egress) == "internet":
        return {"allow": ["*"], "subnets": {"deny": list(DENY)}}
    return {"mode": "deny-all"}


def policy_kind(p):
    """What a network policy (as set, or as Vercel reads it back) does: ("internet" | "off" | "allowlist" | "open" (no
    rules: everything, private ranges included) | "other", its names). Vercel reads a policy back in its own shape
    (mode custom, allowedDomains, deniedCIDRs) and may leave parts out; a policy that doesn't show the refused ranges
    isn't taken for one that has them."""
    if not isinstance(p, dict):
        return "other", []
    mode = p.get("mode")
    if mode == "deny-all" and set(p) <= {"mode"}:
        return "off", []
    if mode == "allow-all":
        return "open", []
    if mode in (None, "custom"):
        names = p.get("allow") if "allow" in p else p.get("allowedDomains")
        deny = ((p.get("subnets") or {}).get("deny") if "subnets" in p else p.get("deniedCIDRs")) or []
        allow_cidrs = ((p.get("subnets") or {}).get("allow") if "subnets" in p else p.get("allowedCIDRs")) or []
        extra = set(p) - {"mode", "allow", "allowedDomains", "subnets", "deniedCIDRs", "allowedCIDRs"}
        if (extra or allow_cidrs or not isinstance(names, list) or not names
                or not all(isinstance(n, str) for n in names) or not set(DENY) <= set(deny)):
            return "other", []
        if names == ["*"]:
            return "internet", []
        if all(DOMAIN.fullmatch(n) for n in names):
            return "allowlist", list(names)
    return "other", []


def breadth(kind, names):
    """What a policy lets out, for comparing two: None (everything, private ranges included), a frozenset of the names
    it allows ("*" for every name), or False when that can't be told."""
    if kind == "open":
        return None
    if kind == "internet":
        return frozenset({"*"})
    if kind == "off":
        return frozenset()
    if kind == "allowlist":
        return frozenset(names)
    return False


def widens(cur_kind, cur_names, new_policy):
    """Whether `new_policy` lets out anything the policy in force doesn't: judged on the policy a change ends with,
    never on the step. Only a policy that's provably the same or narrower doesn't widen; what's in force but can't be
    told counts as narrowest."""
    new_kind, new_names = policy_kind(new_policy)
    new, cur = breadth(new_kind, new_names), breadth(cur_kind, cur_names)
    if new == frozenset():
        return False
    if cur is None:
        return False
    if cur is False or new is None or new is False:
        return True
    if "*" in cur:
        return False
    return "*" in new or not new <= cur


class InstanceView:
    """What lib/remote.py asks of a provider's facade before it starts, stops or changes the rules of a computer
    (owned: the instance read now by its id must carry the computer's name and operation), and its scrub."""

    def __init__(self, api):
        self._api = api

    def __getattr__(self, name):
        return getattr(self._api, name)

    def instance(self, iid):
        sandbox, created = parse_instance(iid)
        if sandbox is None:
            return None
        cur = self._api.sandbox(sandbox)
        if cur is None:
            return None
        sb = cur["sandbox"]
        tags = sb.get("tags") if isinstance(sb.get("tags"), dict) else {}
        if sb.get("createdAt") != created:
            return {"id": iid, "name": "(another sandbox, made since under its name)", "op": "", "state": "RUNNING"}
        st = str(sb.get("status") or "")
        return {"id": iid, "name": tags.get(TAG), "op": tags.get(OP_TAG),
                "state": "RUNNING" if st == "running" else "STOPPED" if st in ("stopped", "failed", "aborted")
                else st.upper()}


class Vercel(RemoteVM):
    """Sandboxes that metacomputer makes in a Vercel team's project. `api` is Vercel's REST API (providers/vercel/api.py,
    which only lib/remote.py and the broker make; tests: a stand-in); without one, nothing that needs Vercel runs."""
    name = "vercel"
    manifest = MANIFEST
    SOFT_STOP = "api"

    def __init__(self, computers=None, states=None, api=None):
        super().__init__(computers, states)
        self._api = InstanceView(api) if api is not None else None

    @property
    def api(self):
        if self._api is None:
            raise AdapterError("Vercel Sandbox needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this "
                               "command's environment (docs/vercel.md)")
        return self._api

    @classmethod
    def size_of(cls, size):
        s = (size or DEFAULT_SIZE).strip()
        if s not in SIZES:
            raise AdapterError(f"--size {s}: a sandbox's size is {', '.join(SIZES)} (vCPUs x GB; Hobby allows 4x8 at most)")
        return s

    @classmethod
    def region_of(cls, region):
        r = (region or "").strip()
        if r and r not in REGIONS:
            raise AdapterError(f"--region {r}: one of Vercel Sandbox's regions ({', '.join(REGIONS)})")
        return r

    def _sandbox(self, name):
        return sandbox_of(self._settings(name))

    def _state(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            raise AdapterError(f"{name} has no sandbox that this Mac knows of (metacomputer cloud status vercel lists "
                               "the project's)")
        return st

    def _live(self, name):
        """(sandbox, its record at Vercel {"sandbox", "session"}, this Mac's record) of the computer's own sandbox,
        checked before anything acts on it: the name it was made under is still the settings', and the sandbox of that
        name was made when the record says, by the operation it names. AdapterError otherwise: nothing is done."""
        sandbox = self._sandbox(name)
        st = self._state(name)
        rec_sb, created = parse_instance(st["instance"])
        if rec_sb != sandbox:
            raise AdapterError(f"{name}'s settings name the sandbox {sandbox}, but it was made as {rec_sb}: nothing was "
                               "done")
        cur = self.api.sandbox(sandbox)
        if cur is None:
            raise AdapterError(f"{name}'s sandbox ({sandbox}) is gone from Vercel: nothing was done (metacomputer vm "
                               f"delete {name} forgets it)")
        sb = cur["sandbox"]
        tags = sb.get("tags") if isinstance(sb.get("tags"), dict) else {}
        if sb.get("createdAt") != created or tags.get(TAG) != name or (st.get("op") and tags.get(OP_TAG) != st["op"]):
            raise AdapterError(f"the sandbox named {sandbox} isn't the one {name} was made as (made at another time, or "
                               "not by its build): metacomputer leaves it alone, and nothing was done")
        return sandbox, cur, st

    def idle_min(self, name, cap=None):
        """IDLE_STOP in minutes: how long a session runs on past its last use (off: the plan's cap)."""
        v = (self._settings(name).get("IDLE_STOP") or "15").strip().lower()
        if v in ("off", "0"):
            return cap or 45
        if not v.isdigit() or not 1 <= int(v) <= 999:
            raise AdapterError(f"IDLE_STOP={v}: minutes (1-999), or off")
        return min(int(v), cap) if cap else int(v)

    def plan(self):
        """(the team's plan, its session cap in minutes): ("", None) when it can't be read."""
        p = self.api.plan()
        return p, CAP_MIN.get(p)

    def plan_note(self):
        p, cap = self.plan()
        if p == "hobby":
            return ("this Vercel team is on Hobby: every session ends 45 minutes after it starts, in use or not (open "
                    "apps close then; metacomputer start starts a new one). Pro raises that to 24 hours")
        if cap:
            return f"this Vercel team is on {p.capitalize()}: a session ends 24 hours after it starts, at the latest"
        return "the Vercel team's plan couldn't be read: on Hobby every session ends after 45 minutes, on Pro after 24 hours"

    # lifecycle
    def create(self, name, region=None, size=None, persist="auto", op=None):
        """Make the computer's sandbox (refused when one of its name exists: never someone else's), tagged with its
        computer and its build's operation, its network policy EGRESS=internet for the setup's downloads. Returns
        {instance, sandbox, created_at, session, region, size}."""
        from cloud import capabilities
        sandbox = self._sandbox(name)
        if not OP_RE.fullmatch(op or ""):
            raise AdapterError("a sandbox is made by a build's operation (32 hex digits)")
        size, region = self.size_of(size), self.region_of(region)
        try:
            capabilities.choose_persist(self.manifest, persist)
        except capabilities.Unavailable as e:
            raise NotSupported(self.name, "PERSIST", str(e)) from None
        if self.api.sandbox(sandbox) is not None:
            raise AdapterError(f"the Vercel project has a sandbox named {sandbox} already; metacomputer leaves it alone "
                               "(choose another name, or --sandbox NAME)")
        _plan, cap = self.plan()
        timeout = min(SETUP_MIN, cap or 45) * 60000
        d = self.api.create(sandbox, IMAGE, SIZES[size], region, timeout, {TAG: name, OP_TAG: op},
                            policy_for("internet"))
        sb = d["sandbox"]
        return {"instance": instance_id(sandbox, sb.get("createdAt")), "sandbox": sandbox,
                "created_at": sb.get("createdAt"), "session": (d.get("session") or {}).get("id"),
                "region": sb.get("region") or region or None, "size": size, "arch": "x86_64"}

    def owned_lost(self, name, op):
        """The sandbox a launch of operation `op` made though its answer was lost: the sandbox of the computer's name
        tagged with that operation. None when there's none; AdapterError when there's one without it (it can't be told
        whose it is: left alone, said)."""
        sandbox = self._sandbox(name)
        if not OP_RE.fullmatch(op or ""):
            return None
        cur = self.api.sandbox(sandbox)
        if cur is None:
            return None
        tags = cur["sandbox"].get("tags") if isinstance(cur["sandbox"].get("tags"), dict) else {}
        if tags.get(OP_TAG) == op and tags.get(TAG) == name:
            return cur["sandbox"]
        raise AdapterError(f"a sandbox named {sandbox} exists in the Vercel project, but nothing shows this build made it "
                           f"(no tag {OP_TAG}={op[:8]}...): it's left alone. If it's yours and unused, delete it yourself; "
                           "or choose another name")

    def session_of(self, name):
        """The running session's ID, or None when it's stopped (the sandbox checked first: _live)."""
        _sb, cur, _st = self._live(name)
        s = cur.get("session") or {}
        return s.get("id") if s.get("status") == "running" else None

    def wake(self, name, guest_up=None):
        """Start a new session from the latest snapshot (nothing when one runs), then the boot script in it (sshd, the
        X server and the desktop: a stop keeps no program). Returns its status, with notes for the person."""
        sandbox, cur, _st = self._live(name)
        t0 = time.monotonic()
        was = (cur.get("session") or {}).get("status")
        # Each session starts with the sandbox's timeout: IDLE_STOP (the first one, the setup's, was longer). The broker
        # keeps it that far ahead while a connection is open.
        _plan, cap = self.plan()
        want = self.idle_min(name, cap) * 60000
        if cur["sandbox"].get("timeout") != want:
            self.api.update(sandbox, timeout=want)
        if was != "running":
            cur = self.api.sandbox(sandbox, resume=True)
            if cur is None or (cur.get("session") or {}).get("status") not in ("running", "pending"):
                raise AdapterError(f"Vercel didn't start {name}'s sandbox (its session is "
                                   f"{static_scrub(((cur or {}).get('session') or {}).get('status'), 40) or 'unknown'})")
        sid = (cur.get("session") or {}).get("id")
        woke = round(time.monotonic() - t0, 2)
        rc, out, err = self.boot(name, sid)
        if rc != 0:
            why = static_scrub(" ".join((err or out).decode(errors="replace").split())[-200:], 200)
            raise AdapterError(f"{name}'s boot script failed (exit {rc}: {why or 'no reason given'})")
        notes = []
        if was != "running":
            notes.append(f"a new session started: {STOP_KEEPS}")
        return dict(self.status(name), woke_in_s=woke, booted_in_s=round(time.monotonic() - t0 - woke, 2),
                    note="; ".join(notes) or None, session=sid)

    def boot(self, name, sid=None):
        """The boot script, as root, in the computer's running session: (exit code, stdout, stderr)."""
        if sid is None:
            sid = self.session_of(name)
            if sid is None:
                raise AdapterError(f"{name} is stopped (metacomputer --vm {name} start)")
        # Not there yet (a sandbox whose setup never reached its first step): nothing to start.
        return self.api.run(sid, ["sh", "-c", 'test -x /opt/metacomputer/vercel/boot || { echo "boot: not set up yet: '
                                              'nothing to start"; exit 0; }; exec /opt/metacomputer/vercel/boot'],
                            sudo=True, timeout_ms=180000)

    def sleep(self, name, guest_up=None, force=False, asked=False):
        """Stop the session: Vercel snapshots the disk, then powers the VM off (about 15 s). The snapshot it replaces is
        deleted unless a checkpoint names it (gc_snapshots)."""
        sandbox, cur, st = self._live(name)
        s = cur.get("session") or {}
        if s.get("status") in ("stopped", "failed", "aborted"):
            return {"name": name, "place": self.manifest["place"], "state": "stopped", "note": "it was stopped already"}
        t0 = time.monotonic()
        self.api.stop(s["id"])
        self.wait_stopped(name)
        gone = self.gc_snapshots(name)
        return {"name": name, "place": self.manifest["place"], "state": "stopped",
                "stopped_in_s": round(time.monotonic() - t0, 2), "snapshots_deleted": gone,
                "note": f"its disk was snapshotted, then it was powered off: {STOP_KEEPS}"}

    def wait_stopped(self, name, timeout=180):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            cur = self.api.sandbox(self._sandbox(name))
            if cur is None or (cur.get("session") or {}).get("status") in ("stopped", "failed", "aborted") \
                    and cur["sandbox"].get("status") not in ("stopping", "snapshotting"):
                return cur
            time.sleep(1.5)
        raise AdapterError(f"{name}'s session didn't stop in {timeout} s")

    def kept_snapshots(self, name):
        """The snapshots a computer keeps on purpose (its checkpoints and baseline, from its record)."""
        st = self.states.get(name) or {}
        out = set()
        for ids in (st.get("checkpoints") or {}).values():
            out.update(i for i in (ids if isinstance(ids, list) else [ids]) if isinstance(i, str))
        return out

    def gc_snapshots(self, name):
        """Deletes the sandbox's snapshots that nothing needs: not its current one (the next start boots it), not the
        one a running session started from, not a checkpoint. A sandbox makes one at every stop, and they'd add up:
        stopped, it keeps one and its checkpoints."""
        sandbox, cur, _st = self._live(name)
        sb, s = cur["sandbox"], cur.get("session") or {}
        source = s.get("sourceSnapshotId") if s.get("status") == "running" else None
        keep = self.kept_snapshots(name) | {x for x in (sb.get("currentSnapshotId"), source) if x}
        if not sb.get("currentSnapshotId"):
            return []                       # what the next start boots can't be told: nothing is deleted
        gone = []
        for snap in self.api.snapshots(sandbox):
            sid = snap.get("id")
            if snap.get("status") == "created" and isinstance(sid, str) and sid not in keep:
                if self.api.delete_snapshot(sid):
                    gone.append(sid)
        return gone

    def exec(self, name, argv, input=None, timeout=None, max_output=None, sudo=True):
        """A command in the computer's running session through Vercel's API, as root (the setup's first step and the
        provider's own commands), its sandbox checked first. Never starts a stopped one."""
        sid = self.session_of(name)
        if sid is None:
            raise AdapterError(f"{name} is stopped: nothing was run (metacomputer --vm {name} start)")
        return self.api.run(sid, list(argv), sudo=sudo, stdin=input,
                            timeout_ms=int((timeout or 3600) * 1000), max_output=max_output or (1 << 20))

    def delete(self, name):
        """The sandbox, its sessions and every snapshot of it (its checkpoints too): nothing is left at Vercel. Never a
        sandbox of that name this computer didn't make."""
        sandbox = self._sandbox(name)
        st = self._state(name)
        rec_sb, created = parse_instance(st["instance"])
        if rec_sb != sandbox:
            raise AdapterError(f"{name}'s settings name the sandbox {sandbox}, but it was made as {rec_sb}: nothing was "
                               "deleted")
        cur = self.api.sandbox(sandbox)
        # Its snapshots are listed before the sandbox goes: Vercel's listing by name shows none of them afterwards
        # (found live), even one it's still deleting.
        listed = set()
        if cur is not None:
            sb = cur["sandbox"]
            tags = sb.get("tags") if isinstance(sb.get("tags"), dict) else {}
            if sb.get("createdAt") != created or tags.get(TAG) != name:
                raise AdapterError(f"the sandbox named {sandbox} isn't the one this computer made: metacomputer leaves "
                                   "it alone")
            listed = {x.get("id") for x in self.api.snapshots(sandbox) if x.get("status") == "created" and x.get("id")}
            self.api.delete(sandbox)
        return dict(self.delete_snapshots_of(name, sandbox, listed), deleted=cur is not None)

    SNAPSHOT_GONE_S = 60

    def delete_snapshots_of(self, name, sandbox, listed=()):
        """Every snapshot of the sandbox (its checkpoints, recorded here, those listed for it before it went, and any
        listed now): deleted, then each read again by its ID until Vercel says it's deleted (one it was deleting
        already answers "expired or deleted", and is listed as created a little longer: found live).
        {"snapshots_deleted": [...], "snapshots_left": [...]}."""
        from .api import VercelError
        gone = []
        ids = set(self.kept_snapshots(name)) | set(listed)
        ids |= {x.get("id") for x in self.api.snapshots(sandbox) if x.get("status") == "created" and x.get("id")}
        ids = sorted(i for i in ids if isinstance(i, str))
        for sid in ids:
            snap = self.api.get_snapshot(sid)
            if snap is None or snap.get("status") != "created":
                continue
            try:
                if self.api.delete_snapshot(sid):
                    gone.append(sid)
            except VercelError as e:
                if "expired or deleted" not in str(e):
                    raise
                gone.append(sid)
        t0 = time.monotonic()
        while True:
            left = [i for i in ids if (self.api.get_snapshot(i) or {}).get("status") == "created"]
            if not left or time.monotonic() - t0 > self.SNAPSHOT_GONE_S:
                break
            time.sleep(2)
        return {"snapshots_deleted": gone, "snapshots_left": left}

    def status(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        sandbox = self._sandbox(name)
        cur = self.api.sandbox(sandbox)
        _rec, created = parse_instance(st["instance"])
        if cur is None or cur["sandbox"].get("createdAt") != created:
            return {"name": name, "place": self.manifest["place"], "state": "absent",
                    **({"note": "a sandbox of that name is another one"} if cur else {})}
        sb, s = cur["sandbox"], cur.get("session") or {}
        word = str(s.get("status") or sb.get("status") or "")
        out = {"name": name, "place": self.manifest["place"], "state": STATES.get(word, "stopped"),
               "provider_state": "RUNNING" if word == "running" else word.upper(), "persist": "disk",
               "region": sb.get("region"), "size": f"{sb.get('vcpus')}x{(sb.get('memory') or 0) // 1024}"
               if sb.get("vcpus") else None, "sandbox": sandbox, "arch": "x86_64"}
        notes = []
        if word == "running" and s.get("startedAt") and s.get("timeout"):
            ends = (s["startedAt"] + s["timeout"]) / 1000.0
            out["session_ends_in_min"] = max(0, round((ends - time.time()) / 60.0, 1))
            notes.append(f"this session ends in {out['session_ends_in_min']} min unless it's in use")
        exp = sb.get("snapshotExpiration")
        if isinstance(exp, (int, float)) and exp > 0:
            notes.append(f"its snapshots expire {round(exp / 86400000)} days after last use: past that, stopped, it's "
                         "lost (Vercel removes a sandbox that can't resume after 14 days)")
        if word != "running" and sb.get("currentSnapshotId"):
            snap = self.api.get_snapshot(sb["currentSnapshotId"])
            if snap is None or snap.get("status") != "created":
                notes.append("its snapshot is gone: it can't start again, and Vercel removes it after 14 days")
            elif snap.get("expiresAt"):
                notes.append("its snapshot expires " + time.strftime("%Y-%m-%d", time.gmtime(snap["expiresAt"] / 1000)))
        if notes:
            out["note"] = "; ".join(notes)
        return out

    def list(self):
        out = []
        mine = {sandbox_of(s): n for n, s in self.computers.items() if s.get("PROVIDER") == self.name}
        for sb in self.api.sandboxes():
            tags = sb.get("tags") if isinstance(sb.get("tags"), dict) else {}
            if TAG not in tags:
                continue
            n = mine.get(sb.get("name"))
            out.append({"name": n or str(tags.get(TAG)), "sandbox": sb.get("name"), "place": self.manifest["place"],
                        "state": STATES.get(str(sb.get("status")), "stopped"), "here": n is not None,
                        "region": sb.get("region")})
        return out

    # snapshots: a computer's checkpoints are Vercel snapshots its record names (checkpoints: {label: [ids]})
    def snapshot(self, name, label):
        """A snapshot of the whole disk, called `label` in the record (lib/remote.py's hook writes it). It STOPS the
        computer (Vercel's snapshot ends the session): the caller starts it again."""
        check_name(label)
        _sandbox, cur, _st = self._live(name)
        s = cur.get("session") or {}
        if s.get("status") != "running":
            raise AdapterError(f"{name} is stopped: a snapshot is taken of a running computer (metacomputer start)")
        d = self.api.snapshot(s["id"])
        snap = d.get("snapshot") or {}
        if not isinstance(snap.get("id"), str):
            raise AdapterError("Vercel took the snapshot but didn't say its ID")
        self.wait_stopped(name)
        return {"label": label, "version": snap["id"], "created": snap.get("createdAt"),
                "size_gb": round((snap.get("sizeBytes") or 0) / 1e9, 2),
                "note": "taking it stopped the computer: open apps closed"}

    def snapshots(self, name):
        st = self._state(name)
        out = []
        for label, ids in sorted((st.get("checkpoints") or {}).items()):
            for i in ids if isinstance(ids, list) else [ids]:
                out.append({"label": label, "version": i, "created": (st.get("checkpoint_times") or {}).get(i)})
        return out

    def version_of(self, name, label):
        check_name(label)
        found = [s for s in self.snapshots(name) if s["label"] == label]
        if not found:
            raise AdapterError(f"{name} has no checkpoint {label} (metacomputer --vm {name} checkpoints)")
        return found[-1]["version"]

    def restore(self, name, label):
        """The next start boots checkpoint `label`: the computer is stopped first (its programs close), and the snapshot
        that stop makes isn't kept. The caller starts it again."""
        v = self.version_of(name, label)
        snap = self.api.get_snapshot(v)
        if snap is None or snap.get("status") != "created":
            raise AdapterError(f"{name}'s checkpoint {label} is gone at Vercel (deleted or expired): nothing was changed")
        sandbox, cur, _st = self._live(name)
        if (cur.get("session") or {}).get("status") == "running":
            self.api.stop(cur["session"]["id"])
            self.wait_stopped(name)
        self.api.update(sandbox, currentSnapshotId=v)
        return {"label": label, "version": v,
                "note": "the computer was stopped and will start from the checkpoint: open apps closed"}

    def delete_snapshot(self, name, label):
        """Deletes every snapshot called `label`. Never the one a stopped computer's next start boots from (it couldn't
        start again: refused). A running one that started from it saves a new snapshot at its next stop."""
        gone = [s["version"] for s in self.snapshots(name) if s["label"] == check_name(label)]
        if not gone:
            raise AdapterError(f"{name} has no checkpoint {label}")
        _sandbox, cur, _st = self._live(name)
        running = (cur.get("session") or {}).get("status") == "running"
        if cur["sandbox"].get("currentSnapshotId") in gone and not running:
            raise AdapterError(f"checkpoint {label} is what {name}'s next start boots from, and it's stopped: deleting it "
                               f"would leave nothing to start from. Start it first (metacomputer --vm {name} start)")
        for v in gone:
            self.api.delete_snapshot(v)
        return {"label": label, "versions": gone}

    # egress: Vercel's network policy
    def egress(self, name):
        s = self._settings(name)
        out = {"model": "network-policy", "setting": check_egress(s.get("EGRESS")), "allow": []}
        st = self.states.get(name) or {}
        if st.get("instance") and self._api is not None:
            _sandbox, cur, _st = self._live(name)
            kind, names = policy_kind(cur["sandbox"].get("networkPolicy"))
            out.update(outside=kind, allow=names)
        return out

    def set_policy(self, name, policy):
        """The network policy, in force at once on a running session (and kept for the next ones), read back."""
        sandbox, cur, _st = self._live(name)
        s = cur.get("session") or {}
        self.api.set_policy(sandbox, policy, session=s.get("id") if s.get("status") == "running" else None)
        cur = self.api.sandbox(sandbox) or {}
        got = policy_kind((cur.get("sandbox") or {}).get("networkPolicy"))
        if got != policy_kind(policy):
            raise AdapterError(f"Vercel's network policy reads back as {got[0]}, not {policy_kind(policy)[0]}")
        return got

    def apply_egress(self, name, egress):
        kind, _ = self.set_policy(name, policy_for(check_egress(egress)))
        return dict(self.egress(name), outside=kind)

    def set_egress(self, name, allow, exact=False):
        self._settings(name)
        try:
            policy = policy_for(allow=allow, exact=exact)
        except ValueError as e:
            raise AdapterError(str(e)) from None
        self.set_policy(name, policy)
        return self.egress(name)

    # connection
    def setup(self):
        return {"ok": True, "note": "make a Vercel access token for the team (docs/vercel.md: it's team-wide), then run "
                                    "metacomputer with VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in its "
                                    "environment: metacomputer vm create NAME --on vercel"}

    def doctor(self):
        if self._api is None:
            return [{"check": "Vercel's credentials", "ok": False,
                     "detail": "VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID aren't in this command's environment"}]
        p, cap = self.plan()
        out = [{"check": "Vercel's API and the team's plan", "ok": bool(p), "detail": f"{p or 'unknown'}"
                + (f" (sessions end after {cap} min at the latest)" if cap else "")}]
        for n, s in sorted(self.computers.items()):
            if s.get("PROVIDER") != self.name or not (self.states.get(n) or {}).get("instance"):
                continue
            try:
                sandbox, cur, _st = self._live(n)
            except AdapterError as e:
                out.append({"check": f"{n}: its sandbox", "ok": False, "detail": str(e)})
                continue
            out.append({"check": f"{n}: its sandbox {sandbox}", "ok": True,
                        "detail": str((cur.get("session") or {}).get("status") or cur["sandbox"].get("status"))})
            kind, _ = policy_kind(cur["sandbox"].get("networkPolicy"))
            out.append({"check": f"{n}: a network policy metacomputer set is in force",
                        "ok": kind in ("internet", "off", "allowlist"), "detail": kind})
            exp = cur["sandbox"].get("snapshotExpiration")
            out.append({"check": f"{n}: its snapshots don't expire by themselves", "ok": exp in (0, None),
                        "detail": "never" if exp in (0, None) else f"after {round(exp / 86400000)} days unused"})
        return out

    # cost
    def rates(self, size=None):
        vcpus = SIZES[self.size_of(size)]
        mem = 2 * vcpus
        awake = mem * MEM_GB_HOUR + DESKTOP_CPU * CPU_HOUR
        busy = mem * MEM_GB_HOUR + vcpus * CPU_HOUR
        stopped = DESKTOP_SNAPSHOT_GB * SNAPSHOT_GB_MONTH / 730.0
        return {"awake_per_hour": round(awake, 4), "asleep_per_hour": round(stopped, 6), "currency": "USD",
                "note": (f"{vcpus} vCPUs and {mem} GB at Vercel's Pro list prices (memory ${MEM_GB_HOUR}/GB-hour, all "
                         f"of it while it runs; CPU ${CPU_HOUR}/CPU-hour, only what's used); all CPUs busy: about "
                         f"${busy:.2f}/h. Stopped: its snapshot only (about {DESKTOP_SNAPSHOT_GB:.0f} GB at "
                         f"${SNAPSHOT_GB_MONTH}/GB-month). Hobby bills nothing within its monthly quota, then stops "
                         "making sandboxes until the next month")}


ADAPTER = Vercel
