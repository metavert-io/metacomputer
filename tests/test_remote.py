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
import gzip
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
        # Review item 12: a tunnel that leaves a child behind (one that ignores SIGTERM) when it exits: the wrapper
        # ends the whole group, whichever way it ends.
        pidf = os.path.join(td, "grandchild.pid")
        tunnel = os.path.join(td, "leaves-child.py")
        with open(tunnel, "w") as f:
            f.write("import os, signal, subprocess, sys, time\n"
                    f"p = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'])\n"
                    f"open({pidf!r}, 'w').write(str(p.pid))\ntime.sleep(float(sys.argv[1]))\n")

        def gone(pid):
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
        for how, delay in (("the tunnel exits by itself", "0.5"), ("SIGHUP to the wrapper", "60")):
            if os.path.exists(pidf):
                os.remove(pidf)
            w = subprocess.Popen([PY, os.path.join(LAB, "lib", "proxywrap.py"), "--", PY, tunnel, delay],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            for _ in range(50):
                if os.path.exists(pidf) and open(pidf).read():
                    break
                time.sleep(0.1)
            gc = int(open(pidf).read())
            if delay == "60":
                time.sleep(0.5)
                w.send_signal(signal.SIGHUP)
            w.wait(timeout=20)
            for _ in range(40):
                if gone(gc):
                    break
                time.sleep(0.1)
            check(f"{how}: the child it left behind (ignoring SIGTERM) is ended too", gone(gc), gc)
            if not gone(gc):
                os.kill(gc, signal.SIGKILL)
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
    fwsrc = open(FIREWALL).read()
    ud = guest.user_data("box", priv, KEY + " host", keys, boot, fwsrc, timezone="Europe/Paris")
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
            files[os.path.basename(cur)] = gzip.decompress(base64.b64decode(ln.split(": ", 1)[1])).decode()
    check("...bootstrap.sh, the two key lists, the guest firewall and its egress setting, exactly",
          files == {"keys-guest": g, "keys-admin": a, "bootstrap.sh": boot, "guest-firewall": fwsrc,
                    "egress": "internet\n"}, sorted(files))
    check("...run once (the firewall loaded first, SSH on 22), then removed",
          '[bash, /run/metacomputer-bootstrap/bootstrap.sh, /run/metacomputer-bootstrap, "", "22"]' in ud
          and "[rm, -rf, /run/metacomputer-bootstrap]" in ud)
    off = guest.user_data("box", priv, KEY, keys, boot, fwsrc, egress="off")
    check("...EGRESS=off is what the guest firewall starts with", "egress" in off and raises(
        guest.user_data, "box", priv, KEY, keys, boot, fwsrc, egress="sometimes", kind=ValueError))
    check("...a firewall that isn't the script is refused", raises(guest.user_data, "box", priv, KEY, keys, boot, "",
                                                                   kind=ValueError))
    check("...small enough for any provider", len(base64.b64encode(ud.encode())) < guest.MAX_USER_DATA)
    check("a host name that isn't one is refused", raises(guest.user_data, "box", priv, KEY, keys, boot, fwsrc,
                                                          hostname="a;b", kind=ValueError))
    check("a host key that isn't an ed25519 pair is refused", raises(guest.user_data, "box", "nope", KEY, keys, boot,
                                                                     fwsrc, kind=ValueError))
    big = bytes(os.urandom(30000)).hex()
    check("user-data too big for a provider is refused", raises(guest.user_data, "box", priv, KEY, keys, big, fwsrc,
                                                                kind=ValueError))
    r = subprocess.run(["/bin/bash", "-n", os.path.join(LAB, "provisioning", "remote", "bootstrap.sh")], capture_output=True)
    check("bootstrap.sh parses", r.returncode == 0, r.stderr)
    for f in ("guest-firewall", "idle-stop"):
        r = subprocess.run(["/bin/sh", "-n", os.path.join(os.path.dirname(FIREWALL), f)], capture_output=True)
        check(f"{f} parses", r.returncode == 0, r.stderr)


def firewall():
    print("\nthe guest firewall (guest-firewall render)")
    uids = "resolve=991 network=998 timesync=995"

    def render(egress, u=uids, ports="22"):
        return subprocess.run(["/bin/sh", FIREWALL, "render", "--egress", egress, "--uids", u, "--ports", ports],
                              capture_output=True, text=True)
    r = render("internet")
    out = r.stdout
    inp = out.split("chain input")[1].split("chain output")[0]
    outp = out.split("chain output")[1]
    check("it renders", r.returncode == 0 and "table inet metacomputer" in out, r.stderr)
    check("inbound: policy drop; loopback, replies, ICMP, DHCP replies and TCP 22, nothing else",
          "policy drop;" in inp and "tcp dport { 22 } accept" in inp
          and [ln.split()[0:4] for ln in inp.splitlines() if "dport" in ln] ==
          [["udp", "dport", "68", "accept"], ["udp", "dport", "546", "accept"], ["tcp", "dport", "{", "22"]], inp)
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
    check("EGRESS=off: everything else refused, package downloads too (as the provider's rules say)",
          'counter reject comment "EGRESS=off"' in off and "80, 443" not in off)
    # Review item 11: a rule tightened now cuts what was already open; only replies to inbound connections pass.
    check("outbound: replies to connections made TO it (SSH) pass as such, before any refusal",
          outp.index("ct direction reply accept") < outp.index("@private4"), outp[:400])
    check("...and no blanket 'established' accept in the outbound chain: an open connection meets today's rules",
          "ct state established" not in outp and "ct state established" not in off.split("chain output")[1])
    # Review item 4: every port sshd listens on stays open (your own server on 2222).
    two = render("internet", ports="2222 22 2222 x 70000").stdout
    check("SSH in on every port sshd listens on (2222 and 22; duplicates and non-ports dropped)",
          "tcp dport { 2222, 22 } accept" in two, two.split("chain input")[1][:600])
    check("...port 22 when none is known", "tcp dport { 22 } accept" in render("internet", ports="").stdout)
    miss = render("internet", "resolve= network=998 timesync=x").stdout
    check("an account the system hasn't: no exception for it (never a rule for a uid that isn't a number)",
          "th dport 53" not in miss and "th dport 123" not in miss and "skuid 998" in miss, miss[-400:])
    check("an egress setting that isn't one is refused", render("sometimes").returncode == 2)
    em = subprocess.run(["/bin/sh", FIREWALL, "render-emergency", "--ports", "2222", "--uids", uids], capture_output=True,
                        text=True).stdout
    emo = em.split("chain output")[1]
    check("the SSH-only fallback: SSH in on its ports; out, policy drop with only replies, lo, ND and DHCP",
          "tcp dport { 2222 } accept" in em and "policy drop;" in emo and "ct direction reply accept" in emo
          and "th dport 53" not in emo and "th dport 123" not in emo and "skuid 998" in emo, em)
    # The guest firewall's private set is the provider rules' (ubuntu_vm.PRIVATE_V4): one list, two layers.
    from cloud import ubuntu_vm
    fw = open(FIREWALL).read()
    p4 = fw.split('private4="', 1)[1].split('"', 1)[0]
    check("the guest firewall's private ranges are ubuntu_vm.PRIVATE_V4 (the provider rules' list)",
          [x.strip() for x in p4.split(",")] == list(ubuntu_vm.PRIVATE_V4), p4)
    fail_closed()


def fail_closed():
    """Review item 6: guest-firewall apply, with a stand-in nft: the full rules, or the SSH-only fallback when they
    don't load, or exit 1 (and the network, which requires it, stays down)."""
    td = tempfile.mkdtemp(prefix="mcfw-")
    try:
        fake = os.path.join(td, "bin")
        os.makedirs(fake)
        with open(os.path.join(fake, "nft"), "w") as f:
            f.write('#!/bin/sh\n# a stand-in for nft: -c/-f FILE; refuses what has a bad set element (FAIL=element, as '
                    'an impossible address does) or everything (FAIL=all)\n'
                    'file=""; for a in "$@"; do file="$a"; done\n'
                    'case "$FAIL" in all) exit 1 ;; element) grep -q "10.0.0.0/33" "$file" && exit 1 ;; esac\n'
                    '[ "$1" = -c ] || cp "$file" "$LOADED"\nexit 0\n')
        os.chmod(os.path.join(fake, "nft"), 0o755)
        etc = os.path.join(td, "etc")
        os.makedirs(etc)
        open(os.path.join(etc, "egress"), "w").write("internet\n")
        open(os.path.join(etc, "ssh-ports"), "w").write("2222\n")
        src = open(FIREWALL).read()
        src = src.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={fake}:/usr/sbin:/usr/bin:/sbin:/bin")
        src = src.replace("/etc/metacomputer/", etc + "/").replace("/run/metacomputer-firewall.", td + "/fw.")
        src = src.replace("/run/metacomputer/firewall-fallback", td + "/fallback").replace("/run/metacomputer", td + "/run")
        broken = src.replace("10.0.0.0/8", "10.0.0.0/33", 1)       # the live test's break: one impossible address
        script = os.path.join(td, "guest-firewall")
        open(script, "w").write(src)
        loaded = os.path.join(td, "loaded")

        def apply(fail):
            if os.path.exists(loaded):
                os.remove(loaded)
            open(script, "w").write(broken if fail == "element" else src)
            return subprocess.run(["/bin/sh", script, "apply"], capture_output=True, text=True,
                                  env={"FAIL": fail, "LOADED": loaded, "PATH": "/usr/bin:/bin"})
        r = apply("")
        check("apply: the full rules loaded (SSH in on the saved port), no fallback mark", r.returncode == 0
              and "tcp dport { 2222 }" in open(loaded).read() and not os.path.exists(td + "/fallback"), r.stderr)
        r = apply("element")
        check("rules that don't load (an impossible address in the private set, as broken live): the SSH-only "
              "fallback, which has no sets, is loaded instead, marked, and said",
              r.returncode == 0 and "SSH-only fallback" in open(loaded).read() and os.path.exists(td + "/fallback")
              and "SSH-only fallback loaded" in r.stderr, r.stderr)
        r = apply("all")
        check("not even the fallback loads: exit 1 (the network, which requires it, doesn't come up)",
              r.returncode == 1 and not os.path.exists(loaded) and "network stays down" in r.stderr, r.stderr)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    def readf(*parts):
        f = os.path.join(LAB, *parts)
        return open(f).read() if os.path.exists(f) else ""

    def in_order(src, *keys):
        """Each key in src, each after the one before (False when one is missing)."""
        at = [src.find(k) for k in keys]
        return -1 not in at and at == sorted(at)
    unit = readf("provisioning", "remote", "rootfs", "etc", "systemd", "system", "metacomputer-firewall.service")
    dropin = readf("provisioning", "remote", "rootfs", "etc", "systemd", "system", "systemd-networkd.service.d",
                   "10-metacomputer-firewall.conf")
    check("the network requires the firewall (systemd-networkd: Requires= and After= it), which runs before it",
          "Requires=metacomputer-firewall.service" in dropin and "After=metacomputer-firewall.service" in dropin
          and "Before=network-pre.target systemd-networkd.service" in unit)
    boot = readf("provisioning", "remote", "bootstrap.sh")
    check("bootstrap.sh loads the firewall before it makes any account (review item 6)",
          in_order(boot, "/opt/metacomputer/bin/guest-firewall apply", "useradd"))
    prov = readf("provisioning", "guest", "provision.sh")
    check("provision.sh: SSH settings and the firewall go in before the guest tree's sshd settings can take effect "
          "and before anything is upgraded (review item 5)",
          in_order(prov, "if [ \"$TARGET\" = remote ]; then remote_ssh_and_firewall; fi", 'step "apt: update + full upgrade"')
          and in_order(prov, "11-metacomputer-owner.conf", 'step "apt: update + full upgrade"'))
    fn = prov.split("remote_ssh_and_firewall() {", 1)[1].split("\n}\n", 1)[0] if "remote_ssh_and_firewall() {" in prov else ""
    check("...the server's own SSH settings saved first, and put back when the new ones would lock the owner out",
          in_order(fn, "/var/lib/metacomputer/rollback/sshd_config.d", 'cp -r --preserve=mode,timestamps "$PAYLOAD/remote/rootfs/." /',
                   "sshd -t", "remote-uninstall rollback", "systemctl reload ssh"))
    un = readf("provisioning", "remote", "rootfs", "opt", "metacomputer", "bin", "remote-uninstall")
    check("remote-uninstall (review item 13): the service disabled, the network's dependency removed, only its own "
          "table deleted, the server's SSH settings put back, sshd checked before reload",
          "systemctl disable --now metacomputer-firewall.service" in un and "10-metacomputer-firewall.conf" in un
          and "nft delete table inet metacomputer" in un and "flush ruleset" not in un
          and in_order(un, "sshd -t", "systemctl reload ssh") and 'cp -pR "$BACKUP/sshd_config.d/."' in un)
    r = subprocess.run(["/bin/sh", "-n", os.path.join(LAB, "provisioning", "remote", "rootfs", "opt", "metacomputer", "bin",
                                                       "remote-uninstall")], capture_output=True)
    check("remote-uninstall parses", bool(un) and r.returncode == 0, r.stderr)


class FakeOCI:
    """The SDK facade's calls, in memory: instances, a network, availability domains that may be full."""

    def __init__(self, full=(), console="", fail_record=False):
        self.network_calls = []
        self.locked = lambda: None
        self.full = set(full)
        self.console = console
        self.nsgs = {}
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
        self.network_calls.append(self.locked())
        self.net = self.net or {"vcn": "v", "subnet": "s", "igw": "g", "security_list": "sl", "route_table": "rt"}
        return self.net

    def nsg_names(self):
        return list(self.nsgs)

    def network(self):
        return self.net

    def security_rules(self):
        return [{"protocol": "6", "source": "0.0.0.0/0", "ports": (22, 22), "icmp": None},
                {"protocol": "1", "source": "0.0.0.0/0", "ports": None, "icmp": (3, 4)}] if self.net else None

    def launch(self, name, ad, shape, ocpus, mem, image_id, subnet_id, user_data, nsg_id=None, op=None):
        self.launches.append((name, ad, shape, ocpus, mem, subnet_id, user_data, nsg_id, op))
        if ad in self.full:
            raise oci.Capacity(f"out of {shape} capacity in {ad}")
        self.n += 1
        iid = f"i{self.n}"
        self.inst[iid] = {"id": iid, "state": "RUNNING", "shape": shape, "name": name, "ad": ad, "op": op}
        return iid

    def instances(self, tag_value=None, op=None):
        return [dict(i, created="t") for i in self.inst.values()
                if i["state"] != "TERMINATED" and (tag_value is None or i["name"] == tag_value)
                and (op is None or i.get("op") == op)]

    def make_nsg(self, name, egress):
        self.make_network()
        self.nsgs.setdefault(name, {"id": "nsg-" + name, "egress": None})
        self.nsg_egress("nsg-" + name, egress)
        return "nsg-" + name

    def nsg(self, name):
        return self.nsgs[name]["id"] if name in self.nsgs else None

    def nsg_egress(self, gid, egress):
        for g in self.nsgs.values():
            if g["id"] == gid:
                g["egress"] = egress

    def nsg_rules(self, gid):
        from cloud import ubuntu_vm
        g = next(g for g in self.nsgs.values() if g["id"] == gid)
        return [{"direction": "EGRESS", "protocol": "all", "destination": c, "source": None}
                for c in (ubuntu_vm.public_v4() if g["egress"] == "internet" else ())]

    def delete_nsg(self, name):
        return self.nsgs.pop(name, None) is not None

    def console_tail(self, iid, lines=40):
        return [ln for ln in self.console.splitlines() if ln.strip()][-lines:]

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
    check("...on its own security group, made first, letting out public addresses (review item 1)",
          api.launches[-1][7] == "nsg-box" and api.nsgs["box"]["egress"] == "internet" and made["nsg"] == "nsg-box")
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
    s = oci.OCI(api=api, env={}, computers={"box": {"VM_NAME": "box", "PROVIDER": "oci", "EGRESS": "internet"}},
                states={"box": {"instance": made["instance"], "size": "arm-2x12"}})
    s.apply_egress("box", "off")
    check("EGRESS=off outside the computer: its security group lets nothing out", api.nsgs["box"]["egress"] == "off"
          and api.nsg_rules("nsg-box") == [] and s.egress("box")["outside"] == "off")
    s.apply_egress("box", "internet")
    check("...and internet: every public block, nothing private", s.egress("box")["outside"] == "internet"
          and len(api.nsg_rules("nsg-box")) == len(__import__("cloud.ubuntu_vm").ubuntu_vm.public_v4()))
    check("an allowlist of names is refused (NotSupported)", isinstance(_err(s.set_egress, "box", ["x.com"]), NotSupported))
    lazy = oci.OCI(env={}, computers={"box": {"VM_NAME": "box", "PROVIDER": "oci", "EGRESS": "internet"}},
                   states={"box": {"instance": made["instance"], "size": "arm-2x12"}})
    real_sdk, oci.SDK = oci.SDK, lambda env: api
    try:
        seen = lazy.egress("box").get("outside")
    finally:
        oci.SDK = real_sdk
    check("...read back through Oracle's API even when nothing asked it anything before (found live: 'unknown')",
          seen == "internet", seen)
    check("...and without the credentials: not known, not an error", oci.OCI(env={}, computers={"box": {
        "VM_NAME": "box", "PROVIDER": "oci", "EGRESS": "internet"}}, states={"box": {"instance": "i9"}}).egress("box")
        .get("outside") is None)
    pub = [{"direction": "EGRESS", "protocol": "all", "destination": c} for c in __import__("cloud.ubuntu_vm").ubuntu_vm.public_v4()]
    check("the group's rules read back: exactly the public blocks is internet; one missing, or one private more, is "
          "'other' (not what was set); none is off",
          oci.egress_of(pub) == "internet" and oci.egress_of(pub[1:]) == "other" and oci.egress_of([]) == "off"
          and oci.egress_of(pub + [{"direction": "EGRESS", "protocol": "all", "destination": "10.0.0.0/8"}]) == "other")
    st = s.status("box")
    check("status: awake, with its address", st["state"] == "awake" and st["host"] == "198.51.100.9", st)
    s.sleep("box")
    check("sleep: a soft stop (the guest shuts down), then stopped", api.actions[-1] == "SOFTSTOP"
          and s.status("box")["state"] == "stopped")
    s.wake("box")
    check("wake: started, awake again", api.actions[-1] == "START" and s.status("box")["state"] == "awake")
    # Review item 8: a RUNNING instance whose SSH doesn't answer is stopped by start only when its console proves the
    # guest powered itself off (its idle stop: OCI still says RUNNING, and bills, until the API stops it).
    api.actions.clear()
    api.console = "Starting OpenBSD Secure Shell server...\n[  OK  ] Reached target multi-user.target"
    why = raises(s.wake, "box", guest_up=False, kind=AdapterError)
    check("start of a running instance that doesn't answer, with no power-off on its console: refused, nothing stopped",
          why and "Nothing was stopped" in why and "stop --force" in why and api.actions == [], (why, api.actions))
    api.console = "[  OK  ] Reached target poweroff.target - System Power Off.\n[ 4109.696080] reboot: Power down\n"
    s.wake("box", guest_up=False)
    check("...with 'reboot: Power down' last on its console: stopped through the API, then started",
          api.actions == ["STOP", "START"] and s.status("box")["state"] == "awake", api.actions)
    api.actions.clear()
    s.wake("box", guest_up=True)
    check("...one that answers: nothing to do", api.actions == [], api.actions)
    api.console = "[  OK  ] Started session-c2.scope"
    s.sleep("box", guest_up=False)
    check("stop when the guest doesn't answer but its console shows no power-off: a soft stop (never a hard one "
          "unasked)", api.actions == ["SOFTSTOP"], api.actions)
    s.wake("box")
    api.actions.clear()
    s.sleep("box", guest_up=True, force=True)
    check("...stop --force: a plain stop at once", api.actions == ["STOP"], api.actions)
    s.wake("box")
    api.console = "[ 12.3] reboot: Power down"
    api.actions.clear()
    s.sleep("box", guest_up=False)
    check("...a guest that powered itself off (its console says so): a plain stop (a soft one would wait 15 minutes)",
          api.actions == ["STOP"] and s.status("box")["state"] == "stopped", api.actions)
    s.wake("box")
    check("list: metacomputer's instances (tagged)", [i["name"] for i in s.list()] == ["box"])
    s.delete("box")
    check("delete: terminated (its boot volume with it), its security group gone, status absent",
          s.status("box")["state"] == "absent" and "box" not in api.nsgs)
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
    check("...outside the guest it refuses private ranges and can turn egress off, but not the metadata service "
          "(OCI applies no security rule to 169.254.0.0/16): never claimed",
          oci.MANIFEST["outside_egress"] == {"enforced": True, "private_ranges": True, "metadata": False, "off": True})
    check("...its billing truth: stopped through the API, only storage; the guest powering off saves nothing",
          oci.MANIFEST["billing"]["stopped"] == "storage" and oci.MANIFEST["billing"]["guest_poweroff"] == "nothing")
    from cloud import ubuntu_vm
    check("your own server: nothing is enforced outside it (only its own firewall, which root there can undo)",
          ubuntu_vm.MANIFEST["outside_egress"] == {"enforced": False, "private_ranges": False, "metadata": False,
                                                   "off": False})
    bad = dict(ubuntu_vm.MANIFEST, outside_egress={"enforced": False, "private_ranges": True, "metadata": False,
                                                  "off": False})
    check("a manifest claiming outside rules while enforcing nothing outside is refused",
          any("nothing is enforced outside" in p for p in cloud.capabilities.check_manifest(bad)))
    sdk_rules()
    u = cloud.adapter("ubuntu-vm", computers={"box": {"VM_NAME": "box", "PROVIDER": "ubuntu-vm", "SSH_HOST": "203.0.113.5"}})
    check("your own server: SSH straight to its address", u.ssh_endpoint("box")["host"] == "203.0.113.5"
          and u.ssh_proxy_command("box") is None)
    check("...never powered on or off by metacomputer (NotSupported)", isinstance(_err(u.sleep, "box"), NotSupported)
          and isinstance(_err(u.wake, "box"), NotSupported))
    check("...only the live view is forwarded", isinstance(_err(u.port_forward, "box", 22), NotSupported))
    check("...its egress setting is read from its settings", u.egress("box")["setting"] == "internet")


def network_race():
    """Two creates at once made two networks (found live): the network and a computer's group are made under a lock
    one process on this Mac holds at a time; a delete never removes the network while another computer is being made
    in it; the oldest network is the one used, and deleting deletes them all."""
    print("\none network, whatever runs at once")
    import remote as remote_mod
    td = tempfile.mkdtemp(prefix="mclock-")
    try:
        log = os.path.join(td, "spans")
        prog = ("import os, sys, time; sys.path.insert(0, %r); import remote\n"
                "with remote.mac_lock('oci-network'):\n"
                "    t0 = time.time(); time.sleep(0.6); open(%r, 'a').write(f'{t0} {time.time()}\\n')\n"
                % (os.path.join(LAB, "lib"), log))
        env = dict(os.environ, MCOMP_LOCK_DIR=os.path.join(td, "locks"))
        procs = [subprocess.Popen([PY, "-c", prog], env=env) for _ in range(3)]
        for pr in procs:
            pr.wait(timeout=30)
        spans = sorted(tuple(map(float, ln.split())) for ln in open(log).read().splitlines())
        check("remote.py's network lock: three processes at once hold it one after another, never together",
              len(spans) == 3 and all(spans[i][1] <= spans[i + 1][0] for i in range(2)), spans)
        check("...its folder is this account's own, 0700", oct(os.stat(os.path.join(td, "locks")).st_mode & 0o777) == "0o700")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    held = []

    class Lock:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *a):
            held.pop()
    api = FakeOCI()
    api.locked = lambda: bool(held)
    a = oci.OCI(api=api, env={}, network_lock=Lock)
    a.create("box", size="arm-1x6", user_data="#cloud-config\n")
    check("create makes the network (and the computer's group, after it) only while holding the network lock",
          api.network_calls and all(api.network_calls) and not held, api.network_calls)
    check("remote.py gives the OCI adapter that lock (its name: oci-network)",
          "network_lock" in open(os.path.join(LAB, "lib", "remote.py")).read().split("def adapter_for", 1)[1][:900]
          and 'mac_lock("oci-network")' in open(os.path.join(LAB, "lib", "remote.py")).read())
    # A delete with --network-too while another computer's group exists (it's being made): the network stays.
    rd = tempfile.mkdtemp(prefix="mcnet-")
    try:
        api = FakeOCI()
        remote_mod.API_FACTORY = lambda: api
        api.make_network()
        api.make_nsg("other", "internet")
        settings = {"VM_NAME": "box", "PLACE": "oci", "PROVIDER": "oci", "SIZE": "arm-1x6", "EGRESS": "internet",
                    "RUN_DIR": rd, "SSH_DIR": rd}
        os.environ["MCOMP_LOCK_DIR"] = os.path.join(rd, "locks")
        remote_mod.cmd_delete(settings, type("A", (), {"network_too": True})())
        check("vm delete --network-too while another computer is being made in the network (its group is there, no "
              "instance yet): the network stays", api.net is not None)
        api.delete_nsg("other")
        remote_mod.cmd_delete(settings, type("A", (), {"network_too": True})())
        check("...and goes once no computer has a group or an instance there", api.net is None)
    finally:
        os.environ.pop("MCOMP_LOCK_DIR", None)
        remote_mod.API_FACTORY = None
        shutil.rmtree(rd, ignore_errors=True)
    # The SDK facade: of two networks, the oldest is the one used.
    import types
    sdk = object.__new__(oci.SDK)
    sdk.compartment = "c"
    vcns = [types.SimpleNamespace(id="v-new", time_created="2026-11-22 20:00", freeform_tags={oci.TAG: "network"},
                                  lifecycle_state="AVAILABLE", default_security_list_id="sl2", default_route_table_id="rt2"),
            types.SimpleNamespace(id="v-old", time_created="2026-11-22 19:00", freeform_tags={oci.TAG: "network"},
                                  lifecycle_state="AVAILABLE", default_security_list_id="sl1", default_route_table_id="rt1"),
            types.SimpleNamespace(id="v-theirs", time_created="2026-01-01", freeform_tags={},
                                  lifecycle_state="AVAILABLE", default_security_list_id="x", default_route_table_id="x")]

    def _all(fn, *a, **k):
        return vcns if fn == "list_vcns" else []
    sdk._all = _all
    sdk.net = types.SimpleNamespace(list_vcns="list_vcns", list_subnets="list_subnets",
                                    list_internet_gateways="list_internet_gateways")
    check("two networks tagged metacomputer's: the oldest is used (and nothing untagged ever is)",
          sdk.network()["vcn"] == "v-old" and [v.id for v in sdk.networks()] == ["v-old", "v-new"])


def sdk_rules():
    """The SDK facade's own rule-making against a recording stand-in for Oracle's client (no network): the subnet's
    security list lets nothing out, and a computer's group lets out exactly the public blocks (or nothing)."""
    from cloud import ubuntu_vm
    import types

    class Models:
        def __getattr__(self, name):
            return lambda **kw: dict(kw, _model=name)
    calls = []

    class Net:
        def update_security_list(self, sl, d):
            calls.append(("sl", d))

        def update_route_table(self, rt, d):
            calls.append(("rt", d))

        def list_network_security_group_security_rules(self, gid):
            raise AssertionError("listed through _all")

        def remove_network_security_group_security_rules(self, gid, d):
            calls.append(("remove", d["security_rule_ids"]))

        def add_network_security_group_security_rules(self, gid, d):
            calls.append(("add", d["security_rules"]))
    sdk = object.__new__(oci.SDK)
    sdk.oci = types.SimpleNamespace(core=types.SimpleNamespace(models=Models()), wait_until=lambda *a, **k: None)
    sdk.net = Net()
    sdk.compartment = "c"
    sdk.network = lambda: {"vcn": "v", "subnet": "s", "igw": "g", "security_list": "sl", "route_table": "rt"}
    sdk._all = lambda fn, *a, **k: [types.SimpleNamespace(id=f"r{i}") for i in range(30)]
    sdk.make_network()
    sl = next(d for k, d in calls if k == "sl")
    check("the subnet's security list: SSH and ICMP 3/4 in, nothing out (each computer's group says what goes out)",
          sl["egress_security_rules"] == [] and len(sl["ingress_security_rules"]) == 2)
    calls.clear()
    sdk.nsg_egress("g1", "internet")
    added = [r for k, d in calls if k == "add" for r in d]
    check("a computer's group, internet: its old rules removed, then exactly the public blocks, in batches of 25",
          [k for k, _ in calls].count("remove") == 2 and sorted(r["destination"] for r in added)
          == sorted(ubuntu_vm.public_v4()) and all(len(d) <= 25 for k, d in calls if k in ("add", "remove"))
          and all(r["direction"] == "EGRESS" and not r["is_stateless"] for r in added), len(added))
    calls.clear()
    sdk.nsg_egress("g1", "off")
    check("...off: its rules removed, none added", not [d for k, d in calls if k == "add"])
    launched = []

    class Compute:
        def launch_instance(self, details, retry_strategy=None):
            launched.append((details, retry_strategy))
            return types.SimpleNamespace(data=types.SimpleNamespace(id="i1"))
    sdk.compute = Compute()
    sdk.oci.retry = types.SimpleNamespace(NoneRetryStrategy=lambda: "no-retry")
    sdk.oci.exceptions = types.SimpleNamespace(ServiceError=type("ServiceError", (Exception,), {}))
    sdk.launch("box", "AD-1", "VM.Standard.A1.Flex", 1, 6, "img", "sub", "#cloud-config\n", nsg_id="g1", op="op-1")
    d, rs = launched[-1]
    check("a launch is tried exactly once (no SDK retry: 'Out of host capacity' is a 500 it would retry), on the "
          "computer's group, tagged with its operation, IMDS v2 only",
          rs == "no-retry" and d["create_vnic_details"]["nsg_ids"] == ["g1"]
          and d["freeform_tags"] == {oci.TAG: "box", oci.OP_TAG: "op-1"}
          and d["instance_options"]["are_legacy_imds_endpoints_disabled"] is True, (rs, d.get("freeform_tags")))


REVIEW_GUARD = None   # tests/ harnesses may run each part on its own (one part's crash doesn't hide the rest)


def review(td):
    """Review items 2, 3 (identity), 7, 9, 10, 13 and 14 against the CLI in a copy of the checkout, and remote.py."""
    import test_multi_vm as tmv
    print("\nthe review's fixes, in a copy of the checkout")
    b = tmv.Box(td, "review")
    shutil.copytree(os.path.join(LAB, "provisioning"), os.path.join(b.co, "provisioning"))
    c = {"b": b, "td": td, "tmv": tmv, "real": f"{os.path.dirname(tmv.PY)}:/usr/bin:/bin:/usr/sbin:/sbin"}
    try:
        b.run("vm", "create", "own1", "--on", "server", "--host", "192.0.2.10", "--user", "owner", "--no-build", "--yes")
        b.run("vm", "create", "arm1", "--on", "oci", "--size", "arm-1x6", "--no-build", "--yes")
        for part in (review_credentials, review_identity, review_default, review_egress, review_delete_recheck,
                     review_remove_from_server, review_build_cleanup, review_create_journal):
            if REVIEW_GUARD:
                REVIEW_GUARD(part.__name__, part, c)
            else:
                part(c)
    finally:
        b.close()


def review_credentials(c):
    """Review item 2: credentials reach lib/remote.py only, from the very first program metacomputer runs"""
    b, td, real, tmv = c["b"], c["td"], c["real"], c["tmv"]
    wrap = os.path.join(td, "wrap")
    os.makedirs(wrap)
    log = os.path.join(td, "seen.log")
    logger = os.path.join(td, "logger.sh")
    with open(logger, "w") as f:
        f.write('#!/bin/sh\nPATH="$REAL_PATH"; me="${0##*/}"\n'
                'if [ -n "${OCI_API_KEY_PEM:-}${OCI_USER:-}" ]; then echo "$me OCI $*" >> "$SEEN_LOG"; fi\n'
                'exec "$me" "$@"\n')
    os.chmod(logger, 0o755)
    for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        for n in os.listdir(d):
            if not os.path.exists(os.path.join(wrap, n)) and os.access(os.path.join(d, n), os.X_OK):
                os.symlink(logger, os.path.join(wrap, n))
    pyw = os.path.join(td, "py-wrapper")
    with open(pyw, "w") as f:
        f.write(f'#!/bin/sh\nif [ -n "${{OCI_API_KEY_PEM:-}}${{OCI_USER:-}}" ]; then echo "python OCI $1" >> "$SEEN_LOG"; fi\n'
                f'exec {tmv.PY} "$@"\n')
    os.chmod(pyw, 0o755)
    real = f"{os.path.dirname(tmv.PY)}:/usr/bin:/bin:/usr/sbin:/sbin"
    env = b.env(OCI_USER="ocid1.user.synthetic", OCI_API_KEY_PEM="SYNTHETIC-KEY-PEM", OCI_TENANCY="t",
                OCI_FINGERPRINT="f", OCI_REGION="r", OCI_COMPARTMENT="c", SEEN_LOG=log, REAL_PATH=real,
                PY=pyw, MCOMP_PY_CHECKED=pyw, PATH=f"{wrap}:{real}")
    for args in (["--vm", "arm1", "status"], ["vm", "create", "arm2", "--on", "oci", "--no-build", "--yes"],
                 ["--vm", "arm1", "stop", "--override"], ["--vm", "arm1", "net", "status"],
                 ["vm", "delete", "arm2", "--confirm", "arm2"], ["--vm", "arm1", "build"]):
        subprocess.run([b.mc, *args], env=env, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    seen = open(log).read().splitlines() if os.path.exists(log) else []
    others = [ln for ln in seen if not (ln.startswith("python OCI ") and ln.split()[2].endswith("/lib/remote.py"))]
    check("credentials in a command's environment reach lib/remote.py and no other program, from the first one "
          "metacomputer runs (status, vm create, stop, net status, vm delete, build)",
          others == [] and any(ln.startswith("python OCI ") for ln in seen), (others[:6], seen[:3]))


def review_identity(c):
    """Review item 3: the identity a vault binding must match goes with vm delete"""
    b, td, real, tmv = c["b"], c["td"], c["real"], c["tmv"]
    ident_dir = os.path.join(b.home, ".ssh", "metacomputer-own1")
    os.makedirs(ident_dir, exist_ok=True)
    open(os.path.join(ident_dir, "known_hosts"), "w").write("mcomp-own1 " + KEY + "\n")
    r = subprocess.run([tmv.PY, os.path.join(b.co, "lib", "remote.py"), "identity", "--vm", "own1", "--write"],
                       env=b.env(), capture_output=True, text=True, timeout=60)
    idf = os.path.join(ident_dir, "computer-id")
    first = open(idf).read().strip() if os.path.exists(idf) else ""
    check("remote.py identity --write: 64 hex digits beside its keys, from this setup and its host key",
          r.returncode == 0 and len(first) == 64 and oct(os.stat(idf).st_mode & 0o777) == "0o600", (r.stderr, first))
    open(os.path.join(ident_dir, "known_hosts"), "w").write("mcomp-own1 " + KEY.replace("AAAAI", "AAAAJ") + "\n")
    subprocess.run([tmv.PY, os.path.join(b.co, "lib", "remote.py"), "identity", "--vm", "own1", "--write"],
                   env=b.env(), capture_output=True, text=True, timeout=60)
    check("...another host key: another identity (the old binding then refuses)",
          os.path.exists(idf) and open(idf).read().strip() not in ("", first))


def review_default(c):
    """Review item 14: default says where the computer really is"""
    b, td, real, tmv = c["b"], c["td"], c["real"], c["tmv"]
    b.run("default", "arm1")
    r = b.run("default", "--json")
    d = json.loads(r.stdout or "{}")
    t = b.run("default")
    check("default: an Oracle Cloud computer is said to be there, not on this Mac (text and JSON)",
          d.get("place") == "oci" and d.get("where") == "Oracle Cloud" and "Oracle Cloud" in t.stdout
          and "this Mac" not in t.stdout.split(")")[0], (d, t.stdout))
    b.run("default", "metacomputer")


def review_egress(c):
    """Review item 10: net egress changes nothing unless it can be applied and read back"""
    b, td, real, tmv = c["b"], c["td"], c["real"], c["tmv"]
    r = b.run("--vm", "own1", "net", "egress", "off", "--override")
    text = open(os.path.join(b.co, "config", "vms", "own1.env")).read()
    check("net egress off on a computer that doesn't answer: refused, the setting left as it was",
          r.returncode != 0 and "doesn't answer" in r.stderr and "nothing was" in r.stderr
          and "EGRESS=internet" in text and "EGRESS=off" not in text, r.stderr[-300:])
    r = b.run("--vm", "own1", "net", "status")
    check("net status: the setting, what's enforced outside (nothing, for your own server) and what's in force "
          "inside, each said apart", "setting (in" in r.stdout and "outside the computer" in r.stdout
          and "its own firewall, in force" in r.stdout and "unknown" in r.stdout, r.stdout)


def review_delete_recheck(c):
    """Review item 9: vm delete refuses a computer a session is using, under its owners lock"""
    b, td, real, tmv = c["b"], c["td"], c["real"], c["tmv"]
    holder = subprocess.Popen(["sleep", "120"])
    try:
        od = os.path.join(b.co, ".run-vms", "own1", "owners")
        os.makedirs(od, exist_ok=True)
        json.dump({"bridge_pid": holder.pid, "claude_pid": holder.pid, "cwd": "/", "started": time.time(),
                   "last_call": time.time(), "last_tool": "click"}, open(os.path.join(od, f"{holder.pid}.json"), "w"))
        r = b.run("vm", "delete", "own1", "--confirm", "own1")
        check("vm delete of a computer a session is using: refused, nothing deleted",
              r.returncode != 0 and "session" in r.stderr and os.path.exists(os.path.join(b.co, "config", "vms", "own1.env")),
              r.stderr[-300:])
    finally:
        holder.kill()
        holder.wait()
    vc = open(os.path.join(b.co, "scripts", "vm-commands.sh")).read()
    fn = vc.split("vm_delete_remote() {", 1)[1].split("\n}\n", 1)[0]
    check("...and every check runs again under the owners lock, right before anything stops (review item 9)",
          fn.index('lock_fd_take 6') < fn.rindex("remote_delete_plan") < fn.index("hub_stop")
          and 'RDP_SIG" = "$first"' in fn)


def review_remove_from_server(c):
    """Review item 13: --remove-from-server takes the setup off through your own SSH, then checks your access"""
    b, td, real, tmv = c["b"], c["td"], c["real"], c["tmv"]
    idf = os.path.join(b.home, ".ssh", "metacomputer-own1", "computer-id")
    fakessh = os.path.join(td, "fakessh")
    os.makedirs(fakessh)
    sshlog = os.path.join(td, "ssh.log")
    with open(os.path.join(fakessh, "ssh"), "w") as f:
        f.write(f'#!/bin/sh\nfor a in "$@"; do last="$a"; done\necho "$last" >> {sshlog}\nexit 0\n')
    os.chmod(os.path.join(fakessh, "ssh"), 0o755)
    r = subprocess.run([b.mc, "vm", "delete", "own1", "--confirm", "own1", "--remove-from-server"],
                       env=b.env(PATH=f"{fakessh}:{real}"), capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL)
    sent = open(sshlog).read().splitlines() if os.path.exists(sshlog) else []
    check("vm delete --remove-from-server: remote-uninstall all through your own SSH with sudo, then a fresh "
          "connection of yours checked, then the settings and identity removed",
          r.returncode == 0 and sent[:2] == ["sudo -n /opt/metacomputer/bin/remote-uninstall all", "true"]
          and not os.path.exists(os.path.join(b.co, "config", "vms", "own1.env")) and not os.path.exists(idf),
          (sent, r.stdout[-300:], r.stderr[-300:]))


def review_build_cleanup(c):
    """Review item 7: a build that fails after making an instance deletes it (or stops it with --keep-on-failure)"""
    b, td, real, tmv = c["b"], c["td"], c["real"], c["tmv"]
    fakepy = os.path.join(td, "fake-oci-python")
    oplog = os.path.join(td, "ops.log")
    with open(fakepy, "w") as f:
        f.write("\n".join([
            "#!" + tmv.PY,
            "import json, os, sys",
            "cmd = sys.argv[2] if len(sys.argv) > 2 else ''",
            "open(%r, 'a').write(' '.join(sys.argv[2:]) + chr(10))" % oplog,
            "if cmd == 'plan':",
            "    print('kind=made', 'arch=arm64', 'label=Oracle Cloud', 'idle_stop=15', 'egress=internet', sep=chr(10))",
            "elif cmd == 'create':",
            "    rd = os.path.join(%r, '.run-vms', 'arm1')" % b.co,
            "    os.makedirs(rd, exist_ok=True)",
            "    json.dump({'instance': 'i-fake', 'host': '192.0.2.1', 'provider': 'oci'}, open(os.path.join(rd, 'remote.json'), 'w'))",
            ""]))
    os.chmod(fakepy, 0o755)
    for keep, want in (([], "delete --vm arm1 --network-too"), (["--keep-on-failure"], "sleep --vm arm1 --force")):
        if os.path.exists(oplog):
            os.remove(oplog)
        rec = os.path.join(b.co, ".run-vms", "arm1", "remote.json")
        if os.path.exists(rec):
            os.remove(rec)
        r = subprocess.run([b.mc, "--vm", "arm1", "build", *keep], capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL,
                           env=b.env(OCI_PYTHON=fakepy, OCI_USER="u", OCI_API_KEY_PEM="k", MCOMP_REMOTE_SSH_WAIT="1"))
        ops = open(oplog).read().splitlines() if os.path.exists(oplog) else []
        check(f"build {' '.join(keep) or '(default)'} that fails after making the instance: {want.split()[0]} it, "
              "and say what's left billing", r.returncode != 0 and "create --vm arm1" in ops and want in ops
              and ("nothing of this build is left billing" in r.stdout or "bills its disk" in r.stdout + r.stderr),
              (ops, r.stdout[-400:], r.stderr[-300:]))
    # Interrupted (found live: bash runs the EXIT trap with the last command's status, often 0): still cleaned up.
    if os.path.exists(oplog):
        os.remove(oplog)
    rec = os.path.join(b.co, ".run-vms", "arm1", "remote.json")
    if os.path.exists(rec):
        os.remove(rec)
    pr = subprocess.Popen([b.mc, "--vm", "arm1", "build"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          env=b.env(OCI_PYTHON=fakepy, OCI_USER="u", OCI_API_KEY_PEM="k", MCOMP_REMOTE_SSH_WAIT="600"))
    for _ in range(300):
        if os.path.exists(oplog) and "identity --vm arm1 --write" in open(oplog).read():
            break
        time.sleep(0.1)
    time.sleep(1)
    pr.send_signal(signal.SIGTERM)
    out, _ = pr.communicate(timeout=120)
    ops = open(oplog).read().splitlines() if os.path.exists(oplog) else []
    check("a build killed (SIGTERM) after making the instance: deleted, and said",
          pr.returncode != 0 and "delete --vm arm1 --network-too" in ops and "nothing of this build is left billing" in out,
          (pr.returncode, ops, out[-300:]))


def review_create_journal(c=None):
    """Review item 7: remote.py create journals the launch; an instance it can't record is deleted; a lost one is found."""
    import remote as remote_mod
    rd = tempfile.mkdtemp(prefix="mcrj-")
    try:
        api = FakeOCI()
        settings = {"VM_NAME": "box", "PLACE": "oci", "PROVIDER": "oci", "SIZE": "arm-1x6", "EGRESS": "internet",
                    "RUN_DIR": rd, "SSH_DIR": rd}
        remote_mod.API_FACTORY = lambda: api
        orig_ud, orig_ws = remote_mod.user_data, remote_mod.write_state
        remote_mod.user_data = lambda s: "#cloud-config\n"
        writes = []

        def failing_write(d, st):
            writes.append(dict(st))
            if st.get("instance"):
                raise OSError(28, "No space left on device")
            orig_ws(d, st)
        remote_mod.write_state = failing_write
        r = raises(remote_mod.cmd_create, settings, None, kind=remote_mod.Refused)
        check("an instance whose record can't be written is deleted again (nothing left billing without a record)",
              r and "deleted again" in r and all(i["state"] == "TERMINATED" for i in api.inst.values())
              and writes and writes[0].get("launching"), (r, api.inst))
        remote_mod.write_state = orig_ws
        lost = {"id": "i-lost", "state": "RUNNING", "shape": "x", "name": "box", "ad": "AD-1", "op": "op-1"}
        api.inst["i-lost"] = lost
        orig_ws(rd, {"provider": "oci", "launching": "op-1"})
        remote_mod.cmd_create(settings, None)
        check("an earlier launch whose record was lost (found by its operation tag) is deleted before the new one",
              api.inst["i-lost"]["state"] == "TERMINATED" and remote_mod.read_state(rd).get("instance") not in (None, "i-lost"))
    finally:
        remote_mod.API_FACTORY = None
        remote_mod.user_data, remote_mod.write_state = orig_ud, orig_ws
        shutil.rmtree(rd, ignore_errors=True)


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
              and "Your server still has metacomputer's setup" in r.stdout and "remote-uninstall all" in r.stdout
              and not b.deletes(), r.stdout + r.stderr)
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
    network_race()
    remote_helpers()
    td = tempfile.mkdtemp(prefix="mcremote-")
    try:
        cli(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    td = tempfile.mkdtemp(prefix="mcreview-")
    try:
        review(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
