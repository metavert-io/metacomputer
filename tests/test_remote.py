#!/usr/bin/env python3
"""A computer elsewhere: an Ubuntu 24.04 VM reached over SSH (docs/remote.md). No VM, no network, no provider.

    python3 tests/test_remote.py

- The transport (lib/cloud/transport.py): where SSH goes, the host key pinned under mcomp-NAME, refusals for
  settings that aren't clear, and the hook a provider's tunnel registers (run through lib/proxywrap.py, which ends a
  tunnel that ignores SIGHUP when ssh goes).
- What the guest is given (lib/cloud/guest.py): each key's job in the key lists, and cloud-init user-data with the
  host key made on the Mac, no default account and bootstrap.sh; the guest firewall's rules (guest-firewall render):
  SSH alone in; out, never metadata, link-local or private ranges, the system services' exceptions by account and port,
  EGRESS=off.
- The OCI driver against a stand-in API: sizes, the availability domains tried once each when one is full, then a
  refusal; wake, sleep, delete, status, list; credentials only from the environment, named but never shown.
- lib/remote.py's target and probe, and the CLI in a copy of the checkout (tests/test_multi_vm.py's Box: its own HOME
  and fake runner): vm create --on server|oci and its refusals, ssh_base's options for each role, list and --on,
  what's refused for a computer elsewhere (reset, net allow, view --tailnet, PLACE outside config/vms/, SSH_AUTH=cert),
  provider credentials kept from everything but lib/remote.py, and vm delete of your own server.
"""
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(LAB, "lib"))
sys.path.insert(0, TESTS)
import cloud  # noqa: E402
from cloud import guest, oci, transport  # noqa: E402
from cloud.adapter import AdapterError, NotSupported  # noqa: E402

FAILS = []
PY = os.environ.get("MCOMP_TEST_PY") or shutil.which("python3") or sys.executable
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq5" + "A" * 39
FIREWALL = os.path.join(LAB, "provisioning", "remote", "rootfs", "opt", "metacomputer", "bin", "guest-firewall")


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def raises(fn, *a, kind=Exception, **k):
    try:
        fn(*a, **k)
    except kind as e:
        return str(e) or type(e).__name__
    except Exception as e:      # noqa: BLE001
        return f"WRONG {type(e).__name__}: {e}"
    return None


def transports():
    print("the transport (lib/cloud/transport.py)")
    s = {"VM_NAME": "box", "SSH_HOST": "203.0.113.5", "SSH_HOST_PORT": "2222"}
    ep = transport.endpoint(s)
    check("ssh: straight to SSH_HOST:SSH_HOST_PORT, no proxy, pinned as mcomp-NAME",
          ep == {"host": "203.0.113.5", "port": 2222, "alias": "mcomp-box", "proxy": None, "transport": "ssh"}, ep)
    check("...a provider's record of the address wins over the settings (an address that moved)",
          transport.endpoint(s, {"host": "198.51.100.7"})["host"] == "198.51.100.7")
    check("...port 22 when none is set", transport.endpoint({"VM_NAME": "box", "SSH_HOST": "vm.example.com"})["port"] == 22)
    t = transport.ssh_target(ep, "cua")
    check("ssh_target: HostKeyAlias, no address check, ProxyCommand=none, the port, user@host last",
          t == ["-o", "HostKeyAlias=mcomp-box", "-o", "CheckHostIP=no", "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
                "-p", "2222", "cua@203.0.113.5"], t)
    for bad in ("", "-oProxyCommand=sh", "a@b.example", "a b", "127.0.0.1", "169.254.169.254", "::1", "fe80::1",
                "0.0.0.0", "224.0.0.1", "1.2.3", "x" * 300, "203.0.113.5:22"):
        why = raises(transport.endpoint, {"VM_NAME": "box", "SSH_HOST": bad}, kind=transport.TransportError)
        check(f"SSH_HOST {bad[:20]!r} refused", why is not None and not why.startswith("WRONG"), why)
    for port in ("0", "70000", "x", "-1"):
        check(f"SSH_HOST_PORT {port!r} refused", raises(transport.endpoint, dict(s, SSH_HOST_PORT=port),
                                                         kind=transport.TransportError) is not None)
    why = raises(transport.endpoint, dict(s, TRANSPORT="carrier-pigeon"), kind=transport.TransportError)
    check("an unknown TRANSPORT is refused, naming the ones there are", why and "ssh" in why, why)
    check("a bad computer name is refused before anything", raises(transport.endpoint, dict(s, VM_NAME="../x"), kind=ValueError))
    check("a bad account name is refused", raises(transport.ssh_target, ep, "root;id", kind=transport.TransportError))
    check("known_hosts line under the alias", transport.known_hosts_line(ep, KEY + " comment") == "mcomp-box " + KEY)
    check("...and a line that isn't a key refused", raises(transport.known_hosts_line, ep, "nope", kind=transport.TransportError))
    # The hook: a provider's tunnel as the ProxyCommand, through the wrapper.
    saved = dict(transport.OPENERS)
    try:
        transport.register("tunnel-test", lambda st, state: {"host": "anything", "port": 22,
                                                             "proxy": ["tunnel", "-s", st["VM_NAME"], "proxy -W", ":22"]})
        ep2 = transport.endpoint(dict(s, TRANSPORT="tunnel-test"))
        t2 = transport.ssh_target(ep2, "cua", wrapper=("/py", "/lib/proxywrap.py"))
        pc = [x for x in t2 if x.startswith("ProxyCommand=")][0]
        check("register(): a provider's tunnel becomes ssh's ProxyCommand, through proxywrap, every word quoted",
              pc == "ProxyCommand=/py /lib/proxywrap.py -- tunnel -s box 'proxy -W' :22" and t2[-1] == "cua@anything", t2)
        check("...the host key is still pinned under the alias", "HostKeyAlias=mcomp-box" in t2)
        check("...a name registered twice is refused", raises(transport.register, "tunnel-test", lambda *_: {}, kind=ValueError))
        check("...and a name that isn't one", raises(transport.register, "Bad Name", lambda *_: {}, kind=ValueError))
        transport.register("tunnel-empty", lambda st, state: {"host": "x", "port": 22, "proxy": []})
        check("...a tunnel with no program is refused", raises(transport.endpoint, dict(s, TRANSPORT="tunnel-empty"),
                                                               kind=transport.TransportError))
    finally:
        transport.OPENERS.clear()
        transport.OPENERS.update(saved)


def proxywrap():
    print("\nthe tunnel wrapper (lib/proxywrap.py)")
    td = tempfile.mkdtemp(prefix="mcpw-")
    try:
        mark = os.path.join(td, "ended")
        # A tunnel that ignores SIGHUP (as `sprite proxy -W` does) and notes when SIGTERM ends it.
        prog = os.path.join(td, "stubborn.py")
        with open(prog, "w") as f:
            f.write("import signal, sys, time\nsignal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                    f"def term(*a):\n    open({mark!r}, 'w').write('TERM')\n    sys.exit(0)\n"
                    "signal.signal(signal.SIGTERM, term)\nwhile True:\n    time.sleep(0.1)\n")
        w = subprocess.Popen([PY, os.path.join(LAB, "lib", "proxywrap.py"), "--", PY, prog],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        time.sleep(0.8)
        w.send_signal(signal.SIGHUP)            # what ssh sends its ProxyCommand when it exits
        rc = w.wait(timeout=10)
        time.sleep(0.3)
        check("SIGHUP to the wrapper: the tunnel that ignores SIGHUP is ended with SIGTERM", os.path.exists(mark)
              and open(mark).read() == "TERM", rc)
        # Its parent gone (ssh killed outright): the wrapper ends the tunnel too.
        os.remove(mark)
        outer = subprocess.Popen([PY, "-c", "import subprocess, sys, time\n"
                                  f"subprocess.Popen([{PY!r}, {os.path.join(LAB, 'lib', 'proxywrap.py')!r}, '--', {PY!r}, {prog!r}])\n"
                                  "time.sleep(0.8)\n"], stdout=subprocess.DEVNULL)
        outer.wait(timeout=10)
        for _ in range(60):
            if os.path.exists(mark):
                break
            time.sleep(0.1)
        check("its parent (ssh) gone: the tunnel is ended", os.path.exists(mark))
        r = subprocess.run([PY, os.path.join(LAB, "lib", "proxywrap.py"), "--", PY, "-c", "import sys; sys.exit(7)"],
                           capture_output=True, timeout=10)
        check("the tunnel's own exit status is the wrapper's", r.returncode == 7, r.returncode)
        r = subprocess.run([PY, os.path.join(LAB, "lib", "proxywrap.py"), PY], capture_output=True, timeout=10)
        check("without -- PROGRAM: usage, exit 2", r.returncode == 2, r.returncode)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def guest_side():
    print("\nwhat the guest is given (lib/cloud/guest.py)")
    keys = {k: KEY.replace("AAAAI", "AAAAI" + k[0].upper()) for k in ("ops", "mcp", "view", "admin")}
    g, a = guest.key_lists(keys)
    lines = g.splitlines()
    check("the desktop user's keys: ops (a PTY), mcp (driver-mcp forced), view (one forward to 127.0.0.1:5900)",
          lines[0].startswith("restrict,pty ") and lines[1].startswith('restrict,command="/opt/metacomputer/bin/driver-mcp" ')
          and lines[2].startswith('restrict,port-forwarding,permitopen="127.0.0.1:5900",command="/usr/bin/sleep 20" ')
          and len(lines) == 3, lines)
    check("...every one restricted, and no key forwards anything but the view's", all(ln.startswith("restrict,") for ln in lines)
          and sum("port-forwarding" in ln for ln in lines) == 1)
    check("cuaadmin: one key, a PTY, nothing else", a == "restrict,pty " + keys["admin"] + "\n", a)
    check("a key that isn't one is refused", raises(guest.key_lists, dict(keys, view="ssh-ed25519 not a key\nevil"),
                                                     kind=ValueError))
    priv = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
    boot = open(os.path.join(LAB, "provisioning", "remote", "bootstrap.sh")).read()
    ud = guest.user_data("box", priv, KEY + " host", keys, boot, timezone="Europe/Paris")
    check("user-data: #cloud-config, passwords off, no root, no default account (users: [])",
          ud.startswith("#cloud-config\n") and "\nssh_pwauth: false\n" in ud and "\ndisable_root: true\n" in ud
          and "\nusers: []\n" in ud, ud[:200])
    check("...the host key made on the Mac, the only one (no others generated)", "  ed25519_private: |\n    -----BEGIN" in ud
          and f'ed25519_public: "{KEY}"' in ud and "ssh_deletekeys: true" in ud and "ssh_genkeytypes: []" in ud)
    files = {}
    cur = None
    for ln in ud.splitlines():
        if ln.startswith("  - path: "):
            cur = ln.split(": ", 1)[1]
        elif ln.startswith("    content: ") and cur:
            files[os.path.basename(cur)] = base64.b64decode(ln.split(": ", 1)[1]).decode()
    check("...bootstrap.sh and the two key lists, exactly", files == {"keys-guest": g, "keys-admin": a, "bootstrap.sh": boot},
          sorted(files))
    check("...run once, then removed", "[bash, /run/metacomputer-bootstrap/bootstrap.sh, /run/metacomputer-bootstrap]" in ud
          and "[rm, -rf, /run/metacomputer-bootstrap]" in ud)
    check("...small enough for any provider", len(base64.b64encode(ud.encode())) < guest.MAX_USER_DATA)
    check("a host name that isn't one is refused", raises(guest.user_data, "box", priv, KEY, keys, boot, hostname="a;b",
                                                          kind=ValueError))
    check("a host key that isn't an ed25519 pair is refused", raises(guest.user_data, "box", "nope", KEY, keys, boot,
                                                                     kind=ValueError))
    check("user-data too big for a provider is refused", raises(guest.user_data, "box", priv, KEY, keys, "x" * 40000,
                                                                kind=ValueError))
    r = subprocess.run(["/bin/bash", "-n", os.path.join(LAB, "provisioning", "remote", "bootstrap.sh")], capture_output=True)
    check("bootstrap.sh parses", r.returncode == 0, r.stderr)
    for f in ("guest-firewall", "idle-stop"):
        r = subprocess.run(["/bin/sh", "-n", os.path.join(os.path.dirname(FIREWALL), f)], capture_output=True)
        check(f"{f} parses", r.returncode == 0, r.stderr)


def firewall():
    print("\nthe guest firewall (guest-firewall render)")
    uids = "resolve=991 network=998 timesync=995 apt=42"

    def render(egress, u=uids):
        return subprocess.run(["/bin/sh", FIREWALL, "render", "--egress", egress, "--uids", u], capture_output=True, text=True)
    r = render("internet")
    out = r.stdout
    inp = out.split("chain input")[1].split("chain output")[0]
    outp = out.split("chain output")[1]
    check("it renders", r.returncode == 0 and "table inet metacomputer" in out, r.stderr)
    check("inbound: policy drop; loopback, replies, ICMP, DHCP replies and TCP 22, nothing else",
          "policy drop;" in inp and "tcp dport 22 accept" in inp
          and [ln.split()[0:3] for ln in inp.splitlines() if "dport" in ln] ==
          [["udp", "dport", "68"], ["udp", "dport", "546"], ["tcp", "dport", "22"]], inp)
    for net in ("169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8"):
        check(f"outbound: {net} is in the refused set", net in out.split("set private4")[1].split("}")[0])
    for net in ("fc00::/7", "fe80::/10", "ff00::/8"):
        check(f"outbound: {net} (IPv6; fc00::/7 holds OCI's metadata fd00:c1::a9fe:a9fe) is refused",
              net in out.split("set private6")[1].split("}")[0])
    check("...refused for every account (no uid on the reject), after only the system exceptions",
          "ip daddr @private4 counter reject" in outp and outp.index("@private4") > outp.index("th dport 123"))
    check("...the exceptions: the resolver (53), the DHCP client (67/547) and the clock (123), each by its account",
          "meta skuid 991 meta l4proto { tcp, udp } th dport 53 accept" in outp
          and "meta skuid 998 meta l4proto udp th dport { 67, 547 } accept" in outp
          and "meta skuid 995 meta l4proto udp th dport 123 accept" in outp)
    check("EGRESS=internet: the rest goes out (no final reject)", "EGRESS=off" not in outp and "policy accept;" in outp)
    off = render("off").stdout
    check("EGRESS=off: everything else refused, package downloads (_apt, 80/443) excepted",
          'counter reject comment "EGRESS=off"' in off and "meta skuid 42 meta l4proto tcp th dport { 80, 443 } accept" in off)
    miss = render("internet", "resolve= network=998 timesync=x apt=42").stdout
    check("an account the system hasn't: no exception for it (never a rule for a uid that isn't a number)",
          "th dport 53" not in miss and "th dport 123" not in miss and "skuid 998" in miss, miss[-400:])
    check("an egress setting that isn't one is refused", render("sometimes").returncode == 2)


class FakeOCI:
    """The SDK facade's calls, in memory: instances, a network, availability domains that may be full."""

    def __init__(self, full=()):
        self.full = set(full)
        self.inst = {}
        self.net = None
        self.launches = []
        self.actions = []
        self.n = 0
        self.compartment = "compartment-x"

    def availability_domains(self, shape=None):
        return ["AD-1", "AD-2", "AD-3"] if shape != "VM.Standard.E2.1.Micro" else ["AD-1"]

    def image(self, shape):
        return {"id": "img", "name": "Canonical-Ubuntu-24.04-" + ("aarch64" if "A1" in shape else "x86")}

    def make_network(self):
        self.net = self.net or {"vcn": "v", "subnet": "s", "igw": "g", "security_list": "sl", "route_table": "rt"}
        return self.net

    def network(self):
        return self.net

    def security_rules(self):
        return [{"protocol": "6", "source": "0.0.0.0/0", "ports": (22, 22), "icmp": None},
                {"protocol": "1", "source": "0.0.0.0/0", "ports": None, "icmp": (3, 4)}] if self.net else None

    def launch(self, name, ad, shape, ocpus, mem, image_id, subnet_id, user_data):
        self.launches.append((name, ad, shape, ocpus, mem, subnet_id, user_data))
        if ad in self.full:
            raise oci.Capacity(f"out of {shape} capacity in {ad}")
        self.n += 1
        iid = f"i{self.n}"
        self.inst[iid] = {"id": iid, "state": "RUNNING", "shape": shape, "name": name, "ad": ad}
        return iid

    def instances(self, tag_value=None):
        return [dict(i, created="t") for i in self.inst.values()
                if i["state"] != "TERMINATED" and (tag_value is None or i["name"] == tag_value)]

    def instance(self, iid):
        i = self.inst.get(iid)
        return None if i is None or i["state"] == "TERMINATED" else dict(i)

    def public_ip(self, iid):
        return "198.51.100.9"

    def action(self, iid, what):
        self.actions.append(what)
        self.inst[iid]["state"] = {"START": "RUNNING", "SOFTSTOP": "STOPPED", "STOP": "STOPPED"}[what]

    def wait_state(self, iid, want, timeout=600):
        return want

    def terminate(self, iid):
        self.inst[iid]["state"] = "TERMINATED"

    def delete_network(self):
        had, self.net = self.net is not None, None
        return had


def oci_driver():
    print("\nthe OCI driver (lib/cloud/oci.py), on a stand-in API")
    api = FakeOCI(full={"AD-1"})
    a = oci.OCI(api=api, env={})
    made = a.create("box", size="arm-2x12", user_data="#cloud-config\n")
    check("create: the network first, then the instance in the first AD that has room (AD-1 full -> AD-2)",
          api.net is not None and made["ad"] == "AD-2" and [l[1] for l in api.launches] == ["AD-1", "AD-2"], api.launches)
    check("...arm-2x12 is VM.Standard.A1.Flex with 2 OCPU and 12 GB (the whole Always Free Arm allowance)",
          api.launches[-1][2:5] == ("VM.Standard.A1.Flex", 2, 12) and made["arch"] == "arm64", api.launches[-1])
    check("...in metacomputer's subnet, with the user-data it was given", api.launches[-1][5] == "s"
          and api.launches[-1][6] == "#cloud-config\n")
    check("...a second create for the same name is refused", raises(a.create, "box", user_data="x", kind=AdapterError))
    full = FakeOCI(full={"AD-1", "AD-2", "AD-3"})
    why = raises(oci.OCI(api=full, env={}).create, "box", size="arm-1x6", user_data="x", kind=oci.Capacity)
    check("every AD full: each tried once, then refused saying so (no loop)", why and "tried once" in why
          and [l[1] for l in full.launches] == ["AD-1", "AD-2", "AD-3"], (why, full.launches))
    micro = FakeOCI()
    oci.OCI(api=micro, env={}).create("tiny", size="x86-micro", user_data="x")
    check("x86-micro: VM.Standard.E2.1.Micro, only where it's offered", micro.launches[-1][1:3] == ("AD-1", "VM.Standard.E2.1.Micro"))
    for bad, kw in (("size", {"size": "huge"}), ("region", {"region": "elsewhere"}), ("PERSIST", {"persist": "none"}),
                    ("user-data", {})):
        why = raises(a.create, "other", **({"user_data": "x"} | kw) if bad != "user-data" else {}, kind=AdapterError)
        check(f"create refuses {bad} it can't do, saying why", why is not None and not why.startswith("WRONG"), why)
    s = oci.OCI(api=api, env={}, states={"box": {"instance": made["instance"], "size": "arm-2x12"}})
    st = s.status("box")
    check("status: awake, with its address", st["state"] == "awake" and st["host"] == "198.51.100.9", st)
    s.sleep("box")
    check("sleep: a soft stop (the guest shuts down), then stopped", api.actions[-1] == "SOFTSTOP"
          and s.status("box")["state"] == "stopped")
    s.wake("box")
    check("wake: started, awake again", api.actions[-1] == "START" and s.status("box")["state"] == "awake")
    # A guest that powered itself off (its idle stop): OCI still says RUNNING, and bills it, until the API stops it.
    api.actions.clear()
    s.wake("box", guest_up=False)
    check("wake of a guest that powered itself off (OCI still RUNNING): a stop through the API, then a start",
          api.actions == ["STOP", "START"] and s.status("box")["state"] == "awake", api.actions)
    api.actions.clear()
    s.wake("box", guest_up=True)
    check("...one that answers: nothing to do", api.actions == [], api.actions)
    s.sleep("box", guest_up=False)
    check("sleep when the guest is already off: a plain stop (a soft one would wait for a shutdown that happened)",
          api.actions == ["STOP"] and s.status("box")["state"] == "stopped", api.actions)
    s.wake("box")
    check("list: metacomputer's instances (tagged)", [i["name"] for i in s.list()] == ["box"])
    s.delete("box")
    check("delete: terminated (its boot volume with it), status absent", s.status("box")["state"] == "absent")
    check("a computer this Mac has no instance for: refused, saying where to look",
          "cloud status oci" in (raises(oci.OCI(api=api, env={}).wake, "ghost", kind=AdapterError) or ""))
    checks = oci.OCI(api=FakeOCI(), env={})
    checks.api.make_network()
    d = {c["check"]: c for c in checks.doctor()}
    check("doctor: the network lets in SSH only (and ICMP 3/4)", d["network lets in SSH only"]["ok"], d)
    secret = "-----BEGIN PRIVATE KEY-----\nSECRETVALUE\n-----END PRIVATE KEY-----"
    why = raises(oci.credentials, {"OCI_USER": "u", "OCI_API_KEY_PEM": secret}, kind=AdapterError)
    check("credentials: only from the environment; missing ones named, no value shown",
          why and "OCI_TENANCY" in why and "OCI_COMPARTMENT" in why and "SECRETVALUE" not in why and "OCI_USER," not in why, why)
    cfg, comp = oci.credentials({k: f"v-{k}" for k in oci.ENV_KEYS})
    check("...all there: the SDK's config, with the key as content (never a path)", cfg["key_content"] == "v-OCI_API_KEY_PEM"
          and comp == "v-OCI_COMPARTMENT" and "key_file" not in cfg)
    no = oci.OCI(env={})
    check("no credentials: a provider call refuses (AdapterError), naming the variables",
          "OCI_USER" in (raises(no.list, kind=AdapterError) or ""))
    for size in oci.SIZES:
        r = a.rates(size)
        check(f"rates {size}: numbers, and whether it's in the Always Free allowance said",
              r["awake_per_hour"] >= r["asleep_per_hour"] >= 0 and ("Always Free" in r["note"]), r)
    check("x86 flex sizes cost money (said as not free)", "not in the Always Free" in a.rates("x86-2x8")["note"])
    check("the manifest: stop-start, a disk that keeps everything, no snapshots, its own firewall",
          oci.MANIFEST["sleep_wake"]["kind"] == "stop-start" and cloud.capabilities.choose_persist(oci.MANIFEST) == "disk"
          and oci.MANIFEST["snapshots"] is None and oci.MANIFEST["egress"]["model"] == "guest-firewall")
    u = cloud.adapter("ubuntu-vm", computers={"box": {"VM_NAME": "box", "PROVIDER": "ubuntu-vm", "SSH_HOST": "203.0.113.5"}})
    check("your own server: SSH straight to its address", u.ssh_endpoint("box")["host"] == "203.0.113.5"
          and u.ssh_proxy_command("box") is None)
    check("...never powered on or off by metacomputer (NotSupported)", isinstance(_err(u.sleep, "box"), NotSupported)
          and isinstance(_err(u.wake, "box"), NotSupported))
    check("...only the live view is forwarded", isinstance(_err(u.port_forward, "box", 22), NotSupported))
    check("...its egress setting is read from its settings", u.egress("box")["setting"] == "internet")


def _err(fn, *a):
    try:
        fn(*a)
    except Exception as e:      # noqa: BLE001
        return e
    return None


def remote_helpers():
    print("\nlib/remote.py target and probe")
    rp = os.path.join(LAB, "lib", "remote.py")
    r = subprocess.run([PY, rp, "target", "--vm", "box", "--user", "cua", "--host", "203.0.113.5", "--port", "22"],
                       capture_output=True, timeout=30)
    parts = r.stdout.split(b"\0")
    check("target: NUL-separated, ending in user@host, pinned as mcomp-box", r.returncode == 0 and parts[-1] == b""
          and parts[-2] == b"cua@203.0.113.5" and b"HostKeyAlias=mcomp-box" in parts, r)
    td = tempfile.mkdtemp(prefix="mcrp-")
    try:
        with open(os.path.join(td, "remote.json"), "w") as f:
            json.dump({"host": "198.51.100.4"}, f)
        r = subprocess.run([PY, rp, "target", "--vm", "box", "--user", "cua", "--run-dir", td], capture_output=True, timeout=30)
        check("...the provider's record gives the address", r.stdout.split(b"\0")[-2] == b"cua@198.51.100.4", r)
        r = subprocess.run([PY, rp, "target", "--vm", "box", "--user", "cua", "--host", "-oProxyCommand=touch x"],
                           capture_output=True, text=True, timeout=30)
        check("...an address that could be an option: refused, nothing printed", r.returncode == 1 and r.stdout == ""
              and "isn't an IP address" in r.stderr, r)
        with open(os.path.join(td, "remote.json"), "w") as f:
            f.write("not json")
        r = subprocess.run([PY, rp, "target", "--vm", "box", "--user", "cua", "--run-dir", td], capture_output=True, text=True)
        check("...a record that isn't ours: refused", r.returncode == 1 and "isn't the record" in r.stderr, r)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        c.sendall(b"SSH-2.0-test\r\n")
        time.sleep(0.5)
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    # probe refuses loopback as a computer's address (transport.check_host), so it's checked on a documentation
    # address that nothing answers on: unreachable within its timeout.
    r = subprocess.run([PY, rp, "probe", "--vm", "box", "--host", "192.0.2.1", "--timeout", "1"], capture_output=True,
                       text=True, timeout=30)
    check("probe: an address nothing answers on is unreachable, within the timeout", r.stdout.strip() == "unreachable", r)
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import remote as remote_mod
    orig = remote_mod.transport.check_host
    remote_mod.transport.check_host = lambda h: h          # only here: let the probe reach the local stand-in
    try:
        class A:
            vm, run_dir, transport, host, timeout = "box", "", "ssh", "127.0.0.1", 2.0
        A.port = str(port)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            remote_mod.cmd_probe(A)
        check("probe: something that answers with an SSH banner is reachable", buf.getvalue().strip() == "reachable",
              buf.getvalue())
    finally:
        remote_mod.transport.check_host = orig
        srv.close()


def cli(td):
    print("\nthe CLI, in a copy of the checkout (no VM, no provider)")
    import test_multi_vm as tmv
    b = tmv.Box(td, "remote")
    for d in ("provisioning",):
        shutil.copytree(os.path.join(LAB, d), os.path.join(b.co, d))
    try:
        r = b.run("vm", "create", "own1", "--on", "server", "--host", "192.0.2.10", "--user", "owner", "--port", "2222",
                  "--no-build", "--yes")
        f = os.path.join(b.co, "config", "vms", "own1.env")
        text = open(f).read() if os.path.exists(f) else ""
        check("vm create --on server: its settings (place, provider, address, the account that sets it up, no idle stop)",
              r.returncode == 0 and all(x in text.splitlines() for x in (
                  "PLACE=server", "PROVIDER=ubuntu-vm", "TRANSPORT=ssh", "SSH_HOST=192.0.2.10", "SSH_HOST_PORT=2222",
                  "SETUP_USER=owner", "IDLE_STOP=off", "EGRESS=internet")), (r.stderr, text))
        check("...0600, and nothing of the runner's touched", oct(os.stat(f).st_mode & 0o777) == "0o600" and not b.deletes())
        r = b.run("vm", "create", "arm1", "--on", "oci", "--size", "arm-1x6", "--egress", "off", "--no-build", "--yes")
        text = open(os.path.join(b.co, "config", "vms", "arm1.env")).read()
        check("vm create --on oci: place, provider, size, egress and the idle stop (15 min)",
              r.returncode == 0 and all(x in text.splitlines() for x in ("PLACE=oci", "PROVIDER=oci", "SIZE=arm-1x6",
                                                                         "EGRESS=off", "IDLE_STOP=15")), (r.stderr, text))
        for args, want in ((["--on", "server", "--user", "owner"], "--host ADDRESS"),
                           (["--on", "server", "--host", "-oProxyCommand=x", "--user", "o"], "isn't an IP address"),
                           (["--on", "server", "--host", "192.0.2.3", "--user", "o", "--size", "arm-1x6"], "--size"),
                           (["--on", "oci", "--host", "192.0.2.3"], "your own server"),
                           (["--on", "oci", "--size", "huge"], "no size"),
                           (["--on", "mars"], "--on mars"),
                           (["--on", "oci", "--egress", "maybe"], "--egress"),
                           (["--host", "192.0.2.3"], "elsewhere")):
            r = b.run("vm", "create", "bad1", *args, "--no-build", "--yes")
            check(f"vm create {' '.join(args)[:40]}: refused ({want})", r.returncode != 0 and want in r.stderr
                  and not os.path.exists(os.path.join(b.co, "config", "vms", "bad1.env")), r.stderr[-300:])
        r = b.run("vm", "create", "oci", "--on", "oci", "--no-build", "--yes")
        check("a computer can't be called oci (a place's name)", r.returncode != 0 and "place's name" in r.stderr, r.stderr)
        # ssh_base for each role: the transport's target after the same strict options as the Mac's VMs.
        script = ('MCOMP_VM_ARG="$1"; . "$2/scripts/lib.sh"; ssh_base "$3"; printf "%s\\n" "${SSH_ARGS[@]}"')
        args = {}
        for role in ("ops", "mcp", "admin", "view"):
            r = subprocess.run(["/bin/bash", "-c", script, "_", "own1", b.co, role], env=b.env(), capture_output=True,
                               text=True, timeout=60)
            args[role] = r.stdout.splitlines()
        o = args["ops"]
        check("ssh_base ops: no config file, batch, only this computer's ops key, the pinned host key, nothing forwarded",
              o[:2] == ["-F", "/dev/null"] and "BatchMode=yes" in o and f"IdentityFile={b.home}/.ssh/metacomputer-own1/ops" in o
              and f"UserKnownHostsFile={b.home}/.ssh/metacomputer-own1/known_hosts" in o and "StrictHostKeyChecking=yes" in o
              and "ClearAllForwardings=yes" in o and "ForwardAgent=no" in o and "IdentityAgent=none" in o, o)
        check("...to the transport's endpoint: HostKeyAlias=mcomp-own1, no proxy, port 2222, cua@192.0.2.10",
              o[-1] == "cua@192.0.2.10" and "HostKeyAlias=mcomp-own1" in o and "ProxyCommand=none" in o
              and o[o.index("-p") + 1] == "2222" and "127.0.0.1" not in " ".join(o), o)
        check("ssh_base admin: cuaadmin with the admin key", args["admin"][-1] == "cuaadmin@192.0.2.10"
              and f"IdentityFile={b.home}/.ssh/metacomputer-own1/admin" in args["admin"])
        check("ssh_base mcp: the mcp key (driver-mcp is forced in the guest)", any(x.endswith("metacomputer-own1/mcp") for x in args["mcp"]))
        check("ssh_base view: the view key, and the one role whose forward isn't cleared",
              any(x.endswith("/view") for x in args["view"]) and "ClearAllForwardings=yes" not in args["view"]
              and "ExitOnForwardFailure=yes" in args["view"])
        r = subprocess.run(["/bin/bash", "-c", 'MCOMP_VM_ARG=""; . "$1/scripts/lib.sh"; ssh_base view', "_", b.co],
                           env=b.env(), capture_output=True, text=True, timeout=60)
        check("the view role is refused for a VM on this Mac", r.returncode != 0 and "view role" in r.stderr, r.stderr)
        # Provider credentials: kept from what the CLI starts, handed to lib/remote.py only.
        script = ('MCOMP_VM_ARG=arm1; . "$1/scripts/lib.sh"; env | grep -c "^OCI_" || true; '
                  '( mc_export_provider_env; env | grep "^OCI_USER=" | cut -d= -f1 )')
        r = subprocess.run(["/bin/bash", "-c", script, "_", b.co], env=b.env(OCI_USER="u1", OCI_API_KEY_PEM="pem"),
                           capture_output=True, text=True, timeout=60)
        check("OCI_* in the environment: not exported to what metacomputer starts, but handed to lib/remote.py",
              r.stdout.split() == ["0", "OCI_USER"], (r.stdout, r.stderr))
        # The listing and --on.
        r = b.run("list", "--json")
        try:
            vms = {v["name"]: v for v in json.loads(r.stdout)["vms"]}
        except (ValueError, KeyError):
            vms = {}
        check("list: each computer's place and where (your own server, Oracle Cloud)",
              vms.get("own1", {}).get("place") == "server" and vms.get("own1", {}).get("where") == "your own server"
              and vms.get("arm1", {}).get("place") == "oci" and vms.get("arm1", {}).get("where") == "Oracle Cloud", r.stdout[:600])
        check("...not set up yet (no pinned host key), and no runner VM asked about", vms.get("own1", {}).get("state") == "not set up")
        r = b.run("list", "--on", "oci", "--json")
        check("list --on oci: only that computer", [v["name"] for v in json.loads(r.stdout or "{}").get("vms", [])] == ["arm1"], r.stdout)
        r = b.run("--on", "oci", "where")
        check("--on oci where: it means arm1, which these settings aren't: say which", r.returncode != 0
              and "--vm arm1" in r.stderr, r.stderr)
        r = b.run("--vm", "own1", "--on", "oci", "where")
        check("--vm own1 --on oci: they disagree", r.returncode != 0 and "disagree" in r.stderr, r.stderr)
        r = b.run("--vm", "own1", "--on", "cloud", "where")
        check("--vm own1 --on cloud: agree", r.returncode == 0, r.stderr)
        # What a computer elsewhere doesn't have, said up front.
        for args, want in ((["reset", "--yes"], "needs snapshots, which your own server doesn't have"),
                           (["checkpoint", "x"], "saving a checkpoint needs snapshots"),
                           (["net", "allow", "example.com"], "no allowlist"),
                           (["volume", "list"], "volumes"),
                           (["view", "--tailnet"], "SSH tunnel")):
            r = b.run("--vm", "own1", *args)
            check(f"{' '.join(args)} on your own server: refused, saying why", r.returncode != 0 and want in r.stderr, r.stderr[-300:])
        r = b.run("--vm", "own1", "net", "status")
        check("net status: its own firewall, EGRESS said", "EGRESS=internet" in r.stdout, r.stdout + r.stderr)
        r = b.run("--vm", "own1", "stop")
        check("stop on your own server: never powered off; the hub here closed", r.returncode == 0
              and "doesn't power your own server off" in r.stdout, r.stdout + r.stderr)
        r = b.run("--vm", "arm1", "stop", "--override")
        check("stop on Oracle Cloud without its credentials: refused, saying what it needs (and that it stops itself)",
              r.returncode != 0 and "credentials" in r.stderr and "minutes unused" in r.stderr, r.stderr[-300:])
        # Settings that can't be a computer elsewhere.
        b.settings("config/local.env", "PLACE=oci\nPROVIDER=oci\n")
        r = b.run("status")
        check("PLACE in config/local.env (the main VM): refused", r.returncode != 0 and "only a computer of its own" in r.stderr,
              r.stderr)
        os.remove(os.path.join(b.co, "config", "local.env"))
        with open(os.path.join(b.co, "config", "vms", "own1.env"), "a") as fh:
            fh.write("SSH_AUTH=cert\n")
        r = b.run("--vm", "own1", "where")
        check("SSH_AUTH=cert for a computer elsewhere: refused (its certificates name the gateway's address)",
              r.returncode != 0 and "SSH_AUTH=cert" in r.stderr, r.stderr)
        text = open(os.path.join(b.co, "config", "vms", "own1.env")).read().replace("SSH_AUTH=cert\n", "")
        with open(os.path.join(b.co, "config", "vms", "own1.env"), "w") as fh:
            fh.write(text)
        b.settings("config/vms/odd.env", "VM_NAME=odd\nSSH_PORT=2390\nVNC_PORT=6090\nPLACE=oci\n")
        r = b.run("--vm", "odd", "where")
        check("PLACE without its PROVIDER: refused", r.returncode != 0 and "PROVIDER" in r.stderr, r.stderr)
        os.remove(os.path.join(b.co, "config", "vms", "odd.env"))
        # vm delete of your own server: the settings go, the server is left alone (and said so).
        r = b.run("vm", "delete", "own1", "--confirm", "own1")
        check("vm delete own1: its settings removed, nothing asked of a provider or the runner, the server left as it is",
              r.returncode == 0 and not os.path.exists(os.path.join(b.co, "config", "vms", "own1.env"))
              and "Your server still has metacomputer's accounts" in r.stdout and not b.deletes(), r.stdout + r.stderr)
        r = b.run("vm", "delete", "arm1", "--confirm", "arm1")
        check("vm delete of an Oracle Cloud computer without its credentials: refused, nothing deleted",
              r.returncode != 0 and "credentials" in r.stderr and os.path.exists(os.path.join(b.co, "config", "vms", "arm1.env")),
              r.stderr[-300:])
    finally:
        b.close()


def main():
    transports()
    proxywrap()
    guest_side()
    firewall()
    oci_driver()
    remote_helpers()
    td = tempfile.mkdtemp(prefix="mcremote-")
    try:
        cli(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
