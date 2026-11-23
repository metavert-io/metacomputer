"""DigitalOcean: metacomputer makes, starts, stops and deletes Ubuntu 24.04 Droplets there (`digitalocean`, place
`digitalocean`), through DigitalOcean's REST API (lib/cloud/vps.py has what the three hourly providers share).

Credentials: DIGITALOCEAN_TOKEN, a personal access token, from the environment only. A custom-scoped token needs
droplet (create, read, update, delete), firewall and ssh_key (create, read, update, delete), and read on regions,
sizes, image, actions and vpc (vpc:read: the resolver's address is checked against the Droplet's VPC; DigitalOcean
asks for snapshot:read beside droplet:read). A create checks vpc:read before it makes anything, and doctor names each
read scope that's missing. It needs no tag scope: metacomputer marks a Droplet by its name, `mc-OPERATION-NAME` (the
build's 32-hex operation, then the computer's name), and a firewall and a key by theirs (`metacomputer-NAME.OPERATION`,
`metacomputer-NAME-OPERATION`, 12 hex digits of it); a Droplet whose name isn't one of those is never touched.

Each computer has its own Cloud Firewall, made before the Droplet and applied to it right after it's made (DigitalOcean
takes no firewall in a Droplet's create call: the Droplet boots behind it within seconds, and the guest's own
firewall loads at its first boot; a Droplet the firewall can't be applied to is deleted at once). DigitalOcean's
firewalls block everything no rule lets through, both ways: in, SSH (TCP 22) only; out, every public IPv4 address
(TCP and UDP on every port, and ICMP: ubuntu_vm.public_v4) with EGRESS=internet, plus DNS (TCP and UDP port 53 only)
to exactly the resolver address(es) the Droplet itself names: a private address on its VPC (found live: without it
nothing resolves), read from the Droplet during its setup (lib/remote.py resolvers), each checked to be inside its
own VPC, at most three, and kept in its record; nothing else on that network. With EGRESS=off, no rule at all.
They don't filter the Droplet's metadata service (169.254.169.254): tested live, it answers through the firewall
with EGRESS=off too, so nothing is claimed for it; the guest's own firewall refuses it.

Droplets are made without IPv6, backups, monitoring or DigitalOcean's Droplet agent, from DigitalOcean's
ubuntu-24-04-x64 image, in the region's default VPC (DigitalOcean puts every Droplet in one; the firewall refuses
private addresses, the VPC's included). Sizes (x86 only: DigitalOcean has no Arm Droplets): s-2vcpu-4gb (the default:
2 vCPU, 4 GB) and s-4vcpu-8gb. Billed by the hour up to the monthly price; a powered-off Droplet is billed in full:
only deleting it stops the charges."""
import ipaddress
import re

from .adapter import AdapterError
from .ubuntu_vm import public_v4
from .ubuntu_vm import check_egress
from .vps import API, OP, Capacity, ProviderError, VPS, egress_from, fw_name, fw_owner, manifest, same_key

ENV_KEY = "DIGITALOCEAN_TOKEN"
# size -> (architecture, vCPUs, memory GB, disk GB, USD an hour): DigitalOcean's list prices, read from its API on
# 2026-11-23; billed by the hour up to the monthly price (s-2vcpu-4gb $24, s-4vcpu-8gb $48).
SIZES = {
    "s-2vcpu-4gb": ("x86_64", 2, 4, 80, 0.03571),
    "s-4vcpu-8gb": ("x86_64", 4, 8, 160, 0.07143),
}
REGIONS = ("nyc3", "sfo2", "tor1", "lon1", "ams3", "sgp1", "syd1")
IMAGE = "ubuntu-24-04-x64"
STATES = {"new": "PROVISIONING", "active": "RUNNING", "off": "STOPPED", "archive": "TERMINATED"}
# A Droplet's name: mc-OPERATION-NAME (DigitalOcean's names allow letters, digits, '.' and '-').
DROPLET = re.compile(r"mc-([0-9a-f]{32})-([A-Za-z0-9][A-Za-z0-9.-]{0,63})")
DROPLET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")

MANIFEST = manifest(
    "digitalocean", "DigitalOcean", SIZES, REGIONS,
    # Its own Cloud Firewall (outside the guest): out, public IPv4 addresses only (and DNS to its VPC's resolver), or
    # nothing at all. The metadata service answers through it (tested live): only the guest refuses it.
    {"enforced": True, "private_ranges": True, "metadata": False, "off": True},
    "billed by the hour up to the monthly price; a powered-off Droplet is billed in full: only deleting it "
    "(metacomputer vm delete) stops the charges, and the idle stop saves nothing")

INBOUND_RULES = [{"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0", "::/0"]}}]


PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


MAX_RESOLVERS = 3


def private(cidr):
    """A CIDR inside the private ranges (where a Droplet's VPC is)."""
    try:
        net = ipaddress.ip_network(cidr)
    except ValueError:
        return False
    return net.version == 4 and any(net.subnet_of(p) for p in PRIVATE)


def resolver_host(cidr):
    """One private IPv4 address, as `A.B.C.D/32`: the only shape a DNS exception may have; None for anything else."""
    try:
        net = ipaddress.ip_network(cidr if "/" in str(cidr) else f"{cidr}/32")
    except ValueError:
        return None
    return str(net) if net.version == 4 and net.prefixlen == 32 and private(str(net)) else None


def outbound(egress, dns=()):
    """The outbound rules for EGRESS: none for off (DigitalOcean's firewalls let out nothing no rule allows); for
    internet, every public block, and DNS (port 53 only) to `dns`: the exact addresses of the Droplet's resolver,
    each a /32 (nothing else in its private network)."""
    if egress == "off":
        return []
    pub = list(public_v4())
    rules = [dict({"protocol": p, "destinations": {"addresses": pub}}, **({"ports": "all"} if p != "icmp" else {}))
             for p in ("tcp", "udp", "icmp")]
    hosts = sorted({h for h in (resolver_host(d) for d in dns) if h})
    if hosts:
        rules += [{"protocol": p, "ports": "53", "destinations": {"addresses": hosts}} for p in ("udp", "tcp")]
    return rules


def normal(r, direction):
    """One of DigitalOcean's rules as {direction, protocol, ports, addresses}. A rule naming anything but addresses
    (Droplets, tags, load balancers) reads back as something metacomputer didn't set."""
    where = r.get("sources" if direction == "in" else "destinations") or {}
    addrs = list(where.get("addresses") or [])
    for k, v in where.items():
        if k != "addresses" and v:
            addrs.append(f"({k})")
    p = r.get("protocol")
    port = str(r.get("ports") or "")
    ports = None if p == "icmp" else ("all" if port in ("", "0", "all", "1-65535") else port)
    return {"direction": direction, "protocol": p, "ports": ports, "addresses": tuple(sorted(addrs))}


class DigitalOceanAPI(API):
    label = "DigitalOcean"
    base = "https://api.digitalocean.com/v2"
    env_key = ENV_KEY

    def refused(self, status, answer, what=""):
        import json
        code, message = "", ""
        try:
            j = json.loads(answer) or {}
            code, message = j.get("id", ""), j.get("message", "")
        except (ValueError, AttributeError, TypeError):
            pass
        if status == 422 and ("not available" in message.lower() or "capacity" in message.lower()):
            return Capacity(self.label, status, code, message, what)
        return ProviderError(self.label, status, code, message, what)

    def _all(self, path, key, query=None):
        out, page = [], 1
        while True:
            d = self.call("GET", path, query=dict(query or {}, page=page, per_page=200))
            items = d.get(key) or []
            out += items
            if not ((d.get("links") or {}).get("pages") or {}).get("next") or not items or page >= 100:
                return out
            page += 1

    # Droplets
    @staticmethod
    def _droplet(d):
        m = DROPLET.fullmatch(d.get("name") or "")
        return {"id": str(d["id"]), "name": m.group(2) if m else None, "state": STATES.get(d.get("status"), "UNKNOWN"),
                "shape": d.get("size_slug"), "ad": (d.get("region") or {}).get("slug"), "created": d.get("created_at"),
                "op": m.group(1) if m else None}

    def instances(self, tag_value=None, op=None):
        return [i for i in (self._droplet(d) for d in self._all("/droplets", "droplets"))
                if i["name"] and (tag_value is None or i["name"] == tag_value) and (op is None or i["op"] == op)
                and i["state"] != "TERMINATED"]

    def _get(self, iid):
        d = self.call("GET", f"/droplets/{int(iid)}", missing_ok=True)
        if d is None:
            return None
        if not DROPLET.fullmatch(d["droplet"].get("name") or ""):
            raise AdapterError("that Droplet isn't named as metacomputer names its own; metacomputer leaves it alone")
        return d["droplet"]

    def instance(self, iid):
        d = self._get(iid)
        return None if d is None else self._droplet(d)

    def public_ip(self, iid):
        d = self._get(iid) or {}
        for n in ((d.get("networks") or {}).get("v4") or []):
            if n.get("type") == "public" and n.get("ip_address"):
                return n["ip_address"]
        return None

    def image(self, arch):
        if arch != "x86_64":
            raise AdapterError("DigitalOcean has x86 Droplets only")
        i = self.call("GET", f"/images/{IMAGE}")["image"]
        return {"id": IMAGE, "name": f"{i.get('distribution', 'Ubuntu')} {i.get('name', '24.04')}"}

    def launch(self, name, size, region, image_id, user_data, fw, kid, op):
        if not DROPLET_NAME.fullmatch(name):
            raise AdapterError(f"{name} can't be a Droplet's name at DigitalOcean (letters, digits, '.' and '-')")
        body = {"name": f"mc-{op}-{name}", "region": region, "size": size, "image": image_id, "ssh_keys": [int(kid)],
                "backups": False, "ipv6": False, "monitoring": False, "with_droplet_agent": False,
                "user_data": user_data}
        try:
            d = self.call("POST", "/droplets", body, what="the Droplet")
        except Capacity as e:
            raise Capacity(self.label, e.status, e.code, f"{e.message}: try another region (--region) or size") from None
        return str(d["droplet"]["id"])

    def action(self, iid, what):
        kind = {"START": "power_on", "SOFTSTOP": "shutdown", "STOP": "power_off"}[what]
        self.call("POST", f"/droplets/{int(iid)}/actions", {"type": kind}, what=f"to {kind.replace('_', ' ')} the Droplet")

    def terminate(self, iid):
        self.call("DELETE", f"/droplets/{int(iid)}", missing_ok=True)

    # each computer's own firewall
    def _fw_list(self):
        return self._all("/firewalls", "firewalls")

    def firewalls(self):
        return [{"id": f["id"], "name": f.get("name"), "computer": fw_owner(f.get("name"))[0],
                 "droplets": len(f.get("droplet_ids") or [])}
                for f in self._fw_list() if fw_owner(f.get("name"))[0]]

    def nsg(self, name):
        for f in self._fw_list():
            if fw_owner(f.get("name"))[0] == name:
                return f["id"]
        return None

    def nsg_owner(self, fw):
        """(computer, operation prefix) the firewall with this id is named for, or None when it's gone."""
        d = self.call("GET", f"/firewalls/{fw}", missing_ok=True)
        return None if d is None else fw_owner((d.get("firewall") or {}).get("name"))

    def nsgs_of_op(self, name, op):
        return [f["id"] for f in self._fw_list() if fw_owner(f.get("name")) == (name, op[:12])]

    def keys_of_op(self, name, op):
        return [str(k["id"]) for k in self._all("/account/keys", "ssh_keys")
                if (k.get("name") or "") == f"metacomputer-{name}-{op[:12]}"]

    def _settled(self, fw, droplet=None):
        def ok():
            f = (self.call("GET", f"/firewalls/{fw}") or {}).get("firewall") or {}
            if f.get("status") == "failed":
                raise ProviderError(self.label, "firewall", "failed", "the firewall's rules failed to apply")
            return f.get("status") == "succeeded" and (droplet is None or int(droplet) in (f.get("droplet_ids") or []))
        self.wait(ok, 180, "the firewall's rules taking effect", step=3)

    def make_nsg(self, name, egress, op, on_made=None):
        """A new firewall of this operation's (never one found by name), its id to on_made as soon as it's made."""
        d = self.call("POST", "/firewalls", {"name": fw_name(name, op), "inbound_rules": INBOUND_RULES,
                                             "outbound_rules": outbound(egress)}, what="the firewall")
        fw = d["firewall"]["id"]
        if on_made:
            on_made(fw)
        return fw

    def _vpcs(self, droplet_ids):
        """[(ip_range, resolver)] of these Droplets' VPCs. The resolver is DigitalOcean's own, as its API names it
        (resolver_ip); DigitalOcean documents it as always the network's second-to-last address, which stands in
        when the field isn't there."""
        out = []
        for did in droplet_ids:
            d = (self.call("GET", f"/droplets/{int(did)}", missing_ok=True) or {}).get("droplet") or {}
            if d.get("vpc_uuid"):
                v = (self.call("GET", f"/vpcs/{d['vpc_uuid']}", missing_ok=True) or {}).get("vpc") or {}
                if private(v.get("ip_range") or ""):
                    net = ipaddress.ip_network(v["ip_range"])
                    out.append((v["ip_range"], str(v.get("resolver_ip") or net[-2])))
        return out

    def _vpc_ranges(self, droplet_ids):
        return sorted({r for r, _res in self._vpcs(droplet_ids)})

    def documented_resolvers(self, iid):
        """DigitalOcean's own resolver for the Droplet's VPC, as /32s: nothing read from the computer, and held to the
        same checks as what a Droplet names (check_resolvers: inside its own VPC's range), so an answer whose
        resolver_ip lies outside that range makes no exception."""
        return self.check_resolvers(iid, [res for _r, res in self._vpcs([iid])])

    def nsg_egress(self, fw, egress, dns=()):
        """The firewall's rules for EGRESS, with DNS to exactly the resolver addresses `dns` (the computer's record
        keeps them: lib/remote.py resolvers)."""
        f = self.call("GET", f"/firewalls/{fw}")["firewall"]
        body = {"name": f["name"], "inbound_rules": INBOUND_RULES, "outbound_rules": outbound(egress, dns),
                "droplet_ids": f.get("droplet_ids") or [], "tags": f.get("tags") or []}
        self.call("PUT", f"/firewalls/{fw}", body, what="the firewall's rules")
        self._settled(fw)

    def attach_firewall(self, fw, iid):
        """Applied to the Droplet right after it's made. Its resolver isn't let through yet: that's the exact
        addresses the Droplet itself names, read during its setup (set_resolvers)."""
        self.call("POST", f"/firewalls/{fw}/droplets", {"droplet_ids": [int(iid)]}, what="the firewall for the Droplet")
        self._settled(fw, iid)

    def check_resolvers(self, iid, addresses):
        """The addresses the Droplet named at its first boot, as /32s the exception may name: private ones only
        (public ones are let out already), each inside the Droplet's own VPC and DigitalOcean's own resolver for it
        (its VPC's resolver_ip), at most MAX_RESOLVERS; AdapterError for anything else."""
        vpcs = self._vpcs([iid])
        ranges = [ipaddress.ip_network(r) for r, _res in vpcs]
        resolvers = {res for _r, res in vpcs}
        out = []
        for a in addresses:
            try:
                ip = ipaddress.ip_address(str(a).strip())
            except ValueError:
                raise AdapterError(f"{str(a)[:40]!r} isn't an address") from None
            if ip.version != 4 or not private(f"{ip}/32"):
                continue
            if not any(ip in r for r in ranges):
                raise AdapterError(f"{ip} isn't in the Droplet's own VPC ({', '.join(map(str, ranges)) or 'none'}): no "
                                   "DNS exception is made for it")
            if str(ip) not in resolvers:
                raise AdapterError(f"{ip} isn't DigitalOcean's resolver for the Droplet's VPC ({', '.join(sorted(resolvers))}"
                                   "): no DNS exception is made for another address on that network")
            out.append(f"{ip}/32")
        out = sorted(set(out))
        if len(out) > MAX_RESOLVERS:
            raise AdapterError(f"the Droplet names {len(out)} private resolvers; at most {MAX_RESOLVERS} are let through")
        return out

    def preflight(self):
        """vpc:read, which a create needs (to check the resolver's address against the Droplet's VPC), checked before
        anything is made."""
        try:
            self.call("GET", "/vpcs", query={"per_page": 1})
        except ProviderError as e:
            if e.status in (401, 403):
                raise AdapterError("DigitalOcean's token lacks the vpc:read scope, which making a computer needs (its "
                                   "resolver is checked against its VPC); nothing was made. Add vpc:read to the "
                                   "token's scopes") from None
            raise

    SCOPES = (("droplet:read", "/droplets"), ("firewall:read", "/firewalls"), ("ssh_key:read", "/account/keys"),
              ("vpc:read", "/vpcs"), ("regions:read", "/regions"), ("sizes:read", "/sizes"),
              ("image:read", f"/images/{IMAGE}"), ("actions:read", "/actions"))

    def scope_checks(self):
        """The read scopes a metacomputer token needs, each tried (create, update and delete can't be checked
        without making something): [(scope, ok, detail)]."""
        out = []
        for scope, path in self.SCOPES:
            try:
                self.call("GET", path, query=None if path.startswith("/images/") else {"per_page": 1})
                out.append((scope, True, "readable"))
            except ProviderError as e:
                out.append((scope, False, f"missing: the token lacks {scope}" if e.status in (401, 403)
                            else f"couldn't tell ({e.status})"))
        return out

    def nsg_rules(self, fw):
        d = self.call("GET", f"/firewalls/{fw}", missing_ok=True)
        if d is None:
            return []
        f = d["firewall"]
        return ([normal(r, "in") for r in f.get("inbound_rules") or []]
                + [normal(r, "out") for r in f.get("outbound_rules") or []])

    def egress_of(self, rules_, resolvers=None):
        """internet: every public block, and nothing else but DNS (port 53, TCP and UDP alike) to the resolver's exact
        addresses (`resolvers`, the computer's record; without one, at most MAX_RESOLVERS private /32s); off: nothing;
        anything else, a wider private range on port 53 included: other."""
        dns = [r for r in rules_ if r["direction"] == "out" and r["protocol"] in ("tcp", "udp") and r["ports"] == "53"
               and r["addresses"] and all(resolver_host(a) == a for a in r["addresses"])]
        eg = egress_from([r for r in rules_ if r not in dns], off=[])
        if not dns:
            return "other" if eg == "internet" and resolvers else eg
        got = {p: set() for p in ("tcp", "udp")}
        for r in dns:
            got[r["protocol"]].update(r["addresses"])
        want = set(resolvers) if resolvers is not None else got["udp"]
        if eg != "internet" or got["tcp"] != got["udp"] or got["udp"] != want or len(want) > MAX_RESOLVERS:
            return "other"
        return eg

    def nsg_names(self):
        return [f["computer"] for f in self.firewalls()]

    def delete_nsg(self, name):
        gone = [f["id"] for f in self._fw_list() if fw_owner(f.get("name"))[0] == name]
        for fw in gone:
            self.call("DELETE", f"/firewalls/{fw}", missing_ok=True)
        return bool(gone)

    def delete_nsg_by_id(self, fw, name):
        d = self.call("GET", f"/firewalls/{fw}", missing_ok=True)
        if d is None or fw_owner(d["firewall"].get("name"))[0] != name:
            return False
        self.call("DELETE", f"/firewalls/{fw}", missing_ok=True)
        return True

    # the first-boot key
    @staticmethod
    def _key_of(name):
        return re.compile(rf"metacomputer-{re.escape(name)}-[0-9a-f]{{12}}")

    def keys(self):
        return [{"id": str(k["id"]), "name": k.get("name")} for k in self._all("/account/keys", "ssh_keys")
                if re.fullmatch(r"metacomputer-.+-[0-9a-f]{12}", k.get("name") or "")]

    def make_key(self, name, op, public_key):
        """(its id, whether this call made it). DigitalOcean takes a key once per account: the same key already there
        under this computer's name (an earlier try whose clean-up didn't finish) is used, not made again."""
        if not OP.fullmatch(op):
            raise AdapterError("an operation id is 32 hex digits")
        try:
            d = self.call("POST", "/account/keys", {"name": f"metacomputer-{name}-{op[:12]}", "public_key": public_key},
                          what="the SSH key")
            return str(d["ssh_key"]["id"]), True
        except ProviderError as e:
            if e.status != 422:
                raise
        pat = self._key_of(name)
        for k in self._all("/account/keys", "ssh_keys"):
            if pat.fullmatch(k.get("name") or "") and same_key(k.get("public_key"), public_key):
                return str(k["id"]), False
        raise AdapterError(f"DigitalOcean has {name}'s admin key already under another name: remove it in "
                           "DigitalOcean's console (Settings > Security), then try again")

    def delete_key_by_id(self, kid, name):
        d = self.call("GET", f"/account/keys/{int(kid)}", missing_ok=True)
        if d is None or not self._key_of(name).fullmatch(d["ssh_key"].get("name") or ""):
            return False
        self.call("DELETE", f"/account/keys/{int(kid)}", missing_ok=True)
        return True

    def delete_keys(self, name):
        pat, n = self._key_of(name), 0
        for k in self._all("/account/keys", "ssh_keys"):
            if pat.fullmatch(k.get("name") or ""):
                self.call("DELETE", f"/account/keys/{int(k['id'])}", missing_ok=True)
                n += 1
        return n


class DigitalOcean(VPS):
    """Droplets metacomputer makes at DigitalOcean."""
    name = "digitalocean"
    manifest = MANIFEST
    API = DigitalOceanAPI
    SIZES = SIZES
    DEFAULT_SIZE = "s-2vcpu-4gb"
    DEFAULT_REGION = "nyc3"
    SOFT_STOP = "api"            # DigitalOcean's shutdown action presses the guest's power button (ACPI)
    thing = "Droplet"
    RESOLVER_EXCEPTION = True    # its resolver is a private VPC address: DNS to exactly that, read during the setup

    @staticmethod
    def look_for(name, op):
        return (f"DigitalOcean's console (Droplets named mc-{op}-{name}), or: curl -H \"Authorization: Bearer "
                f"$DIGITALOCEAN_TOKEN\" 'https://api.digitalocean.com/v2/droplets?name=mc-{op}-{name}'")

    def set_resolvers(self, name, addresses=None, egress="internet"):
        """Let DNS through the computer's firewall to exactly its resolver. `addresses`: what the Droplet named at its
        first boot (lib/remote.py takes them then, before anything but cloud-init and bootstrap.sh has run in it, and
        only once): each must be DigitalOcean's own resolver for its VPC. None: the pinned ones, never read from the
        computer again; with none pinned, DigitalOcean's documented resolver for its VPC. Returns them, pinned."""
        st = self._state(name)
        if addresses is None:
            hosts = st.get("resolvers")
            if hosts is None:
                hosts = self.api.documented_resolvers(st["instance"])
        else:
            if st.get("resolvers") is not None:
                raise AdapterError(f"{name}'s resolver was pinned at its first boot; it's never read from the computer "
                                   "again")
            hosts = self.api.check_resolvers(st["instance"], addresses)
        fw = st.get("nsg")                 # the one recorded when it was made (lib/remote.py checks it's still its)
        if fw is None:
            raise AdapterError(f"{name}'s record names no firewall of its own at DigitalOcean: no rule was changed")
        self.api.nsg_egress(fw, check_egress(egress), dns=hosts)
        self.states[name] = dict(st, resolvers=hosts)
        return hosts
