#!/usr/bin/env python3
"""A computer that isn't a Lume VM on this Mac: an Ubuntu 24.04 VM reached over SSH (your own server, or one a
provider made). bin/metacomputer and scripts/remote-build.sh call this for what needs the provider or the
computer's record; everything that talks to the computer itself goes through scripts/lib.sh's ssh_base, whose
remote half asks `target` here (lib/cloud/transport.py) once per command.

    remote.py target --vm NAME --run-dir DIR --role ROLE --user USER --transport T --host H --port P
        the ssh options and destination for that computer, NUL-separated (scripts/lib.sh ssh_base)
    remote.py create --vm NAME                 a provider's computer: its instance (and the provider's network)
    remote.py user-data --vm NAME              print the cloud-init user-data a new instance gets
    remote.py wake | sleep | delete --vm NAME  through the provider (your own server: refused, with why)
    remote.py status --vm NAME [--json]        the provider's view of it (and the address, when it runs)
    remote.py state --vm NAME                  one word: awake, stopped, starting, stopping, absent, unknown
    remote.py rates --vm NAME [--json]         what it costs awake and asleep
    remote.py cloud status|doctor|setup|teardown PROVIDER [--json]
        what metacomputer has made at a provider (tagged), checks, the provider's network, and removing it

The computer's settings are its settings file (config/vms/NAME.env, read through lib/labconfig.py, as every
helper reads them); what its provider made (the instance, its address) is RUN_DIR/remote.json, written only here,
0600. Provider credentials come only from the environment (lib/cloud/oci.py). Nothing here prints one."""
import argparse
import datetime
import json
import os
import sys
import tempfile
import time

LIB = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(LIB)
sys.path.insert(0, LIB)
import cloud  # noqa: E402
from cloud import guest as cloud_guest  # noqa: E402
from cloud import transport  # noqa: E402
from cloud.adapter import AdapterError  # noqa: E402

STATE_FILE = "remote.json"
ROLES = ("admin", "ops", "mcp", "view")
WRAPPER = (sys.executable, os.path.join(LIB, "proxywrap.py"))


class Refused(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --- the computer's record ---------------------------------------------------------------------------
def settings_for(vm):
    """The settings of the computer `vm` (its config/vms/NAME.env), as every helper reads them."""
    import labconfig
    env = dict(os.environ)
    env["MCOMP_VM"] = vm
    env.pop("MCOMP_CONFIG", None)
    env.pop("MCOMP_NO_DEFAULT_VM", None)
    s = labconfig.load(env)
    if s.get("VM_NAME") != vm:
        raise Refused(f"the settings for {vm} name {s.get('VM_NAME')!r}")
    if (s.get("PLACE") or "mac") == "mac":
        raise Refused(f"{vm} is a VM on this Mac, not one reached over SSH")
    # Its folders are fixed by its name (scripts/lib.sh mc_vm_default; a config/vms/ file can't set them). When
    # metacomputer runs this for that computer, its own values come along and must be the same folders.
    fixed = {"SSH_DIR": os.path.join(os.environ.get("HOME", os.path.expanduser("~")), ".ssh", f"metacomputer-{vm}"),
             "RUN_DIR": os.path.join(LAB, ".run-vms", vm), "LOG_DIR": os.path.join(LAB, "logs-vms", vm)}
    if os.environ.get("MCOMP_VM_NAME") == vm:
        for k, e in (("SSH_DIR", "MCOMP_SSH_DIR"), ("RUN_DIR", "MCOMP_RUN_DIR"), ("LOG_DIR", "MCOMP_LOG_DIR")):
            v = os.environ.get(e)
            if v and os.path.realpath(v) != os.path.realpath(fixed[k]):
                raise Refused(f"{e} ({v}) isn't {vm}'s own folder ({fixed[k]})")
    s.update(fixed)
    return s


def run_dir(s):
    return s.get("RUN_DIR") or os.path.join(LAB, ".run-vms", s["VM_NAME"])


def read_state(rd):
    p = os.path.join(rd, STATE_FILE)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Refused(f"can't read {p}: {e.strerror}") from None
    with os.fdopen(fd) as f:
        data = f.read(65536)
    try:
        d = json.loads(data)
    except ValueError:
        raise Refused(f"{p} isn't the record this program writes; look at it, then remove it") from None
    if not isinstance(d, dict):
        raise Refused(f"{p} isn't the record this program writes")
    return d


def write_state(rd, d):
    os.makedirs(rd, mode=0o700, exist_ok=True)
    p = os.path.join(rd, STATE_FILE)
    fd, tmp = tempfile.mkstemp(prefix=".remote.", dir=rd)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def adapter_for(s, state=None):
    prov = s.get("PROVIDER") or ""
    if prov not in cloud.ADAPTERS or prov in ("local-lume", "fake"):
        raise Refused(f"PROVIDER={prov or '(none)'} isn't a provider of computers reached over SSH "
                      f"({', '.join(sorted(set(cloud.ADAPTERS) - {'local-lume', 'fake'}))})")
    cls = cloud.ADAPTERS[prov]
    if cls.manifest["place"] != (s.get("PLACE") or ""):
        raise Refused(f"PLACE={s.get('PLACE')} isn't {prov}'s place ({cls.manifest['place']})")
    return cloud.adapter(prov, computers={s["VM_NAME"]: s}, states={s["VM_NAME"]: state or {}})


# --- what metacomputer does there -----------------------------------------------------------------------
def key_text(ssh_dir, name):
    with open(os.path.join(ssh_dir, name)) as f:
        return f.read()


def user_data(s):
    ssh_dir = s["SSH_DIR"]
    keys = {k: key_text(ssh_dir, f"{k}.pub") for k in ("ops", "mcp", "view", "admin")}
    with open(os.path.join(LAB, "provisioning", "remote", "bootstrap.sh")) as f:
        bootstrap = f.read()
    tz = None
    try:
        tz = os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        pass
    return cloud_guest.user_data(s["VM_NAME"], key_text(ssh_dir, "hostkey"), key_text(ssh_dir, "hostkey.pub"), keys,
                                 bootstrap, hostname=s.get("VM_HOSTNAME") or "metacomputer", timezone=tz)


def cmd_create(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    if st.get("instance"):
        raise Refused(f"{s['VM_NAME']} has an instance already ({st.get('provider')}); metacomputer --vm "
                      f"{s['VM_NAME']} status shows it")
    a = adapter_for(s, st)
    if a.manifest["sleep_wake"]["kind"] == "none":
        raise Refused(f"{a.manifest['label']}: there's nothing to make (you bring the server)")
    ud = user_data(s)
    t0 = time.time()
    print(f"metacomputer: making {s['VM_NAME']} on {a.manifest['label']} ({s.get('SIZE') or 'the default size'})",
          file=sys.stderr)
    made = a.create(s["VM_NAME"], size=s.get("SIZE") or None, persist=s.get("PERSIST") or "auto", user_data=ud)
    st = {"provider": a.name, "created": now(), "launch_started": t0, **made}
    write_state(rd, st)                      # the instance is recorded before anything else can fail
    print(f"  instance launched in {made['ad'].split(':')[-1]} ({made['shape']}, {made['image']})", file=sys.stderr)
    a.api.wait_state(made["instance"], "RUNNING")
    host = None
    for _ in range(60):
        host = a.api.public_ip(made["instance"])
        if host:
            break
        time.sleep(5)
    if not host:
        raise Refused("the instance runs but has no public address; metacomputer vm delete removes it")
    st.update(host=host, running_at=time.time())
    write_state(rd, st)
    print(f"  running at {host} after {int(time.time() - t0)} s", file=sys.stderr)
    return 0


def guest_up(s, st, tries=3, wait=5.0):
    """Whether the computer's SSH answers (a few tries: a busy guest can be slow); None when it can't be told
    (a provider's tunnel)."""
    ep = transport.endpoint(s, {"host": st.get("host")} if st.get("host") else None)
    if ep["proxy"] is not None:
        return None
    for i in range(tries):
        if probe_once(ep["host"], ep["port"], 4.0):
            return True
        if i + 1 < tries:
            time.sleep(wait)
    return False


def probe_once(host, port, timeout):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout) as c:
            c.settimeout(timeout)
            return c.recv(4).startswith(b"SSH-")
    except OSError:
        return False


def cmd_wake(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    up = None
    if a.name == "oci" and a.status(s["VM_NAME"]).get("provider_state") == "RUNNING":
        up = guest_up(s, st)
        if up is False:
            print(f"metacomputer: {s['VM_NAME']} runs at {a.manifest['label']} but doesn't answer (it powered itself "
                  "off when unused, which the provider doesn't notice): stopping it there, then starting it",
                  file=sys.stderr)
    out = a.wake(s["VM_NAME"], guest_up=up) if a.name == "oci" else a.wake(s["VM_NAME"])
    if out.get("host") and out["host"] != st.get("host"):
        st["host"] = out["host"]
        write_state(rd, st)
    st["woken"] = now()
    write_state(rd, st)
    print(out.get("state", "?"))
    return 0


def cmd_sleep(s, args):
    st = read_state(run_dir(s))
    a = adapter_for(s, st)
    out = a.sleep(s["VM_NAME"], guest_up=guest_up(s, st, tries=2) is not False) if a.name == "oci" else a.sleep(s["VM_NAME"])
    print(out.get("state", "?"))
    return 0


def cmd_delete(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    if a.manifest["sleep_wake"]["kind"] == "none":
        print("nothing to delete at the provider: it's your own server", file=sys.stderr)
        return 0
    if st.get("instance"):
        a.delete(s["VM_NAME"])
        print(f"deleted the instance and its boot volume ({a.manifest['label']})", file=sys.stderr)
    left = [i for i in a.api.instances(s["VM_NAME"])]
    if left:
        raise Refused(f"{a.manifest['label']} still has an instance for {s['VM_NAME']}: {left}")
    st.pop("instance", None)
    st["deleted"] = now()
    write_state(rd, st)
    if args.network_too and not a.api.instances():
        if a.api.delete_network():
            print(f"deleted metacomputer's network at {a.manifest['label']} (no computer uses it now)", file=sys.stderr)
    return 0


def cmd_status(s, args):
    rd = run_dir(s)
    st = read_state(rd)
    a = adapter_for(s, st)
    if a.manifest["sleep_wake"]["kind"] == "none":
        out = {"name": s["VM_NAME"], "place": a.manifest["place"], "state": "unknown",
               "host": s.get("SSH_HOST"), "note": "your own server: metacomputer reaches it over SSH"}
    elif args.offline:
        out = {"name": s["VM_NAME"], "place": a.manifest["place"], "state": None, "host": st.get("host"),
               "note": "its provider was not asked (no credentials in this command)"}
    else:
        out = a.status(s["VM_NAME"])
        if out.get("host") and out["host"] != st.get("host"):
            st["host"] = out["host"]
            write_state(rd, st)
        out["host"] = out.get("host") or st.get("host")
        if out.get("provider_state") == "RUNNING" and guest_up(s, st, tries=1) is False:
            out["note"] = ("it doesn't answer: if it powered itself off (unused for IDLE_STOP minutes), the provider still "
                           "counts it as running, and bills a billed size, until metacomputer stop or start")
    out.update(provider=a.name, where=a.manifest["label"], size=s.get("SIZE") or st.get("size"),
               arch=st.get("arch") or s.get("GUEST_ARCH"), egress=s.get("EGRESS") or "internet",
               idle_stop=s.get("IDLE_STOP") or ("off" if a.manifest["sleep_wake"]["kind"] == "none" else "15"))
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        for k in ("where", "state", "host", "size", "arch", "egress", "idle_stop"):
            if out.get(k) is not None:
                print(f"{k}: {out[k]}")
    return 0


def cmd_state(s, args):
    try:
        st = read_state(run_dir(s))
        a = adapter_for(s, st)
        if a.manifest["sleep_wake"]["kind"] == "none":
            print("unknown")
            return 0
        print(a.status(s["VM_NAME"]).get("state", "unknown"))
    except (AdapterError, Refused):
        print("unknown")
    return 0


def cmd_rates(s, args):
    a = adapter_for(s, read_state(run_dir(s)))
    r = a.rates(s.get("SIZE") or None) if a.name == "oci" else a.rates()
    print(json.dumps(r) if args.json else f"${r['awake_per_hour']}/h awake, ${r['asleep_per_hour']}/h stopped: {r['note']}")
    return 0


def cmd_plan(s, args):
    """What the build needs to know, one KEY=VALUE per line: kind (made: a provider makes it; own: your server),
    arch (from the size, for a provider's; else GUEST_ARCH or "detect"), label, idle_stop."""
    a = adapter_for(s, read_state(run_dir(s)))
    made = a.manifest["sleep_wake"]["kind"] != "none"
    arch = "detect"
    if made and a.name == "oci":
        from cloud import oci as cloud_oci
        arch = cloud_oci.SIZES[cloud_oci.size_of(s.get("SIZE") or None)][3]
    elif s.get("GUEST_ARCH"):
        from cloud import ubuntu_vm
        arch = ubuntu_vm.check_arch(s["GUEST_ARCH"])
    idle = (s.get("IDLE_STOP") or "15").strip().lower() if made else "off"
    if idle not in ("off", "0") and not idle.isdigit():
        raise Refused(f"IDLE_STOP={idle}: minutes, or off")
    from cloud import ubuntu_vm
    print(f"kind={'made' if made else 'own'}\narch={arch}\nlabel={a.manifest['label']}\nidle_stop={idle}\n"
          f"egress={ubuntu_vm.check_egress(s.get('EGRESS'))}")
    return 0


def cmd_key_lists(s, args):
    """The two accounts' key lists (lib/cloud/guest.py key_lists), into DIR: keys-guest and keys-admin."""
    ssh_dir = s["SSH_DIR"]
    guest, admin = cloud_guest.key_lists({k: key_text(ssh_dir, f"{k}.pub") for k in ("ops", "mcp", "view", "admin")})
    for name, body in (("keys-guest", guest), ("keys-admin", admin)):
        with open(os.path.join(args.dir, name), "w") as f:
            f.write(body)
    return 0


def cmd_user_data(s, args):
    sys.stdout.write(user_data(s))
    return 0


def cmd_cloud(args):
    if args.provider not in cloud.ADAPTERS or args.provider in ("local-lume", "fake"):
        raise Refused(f"no provider called {args.provider} ({', '.join(sorted(cloud.providers().values()))})")
    a = cloud.adapter(args.provider)
    if args.action == "status":
        if a.name == "ubuntu-vm":
            out = {"provider": a.name, "note": "your own servers are listed by metacomputer list --on server"}
        else:
            out = {"provider": a.name, "instances": a.list(), "network": a.api.network(),
                   "boot_volumes": a.api.boot_volumes()}
    elif args.action == "doctor":
        out = {"provider": a.name, "checks": a.doctor()}
    elif args.action == "setup":
        out = a.setup()
    else:   # teardown: the network, only when no instance of metacomputer's is left
        if a.name == "ubuntu-vm":
            raise Refused("nothing to tear down for your own servers")
        left = a.list()
        if left:
            raise Refused(f"{a.manifest['label']} still has metacomputer's instances: "
                          f"{', '.join(i['name'] for i in left)} (metacomputer vm delete NAME first)")
        out = {"provider": a.name, "network_deleted": a.api.delete_network()}
    print(json.dumps(out, indent=None if args.json else 2, sort_keys=True, default=str))
    return 0


def cmd_target(args):
    """scripts/lib.sh ssh_base: the transport's options and destination, NUL-separated, from the values bash loaded
    (no second reading of the settings) and the provider's record (the address it gave)."""
    state = read_state(args.run_dir) if args.run_dir else {}
    settings = {"VM_NAME": args.vm, "TRANSPORT": args.transport, "SSH_HOST": args.host, "SSH_HOST_PORT": args.port}
    ep = transport.endpoint(settings, {"host": state.get("host")} if state.get("host") else None)
    out = transport.ssh_target(ep, args.user, wrapper=WRAPPER)
    sys.stdout.write("\0".join(out) + "\0")
    return 0


def cmd_probe(args):
    """Whether the computer's SSH port answers (a TCP connection, a few seconds at most): reachable or unreachable.
    A computer reached through its provider's tunnel can't be probed this way: unknown."""
    import socket
    state = read_state(args.run_dir) if args.run_dir else {}
    settings = {"VM_NAME": args.vm, "TRANSPORT": args.transport, "SSH_HOST": args.host, "SSH_HOST_PORT": args.port}
    ep = transport.endpoint(settings, {"host": state.get("host")} if state.get("host") else None)
    if ep["proxy"] is not None:
        print("unknown")
        return 0
    try:
        with socket.create_connection((ep["host"], ep["port"]), timeout=args.timeout) as c:
            c.settimeout(args.timeout)
            ok = c.recv(4).startswith(b"SSH-")
    except OSError:
        ok = False
    print("reachable" if ok else "unreachable")
    return 0


def cmd_missing(args):
    """Why a feature can't be done on this provider's computers (lib/cloud/capabilities.py's feature map)."""
    from cloud import capabilities
    cls = cloud.ADAPTERS.get(args.provider)
    if cls is None:
        raise Refused(f"no provider {args.provider}")
    feature = {"baseline": "reset", "checkpoints": "checkpoint"}.get(args.feature, args.feature)
    if feature not in capabilities.FEATURES:
        raise Refused(f"no feature {args.feature}")
    print(capabilities.missing(cls.manifest, feature) or "")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="remote.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("target")
    for k in ("--vm", "--user"):
        p.add_argument(k, required=True)
    p.add_argument("--run-dir", default="")
    p.add_argument("--transport", default="ssh")
    p.add_argument("--host", default="")
    p.add_argument("--port", default="22")
    p = sub.add_parser("probe")
    p.add_argument("--vm", required=True)
    p.add_argument("--run-dir", default="")
    p.add_argument("--transport", default="ssh")
    p.add_argument("--host", default="")
    p.add_argument("--port", default="22")
    p.add_argument("--timeout", type=float, default=4.0)
    p = sub.add_parser("missing")
    p.add_argument("--provider", required=True)
    p.add_argument("--feature", required=True)
    p = sub.add_parser("key-lists")
    p.add_argument("--vm", required=True)
    p.add_argument("--dir", required=True)
    for name in ("create", "wake", "sleep", "delete", "status", "state", "rates", "user-data", "plan"):
        p = sub.add_parser(name)
        p.add_argument("--vm", required=True)
        p.add_argument("--json", action="store_true")
        if name == "status":
            p.add_argument("--offline", action="store_true", help="the record only: the provider isn't asked")
        if name == "delete":
            p.add_argument("--network-too", action="store_true")
    p = sub.add_parser("cloud")
    p.add_argument("action", choices=("status", "doctor", "setup", "teardown"))
    p.add_argument("provider")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "target":
            return cmd_target(a)
        if a.cmd == "probe":
            return cmd_probe(a)
        if a.cmd == "missing":
            return cmd_missing(a)
        if a.cmd == "cloud":
            return cmd_cloud(a)
        s = settings_for(a.vm)
        return {"create": cmd_create, "wake": cmd_wake, "sleep": cmd_sleep, "delete": cmd_delete,
                "status": cmd_status, "state": cmd_state, "rates": cmd_rates, "user-data": cmd_user_data,
                "plan": cmd_plan, "key-lists": cmd_key_lists}[a.cmd](s, a)
    except (Refused, AdapterError) as e:
        print(f"metacomputer: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"metacomputer: {e}", file=sys.stderr)
        return 1
    except Exception as e:      # noqa: BLE001  (the provider's SDK: said in one line, without its request details)
        if type(e).__name__ != "ServiceError":
            raise
        print(f"metacomputer: the provider refused: {getattr(e, 'status', '?')} {getattr(e, 'code', '')}: "
              f"{str(getattr(e, 'message', '') or '')[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
