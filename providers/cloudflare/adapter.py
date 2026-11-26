"""Cloudflare Containers (`cloudflare`, place `cloudflare`): metacomputer puts a control Worker into the user's own
Cloudflare account for each computer, whose Durable Object runs the computer's container (an Ubuntu 24.04 microVM), and
drives it through that Worker; everything that talks to the desktop is the generic remote computer's
(lib/cloud/ubuntu_vm.py RemoteVM): SSH with metacomputer's own keys, the host key pinned as mcomp-NAME.

What's deployed for a computer, all named after its Worker (CLOUDFLARE_WORKER: metacomputer-NAME unless vm create
--worker names it), on workers.dev only, never a route on the account's domains:
- the Worker (providers/cloudflare/worker/: index.js and logic.js), its secret the computer's own key, its plain-text
  bindings the computer's name, its build's operation and its size;
- its Durable Object namespace (class Computer) and its container application (Cloudflare's own SSH off);
- a repository in the account's container registry holding Ubuntu's image (pinned by digest) and, Cloudflare's doing,
  the computer's snapshots (which can't be deleted: each expires 30 days after its last use).

Two credentials, for two kinds of work:
- the account's API token (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID), for vm create and vm delete only: the REST API
  (providers/cloudflare/api.py) and the registry (images.py);
- the computer's own key (64 hex digits, made on the Mac at create, kept 0600 beside its SSH keys), for everything
  else: start, stop, checkpoints, egress, the boot script, the logs, and SSH through the broker. The Worker checks every
  request's HMAC with it (worker/logic.js verify); after the setup it refuses arbitrary commands.

What a computer is there, as metacomputer found it (docs/cloudflare.md):
- x86_64, a microVM per container; no systemd, no /dev/uinput (foreground input), sshd on port 2222 (the platform's
  TCP route reaches the container's own address, not its loopback).
- A stop snapshots the disk while the container still runs (the desktop's programs ended first), then destroys it: files
  are kept, programs aren't. A start restores the latest snapshot (about 0.2 s), then runs the boot script.
- Cloudflare can stop a container without warning (a host event): what changed since its last snapshot is lost; the
  Worker sees it as a stop by Cloudflare, and status says so.
- The idle stop is the Worker's (IDLE_STOP minutes after the last connection closed: a snapshot, then the stop), with
  the container's own inactivity timeout (IDLE_STOP + 10 minutes, 6 hours at most) under it.

Identity: the computer's Worker by its name, whose Durable Object's id (fixed for that Worker; one deleted and made
again under the name has another) is recorded with its build's operation: instance = <worker>-<first 16 hex digits of
the object's id>. Every operation through the Worker checks its answer names this computer, its operation and that
object (_live); the token's operations check the Worker's bindings (owns_script)."""
import contextlib
import re
import time

from cloud import transport
from cloud.adapter import AdapterError, NotSupported, check_name
from cloud.ubuntu_vm import RemoteVM, check_egress

from .manifest import DEFAULT_SIZE, IMAGE_DIGEST, MANIFEST, SIZES

WORKER_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,52}[a-z0-9])?")
OP_RE = re.compile(r"[0-9a-f]{32}")
DO_ID = re.compile(r"[0-9a-f]{64}")
DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?=[a-z0-9-]*[a-z])[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
MAX_NAMES = 63
IDLE_MAX = 350
STOP_KEEPS = "files, packages and settings are kept; every program closes (open apps are closed)"
DAY_MS = 86400000
EXPIRE_DAYS = 30
# Cloudflare's list prices (USD, developers.cloudflare.com/containers/platform/pricing, read 2026-11-26), past what
# Workers Paid includes each month (25 GiB-hours of memory, 375 vCPU-minutes, 200 GB-hours of disk): memory and disk
# as provisioned while it runs, CPU as used. Snapshots: free during the beta.
MEM_GIB_HOUR = 0.009
CPU_HOUR = 0.072
DISK_GB_HOUR = 0.000252
DESKTOP_CPU = 0.1
STATES = {"running": "awake", "stopped": "stopped", "never": "stopped", "stopping": "stopping"}


def worker_of(settings):
    """The computer's Worker: CLOUDFLARE_WORKER, or metacomputer-NAME (lowercased; dots and underscores as dashes)."""
    n = (settings.get("CLOUDFLARE_WORKER") or "").strip()
    if not n:
        n = "metacomputer-" + re.sub(r"[^a-z0-9-]", "-", (settings.get("VM_NAME") or "").lower()).strip("-")
    if not WORKER_NAME.fullmatch(n):
        raise AdapterError(f"CLOUDFLARE_WORKER {n!r} isn't a Worker's name metacomputer takes (lowercase letters, digits "
                           "and dashes, 54 at most)")
    return n


def instance_id(worker, do_id):
    """The computer's identity: its Worker's name and its Durable Object's id (a word the tunnel's check and the
    vault's binding take: [A-Za-z0-9_-], 80 at most)."""
    if not isinstance(do_id, str) or not DO_ID.fullmatch(do_id):
        raise AdapterError("the Worker didn't say its Durable Object's id")
    return f"{worker_of({'CLOUDFLARE_WORKER': worker})}-{do_id[:16]}"


def parse_instance(iid):
    """(worker, the first 16 hex digits of its object's id) of an instance id, or (None, None)."""
    m = re.fullmatch(r"([a-z0-9](?:[a-z0-9-]{0,52}[a-z0-9])?)-([0-9a-f]{16})", str(iid or ""))
    return (m.group(1), m.group(2)) if m else (None, None)


def broker_paths(lab, vm):
    """The computer's broker's socket (in a private folder of this account's: a Unix socket's path is limited to about
    104 bytes) for checkout `lab` and computer `vm`: the same for every program that asks."""
    import hashlib
    import os
    h = hashlib.sha256(f"{os.path.realpath(lab)}:{check_name(vm)}".encode()).hexdigest()[:16]
    d = f"/tmp/mcomp-{os.getuid()}-cloudflare"
    return d, os.path.join(d, h + ".sock")


def key_path(settings):
    """Where the computer's own key is kept: beside its SSH keys (0600, in their 0700 folder)."""
    import os
    return os.path.join(settings["SSH_DIR"], "cloudflare-key")


def opener(settings, state=None):
    """The `cloudflare` transport: ssh through providers/cloudflare/tunnel.py, which asks the computer's broker (the one
    that holds its key) to carry the connection to the container's sshd through the Worker. ssh's host name is the
    Worker's (the tunnel carries it; the host key is pinned under mcomp-NAME)."""
    import os
    worker = worker_of(settings)
    lab = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _d, sock = broker_paths(lab, settings["VM_NAME"])
    tunnel = os.path.join(lab, "providers", "cloudflare", "tunnel.py")
    return {"host": worker, "port": 22, "proxy": [tunnel, "relay", "--sock", sock, "--worker", worker]}


transport.register("cloudflare", opener)


# --- the egress, as policies (a computer's EGRESS, or an allowlist of names) ----------------------------------------
def policy_for(egress=None, allow=None):
    """{"internet": bool, "allow": [names]}: EGRESS=internet (the platform's internet: private ranges and the metadata
    address refused), off (nothing), or an allowlist of exact names (the internet off; each name through metacomputer's
    handler). ValueError for a name that isn't one."""
    if allow is not None:
        names = []
        for a in allow:
            d = str(a).strip().lower().rstrip(".")
            if d.startswith("*."):
                raise ValueError(f"{a!r}: Cloudflare's outbound handlers take exact names (no wildcards): list each name")
            if not DOMAIN.fullmatch(d) or d == "metacomputer.invalid":
                raise ValueError(f"{a!r} isn't a DNS name (an address can't be allowed by name)")
            names.append(d)
        names = list(dict.fromkeys(names))
        if not names or len(names) > MAX_NAMES:
            raise ValueError(f"an allowlist has 1 to {MAX_NAMES} names")
        return {"internet": False, "allow": names}
    if check_egress(egress) == "internet":
        return {"internet": True, "allow": []}
    return {"internet": False, "allow": []}


def policy_kind(p):
    """("internet" | "off" | "allowlist" | "other", names) of a policy, or of what the Worker says is set or in force
    ({"internet": bool, "names": {name: "allow"|"deny"}} or {"setting", "allow"})."""
    if not isinstance(p, dict):
        return "other", []
    if "names" in p:
        if not isinstance(p.get("internet"), bool) or not isinstance(p.get("names"), dict):
            return "other", []
        if p["internet"]:
            return ("internet", []) if not p["names"] else ("other", [])
        allowed = sorted(n for n, k in p["names"].items() if k == "allow")
        if any(k not in ("allow", "deny") for k in p["names"].values()):
            return "other", []
        return ("allowlist", allowed) if allowed else ("off", [])
    if isinstance(p.get("internet"), bool) and isinstance(p.get("allow"), list):
        if p["internet"]:
            return ("internet", []) if not p["allow"] else ("other", [])
        return ("allowlist", list(p["allow"])) if p["allow"] else ("off", [])
    return "other", []


def breadth(kind, names):
    if kind == "internet":
        return frozenset({"*"})
    if kind == "off":
        return frozenset()
    if kind == "allowlist":
        return frozenset(names)
    return False


def widens(cur_kind, cur_names, new_policy):
    """Whether `new_policy` lets out anything what's in force doesn't (judged on where a change ends, never on the
    step). What's in force but can't be told counts as the narrowest."""
    new_kind, new_names = policy_kind(new_policy)
    new, cur = breadth(new_kind, new_names), breadth(cur_kind, cur_names)
    if new == frozenset():
        return False
    if cur is False or new is False:
        return True
    if "*" in cur:
        return False
    return "*" in new or not new <= cur


def refresh_words(trouble):
    """The Worker's refresh trouble (logic.js refreshTrouble), in words: a kept snapshot whose renewal keeps failing, by
    the names it's kept under, with its failures, its last error and the date it expires; or one that expired."""
    out = []
    for t in trouble or []:
        if not isinstance(t, dict):
            continue
        names = "/".join(t.get("labels") or []) or "a kept snapshot"
        if t.get("expired"):
            out.append(f"{names}: its snapshot expired on {days(t.get('expires'))} (Cloudflare no longer has it)")
        else:
            out.append(f"{names}: its snapshot couldn't be renewed ({int(t.get('failures') or 0)} tries; last: "
                       f"{str(t.get('error') or 'no answer')[:120]}): tried again until then, but it expires on "
                       f"{days(t.get('expires'))} unless one works"
                       + ("; a start and a stop make a new latest one" if "latest" in (t.get("labels") or []) else ""))
    return out


def days(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000.0)) if isinstance(ms, (int, float)) and ms > 0 else "?"


class Facade:
    """What lib/remote.py asks of a provider's facade before it starts, stops or changes the rules of a computer
    (owned: the instance read now must carry the computer's name and operation), and its scrub. The Worker answers
    (with the computer's key); without one, the account's API does (the Worker's bindings)."""

    def __init__(self, adapter):
        self._a = adapter

    def scrub(self, text, cap=300):
        a = self._a
        for x in (a._worker, a._rest):
            if x is not None:
                return x.scrub(text, cap)
        from cloud.vps import static_scrub
        return static_scrub(text, cap)

    def instance(self, iid):
        a = self._a
        worker, do16 = parse_instance(iid)
        if worker is None:
            return None
        if a._worker is not None:
            try:
                s = a._worker.call("status", {}, 60)
            except Exception as e:      # noqa: BLE001  (the Worker gone, or another under the name)
                code = getattr(e, "status", None)
                if code == 404:
                    return None
                if code == 401:
                    return {"id": iid, "name": "(another Worker, made since under its name)", "op": "", "state": "RUNNING"}
                raise AdapterError(f"the control Worker couldn't be asked ({a._worker.scrub(e)})") from None
            if not str(s.get("do") or "").startswith(do16):
                return {"id": iid, "name": "(another Worker, made since under its name)", "op": "", "state": "RUNNING"}
            return {"id": iid, "name": s.get("computer"), "op": s.get("op"),
                    "state": "RUNNING" if s.get("running") else "STOPPED"}
        settings = a.rest.script_settings(worker)
        if settings is None:
            return None
        return {"id": iid, "name": a.rest.plain(settings, "MCOMP_COMPUTER"), "op": a.rest.plain(settings, "MCOMP_OP"),
                "state": "UNKNOWN"}


class Cloudflare(RemoteVM):
    """Computers whose control Worker metacomputer deploys into a Cloudflare account. `rest` (api.py) and `images`
    (images.py) need the account's token (create, delete); `worker` (wire.py Worker) needs the computer's key (the rest).
    lib/remote.py makes them (tests: stand-ins); without the one an operation needs, it's refused, saying which."""
    name = "cloudflare"
    manifest = MANIFEST
    SOFT_STOP = "api"

    def __init__(self, computers=None, states=None, rest=None, images=None, worker=None):
        super().__init__(computers, states)
        self._rest, self._images, self._worker = rest, images, worker

    @property
    def api(self):
        return Facade(self)

    @property
    def rest(self):
        if self._rest is None:
            raise AdapterError("this needs the Cloudflare account's API token: CLOUDFLARE_API_TOKEN and "
                               "CLOUDFLARE_ACCOUNT_ID in this command's environment (docs/cloudflare.md)")
        return self._rest

    @property
    def images(self):
        if self._images is None:
            raise AdapterError("this needs the Cloudflare account's API token (the registry): CLOUDFLARE_API_TOKEN and "
                               "CLOUDFLARE_ACCOUNT_ID in this command's environment")
        return self._images

    @property
    def worker(self):
        if self._worker is None:
            raise AdapterError("this computer's control Worker key isn't on this Mac (it's made by vm create, beside "
                               "its SSH keys): nothing was asked")
        return self._worker

    def attach_worker(self, worker):
        self._worker = worker

    @classmethod
    def size_of(cls, size):
        s = (size or DEFAULT_SIZE).strip()
        if s not in SIZES:
            raise AdapterError(f"--size {s}: a container's size is {', '.join(SIZES)} (vCPUs/GiB/GB: "
                               + ", ".join(f"{k} {v[0]}/{v[1]}/{v[2]}" for k, v in SIZES.items()) + ")")
        return s

    @classmethod
    def region_of(cls, region):
        if (region or "").strip():
            raise AdapterError("--region: Cloudflare places a container itself (near the request that starts it)")
        return ""

    @classmethod
    def idle_of(cls, v):
        v = (v or "15").strip().lower()
        if not v.isdigit() or not 1 <= int(v) <= IDLE_MAX:
            raise AdapterError(f"IDLE_STOP={v}: minutes, 1 to {IDLE_MAX} (Cloudflare stops an idle container after 6 "
                               "hours at most)")
        return int(v)

    def _worker_name(self, name):
        return worker_of(self._settings(name))

    def _state(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            raise AdapterError(f"{name} has no control Worker that this Mac knows of (metacomputer cloud status "
                               "cloudflare)")
        return st

    def ask(self, verb, body=None, timeout=900):
        """One request to the computer's Worker; refusals in words."""
        try:
            return self.worker.call(verb, body or {}, timeout)
        except AdapterError:
            raise
        except Exception as e:      # noqa: BLE001  (wire.WorkerError, a connection that failed)
            code = getattr(e, "status", None)
            if code == 401:
                raise AdapterError("the control Worker refuses this computer's key: it isn't the Worker this computer "
                                   "was made with (deleted and made again since?); nothing was done") from None
            if code == 404:
                raise AdapterError("the control Worker isn't there (deleted at Cloudflare?): nothing was done "
                                   "(metacomputer vm delete forgets it)") from None
            raise AdapterError(f"the control Worker refused or didn't answer ({self.worker.scrub(e)})") from None

    def _live(self, name):
        """(the Worker's status, this Mac's record) of the computer's own Worker, checked before anything acts on it: its
        name is still the settings', and the Worker answers for this computer, its build's operation and the Durable
        Object recorded for it. AdapterError otherwise: nothing is done."""
        worker = self._worker_name(name)
        st = self._state(name)
        rec_worker, do16 = parse_instance(st["instance"])
        if rec_worker != worker:
            raise AdapterError(f"{name}'s settings name the Worker {worker}, but it was made as {rec_worker}: nothing "
                               "was done")
        s = self.ask("status", {}, 60)
        if not self.owns(name, s, st):
            raise AdapterError(f"the Worker named {worker} doesn't answer for {name} (another computer's, or made again "
                               "since): metacomputer leaves it alone, and nothing was done")
        return s, st

    @staticmethod
    def owns(name, s, st):
        """THE ownership check through the Worker: it answers for this computer, its build's operation and the Durable
        Object recorded for it."""
        _w, do16 = parse_instance(st.get("instance"))
        return (do16 is not None and isinstance(s, dict) and s.get("computer") == name
                and str(s.get("do") or "").startswith(do16) and DO_ID.fullmatch(str(s.get("do") or ""))
                and (not st.get("op") or s.get("op") == st["op"]))

    @staticmethod
    def owns_script(name, settings, op, rest):
        """THE ownership check through the account's API (delete, abort, a lost launch): the Worker's bindings name this
        computer and the operation that made it."""
        return (settings is not None and rest.plain(settings, "MCOMP_COMPUTER") == name
                and bool(op) and rest.plain(settings, "MCOMP_OP") == op)

    # lifecycle ------------------------------------------------------------------------------------------------------
    def create(self, name, region=None, size=None, persist="auto", op=None, key=None, modules=None, made=None, log=None,
               guard=None):
        """The computer's control Worker, deployed: Ubuntu's image into the account's registry (by digest), the image
        prepared to run, the Worker uploaded (sent once; its bindings carry the computer and this operation; its secret
        the computer's key), its workers.dev address on, its container application made with Cloudflare's SSH off and
        read back. Refused when a Worker of that name exists (never someone else's): checked first, and again right
        before the upload, under `guard(worker)` (lib's lock on this Mac, held from that check through the upload's
        read-back: Cloudflare's upload has no condition to send it under, so it replaces whatever is there). After the
        upload, the Worker's bindings must name this operation: another's (a create elsewhere that won the race) is left
        alone and refused. `made(what, value)` journals each part as it's made. Returns {worker, url, namespace, repo,
        digest, size}."""
        from cloud import capabilities
        worker = self._worker_name(name)
        if not OP_RE.fullmatch(op or ""):
            raise AdapterError("a computer's Worker is made by a build's operation (32 hex digits)")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise AdapterError("the computer's key isn't one metacomputer made")
        self.region_of(region)
        size = self.size_of(size)
        try:
            capabilities.choose_persist(self.manifest, persist)
        except capabilities.Unavailable as e:
            raise NotSupported(self.name, "PERSIST", str(e)) from None
        made = made or (lambda what, value: None)
        say = log or (lambda m: None)
        rest = self.rest
        if rest.script_settings(worker) is not None:
            raise AdapterError(f"the Cloudflare account has a Worker named {worker} already; metacomputer leaves it "
                               "alone (choose another name: vm create --worker NAME)")
        repo = worker
        made("made_image", {"repo": repo, "digest": IMAGE_DIGEST})
        t0 = time.monotonic()
        self.images.copy_image(repo, IMAGE_DIGEST, log=say)
        ref = rest.image_ref(repo, IMAGE_DIGEST)
        for _ in range(450):
            status, why = rest.prepare_image(ref)
            if status != "pending":
                break
            time.sleep(2)
        if status != "ready":
            raise AdapterError(f"Cloudflare didn't prepare the image to run ({status or 'no answer'}{': ' + why if why else ''})")
        say(f"the image is in the account's registry and ready to run ({round(time.monotonic() - t0, 1)} s)")
        with (guard or (lambda w: contextlib.nullcontext()))(worker):
            # The image's copy and preparation take minutes: another create may have made a Worker of this name since.
            if rest.script_settings(worker) is not None:
                raise AdapterError(f"a Worker named {worker} appeared in the Cloudflare account while the image was "
                                   "prepared (another vm create, here or elsewhere?): metacomputer leaves it alone and "
                                   "uploaded nothing (choose another name: vm create --worker NAME)")
            made("worker_sent", worker)
            rest.upload(worker, modules, {"MCOMP_COMPUTER": name, "MCOMP_OP": op, "MCOMP_SIZE": size}, key, ref, size)
            got = rest.script_settings(worker)
        if got is not None and not self.owns_script(name, got, op, rest):
            raise AdapterError(f"the Worker named {worker} carries another computer's or operation's bindings just after "
                               "this upload (another vm create replaced it): metacomputer leaves it alone, and this "
                               "computer wasn't made")
        made("made_worker", worker)
        rest.enable_workers_dev(worker)
        ns = None
        for _ in range(10):
            ns = rest.namespace_of(worker)
            if ns:
                break
            time.sleep(2)
        if not ns:
            raise AdapterError("Cloudflare made the Worker but not its Durable Object namespace")
        made("namespace", ns)
        app = rest.application(ns)
        if app is None:
            app = rest.create_application(worker, ns)
            app = rest.application(ns) or app
        if app.get("name") != worker:
            raise AdapterError("the container application of the Worker's namespace isn't the Worker's")
        made("made_app", ns)
        if (app.get("configuration") or {}).get("wrangler_ssh", {}).get("enabled") is not False:
            rest.ssh_off(ns)
            app = rest.application(ns) or {}
            if (app.get("configuration") or {}).get("wrangler_ssh", {}).get("enabled") is not False:
                raise AdapterError("Cloudflare's own SSH couldn't be turned off for the computer's container application")
        sub = rest.subdomain()
        return {"worker": worker, "url": f"https://{worker}.{sub}.workers.dev", "namespace": ns, "repo": repo,
                "digest": IMAGE_DIGEST, "size": size, "arch": "x86_64"}

    def owned_lost(self, name, op):
        """The Worker a launch of operation `op` made though its answer was lost: the Worker of the computer's name whose
        bindings carry the computer and that operation. None when there's none; AdapterError when there's one without
        them (it can't be told whose it is: left alone, said)."""
        worker = self._worker_name(name)
        if not OP_RE.fullmatch(op or ""):
            return None
        settings = self.rest.script_settings(worker)
        if settings is None:
            return None
        if self.owns_script(name, settings, op, self.rest):
            return {"worker": worker}
        raise AdapterError(f"a Worker named {worker} exists in the Cloudflare account, but nothing shows this build made "
                           f"it (its bindings don't name {name} and operation {op[:8]}...): it's left alone. If it's "
                           "yours and unused, delete it yourself; or choose another name")

    def wake(self, name, guest_up=None, from_image=False):
        """Start the container from its latest snapshot (nothing when it runs), then the boot script in it (sshd, the
        X server and the desktop: a stop keeps no program). Returns its status, with notes for the person."""
        s, st = self._live(name)
        idle = self.idle_of(self._settings(name).get("IDLE_STOP"))
        t0 = time.monotonic()
        was = bool(s.get("running"))
        lost = s.get("lost")
        out = self.ask("start", {"boot": True, "idle_min": idle, "from_image": bool(from_image)}, 900)
        notes = []
        if not was:
            notes.append(f"it started from its latest snapshot: {STOP_KEEPS}")
        if lost:
            notes.append(f"Cloudflare had stopped it on {days(lost.get('at'))} without warning: what changed after its "
                         f"last snapshot ({days(lost.get('since'))}) was lost")
        return dict(self.status(name), woke_in_s=round((out.get("started_ms") or 0) / 1000.0, 2),
                    booted_in_s=round((out.get("booted_ms") or 0) / 1000.0, 2), total_s=round(time.monotonic() - t0, 2),
                    note="; ".join(notes) or None)

    def sleep(self, name, guest_up=None, force=False, asked=False):
        """The stop: the desktop's programs ended, the disk snapshotted (while it still runs), then the container
        destroyed. A snapshot that fails stops nothing."""
        s, _st = self._live(name)
        if not s.get("running"):
            return {"name": name, "place": self.manifest["place"], "state": "stopped", "note": "it was stopped already"}
        try:
            out = self.ask("stop", {}, 900)
        except AdapterError:
            now = self._stopping(name)
            if now:
                raise AdapterError(now) from None
            raise
        snap = out.get("snapshot") or {}
        return {"name": name, "place": self.manifest["place"], "state": "stopped",
                "stopped_in_s": round((out.get("ms") or 0) / 1000.0, 2),
                "snapshot_gb": round((snap.get("size") or 0) / 1e9, 2),
                "note": f"its disk was snapshotted, then the container was ended: {STOP_KEEPS}"}

    def _stopping(self, name):
        """What's true after a stop that answered an error: the words for a computer whose disk was saved but whose
        container Cloudflare didn't end (its Worker says "stopping"), or None (anything else: the error stands)."""
        try:
            s = self.ask("status", {}, 60)
        except AdapterError:
            return None
        if s.get("state") != "stopping":
            return None
        p = s.get("stop_pending") or {}
        latest = s.get("latest") or {}
        return (f"{name}'s disk was saved (its latest snapshot, kept until {days(latest.get('expires'))}), but Cloudflare "
                f"didn't end its container ({str(p.get('error') or 'no answer')[:120]}): it's still running, and its "
                "control Worker tries to end it again every minute. Nothing is lost. metacomputer status says when it's "
                "stopped; stop again to try now, or start to take it up again")

    def exec(self, name, argv, input=None, timeout=None, max_output=None):
        """A command in the container as root, through the Worker, only until the computer is set up (the Worker refuses
        it after). Never starts a stopped one. (exit code, stdout, stderr)."""
        import base64
        s, _st = self._live(name)
        if not s.get("running"):
            raise AdapterError(f"{name} is stopped: nothing was run (metacomputer --vm {name} start)")
        body = {"argv": list(argv), "timeout_s": int(timeout or 600), "max_output": int(max_output or (1 << 20))}
        if input is not None:
            body["stdin_b64"] = base64.b64encode(bytes(input)).decode()
        out = self.ask("exec", body, int(timeout or 600) + 60)
        return (out.get("code") if isinstance(out.get("code"), int) else 1, base64.b64decode(out.get("stdout_b64") or ""),
                base64.b64decode(out.get("stderr_b64") or ""))

    def boot(self, name, restart_desktop=False):
        s, _st = self._live(name)
        if not s.get("running"):
            raise AdapterError(f"{name} is stopped (metacomputer --vm {name} start)")
        return self.ask("boot", {"restart_desktop": bool(restart_desktop)}, 600)

    def logs(self, name, lines=200):
        import base64
        self._live(name)
        out = self.ask("logs", {"lines": int(lines)}, 120)
        return base64.b64decode(out.get("stdout_b64") or "")

    def setup_done(self, name):
        """The Worker closes arbitrary commands for good (the setup is over)."""
        self._live(name)
        return self.ask("setup-done", {}, 60)

    def delete(self, name, journal=None):
        """The computer's container and its object's storage (through the Worker, with its key: what Cloudflare keeps is
        said, with dates), its container application, its Worker (its Durable Object namespace goes with it), and the
        image's manifest in its repository. Never a Worker of that name this computer didn't make (owns_script: the same
        check as a lost launch's). `journal(plan)` is called before anything is deleted. Returns {deleted, residue,
        registry, image}: residue is the snapshots the Worker knew of (with their expiry), registry what the repository
        still holds."""
        worker = self._worker_name(name)
        st = self.states.get(name) or {}
        rec_worker, _do16 = parse_instance(st.get("instance"))
        if st.get("instance") and rec_worker != worker:
            raise AdapterError(f"{name}'s settings name the Worker {worker}, but it was made as {rec_worker}: nothing was "
                               "deleted")
        rest = self.rest
        settings = rest.script_settings(worker)
        op = st.get("op") or st.get("launching")
        if settings is not None and not self.owns_script(name, settings, op, rest):
            raise AdapterError(f"the Worker named {worker} isn't the one {name} was made with (its bindings name another "
                               "computer or operation): metacomputer leaves it alone, and nothing was deleted")
        ns = st.get("namespace") or (rest.namespace_of(worker) if settings is not None else None)
        repo = st.get("repo") or ((st.get("made_image") or {}).get("repo"))
        digest = st.get("digest") or ((st.get("made_image") or {}).get("digest")) or IMAGE_DIGEST
        plan = {"worker": worker if settings is not None else None, "namespace": ns, "repo": repo, "digest": digest}
        # What an earlier try cut off after the Worker's forget learned (forget wipes the Worker's record): kept.
        residue = (st.get("deleting") or {}).get("residue")
        if journal is not None:
            journal(dict(plan, residue=residue))
        if settings is not None and self._worker is not None:
            try:
                got = self.worker.call("forget", {}, 300).get("residue")
            except Exception:      # noqa: BLE001  (the key refused or the Worker unreachable: its deletion still ends it)
                got = None
            if got:
                residue = got
                if journal is not None:
                    journal(dict(plan, residue=residue))      # before anything else goes: a retry still says it
        if ns:
            app = rest.application(ns)
            if app is not None and app.get("name") == worker:
                rest.delete_application(ns)
        if settings is not None:
            rest.delete_script(worker)
        gone = rest.script_settings(worker) is None and (not ns or rest.application(ns) is None)
        image = None
        if repo:
            ok, said = self.images.delete_image(repo, digest)
            image = {"deleted": ok, "said": said}
        left = self.images.residue(repo) if repo else []
        return {"deleted": settings is not None, "gone": gone, "residue": residue, "registry": left, "image": image,
                "worker": worker, "repo": repo}

    def status(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        if self._worker is None:
            return {"name": name, "place": self.manifest["place"], "state": "unknown",
                    "note": "its control Worker's key isn't on this Mac"}
        try:
            s, st = self._live(name)
        except AdapterError as e:
            if "isn't there" in str(e):
                return {"name": name, "place": self.manifest["place"], "state": "absent", "note": str(e)}
            raise
        word = "stopping" if s.get("state") == "stopping" else ("running" if s.get("running") else str(s.get("state") or "stopped"))
        out = {"name": name, "place": self.manifest["place"], "state": STATES.get(word, "stopped"),
               "provider_state": word.upper() if word in ("stopping", "running") else "STOPPED", "persist": "disk",
               "size": s.get("size"), "worker": self._worker_name(name), "arch": "x86_64",
               "egress": (s.get("egress") or {}).get("setting")}
        notes = []
        lost = s.get("lost")
        if lost:
            notes.append(f"Cloudflare stopped it on {days(lost.get('at'))} without warning: what changed after its last "
                         f"snapshot ({days(lost.get('since'))}) was lost")
        if word == "stopping":
            p = s.get("stop_pending") or {}
            notes.append(f"stopping: its disk was saved, but Cloudflare hasn't ended its container yet "
                         f"({str(p.get('error') or 'no answer')[:120]}; tried {p.get('tries') or 1} time(s), again every "
                         "minute). Nothing is lost")
        latest = s.get("latest") or {}
        if latest.get("expires"):
            notes.append(f"its latest snapshot is kept until {days(latest['expires'])} (renewed before then)")
        notes.extend(refresh_words(s.get("refresh")))
        if s.get("running") and word != "stopping":
            notes.append(f"it stops by itself {s.get('idle_min')} min after the last connection closes")
        if notes:
            out["note"] = "; ".join(notes)
        return out

    def list(self):
        out = []
        for n, s in sorted(self.computers.items()):
            if s.get("PROVIDER") != self.name:
                continue
            st = self.states.get(n) or {}
            out.append({"name": n, "worker": worker_of(s), "place": self.manifest["place"], "here": True,
                        "state": "recorded" if st.get("instance") else "absent"})
        return out

    # snapshots: a computer's checkpoints are snapshots its Worker names --------------------------------------------
    def snapshot(self, name, label, replace=False):
        """A live snapshot of the whole disk, called `label` (the Worker keeps the names). The computer keeps running."""
        check_name(label)
        s, _st = self._live(name)
        if not s.get("running"):
            raise AdapterError(f"{name} is stopped: a checkpoint is taken of a running computer (metacomputer start)")
        out = self.ask("snapshot", {"label": label, "replace": bool(replace)}, 900)
        snap = out.get("snapshot") or {}
        return {"label": label, "version": snap.get("id"), "size_gb": round((snap.get("size") or 0) / 1e9, 2),
                "snapshot_s": round((out.get("ms") or 0) / 1000.0, 2), "let_go": out.get("let_go"),
                "note": "taking it didn't interrupt the computer"}

    def snapshots(self, name):
        s, _st = self._live(name)
        return [{"label": label, "version": (c or {}).get("id"), "created": days((c or {}).get("made")),
                 "expires": days((c or {}).get("expires"))}
                for label, c in sorted((s.get("checkpoints") or {}).items())]

    def restore(self, name, label):
        """Back to checkpoint `label`: what runs now is discarded, the container starts from it and boots."""
        check_name(label)
        self._live(name)
        out = self.ask("restore", {"label": label}, 900)
        return {"label": label, "restored_in_s": round((out.get("ms") or 0) / 1000.0, 2),
                "note": "the computer was started again from the checkpoint: open apps closed"}

    def delete_snapshot(self, name, label):
        """Forgets checkpoint `label`: Cloudflare can't delete a snapshot; nothing renews it now, and it expires 30 days
        after its last use (said)."""
        check_name(label)
        self._live(name)
        out = self.ask("uncheckpoint", {"label": label}, 60)
        gone = out.get("let_go") or {}
        return {"label": label, "versions": [gone.get("id")] if gone.get("id") else [],
                "expires": days(gone.get("expires")) if gone else None}

    # egress: the platform's outbound rules and metacomputer's handlers -------------------------------------------
    def egress(self, name):
        s = self._settings(name)
        out = {"model": "http-intercept", "setting": check_egress(s.get("EGRESS")), "allow": []}
        st = self.states.get(name) or {}
        if st.get("instance") and self._worker is not None:
            w, _st = self._live(name)
            e = w.get("egress") or {}
            src = e.get("in_force") if w.get("running") and e.get("in_force") else {"internet": e.get("setting") == "internet",
                                                                                    "allow": e.get("allow") or []}
            kind, names = policy_kind(src)
            out.update(outside=kind, allow=names, running=bool(w.get("running")),
                       in_force=bool(w.get("running") and e.get("in_force")))
        return out

    def set_policy(self, name, policy):
        """The egress, recorded by the Worker (the next start has it) and put in force on a running container: live
        within off and allowlists, by a restart (its disk snapshotted first) between those and the internet. Read
        back."""
        self._live(name)
        body = {"egress": "internet" if policy["internet"] else "off"} if policy["internet"] or not policy["allow"] \
            else {"allow": list(policy["allow"])}
        out = self.ask("net", body, 900)
        e = self.egress(name)
        if (e.get("outside"), sorted(e.get("allow") or [])) != (policy_kind(policy)[0], sorted(policy_kind(policy)[1])):
            raise AdapterError(f"the Worker reads the egress back as {e.get('outside')}, not {policy_kind(policy)[0]}")
        return dict(e, restarted=bool(out.get("restarted")))

    def apply_egress(self, name, egress):
        return self.set_policy(name, policy_for(check_egress(egress)))

    def set_egress(self, name, allow, exact=True):
        self._settings(name)
        try:
            policy = policy_for(allow=allow)
        except ValueError as e:
            raise AdapterError(str(e)) from None
        return self.set_policy(name, policy)

    # connection ---------------------------------------------------------------------------------------------------
    def setup(self):
        return {"ok": True, "note": "make an API token for the Cloudflare account (docs/cloudflare.md: Workers Scripts "
                                    "Edit and Containers Edit; an account-owned token in an account of its own is "
                                    "best), then run metacomputer vm create NAME --on cloudflare with "
                                    "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in its environment"}

    def doctor(self):
        out = []
        if self._rest is None:
            out.append({"check": "the account's API token", "ok": None,
                        "detail": "not in this command's environment (needed only for vm create and vm delete)"})
        else:
            try:
                out.append({"check": "the account's API token reads its workers.dev subdomain", "ok": bool(self.rest.subdomain()),
                            "detail": "ok"})
            except AdapterError as e:
                out.append({"check": "the account's API token reads its workers.dev subdomain", "ok": False, "detail": str(e)})
        for n, s in sorted(self.computers.items()):
            if s.get("PROVIDER") != self.name or not (self.states.get(n) or {}).get("instance"):
                continue
            if self._worker is None:
                out.append({"check": f"{n}: its control Worker's key", "ok": False, "detail": "not on this Mac"})
                continue
            try:
                w, _st = self._live(n)
            except AdapterError as e:
                out.append({"check": f"{n}: its control Worker", "ok": False, "detail": str(e)})
                continue
            out.append({"check": f"{n}: its control Worker answers for it (with its own key)", "ok": True,
                        "detail": "running" if w.get("running") else str(w.get("state"))})
            kind, names = policy_kind((w.get("egress") or {}).get("in_force") or {"internet": (w.get("egress") or {}).get("setting") == "internet",
                                                                                   "allow": (w.get("egress") or {}).get("allow") or []})
            out.append({"check": f"{n}: an egress metacomputer set", "ok": kind in ("internet", "off", "allowlist"), "detail": kind})
            exp = [r for r in w.get("residue") or [] if r.get("labels")]
            soon = [r for r in exp if r.get("expires", 0) - (w.get("now") or 0) < 3 * DAY_MS]
            trouble = refresh_words(w.get("refresh"))
            out.append({"check": f"{n}: its kept snapshots are renewed before they expire", "ok": not soon and not trouble,
                        "detail": "; ".join(trouble) or ", ".join(f"{'/'.join(r['labels'])} until {days(r['expires'])}"
                                                                  for r in exp) or "none yet"})
            if w.get("state") == "stopping":
                p = w.get("stop_pending") or {}
                out.append({"check": f"{n}: its last stop ended the container", "ok": False,
                            "detail": f"its disk was saved, but Cloudflare hasn't ended it ({str(p.get('error') or '')[:120]}; "
                                      "tried again every minute)"})
            if self._rest is not None:
                app = self.rest.application((self.states.get(n) or {}).get("namespace") or "0" * 32)
                ssh = ((app or {}).get("configuration") or {}).get("wrangler_ssh", {}).get("enabled")
                out.append({"check": f"{n}: Cloudflare's own SSH is off", "ok": ssh is False, "detail": str(ssh)})
        return out

    # cost --------------------------------------------------------------------------------------------------------
    def rates(self, size=None):
        vcpu, mem, disk = SIZES[self.size_of(size)]
        awake = mem * MEM_GIB_HOUR + disk * DISK_GB_HOUR + DESKTOP_CPU * CPU_HOUR
        busy = mem * MEM_GIB_HOUR + disk * DISK_GB_HOUR + vcpu * CPU_HOUR
        return {"awake_per_hour": round(awake, 4), "asleep_per_hour": 0.0, "currency": "USD",
                "note": (f"{vcpu} vCPU, {mem} GiB and {disk} GB at Cloudflare's list prices past what Workers Paid "
                         f"includes (memory ${MEM_GIB_HOUR}/GiB-hour and disk as provisioned while it runs; CPU "
                         f"${CPU_HOUR}/vCPU-hour as used); all CPUs busy: about ${busy:.2f}/h. Workers Paid is $5 a "
                         "month and includes 25 GiB-hours of memory a month. Stopped: its snapshots, free during "
                         "Cloudflare's beta (their price after it isn't known)")}


ADAPTER = Cloudflare
