"""Fly Sprites' part of lib/remote.py: what it does differently from a provider that makes a server (its launch through
Fly's command, its `sprite` transport's checks, its network policy as an allowlist of names, checkpoints, `sprite
exec`). lib/remote.py calls these hooks (lib/remote.py `hooks_for`) with itself as R, for its records, refusals and
locks; nothing here is run on its own.

    provider_kw(R, s)                     what the adapter is made with: the facade of Fly's sprite command
    create(R, s, a, rd, st, op)           the computer's Sprite, journaled like any launch
    abort(R, s, a, rd, st, op, keep)      a failed build's clean-up
    delete(R, s, a, rd, st, args)         vm delete's part at Fly
    plan(R, s, a, common)                 the build's plan (lib/remote.py cmd_plan; common: the lines every plan has)
    cloud(R, args)                        cloud status|doctor|setup|teardown sprites
    egress_check(R, s, a, st, args)       before net egress changes the policy: widening only at your own terminal
    transport hooks (the `sprite` transport): connect_wrapper, tunnel, probe
    COMMANDS                              exec, checkpoint and allow: commands only a Sprite has"""
import json
import os
import sys
import time

from cloud.adapter import AdapterError

from . import adapter as cloud_sprites

# Tests may hand in a stand-in for Fly's sprite command (a callable taking the command's path); never set otherwise.
CLI_FACTORY = None


def sprite_cli(path):
    if CLI_FACTORY is not None:
        return CLI_FACTORY(path)
    from . import cli
    return cli.SpriteCLI(path)


def provider_kw(R, s=None):
    """For Fly Sprites, the facade of its sprite command (which keeps its own sign-in)."""
    try:
        path = cloud_sprites.cli_path(dict(s or {}, SPRITE_CLI=(s or {}).get("SPRITE_CLI") or os.environ.get("SPRITE_CLI", "")))
    except AdapterError:
        return {}               # the adapter refuses what needs the command, saying so
    return {"cli": sprite_cli(path)}


def lost_sprite(a, s, st):
    """The Sprite a launch journaled in `st` made though its answer was lost: only one that carries that launch's
    operation label, set when Fly made it (providers/sprites/adapter.py owned_lost). A Sprite of that name without it
    is nobody's this Mac can name: AdapterError, and it's left alone. A name and a time are never enough."""
    if not st.get("launching"):
        return None
    return a.owned_lost(s["VM_NAME"], st["launching"])


def create(R, s, a, rd, st, op):
    """Fly Sprites: the computer's Sprite, journaled like any launch (lost answers are found by name and time), then
    its network policy at EGRESS=internet for the setup's downloads (the build sets EGRESS when it ends)."""
    lost = lost_sprite(a, s, st)
    if lost:
        print(f"metacomputer: an earlier launch of {s['VM_NAME']} made a Sprite it never recorded (its label shows it): "
              "deleting it", file=sys.stderr)
        a.cli.destroy(cloud_sprites.org_of(s), cloud_sprites.sprite_of(s))
    t0 = time.time()
    st = {k: v for k, v in st.items() if k not in ("kept", "aborted", "deleted")}
    try:
        R.write_state(rd, dict(st, provider=a.name, launching=op, launch_t=t0))
    except OSError as e:
        raise R.Refused(f"couldn't make the launch's journal durable ({e.strerror or e}): nothing was made") from None
    print(f"metacomputer: making {s['VM_NAME']} on {a.manifest['label']} (the Sprite {cloud_sprites.sprite_of(s)} in "
          f"{cloud_sprites.org_of(s)})", file=sys.stderr)
    made = a.create(s["VM_NAME"], persist=s.get("PERSIST") or "auto", op=op)
    st = {"provider": a.name, "created": R.now(), "launch_started": t0, "op": op, "made_by_build": True, **made}
    try:
        R.write_state(rd, st)
    except OSError as e:
        a.cli.destroy(made["org"], made["sprite"])
        raise R.Refused(f"couldn't record the Sprite it made ({e.strerror}), so it was deleted again; nothing is left "
                        "billing") from None
    a.states[s["VM_NAME"]] = st
    kind, _ = a.set_policy(s["VM_NAME"], cloud_sprites.rules_for("internet"))
    print(f"  the Sprite is made ({int(time.time() - t0)} s); its network policy: {kind} for the setup", file=sys.stderr)
    return 0


def abort(R, s, a, rd, st, op, keep):
    """A failed build's clean-up on Fly Sprites: the Sprite this operation made (its id recorded, or a lost launch
    found by name and time) is deleted; --keep leaves it (asleep, it bills its disk only)."""
    org, sprite = cloud_sprites.org_of(s), cloud_sprites.sprite_of(s)
    if not st.get("instance") and st.get("launching") == op:
        lost = lost_sprite(a, s, st)
        if lost:
            st = dict({k: v for k, v in st.items() if k != "launching"}, instance=lost.get("id"), sprite=sprite,
                      org=org, op=op)
            R.write_state(rd, st)
    if not st.get("instance"):
        st = {k: v for k, v in st.items() if k != "launching"}
        R.write_state(rd, dict(st, aborted=R.now()))
        print("nothing to delete: no Sprite was made", file=sys.stderr)
        return 0
    if keep:
        R.write_state(rd, dict(st, kept=R.now()))
        print("kept")
        return 0
    a.states[s["VM_NAME"]] = st
    a.delete(s["VM_NAME"])
    st = {k: v for k, v in st.items() if k not in ("instance", "launching", "op")}
    R.write_state(rd, dict(st, aborted=R.now()))
    print(f"deleted the Sprite this build made ({a.manifest['label']})", file=sys.stderr)
    return 0


def delete(R, s, a, rd, st, args):
    """vm delete at Fly: the recorded Sprite (with its disk and checkpoints), or one a lost launch made (its label)."""
    if st.get("instance"):
        gone = a.delete(s["VM_NAME"])["deleted"]
        print("deleted the Sprite, its disk and its checkpoints" if gone else "its Sprite was gone already",
              file=sys.stderr)
    else:
        lost = lost_sprite(a, s, st)
        if lost:
            a.cli.destroy(cloud_sprites.org_of(s), cloud_sprites.sprite_of(s))
            print("deleted a Sprite launched for it that this Mac had no record of (its label shows it)",
                  file=sys.stderr)
    st = {k: v for k, v in st.items() if k not in ("instance", "launching")}
    R.write_state(rd, dict(st, deleted=R.now()))
    return 0


def plan(R, s, a, common):
    """The build's plan for a Sprite: its own first steps (providers/sprites/build.sh), x86_64, no idle stop (it sleeps
    by itself), no systemd, cloud-init or firewall of its own (Fly's network policy is the one layer), and a connection
    held open while it's set up (a Sprite pauses about 10 s after its last one closes)."""
    from cloud import ubuntu_vm
    return (f"kind=provider\narch=x86_64\nlabel={a.manifest['label']}\nidle_stop=off\n"
            f"egress={ubuntu_vm.check_egress(s.get('EGRESS'))}\n{common}\n"
            "target=sprite\nsystemd=no\ncloud_init=no\nguest_firewall=no\nkeep_awake=yes")


def sprite_computers(R, extra_org=None):
    """The Sprites computers set up on this Mac (their settings, by name), and one stand-in per --org given, so the
    organization is listed even with no computer of it here."""
    out = {}
    vms = os.path.join(R.LAB, "config", "vms")
    for f in sorted(os.listdir(vms)) if os.path.isdir(vms) else []:
        if not f.endswith(".env"):
            continue
        try:
            st = R.settings_for(f[:-4])
        except Exception:      # noqa: BLE001  (a file that doesn't resolve is listed by metacomputer list, not here)
            continue
        if st.get("PROVIDER") == "sprites":
            out[st["VM_NAME"]] = st
    if extra_org:
        out.setdefault("-org-" + extra_org, {"VM_NAME": "-org-" + extra_org, "PROVIDER": "sprites",
                                             "SPRITE_ORG": extra_org, "SPRITE_NAME": "x"})
    return out


def cloud(R, args):
    """cloud status|doctor|setup sprites: every Sprite in the organizations of this Mac's computers (and --org);
    nothing to tear down (each computer's Sprite goes with vm delete)."""
    import cloud as cloud_pkg
    comps = sprite_computers(R, args.org)
    a = cloud_pkg.adapter("sprites", computers=comps, **provider_kw(R))
    if args.action == "status":
        out = {"provider": a.name, "sprites": a.list(),
               "note": "every Sprite in the organizations of this Mac's computers (and --org): here = one of them"}
    elif args.action == "doctor":
        out = {"provider": a.name, "checks": a.doctor()}
    elif args.action == "setup":
        out = a.setup()
    else:
        raise R.Refused("nothing to tear down at Fly Sprites: each computer's Sprite goes with metacomputer vm delete NAME")
    print(json.dumps(out, indent=None if args.json else 2, sort_keys=True, default=str))
    return 0


# --- the `sprite` transport: lib/remote.py target, tunnel and probe ------------------------------------------------
def sprite_check_argv(R, args, ep):
    """How Fly's command (the tunnel's own program) is asked about the Sprite: its record from the API, whose "id" must
    be the one recorded when it was made (lib/proxywrap.py --check, and the vault's binding)."""
    org = cloud_sprites.org_of({"SPRITE_ORG": R.setting(args, "SPRITE_ORG")})
    return [ep["proxy"][0], "-o", org, "api", f"/v1/sprites/{ep['host']}", "--", "-sS"]


def check_sprite(R, args, ep, state=None):
    """The Sprite a connection (or the vault's binding) goes to is the computer's own: made under the organization and
    name the settings say, and the Sprite of that name at Fly has the id recorded when it was made. Refused otherwise
    (one deleted and made again under the name is another, whoever made it). Returns its record."""
    state = R.read_state(args.run_dir) if state is None and args.run_dir else (state or {})
    want = state.get("instance")
    if not want:
        raise R.Refused(f"{args.vm} has no Sprite on record: nothing to connect to")
    org = cloud_sprites.org_of({"SPRITE_ORG": R.setting(args, "SPRITE_ORG")})
    if (state.get("org") and state["org"] != org) or (state.get("sprite") and state["sprite"] != ep["host"]):
        raise R.Refused(f"{args.vm}'s settings name {ep['host']} in {org}, but it was made as {state.get('sprite')} in "
                        f"{state.get('org')}: no connection was made")
    cur = sprite_cli(ep["proxy"][0]).info(org, ep["host"])
    if cur is None:
        raise R.Refused(f"{args.vm}'s Sprite is gone from Fly: no connection was made (metacomputer vm delete {args.vm})")
    if cur.get("id") != want:
        raise R.Refused(f"the Sprite named {ep['host']} in {org} isn't the one {args.vm} was made as (another id): no "
                        "connection was made")
    return state


def connect_wrapper(R, args, ep):
    """Every connection goes to the computer's own Sprite, or none: checked now, and again by the ProxyCommand right
    before each connection's tunnel starts (lib/proxywrap.py --id), so arguments a caller keeps (scripts/lib.sh keeps
    them for the rest of its command, the hub for its life) never reach a Sprite made again under the name."""
    st = check_sprite(R, args, ep)
    chk = sprite_check_argv(R, args, ep)
    return ("--id", st["instance"], "--check", str(len(chk)), *chk)


def tunnel(R, args, ep):
    """The vault's binding of a computer behind Fly's tunnel (lib/remote.py cmd_tunnel): the tunnel's program and
    arguments, the Sprite's id and how the same program asks Fly about it, all from one look at the computer's own
    Sprite (check_sprite)."""
    st = check_sprite(R, args, ep)
    return ep["proxy"], sprite_check_argv(R, args, ep), st.get("instance")


def probe(R, args, ep):
    """A connection through the tunnel would wake (and bill) a Sprite: Fly's API says asleep or awake instead, without
    waking it."""
    try:
        cur = sprite_cli(ep["proxy"][0]).info(cloud_sprites.org_of({"SPRITE_ORG": R.setting(args, "SPRITE_ORG")}),
                                              ep["host"])
        want = (R.read_state(args.run_dir) if args.run_dir else {}).get("instance")
        return ("absent" if cur is None or (want and cur.get("id") != want)
                else "awake" if cur.get("status") == "running" else "asleep")
    except (AdapterError, R.Refused):
        return "unknown"


# --- the network policy ---------------------------------------------------------------------------------------------
def require_narrowing(R, s, a, st, new_rules, setup=False, what="that"):
    """Fly Sprites: a change of the network policy whose end state lets out anything the policy in force doesn't runs
    only at the user's own terminal, however it's split into steps (judged on the policy it ends with:
    providers/sprites/adapter.py widens). The one exception is the setup's own: --setup, while this computer's setup
    hasn't finished (its record says so: setup-done), opens it for the packages it downloads, and the build sets
    EGRESS after."""
    import owners
    cur = a.egress(s["VM_NAME"])
    if not cloud_sprites.widens(cur.get("outside"), cur.get("allow") or [], new_rules):
        return
    if setup:
        if st.get("setup_done"):
            raise R.Refused(f"{s['VM_NAME']} is set up already: the setup's exception doesn't apply. Nothing changed")
        if new_rules != cloud_sprites.rules_for("internet"):
            raise R.Refused("the setup opens the policy to every name, and nothing else. Nothing changed")
        return
    if not owners.at_user_terminal((0, 2)):
        raise R.Refused(f"{what} lets the computer reach more than the policy in force does: only at your own terminal: "
                        f"metacomputer --vm {s['VM_NAME']} {what}. Nothing changed")


def egress_check(R, s, a, st, args):
    """net egress internet|off: the policy it ends with, judged before anything changes (require_narrowing)."""
    require_narrowing(R, s, a, st, cloud_sprites.rules_for(args.set), args.setup, f"net egress {args.set}")


# --- commands only a Sprite has ---------------------------------------------------------------------------------------
EXEC_MAX_INPUT = 64 << 20          # the bootstrap's folder is about 30 KB


def cmd_exec(R, s, a, st, args):
    """A command in the Sprite through Fly's own authenticated channel (`sprite exec`), with this command's standard
    input; its output and exit status are the command's. The setup's first step (before its own SSH exists)."""
    argv = list(args.argv[1:] if args.argv[:1] == ["--"] else args.argv)
    if not argv:
        raise R.Refused("exec needs a command")
    # Its standard input only when asked (--stdin): a caller's own stdin may be a pipe that never ends (found live:
    # desktop-restart from a background shell waited on it for good).
    data = sys.stdin.buffer.read(EXEC_MAX_INPUT + 1) if args.stdin else None
    if data is not None and len(data) > EXEC_MAX_INPUT:
        raise R.Refused(f"exec takes at most {EXEC_MAX_INPUT} bytes on its standard input")
    rc, out, err = a.exec(s["VM_NAME"], argv, input=data, timeout=args.timeout, max_output=args.max_output)
    sys.stdout.buffer.write(out)
    sys.stdout.flush()
    sys.stderr.buffer.write(err[-65536:])
    return rc


def cmd_checkpoint(R, s, a, st, args):
    """A provider's snapshots: list, create, restore or delete the checkpoint called LABEL (Fly Sprites)."""
    if not st.get("instance"):
        raise R.Refused(f"{s['VM_NAME']} has no Sprite that this Mac knows of")
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
    fn = {"create": a.snapshot, "restore": a.restore, "delete": a.delete_snapshot}[args.action]
    t0 = time.monotonic()
    older = [c["version"] for c in a.snapshots(s["VM_NAME"]) if c["label"] == args.label] if args.replace else []
    out = fn(s["VM_NAME"], args.label)
    out["seconds"] = round(time.monotonic() - t0, 2)
    if older:                        # --replace: the new one is made first, then the older ones of that name go
        for v in older:
            a.cli.delete_checkpoint(cloud_sprites.org_of(s), cloud_sprites.sprite_of(s), v)
        out["replaced"] = older
    print(json.dumps(out, sort_keys=True))
    return 0


def cmd_allow(R, s, a, st, args):
    """Fly Sprites: the network policy as an allowlist of DNS names (each with its subdomains unless --exact), each change
    read back. With a list in force, names are added or taken off (--remove); --clear goes back to EGRESS; --show
    prints what's in force. A change whose resulting policy lets out more than the one in force only at the user's own
    terminal (require_narrowing), judged on where it ends, whatever steps led there."""
    if not st.get("instance"):
        raise R.Refused(f"{s['VM_NAME']} has no Sprite that this Mac knows of")
    name, setting = s["VM_NAME"], (s.get("EGRESS") or "internet")
    cur = a.egress(name)
    kind, have = cur.get("outside"), list(cur.get("allow") or [])
    if args.show:
        out = cur
    elif args.clear:
        require_narrowing(R, s, a, st, cloud_sprites.rules_for(setting), what="net clear")
        out = a.apply_egress(name, setting)
    else:
        if not args.names:
            raise R.Refused("net allow and net remove need names")
        try:
            want = [r["domain"] for r in cloud_sprites.rules_for(allow=args.names, exact=args.exact)]
        except ValueError as e:
            raise R.Refused(str(e)) from None
        if args.remove:
            if kind != "allowlist":
                raise R.Refused("no allowlist is in force to take names off (metacomputer net status)")
            left = [d for d in have if d not in want]
            new_rules = cloud_sprites.rules_for(allow=left, exact=True) if left else cloud_sprites.rules_for("off")
        else:
            new = list(dict.fromkeys((have if kind == "allowlist" else []) + want))
            new_rules = cloud_sprites.rules_for(allow=new, exact=True)
        require_narrowing(R, s, a, st, new_rules, what=f"net {'remove' if args.remove else 'allow'} {' '.join(args.names)}")
        kind_new, names_new = cloud_sprites.policy_kind(new_rules)
        out = a.set_egress(name, names_new, exact=True) if kind_new == "allowlist" else a.apply_egress(name, "off")
    print(json.dumps({"outside": out.get("outside"), "allow": out.get("allow", [])}, sort_keys=True))
    return 0


# lib/remote.py's subcommand -> (what it's called in a refusal, the hook)
COMMANDS = {"exec": ("exec", cmd_exec), "checkpoint": ("checkpoints at the provider", cmd_checkpoint),
            "allow": ("an allowlist of names", cmd_allow)}
