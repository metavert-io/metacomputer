#!/usr/bin/env python3
"""A computer that isn't a Lume VM on this Mac: an Ubuntu 24.04 VM reached over SSH (your own server, or one a
provider made). bin/metacomputer and scripts/remote-build.sh call this for what needs the provider or the
computer's record; everything that talks to the computer itself goes through scripts/lib.sh's ssh_base, whose
remote half asks `target` here (lib/cloud/transport.py) once per command.

    remote.py target --vm NAME --run-dir DIR --role ROLE --user USER --transport T --host H --port P [--setting K=V]...
        the ssh options and destination for that computer, NUL-separated (scripts/lib.sh ssh_base); --setting: the
        settings a provider's transport reads (its manifest's transport_settings)
    remote.py create --vm NAME                 a provider's computer: its instance (and the provider's network);
                                               a launch whose record was lost (by its operation tag) is deleted
    remote.py egress --vm NAME --set internet|off   the provider's outside rules for EGRESS (where it has them)
    remote.py resolvers --vm NAME --first-boot --set ADDRESS... | --pinned   the provider's DNS resolver, let through
                                               its firewall where it's on the private network (its adapter's
                                               RESOLVER_EXCEPTION): read once at the first boot, checked and pinned;
                                               never read again
    remote.py identity --vm NAME --write|--remove   the computer's identity (its instance, or this setup of your own
                                               server, and its host key), beside its keys: the vault's binding must
                                               match it
    remote.py user-data --vm NAME              print the cloud-init user-data a new instance gets
    remote.py wake | sleep | delete --vm NAME  through the provider (your own server: refused, with why); delete
                                               refuses a launch that got no answer and shows nowhere, unless
                                               --checked-provider (it stays listed by cloud status)
    remote.py status --vm NAME [--json]        the provider's view of it (and the address, when it runs)
    remote.py state --vm NAME                  one word: awake, stopped, starting, stopping, absent, unknown
    remote.py rates --vm NAME [--json]         what it costs awake and asleep
    remote.py cloud status|doctor|setup|teardown PROVIDER [--json] [--forget OP] [its own options]
        what metacomputer has made at a provider (tagged), checks, the provider's network, and removing it; a
        provider's own options are its manifest's cloud_options
    remote.py exec --vm NAME [--stdin] -- ARGV...   a command in the computer through its provider's own
                                               authenticated channel (the setup's first step: before its own SSH
                                               exists), where the provider has one
    remote.py checkpoint --vm NAME list|create|restore|delete [LABEL]   a provider's snapshots, where it has them
    remote.py allow --vm NAME [--exact] [--remove] NAME... | --clear | --show   an allowlist of DNS names as its
                                               provider's network policy, where it has one (--clear: back to EGRESS)
    remote.py tunnel ... (target's options)    the vault's binding of a computer behind a provider's tunnel, one
                                               argument a line: the tunnel, its arguments, the provider's id and how
                                               to ask about it, all or a refusal; nothing for one SSH reaches straight

What a provider does differently is its own (providers/<name>/remote.py, called with this module: hooks_for); what
it has, the words it's shown with and its credentials are its manifest's (lib/cloud/manifests.py).

The computer's settings are its settings file (config/vms/NAME.env, read through lib/labconfig.py, as every
helper reads them); what its provider made (the instance, its address) is RUN_DIR/remote.json, written only here,
0600. Provider credentials come only from the environment (PROVIDER_ENV: each provider's manifest names its own).
Nothing here prints one."""
import argparse
import contextlib
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

LIB = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(LIB)
sys.path.insert(0, LIB)
import cloud  # noqa: E402
from cloud import guest as cloud_guest  # noqa: E402
from cloud import manifests  # noqa: E402
from cloud import transport  # noqa: E402
from cloud.adapter import AdapterError  # noqa: E402

STATE_FILE = "remote.json"
ROLES = ("admin", "ops", "mcp", "view")
WRAPPER = (sys.executable, os.path.join(LIB, "proxywrap.py"))


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strictargs  # noqa: E402  (argparse that refuses what a command wouldn't use)


class Refused(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --- the provider's credentials ---------------------------------------------------------------------------
# metacomputer starts this program with them in its environment (scripts/lib.sh remote_py) and nothing else. They
# leave this process's environment before anything else runs, and go to the provider's adapter as a value: no child
# this program starts (getconf, the settings resolver's bash, ssh) can inherit them.
PROVIDER_ENV = manifests.credentials()      # every provider's, as its manifest names them (lib/cloud/manifests.py)
CREDS = {}


NOT_SECRET = manifests.not_secret()       # a credential's part that isn't one (a region's name is in ordinary messages)


def cred_values():
    """What no message may show: each credential this program was given (PROVIDER_ENV, longest first), and each line
    of a multi-line one (an API key's PEM, whose lines a provider could echo one at a time). The only credentials this
    process reads; a provider whose own command keeps its sign-in has none here (this program never sees it), and what
    that command says is scrubbed of anything credential-shaped where it becomes a message (its facade's job)."""
    out = set()
    for k, v in CREDS.items():
        if not v or k in NOT_SECRET:
            continue
        out.add(v)
        out.update(ln.strip() for ln in v.splitlines() if len(ln.strip()) >= 16 and not ln.startswith("-----"))
    return sorted((v for v in out if len(v) >= 8), key=len, reverse=True)


def no_creds(text):
    """Text this program prints, with no credential it was given in it (whatever a provider echoed back into a message;
    lib/cloud/vps.py scrub_exception scrubs what leaves a provider's facade, and this is the last look). Everything
    this program prints passes it: main() puts NoCreds over its standard output and error."""
    out = str(text)
    for v in cred_values():
        out = out.replace(v, "[redacted]")
    return out


def no_creds_bytes(data):
    out = bytes(data)
    for v in cred_values():
        out = out.replace(v.encode(), b"[redacted]")
    return out


class NoCreds:
    """A stream every write of which goes through no_creds (no_creds_bytes for its .buffer): whatever this program
    prints, a note, a warning or an error, never shows a credential it was given."""

    def __init__(self, stream, binary=False):
        self._stream, self._binary = stream, binary

    def write(self, data):
        return self._stream.write(no_creds_bytes(data) if self._binary else no_creds(data))

    @property
    def buffer(self):
        return NoCreds(self._stream.buffer, binary=True)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def take_credentials(environ=None):
    environ = os.environ if environ is None else environ
    for k in PROVIDER_ENV:
        v = environ.pop(k, None)
        if v is not None:
            CREDS[k] = v
    return CREDS


def child_env(extra=None):
    """The environment of a program this one starts: this one's (the credentials are gone from it already), and never
    one of the provider's variables even if something put them back."""
    env = {k: v for k, v in os.environ.items() if k not in PROVIDER_ENV}
    env.update(extra or {})
    return env


# --- the computer's record ---------------------------------------------------------------------------
def settings_for(vm):
    """The settings of the computer `vm` (its config/vms/NAME.env), as every helper reads them."""
    import labconfig
    env = dict(os.environ)
    env["MCOMP_VM"] = vm
    env.pop("MCOMP_CONFIG", None)
    env.pop("MCOMP_NO_DEFAULT_VM", None)
    s = labconfig.load(env)
    if s.get("VM_NAME") != vm:
        raise Refused(f"the settings for {vm} name {s.get('VM_NAME')!r}")
    if (s.get("PLACE") or "mac") == "mac":
        raise Refused(f"{vm} is a VM on this Mac, not one reached over SSH")
    # Its folders are fixed by its name (scripts/lib.sh mc_vm_default; a config/vms/ file can't set them). When
    # metacomputer runs this for that computer, its own values come along and must be the same folders.
    fixed = {"SSH_DIR": os.path.join(os.environ.get("HOME", os.path.expanduser("~")), ".ssh", f"metacomputer-{vm}"),
             "RUN_DIR": os.path.join(LAB, ".run-vms", vm), "LOG_DIR": os.path.join(LAB, "logs-vms", vm)}
    if os.environ.get("MCOMP_VM_NAME") == vm:
        for k, e in (("SSH_DIR", "MCOMP_SSH_DIR"), ("RUN_DIR", "MCOMP_RUN_DIR"), ("LOG_DIR", "MCOMP_LOG_DIR")):
            v = os.environ.get(e)
            if v and os.path.realpath(v) != os.path.realpath(fixed[k]):
                raise Refused(f"{e} ({v}) isn't {vm}'s own folder ({fixed[k]})")
    s.update(fixed)
    return s


def run_dir(s):
    return s.get("RUN_DIR") or os.path.join(LAB, ".run-vms", s["VM_NAME"])


def read_state(rd, name=STATE_FILE):
    p = os.path.join(rd, name)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Refused(f"can't read {p}: {e.strerror}") from None
    with os.fdopen(fd) as f:
        data = f.read(65536)
    try:
        d = json.loads(data)
    except ValueError:
        raise Refused(f"{p} isn't the record this program writes; look at it, then remove it") from None
    if not isinstance(d, dict):
        raise Refused(f"{p} isn't the record this program writes")
    return d


def write_state(rd, d, name=STATE_FILE):
    """The record, replaced whole and durable: its contents reach the disk (F_FULLFSYNC on macOS, which plain fsync
    doesn't promise there) before it takes the old one's place, and the folder's entry after, so a crash or a power
    cut never loses a launch's journal once the launch could have started."""
    os.makedirs(rd, mode=0o700, exist_ok=True)
    p = os.path.join(rd, name)
    fd, tmp = tempfile.mkstemp(prefix=".remote.", dir=rd)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        durable(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    dfd = os.open(rd, os.O_RDONLY)
    try:
        durable(dfd)
    finally:
        os.close(dfd)


def durable(fd):
    """What's written to fd is on the disk when this returns (macOS: F_FULLFSYNC; elsewhere, fsync). Only a file
    system that doesn't support F_FULLFSYNC falls back to fsync; any other error (an I/O error) is raised: a journal
    that may not be on the disk refuses the launch."""
    import errno
    import fcntl
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as e:
            if e.errno not in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP), errno.ENOTTY, errno.EINVAL):
                raise
    os.fsync(fd)


# Tests hand in a stand-in for the provider's SDK facade (a callable that returns one); never set otherwise.
API_FACTORY = None


@contextlib.contextmanager
def mac_lock(name):
    """One process of this account's at a time holds NAME (in scripts/lib.sh's lock folder: MCOMP_LOCK_DIR, or the
    per-user temporary folder's metacomputer-locks); the others wait for it."""
    import fcntl
    d = os.environ.get("MCOMP_LOCK_DIR") or ""
    if not d:
        try:
            t = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                               timeout=10, env=child_env()).stdout.strip().rstrip("/")
        except (OSError, subprocess.SubprocessError):
            t = ""
        d = os.path.join(t or f"/tmp/mcomp-{os.getuid()}", "metacomputer-locks")
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise Refused(f"the lock folder {d} isn't a folder of this account's own")
    fd = os.open(os.path.join(d, name + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def makes(a):
    """Whether the adapter's provider makes (and starts, stops, deletes) its computers: not your own server."""
    return a.manifest["sleep_wake"]["kind"] != "none"


def stop_start(a):
    """Whether the provider's computers are stopped and started (its manifest's sleep_wake): their adapters take what
    the guest's SSH says (guest_up) and --force; one that sleeps by itself takes neither."""
    return a.manifest["sleep_wake"]["kind"] == "stop-start"


def still_there(a, found, skip=()):
    """The instances of a listing the provider still has, asked one by one by id (a provider's listings can lag behind
    a delete), less those in `skip`."""
    return [i for i in found if i["id"] not in skip and a.api.instance(i["id"]) is not None]


def network_lock(prov):
    """The lock a provider's shared resources (a shared network; a computer's firewall and key) are changed under."""
    return mac_lock(f"{prov}-network")


def adapter_for(s, state=None):
    prov = s.get("PROVIDER") or ""
    if prov not in cloud.ADAPTERS or prov in ("local-lume", "fake"):
        raise Refused(f"PROVIDER={prov or '(none)'} isn't a provider of computers reached over SSH "
                      f"({', '.join(sorted(set(cloud.ADAPTERS) - {'local-lume', 'fake'}))})")
    cls = cloud.ADAPTERS[prov]
    if cls.manifest["place"] != (s.get("PLACE") or ""):
        raise Refused(f"PLACE={s.get('PLACE')} isn't {prov}'s place ({cls.manifest['place']})")
    return cloud.adapter(prov, computers={s["VM_NAME"]: s}, states={s["VM_NAME"]: state or {}}, **provider_kw(prov, s))


def hooks_for(prov, need=None):
    """The provider's own part of this program (providers/<name>/remote.py), or None: given `need`, only when it has
    that hook. It's called with this module (R) for the records, refusals and locks here."""
    h = cloud.registry.hooks(prov) if prov in cloud.registry.PROVIDERS else None
    if h is None or (need is not None and not hasattr(h, need)):
        return None
    return h


def provider_kw(prov, s=None):
    """What a provider's adapter is made with here: its credentials (a value, not the environment), the way to its
    REST API (lib/provider_http.py, for an adapter that says REST_API; an SDK has its own) and the lock its shared
    network, or a computer's firewall and key, are changed under; or what its own hook makes it with (a provider
    whose command keeps its own sign-in: the facade of that command)."""
    h = hooks_for(prov, "provider_kw")
    if h is not None:
        return h.provider_kw(sys.modules[__name__], s)
    cls = cloud.ADAPTERS.get(prov)
    if cls is None or cls.test_only or cls.manifest["sleep_wake"]["kind"] == "none" or prov == "local-lume":
        return {}
    kw = {"api": API_FACTORY()} if API_FACTORY is not None else {"env": dict(CREDS)}
    if getattr(cls, "REST_API", False) and API_FACTORY is None:
        import provider_http
        kw["http"] = provider_http.send
    kw["network_lock"] = lambda: network_lock(prov)
    return kw


# --- what metacomputer does there -----------------------------------------------------------------------
def key_text(ssh_dir, name):
    with open(os.path.join(ssh_dir, name)) as f:
        return f.read()


def user_data(s):
    ssh_dir = s["SSH_DIR"]
    keys = {k: key_text(ssh_dir, f"{k}.pub") for k in ("ops", "mcp", "view", "admin")}
    with open(os.path.join(LAB, "provisioning", "remote", "bootstrap.sh")) as f:
        bootstrap = f.read()
    tz = None
    try:
        tz = os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        pass
    with open(os.path.join(LAB, "provisioning", "remote", "rootfs", "opt", "metacomputer", "bin", "guest-firewall")) as f:
        firewall = f.read()
    return cloud_guest.user_data(s["VM_NAME"], key_text(ssh_dir, "hostkey"), key_text(ssh_dir, "hostkey.pub"), keys,
                                 bootstrap, firewall, egress=(s.get("EGRESS") or "internet").strip().lower(),
                                 hostname=s.get("VM_HOSTNAME") or "metacomputer", timezone=tz)


OP_RE = __import__("re").compile(r"[0-9a-f]{32}")


def cmd_create(s, args):
    """Launch the computer's instance. The launch is journaled first (an operation id in the record, the same id as a
    tag on the instance), so an instance whose record was lost is found and deleted by the next try; an instance
    that can't be recorded is deleted at once. Nothing billed is ever left without a record here. The build names
    the operation (--op), so a failed build deletes only what that operation made (cmd_abort)."""
    import uuid
    op = getattr(args, "op", None) or uuid.uuid4().hex
    if not OP_RE.fullmatch(op):
        raise Refused("--op must be 32 hex digits")
    rd = run_dir(s)
    st = read_state(rd)
    if st.get("instance"):
        raise Refused(f"{s['VM_NAME']} has an instance already ({st.get('provider')}); metacomputer --vm "
                      f"{s['VM_NAME']} status shows it")
    a = adapter_for(s, st)
    if a.manifest["sleep_wake"]["kind"] == "none":
        raise Refused(f"{a.manifest['label']}: there's nothing to make (you bring the server)")
    h = hooks_for(a.name, "create")
    if h is not None:
        return h.create(sys.modules[__name__], s, a, rd, st, op)
    if st.get("launching"):
        # An earlier operation that never finished: what it launched is found by the ids it journaled (asked one by
        # one) and its mark, and deleted, with what it made; one whose launch got no answer and shows nowhere yet
        # blocks a new launch under this name (it could make a second server).
        prev = st["launching"]
        found, unresolved = reconcile(s, st, a, prev)
        for iid in found:
            print(f"metacomputer: an earlier launch of {s['VM_NAME']} left an instance it never finished: deleting it",
                  file=sys.stderr)
            a.api.terminate(iid)
            a.api.wait_state(iid, "TERMINATED", timeout=900)
        if unresolved:
            write_state(rd, dict(st, unresolved=now()))
            raise Refused(unresolved_words(s, a, prev) + " No new one is made under this name until then.")
        delete_op_parts(s, st, a, prev, still_there(a, a.api.instances(s["VM_NAME"])))
        st = read_state(rd)
    ud = user_data(s)
    t0 = time.time()
    st = {k: v for k, v in st.items() if k not in JOURNAL_KEYS}
    # Every operation started for this computer stays named in its record (`ops`, not journal keys: never cleared by a
    # later try), so what one of them made and couldn't delete (its group, at a provider whose groups go only by the
    # record: groups_by_record) is found by its mark again at vm delete, never by the computer's name.
    ops = [o for o in (st.get("ops") or []) if isinstance(o, str) and OP_RE.fullmatch(o) and o != op][-(MAX_OPS - 1):] + [op]
    try:
        write_state(rd, dict(st, provider=a.name, launching=op, ops=ops))   # the journal, durable before anything is billed
    except OSError as e:
        raise Refused(f"couldn't make the launch's journal durable ({e.strerror or e}): nothing was launched") from None

    def made_here(what, rid):
        # Called the moment this operation makes the network, the computer's group (its firewall), its first-boot
        # key or its instance (made_instance), with its id, and when its launch is sent and answered (launch_state):
        # only those exact resources are ever this operation's to delete (cmd_abort), and a launch with no answer is
        # known to be one.
        cur = read_state(rd)
        if cur.get("launching") == op:
            write_state(rd, dict(cur, **{what: rid}))
    print(f"metacomputer: making {s['VM_NAME']} on {a.manifest['label']} ({s.get('SIZE') or 'the default size'})",
          file=sys.stderr)
    kw = {}
    if a.manifest["regions"]:
        kw["region"] = s.get("REGION") or None
    if getattr(a, "takes_key", False):
        # The provider makes a server without a root password only when it's given a key: the admin key's public
        # half, for the first boot only (bootstrap.sh takes it off root again).
        kw["public_key"] = key_text(s["SSH_DIR"], "admin.pub")
    made = a.create(s["VM_NAME"], size=s.get("SIZE") or None, persist=s.get("PERSIST") or "auto", user_data=ud,
                    egress="internet", op=op,    # public addresses while it's set up: remote-build.sh sets EGRESS after
                    made=made_here, **kw)
    mine = read_state(rd)
    st = {"provider": a.name, "created": now(), "launch_started": t0, "op": op, "made_by_build": True,
          "made_network": mine.get("made_network"), "made_nsg": mine.get("made_nsg"),
          "made_key": mine.get("made_key"), "launch_state": mine.get("launch_state"), "ops": mine.get("ops") or [op],
          **made}
    try:
        write_state(rd, st)                  # the instance is recorded before anything else can fail
    except OSError as e:
        a.api.terminate(made["instance"])
        raise Refused(f"couldn't record the instance it made ({e.strerror}), so it was deleted again; nothing is left "
                      "billing") from None
    print(f"  {getattr(a, 'thing', 'instance')} launched in {str(made['ad']).split(':')[-1]} ({made['shape']}, "
          f"{made['image']})", file=sys.stderr)
    a.api.wait_state(made["instance"], "RUNNING")
    host = None
    for _ in range(60):
        host = a.api.public_ip(made["instance"])
        if host:
            break
        time.sleep(5)
    if not host:
        raise Refused("the instance runs but has no public address; metacomputer vm delete removes it")
    st.update(host=host, running_at=time.time())
    write_state(rd, st)
    print(f"  running at {host} after {int(time.time() - t0)} s", file=sys.stderr)
    return 0


# What a launch leaves in the journal (lib/cloud: the `made` callback), cleared when a new operation starts.
JOURNAL_KEYS = ("made_network", "made_nsg", "made_key", "made_instance", "launch_state", "kept", "unresolved")
MAX_OPS = 64               # operations a computer's record keeps naming (ops)
LOST_LAUNCH_LOOKS = 6       # a launch with no answer: the provider's listing is looked at this many times more,
LOST_LAUNCH_GAP_S = 10      # this far apart (a listing can lag behind what was just made: one provider's did)


class NotOwned(Refused):
    """An instance this Mac was about to act on doesn't carry, now, the computer's name and the operation recorded for
    it: nothing is acted on, and the record stays."""


def said(a, text, cap=64):
    """What the provider said (an instance's name or mark), fit to go into a message here."""
    scrub = getattr(a.api, "scrub", None)
    return scrub(text, cap) if callable(scrub) else str(text)[:cap]


def owned(a, iid, name, op, why="journaled"):
    """THE check before anything here deletes, stops, starts or forgets an instance (a failed build's clean-up and
    --keep, a create's recovery of an unfinished one, vm delete, start, stop, cloud teardown): the instance read now
    by its own id, never taken from a listing's row, must carry computer `name` and operation `op`, as this Mac
    recorded them. Returns it, or None when it's gone. Anything else raises NotOwned: nothing is acted on, and the
    record (the journal, the registry of unresolved launches) stays."""
    label, thing = a.manifest["label"], getattr(a, "thing", "instance")
    if not isinstance(op, str) or not op:
        raise NotOwned(f"{name}'s record names no operation for {label}'s {thing} {iid}, so nothing here can tell it's "
                       "the one this Mac made: nothing was deleted or stopped, and the record stays")
    cur = a.api.instance(iid)
    if cur is None:
        return None
    if cur.get("name") != name or cur.get("op") != op:
        raise NotOwned(f"{label}'s {thing} {iid}, {why} by {name}'s operation {op[:12]}, is now marked for "
                       f"{said(a, cur.get('name'))!r}'s operation {said(a, str(cur.get('op') or 'none')[:12])}: nothing was "
                       f"deleted or stopped, and the record stays. Look at it: {look_for(a, name, op)}")
    return cur


def owned_all(a, ids, name, op, why="journaled"):
    """owned() for every candidate (the ids journaled, a listing's rows), all of them checked before any is acted on:
    {id: instance} of those that are there."""
    out = {}
    for iid in dict.fromkeys(i for i in ids if i):
        cur = owned(a, iid, name, op, why)
        if cur is not None:
            out[iid] = cur
    return out


def reconcile(s, st, a, op):
    """What operation `op` launched: the instances it journaled, each asked for by its own id (never inferred from a
    listing, which can lag behind or miss what was just made), and any its mark shows in the provider's listing, each
    of those read again by its own id too (a listing's row is only a candidate: owned). Returns ({id: instance},
    unresolved). unresolved: its launch was sent, no answer named what it made, and nothing shows even after looking
    again: a server may exist, and bill, that nothing here can name. A read that fails, or an instance that doesn't
    carry this computer's name and this operation now, raises: nothing is assumed, and nothing is acted on."""
    name = s["VM_NAME"]
    mine = st.get("op") == op or st.get("launching") == op
    recorded = [x for x in dict.fromkeys((st.get("instance"), st.get("made_instance"))) if x] if mine else []
    found = owned_all(a, recorded, name, op, "journaled")

    def look():
        rows = [i["id"] for i in a.api.instances(name, op=op) if i["id"] not in found]
        found.update(owned_all(a, rows, name, op, "listed for"))
    look()
    lost = mine and st.get("launch_state") == "sent" and not recorded
    if lost and not found:
        for _ in range(LOST_LAUNCH_LOOKS):
            time.sleep(LOST_LAUNCH_GAP_S)
            look()
            if found:
                break
    return found, bool(lost and not found)


def look_for(a, name, op):
    """Where the person looks for what operation `op` may have made: the provider's console and API, exactly."""
    return a.look_for(name, op) if hasattr(a, "look_for") else f"metacomputer cloud status {a.name}"


def unresolved_words(s, a, op):
    return (f"{s['VM_NAME']}'s launch (operation {op}) got no answer from {a.manifest['label']}, and no server from it "
            f"shows there yet: one may exist, and bill, that nothing here can name. Look for it: "
            f"{look_for(a, s['VM_NAME'], op)} (or metacomputer cloud status {a.name}); once it shows, metacomputer vm "
            f"delete {s['VM_NAME']} (or another build) deletes it.")


UNRESOLVED_FILE = "unresolved-launches.json"   # beside every computer's runtime folder: vm delete never removes it


def unresolved_dir():
    return os.path.join(LAB, ".run-vms")


def unresolved_launches():
    """Launches whose answer was lost and that showed nowhere when their computer was deleted anyway (vm delete
    --checked-provider): [{provider, computer, op, since, look}]. Kept until a teardown finds and deletes what they
    made, or the person forgets one by its operation (cloud teardown PROVIDER --forget OP)."""
    d = read_state(unresolved_dir(), UNRESOLVED_FILE)
    return [e for e in d.get("launches") or [] if isinstance(e, dict) and OP_RE.fullmatch(str(e.get("op") or ""))]


REGISTRY_LOCK = "unresolved-launches"        # one lock for the registry, whichever provider or computer changes it


def update_unresolved(change):
    """The registry's only writer: read, changed (`change`: entries -> entries) and written again under one lock that
    every process of this account's takes for it, so two changes at once (two vm deletes, a teardown and a vm delete)
    never lose each other's. Returns what it wrote."""
    with mac_lock(REGISTRY_LOCK):
        entries = change(unresolved_launches())
        write_state(unresolved_dir(), {"launches": entries}, UNRESOLVED_FILE)
        return entries


def forget_parts(a, computer, op):
    """What an unresolved launch's operation made besides its server (its firewall, its key), deleted once the launch
    is settled: by the operation's mark where the provider has one, else everything of the computer's (its record here
    is gone already)."""
    if hasattr(a.api, "nsgs_of_op"):
        for g in a.api.nsgs_of_op(computer, op):
            delete_group(a, g, computer, op)
        for k in (a.api.keys_of_op(computer, op) if hasattr(a.api, "keys_of_op") else ()):
            a.api.delete_key_by_id(k, computer)
    elif not a.api.instances(computer):
        a.api.delete_nsg(computer)


def delete_group(a, gid, name, op):
    """Deletes the security group (firewall) `gid` of computer `name`'s operation `op`. Where the provider's groups are
    acted on only as the record names them (its adapter's groups_by_record), it goes only while, read by its id, it's
    tagged for that computer and that operation."""
    if getattr(a, "groups_by_record", False):
        return a.api.delete_nsg_by_id(gid, name, op)
    return a.api.delete_nsg_by_id(gid, name)


def remember_unresolved(s, a, op):
    entry = {"provider": a.name, "computer": s["VM_NAME"], "op": op, "since": now(), "look": look_for(a, s["VM_NAME"], op)}
    update_unresolved(lambda entries: [e for e in entries if e.get("op") != op] + [entry])


def delete_op_parts(s, st, a, op, others):
    """The firewall (security group), first-boot key and network operation `op` made: by the ids it journaled as it
    made them, and by its mark (one whose create answer was lost), under the network lock creates and `cloud
    setup|teardown` take: never whichever group or network exists now (one made since is someone else's). A record
    from before ids were kept (True) deletes nothing. What costs nothing and can't be deleted is said, not failed."""
    made_nsg, made_net, made_key = st.get("made_nsg"), st.get("made_network"), st.get("made_key")
    name = s["VM_NAME"]
    try:
        with network_lock(a.name):
            groups = [made_nsg] if isinstance(made_nsg, str) else []
            keys = [made_key] if isinstance(made_key, str) else []
            if hasattr(a.api, "nsgs_of_op"):
                groups += [g for g in a.api.nsgs_of_op(name, op) if g not in groups]
            if hasattr(a.api, "keys_of_op"):
                keys += [k for k in a.api.keys_of_op(name, op) if k not in keys]
            for g in groups:
                if not others and delete_group(a, g, name, op):
                    print(f"deleted the security group (firewall) this build made ({a.manifest['label']})",
                          file=sys.stderr)
            for k in keys:              # its first-boot key: needed only to make the server
                if a.api.delete_key_by_id(k, name):
                    print(f"deleted the SSH key this build gave {a.manifest['label']}", file=sys.stderr)
            if isinstance(made_net, str) and not a.api.instances() and a.api.delete_network_by_id(made_net):
                print(f"deleted the network this build made at {a.manifest['label']} (nothing uses it now)",
                      file=sys.stderr)
    except Exception as e:  # noqa: BLE001  (nothing billed is left: say so, don't fail)
        print(f"what this build made of metacomputer's network, firewall or key wasn't all deleted ({no_creds(e)}); it costs "
              f"nothing: metacomputer cloud status {a.name} lists it, metacomputer vm delete {name} deletes it",
              file=sys.stderr)


def cmd_abort(s, args):
    """A failed build's clean-up: deletes only what its own operation made (`--op`, the tag its launch carried and
    the journal kept): its instances, found by the ids it journaled (each read by id) and by its mark, then the
    computer's security group, key and network only if that operation made them and nothing else uses them. An
    instance it didn't make, of this name or any other, is never touched. A launch that got no answer and shows
    nowhere leaves the journal as it is, says so and fails (exit 3): never "nothing is left". --keep stops this
    operation's instance instead (its disk stays)."""
    op = args.op
    if not OP_RE.fullmatch(op or ""):
        raise Refused("--op must be 32 hex digits")
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    if a.manifest["sleep_wake"]["kind"] == "none":
        return 0
    mine = st.get("op") == op or st.get("launching") == op
    if not mine:
        print(f"metacomputer: nothing of {s['VM_NAME']} was made by this build; nothing deleted", file=sys.stderr)
        return 0
    h = hooks_for(a.name, "abort")
    if h is not None:
        return h.abort(sys.modules[__name__], s, a, rd, st, op, args.keep)
    found, unresolved = reconcile(s, st, a, op)
    if unresolved:
        write_state(rd, dict(st, unresolved=now()))      # the journal stays, so the next try looks again
        print(f"metacomputer: {unresolved_words(s, a, op)} Its firewall and key are kept for it.", file=sys.stderr)
        return 3
    if args.keep:
        if found:
            # Recorded first, durably: this operation's instance, kept. A launch whose answer was lost is only in the
            # journal as `launching`; left so, the next try would take it for a lost launch and delete it.
            st = {k: v for k, v in st.items() if k != "launching"}
            st.update(instance=next(iter(found)), op=op, kept=now(), provider=a.name)
            write_state(rd, st)
        for iid, i in found.items():
            if i.get("state") not in ("STOPPED", "STOPPING"):
                a.api.action(iid, "STOP")
            a.api.wait_state(iid, "STOPPED", timeout=getattr(a, "STOP_WAIT_S", 1080))
        print("stopped" if found else "nothing to stop")
        return 0
    gone = []
    for iid in found:
        a.api.terminate(iid)
        a.api.wait_state(iid, "TERMINATED", timeout=900)       # read by its id until it's gone
        gone.append(iid)
    if gone:
        print(f"deleted the {getattr(a, 'thing', 'instance')} this build made, and its disk ({a.manifest['label']})",
              file=sys.stderr)
    others = still_there(a, a.api.instances(s["VM_NAME"]), skip=gone)
    parts = dict(st)
    st = {k: v for k, v in st.items() if k not in ("instance", "launching", "host", "op", "made_instance",
                                                   "launch_state", "unresolved")}
    st["aborted"] = now()
    write_state(rd, st)
    delete_op_parts(s, parts, a, op, others)
    if others:
        print(f"left alone: {len(others)} instance(s) of {s['VM_NAME']} this build didn't make "
              f"(metacomputer cloud status {a.name})", file=sys.stderr)
    return 0


def guest_up(s, st, tries=3, wait=5.0):
    """Whether the computer's SSH answers (a few tries: a busy guest can be slow); None when it can't be told
    (a provider's tunnel)."""
    ep = transport.endpoint(s, {"host": st.get("host")} if st.get("host") else None)
    if ep["proxy"] is not None:
        return None
    for i in range(tries):
        if probe_once(ep["host"], ep["port"], 4.0):
            return True
        if i + 1 < tries:
            time.sleep(wait)
    return False


def probe_once(host, port, timeout):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout) as c:
            c.settimeout(timeout)
            return c.recv(4).startswith(b"SSH-")
    except OSError:
        return False


def check_recorded(s, st, a):
    """Before a start or a stop: the computer's recorded instance still carries its name and its operation (owned)."""
    if stop_start(a) and st.get("instance"):
        owned(a, st["instance"], s["VM_NAME"], st.get("op"), "recorded")


def check_firewall(s, st, a):
    """Before any rule outside the computer changes (net egress, the resolver's exception): its recorded instance is
    still its (owned: name and operation), and the firewall is the one recorded for it, read by its own id, marked now
    for this computer and this operation (its labels, or its name's operation part, where the provider has one).
    NotOwned otherwise: no rule is changed."""
    if not stop_start(a) or not st.get("instance"):
        return
    name, op = s["VM_NAME"], st.get("op")
    owned(a, st["instance"], name, op, "recorded")
    if not hasattr(a.api, "nsg_owner"):
        return
    label, fw = a.manifest["label"], st.get("nsg")
    if not fw:
        raise NotOwned(f"{name}'s record names no firewall of its own at {label}: no rule was changed")
    who = a.api.nsg_owner(fw)
    if who is None:
        raise NotOwned(f"{name}'s firewall {fw} is gone from {label}: no rule was changed (metacomputer --vm {name} "
                       "status; delete it and make it again)")
    comp, mark = who
    if comp == name and not mark:
        raise NotOwned(f"{name}'s firewall {fw} at {label} was made by an earlier version of metacomputer and carries no "
                       f"operation, so nothing here can tell it's still this computer's: no rule was changed (delete "
                       f"it and make it again: metacomputer vm delete {name})")
    if comp != name or not mark or not op.startswith(str(mark)):
        raise NotOwned(f"{label}'s firewall {fw}, recorded for {name}'s operation {op[:12]}, is now marked for "
                       f"{said(a, comp)!r}'s operation {said(a, str(mark or 'none')[:12])}: no rule was changed, and the "
                       f"record stays. Look at it: {look_for(a, name, op)}")


def cmd_wake(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    check_recorded(s, st, a)
    up = None
    if stop_start(a) and a.status(s["VM_NAME"]).get("provider_state") == "RUNNING":
        up = guest_up(s, st)
        if up is False and getattr(a, "CONSOLE_POWEROFF", False):
            print(f"metacomputer: {s['VM_NAME']} runs at {a.manifest['label']} but doesn't answer: checking its "
                  "console for a power-off", file=sys.stderr)
    out = a.wake(s["VM_NAME"], guest_up=up) if stop_start(a) else a.wake(s["VM_NAME"])
    if out.get("woke_in_s") is not None:
        print(f"answered in {out['woke_in_s']} s", file=sys.stderr)
    if out.get("host") and out["host"] != st.get("host"):
        st["host"] = out["host"]
        write_state(rd, st)
    st["woken"] = now()
    write_state(rd, st)
    print(out.get("state", "?"))
    return 0


def cmd_sleep(s, args):
    st = read_state(run_dir(s))
    a = adapter_for(s, st)
    check_recorded(s, st, a)
    if stop_start(a) and getattr(a, "SOFT_STOP", "api") == "guest" and not args.force:
        # The provider can't ask the guest to shut down: the CLI asked it over SSH (--asked). Only the provider's own
        # state can then say it's off; SSH going quiet proves nothing, and nothing is halted on it (adapter.sleep).
        if not args.asked:
            raise Refused(f"{a.manifest['label']} has no call that asks the guest to shut down: metacomputer --vm "
                          f"{s['VM_NAME']} stop asks it over SSH first (or stop --force halts it at once)")
        out = a.sleep(s["VM_NAME"], guest_up=None, force=False, asked=True)
    elif stop_start(a):
        out = a.sleep(s["VM_NAME"], guest_up=guest_up(s, st, tries=2), force=args.force)
    else:
        out = a.sleep(s["VM_NAME"])
    print(out.get("state", "?"))
    return 0


def cmd_delete(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    if a.manifest["sleep_wake"]["kind"] == "none":
        print("nothing to delete at the provider: it's your own server", file=sys.stderr)
        return 0
    h = hooks_for(a.name, "delete")
    if h is not None:
        return h.delete(sys.modules[__name__], s, a, rd, st, args)
    unresolved_op = None
    name, label, thing = s["VM_NAME"], a.manifest["label"], getattr(a, "thing", "instance")
    # What this deletes is decided, and checked, before anything is deleted: each instance read by its own id must
    # carry this computer's name and the operation recorded for it (owned), and one of its name that this Mac didn't
    # make (another operation's) stops it here, with nothing deleted and the record kept.
    targets, recorded = {}, None
    if st.get("instance"):
        recorded = owned(a, st["instance"], name, st.get("op"), "recorded")
    elif st.get("launching"):
        # An operation that never finished: what it launched, by its journaled ids and its mark.
        found, unresolved = reconcile(s, st, a, st["launching"])
        targets.update(found)
        if unresolved:
            # Never forgotten silently: refused (the journal stays) until the person says they've looked; then the
            # operation stays listed by `cloud status`, with where to look.
            if not getattr(args, "checked_provider", False):
                write_state(rd, dict(st, unresolved=now()))
                raise Refused(f"{unresolved_words(s, a, st['launching'])} Nothing was deleted and its record stays. "
                              f"Once you've looked: metacomputer vm delete {name} --checked-provider, in your own "
                              f"terminal (its operation stays listed by metacomputer cloud status {a.name})")
            unresolved_op = st["launching"]
    ops = {o for o in (st.get("op"), st.get("launching")) if o}
    foreign = []
    for i in a.api.instances(name):
        if i["id"] in targets or i["id"] == st.get("instance"):
            continue
        cur = a.api.instance(i["id"])           # a listing's row is only a candidate: read by its id
        if cur is None or cur.get("name") != name:
            continue
        if cur.get("op") in ops:
            targets.update(owned_all(a, [i["id"]], name, cur["op"], "listed for"))
        else:
            foreign.append(cur)
    if foreign:
        which = ", ".join(f"{c['id']} (operation {said(a, str(c.get('op') or 'none')[:12])})" for c in foreign)
        raise NotOwned(f"{label} has {len(foreign)} {thing}(s) named for {name} that this Mac's record of it didn't "
                       f"make ({which}): "
                       f"nothing was deleted, and the record stays. If one is yours (a build whose record was lost), "
                       f"delete it at {label} ({look_for(a, name, foreign[0].get('op') or '')}), then metacomputer vm "
                       f"delete {name} again")
    if unresolved_op is not None:
        remember_unresolved(s, a, unresolved_op)
        print(f"metacomputer: warning: {unresolved_words(s, a, unresolved_op)} You said you've checked: this "
              f"Mac's settings and records go, and operation {unresolved_op} stays listed by metacomputer cloud "
              f"status {a.name}, with its firewall and key kept for it, until a teardown finds and deletes what it "
              f"made (or cloud teardown {a.name} --forget {unresolved_op}, in your own terminal).", file=sys.stderr)
    if st.get("instance"):
        a.delete(name)          # the recorded one, checked above (gone already: its firewall and key go)
        print(f"deleted the {thing} and its disk ({label})" if recorded is not None else
              f"its {thing} was gone already at {label}", file=sys.stderr)
    for iid in targets:
        print(f"deleting an instance an unfinished build of {name} launched", file=sys.stderr)
        a.api.terminate(iid)
        a.api.wait_state(iid, "TERMINATED", timeout=900)
    if unresolved_op is None:
        if getattr(a, "groups_by_record", False):
            # Groups acted on only by the record (groups_by_record): only the groups its record names (the ids it
            # recorded, and the marks of the operations this Mac started for it), each read by its id and tagged for it
            # and that operation: never one found by its name (another Mac's computer of the same name, or one being
            # made, is left as it is)
            a.delete_groups(name, st)
        else:
            a.api.delete_nsg(s["VM_NAME"])
        if hasattr(a.api, "delete_keys"):
            a.api.delete_keys(s["VM_NAME"])   # its first-boot key (a provider that takes one)
    # else: the firewall and key a server that may exist would be using stay, with the record (cloud teardown)
    left = still_there(a, a.api.instances(s["VM_NAME"]))
    if left:
        raise Refused(f"{a.manifest['label']} still has an instance for {s['VM_NAME']}: {left}")
    st.pop("instance", None)
    st.pop("launching", None)
    st["deleted"] = now()
    write_state(rd, st)
    if args.network_too:
        # Under the lock creates make the network with: never while another computer is being made (its group is
        # there before its instance) or has an instance.
        try:
            with network_lock(a.name):
                if not a.api.instances() and not a.api.nsg_names() and a.api.delete_network():
                    print(f"deleted metacomputer's network at {a.manifest['label']} (no computer uses it now)",
                          file=sys.stderr)
        except Exception as e:  # the instance is gone; a network left over costs nothing (say so, don't fail)
            print(f"{s['VM_NAME']}'s instance is deleted, but metacomputer's network wasn't ({no_creds(e)}); a network costs "
                  f"nothing at {a.manifest['label']}: metacomputer cloud teardown {a.name} deletes it", file=sys.stderr)
    return 0


def cmd_status(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    if a.manifest["sleep_wake"]["kind"] == "none":
        out = {"name": s["VM_NAME"], "place": a.manifest["place"], "state": "unknown",
               "host": s.get("SSH_HOST"), "note": "your own server: metacomputer reaches it over SSH"}
    elif args.offline:
        out = {"name": s["VM_NAME"], "place": a.manifest["place"], "state": None, "host": st.get("host"),
               "note": "its provider was not asked (no credentials in this command)"}
    else:
        out = a.status(s["VM_NAME"])
        if out.get("host") and out["host"] != st.get("host"):
            st["host"] = out["host"]
            write_state(rd, st)
        out["host"] = out.get("host") or st.get("host")
        if out.get("provider_state") == "RUNNING" and guest_up(s, st, tries=1) is False:
            if getattr(a, "CONSOLE_POWEROFF", False):
                out["note"] = ("it doesn't answer: if it powered itself off (unused for IDLE_STOP minutes), the provider "
                               "still counts it as running, and bills a billed size, until metacomputer stop (start does it "
                               "when its console shows the power-off)")
            elif getattr(a, "SEES_GUEST_POWEROFF", True):
                out["note"] = (f"it doesn't answer, though {a.manifest['label']} says it runs: it may be starting, busy "
                               "or cut off")
            else:
                out["note"] = (f"it doesn't answer, though {a.manifest['label']} says it runs: {a.manifest['label']} "
                               "doesn't see a guest that powered itself off (the idle stop does that), so it may be off; "
                               f"or busy or cut off. To restart it: metacomputer --vm {s['VM_NAME']} stop --force, then start")
    if makes(a):
        out["billing"] = a.manifest["billing"]["note"]
    out.update(provider=a.name, where=a.manifest["label"], size=s.get("SIZE") or st.get("size"),
               arch=st.get("arch") or s.get("GUEST_ARCH"), egress=s.get("EGRESS") or "internet",
               idle_stop=s.get("IDLE_STOP") or ("off" if a.manifest["sleep_wake"]["kind"] == "none" else "15"))
    if a.manifest["sleep_wake"]["kind"] == "auto-sleep":
        out["idle_stop"] = "n/a: it sleeps by itself about 10 s after nothing is connected"
        out["arch"] = out.get("arch") or "x86_64"
    if not a.manifest["input"]["background"]:
        out["input"] = "foreground only: " + a.manifest["input"]["note"]
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        for k in ("where", "state", "host", "size", "arch", "egress", "idle_stop", "input", "billing"):
            if out.get(k) is not None:
                print(f"{k}: {out[k]}")
    return 0


def cmd_state(s, args):
    try:
        st = read_state(run_dir(s))
        a = adapter_for(s, st)
        if a.manifest["sleep_wake"]["kind"] == "none":
            print("unknown")
            return 0
        print(a.status(s["VM_NAME"]).get("state", "unknown"))
    except (AdapterError, Refused):
        print("unknown")
    return 0


def cmd_rates(s, args):
    a = adapter_for(s, read_state(run_dir(s)))
    r = a.rates(s.get("SIZE") or None) if makes(a) else a.rates()
    asleep = "asleep" if a.manifest["sleep_wake"]["kind"] == "auto-sleep" else "stopped"
    print(json.dumps(r) if args.json else f"${r['awake_per_hour']}/h awake, ${r['asleep_per_hour']:.6f}/h {asleep}: {r['note']}")
    return 0


def cmd_resolvers(s, args):
    """The DNS resolver let through a computer's firewall at a provider whose resolver is on its private network
    (RESOLVER_EXCEPTION): exactly that, port 53. --first-boot --set ADDRESS...: what the computer named at its first
    boot (the build reads it before anything but cloud-init and bootstrap.sh has run in it), each checked to be the
    provider's own resolver for its network, and pinned: taken once, never again. --pinned: the pinned ones (a build
    that sets the computer up again, and `net egress`, never read the computer); with none pinned, the provider's
    documented resolver. Prints them (or "none")."""
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    if not getattr(a, "RESOLVER_EXCEPTION", False):
        print("none")
        return 0
    if args.first_boot == args.pinned:
        raise Refused("resolvers: --first-boot --set ADDRESS... (once, at its first boot) or --pinned")
    if args.first_boot and st.get("resolvers") is not None:
        raise Refused(f"{s['VM_NAME']}'s resolver was pinned at its first boot; it's never read from the computer again "
                      f"(--pinned uses it)")
    check_firewall(s, st, a)
    hosts = a.set_resolvers(s["VM_NAME"], (args.set or []) if args.first_boot else None)
    write_state(rd, dict(read_state(rd), resolvers=hosts))
    print(" ".join(hosts) or "none")
    return 0


def cmd_plan(s, args):
    """What the build and the CLI need to know, one KEY=VALUE per line: kind (made: a provider makes it; own: your
    server; provider: the provider's own first steps, providers/<name>/build.sh), arch (from the size, for a
    provider's; else GUEST_ARCH or "detect"), label, idle_stop, egress, outside (yes: the provider holds EGRESS outside
    the computer too), billing_stopped (what a stopped computer still bills: storage, full, unknown), soft_stop (api:
    the provider asks the guest to shut down; guest: the CLI asks it over SSH), provider (the adapter's name),
    resolvers, and how the guest is set up there: target (MCOMP_TARGET: remote, or the provider's own), systemd,
    cloud_init, guest_firewall (its own firewall is a layer there), keep_awake (a connection held open while it's set
    up: a computer that sleeps by itself)."""
    a = adapter_for(s, read_state(run_dir(s)))
    made = makes(a)
    arch = "detect"
    common = (f"outside={'yes' if a.manifest['outside_egress']['enforced'] else 'no'}\n"
              f"billing_stopped={a.manifest['billing']['stopped']}\nsoft_stop={getattr(a, 'SOFT_STOP', 'api')}\n"
              f"provider={a.name}\nresolvers={'yes' if getattr(a, 'RESOLVER_EXCEPTION', False) else 'no'}")
    h = hooks_for(a.name, "plan")
    if h is not None:
        print(h.plan(sys.modules[__name__], s, a, common))
        return 0
    if made:
        arch = a.arch(s.get("SIZE") or None)
    elif s.get("GUEST_ARCH"):
        from cloud import ubuntu_vm
        arch = ubuntu_vm.check_arch(s["GUEST_ARCH"])
    idle = (s.get("IDLE_STOP") or "15").strip().lower() if made else "off"
    if idle not in ("off", "0") and not idle.isdigit():
        raise Refused(f"IDLE_STOP={idle}: minutes, or off")
    from cloud import ubuntu_vm
    print(f"kind={'made' if made else 'own'}\narch={arch}\nlabel={a.manifest['label']}\nidle_stop={idle}\n"
          f"egress={ubuntu_vm.check_egress(s.get('EGRESS'))}\n{common}\n"
          "target=remote\nsystemd=yes\ncloud_init=yes\nguest_firewall=yes\nkeep_awake=no")
    return 0


def cmd_key_lists(s, args):
    """The two accounts' key lists (lib/cloud/guest.py key_lists), into DIR: keys-guest and keys-admin."""
    ssh_dir = s["SSH_DIR"]
    guest, admin = cloud_guest.key_lists({k: key_text(ssh_dir, f"{k}.pub") for k in ("ops", "mcp", "view", "admin")})
    for name, body in (("keys-guest", guest), ("keys-admin", admin)):
        with open(os.path.join(args.dir, name), "w") as f:
            f.write(body)
    return 0


def cmd_user_data(s, args):
    sys.stdout.write(user_data(s))
    return 0


def cmd_cloud(args):
    if args.provider not in cloud.ADAPTERS or args.provider in ("local-lume", "fake"):
        raise Refused(f"no provider called {args.provider} ({', '.join(sorted(cloud.providers().values()))})")
    h = hooks_for(args.provider, "cloud")
    if h is not None:
        return h.cloud(sys.modules[__name__], args)
    a = cloud.adapter(args.provider, **provider_kw(args.provider))
    if args.action == "status":
        if a.name == "ubuntu-vm":
            out = {"provider": a.name, "note": "your own servers are listed by metacomputer list --on server"}
        elif hasattr(a.api, "inventory"):
            out = {"provider": a.name, "instances": a.list(), **a.api.inventory()}
        else:
            out = {"provider": a.name, "instances": a.list(), "network": a.api.network(),
                   "boot_volumes": a.api.boot_volumes()}
        if a.name != "ubuntu-vm":
            out["unresolved_launches"] = [e for e in unresolved_launches() if e.get("provider") == a.name]
    elif args.action == "doctor":
        out = {"provider": a.name, "checks": a.doctor()}
    elif args.action == "setup":
        out = a.setup()
    else:   # teardown: the network, only when no computer of metacomputer's has it, or is being made in it
        if a.name == "ubuntu-vm":
            raise Refused("nothing to tear down for your own servers")
        # Under the lock a build makes the network and its computer's group with: a group without an instance yet
        # is a computer being made (or one whose setup was cut off), and the network stays for it.
        with network_lock(a.name):
            # Launches a vm delete left unresolved, first. What shows now under their mark is only a candidate: each
            # is read by its own id and must carry the entry's computer and operation (owned), all of them checked
            # before anything is deleted; any that doesn't stops the teardown, with nothing deleted and the registry
            # as it was. An entry goes once what it made is deleted, or when it's forgotten on purpose (--forget OP,
            # which nothing that shows under its mark lets happen). The registry is changed under its own lock
            # (update_unresolved), dropping only the entries settled here: one added meanwhile stays.
            mine = [e for e in unresolved_launches() if e.get("provider") == a.name]
            forget = getattr(args, "forget", None)
            if forget and not [e for e in mine if e["op"] == forget]:
                raise Refused(f"no unresolved launch of operation {forget} at {a.manifest['label']} "
                              f"(metacomputer cloud status {a.name} lists them): nothing was forgotten")
            plan = []
            for e in mine:
                rows = [i["id"] for i in a.api.instances(e["computer"], op=e["op"])]
                shown = owned_all(a, rows, e["computer"], e["op"], "listed for")
                if forget == e["op"] and shown:
                    raise Refused(f"operation {e['op']} has {len(shown)} {getattr(a, 'thing', 'instance')}(s) at "
                                  f"{a.manifest['label']} now ({', '.join(shown)}): it isn't forgotten while one shows. "
                                  f"metacomputer cloud teardown {a.name} (without --forget) deletes it")
                if shown or forget == e["op"]:
                    plan.append((e, shown))
            settled = []
            for e, shown in plan:
                for iid in shown:
                    a.api.terminate(iid)
                    a.api.wait_state(iid, "TERMINATED", timeout=900)
                forget_parts(a, e["computer"], e["op"])
                settled.append(e["op"])
            if settled:
                update_unresolved(lambda entries: [x for x in entries if x.get("op") not in settled])
            kept = [e for e in unresolved_launches() if e.get("provider") == a.name]
            left = a.list()
            if left:
                raise Refused(f"{a.manifest['label']} still has metacomputer's instances: "
                              f"{', '.join(i['name'] for i in left)} (metacomputer vm delete NAME first)")
            pending = a.api.nsg_names()
            if pending:
                raise Refused(f"a computer is being made in metacomputer's network at {a.manifest['label']} (its "
                              f"security group is there): {', '.join(sorted(pending))}. When its build has ended, or "
                              "if it was cut off: metacomputer vm delete NAME, then this again"
                              + (". A group no computer's record on this Mac names (another Mac's, or one whose "
                                 "record is gone) is never deleted on its name: delete it in "
                                 f"{a.manifest['label']}'s console ({a.GROUPS_IN_CONSOLE}) once you know it's no one's"
                                 if getattr(a, "groups_by_record", False) else ""))
            out = {"provider": a.name, "network_deleted": a.api.delete_network(), "unresolved_settled": settled,
                   "unresolved_left": [e["op"] for e in kept]}
            if hasattr(a.api, "keys"):
                # First-boot keys left by a clean-up that didn't finish (no computer is being made: no firewall is
                # there without its server).
                gone = []
                for k in a.api.keys():
                    m = __import__("re").fullmatch(r"metacomputer-(.+)-[0-9a-f]{12}", k.get("name") or "")
                    if m and a.api.delete_key_by_id(k["id"], m.group(1)):
                        gone.append(k["name"])
                out["keys_deleted"] = gone
    print(json.dumps(out, indent=None if args.json else 2, sort_keys=True, default=str))
    return 0


def cmd_target(args):
    """scripts/lib.sh ssh_base: the transport's options and destination, NUL-separated, from the values bash loaded
    (no second reading of the settings) and the provider's record (the address it gave)."""
    ep = target_endpoint(args)
    wrapper = WRAPPER
    h = transport_hooks(ep["transport"], "connect_wrapper")
    if h is not None:
        # A provider's tunnel: every connection goes to the computer's own, or none, checked now and again by the
        # ProxyCommand right before each connection's tunnel starts (lib/proxywrap.py --id), so arguments a caller
        # keeps (scripts/lib.sh keeps them for the rest of its command, the hub for its life) never reach one made
        # again under the name.
        wrapper = (*WRAPPER, *h.connect_wrapper(sys.modules[__name__], args, ep))
    out = transport.ssh_target(ep, args.user, wrapper=wrapper)
    sys.stdout.write("\0".join(out) + "\0")
    return 0


def transport_hooks(name, need):
    """The hooks of the provider whose transport `name` is (its manifest's transport), when it has `need`; else None."""
    prov = manifests.by_transport(name)
    return hooks_for(prov, need) if prov else None


def transport_settings(args):
    """The settings a provider's transport reads (--setting KEY=VALUE, each a name its manifest's transport_settings
    lists, given once): {KEY: VALUE}; Refused otherwise."""
    allowed, out = set(manifests.transport_settings()), {}
    for kv in getattr(args, "setting", None) or []:
        k, eq, v = kv.partition("=")
        if not eq or k not in allowed or k in out:
            raise Refused(f"--setting {k[:64]!r}: not a setting of a provider's transport, or given twice")
        out[k] = v
    return out


def setting(args, key):
    """One of the transport's settings (transport_settings), or "" when it wasn't given."""
    return transport_settings(args).get(key, "")


def target_endpoint(args):
    """The endpoint from the values bash loaded (target, tunnel, probe) and the provider's record."""
    state = read_state(args.run_dir) if args.run_dir else {}
    settings = {"VM_NAME": args.vm, "TRANSPORT": args.transport, "SSH_HOST": args.host, "SSH_HOST_PORT": args.port,
                **{k: "" for k in manifests.transport_settings()}, **transport_settings(args)}
    return transport.endpoint(settings, {"host": state.get("host")} if state.get("host") else None)


def cmd_tunnel(args):
    """What the vault's binding pins for a computer behind a provider's tunnel, as `metacomputer secrets bind` hands it
    to the vault, one argument a line: --tunnel PROGRAM and --tunnel-arg=ARG for each of its arguments (the program
    that carries its SSH: lib/cloud/transport.py endpoint's proxy), --tunnel-id ID (the provider's own id for it,
    recorded when it was made) and --tunnel-check-arg=ARG for each argument with which the same program asks the
    provider about it (an answer in JSON whose "id" must be that one). All of them together, from one look at the
    computer's own (its provider's tunnel hook checks it), each checked to be a word the vault takes, or nothing and
    a refusal: never a binding of the name as an address. Nothing for a computer reached straight over SSH."""
    ep = target_endpoint(args)
    if not ep["proxy"]:
        return 0
    h = transport_hooks(ep["transport"], "tunnel")
    if h is None:
        raise Refused(f"the {ep['transport']} transport gives the vault no provider id to check: nothing to bind")
    proxy, chk, tid = h.tunnel(sys.modules[__name__], args, ep)
    word = re.compile(r"[A-Za-z0-9._:/@=+_-]{1,200}")       # vault/Store.swift validTunnelArg
    if not (proxy and proxy[0].startswith("/") and all(word.fullmatch(x) for x in proxy)
            and len(chk) > 1 and chk[0] == proxy[0] and all(word.fullmatch(x) for x in chk)
            and isinstance(tid, str) and re.fullmatch(r"[A-Za-z0-9_-]{4,80}", tid)):
        raise Refused(f"{args.vm}'s tunnel, or the provider's id for it, isn't one the vault can pin: nothing to bind")
    out = ["--tunnel", proxy[0], *[f"--tunnel-arg={x}" for x in proxy[1:]], "--tunnel-id", tid,
           *[f"--tunnel-check-arg={x}" for x in chk[1:]]]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def cmd_probe(args):
    """Whether the computer's SSH port answers (a TCP connection, a few seconds at most): reachable or unreachable.
    A computer reached through its provider's tunnel can't be probed this way: unknown."""
    import socket
    try:
        ep = target_endpoint(args)
    except AdapterError:
        print("unknown")
        return 0
    if ep["proxy"] is not None:
        # A connection through a provider's tunnel would wake (and bill) a computer that sleeps: a provider's own API
        # may say asleep or awake instead, without waking it (its transport's probe hook); any other can't be told.
        h = transport_hooks(args.transport, "probe")
        print(h.probe(sys.modules[__name__], args, ep) if h is not None else "unknown")
        return 0
    try:
        with socket.create_connection((ep["host"], ep["port"]), timeout=args.timeout) as c:
            c.settimeout(args.timeout)
            ok = c.recv(4).startswith(b"SSH-")
    except OSError:
        ok = False
    print("reachable" if ok else "unreachable")
    return 0


def cmd_setup_done(s, args):
    """The build's end: this computer is set up, so the setup's exception to the network policy's rule (require_narrowing)
    is over for good."""
    rd = run_dir(s)
    st = read_state(rd)
    if not st.get("instance"):
        raise Refused(f"{s['VM_NAME']} has no instance on record")
    write_state(rd, dict(st, setup_done=now()))
    return 0


def cmd_egress(s, args):
    """EGRESS outside the computer (its provider's rules); the guest's own firewall is set over SSH by the CLI."""
    st = read_state(run_dir(s))
    a = adapter_for(s, st)
    if not a.manifest["outside_egress"]["enforced"]:
        print("none: nothing outside the computer enforces its egress here (only its own firewall)")
        return 0
    if args.set:
        h = hooks_for(a.name, "egress_check")
        if h is not None:
            h.egress_check(sys.modules[__name__], s, a, st, args)
        check_firewall(s, st, a)
        out = a.apply_egress(s["VM_NAME"], args.set)
    else:
        out = a.egress(s["VM_NAME"])
    print(out.get("outside") or "unknown")
    return 0


def identity_text(s, st):
    """What the computer is: its provider's instance (or, for your own server, the setup that pinned it) and its
    pinned host key, hashed. The vault's binding of it must name the same; a computer deleted, or made again under
    the same name, has another."""
    import hashlib
    with open(os.path.join(s["SSH_DIR"], "known_hosts")) as f:
        line = next((ln.strip() for ln in f if ln.startswith(f"mcomp-{s['VM_NAME']} ")), "")
    if not line:
        raise Refused("its host key isn't pinned yet")
    basis = st.get("instance") or st.get("setup_id")
    if not basis:
        raise Refused("it has no instance or setup on record")
    return hashlib.sha256(f"{s['VM_NAME']}\0{basis}\0{line}".encode()).hexdigest()


def cmd_identity(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    p = os.path.join(s["SSH_DIR"], "computer-id")
    if args.remove:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        return 0
    if not st.get("instance") and not st.get("setup_id"):
        import uuid
        st["setup_id"] = uuid.uuid4().hex        # your own server: this setup of it (vm delete forgets it)
        write_state(rd, st)
    ident = identity_text(s, st)
    fd, tmp = tempfile.mkstemp(prefix=".computer-id.", dir=s["SSH_DIR"])
    with os.fdopen(fd, "w") as f:
        f.write(ident + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    print(ident)
    return 0


def provider_command(s, args):
    """A command only some providers have (exec, checkpoint, allow): run by the computer's provider's own hook
    (providers/<name>/remote.py COMMANDS), or refused, naming the providers that have it."""
    st = read_state(run_dir(s))
    a = adapter_for(s, st)
    h = hooks_for(a.name, "COMMANDS")
    if h is None or args.cmd not in h.COMMANDS:
        have = [n for n in cloud.registry.PROVIDERS if args.cmd in getattr(hooks_for(n, "COMMANDS"), "COMMANDS", {})]
        what = next((getattr(hooks_for(n), "COMMANDS")[args.cmd][0] for n in have), args.cmd)
        labels = manifests.labels_where(lambda m: m["name"] in have) or "no provider here"
        raise Refused(f"{what} is for a computer on {labels}; {s['VM_NAME']} is on {a.manifest['label']}")
    return h.COMMANDS[args.cmd][1](sys.modules[__name__], s, a, st, args)


def cmd_missing(args):
    """Why a feature can't be done on this provider's computers (lib/cloud/capabilities.py's feature map)."""
    from cloud import capabilities
    cls = cloud.ADAPTERS.get(args.provider)
    if cls is None:
        raise Refused(f"no provider {args.provider}")
    feature = {"baseline": "reset", "checkpoints": "checkpoint"}.get(args.feature, args.feature)
    if feature not in capabilities.FEATURES:
        raise Refused(f"no feature {args.feature}")
    print(capabilities.missing(cls.manifest, feature) or "")
    return 0


VM_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")     # a computer's name (scripts/lib.sh mc_valid_vm_name)
# The subcommands that change a computer (its power, disks, checkpoints, network rules, what runs in it, or whether it
# exists), and the forms of the others that do: tests/acting.py's table names the same.
CHANGES = ("exec", "create", "wake", "sleep", "delete", "abort", "resolvers")


def changes_computer(a):
    if a.cmd in CHANGES:
        return True
    if a.cmd == "checkpoint":
        return a.action != "list"
    if a.cmd == "allow":
        return not a.show
    if a.cmd == "egress":
        return bool(a.set)
    return False


def shared_network_computers(a):
    """For `cloud setup PROVIDER` and `create --vm NAME`: this checkout's computers at that provider (config/vms/, by
    their PROVIDER) when its adapter's setup and creates rewrite a network they share; [] otherwise."""
    if not ((a.cmd == "cloud" and a.action == "setup") or a.cmd == "create"):
        return []
    import labconfig
    try:
        comps = [c for c in labconfig.computers(LAB) if c["file"].startswith(os.path.join(LAB, "config", "vms") + os.sep)]
    except Exception as e:      # noqa: BLE001  (settings that can't be read: which computers share it can't be told)
        import handoff_gate
        handoff_gate.refuse(f"couldn't read this checkout's computers' settings ({e}), so the handoffs of the computers "
                            "that share the provider's network can't be checked; nothing was done")
    prov = a.provider if a.cmd == "cloud" else next((c["provider"] for c in comps if c["name"] == a.vm), "")
    cls = cloud.ADAPTERS.get(prov)
    if not cls or not getattr(cls, "SHARED_NETWORK_CHANGES", False):
        return []
    return [c for c in comps if c["provider"] == prov]


def no_teardown(prov):
    """Whether `cloud teardown PROVIDER` has nothing to remove there (its manifest says so)."""
    return prov in cloud.registry.PROVIDERS and not manifests.cli(prov)["teardown"]


def main(argv=None):
    """The command line. Everything it prints, on standard output and error, passes no_creds (NoCreds)."""
    real = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = NoCreds(real[0]), NoCreds(real[1])
    try:
        return run(argv)
    finally:
        sys.stdout, sys.stderr = real


def run(argv=None):
    ap = strictargs.Parser(prog="remote.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("target", "tunnel", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--vm", required=True)
        p.add_argument("--user", required=name == "target", default="")
        p.add_argument("--run-dir", default="")
        p.add_argument("--transport", default="ssh")
        p.add_argument("--host", default="")
        p.add_argument("--port", default="22")
        p.add_argument("--setting", action="append", default=[], help="KEY=VALUE: a setting the provider's transport "
                       "reads (its manifest's transport_settings)")
        if name == "probe":
            p.add_argument("--timeout", type=float, default=4.0)
    p = sub.add_parser("exec")
    p.add_argument("--vm", required=True)
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--max-output", type=int, default=1 << 20, help="bytes of output at most (the command is ended past it)")
    p.add_argument("--stdin", action="store_true", help="pass this command's standard input on (else none)")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p = sub.add_parser("checkpoint")
    p.add_argument("--vm", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("action", choices=("list", "create", "restore", "delete"))
    p.add_argument("label", nargs="?")
    p.add_argument("--replace", action="store_true", help="create: then delete the older checkpoints of that name")
    p = sub.add_parser("allow")
    p.add_argument("--vm", required=True)
    p.add_argument("--exact", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--remove", action="store_true")
    g.add_argument("--clear", action="store_true")
    g.add_argument("--show", action="store_true")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("egress")
    p.add_argument("--vm", required=True)
    p.add_argument("--set", choices=("internet", "off"))
    p.add_argument("--setup", action="store_true", help="the build's own opening for its downloads (a provider whose "
                   "policy the setup opens)")
    p = sub.add_parser("setup-done")
    p.add_argument("--vm", required=True)
    p = sub.add_parser("identity")
    p.add_argument("--vm", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--remove", action="store_true")
    p = sub.add_parser("resolvers")
    p.add_argument("--vm", required=True)
    p.add_argument("--set", nargs="*", default=[])
    p.add_argument("--first-boot", action="store_true")
    p.add_argument("--pinned", action="store_true")
    p = sub.add_parser("missing")
    p.add_argument("--provider", required=True)
    p.add_argument("--feature", required=True)
    p = sub.add_parser("key-lists")
    p.add_argument("--vm", required=True)
    p.add_argument("--dir", required=True)
    for name in ("create", "wake", "sleep", "delete", "status", "state", "rates", "user-data", "plan", "abort"):
        p = sub.add_parser(name)
        p.add_argument("--vm", required=True)
        p.add_argument("--json", action="store_true")
        if name in ("create", "abort"):
            p.add_argument("--op", required=name == "abort")
        if name == "abort":
            p.add_argument("--keep", action="store_true")
        if name == "status":
            p.add_argument("--offline", action="store_true", help="the record only: the provider isn't asked")
        if name == "delete":
            p.add_argument("--network-too", action="store_true")
            p.add_argument("--checked-provider", action="store_true",
                           help="an unresolved launch: you've looked at the provider (it stays listed by cloud status)")
        if name == "sleep":
            p.add_argument("--force", action="store_true")
            p.add_argument("--asked", action="store_true", help="the CLI asked the guest to power off over SSH")
    p = sub.add_parser("cloud")
    p.add_argument("action", choices=("status", "doctor", "setup", "teardown"))
    p.add_argument("provider")
    p.add_argument("--json", action="store_true")
    for opt, env in manifests.cloud_options():          # a provider's own (its manifest's cloud_options)
        p.add_argument(opt, default=os.environ.get(env, ""))
    p.add_argument("--forget", default=None, help="teardown: an unresolved launch's operation you've dealt with")
    a = ap.parse_args(argv)
    take_credentials()
    # What deletes at a provider whatever computer it was (teardown), or forgets a launch that may have made one, is
    # the user's own call at their own terminal, run this way as through `metacomputer cloud` (a provider with nothing
    # to tear down, its manifest's teardown False: cmd_cloud says so).
    if a.cmd == "cloud" and (a.action == "teardown" or a.forget is not None) and not no_teardown(a.provider):
        import owners
        if not owners.at_user_terminal((0,)):
            print(f"metacomputer: cloud {a.action} {a.provider}{' --forget' if a.forget is not None else ''} deletes "
                  "what metacomputer left at the provider (servers included) or forgets a launch that may have made "
                  "one, so it's yours to run: in your own terminal (not through an agent). Nothing was changed",
                  file=sys.stderr)
            return 3
    # While the user has the computer (a handoff), what changes it waits (lib/handoff_gate.py): exit 4 before any
    # provider is asked anything or any record is written, except at the user's own terminal or under the hold of the
    # command that runs this (bin/metacomputer, scripts/remote-build.sh). Its RUN_DIR is fixed by its name.
    if changes_computer(a):
        if not VM_NAME_RE.fullmatch(a.vm):
            print(f"metacomputer: '{a.vm[:64]}' isn't a computer's name; nothing was done", file=sys.stderr)
            return 2
        import handoff_gate
        handoff_gate.wait_or_hold(os.path.join(LAB, ".run-vms", a.vm, "owners"))
    # A provider whose setup and every create rewrite a network its other computers share too (its adapter says so:
    # SHARED_NETWORK_CHANGES): each of this checkout's computers there waits as well, taken in order.
    shared = shared_network_computers(a)
    if shared:
        import handoff_gate
        handoff_gate.wait_or_hold_all([os.path.join(c["run_dir"], "owners") for c in shared])
    try:
        if a.cmd == "target":
            return cmd_target(a)
        if a.cmd == "tunnel":
            return cmd_tunnel(a)
        if a.cmd == "probe":
            return cmd_probe(a)
        if a.cmd == "missing":
            return cmd_missing(a)
        if a.cmd == "cloud":
            return cmd_cloud(a)
        s = settings_for(a.vm)
        return {"create": cmd_create, "abort": cmd_abort, "wake": cmd_wake, "sleep": cmd_sleep, "delete": cmd_delete,
                "status": cmd_status, "state": cmd_state, "rates": cmd_rates, "user-data": cmd_user_data,
                "plan": cmd_plan, "key-lists": cmd_key_lists, "egress": cmd_egress, "resolvers": cmd_resolvers,
                "identity": cmd_identity, "exec": provider_command, "checkpoint": provider_command,
                "allow": provider_command, "setup-done": cmd_setup_done}[a.cmd](s, a)
    except (Refused, AdapterError) as e:
        print(f"metacomputer: {no_creds(e)}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"metacomputer: {no_creds(e)}", file=sys.stderr)
        return 1
    except Exception as e:      # noqa: BLE001  (the provider's SDK: said in one line, without its request details)
        if type(e).__name__ != "ServiceError":
            raise
        print(no_creds(f"metacomputer: the provider refused: {getattr(e, 'status', '?')} {getattr(e, 'code', '')}: "
                       f"{no_creds(getattr(e, 'message', '') or '')[:300]}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
