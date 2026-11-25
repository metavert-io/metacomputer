"""Servers a provider makes and bills by the hour, through its REST API: each such provider's driver
(providers/<name>/adapter.py) is a thin one on this base. What they share is here; each driver adds its API's calls,
its sizes, its prices and what its firewall enforces. Everything else is the generic Ubuntu VM's (lib/cloud/ubuntu_vm.py).

Credentials come only from the environment (one API token per provider, named by its driver), so a secrets manager
can hand them over for one command; nothing writes them to disk and no value is printed. Each computer gets, at its
provider, marked with its name and the build's operation (so a launch whose record was lost is found):
  - its own firewall, made before its server and deleted with it: the outside layer, which root in the guest can't
    change. What each provider's firewall enforces is its manifest's `outside_egress`, and no more is claimed;
  - an SSH key: the public half of the computer's admin key, because a provider makes a server without a root
    password only when it's given a key. It's for the first boot only: the user-data's disable_root keeps root from
    using it, bootstrap.sh removes it and locks root's password, and it's deleted with the computer;
  - the server: Ubuntu 24.04, IPv4 only, a public address, no backups, monitoring or provider agent, and the
    user-data of lib/cloud/guest.py.
A stopped server bills in full at all three (billing "full"): only deleting it stops the charges, and an idle stop
(the guest powering itself off) saves nothing.

HTTP goes through `http`, handed in by lib/remote.py (lib/provider_http.py send) or a test's stand-in: nothing here
opens a connection. A call that makes something (a server, a firewall, a key) or acts is sent exactly once, never
retried: a second try after a lost answer could make a second one. Reads and deletes are tried again on throttling
and the provider's passing errors.

The facade's method names are the ones lib/remote.py uses for every provider, the first one's (an SDK's): `nsg` is the
computer's own firewall at the provider, and these providers have no shared network (network() is None)."""
import contextlib
import functools
import inspect
import json
import os
import re
import time
import uuid

from .adapter import AdapterError, NotSupported, check_name
from .ubuntu_vm import BASE, RemoteVM, check_egress, public_v4

TAG = "metacomputer"
OP_TAG = "metacomputer-op"
OP = re.compile(r"[0-9a-f]{32}")
# What lib/remote.py and the adapters call a server's state, whatever the provider calls it.
STATES = {"RUNNING": "awake", "STARTING": "starting", "PROVISIONING": "starting", "STOPPING": "stopping",
          "STOPPED": "stopped", "TERMINATING": "absent", "TERMINATED": "absent"}
INBOUND = (("tcp", "22", ("0.0.0.0/0", "::/0")),)        # what each firewall lets in: SSH, from anywhere, key only
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# What a provider says in an error is passed on only through scrub(): its token removed, and anything shaped like a
# credential (a long run of token characters, "Bearer ...") too, capped in length.
TOKEN_SHAPED = re.compile(r"(?i)(?:bearer\s+)?[A-Za-z0-9_\-+/=]{32,}")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MAX_SAID = 300


def static_scrub(text, cap=MAX_SAID):
    """Text fit to print when no API (so no token) is at hand: control characters out, anything credential-shaped
    replaced, capped."""
    out = CONTROL.sub(" ", str(text or ""))
    return TOKEN_SHAPED.sub(lambda m: m.group(0) if UUID.fullmatch(m.group(0)) else "[redacted]", out)[:cap]


def scrub_exception(e, scrub):
    """The one place an exception is made fit to print once it leaves the provider's facade or its adapter (every
    method of both goes through it: scrub_errors): a ProviderError's message and code, and every exception's text,
    through `scrub` (the API's: its token and anything credential-shaped replaced). Whatever the provider said that
    went into it, in a refusal, an action's error, a timeout's description or anything else, comes out scrubbed. An
    OSError keeps its own fields (its text is the system's, not the provider's)."""
    if isinstance(e, ProviderError):
        e.message, e.code = scrub(e.message), scrub(e.code, 80)
    if not isinstance(e, OSError):
        e.args = tuple(scrub(x, 1000) if isinstance(x, str) else x for x in e.args)
    return e


def _scrubbing(fn, scrubber):
    @functools.wraps(fn)
    def run(self, *a, **k):
        try:
            return fn(self, *a, **k)
        except Exception as e:  # noqa: BLE001  (scrubbed and raised again, whatever it is)
            scrub_exception(e, scrubber(self))
            raise
    run.scrubs_errors = True
    return run


def scrub_errors(cls, scrubber):
    """Every method `cls` defines (not its dunders, nor the scrubbing itself) raises only scrubbed exceptions."""
    for name, v in list(vars(cls).items()):
        if (name.startswith("__") or name in ("scrub", "_scrubbed") or not inspect.isfunction(v)
                or getattr(v, "scrubs_errors", False)):
            continue
        setattr(cls, name, _scrubbing(v, scrubber))
    return cls


class ProviderError(AdapterError):
    """The provider refused a call: its status, its error code and its message (never the request)."""

    def __init__(self, label, status, code, message, what=""):
        self.status, self.code, self.message = status, str(code or ""), CONTROL.sub(" ", str(message or ""))[:400]
        head = " ".join(x for x in (str(status), self.code) if x)
        super().__init__(f"{label} refused{(' ' + what) if what else ''}: {head}" + (f": {self.message}" if self.message else ""))


    @property
    def definitive(self):
        """The provider answered that it didn't do it (a 4xx): nothing was made. A 5xx, or no answer, proves nothing."""
        return isinstance(self.status, int) and 400 <= self.status < 500


class Capacity(ProviderError):
    """The provider has no room for that size in that region now."""


class LaunchUnknown(AdapterError):
    """A create was sent and no answer says what became of it: a server may exist that nothing here can name."""


def _q(v):
    """A query value, percent-encoded (lib/cloud imports no urllib)."""
    return "".join(c if c.isalnum() or c in "-._~" else "%" + "%".join(f"{b:02X}" for b in c.encode()) for c in str(v))


def query_string(q):
    return "&".join(f"{_q(k)}={_q(v)}" for k, v in q.items() if v is not None)


FW_NAME = re.compile(r"metacomputer-(.+?)(?:\.([0-9a-f]{12}))?")


def fw_name(name, op):
    """A computer's firewall's name: its computer and the operation that made it (metacomputer-NAME.OPERATION12)."""
    return f"metacomputer-{name}.{op[:12]}"


def fw_owner(fwname):
    """(computer, operation prefix or None) a firewall's name says it's of, or (None, None) for one not
    metacomputer's."""
    m = FW_NAME.fullmatch(fwname or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def same_key(a, b):
    """Two OpenSSH public keys are the same key (type and base64; the comment doesn't count)."""
    return " ".join((a or "").split()[:2]) == " ".join((b or "").split()[:2])


class API:
    """The provider's REST API, as plain values (dicts, strings): a stand-in can answer for it in the tests. Each
    driver says its label, its address, its token's variable and its calls."""
    label = "a provider"
    base = ""
    env_key = ""
    NAME_LEN = 64

    def __init__(self, env=None, http=None, sleep=None, clock=None):
        env = os.environ if env is None else env
        token = (env.get(self.env_key) or "").strip()
        if not token:
            raise AdapterError(f"{self.label} needs its API token in the environment (from your secrets manager, for "
                               f"this command only): {self.env_key} missing")
        if http is None:
            raise AdapterError(f"no way to reach {self.label}'s API was given (lib/remote.py gives it)")
        self.__token = token
        self._http = http
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic

    def __repr__(self):
        return f"<{self.label} API>"

    def scrub(self, text, cap=MAX_SAID):
        """Text from the provider (an error's message or code, a connection's error) fit to print: control
        characters out, this API's token and anything credential-shaped replaced, capped."""
        out = CONTROL.sub(" ", str(text or ""))
        if self.__token:
            out = out.replace(self.__token, "[redacted]")
        out = TOKEN_SHAPED.sub(lambda m: m.group(0) if UUID.fullmatch(m.group(0)) else "[redacted]", out)
        return out[:cap]

    def _scrubbed(self, err):
        """The exception with everything the provider said in it scrubbed (scrub_exception)."""
        return scrub_exception(err, self.scrub)

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        scrub_errors(cls, API.scrubber)

    @staticmethod
    def scrubber(api):
        return api.scrub

    def preflight(self):
        """What a create needs that can be checked before anything is made (a token's scope, say)."""

    # --- HTTP ---------------------------------------------------------------------------------------------
    def call(self, method, path, body=None, query=None, missing_ok=False, what=""):
        """One API call: its JSON answer ({} when it has none), None for a 404 when missing_ok. A call that makes or
        changes something (POST, PUT) is sent once; GET and DELETE are tried again (up to 5 times, with backoff) on
        429, 5xx and a connection that failed. Anything else is a ProviderError (or this driver's reading of it)."""
        url = self.base + path + (("?" + query_string(query)) if query else "")
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {self.__token}", "Accept": "application/json",
                   "User-Agent": "metacomputer"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        tries = 5 if method in ("GET", "DELETE") else 1
        for attempt in range(tries):
            try:
                status, rh, answer = self._http(method, url, headers, data, 60)
            except OSError as e:
                if attempt + 1 < tries:
                    self._sleep(2 ** attempt)
                    continue
                if method == "POST":
                    # A create (or an action) that may have reached the provider: what it did isn't known.
                    raise LaunchUnknown(f"{self.label}'s API didn't answer ({self.scrub(e)})") from None
                raise AdapterError(f"{self.label}'s API didn't answer ({self.scrub(e)})") from None
            if 200 <= status < 300:
                try:
                    return json.loads(answer) if (answer or b"").strip() else {}
                except ValueError:
                    raise AdapterError(f"{self.label}'s API answered with something that isn't JSON") from None
            if status == 404 and missing_ok:
                return None
            if attempt + 1 < tries and (status == 429 or status >= 500):
                wait = 2 ** attempt
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    wait = max(wait, min(60, int({k.lower(): v for k, v in (rh or {}).items()}.get("retry-after"))))
                self._sleep(wait)
                continue
            raise self._scrubbed(self.refused(status, answer, what))
        raise AdapterError(f"{self.label}'s API kept failing")      # not reached

    def refused(self, status, answer, what=""):
        """The provider's error as an exception: each driver reads its own error shape."""
        code, message = "", ""
        with contextlib.suppress(ValueError, AttributeError, TypeError):
            j = json.loads(answer)
            code, message = j.get("code", ""), j.get("message", "")
        return ProviderError(self.label, status, code, message, what)

    def wait(self, test, timeout, what, step=5):
        """Wait until test() is true (each look `step` seconds apart), or raise saying what didn't happen."""
        t0 = self._clock()
        while True:
            if test():
                return
            if self._clock() - t0 >= timeout:
                raise AdapterError(f"{self.label}: {what} didn't happen in {int(timeout)} s")
            self._sleep(step)

    # --- what lib/remote.py and the adapter use, the same on every provider -----------------------------------
    def wait_state(self, iid, want, timeout=600):
        """Wait until the server is in state `want` (TERMINATED: gone)."""
        seen = {}

        def there():
            i = self.instance(iid)
            seen["st"] = st = i["state"] if i else "TERMINATED"
            if st == "TERMINATED" and want != "TERMINATED":
                raise AdapterError(f"the server was deleted at {self.label}")
            return st == want
        try:
            self.wait(there, timeout, f"the server reaching {want}")
        except AdapterError as e:
            if "didn't happen" in str(e):
                raise AdapterError(f"the server didn't reach {want} in {int(timeout)} s at {self.label} (it's "
                                   f"{seen.get('st', 'unknown')})") from None
            raise
        return want

    # No network shared between computers: each has its own firewall, and the provider gives the server its address.
    def network(self):
        return None

    def networks(self):
        return []

    def make_network(self):
        return None

    def delete_network(self):
        return False

    def network_by_id(self, _vid):
        return None

    def delete_network_by_id(self, _vid):
        return False

    def boot_volumes(self):
        return []                   # the server's disk is part of it, deleted with it

    def security_rules(self):
        return None

    def attach_firewall(self, fw, iid):
        """A provider that can't name the firewall when the server is made applies it right after."""

    def inventory(self):
        """What metacomputer has at this provider besides servers (cloud status): firewalls and keys, by name."""
        return {"firewalls": self.firewalls(), "ssh_keys": self.keys()}

    # Each driver: instances, instance, public_ip, image, launch, action, terminate, nsg, make_nsg, nsg_egress,
    # nsg_rules, egress_of, firewalls, delete_nsg, delete_nsg_by_id, nsg_names, make_key, keys, delete_key_by_id,
    # delete_keys.


scrub_errors(API, API.scrubber)


def merged(rules):
    """A firewall's rules, normalised ({direction, protocol, ports, addresses}), merged by direction, protocol and
    ports: {(direction, protocol, ports): set of addresses}. A rule split in two (a provider's limit on addresses per
    rule) reads back as one."""
    out = {}
    for r in rules:
        out.setdefault((r["direction"], r["protocol"], r["ports"]), set()).update(r["addresses"])
    return out


def egress_from(rules, off=()):
    """What a firewall lets out, read back from its rules: "internet" (exactly every public IPv4 block, TCP and UDP on
    every port and ICMP, nothing else), "off" (exactly the provider's way of saying nothing: `off`, the outbound rules
    that mean nothing gets out), or "other" (anything else: not what metacomputer set)."""
    out = {k: v for k, v in merged(rules).items() if k[0] == "out"}
    if out == merged(off):
        return "off"
    pub = set(public_v4())
    if out == {("out", "tcp", "all"): pub, ("out", "udp", "all"): pub, ("out", "icmp", None): pub}:
        return "internet"
    return "other"


def inbound_ok(rules):
    """The firewall lets in SSH (TCP 22, from anywhere) and nothing else."""
    want = {("in", p, ports): set(a) for p, ports, a in INBOUND}
    return {k: v for k, v in merged(rules).items() if k[0] == "in"} == want


class VPS(RemoteVM):
    """A server a provider makes and bills by the hour (each driver: its API, sizes, prices and manifest).
    `api` is the REST facade (default: the driver's API from `env`'s token and `http`, made at the first call that
    needs it)."""
    API = API
    SIZES = {}                  # size -> (architecture, vCPUs, memory GB, disk GB, USD an hour)
    DEFAULT_SIZE = ""
    DEFAULT_REGION = ""
    REGION = re.compile(r"[a-z]{2,8}[0-9]{0,2}")
    EXTRA_PER_HOUR = 0.0        # billed beside the server, stopped or not (an IPv4 address, say)
    PRICES_READ = "2026-11-23"
    # How a stop asks the guest to shut down: "api" (the provider presses its power button, ACPI) or "guest" (the
    # provider has no such call: metacomputer stop asks the guest over SSH first, then this waits for the provider to
    # see it off). Never a hard power-off unless asked (stop --force).
    SOFT_STOP = "api"
    SEES_GUEST_POWEROFF = True  # the provider's state says stopped once the guest powers itself off (not every one's)
    STOP_WAIT_S = 360
    takes_key = True            # create() wants the computer's admin public key (lib/remote.py passes it)
    RESOLVER_EXCEPTION = False  # its resolver is on the provider's private network: set_resolvers
    REST_API = True             # lib/remote.py hands it lib/provider_http.py's send for its REST API
    thing = "server"

    def __init__(self, computers=None, states=None, api=None, env=None, http=None, network_lock=None):
        super().__init__(computers, states)
        self._api, self._env, self._http = api, env, http
        # Held while the computer's firewall and key are made: lib/remote.py passes a lock one process on this Mac
        # holds at a time (the same one `cloud setup|teardown` take).
        self._network_lock = network_lock or contextlib.nullcontext

    @property
    def api(self):
        if self._api is None:
            self._api = self.API(self._env, self._http)
        return self._api

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        scrub_errors(cls, VPS.scrubber)

    @staticmethod
    def scrubber(adapter):
        """The adapter's exceptions are scrubbed with its API's token too, once it has one (scrub_exception)."""
        return adapter._api.scrub if isinstance(adapter._api, API) else static_scrub

    @classmethod
    def size_of(cls, size):
        s = size or cls.DEFAULT_SIZE
        if s not in cls.SIZES:
            raise NotSupported(cls.name, "size", f"{cls.manifest['label']} has no size {s!r} here: {', '.join(cls.SIZES)}")
        return s

    @classmethod
    def region_of(cls, region):
        r = (region or cls.DEFAULT_REGION).strip().lower()
        if not cls.REGION.fullmatch(r):
            raise NotSupported(cls.name, "region", f"{region!r} isn't a {cls.manifest['label']} region "
                               f"(for example {', '.join(cls.manifest['regions'])})")
        return r

    @classmethod
    def arch(cls, size):
        return cls.SIZES[cls.size_of(size)][0]

    def _state(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            raise AdapterError(f"{name} has no server at {self.manifest['label']} that this Mac knows of (metacomputer "
                               f"cloud status {self.name} lists metacomputer's servers there)")
        return st

    # lifecycle
    def create(self, name, region=None, size=None, persist="auto", user_data=None, egress="internet", op=None,
               made=None, public_key=None):
        """Make the server for `name`: its own firewall and its first-boot key first (under the lock, each told to
        `made` with its id as it's made), then the server, tried once. `op` marks the server with the build that made
        it, so a launch whose record was lost is found (and deleted). Returns {instance, ad (the region), shape,
        size, image, arch, nsg (its firewall), key}."""
        check_name(name)
        from . import capabilities
        try:
            capabilities.choose_persist(self.manifest, persist)
        except capabilities.Unavailable as e:
            raise NotSupported(self.name, "PERSIST", str(e)) from None
        sz, rg = self.size_of(size), self.region_of(region)
        check_egress(egress)
        if not user_data:
            raise AdapterError("a new computer needs its user-data (lib/cloud/guest.py; metacomputer vm create makes it)")
        if not public_key:
            raise AdapterError(f"a new computer needs its admin key's public half: {self.manifest['label']} makes a "
                               "server without a root password only when it's given a key")
        public_key = " ".join(public_key.split()[:3])     # "TYPE BASE64 [comment]", one line
        op = op or uuid.uuid4().hex
        if not OP.fullmatch(op):
            raise AdapterError("an operation id is 32 hex digits")
        made = made or (lambda what, rid: None)
        api = self.api
        api.preflight()                      # before anything is made (a token's scope, say)
        if api.instances(name):
            raise AdapterError(f"{self.manifest['label']} has a server for {name} already (metacomputer cloud status "
                               f"{self.name})")
        with self._network_lock():
            # Each resource this operation makes is told to `made` the moment its create call returns, with its id,
            # before any call that configures it (and could fail): only what this operation's own create made is
            # ever claimed, never something a listing shows (a listing can lag, or show another's).
            fw = api.make_nsg(name, egress, op=op, on_made=lambda fid: made("made_nsg", fid))
            kid, new_key = api.make_key(name, op, public_key)
            if new_key:
                made("made_key", kid)
        img = api.image(self.arch(sz))
        made("launch_state", "sent")         # from here a server may exist: the journal says so before the call
        try:
            iid = api.launch(name, sz, rg, img["id"], user_data, fw, kid, op)
        except ProviderError as e:
            if e.definitive:
                made("launch_state", "refused")
            raise
        try:
            made("made_instance", iid)       # journaled before anything else is asked of it
            made("launch_state", "made")
        except Exception:
            with contextlib.suppress(Exception):
                api.terminate(iid)           # one that can't be recorded is deleted at once
            raise
        try:
            api.attach_firewall(fw, iid)
        except Exception:
            # Never left running without its outside layer: deleted at once (and its id is in the journal).
            with contextlib.suppress(Exception):
                api.terminate(iid)
            raise
        return {"instance": iid, "ad": rg, "shape": sz, "size": sz, "image": img["name"], "arch": self.arch(sz),
                "nsg": fw, "key": kid, "region": rg}

    def wake(self, name, guest_up=None):
        """Start it. A server the provider says runs but whose SSH doesn't answer (guest_up=False) is never stopped
        here: it may be busy or cut off (the provider sees a guest that powered itself off as off)."""
        st = self._state(name)
        cur = self.api.instance(st["instance"])
        if cur is None:
            raise AdapterError(f"{name}'s server is gone from {self.manifest['label']}")
        if cur["state"] == "RUNNING" and guest_up is False:
            # Never stopped or started on a guess: a start of a guest that runs restarts it at some providers (found
            # live).
            why = ("it may be starting, busy or cut off" if self.SEES_GUEST_POWEROFF else
                   f"{self.manifest['label']} doesn't see a guest that powered itself off (the idle stop does that), so "
                   "it may be off, or busy or cut off")
            raise AdapterError(f"{name} runs at {self.manifest['label']} but its SSH doesn't answer: {why}. Nothing was "
                               f"stopped or started. Check it (metacomputer --vm {name} status); to restart it anyway: "
                               f"metacomputer --vm {name} stop --force, then start")
        if cur["state"] in ("STOPPING", "STARTING", "PROVISIONING"):
            self.api.wait_state(st["instance"], "STOPPED" if cur["state"] == "STOPPING" else "RUNNING",
                                timeout=self.STOP_WAIT_S)
            cur = self.api.instance(st["instance"]) or cur
        if cur["state"] != "RUNNING":
            self.api.action(st["instance"], "START")
            self.api.wait_state(st["instance"], "RUNNING")
        return self.status(name)

    def sleep(self, name, guest_up=True, force=False, asked=False):
        """Stop it: the guest shuts down, then the provider sees it off; a hard power-off only with force. SOFT_STOP
        "api": the provider's own shutdown call. "guest": the CLI asked the guest over SSH (asked); then only the
        provider's own state can say it's off, and if it doesn't, nothing is halted (SSH going quiet is no proof).
        Stopped, it still bills in full: only deleting it stops the charges."""
        st = self._state(name)
        cur = self.api.instance(st["instance"])
        if cur and cur["state"] not in ("STOPPED", "STOPPING"):
            if force:
                self.api.action(st["instance"], "STOP")
            elif self.SOFT_STOP == "api":
                self.api.action(st["instance"], "SOFTSTOP")
            elif not asked:
                raise AdapterError(f"{self.manifest['label']} has no call that asks the guest to shut down: metacomputer "
                                   f"--vm {name} stop asks it over SSH (stop --force halts it at once)")
            # SOFT_STOP "guest", asked: nothing is sent. Only the provider's own state can say the guest is off (SSH
            # going quiet proves nothing: the guest may be stalled, cut off, or not obeying), and it's never halted
            # on a guess.
        if cur:
            try:
                self.api.wait_state(st["instance"], "STOPPED", timeout=self.STOP_WAIT_S)
            except AdapterError as e:
                if force:
                    raise
                if self.SOFT_STOP == "guest":
                    raise AdapterError(
                        f"{name} was asked to power off, but {self.manifest['label']} doesn't report a guest's own "
                        f"power-off, so nothing here can tell whether it's off: it may still be on, and it's billed in "
                        f"full either way. Nothing was halted. To halt it through {self.manifest['label']}: metacomputer "
                        f"--vm {name} stop --force") from None
                raise AdapterError(f"{name} didn't shut down ({e}); nothing else was done. To power it off at once: "
                                   f"metacomputer --vm {name} stop --force") from None
        return self.status(name)

    def delete(self, name):
        st = self._state(name)
        if self.api.instance(st["instance"]) is not None:
            self.api.terminate(st["instance"])
            self.api.wait_state(st["instance"], "TERMINATED", timeout=900)
        # By the ids recorded when they were made first (a provider's listings can lag behind: one's did, found live),
        # then whatever else is listed under its name.
        if st.get("nsg"):
            self.api.delete_nsg_by_id(st["nsg"], name)
        if st.get("key"):
            self.api.delete_key_by_id(st["key"], name)
        self.api.delete_nsg(name)
        self.api.delete_keys(name)

    def set_egress(self, name, allow):
        raise NotSupported(self.name, "egress allowlist", f"{self.manifest['label']}'s computers have no allowlist of "
                           "names: EGRESS=internet (public addresses only) or off")

    def apply_egress(self, name, egress):
        """EGRESS in its own firewall at the provider (outside the guest); the guest's firewall is set over SSH."""
        self._state(name)
        if not self.manifest["outside_egress"]["enforced"]:
            raise NotSupported(self.name, "outside egress rules", f"{self.manifest['label']}'s firewall filters only "
                               "what comes in: the computer's own firewall is what holds its egress")
        st = self.states.get(name) or {}
        fw = st.get("nsg")      # the one recorded when it was made, never one found by name (lib/remote.py checks it)
        if fw is None:
            raise AdapterError(f"{name}'s record names no firewall of its own at {self.manifest['label']}: no rule was "
                               "changed (delete it and make it again)")
        if self.RESOLVER_EXCEPTION:
            self.api.nsg_egress(fw, check_egress(egress), dns=st.get("resolvers") or ())
        else:
            self.api.nsg_egress(fw, check_egress(egress))
        return self.egress(name)

    def set_resolvers(self, name, addresses=None, egress="internet"):
        """A provider whose resolver is reachable already needs no exception."""
        self._state(name)
        return []

    def egress(self, name):
        st = self.states.get(check_name(name)) or {}
        s = self._settings(name)
        out = {"model": "guest-firewall", "setting": check_egress(s.get("EGRESS")), "allow": []}
        if st.get("instance"):
            if not self.manifest["outside_egress"]["enforced"]:
                out["outside"] = "none"
                return out
            try:
                api = self.api              # the provider's API, when its token is here (else: not known)
            except AdapterError:
                api = None
            fw = (st.get("nsg") or api.nsg(name)) if api else None
            if fw is None:
                out["outside"] = None
            elif self.RESOLVER_EXCEPTION:
                out["outside"] = api.egress_of(api.nsg_rules(fw), resolvers=st.get("resolvers") or [])
            else:
                out["outside"] = api.egress_of(api.nsg_rules(fw))
        return out

    def status(self, name):
        st = self.states.get(check_name(name)) or {}
        if not st.get("instance"):
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        cur = self.api.instance(st["instance"])
        if cur is None:
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        out = {"name": name, "place": self.manifest["place"], "state": STATES.get(cur["state"], cur["state"].lower()),
               "provider_state": cur["state"], "persist": "disk", "region": cur.get("ad"), "size": st.get("size")}
        if cur["state"] == "RUNNING":
            out["host"] = self.api.public_ip(st["instance"])
        return out

    def list(self):
        return [{"name": i["name"], "place": self.manifest["place"], "state": STATES.get(i["state"], i["state"].lower()),
                 "shape": i["shape"], "instance": i["id"], "region": i.get("ad")} for i in self.api.instances()]

    # connection
    def setup(self):
        """Checks the token (a read): there's nothing shared to make, each computer gets its own firewall and key."""
        n = len(self.api.instances())
        return {"ok": True, "servers": n, "note": f"{self.manifest['label']} answered; nothing shared to make: each "
                "computer gets its own firewall (SSH in only) and first-boot key, made with it and deleted with it"}

    def doctor(self):
        out = []
        try:
            api = self.api
            servers = api.instances()
        except AdapterError as e:
            return [{"check": "credentials", "ok": False, "detail": str(e)}]
        out.append({"check": "credentials", "ok": True, "detail": "the API answered"})
        for scope, ok, detail in (api.scope_checks() if hasattr(api, "scope_checks") else ()):
            out.append({"check": f"token scope {scope}", "ok": ok, "detail": detail})
        enforced = self.manifest["outside_egress"]["enforced"]
        for i in servers:
            fw = api.nsg(i["name"])
            rules = api.nsg_rules(fw) if fw else []
            out.append({"check": f"{i['name']}: its own firewall lets in SSH only", "ok": bool(fw) and inbound_ok(rules),
                        "detail": f"{len(rules)} rules" if fw else "no firewall"})
            if enforced:
                eg = api.egress_of(rules) if fw else None
                out.append({"check": f"{i['name']}: its firewall lets out public addresses only, or nothing",
                            "ok": eg in ("internet", "off"), "detail": eg or "no firewall"})
        if not enforced:
            out.append({"check": "egress outside the computer", "ok": True,
                        "detail": f"none: {self.manifest['label']}'s firewall filters only what comes in; each "
                                  "computer's own firewall holds its egress"})
        out.append({"check": "servers", "ok": True, "detail": str(len(servers))})
        return out

    # cost
    def rates(self, size=None, region=None):
        sz = self.size_of(size)
        arch, cpus, mem, disk, per_hour = self.SIZES[sz]
        rate = round(per_hour + self.EXTRA_PER_HOUR, 5)
        note = (f"{sz}: {cpus} vCPU, {mem} GB, {disk} GB disk, {arch}, at {self.manifest['label']}'s list price "
                f"(read {self.PRICES_READ}). " + self.manifest["billing"]["note"])
        return {"awake_per_hour": rate, "asleep_per_hour": rate, "currency": "USD", "note": note}


scrub_errors(VPS, VPS.scrubber)


def manifest(name, label, sizes, regions, outside_egress, billing_note):
    """The manifest every driver here has, with its own name, sizes, regions, outside layer and billing words."""
    return dict(BASE, name=name, place=name, label=label,
                # Stop and start through the API: the disk keeps everything; running programs don't survive a stop.
                sleep_wake={"kind": "stop-start", "keeps_processes": False},
                sizes=tuple(sizes), regions=tuple(regions), outside_egress=outside_egress,
                # A stopped server bills in full, and so does one whose guest powered itself off: only deleting it
                # stops the charges.
                billing={"stopped": "full", "guest_poweroff": "nothing", "note": billing_note})
