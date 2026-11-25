"""What a VM reached over the internet is given before metacomputer first signs in: the cloud-init user-data for a
VM a provider makes, and the key lists every such VM gets (your own server gets the same lists through
provisioning/remote/bootstrap.sh, run over your own SSH connection).

The user-data does three things and no more: it pins the SSH host key generated on this Mac for this computer (so
the first connection is already checked: no trust on first use), it makes no default account (the provider's usual
`ubuntu` user with passwordless sudo never exists), and it runs provisioning/remote/bootstrap.sh, which loads the
guest firewall (guest-firewall, with the computer's EGRESS) and then makes the two accounts metacomputer signs in
with. Everything else (the desktop, Cua Driver, the firewall) is the setup over
SSH that your own server gets too (lib/remote.py), so both are set up the same way.

The host's private key is in the user-data, which the provider keeps with the instance and serves to it through
its metadata service: the guest firewall refuses that service to every account once the setup has run, cloud-init
is turned off (finalize.sh), and its cached copy is deleted. Pure functions: nothing here touches a file, the
network or a program."""
import base64
import gzip
import json
import re

from .adapter import check_name

MAX_USER_DATA = 32000           # bytes, base64-encoded: the largest instance metadata the big providers all take
KEY_LINE = re.compile(r"(?:[a-z0-9-]+(?:=\"[^\"\n]*\")?(?:,[a-z0-9-]+(?:=\"[^\"\n]*\")?)* )?"
                      r"(ssh-ed25519|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]{20,2000}(?: \S{1,100})?")


def key_lists(keys):
    """The authorized_keys lines for the two accounts, from the public keys made for this computer (a dict: ops,
    mcp, view, admin -> "ssh-ed25519 AAAA... comment"). Each key may do only its own job:
        ops    the desktop user's shell (sh, put, get, record): a PTY, no forwarding
        mcp    Cua Driver's MCP server and nothing else (driver-mcp is forced)
        view   one local forward, to the live view on 127.0.0.1:5900, and a short sleep (no shell)
        admin  maintenance (cuaadmin): a PTY, no forwarding"""
    def one(k):
        line = " ".join((keys.get(k) or "").split()[:2])
        if not KEY_LINE.fullmatch(line):
            raise ValueError(f"the {k} key isn't an SSH public key")
        return line
    guest = "\n".join([
        "restrict,pty " + one("ops"),
        'restrict,command="/opt/metacomputer/bin/driver-mcp" ' + one("mcp"),
        'restrict,port-forwarding,permitopen="127.0.0.1:5900",command="/usr/bin/sleep 20" ' + one("view"),
    ]) + "\n"
    admin = "restrict,pty " + one("admin") + "\n"
    return guest, admin


def _q(s):
    return json.dumps(s)        # a JSON string is a valid YAML scalar


def user_data(name, host_private, host_public, keys, bootstrap, firewall, egress="internet", hostname="metacomputer",
              timezone=None):
    """The #cloud-config for a VM a provider makes (module docstring). ValueError when something isn't what it
    should be (a key, the host name), or when it's larger than a provider takes."""
    check_name(name)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname or ""):
        raise ValueError(f"{hostname!r} isn't a host name")
    if "OPENSSH PRIVATE KEY" not in (host_private or "") or not (host_public or "").startswith("ssh-ed25519 "):
        raise ValueError("the host key isn't an ed25519 key pair")
    if timezone is not None and not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", timezone):
        raise ValueError(f"{timezone!r} isn't a time zone")
    if egress not in ("internet", "off"):
        raise ValueError(f"{egress!r} isn't an EGRESS setting")
    if not (firewall or "").startswith("#!/bin/sh"):
        raise ValueError("the guest firewall isn't the script it should be")
    guest, admin = key_lists(keys)
    files = {"keys-guest": guest, "keys-admin": admin, "egress": egress + "\n", "guest-firewall": firewall,
             "bootstrap.sh": bootstrap}
    lines = ["#cloud-config",
             f"# metacomputer {name}: the host key pinned on the Mac, no default account, then bootstrap.sh",
             f"hostname: {hostname}", "manage_etc_hosts: true", "ssh_pwauth: false", "disable_root: true",
             "ssh_deletekeys: true", "ssh_genkeytypes: [ed25519]", "users: []", "ssh_keys:", "  ed25519_private: |"]
    lines += [f"    {ln}" for ln in host_private.strip().splitlines()]
    lines += [f"  ed25519_public: {_q(' '.join(host_public.split()[:2]))}"]
    if timezone:
        lines += [f"timezone: {_q(timezone)}"]
    lines += ["write_files:"]
    for fname, body in files.items():
        lines += [f"  - path: /run/metacomputer-bootstrap/{fname}", "    owner: root:root",
                  f"    permissions: {_q('0700' if fname in ('bootstrap.sh', 'guest-firewall') else '0600')}",
                  "    encoding: gz+b64",
                  f"    content: {base64.b64encode(gzip.compress(body.encode(), mtime=0)).decode()}"]
    lines += ["runcmd:",
              '  - [bash, /run/metacomputer-bootstrap/bootstrap.sh, /run/metacomputer-bootstrap, "", "22"]',
              '  - [rm, -rf, /run/metacomputer-bootstrap]']
    text = "\n".join(lines) + "\n"
    if len(base64.b64encode(text.encode())) > MAX_USER_DATA:
        raise ValueError(f"the user-data is {len(text)} bytes: more than a provider takes")
    return text
