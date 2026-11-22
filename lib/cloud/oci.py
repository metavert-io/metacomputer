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
that lets in SSH (port 22) and the ICMP message TCP needs to size its packets, and nothing else. Instances get only
the IMDS v2 endpoints, no Oracle Cloud Agent plugins, a public address, and the user-data of lib/cloud/guest.py.

Sizes: arm-2x12 (VM.Standard.A1.Flex: 2 OCPU, 12 GB, the whole Always Free Arm allowance), arm-1x6, x86-1x8 or
x86-2x8 (VM.Standard.E5.Flex, billed; on x86 an OCPU is two hardware threads), and x86-micro (VM.Standard.E2.1.Micro:
Always Free, but 1 GB and a fraction of a core: slow, for trying it out). The image is Canonical's newest Ubuntu 24.04
(not Minimal) for the shape's architecture, as OCI lists it.

The SDK (`pip install oci`, any Python the CLI runs as OCI_PYTHON) is imported only when a call needs it; the
adapter takes another `api` (tests: a stand-in), so nothing here reaches the network on its own."""
import base64
import os
import time

from .adapter import AdapterError, NotSupported, check_name
from .ubuntu_vm import BASE, RemoteVM

ENV_KEYS = ("OCI_USER", "OCI_TENANCY", "OCI_FINGERPRINT", "OCI_REGION", "OCI_API_KEY_PEM", "OCI_COMPARTMENT")
TAG = "metacomputer"
NETWORK_TAG = "network"
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
                sizes=tuple(SIZES))


class Capacity(AdapterError):
    """OCI has no room for that shape in that availability domain now ("Out of host capacity")."""


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
        # row) and on the service's passing errors, with backoff, then gives up.
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

    def instances(self, tag_value=None):
        out = []
        for i in self._all(self.compute.list_instances, self.compartment):
            t = (i.freeform_tags or {}).get(TAG)
            if t and (tag_value is None or t == tag_value) and i.lifecycle_state != "TERMINATED":
                out.append({"id": i.id, "name": t, "state": i.lifecycle_state, "shape": i.shape,
                            "ad": i.availability_domain, "created": str(i.time_created)})
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
                "ad": i.availability_domain}

    def public_ip(self, iid):
        for att in self._all(self.compute.list_vnic_attachments, self.compartment, instance_id=iid):
            if att.lifecycle_state != "ATTACHED":
                continue
            vnic = self.net.get_vnic(att.vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
        return None

    # the network
    def network(self):
        """{vcn, subnet, igw} ids of metacomputer's network in the compartment, or None."""
        vcns = [v for v in self._all(self.net.list_vcns, self.compartment)
                if (v.freeform_tags or {}).get(TAG) == NETWORK_TAG and v.lifecycle_state == "AVAILABLE"]
        if not vcns:
            return None
        v = vcns[0]
        subs = [s for s in self._all(self.net.list_subnets, self.compartment, vcn_id=v.id)
                if s.lifecycle_state == "AVAILABLE"]
        igws = [g for g in self._all(self.net.list_internet_gateways, self.compartment, vcn_id=v.id)
                if g.lifecycle_state == "AVAILABLE"]
        return {"vcn": v.id, "subnet": subs[0].id if subs else None, "igw": igws[0].id if igws else None,
                "security_list": v.default_security_list_id, "route_table": v.default_route_table_id}

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
        # and the ICMP "fragmentation needed" message (path MTU) only; out, anything (the guest firewall decides).
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
            egress_security_rules=[m.EgressSecurityRule(protocol="all", destination="0.0.0.0/0",
                                                        destination_type="CIDR_BLOCK", is_stateless=False)]))
        if not n["subnet"]:
            s = self.net.create_subnet(m.CreateSubnetDetails(
                compartment_id=self.compartment, vcn_id=n["vcn"], cidr_block=SUBNET_CIDR, display_name="metacomputer",
                dns_label="public", prohibit_public_ip_on_vnic=False, route_table_id=n["route_table"],
                security_list_ids=[n["security_list"]], freeform_tags=tags)).data
            wait(self.net, self.net.get_subnet(s.id), "lifecycle_state", "AVAILABLE", max_wait_seconds=300)
            n["subnet"] = s.id
        return n

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

    def delete_network(self):
        n = self.network()
        if not n:
            return False
        m = self.oci.core.models
        if n["subnet"]:
            self.net.delete_subnet(n["subnet"])
            self._gone(self.net.get_subnet, n["subnet"])
        self.net.update_route_table(n["route_table"], m.UpdateRouteTableDetails(route_rules=[]))
        if n["igw"]:
            self.net.delete_internet_gateway(n["igw"])
            self._gone(self.net.get_internet_gateway, n["igw"])
        self.net.delete_vcn(n["vcn"])
        self._gone(self.net.get_vcn, n["vcn"])
        return True

    # instances
    def launch(self, name, ad, shape, ocpus, memory_gb, image_id, subnet_id, user_data):
        m = self.oci.core.models
        try:
            i = self.compute.launch_instance(m.LaunchInstanceDetails(
                compartment_id=self.compartment, availability_domain=ad, display_name=f"metacomputer-{name}",
                shape=shape, shape_config=(m.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memory_gb)
                                           if shape.endswith(".Flex") else None),
                source_details=m.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=BOOT_GB),
                create_vnic_details=m.CreateVnicDetails(subnet_id=subnet_id, assign_public_ip=True,
                                                        display_name=f"metacomputer-{name}"),
                metadata={"user_data": base64.b64encode(user_data.encode()).decode()},
                instance_options=m.InstanceOptions(are_legacy_imds_endpoints_disabled=True),
                agent_config=m.LaunchInstanceAgentConfigDetails(is_monitoring_disabled=True,
                                                                is_management_disabled=True,
                                                                are_all_plugins_disabled=True),
                freeform_tags={TAG: name})).data
        except self.oci.exceptions.ServiceError as e:
            if "capacity" in (e.message or "").lower():
                raise Capacity(f"Oracle Cloud is out of {shape} capacity in {ad.split(':')[-1]} now") from None
            raise AdapterError(f"Oracle Cloud refused the instance: {e.status} {e.code}: {e.message}") from None
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

    def __init__(self, computers=None, states=None, api=None, env=None):
        super().__init__(computers, states)
        self._api = api
        self._env = env

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
    def create(self, name, region=None, size=None, persist="auto", user_data=None, ads=None):
        """Launch the instance for `name` (its network first, if there's none). Tries the availability domains in
        order once each when one has no capacity, then stops. Returns {instance, ad, shape, size, image, arch}."""
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
        net = api.make_network()
        img = api.image(shape)
        tried = []
        ads = ads or api.availability_domains(shape)
        if not ads:
            raise AdapterError(f"no availability domain in this region offers {shape} to this compartment")
        for ad in ads:
            try:
                iid = api.launch(name, ad, shape, ocpus, mem, img["id"], net["subnet"], user_data)
            except Capacity as e:
                tried.append(str(e))
                continue
            return {"instance": iid, "ad": ad, "shape": shape, "size": sz, "image": img["name"], "arch": arch,
                    "ocpus": ocpus, "memory_gb": mem}
        raise Capacity("; ".join(tried) + ". Every availability domain was tried once; try again later, or another size")

    def wake(self, name, guest_up=None):
        """Start it. guest_up=False (its SSH doesn't answer though OCI says RUNNING): the guest powered itself off
        (its idle stop), which OCI doesn't notice, so it's stopped through the API first, then started."""
        st = self._state(name)
        cur = self.api.instance(st["instance"])
        if cur is None:
            raise AdapterError(f"{name}'s instance is gone from Oracle Cloud")
        if cur["state"] == "RUNNING" and guest_up is False:
            self.api.action(st["instance"], "STOP")
            self.api.wait_state(st["instance"], "STOPPED")
            cur = self.api.instance(st["instance"]) or cur
        if cur["state"] != "RUNNING":
            if cur["state"] == "STOPPING":
                self.api.wait_state(st["instance"], "STOPPED")
            self.api.action(st["instance"], "START")
            self.api.wait_state(st["instance"], "RUNNING")
        return self.status(name)

    def sleep(self, name, guest_up=True):
        """Stop it: a soft stop (the guest shuts down, then OCI stops it) while the guest runs; a plain stop when it
        already powered itself off (OCI would wait 15 minutes for a shutdown that already happened). Only a stop
        through the API stops OCI's billing: a guest that powered itself off still counts as running."""
        st = self._state(name)
        cur = self.api.instance(st["instance"])
        if cur and cur["state"] not in ("STOPPED", "STOPPING"):
            self.api.action(st["instance"], "SOFTSTOP" if guest_up else "STOP")
        if cur:
            self.api.wait_state(st["instance"], "STOPPED")
        return self.status(name)

    def delete(self, name):
        st = self._state(name)
        if self.api.instance(st["instance"]) is not None:
            self.api.terminate(st["instance"])
            self.api.wait_state(st["instance"], "TERMINATED", timeout=900)

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
