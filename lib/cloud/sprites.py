"""Fly.io Sprites (`sprites`, place `sprites`): metacomputer makes, wakes, checkpoints and deletes Sprites, persistent
Firecracker VMs that sleep by themselves when nothing is connected; everything that talks to the desktop is the generic
remote computer's (lib/cloud/ubuntu_vm.py RemoteVM): SSH with metacomputer's own keys, the host key pinned as
mcomp-NAME.

A Sprite has no address of its own: SSH reaches it through Fly's authenticated tunnel, `sprite proxy -W :22`, which
this module registers as the `sprite` transport (lib/cloud/transport.py). ssh runs it as its ProxyCommand through
lib/proxywrap.py, which ends it when ssh goes: an open tunnel keeps a Sprite awake, and billing.

Everything else goes through Fly's `sprite` command (its own sign-in, kept in the login keychain by the CLI itself:
metacomputer never sees the token): `sprite api` for the Sprites API (create, read, delete, the network policy,
checkpoints) and `sprite exec` for the setup's first step. Each computer's settings name its organization
(SPRITE_ORG) and its Sprite (SPRITE_NAME, by default the computer's name); SPRITE_CLI names the command (by default
`sprite` on PATH, else ~/.local/bin/sprite).

What a Sprite is, as metacomputer found it (docs/sprites.md):
- Ubuntu 26.04 on x86_64, 8 CPUs, memory managed by Fly, a 100 GB disk that keeps everything; no systemd, cloud-init
  or kernel modules (no /dev/uinput: the driver's input goes in the foreground). The desktop runs as Sprites services.
- Sleep: about 10 s after its last connection closes it pauses, with its programs; the next connection wakes it (a
  fraction of a second, a second or two after a long sleep). Asleep, only its disk is billed.
- Checkpoints of the whole disk, copy-on-write, taken in a fraction of a second; a restore restarts the Sprite's
  programs (the desktop comes back as a service; open apps don't) and cuts its connections.
- Egress: Fly's network policy, a list of DNS names, enforced outside the VM; while it's enforced, raw IP addresses,
  private ranges and the metadata service are refused. metacomputer always sets one (an empty list enforces nothing):
  EGRESS=internet allows every name, EGRESS=off denies every name, `net allow` lists names.

Nothing here makes a network call or starts a program: the adapter is given the CLI's facade (lib/spritecli.py, which
only lib/remote.py makes; tests: a stand-in)."""
import os
import re
import shutil
import time

from . import transport
from .adapter import AdapterError, NotSupported, check_name
from .ubuntu_vm import BASE, RemoteVM, check_egress

SPRITE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ORG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
# A name in the network policy: a DNS name, or *.name for its subdomains (never an address: Fly takes anything; a name
# whose last label is all digits is an IPv4 address written as a name)
DOMAIN = re.compile(r"(?:\*\.)?(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
                    r"\.(?=[a-z0-9-]*[a-z])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_NAMES = 64
CHECKPOINT_PREFIX = "metacomputer:"          # a checkpoint's comment: metacomputer:LABEL
LABEL = "metacomputer"                       # every Sprite metacomputer makes carries it...
OP_LABEL = "mcop-"                           # ...and its build's operation (mcop-<32 hex>), set atomically at creation:
                                             # the only proof that a Sprite whose answer was lost is that build's
OP_RE = re.compile(r"[0-9a-f]{32}")

# Fly's list prices for Sprites (USD, fly.io/pricing, read 2026-11-22): CPU time used, memory in use, the disk while
# awake (hot) and while asleep (cold). A Sprite has 8 CPUs and 8 GB here (its resources policy; Fly manages it).
CPU_HOUR = 0.0385
MEM_GB_HOUR = 0.021875
HOT_GB_HOUR = 0.000683
COLD_GB_HOUR = 0.000027
DESKTOP_DISK_GB = 5          # what a set-up desktop writes (the packages, the driver, the profile)
DESKTOP_MEM_GB = 1.5         # an idle desktop with Firefox open, as measured
DESKTOP_CPU = 0.1            # CPUs busy on average while it's driven (mostly idle between calls)

MANIFEST = dict(
    BASE, name="sprites", place="sprites", label="Fly Sprites",
    root_disk="persistent",
    # Checkpoints of the whole disk (copy-on-write): baseline, reset and named checkpoints. A restore restarts the
    # Sprite's programs (open apps close; the desktop comes back) and cuts its connections.
    snapshots={"keep": None, "what": "checkpoints of the whole disk, copy-on-write; a restore restarts its programs"},
    # Asleep about 10 s after its last connection closes; awake again at the next one, its programs as they were.
    sleep_wake={"kind": "auto-sleep", "keeps_processes": True},
    # Fly's network policy (DNS names), enforced outside the VM: an allowlist with `net allow`.
    egress={"model": "dns-allowlist", "allowlist": True, "private_ranges_blocked": True},
    # Enforced outside the VM (nothing in it, root included, can change it). While a policy is in force Fly refuses
    # raw IP addresses (an address is let through only after an allowed name resolved to it), private ranges and the
    # metadata service, even through a name that resolves to one (checked live: 10/8, 172.16/12, 192.168/16,
    # 100.64/10, 169.254.169.254, Fly's private network); EGRESS=off denies every name.
    outside_egress={"enforced": True, "private_ranges": True, "metadata": True, "off": True},
    # Asleep, a Sprite bills only its disk (cold storage); awake, the CPU time it uses, its memory and its disk.
    billing={"stopped": "storage", "guest_poweroff": "n/a",
             "note": "asleep (about 10 s after nothing is connected): only its disk, at Fly's storage rate; awake: the "
                     "CPU time it uses, the memory in use and its disk"},
    # The Sprite's kernel has no uinput, so the driver has no virtual devices: keys, typing and pixel clicks go in
    # the foreground (XTEST); clicks and actions on an element go through accessibility, as anywhere.
    input={"background": False, "note": "no /dev/uinput on a Sprite: keys, typing and pixel clicks go in the "
                                        "foreground; actions on an element through accessibility"},
    sizes=(), regions=(), fresh_start=False)


def default_cli():
    """The `sprite` command: on PATH, else where Fly's installer puts it."""
    found = shutil.which("sprite")
    if found:
        return os.path.realpath(found)
    home = os.path.expanduser("~/.local/bin/sprite")
    return os.path.realpath(home) if os.path.exists(home) else ""


def cli_path(settings):
    """SPRITE_CLI as an absolute path to a program, or the default; AdapterError when there's none."""
    p = (settings.get("SPRITE_CLI") or "").strip() or default_cli()
    if not p:
        raise AdapterError("Fly's sprite command isn't installed (docs/sprites.md: install it, then sprite login)")
    p = os.path.expanduser(p)
    if not os.path.isabs(p) or not re.fullmatch(r"[A-Za-z0-9._/+@_-]{1,400}", p):
        raise AdapterError(f"SPRITE_CLI {p!r} isn't an absolute path metacomputer runs")
    return p


def org_of(settings):
    o = (settings.get("SPRITE_ORG") or "").strip()
    if not ORG_NAME.fullmatch(o):
        raise AdapterError(f"SPRITE_ORG {o!r} isn't a Fly organization's name (vm create --on sprites --org ORG)")
    return o


def sprite_of(settings):
    n = (settings.get("SPRITE_NAME") or settings.get("VM_NAME") or "").strip()
    if not SPRITE_NAME.fullmatch(n):
        raise AdapterError(f"SPRITE_NAME {n!r} isn't a Sprite's name (lowercase letters, digits and dashes)")
    return n


def opener(settings, state=None):
    """The `sprite` transport: ssh through `sprite proxy -W :22` (Fly's authenticated tunnel to the Sprite's port 22,
    where its sshd listens on the Sprite's own loopback). ssh's host name is the Sprite's (the tunnel carries it; the
    host key is pinned under mcomp-NAME)."""
    sprite = sprite_of(settings)
    return {"host": sprite, "port": 22,
            "proxy": [cli_path(settings), "-o", org_of(settings), "-s", sprite, "proxy", "-W", ":22"]}


transport.register("sprite", opener)


def rules_for(egress=None, allow=None, exact=False):
    """The network policy's rules: EGRESS=internet (every name), off (no name), or an allowlist of names (each with
    its subdomains unless exact; `*.name` is the subdomains only). ValueError for a name that isn't one."""
    if allow is not None:
        names = []
        for a in allow:
            d = str(a).strip().lower().rstrip(".")
            if not DOMAIN.fullmatch(d):
                raise ValueError(f"{a!r} isn't a DNS name (an address can't be allowed: Fly's policy lists names)")
            names.append(d)
            if not exact and not d.startswith("*."):
                names.append("*." + d)
        names = list(dict.fromkeys(names))
        if not names or len(names) > MAX_NAMES:
            raise ValueError(f"an allowlist has 1 to {MAX_NAMES} names (a name counts twice with its subdomains)")
        return [{"domain": d, "action": "allow"} for d in names]
    if check_egress(egress) == "internet":
        return [{"domain": "*", "action": "allow"}]
    return [{"domain": "*", "action": "deny"}]


def breadth(kind, names):
    """What a policy lets out, for comparing two: None (everything: no rules enforce nothing), a frozenset of the
    names it allows ("*" for every name), or False when that can't be told (rules metacomputer didn't set)."""
    if kind == "none":
        return None
    if kind == "internet":
        return frozenset({"*"})
    if kind == "off":
        return frozenset()
    if kind == "allowlist":
        return frozenset(names)
    return False


def widens(cur_kind, cur_names, new_rules):
    """Whether the policy `new_rules` lets out anything the policy in force doesn't: judged on the policy a change ends
    with, never on the step, so no split into steps gets around it. Only a policy that's provably the same or narrower
    doesn't widen: nothing (off) never does; every name doesn't unless every name (or nothing enforced) is in force;
    an allowlist doesn't when each of its names is already allowed, by name or by every name. What's in force but
    can't be told counts as narrowest."""
    new_kind, new_names = policy_kind(new_rules)
    new = breadth(new_kind, new_names)
    cur = breadth(cur_kind, cur_names)
    if new == frozenset():
        return False
    if cur is None:
        return False                     # nothing is enforced now: any policy narrows it
    if cur is False or new is None or new is False:
        return True
    if "*" in cur:
        return False
    return "*" in new or not new <= cur


def policy_kind(rules):
    """What a network policy read back does: "internet", "off", "allowlist" (its names), "none" (no rules: Fly
    enforces nothing then, raw IPs and private ranges included) or "other" (not what metacomputer sets)."""
    if not isinstance(rules, list):
        return "other", []
    if not rules:
        return "none", []
    if rules == [{"domain": "*", "action": "allow"}]:
        return "internet", []
    if rules == [{"domain": "*", "action": "deny"}]:
        return "off", []
    if all(isinstance(r, dict) and r.get("action") == "allow" and isinstance(r.get("domain"), str)
           and DOMAIN.fullmatch(r["domain"]) and set(r) == {"domain", "action"} for r in rules):
        return "allowlist", [r["domain"] for r in rules]
    return "other", []


class Sprites(RemoteVM):
    """Sprites that metacomputer makes in a Fly organization. `cli` is the facade of Fly's command (lib/spritecli.py
    SpriteCLI; default: none, so the adapter alone never runs a program; lib/remote.py gives it one)."""
    name = "sprites"
    manifest = MANIFEST

    def __init__(self, computers=None, states=None, cli=None):
        super().__init__(computers, states)
        self._cli = cli

    @property
    def cli(self):
        if self._cli is None:
            raise AdapterError("Fly Sprites needs its sprite command (SPRITE_CLI; docs/sprites.md)")
        return self._cli

    def _where(self, name):
        s = self._settings(name)
        return org_of(s), sprite_of(s)

    def _state(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            raise AdapterError(f"{name} has no Sprite that this Mac knows of (metacomputer cloud status sprites "
                               "lists the organization's Sprites)")
        return st

    def _live(self, name):
        """(org, sprite, record) of the computer's own Sprite, checked against Fly's API before anything acts on it:
        the organization and name it was made under are the settings' still, and the Sprite of that name has the id
        recorded when it was made. A Sprite deleted and made again under the name (by anyone) is another one, and
        nothing is done to it. AdapterError otherwise."""
        org, sprite = self._where(name)
        st = self._state(name)
        if (st.get("org") and st["org"] != org) or (st.get("sprite") and st["sprite"] != sprite):
            raise AdapterError(f"{name}'s settings name the Sprite {sprite} in {org}, but it was made as {st.get('sprite')} "
                               f"in {st.get('org')}: nothing was done")
        cur = self.cli.info(org, sprite)
        if cur is None:
            raise AdapterError(f"{name}'s Sprite ({sprite} in {org}) is gone from Fly: nothing was done "
                               f"(metacomputer vm delete {name} forgets it)")
        if cur.get("id") != st["instance"] or (cur.get("organization") not in (None, org)):
            raise AdapterError(f"the Sprite named {sprite} in {org} isn't the one {name} was made as (another id: deleted "
                               "and made again since?): metacomputer leaves it alone, and nothing was done")
        return org, sprite, st

    # lifecycle
    def create(self, name, region=None, size=None, persist="auto", op=None):
        """Make the computer's Sprite (refused when one of its name exists: never someone else's), labelled with its
        build's operation (`op`, set at creation: what proves a lost launch is that build's). Returns {instance,
        sprite, org, created_at}."""
        org, sprite = self._where(name)
        if not OP_RE.fullmatch(op or ""):
            raise AdapterError("a Sprite is made by a build's operation (32 hex digits)")
        from . import capabilities
        if region is not None or size is not None:
            raise NotSupported(self.name, "region or size", "Fly picks a Sprite's region and size (8 CPUs, memory as "
                                                             "needed)")
        try:
            capabilities.choose_persist(self.manifest, persist)
        except capabilities.Unavailable as e:
            raise NotSupported(self.name, "PERSIST", str(e)) from None
        if self.cli.info(org, sprite) is not None:
            raise AdapterError(f"the organization {org} has a Sprite named {sprite} already; metacomputer leaves it "
                               "alone (choose another name, or --sprite NAME)")
        d = self.cli.create(org, sprite, labels=[LABEL, OP_LABEL + op])
        sid = d.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", sid):
            got = self.cli.info(org, sprite) or {}
            sid = got.get("id")
        if not isinstance(sid, str) or not sid:
            raise AdapterError(f"Fly made the Sprite {sprite} but didn't say its id")
        return {"instance": sid, "sprite": sprite, "org": org, "created_at": d.get("created_at")}

    def owned_lost(self, name, op):
        """The Sprite a launch of operation `op` made though its answer was lost: the Sprite of the computer's name
        that carries that operation's label (set atomically when it was made). None when there's none; AdapterError
        when there's one without it (it can't be told whose it is: left alone, said)."""
        org, sprite = self._where(name)
        if not OP_RE.fullmatch(op or ""):
            return None
        cur = self.cli.info(org, sprite)
        if cur is None:
            return None
        labels = cur.get("labels") if isinstance(cur.get("labels"), list) else []
        if OP_LABEL + op in labels and cur.get("organization") in (None, org):
            return cur
        raise AdapterError(f"a Sprite named {sprite} exists in {org}, but nothing shows this build made it (no label "
                           f"{OP_LABEL}{op[:8]}...): it's left alone. If it's yours and unused, delete it yourself "
                           f"(sprite destroy {sprite}); or choose another name")

    def set_policy(self, name, rules):
        org, sprite, _ = self._live(name)
        self.cli.set_policy(org, sprite, rules)
        got = self.cli.policy(org, sprite)
        if got != rules:
            raise AdapterError("Fly's network policy reads back as something else than was set")
        return policy_kind(got)

    def wake(self, name):
        """A connection wakes a Sprite: one command run in it (`true`) is the check that it's there and answers."""
        org, sprite, _ = self._live(name)
        t0 = time.monotonic()
        rc, _o, err = self.cli.exec(org, sprite, ["true"], timeout=120)
        if rc != 0:
            why = " ".join(err.decode(errors="replace").split())[-160:]
            raise AdapterError(f"{name}'s Sprite didn't answer ({why or 'no reason given'})")
        return dict(self.status(name), woke_in_s=round(time.monotonic() - t0, 2))

    def sleep(self, name):
        """Nothing to send: a Sprite pauses by itself about 10 s after its last connection closes (the CLI closes this
        Mac's: the hub, the live view's tunnel, a recording)."""
        self._state(name)
        return {"name": name, "place": self.manifest["place"], "state": "asleep",
                "note": "it pauses about 10 s after its last connection closes"}

    def exec(self, name, argv, input=None, timeout=None, max_output=None):
        """A command in the computer's own Sprite through Fly's channel (`sprite exec`), its id checked first."""
        org, sprite, _ = self._live(name)
        return self.cli.exec(org, sprite, argv, input=input, timeout=timeout, max_output=max_output)

    def delete(self, name):
        org, sprite = self._where(name)
        st = self._state(name)
        if (st.get("org") and st["org"] != org) or (st.get("sprite") and st["sprite"] != sprite):
            raise AdapterError(f"{name}'s settings name the Sprite {sprite} in {org}, but it was made as {st.get('sprite')} "
                               f"in {st.get('org')}: nothing was deleted")
        cur = self.cli.info(org, sprite)
        if cur is not None and cur.get("id") != st["instance"]:
            raise AdapterError(f"the Sprite named {sprite} isn't the one this computer made (another id): metacomputer "
                               "leaves it alone")
        if cur is not None:
            self.cli.destroy(org, sprite)
        return {"deleted": cur is not None}

    def status(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        org, sprite = self._where(name)
        cur = self.cli.info(org, sprite)
        if cur is None or cur.get("id") != st["instance"]:
            return {"name": name, "place": self.manifest["place"], "state": "absent",
                    **({"note": "a Sprite of that name is another one"} if cur else {})}
        word = str(cur.get("status") or "")
        return {"name": name, "place": self.manifest["place"],
                "state": "awake" if word == "running" else "asleep", "provider_state": word, "persist": "disk",
                "region": None, "size": None, "sprite": sprite, "org": org}

    def list(self):
        out = []
        orgs = sorted({org_of(s) for s in self.computers.values() if s.get("PROVIDER") == self.name})
        mine = {(org_of(s), sprite_of(s)): n for n, s in self.computers.items() if s.get("PROVIDER") == self.name}
        for org in orgs:
            for i in self.cli.list(org):
                n = mine.get((org, i["name"]))
                out.append({"name": n or i["name"], "sprite": i["name"], "org": org, "place": self.manifest["place"],
                            "state": "awake" if i.get("status") == "running" else "asleep",
                            "here": n is not None})
        return out

    # snapshots: Fly's checkpoints, named by their comment (metacomputer:LABEL)
    def _checkpoints(self, name):
        org, sprite, _ = self._live(name)
        return org, sprite, self.cli.checkpoints(org, sprite)

    def snapshot(self, name, label):
        check_name(label)
        org, sprite, _ = self._checkpoints(name)
        return {"label": label, "version": self.cli.checkpoint(org, sprite, CHECKPOINT_PREFIX + label)}

    def snapshots(self, name):
        _org, _sprite, cps = self._checkpoints(name)
        out = []
        for c in cps:
            com = str(c.get("comment") or "")
            if com.startswith(CHECKPOINT_PREFIX):
                out.append({"label": com[len(CHECKPOINT_PREFIX):], "version": c["id"], "created": c.get("create_time")})
        return out

    def version_of(self, name, label):
        """The newest checkpoint called `label`, or AdapterError."""
        check_name(label)
        found = [s for s in self.snapshots(name) if s["label"] == label]
        if not found:
            raise AdapterError(f"{name} has no checkpoint {label} (metacomputer --vm {name} checkpoints)")
        return max(found, key=lambda s: (str(s.get("created") or ""), int(s["version"][1:] or 0)))["version"]

    def restore(self, name, label):
        v = self.version_of(name, label)
        org, sprite, _ = self._live(name)          # again, right before: the version is that Sprite's
        self.cli.restore(org, sprite, v)
        return {"label": label, "version": v,
                "note": "the Sprite's programs restarted: the desktop comes back as a service, open apps don't"}

    def delete_snapshot(self, name, label):
        """Deletes every checkpoint called `label`."""
        gone = [s["version"] for s in self.snapshots(name) if s["label"] == check_name(label)]
        org, sprite, _ = self._live(name)
        if not gone:
            raise AdapterError(f"{name} has no checkpoint {label}")
        for v in gone:
            self.cli.delete_checkpoint(org, sprite, v)
        return {"label": label, "versions": gone}

    # egress: Fly's network policy
    def egress(self, name):
        s = self._settings(name)
        out = {"model": "dns-allowlist", "setting": check_egress(s.get("EGRESS")), "allow": []}
        st = self.states.get(name) or {}
        if st.get("instance") and self._cli is not None:
            org, sprite, _ = self._live(name)
            kind, names = policy_kind(self.cli.policy(org, sprite))
            out.update(outside=kind, allow=names)
        return out

    def apply_egress(self, name, egress):
        """EGRESS as the network policy (internet: every name; off: none), read back."""
        kind, _ = self.set_policy(name, rules_for(check_egress(egress)))
        return dict(self.egress(name), outside=kind)

    def set_egress(self, name, allow, exact=False):
        """An allowlist of names as the network policy (each with its subdomains unless exact), read back."""
        self._settings(name)
        try:
            rules = rules_for(allow=allow, exact=exact)
        except ValueError as e:
            raise AdapterError(str(e)) from None
        self.set_policy(name, rules)
        return self.egress(name)

    # connection
    def setup(self):
        return {"ok": True, "note": "install Fly's sprite command and sign in (sprite login); then metacomputer vm create "
                                    "NAME --on sprites --org ORG"}

    def doctor(self):
        if self._cli is None:
            return [{"check": "sprite command", "ok": False, "detail": "none given (SPRITE_CLI; docs/sprites.md)"}]
        out = [{"check": "sprite command", "ok": os.access(self.cli.path, os.X_OK), "detail": self.cli.path}]
        for n, s in sorted(self.computers.items()):
            if s.get("PROVIDER") != self.name:
                continue
            org, sprite = org_of(s), sprite_of(s)
            try:
                cur = self.cli.info(org, sprite)
            except AdapterError as e:
                out.append({"check": f"{n}: Fly's API", "ok": False, "detail": str(e)})
                continue
            out.append({"check": f"{n}: its Sprite {sprite} in {org}", "ok": cur is not None,
                        "detail": (cur or {}).get("status") or "not found"})
            if cur is not None:
                kind, _ = policy_kind(self.cli.policy(org, sprite))
                out.append({"check": f"{n}: a network policy is enforced", "ok": kind in ("internet", "off", "allowlist"),
                            "detail": kind})
        return out

    # cost
    def rates(self, size=None):
        awake = DESKTOP_CPU * CPU_HOUR + DESKTOP_MEM_GB * MEM_GB_HOUR + DESKTOP_DISK_GB * HOT_GB_HOUR
        busy = 8 * CPU_HOUR + 8 * MEM_GB_HOUR + DESKTOP_DISK_GB * HOT_GB_HOUR
        return {"awake_per_hour": round(awake, 4), "asleep_per_hour": round(DESKTOP_DISK_GB * COLD_GB_HOUR, 6),
                "currency": "USD",
                "note": (f"a desktop driven now and then, at Fly's list prices (CPU time ${CPU_HOUR}/CPU-hour, memory "
                         f"${MEM_GB_HOUR}/GB-hour in use, disk ${HOT_GB_HOUR}/GB-hour awake and ${COLD_GB_HOUR:.6f}/GB-hour "
                         f"asleep); all 8 CPUs busy with 8 GB in use: about ${busy:.2f}/h. Asleep (about 10 s after "
                         "nothing is connected): its disk only")}
