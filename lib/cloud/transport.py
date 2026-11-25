"""How SSH reaches a computer that isn't a Lume VM on this Mac: the one place that decides it, for every command
that talks to the computer (the hub's driver connection, sh, put and get, record, the live view's tunnel, the
vault's fills and API tokens).

A computer on this Mac is reached on its own port on this Mac's loopback (scripts/lib.sh ssh_base, unchanged).
Any other computer is reached by SSH to an endpoint, opened in the way its settings name (TRANSPORT):

    ssh       straight to SSH_HOST:SSH_HOST_PORT: an Ubuntu VM with an address of its own (your own server, one
              a provider made). No program runs on this Mac besides ssh.

A provider whose computers have no address of their own (its own tunnel program, its manifest's transport) adds its
opener with register(): it returns the program, as an argument list, that carries one SSH connection on its standard input and
output; ssh runs it as its ProxyCommand through lib/proxywrap.py, which ends it (SIGTERM) when ssh goes, so a
tunnel never outlives its connection and keeps the computer awake and billing.

Whatever opens the connection, the computer's host key is pinned under one name, mcomp-NAME (HostKeyAlias): never
under an address that can change or a name DNS answers. The key comes from the computer's own setup (generated on
this Mac and given to cloud-init, or read over your own authenticated connection to your server), never from a
first connection.

    endpoint(settings, state=None)  -> {"host", "port", "alias", "proxy"} (proxy: None or an argument list)
    ssh_target(endpoint, user)      -> the ssh options and destination for that endpoint, after ssh's own options

Nothing here makes a connection or starts a program; settings come from the computer's settings file
(config/vms/NAME.env) and state from what its provider made (lib/remote.py keeps it in RUN_DIR/remote.json)."""
import ipaddress
import re
import shlex

from .adapter import AdapterError, check_name

HOST_NAME = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
ALIAS_PREFIX = "mcomp-"


class TransportError(AdapterError):
    """Settings that don't say clearly how to reach a computer; str() says what's wrong."""


def check_host(host):
    """An IPv4 or IPv6 address, or a DNS name: nothing else (no user@, no port, no option-like text)."""
    if not isinstance(host, str) or not host:
        raise TransportError("SSH_HOST is empty: the computer's address (an IP address or a DNS name)")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host.startswith("-") or not HOST_NAME.fullmatch(host) or host.replace(".", "").isdigit():
            raise TransportError(f"SSH_HOST {host!r} isn't an IP address or a DNS name") from None
        return host
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        raise TransportError(f"SSH_HOST {host} isn't a computer's own address (loopback, link-local or multicast)")
    return str(ip)


def check_port(port):
    try:
        p = int(str(port))
    except ValueError:
        raise TransportError(f"SSH_HOST_PORT {port!r} isn't a port") from None
    if not 1 <= p <= 65535:
        raise TransportError(f"SSH_HOST_PORT {p} isn't a port (1-65535)")
    return p


def alias(name):
    """The name a computer's host key is pinned under (known_hosts and the vault's binding)."""
    return ALIAS_PREFIX + check_name(name)


def _ssh(settings, state):
    host = (state or {}).get("host") or settings.get("SSH_HOST", "")
    return {"host": check_host(host), "port": check_port(settings.get("SSH_HOST_PORT") or 22), "proxy": None}


# TRANSPORT -> opener(settings, state) -> {"host", "port", "proxy"}. A provider's adapter registers its own here.
OPENERS = {"ssh": _ssh}


def register(name, opener):
    """Add a way to open a computer's SSH connection (a provider's tunnel). opener(settings, state) returns
    {"host": what ssh connects to (any name, when a proxy carries it), "port": int, "proxy": [program, args...]}."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name or ""):
        raise ValueError(f"not a transport name: {name!r}")
    if name in OPENERS:
        raise ValueError(f"the transport {name} is registered already")
    OPENERS[name] = opener


def endpoint(settings, state=None):
    """Where SSH reaches the computer these settings are for. TransportError when that isn't clear."""
    name = check_name(settings.get("VM_NAME", ""))
    how = settings.get("TRANSPORT") or "ssh"
    opener = OPENERS.get(how)
    if opener is None:
        raise TransportError(f"TRANSPORT={how} isn't a way metacomputer opens a connection here ({', '.join(sorted(OPENERS))})")
    ep = opener(settings, state)
    proxy = ep.get("proxy")
    if proxy is not None and (not isinstance(proxy, list) or not proxy or not all(isinstance(x, str) and x for x in proxy)):
        raise TransportError(f"the {how} transport gave no program to run")
    return {"host": ep["host"], "port": check_port(ep["port"]), "alias": alias(name), "proxy": proxy,
            "transport": how}


def proxy_command(ep, wrapper):
    """ssh's ProxyCommand for the endpoint: `none`, or the proxy run through the wrapper (lib/proxywrap.py, as an
    argument list) that ends it when ssh goes. Every word is quoted: ssh hands the line to a shell."""
    if ep["proxy"] is None:
        return "none"
    return " ".join(shlex.quote(x) for x in [*wrapper, "--", *ep["proxy"]])


def ssh_target(ep, user, wrapper=("python3", "proxywrap.py")):
    """The options and destination for the endpoint, for after ssh's own (-F none, keys, known_hosts...): the alias
    the host key is pinned under, the proxy (or none), the port and user@host. No address check (CheckHostIP=no):
    the pinned key is the identity."""
    if not USER.fullmatch(user or ""):
        raise TransportError(f"{user!r} isn't an account name")
    return ["-o", f"HostKeyAlias={ep['alias']}", "-o", "CheckHostIP=no",
            "-o", f"ProxyCommand={proxy_command(ep, list(wrapper))}", "-o", "ProxyJump=none",
            "-p", str(ep["port"]), f"{user}@{ep['host']}"]


def known_hosts_line(ep, host_key):
    """The known_hosts line that pins `host_key` ("TYPE BASE64") for the endpoint."""
    parts = (host_key or "").split()
    if len(parts) < 2 or not re.fullmatch(r"[a-z0-9-]{3,40}", parts[0]) or not re.fullmatch(r"[A-Za-z0-9+/=]{20,2000}", parts[1]):
        raise TransportError("that isn't an SSH host key ('ssh-ed25519 AAAA...')")
    return f"{ep['alias']} {parts[0]} {parts[1]}"
