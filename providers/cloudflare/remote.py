"""Cloudflare Containers' part of lib/remote.py: what it does differently from a provider that makes a server (its
control Worker deployed into the account, its `cloudflare` transport through the broker, the egress through the
Worker, its snapshots as checkpoints, the boot script and the logs through the Worker). lib/remote.py calls these hooks
(lib/remote.py `hooks_for`) with itself as R, for its records, refusals and locks; nothing here is run on its own.

    provider_kw(R, s)                     what the adapter is made with: the account's API (the token, from lib/remote.py's
                                          credentials) and the computer's Worker (its own key, kept beside its SSH keys)
    create(R, s, a, rd, st, op)           the computer's control Worker, journaled like any launch; its first start; the
                                          broker
    abort(R, s, a, rd, st, op, keep)      a failed build's clean-up
    wake(R, s, a, rd, st)                 a start (from its latest snapshot), the boot script, then the broker
    delete(R, s, a, rd, st, args)         vm delete's part at Cloudflare (and what Cloudflare keeps, said with dates)
    plan(R, s, a, common)                 the build's plan (lib/remote.py cmd_plan)
    cloud(R, args)                        cloud status|doctor|setup cloudflare
    egress_check(R, s, a, st, args)       before net egress changes it: widening only at your own terminal
    transport hooks (the `cloudflare` transport): connect_wrapper, tunnel, probe
    COMMANDS                              exec, checkpoint and allow

The computer's key is made here at create (64 hex digits, 0600, beside its SSH keys: $SSH_DIR/cloudflare-key), read
only here, and handed to the broker (providers/cloudflare/broker.py) on its standard input: never its arguments, its
environment, a log or another program. The account's token never reaches the broker: it's for create and delete."""
import contextlib
import json
import os
import stat
import subprocess
import sys
import time

from cloud.adapter import AdapterError

from . import adapter as cf

# Tests may hand in stand-ins (never set otherwise): API_FACTORY(creds) -> (rest, images); WORKER_FACTORY(url, key) ->
# a Worker client; BROKER_FACTORY(argv, cfg, log) starts the broker.
API_FACTORY = None
WORKER_FACTORY = None
BROKER_FACTORY = None
HERE = os.path.dirname(os.path.abspath(__file__))
BROKER = os.path.join(HERE, "broker.py")
TUNNEL = os.path.join(HERE, "tunnel.py")
CRED_KEYS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
BOOT = "/opt/metacomputer/cloudflare/boot"
KEY_RE = __import__("re").compile(r"[0-9a-f]{64}")


# --- the computer's own key ------------------------------------------------------------------------------------------
def read_key(s):
    """The computer's key, or None when there's none. Refused (Refused) when it's there but isn't a private file of this
    account's (0600, a regular file, not a link) or isn't one metacomputer made."""
    p = cf.key_path(s)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise AdapterError(f"can't read the computer's key ({e.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise AdapterError(f"{p} isn't a private file of this account's (0600): not used")
        k = os.read(fd, 200).decode(errors="replace").strip()
    finally:
        os.close(fd)
    if not KEY_RE.fullmatch(k):
        raise AdapterError(f"{p} isn't a key metacomputer made: not used")
    return k


def new_key(s):
    """A new key for the computer, written 0600 into its key folder (which must be a private folder of this
    account's), replacing any earlier one of a computer of this name."""
    d = s["SSH_DIR"]
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise AdapterError(f"{d} isn't a private folder of this account's (0700): no key was made")
    key = os.urandom(32).hex()
    tmp = os.path.join(d, ".cloudflare-key.%d" % os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, key.encode() + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, cf.key_path(s))
    return key


def remove_key(s):
    try:
        os.remove(cf.key_path(s))
        return True
    except FileNotFoundError:
        return False


def make_worker(url, key):
    if WORKER_FACTORY is not None:
        return WORKER_FACTORY(url, key)
    from . import wire
    return wire.Worker(url, key)


def provider_kw(R, s=None):
    """The account's REST API and registry when this command has the token (create, delete, cloud); the computer's
    Worker when its key and its address are on this Mac (everything else). Either may be missing: the adapter refuses
    what needs one, saying which."""
    kw = {}
    creds = {k: (R.CREDS.get(k) or "").strip() for k in CRED_KEYS}
    if all(creds.values()):
        if API_FACTORY is not None:
            kw["rest"], kw["images"] = API_FACTORY(creds)
        else:
            import provider_http
            from . import api, images
            try:
                rest = api.API(env=creds, http=provider_http.send)
            except AdapterError as e:
                raise R.Refused(str(e)) from None
            kw["rest"], kw["images"] = rest, images.Registries(rest)
    if s is not None and s.get("SSH_DIR"):
        st = R.read_state(R.run_dir(s))
        try:
            key = read_key(s)
        except AdapterError as e:
            raise R.Refused(str(e)) from None
        if key and st.get("url"):
            try:
                kw["worker"] = make_worker(st["url"], key)
            except Exception as e:      # noqa: BLE001  (an address that isn't a Worker's: said, nothing asked)
                raise R.Refused(f"{s['VM_NAME']}'s record names a Worker address metacomputer doesn't use ({e})") from None
    return kw


def modules():
    """The control Worker's two modules, as they're uploaded."""
    out = {}
    for f in ("index.js", "logic.js"):
        with open(os.path.join(HERE, "worker", f), "rb") as fh:
            out[f] = fh.read()
    return out


# --- the broker ------------------------------------------------------------------------------------------------------
def broker_sock(R, s):
    return cf.broker_paths(R.LAB, s["VM_NAME"])[1]


def start_broker(R, s, a, st):
    """The computer's broker, with its key: one that answers for this computer already is kept (its connections stay);
    one for another (a computer made again under the name) is ended first. The key and the Worker's address go on its
    standard input, nothing else does."""
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
    key = read_key(s)
    if not key or not st.get("url"):
        raise R.Refused(f"{s['VM_NAME']}'s connection broker needs its control Worker's key and address, which this Mac "
                        "doesn't have (made by vm create)")
    cfg = {"key": key, "url": st["url"], "vm": s["VM_NAME"], "worker": cf.worker_of(s), "iid": st["instance"],
           "do": st.get("do_id") or "", "op": st.get("op") or "", "sock": sock}
    rd = R.run_dir(s)
    os.makedirs(rd, mode=0o700, exist_ok=True)
    log = os.path.join(rd, "cloudflare-broker.log")
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
    del cfg, key
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
# A launch whose answer was lost (the Worker's upload) may show only later: it's looked for again and again, LOST_GAP_S
# apart, until it has been missing for VISIBILITY_WINDOW_S of consecutive lookups in this attempt (counted from this
# attempt's first empty lookup, never from the launch). Until then nothing says "nothing was made", and the journal stays.
LOST_GAP_S = 10
VISIBILITY_WINDOW_S = 120


def resolve_lost(R, a, s, st):
    """The Worker a launch journaled in `st` (launching) made though its answer was lost: only one whose bindings carry
    this computer and that launch's operation (adapter.owned_lost; a name alone is never enough). Looked for until it
    shows, or until VISIBILITY_WINDOW_S of consecutive empty lookups in this call: None only then. Only a launch that
    reached the Worker's upload is looked for (worker_sent in the journal); a lookup that fails, or a Worker of that
    name without the bindings, raises, and the journal stays."""
    op = st.get("launching")
    if not op or not st.get("worker_sent"):
        return None
    first_empty = None
    while True:
        found = a.owned_lost(s["VM_NAME"], op)
        if found:
            return found
        now = time.time()
        if first_empty is None:
            first_empty = now
        if now - first_empty >= VISIBILITY_WINDOW_S:
            return None
        print(f"metacomputer: {s['VM_NAME']}'s launch got no answer and its Worker doesn't show yet: looking again every "
              f"{LOST_GAP_S} s until {VISIBILITY_WINDOW_S} s of lookups have found nothing", file=sys.stderr)
        time.sleep(LOST_GAP_S)


def adopt_lost(R, s, a, rd, st, lost):
    """A lost launch found: recorded as the computer's own (its Worker and operation), so it's deleted as any is."""
    st = dict({k: v for k, v in st.items() if k != "launching"}, worker=lost["worker"], op=st["launching"],
              made_worker=lost["worker"], adopted=R.now())
    R.write_state(rd, st)
    a.states[s["VM_NAME"]] = st
    return st


def residue_words(out):
    """What Cloudflare keeps of a deleted computer, in words, and as data for its log folder."""
    snaps = out.get("residue")
    reg = out.get("registry") or []
    n_reg = sum(1 for _t, k, _z in reg if k == "snapshot")
    conv = [t for t, k, _z in reg if k == "conversion"]
    lines = []
    if snaps:
        for r in snaps:
            lines.append(f"  a snapshot of its disk ({round((r.get('size') or 0) / 1e9, 2)} GB"
                         f"{', ' + '/'.join(r['labels']) if r.get('labels') else ''}), kept until {cf.days(r.get('expires'))}")
    elif n_reg:
        lines.append(f"  {n_reg} snapshot manifest(s) of its disk in its registry repository, kept at most until "
                     f"{cf.days(time.time() * 1000 + cf.EXPIRE_DAYS * cf.DAY_MS)} (30 days after their last use)")
    if conv:
        lines.append(f"  Cloudflare's conversion of its image ({round(sum(z for _t, k, z in reg if k == 'conversion') / 1e6)} "
                     "MB, no files of yours), which its API won't delete either")
    return lines


def note_residue(R, s, out):
    """What's left at Cloudflare, also written to the computer's log folder (vm delete keeps logs): never a key or an
    account's ID."""
    try:
        d = s.get("LOG_DIR") or os.path.join(R.LAB, "logs-vms", s["VM_NAME"])
        os.makedirs(d, mode=0o700, exist_ok=True)
        p = os.path.join(d, "cloudflare-residue.json")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"deleted": R.now(), "worker": out.get("worker"), "repository": out.get("repo"),
                       "snapshots": out.get("residue"), "registry": [{"tag": t[:80], "kind": k, "bytes": z}
                                                                     for t, k, z in out.get("registry") or []]},
                      f, indent=2, sort_keys=True)
        return p
    except OSError:
        return None


def delete_everything(R, s, a, rd, st):
    """The computer's Worker, container application, container and image's manifest (adapter.delete), the plan
    journaled in the record (deleting) before anything is deleted, so a try that's cut off is finished by the next. The
    record and the journal stay while anything of it is still there. Its key goes once the Worker has."""
    def journal(plan):
        cur = R.read_state(rd)
        R.write_state(rd, dict(cur, deleting=plan))
    a.states[s["VM_NAME"]] = st
    out = a.delete(s["VM_NAME"], journal=journal)
    if not out.get("gone"):
        raise R.Refused(f"{s['VM_NAME']}'s Worker or container application still shows at Cloudflare: its record stays; "
                        f"metacomputer vm delete {s['VM_NAME']} again finishes it")
    remove_key(s)
    lines = residue_words(out)
    if lines:
        print("metacomputer: Cloudflare keeps, and its API can't delete:", file=sys.stderr)
        for ln in lines:
            print(ln, file=sys.stderr)
        if any("snapshot" in ln for ln in lines):
            print("  (they cost nothing during Cloudflare's beta; a snapshot holds the computer's files, its browser "
                  "profile and anything typed into it)", file=sys.stderr)
        p = note_residue(R, s, out)
        if p:
            print(f"  (listed in {p})", file=sys.stderr)
    cur = R.read_state(rd)
    R.write_state(rd, {k: v for k, v in cur.items() if k != "deleting"})
    return out


CREATE_LOCK_WAIT_S = 120


def create_lock(R, s):
    """vm create's lock on this Mac for a Worker's name (a flock in this account's private folder for the computers'
    brokers), held from the check that no Worker of that name exists through the upload and its read-back: two creates
    here can't both upload one. Cloudflare's upload takes no condition, so a create on another Mac is caught only by
    the read-back (adapter.create)."""
    import flockwait
    d = cf.broker_paths(R.LAB, s["VM_NAME"])[0]

    @contextlib.contextmanager
    def lock(worker):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(d)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise R.Refused(f"{d} isn't a private folder of this account's (0700): nothing was uploaded")
        fd = os.open(os.path.join(d, f"create-{worker}.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                flockwait.exclusive(fd, CREATE_LOCK_WAIT_S, 0.2)
            except BlockingIOError:
                raise R.Refused(f"another vm create on this Mac is uploading a Worker named {worker}: nothing was "
                                "uploaded") from None
            yield
        finally:
            os.close(fd)
    return lock


def create(R, s, a, rd, st, op):
    """The computer's control Worker, journaled like any launch (a lost upload is found by its Worker's bindings,
    looked for over a visibility window); its key made first (it's the Worker's secret); then its identity (the
    Worker's own word, with the key), its first start (from the image, the internet on for the setup's downloads) and
    its broker, so the setup's first step can start."""
    lost = resolve_lost(R, a, s, st)
    if lost:
        print(f"metacomputer: an earlier launch of {s['VM_NAME']} made a Worker it never recorded (its bindings show it): "
              "deleting it", file=sys.stderr)
        delete_everything(R, s, a, rd, adopt_lost(R, s, a, rd, st, lost))
        st = R.read_state(rd)
    elif st.get("made_image") and not st.get("instance"):
        # An earlier try copied the image but never uploaded a Worker: its manifest is this computer's to replace.
        pass
    t0 = time.time()
    keep = {k: v for k, v in st.items() if k in ("ops",)}
    worker = cf.worker_of(s)
    try:
        R.write_state(rd, dict(keep, provider=a.name, launching=op, launch_t=t0, worker=worker))
    except OSError as e:
        raise R.Refused(f"couldn't make the launch's journal durable ({e.strerror or e}): nothing was made") from None

    def made(what, value):
        cur = R.read_state(rd)
        if cur.get("launching") == op:
            R.write_state(rd, dict(cur, **{what: value}))
    key = new_key(s)
    idle = a.idle_of(s.get("IDLE_STOP"))
    size = a.size_of(s.get("SIZE"))
    print(f"metacomputer: making {s['VM_NAME']} on {a.manifest['label']} (its control Worker {worker}, {size})",
          file=sys.stderr)
    got = a.create(s["VM_NAME"], size=size, persist=s.get("PERSIST") or "auto", op=op, key=key, modules=modules(),
                   made=made, log=lambda m: print(f"  {m}", file=sys.stderr), guard=create_lock(R, s))
    w = make_worker(got["url"], key)
    a.attach_worker(w)
    try:
        ws = w.call("status", {}, 60)
    except Exception as e:      # noqa: BLE001
        raise R.Refused(f"the new Worker didn't answer with the computer's key ({w.scrub(e)})") from None
    if ws.get("computer") != s["VM_NAME"] or ws.get("op") != op:
        raise R.Refused("the new Worker answers for another computer or operation: nothing more was done")
    cur = R.read_state(rd)
    st = {"provider": a.name, "created": R.now(), "launch_started": t0, "op": op, "made_by_build": True,
          "policy": "internet", "instance": cf.instance_id(worker, ws["do"]), "do_id": ws["do"], "ops": cur.get("ops"),
          **{k: cur[k] for k in ("made_image", "worker_sent", "made_worker", "namespace", "made_app") if k in cur},
          **got}
    try:
        R.write_state(rd, st)
    except OSError as e:
        raise R.Refused(f"couldn't record the Worker it made ({e.strerror}); metacomputer vm delete {s['VM_NAME']} "
                        "deletes it") from None
    a.states[s["VM_NAME"]] = st
    print(f"  the control Worker is in the account ({round(time.time() - t0, 1)} s); starting the container from the "
          "image, the internet on for the setup", file=sys.stderr)
    out = w.call("start", {"from_image": True, "boot": False, "idle_min": idle}, 900)
    print(f"  the container answers ({round((out.get('started_ms') or 0) / 1000.0, 1)} s)", file=sys.stderr)
    start_broker(R, s, a, st)
    return 0


def abort(R, s, a, rd, st, op, keep):
    """A failed build's clean-up at Cloudflare: what this operation made (recorded, or a lost upload found by its
    Worker's bindings over the visibility window) is deleted; --keep stops it instead (its disk snapshotted)."""
    if not st.get("instance") and st.get("launching") == op:
        lost = resolve_lost(R, a, s, st)
        if lost:
            st = adopt_lost(R, s, a, rd, st, lost)
    if not st.get("instance") and not st.get("made_worker") and not st.get("made_image"):
        st = {k: v for k, v in st.items() if k not in ("launching", "worker_sent")}
        R.write_state(rd, dict(st, aborted=R.now()))
        remove_key(s)
        print("nothing to delete: no Worker was made", file=sys.stderr)
        return 0
    a.states[s["VM_NAME"]] = st
    if keep and st.get("instance"):
        try:
            a.sleep(s["VM_NAME"])
        finally:
            R.write_state(rd, dict(R.read_state(rd), kept=R.now()))
        print("stopped")
        return 0
    stop_broker(R, s)
    delete_everything(R, s, a, rd, st)
    st = {k: v for k, v in R.read_state(rd).items()
          if k not in ("instance", "launching", "op", "url", "do_id", "made_image", "worker_sent", "made_worker",
                       "namespace", "made_app")}
    R.write_state(rd, dict(st, aborted=R.now()))
    print(f"deleted the control Worker this build made, its container and its image ({a.manifest['label']})",
          file=sys.stderr)
    return 0


def sync_setup_done(R, s, a, st):
    """A computer set up (its record says so): its Worker closes arbitrary commands, for good."""
    if st.get("setup_done") and not st.get("worker_sealed"):
        a.setup_done(s["VM_NAME"])
        rd = R.run_dir(s)
        R.write_state(rd, dict(R.read_state(rd), worker_sealed=R.now()))


def wake(R, s, a, rd, st):
    """metacomputer start: the container from its latest snapshot (nothing when it runs), the boot script, then the
    broker. Says what a stop doesn't keep, and a stop Cloudflare made without warning."""
    R.check_recorded(s, st, a)
    sync_setup_done(R, s, a, st)
    out = a.wake(s["VM_NAME"])
    print(f"answered in {out.get('woke_in_s')} s; booted in {out.get('booted_in_s')} s", file=sys.stderr)
    if out.get("note"):
        print(f"metacomputer: {out['note']}", file=sys.stderr)
    start_broker(R, s, a, R.read_state(rd))
    R.write_state(rd, dict(R.read_state(rd), woken=R.now()))
    print(out.get("state", "?"))
    return 0


def delete(R, s, a, rd, st, args):
    """vm delete at Cloudflare: the recorded Worker with its container application, container and image's manifest
    (journaled first), or one a lost launch made (its bindings, looked for over the visibility window). The record goes
    only when nothing of it shows; what Cloudflare keeps (snapshots) is said, with dates."""
    stop_broker(R, s)
    if not st.get("instance") and st.get("launching"):
        lost = resolve_lost(R, a, s, st)
        if lost:
            print("deleting a Worker launched for it that this Mac had no record of (its bindings show it)",
                  file=sys.stderr)
            st = adopt_lost(R, s, a, rd, st, lost)
    if st.get("instance") or st.get("made_worker") or st.get("made_image") or st.get("deleting"):
        if st.get("deleting") and not st.get("instance"):
            st = dict(st, **{k: v for k, v in st["deleting"].items() if k in ("namespace", "repo", "digest") and v})
        out = delete_everything(R, s, a, rd, st)
        print(("deleted its control Worker, its container application and its image's manifest" if out["deleted"]
               else "its control Worker was gone already; deleted what was left of it"), file=sys.stderr)
    else:
        remove_key(s)
    st = {k: v for k, v in R.read_state(rd).items()
          if k not in ("instance", "launching", "url", "do_id", "made_image", "worker_sent", "made_worker", "namespace",
                       "made_app", "deleting")}
    R.write_state(rd, dict(st, deleted=R.now()))
    return 0


def plan(R, s, a, common):
    """The build's plan for a container: its own first steps (providers/cloudflare/build.sh), x86_64, IDLE_STOP as the
    Worker's idle stop, no systemd, cloud-init or egress firewall of its own (the platform's outbound rules and the
    Worker's handlers are the boundary), a connection held open while it's set up (an open one keeps it from its idle
    stop)."""
    from cloud import ubuntu_vm
    try:
        idle = a.idle_of(s.get("IDLE_STOP"))
    except AdapterError as e:
        raise R.Refused(str(e)) from None
    return (f"kind=provider\narch=x86_64\nlabel={a.manifest['label']}\nidle_stop={idle}\n"
            f"egress={ubuntu_vm.check_egress(s.get('EGRESS'))}\n{common}\n"
            "target=cloudflare\nsystemd=no\ncloud_init=no\nguest_firewall=no\nkeep_awake=yes")


def cloudflare_computers(R):
    out = {}
    vms = os.path.join(R.LAB, "config", "vms")
    for f in sorted(os.listdir(vms)) if os.path.isdir(vms) else []:
        if not f.endswith(".env"):
            continue
        try:
            st = R.settings_for(f[:-4])
        except Exception:      # noqa: BLE001  (a file that doesn't resolve is listed by metacomputer list, not here)
            continue
        if st.get("PROVIDER") == "cloudflare":
            out[st["VM_NAME"]] = st
    return out


def cloud(R, args):
    """cloud status|doctor|setup cloudflare: this checkout's computers there (each Worker checked with its own key), the
    account's token checked; nothing to tear down (each computer's Worker goes with vm delete)."""
    import cloud as cloud_pkg
    comps = cloudflare_computers(R)
    states = {n: R.read_state(R.run_dir(c)) for n, c in comps.items()}
    if args.action == "teardown":
        raise R.Refused("nothing to tear down at Cloudflare: each computer's control Worker and container go with "
                        "metacomputer vm delete NAME")
    kw = provider_kw(R)
    if args.action == "setup":
        out = cloud_pkg.adapter("cloudflare", **kw).setup()
    else:
        rows, checks = [], []
        for n, c in comps.items():
            a = cloud_pkg.adapter("cloudflare", computers={n: c}, states={n: states[n]}, **provider_kw(R, c))
            if args.action == "status":
                try:
                    rows.append(dict(a.status(n), worker=cf.worker_of(c)))
                except AdapterError as e:
                    rows.append({"name": n, "worker": cf.worker_of(c), "state": "unknown", "note": str(e)})
            else:
                checks += a.doctor()[(1 if checks else 0):]
        if args.action == "status":
            out = {"provider": "cloudflare", "computers": rows,
                   "note": "this checkout's computers on Cloudflare, each asked through its own control Worker"}
        else:
            if not comps:
                checks = cloud_pkg.adapter("cloudflare", **kw).doctor()
            out = {"provider": "cloudflare", "checks": checks}
    print(json.dumps(out, indent=None if args.json else 2, sort_keys=True, default=str))
    return 0


# --- the `cloudflare` transport: lib/remote.py target, tunnel and probe ----------------------------------------------
def check_argv(R, args, ep):
    """How the tunnel's own program asks the broker about the computer: its identity now, as JSON {"id": ...}
    (lib/proxywrap.py --check, and the vault's binding)."""
    proxy = ep["proxy"]
    return [proxy[0], "check", *proxy[2:]]


def recorded(R, args, ep):
    """The computer's record, its Worker the one the settings name (from the record: no credential here)."""
    st = R.read_state(args.run_dir) if args.run_dir else {}
    iid = st.get("instance")
    if not iid:
        raise R.Refused(f"{args.vm} has no control Worker on record: nothing to connect to")
    worker, _do = cf.parse_instance(iid)
    if worker != ep["host"]:
        raise R.Refused(f"{args.vm}'s settings name the Worker {ep['host']}, but it was made as {worker}: no connection "
                        "was made")
    return st


def connect_wrapper(R, args, ep):
    """Every connection goes to the computer's own Worker and Durable Object, or none: the ProxyCommand asks the broker,
    right before each connection (lib/proxywrap.py --id), for the computer's identity as its Worker says it now, which
    must be the one recorded when it was made; the broker checks the object's id again on the connection itself."""
    st = recorded(R, args, ep)
    chk = check_argv(R, args, ep)
    return ("--id", st["instance"], "--check", str(len(chk)), *chk)


def tunnel(R, args, ep):
    """The vault's binding of a computer behind the broker: the tunnel's program and arguments, the computer's identity
    and how the same program asks about it."""
    st = recorded(R, args, ep)
    return ep["proxy"], check_argv(R, args, ep), st["instance"]


def probe(R, args, ep):
    """Without credentials: the broker says whether the container runs (reachable), or nothing does (unreachable)."""
    from . import broker as bk
    try:
        st = recorded(R, args, ep)
    except R.Refused:
        return "unknown"
    ans = bk.ask(ep["proxy"][3], {"op": "check", "worker": ep["host"]}, timeout=10)
    if not ans or ans.get("id") != st["instance"]:
        return "unreachable"
    return "reachable" if ans.get("state") == "running" else "unreachable"


# --- the egress ------------------------------------------------------------------------------------------------------
def require_narrowing(R, s, a, st, new_policy, setup=False, what="that"):
    """A change of the egress whose end state lets out anything what's in force doesn't runs only at the user's own
    terminal, however it's split into steps (adapter.widens). The one exception is the setup's own: --setup, while this
    computer's setup hasn't finished, opens it to EGRESS=internet for its downloads."""
    import owners
    cur = a.egress(s["VM_NAME"])
    if not cf.widens(cur.get("outside"), cur.get("allow") or [], new_policy):
        return
    if setup:
        if st.get("setup_done"):
            raise R.Refused(f"{s['VM_NAME']} is set up already: the setup's exception doesn't apply. Nothing changed")
        if new_policy != cf.policy_for("internet"):
            raise R.Refused("the setup opens the egress to EGRESS=internet, and nothing else. Nothing changed")
        return
    if not owners.at_user_terminal((0, 2)):
        raise R.Refused(f"{what} lets the computer reach more than what's in force does: only at your own terminal: "
                        f"metacomputer --vm {s['VM_NAME']} {what}. Nothing changed")


def egress_check(R, s, a, st, args):
    """net egress internet|off: the egress it ends with, judged before anything changes (require_narrowing)."""
    require_narrowing(R, s, a, st, cf.policy_for(args.set), args.setup, f"net egress {args.set}")


# --- commands only a computer here has ---------------------------------------------------------------------------------
EXEC_MAX_INPUT = 48 << 20


def cmd_exec(R, s, a, st, args):
    """A command in the container as root, through its Worker, with this command's standard input when asked (--stdin),
    only until it's set up (the Worker refuses it after). Two fixed forms stay open after: the boot script
    (`BOOT` or `BOOT restart-desktop`) and its logs (`BOOT logs N`). Never starts a stopped one."""
    argv = list(args.argv[1:] if args.argv[:1] == ["--"] else args.argv)
    if not argv:
        raise R.Refused("exec needs a command")
    if argv in ([BOOT], [BOOT, "restart-desktop"]):
        out = a.boot(s["VM_NAME"], restart_desktop=len(argv) == 2)
        print(f"booted in {round((out.get('booted_ms') or 0) / 1000.0, 1)} s", file=sys.stderr)
        return 0
    if len(argv) == 3 and argv[:2] == [BOOT, "logs"] and argv[2].isdigit():
        sys.stdout.buffer.write(a.logs(s["VM_NAME"], int(argv[2])))
        sys.stdout.flush()
        return 0
    data = sys.stdin.buffer.read(EXEC_MAX_INPUT + 1) if args.stdin else None
    if data is not None and len(data) > EXEC_MAX_INPUT:
        raise R.Refused(f"exec takes at most {EXEC_MAX_INPUT} bytes on its standard input")
    rc, out, err = a.exec(s["VM_NAME"], argv, input=data, timeout=args.timeout, max_output=args.max_output)
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    sys.stderr.buffer.write(err[-65536:])
    return rc if isinstance(rc, int) else 1


def cmd_checkpoint(R, s, a, st, args):
    """A computer's checkpoints: snapshots its Worker names. list, create (live: the computer keeps running), restore
    (what runs is discarded; it starts from the checkpoint) or delete (forgotten: Cloudflare keeps the snapshot until it
    expires, said)."""
    if not st.get("instance"):
        raise R.Refused(f"{s['VM_NAME']} has no control Worker that this Mac knows of")
    if args.action == "list":
        out = a.snapshots(s["VM_NAME"])
        if args.json:
            print(json.dumps(out, sort_keys=True))
        else:
            for c in out:
                print(f"{c['label']}\t{c['version']}\t{c.get('created') or ''}\tkept until {c.get('expires')}")
        return 0
    if not args.label:
        raise R.Refused(f"checkpoint {args.action} needs a name")
    t0 = time.monotonic()
    if args.action == "create":
        R.check_recorded(s, st, a)
        sync_setup_done(R, s, a, st)
        out = a.snapshot(s["VM_NAME"], args.label, replace=args.replace)
        if out.get("let_go"):
            print(f"metacomputer: the checkpoint it replaced is kept by Cloudflare until "
                  f"{cf.days((out['let_go'] or {}).get('expires'))} (its API can't delete one)", file=sys.stderr)
    elif args.action == "restore":
        R.check_recorded(s, st, a)
        out = a.restore(s["VM_NAME"], args.label)
        start_broker(R, s, a, st)
    else:
        out = a.delete_snapshot(s["VM_NAME"], args.label)
        if out.get("expires"):
            print(f"metacomputer: checkpoint {args.label} is forgotten; Cloudflare keeps its snapshot until "
                  f"{out['expires']} (its API can't delete one)", file=sys.stderr)
    out["seconds"] = round(time.monotonic() - t0, 2)
    print(json.dumps(out, sort_keys=True))
    return 0


def cmd_allow(R, s, a, st, args):
    """The egress as an allowlist of exact DNS names, each change read back. With a list in force, names are added or
    taken off (--remove); --clear goes back to EGRESS; --show prints what's in force. A change whose resulting egress
    lets out more than what's in force runs only at the user's own terminal."""
    if not st.get("instance"):
        raise R.Refused(f"{s['VM_NAME']} has no control Worker that this Mac knows of")
    name, setting = s["VM_NAME"], (s.get("EGRESS") or "internet")
    cur = a.egress(name)
    kind, have = cur.get("outside"), list(cur.get("allow") or [])
    if args.show:
        out = cur
    elif args.clear:
        require_narrowing(R, s, a, st, cf.policy_for(setting), what="net clear")
        out = a.apply_egress(name, setting)
    else:
        if not args.names:
            raise R.Refused("net allow and net remove need names")
        try:
            want = cf.policy_for(allow=args.names)["allow"]
        except ValueError as e:
            raise R.Refused(str(e)) from None
        if args.remove:
            if kind != "allowlist":
                raise R.Refused("no allowlist is in force to take names off (metacomputer net status)")
            left = [d for d in have if d not in want]
            new = cf.policy_for(allow=left) if left else cf.policy_for("off")
        else:
            new = cf.policy_for(allow=list(dict.fromkeys((have if kind == "allowlist" else []) + want)))
        require_narrowing(R, s, a, st, new, what=f"net {'remove' if args.remove else 'allow'} {' '.join(args.names)}")
        out = a.set_policy(name, new)
    if out.get("restarted"):
        start_broker(R, s, a, st)
    print(json.dumps({"outside": out.get("outside"), "allow": out.get("allow", []), "running": out.get("running"),
                      "restarted": bool(out.get("restarted"))}, sort_keys=True))
    return 0


# lib/remote.py's subcommand -> (what it's called in a refusal, the hook)
COMMANDS = {"exec": ("exec", cmd_exec), "checkpoint": ("checkpoints at the provider", cmd_checkpoint),
            "allow": ("an allowlist of names", cmd_allow)}
