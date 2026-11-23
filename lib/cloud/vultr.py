"""Vultr: metacomputer makes, starts, stops and deletes Ubuntu 24.04 Cloud Compute instances there (`vultr`, place
`vultr`), through Vultr's REST API (lib/cloud/vps.py has what the three hourly providers share).

Credentials: VULTR_API_KEY, from the environment only. Vultr accepts a key only from the addresses on its access
control list: a call from any other address is refused with "Unauthorized IP address", and metacomputer then says
which address to add and where (it changes when this Mac's public address does). A key of a user whose permissions
are a custom policy needs the instance, SSH key and firewall permissions; when Vultr refuses one, metacomputer
reports Vultr's own words for what's missing rather than guess. metacomputer marks an instance by its label,
`mc-OPERATION-NAME` (the build's 32-hex operation, then the computer's name), a firewall group by its description
(`metacomputer-NAME`) and a key by its name (`metacomputer-NAME-OPERATION`); an instance whose label isn't one of
those is never touched.

Each computer has its own firewall group, made with its rules before the instance and named in its create call:
SSH (TCP 22) in, nothing else. **Vultr's firewall filters only what comes in**: nothing outside the computer limits
what it sends, so the computer's own firewall is the only egress layer here (its manifest's outside_egress claims
nothing), and `net egress` changes only that.

Instances are made without IPv6, automatic backups, DDoS protection or an activation e-mail, from Vultr's "Ubuntu
24.04 LTS x64". Vultr sets a root password on every instance (shown in its console); bootstrap.sh locks it. Vultr has
no call that asks a guest to shut down (its halt is a power-off), and doesn't see a guest that powered itself off (it
still says "running": found live), and its start restarts an instance that runs (found live). So `metacomputer stop`
asks the guest itself over SSH, waits until its SSH has stopped answering for 30 s, and only then halts it through
the API, which makes Vultr's state say stopped; nothing is ever started or halted on a guess. Its idle stop is off by
default: it would save nothing (a stopped instance bills in full) and leave a guest Vultr thinks runs. Sizes (x86 only here): vc2-2c-4gb (the default: 2 vCPU, 4 GB) and vc2-4c-8gb.
Billed by the hour up to the monthly price; a stopped instance is billed in full: only deleting it stops the
charges."""
import re

from .adapter import AdapterError, NotSupported
from .vps import API, OP, Capacity, ProviderError, VPS, fw_name, fw_owner, manifest

ENV_KEY = "VULTR_API_KEY"
# size -> (architecture, vCPUs, memory GB, disk GB, USD an hour): Vultr's list prices, read from its API on
# 2026-11-23; billed by the hour up to the monthly price (vc2-2c-4gb $20, vc2-4c-8gb $40).
SIZES = {
    "vc2-2c-4gb": ("x86_64", 2, 4, 80, 0.027),
    "vc2-4c-8gb": ("x86_64", 4, 8, 160, 0.055),
}
REGIONS = ("ewr", "ord", "dfw", "sea", "lax", "atl", "ams", "lhr")
OS_NAME = "Ubuntu 24.04 LTS x64"
INSTANCE = re.compile(r"mc-([0-9a-f]{32})-([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
UNAUTHORIZED_IP = re.compile(r"Unauthorized IP address:?\s*([0-9a-fA-F:.]{2,45})")

MANIFEST = manifest(
    "vultr", "Vultr", SIZES, REGIONS,
    # Vultr's firewall groups filter only what comes in (SSH, here): nothing outside the computer holds its egress.
    {"enforced": False, "private_ranges": False, "metadata": False, "off": False},
    "billed by the hour up to the monthly price; a stopped instance is billed in full: only deleting it "
    "(metacomputer vm delete) stops the charges, and the idle stop saves nothing")
RULES = (("v4", "0.0.0.0", 0), ("v6", "::", 0))      # SSH in, from anywhere (key only)


def normal(r):
    """One of Vultr's rules as {direction, protocol, ports, addresses}: a rule from a subnet reads as its CIDR (Vultr
    answers it in `source` too); one from anything else (Cloudflare's addresses, a load balancer) as that, marked."""
    cidr = f"{r.get('subnet')}/{r.get('subnet_size')}"
    src = r.get("source") or ""
    addr = cidr if src in ("", cidr) else f"({src})"
    if r.get("action", "accept") != "accept":
        addr = f"({r.get('action')} {addr})"
    proto = r.get("protocol")
    port = str(r.get("port") or "")
    return {"direction": "in", "protocol": proto, "ports": None if proto == "icmp" else (port or "all"),
            "addresses": (addr,)}


class VultrAPI(API):
    label = "Vultr"
    base = "https://api.vultr.com/v2"
    env_key = ENV_KEY

    def refused(self, status, answer, what=""):
        import json
        message = ""
        try:
            message = str((json.loads(answer) or {}).get("error", "") or "")
        except (ValueError, AttributeError, TypeError):
            message = "(an answer that isn't Vultr's error format)"     # never its raw body
        m = UNAUTHORIZED_IP.search(message or "")
        if status == 401 and m:
            ip = m.group(1).strip(".")
            return ProviderError(self.label, status, "unauthorized-ip", (
                f"its API key doesn't accept calls from this Mac's address, {ip}. Add {ip}/32 to the key's access "
                "control list in Vultr's console (Account > API, Access Control; for a sub-account's or service "
                "user's key, that key's own list), then run this again. It changes when this Mac's public address "
                "does"), what)
        if status in (401, 403):
            return ProviderError(self.label, status, "not-permitted", (
                f"{message or 'no reason given'} (this is Vultr's own answer{' to ' + what if what else ''}: the API "
                "key's user lacks the permission it names; add it to the user's policy in Vultr's console)"), what)
        if "capacity" in (message or "").lower() or "not available" in (message or "").lower():
            return Capacity(self.label, status, "", message, what)
        return ProviderError(self.label, status, "", message, what)

    def _all(self, path, key, query=None):
        out, cursor = [], None
        for _ in range(100):
            d = self.call("GET", path, query=dict(query or {}, per_page=500, cursor=cursor))
            out += d.get(key) or []
            cursor = (((d.get("meta") or {}).get("links")) or {}).get("next")
            if not cursor:
                break
        return out

    # instances
    @staticmethod
    def _instance(i):
        m = INSTANCE.fullmatch(i.get("label") or "")
        st = i.get("status")
        state = {"pending": "PROVISIONING", "resizing": "STARTING", "suspended": "SUSPENDED"}.get(st)
        if state is None:
            state = {"running": "RUNNING", "stopped": "STOPPED"}.get(i.get("power_status"), "UNKNOWN")
        return {"id": i["id"], "name": m.group(2) if m else None, "state": state, "shape": i.get("plan"),
                "ad": i.get("region"), "created": i.get("date_created"), "op": m.group(1) if m else None}

    def instances(self, tag_value=None, op=None):
        return [x for x in (self._instance(i) for i in self._all("/instances", "instances"))
                if x["name"] and (tag_value is None or x["name"] == tag_value) and (op is None or x["op"] == op)]

    def _get(self, iid):
        if not re.fullmatch(r"[0-9a-f-]{36}", str(iid)):
            raise AdapterError("that isn't a Vultr instance id")
        d = self.call("GET", f"/instances/{iid}", missing_ok=True)
        if d is None:
            return None
        if not INSTANCE.fullmatch(d["instance"].get("label") or ""):
            raise AdapterError("that instance isn't labelled as metacomputer labels its own; metacomputer leaves it alone")
        return d["instance"]

    def instance(self, iid):
        i = self._get(iid)
        return None if i is None else self._instance(i)

    def public_ip(self, iid):
        ip = (self._get(iid) or {}).get("main_ip")
        return ip if ip and ip != "0.0.0.0" else None

    def image(self, arch):
        if arch != "x86_64":
            raise AdapterError("metacomputer makes x86 instances at Vultr")
        for o in self._all("/os", "os"):
            if o.get("name") == OS_NAME:
                return {"id": o["id"], "name": OS_NAME}
        raise AdapterError(f"Vultr lists no {OS_NAME} image")

    def launch(self, name, size, region, image_id, user_data, fw, kid, op):
        import base64
        body = {"region": region, "plan": size, "os_id": int(image_id), "label": f"mc-{op}-{name}",
                "hostname": "metacomputer", "sshkey_id": [kid], "firewall_group_id": fw,
                "user_data": base64.b64encode(user_data.encode()).decode(), "backups": "disabled",
                "enable_ipv6": False, "disable_public_ipv4": False, "activation_email": False,
                "ddos_protection": False}
        try:
            d = self.call("POST", "/instances", body, what="creating the instance (POST /instances)")
        except Capacity as e:
            raise Capacity(self.label, e.status, e.code, f"{e.message}: try another region (--region) or size") from None
        return d["instance"]["id"]          # (its default_password is never read: bootstrap.sh locks root's password)

    def action(self, iid, what):
        if what == "SOFTSTOP":
            raise NotSupported("vultr", "soft stop", "Vultr has no call that asks the guest to shut down: "
                               "metacomputer stop asks the guest over SSH")
        verb = {"START": "start", "STOP": "halt"}[what]
        self._get(iid)
        self.call("POST", f"/instances/{iid}/{verb}", {}, what=f"to {verb} the instance")

    def terminate(self, iid):
        if self._get(iid) is not None:
            self.call("DELETE", f"/instances/{iid}", missing_ok=True)

    # each computer's own firewall group (inbound only)
    def _groups(self):
        return self._all("/firewalls", "firewall_groups")

    def firewalls(self):
        return [{"id": g["id"], "name": g.get("description"), "computer": fw_owner(g.get("description"))[0],
                 "instances": g.get("instance_count")}
                for g in self._groups() if fw_owner(g.get("description"))[0]]

    def nsg(self, name):
        for g in self._groups():
            if fw_owner(g.get("description"))[0] == name:
                return g["id"]
        return None

    def nsg_owner(self, fw):
        """(computer, operation prefix) the firewall group with this id is described as, or None when it's gone."""
        d = self.call("GET", f"/firewalls/{fw}", missing_ok=True)
        return None if d is None else fw_owner((d.get("firewall_group") or {}).get("description"))

    def nsgs_of_op(self, name, op):
        return [g["id"] for g in self._groups() if fw_owner(g.get("description")) == (name, op[:12])]

    def keys_of_op(self, name, op):
        return [k["id"] for k in self._all("/ssh-keys", "ssh_keys")
                if (k.get("name") or "") == f"metacomputer-{name}-{op[:12]}"]

    def make_nsg(self, name, egress, op, on_made=None):
        """A new firewall group of this operation's (never one found by name), its id to on_made as soon as the
        group is made, before its SSH rules are added (a group with no rule isn't enforced: the server is made only
        after they are). EGRESS isn't a rule here: Vultr's firewall doesn't filter what goes out."""
        fw = self.call("POST", "/firewalls", {"description": fw_name(name, op)},
                       what="the firewall group")["firewall_group"]["id"]
        if on_made:
            on_made(fw)
        for ip_type, subnet, size in RULES:
            self.call("POST", f"/firewalls/{fw}/rules", {"ip_type": ip_type, "protocol": "tcp", "subnet": subnet,
                                                         "subnet_size": size, "port": "22",
                                                         "notes": "metacomputer: SSH, key only"},
                      what="the firewall group's SSH rule")
        return fw

    def nsg_egress(self, fw, egress):
        raise NotSupported("vultr", "outside egress rules", "Vultr's firewall filters only what comes in")

    def nsg_rules(self, fw):
        d = self.call("GET", f"/firewalls/{fw}/rules", query={"per_page": 500}, missing_ok=True)
        return [] if d is None else [normal(r) for r in d.get("firewall_rules") or []]

    def egress_of(self, rules_):
        return "none"

    def nsg_names(self):
        return [g["computer"] for g in self.firewalls()]

    def _delete_group(self, fw):
        def gone():
            try:
                self.call("DELETE", f"/firewalls/{fw}", missing_ok=True)
                return True
            except ProviderError as e:
                if e.status not in (400, 409, 412, 422):
                    raise
                return False            # still linked to an instance being deleted: a few tries
        self.wait(gone, 120, "deleting the firewall group (still linked)", step=5)

    def delete_nsg(self, name):
        gone = [g["id"] for g in self._groups() if fw_owner(g.get("description"))[0] == name]
        for fw in gone:
            self._delete_group(fw)
        return bool(gone)

    def delete_nsg_by_id(self, fw, name):
        d = self.call("GET", f"/firewalls/{fw}", missing_ok=True)
        if d is None or fw_owner(d["firewall_group"].get("description"))[0] != name:
            return False
        self._delete_group(fw)
        return True

    # the first-boot key
    @staticmethod
    def _key_of(name):
        return re.compile(rf"metacomputer-{re.escape(name)}-[0-9a-f]{{12}}")

    def keys(self):
        return [{"id": k["id"], "name": k.get("name")} for k in self._all("/ssh-keys", "ssh_keys")
                if re.fullmatch(r"metacomputer-.+-[0-9a-f]{12}", k.get("name") or "")]

    def make_key(self, name, op, public_key):
        if not OP.fullmatch(op):
            raise AdapterError("an operation id is 32 hex digits")
        d = self.call("POST", "/ssh-keys", {"name": f"metacomputer-{name}-{op[:12]}", "ssh_key": public_key},
                      what="the SSH key")
        return d["ssh_key"]["id"], True

    def delete_key_by_id(self, kid, name):
        d = self.call("GET", f"/ssh-keys/{kid}", missing_ok=True)
        if d is None or not self._key_of(name).fullmatch(d["ssh_key"].get("name") or ""):
            return False
        self.call("DELETE", f"/ssh-keys/{kid}", missing_ok=True)
        return True

    def delete_keys(self, name):
        pat, n = self._key_of(name), 0
        for k in self._all("/ssh-keys", "ssh_keys"):
            if pat.fullmatch(k.get("name") or ""):
                self.call("DELETE", f"/ssh-keys/{k['id']}", missing_ok=True)
                n += 1
        return n


class Vultr(VPS):
    """Instances metacomputer makes at Vultr."""
    name = "vultr"
    manifest = MANIFEST
    API = VultrAPI
    SIZES = SIZES
    DEFAULT_SIZE = "vc2-2c-4gb"
    DEFAULT_REGION = "ewr"
    REGION = re.compile(r"[a-z]{3}")
    SOFT_STOP = "guest"          # no shutdown call: metacomputer stop asks the guest over SSH first
    SEES_GUEST_POWEROFF = False  # found live: a guest that powered off stays "running"; and start restarts a running one
    STOP_WAIT_S = 120            # how long stop waits for Vultr's state to say stopped (it didn't, live) before saying so
    thing = "instance"

    @staticmethod
    def look_for(name, op):
        return (f"Vultr's console (instances labelled mc-{op}-{name}), or: curl -H \"Authorization: Bearer "
                f"$VULTR_API_KEY\" 'https://api.vultr.com/v2/instances?label=mc-{op}-{name}'")
