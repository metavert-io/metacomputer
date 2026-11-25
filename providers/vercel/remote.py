"""Vercel Sandbox's part of lib/remote.py: what it does differently from a provider that makes a server (its launch,
its `vercel` transport through the broker, its network policy, its snapshots as checkpoints, commands through Vercel's
API). lib/remote.py calls these hooks (lib/remote.py `hooks_for`) with itself as R, for its records, refusals and
locks; nothing here is run on its own.

    provider_kw(R, s)                     what the adapter is made with: Vercel's API, from lib/remote.py's credentials
    create(R, s, a, rd, st, op)           the computer's sandbox, journaled like any launch; then its broker
    abort(R, s, a, rd, st, op, keep)      a failed build's clean-up
    wake(R, s, a, rd, st)                 a new session, the boot script in it, then its broker
    delete(R, s, a, rd, st, args)         vm delete's part at Vercel (the sandbox and every snapshot of it)
    plan(R, s, a, common)                 the build's plan (lib/remote.py cmd_plan)
    cloud(R, args)                        cloud status|doctor|setup vercel
    egress_check(R, s, a, st, args)       before net egress changes the policy: widening only at your own terminal
    transport hooks (the `vercel` transport): connect_wrapper, tunnel, probe
    COMMANDS                              exec, checkpoint and allow

The broker (providers/vercel/broker.py) is the only other process that gets the credentials: start_broker hands them
over on its standard input, never in its arguments or environment."""
import json
import os
import subprocess
import sys
import time

from cloud.adapter import AdapterError

from . import adapter as vc

# Tests may hand in a stand-in for Vercel's API (a callable taking the credentials' dict); never set otherwise.
API_FACTORY = None
# Tests may hand in a stand-in for starting the broker (a callable taking its argv, settings and log path).
BROKER_FACTORY = None
BROKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker.py")
TUNNEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tunnel.py")
CRED_KEYS = ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID")


def provider_kw(R, s=None):
    """Vercel's API, made from lib/remote.py's own credentials (taken off its environment first thing); none when they
    aren't all there (the adapter then refuses what needs Vercel, saying which)."""
    creds = {k: (R.CREDS.get(k) or "").strip() for k in CRED_KEYS}
    if not all(creds.values()):
        return {}
    if API_FACTORY is not None:
        return {"api": API_FACTORY(creds)}
    import provider_http
    from . import api
    try:
        return {"api": api.API(env=creds, http=provider_http.send, stream=provider_http.stream)}
    except AdapterError as e:
        raise R.Refused(str(e)) from None


# --- the broker ------------------------------------------------------------------------------------------------------
def broker_sock(R, s):
    return vc.broker_paths(R.LAB, s["VM_NAME"])[1]


def start_broker(R, s, a, st):
    """The computer's broker, running with this command's credentials: one that answers for this computer already is
    kept (its connections stay); one for another (a computer made again under the name) is ended first. The
    credentials go on its standard input, nothing else does."""
    from . import broker as bk
    sock = broker_sock(R, s)
    ans = bk.ask(sock, {"op": "ping"}, timeout=3)
    if ans and ans.get("iid") == st.get("instance"):
        return "running"
    if ans:
        bk.ask(sock, {"op": "quit"}, timeout=3)
        for _ in range(30):
            if bk.ask(sock, {"op": "ping"}, timeout=1) is None:
                break
            time.sleep(0.2)
    creds = {k: R.CREDS.get(k) or "" for k in CRED_KEYS}
    if not all(creds.values()):
        raise R.Refused(f"{s['VM_NAME']}'s connection broker needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID "
                        "in this command's environment")
    plan, cap = a.plan()
    cfg = dict(creds, vm=s["VM_NAME"], sandbox=vc.sandbox_of(s), iid=st["instance"], op=st.get("op") or "",
               sock=sock, idle_min=a.idle_min(s["VM_NAME"], cap), cap_min=cap)
    rd = R.run_dir(s)
    os.makedirs(rd, mode=0o700, exist_ok=True)
    log = os.path.join(rd, "vercel-broker.log")
    argv = [sys.executable, BROKER, "serve"]
    if BROKER_FACTORY is not None:
        BROKER_FACTORY(argv, cfg, log)
    else:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=fd, stderr=fd, cwd="/", close_fds=True,
                                 start_new_session=True, env=R.child_env({"PYTHONDONTWRITEBYTECODE": "1"}))
        finally:
            os.close(fd)
        p.stdin.write(json.dumps(cfg).encode())
        p.stdin.close()
        del cfg
    for _ in range(50):
        got = bk.ask(sock, {"op": "ping"}, timeout=1)
        if got and got.get("iid") == st["instance"]:
            return "started"
        time.sleep(0.1)
    raise R.Refused(f"{s['VM_NAME']}'s connection broker didn't start (its log: {log})")


def stop_broker(R, s):
    from . import broker as bk
    sock = broker_sock(R, s)
    if bk.ask(sock, {"op": "quit"}, timeout=3) is not None:
        for _ in range(30):
            if bk.ask(sock, {"op": "ping"}, timeout=1) is None:
                return True
            time.sleep(0.2)
    return False


# --- lifecycle -------------------------------------------------------------------------------------------------------
# A launch whose answer was lost may show at Vercel only later (its create may still be in flight, or its listing lag
# behind): it's looked for again and again, LOST_GAP_S apart, until VISIBILITY_WINDOW_S have passed since it was sent,
# twice the create call's own 60 s timeout. Until then nothing says "nothing was made", and the journal stays.
LOST_GAP_S = 10
VISIBILITY_WINDOW_S = 120


def resolve_lost(R, a, s, st):
    """The sandbox a launch journaled in `st` (launching, launch_t) made though its answer was lost: only one tagged
    with that launch's operation (adapter.owned_lost; a name alone is never enough). It's looked for until it shows,
    or until VISIBILITY_WINDOW_S after the launch was sent, LOST_GAP_S apart: None only then. A lookup that fails, or a
    sandbox of that name without the tag, raises: the journal is left as it is."""
    op = st.get("launching")
    if not op:
        return None
    sent = st.get("launch_t") if isinstance(st.get("launch_t"), (int, float)) else time.time()
    while True:
        found = a.owned_lost(s["VM_NAME"], op)
        if found:
            return found
        if time.time() >= sent + VISIBILITY_WINDOW_S:
            return None
        print(f"metacomputer: {s['VM_NAME']}'s launch got no answer and its sandbox doesn't show yet: looking again until "
              f"{VISIBILITY_WINDOW_S} s after it was sent", file=sys.stderr)
        time.sleep(min(LOST_GAP_S, max(0.0, sent + VISIBILITY_WINDOW_S - time.time())) or LOST_GAP_S)


def delete_everything(R, s, a, rd, st):
    """The computer's sandbox and every snapshot of it (adapter.delete), its snapshots journaled in the record
    (snapshots_to_delete, durably) before anything is deleted, so a try that's cut off is finished by the next from the
    journal. The journal goes only once each of them reads deleted; while one doesn't, Refused, naming it."""
    def journal(ids):
        cur = R.read_state(rd)
        R.write_state(rd, dict(cur, snapshots_to_delete=sorted(set(cur.get("snapshots_to_delete") or []) | set(ids))))
    a.states[s["VM_NAME"]] = dict(st, snapshots_to_delete=R.read_state(rd).get("snapshots_to_delete") or [])
    out = a.delete(s["VM_NAME"], journal=journal)
    left = out.get("snapshots_left") or []
    cur = R.read_state(rd)
    if left:
        R.write_state(rd, dict(cur, snapshots_to_delete=sorted(left)))
        raise R.Refused(f"{len(left)} snapshot(s) of {s['VM_NAME']}'s sandbox still read created at Vercel "
                        f"({', '.join(left)}): its record stays; metacomputer vm delete {s['VM_NAME']} again deletes them")
    R.write_state(rd, {k: v for k, v in cur.items() if k != "snapshots_to_delete"})
    return out


def adopt_lost(R, s, a, rd, st, lost):
    """A lost launch found: recorded as the computer's own (its identity and operation), so it's deleted as any is."""
    sandbox = vc.sandbox_of(s)
    st = dict({k: v for k, v in st.items() if k != "launching"},
              instance=vc.instance_id(sandbox, lost.get("createdAt")), sandbox=sandbox, op=st["launching"])
    R.write_state(rd, st)
    a.states[s["VM_NAME"]] = st
    return st


def create(R, s, a, rd, st, op):
    """The computer's sandbox, journaled like any launch (a lost answer is found by its name and its operation's tag,
    looked for over Vercel's visibility delay), its network policy EGRESS=internet for the setup's downloads (the
    build sets EGRESS when it ends); then its broker, so the setup's SSH can start."""
    lost = resolve_lost(R, a, s, st)
    if lost:
        print(f"metacomputer: an earlier launch of {s['VM_NAME']} made a sandbox it never recorded (its tag shows it): "
              "deleting it", file=sys.stderr)
        delete_everything(R, s, a, rd, adopt_lost(R, s, a, rd, st, lost))
        st = R.read_state(rd)
    t0 = time.time()
    st = {k: v for k, v in st.items() if k not in ("kept", "aborted", "deleted", "instance", "launching", "op")}
    try:
        R.write_state(rd, dict(st, provider=a.name, launching=op, launch_t=t0))
    except OSError as e:
        raise R.Refused(f"couldn't make the launch's journal durable ({e.strerror or e}): nothing was made") from None
    print(f"metacomputer: making {s['VM_NAME']} on {a.manifest['label']} (the sandbox {vc.sandbox_of(s)}, "
          f"{a.size_of(s.get('SIZE'))}{(' in ' + s['REGION']) if s.get('REGION') else ''})", file=sys.stderr)
    print(f"  {a.plan_note()}", file=sys.stderr)
    made = a.create(s["VM_NAME"], region=s.get("REGION") or None, size=s.get("SIZE") or None,
                    persist=s.get("PERSIST") or "auto", op=op)
    st = {"provider": a.name, "created": R.now(), "launch_started": t0, "op": op, "made_by_build": True,
          "policy": "internet", **made}
    try:
        R.write_state(rd, st)
    except OSError as e:
        a.api.delete(made["sandbox"])
        raise R.Refused(f"couldn't record the sandbox it made ({e.strerror}), so it was deleted again; nothing is left "
                        "billing") from None
    a.states[s["VM_NAME"]] = st
    print(f"  the sandbox is made ({round(time.time() - t0, 1)} s); its network policy: internet for the setup",
          file=sys.stderr)
    start_broker(R, s, a, st)
    return 0


def abort(R, s, a, rd, st, op, keep):
    """A failed build's clean-up at Vercel: the sandbox this operation made (recorded, or a lost launch found by its
    tag over Vercel's visibility delay) is deleted with every snapshot of it; --keep stops it instead (stopped, it
    bills its snapshot only). "Nothing was made" only once the whole window has passed with nothing found."""
    if not st.get("instance") and st.get("launching") == op:
        lost = resolve_lost(R, a, s, st)
        if lost:
            st = adopt_lost(R, s, a, rd, st, lost)
    if not st.get("instance"):
        st = {k: v for k, v in st.items() if k != "launching"}
        R.write_state(rd, dict(st, aborted=R.now()))
        print("nothing to delete: no sandbox was made", file=sys.stderr)
        return 0
    a.states[s["VM_NAME"]] = st
    if keep:
        try:
            a.sleep(s["VM_NAME"])
        finally:
            R.write_state(rd, dict(st, kept=R.now()))
        print("stopped")
        return 0
    stop_broker(R, s)
    delete_everything(R, s, a, rd, st)
    st = {k: v for k, v in R.read_state(rd).items()
          if k not in ("instance", "launching", "op", "checkpoints", "checkpoint_times")}
    R.write_state(rd, dict(st, aborted=R.now()))
    print(f"deleted the sandbox this build made and its snapshots ({a.manifest['label']})", file=sys.stderr)
    return 0


def wake(R, s, a, rd, st):
    """metacomputer start: a new session from the latest snapshot (none when one runs), the boot script in it, the
    snapshot that stop left behind deleted unless a checkpoint names it, then the broker. Says what a stop doesn't
    keep, and the plan's cap."""
    R.check_recorded(s, st, a)
    out = a.wake(s["VM_NAME"])
    print(f"answered in {out.get('woke_in_s')} s; booted in {out.get('booted_in_s')} s", file=sys.stderr)
    if out.get("note"):
        print(f"metacomputer: {out['note']}", file=sys.stderr)
    print(f"metacomputer: {a.plan_note()}", file=sys.stderr)
    try:
        gone = a.gc_snapshots(s["VM_NAME"])
        if gone:
            print(f"  deleted {len(gone)} snapshot(s) no start needs now", file=sys.stderr)
    except AdapterError as e:
        print(f"metacomputer: warning: old snapshots weren't cleaned up ({e})", file=sys.stderr)
    start_broker(R, s, a, st)
    R.write_state(rd, dict(R.read_state(rd), woken=R.now()))
    print(out.get("state", "?"))
    return 0


def delete(R, s, a, rd, st, args):
    """vm delete at Vercel: the recorded sandbox with its sessions and every snapshot (checkpoints too: journaled
    first, each read back until deleted), or one a lost launch made (its tag, looked for over Vercel's visibility
    delay). The record goes only when nothing of it is left."""
    stop_broker(R, s)
    if not st.get("instance") and st.get("launching"):
        lost = resolve_lost(R, a, s, st)
        if lost:
            print("deleting a sandbox launched for it that this Mac had no record of (its tag shows it)", file=sys.stderr)
            st = adopt_lost(R, s, a, rd, st, lost)
    if st.get("instance"):
        out = delete_everything(R, s, a, rd, st)
        print(("deleted the sandbox and its " if out["deleted"] else "its sandbox was gone already; deleted its ")
              + f"{len(out.get('snapshots_deleted') or [])} remaining snapshot(s)", file=sys.stderr)
    st = {k: v for k, v in R.read_state(rd).items()
          if k not in ("instance", "launching", "checkpoints", "checkpoint_times", "snapshots_to_delete")}
    R.write_state(rd, dict(st, deleted=R.now()))
    return 0


def plan(R, s, a, common):
    """The build's plan for a sandbox: its own first steps (providers/vercel/build.sh), x86_64, IDLE_STOP as the session
    timeout, no systemd, cloud-init or egress firewall of its own (Vercel's network policy is the boundary), a
    connection held open while it's set up (the broker extends the session while one is)."""
    from cloud import ubuntu_vm
    idle = (s.get("IDLE_STOP") or "15").strip().lower()
    if idle not in ("off", "0") and not (idle.isdigit() and 1 <= int(idle) <= 999):
        raise R.Refused(f"IDLE_STOP={idle}: minutes (1-999), or off")
    return (f"kind=provider\narch=x86_64\nlabel={a.manifest['label']}\nidle_stop={idle}\n"
            f"egress={ubuntu_vm.check_egress(s.get('EGRESS'))}\n{common}\n"
            "target=vercel\nsystemd=no\ncloud_init=no\nguest_firewall=no\nkeep_awake=yes")


def vercel_computers(R):
    out = {}
    vms = os.path.join(R.LAB, "config", "vms")
    for f in sorted(os.listdir(vms)) if os.path.isdir(vms) else []:
        if not f.endswith(".env"):
            continue
        try:
            st = R.settings_for(f[:-4])
        except Exception:      # noqa: BLE001  (a file that doesn't resolve is listed by metacomputer list, not here)
            continue
        if st.get("PROVIDER") == "vercel":
            out[st["VM_NAME"]] = st
    return out


def cloud(R, args):
    """cloud status|doctor|setup vercel: the project's sandboxes metacomputer made (tagged), the team's plan; nothing to
    tear down (each computer's sandbox and snapshots go with vm delete)."""
    import cloud as cloud_pkg
    comps = vercel_computers(R)
    states = {n: R.read_state(R.run_dir(c)) for n, c in comps.items()}
    a = cloud_pkg.adapter("vercel", computers=comps, states=states, **provider_kw(R))
    if args.action == "status":
        p, cap = a.plan()
        out = {"provider": a.name, "plan": p or None, "session_cap_minutes": cap, "sandboxes": a.list(),
               "note": "the project's sandboxes metacomputer made (tagged metacomputer): here = a computer of this Mac's"}
    elif args.action == "doctor":
        out = {"provider": a.name, "checks": a.doctor()}
    elif args.action == "setup":
        out = a.setup()
    else:
        raise R.Refused("nothing to tear down at Vercel: each computer's sandbox and snapshots go with metacomputer vm "
                        "delete NAME")
    print(json.dumps(out, indent=None if args.json else 2, sort_keys=True, default=str))
    return 0


# --- the `vercel` transport: lib/remote.py target, tunnel and probe ------------------------------------------------
def check_argv(R, args, ep):
    """How the tunnel's own program asks the broker about the sandbox: its identity now, as JSON {"id": ...}
    (lib/proxywrap.py --check, and the vault's binding)."""
    proxy = ep["proxy"]
    return [proxy[0], "check", *proxy[2:]]


def recorded(R, args, ep):
    """The computer's record, its sandbox the one the settings name (from the record: no credentials here)."""
    st = R.read_state(args.run_dir) if args.run_dir else {}
    iid = st.get("instance")
    if not iid:
        raise R.Refused(f"{args.vm} has no sandbox on record: nothing to connect to")
    sandbox, _created = vc.parse_instance(iid)
    if sandbox != ep["host"]:
        raise R.Refused(f"{args.vm}'s settings name the sandbox {ep['host']}, but it was made as {sandbox}: no connection "
                        "was made")
    return st


def connect_wrapper(R, args, ep):
    """Every connection goes to the computer's own sandbox, or none: the ProxyCommand asks the broker, right before
    each connection's relay starts (lib/proxywrap.py --id), for the sandbox's identity as Vercel has it, which must be
    the one recorded when it was made; the broker checks it again before it runs the relay."""
    st = recorded(R, args, ep)
    chk = check_argv(R, args, ep)
    return ("--id", st["instance"], "--check", str(len(chk)), *chk)


def tunnel(R, args, ep):
    """The vault's binding of a computer behind the broker: the tunnel's program and arguments, the sandbox's identity
    and how the same program asks about it."""
    st = recorded(R, args, ep)
    return ep["proxy"], check_argv(R, args, ep), st["instance"]


def probe(R, args, ep):
    """Without credentials: the broker says whether the session runs (reachable), or nothing does (unreachable)."""
    from . import broker as bk
    try:
        st = recorded(R, args, ep)
    except R.Refused:
        return "unknown"
    ans = bk.ask(ep["proxy"][3], {"op": "check", "sandbox": ep["host"]}, timeout=10)
    if not ans or ans.get("id") != st["instance"]:
        return "unreachable"
    return "reachable" if ans.get("state") == "running" else "unreachable"


# --- the network policy ---------------------------------------------------------------------------------------------
def require_narrowing(R, s, a, st, new_policy, setup=False, what="that"):
    """A change of the network policy whose end state lets out anything the policy in force doesn't runs only at the
    user's own terminal, however it's split into steps (adapter.widens). The one exception is the setup's own: --setup,
    while this computer's setup hasn't finished (setup_done), opens it to EGRESS=internet for its downloads."""
    import owners
    cur = a.egress(s["VM_NAME"])
    if not vc.widens(cur.get("outside"), cur.get("allow") or [], new_policy):
        return
    if setup:
        if st.get("setup_done"):
            raise R.Refused(f"{s['VM_NAME']} is set up already: the setup's exception doesn't apply. Nothing changed")
        if new_policy != vc.policy_for("internet"):
            raise R.Refused("the setup opens the policy to EGRESS=internet, and nothing else. Nothing changed")
        return
    if not owners.at_user_terminal((0, 2)):
        raise R.Refused(f"{what} lets the computer reach more than the policy in force does: only at your own terminal: "
                        f"metacomputer --vm {s['VM_NAME']} {what}. Nothing changed")


def egress_check(R, s, a, st, args):
    """net egress internet|off: the policy it ends with, judged before anything changes (require_narrowing)."""
    require_narrowing(R, s, a, st, vc.policy_for(args.set), args.setup, f"net egress {args.set}")


# --- commands only a sandbox has --------------------------------------------------------------------------------------
EXEC_MAX_INPUT = 64 << 20


def cmd_exec(R, s, a, st, args):
    """A command in the sandbox's running session as root, through Vercel's own authenticated API, with this command's
    standard input when asked (--stdin); its output and exit status are the command's. Never starts a stopped one."""
    argv = list(args.argv[1:] if args.argv[:1] == ["--"] else args.argv)
    if not argv:
        raise R.Refused("exec needs a command")
    data = sys.stdin.buffer.read(EXEC_MAX_INPUT + 1) if args.stdin else None
    if data is not None and len(data) > EXEC_MAX_INPUT:
        raise R.Refused(f"exec takes at most {EXEC_MAX_INPUT} bytes on its standard input")
    rc, out, err = a.exec(s["VM_NAME"], argv, input=data, timeout=args.timeout, max_output=args.max_output)
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    sys.stderr.buffer.write(err[-65536:])
    return rc if isinstance(rc, int) else 1


def restart(R, s, a, rd, st, why):
    """After a snapshot or a restore (each stops the computer): a new session, the boot script, the broker."""
    a.states[s["VM_NAME"]] = st
    out = a.wake(s["VM_NAME"])
    try:
        a.gc_snapshots(s["VM_NAME"])
    except AdapterError as e:
        print(f"metacomputer: warning: old snapshots weren't cleaned up ({e})", file=sys.stderr)
    start_broker(R, s, a, st)
    print(f"metacomputer: {why}: it was started again ({out.get('woke_in_s')} s, booted in {out.get('booted_in_s')} s); "
          "open apps were closed", file=sys.stderr)


def cmd_checkpoint(R, s, a, st, args):
    """A computer's checkpoints: Vercel snapshots its record names (checkpoints: {LABEL: [ids]}). list, create (Vercel's
    snapshot stops the computer: it's started again), restore (stopped, then started from it) or delete."""
    if not st.get("instance"):
        raise R.Refused(f"{s['VM_NAME']} has no sandbox that this Mac knows of")
    rd = R.run_dir(s)
    if args.action == "list":
        out = a.snapshots(s["VM_NAME"])
        if args.json:
            print(json.dumps(out, sort_keys=True))
        else:
            for c in out:
                print(f"{c['label']}\t{c['version']}\t{c.get('created') or ''}")
        return 0
    if not args.label:
        raise R.Refused(f"checkpoint {args.action} needs a name")
    t0 = time.monotonic()
    if args.action == "create":
        R.check_recorded(s, st, a)
        older = [c["version"] for c in a.snapshots(s["VM_NAME"]) if c["label"] == args.label] if args.replace else []
        out = a.snapshot(s["VM_NAME"], args.label)
        st = R.read_state(rd)
        cps = dict(st.get("checkpoints") or {})
        cps[args.label] = [v for v in (cps.get(args.label) or []) if v not in older] + [out["version"]]
        times = dict(st.get("checkpoint_times") or {}, **{out["version"]: R.now()})
        st = dict(st, checkpoints=cps, checkpoint_times=times)
        R.write_state(rd, st)
        out["snapshot_s"] = round(time.monotonic() - t0, 2)
        if older:                     # --replace: the new one is recorded first, then the older ones of that name go
            for v in older:
                if v != a.api.sandbox(vc.sandbox_of(s))["sandbox"].get("currentSnapshotId"):
                    a.api.delete_snapshot(v)
            times = {k: v for k, v in st["checkpoint_times"].items() if k not in older}
            R.write_state(rd, dict(st, checkpoint_times=times))
            out["replaced"] = older
        restart(R, s, a, rd, R.read_state(rd), f"taking checkpoint {args.label} stopped {s['VM_NAME']}")
    elif args.action == "restore":
        R.check_recorded(s, st, a)
        out = a.restore(s["VM_NAME"], args.label)
        restart(R, s, a, rd, st, f"{s['VM_NAME']} was put back to checkpoint {args.label}")
    else:
        out = a.delete_snapshot(s["VM_NAME"], args.label)
        st = R.read_state(rd)
        cps = {k: v for k, v in (st.get("checkpoints") or {}).items() if k != args.label}
        times = {k: v for k, v in (st.get("checkpoint_times") or {}).items() if k not in out["versions"]}
        R.write_state(rd, dict(st, checkpoints=cps, checkpoint_times=times))
    out["seconds"] = round(time.monotonic() - t0, 2)
    print(json.dumps(out, sort_keys=True))
    return 0


def cmd_allow(R, s, a, st, args):
    """The network policy as an allowlist of DNS names (each with its subdomains unless --exact), each change read back.
    With a list in force, names are added or taken off (--remove); --clear goes back to EGRESS; --show prints what's in
    force. A change whose resulting policy lets out more than the one in force only at the user's own terminal."""
    if not st.get("instance"):
        raise R.Refused(f"{s['VM_NAME']} has no sandbox that this Mac knows of")
    name, setting = s["VM_NAME"], (s.get("EGRESS") or "internet")
    cur = a.egress(name)
    kind, have = cur.get("outside"), list(cur.get("allow") or [])
    if args.show:
        out = cur
    elif args.clear:
        require_narrowing(R, s, a, st, vc.policy_for(setting), what="net clear")
        out = a.apply_egress(name, setting)
    else:
        if not args.names:
            raise R.Refused("net allow and net remove need names")
        try:
            want = vc.policy_for(allow=args.names, exact=args.exact)["allow"]
        except ValueError as e:
            raise R.Refused(str(e)) from None
        if args.remove:
            if kind != "allowlist":
                raise R.Refused("no allowlist is in force to take names off (metacomputer net status)")
            left = [d for d in have if d not in want]
            new = vc.policy_for(allow=left, exact=True) if left else vc.policy_for("off")
        else:
            new = vc.policy_for(allow=list(dict.fromkeys((have if kind == "allowlist" else []) + want)), exact=True)
        require_narrowing(R, s, a, st, new, what=f"net {'remove' if args.remove else 'allow'} {' '.join(args.names)}")
        k_new, n_new = vc.policy_kind(new)
        out = a.set_egress(name, n_new, exact=True) if k_new == "allowlist" else a.apply_egress(name, "off")
    print(json.dumps({"outside": out.get("outside"), "allow": out.get("allow", [])}, sort_keys=True))
    return 0


# lib/remote.py's subcommand -> (what it's called in a refusal, the hook)
COMMANDS = {"exec": ("exec", cmd_exec), "checkpoint": ("checkpoints at the provider", cmd_checkpoint),
            "allow": ("an allowlist of names", cmd_allow)}
