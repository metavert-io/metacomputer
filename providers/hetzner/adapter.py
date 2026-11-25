"""Hetzner Cloud: metacomputer makes, starts, stops and deletes Ubuntu 24.04 servers there (`hetzner`, place
`hetzner`), through Hetzner's REST API (lib/cloud/vps.py has what the three hourly providers share).

Credentials: HCLOUD_TOKEN, an API token of one Hetzner Cloud project (Read & Write), from the environment only. The
project is the boundary: everything metacomputer makes there carries the label metacomputer=NAME (and the build's
metacomputer-op), and a server without that label is never touched.

Each computer has its own Cloud Firewall, made before the server and named in its create call, so the server is
never up without it: in, SSH (TCP 22) only; out, every public IPv4 address (TCP and UDP on every port, and ICMP: the
103 blocks of ubuntu_vm.public_v4, nothing private, shared or link-local) with EGRESS=internet, or nothing with
EGRESS=off. Hetzner lets everything out of a firewall that has no outbound rule, so "nothing" is one outbound rule
that reaches no one: ICMP to 192.0.2.1, a documentation address no network routes (which the guest refuses too).
Hetzner's firewalls are stateful (replies to SSH get out) and, by Hetzner's own documentation, never filter the
metadata service ("answered locally"), DHCP or its DNS resolvers: the metadata service can't be refused from outside;
only the guest's own firewall refuses it.

Servers are made IPv4-only (no IPv6 address), from Hetzner's ubuntu-24.04 image for the size's architecture; their
primary IPv4 address is deleted with them. Sizes: Arm (CAX, Ampere) cax11 (the default: 2 vCPU, 4 GB), cax21,
cax31; x86 cx23 and cx33. These are in Hetzner's EU locations (fsn1, nbg1, hel1). Billed by the hour, up to the
size's monthly price; a stopped server is billed in full, and its IPv4 address too: only deleting it stops the
charges."""
from cloud.adapter import AdapterError
from cloud.ubuntu_vm import public_v4
from cloud.vps import API, OP_TAG, TAG, Capacity, ProviderError, VPS, egress_from, fw_name, same_key

from .manifest import ENV_KEY, MANIFEST, REGIONS, SIZES  # noqa: F401  (its manifest: providers/hetzner/manifest.py)

IPV4_PER_HOUR = 0.001           # the server's primary IPv4 address ($0.60 a month), billed while it exists
HETZNER_ARCH = {"arm64": "arm", "x86_64": "x86"}
STATES = {"initializing": "PROVISIONING", "starting": "STARTING", "running": "RUNNING", "stopping": "STOPPING",
          "off": "STOPPED", "deleting": "TERMINATING", "migrating": "STARTING", "rebuilding": "STARTING"}
# EGRESS=off: Hetzner lets everything out of a firewall without an outbound rule, so it gets one that reaches no one.
OFF_RULE = {"direction": "out", "protocol": "icmp", "destination_ips": ["192.0.2.1/32"],
            "description": "metacomputer: EGRESS=off (one outbound rule must exist; this one reaches no one)"}
ALL_PORTS = "1-65535"
MAX_IPS_PER_RULE = 100          # Hetzner's limit on the addresses of one rule (found live: 103 refused, 100 taken)


def rules(egress):
    """The firewall's rules for EGRESS: SSH in; out, every public IPv4 block, or the one rule that reaches no one."""
    inbound = [{"direction": "in", "protocol": "tcp", "port": "22", "source_ips": ["0.0.0.0/0", "::/0"],
                "description": "metacomputer: SSH, key only"}]
    if egress == "off":
        return inbound + [dict(OFF_RULE)]
    pub = list(public_v4())
    # Hetzner takes at most 100 addresses in one rule: the 103 public blocks go in two rules per protocol.
    parts = [pub[i:i + MAX_IPS_PER_RULE] for i in range(0, len(pub), MAX_IPS_PER_RULE)]
    if len(parts) > 1:
        half = (len(pub) + 1) // 2
        parts = [pub[:half], pub[half:]]
    return inbound + [{"direction": "out", "protocol": p, "destination_ips": part,
                       "description": "metacomputer: public addresses only", **({"port": ALL_PORTS} if p != "icmp" else {})}
                      for p in ("tcp", "udp", "icmp") for part in parts]


def normal(r):
    """One of Hetzner's rules as {direction, protocol, ports, addresses}."""
    port = r.get("port")
    ports = None if r.get("protocol") not in ("tcp", "udp") else ("all" if port in ("any", ALL_PORTS) else str(port))
    addrs = r.get("source_ips") if r.get("direction") == "in" else r.get("destination_ips")
    return {"direction": r.get("direction"), "protocol": r.get("protocol"), "ports": ports,
            "addresses": tuple(sorted(addrs or ()))}


class HetznerAPI(API):
    label = "Hetzner Cloud"
    base = "https://api.hetzner.cloud/v1"
    env_key = ENV_KEY

    def refused(self, status, answer, what=""):
        import json
        code, message = "", ""
        try:
            err = (json.loads(answer) or {}).get("error") or {}
            code, message = err.get("code", ""), err.get("message", "")
        except (ValueError, AttributeError, TypeError):
            pass
        if code in ("resource_unavailable", "placement_error"):
            return Capacity(self.label, status, code, message, what)
        return ProviderError(self.label, status, code, message, what)

    def _all(self, path, key, query=None):
        out, page = [], 1
        while True:
            d = self.call("GET", path, query=dict(query or {}, page=page, per_page=50))
            out += d.get(key) or []
            nxt = ((d.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not nxt or page >= 200:
                return out
            page = nxt

    def _wait_actions(self, kind, actions, timeout=180):
        for a in actions or []:
            def done(aid=a["id"]):
                act = (self.call("GET", f"/{kind}/actions/{aid}") or {}).get("action") or {}
                if act.get("status") == "error":
                    # An action's error comes inside a 200 answer: scrubbed like every other provider error.
                    e = act.get("error") or {}
                    raise self._scrubbed(ProviderError(self.label, "action", e.get("code"), e.get("message")))
                return act.get("status") == "success"
            self.wait(done, timeout, f"{a.get('command', 'an action')}", step=2)

    # servers
    @staticmethod
    def _server(s):
        labels = s.get("labels") or {}
        loc = (s.get("location") or ((s.get("datacenter") or {}).get("location")) or {}).get("name")
        return {"id": str(s["id"]), "name": labels.get(TAG), "state": STATES.get(s.get("status"), "UNKNOWN"),
                "shape": (s.get("server_type") or {}).get("name"), "ad": loc, "created": s.get("created"),
                "op": labels.get(OP_TAG)}

    def instances(self, tag_value=None, op=None):
        sel = f"{TAG}={tag_value}" if tag_value else TAG
        return [i for i in (self._server(s) for s in self._all("/servers", "servers", {"label_selector": sel}))
                if i["name"] and (tag_value is None or i["name"] == tag_value) and (op is None or i["op"] == op)
                and i["state"] != "TERMINATING"]

    def _get_server(self, iid):
        d = self.call("GET", f"/servers/{int(iid)}", missing_ok=True)
        if d is None:
            return None
        s = d["server"]
        if not (s.get("labels") or {}).get(TAG):
            raise AdapterError("that server has no metacomputer label at Hetzner Cloud; metacomputer leaves it alone")
        return s

    def instance(self, iid):
        s = self._get_server(iid)
        return None if s is None else self._server(s)

    def public_ip(self, iid):
        s = self._get_server(iid)
        return (((s or {}).get("public_net") or {}).get("ipv4") or {}).get("ip")

    def image(self, arch):
        imgs = self.call("GET", "/images", query={"type": "system", "name": "ubuntu-24.04",
                                                   "architecture": HETZNER_ARCH[arch]}).get("images") or []
        for i in imgs:
            if i.get("status") == "available":
                return {"id": i["id"], "name": f"{i['name']} ({i.get('architecture')})"}
        raise AdapterError(f"Hetzner Cloud lists no ubuntu-24.04 image for {arch}")

    def launch(self, name, size, region, image_id, user_data, fw, kid, op):
        body = {"name": f"metacomputer-{name}", "server_type": size, "image": image_id, "location": region,
                "ssh_keys": [int(kid)], "user_data": user_data, "labels": {TAG: name, OP_TAG: op},
                "firewalls": [{"firewall": int(fw)}], "start_after_create": True, "automount": False,
                "public_net": {"enable_ipv4": True, "enable_ipv6": False}}
        try:
            d = self.call("POST", "/servers", body, what="the server")
        except Capacity as e:
            raise self._scrubbed(Capacity(self.label, e.status, e.code, f"no {size} free in {region} now ({e.message}): "
                                          "try again later, or another region (--region) or size")) from None
        return str(d["server"]["id"])       # (its root_password is null: a key was given; nothing else is read)

    def action(self, iid, what):
        verb = {"START": "poweron", "SOFTSTOP": "shutdown", "STOP": "poweroff"}[what]
        self.call("POST", f"/servers/{int(iid)}/actions/{verb}", {}, what=f"to {verb} the server")

    def terminate(self, iid):
        self.call("DELETE", f"/servers/{int(iid)}", missing_ok=True)

    # each computer's own firewall
    def firewalls(self):
        return [{"id": str(f["id"]), "name": f.get("name"), "computer": (f.get("labels") or {}).get(TAG),
                 "applied_to": len(f.get("applied_to") or [])}
                for f in self._all("/firewalls", "firewalls", {"label_selector": TAG})]

    def nsg(self, name):
        for f in self._all("/firewalls", "firewalls", {"label_selector": f"{TAG}={name}"}):
            if (f.get("labels") or {}).get(TAG) == name:
                return str(f["id"])
        return None

    def make_nsg(self, name, egress, op, on_made=None):
        """A new firewall of this operation's (never one found by name): its id goes to on_made as soon as the
        create returns, before its actions are waited for."""
        d = self.call("POST", "/firewalls", {"name": fw_name(name, op), "labels": {TAG: name, OP_TAG: op},
                                             "rules": rules(egress)}, what="the firewall")
        fw = str(d["firewall"]["id"])
        if on_made:
            on_made(fw)
        self._wait_actions("firewalls", d.get("actions"))
        return fw

    def nsg_owner(self, fw):
        """(computer, operation) the firewall with this id is marked for (its labels), or None when it's gone."""
        d = self.call("GET", f"/firewalls/{int(fw)}", missing_ok=True)
        if d is None:
            return None
        labels = (d.get("firewall") or {}).get("labels") or {}
        return labels.get(TAG), labels.get(OP_TAG)

    def nsgs_of_op(self, name, op):
        """The firewalls marked with this computer and operation (one whose create answer was lost is found so)."""
        return [str(f["id"]) for f in self._all("/firewalls", "firewalls", {"label_selector": f"{TAG}={name}"})
                if (f.get("labels") or {}).get(OP_TAG) == op]

    def keys_of_op(self, name, op):
        return [str(k["id"]) for k in self._all("/ssh_keys", "ssh_keys", {"label_selector": f"{TAG}={name}"})
                if (k.get("labels") or {}).get(OP_TAG) == op]

    def nsg_egress(self, fw, egress):
        d = self.call("POST", f"/firewalls/{int(fw)}/actions/set_rules", {"rules": rules(egress)},
                      what="the firewall's rules")
        self._wait_actions("firewalls", d.get("actions"))

    def nsg_rules(self, fw):
        d = self.call("GET", f"/firewalls/{int(fw)}", missing_ok=True)
        return [] if d is None else [normal(r) for r in d["firewall"].get("rules") or []]

    def egress_of(self, rules_):
        return egress_from(rules_, off=[normal(OFF_RULE)])

    def nsg_names(self):
        return [f["computer"] for f in self.firewalls() if f["computer"]]

    def _delete_firewall(self, fw):
        # Hetzner refuses while it's still applied to a server being deleted ("resource_in_use"): a few tries.
        def gone():
            try:
                self.call("DELETE", f"/firewalls/{int(fw)}", missing_ok=True)
                return True
            except ProviderError as e:
                if e.code != "resource_in_use":
                    raise
                return False
        self.wait(gone, 120, "deleting the firewall (still in use)", step=5)

    def delete_nsg(self, name):
        fw = self.nsg(name)
        if fw:
            self._delete_firewall(fw)
        return bool(fw)

    def delete_nsg_by_id(self, fw, name):
        d = self.call("GET", f"/firewalls/{int(fw)}", missing_ok=True)
        if d is None or (d["firewall"].get("labels") or {}).get(TAG) != name:
            return False
        self._delete_firewall(fw)
        return True

    # the first-boot key
    def keys(self):
        return [{"id": str(k["id"]), "name": k.get("name"), "computer": (k.get("labels") or {}).get(TAG)}
                for k in self._all("/ssh_keys", "ssh_keys", {"label_selector": TAG})]

    def make_key(self, name, op, public_key):
        """(its id, whether this call made it). Hetzner takes a key once per project: the same key already there for
        this computer (an earlier try whose clean-up didn't finish) is used, not made again."""
        try:
            d = self.call("POST", "/ssh_keys", {"name": f"metacomputer-{name}-{op[:12]}", "public_key": public_key,
                                                "labels": {TAG: name, OP_TAG: op}}, what="the SSH key")
            return str(d["ssh_key"]["id"]), True
        except ProviderError as e:
            if e.code != "uniqueness_error":
                raise
        for k in self._all("/ssh_keys", "ssh_keys", {"label_selector": f"{TAG}={name}"}):
            if same_key(k.get("public_key"), public_key):
                return str(k["id"]), False
        raise AdapterError(f"Hetzner Cloud has {name}'s admin key already under a key that isn't this computer's: "
                           "remove it in Hetzner's console (Security > SSH keys), then try again")

    def delete_key_by_id(self, kid, name):
        d = self.call("GET", f"/ssh_keys/{int(kid)}", missing_ok=True)
        if d is None or (d["ssh_key"].get("labels") or {}).get(TAG) != name:
            return False
        self.call("DELETE", f"/ssh_keys/{int(kid)}", missing_ok=True)
        return True

    def delete_keys(self, name):
        n = 0
        for k in self._all("/ssh_keys", "ssh_keys", {"label_selector": f"{TAG}={name}"}):
            if (k.get("labels") or {}).get(TAG) == name:
                self.call("DELETE", f"/ssh_keys/{int(k['id'])}", missing_ok=True)
                n += 1
        return n

    def inventory(self):
        out = super().inventory()
        # A server's primary IPv4 address bills on its own; metacomputer's are deleted with their servers.
        out["primary_ips"] = [{"id": str(p["id"]), "assignee": p.get("assignee_id"), "auto_delete": p.get("auto_delete")}
                              for p in self._all("/primary_ips", "primary_ips")
                              if (p.get("labels") or {}).get(TAG) or p.get("assignee_id") in
                              {int(i["id"]) for i in self.instances()}]
        return out


class Hetzner(VPS):
    """Servers metacomputer makes at Hetzner Cloud."""
    name = "hetzner"
    manifest = MANIFEST
    API = HetznerAPI
    SIZES = SIZES
    DEFAULT_SIZE = "cax11"
    DEFAULT_REGION = "fsn1"
    EXTRA_PER_HOUR = IPV4_PER_HOUR
    SOFT_STOP = "api"            # Hetzner's shutdown presses the guest's power button (ACPI)

    @staticmethod
    def look_for(name, op):
        """Where to look for what an operation may have made: the console, and the API call."""
        return (f"Hetzner's console (the project's Servers, label metacomputer-op={op}), or: curl -H \"Authorization: "
                f"Bearer $HCLOUD_TOKEN\" 'https://api.hetzner.cloud/v1/servers?label_selector=metacomputer-op%3D{op}'")


ADAPTER = Hetzner
