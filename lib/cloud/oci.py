"""Oracle Cloud (OCI): metacomputer makes, starts, stops and deletes Ubuntu 24.04 VMs there (`oci`, place `oci`);
everything else is the generic Ubuntu VM's (lib/cloud/ubuntu_vm.py).

Credentials come only from the environment, so a secrets manager can hand them over for one command (most can run a
command with secrets in its environment); nothing is written to disk and no value is printed:
    OCI_USER, OCI_TENANCY, OCI_FINGERPRINT, OCI_REGION   the API user, its tenancy, its key's fingerprint, the region
    OCI_API_KEY_PEM                                      that API key's private key, as PEM text (not a path)
    OCI_COMPARTMENT                                      the compartment metacomputer works in, and only in
Every instance, network and volume it makes is in that compartment and carries the freeform tag
metacomputer=NAME (its network: metacomputer=network), so `metacomputer cloud status oci` finds them even when this
Mac lost their settings. The network is one VCN with one public subnet, an internet gateway, and a security list
that lets in SSH (port 22) and the ICMP message TCP needs to size its packets, and lets nothing out. Each computer has
a network security group of its own (metacomputer-NAME) on its network card, which lets out only public addresses
(EGRESS=internet: every IPv4 address but the private, shared and reserved ranges of ubuntu_vm.PRIVATE_V4) or nothing
(EGRESS=off). That's outside the guest: root in the computer can't change it. OCI applies no security rule to
169.254.0.0/16, so its metadata service, DNS and NTP there can't be refused from outside; the guest's own firewall
refuses the metadata service (a second layer, which root in the guest could undo). Instances get only the IMDS v2
endpoints, no Oracle Cloud Agent plugins, a public address, and the user-data of lib/cloud/guest.py.

Sizes: arm-2x12 (VM.Standard.A1.Flex: 2 OCPU, 12 GB, the whole Always Free Arm allowance), arm-1x6, x86-1x8 or
x86-2x8 (VM.Standard.E5.Flex, billed; on x86 an OCPU is two hardware threads), and x86-micro (VM.Standard.E2.1.Micro:
Always Free, but 1 GB and a fraction of a core: slow, for trying it out). The image is Canonical's newest Ubuntu 24.04
(not Minimal) for the shape's architecture, as OCI lists it.

The SDK (`pip install oci`, any Python the CLI runs as OCI_PYTHON) is imported only when a call needs it; the
adapter takes another `api` (tests: a stand-in), so nothing here reaches the network on its own."""
import base64
import contextlib
import os
import time

from .adapter import AdapterError, NotSupported, check_name
from .ubuntu_vm import BASE, RemoteVM, check_egress, public_v4

ENV_KEYS = ("OCI_USER", "OCI_TENANCY", "OCI_FINGERPRINT", "OCI_REGION", "OCI_API_KEY_PEM", "OCI_COMPARTMENT")
TAG = "metacomputer"
NETWORK_TAG = "network"
OP_TAG = "metacomputer-op"      # the build that launched it: a launch whose record was lost is found by this
VCN_CIDR = "10.77.0.0/16"
SUBNET_CIDR = "10.77.0.0/24"
BOOT_GB = 50

# size -> (shape, OCPUs, memory GB, architecture)
SIZES = {
    "arm-2x12": ("VM.Standard.A1.Flex", 2, 12, "arm64"),
    "arm-1x6": ("VM.Standard.A1.Flex", 1, 6, "arm64"),
    "x86-1x8": ("VM.Standard.E5.Flex", 1, 8, "x86_64"),
    "x86-2x8": ("VM.Standard.E5.Flex", 2, 8, "x86_64"),
    "x86-micro": ("VM.Standard.E2.1.Micro", 1, 1, "x86_64"),
}
DEFAULT_SIZE = "arm-2x12"
# Oracle's list prices (USD, read 2026-11): per OCPU-hour and per GB-hour of memory while running, and the boot
# volume's storage per GB-month whether it runs or not. The Always Free allowance covers Arm (A1) up to 2 OCPU and
# 12 GB in all for the tenancy, and 200 GB of block volumes: arm-2x12 or arm-1x6 with its 50 GB disk costs nothing
# inside it.
PRICES = {"VM.Standard.A1.Flex": (0.01, 0.0015), "VM.Standard.E5.Flex": (0.03, 0.002),
          "VM.Standard.E2.1.Micro": (0.0, 0.0)}
BOOT_GB_MONTH = 0.0255
FREE_SHAPES = ("VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro")

STATES = {"RUNNING": "awake", "STARTING": "starting", "PROVISIONING": "starting", "STOPPING": "stopping",
          "STOPPED": "stopped", "TERMINATING": "absent", "TERMINATED": "absent", "CREATING_IMAGE": "awake",
          "MOVING": "starting"}

MANIFEST = dict(BASE, name="oci", place="oci", label="Oracle Cloud",
                # Stop and start: the boot volume keeps everything; running programs don't survive a stop.
                sleep_wake={"kind": "stop-start", "keeps_processes": False},
                sizes=tuple(SIZES),
                # The computer's own network security group (outside the guest): egress only to public addresses,
                # or none with EGRESS=off. OCI doesn't apply security rules to 169.254.0.0/16, so its metadata
                # service (and its DNS) can't be refused from outside: only the guest's firewall refuses them.
                outside_egress={"enforced": True, "private_ranges": True, "metadata": False, "off": True},
                # Stopped through the API, a standard shape bills only its boot volume. A guest that powers itself
                # off (the idle stop) still counts as running, and bills, until it's stopped through the API.
                billing={"stopped": "storage", "guest_poweroff": "nothing",
                         "note": "stopped through the API: only the boot volume is billed (standard shapes); a guest "
                                 "that powered itself off is still billed until metacomputer stop"})


class Capacity(AdapterError):
    """OCI has no room for that shape in that availability domain now ("Out of host capacity")."""


# A soft stop: OCI gives the guest 15 minutes (900 s) to shut down before it powers it off; then the instance's own
# transition to STOPPED. A stop is waited for that long, so a valid stop is never reported as failed.
SHUTDOWN_WINDOW_S = 900
STOP_WAIT_S = SHUTDOWN_WINDOW_S + 180
CONSOLE_CHUNK = 262144              # bytes per read of a console capture (OCI keeps up to 1 MB of it)


def credentials(env=None):
    """The SDK's config from the environment: KeyError-free, refusing (AdapterError) with the names that are missing,
    never their values."""
    env = os.environ if env is None else env
    missing = [k for k in ENV_KEYS if not (env.get(k) or "").strip()]
    if missing:
        raise AdapterError("Oracle Cloud needs its credentials in the environment (from your secrets manager, for "
                           f"this command only): {', '.join(missing)} missing")
    return {"user": env["OCI_USER"].strip(), "tenancy": env["OCI_TENANCY"].strip(),
            "fingerprint": env["OCI_FINGERPRINT"].strip(), "region": env["OCI_REGION"].strip(),
            "key_content": env["OCI_API_KEY_PEM"]}, env["OCI_COMPARTMENT"].strip()


def size_of(size):
    s = size or DEFAULT_SIZE
    if s not in SIZES:
        raise NotSupported("oci", "size", f"Oracle Cloud has no size {s!r} here: {', '.join(SIZES)}")
    return s


def egress_of(rules):
    """What a computer's group lets out, read back from its rules: "off" (nothing), "internet" (exactly every public
    IPv4 block, any protocol, and nothing else), or "other" (anything else: not what metacomputer set)."""
    out = [r for r in rules if r["direction"] == "EGRESS"]
    if not out:
        return "off"
    if sorted(r["destination"] for r in out) == sorted(public_v4()) and all(r["protocol"] == "all" for r in out):
        return "internet"
    return "other"


class SDK:
    """The calls the adapter makes, on Oracle's Python SDK. Each takes and returns plain values (dicts, strings), so
    a stand-in can take its place in the tests. Nothing in it prints a credential."""

    def __init__(self, env=None):
        cfg, self.compartment = credentials(env)
        try:
            import oci  # noqa: PLC0415  (only here: the rest of metacomputer never needs the SDK)
        except ImportError:
            raise AdapterError("Oracle's Python SDK isn't installed for this Python: pip install oci in a venv, then "
                               "set OCI_PYTHON to its python (docs/remote.md, Oracle Cloud)") from None
        self.oci = oci
        oci.config.validate_config(cfg)
        self.cfg = cfg
        # Oracle's own retry strategy: it waits and tries again on throttling (429, as after a stop and a start in a
        # row) and on the service's passing errors, with backoff, then gives up. Never for a launch (below).
        retry = oci.retry.DEFAULT_RETRY_STRATEGY
        self.compute = oci.core.ComputeClient(cfg, retry_strategy=retry)
        self.net = oci.core.VirtualNetworkClient(cfg, retry_strategy=retry)
        self.identity = oci.identity.IdentityClient(cfg, retry_strategy=retry)

    def _all(self, fn, *a, **k):
        return self.oci.pagination.list_call_get_all_results(fn, *a, **k).data

    # reads
    def availability_domains(self, shape=None):
        """The region's availability domains; with a shape, only those that offer it."""
        ads = [a.name for a in self.identity.list_availability_domains(self.cfg["tenancy"]).data]
        if shape is None:
            return ads
        return [ad for ad in ads if any(s.shape == shape for s in self._all(self.compute.list_shapes, self.compartment,
                                                                            availability_domain=ad))]

    def image(self, shape):
        imgs = self.compute.list_images(self.compartment, operating_system="Canonical Ubuntu",
                                        operating_system_version="24.04", shape=shape, sort_by="TIMECREATED",
                                        sort_order="DESC").data
        for i in imgs:
            if "Minimal" not in (i.display_name or ""):
                return {"id": i.id, "name": i.display_name}
        raise AdapterError(f"Oracle Cloud lists no Ubuntu 24.04 image for {shape} in this region")

    def instances(self, tag_value=None, op=None):
        out = []
        for i in self._all(self.compute.list_instances, self.compartment):
            tags = i.freeform_tags or {}
            t = tags.get(TAG)
            if (t and (tag_value is None or t == tag_value) and (op is None or tags.get(OP_TAG) == op)
                    and i.lifecycle_state not in ("TERMINATED", "TERMINATING")):
                out.append({"id": i.id, "name": t, "state": i.lifecycle_state, "shape": i.shape,
                            "ad": i.availability_domain, "created": str(i.time_created), "op": tags.get(OP_TAG)})
        return out

    def instance(self, iid):
        try:
            i = self.compute.get_instance(iid).data
        except self.oci.exceptions.ServiceError as e:
            if e.status == 404:
                return None
            raise
        if i.compartment_id != self.compartment:
            raise AdapterError("that instance isn't in the configured compartment; metacomputer leaves it alone")
        return {"id": i.id, "state": i.lifecycle_state, "shape": i.shape, "name": (i.freeform_tags or {}).get(TAG),
                "ad": i.availability_domain, "op": (i.freeform_tags or {}).get(OP_TAG)}

    def public_ip(self, iid):
        for att in self._all(self.compute.list_vnic_attachments, self.compartment, instance_id=iid):
            if att.lifecycle_state != "ATTACHED":
                continue
            vnic = self.net.get_vnic(att.vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
        return None

    # the network
    def networks(self):
        """metacomputer's VCNs in the compartment, oldest first. One, normally: two creates at once from different
        Macs could each make one (one Mac takes a lock for it); the oldest is the one used, and delete_network
        deletes them all."""
        return sorted((v for v in self._all(self.net.list_vcns, self.compartment)
                       if (v.freeform_tags or {}).get(TAG) == NETWORK_TAG and v.lifecycle_state == "AVAILABLE"),
                      key=lambda v: (str(v.time_created), v.id))

    def network(self):
        """{vcn, subnet, igw} ids of metacomputer's network in the compartment (the oldest, if there are more), or
        None."""
        vcns = self.networks()
        return self._network_of(vcns[0]) if vcns else None

    def make_network(self):
        m, tags = self.oci.core.models, {TAG: NETWORK_TAG}
        wait = self.oci.wait_until
        n = self.network()
        if n is None:
            v = self.net.create_vcn(m.CreateVcnDetails(compartment_id=self.compartment, cidr_blocks=[VCN_CIDR],
                                                       display_name="metacomputer", dns_label="metacomputer",
                                                       freeform_tags=tags)).data
            wait(self.net, self.net.get_vcn(v.id), "lifecycle_state", "AVAILABLE", max_wait_seconds=300)
            n = self.network()
        if not n["igw"]:
            g = self.net.create_internet_gateway(m.CreateInternetGatewayDetails(
                compartment_id=self.compartment, vcn_id=n["vcn"], is_enabled=True, display_name="metacomputer",
                freeform_tags=tags)).data
            wait(self.net, self.net.get_internet_gateway(g.id), "lifecycle_state", "AVAILABLE", max_wait_seconds=300)
            n["igw"] = g.id
        # The VCN's own route table and security list, made to say exactly this: out through the gateway; in, SSH
        # and the ICMP "fragmentation needed" message (path MTU) only; out, nothing: each computer's own network
        # security group says what it may reach (nsg_egress).
        self.net.update_route_table(n["route_table"], m.UpdateRouteTableDetails(route_rules=[
            m.RouteRule(destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=n["igw"])]))
        self.net.update_security_list(n["security_list"], m.UpdateSecurityListDetails(
            ingress_security_rules=[
                m.IngressSecurityRule(protocol="6", source="0.0.0.0/0", source_type="CIDR_BLOCK", is_stateless=False,
                                      tcp_options=m.TcpOptions(destination_port_range=m.PortRange(min=22, max=22)),
                                      description="metacomputer: SSH, key only"),
                m.IngressSecurityRule(protocol="1", source="0.0.0.0/0", source_type="CIDR_BLOCK", is_stateless=False,
                                      icmp_options=m.IcmpOptions(type=3, code=4),
                                      description="metacomputer: path MTU (fragmentation needed)")],
            egress_security_rules=[]))
        if not n["subnet"]:
            s = self.net.create_subnet(m.CreateSubnetDetails(
                compartment_id=self.compartment, vcn_id=n["vcn"], cidr_block=SUBNET_CIDR, display_name="metacomputer",
                dns_label="public", prohibit_public_ip_on_vnic=False, route_table_id=n["route_table"],
                security_list_ids=[n["security_list"]], freeform_tags=tags)).data
            wait(self.net, self.net.get_subnet(s.id), "lifecycle_state", "AVAILABLE", max_wait_seconds=300)
            n["subnet"] = s.id
        return n

    # each computer's own network security group: what it may reach, outside the guest
    def nsg(self, name):
        """The id of the computer's network security group, or None."""
        n = self.network()
        if not n:
            return None
        for g in self._all(self.net.list_network_security_groups, compartment_id=self.compartment, vcn_id=n["vcn"]):
            if (g.freeform_tags or {}).get(TAG) == name and g.lifecycle_state == "AVAILABLE":
                return g.id
        return None

    def make_nsg(self, name, egress):
        n = self.network() or self.make_network()
        gid = self.nsg(name)
        if gid is None:
            m = self.oci.core.models
            g = self.net.create_network_security_group(m.CreateNetworkSecurityGroupDetails(
                compartment_id=self.compartment, vcn_id=n["vcn"], display_name=f"metacomputer-{name}",
                freeform_tags={TAG: name})).data
            self.oci.wait_until(self.net, self.net.get_network_security_group(g.id), "lifecycle_state", "AVAILABLE",
                                max_wait_seconds=300)
            gid = g.id
        self.nsg_egress(gid, egress)
        return gid

    def nsg_egress(self, gid, egress):
        """Make the group's rules exactly: egress to every public IPv4 block (internet), or none (off); no ingress
        (the subnet's security list lets in SSH)."""
        m = self.oci.core.models
        old = [r.id for r in self._all(self.net.list_network_security_group_security_rules, gid)]
        for i in range(0, len(old), 25):
            self.net.remove_network_security_group_security_rules(
                gid, m.RemoveNetworkSecurityGroupSecurityRulesDetails(security_rule_ids=old[i:i + 25]))
        if check_egress(egress) == "internet":
            rules = [m.AddSecurityRuleDetails(direction="EGRESS", protocol="all", destination=cidr,
                                              destination_type="CIDR_BLOCK", is_stateless=False,
                                              description="metacomputer: public addresses only")
                     for cidr in public_v4()]
            for i in range(0, len(rules), 25):
                self.net.add_network_security_group_security_rules(
                    gid, m.AddNetworkSecurityGroupSecurityRulesDetails(security_rules=rules[i:i + 25]))

    def nsg_rules(self, gid):
        return [{"direction": r.direction, "protocol": r.protocol, "destination": r.destination, "source": r.source}
                for r in self._all(self.net.list_network_security_group_security_rules, gid)]

    def delete_nsg(self, name):
        gid = self.nsg(name)
        if gid:
            self._delete(self.net.delete_network_security_group, self.net.get_network_security_group, gid)
        return bool(gid)

    def console_tail(self, iid, lines=40):
        """The last lines of the instance's serial console (captured now): what the guest itself last said."""
        m = self.oci.core.models
        h = self.compute.capture_console_history(m.CaptureConsoleHistoryDetails(instance_id=iid)).data
        try:
            for _ in range(30):
                if self.compute.get_console_history(h.id).data.lifecycle_state == "SUCCEEDED":
                    break
                time.sleep(2)
            # Read through to the end of the capture (OCI says what's left in opc-bytes-remaining): its last line
            # is what the guest said last, not the last line of its first part.
            text, offset = "", 0
            for _ in range(64):
                r = self.compute.get_console_history_content(h.id, offset=offset, length=CONSOLE_CHUNK)
                chunk = r.data if isinstance(r.data, str) else (r.data or b"").decode(errors="replace")
                text = (text + chunk)[-2 * CONSOLE_CHUNK:]
                left = int(((r.headers or {}).get("opc-bytes-remaining") or "0").strip() or 0)
                if left <= 0 or not chunk:
                    break
                offset += CONSOLE_CHUNK
        finally:
            try:
                self.compute.delete_console_history(h.id)
            except self.oci.exceptions.ServiceError:
                pass
        return [ln for ln in text.splitlines() if ln.strip()][-lines:]

    def security_rules(self):
        n = self.network()
        if not n:
            return None
        sl = self.net.get_security_list(n["security_list"]).data
        return [{"protocol": r.protocol, "source": r.source,
                 "ports": (r.tcp_options.destination_port_range.min, r.tcp_options.destination_port_range.max)
                 if r.tcp_options and r.tcp_options.destination_port_range else None,
                 "icmp": (r.icmp_options.type, r.icmp_options.code) if r.icmp_options else None}
                for r in sl.ingress_security_rules]

    def _gone(self, get, rid, timeout=600):
        """Wait until a deleted resource is TERMINATED or not found (it can be gone before the first look)."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                if get(rid).data.lifecycle_state == "TERMINATED":
                    return
            except self.oci.exceptions.ServiceError as e:
                if e.status == 404:
                    return
                raise
            time.sleep(3)
        raise AdapterError("a network resource wasn't deleted in time; metacomputer cloud status oci shows what's left")

    def network_by_id(self, vcn_id):
        """metacomputer's network whose VCN is exactly `vcn_id` (tagged, in the compartment, available), or None."""
        for v in self.networks():
            if v.id == vcn_id:
                return self._network_of(v)
        return None

    def _network_of(self, v):
        subs = [s for s in self._all(self.net.list_subnets, self.compartment, vcn_id=v.id) if s.lifecycle_state == "AVAILABLE"]
        igws = [g for g in self._all(self.net.list_internet_gateways, self.compartment, vcn_id=v.id)
                if g.lifecycle_state == "AVAILABLE"]
        return {"vcn": v.id, "subnet": subs[0].id if subs else None, "igw": igws[0].id if igws else None,
                "security_list": v.default_security_list_id, "route_table": v.default_route_table_id}

    def delete_network_by_id(self, vcn_id):
        """Deletes the network whose VCN is exactly `vcn_id`, if it's still metacomputer's and has no security group
        left in it; never another one."""
        n = self.network_by_id(vcn_id)
        if not n:
            return False
        groups = [g for g in self._all(self.net.list_network_security_groups, compartment_id=self.compartment, vcn_id=vcn_id)
                  if g.lifecycle_state not in ("TERMINATED", "TERMINATING")]
        if groups:
            return False
        self._delete_network_parts(n)
        return True

    def delete_nsg_by_id(self, gid, name):
        """Deletes the security group `gid` if it's still the one of computer `name`; never another one."""
        try:
            g = self.net.get_network_security_group(gid).data
        except self.oci.exceptions.ServiceError as e:
            if e.status == 404:
                return False
            raise
        if (g.freeform_tags or {}).get(TAG) != name or g.lifecycle_state in ("TERMINATED", "TERMINATING") \
                or g.compartment_id != self.compartment:
            return False
        self._delete(self.net.delete_network_security_group, self.net.get_network_security_group, gid)
        return True

    def delete_network(self):
        """Deletes every network of metacomputer's in the compartment (called once no instance is left)."""
        done = False
        while self._delete_one_network():
            done = True
        return done

    def _delete_one_network(self):
        n = self.network()
        if not n:
            return False
        for g in self._all(self.net.list_network_security_groups, compartment_id=self.compartment, vcn_id=n["vcn"]):
            if g.lifecycle_state != "TERMINATED" and (g.freeform_tags or {}).get(TAG):
                self._delete(self.net.delete_network_security_group, self.net.get_network_security_group, g.id)
        self._delete_network_parts(n)
        return True

    def _delete_network_parts(self, n):
        m = self.oci.core.models
        if n["subnet"]:
            self._delete(self.net.delete_subnet, self.net.get_subnet, n["subnet"])
        try:
            self.net.update_route_table(n["route_table"], m.UpdateRouteTableDetails(route_rules=[]))
        except self.oci.exceptions.ServiceError as e:
            if e.status != 404:
                raise
        if n["igw"]:
            self._delete(self.net.delete_internet_gateway, self.net.get_internet_gateway, n["igw"])
        self._delete(self.net.delete_vcn, self.net.get_vcn, n["vcn"])

    def _delete(self, delete, get, rid):
        """Delete a network resource and wait until it's gone; one already gone (404) is fine."""
        try:
            delete(rid)
        except self.oci.exceptions.ServiceError as e:
            if e.status != 404:
                raise
        self._gone(get, rid)

    def nsg_names(self):
        """The computers that have a security group in metacomputer's network(s) (made with, or for, an instance)."""
        out = []
        for v in self.networks():
            for g in self._all(self.net.list_network_security_groups, compartment_id=self.compartment, vcn_id=v.id):
                if g.lifecycle_state not in ("TERMINATED", "TERMINATING") and (g.freeform_tags or {}).get(TAG):
                    out.append(g.freeform_tags[TAG])
        return out

    # instances
    def launch(self, name, ad, shape, ocpus, memory_gb, image_id, subnet_id, user_data, nsg_id=None, op=None):
        m = self.oci.core.models
        try:
            i = self.compute.launch_instance(m.LaunchInstanceDetails(
                compartment_id=self.compartment, availability_domain=ad, display_name=f"metacomputer-{name}",
                shape=shape, shape_config=(m.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memory_gb)
                                           if shape.endswith(".Flex") else None),
                source_details=m.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=BOOT_GB),
                create_vnic_details=m.CreateVnicDetails(subnet_id=subnet_id, assign_public_ip=True,
                                                        display_name=f"metacomputer-{name}",
                                                        nsg_ids=[nsg_id] if nsg_id else None),
                metadata={"user_data": base64.b64encode(user_data.encode()).decode()},
                instance_options=m.InstanceOptions(are_legacy_imds_endpoints_disabled=True),
                agent_config=m.LaunchInstanceAgentConfigDetails(is_monitoring_disabled=True,
                                                                is_management_disabled=True,
                                                                are_all_plugins_disabled=True),
                freeform_tags=dict({TAG: name}, **({OP_TAG: op} if op else {}))),
                # Tried exactly once: "Out of host capacity" is a 500, which Oracle's default strategy retries for up
                # to ten minutes, and a launch retried after a lost answer could make a second instance.
                retry_strategy=self.oci.retry.NoneRetryStrategy()).data
        except self.oci.exceptions.ServiceError as e:
            if "capacity" in (e.message or "").lower():
                raise Capacity(f"Oracle Cloud is out of {shape} capacity in {ad.split(':')[-1]} now") from None
            err = AdapterError(f"Oracle Cloud refused the instance: {e.status} {e.code}: {e.message}")
            err.status = e.status          # a 4xx: Oracle made nothing; a 5xx proves nothing (lib/remote.py)
            raise err from None
        return i.id

    def wait_state(self, iid, want, timeout=600):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            i = self.instance(iid)
            st = i["state"] if i else "TERMINATED"
            if st == want:
                return st
            if st == "TERMINATED" and want != "TERMINATED":
                raise AdapterError("the instance was terminated")
            time.sleep(5)
        raise AdapterError(f"the instance didn't reach {want} in {timeout} s")

    def action(self, iid, what):
        self.compute.instance_action(iid, what)

    def terminate(self, iid):
        self.compute.terminate_instance(iid, preserve_boot_volume=False)

    def boot_volumes(self):
        """Boot volumes in the compartment that are metacomputer's (made with one of its instances) and still there."""
        out = []
        for ad in self.availability_domains():
            bs = self.oci.core.BlockstorageClient(self.cfg, retry_strategy=self.oci.retry.DEFAULT_RETRY_STRATEGY)
            for v in self._all(bs.list_boot_volumes, availability_domain=ad, compartment_id=self.compartment):
                if v.lifecycle_state != "TERMINATED" and (v.display_name or "").startswith("metacomputer-"):
                    out.append({"id": v.id, "name": v.display_name, "state": v.lifecycle_state})
        return out


class OCI(RemoteVM):
    """VMs that metacomputer makes in Oracle Cloud. `api` is the SDK facade (default: SDK() from the environment's
    credentials, made at the first call that needs it)."""
    name = "oci"
    manifest = MANIFEST
    STOP_WAIT_S = STOP_WAIT_S
    SOFT_STOP = "api"            # OCI's SOFTSTOP asks the guest to shut down (ACPI)
    takes_key = False            # OCI makes an instance without a key or a root password
    thing = "instance"
    size_of = staticmethod(size_of)

    @staticmethod
    def look_for(name, op):
        return (f"Oracle Cloud's console (Compute > Instances in the compartment, freeform tag metacomputer-op={op}), or: "
                f"oci compute instance list --compartment-id \"$OCI_COMPARTMENT\" --query "
                f"\"data[?\\\"freeform-tags\\\".\\\"metacomputer-op\\\"=='{op}']\"")

    @staticmethod
    def arch(size):
        return SIZES[size_of(size)][3]

    def __init__(self, computers=None, states=None, api=None, env=None, network_lock=None):
        super().__init__(computers, states)
        self._api = api
        self._env = env
        # Held while the shared network (and a computer's group in it) is made: lib/remote.py passes a lock that one
        # process on this Mac holds at a time, so two creates at once don't each make a network.
        self._network_lock = network_lock or contextlib.nullcontext

    @property
    def api(self):
        if self._api is None:
            self._api = SDK(self._env)
        return self._api

    def _state(self, name):
        check_name(name)
        st = self.states.get(name) or {}
        if not st.get("instance"):
            raise AdapterError(f"{name} has no instance in Oracle Cloud that this Mac knows of (metacomputer "
                               "cloud status oci lists metacomputer's instances there)")
        return st

    # lifecycle
    def create(self, name, region=None, size=None, persist="auto", user_data=None, ads=None, egress="internet",
               op=None, made=None):
        """Launch the instance for `name` (its network and its own security group first). Tries the availability
        domains in order once each when one has no capacity, then stops. `op` tags the instance with the build that
        launched it, so a launch whose record was lost can be found (and deleted). Returns {instance, ad, shape,
        size, image, arch, nsg}."""
        check_name(name)
        from . import capabilities
        if region is not None:
            raise NotSupported(self.name, "region", "Oracle Cloud's region is your credentials' (OCI_REGION)")
        try:
            capabilities.choose_persist(self.manifest, persist)
        except capabilities.Unavailable as e:
            raise NotSupported(self.name, "PERSIST", str(e)) from None
        sz = size_of(size)
        shape, ocpus, mem, arch = SIZES[sz]
        if not user_data:
            raise AdapterError("a new computer needs its user-data (lib/cloud/guest.py; metacomputer vm create makes it)")
        api = self.api
        if api.instances(name):
            raise AdapterError(f"Oracle Cloud has an instance for {name} already (metacomputer cloud status oci)")
        with self._network_lock():
            # What this operation makes is told to `made` here, under the lock, when it's made: an absence seen
            # earlier proves nothing (another create or `cloud setup` could have made it since).
            new_net = api.network() is None
            net = api.make_network()
            if new_net and made:
                made("made_network", net["vcn"])
            new_nsg = api.nsg(name) is None
            nsg = api.make_nsg(name, egress)
            if new_nsg and made:
                made("made_nsg", nsg)
        img = api.image(shape)
        tried = []
        ads = ads or api.availability_domains(shape)
        if not ads:
            raise AdapterError(f"no availability domain in this region offers {shape} to this compartment")
        made = made or (lambda what, rid: None)
        for ad in ads:
            made("launch_state", "sent")     # from here an instance may exist: the journal says so before the call
            try:
                iid = api.launch(name, ad, shape, ocpus, mem, img["id"], net["subnet"], user_data, nsg_id=nsg, op=op)
            except Capacity as e:
                made("launch_state", "refused")
                tried.append(str(e))
                continue
            except AdapterError as e:
                if isinstance(getattr(e, "status", None), int) and 400 <= e.status < 500:
                    made("launch_state", "refused")
                raise
            made("made_instance", iid)       # journaled the moment the launch returns
            made("launch_state", "made")
            return {"instance": iid, "ad": ad, "shape": shape, "size": sz, "image": img["name"], "arch": arch,
                    "ocpus": ocpus, "memory_gb": mem, "nsg": nsg}
        raise Capacity("; ".join(tried) + ". Every availability domain was tried once; try again later, or another size")

    def powered_off(self, name):
        """Whether the guest of a RUNNING instance powered itself off: its serial console's last line says so
        (`reboot: Power down`). OCI doesn't notice a shutdown from inside, so this is the only proof there is; an
        instance whose SSH merely doesn't answer may be busy or cut off, and is never stopped on that ground."""
        st = self._state(name)
        tail = self.api.console_tail(st["instance"])
        return bool(tail) and tail[-1].rstrip().endswith("reboot: Power down")

    def wake(self, name, guest_up=None):
        """Start it. guest_up=False (its SSH doesn't answer though OCI says RUNNING): stopped through the API and
        started again only when its console proves the guest powered itself off (its idle stop); otherwise refused,
        saying what to do: a running computer that doesn't answer is never stopped by `start`."""
        st = self._state(name)
        cur = self.api.instance(st["instance"])
        if cur is None:
            raise AdapterError(f"{name}'s instance is gone from Oracle Cloud")
        if cur["state"] == "RUNNING" and guest_up is False:
            if not self.powered_off(name):
                raise AdapterError(f"{name} runs at Oracle Cloud but its SSH doesn't answer, and its console doesn't "
                                   "show that it powered itself off: it may be busy or cut off. Nothing was stopped. "
                                   f"Check it (metacomputer --vm {name} status); to restart it anyway: metacomputer "
                                   f"--vm {name} stop --force, then start")
            self.api.action(st["instance"], "STOP")
            self.api.wait_state(st["instance"], "STOPPED", timeout=STOP_WAIT_S)
            cur = self.api.instance(st["instance"]) or cur
        if cur["state"] != "RUNNING":
            if cur["state"] == "STOPPING":
                self.api.wait_state(st["instance"], "STOPPED", timeout=STOP_WAIT_S)
            self.api.action(st["instance"], "START")
            self.api.wait_state(st["instance"], "RUNNING")
        return self.status(name)

    def sleep(self, name, guest_up=True, force=False):
        """Stop it: a soft stop (the guest shuts down, then OCI stops it). A plain stop (power off at once) only with
        force, or when its console proves the guest already powered itself off (OCI would wait 15 minutes for a
        shutdown that already happened). Only a stop through the API stops OCI's billing."""
        st = self._state(name)
        cur = self.api.instance(st["instance"])
        if cur and cur["state"] not in ("STOPPED", "STOPPING"):
            hard = force or (guest_up is False and self.powered_off(name))
            self.api.action(st["instance"], "STOP" if hard else "SOFTSTOP")
        if cur:
            self.api.wait_state(st["instance"], "STOPPED", timeout=STOP_WAIT_S)
        return self.status(name)

    def delete(self, name):
        st = self._state(name)
        if self.api.instance(st["instance"]) is not None:
            self.api.terminate(st["instance"])
            self.api.wait_state(st["instance"], "TERMINATED", timeout=900)
        self.api.delete_nsg(name)

    def set_egress(self, name, allow):
        raise NotSupported(self.name, "egress allowlist", "Oracle Cloud's computers have no allowlist of names: "
                           "EGRESS=internet (public addresses only, outside the guest too) or off")

    def apply_egress(self, name, egress):
        """EGRESS for its own security group (outside the guest); the guest's firewall is set over SSH."""
        self._state(name)
        gid = self.api.nsg(name)
        if gid is None:
            raise AdapterError(f"{name} has no security group of its own at Oracle Cloud (made by an earlier version: "
                               "delete it and make it again)")
        self.api.nsg_egress(gid, check_egress(egress))
        return self.egress(name)

    def egress(self, name):
        st = self.states.get(check_name(name)) or {}
        s = self._settings(name)
        out = {"model": "guest-firewall", "setting": check_egress(s.get("EGRESS")), "allow": []}
        if st.get("instance"):
            try:
                api = self.api               # Oracle's API, when the credentials are here (else: not known)
            except AdapterError:
                api = None
            gid = api.nsg(name) if api else None
            rules = api.nsg_rules(gid) if gid else None
            out["outside"] = None if rules is None else egress_of(rules)
        return out

    def status(self, name):
        st = self.states.get(check_name(name)) or {}
        if not st.get("instance"):
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        cur = self.api.instance(st["instance"])
        if cur is None:
            return {"name": name, "place": self.manifest["place"], "state": "absent"}
        out = {"name": name, "place": self.manifest["place"], "state": STATES.get(cur["state"], cur["state"].lower()),
               "provider_state": cur["state"], "persist": "disk", "region": None, "size": st.get("size")}
        if cur["state"] == "RUNNING":
            out["host"] = self.api.public_ip(st["instance"])
        return out

    def list(self):
        return [{"name": i["name"], "place": self.manifest["place"], "state": STATES.get(i["state"], i["state"].lower()),
                 "shape": i["shape"], "instance": i["id"]} for i in self.api.instances()]

    # connection
    def setup(self):
        """Checks the credentials and the compartment, and makes the network (or finds it)."""
        api = self.api
        with self._network_lock():
            n = api.make_network()
        return {"ok": True, "network": n, "note": "the network lets in SSH only (port 22)"}

    def doctor(self):
        out = []
        try:
            api = self.api
        except AdapterError as e:
            return [{"check": "credentials", "ok": False, "detail": str(e)}]
        out.append({"check": "credentials", "ok": True, "detail": "the API answered"})
        ads = api.availability_domains()
        out.append({"check": "availability domains", "ok": bool(ads), "detail": str(len(ads))})
        rules = api.security_rules()
        if rules is None:
            out.append({"check": "network", "ok": True, "detail": "none yet (made with the first computer)"})
        else:
            ok = all((r["protocol"] == "6" and r["ports"] == (22, 22)) or (r["protocol"] == "1" and r["icmp"] == (3, 4))
                     for r in rules)
            out.append({"check": "network lets in SSH only", "ok": ok, "detail": str(rules)})
            for i in api.instances():
                gid = api.nsg(i["name"])
                rs = api.nsg_rules(gid) if gid else []
                bad = [r for r in rs if r["direction"] != "EGRESS" or r["destination"] not in public_v4()]
                out.append({"check": f"{i['name']}: its own security group lets out public addresses only",
                            "ok": bool(gid) and not bad, "detail": f"{len(rs)} rules" if gid else "no group"})
        out.append({"check": "instances", "ok": True, "detail": str(len(api.instances()))})
        return out

    # cost
    def rates(self, size=None):
        shape, ocpus, mem, _arch = SIZES[size_of(size)]
        per_ocpu, per_gb = PRICES[shape]
        disk = round(BOOT_GB * BOOT_GB_MONTH / 730, 5)
        free = {"VM.Standard.A1.Flex": "inside the Always Free allowance (Arm up to 2 OCPU and 12 GB, and 200 GB of "
                                       "disk, in all for the tenancy) it costs nothing",
                "VM.Standard.E2.1.Micro": "inside the Always Free allowance (two of these micro instances, and 200 GB "
                                          "of disk, in all for the tenancy) it costs nothing"}
        note = (f"{shape} {ocpus} OCPU, {mem} GB, {BOOT_GB} GB disk at Oracle's list prices; "
                + free.get(shape, "not in the Always Free allowance"))
        return {"awake_per_hour": round(ocpus * per_ocpu + mem * per_gb + disk, 5), "asleep_per_hour": disk,
                "currency": "USD", "note": note}
