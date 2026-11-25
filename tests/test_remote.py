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
- The Hetzner Cloud, DigitalOcean and Vultr drivers against stand-ins of their REST APIs (StandInREST): the firewall
  and first-boot key before the server, under the lock and journaled by id; the create call's shape, sent once; each
  firewall's rules read back (Vultr's: inbound only, nothing claimed outside); stop, start, delete by recorded ids
  while lists lag; servers metacomputer didn't mark left alone; Vultr's address and permission refusals; and
  lib/remote.py's journal, abort, delete and teardown with each.
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
from cloud import guest, transport  # noqa: E402
from cloud.adapter import AdapterError, NotSupported  # noqa: E402

oci = cloud.registry.module("oci")

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
        # Found live: the hub ends a connection by SIGKILLing its whole process group (ssh and the wrapper ssh started),
        # which used to end the wrapper before it could end the tunnel. Now the wrapper leaves that group first: the
        # group's SIGKILL reaches ssh (here, a stand-in parent), and the wrapper ends the tunnel when it sees ssh gone.
        os.remove(mark) if os.path.exists(mark) else None
        pw = os.path.join(LAB, "lib", "proxywrap.py")
        ssh_like = subprocess.Popen([PY, "-c", "import subprocess, sys, time\n"
                                     f"subprocess.Popen([{PY!r}, {pw!r}, '--', {PY!r}, {prog!r}])\n"
                                     "time.sleep(60)\n"], stdout=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.0)
        os.killpg(ssh_like.pid, signal.SIGKILL)       # the hub's _end_transport: SIGKILL to the connection's group
        ssh_like.wait(timeout=10)
        for _ in range(80):
            if os.path.exists(mark):
                break
            time.sleep(0.1)
        check("a SIGKILL to ssh's whole process group (as the hub ends a connection): the tunnel is still ended",
              os.path.exists(mark) and open(mark).read() == "TERM")
        # Found by review: a wrapper whose ssh had already gone when it started took launchd for its parent, and ran the
        # tunnel for good. Started orphaned (the shell that started it exits at once), it never leaves one running.
        started = os.path.join(td, "started.pid")
        starter = os.path.join(td, "starter.py")
        with open(starter, "w") as f:
            f.write(f"import os, time\nopen({started!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
        subprocess.run(["/bin/sh", "-c", f"{PY} {pw} -- {PY} {starter} </dev/null >/dev/null 2>&1 &"], timeout=10)
        time.sleep(3)
        alive = False
        if os.path.exists(started):
            pid = int(open(started).read() or 0)
            try:
                os.kill(pid, 0)
                alive = True
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        check("a wrapper started after its ssh had gone (orphaned from the start): no tunnel left running", not alive)
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
          and f'ed25519_public: "{KEY}"' in ud and "ssh_deletekeys: true" in ud and "ssh_genkeytypes: [ed25519]" in ud)
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
    check("...the exceptions: the resolver (53) and the clock (123), each by its account; DHCP by its ports",
          "meta skuid 991 meta l4proto { tcp, udp } th dport 53 accept" in outp
          and "meta skuid 995 meta l4proto udp th dport 123 accept" in outp
          and "udp sport 68 udp dport 67 ip daddr {" in outp and "udp sport 546 udp dport 547 ip6 daddr { ff02::1:2, fe80::/10 } accept" in outp)
    dhcp_stacks()
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
          "th dport 53" not in miss and "th dport 123" not in miss and "udp sport 68 udp dport 67" in miss, miss[-400:])
    check("an egress setting that isn't one is refused", render("sometimes").returncode == 2)
    st = subprocess.run(["/bin/sh", FIREWALL, "render", "--egress", "setup", "--ports", "22", "--uids",
                         uids + " apt=42", "--until", "1795000000"], capture_output=True, text=True).stdout
    sto = st.split("chain output")[1] if "chain output" in st else ""
    check("EGRESS=off while it's being set up: apt's downloads (_apt, TCP 80/443) after the private refusals, then "
          "everything else refused (found by review: a new computer with EGRESS=off couldn't install anything)",
          "meta skuid 42 meta time < 1795000000 meta l4proto tcp th dport { 80, 443 } accept" in sto
          and sto.index("@private4") < sto.index("skuid 42") < sto.index('"EGRESS=off"'), sto[-600:])
    nountil = subprocess.run(["/bin/sh", FIREWALL, "render", "--egress", "setup", "--uids", uids + " apt=42"],
                             capture_output=True, text=True)
    check("...the setup's downloads never without the time they end (refused without --until)",
          nountil.returncode == 2 and "--until" in nountil.stderr, nountil.stderr)
    check("...and once set up, off is off: no apt exception", "skuid 42" not in render("off", uids + " apt=42").stdout)
    em = subprocess.run(["/bin/sh", FIREWALL, "render-emergency", "--ports", "2222", "--uids", uids], capture_output=True,
                        text=True).stdout
    emo = em.split("chain output")[1]
    check("the SSH-only fallback: SSH in on its ports; out, policy drop with only replies, lo, ND and DHCP",
          "tcp dport { 2222 } accept" in em and "policy drop;" in emo and "ct direction reply accept" in emo
          and "th dport 53" not in emo and "th dport 123" not in emo and "udp sport 546 udp dport 547" in emo
          and "udp sport 68 udp dport 67" in emo, em)
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
        sdrun = os.path.join(td, "systemd-run.log")
        for n in ("systemd-run", "systemctl"):           # what apply asks of systemd, recorded
            with open(os.path.join(fake, n), "w") as f:
                f.write(f'#!/bin/sh\necho "{n} $*" >> {sdrun}\nexit 0\n')
            os.chmod(os.path.join(fake, n), 0o755)
        with open(os.path.join(fake, "id"), "w") as f:     # the guest's system accounts (this Mac has none of them)
            f.write('#!/bin/sh\ncase "$2" in systemd-resolve) echo 991 ;; systemd-network) echo 998 ;; _chrony) echo 995 ;;\n'
                    '  _apt) echo 42 ;; *) exit 1 ;; esac\n')
        os.chmod(os.path.join(fake, "id"), 0o755)
        etc = os.path.join(td, "etc")
        os.makedirs(etc)
        open(os.path.join(etc, "egress"), "w").write("internet\n")
        open(os.path.join(etc, "ssh-ports"), "w").write("2222\n")
        src = open(FIREWALL).read()
        src = src.replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={fake}:/usr/sbin:/usr/bin:/sbin:/bin")
        src = src.replace("/etc/metacomputer/", etc + "/").replace("/run/metacomputer-firewall.", td + "/fw.")
        src = src.replace("/run/metacomputer/firewall-fallback", td + "/fallback").replace("/run/metacomputer", td + "/run")
        src = src.replace("/proc/", td + "/proc/").replace("/opt/metacomputer/bin/guest-firewall end-setup", "end-setup")
        broken = src.replace("10.0.0.0/8", "10.0.0.0/33", 1)       # the live test's break: one impossible address
        script = os.path.join(td, "guest-firewall")
        open(script, "w").write(src)
        loaded = os.path.join(td, "loaded")

        def apply(fail):
            if os.path.exists(loaded):
                os.remove(loaded)
            open(script, "w").write(broken if fail == "element" else src)
            if os.path.exists(sdrun):
                os.remove(sdrun)
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
        # The setup's downloads (EGRESS=off): counted in this boot's uptime, never by the wall clock alone.
        open(os.path.join(etc, "egress"), "w").write("off\n")
        marker = os.path.join(etc, "setup-egress")
        proc = os.path.join(td, "proc")
        os.makedirs(os.path.join(proc, "sys", "kernel", "random"), exist_ok=True)
        open(os.path.join(proc, "sys", "kernel", "random", "boot_id"), "w").write("boot-one\n")

        def at_uptime(sec):
            open(os.path.join(proc, "uptime"), "w").write(f"{sec}.42 99.0\n")
        at_uptime(1000)
        r = subprocess.run(["/bin/sh", script, "mark-setup"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        check("mark-setup: the marker names this boot and the uptime now", r.returncode == 0
              and open(marker).read().split() == ["boot-one", "1000"], (r.stderr, open(marker).read() if os.path.exists(marker) else ""))
        at_uptime(1600)                     # 10 minutes of uptime later
        t0 = int(time.time())
        r = apply("")
        got = open(loaded).read() if os.path.exists(loaded) else ""
        runs = open(sdrun).read() if os.path.exists(sdrun) else ""
        until = int(got.split("meta time < ")[1].split()[0]) if "meta time < " in got else 0
        check("apply in the same boot, 10 minutes of uptime after the marker: apt's downloads, ended by a timer on the "
              "monotonic clock after the 2 h 50 min left (systemd-run --on-active), the rule's wall-clock end a backstop",
              r.returncode == 0 and "--on-active=10200s" in runs and "end-setup" in runs and t0 + 10200 <= until <= t0 + 10205
              and '"EGRESS=off"' in got, (r.stderr, runs, got[-300:]))
        for what, boot, up, mark in (("a marker of another boot (after a reboot)", "boot-two", "1600", "boot-one 1000"),
                                     ("a marker from the future (its uptime ahead of now: a reset clock or a forged one)",
                                      "boot-one", "900", "boot-one 1000"),
                                     ("a marker 3 hours of uptime old", "boot-one", str(1000 + 10800), "boot-one 1000"),
                                     ("a marker of the old kind (a date: the wall clock alone)", "boot-one", "1600",
                                      "2026-11-23T04:00:00+00:00")):
            open(os.path.join(proc, "sys", "kernel", "random", "boot_id"), "w").write(boot + "\n")
            at_uptime(int(up))
            open(marker, "w").write(mark + "\n")
            r = apply("")
            got = open(loaded).read() if os.path.exists(loaded) else ""
            check(f"...{what}: ignored, off is off (found by review: the wall clock decided)",
                  r.returncode == 0 and "80, 443" not in got and '"EGRESS=off"' in got, got[-300:])
        open(os.path.join(proc, "sys", "kernel", "random", "boot_id"), "w").write("boot-one\n")
        at_uptime(1600)
        open(marker, "w").write("boot-one 1000\n")
        r = subprocess.run(["/bin/sh", script, "end-setup"], capture_output=True, text=True,
                           env={"FAIL": "", "LOADED": loaded, "PATH": "/usr/bin:/bin", "SDRUN": sdrun})
        got = open(loaded).read() if os.path.exists(loaded) else ""
        check("end-setup (what the timer runs): the marker gone, off loaded", r.returncode == 0 and not os.path.exists(marker)
              and "80, 443" not in got and '"EGRESS=off"' in got, (r.stderr, got[-300:]))
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
                   "if ! sshd -t") and in_order(fn[fn.find("if ! sshd -t"):], "if ! sshd -t", "remote-uninstall rollback",
                                                "systemctl reload ssh"))
    un = readf("provisioning", "remote", "rootfs", "opt", "metacomputer", "bin", "remote-uninstall")
    check("remote-uninstall (review item 13): the service disabled, the network's dependency removed, only its own "
          "table deleted, the server's SSH settings put back, sshd checked before reload",
          "systemctl disable --now metacomputer-firewall.service" in un and "10-metacomputer-firewall.conf" in un
          and "nft delete table inet metacomputer" in un and "flush ruleset" not in un
          and in_order(un, "sshd -t", "systemctl reload ssh") and 'cp -pR "$BACKUP/sshd_config.d/."' in un)
    r = subprocess.run(["/bin/sh", "-n", os.path.join(LAB, "provisioning", "remote", "rootfs", "opt", "metacomputer", "bin",
                                                       "remote-uninstall")], capture_output=True)
    check("remote-uninstall parses", bool(un) and r.returncode == 0, r.stderr)
    nm = readf("provisioning", "remote", "rootfs", "etc", "systemd", "system", "NetworkManager.service.d",
               "10-metacomputer-firewall.conf")
    check("NetworkManager requires the firewall too (a server Ubuntu set up with it), and the firewall runs before it "
          "(found by review: only systemd-networkd did)", "Requires=metacomputer-firewall.service" in nm
          and "After=metacomputer-firewall.service" in nm and "NetworkManager.service" in unit.split("Before=")[1].split("\n")[0])
    check("...a server whose network neither manages is refused by the setup, its own settings put back",
          in_order(fn, "systemctl is-enabled --quiet systemd-networkd.service", "NetworkManager.service",
                   "remote-uninstall rollback", "false"))
    check("the setup's apt downloads for EGRESS=off: marked by bootstrap.sh and provision.sh, ended by finalize.sh, "
          "which loads the rules again", "guest-firewall mark-setup" in boot and "guest-firewall mark-setup" in fn
          and in_order(readf("provisioning", "guest", "finalize.sh"), "rm -f /etc/metacomputer/setup-egress",
                       "/opt/metacomputer/bin/guest-firewall apply", "finalized"))
    prov_fail = prov.split("end_setup_egress() {", 1)[1].split("\n}\n", 1)[0] if "end_setup_egress() {" in prov else ""
    check("a failed setup ends its downloads itself: provision.sh's failure handler removes the marker and loads the "
          "configured policy again", "rm -f /etc/metacomputer/setup-egress" in prov_fail
          and "guest-firewall apply" in prov_fail and "end_setup_egress' ERR" in prov)
    rbs = open(os.path.join(LAB, "scripts", "remote-build.sh")).read()
    closing = rbs.split("close_guest_setup_egress() {", 1)[1].split("\n}\n", 1)[0] if "close_guest_setup_egress() {" in rbs else ""
    check("...and both make it reach the disk (sync) before anything can power it off: a provider's stop is a power-off "
          "(found live: the marker came back after one)", "sync" in prov_fail
          and "guest-firewall apply && sync'" in closing)
    uninstall_order()


class FakeOCI:
    """The SDK facade's calls, in memory: instances, a network, availability domains that may be full."""

    def __init__(self, full=(), console="", fail_record=False):
        self.waits = []
        self.made_with = []
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
        self.fail_configure = False
        self.rule_changes = []

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
        self.inst[iid] = {"id": iid, "state": "RUNNING", "shape": shape, "name": name, "ad": ad, "op": op, "nsg": nsg_id}
        return iid

    def instances(self, tag_value=None, op=None):
        return [dict(i, created="t") for i in self.inst.values()
                if i["state"] != "TERMINATED" and (tag_value is None or i["name"] == tag_value)
                and (op is None or i.get("op") == op)]

    def make_nsg(self, name, egress, op, on_made=None):
        """Oracle's create, then its wait and rules, as the facade does them: a new group every time (never one found
        by name), tagged with its computer and operation; fail_configure fails after the create answered."""
        assert isinstance(op, str) and len(op) == 32, op
        self.made_with.append(egress)
        self.make_network()
        self.n += 1
        gid = f"nsg-{name}-{self.n}"
        self.nsgs[gid] = {"id": gid, "name": name, "op": op, "egress": None, "compartment": self.compartment}
        if on_made:
            on_made(gid)
        if self.fail_configure:
            raise AdapterError("Oracle Cloud refused the group's rules: 500 InternalError")
        self.nsg_egress(gid, egress)
        return gid

    def plant(self, name, op=None, egress="internet"):
        """A group made outside this test's create (another Mac's, an earlier version's without an operation tag)."""
        self.n += 1
        gid = f"nsg-{name}-{self.n}"
        self.nsgs[gid] = {"id": gid, "name": name, "op": op, "egress": egress, "compartment": self.compartment}
        return gid

    def groups(self, name):
        return [g for g in self.nsgs.values() if g["name"] == name]

    def drop(self, name):
        for g in self.groups(name):
            del self.nsgs[g["id"]]

    def nsg_owner(self, gid):
        g = self.nsgs.get(gid)
        if g is None or g["compartment"] != self.compartment:
            return None
        return g["name"], g["op"]

    def nsgs_of_op(self, name, op):
        return [g["id"] for g in self.nsgs.values() if g["name"] == name and g["op"] == op]

    def nsg_names(self):
        return [g["name"] for g in self.nsgs.values()]

    def nsg_egress(self, gid, egress):
        self.rule_changes.append(gid)
        self.nsgs[gid]["egress"] = egress

    def nsg_rules(self, gid):
        from cloud import ubuntu_vm
        g = self.nsgs[gid]
        return [{"direction": "EGRESS", "protocol": "all", "destination": c, "source": None}
                for c in (ubuntu_vm.public_v4() if g["egress"] == "internet" else ())]

    def delete_nsg_by_id(self, gid, name, op=None, legacy=False):
        who = self.nsg_owner(gid)
        if who is None or who[0] != name or not (who[1] == op if who[1] is not None else legacy):
            return False
        del self.nsgs[gid]
        return True

    def instance_nsgs(self, iid):
        i = self.inst.get(iid)
        return [i["nsg"]] if i and i.get("nsg") in self.nsgs else []

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
        self.waits.append((want, timeout))
        return want

    def terminate(self, iid):
        self.inst[iid]["state"] = "TERMINATED"

    def delete_network(self):
        had, self.net = self.net is not None, None
        return had

    def network_by_id(self, vid):
        return self.net if self.net and self.net["vcn"] == vid else None

    def delete_network_by_id(self, vid):
        if not self.network_by_id(vid) or self.nsgs:
            return False
        self.net = None
        return True

def oci_driver():
    print("\nthe OCI driver (providers/oci/adapter.py), on a stand-in API")
    api = FakeOCI(full={"AD-1"})
    a = oci.OCI(api=api, env={})
    OP1 = "a1" * 16
    made = a.create("box", size="arm-2x12", user_data="#cloud-config\n", op=OP1)
    gid = made["nsg"]
    check("create: the network first, then the instance in the first AD that has room (AD-1 full -> AD-2)",
          api.net is not None and made["ad"] == "AD-2" and [l[1] for l in api.launches] == ["AD-1", "AD-2"], api.launches)
    check("...arm-2x12 is VM.Standard.A1.Flex with 2 OCPU and 12 GB (the whole Always Free Arm allowance)",
          api.launches[-1][2:5] == ("VM.Standard.A1.Flex", 2, 12) and made["arch"] == "arm64", api.launches[-1])
    check("...in metacomputer's subnet, with the user-data it was given", api.launches[-1][5] == "s"
          and api.launches[-1][6] == "#cloud-config\n")
    check("...on its own security group, made first, letting out public addresses (review item 1)",
          api.launches[-1][7] == gid and api.nsgs[gid]["egress"] == "internet"
          and (api.nsgs[gid]["name"], api.nsgs[gid]["op"]) == ("box", OP1))
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
                states={"box": {"instance": made["instance"], "size": "arm-2x12", "nsg": gid, "op": OP1}})
    s.apply_egress("box", "off")
    check("EGRESS=off outside the computer: its security group lets nothing out", api.nsgs[gid]["egress"] == "off"
          and api.nsg_rules(gid) == [] and s.egress("box")["outside"] == "off")
    s.apply_egress("box", "internet")
    check("...and internet: every public block, nothing private", s.egress("box")["outside"] == "internet"
          and len(api.nsg_rules(gid)) == len(__import__("cloud.ubuntu_vm").ubuntu_vm.public_v4()))
    check("an allowlist of names is refused (NotSupported)", isinstance(_err(s.set_egress, "box", ["x.com"]), NotSupported))
    lazy = oci.OCI(env={}, computers={"box": {"VM_NAME": "box", "PROVIDER": "oci", "EGRESS": "internet"}},
                   states={"box": {"instance": made["instance"], "size": "arm-2x12", "nsg": gid, "op": OP1}})
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
    check("a stop is waited for through OCI's own shutdown window (900 s) and the transition after it, never less "
          "(found by review: 600 s reported a valid soft stop as failed)",
          api.waits and all(t >= 900 + 60 for w, t in api.waits if w == "STOPPED") and oci.STOP_WAIT_S >= 960,
          [t for w, t in api.waits if w == "STOPPED"])
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
          s.status("box")["state"] == "absent" and not api.groups("box"))
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


# --- the hourly providers' drivers (lib/cloud/vps.py, hetzner.py, digitalocean.py, vultr.py) ------------------------
SYNTH_TOKEN = "SYNTHETIC-TOKEN-0000"
VPS_ENV = {"hetzner": "HCLOUD_TOKEN", "digitalocean": "DIGITALOCEAN_TOKEN", "vultr": "VULTR_API_KEY"}


class StandInREST:
    """One provider's REST API in memory: the calls the drivers make, answered the way the provider answers them
    (paths, bodies, error shapes, as read from each one's API live). Every call is recorded (method, path, body); the
    Authorization header must carry the token and nothing is ever printed. `fail` answers a status first for a
    (method, path prefix); `lag` makes listings come back empty (DigitalOcean's do, now and then: found live);
    `stuck` keeps a server running whatever it's asked; `deny_ip` answers every call as Vultr does to an address not
    on the key's list."""

    def __init__(self, prov):
        self.prov = prov
        self.calls = []
        self.servers, self.fws, self.keys, self.rules = {}, {}, {}, {}
        self.n = 1000
        self.fail = {}
        self.lag = False
        self.stuck = False
        self.deny_ip = None
        self.deny_create = None
        self.deny_paths = set()
        self.lag_once = set()          # listings (first path part) that come back empty once
        self.lose_answer = set()       # (method, path) whose call is carried out, but whose answer is lost
        self.fw_action_error = False   # Hetzner: a new firewall's actions end in an error
        self.reflect = None            # an error body that reflects this text (a credential, say)
        self.vpc_resolver_field = True # DigitalOcean: the VPC's resolver_ip in its answer (else the documented rule)
        self.action_message = "it failed"   # Hetzner: what a failed action says
        self.fw_action_stuck = None    # Hetzner: a new firewall's action, with this command, that never ends
        self.vpc_resolver_ip = "10.10.15.254"   # DigitalOcean: the VPC's resolver_ip, when it's in the answer
        self.locked = lambda: None
        self.lock_seen = []

    def _id(self):
        self.n += 1
        if self.prov == "vultr":
            return f"{self.n:08x}-0000-4000-8000-{self.n:012x}"
        return self.n

    @staticmethod
    def ok(status, body=None):
        return status, {}, (json.dumps(body).encode() if body is not None else b"")

    def err(self, status, code, message):
        if self.prov == "hetzner":
            return self.ok(status, {"error": {"code": code, "message": message, "details": {}}})
        if self.prov == "digitalocean":
            return self.ok(status, {"id": code, "message": message})
        return self.ok(status, {"error": message, "status": status})

    def __call__(self, method, url, headers, body, timeout):
        if headers.get("Authorization") != f"Bearer {SYNTH_TOKEN}":
            return self.err(401, "unauthorized", "unable to authenticate you")
        rest = url.split("://", 1)[1].split("/", 2)[2]
        path, _, qs = rest.partition("?")
        q = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        data = json.loads(body) if body else None
        self.calls.append((method, path, data, q))
        if method == "GET" and path.split("/")[0] in self.lag_once and "/" not in path:
            self.lag_once.discard(path.split("/")[0])
            key = {"servers": "servers", "droplets": "droplets", "instances": "instances", "firewalls":
                   "firewalls" if self.prov != "vultr" else "firewall_groups", "ssh_keys": "ssh_keys"}.get(path, path)
            return self.ok(200, {key: [], "meta": {"pagination": {"next_page": None}, "links": {"next": ""}},
                                 "links": {}})
        if self.reflect is not None and method == "POST" and path in ("servers", "droplets", "instances"):
            return (500, {}, self.reflect) if self.prov == "vultr" and not self.reflect.startswith(b"{") \
                else (403, {}, self.reflect)
        if (method, path) in self.lose_answer:
            getattr(self, "h_" + self.prov)(method, path.split("/"), data, q)
            raise OSError("the connection was reset")
        if self.deny_ip:
            return self.err(401, "", f"Unauthorized IP address: {self.deny_ip}")
        for (m, prefix), statuses in list(self.fail.items()):
            if m == method and path.startswith(prefix) and statuses:
                return self.err(statuses.pop(0), "service_error", "a passing error")
        return getattr(self, "h_" + self.prov)(method, path.split("/"), data, q)

    # --- Hetzner Cloud ------------------------------------------------------------------------------------
    def h_hetzner(self, m, p, d, q):
        page = {"meta": {"pagination": {"next_page": None}}}
        if p[0] == "servers" and len(p) == 1:
            if m == "GET":
                sel = q.get("label_selector", "").replace("%3D", "=")
                out = [s for s in self.servers.values() if not sel or ("=" not in sel and sel in s["labels"])
                       or ("=" in sel and s["labels"].get(sel.split("=")[0]) == sel.split("=")[1])]
                return self.ok(200, dict(page, servers=[] if self.lag else out))
            self.lock_seen.append(("server", self.locked()))
            if self.deny_create:
                return self.err(*self.deny_create)
            sid = self._id()
            s = {"id": sid, "name": d["name"], "labels": d["labels"], "status": "running",
                 "server_type": {"name": d["server_type"]}, "location": {"name": d["location"]},
                 "public_net": {"ipv4": {"ip": "203.0.113.7"}}, "created": "t", "body": d}
            self.servers[sid] = s
            for f in d.get("firewalls") or []:
                self.fws[f["firewall"]]["applied_to"].append(sid)
            return self.ok(201, {"server": s, "root_password": None, "action": {"id": 1}})
        if p[0] == "servers" and len(p) >= 2 and p[1] != "actions":
            s = self.servers.get(int(p[1]))
            if s is None:
                return self.err(404, "not_found", "server not found")
            if m == "GET":
                return self.ok(200, {"server": s})
            if m == "DELETE":
                del self.servers[int(p[1])]
                for f in self.fws.values():
                    f["applied_to"] = [x for x in f["applied_to"] if x != int(p[1])]
                return self.ok(200, {"action": {"id": 2}})
            if m == "POST" and p[2] == "actions" and not self.stuck:
                s["status"] = {"poweron": "running", "shutdown": "off", "poweroff": "off"}[p[3]]
            return self.ok(201, {"action": {"id": 3, "command": p[3]}})
        if p[0] in ("firewalls", "servers") and len(p) == 3 and p[1] == "actions":
            if int(p[2]) == 77:
                return self.ok(200, {"action": {"id": 77, "status": "error",
                                                "error": {"code": "action_failed", "message": self.action_message}}})
            if int(p[2]) == 78:
                return self.ok(200, {"action": {"id": 78, "status": "running", "command": self.fw_action_stuck}})
            return self.ok(200, {"action": {"id": int(p[2]), "status": "success"}})
        if p[0] == "images":
            arch = q.get("architecture")
            return self.ok(200, {"images": [{"id": 77 if arch == "arm" else 78, "name": "ubuntu-24.04",
                                             "architecture": arch, "status": "available"}]})
        if p[0] == "firewalls":
            if len(p) == 1 and m == "GET":
                sel = q.get("label_selector", "").replace("%3D", "=")
                out = [f for f in self.fws.values() if ("=" not in sel and sel in f["labels"])
                       or ("=" in sel and f["labels"].get(sel.split("=")[0]) == sel.split("=")[1])]
                return self.ok(200, dict(page, firewalls=[] if self.lag else out))
            if len(p) == 1 and m == "POST":
                self.lock_seen.append(("firewall", self.locked()))
                for r in d["rules"]:
                    if len(r.get("destination_ips") or r.get("source_ips") or []) > 100:
                        return self.err(422, "invalid_input", "value required to be smaller")
                fid = self._id()
                self.fws[fid] = {"id": fid, "name": d["name"], "labels": d["labels"], "rules": d["rules"], "applied_to": []}
                acts = [{"id": 77, "command": "apply_firewall"}] if self.fw_action_error else []
                if self.fw_action_stuck is not None:
                    acts.append({"id": 78, "command": self.fw_action_stuck})
                return self.ok(201, {"firewall": self.fws[fid], "actions": acts})
            f = self.fws.get(int(p[1]))
            if f is None:
                return self.err(404, "not_found", "firewall not found")
            if m == "GET":
                return self.ok(200, {"firewall": f})
            if m == "DELETE":
                if f["applied_to"]:
                    return self.err(422, "resource_in_use", "firewall is still in use")
                del self.fws[int(p[1])]
                return self.ok(204)
            if m == "POST" and p[3] == "set_rules":
                f["rules"] = d["rules"]
                return self.ok(201, {"actions": [{"id": 9, "command": "set_firewall_rules"}]})
        if p[0] == "ssh_keys":
            if len(p) == 1 and m == "GET":
                sel = q.get("label_selector", "").replace("%3D", "=")
                out = [k for k in self.keys.values() if ("=" not in sel and sel in k["labels"])
                       or ("=" in sel and k["labels"].get(sel.split("=")[0]) == sel.split("=")[1])]
                return self.ok(200, dict(page, ssh_keys=[] if self.lag else out))
            if len(p) == 1 and m == "POST":
                self.lock_seen.append(("key", self.locked()))
                if any(k["public_key"] == d["public_key"] for k in self.keys.values()):
                    return self.err(409, "uniqueness_error", "SSH key with the same fingerprint already exists")
                kid = self._id()
                self.keys[kid] = dict(d, id=kid)
                return self.ok(201, {"ssh_key": self.keys[kid]})
            k = self.keys.get(int(p[1]))
            if k is None:
                return self.err(404, "not_found", "key not found")
            if m == "GET":
                return self.ok(200, {"ssh_key": k})
            del self.keys[int(p[1])]
            return self.ok(204)
        if p[0] == "primary_ips":
            return self.ok(200, dict(page, primary_ips=[]))
        return self.err(404, "not_found", "no such path")

    # --- DigitalOcean -------------------------------------------------------------------------------------
    def h_digitalocean(self, m, p, d, q):
        links = {"links": {}, "meta": {"total": 0}}
        if p[0] == "droplets" and len(p) == 1:
            if m == "GET":
                return self.ok(200, dict(links, droplets=[] if self.lag else list(self.servers.values())))
            self.lock_seen.append(("server", self.locked()))
            if self.deny_create:
                return self.err(*self.deny_create)
            sid = self._id()
            s = {"id": sid, "name": d["name"], "status": "active", "size_slug": d["size"], "region": {"slug": d["region"]},
                 "vpc_uuid": "vpc-1", "networks": {"v4": [{"type": "private", "ip_address": "10.10.0.5"},
                                     {"type": "public", "ip_address": "203.0.113.8"}]}, "created_at": "t", "body": d}
            self.servers[sid] = s
            return self.ok(202, {"droplet": s})
        if p[0] == "droplets":
            s = self.servers.get(int(p[1]))
            if s is None:
                return self.err(404, "not_found", "The resource you were accessing could not be found.")
            if m == "GET":
                return self.ok(200, {"droplet": s})
            if m == "DELETE":
                del self.servers[int(p[1])]
                for f in self.fws.values():
                    f["droplet_ids"] = [x for x in f["droplet_ids"] if x != int(p[1])]
                return self.ok(204)
            if not self.stuck:
                s["status"] = {"power_on": "active", "shutdown": "off", "power_off": "off"}[d["type"]]
            return self.ok(201, {"action": {"id": 5, "type": d["type"]}})
        if p[0] == "images":
            return self.ok(200, {"image": {"slug": p[1], "distribution": "Ubuntu", "name": "24.04 (LTS) x64"}})
        if p[0] in self.deny_paths:
            return self.err(403, "forbidden", "You are not authorized to perform this operation")
        if p[0] == "vpcs" and len(p) == 1:
            return self.ok(200, dict(links, vpcs=[{"id": "vpc-1", "ip_range": "10.10.0.0/20"}]))
        if p[0] == "vpcs":
            vpc = {"id": p[1], "ip_range": "10.10.0.0/20"}
            if self.vpc_resolver_field:
                vpc["resolver_ip"] = self.vpc_resolver_ip
            return self.ok(200, {"vpc": vpc})
        if p[0] in ("regions", "sizes", "actions"):
            return self.ok(200, dict(links, **{p[0]: []}))
        if p[0] == "firewalls":
            if len(p) == 1 and m == "GET":
                return self.ok(200, dict(links, firewalls=[] if self.lag else list(self.fws.values())))
            if len(p) == 1 and m == "POST":
                self.lock_seen.append(("firewall", self.locked()))
                fid = f"fw-{self._id()}"
                self.fws[fid] = {"id": fid, "name": d["name"], "status": "succeeded", "inbound_rules": d["inbound_rules"],
                                 "outbound_rules": d["outbound_rules"], "droplet_ids": [], "tags": []}
                return self.ok(202, {"firewall": self.fws[fid]})
            f = self.fws.get(p[1])
            if f is None:
                return self.err(404, "not_found", "The resource you were accessing could not be found.")
            if len(p) == 3 and p[2] == "droplets":
                f["droplet_ids"] += d["droplet_ids"]
                return self.ok(204)
            if m == "GET":
                return self.ok(200, {"firewall": f})
            if m == "PUT":
                f.update(inbound_rules=d["inbound_rules"], outbound_rules=d["outbound_rules"], droplet_ids=d["droplet_ids"])
                return self.ok(200, {"firewall": f})
            del self.fws[p[1]]
            return self.ok(204)
        if p[0] == "account" and p[1] == "keys":
            if len(p) == 2 and m == "GET":
                return self.ok(200, dict(links, ssh_keys=[] if self.lag else list(self.keys.values())))
            if len(p) == 2 and m == "POST":
                self.lock_seen.append(("key", self.locked()))
                if any(k["public_key"] == d["public_key"] for k in self.keys.values()):
                    return self.err(422, "unprocessable_entity", "SSH Key is already in use on your account")
                kid = self._id()
                self.keys[kid] = dict(d, id=kid)
                return self.ok(201, {"ssh_key": self.keys[kid]})
            k = self.keys.get(int(p[2]))
            if k is None:
                return self.err(404, "not_found", "The resource you were accessing could not be found.")
            if m == "GET":
                return self.ok(200, {"ssh_key": k})
            del self.keys[int(p[2])]
            return self.ok(204)
        return self.err(404, "not_found", "no such path")

    # --- Vultr --------------------------------------------------------------------------------------------
    def h_vultr(self, m, p, d, q):
        meta = {"meta": {"total": 0, "links": {"next": "", "prev": ""}}}
        if p[0] == "instances" and len(p) == 1:
            if m == "GET":
                return self.ok(200, dict(meta, instances=[] if self.lag else list(self.servers.values())))
            self.lock_seen.append(("server", self.locked()))
            if self.deny_create:
                return self.err(*self.deny_create)
            sid = self._id()
            s = {"id": sid, "label": d["label"], "status": "active", "power_status": "running", "plan": d["plan"],
                 "region": d["region"], "main_ip": "203.0.113.9", "date_created": "t", "body": d,
                 "default_password": "SYNTHETIC-ROOT-PASSWORD"}
            self.servers[sid] = s
            return self.ok(202, {"instance": s})
        if p[0] == "instances":
            s = self.servers.get(p[1])
            if s is None:
                return self.err(404, "", "Invalid instance-id.")
            if m == "GET":
                return self.ok(200, {"instance": s})
            if m == "DELETE":
                del self.servers[p[1]]
                return self.ok(204)
            if not self.stuck:
                s["power_status"] = {"start": "running", "halt": "stopped"}[p[2]]
            return self.ok(204)
        if p[0] == "os":
            return self.ok(200, dict(meta, os=[{"id": 2284, "name": "Ubuntu 24.04 LTS x64", "arch": "x64"},
                                               {"id": 1, "name": "Ubuntu 22.04 LTS x64", "arch": "x64"}]))
        if p[0] == "firewalls":
            if len(p) == 1 and m == "GET":
                return self.ok(200, dict(meta, firewall_groups=[] if self.lag else list(self.fws.values())))
            if len(p) == 1 and m == "POST":
                self.lock_seen.append(("firewall", self.locked()))
                fid = self._id()
                self.fws[fid] = {"id": fid, "description": d["description"], "instance_count": 0}
                self.rules[fid] = []
                return self.ok(201, {"firewall_group": self.fws[fid]})
            f = self.fws.get(p[1])
            if f is None:
                return self.err(404, "", "Invalid firewall group.")
            if len(p) == 3 and m == "GET":
                return self.ok(200, dict(meta, firewall_rules=self.rules[p[1]]))
            if len(p) == 3 and m == "POST":
                r = dict(d, id=len(self.rules[p[1]]) + 1, action="accept", direction="in",
                         source=f"{d['subnet']}/{d['subnet_size']}")
                self.rules[p[1]].append(r)
                return self.ok(201, {"firewall_rule": r})
            if m == "GET":
                return self.ok(200, {"firewall_group": f})
            if any(s["body"].get("firewall_group_id") == p[1] for s in self.servers.values()):
                return self.err(400, "", "Firewall group is linked to instances")
            del self.fws[p[1]]
            return self.ok(204)
        if p[0] == "ssh-keys":
            if len(p) == 1 and m == "GET":
                return self.ok(200, dict(meta, ssh_keys=[] if self.lag else list(self.keys.values())))
            if len(p) == 1 and m == "POST":
                self.lock_seen.append(("key", self.locked()))
                kid = self._id()
                self.keys[kid] = dict(d, id=kid)
                return self.ok(201, {"ssh_key": self.keys[kid]})
            k = self.keys.get(p[1])
            if k is None:
                return self.err(404, "", "Invalid SSH key.")
            if m == "GET":
                return self.ok(200, {"ssh_key": k})
            del self.keys[p[1]]
            return self.ok(204)
        return self.err(404, "", "no such path")


def plant_fw(h, prov, name, op):
    """Another operation's firewall for computer `name`, put straight into a stand-in (as the provider would list it)."""
    fid = h._id()
    if prov == "hetzner":
        h.fws[fid] = {"id": fid, "name": f"metacomputer-{name}.{op[:12]}", "labels": {"metacomputer": name,
                      "metacomputer-op": op}, "rules": [], "applied_to": []}
    elif prov == "digitalocean":
        fid = f"fw-{fid}"
        h.fws[fid] = {"id": fid, "name": f"metacomputer-{name}.{op[:12]}", "status": "succeeded", "inbound_rules": [],
                      "outbound_rules": [], "droplet_ids": [], "tags": []}
    else:
        h.fws[fid] = {"id": fid, "description": f"metacomputer-{name}.{op[:12]}", "instance_count": 0}
        h.rules[fid] = []
    return fid


def vps_api(prov, standin):
    """The driver's API on a stand-in, with a clock of its own: each wait's sleep moves it on, so a wait that never
    ends ends at once, at its timeout."""
    cls = cloud.ADAPTERS[prov]
    now = [0.0]
    return cls.API({VPS_ENV[prov]: SYNTH_TOKEN}, standin, sleep=lambda s: now.__setitem__(0, now[0] + s),
                   clock=lambda: now[0])


def vps_drivers():
    """The three hourly providers' drivers against stand-ins of their APIs."""
    import contextlib
    from cloud import vps
    from cloud.ubuntu_vm import public_v4
    print("\nthe hourly providers' drivers (Hetzner Cloud, DigitalOcean, Vultr), on stand-ins of their APIs")
    pub = set(public_v4())
    for prov in ("hetzner", "digitalocean", "vultr"):
        cls = cloud.ADAPTERS[prov]
        label = cls.manifest["label"]
        h = StandInREST(prov)
        held = {"n": 0}

        @contextlib.contextmanager
        def lock():
            held["n"] += 1
            try:
                yield
            finally:
                held["n"] -= 1
        h.locked = lambda: held["n"] > 0
        api = vps_api(prov, h)
        a = cls(api=api, network_lock=lock)
        made = []
        r = a.create("box", user_data="#cloud-config\n", public_key=KEY + " mcomp-box-admin", op="ab" * 16,
                     made=lambda what, rid: made.append((what, rid, held["n"] > 0)))
        srv = h.servers[int(r["instance"]) if prov != "vultr" else r["instance"]]
        body = srv["body"]
        order = [c[0] + " " + c[1] for c in h.calls if c[0] in ("POST", "PUT")]
        first_server = next(i for i, c in enumerate(order) if c.split()[1] in ("servers", "droplets", "instances"))
        first_fw = next(i for i, c in enumerate(order) if c.split()[1] == "firewalls")
        first_key = next(i for i, c in enumerate(order) if c.split()[1] in ("ssh_keys", "account/keys", "ssh-keys"))
        names = [w for w, _i, _l in made]
        check(f"{label}: its own firewall and its first-boot key are made before the server, under the lock, and each is "
              "told to the journal with its id as it's made; then the launch is journaled as sent, and the server's id "
              "the moment the create returns",
              first_fw < first_server and first_key < first_server and names[:2] == ["made_nsg", "made_key"]
              and all(lk for w, _i, lk in made if w in ("made_nsg", "made_key"))
              and names[2:] == ["launch_state", "made_instance", "launch_state"]
              and [x[1] for x in made[2:]] == ["sent", r["instance"], "made"]
              and ("firewall", True) in h.lock_seen and ("key", True) in h.lock_seen
              and ("server", False) in h.lock_seen, (order, made, h.lock_seen))
        check(f"{label}: the server is made exactly once (one create call)",
              sum(1 for c in order if c.split()[1] in ("servers", "droplets", "instances")) == 1, order)
        ud_ok = (body.get("user_data") == "#cloud-config\n" if prov != "vultr"
                 else base64.b64decode(body.get("user_data", "")).decode() == "#cloud-config\n")
        if prov == "hetzner":
            shape = (body["server_type"] == "cax11" and body["location"] == "fsn1" and body["image"] == 77
                     and body["public_net"] == {"enable_ipv4": True, "enable_ipv6": False}
                     and body["firewalls"] == [{"firewall": int(r["nsg"])}] and body["ssh_keys"] == [int(r["key"])]
                     and body["labels"] == {"metacomputer": "box", "metacomputer-op": "ab" * 16})
            tagged = shape
        elif prov == "digitalocean":
            shape = (body["size"] == "s-2vcpu-4gb" and body["region"] == "nyc3" and body["image"] == "ubuntu-24-04-x64"
                     and body["ipv6"] is False and body["backups"] is False and body["monitoring"] is False
                     and body["with_droplet_agent"] is False and body["ssh_keys"] == [int(r["key"])])
            tagged = body["name"] == "mc-" + "ab" * 16 + "-box"
            check(f"{label}: the firewall is applied to the Droplet right after it's made (it takes none in its create "
                  "call), and read back as applied", int(r["instance"]) in h.fws[r["nsg"]]["droplet_ids"]
                  and order.index(f"POST firewalls/{r['nsg']}/droplets") == first_server + 1, order)
            dns = [x for x in h.fws[r["nsg"]]["outbound_rules"] if x.get("ports") == "53"]
            check(f"{label}: no DNS exception when it's made: nothing private is let through until the resolver's exact "
                  "address is known (F3: it was the whole VPC range)", dns == [], dns)
        else:
            shape = (body["plan"] == "vc2-2c-4gb" and body["region"] == "ewr" and body["os_id"] == 2284
                     and body["enable_ipv6"] is False and body["backups"] == "disabled" and body["ddos_protection"] is False
                     and body["activation_email"] is False and body["firewall_group_id"] == r["nsg"]
                     and body["sshkey_id"] == [r["key"]])
            tagged = body["label"] == "mc-" + "ab" * 16 + "-box"
        check(f"{label}: the server's create call: the default size and region, Ubuntu 24.04 for its architecture, IPv4 "
              "only, no backups, its firewall and key, the user-data", shape and ud_ok, body)
        check(f"{label}: ...marked with its name and the build's operation (found again if its record is lost)",
              tagged and [i["name"] for i in api.instances("box", op="ab" * 16)] == ["box"]
              and api.instances("box", op="cd" * 16) == [], body)
        check(f"{label}: the default size's architecture ({'arm64' if prov == 'hetzner' else 'x86_64'}); x86 only where "
              "the provider has no Arm", r["arch"] == ("arm64" if prov == "hetzner" else "x86_64"))
        why = raises(a.create, "box", user_data="x", public_key=KEY, kind=AdapterError)
        check(f"{label}: a second create for the same name is refused (it has a server already)", why and "already" in why, why)
        for bad, kw in (("size", {"size": "huge"}), ("region", {"region": "../x"}), ("PERSIST", {"persist": "none"}),
                        ("user-data", {"user_data": ""}), ("key", {"public_key": ""}), ("egress", {"egress": "some"})):
            args = dict({"user_data": "x", "public_key": KEY}, **kw)
            why = raises(a.create, "other", **args, kind=AdapterError)
            check(f"{label}: create refuses a bad {bad}, saying why, before anything is made",
                  why and not h.servers.get("other"), why)
        st = {"box": dict(r)}
        comp = {"box": {"VM_NAME": "box", "PROVIDER": prov, "EGRESS": "internet"}}
        s = cls(api=api, computers=comp, states=st)
        rules = api.nsg_rules(r["nsg"])
        check(f"{label}: its firewall lets in SSH only (TCP 22, from anywhere)", vps.inbound_ok(rules), rules)
        if cls.manifest["outside_egress"]["enforced"]:
            check(f"{label}: ...and lets out exactly every public IPv4 block (TCP, UDP, ICMP), nothing private (but, at "
                  "DigitalOcean, DNS to its VPC)", api.egress_of(rules) == "internet" and s.egress("box")["outside"] == "internet"
                  and all(set(x["addresses"]) <= pub for x in rules if x["direction"] == "out"
                          and not (prov == "digitalocean" and x["ports"] == "53")), rules)
            s.apply_egress("box", "off")
            off = api.nsg_rules(r["nsg"])
            check(f"{label}: EGRESS=off outside the computer: read back as off, SSH still in",
                  s.egress("box")["outside"] == "off" and vps.inbound_ok(off), off)
            s.apply_egress("box", "internet")
            check(f"{label}: ...and internet again", s.egress("box")["outside"] == "internet")
        else:
            check(f"{label}: its firewall filters only what comes in: nothing claimed outside, `net egress` can't set it there",
                  s.egress("box")["outside"] == "none" and isinstance(_err(s.apply_egress, "box", "off"), NotSupported)
                  and cls.manifest["outside_egress"] == {"enforced": False, "private_ranges": False, "metadata": False,
                                                         "off": False})
        check(f"{label}: an allowlist of names is refused (NotSupported)", isinstance(_err(s.set_egress, "box", ["x.com"]),
                                                                                    NotSupported))
        check(f"{label}: status: awake, with its public address", s.status("box")["state"] == "awake"
              and s.status("box")["host"].startswith("203.0.113."), s.status("box"))
        h.calls.clear()
        if cls.SOFT_STOP == "guest":
            why = raises(s.sleep, "box", kind=AdapterError)
            check(f"{label}: stop without the guest asked first (Vultr has no shutdown call): refused, nothing sent",
                  why and "asks it over SSH" in why and not [c for c in h.calls if c[0] == "POST"], why)
            # F2: the CLI asked the guest over SSH and its SSH went quiet, but Vultr's own state still says running
            # (as it did live): that's no proof it's off, so nothing is halted.
            h.calls.clear()
            why = raises(s.sleep, "box", guest_up=False, asked=True, kind=AdapterError)
            check(f"{label}: asked, its SSH quiet, but Vultr's own state never says stopped: nothing halted; it says the "
                  "guest may still be on and billing, with stop --force as the choice (F2: SSH silence alone halted "
                  "it)", why and "may still be on" in why and "stop --force" in why
                  and not [c for c in h.calls if c[0] == "POST"], (why, h.calls[-3:]))
            srv["power_status"] = "stopped"          # Vultr's own state says it's off: that, and only that, counts
            h.calls.clear()
            s.sleep("box", guest_up=None, asked=True)
        else:
            s.sleep("box")
        acts = [c for c in h.calls if c[0] == "POST"]
        if cls.SOFT_STOP == "api":
            check(f"{label}: stop asks the guest to shut down (the provider's soft call), never a hard power-off unasked",
                  len(acts) == 1 and ("shutdown" in json.dumps(acts[0][2]) or acts[0][1].endswith("/shutdown"))
                  and s.status("box")["state"] == "stopped", acts)
        else:
            check(f"{label}: stop once Vultr's own state says the asked guest is off: nothing sent, stopped",
                  acts == [] and s.status("box")["state"] == "stopped", acts)
        h.calls.clear()
        s.wake("box")
        acts = [c for c in h.calls if c[0] == "POST"]
        check(f"{label}: start powers a stopped server on, and waits for it to run", len(acts) == 1
              and s.status("box")["state"] == "awake", acts)
        h.calls.clear()
        why = raises(s.wake, "box", guest_up=False, kind=AdapterError)
        check(f"{label}: start of a running server whose SSH doesn't answer: refused, nothing stopped",
              why and "Nothing was stopped" in why and not [c for c in h.calls if c[0] == "POST"], why)
        h.calls.clear()
        s.sleep("box", force=True)
        acts = [c for c in h.calls if c[0] == "POST"]
        check(f"{label}: stop --force: a power-off at once", len(acts) == 1 and ("power_off" in json.dumps(acts[0][2])
              or acts[0][1].endswith(("/poweroff", "/halt"))), acts)
        s.wake("box")
        h.stuck = True
        h.calls.clear()
        why = raises(s.sleep, "box", kind=AdapterError)
        check(f"{label}: a guest that doesn't shut down: refused after waiting, saying stop --force; never powered off "
              "unasked", why and "stop --force" in why and not any(
                  c[1].endswith(("/poweroff", "/halt")) or "power_off" in json.dumps(c[2]) for c in h.calls), why)
        h.stuck = False
        check(f"{label}: list: metacomputer's servers", [i["name"] for i in s.list()] == ["box"])
        # A server that isn't metacomputer's is never touched.
        if prov == "hetzner":
            h.servers[9] = {"id": 9, "name": "theirs", "labels": {}, "status": "running", "server_type": {"name": "x"},
                            "location": {"name": "fsn1"}, "public_net": {"ipv4": {"ip": "203.0.113.70"}}}
            other = "9"
        elif prov == "digitalocean":
            h.servers[9] = {"id": 9, "name": "web-1", "status": "active", "size_slug": "x", "region": {"slug": "nyc3"},
                            "networks": {"v4": []}}
            other = "9"
        else:
            other = "00000009-0000-4000-8000-000000000009"
            h.servers[other] = {"id": other, "label": "web-1", "status": "active", "power_status": "running", "plan": "x",
                                "region": "ewr", "main_ip": "203.0.113.70", "body": {}}
        why = raises(api.instance, other, kind=AdapterError)
        check(f"{label}: a server metacomputer didn't mark is refused by id and left out of its lists",
              why and "leaves it alone" in why and "theirs" not in [i["name"] for i in api.instances()]
              and "web-1" not in [i["name"] for i in api.instances()], why)
        why = raises(api.terminate, other, kind=AdapterError) if prov == "vultr" else None
        if prov == "vultr":
            check(f"{label}: ...and never deleted by id", why and other in h.servers)
        del h.servers[int(other) if prov != "vultr" else other]
        # Lists that lag behind (DigitalOcean's, found live): delete still goes by the ids recorded.
        h.lag = True
        s.delete("box")
        h.lag = False
        check(f"{label}: delete: the server, its firewall and its key gone, by the ids recorded (even while the "
              "provider's lists lag behind)", not h.servers and not h.fws and not h.keys, (h.servers, h.fws, h.keys))
        check(f"{label}: status after delete: absent", s.status("box")["state"] == "absent")
        # Reads retried on throttling; a create never.
        h2 = StandInREST(prov)
        api2 = vps_api(prov, h2)
        path = {"hetzner": "servers", "digitalocean": "droplets", "vultr": "instances"}[prov]
        h2.fail[("GET", path)] = [429, 503]
        check(f"{label}: a read throttled (429) or failing in passing (503) is tried again", api2.instances() == []
              and sum(1 for c in h2.calls if c[0] == "GET" and c[1] == path) == 3)
        h2.fail[("POST", path)] = [503]
        why = raises(cls(api=api2).create, "two", user_data="x", public_key=KEY, kind=AdapterError)
        check(f"{label}: a create that fails (503) is never tried again: a retry after a lost answer could make a second "
              "server", why and sum(1 for c in h2.calls if c[0] == "POST" and c[1] == path) == 1 and not h2.servers, why)
        # Credentials and what's said.
        why = raises(cls.API, {}, h2, kind=AdapterError)
        check(f"{label}: credentials only from the environment: a missing token is named ({VPS_ENV[prov]}), never shown",
              why and VPS_ENV[prov] in why)
        why = raises(cls.API, {VPS_ENV[prov]: SYNTH_TOKEN}, None, kind=AdapterError)
        check(f"{label}: no HTTP transport given: refused (nothing reaches the network on its own)", why is not None)
        h3 = StandInREST(prov)
        h3.deny_create = (403, "forbidden", "SYNTHETIC: not allowed to make that")
        why = raises(cls(api=vps_api(prov, h3)).create, "three", user_data="x", public_key=KEY, kind=AdapterError)
        check(f"{label}: the provider's refusal is said in its own words, without the token",
              why and "not allowed to make that" in why and SYNTH_TOKEN not in why and SYNTH_TOKEN not in repr(vps_api(prov, h3)),
              why)
        check(f"{label}: the manifest: stop-start, a disk that keeps everything, no snapshots, its own firewall in the "
              "guest", cls.manifest["sleep_wake"]["kind"] == "stop-start" and cloud.capabilities.choose_persist(cls.manifest)
              == "disk" and cls.manifest["snapshots"] is None and cls.manifest["egress"]["model"] == "guest-firewall")
        check(f"{label}: its billing truth: a stopped server bills in full, the guest powering off saves nothing, and its "
              "rate is the same awake and stopped", cls.manifest["billing"]["stopped"] == "full"
              and cls.manifest["billing"]["guest_poweroff"] == "nothing" and "only deleting it" in cls.manifest["billing"]["note"]
              and a.rates()["awake_per_hour"] == a.rates()["asleep_per_hour"] > 0)
        check(f"{label}: the metadata service is never claimed as refused outside", cls.manifest["outside_egress"]["metadata"] is False)
        for size in cls.SIZES:
            rr = a.rates(size)
            check(f"{label}: rates {size}: a number, the list price's date and the billing truth said",
                  rr["awake_per_hour"] > 0 and "2026-11-23" in rr["note"] and "only deleting it" in rr["note"], rr)
    # Hetzner: its own details.
    hetzner, vultr = cloud.registry.module("hetzner"), cloud.registry.module("vultr")
    rs = hetzner.rules("internet")
    check("Hetzner Cloud: at most 100 addresses in one rule (its limit, found live), the 103 public blocks in two rules "
          "per protocol, read back as one", all(len(x.get("destination_ips") or []) <= 100 for x in rs)
          and sum(1 for x in rs if x["direction"] == "out") == 6
          and vps.egress_from([hetzner.normal(x) for x in rs], off=[hetzner.normal(hetzner.OFF_RULE)]) == "internet")
    check("Hetzner Cloud: EGRESS=off is one outbound rule that reaches no one (with none, Hetzner lets everything out): "
          "none at all reads back as not what metacomputer set",
          vps.egress_from([hetzner.normal(x) for x in hetzner.rules("off")], off=[hetzner.normal(hetzner.OFF_RULE)]) == "off"
          and vps.egress_from([], off=[hetzner.normal(hetzner.OFF_RULE)]) == "other"
          and hetzner.OFF_RULE["destination_ips"] == ["192.0.2.1/32"])
    h = StandInREST("hetzner")
    api = vps_api("hetzner", h)
    k1, new1 = api.make_key("box", "ab" * 16, KEY)
    k2, new2 = api.make_key("box", "cd" * 16, KEY)
    check("Hetzner Cloud: the same key already there for this computer (a clean-up that didn't finish) is used, not "
          "made again, and not recorded as this build's", k1 == k2 and new1 and not new2)
    h.keys[int(k1)]["labels"] = {"metacomputer": "elsewhere"}
    check("...one that's another computer's: refused", raises(api.make_key, "box", "ef" * 16, KEY, kind=AdapterError))
    # Vultr: the address its key doesn't accept, said plainly.
    h = StandInREST("vultr")
    h.deny_ip = "198.51.100.23"
    why = raises(vps_api("vultr", h).instances, kind=AdapterError)
    check("Vultr: 'Unauthorized IP address': says which address to add (/32) and where (the key's access control)",
          why and "198.51.100.23/32" in why and "Access Control" in why and SYNTH_TOKEN not in why, why)
    h = StandInREST("vultr")
    h.deny_create = (403, "", "Unauthorized: missing permission compute.instance.create")
    why = raises(cloud.ADAPTERS["vultr"](api=vps_api("vultr", h)).create, "box", user_data="x", public_key=KEY,
                 kind=AdapterError)
    check("Vultr: a permission its policy lacks: Vultr's own words for it, and which call it was (nothing guessed)",
          why and "missing permission compute.instance.create" in why and "POST /instances" in why, why)
    h = StandInREST("vultr")
    r = cloud.ADAPTERS["vultr"](api=vps_api("vultr", h)).create("box", user_data="x", public_key=KEY, op="ab" * 16)
    check("Vultr: the root password it returns is never kept or shown (bootstrap.sh locks it)",
          "SYNTHETIC-ROOT-PASSWORD" not in json.dumps(r))
    check("Vultr: its firewall group has its SSH rules before the instance is made (a group with no rule isn't "
          "enforced)", [c[1] for c in h.calls if c[0] == "POST"].index("instances")
          > max(i for i, c in enumerate(c[1] for c in h.calls if c[0] == "POST") if c.endswith("/rules")))
    check("Vultr: x86 only, no Arm size offered", all(v[0] == "x86_64" for v in vultr.SIZES.values()))
    # The HTTP transport: only the providers' API hosts, over https, and no redirect followed.
    import provider_http
    for url in ("http://api.hetzner.cloud/v1/servers", "https://example.com/v1/servers", "https://api.vultr.com.evil/x"):
        check(f"provider_http refuses {url.split('/')[2]} over {url.split(':')[0]}", isinstance(
            _err(provider_http.send, "GET", url, {}, None, 5), OSError))
    check("provider_http never follows a redirect (it would carry the token elsewhere)",
          provider_http._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere/") is None)
    apt_mirror_rewrite()
    prov_sh = open(os.path.join(LAB, "provisioning", "guest", "provision.sh")).read()
    i_off = prov_sh.find('if [ "${MCOMP_TARGET:-}" = remote ]; then\n  # A VM reached over the internet: cloud-init off before the reboot')
    check("provision.sh on a computer elsewhere: cloud-init off before the reboot into the desktop (its datasource may "
          "need the metadata service, which the firewall refuses: Vultr's fell back to none and made new host keys)",
          0 < i_off < prov_sh.index('date -Is > "$STATE/provisioned"')
          and "touch /etc/cloud/cloud-init.disabled" in prov_sh[i_off:i_off + 600])
    bootstrap = open(os.path.join(LAB, "provisioning", "remote", "bootstrap.sh")).read()
    check("bootstrap.sh on a provider's computer: root's key (the provider's first-boot one) removed and its password "
          "locked (Vultr sets one); your own server's root left alone",
          'if [ -z "$OWNER" ]; then\n  rm -f /root/.ssh/authorized_keys /root/.ssh/authorized_keys2\n  passwd -l root' in bootstrap)


def apt_mirror_rewrite():
    """provision.sh's apt sources on a provider's computer: a mirror that resolves to a private address (DigitalOcean's
    mirrors.digitalocean.com, inside DigitalOcean: found live) is replaced by Ubuntu's archive (ports for arm64); a
    public source is left; your own server's sources are left alone (the call is only for a computer with no owner).
    It runs before anything uses apt, once DNS works (on DigitalOcean the resolver is let through after SSH answers).
    The block runs in a sandbox, with getent and dpkg stand-ins."""
    src = open(os.path.join(LAB, "provisioning", "guest", "provision.sh")).read()
    guard = 'if [ "$TARGET" = remote ] && [ ! -s /etc/metacomputer/owner ]; then'
    blk = src[src.index("is_private4() {"):src.index(guard)]
    td = tempfile.mkdtemp(prefix="mcapt-")
    try:
        results = {}
        for arch in ("amd64", "arm64"):
            d = os.path.join(td, arch)
            os.makedirs(os.path.join(d, "etc", "apt", "sources.list.d"))
            f = os.path.join(d, "etc", "apt", "sources.list.d", "ubuntu.sources")
            with open(f, "w") as fh:
                fh.write("Types: deb\nURIs: http://mirrors.example-cloud.test/ubuntu/\nSuites: noble noble-updates\n\n"
                         "Types: deb\nURIs: http://security.ubuntu.com/ubuntu/\nSuites: noble-security\n\n"
                         "Types: deb\nURIs: http://gone.example-cloud.test/x/\nSuites: noble\n")
            with open(os.path.join(d, "etc", "apt", "sources.list"), "w") as fh:   # Ubuntu 24.04's: a comment only
                fh.write("# Ubuntu sources have moved to /etc/apt/sources.list.d/ubuntu.sources\n")
            trapped = os.path.join(d, "trapped")
            # As provision.sh runs it: set -E and an ERR trap that marks the setup failed (found live: a grep with no
            # match inside $(...) ran it there, and the setup was marked failed with no message).
            script = (f'set -Eeuo pipefail\ntrap \'echo trapped >> {trapped}\' ERR\ndpkg() {{ echo {arch}; }}\n'
                      'getent() { case "$2" in mirrors.example-cloud.test) echo "10.0.0.2 STREAM";; '
                      'gone.example-cloud.test) return 2;; *) echo "203.0.113.20 STREAM";; esac; }\n'
                      'sed() { if [ "$1" = -i ]; then shift; f="${@: -1}"; /usr/bin/sed "$@" > "$f.new" && mv "$f.new" "$f"; '
                      'else /usr/bin/sed "$@"; fi; }\n' + blk.replace("/etc/apt/", os.path.join(d, "etc", "apt") + "/")
                      + "\napt_public_mirror\n")
            r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=60)
            results[arch] = (r.returncode, open(f).read(), r.stderr[-200:], os.path.exists(trapped))
        amd, arm = results["amd64"], results["arm64"]
        i_guard, i_apt = src.index(guard), src.index('"${APT[@]}" purge snapd')
        check("provision.sh: a provider's apt mirror on its private network is replaced by Ubuntu's archive (ports for "
              "arm64), a public source left, before the first apt call; only on a computer with no owner (your own "
              "server's sources are yours)",
              amd[0] == 0 and "URIs: http://archive.ubuntu.com/ubuntu/" in amd[1] and "security.ubuntu.com" in amd[1]
              and "mirrors.example-cloud" not in amd[1] and "URIs: http://gone.example-cloud.test/x/" in amd[1]
              and "URIs: http://ports.ubuntu.com/ubuntu-ports/" in arm[1]
              and i_guard < i_apt and "apt_public_mirror" in src[i_guard:i_guard + 200], results)
        check("provision.sh: ...under its own set -E and ERR trap, a source file with no URL (Ubuntu 24.04's sources.list) "
              "and a host that doesn't resolve never run the trap (found live: they marked the setup failed)",
              not amd[3] and not arm[3] and amd[0] == 0, results)
        boot = open(os.path.join(LAB, "provisioning", "remote", "bootstrap.sh")).read()
        check("...not in bootstrap.sh any more: at first boot a DigitalOcean computer can't resolve names yet (its "
              "resolver is let through once the setup reads it)", "apt_public_mirror" not in boot and "is_private4" not in boot)
    finally:
        shutil.rmtree(td, ignore_errors=True)


AUDIT_GUARD = None    # a harness may run each part on its own (one part's crash doesn't hide the rest)


def audit_fixes():
    """The audit's seven findings (2026-11-23), each against the stand-ins: F1 a launch reconciled by its journaled ids
    (read one by one), never by an empty listing, and one with no answer left unresolved; F2 tested in vps_drivers and
    vps_journal; F3 DigitalOcean's DNS to the resolver's exact address only; F4 a firewall claimed only by this
    operation's own create; F5 its id journaled before anything configures it; F6 provider errors scrubbed of
    credentials; F7 DigitalOcean's vpc:read checked before anything is made, and named by cloud doctor."""
    print("\nthe audit's findings (F1-F7), on the stand-ins")
    import contextlib
    import remote as remote_mod
    rd = tempfile.mkdtemp(prefix="mcaudit-")
    old = (remote_mod.user_data, remote_mod.mac_lock, remote_mod.LOST_LAUNCH_GAP_S if hasattr(remote_mod,
           "LOST_LAUNCH_GAP_S") else None)
    try:
        os.environ["MCOMP_LOCK_DIR"] = os.path.join(rd, "locks")
        remote_mod.user_data = lambda s: "#cloud-config\n"
        if hasattr(remote_mod, "LOST_LAUNCH_GAP_S"):
            remote_mod.LOST_LAUNCH_GAP_S = 0
        create_path = {"hetzner": "servers", "digitalocean": "droplets", "vultr": "instances"}
        n = [0]

        def box(prov):
            n[0] += 1
            d = os.path.join(rd, f"{prov}-{n[0]}")
            os.makedirs(d)
            with open(os.path.join(d, "admin.pub"), "w") as f:
                f.write(KEY + "\n")
            h = StandInREST(prov)
            api = vps_api(prov, h)
            remote_mod.API_FACTORY = lambda: api
            return h, api, {"VM_NAME": "box", "PLACE": prov, "PROVIDER": prov, "EGRESS": "internet", "RUN_DIR": d,
                            "SSH_DIR": d}

        def args(**k):
            return type("A", (), dict({"op": None, "keep": False, "network_too": False}, **k))()
        parts = []

        def part_f2():
            # F2: Vultr's stop, asked, with the guest's SSH silent and Vultr's own state still running: never halted.
            h, api, st0 = box("vultr")
            remote_mod.cmd_create(st0, args(op="c9" * 16))
            srv = next(iter(h.servers.values()))
            real_up, real_time = remote_mod.guest_up, remote_mod.time
            clock = [0.0]
            remote_mod.guest_up = lambda s_, st_, tries=3, wait=5.0: False
            remote_mod.time = type("T", (), {"monotonic": staticmethod(lambda: clock[0]), "time": staticmethod(real_time.time),
                                             "sleep": staticmethod(lambda x: clock.__setitem__(0, clock[0] + x))})
            h.calls.clear()
            try:
                why = raises(remote_mod.cmd_sleep, st0, type("A", (), {"force": False, "asked": True})(), kind=Exception)
            finally:
                remote_mod.guest_up, remote_mod.time = real_up, real_time
            halts = [c for c in h.calls if c[0] == "POST" and c[1].endswith("/halt")]
            check("Vultr (F2): stop, the guest asked, its SSH silent, Vultr's own state still running: no halt is sent; "
                  "it says the guest may still be on and billing, stop --force being the explicit choice",
                  not halts and srv["power_status"] == "running" and why and "may still be on" in why
                  and "stop --force" in why, (halts, why))
        parts.append(part_f2)

        def part_f1_f4():
            for prov in ("hetzner", "digitalocean", "vultr"):
                label = cloud.ADAPTERS[prov].manifest["label"]
                # F1: the create reaches the provider, its answer is lost, and the listing lags behind.
                h, api, st0 = box(prov)
                h.lose_answer.add(("POST", create_path[prov]))
                h.lag_once.update({create_path[prov]} if False else set())
                real_instances = api.instances
                api.instances = lambda tag_value=None, op=None: []          # the listing lags behind, throughout
                op = "a1" * 16
                raises(remote_mod.cmd_create, st0, args(op=op), kind=Exception)
                rc = remote_mod.cmd_abort(st0, args(op=op))
                j = remote_mod.read_state(st0["RUN_DIR"])
                check(f"{label} (F1): a create whose answer was lost while the listing lags: the clean-up never says nothing "
                      "is left: it fails (exit 3) and keeps the journal; the server and its firewall are left for it",
                      rc == 3 and j.get("launching") == op and j.get("launch_state") == "sent" and len(h.servers) == 1
                      and len(h.fws) == 1, (rc, j, len(h.servers)))
                why = raises(remote_mod.cmd_create, st0, args(op="b2" * 16), kind=remote_mod.Refused)
                check(f"{label} (F1): ...and a new create under that name is refused until it's resolved (no second "
                      "server), saying how to check", why and "cloud status" in why and len(h.servers) == 1, why)
                api.instances = real_instances                               # the listing catches up
                rc = remote_mod.cmd_abort(st0, args(op=op))
                j = remote_mod.read_state(st0["RUN_DIR"])
                check(f"{label} (F1): ...once it shows (by its mark), the clean-up deletes it, its firewall and key, and "
                      "the journal is closed", rc == 0 and not h.servers and not h.fws and not h.keys
                      and "launching" not in j, (rc, j, h.servers, h.fws, h.keys))
                # F1: an instance recorded by id, while the listing lags: deleted by its id (and --keep keeps it).
                h, api, st0 = box(prov)
                remote_mod.cmd_create(st0, args(op="c3" * 16))
                api.instances = lambda tag_value=None, op=None: []
                rc = remote_mod.cmd_abort(st0, args(op="c3" * 16, keep=True))
                srv = next(iter(h.servers.values()))
                off = srv.get("status") == "off" or srv.get("power_status") == "stopped"
                check(f"{label} (F1): --keep-on-failure with the listing lagging: the journaled instance is kept and "
                      "powered off by its id", rc == 0 and off and remote_mod.read_state(st0["RUN_DIR"]).get("kept"), (rc, srv))
                rc = remote_mod.cmd_abort(st0, args(op="c3" * 16))
                check(f"{label} (F1): ...and the clean-up deletes it by its id, though the listing shows nothing",
                      rc == 0 and not h.servers and not h.fws and not h.keys, (h.servers, h.fws, h.keys))
                # F4: a firewall of this computer's from another operation exists; the listing misses it once.
                h, api, st0 = box(prov)
                theirs = plant_fw(h, prov, "box", "d4" * 16)
                h.lag_once.add("firewalls")
                h.fail[("POST", create_path[prov])] = [400]                  # the create is refused: the clean-up runs
                raises(remote_mod.cmd_create, st0, args(op="e5" * 16), kind=Exception)
                j = remote_mod.read_state(st0["RUN_DIR"])
                remote_mod.cmd_abort(st0, args(op="e5" * 16))
                check(f"{label} (F4): a firewall is claimed only by this operation's own create: another operation's (which "
                      "a lagging listing hid once) is never recorded as this one's, nor deleted by its clean-up",
                      j.get("made_nsg") and str(j.get("made_nsg")) != str(theirs) and theirs in h.fws and len(h.fws) == 1,
                      (j.get("made_nsg"), theirs, list(h.fws)))
                check(f"{label} (F1): a create the provider refused (a 4xx) is journaled as refused: nothing to look for",
                      j.get("launch_state") == "refused", j.get("launch_state"))
        parts.append(part_f1_f4)

        def part_f1_attach():
            # F1: the instance's id is journaled before anything else is asked of it (DigitalOcean: the firewall attach).
            h, api, st0 = box("digitalocean")
            h.fail[("POST", "firewalls/")] = [500]                           # applying the firewall fails...
            h.fail[("DELETE", "droplets/")] = [500] * 5                      # ...and so does the delete that follows
            api.instances = lambda tag_value=None, op=None: []
            raises(remote_mod.cmd_create, st0, args(op="f6" * 16), kind=Exception)
            j = remote_mod.read_state(st0["RUN_DIR"])
            rc = remote_mod.cmd_abort(st0, args(op="f6" * 16))
            check("DigitalOcean (F1): the Droplet's id is journaled the moment its create returns, before the firewall is "
                  "applied: a failure after that is cleaned up by that id, the listing lagging",
                  j.get("made_instance") and rc == 0 and not h.servers, (j, rc, h.servers))
        parts.append(part_f1_attach)

        def part_f5():
            # F5: a firewall's id is journaled before anything configures it.
            for prov, knob in (("vultr", lambda h: h.fail.__setitem__(("POST", "firewalls/"), [500])),
                               ("hetzner", lambda h: setattr(h, "fw_action_error", True))):
                h, api, st0 = box(prov)
                knob(h)
                raises(remote_mod.cmd_create, st0, args(op="a7" * 16), kind=Exception)
                j = remote_mod.read_state(st0["RUN_DIR"])
                h.fail.clear()
                h.fw_action_error = False
                remote_mod.cmd_abort(st0, args(op="a7" * 16))
                check(f"{cloud.ADAPTERS[prov].manifest['label']} (F5): the firewall's id reaches the journal as soon as its "
                      "create returns: configuring it fails after (a rule, its actions), and the clean-up deletes it by that "
                      "id", j.get("made_nsg") and not h.fws and not h.servers, (j.get("made_nsg"), list(h.fws)))
        parts.append(part_f5)

        def part_f6():
            # F6: what a provider says is scrubbed of credentials before anything prints it.
            shaped = "dop_v1_" + "0123456789abcdef" * 4
            for prov in ("hetzner", "digitalocean", "vultr"):
                label = cloud.ADAPTERS[prov].manifest["label"]
                body = {"hetzner": {"error": {"code": SYNTH_TOKEN, "message": f"bad token {SYNTH_TOKEN} {shaped}"}},
                        "digitalocean": {"id": "unauthorized", "message": f"bad token Bearer {SYNTH_TOKEN} {shaped}"},
                        "vultr": {"error": f"bad token {SYNTH_TOKEN} {shaped}", "status": 403}}[prov]
                h = StandInREST(prov)
                h.reflect = json.dumps(body).encode()
                why = raises(cloud.ADAPTERS[prov](api=vps_api(prov, h)).create, "box", user_data="x", public_key=KEY,
                             kind=AdapterError)
                check(f"{label} (F6): an error that reflects the token (or anything credential-shaped) is printed without "
                      "it", why and SYNTH_TOKEN not in why and shaped not in why and "[redacted]" in why, why)
                h2 = StandInREST(prov)
                api2 = vps_api(prov, h2)

                def reset(*a, **k):
                    raise OSError(f"proxy said: Authorization: Bearer {SYNTH_TOKEN}")
                api2._http = reset
                why = raises(api2.instances, kind=AdapterError)
                check(f"{label} (F6): ...and a connection error that carries it too", why and SYNTH_TOKEN not in why, why)
            h = StandInREST("vultr")
            h.reflect = f"<html>proxy page {SYNTH_TOKEN}</html>".encode()
            why = raises(cloud.ADAPTERS["vultr"](api=vps_api("vultr", h)).create, "box", user_data="x", public_key=KEY,
                         kind=AdapterError)
            check("Vultr (F6): an answer that isn't its error format is never echoed raw", why and SYNTH_TOKEN not in why
                  and "proxy page" not in why, why)
        parts.append(part_f6)

        def part_f7():
            # F7: DigitalOcean's vpc:read, checked before anything is made, and named by cloud doctor.
            h = StandInREST("digitalocean")
            h.deny_paths.add("vpcs")
            a = cloud.ADAPTERS["digitalocean"](api=vps_api("digitalocean", h))
            why = raises(a.create, "box", user_data="x", public_key=KEY, kind=AdapterError)
            check("DigitalOcean (F7): a token without vpc:read: refused before anything is made, naming the scope",
                  why and "vpc:read" in why and not [c for c in h.calls if c[0] == "POST"], (why, h.calls[-3:]))
            d = {c["check"]: c for c in a.doctor()}
            check("DigitalOcean (F7): cloud doctor names the missing scope", "token scope vpc:read" in d
                  and not d["token scope vpc:read"]["ok"] and "vpc:read" in d["token scope vpc:read"]["detail"]
                  and d.get("token scope droplet:read", {}).get("ok"), list(d))
            doc = open(os.path.join(LAB, "docs", "remote.md")).read()
            check("docs (F7): the scopes a DigitalOcean token needs include vpc:read",
                  "`vpc:read`" in next((ln for ln in doc.splitlines() if ln.startswith("| DigitalOcean | `DIGITALOCEAN_TOKEN`")), ""))
        parts.append(part_f7)

        def part_f3():
            # F3: DigitalOcean's DNS to the resolver's exact addresses only, checked against the Droplet's VPC.
            dom = cloud.registry.module("digitalocean")
            h, api, st0 = box("digitalocean")
            remote_mod.cmd_create(st0, args(op="b8" * 16))
            j = remote_mod.read_state(st0["RUN_DIR"])
            pre = api.nsg_rules(j["nsg"])
            check("DigitalOcean (F3): as made, its firewall lets nothing private through on port 53 (the whole VPC range "
                  "was)", not [x for x in pre if x["ports"] == "53"], [x for x in pre if x["ports"] == "53"])
            wide = [x for x in pre if x["ports"] != "53"] + [{"direction": "out", "protocol": p, "ports": "53",
                                                             "addresses": ("10.0.0.0/8",)} for p in ("tcp", "udp")]
            check("DigitalOcean (F3): port 53 to a whole private range reads back as not what metacomputer set (it read "
                  "as internet)", api.egress_of(wide) == "other", wide[-2:])
            def R(**k):
                return type("A", (), dict({"set": [], "first_boot": False, "pinned": False}, **k))()
            # At first boot, an address the Droplet names that isn't DigitalOcean's own resolver for its VPC, one outside
            # the VPC, or something that isn't an address: refused, nothing pinned.
            why = raises(remote_mod.cmd_resolvers, st0, R(set=["10.10.0.99"], first_boot=True), kind=AdapterError)
            check("DigitalOcean (F3, re-check): another host on the Droplet's own VPC, named by the guest: refused (only "
                  "DigitalOcean's resolver for that VPC, its resolver_ip, is let through)", why and "resolver" in why
                  and remote_mod.read_state(st0["RUN_DIR"]).get("resolvers") is None, why)
            why = raises(remote_mod.cmd_resolvers, st0, R(set=["10.99.0.1"], first_boot=True), kind=AdapterError)
            check("DigitalOcean (F3): an address outside the Droplet's own VPC: refused", why and "VPC" in why, why)
            why = raises(remote_mod.cmd_resolvers, st0, R(set=["not-an-address"], first_boot=True), kind=AdapterError)
            check("DigitalOcean (F3): something that isn't an address: refused", why is not None, why)
            rc = remote_mod.cmd_resolvers(st0, R(set=["10.10.15.254", "203.0.113.53"], first_boot=True))
            j = remote_mod.read_state(st0["RUN_DIR"])
            fw = h.fws[j["nsg"]]
            dns = [x for x in fw["outbound_rules"] if x.get("ports") == "53"]
            check("DigitalOcean (F3): DNS let through to exactly the private resolver the Droplet names (/32, port 53, TCP "
                  "and UDP), recorded; its public one needs nothing; nothing else on its private network",
                  rc == 0 and j.get("resolvers") == ["10.10.15.254/32"] and sorted(x["protocol"] for x in dns) == ["tcp", "udp"]
                  and all(x["destinations"]["addresses"] == ["10.10.15.254/32"] for x in dns), (j.get("resolvers"), dns))
            why = raises(remote_mod.cmd_resolvers, st0, R(set=["10.10.15.254"], first_boot=True), kind=remote_mod.Refused)
            check("DigitalOcean (F3, re-check): pinned at first boot, the resolver is never read from the computer "
                  "again (a second --first-boot is refused)", why and "pinned" in why, why)
            s_ = cloud.ADAPTERS["digitalocean"](api=api, computers={"box": {"VM_NAME": "box", "PROVIDER": "digitalocean"}},
                                                states={"box": remote_mod.read_state(st0["RUN_DIR"])})
            s_.apply_egress("box", "off")
            s_.apply_egress("box", "internet")
            dns = [x for x in h.fws[j["nsg"]]["outbound_rules"] if x.get("ports") == "53"]
            check("DigitalOcean (F3): net egress off then internet keeps exactly the recorded resolver",
                  s_.egress("box")["outside"] == "internet" and all(x["destinations"]["addresses"] == ["10.10.15.254/32"]
                                                                    for x in dns) and len(dns) == 2, dns)
            rules = api.nsg_rules(j["nsg"])
            wide = [dict(x, addresses=("10.0.0.0/8",)) if x["ports"] == "53" else x for x in rules]
            other = [dict(x, addresses=("10.10.0.9/32",)) if x["ports"] == "53" else x for x in rules]
            check("DigitalOcean (F3): read back, DNS to a wider private range, or to another address than the recorded "
                  "resolver, is not what metacomputer set", api.egress_of(wide, resolvers=["10.10.15.254/32"]) == "other"
                  and api.egress_of(other, resolvers=["10.10.15.254/32"]) == "other" and api.egress_of(wide) == "other"
                  and api.egress_of(rules, resolvers=["10.10.15.254/32"]) == "internet" and dom.resolver_host("10.0.0.0/8") is None)
            plan = subprocess.run([PY, os.path.join(LAB, "lib", "remote.py"), "missing", "--provider", "digitalocean",
                                   "--feature", "reset"], capture_output=True, text=True)
            rb = open(os.path.join(LAB, "scripts", "remote-build.sh")).read()
            at = rb.index('if [ "$(plan resolvers)" = yes ]; then')
            blk = rb[at:rb.index('ARCH_SEEN="$(gssh admin', at)]
            check("the build reads the resolver from a new computer once, at its first boot (before the setup's downloads),"
                  " and a computer set up again uses the pinned one, never read from it again",
                  "/run/systemd/resolve/resolv.conf" in blk and 'remote_py resolvers --vm "$VM_NAME" --first-boot --set $res' in blk
                  and 'remote_py resolvers --vm "$VM_NAME" --pinned' in blk
                  and blk.index('if [ "$STATE_HAS_INSTANCE" = 0 ]; then') < blk.index("resolv.conf") < blk.index("else")
                  and rb.index("remote_py resolvers") < rb.index('"$(dirname "$0")/fetch-artifacts.sh" remote'), plan.stderr)
        parts.append(part_f3)

        def part_recheck_delete():
            # Re-check P1: vm delete of a computer whose launch is unresolved never forgets it.
            import contextlib as _cl
            import io
            udir = os.path.join(rd, "unresolved")
            os.makedirs(udir, exist_ok=True)
            real_dir = getattr(remote_mod, "unresolved_dir", None)
            remote_mod.unresolved_dir = lambda: udir
            try:
                for prov in ("hetzner", "digitalocean", "vultr"):
                    label = cloud.ADAPTERS[prov].manifest["label"]
                    h, api, st0 = box(prov)
                    h.lose_answer.add(("POST", create_path[prov]))
                    real_list = api.instances
                    api.instances = lambda tag_value=None, op=None: []
                    op = "d0" * 16
                    raises(remote_mod.cmd_create, st0, args(op=op), kind=Exception)
                    remote_mod.cmd_abort(st0, args(op=op))
                    why = raises(remote_mod.cmd_delete, st0, args(network_too=True, checked_provider=False),
                                 kind=remote_mod.Refused)
                    j = remote_mod.read_state(st0["RUN_DIR"])
                    check(f"{label} (re-check P1): vm delete with the launch unresolved: refused, the journal kept, the "
                          "server left, and it says exactly where to look and how to go on once you've looked",
                          why and "--checked-provider" in why and "curl" in why and op in why and j.get("launching") == op
                          and len(h.servers) == 1, (why, j))
                    rc = remote_mod.cmd_delete(st0, args(network_too=True, checked_provider=True))
                    left = [e for e in remote_mod.unresolved_launches() if e.get("op") == op]
                    out = io.StringIO()
                    with _cl.redirect_stdout(out):
                        remote_mod.cmd_cloud(type("A", (), {"provider": prov, "action": "status", "json": True,
                                                            "forget": None})())
                    shown = json.loads(out.getvalue()).get("unresolved_launches") or []
                    check(f"{label} (re-check P1): ...with --checked-provider: the rest is deleted, and the operation stays "
                          "recorded where cloud status shows it, with the console and API lookup",
                          rc == 0 and len(left) == 1 and left[0]["computer"] == "box" and "curl" in left[0]["look"]
                          and [e.get("op") for e in shown] == [op] and len(h.servers) == 1, (left, shown))
                    check(f"{label} (re-check P1): ...and the firewall and key a server that may exist would be using are "
                          "kept for it", len(h.fws) == 1 and len(h.keys) == 1, (list(h.fws), list(h.keys)))
                    api.instances = real_list                     # the listing catches up: teardown settles it
                    out = io.StringIO()
                    with _cl.redirect_stdout(out):
                        remote_mod.cmd_cloud(type("A", (), {"provider": prov, "action": "teardown", "json": True,
                                                            "forget": None})())
                    res = json.loads(out.getvalue())
                    check(f"{label} (re-check P1): ...and cloud teardown deletes what shows under its mark, then drops it",
                          not h.servers and not h.fws and not h.keys and res.get("unresolved_settled") == [op]
                          and not [e for e in remote_mod.unresolved_launches() if e.get("op") == op], res)
            finally:
                if real_dir is not None:
                    remote_mod.unresolved_dir = real_dir
        parts.append(part_recheck_delete)

        def part_recheck_direct_id():
            # Re-check P2: an instance read by its journaled id must carry this operation's mark too.
            for prov in ("hetzner", "digitalocean", "vultr"):
                label = cloud.ADAPTERS[prov].manifest["label"]
                h, api, st0 = box(prov)
                op, other = "e1" * 16, "e2" * 16
                remote_mod.cmd_create(st0, args(op=op))
                srv = next(iter(h.servers.values()))
                if prov == "hetzner":
                    srv["labels"]["metacomputer-op"] = other
                elif prov == "digitalocean":
                    srv["name"] = f"mc-{other}-box"
                else:
                    srv["label"] = f"mc-{other}-box"
                h.calls.clear()
                for keep in (True, False):
                    why = raises(remote_mod.cmd_abort, st0, args(op=op, keep=keep), kind=Exception)
                    acts = [c for c in h.calls if c[0] in ("POST", "DELETE")]
                    j = remote_mod.read_state(st0["RUN_DIR"])
                    check(f"{label} (re-check P2): the id this operation journaled now names an instance marked for another "
                          f"operation: {'--keep' if keep else 'the clean-up'} refuses, touches nothing, keeps the record",
                          why and "another" not in "" and other[:12] in why and not acts and len(h.servers) == 1
                          and j.get("instance"), (why, acts))
        parts.append(part_recheck_direct_id)

        def part_recheck_resolver():
            # Re-check F3: with nothing pinned, a computer set up again gets DigitalOcean's documented resolver for its
            # VPC (the second-to-last address, when the API's resolver_ip isn't there), never one read from it.
            h, api, st0 = box("digitalocean")
            h.vpc_resolver_field = False
            remote_mod.cmd_create(st0, args(op="e3" * 16))
            got = io_out = None
            import contextlib as _cl
            import io
            io_out = io.StringIO()
            with _cl.redirect_stdout(io_out):
                remote_mod.cmd_resolvers(st0, type("A", (), {"set": [], "first_boot": False, "pinned": True})())
            got = remote_mod.read_state(st0["RUN_DIR"]).get("resolvers")
            check("DigitalOcean (re-check F3): --pinned with nothing pinned: DigitalOcean's documented resolver for the "
                  "Droplet's VPC (its second-to-last address), nothing read from the computer", got == ["10.10.15.254/32"]
                  and "10.10.15.254/32" in io_out.getvalue(), (got, io_out.getvalue()))
        parts.append(part_recheck_resolver)

        def part_recheck_action_scrub():
            # Re-check F6: Hetzner's asynchronous action errors (inside a 200 answer) are scrubbed too.
            h = StandInREST("hetzner")
            h.fw_action_error = True
            h.action_message = f"denied for Bearer {SYNTH_TOKEN} " + "dop_v1_" + "0123456789abcdef" * 4
            why = raises(cloud.ADAPTERS["hetzner"](api=vps_api("hetzner", h)).create, "box", user_data="x",
                         public_key=KEY, kind=AdapterError)
            check("Hetzner Cloud (re-check F6): a failed action's error, which arrives inside a 200 answer, is printed "
                  "without the token or anything credential-shaped", why and SYNTH_TOKEN not in why
                  and "0123456789abcdef0123456789abcdef" not in why and "[redacted]" in why, why)
        parts.append(part_recheck_action_scrub)

        # --- the second re-check (2026-11-23, codex-recheck2.md) ----------------------------------------------------
        def retag(prov, srv, op, name="box"):
            # What the provider's record of a server says now: its computer and its operation.
            if prov == "hetzner":
                srv["labels"]["metacomputer"], srv["labels"]["metacomputer-op"] = name, op
            elif prov == "digitalocean":
                srv["name"] = f"mc-{op}-{name}"
            else:
                srv["label"] = f"mc-{op}-{name}"

        def stale_listing(api, rows):
            # A listing that lags: it shows the rows it showed before, whatever the servers say now.
            rows = [dict(r) for r in rows]
            api.instances = lambda tag_value=None, op=None: [dict(r) for r in rows if (tag_value is None or r["name"] ==
                                                             tag_value) and (op is None or r["op"] == op)]

        def acted(h):
            return [c for c in h.calls if c[0] in ("POST", "DELETE", "PUT")]

        def teardown(prov, forget=None):
            import contextlib as _cl
            import io
            out = io.StringIO()
            with _cl.redirect_stdout(out):
                remote_mod.cmd_cloud(type("A", (), {"provider": prov, "action": "teardown", "json": True,
                                                    "forget": forget})())
            return json.loads(out.getvalue())

        def part_recheck2_registry():
            # Re-check 2, P1: the registry of unresolved launches is changed under one lock, so two changes at once
            # (two vm deletes --checked-provider; a teardown and a vm delete) never lose each other's entries.
            udir = os.path.join(rd, "registry")
            os.makedirs(udir, exist_ok=True)
            real_dir, real_read = remote_mod.unresolved_dir, remote_mod.read_state
            remote_mod.unresolved_dir = lambda: udir

            def slow_read(d, name=remote_mod.STATE_FILE):
                out = real_read(d, name)
                if name == remote_mod.UNRESOLVED_FILE:
                    time.sleep(0.3)         # the other change has every chance to come between the read and the write
                return out
            try:
                a = cloud.ADAPTERS["hetzner"](api=vps_api("hetzner", StandInREST("hetzner")))
                ops = ["f1" * 16, "f2" * 16]
                remote_mod.read_state = slow_read
                ts = [threading.Thread(target=remote_mod.remember_unresolved, args=({"VM_NAME": f"box{i}"}, a, op))
                      for i, op in enumerate(ops)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                remote_mod.read_state = real_read
                got = sorted(e["op"] for e in remote_mod.unresolved_launches())
                check("(re-check 2, P1) two vm deletes --checked-provider at once, for two computers: both unresolved "
                      "launches stay recorded (one lock for the registry)", got == sorted(ops), got)
                # A teardown settling one launch while a vm delete records another: the one recorded meanwhile stays.
                h, api, st0 = box("hetzner")
                op3, op4 = "f3" * 16, "f4" * 16
                a = cloud.ADAPTERS["hetzner"](api=api)
                a.create("box", user_data="x", public_key=KEY, op=op3)
                remote_mod.update_unresolved(lambda e: []) if hasattr(remote_mod, "update_unresolved") else \
                    remote_mod.write_unresolved([])
                remote_mod.remember_unresolved(st0, a, op3)
                real_term = api.terminate

                def term(iid):
                    remote_mod.remember_unresolved({"VM_NAME": "box4"}, a, op4)     # another vm delete, meanwhile
                    return real_term(iid)
                api.terminate = term
                res = teardown("hetzner")
                got = [e["op"] for e in remote_mod.unresolved_launches()]
                check("(re-check 2, P1) a teardown that settles one unresolved launch while another is recorded: the one "
                      "recorded meanwhile stays, the settled one goes", op4 in got and op3 not in got
                      and res.get("unresolved_settled") == [op3] and not h.servers, (got, res))
            finally:
                remote_mod.unresolved_dir, remote_mod.read_state = real_dir, real_read
        parts.append(part_recheck2_registry)

        def part_recheck2_owned():
            # Re-check 2, P2: whatever deletes, stops, starts or forgets a server reads it by its own id first and acts
            # only if it carries the computer's name and the recorded operation (lib/remote.py owned); a listing's row
            # is only a candidate. Anything else refuses, acts on nothing, and keeps the record.
            udir = os.path.join(rd, "registry-owned")
            os.makedirs(udir, exist_ok=True)
            real_dir, real_up = remote_mod.unresolved_dir, remote_mod.guest_up
            remote_mod.unresolved_dir = lambda: udir
            remote_mod.guest_up = lambda *a, **k: True
            try:
                for prov in ("hetzner", "digitalocean", "vultr"):
                    label = cloud.ADAPTERS[prov].manifest["label"]
                    # The recorded server, marked now for another operation: vm delete, stop and start refuse.
                    h, api, st0 = box(prov)
                    op, other = "a1" * 16, "a2" * 16
                    remote_mod.cmd_create(st0, args(op=op))
                    srv = next(iter(h.servers.values()))
                    retag(prov, srv, other)
                    h.calls.clear()
                    whys = [raises(remote_mod.cmd_delete, st0, args(network_too=True, checked_provider=False),
                                   kind=Exception),
                            raises(remote_mod.cmd_sleep, st0, type("A", (), {"force": True, "asked": False})(),
                                   kind=Exception),
                            raises(remote_mod.cmd_wake, st0, args(), kind=Exception)]
                    j = remote_mod.read_state(st0["RUN_DIR"])
                    check(f"{label} (re-check 2, P2): the recorded server, marked now for another operation: vm delete, "
                          "stop --force and start each refuse, nothing is sent, and the record stays",
                          all(w and other[:12] in w for w in whys) and not acted(h) and len(h.servers) == 1
                          and j.get("instance"), (whys, acted(h)))
                    # A lost launch, and a listing that still shows its mark on a server now marked for another.
                    h, api, st0 = box(prov)
                    op, other = "b1" * 16, "b2" * 16
                    h.lose_answer.add(("POST", create_path[prov]))
                    raises(remote_mod.cmd_create, st0, args(op=op), kind=Exception)
                    srv = next(iter(h.servers.values()))
                    stale = api.instances("box", op=op)
                    retag(prov, srv, other)
                    stale_listing(api, stale)
                    h.calls.clear()
                    whys = [raises(remote_mod.cmd_abort, st0, args(op=op, keep=k), kind=Exception) for k in (False, True)]
                    j = remote_mod.read_state(st0["RUN_DIR"])
                    check(f"{label} (re-check 2, P2): a stale listing shows this operation's mark on a server now marked for "
                          "another: the clean-up and --keep refuse, touch nothing, keep the journal",
                          all(w and other[:12] in w for w in whys) and not acted(h) and len(h.servers) == 1
                          and j.get("launching") == op, (whys, acted(h)))
                    # An unresolved launch in the registry, and a server under its mark whose computer is now another.
                    h, api, st0 = box(prov)
                    op = "c1" * 16
                    h.lose_answer.add(("POST", create_path[prov]))
                    raises(remote_mod.cmd_create, st0, args(op=op), kind=Exception)
                    srv = next(iter(h.servers.values()))
                    stale = api.instances("box", op=op)
                    remote_mod.remember_unresolved(st0, cloud.ADAPTERS[prov](api=api), op)
                    retag(prov, srv, op, name="other")
                    stale_listing(api, stale)
                    h.calls.clear()
                    why = raises(teardown, prov, kind=Exception)
                    kept = [e["op"] for e in remote_mod.unresolved_launches()]
                    check(f"{label} (re-check 2, P2): cloud teardown, a server under the launch's mark whose computer is now "
                          "another: refused, nothing deleted, the launch still recorded",
                          why and "'other'" in why and not acted(h) and len(h.servers) == 1 and op in kept, (why, acted(h)))
                    remote_mod.update_unresolved(lambda e: []) if hasattr(remote_mod, "update_unresolved") else \
                        remote_mod.write_unresolved([])
                    # A server named for the computer that its record didn't make (another operation's): vm delete refuses.
                    h, api, st0 = box(prov)
                    op, other = "d1" * 16, "d2" * 16
                    remote_mod.cmd_create(st0, args(op=op))
                    srv = next(iter(h.servers.values()))
                    twin = json.loads(json.dumps(srv))
                    twin["id"] = h._id()
                    retag(prov, twin, other)
                    h.servers[twin["id"]] = twin
                    h.calls.clear()
                    why = raises(remote_mod.cmd_delete, st0, args(network_too=True, checked_provider=False), kind=Exception)
                    j = remote_mod.read_state(st0["RUN_DIR"])
                    check(f"{label} (re-check 2, P2): vm delete with a server named for the computer that its record didn't "
                          "make (another operation's): refused, nothing deleted (its own server neither), the record kept",
                          why and other[:12] in why and not acted(h) and len(h.servers) == 2 and j.get("instance"),
                          (why, acted(h)))
            finally:
                remote_mod.unresolved_dir, remote_mod.guest_up = real_dir, real_up
        parts.append(part_recheck2_owned)

        def part_recheck2_plan():
            # Re-check 2, P2: a plan that can't be read is never taken for "no" (no outside layer, an api soft stop).
            import re as _re
            src = open(os.path.join(LAB, "bin", "metacomputer")).read()

            def fn(name):
                m = _re.search(rf"^{name}\(\) \{{.*?^\}}$", src, _re.S | _re.M)
                return m.group(0) if m else ""
            stubs = r'''
set -u
die() { echo "DIE: $*" >&2; exit 1; }
ok() { echo "OK: $*"; }; warn() { echo "WARN: $*" >&2; }; say() { echo "SAY: $*"; }
owners_check() { :; }; handoff_hold_unless_override() { :; }; expect_guard() { :; }; hub_stop() { :; }
emit_event() { :; }; vm_running() { return 0; }; remote_made() { return 0; }
remote_can_ask() { [ "$CREDS" = 1 ]; }
remote_where() { echo "Hetzner Cloud"; }; remote_cred_names() { echo HCLOUD_TOKEN; }
remote_py() { echo "remote_py $*" >> "$LOG"; if [ "$1" = plan ]; then echo "metacomputer: no size big" >&2; return 1; fi; return 0; }
gssh() { echo "gssh $*" >> "$LOG"; echo off; }
remote_egress_guest() { echo off; }
browser_set() { echo "browser_set $*" >> "$LOG"; }
view_tunnel_close() { :; }; vault_disconnect() { :; }; sprite_tunnels_left() { :; }; do_record() { :; }
PROVIDER=hetzner VM_NAME=box RUN_DIR=/nonexistent-run MC_SETTINGS_LABEL=x EGRESS=internet IDLE_STOP=15
'''
            body = "\n".join(fn(n) for n in ("parse_flags", "remote_plan_load", "remote_plan", "remote_billing_words",
                                              "remote_net_egress", "do_stop_remote"))
            res = {}
            for what, creds, call in (("egress", "0", "remote_net_egress off"), ("stop", "1", "do_stop_remote")):
                log = os.path.join(rd, f"plan-{what}.log")
                open(log, "w").close()
                r = subprocess.run(["bash", "-c", stubs + body + "\n" + call], capture_output=True, text=True,
                                   env=dict(os.environ, LOG=log, CREDS=creds))
                res[what] = (r.returncode, open(log).read(), r.stderr)
            rc, log, err = res["egress"]
            check("(re-check 2, P2) net egress off when the provider's plan can't be read: refused, saying why, and nothing "
                  "is changed (not the guest's firewall, not the setting)", rc != 0 and "gssh" not in log
                  and "browser_set" not in log and "nothing was changed" in err, res["egress"])
            rc, log, err = res["stop"]
            check("(re-check 2, P2) stop when the plan can't be read: refused before anything is sent (never taken for a "
                  "provider that asks the guest itself)", rc != 0 and "remote_py sleep" not in log and "gssh" not in log
                  and "nothing was stopped" in err, res["stop"])
            rb = open(os.path.join(LAB, "scripts", "remote-build.sh")).read()
            got = {}
            if "plan_word() {" in rb:
                blk = rb[rb.index("plan() {"):rb.index("\n", rb.index('[ -n "$(plan provider)" ]'))]
                whole = ("kind=made\narch=arm64\nlabel=x\nidle_stop=15\negress=off\noutside=yes\nbilling_stopped=full\n"
                         "soft_stop=api\nprovider=hetzner\nresolvers=no")
                for k, p in (("whole", whole), ("no outside", whole.replace("outside=yes\n", "")),
                             ("unknown word", whole.replace("outside=yes", "outside=maybe"))):
                    r = subprocess.run(["bash", "-c", 'die() { echo "DIE: $*" >&2; exit 1; }\nVM_NAME=box\nPLAN="$1"\n'
                                        + blk + "\necho GOES-ON", "-", p], capture_output=True, text=True)
                    got[k] = "GOES-ON" in r.stdout
            check("(re-check 2, P2) the build: a plan without a word it decides on (outside), or with one it doesn't know, "
                  "stops it before anything is made; a whole plan goes on",
                  got == {"whole": True, "no outside": False, "unknown word": False}, got)
        parts.append(part_recheck2_plan)

        def part_recheck2_timeout_scrub():
            # Re-check 2, P2: every exception leaving a provider's facade or adapter is scrubbed in one place: an action
            # that never ends, whose timeout says which action in the provider's own words, too.
            h = StandInREST("hetzner")
            h.fw_action_stuck = f"apply_firewall Bearer {SYNTH_TOKEN} " + "hcloud_" + "abcdef0123456789" * 4
            why = raises(cloud.ADAPTERS["hetzner"](api=vps_api("hetzner", h)).create, "box", user_data="x",
                         public_key=KEY, op="7a" * 16, kind=AdapterError)
            check("Hetzner Cloud (re-check 2, P2): an action that never ends: its timeout is printed without the token or "
                  "anything credential-shaped", why and "didn't happen" in why and SYNTH_TOKEN not in why
                  and "abcdef0123456789abcdef0123456789" not in why and "[redacted]" in why, why)
        parts.append(part_recheck2_timeout_scrub)

        def part_recheck2_user_only():
            # Re-check 2, P2: cloud teardown (it deletes servers), --forget and vm delete --checked-provider (each says
            # the person looked at the provider) run only at the user's own terminal.
            env = {k: v for k, v in os.environ.items() if k not in ("MCOMP_CONFIG", "HCLOUD_TOKEN", "CLAUDECODE")}
            env["MCOMP_SESSIONS_DIR"] = os.path.join(rd, "sessions")
            out = {}
            for k, argv in (("teardown", ["cloud", "teardown", "hetzner"]),
                            ("forget", ["cloud", "status", "hetzner", "--forget", "0" * 32]),
                            ("checked", ["vm", "delete", "box", "--checked-provider"])):
                r = subprocess.run([os.path.join(LAB, "bin", "metacomputer"), *argv], capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, env=env, timeout=120)
                out[k] = (r.returncode, r.stderr[-400:])
            check("(re-check 2, P2) cloud teardown, --forget and vm delete --checked-provider, run without the user's own "
                  "terminal: refused, saying to run it there, before anything asks the provider",
                  all(rc != 0 and "your own terminal" in e and "API token" not in e for rc, e in out.values()), out)
        parts.append(part_recheck2_user_only)

        def part_recheck2_unpinned_resolver():
            # Re-check 2, P3: with nothing pinned, DigitalOcean's resolver_ip must lie in the Droplet's own VPC too.
            h, api, st0 = box("digitalocean")
            h.vpc_resolver_ip = "10.99.0.53"
            remote_mod.cmd_create(st0, args(op="e4" * 16))
            h.calls.clear()
            why = raises(remote_mod.cmd_resolvers, st0, type("A", (), {"set": [], "first_boot": False,
                                                                       "pinned": True})(), kind=Exception)
            j = remote_mod.read_state(st0["RUN_DIR"])
            check("DigitalOcean (re-check 2, P3): with nothing pinned, a resolver_ip outside the Droplet's own VPC (an "
                  "inconsistent answer) makes no DNS exception: refused, the firewall unchanged, nothing pinned",
                  why and "10.99.0.53" in why and not [c for c in h.calls if c[0] == "PUT"] and j.get("resolvers") is None,
                  (why, acted(h)))
        parts.append(part_recheck2_unpinned_resolver)

        # --- the third re-check (2026-11-23, codex-recheck3.md) -----------------------------------------------------
        SECRET = "synthetic-secret-" + "0a1b2c3d" * 4
        # A multi-line credential shaped like an API key's PEM (synthetic: its lines are made up, its armour says so)
        PEM = ("-----BEGIN SYNTHETIC TEST CREDENTIAL-----\n" + "c3ludGhldGljLWxpbmUtb25lLWZvci1hLXRlc3Qtb25seQ" + "x" * 18
               + "\n" + "c3ludGhldGljLWxpbmUtdHdvLWZvci1hLXRlc3Qtb25seQ" + "y" * 18
               + "\n-----END SYNTHETIC TEST CREDENTIAL-----\n")
        PEM_LINE = PEM.splitlines()[2]

        def part_recheck3_creds():
            # Re-check 3, P2: whatever lib/remote.py prints passes no_creds: the top-level handler's OCI ServiceError,
            # the clean-up's warnings; and what Fly's sprite command says is scrubbed where it becomes a message.
            import contextlib as _cl
            import io
            saved = dict(remote_mod.CREDS)
            real_cloud, real_api = remote_mod.cmd_cloud, remote_mod.API_FACTORY
            try:
                remote_mod.CREDS.clear()

                class ServiceError(Exception):
                    status, code = 401, "NotAuthenticated"
                    message = f"the key {PEM_LINE} and {SECRET} weren't accepted"

                def refuse(args):
                    raise ServiceError("refused")
                remote_mod.cmd_cloud = refuse
                err = io.StringIO()
                env_before = {k: os.environ.get(k) for k in ("OCI_API_KEY_PEM", "HCLOUD_TOKEN")}
                os.environ["OCI_API_KEY_PEM"], os.environ["HCLOUD_TOKEN"] = PEM, SECRET
                with _cl.redirect_stderr(err):
                    rc = remote_mod.main(["cloud", "status", "oci"])
                for k, v in env_before.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                said = err.getvalue()
                check("(re-check 3, P2) the top-level handler's OCI ServiceError: printed without a credential this "
                      "program was given (a line of the API key's PEM, a token)", rc == 1 and "refused" in said
                      and PEM_LINE not in said and SECRET not in said and "[redacted]" in said, said)
                remote_mod.cmd_cloud = real_cloud
                # The clean-up's warnings: what it couldn't delete, and the network vm delete couldn't.

                class Boom:
                    def __getattr__(self, n):
                        def fail(*a, **k):
                            raise RuntimeError(f"the provider said {SECRET}")
                        return fail
                a = type("A", (), {"name": "oci", "manifest": {"label": "Oracle Cloud"}, "api": Boom()})()
                err = io.StringIO()
                with _cl.redirect_stderr(err):
                    remote_mod.delete_op_parts({"VM_NAME": "box"}, {"made_nsg": "nsg-1"}, a, "c3" * 16, [])
                warned = err.getvalue()
                api = FakeOCI()
                d = os.path.join(rd, "oci-creds")
                os.makedirs(d, exist_ok=True)
                st0 = {"VM_NAME": "box", "PLACE": "oci", "PROVIDER": "oci", "SIZE": "arm-1x6", "EGRESS": "internet",
                       "RUN_DIR": d, "SSH_DIR": d}
                remote_mod.API_FACTORY = lambda: api
                remote_mod.cmd_create(st0, args(op="c4" * 16))

                def no_network():
                    raise RuntimeError(f"denied: {SECRET}")
                api.delete_network = no_network
                err = io.StringIO()
                with _cl.redirect_stderr(err):
                    remote_mod.cmd_delete(st0, args(network_too=True, checked_provider=False))
                warned += err.getvalue()
                check("(re-check 3, P2) the clean-up's warnings (what a build made, and the network vm delete couldn't "
                      "delete) never show a credential this program was given", "wasn't all deleted" in warned
                      and "network wasn't" in warned and SECRET not in warned and warned.count("[redacted]") >= 2, warned)
                spritecli = cloud.registry.module("sprites", "cli")
                cli = spritecli.SpriteCLI("/usr/bin/false")
                fly = "FlyV1 fm2_" + "lJPECAAAAAAAAbcdefghij0123456789ABCDEF" * 3
                cli.run = lambda *a, **k: (1, b"", f"Error: unauthorized for token {fly}\n".encode())
                why = raises(cli.api, "org", "/v1/sprites", kind=AdapterError)
                check("(re-check 3, P2) Fly's sprite command's error, which becomes the message: anything credential-"
                      "shaped in it (its own sign-in, which this program never sees) is replaced",
                      why and "unauthorized" in why and "lJPECAAAAAAAAbcdefghij0123456789" not in why
                      and "[redacted]" in why, why)
            finally:
                remote_mod.CREDS.clear()
                remote_mod.CREDS.update(saved)
                remote_mod.cmd_cloud, remote_mod.API_FACTORY = real_cloud, real_api
        parts.append(part_recheck3_creds)

        def part_recheck3_egress():
            # Re-check 3, P2: net egress (and DigitalOcean's resolver exception) change a firewall only when the
            # recorded instance is still the computer's (owned) and the firewall is the one recorded for it, marked
            # for this computer and operation.
            for prov in ("hetzner", "digitalocean"):
                label = cloud.ADAPTERS[prov].manifest["label"]
                for what in ("instance", "firewall"):
                    h, api, st0 = box(prov)
                    op, other = "e5" * 16, "e6" * 16
                    remote_mod.cmd_create(st0, args(op=op))
                    if what == "instance":
                        retag(prov, next(iter(h.servers.values())), other)
                    else:
                        fw = next(iter(h.fws.values()))
                        if prov == "hetzner":
                            fw["labels"]["metacomputer-op"] = other
                        else:
                            fw["name"] = f"metacomputer-box.{other[:12]}"
                    h.calls.clear()
                    whys = [raises(remote_mod.cmd_egress, st0, type("A", (), {"set": "off", "setup": False})(),
                                   kind=Exception)]
                    if prov == "digitalocean":
                        whys.append(raises(remote_mod.cmd_resolvers, st0, type("A", (), {"set": [], "first_boot": False,
                                                                                         "pinned": True})(),
                                           kind=Exception))
                    check(f"{label} (re-check 3, P2): the {what} marked now for another operation: net egress off"
                          f"{' and the resolver exception' if prov == 'digitalocean' else ''} refuse, and no rule is "
                          "changed", all(w and other[:12] in w for w in whys) and not acted(h), (whys, acted(h)))
        parts.append(part_recheck3_egress)

        def part_recheck3_stop_first():
            # Re-check 3, P2: stop checks the plan (and the credentials) before it touches anything here: the hub and
            # a recording are left as they were when it refuses.
            import re as _re
            src = open(os.path.join(LAB, "bin", "metacomputer")).read()

            def fn(name):
                m = _re.search(rf"^{name}\(\) \{{.*?^\}}$", src, _re.S | _re.M)
                return m.group(0) if m else ""
            run_dir = os.path.join(rd, "stop-first")
            os.makedirs(run_dir, exist_ok=True)
            open(os.path.join(run_dir, "manual-recording"), "w").write("r1\n")
            log = os.path.join(rd, "stop-first.log")
            open(log, "w").close()
            stubs = r'''
set -u
die() { echo "DIE: $*" >&2; exit 1; }
ok() { echo "OK: $*"; }; warn() { echo "WARN: $*" >&2; }; say() { echo "SAY: $*"; }
owners_check() { :; }; handoff_hold_unless_override() { :; }; expect_guard() { :; }
hub_stop() { echo "hub_stop" >> "$LOG"; }
do_record() { echo "do_record $*" >> "$LOG"; }
emit_event() { :; }; vm_running() { return 0; }; remote_made() { return 0; }; remote_can_ask() { return 0; }
remote_where() { echo "Hetzner Cloud"; }; remote_cred_names() { echo HCLOUD_TOKEN; }
remote_py() { echo "remote_py $*" >> "$LOG"; if [ "$1" = plan ]; then echo "metacomputer: no size big" >&2; return 1; fi; return 0; }
gssh() { echo "gssh $*" >> "$LOG"; }
view_tunnel_close() { :; }; vault_disconnect() { :; }; sprite_tunnels_left() { :; }
PROVIDER=hetzner VM_NAME=box MC_SETTINGS_LABEL=x EGRESS=internet IDLE_STOP=15
'''
            body = "\n".join(fn(n) for n in ("parse_flags", "remote_plan_load", "remote_plan", "remote_billing_words",
                                              "do_stop_remote"))
            r = subprocess.run(["bash", "-c", stubs + body + "\ndo_stop_remote"], capture_output=True, text=True,
                               env=dict(os.environ, LOG=log, RUN_DIR=run_dir))
            done = open(log).read()
            check("(re-check 3, P2) stop when the plan can't be read: refused before the hub is stopped or a recording "
                  "finished, so \"nothing was stopped\" holds", r.returncode != 0 and "hub_stop" not in done
                  and "do_record" not in done and "remote_py sleep" not in done and "nothing was stopped" in r.stderr,
                  (r.returncode, done, r.stderr[-300:]))
        parts.append(part_recheck3_stop_first)

        for part in parts:
            if AUDIT_GUARD:
                AUDIT_GUARD(part.__name__, part)
            else:
                part()
    finally:
        os.environ.pop("MCOMP_LOCK_DIR", None)
        remote_mod.API_FACTORY = None
        remote_mod.user_data, remote_mod.mac_lock = old[0], old[1]
        if old[2] is not None:
            remote_mod.LOST_LAUNCH_GAP_S = old[2]
        shutil.rmtree(rd, ignore_errors=True)


def vps_journal():
    """lib/remote.py with an hourly provider: the journal before anything is made, the firewall and key recorded by id
    under the provider's lock, the failed build's clean-up deleting exactly those, and vm delete."""
    print("\nlib/remote.py with the hourly providers")
    import contextlib
    import remote as remote_mod
    rd = tempfile.mkdtemp(prefix="mcvps-")
    old_ud, old_lock = remote_mod.user_data, remote_mod.mac_lock
    try:
        os.environ["MCOMP_LOCK_DIR"] = os.path.join(rd, "locks")
        remote_mod.user_data = lambda s: "#cloud-config\n"
        for prov in ("hetzner", "digitalocean", "vultr"):
            label = cloud.ADAPTERS[prov].manifest["label"]
            d = os.path.join(rd, prov)
            os.makedirs(d)
            with open(os.path.join(d, "admin.pub"), "w") as f:
                f.write(KEY + " mcomp-box-admin\n")
            settings = {"VM_NAME": "box", "PLACE": prov, "PROVIDER": prov, "EGRESS": "internet", "RUN_DIR": d, "SSH_DIR": d}
            h = StandInREST(prov)
            locks = []

            @contextlib.contextmanager
            def spy(name):
                locks.append(name)
                h.locked = lambda: True
                try:
                    yield
                finally:
                    h.locked = lambda: False
            remote_mod.mac_lock = spy
            remote_mod.API_FACTORY = lambda: vps_api(prov, h)
            seen = []
            real = h.__class__.__call__

            def watch(method, url, headers, body, timeout, h=h):
                if method == "POST" and url.endswith(("/v1/servers", "/v2/droplets", "/v2/instances")):
                    seen.append(remote_mod.read_state(d).get("launching"))
                return real(h, method, url, headers, body, timeout)
            h_api = vps_api(prov, watch)
            remote_mod.API_FACTORY = lambda: h_api
            op = "a1" * 16
            remote_mod.cmd_create(settings, type("A", (), {"op": op})())
            st = remote_mod.read_state(d)
            check(f"{label}: the journal names the operation before the server is made", seen == [op], seen)
            check(f"{label}: the record: its server, firewall and key, each by the id this operation made it with, under "
                  f"the provider's own lock ({prov}-network)", st.get("instance") and st.get("made_nsg") == st.get("nsg")
                  and st.get("made_key") == st.get("key") and locks == [f"{prov}-network"] and st.get("region"), (st, locks))
            check(f"{label}: the admin key's public half is the one given for the first boot", any(
                k.get("public_key", k.get("ssh_key")) == KEY + " mcomp-box-admin" for k in h.keys.values()))
            # A failed build: abort --op deletes exactly what it made.
            remote_mod.cmd_abort(settings, type("A", (), {"op": "b2" * 16, "keep": False})())
            check(f"{label}: abort with another operation's id: nothing deleted", h.servers and h.fws and h.keys)
            remote_mod.cmd_abort(settings, type("A", (), {"op": op, "keep": True})())
            srv = next(iter(h.servers.values()))
            stopped = srv.get("status") == "off" or srv.get("power_status") == "stopped"
            check(f"{label}: --keep-on-failure: kept, recorded, powered off (the build failed), nothing deleted",
                  stopped and remote_mod.read_state(d).get("kept") and h.fws and h.keys)
            remote_mod.cmd_abort(settings, type("A", (), {"op": op, "keep": False})())
            check(f"{label}: abort of its own operation: its server, its firewall and its key deleted, by id",
                  not h.servers and not h.fws and not h.keys, (h.servers, h.fws, h.keys))
            # vm delete of a set-up computer (made again), with the provider's lists lagging.
            remote_mod.write_state(d, {})
            remote_mod.cmd_create(settings, type("A", (), {"op": "c3" * 16})())
            h.lag = True
            remote_mod.cmd_delete(settings, type("A", (), {"network_too": True})())
            h.lag = False
            check(f"{label}: vm delete: nothing of it left at the provider", not h.servers and not h.fws and not h.keys,
                  (h.servers, h.fws, h.keys))
            # A listing that still shows a server already deleted (DigitalOcean's lag the other way): asked by id, it's
            # gone, so vm delete neither refuses nor deletes anything for it.
            ghost = {"id": "0000270f-0000-4000-8000-00000000270f" if prov == "vultr" else "9999", "name": "box",
                     "state": "RUNNING", "shape": "x", "ad": "x", "created": "t", "op": "c3" * 16}
            real_list = h_api.instances
            h_api.instances = lambda tag_value=None, op=None: [ghost]
            h.calls.clear()
            try:
                rc = remote_mod.cmd_delete(settings, type("A", (), {"network_too": False})())
            finally:
                h_api.instances = real_list
            check(f"{label}: a stale listing of a deleted server: vm delete goes ahead, deletes nothing for it",
                  rc == 0 and not [c for c in h.calls if c[0] == "DELETE" and ghost["id"] in c[1]], h.calls[-5:])
            # A firewall left without its server (a build cut off): teardown refuses; then it's deleted.
            api = h_api
            api.make_nsg("pending", "internet", op="f" * 32)
            why = raises(remote_mod.cmd_cloud, type("A", (), {"provider": prov, "action": "teardown", "json": True})(),
                         kind=remote_mod.Refused)
            check(f"{label}: cloud teardown while a computer's firewall is there without its server: refused",
                  why and "being made" in why, why)
            api.delete_nsg("pending")
            api.make_key("old", "d4" * 16, KEY + " other")
            remote_mod.cmd_cloud(type("A", (), {"provider": prov, "action": "teardown", "json": True})())
            check(f"{label}: ...and once nothing is being made, it deletes first-boot keys left over", not h.keys, h.keys)
        # Vultr's stop through lib/remote.py: only after the CLI asked the guest, and only once its SSH has gone quiet.
        d = os.path.join(rd, "vultr-stop")
        os.makedirs(d)
        with open(os.path.join(d, "admin.pub"), "w") as f:
            f.write(KEY + "\n")
        settings = {"VM_NAME": "box", "PLACE": "vultr", "PROVIDER": "vultr", "EGRESS": "internet", "RUN_DIR": d, "SSH_DIR": d}
        h = StandInREST("vultr")
        h_api = vps_api("vultr", h)
        remote_mod.API_FACTORY = lambda: h_api
        remote_mod.mac_lock = old_lock
        remote_mod.cmd_create(settings, type("A", (), {"op": "d4" * 16})())
        srv = next(iter(h.servers.values()))
        clock = [0.0]
        real_time, real_up = remote_mod.time, remote_mod.guest_up
        remote_mod.time = type("T", (), {"monotonic": staticmethod(lambda: clock[0]), "time": staticmethod(real_time.time),
                                         "sleep": staticmethod(lambda x: clock.__setitem__(0, clock[0] + x))})
        answers = []
        remote_mod.guest_up = lambda s_, st_, tries=3, wait=5.0: answers.pop(0) if answers else False
        try:
            why = raises(remote_mod.cmd_sleep, settings, type("A", (), {"force": False, "asked": False})(),
                         kind=remote_mod.Refused)
            check("Vultr: lib/remote.py sleep without the CLI having asked the guest: refused, nothing sent",
                  why and "asks it over SSH" in why and srv["power_status"] == "running", why)
            h.calls.clear()
            answers[:] = [False] * 1000                 # its SSH goes quiet (stalled, cut off, or not obeying)
            why = raises(remote_mod.cmd_sleep, settings, type("A", (), {"force": False, "asked": True})(),
                         kind=AdapterError)
            check("Vultr (F2): asked, its SSH silent for minutes, Vultr's own state still running: never halted; "
                  "says it may still be on and billing (stop --force: the explicit choice)",
                  why and "may still be on" in why and "billed" in why and "stop --force" in why
                  and not [c for c in h.calls if c[0] == "POST"] and srv["power_status"] == "running", (why, h.calls[-3:]))
            h.calls.clear()
            remote_mod.cmd_sleep(settings, type("A", (), {"force": True, "asked": False})())
            check("Vultr: stop --force halts it (the user's call)", [c[1] for c in h.calls if c[0] == "POST"][-1:]
                  == [f"instances/{srv['id']}/halt"] and srv["power_status"] == "stopped", h.calls[-3:])
        finally:
            remote_mod.time, remote_mod.guest_up = real_time, real_up
        cli = open(os.path.join(LAB, "bin", "metacomputer")).read()
        stop = cli.split("do_stop_remote() {", 1)[1].split("\n}\n", 1)[0]
        egress = cli.split("remote_net_egress() {", 1)[1].split("\n}\n", 1)[0]
        egress = egress.split("\n  fi\n", 1)[1] if 'if [ "${PROVIDER:-}" = sprites ]; then' in egress else egress
        check("stop (CLI): with a provider that has no shutdown call, the guest must answer, is asked over SSH, then "
              "lib/remote.py sleep --asked; one that doesn't answer is refused, never halted",
              "gssh admin true >/dev/null 2>&1 || die" in stop and "metacomputer-admin poweroff" in stop
              and "asked=--asked" in stop and stop.index("gssh admin true") < stop.index("metacomputer-admin poweroff"))
        check("net egress (CLI): a provider with no outside egress layer (Vultr) changes only the guest's firewall",
              'if remote_made && [ "$outside" = no ]; then' in egress
              and egress.index("remote_plan_load") < egress.index("remote_plan outside") < egress.index("remote_py egress"))
        # The plan the CLI reads.
        out = subprocess.run([PY, os.path.join(LAB, "lib", "remote.py"), "missing", "--provider", "hetzner",
                              "--feature", "reset"], capture_output=True, text=True).stdout
        check("reset on an hourly provider's computer: refused, saying it needs snapshots", "snapshots" in out, out)
    finally:
        os.environ.pop("MCOMP_LOCK_DIR", None)
        remote_mod.API_FACTORY = None
        remote_mod.user_data, remote_mod.mac_lock = old_ud, old_lock
        shutil.rmtree(rd, ignore_errors=True)


def dhcp_stacks():
    """F6: the DHCP clients of both network stacks get out, in the full rules and in the fallback: systemd-networkd's
    (its own account) and NetworkManager's (root, DHCPv6 to ff02::1:2). Matched by ports and destinations, never by an
    account, and before the private-range refusals."""
    for stack, uids in (("systemd-networkd", "resolve=991 network=998 timesync=995 apt=42"),
                        ("NetworkManager (no systemd-network account, root DHCP)", "resolve=991 network= timesync=995 apt=42")):
        for what, args in (("full rules", ["render", "--egress", "off"]), ("fallback", ["render-emergency"])):
            out = subprocess.run(["/bin/sh", FIREWALL, *args, "--ports", "22", "--uids", uids], capture_output=True,
                                 text=True).stdout
            o = out.split("chain output")[1] if "chain output" in out else ""
            v4 = next((ln for ln in o.splitlines() if "udp sport 68 udp dport 67" in ln), "")
            v6 = next((ln for ln in o.splitlines() if "udp sport 546 udp dport 547" in ln), "")
            stop = o.find("@private") if "@private" in o else len(o)
            check(f"{stack}, {what}: DHCPv4 (68 -> 67: broadcast, private and link-local) and DHCPv6 (546 -> 547: "
                  "ff02::1:2, link-local) let out for any account, before any refusal (found by review: only "
                  "systemd-network's account was)",
                  v4 and v6 and "skuid" not in v4 + v6 and "255.255.255.255" in v4 and "169.254.0.0/16" in v4
                  and "ff02::1:2" in v6 and "fe80::/10" in v6 and o.find(v4) < stop and o.find(v6) < stop, o[:900])


def owner_ssh_options():
    """F2: every connection as your own account forwards nothing of this Mac's, whatever your ssh config says: ssh -G
    over a config that turns on the agent, X11, every kind of forwarding, a local command and a master."""
    td = tempfile.mkdtemp(prefix="mcopt-")
    try:
        cfg = os.path.join(td, "config")
        open(cfg, "w").write("Host *\n  ForwardAgent yes\n  ForwardX11 yes\n  ForwardX11Trusted yes\n"
                             "  LocalForward 9999 127.0.0.1:22\n  RemoteForward 9998 127.0.0.1:22\n  DynamicForward 9997\n"
                             "  PermitLocalCommand yes\n  LocalCommand touch /tmp/never\n  ControlMaster auto\n"
                             f"  ControlPath {td}/cm-%C\n  GSSAPIDelegateCredentials yes\n  Tunnel yes\n  RemoteCommand echo hi\n")
        lib = open(os.path.join(LAB, "scripts", "lib.sh")).read()
        fn = lib[lib.find("mc_owner_ssh_opts() {"):] if "mc_owner_ssh_opts() {" in lib else ""
        fn = fn[:fn.find("\n}\n") + 3] if fn else ""
        r = subprocess.run(["/bin/bash", "-c", fn + '\nmc_owner_ssh_opts 2222; /usr/bin/ssh -G -F "$1" "${MC_OWNER_OPTS[@]}" '
                            'owner@192.0.2.5', "_", cfg], capture_output=True, text=True)
        g = dict(ln.split(" ", 1) for ln in r.stdout.splitlines() if " " in ln)
        check("your own account's connections, over an ssh config that forwards everything: no agent, no X11, no "
              "forwardings, no local command, no shared connection, no delegated credentials (found by review: the "
              "agent was forwarded)",
              g.get("forwardagent") == "no" and g.get("forwardx11") == "no" and g.get("forwardx11trusted") == "no"
              and g.get("clearallforwardings") == "yes" and g.get("permitlocalcommand") == "no"
              and g.get("controlmaster") == "false" and "controlpath" not in g and "remotecommand" not in g
              and g.get("gssapidelegatecredentials") == "no" and g.get("tunnel") == "false"
              and not any(k in g for k in ("localforward", "remoteforward", "dynamicforward")) and g.get("port") == "2222",
              {k: g.get(k) for k in ("forwardagent", "forwardx11", "clearallforwardings", "permitlocalcommand",
                                     "controlmaster", "controlpath", "localforward", "tunnel")})
    finally:
        shutil.rmtree(td, ignore_errors=True)
    rb = open(os.path.join(LAB, "scripts", "remote-build.sh")).read()
    vc = open(os.path.join(LAB, "scripts", "vm-commands.sh")).read()
    check("...and those are the options of every such connection: the build's checks and bootstrap (OWN, ssh -tt) and "
          "the uninstall", 'OWN=(ssh -o BatchMode=yes "${MC_OWNER_OPTS[@]}"' in rb and 'ssh -tt "${MC_OWNER_OPTS[@]}"' in rb
          and 'local own=(ssh -o BatchMode=yes "${MC_OWNER_OPTS[@]}"' in vc
          and rb.count('mc_owner_ssh_opts "${SSH_HOST_PORT:-22}"') == 1 and vc.count('mc_owner_ssh_opts "${SSH_HOST_PORT:-22}"') == 1)


def journal_r4():
    """F3, F4, F7: a kept instance is recorded before it's reported (a retry never deletes it); what an operation
    made is recorded under the network lock when it's made; the journal is on the disk before anything is launched."""
    print("\nthe launch journal")
    import contextlib
    import remote as remote_mod
    rd = tempfile.mkdtemp(prefix="mcjr-")
    old_ud, old_lock, old_durable = remote_mod.user_data, remote_mod.mac_lock, getattr(remote_mod, "durable", None)
    try:
        os.environ["MCOMP_LOCK_DIR"] = os.path.join(rd, "locks")
        remote_mod.user_data = lambda s: "#cloud-config\n"
        settings = {"VM_NAME": "box", "PLACE": "oci", "PROVIDER": "oci", "SIZE": "arm-1x6", "EGRESS": "internet",
                    "RUN_DIR": rd, "SSH_DIR": rd}
        # F3: a launch whose answer was lost (only `launching` in the journal), kept by --keep-on-failure
        api = FakeOCI()
        remote_mod.API_FACTORY = lambda: api
        op = "d" * 32
        api.inst["i-lost"] = {"id": "i-lost", "state": "RUNNING", "shape": "x", "name": "box", "ad": "AD-1", "op": op}
        remote_mod.write_state(rd, {"provider": "oci", "launching": op})
        remote_mod.cmd_abort(settings, type("A", (), {"op": op, "keep": True})())
        st = remote_mod.read_state(rd)
        check("--keep-on-failure on a launch whose answer was lost: the instance found is recorded as this computer's, "
              "kept, before it's stopped and reported", st.get("instance") == "i-lost" and st.get("kept")
              and "launching" not in st and api.inst["i-lost"]["state"] == "STOPPED", st)
        n_act = len(api.actions)
        remote_mod.cmd_abort(settings, type("A", (), {"op": op, "keep": True})())
        check("...kept again (a second failure): an instance already stopped isn't asked to stop again",
              len(api.actions) == n_act and remote_mod.read_state(rd).get("instance") == "i-lost", api.actions)
        why = raises(remote_mod.cmd_create, settings, type("A", (), {"op": "e" * 32})(), kind=remote_mod.Refused)
        check("...a later try never deletes it (found by review: the retry terminated it as a lost launch)",
              why and api.inst["i-lost"]["state"] == "STOPPED", (why, api.inst["i-lost"]["state"]))
        # F4: the network appears (another create, or cloud setup) between a look from outside the lock and the lock
        api = FakeOCI(full={"AD-1", "AD-2", "AD-3"})
        remote_mod.API_FACTORY = lambda: api
        rd2 = os.path.join(rd, "two")
        os.makedirs(rd2)

        @contextlib.contextmanager
        def lock_after_setup(name):
            if name == "oci-network" and api.net is None:
                api.make_network()                   # `cloud setup` got the lock first and made it
            yield
        remote_mod.mac_lock = lock_after_setup
        s2 = dict(settings, RUN_DIR=rd2, SSH_DIR=rd2)
        raises(remote_mod.cmd_create, s2, type("A", (), {"op": "f" * 32})(), kind=Exception)
        st2 = remote_mod.read_state(rd2)
        remote_mod.cmd_abort(s2, type("A", (), {"op": "f" * 32, "keep": False})())
        check("a network another operation made just before this one took the lock: not this one's (made_network not "
              "recorded), so its failed create's clean-up leaves it; the group it did make goes",
              not st2.get("made_network") and st2.get("made_nsg") and api.net is not None and not api.groups("box"),
              (st2, api.net, api.nsgs))
        remote_mod.mac_lock = old_lock
        # N3: abort deletes the exact network and group this operation made, never a replacement made since
        api = FakeOCI(full={"AD-1", "AD-2", "AD-3"})
        remote_mod.API_FACTORY = lambda: api
        rd4 = os.path.join(rd, "four")
        os.makedirs(rd4)
        s4 = dict(settings, RUN_DIR=rd4, SSH_DIR=rd4)
        raises(remote_mod.cmd_create, s4, type("A", (), {"op": "b2" * 16})(), kind=Exception)
        st4 = remote_mod.read_state(rd4)
        api.nsgs.clear()                                   # its group gone, then `cloud teardown` and `cloud setup`:
        api.net = {"vcn": "v2", "subnet": "s2", "igw": "g2", "security_list": "sl2", "route_table": "rt2"}
        remote_mod.cmd_abort(s4, type("A", (), {"op": "b2" * 16, "keep": False})())
        check("abort deletes only the network and group this operation made, by the ids it recorded when it made them: a "
              "network made since (its own gone) is left alone (found by review: whichever network existed was deleted)",
              st4.get("made_network") == "v" and str(st4.get("made_nsg")).startswith("nsg-box-") and api.net
              and api.net["vcn"] == "v2",
              (st4.get("made_network"), st4.get("made_nsg"), api.net))
        api.net = None
        raises(remote_mod.cmd_create, s4, type("A", (), {"op": "b3" * 16})(), kind=Exception)
        remote_mod.cmd_abort(s4, type("A", (), {"op": "b3" * 16, "keep": False})())
        check("...and its own, still there, goes", api.net is None and not api.nsgs, (api.net, api.nsgs))
        # N4: durability errors refuse the launch; only F_FULLFSYNC not being supported falls back to fsync
        import errno
        import fcntl
        real_fcntl, real_fsync = fcntl.fcntl, os.fsync
        synced = []

        def eio(fd, op, *a):
            if op == getattr(fcntl, "F_FULLFSYNC", -1):
                raise OSError(errno.EIO, "I/O error")
            return real_fcntl(fd, op, *a)

        def unsupported(fd, op, *a):
            if op == getattr(fcntl, "F_FULLFSYNC", -1):
                raise OSError(errno.ENOTSUP, "not supported")
            return real_fcntl(fd, op, *a)
        api = FakeOCI()
        remote_mod.API_FACTORY = lambda: api
        rd5 = os.path.join(rd, "five")
        os.makedirs(rd5)
        try:
            fcntl.fcntl = eio
            why = raises(remote_mod.cmd_create, dict(settings, RUN_DIR=rd5, SSH_DIR=rd5), type("A", (), {"op": "c1" * 16})(),
                         kind=remote_mod.Refused)
            check("an I/O error making the journal durable refuses the launch: nothing launched (found by review: it "
                  "was swallowed, fsync taken instead)", why and "durable" in why and not api.launches, (why, api.launches))
            fcntl.fcntl = unsupported
            os.fsync = lambda fd: synced.append(fd) or real_fsync(fd)
            remote_mod.write_state(rd5, {"x": 1})
            check("...a file system without F_FULLFSYNC: fsync instead, and the journal is written",
                  len(synced) >= 2 and remote_mod.read_state(rd5) == {"x": 1}, synced)
        finally:
            fcntl.fcntl, os.fsync = real_fcntl, real_fsync
        # F7: the journal is on the disk (contents, then the folder's entry) before the launch
        events = []
        real = remote_mod.durable

        def spy(fd):
            events.append("durable")
            real(fd)
        remote_mod.durable = spy
        api = FakeOCI()
        remote_mod.API_FACTORY = lambda: api
        real_launch = api.launch

        def launch(*a, **k):
            events.append("launch:" + str(remote_mod.read_state(rd3).get("launching")))
            return real_launch(*a, **k)
        api.launch = launch
        rd3 = os.path.join(rd, "three")
        os.makedirs(rd3)
        remote_mod.cmd_create(dict(settings, RUN_DIR=rd3, SSH_DIR=rd3), type("A", (), {"op": "a1" * 16})())
        first_launch = next((i for i, e in enumerate(events) if e.startswith("launch")), -1)
        check("the journal (its contents and its folder's entry) is made durable before anything billable is launched "
              "(found by review: a crash could lose it), F_FULLFSYNC on macOS",
              first_launch >= 2 and events[:first_launch].count("durable") >= 2 and events[first_launch] == "launch:" + "a1" * 16
              and "F_FULLFSYNC" in open(os.path.join(LAB, "lib", "remote.py")).read(), events[:8])
    finally:
        os.environ.pop("MCOMP_LOCK_DIR", None)
        remote_mod.API_FACTORY = None
        remote_mod.user_data, remote_mod.mac_lock = old_ud, old_lock
        if old_durable is not None:
            remote_mod.durable = old_durable
        shutil.rmtree(rd, ignore_errors=True)


def uninstall_order():
    """remote-uninstall, in a sandbox with a stand-in systemctl that does what systemd's Requires= does: stopping the
    firewall while a network manager still requires it stops the network manager too (found by review)."""
    td = tempfile.mkdtemp(prefix="mcun-")
    try:
        fake = os.path.join(td, "bin")
        os.makedirs(fake)
        etc = os.path.join(td, "etc")
        for d in ("systemd/system/systemd-networkd.service.d", "systemd/system/NetworkManager.service.d", "ssh/sshd_config.d"):
            os.makedirs(os.path.join(etc, d))
        for d in ("systemd-networkd", "NetworkManager"):
            open(os.path.join(etc, "systemd/system", d + ".service.d", "10-metacomputer-firewall.conf"), "w").write("x\n")
        log = os.path.join(td, "calls")
        with open(os.path.join(fake, "systemctl"), "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {log}\n'
                    f'case "$*" in\n'
                    f'  "disable --now metacomputer-firewall.service")\n'
                    f'    [ -e {etc}/systemd/system/systemd-networkd.service.d/10-metacomputer-firewall.conf ] && touch {td}/networkd-stopped\n'
                    f'    exit 0 ;;\n'
                    f'  "is-active --quiet systemd-networkd.service") [ ! -e {td}/networkd-stopped ] ;;\n'
                    f'  "is-active --quiet NetworkManager.service") exit 3 ;;\n'
                    f'  "start systemd-networkd.service") rm -f {td}/networkd-stopped ;;\n'
                    f'  *) exit 0 ;;\nesac\n')
        for n in ("nft", "sshd"):
            with open(os.path.join(fake, n), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
        for n in os.listdir(fake):
            os.chmod(os.path.join(fake, n), 0o755)
        src = open(os.path.join(LAB, "provisioning", "remote", "rootfs", "opt", "metacomputer", "bin", "remote-uninstall")).read()
        src = src.replace('[ "$(id -u)" = 0 ]', "true").replace("PATH=/usr/sbin:/usr/bin:/sbin:/bin", f"PATH={fake}:/usr/bin:/bin")
        src = src.replace("/etc/", etc + "/").replace("/var/lib/metacomputer", td + "/var").replace("/run/metacomputer", td + "/run")
        script = os.path.join(td, "remote-uninstall")
        open(script, "w").write(src)
        r = subprocess.run(["/bin/sh", script, "rollback"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        never_stopped = not os.path.exists(os.path.join(td, "networkd-stopped"))
        check("remote-uninstall rollback: the network's dependency removed and systemd reloaded before the firewall "
              "stops, so the running network manager never stops with it",
              r.returncode == 0 and never_stopped and "daemon-reload" in calls
              and calls.index("daemon-reload") < calls.index("disable --now metacomputer-firewall.service")
              and "start systemd-networkd.service" not in calls, (r.stderr[-300:], calls))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def credentials_children():
    """Item 2, as re-checked: no program lib/remote.py starts gets the provider's credentials (getconf for its lock
    folder, the settings resolver's bash): they leave its environment before anything runs, and go to the adapter."""
    print("\nlib/remote.py's own children")
    prog = r"""
import json, os, subprocess, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import remote, test_remote
seen = []
real_popen = subprocess.Popen.__init__
def spy(self, args, *a, **k):
    env = k.get("env")
    env = os.environ if env is None else env
    seen.append([str(args[0] if isinstance(args, (list, tuple)) else args), sorted(x for x in env if x.startswith("OCI_"))])
    return real_popen(self, args, *a, **k)
subprocess.Popen.__init__ = spy
api = test_remote.FakeOCI()
remote.API_FACTORY = lambda: api
os.environ.pop("MCOMP_LOCK_DIR", None)
rc = remote.main(["cloud", "setup", "oci", "--json"])
print(json.dumps({"rc": rc, "seen": seen, "left": sorted(k for k in os.environ if k.startswith("OCI_")),
                  "creds": sorted(remote.CREDS)}))
"""
    env = dict(os.environ, OCI_USER="ocid1.user.synthetic", OCI_API_KEY_PEM="SYNTHETIC-PEM", OCI_TENANCY="t",
               OCI_FINGERPRINT="f", OCI_REGION="r", OCI_COMPARTMENT="c")
    r = subprocess.run([PY, "-c", prog, os.path.join(LAB, "lib"), TESTS], env=env, capture_output=True, text=True,
                       timeout=120)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {}
    kids = out.get("seen") or []
    check("a credential-bearing lib/remote.py starts its children (getconf for the lock folder included) without "
          "OCI_*, keeps none in its own environment, and hands them to the adapter as a value (found by review: "
          "getconf got OCI_API_KEY_PEM)", out.get("rc") == 0 and kids and all(not k[1] for k in kids)
          and any(k[0].endswith("getconf") for k in kids) and out.get("left") == []
          and "OCI_API_KEY_PEM" in (out.get("creds") or []), (out, r.stderr[-400:]))


def abort_scoped():
    """New #1: a failed create can't delete an instance it didn't make. The build names its operation; lib/remote.py
    abort deletes only what that operation made (reproduced by review: an existing same-named instance was
    terminated by the clean-up)."""
    print("\na failed build deletes only what it made")
    import remote as remote_mod
    rd = tempfile.mkdtemp(prefix="mcab-")
    old_ud = remote_mod.user_data
    try:
        os.environ["MCOMP_LOCK_DIR"] = os.path.join(rd, "locks")
        api = FakeOCI()
        api.make_network()
        theirs = api.make_nsg("box", "internet", op="o" * 32)
        api.inst["i-theirs"] = {"id": "i-theirs", "state": "RUNNING", "shape": "x", "name": "box", "ad": "AD-1", "op": "o" * 32}
        remote_mod.API_FACTORY = lambda: api
        remote_mod.user_data = lambda s: "#cloud-config\n"
        settings = {"VM_NAME": "box", "PLACE": "oci", "PROVIDER": "oci", "SIZE": "arm-1x6", "EGRESS": "internet",
                    "RUN_DIR": rd, "SSH_DIR": rd}
        op = "a" * 32
        why = raises(remote_mod.cmd_create, settings, type("A", (), {"op": op})(), kind=Exception)
        remote_mod.cmd_abort(settings, type("A", (), {"op": op, "keep": False})())
        check("an instance of the same name this build didn't make (its record here lost): create refuses, and the "
              "clean-up leaves it, its security group and the network alone",
              why and api.inst["i-theirs"]["state"] == "RUNNING" and [g["id"] for g in api.groups("box")] == [theirs]
              and api.net is not None, (why, api.inst))
        # Its own launch: deleted, with the group and the network it made (nothing else uses them).
        api2 = FakeOCI()
        remote_mod.API_FACTORY = lambda: api2
        rd2 = os.path.join(rd, "two")
        os.makedirs(rd2)
        s2 = dict(settings, RUN_DIR=rd2, SSH_DIR=rd2)
        remote_mod.cmd_create(s2, type("A", (), {"op": "b" * 32})())
        mine = [i for i in api2.inst.values() if i.get("op") == "b" * 32]
        remote_mod.cmd_abort(s2, type("A", (), {"op": "c" * 32, "keep": False})())
        check("...abort with another operation's id: nothing deleted", mine and mine[0]["state"] == "RUNNING")
        remote_mod.cmd_abort(s2, type("A", (), {"op": "b" * 32, "keep": True})())
        check("...--keep-on-failure: this build's instance stopped, nothing deleted", api2.actions[-1:] == ["STOP"]
              and mine[0]["state"] == "STOPPED", api2.actions)
        remote_mod.cmd_abort(s2, type("A", (), {"op": "b" * 32, "keep": False})())
        check("...its own operation: its instance, its security group and the network it made are deleted",
              mine[0]["state"] == "TERMINATED" and not api2.groups("box") and api2.net is None, (api2.inst, api2.nsgs))
    finally:
        os.environ.pop("MCOMP_LOCK_DIR", None)
        remote_mod.API_FACTORY = None
        remote_mod.user_data = old_ud
        shutil.rmtree(rd, ignore_errors=True)
    src = open(os.path.join(LAB, "scripts", "remote-build.sh")).read()
    check("remote-build.sh: the operation is named before the launch and passed to it; the clean-up is abort --op, "
          "never delete", 'remote_py create --vm "$VM_NAME" --op "$op"' in src and 'remote_py abort --vm "$VM_NAME" --op "$ABORT_OP"' in src
          and "remote_py delete" not in src.split("cleanup_failed() {")[1].split("\n}\n")[0])


# --- Oracle Cloud's security groups, through the real SDK facade on an in-memory Oracle -------------------------------
class OracleStandIn:
    """Oracle's own clients (VirtualNetworkClient, ComputeClient, IdentityClient), in memory, under
    providers/oci/adapter.py's real SDK facade: groups and instances by OCID with freeform tags, rules per group, VNICs
    with their groups. Every change to a group's rules is recorded (rule_changes), and every group deleted (deleted)."""

    class ServiceError(Exception):
        def __init__(self, status, code="", message=""):
            super().__init__(message)
            self.status, self.code, self.message = status, code, message

    def __init__(self, compartment="c"):
        import types
        self.compartment = compartment
        self.n = 0
        self.groups = {}          # gid -> {id, compartment_id, vcn_id, display_name, freeform_tags, lifecycle_state}
        self.rules = {}           # gid -> [rule]
        self.rule_changes = []
        self.deleted = []
        self.instances = {}
        self.vnics = {}
        self.fail_rules = False
        self.full = set()
        self.net_deleted = []     # the network's parts deleted (subnet, gateway, VCN)
        ns = types.SimpleNamespace
        st = self

        class Models:
            def __getattr__(self, name):
                return lambda **kw: ns(**kw)

        def pages(fn, *a, **k):
            return ns(data=fn(*a, **k))
        self.oci = ns(core=ns(models=Models()), wait_until=lambda *a, **k: None,
                      exceptions=ns(ServiceError=OracleStandIn.ServiceError), pagination=ns(list_call_get_all_results=pages),
                      retry=ns(NoneRetryStrategy=lambda: "no-retry"))
        vcn = ns(id="vcn-1", time_created="2026-11-01", freeform_tags={oci.TAG: oci.NETWORK_TAG}, lifecycle_state="AVAILABLE",
                 default_security_list_id="sl-1", default_route_table_id="rt-1")

        class Net:
            def list_vcns(self, compartment):
                return [vcn] if compartment == st.compartment and "vcn-1" not in st.net_deleted else []

            def _part(self, rid):
                if rid in st.net_deleted:
                    raise OracleStandIn.ServiceError(404, "NotAuthorizedOrNotFound")
                return ns(data=ns(id=rid, lifecycle_state="AVAILABLE"))

            def delete_subnet(self, rid):
                st.net_deleted.append(rid)

            def delete_internet_gateway(self, rid):
                st.net_deleted.append(rid)

            def delete_vcn(self, rid):
                st.net_deleted.append(rid)

            def get_subnet(self, rid):
                return self._part(rid)

            def get_internet_gateway(self, rid):
                return self._part(rid)

            def get_vcn(self, rid):
                return self._part(rid)

            def list_subnets(self, compartment, vcn_id=None):
                return [ns(id="subnet-1", lifecycle_state="AVAILABLE")]

            def list_internet_gateways(self, compartment, vcn_id=None):
                return [ns(id="igw-1", lifecycle_state="AVAILABLE")]

            def update_route_table(self, rt, d):
                pass

            def update_security_list(self, sl, d):
                pass

            def create_network_security_group(self, d):
                st.n += 1
                gid = f"ocid1.networksecuritygroup.{st.n}"
                st.groups[gid] = {"id": gid, "compartment_id": d.compartment_id, "vcn_id": d.vcn_id,
                                  "display_name": d.display_name, "freeform_tags": dict(d.freeform_tags or {}),
                                  "lifecycle_state": "AVAILABLE"}
                st.rules[gid] = []
                return ns(data=ns(id=gid))

            def get_network_security_group(self, gid):
                g = st.groups.get(gid)
                if g is None:
                    raise OracleStandIn.ServiceError(404, "NotAuthorizedOrNotFound")
                return ns(data=ns(**g))

            def list_network_security_groups(self, compartment_id=None, vcn_id=None):
                return [ns(**g) for g in st.groups.values() if g["compartment_id"] == compartment_id
                        and g["vcn_id"] == vcn_id]

            def delete_network_security_group(self, gid):
                if gid not in st.groups:
                    raise OracleStandIn.ServiceError(404, "NotAuthorizedOrNotFound")
                del st.groups[gid]
                st.deleted.append(gid)

            def list_network_security_group_security_rules(self, gid):
                return [ns(**r) for r in st.rules[gid]]

            def remove_network_security_group_security_rules(self, gid, d):
                st.rule_changes.append(("remove", gid))
                st.rules[gid] = [r for r in st.rules[gid] if r["id"] not in d.security_rule_ids]

            def add_network_security_group_security_rules(self, gid, d):
                st.rule_changes.append(("add", gid))
                if st.fail_rules:
                    raise OracleStandIn.ServiceError(500, "InternalError", "the rules weren't added")
                for r in d.security_rules:
                    st.n += 1
                    st.rules[gid].append({"id": f"rule-{st.n}", "direction": r.direction, "protocol": r.protocol,
                                          "destination": r.destination, "source": None})

            def get_vnic(self, vid):
                return ns(data=ns(**st.vnics[vid]))

            def get_security_list(self, sl):
                return ns(data=ns(ingress_security_rules=[]))

        class Compute:
            def list_shapes(self, compartment, availability_domain=None):
                return [ns(shape="VM.Standard.A1.Flex")]

            def list_images(self, compartment, **k):
                return ns(data=[ns(id="img-1", display_name="Canonical-Ubuntu-24.04-aarch64")])

            def launch_instance(self, d, retry_strategy=None):
                if d.availability_domain in st.full:
                    raise OracleStandIn.ServiceError(500, "InternalError", "Out of host capacity.")
                st.n += 1
                iid, vid = f"ocid1.instance.{st.n}", f"ocid1.vnic.{st.n}"
                st.vnics[vid] = {"id": vid, "public_ip": "198.51.100.7", "nsg_ids": list(d.create_vnic_details.nsg_ids or [])}
                st.instances[iid] = {"id": iid, "compartment_id": d.compartment_id, "lifecycle_state": "RUNNING",
                                     "shape": d.shape, "availability_domain": d.availability_domain,
                                     "freeform_tags": dict(d.freeform_tags or {}), "time_created": "t", "vnic": vid}
                return ns(data=ns(id=iid))

            def get_instance(self, iid):
                i = st.instances.get(iid)
                if i is None:
                    raise OracleStandIn.ServiceError(404, "NotAuthorizedOrNotFound")
                return ns(data=ns(**i))

            def list_instances(self, compartment):
                return [ns(**i) for i in st.instances.values() if i["compartment_id"] == compartment]

            def terminate_instance(self, iid, preserve_boot_volume=False):
                st.instances[iid]["lifecycle_state"] = "TERMINATED"

            def instance_action(self, iid, what):
                st.instances[iid]["lifecycle_state"] = "STOPPED" if "STOP" in what else "RUNNING"

            def list_vnic_attachments(self, compartment, instance_id=None):
                i = st.instances.get(instance_id)
                return [ns(lifecycle_state="ATTACHED", vnic_id=i["vnic"])] if i else []

        class Identity:
            def list_availability_domains(self, tenancy):
                return ns(data=[ns(name="AD-1"), ns(name="AD-2")])
        self.net, self.compute, self.identity = Net(), Compute(), Identity()

    def sdk(self):
        s = object.__new__(oci.SDK)
        s.oci, s.net, s.compute, s.identity = self.oci, self.net, self.compute, self.identity
        s.compartment, s.cfg = self.compartment, {"tenancy": "t"}
        return s

    def plant(self, name, op=None, compartment=None, rules=0):
        """A group made outside this Mac's create: another Mac's computer of the same name (another operation), or one
        an earlier version made (no operation tag)."""
        self.n += 1
        gid = f"ocid1.networksecuritygroup.{self.n}"
        tags = {oci.TAG: name, **({oci.OP_TAG: op} if op else {})}
        self.groups[gid] = {"id": gid, "compartment_id": compartment or self.compartment, "vcn_id": "vcn-1",
                            "display_name": f"metacomputer-{name}", "freeform_tags": tags, "lifecycle_state": "AVAILABLE"}
        self.rules[gid] = [{"id": f"r{gid}-{i}", "direction": "EGRESS", "protocol": "all", "destination": "10.0.0.0/8",
                            "source": None} for i in range(rules)]
        return gid

    def touched(self, gid):
        return [c for c in self.rule_changes if c[1] == gid]


def oci_groups():
    """Item: Oracle Cloud's security group (NSG) is this computer's and this operation's by the create's answer, never
    by a lookup of its name; everything after acts only on the recorded id, read again and checked against both tags."""
    print("\nOracle Cloud's security groups: the create's answer, never a lookup by name")
    import contextlib
    import io
    import remote as remote_mod
    rd = tempfile.mkdtemp(prefix="mcnsg-")
    old_ud = remote_mod.user_data
    OP, OP2, THEIRS, OLD = "a" * 32, "b" * 32, "c" * 32, "d" * 32

    def args(**k):
        return type("A", (), dict({"op": None, "keep": False, "network_too": False, "checked_provider": False,
                                   "set": None, "setup": False}, **k))()

    def box(tag):
        d = os.path.join(rd, tag)
        os.makedirs(d)
        return {"VM_NAME": "box", "PLACE": "oci", "PROVIDER": "oci", "SIZE": "arm-1x6", "EGRESS": "internet",
                "RUN_DIR": d, "SSH_DIR": d}

    def quiet(fn, *a, **k):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return fn(*a, **k)

    def raised(fn, *a, kind=Exception):
        try:
            quiet(fn, *a)
        except kind as e:
            return str(e) or type(e).__name__
        except Exception as e:  # noqa: BLE001
            return f"WRONG {type(e).__name__}: {e}"
        return None
    try:
        os.environ["MCOMP_LOCK_DIR"] = os.path.join(rd, "locks")
        remote_mod.user_data = lambda s: "#cloud-config\n"
        # 1. A group of the same name there already (another Mac's computer "box", another operation): the create makes
        #    its own, journals it from the create's answer, launches on it, and never reuses or changes theirs.
        o = OracleStandIn()
        remote_mod.API_FACTORY = o.sdk
        theirs = o.plant("box", THEIRS)
        s1 = box("one")
        rc = quiet(remote_mod.cmd_create, s1, args(op=OP))
        st = remote_mod.read_state(s1["RUN_DIR"])
        mine = st.get("nsg")
        vnic = o.vnics[o.instances[st["instance"]]["vnic"]] if st.get("instance") in o.instances else {}
        check("(OCI groups) a create journals its own new group from the create's answer (made_nsg) even when a group "
              "tagged with the computer's name is there already, launches on it, and never reuses or changes that one",
              rc == 0 and mine and mine != theirs and st.get("made_nsg") == mine and vnic.get("nsg_ids") == [mine]
              and o.groups[mine]["freeform_tags"] == {oci.TAG: "box", oci.OP_TAG: OP} and not o.touched(theirs)
              and theirs in o.groups, (st, theirs, o.rule_changes))
        check("(OCI groups) ...its display name carries the operation (metacomputer-NAME-OP12), and the record keeps the "
              "operation (ops)", o.groups.get(mine, {}).get("display_name") == f"metacomputer-box-{OP[:12]}"
              and st.get("ops") == [OP], (o.groups.get(mine), st.get("ops")))
        # 2. net egress and the adapter's apply_egress act only on the recorded group, read by its id, still tagged for
        #    this computer and its operation. Re-tagged for another operation: refused, no rule changed anywhere.
        o.groups[mine]["freeform_tags"][oci.OP_TAG] = OP2
        n = len(o.rule_changes)
        why = raised(remote_mod.cmd_egress, s1, args(set="off"), kind=remote_mod.NotOwned)
        a1 = oci.OCI(api=o.sdk(), env={}, computers={"box": dict(s1)}, states={"box": remote_mod.read_state(s1["RUN_DIR"])})
        why2 = raised(a1.apply_egress, "box", "off", kind=AdapterError)
        check("(OCI groups) net egress on a recorded group now tagged for another operation: refused, and no group's "
              "rules changed (the adapter's apply_egress refuses it too)",
              why and not why.startswith("WRONG") and why2 and not why2.startswith("WRONG")
              and len(o.rule_changes) == n, (why, why2, o.rule_changes[n:]))
        check("(OCI groups) ...and egress() doesn't report that group's rules as the computer's (not known)",
              a1.egress("box").get("outside") is None, a1.egress("box"))
        o.groups[mine]["freeform_tags"][oci.OP_TAG] = OP
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = remote_mod.cmd_egress(s1, args(set="off"))
        check("(OCI groups) ...tagged for it and its operation again: net egress off changes exactly that group",
              rc == 0 and out.getvalue().strip() == "off" and o.rules[mine] == [] and not o.touched(theirs),
              (out.getvalue(), o.rule_changes[n:]))
        # 3. doctor reports the groups OCI applies to the instance's network card (and changes nothing).
        n = len(o.rule_changes)
        o.rules[theirs] = [{"id": "x", "direction": "EGRESS", "protocol": "all", "destination": "10.0.0.0/8", "source": None}]
        d = {c["check"]: c for c in oci.OCI(api=o.sdk(), env={}).doctor()}
        row = d.get("box: its own security group lets out public addresses only") or {}
        check("(OCI groups) doctor reads the group on the instance's own network card, not one found by its name (a "
              "same-named group of another operation with a private rule is not this computer's), and changes nothing",
              row.get("ok") is True and len(o.rule_changes) == n, (row, o.rule_changes[n:]))
        # 4. vm delete: its instance and its own group go; the same-named group of another operation stays.
        rc = quiet(remote_mod.cmd_delete, s1, args())
        check("(OCI groups) vm delete deletes the computer's own group (by its recorded id, checked) and never a "
              "same-named group of another operation", rc == 0 and mine not in o.groups and theirs in o.groups
              and o.deleted == [mine], (o.deleted, list(o.groups)))
        # 5. A failed configure after the create (its rules refused): the id is in the journal already, and the
        #    clean-up (abort --op) deletes exactly that one, leaving the other operation's.
        o2 = OracleStandIn()
        remote_mod.API_FACTORY = o2.sdk
        theirs2 = o2.plant("box", THEIRS)
        o2.fail_rules = True
        s2 = box("two")
        why = raised(remote_mod.cmd_create, s2, args(op=OP2))
        j = remote_mod.read_state(s2["RUN_DIR"])
        made = j.get("made_nsg")
        check("(OCI groups) a create whose group's rules fail after the create answered: the group's id is journaled "
              "already (made_nsg), so the clean-up can find it; nothing launched", why and made and made != theirs2
              and made in o2.groups and not o2.instances, (why, j, list(o2.groups)))
        o2.fail_rules = False
        rc = quiet(remote_mod.cmd_abort, s2, args(op=OP2))
        check("(OCI groups) abort --op deletes only its own operation's group: the same-named group of another "
              "operation stays, its rules untouched", rc == 0 and made not in o2.groups and theirs2 in o2.groups
              and not o2.touched(theirs2) and o2.deleted == [made], (o2.deleted, list(o2.groups), o2.rule_changes))
        # 6. abort --op after a launch that found no room (every AD full): its group goes, the other's stays.
        o3 = OracleStandIn()
        remote_mod.API_FACTORY = o3.sdk
        theirs3 = o3.plant("box", THEIRS, rules=1)
        o3.full = {"AD-1", "AD-2"}
        s3 = box("three")
        raised(remote_mod.cmd_create, s3, args(op=OP))
        made3 = remote_mod.read_state(s3["RUN_DIR"]).get("made_nsg")
        quiet(remote_mod.cmd_abort, s3, args(op=OP))
        check("(OCI groups) a build with no capacity anywhere: abort --op deletes the group its create made and "
              "nothing else (the same-named group of another operation stays, rules unchanged)",
              made3 and made3 != theirs3 and o3.deleted == [made3] and theirs3 in o3.groups and not o3.touched(theirs3),
              (made3, o3.deleted, o3.rule_changes))
        # 7. (round 2) The group is re-read by its id right before its first rule is written: one re-tagged for another
        #    operation while the create waited for it gets no rule, the build stops, and the journal keeps its id.
        o4 = OracleStandIn()
        remote_mod.API_FACTORY = o4.sdk

        def retag(*a, **k):                     # what happens to the group while the create waits for it
            for g in o4.groups.values():
                if g["freeform_tags"].get(oci.OP_TAG) == OP:
                    g["freeform_tags"][oci.OP_TAG] = THEIRS
        o4.oci.wait_until = retag
        s4 = box("r2-retagged")
        why = raised(remote_mod.cmd_create, s4, args(op=OP))
        made4 = remote_mod.read_state(s4["RUN_DIR"]).get("made_nsg")
        check("(OCI groups, round 2) a group re-tagged for another operation while the create waited for it: no rule "
              "is written to it, nothing is launched, and its id stays in the journal",
              why and not why.startswith("WRONG") and made4 in o4.groups and not o4.touched(made4) and not o4.instances,
              (why, made4, o4.rule_changes, list(o4.instances)))
        # 8. (round 2) Deleting metacomputer's network deletes only the groups this Mac's records name (read by id,
        #    tagged for that computer and one of its operations). Another Mac's group made after cloud teardown looked
        #    (the race no lock here covers) stops it, with nothing deleted: not the group, not the network.
        o5 = OracleStandIn()
        foreign = o5.plant("other", THEIRS)

        def raced():                            # teardown's look for groups came before the other Mac made its own
            s = o5.sdk()
            s.nsg_names = lambda: []
            return s
        remote_mod.API_FACTORY = raced
        why = raised(remote_mod.cmd_cloud, type("A", (), {"provider": "oci", "action": "teardown", "json": True,
                                                         "org": None, "forget": None})())
        check("(OCI groups, round 2) cloud teardown racing another Mac's new group: refused, the group and the network "
              "left as they are", why and not why.startswith("WRONG") and foreign in o5.groups and not o5.deleted
              and not o5.net_deleted, (why, o5.deleted, o5.net_deleted))
        mine5 = o5.plant("box", OP)
        o5.groups.pop(foreign)
        why = raised(o5.sdk().delete_network, kind=Exception)
        check("(OCI groups, round 2) ...nor a group of a computer of this Mac's that the call wasn't told about",
              why and mine5 in o5.groups and not o5.net_deleted, (why, o5.deleted, o5.net_deleted))
        sdk5 = o5.sdk()
        o5.groups[mine5]["freeform_tags"][oci.OP_TAG] = THEIRS
        why = raised(sdk5.delete_network, [(mine5, "box", [OP])], kind=Exception)
        check("(OCI groups, round 2) ...nor a recorded group now tagged for another operation",
              why and mine5 in o5.groups and not o5.net_deleted, (why, o5.deleted, o5.net_deleted))
        o5.groups[mine5]["freeform_tags"][oci.OP_TAG] = OP
        done = quiet(sdk5.delete_network, [(mine5, "box", [OP])])
        check("(OCI groups, round 2) ...and a group the records name, still tagged for that computer and operation, "
              "goes with the network", done is True and mine5 not in o5.groups and "vcn-1" in o5.net_deleted,
              (done, o5.deleted, o5.net_deleted))
        # 7. A computer made by an earlier version: its recorded group carries no operation. Rules: refused (delete it
        #    and make it again); vm delete removes exactly that recorded group, and never another operation's.
        o4 = OracleStandIn()
        remote_mod.API_FACTORY = o4.sdk
        legacy = o4.plant("box", None)
        other4 = o4.plant("box", THEIRS)
        o4.full = set()
        i4 = o4.compute.launch_instance(o4.oci.core.models.X(
            compartment_id="c", availability_domain="AD-1", shape="VM.Standard.A1.Flex",
            create_vnic_details=o4.oci.core.models.X(nsg_ids=[legacy]), freeform_tags={oci.TAG: "box", oci.OP_TAG: OLD})).data.id
        s4 = box("four")
        remote_mod.write_state(s4["RUN_DIR"], {"provider": "oci", "instance": i4, "op": OLD, "nsg": legacy})
        n = len(o4.rule_changes)
        why = raised(remote_mod.cmd_egress, s4, args(set="off"), kind=remote_mod.NotOwned)
        a4 = oci.OCI(api=o4.sdk(), env={}, computers={"box": dict(s4)}, states={"box": remote_mod.read_state(s4["RUN_DIR"])})
        why2 = raised(a4.apply_egress, "box", "off", kind=AdapterError)
        check("(OCI groups) a group an earlier version made (no operation tag): net egress and apply_egress refuse, "
              "saying to delete it and make it again, and no rule changes", why and "earlier version" in why
              and why2 and "earlier version" in why2 and "make it again" in why2 and len(o4.rule_changes) == n, (why, why2))
        quiet(remote_mod.cmd_delete, s4, args())
        check("(OCI groups) ...vm delete removes exactly the recorded group (its own, from before), never the "
              "same-named group of another operation", o4.deleted == [legacy] and other4 in o4.groups, o4.deleted)
        # 8. A group an earlier operation of this Mac's left (its clean-up failed): found again at vm delete by that
        #    operation's mark (the record's ops), never by the name: another operation's stays, and cloud teardown
        #    then says how to remove a group no record here names (it never deletes one on its name).
        o5 = OracleStandIn()
        remote_mod.API_FACTORY = o5.sdk
        foreign = o5.plant("box", THEIRS)
        left = o5.plant("box", OLD)
        s5 = box("five")
        quiet(remote_mod.cmd_create, s5, args(op=OP))
        st5 = remote_mod.read_state(s5["RUN_DIR"])
        remote_mod.write_state(s5["RUN_DIR"], dict(st5, ops=[OLD] + st5.get("ops", [])))
        quiet(remote_mod.cmd_delete, s5, args())
        check("(OCI groups) vm delete deletes a group an earlier operation of this computer's left (its mark is one the "
              "record names), and not a same-named group of an operation it doesn't", left not in o5.groups
              and st5.get("nsg") not in o5.groups and foreign in o5.groups, (o5.deleted, list(o5.groups)))
        why = raised(remote_mod.cmd_cloud, type("A", (), {"provider": "oci", "action": "teardown", "json": True,
                                                          "forget": None})(), kind=remote_mod.Refused)
        check("(OCI groups) ...cloud teardown then refuses (a group is there) and says how to remove one no record here "
              "names (the console), never deleting it on its name", why and "console" in why
              and "never deleted on its name" in why and foreign in o5.groups, why)
        # 9. The facade's own checks: a group read by its id must be tagged for the computer AND the operation.
        o6 = OracleStandIn()
        sdk = o6.sdk()
        g_other = o6.plant("box", THEIRS)
        g_away = o6.plant("box", OP, compartment="elsewhere")
        g_old = o6.plant("box", None)
        g_mine = o6.plant("box", OP)
        ok = (sdk.nsg_owner(g_other) == ("box", THEIRS) and sdk.nsg_owner(g_away) is None
              and sdk.nsg_owner(g_old) == ("box", None) and sdk.nsg_owner("ocid1.networksecuritygroup.gone") is None
              and sdk.nsgs_of_op("box", OP) == [g_mine])
        r = [sdk.delete_nsg_by_id(g_other, "box", OP), sdk.delete_nsg_by_id(g_mine, "other", OP),
             sdk.delete_nsg_by_id(g_old, "box", OP), sdk.delete_nsg_by_id(g_mine, "box", None),
             sdk.delete_nsg_by_id(g_away, "box", OP)]
        check("(OCI groups) the facade: nsg_owner reads a group by its id (gone, or in another compartment: None); "
              "delete_nsg_by_id refuses another operation's, another computer's, one without an operation (unless "
              "it's the recorded legacy one), one with no operation to check against, and one elsewhere",
              ok and r == [False] * 5 and not o6.deleted, (ok, r, o6.deleted))
        check("(OCI groups) ...and deletes the one tagged for the computer and that operation; make_nsg refuses "
              "without an operation", sdk.delete_nsg_by_id(g_mine, "box", OP) and o6.deleted == [g_mine]
              and raised(sdk.make_nsg, "box", "internet", None, kind=AdapterError) is not None, o6.deleted)
    finally:
        os.environ.pop("MCOMP_LOCK_DIR", None)
        remote_mod.API_FACTORY = None
        remote_mod.user_data = old_ud
        shutil.rmtree(rd, ignore_errors=True)


def cloud_lock():
    """New #3: cloud setup and teardown change the shared network under the same lock as a build, and teardown leaves
    a network a computer is being made in (its group there, no instance yet)."""
    import remote as remote_mod
    held = []
    real = remote_mod.mac_lock
    import contextlib

    @contextlib.contextmanager
    def spy(name):
        held.append(name)
        yield
    remote_mod.mac_lock = spy
    try:
        api = FakeOCI()
        remote_mod.API_FACTORY = lambda: api
        rc = remote_mod.cmd_cloud(type("A", (), {"provider": "oci", "action": "setup", "json": True})())
        check("cloud setup makes the network under the network lock", rc == 0 and held == ["oci-network"] and api.net, held)
        api.make_nsg("pending", "internet", op="b" * 32)   # a build between its group and its launch
        held.clear()
        why = raises(remote_mod.cmd_cloud, type("A", (), {"provider": "oci", "action": "teardown", "json": True})(),
                     kind=remote_mod.Refused)
        check("cloud teardown while a computer is being made (its group, no instance): refused, under the lock, the "
              "network kept", why and "being made" in why and held == ["oci-network"] and api.net is not None, (why, held))
        api.drop("pending")
        rc = remote_mod.cmd_cloud(type("A", (), {"provider": "oci", "action": "teardown", "json": True})())
        check("...and once nothing is made there, it goes", rc == 0 and api.net is None)
    finally:
        remote_mod.mac_lock = real
        remote_mod.API_FACTORY = None


def owner_master(td):
    """New #2: your own server's owner checks can't pass through an SSH connection you already have open (a
    ControlMaster your ssh config keeps): a real sshd here, a master open to it, then the server stops letting your
    key in; the build's check after the bootstrap and vm delete's after the removal must both see that."""
    print("\nyour own server: owner checks with a master connection open")
    import test_multi_vm as tmv
    sshd = "/usr/sbin/sshd"
    if not os.access(sshd, os.X_OK):
        print("SKIP no sshd here")
        return
    d = os.path.join(td, "s")
    os.makedirs(d)
    for k in ("hostkey", "userkey"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(d, k)], check=True)
    ak = os.path.join(d, "authorized_keys")
    shutil.copy(os.path.join(d, "userkey.pub"), ak)
    os.chmod(ak, 0o600)
    hostkey = " ".join(open(os.path.join(d, "hostkey.pub")).read().split()[:2])
    # What the server does with each command it's sent (ForceCommand): the bootstrap reports a host key and, like a
    # setup that locks its owner out, stops taking the owner's key; remote-uninstall all does the same.
    force = os.path.join(d, "force.sh")
    with open(force, "w") as f:
        f.write("#!/bin/sh\n"
                f'echo "$SSH_ORIGINAL_COMMAND" >> {d}/commands\n'
                'case "$SSH_ORIGINAL_COMMAND" in\n'
                '  "dpkg --print-architecture") echo arm64 ;;\n'
                '  *"tar -xf -"*) cat > /dev/null ;;\n'
                '  "sudo -n true") exit 0 ;;\n'
                f'  *bootstrap.sh*) : > {ak}; echo "HOSTKEY {hostkey}" ;;\n'
                f'  *"remote-uninstall all"*) : > {ak} ;;\n'
                '  *) exit 0 ;;\nesac\n')
    os.chmod(force, 0o755)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with open(os.path.join(d, "sshd_config"), "w") as f:
        f.write(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d}/hostkey\nAuthorizedKeysFile {ak}\n"
                f"PasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\n"
                f"PidFile {d}/sshd.pid\nForceCommand {force}\n")
    r = subprocess.run([sshd, "-f", os.path.join(d, "sshd_config"), "-E", os.path.join(d, "sshd.log")], capture_output=True)
    if r.returncode != 0:
        print("SKIP sshd doesn't start here:", r.stderr[-200:])
        return
    try:
        user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        known = os.path.join(d, "known")
        server = "192.0.2.50"     # the server's address in its settings (a loopback one is refused): your config maps it here
        open(known, "w").write(f"[127.0.0.1]:{port} {hostkey}\n[{server}]:{port} {hostkey}\n")
        # The owner's own ssh config: the server, a key, its known_hosts, and a ControlMaster that keeps a connection
        # open (only through it would a check reach the server after it stopped taking the key).
        cfg = os.path.join(d, "config")
        open(cfg, "w").write(f"Host {server}\n  HostName 127.0.0.1\n"
                             f"Host *\n  IdentityFile {d}/userkey\n  IdentitiesOnly yes\n  UserKnownHostsFile {known}\n"
                             f"  ControlMaster auto\n  ControlPath {d}/cm-%C\n  ControlPersist 120\n")
        wrap = os.path.join(d, "wrap")
        os.makedirs(wrap)
        with open(os.path.join(wrap, "ssh"), "w") as f:      # your ssh, reading your config (only -F differs here)
            f.write(f'#!/bin/sh\nexec /usr/bin/ssh -F {cfg} "$@"\n')
        os.chmod(os.path.join(wrap, "ssh"), 0o755)

        def open_master():
            shutil.copy(os.path.join(d, "userkey.pub"), ak)
            subprocess.run(["/usr/bin/ssh", "-F", cfg, "-o", "BatchMode=yes", "-fN", "-p", str(port), f"{user}@{server}"],
                           capture_output=True, timeout=30)
            return subprocess.run(["/usr/bin/ssh", "-F", cfg, "-O", "check", "-p", str(port), f"{user}@{server}"],
                                  capture_output=True, timeout=10).returncode == 0
        b = tmv.Box(td, "owner")
        shutil.copytree(os.path.join(LAB, "provisioning"), os.path.join(b.co, "provisioning"))
        try:
            r = b.run("vm", "create", "own2", "--on", "server", "--host", server, "--user", user, "--port", str(port),
                      "--no-build", "--yes")
            check("(a server's settings for the check)", r.returncode == 0, r.stderr[-300:])
            env = b.env(PATH=f"{wrap}:{os.path.dirname(tmv.PY)}:/usr/bin:/bin:/usr/sbin:/sbin", MCOMP_REMOTE_SSH_WAIT="1")
            master = open_master()
            r = subprocess.run([b.mc, "--vm", "own2", "build"], env=env, capture_output=True, text=True, timeout=300,
                               stdin=subprocess.DEVNULL)
            out = r.stdout + r.stderr
            check("the build's owner check, with a master connection of yours open: a new connection is tried (it "
                  "fails: the bootstrap stopped taking your key), so the setup is rolled back and the build stops",
                  master and r.returncode != 0 and "couldn't sign in afterwards (after the bootstrap)" in out,
                  (master, out[-500:]))
            master = open_master()
            r = subprocess.run([b.mc, "vm", "delete", "own2", "--confirm", "own2", "--remove-from-server"], env=env,
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            check("vm delete --remove-from-server with a master open: the check after the removal is a new connection, "
                  "which fails, so nothing here is deleted", master and r.returncode != 0 and "can't sign in" in r.stderr
                  and os.path.exists(os.path.join(b.co, "config", "vms", "own2.env")), (master, r.stderr[-400:]))
        finally:
            subprocess.run(["/usr/bin/ssh", "-F", cfg, "-O", "exit", "-p", str(port), f"{user}@{server}"],
                           capture_output=True, timeout=10)
            b.close()
    finally:
        try:
            os.kill(int(open(os.path.join(d, "sshd.pid")).read().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass


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
    rsrc = open(os.path.join(LAB, "lib", "remote.py")).read()
    check("remote.py gives the OCI adapter that lock (its name: oci-network; each provider's own: PROVIDER-network)",
          'kw["network_lock"] = lambda: network_lock(prov)' in rsrc and 'return mac_lock(f"{prov}-network")' in rsrc
          and "**provider_kw(prov, s))" in rsrc)
    # A delete with --network-too while another computer's group exists (it's being made): the network stays.
    rd = tempfile.mkdtemp(prefix="mcnet-")
    try:
        api = FakeOCI()
        remote_mod.API_FACTORY = lambda: api
        api.make_network()
        api.make_nsg("other", "internet", op="e" * 32)
        settings = {"VM_NAME": "box", "PLACE": "oci", "PROVIDER": "oci", "SIZE": "arm-1x6", "EGRESS": "internet",
                    "RUN_DIR": rd, "SSH_DIR": rd}
        os.environ["MCOMP_LOCK_DIR"] = os.path.join(rd, "locks")
        remote_mod.cmd_delete(settings, type("A", (), {"network_too": True})())
        check("vm delete --network-too while another computer is being made in the network (its group is there, no "
              "instance yet): the network stays", api.net is not None)
        api.drop("other")
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
    # The console capture read through to its end: OCI says what's left (opc-bytes-remaining) after each part.
    parts = ["boot...\n" + "x" * 100 + "\n[ OK ] Reached target multi-user.target\n",
             "y" * 100 + "\n[ 4109.69] systemd-shutdown[1]: Syncing filesystems\n",
             "[ 4109.70] reboot: Power down\n"]
    reads = []

    class Console:
        def capture_console_history(self, d):
            return types.SimpleNamespace(data=types.SimpleNamespace(id="h1"))

        def get_console_history(self, hid):
            return types.SimpleNamespace(data=types.SimpleNamespace(lifecycle_state="SUCCEEDED"))

        def get_console_history_content(self, hid, offset=0, length=None):
            i = len(reads)
            reads.append((offset, length))
            left = sum(len(x) for x in parts[i + 1:])
            return types.SimpleNamespace(data=parts[i], headers={"opc-bytes-remaining": str(left)})

        def delete_console_history(self, hid):
            pass
    sdk.compute = Console()
    sdk.oci.exceptions = types.SimpleNamespace(ServiceError=type("ServiceError", (Exception,), {}))
    tail = sdk.console_tail("i1")
    check("the console's last line is read from the end of the whole capture (every part OCI has, by offset), not "
          "from its first part (found by review)", tail and tail[-1].endswith("reboot: Power down") and len(reads) == 3
          and [o for o, _ in reads] == [0, oci.CONSOLE_CHUNK, 2 * oci.CONSOLE_CHUNK], (tail[-1:], reads))
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
                     review_remove_from_server, review_build_cleanup, review_resumed_failure, review_resume_setup,
                     review_start_owners,
                     review_create_journal):
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
    # Another session using it: refused unless --override, which it now reads (its owners check), as start's does.
    # Found by the strict parsers (round 3): --override was accepted here and never read.
    holder = subprocess.Popen(["sleep", "120"])
    od = os.path.join(b.co, ".run-vms", "own1", "owners")
    os.makedirs(od, exist_ok=True)
    claim = os.path.join(od, f"{holder.pid}.json")
    try:
        json.dump({"bridge_pid": holder.pid, "claude_pid": holder.pid, "cwd": "/", "started": time.time(),
                   "last_call": time.time(), "last_tool": "click"}, open(claim, "w"))
        r0 = b.run("--vm", "own1", "net", "egress", "off")
        r = b.run("--vm", "own1", "net", "egress", "off", "--override")
    finally:
        holder.kill()
        holder.wait()
        if os.path.exists(claim):
            os.remove(claim)
    check("net egress while another session uses the computer: refused without --override, past that check with it",
          r0.returncode != 0 and "another session is using" in r0.stderr and "another session is using" not in r.stderr,
          (r0.stderr[-200:], r.stderr[-200:]))
    # (round 4) Only --override is taken out before net's own grammar: another flag (before the subcommand too) is a
    # usage error, as on main, never dropped on the way to changing the setting.
    env_before = open(os.path.join(b.co, "config", "vms", "own1.env")).read()
    bad = [b.run("--vm", "own1", "net", *a) for a in (["--yes", "egress", "off"], ["--discard-unsynced", "egress", "off"],
                                                      ["egress", "--yes", "off"], ["--override", "--yes", "egress", "off"])]
    check("net --yes egress off (and --discard-unsynced, anywhere): a usage error, nothing asked, the setting as it was",
          all(x.returncode == 2 and "usage" in x.stderr and "doesn't answer" not in x.stderr for x in bad)
          and open(os.path.join(b.co, "config", "vms", "own1.env")).read() == env_before,
          [(x.returncode, x.stderr[-160:]) for x in bad])
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
            "    print('kind=made', 'arch=arm64', 'label=Oracle Cloud', 'idle_stop=15', 'egress=internet', 'outside=yes',"
            "          'billing_stopped=storage', 'soft_stop=api', 'provider=oci', 'resolvers=no', sep=chr(10))",
            "elif cmd == 'create':",
            "    rd = os.path.join(%r, '.run-vms', 'arm1')" % b.co,
            "    os.makedirs(rd, exist_ok=True)",
            "    json.dump({'instance': 'i-fake', 'host': '192.0.2.1', 'provider': 'oci'}, open(os.path.join(rd, 'remote.json'), 'w'))",
            ""]))
    os.chmod(fakepy, 0o755)
    for keep, want in (([], "abort --vm arm1 --op"), (["--keep-on-failure"], "abort --vm arm1 --op")):
        if os.path.exists(oplog):
            os.remove(oplog)
        rec = os.path.join(b.co, ".run-vms", "arm1", "remote.json")
        if os.path.exists(rec):
            os.remove(rec)
        r = subprocess.run([b.mc, "--vm", "arm1", "build", *keep], capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL,
                           env=b.env(OCI_PYTHON=fakepy, OCI_USER="u", OCI_API_KEY_PEM="k", MCOMP_REMOTE_SSH_WAIT="1"))
        ops = open(oplog).read().splitlines() if os.path.exists(oplog) else []
        cr = next((o for o in ops if o.startswith("create --vm arm1 --op ")), "")
        ab = next((o for o in ops if o.startswith(want)), "")
        if keep:
            eg = [i for i, o in enumerate(ops) if o.startswith("egress --vm arm1 --set internet")]
            ka = [i for i, o in enumerate(ops) if o.startswith("abort --vm arm1") and "--keep" in o]
            check("build --keep-on-failure: the setup's egress closed too, the provider's rules set back to EGRESS after "
                  "the instance is recorded as kept (found by review: a kept instance kept the setup's egress)",
                  eg and ka and ka[0] < eg[0] and "end the setup's downloads" in r.stdout + r.stderr, (ops, r.stdout[-300:]))
        check(f"build {' '.join(keep) or '(default)'} that fails after making the instance: abort with this build's own "
              "operation id (the one its create was given), " + ("--keep" if keep else "deleting only what it made")
              + ", and say what's left billing", r.returncode != 0 and cr and ab and ab.split()[4] == cr.split()[4]
              and ("--keep" in ab) == bool(keep) and not any(o.startswith("delete ") for o in ops)
              and ("nothing this build made is left billing" in r.stdout or "bills its disk" in r.stdout + r.stderr),
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
    check("a build killed (SIGTERM) after making the instance: what it made deleted, and said",
          pr.returncode != 0 and any(o.startswith("abort --vm arm1 --op ") and "--keep" not in o for o in ops)
          and "nothing this build made is left billing" in out, (pr.returncode, ops, out[-300:]))


def review_resumed_failure(c):
    """F5: a build that set up again an instance it found stopped (kept earlier) and failed: the setup's egress closed
    (the provider's rules back to EGRESS) and the instance stopped again; nothing deleted."""
    b, td, tmv = c["b"], c["td"], c["tmv"]
    fakepy = os.path.join(td, "fake-oci-python-2")
    oplog = os.path.join(td, "ops2.log")
    with open(fakepy, "w") as f:
        f.write("\n".join([
            "#!" + tmv.PY,
            "import json, os, sys",
            "cmd = sys.argv[2] if len(sys.argv) > 2 else ''",
            "open(%r, 'a').write(' '.join(sys.argv[2:]) + chr(10))" % oplog,
            "if cmd == 'plan':",
            "    print('kind=made', 'arch=arm64', 'label=Oracle Cloud', 'idle_stop=15', 'egress=off', 'outside=yes',"
            "          'billing_stopped=storage', 'soft_stop=api', 'provider=oci', 'resolvers=no', sep=chr(10))",
            "elif cmd == 'status':",
            "    print(json.dumps({'provider_state': 'STOPPED'}))",
            "elif cmd == 'egress':",
            "    print('off')",
            ""]))
    os.chmod(fakepy, 0o755)
    rd = os.path.join(b.co, ".run-vms", "arm1")
    os.makedirs(rd, exist_ok=True)
    json.dump({"instance": "i-kept", "host": "192.0.2.1", "provider": "oci", "op": "c" * 32, "kept": "x"},
              open(os.path.join(rd, "remote.json"), "w"))
    os.makedirs(os.path.join(b.home, ".ssh", "metacomputer-arm1"), exist_ok=True)
    open(os.path.join(b.home, ".ssh", "metacomputer-arm1", "known_hosts"), "w").write("mcomp-arm1 " + KEY + "\n")
    r = subprocess.run([b.mc, "--vm", "arm1", "build"], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                       env=b.env(OCI_PYTHON=fakepy, OCI_USER="u", OCI_API_KEY_PEM="k", MCOMP_REMOTE_SSH_WAIT="1"))
    ops = open(oplog).read().splitlines() if os.path.exists(oplog) else []
    check("a build that set up again an instance it found stopped, and failed: the provider's rules back to EGRESS=off "
          "and the instance stopped again; nothing deleted or aborted (found by review: resumed instances kept the "
          "setup's egress)", r.returncode != 0 and "wake --vm arm1" in ops and "egress --vm arm1 --set off" in ops
          and "sleep --vm arm1 --force" in ops and ops.index("egress --vm arm1 --set off") < ops.index("sleep --vm arm1 --force")
          and not any(o.startswith(("abort", "delete")) for o in ops), (ops, r.stdout[-400:]))
    os.remove(os.path.join(rd, "remote.json"))


def review_resume_setup(c):
    """N1, N5, N2: a build setting up again a kept instance records its identity (a launch whose answer was lost never
    got one) and opens its provider's rules for the setup's downloads, both closed again on failure; and past the
    build's own deadline (this Mac's clock), the build closes the setup's downloads and stops."""
    b, td, tmv = c["b"], c["td"], c["tmv"]
    fakepy = os.path.join(td, "fake-oci-python-3")
    oplog = os.path.join(td, "ops3.log")
    with open(fakepy, "w") as f:
        f.write("\n".join([
            "#!" + tmv.PY,
            "import json, os, sys",
            "cmd = sys.argv[2] if len(sys.argv) > 2 else ''",
            "open(%r, 'a').write(' '.join(sys.argv[2:]) + chr(10))" % oplog,
            "if cmd == 'plan':",
            "    print('kind=made', 'arch=arm64', 'label=Oracle Cloud', 'idle_stop=15', 'egress=off', 'outside=yes',"
            "          'billing_stopped=storage', 'soft_stop=api', 'provider=oci', 'resolvers=no', sep=chr(10))",
            "elif cmd == 'status':",
            "    print(json.dumps({'provider_state': 'STOPPED'}))",
            "elif cmd == 'egress':",
            "    print(sys.argv[-1] if '--set' in sys.argv else 'off')",
            ""]))
    os.chmod(fakepy, 0o755)
    fakessh = os.path.join(td, "fakessh3")
    os.makedirs(fakessh, exist_ok=True)
    with open(os.path.join(fakessh, "ssh"), "w") as f:      # it boots; it isn't finalized; nothing else answers
        f.write('#!/bin/sh\nfor a in "$@"; do last="$a"; done\ncase "$last" in true) exit 0 ;; '
                '"test -f /var/lib/metacomputer/finalized") exit 1 ;; *) exit 255 ;; esac\n')
    os.chmod(os.path.join(fakessh, "ssh"), 0o755)
    rd = os.path.join(b.co, ".run-vms", "arm1")
    os.makedirs(rd, exist_ok=True)
    os.makedirs(os.path.join(b.home, ".ssh", "metacomputer-arm1"), exist_ok=True)
    open(os.path.join(b.home, ".ssh", "metacomputer-arm1", "known_hosts"), "w").write("mcomp-arm1 " + KEY + "\n")
    for extra, label in (({}, "resume"), ({"MCOMP_SETUP_DEADLINE_S": "0"}, "deadline")):
        if os.path.exists(oplog):
            os.remove(oplog)
        json.dump({"instance": "i-kept", "host": "192.0.2.1", "provider": "oci", "op": "c" * 32, "kept": "x"},
                  open(os.path.join(rd, "remote.json"), "w"))
        r = subprocess.run([b.mc, "--vm", "arm1", "build"], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                           env=b.env(OCI_PYTHON=fakepy, OCI_USER="u", OCI_API_KEY_PEM="k",
                                     PATH=f"{fakessh}:{os.path.dirname(tmv.PY)}:/usr/bin:/bin:/usr/sbin:/sbin", **extra))
        ops = open(oplog).read().splitlines() if os.path.exists(oplog) else []
        out = r.stdout + r.stderr

        def at(o):
            return ops.index(o) if o in ops else -1
        if label == "resume":
            check("setting up again a kept instance: its identity recorded (N5: a lost launch's never was), its provider's "
                  "rules opened for the setup's downloads (N1: a failed setup had closed them), then on failure closed "
                  "again and the instance stopped again",
                  r.returncode != 0 and 0 <= at("wake --vm arm1") < at("identity --vm arm1 --write")
                  and at("identity --vm arm1 --write") < at("egress --vm arm1 --set internet") < at("egress --vm arm1 --set off")
                  < at("sleep --vm arm1 --force"), (ops, out[-400:]))
        else:
            check("past the build's own deadline (this Mac's clock, whatever the guest's says): it stops, and the setup's "
                  "downloads are closed outside (N2)", r.returncode != 0 and "may stay open 0 minutes at most" in out
                  and at("egress --vm arm1 --set internet") < at("egress --vm arm1 --set off"), (ops, out[-400:]))
    os.remove(os.path.join(rd, "remote.json"))
    rb = open(os.path.join(LAB, "scripts", "remote-build.sh")).read()
    fin = rb.index('gssh admin "sudo bash -s $ADMIN_USER')
    check("the setup never finishes without the computer's identity: checked just before finalize.sh (N5)",
          rb.rfind('[ -s "$SSH_DIR/computer-id" ] || die', 0, fin) > rb.rfind("setup_deadline_check", 0, fin) - 400 > 0
          and rb.rfind('[ -s "$SSH_DIR/computer-id" ] || die', 0, fin) > 0)


def review_start_owners(c):
    """Item 8, as re-checked: start may stop a computer elsewhere first (its console shows it powered off), so it
    checks who has the computer first, as stop does."""
    b = c["b"]
    holder = subprocess.Popen(["sleep", "120"])
    try:
        od = os.path.join(b.co, ".run-vms", "arm1", "owners")
        os.makedirs(od, exist_ok=True)
        f = os.path.join(od, f"{holder.pid}.json")
        json.dump({"bridge_pid": holder.pid, "claude_pid": holder.pid, "cwd": "/", "started": time.time(),
                   "last_call": time.time(), "last_tool": "click"}, open(f, "w"))
        r = b.run("--vm", "arm1", "start")
        check("start of a computer elsewhere that a session is using: refused before anything is asked of the provider",
              r.returncode != 0 and "another session is using" in r.stderr, r.stderr[-300:])
        os.remove(f)
    finally:
        holder.kill()
        holder.wait()


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
        # (its parser refuses it first now, as a value that starts with '-': lib/strictargs.py; target's own check of
        # the address stays behind it)
        check("...an address that could be an option: refused, nothing printed", r.returncode == 2 and r.stdout == ""
              and "--host needs a value, not an option" in r.stderr, r)
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
    r = subprocess.run([PY, rp, "probe", "--vm", "box", "--host", "192.0.2.1", "--timeout", "1", "--setting", "NOPE=1"],
                       capture_output=True, text=True, timeout=30)
    check("probe: settings it refuses (a transport setting no provider has) read as unknown, as an adapter's error "
          "does (review of item 5, C3)", r.returncode == 0 and r.stdout.strip() == "unknown", r)
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
        # While the user has it (a handoff), an agent's `build` of it waits: it sets the computer up again over SSH.
        od = os.path.join(b.co, ".run-vms", "own1", "owners")
        os.makedirs(od, mode=0o700, exist_ok=True)
        now = time.time()
        hf = os.open(os.path.join(od, "handoff.state"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(hf, "w") as fh:
            json.dump({"v": 1, "id": "h-dddddddddd01", "status": "open", "why": "login", "say": "x", "opened": now,
                       "deadline": now + 300, "timeout_s": 300, "cwd": td, "claude_pid": None}, fh)
        sd = os.path.join(td, "build-held-ssh")
        os.makedirs(sd)
        slog = os.path.join(td, "build-held-ssh.log")
        with open(os.path.join(sd, "ssh"), "w") as fh:
            fh.write(f'#!/bin/sh\necho "$*" >> "{slog}"\nexit 0\n')
        os.chmod(os.path.join(sd, "ssh"), 0o755)
        r = b.run("--vm", "own1", "build", env=b.env(PATH=sd + os.pathsep + os.environ.get("PATH", "")))
        check("an agent's `build` of a computer elsewhere while the user has it (a handoff): held (exit 4), nothing "
              "reaches it", r.returncode == 4 and "the user has the computer" in r.stderr and not os.path.exists(slog),
              (r.returncode, r.stderr[-300:]))
        # ...and so is scripts/remote-build.sh run on its own, as a program would (it checks itself)
        r = subprocess.run([os.path.join(b.co, "scripts", "remote-build.sh")], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, env=b.env(MCOMP_VM="own1", MCOMP_REMOTE_SSH_WAIT="1",
                                                               PATH=sd + os.pathsep + os.environ.get("PATH", "")))
        check("scripts/remote-build.sh run on its own while the user has it: held (exit 4), nothing reaches it",
              r.returncode == 4 and "the user has the computer" in r.stderr and not os.path.exists(slog),
              (r.returncode, r.stderr[-300:]))
        os.remove(os.path.join(od, "handoff.state"))
        r = b.run("vm", "create", "arm1", "--on", "oci", "--size", "arm-1x6", "--egress", "off", "--no-build", "--yes")
        text = open(os.path.join(b.co, "config", "vms", "arm1.env")).read()
        check("vm create --on oci: place, provider, size, egress and the idle stop (15 min)",
              r.returncode == 0 and all(x in text.splitlines() for x in ("PLACE=oci", "PROVIDER=oci", "SIZE=arm-1x6",
                                                                         "EGRESS=off", "IDLE_STOP=15")), (r.stderr, text))
        for args, want in ((["--on", "server", "--user", "owner"], "--host ADDRESS"),
                           (["--on", "server", "--host", "-oProxyCommand=x", "--user", "o"], "--host needs a value, not an option"),
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
        # The hourly providers: their defaults, sizes and regions checked, the region written.
        for name, args, want in (("hz1", ["--on", "hetzner"], ("PLACE=hetzner", "PROVIDER=hetzner", "SIZE=cax11",
                                                               "REGION=fsn1", "IDLE_STOP=15", "EGRESS=internet")),
                                 ("do1", ["--on", "digitalocean", "--size", "s-4vcpu-8gb", "--region", "sfo2", "--egress", "off"],
                                  ("PLACE=digitalocean", "PROVIDER=digitalocean", "SIZE=s-4vcpu-8gb", "REGION=sfo2", "EGRESS=off")),
                                 ("vu1", ["--on", "vultr", "--idle-stop", "off"], ("PLACE=vultr", "PROVIDER=vultr",
                                                                                   "SIZE=vc2-2c-4gb", "REGION=ewr", "IDLE_STOP=off"))):
            r = b.run("vm", "create", name, *args, "--no-build", "--yes")
            f = os.path.join(b.co, "config", "vms", f"{name}.env")
            text = open(f).read() if os.path.exists(f) else ""
            check(f"vm create {' '.join(args)}: its place, provider, size and region (defaults filled in)",
                  r.returncode == 0 and all(x in text.splitlines() for x in want), (r.stderr[-300:], text))
        for args, want in ((["--on", "hetzner", "--size", "huge"], "no size"),
                           (["--on", "vultr", "--region", "x/y"], "region"),
                           (["--on", "digitalocean", "--host", "192.0.2.3"], "your own server"),
                           (["--on", "oci", "--region", "fsn1"], "OCI_REGION"),
                           (["--on", "server", "--host", "192.0.2.3", "--user", "o", "--region", "fsn1"], "--region"),
                           (["--region", "fsn1"], "elsewhere")):
            r = b.run("vm", "create", "bad2", *args, "--no-build", "--yes")
            check(f"vm create {' '.join(args)[:44]}: refused ({want})", r.returncode != 0 and want in r.stderr
                  and not os.path.exists(os.path.join(b.co, "config", "vms", "bad2.env")), r.stderr[-300:])
        r = b.run("vm", "create", "hetzner", "--on", "hetzner", "--no-build", "--yes")
        check("a computer can't be called hetzner (a place's name)", r.returncode != 0 and "place's name" in r.stderr, r.stderr)
        r = b.run("--vm", "hz1", "stop", "--override")
        check("stop on Hetzner Cloud without its token: refused, naming the variable, and saying a stopped server is billed "
              "in full", r.returncode != 0 and "HCLOUD_TOKEN" in r.stderr and "only deleting it" in r.stderr, r.stderr[-400:])
        r = b.run("vm", "delete", "do1", "--confirm", "do1")
        check("vm delete of a DigitalOcean computer without its token: refused, naming it, nothing deleted",
              r.returncode != 0 and "DIGITALOCEAN_TOKEN" in r.stderr and os.path.exists(os.path.join(b.co, "config", "vms",
                                                                                                    "do1.env")), r.stderr[-300:])
        script = ('MCOMP_VM_ARG=hz1; . "$1/scripts/lib.sh"; env | grep -cE "^(HCLOUD_TOKEN|DIGITALOCEAN_TOKEN|VULTR_API_KEY)=" || true; '
                  '( mc_export_provider_env; env | grep -E "^(HCLOUD_TOKEN|DIGITALOCEAN_TOKEN|VULTR_API_KEY)=" | cut -d= -f1 | sort )')
        r = subprocess.run(["/bin/bash", "-c", script, "_", b.co], capture_output=True, text=True, timeout=60,
                           env=b.env(HCLOUD_TOKEN="t1", DIGITALOCEAN_TOKEN="t2", VULTR_API_KEY="t3"))
        check("the three API tokens in the environment: not exported to what metacomputer starts, but handed to "
              "lib/remote.py", r.stdout.split() == ["0", "DIGITALOCEAN_TOKEN", "HCLOUD_TOKEN", "VULTR_API_KEY"],
              (r.stdout, r.stderr))
        r = subprocess.run(["/bin/bash", "-c", 'MCOMP_VM_ARG=vu1; . "$1/scripts/lib.sh"; remote_can_ask && echo yes || echo no',
                            "_", b.co], capture_output=True, text=True, timeout=60, env=b.env(VULTR_API_KEY="t3"))
        r2 = subprocess.run(["/bin/bash", "-c", 'MCOMP_VM_ARG=vu1; . "$1/scripts/lib.sh"; remote_can_ask && echo yes || echo no',
                             "_", b.co], capture_output=True, text=True, timeout=60, env=b.env(HCLOUD_TOKEN="t1"))
        check("remote_can_ask: a provider's own token, and no other's", r.stdout.strip() == "yes" and r2.stdout.strip() == "no",
              (r.stdout, r2.stdout, r.stderr))
        r = b.run("default", "hz1")
        rj = b.run("default", "hz1", "--json")
        try:
            dj = json.loads(rj.stdout)
        except ValueError:
            dj = {}
        check("default hz1: says where it is (Hetzner Cloud), not this Mac (found live: it said this Mac for any "
              "computer)", r.returncode == 0 and "hz1 (Hetzner Cloud, config/vms/hz1.env) is now your default computer"
              in r.stdout and dj.get("place") == "hetzner" and dj.get("where") == "Hetzner Cloud", (r.stdout, rj.stdout))
        os.remove(os.path.join(b.co, "config", "default-vm"))
        for name in ("hz1", "do1", "vu1"):
            os.remove(os.path.join(b.co, "config", "vms", f"{name}.env"))
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


VT_STUBS = r'''
set -u
die() { echo "DIE: $*" >&2; exit 1; }
ok() { echo "OK: $*"; }; warn() { echo "WARN: $*" >&2; }; say() { echo "SAY: $*"; }
vm_running() { return 0; }; ssh_base() { SSH_ARGS=(-p 2222 box); }; mc_tilde() { printf '%s' "$1"; }
MC_REMOTE=1 TRANSPORT=${TRANSPORT_KIND:-ssh} VM_NAME=box VNC_PORT=$PORT
'''

# The tunnel's ssh, standing in: it says something while it connects; with -f (`ssh -f`) it leaves a background half
# that keeps its output open (and, with FLOOD, prints 5 MB) and exits 0; without -f (a provider's tunnel) it listens on
# the port itself, as ssh's forward does, and stays.
VT_SSH = r'''#!/bin/bash
echo "ssh $*" >> "$CALLS"
printf "Warning: the tunnel \033[31msays\033[0m this\n" >&2
if [ "${SSH_FAILS:-0}" = 1 ]; then echo "Permission denied (publickey)." >&2; exit 255; fi
case " $* " in
  *" -f "*) if [ "${FLOOD:-0}" = 1 ]; then ( head -c 5000000 /dev/zero | tr '\0' 'x'; echo; echo FLOOD-END; sleep 2 ) &
            elif [ "${ENDLESS:-0}" = 1 ]; then ( cat /dev/zero & c=$!; sleep 20; kill $c ) &
            else ( sleep 15 ) & fi
            exit 0 ;;
  *) exec "$PY" -c 'import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(1); time.sleep(15)' "$PORT" ;;
esac
'''


def view_tunnel_pipes():
    """`view --tunnel` to a computer elsewhere, its output read through pipes by a program (an agent's tool call, a
    script): it returns as soon as the tunnel is up, not when the tunnel closes (`ssh -f` stays in the background with
    the descriptors it was started with). What ssh says goes to a private log (0600 in a 0700 folder) that never grows
    past its cap, however much the VM's end makes ssh print (lib/tunnellog.py)."""
    import re as _re
    import socket as _socket
    import time as _time
    cap, cut = 1 << 20, b"left out of this log"     # lib/tunnellog.py's CAP and the words of its CUT line
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    m = _re.search(r"^view_tunnel\(\) \{.*?^\}$", src, _re.S | _re.M)
    if not m:
        check("view --tunnel: view_tunnel() found in bin/metacomputer", False)
        return
    td = tempfile.mkdtemp(prefix="mcvt-", dir="/tmp")
    try:
        bindir, logdir = os.path.join(td, "bin"), os.path.join(td, "logs")
        os.makedirs(bindir)
        os.makedirs(logdir)
        with open(os.path.join(bindir, "ssh"), "w") as f:
            f.write(VT_SSH)
        os.chmod(os.path.join(bindir, "ssh"), 0o755)
        log = os.path.join(logdir, "tunnel", "view-tunnel.log")

        def free_port():
            with _socket.socket() as so:
                so.bind(("127.0.0.1", 0))
                return so.getsockname()[1]

        def run(case, **extra):
            calls = os.path.join(td, f"calls-{case}.log")
            env = dict(os.environ, PATH=bindir + ":" + os.environ.get("PATH", ""), CALLS=calls, PORT=str(free_port()),
                       LOG_DIR=logdir, LAB_ROOT=LAB, PY=sys.executable, **extra)
            t0 = _time.monotonic()
            try:
                r = subprocess.run(["bash", "-c", VT_STUBS + m.group(0) + "\nview_tunnel"], capture_output=True,
                                   text=True, env=env, timeout=10)
                return (r.returncode, r.stdout, r.stderr, round(_time.monotonic() - t0, 1),
                        open(calls).read() if os.path.exists(calls) else "")
            except subprocess.TimeoutExpired:
                return ("timeout", "", "", round(_time.monotonic() - t0, 1), "")

        rc, out, err, secs, calls = res = run("up")
        check("view --tunnel, its output read through pipes: it returns once the tunnel is up (not when the background "
              "ssh ends)", rc == 0 and secs < 6 and "ssh -f -T -L" in calls and "live view tunnel" in out, res)
        check("...what ssh said while it connected is still shown, on stderr, escaped (termsafe), and kept in its log",
              "Warning: the tunnel" in err and "\x1b[31m" not in err
              and "Warning: the tunnel" in (open(log).read() if os.path.exists(log) else ""), (err, os.path.exists(log)))
        modes = [oct(os.stat(x).st_mode & 0o777) if os.path.exists(x) else None for x in (log, os.path.dirname(log))]
        check("...its log is private: the file 0600, its folder 0700", modes == ["0o600", "0o700"], modes)
        rc, out, err, secs, calls = res = run("refused", SSH_FAILS="1")
        check("...a tunnel that can't open: refused promptly, saying what ssh said and where its log is",
              rc == 1 and secs < 6 and "Permission denied" in err and "view-tunnel.log" in err
              and "couldn't open the live view's SSH tunnel" in err, res)
        # The VM's end makes ssh print without end, after it went to the background: the log stops at its cap.
        rc, out, err, secs, calls = res = run("flood", FLOOD="1")
        end = _time.monotonic() + 30
        while _time.monotonic() < end and os.path.exists(log) and cut not in open(log, "rb").read():
            _time.sleep(0.2)
        _time.sleep(1.5)                     # the background half goes on printing; nothing more may reach the log
        size = os.path.getsize(log) if os.path.exists(log) else -1
        check("...the VM's end making ssh print 5 MB after it went to the background: the command still returns at "
              "once, and its log stops at its cap (1 MB, the line saying the rest was left out included)",
              rc == 0 and secs < 6 and 0 < size <= cap and cut in open(log, "rb").read()
              and b"FLOOD-END" not in open(log, "rb").read(), (res[:4], size))
        # (round 3) The background half printing without pause as ssh goes to the background: what it wrote before is
        # read for a moment at most, then the command returns; the reader goes on draining after.
        rc, out, err, secs, calls = res = run("endless", ENDLESS="1")
        check("...ssh's background half printing without pause: the command still returns at once (what came before "
              "is read for a moment at most)", rc == 0 and secs < 6, res[:4])
        # The same where the pipe never once runs dry (a pipe that's always readable, fed without end: the race the
        # stand-in above can lose): the reader still says the tunnel is up within moments, then drains on.
        import threading as _threading
        sys.path.insert(0, os.path.join(LAB, "lib"))
        import tunnellog as _tl
        r, w = os.pipe()
        sr, sw = os.pipe()
        feed_fd, stop = os.dup(w), _threading.Event()

        def feed():
            while not stop.is_set():
                try:
                    os.write(feed_fd, b"z" * 65536)
                except OSError:
                    return
        logfd = os.open(os.path.join(td, "always-readable.log"), os.O_WRONLY | os.O_CREAT, 0o600)
        real_select = _tl.select.select
        _tl.select.select = lambda rl, wl, xl, timeout=None: (list(rl), [], [])
        feeder = _threading.Thread(target=feed, daemon=True)
        feeder.start()
        reader = _threading.Thread(target=_tl.reader, args=(["/usr/bin/true"], logfd, 1 << 20, "fork", 0, 15, r, w, sw),
                                   daemon=True)
        t0 = _time.monotonic()
        reader.start()
        told = real_select([sr], [], [], 5)[0]
        secs = round(_time.monotonic() - t0, 1)
        status = os.read(sr, 16).split(b"\n")[0] if told else b""
        stop.set()
        os.close(feed_fd)
        reader.join(10)
        _tl.select.select = real_select
        for fd in (sr, logfd):
            os.close(fd)
        check("...a pipe that never runs dry: the reader still says the tunnel is up within moments, then drains on",
              told and status == b"0" and secs < 3, (secs, status))
        # (round 4) A cap smaller than the line saying the rest was left out: refused (the command line before anything
        # runs, and the log itself), so no cap is ever overrun; at exactly that size the file holds the line and no more.
        small = os.path.join(td, "small-cap", "t.log")
        ran = os.path.join(td, "small-cap-ran")
        r = subprocess.run([PY, os.path.join(LAB, "lib", "tunnellog.py"), "--log", small, "--cap", "16", "--fork", "--",
                            "/usr/bin/touch", ran], capture_output=True, text=True, timeout=30)
        r2 = subprocess.run([PY, os.path.join(LAB, "lib", "tunnellog.py"), "--log", small, "--cap", "4096", "--cap",
                             "8192", "--fork", "--", "/usr/bin/touch", ran], capture_output=True, text=True, timeout=30)
        try:
            _tl.Log(os.open(os.devnull, os.O_WRONLY), 16)
            small_log = "accepted"
        except ValueError:
            small_log = "refused"
        exact = os.path.join(td, "exact-cap.log")
        efd = os.open(exact, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        el = _tl.Log(efd, len(_tl.CUT))
        for _ in range(3):
            el.write(b"y" * 1000)
        os.close(efd)
        check("...a cap smaller than the left-out line (--cap 16) is refused before anything runs, as is a second --cap; "
              "the log refuses one too, and at exactly that size holds the line and no more",
              r.returncode == 2 and "--cap" in r.stderr and r2.returncode == 2 and not os.path.exists(ran)
              and not os.path.exists(small) and small_log == "refused"
              and open(exact, "rb").read() == _tl.CUT, (r.returncode, r.stderr[-200:], r2.returncode, small_log,
                                                        os.path.getsize(exact)))
        # A log that's a link, or shared with another file (a hard link): refused, nothing written through it.
        if os.path.lexists(log):
            os.remove(log)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        target = os.path.join(td, "elsewhere")
        open(target, "w").write("keep me\n")
        os.symlink(target, log)
        rc, out, err, secs, calls = res = run("link")
        check("...a log that's a link: refused, nothing written through it, no ssh started", rc == 1 and not calls
              and open(target).read() == "keep me\n" and "link" in err, res)
        os.remove(log)
        os.link(target, log)
        rc, out, err, secs, calls = res = run("hardlink")
        check("...a log that's another file's hard link: refused, that file untouched", rc == 1 and not calls
              and open(target).read() == "keep me\n", res)
        os.remove(log)
        os.chmod(os.path.dirname(log), 0o755)
        rc, out, err, secs, calls = res = run("widefolder")
        check("...a log folder of this account's that others could read is made 0700 first",
              rc == 0 and oct(os.stat(os.path.dirname(log)).st_mode & 0o777) == "0o700", (res, oct(os.stat(os.path.dirname(log)).st_mode)))
        # A provider's tunnel (Fly Sprites): ssh stays in the helper's session (lib/proxywrap.py ends the tunnel when
        # ssh goes), and the command returns once ssh listens on the port.
        rc, out, err, secs, calls = res = run("listen", TRANSPORT_KIND="sprite")
        check("...a provider's tunnel (Fly Sprites): ssh without -f, up once it listens on the port; the command "
              "returns at once, and what ssh said is shown", rc == 0 and secs < 8 and "ssh -T -o ExitOnForwardFailure=yes"
              in calls and " -f " not in calls and "Warning: the tunnel" in err, res)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main():
    transports()
    proxywrap()
    guest_side()
    firewall()
    oci_driver()
    vps_drivers()
    vps_journal()
    audit_fixes()
    network_race()
    credentials_children()
    owner_ssh_options()
    journal_r4()
    abort_scoped()
    oci_groups()
    cloud_lock()
    remote_helpers()
    view_tunnel_pipes()
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
    td = tempfile.mkdtemp(prefix="mcown-", dir="/tmp")     # short: ssh's control socket paths are limited
    try:
        owner_master(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
