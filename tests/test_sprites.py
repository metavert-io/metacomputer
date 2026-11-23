#!/usr/bin/env python3
"""Fly Sprites (docs/sprites.md), against a stand-in `sprite` command. No VM, no network, no Fly account.

    python3 tests/test_sprites.py

- The `sprite` transport (lib/cloud/sprites.py opener): ssh through `sprite proxy -W :22` as the ProxyCommand, run
  through lib/proxywrap.py, the host key pinned under mcomp-NAME; bad organizations, Sprite names and CLI paths refused.
- The network policy: EGRESS=internet allows every name, off denies every name, an allowlist lists names (each with its
  subdomains unless exact); addresses and anything that isn't a DNS name refused (Fly takes anything); what's read
  back is classified (none: nothing enforced).
- The adapter through the facade of Fly's command (lib/spritecli.py) and a stand-in for it: create (refused when a
  Sprite of that name exists), wake, sleep, status, delete (never another Sprite of the same name), checkpoints by
  name (create, list, restore, delete), the policy set and read back; the command runs with HOME, PATH and LANG only,
  and `sprite exec` never forwards ports back to the Mac.
- lib/remote.py: a launch journaled before the Sprite is made; a lost launch (its answer never came) found by name
  and time and deleted, and an older Sprite of that name (someone else's) left alone; a failed build's abort.
- The hub on a computer without background input: keys, typing and pixel clicks sent in the foreground, a note said;
  actions on an element left on their route.
- The guest: the service scripts hide /.sprite in a private mount namespace before the desktop and sshd start, sshd
  listens on the Sprite's loopback by number, the browsers' own DNS-over-HTTPS is locked off.
- The CLI in a copy of the checkout (tests/test_multi_vm.py's Box), with the stand-in as SPRITE_CLI: vm create --on
  sprites and its refusals, ssh_base through Fly's tunnel, net status/egress/allow/remove/clear (widening only at the
  user's own terminal), checkpoints, stop, cloud status, vm delete.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(LAB, "lib"))
sys.path.insert(0, TESTS)
import cloud  # noqa: E402
import mcp_hub  # noqa: E402
import remote  # noqa: E402
import spritecli  # noqa: E402
from cloud import capabilities as caps  # noqa: E402
from cloud import sprites, transport  # noqa: E402
from cloud.adapter import AdapterError  # noqa: E402

FAILS = []
PY = os.environ.get("MCOMP_TEST_PY") or shutil.which("python3") or sys.executable

# A stand-in for Fly's `sprite` command: the Sprites API (`api PATH -- curl options`) and `exec`, kept in a JSON file
# in $HOME (the facade gives it only HOME, PATH and LANG). Every call is logged with the environment's names.
STANDIN = r'''#!PY
import json, os, re, sys, time
home = os.environ.get("HOME", "/nonexistent")
state_f, log_f = os.path.join(home, ".sprite-standin.json"), os.path.join(home, ".sprite-standin.log")
st = json.load(open(state_f)) if os.path.exists(state_f) else {"sprites": {}, "n": 0}
argv = sys.argv[1:]
with open(log_f, "a") as f:
    f.write(json.dumps({"argv": argv, "env": sorted(os.environ)}) + "\n")
def save():
    json.dump(st, open(state_f, "w"))
org = sprite = None
while argv and argv[0] in ("-o", "-s"):
    if argv[0] == "-o": org = argv[1]
    else: sprite = argv[1]
    argv = argv[2:]
if st.get("fail"):
    print("Error: not signed in", file=sys.stderr); sys.exit(1)
cmd = argv[0] if argv else ""
if cmd == "api":
    path = argv[1]; rest = argv[argv.index("--") + 1:] if "--" in argv else []
    method = rest[rest.index("-X") + 1] if "-X" in rest else "GET"
    body = json.loads(sys.stdin.read() or "null") if "--data-binary" in rest else None
    def out(obj, code):     # the status line only when asked for it (-w), as curl does
        sys.stdout.write((obj if isinstance(obj, str) else json.dumps(obj)) + ("\n" + str(code) if "-w" in rest else ""))
        sys.exit(0)
    st["api_n"] = st.get("api_n", 0) + 1; save()
    if st.get("api_fail_after") is not None and st["api_n"] > st["api_fail_after"]:
        print("Error: Fly didn't answer", file=sys.stderr); sys.exit(1)     # Fly unreachable from this call on
    sp = st["sprites"]
    m = re.fullmatch(r"/v1/sprites(?:\?prefix=(.*))?", path)
    if m and method == "GET":
        items = [v for k, v in sorted(sp.items()) if v["org"] == org and (not m.group(1) or k.startswith(m.group(1)))]
        out({"sprites": [dict(v["info"]) for v in items], "data": [dict(v["info"]) for v in items]}, 200)
    if path == "/v1/sprites" and method == "POST":
        name = body["name"]
        if name in sp: out({"error": "exists"}, 409)
        st["n"] += 1
        info = {"id": "sprite-%04d" % st["n"], "name": name, "status": "cold", "organization": org,
                "created_at": st.get("now") or time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())}
        if body.get("labels"):
            info["labels"] = list(body["labels"])
        sp[name] = {"org": org, "info": info, "policy": [], "checkpoints": [], "v": 1}
        save()
        if st.get("lose_create"):
            sys.exit(1)
        out(info, 201)
    m = re.fullmatch(r"/v1/sprites/([a-z0-9-]+)(/.*)?", path)
    if not m: out({"error": "no route"}, 404)
    name, sub = m.group(1), m.group(2) or ""
    if name not in sp or sp[name]["org"] != org: out({"error": "not found"}, 404)
    s = sp[name]
    if sub == "" and method == "GET": out(s["info"], 200)
    if sub == "" and method == "DELETE":
        del sp[name]; save(); out("", 204)
    if sub == "/policy/network" and method == "GET": out({"rules": s["policy"]}, 200)
    if sub == "/policy/network" and method == "POST":
        s["policy"] = body["rules"]; save(); out("", 204)
    if sub == "/checkpoints" and method == "GET":
        out([{"id": "Current"}] + [dict(c) for c in reversed(s["checkpoints"])], 200)
    if sub == "/checkpoint" and method == "POST":
        s["v"] += 1; v = "v%d" % s["v"]
        s["checkpoints"].append({"id": v, "comment": body.get("comment"), "create_time": "2026-11-23T00:00:%02dZ" % s["v"]})
        save()
        out('{"type":"info","data":"x"}\n{"type":"complete","data":"Checkpoint %s created successfully"}' % v, 200)
    m2 = re.fullmatch(r"/checkpoints/(v[0-9]+)(/restore)?", sub)
    if m2 and method == "POST" and m2.group(2):
        s["restored"] = m2.group(1); save(); out('{"type":"complete","data":"restored"}', 200)
    if m2 and method == "DELETE" and not m2.group(2):
        s["checkpoints"] = [c for c in s["checkpoints"] if c["id"] != m2.group(1)]; save(); out("", 204)
    out({"error": "no route"}, 404)
if cmd == "exec":
    if "--no-port-forward" not in argv:
        print("STANDIN: exec without --no-port-forward", file=sys.stderr); sys.exit(99)
    rest = argv[argv.index("--") + 1:]
    data = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    st.setdefault("execs", []).append({"sprite": sprite, "argv": rest, "stdin": len(data)}); save()
    if rest == ["true"]: sys.exit(0)
    if rest[:1] == ["flood"]:              # a guest's output that never ends (a hostile log line)
        chunk = b"x" * 65536
        try:
            while True:
                sys.stdout.buffer.write(chunk)
        except BrokenPipeError:
            sys.exit(0)
    if "bootstrap.sh" in " ".join(rest):
        print("bootstrap: accounts ready"); print("HOSTKEY ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq5" + "A" * 39)
    sys.exit(0)
if cmd == "proxy":
    sys.exit(1)       # no SSH here: a connection through it fails at once
print("STANDIN: unknown " + " ".join(argv), file=sys.stderr); sys.exit(2)
'''


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


def make_standin(d):
    p = os.path.join(d, "sprite")
    with open(p, "w") as f:
        f.write(STANDIN.replace("#!PY", "#!" + PY, 1))
    os.chmod(p, 0o755)
    return p


def standin_state(home):
    p = os.path.join(home, ".sprite-standin.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def set_state(home, **kw):
    st = standin_state(home) or {"sprites": {}, "n": 0}
    st.update(kw)
    json.dump(st, open(os.path.join(home, ".sprite-standin.json"), "w"))


def standin_log(home):
    p = os.path.join(home, ".sprite-standin.log")
    return [json.loads(x) for x in open(p)] if os.path.exists(p) else []


def transport_and_policy():
    print("the sprite transport and the network policy")
    s = {"VM_NAME": "box", "TRANSPORT": "sprite", "SPRITE_ORG": "my-org", "SPRITE_NAME": "box-sprite",
         "SPRITE_CLI": "/opt/fly/bin/sprite"}
    ep = transport.endpoint(s)
    check("the sprite transport: Fly's tunnel to the Sprite's port 22 as the ProxyCommand, pinned as mcomp-NAME",
          ep == {"host": "box-sprite", "port": 22, "alias": "mcomp-box", "transport": "sprite",
                 "proxy": ["/opt/fly/bin/sprite", "-o", "my-org", "-s", "box-sprite", "proxy", "-W", ":22"]}, ep)
    t = transport.ssh_target(ep, "cua", wrapper=("/py", "/lib/proxywrap.py"))
    check("...run through lib/proxywrap.py (which ends it when ssh goes), the host key under its alias",
          "ProxyCommand=/py /lib/proxywrap.py -- /opt/fly/bin/sprite -o my-org -s box-sprite proxy -W :22" in t
          and "HostKeyAlias=mcomp-box" in t and t[-1] == "cua@box-sprite", t)
    check("...the Sprite's name defaults to the computer's", transport.endpoint(dict(s, SPRITE_NAME=""))["host"] == "box")
    pw = os.path.join(LAB, "lib", "proxywrap.py")
    for argv in (["--id", "x", "--check", "1", "/bin/echo", "--", "/bin/true"],            # not an id
                 ["--id", "sprite-1", "--check", "0", "--", "/bin/true"],                  # no check program
                 ["--id", "sprite-1", "--check", "2", "/bin/echo", "--", "/bin/true"],     # the count runs into the tunnel
                 ["--id", "sprite-1", "--check", "1", "echo", "--", "/bin/true"],          # not an absolute path
                 ["--id", "sprite-1", "--", "/bin/true"]):
        rr = subprocess.run([PY, pw, *argv], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        check(f"proxywrap {' '.join(argv[:4])}...: refused (usage)", rr.returncode == 2 and "usage" in rr.stderr, rr.stderr)
    for k, v, want in (("SPRITE_ORG", "", "SPRITE_ORG"), ("SPRITE_ORG", "-o x", "SPRITE_ORG"),
                       ("SPRITE_NAME", "Box_1", "SPRITE_NAME"), ("SPRITE_NAME", "-s", "SPRITE_NAME"),
                       ("SPRITE_CLI", "sprite", "absolute"), ("SPRITE_CLI", "/a b/sprite", "absolute")):
        why = raises(transport.endpoint, dict(s, **{k: v}), kind=AdapterError)
        check(f"{k}={v!r} refused", why is not None and want in why and not why.startswith("WRONG"), why)
    r = sprites.rules_for
    check("EGRESS=internet: every name", r("internet") == [{"domain": "*", "action": "allow"}])
    check("EGRESS=off: no name", r("off") == [{"domain": "*", "action": "deny"}])
    check("an allowlist: each name with its subdomains", r(allow=["Example.com."]) ==
          [{"domain": "example.com", "action": "allow"}, {"domain": "*.example.com", "action": "allow"}])
    check("...--exact: the name only; *.name: the subdomains only",
          r(allow=["example.com"], exact=True) == [{"domain": "example.com", "action": "allow"}]
          and r(allow=["*.example.com"]) == [{"domain": "*.example.com", "action": "allow"}])
    for bad in ("10.0.0.1", "169.254.169.254", "*", "localhost", "exa mple.com", "-x.com", "[::1]", "a..b", ""):
        check(f"allowlist name {bad!r} refused (Fly's API takes anything)", raises(r, allow=[bad], kind=ValueError) is not None)
    check("...and a list too long", raises(r, allow=[f"n{i}.example.com" for i in range(40)], kind=ValueError) is not None)
    check("EGRESS that isn't one refused", raises(r, "maybe", kind=AdapterError) is not None)
    w = sprites.widens
    A = r(allow=["a.com"])
    cases = [("internet", [], r("off"), False), ("internet", [], A, False), ("internet", [], r("internet"), False),
             ("off", [], r("internet"), True), ("off", [], A, True), ("off", [], r("off"), False),
             ("allowlist", ["a.com", "*.a.com"], r("internet"), True),
             ("allowlist", ["a.com", "*.a.com"], r(allow=["a.com"], exact=True), False),
             ("allowlist", ["a.com"], r(allow=["a.com"]), True),
             ("allowlist", ["a.com", "*.a.com"], r(allow=["b.com"]), True),
             ("other", [], r("internet"), True), ("other", [], A, True), ("other", [], r("off"), False),
             (None, [], r("internet"), True), ("none", [], r("internet"), False)]
    bad = [c for c in cases if w(c[0], c[1], c[2]) != c[3]]
    check("widens(): judged on the policy a change ends with (found by review: off, then every name, got through); "
          "nothing never widens, every name widens anything but every name, a list widens unless each name is allowed, "
          "and what can't be told counts as narrowest", not bad, bad)
    pk = sprites.policy_kind
    check("read back: internet, off, an allowlist, none (no rules: nothing enforced), other",
          pk(r("internet"))[0] == "internet" and pk(r("off"))[0] == "off" and pk([])[0] == "none"
          and pk(r(allow=["a.com"])) == ("allowlist", ["a.com", "*.a.com"])
          and pk([{"domain": "a.com", "action": "deny"}])[0] == "other"
          and pk([{"domain": "10.0.0.1", "action": "allow"}])[0] == "other" and pk(None)[0] == "other")


def manifest():
    print("\nthe manifest")
    m = sprites.MANIFEST
    check("sound", caps.check_manifest(m) == [], caps.check_manifest(m))
    check("registered: the place sprites, in the group cloud", cloud.by_place("sprites") is sprites.Sprites
          and cloud.providers().get("sprites") == "Fly Sprites")
    check("its truth: a disk that keeps everything, checkpoints, sleep that keeps programs, an allowlist of names",
          m["root_disk"] == "persistent" and m["snapshots"] and m["sleep_wake"] == {"kind": "auto-sleep", "keeps_processes": True}
          and m["egress"]["allowlist"] and caps.choose_persist(m) == "disk")
    check("...enforced outside the VM: private ranges, the metadata service, and off",
          m["outside_egress"] == {"enforced": True, "private_ranges": True, "metadata": True, "off": True})
    check("...asleep, only its disk is billed; no guest power-off to speak of",
          m["billing"]["stopped"] == "storage" and m["billing"]["guest_poweroff"] == "n/a")
    check("...no background input (no /dev/uinput): declared, and the feature map says so",
          m["input"]["background"] is False and "uinput" in m["input"]["note"]
          and "Fly Sprites" in (caps.missing(m, "background_input") or ""))
    check("...every other provider has it", all(c.manifest["input"]["background"] for n, c in cloud.ADAPTERS.items()
                                                 if n != "sprites"))
    lib = open(os.path.join(LAB, "scripts", "lib.sh")).read()
    off = {n for n, c in cloud.ADAPTERS.items() if not c.manifest["input"]["background"]}
    check("scripts/lib.sh tells the hub which providers lack it (MCOMP_BACKGROUND_INPUT), as their manifests say",
          all(f"{n}) printf off" in lib for n in off) and off == {"sprites"}, off)
    rt = sprites.Sprites().rates()
    check("rates: awake a few cents an hour driven now and then, asleep its disk only", 0 < rt["awake_per_hour"] < 0.1
          and 0 < rt["asleep_per_hour"] < 0.001 and "8 CPUs" in rt["note"], rt)


def adapter(td):
    print("\nthe adapter, through the facade of Fly's command and a stand-in")
    home = os.path.join(td, "home-a")
    os.makedirs(home)
    cli_path = make_standin(td)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    os.environ["OCI_USER"] = "must-not-pass"         # nothing of the caller's environment reaches the command
    try:
        cli = spritecli.SpriteCLI(cli_path)
        comp = {"VM_NAME": "box", "PROVIDER": "sprites", "PLACE": "sprites", "SPRITE_ORG": "my-org", "SPRITE_NAME": "box",
                "EGRESS": "internet", "SPRITE_CLI": cli_path}
        a = sprites.Sprites(computers={"box": comp}, states={"box": {}}, cli=cli)
        check("create needs its build's operation", raises(a.create, "box", kind=AdapterError) is not None)
        made = a.create("box", op="e" * 32)
        check("create: the Sprite, its id recorded", made["instance"] == "sprite-0001" and made["sprite"] == "box"
              and made["org"] == "my-org", made)
        check("...labelled, when Fly makes it, with metacomputer's label and its build's operation",
              standin_state(home)["sprites"]["box"]["info"].get("labels") == ["metacomputer", "mcop-" + "e" * 32])
        why = raises(a.create, "box", op="e" * 32, kind=AdapterError)
        check("create again: refused, a Sprite of that name exists (never someone else's)", why and "already" in why, why)
        log = standin_log(home)
        check("the command runs with HOME, PATH and LANG only (no caller's variables, no credentials)",
              all(set(c["env"]) <= {"HOME", "PATH", "LANG", "__CF_USER_TEXT_ENCODING", "PWD", "SHLVL", "_"} for c in log)
              and not any("OCI_USER" in c["env"] for c in log), log[-1]["env"])
        a.states["box"] = made
        check("set_policy: set and read back", a.set_policy("box", sprites.rules_for("internet")) == ("internet", []))
        set_state(home, sprites=dict(standin_state(home)["sprites"]))
        st = a.status("box")
        check("status: asleep (Fly's word: cold), the Sprite and its organization", st["state"] == "asleep"
              and st["provider_state"] == "cold" and st["org"] == "my-org", st)
        w = a.wake("box")
        ex = standin_state(home)["execs"][-1]
        check("wake: one command (true) through sprite exec, timed", "woke_in_s" in w and ex["argv"] == ["true"], ex)
        check("...and exec never forwards the Sprite's ports back to this Mac (--no-port-forward)",
              all("--no-port-forward" in c["argv"] for c in standin_log(home) if "exec" in c["argv"]))
        check("sleep: nothing to send (it pauses by itself)", a.sleep("box")["state"] == "asleep")
        # checkpoints by name
        a.snapshot("box", "baseline")
        a.snapshot("box", "ck1")
        a.snapshot("box", "ck1")
        names = [(x["label"], x["version"]) for x in a.snapshots("box")]
        check("checkpoints: named by their comment, newest first", names == [("ck1", "v4"), ("ck1", "v3"), ("baseline", "v2")],
              names)
        check("...a name means its newest", a.version_of("box", "ck1") == "v4")
        out = a.restore("box", "ck1")
        check("restore: the newest of that name, and it says the programs restarted", out["version"] == "v4"
              and standin_state(home)["sprites"]["box"]["restored"] == "v4" and "restarted" in out["note"], out)
        check("restore of a name there isn't: refused", raises(a.restore, "box", "nope", kind=AdapterError) is not None)
        a.delete_snapshot("box", "ck1")
        check("delete a checkpoint: every one of that name", [x["label"] for x in a.snapshots("box")] == ["baseline"])
        # egress
        e = a.apply_egress("box", "off")
        check("EGRESS=off: deny every name, read back", e["outside"] == "off"
              and standin_state(home)["sprites"]["box"]["policy"] == [{"domain": "*", "action": "deny"}], e)
        e = a.set_egress("box", ["example.com"])
        check("an allowlist: read back with its names", e["outside"] == "allowlist" and e["allow"] == ["example.com", "*.example.com"], e)
        check("...an address refused (nothing sent)", raises(a.set_egress, "box", ["1.1.1.1"], kind=AdapterError) is not None)
        # someone else's Sprite of the same name
        sp = standin_state(home)["sprites"]
        sp["box"]["info"]["id"] = "sprite-other"
        set_state(home, sprites=sp)
        check("status: a Sprite of that name with another id is another one (absent here)", a.status("box")["state"] == "absent")
        # Found by review: only status and delete checked the id; the rest addressed the Sprite by name.
        pol_before = standin_state(home)["sprites"]["box"]["policy"]
        n_cp = len(standin_state(home)["sprites"]["box"]["checkpoints"])
        restored_before = standin_state(home)["sprites"]["box"].get("restored")
        refused = {}
        for what, fn in (("set_policy", lambda: a.set_policy("box", sprites.rules_for("off"))),
                         ("apply_egress", lambda: a.apply_egress("box", "internet")),
                         ("set_egress", lambda: a.set_egress("box", ["example.org"])),
                         ("egress", lambda: a.egress("box")),
                         ("wake", lambda: a.wake("box")),
                         ("exec", lambda: a.exec("box", ["id"])),
                         ("snapshot", lambda: a.snapshot("box", "ck9")),
                         ("snapshots", lambda: a.snapshots("box")),
                         ("restore", lambda: a.restore("box", "baseline")),
                         ("delete_snapshot", lambda: a.delete_snapshot("box", "baseline"))):
            refused[what] = raises(fn, kind=AdapterError)
        st_now = standin_state(home)["sprites"]["box"]
        check("a Sprite of that name with another id (deleted and made again): policy, checkpoints, restore, exec, wake "
              "all refused, and nothing reached it",
              all(v and "isn't the one" in v for v in refused.values()) and st_now["policy"] == pol_before
              and len(st_now["checkpoints"]) == n_cp and st_now.get("restored") == restored_before
              and not any(e["argv"] == ["id"] for e in standin_state(home).get("execs", [])), refused)
        a.states["box"] = dict(made, org="other-org")
        sp["box"]["info"]["id"] = "sprite-0001"
        set_state(home, sprites=sp)
        check("...and settings that name another organization than it was made in: refused too",
              "it was made as" in (raises(a.set_policy, "box", sprites.rules_for("off"), kind=AdapterError) or ""))
        a.states["box"] = made
        sp["box"]["info"]["id"] = "sprite-other"
        set_state(home, sprites=sp)
        why = raises(a.delete, "box", kind=AdapterError)
        check("delete: never a Sprite of that name with another id", why and "isn't the one" in why
              and "box" in standin_state(home)["sprites"], why)
        sp["box"]["info"]["id"] = "sprite-0001"
        set_state(home, sprites=sp)
        check("delete: its own", a.delete("box") == {"deleted": True} and "box" not in standin_state(home)["sprites"])
        # Output the command prints is capped (found by review: a guest's log line could fill this Mac's memory).
        small = spritecli.SpriteCLI(cli_path, max_stdout=1 << 20)
        why = raises(small.exec, "my-org", "box", ["flood"], timeout=60, kind=AdapterError)
        check("a command whose output never ends: ended past the cap (1 MB here), refused, nothing passed on",
              why and "more than 1048576 bytes" in why, why)
        why = raises(spritecli.SpriteCLI(cli_path).exec, "my-org", "box", ["flood"], timeout=60, max_output=4096,
                     kind=AdapterError)
        check("...and exec's own cap (remote.py exec --max-output) applies", why and "more than 4096 bytes" in why, why)
        set_state(home, fail=True)
        why = raises(a.status, "box", kind=AdapterError)
        check("a command that isn't signed in: refused, saying so (never its output whole)", why and "sprite login" in why, why)
        set_state(home, fail=False)
        check("doctor with no command given: says so, runs nothing", sprites.Sprites().doctor()[0]["ok"] is False)
    finally:
        os.environ.pop("OCI_USER", None)
        if old_home is not None:
            os.environ["HOME"] = old_home


def journal(td):
    print("\nlib/remote.py: the launch journal, lost launches, abort")
    home = os.path.join(td, "home-j")
    os.makedirs(home)
    cli_path = make_standin(td)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        rd = os.path.join(td, "run")
        comp = {"VM_NAME": "box", "PROVIDER": "sprites", "PLACE": "sprites", "SPRITE_ORG": "my-org", "SPRITE_NAME": "box",
                "EGRESS": "internet", "SPRITE_CLI": cli_path, "RUN_DIR": rd}

        def adapter_now():
            return sprites.Sprites(computers={"box": comp}, states={"box": remote.read_state(rd)},
                                   cli=spritecli.SpriteCLI(cli_path))
        # The answer to the create is lost: the Sprite exists, the record says only `launching`.
        set_state(home, lose_create=True)
        why = raises(remote.create_sprite, comp, adapter_now(), rd, remote.read_state(rd), "a" * 32, kind=Exception)
        st = remote.read_state(rd)
        check("a launch is journaled before the Sprite is made (its answer lost: launching, no instance)",
              why is not None and st.get("launching") == "a" * 32 and not st.get("instance") and "box" in standin_state(home)["sprites"], st)
        set_state(home, lose_create=False)
        r = remote.create_sprite(comp, adapter_now(), rd, remote.read_state(rd), "b" * 32)
        st = remote.read_state(rd)
        log = [c for c in standin_log(home) if "DELETE" in c["argv"]]
        check("the next try finds the lost launch by its operation's label, deletes it, then makes its own",
              r == 0 and st.get("instance") == "sprite-0002" and len(log) == 1 and st.get("op") == "b" * 32, st)
        check("...and its network policy is set for the setup (every name), read back",
              standin_state(home)["sprites"]["box"]["policy"] == [{"domain": "*", "action": "allow"}])
        # Found by review: a name and a time were taken as proof. A Sprite of that name made just after the journal,
        # by someone else (no label of this operation's), is left alone, by create, abort and delete alike.
        os.remove(os.path.join(rd, "remote.json"))
        sp = standin_state(home)["sprites"]
        sp["box"]["info"]["created_at"] = "2099-01-01T00:00:00.000000Z"     # after any journal
        sp["box"]["info"]["labels"] = ["metacomputer", "mcop-" + "f" * 32]  # another build's (or none)
        set_state(home, sprites=sp)
        remote.write_state(rd, {"launching": "c" * 32, "launch_t": 1800000000.0, "provider": "sprites"})
        why = raises(remote.lost_sprite, adapter_now(), comp, remote.read_state(rd), kind=AdapterError)
        check("a Sprite of that name without this operation's label isn't a lost launch: said, left alone",
              why and "left alone" in why, why)
        why = raises(remote.create_sprite, comp, adapter_now(), rd, remote.read_state(rd), "c" * 32, kind=AdapterError)
        remote.abort_sprite(comp, adapter_now(), rd, dict(remote.read_state(rd), launching="c" * 32), "c" * 32, keep=False) \
            if False else None
        try:
            remote.abort_sprite(comp, adapter_now(), rd, remote.read_state(rd), "c" * 32, keep=False)
        except AdapterError:
            pass
        st = remote.read_state(rd)
        check("...create refuses over it, abort deletes nothing, and it's still there",
              why and "box" in standin_state(home)["sprites"] and not st.get("instance")
              and not [c for c in standin_log(home) if "DELETE" in c["argv"]][1:], why)
        for labels in ([], None):
            sp["box"]["info"].pop("labels", None)
            if labels is not None:
                sp["box"]["info"]["labels"] = labels
            set_state(home, sprites=sp)
            why = raises(remote.lost_sprite, adapter_now(), comp, remote.read_state(rd), kind=AdapterError)
            check(f"...nor one with no labels at all ({labels!r})", why and "left alone" in why, why)
        # abort: deletes what this operation made (its label), keeps on --keep
        sp["box"]["info"]["labels"] = ["metacomputer", "mcop-" + "d" * 32]
        set_state(home, sprites=sp)
        remote.write_state(rd, {"launching": "d" * 32, "launch_t": 1800000000.0, "provider": "sprites"})
        remote.abort_sprite(comp, adapter_now(), rd, remote.read_state(rd), "d" * 32, keep=True)
        check("abort --keep: a lost launch of this operation (its label) recorded as its own, and kept",
              remote.read_state(rd).get("instance") == sp["box"]["info"]["id"] and remote.read_state(rd).get("kept")
              and "box" in standin_state(home)["sprites"])
        remote.abort_sprite(comp, adapter_now(), rd, remote.read_state(rd), "d" * 32, keep=False)
        check("abort: the Sprite this operation made is deleted, the record says so",
              "box" not in standin_state(home)["sprites"] and not remote.read_state(rd).get("instance")
              and remote.read_state(rd).get("aborted"))
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home


def hub_guard():
    print("\nthe hub on a computer without background input")
    fo = mcp_hub.foreground_only
    a = {"x": 10, "y": 20}
    check("a pixel click: sent in the foreground, with a note", fo("click", a) and a["delivery_mode"] == "foreground")
    a = {"keys": ["ctrl", "l"]}
    check("keys: in the foreground", fo("hotkey", a) and a["delivery_mode"] == "foreground")
    a = {"text": "hi", "delivery_mode": "background"}
    check("typing asked for in the background: in the foreground", fo("type_text", a) and a["delivery_mode"] == "foreground")
    a = {"element_token": "s0000abcd:3"}
    check("a click on an element: left on its route (accessibility works there)", fo("click", a) is None
          and "delivery_mode" not in a)
    for tool, extra in (("hotkey", {"keys": ["ctrl", "a"]}), ("press_key", {"key": "Return"}), ("type_text", {"text": "x"})):
        a = dict(extra, element_token="s0000abcd:3")
        check(f"{tool} aimed at an element: still keyboard input, sent in the foreground (found by review)",
              fo(tool, a) and a["delivery_mode"] == "foreground", a)
    a = {"delivery_mode": "foreground"}
    check("already foreground: nothing to say", fo("press_key", a) is None)
    check("a read: nothing", fo("get_window_state", {"pid": 1}) is None)
    b = {"steps": [{"tool": "click", "args": {"element_token": "s0000abcd:1"}}, {"press_key": {"key": "Return"}},
                   {"tool": "type_text", "args": {"text": "x", "delivery_mode": "foreground"}},
                   {"hotkey": {"keys": ["ctrl", "a"], "element_token": "s0000abcd:2"}}]}
    n = fo("run_steps", b)
    check("run_steps: each step by its effective mode (element clicks left; keys moved, element-aimed ones too)", n and
          "delivery_mode" not in b["steps"][0]["args"] and b["steps"][1]["press_key"]["delivery_mode"] == "foreground"
          and b["steps"][3]["hotkey"]["delivery_mode"] == "foreground", b)
    b = {"delivery_mode": "foreground", "steps": [{"press_key": {"key": "a"}}]}
    check("...a batch already in the foreground: nothing", fo("run_steps", b) is None)
    h = mcp_hub.Hub(env={"MCOMP_BACKGROUND_INPUT": "off", "MCOMP_VM_NAME": "x"}, local=True)
    h2 = mcp_hub.Hub(env={"MCOMP_VM_NAME": "x"}, local=True)
    check("the hub reads it from MCOMP_BACKGROUND_INPUT (off: foreground only; on by default)",
          h.foreground_only is True and h2.foreground_only is False)


def guest():
    print("\nthe guest's services")
    d = os.path.join(LAB, "provisioning", "sprite", "rootfs", "opt", "metacomputer", "sprite")
    hide = open(os.path.join(d, "hide-sprite-api")).read()
    check("hide-sprite-api: an empty tmpfs over /.sprite, then the program", "mount -t tmpfs" in hide
          and "/.sprite" in hide and hide.rstrip().endswith('exec "$@"'))
    sess = open(os.path.join(d, "svc-session")).read()
    check("svc-session: a mount namespace with /.sprite covered and a PID namespace with its own /proc, tini its init",
          "exec unshare --mount --pid --fork --propagation private --mount-proc --" in sess
          and "/opt/metacomputer/sprite/hide-sprite-api /usr/bin/tini --" in sess
          and sess.index("session.pid") < sess.index("exec unshare"))
    ins = open(os.path.join(d, "in-session")).read()
    check("in-session: enters only the session svc-session made (its namespaces aren't the caller's, /.sprite covered, "
          "no API socket under its root), mount and PID namespaces both",
          'exec nsenter --target "$c" --mount --pid -- "$@"' in ins and "sprite-hidden" in ins
          and '[ ! -e "/proc/$c/root/.sprite/api.sock" ]' in ins and "ns/pid" in ins and "ns/mnt" in ins)
    # Found by review: xauth ran as the desktop user before its service entered the masked namespace, so another of that
    # user's processes could reach the API socket through /proc/PID/root. Nothing runs as the desktop user (runuser,
    # su, setpriv, sudo -u) in a service script before it's inside the session.
    import re as _re
    for svc in ("svc-xvfb", "svc-desktop", "svc-sshd"):
        t = open(os.path.join(d, svc)).read()
        body = [ln for ln in t.splitlines() if not ln.lstrip().startswith("#")]
        before = []
        for ln in body:
            if "/opt/metacomputer/sprite/in-session" in ln:
                break
            before.append(ln)
        check(f"{svc}: joins the session (in-session) and runs nothing as the desktop user before it",
              any("exec /opt/metacomputer/sprite/in-session" in ln for ln in body)
              and not any(_re.search(r"\b(runuser|su|setpriv)\b|sudo -u", ln) for ln in before)
              and "unshare -m" not in t, svc)
    inner = open(os.path.join(d, "xvfb-inner")).read()
    check("xvfb-inner: the X cookie and the server as the desktop user, only inside the session (checked first)",
          inner.index("sprite-hidden") < inner.index("xauth") and 'exec runuser -u "$U" -- /usr/bin/Xvfb' in inner)
    x = open(os.path.join(d, "svc-xvfb")).read()
    check("svc-xvfb: /dev/shm enlarged and a stale X lock removed as root, then the server in the session",
          "remount,size=2g /dev/shm" in x and "stale" in x and "exec /opt/metacomputer/sprite/in-session /opt/metacomputer/sprite/xvfb-inner" in x
          and "-nolisten tcp" in inner)
    svcs = open(os.path.join(d, "services")).read()
    check("services: session first, sshd and xvfb in it, desktop after xvfb",
          "mk session svc-session; mk sshd svc-sshd session" in svcs and "mk xvfb svc-xvfb session; mk desktop svc-desktop xvfb" in svcs)
    conf = open(os.path.join(LAB, "provisioning", "sprite", "rootfs", "etc", "ssh", "sshd_config.d",
                             "05-metacomputer-sprite.conf")).read()
    check("sshd listens on the Sprite's loopback, by number", "ListenAddress 127.0.0.1" in conf and "ListenAddress ::1" in conf
          and "localhost" not in conf.replace('"localhost"', "").split("ListenAddress")[1])
    prov = open(os.path.join(LAB, "provisioning", "guest", "provision.sh")).read()
    check("provision.sh (sprite): the browsers' own DNS over HTTPS locked off, the live view on 127.0.0.1",
          '"DNSOverHTTPS" = {"Enabled": false, "Locked": true}' in prov.replace(".policies.DNSOverHTTPS", '"DNSOverHTTPS"')
          and "DnsOverHttpsMode" in prov and "127.0.0.1\\n' > /etc/metacomputer/vnc-listen" in prov)
    si = open(os.path.join(LAB, "provisioning", "guest", "rootfs", "opt", "metacomputer", "bin", "session-init")).read()
    check("the live view on loopback only: x11vnc's IPv6 listener off too (it opened [::]:5900 despite -noipv6)",
          'LISTEN=127.0.0.1 V6="-rfbportv6 -1"' in si and '-listen "$LISTEN" -noipv6 $V6' in si)
    boot = open(os.path.join(LAB, "provisioning", "sprite", "bootstrap.sh")).read()
    check("bootstrap.sh: the session and sshd as Sprites services, sshd checked to be in the session, its host key last",
          "services sshd" in boot and "tini" in boot and "sshd isn't in the session's namespaces" in boot
          and boot.rstrip().splitlines()[-1].startswith('echo "HOSTKEY'))
    fin = open(os.path.join(LAB, "provisioning", "guest", "finalize.sh")).read()
    check("finalize.sh leaves a Sprite's own account's sudo (Fly's channel) and needs no cloud-init",
          '[ -e /etc/metacomputer/remote ] && [ -z "$OWNER" ]' in fin and "[ ! -d /etc/cloud ]" in fin)


def cli(td):
    print("\nthe CLI, in a copy of the checkout, with the stand-in as Fly's command")
    import test_multi_vm as tmv
    b = tmv.Box(td, "sprites")
    shutil.copytree(os.path.join(LAB, "provisioning"), os.path.join(b.co, "provisioning"))
    standin = make_standin(td)
    env = b.env(SPRITE_CLI=standin)
    try:
        r = b.run("vm", "create", "sp1", "--on", "sprites", "--org", "my-org", "--no-build", "--yes", env=env)
        f = os.path.join(b.co, "config", "vms", "sp1.env")
        text = open(f).read() if os.path.exists(f) else ""
        check("vm create --on sprites: place, provider, the sprite transport, its organization and Sprite, no idle stop",
              r.returncode == 0 and all(x in text.splitlines() for x in (
                  "PLACE=sprites", "PROVIDER=sprites", "TRANSPORT=sprite", "SPRITE_ORG=my-org", "SPRITE_NAME=sp1",
                  "EGRESS=internet", "IDLE_STOP=off")), (r.stderr, text))
        for args, want in ((["--on", "sprites"], "--org ORG"), (["--on", "sprites", "--org", "-x"], "SPRITE_ORG"),
                           (["--on", "sprites", "--org", "o", "--size", "big"], "Fly sizes it"),
                           (["--on", "sprites", "--org", "o", "--sprite", "Bad_Name"], "SPRITE_NAME"),
                           (["--on", "oci", "--sprite", "x"], "--sprite is for --on sprites")):
            r = b.run("vm", "create", "bad1", *args, "--no-build", "--yes", env=env)
            check(f"vm create {' '.join(args)[:44]}: refused ({want})", r.returncode != 0 and want in r.stderr
                  and not os.path.exists(os.path.join(b.co, "config", "vms", "bad1.env")), r.stderr[-300:])
        r = b.run("vm", "create", "bad2", "--on", "sprites", "--org", "o", "--no-build", "--yes",
                  env=b.env(SPRITE_CLI="/nonexistent/sprite", PATH="/usr/bin:/bin"))
        check("vm create --on sprites without Fly's command: refused, saying to install it", r.returncode != 0
              and "sprite login" in r.stderr, r.stderr[-300:])
        # A Sprite as if made: its record and the stand-in's.
        rd = os.path.join(b.co, ".run-vms", "sp1")
        os.makedirs(rd, exist_ok=True)
        json.dump({"provider": "sprites", "instance": "sprite-0001", "sprite": "sp1", "org": "my-org"},
                  open(os.path.join(rd, "remote.json"), "w"))
        set_state(b.home, sprites={"sp1": {"org": "my-org", "info": {"id": "sprite-0001", "name": "sp1", "status": "cold",
                                                                     "created_at": "2026-11-23T00:00:00Z"},
                                           "policy": [{"domain": "*", "action": "allow"}], "checkpoints": [], "v": 1}}, n=1)
        script = 'MCOMP_VM_ARG="$1"; . "$2/scripts/lib.sh"; ssh_base "$3"; printf "%s\\n" "${SSH_ARGS[@]}"'
        r = subprocess.run(["/bin/bash", "-c", script, "_", "sp1", b.co, "ops"], env=env, capture_output=True,
                           text=True, timeout=60)
        o = r.stdout.splitlines()
        pc = next((x for x in o if x.startswith("ProxyCommand=")), "")
        check("ssh_base ops: through Fly's tunnel (sprite proxy -W :22) run by lib/proxywrap.py, pinned as mcomp-sp1",
              f"-- {standin} -o my-org -s sp1 proxy -W :22" in pc and "proxywrap.py" in pc and o[-1] == "cua@sp1"
              and "HostKeyAlias=mcomp-sp1" in o and "StrictHostKeyChecking=yes" in o and "ForwardAgent=no" in o, (pc, r.stderr))
        check("...its ProxyCommand asks Fly for the Sprite's id before each connection (proxywrap --id, the id recorded "
              "when it was made, and how the same command asks: its API record)",
              f"--id sprite-0001 --check 7 {standin} -o my-org api /v1/sprites/sp1 -- -sS -- {standin}" in pc, pc)

        def connect(pc_):
            """One connection's ProxyCommand, run as ssh runs it (sh -c, nothing on its input): (exit status, stderr,
            whether Fly's tunnel was started)."""
            before = len(standin_log(b.home))
            rr = subprocess.run(["/bin/sh", "-c", "exec " + pc_[len("ProxyCommand="):]], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60)
            return rr.returncode, rr.stderr, any("proxy" in e["argv"] for e in standin_log(b.home)[before:])
        rc_, err_, tunneled = connect(pc)
        check("...the Sprite still the one it was made as: the tunnel starts (sprite proxy)", tunneled, (rc_, err_))
        # Found by review: connections went to whichever Sprite had the name. Now each is checked to be its own first,
        # and (found by the re-check) not only when the arguments are made: scripts/lib.sh keeps them for the rest of its
        # command, and the hub for its life.
        sp_ = standin_state(b.home)["sprites"]
        sp_["sp1"]["info"]["id"] = "sprite-9999"
        set_state(b.home, sprites=sp_)
        rc_, err_, tunneled = connect(pc)
        check("kept arguments, and a Sprite of that name made again since (another id): that connection's tunnel never "
              "starts, said why", rc_ != 0 and not tunneled and "isn't the one this computer was made as" in err_, (rc_, err_))
        r = subprocess.run(["/bin/bash", "-c", script, "_", "sp1", b.co, "ops"], env=env, capture_output=True,
                           text=True, timeout=60)
        check("ssh_base: a Sprite of that name with another id (made again): no connection, said why",
              r.returncode != 0 and "isn't the one sp1 was made as" in r.stderr and not r.stdout, r.stderr)
        r = b.run("--vm", "sp1", "net", "allow", "example.com", env=env)
        check("...nor any change through Fly's API (net allow)", r.returncode != 0 and "isn't the one" in r.stderr
              and standin_state(b.home)["sprites"]["sp1"]["policy"] == [{"domain": "*", "action": "allow"}], r.stderr)
        sp_["sp1"]["info"]["id"] = "sprite-0001"
        set_state(b.home, sprites=sp_, api_fail_after=standin_state(b.home).get("api_n", 0))
        rc_, err_, tunneled = connect(pc)
        check("...and when Fly can't be asked: no tunnel either", rc_ != 0 and not tunneled and "couldn't be asked" in err_,
              (rc_, err_))
        set_state(b.home, api_fail_after=None)
        r = subprocess.run(["/bin/bash", "-c", 'MCOMP_VM_ARG=sp1; . "$1/scripts/lib.sh"; export_options; echo "$MCOMP_BACKGROUND_INPUT"',
                            "_", b.co], env=env, capture_output=True, text=True, timeout=60)
        check("the hub is told this computer has no background input", r.stdout.strip() == "off", r.stdout + r.stderr)
        r = b.run("--vm", "sp1", "net", "status", env=env)
        check("net status: the setting and Fly's policy in force, apart", r.returncode == 0 and "EGRESS=internet" in r.stdout
              and "Fly's network policy" in r.stdout and "internet: every name" in r.stdout, r.stdout + r.stderr)
        r = b.run("--vm", "sp1", "net", "allow", "example.com", env=env)
        pol = standin_state(b.home)["sprites"]["sp1"]["policy"]
        check("net allow (from internet: it narrows, anyone may): Fly's policy lists the names",
              r.returncode == 0 and pol == sprites.rules_for(allow=["example.com"]) and "allowlist" in r.stdout, (r.stderr, pol))
        r = b.run("--vm", "sp1", "net", "allow", "other.org", env=env)
        check("net allow more names (it widens): only at the user's own terminal; nothing changed", r.returncode != 0
              and "own terminal" in r.stderr and standin_state(b.home)["sprites"]["sp1"]["policy"] == pol, r.stderr)
        r = b.run("--vm", "sp1", "net", "allow", "10.0.0.1", env=env)
        check("net allow an address: refused", r.returncode != 0 and "isn't a DNS name" in r.stderr, r.stderr)
        r = b.run("--vm", "sp1", "net", "allow", "example.com", "--for", "1h", env=env)
        check("net allow --for: a Sprite's list stays until net clear", r.returncode != 0 and "net clear" in r.stderr, r.stderr)
        r = b.run("--vm", "sp1", "net", "clear", env=env)
        check("net clear (back to EGRESS=internet: it widens): only at the user's own terminal", r.returncode != 0
              and "own terminal" in r.stderr, r.stderr)
        r = b.run("--vm", "sp1", "net", "remove", "example.com", env=env)
        check("net remove the last name (it narrows): every name denied", r.returncode == 0
              and standin_state(b.home)["sprites"]["sp1"]["policy"] == [{"domain": "*", "action": "deny"}], r.stderr)
        # Found by review: narrowing first let a second step widen without the terminal. Judged on where a change ends:
        r = b.run("--vm", "sp1", "net", "egress", "internet", "--override", env=env)
        check("net egress internet after the list's last name went (off to every name): only at the user's terminal",
              r.returncode != 0 and "own terminal" in r.stderr
              and standin_state(b.home)["sprites"]["sp1"]["policy"] == [{"domain": "*", "action": "deny"}], r.stderr)
        r = b.run("--vm", "sp1", "net", "clear", env=env)
        check("...nor net clear (back to EGRESS=internet from nothing)", r.returncode != 0 and "own terminal" in r.stderr
              and standin_state(b.home)["sprites"]["sp1"]["policy"] == [{"domain": "*", "action": "deny"}], r.stderr)
        r = b.run("--vm", "sp1", "net", "allow", "example.com", env=env)
        check("...nor a list where nothing goes out", r.returncode != 0 and "own terminal" in r.stderr, r.stderr)
        r = b.run("--vm", "sp1", "net", "egress", "off", "--override", env=env)
        check("net egress off (the same or narrower): goes ahead, read back", r.returncode == 0
              and "EGRESS=off" in r.stdout, r.stdout + r.stderr)
        # (round 4) A flag net doesn't take, before its subcommand: a usage error, nothing asked of Fly (it once reached
        # the Sprite's clear: everything but --override was taken out unread).
        pol0, n0 = standin_state(b.home)["sprites"]["sp1"]["policy"], standin_state(b.home).get("api_n", 0)
        bad = [b.run("--vm", "sp1", "net", *a, env=env) for a in (["--discard-unsynced", "clear"], ["--yes", "remove", "x.com"],
                                                                  ["--yes", "egress", "off"])]
        check("net --discard-unsynced clear (and --yes before remove or egress): a usage error, nothing asked of Fly",
              all(x.returncode == 2 and "usage" in x.stderr for x in bad)
              and standin_state(b.home)["sprites"]["sp1"]["policy"] == pol0 and standin_state(b.home).get("api_n", 0) == n0,
              [(x.returncode, x.stderr[-160:]) for x in bad])
        r = subprocess.run([PY, os.path.join(b.co, "lib", "remote.py"), "egress", "--vm", "sp1", "--set", "internet",
                            "--setup"], env=env, capture_output=True, text=True, timeout=60)
        check("the setup's own exception (egress --setup) opens it while the computer isn't set up",
              r.returncode == 0 and standin_state(b.home)["sprites"]["sp1"]["policy"] == [{"domain": "*", "action": "allow"}],
              r.stderr)
        subprocess.run([PY, os.path.join(b.co, "lib", "remote.py"), "egress", "--vm", "sp1", "--set", "off"], env=env,
                       capture_output=True, timeout=60)
        r = subprocess.run([PY, os.path.join(b.co, "lib", "remote.py"), "setup-done", "--vm", "sp1"], env=env,
                           capture_output=True, text=True, timeout=60)
        r = subprocess.run([PY, os.path.join(b.co, "lib", "remote.py"), "egress", "--vm", "sp1", "--set", "internet",
                            "--setup"], env=env, capture_output=True, text=True, timeout=60)
        check("...and never once it's set up (setup-done)", r.returncode != 0 and "set up already" in r.stderr
              and standin_state(b.home)["sprites"]["sp1"]["policy"] == [{"domain": "*", "action": "deny"}], r.stderr)
        sp_ = standin_state(b.home)["sprites"]
        sp_["sp1"]["policy"] = [{"domain": "*", "action": "allow"}]        # the user, at their terminal, opened it again
        set_state(b.home, sprites=sp_)
        r = b.run("--vm", "sp1", "checkpoint", "ck1", env=env)
        r2 = b.run("--vm", "sp1", "checkpoints", env=env)
        check("checkpoint NAME: made at Fly while it runs; checkpoints lists it", r.returncode == 0 and "ck1\tv2" in r2.stdout,
              r.stdout + r.stderr + r2.stdout)
        r = b.run("--vm", "sp1", "checkpoint", "ck1", env=env)
        check("...again: refused without --replace", r.returncode != 0 and "--replace" in r.stderr, r.stderr)
        r = b.run("--vm", "sp1", "checkpoint", "ck1", "--replace", env=env)
        r2 = b.run("--vm", "sp1", "checkpoints", env=env)
        check("...--replace: the new one made, then the older one deleted", r.returncode == 0
              and r2.stdout.count("ck1\t") == 1 and "ck1\tv3" in r2.stdout, r2.stdout + r.stderr)
        r = b.run("--vm", "sp1", "checkpoint", "baseline", env=env)
        check("checkpoint baseline: refused (that's metacomputer baseline)", r.returncode != 0 and "baseline" in r.stderr)
        # Found by the re-check: checkpoint --delete asked Fly with neither the ownership check nor the handoff's dispatch
        # lock, and checkpoint NAME checked ownership but took no lock, so a handoff opened after its check still had the
        # user's private state saved. Now both, held through Fly's change, as every acting command's.
        owners_d = os.path.join(rd, "owners")
        os.makedirs(owners_d, exist_ok=True)
        t_ = time.time()
        json.dump({"v": 1, "id": "h-0123456789ab", "status": "open", "why": "login", "opened": t_, "deadline": t_ + 600},
                  open(os.path.join(owners_d, "handoff.state"), "w"))

        def fly_changes(since):
            return [e["argv"] for e in standin_log(b.home)[since:]
                    if "api" in e["argv"] and any(x in e["argv"] for x in ("POST", "DELETE"))]
        n_ = len(standin_log(b.home))
        r = b.run("--vm", "sp1", "checkpoint", "--delete", "ck1", "--yes", env=env)
        check("checkpoint --delete while the user has the computer (a handoff): refused, nothing asked of Fly",
              r.returncode != 0 and "metacomputer handoff" in r.stderr and not fly_changes(n_)
              and "ck1\t" in b.run("--vm", "sp1", "checkpoints", env=env).stdout, (r.stderr, fly_changes(n_)))
        for args_ in (["checkpoint", "ck2"], ["baseline", "--yes"], ["restore", "ck1", "--yes"], ["reset", "--yes"]):
            n_ = len(standin_log(b.home))
            r = b.run("--vm", "sp1", *args_, env=env)
            check(f"{' '.join(args_[:2])} while the user has the computer: refused, nothing asked of Fly",
                  r.returncode != 0 and "metacomputer handoff" in r.stderr and not fly_changes(n_), (r.stderr, fly_changes(n_)))
        os.remove(os.path.join(owners_d, "handoff.state"))
        # A handoff being opened (it holds this VM's dispatch lock while it writes its record): an acting step waits,
        # then doesn't act. The ownership check before it has passed by then: only the lock, held through Fly's
        # change, keeps the checkpoint from being taken after the handoff opens.
        import fcntl
        lk = os.open(os.path.join(owners_d, ".dispatch"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)
            for args_ in (["checkpoint", "ck2"], ["checkpoint", "--delete", "ck1", "--yes"]):
                n_ = len(standin_log(b.home))
                r = b.run("--vm", "sp1", *args_, env=env)
                check(f"{' '.join(args_[:2])} while a handoff is being opened: no change at Fly", r.returncode != 0
                      and "handoff is being opened" in r.stderr and not fly_changes(n_), (r.stderr, fly_changes(n_)))
        finally:
            os.close(lk)
        src_ = open(os.path.join(LAB, "bin", "metacomputer")).read()
        body_ = src_[src_.index("do_snap_sprite() {"):src_.index("\n}\n", src_.index("do_snap_sprite() {"))]
        unguarded = []
        for m_ in re.finditer(r'remote_py checkpoint --vm "\$VM_NAME" (create|delete|restore)\b', body_):
            branch = re.split(r";;|return 0", body_[:m_.start()])[-1]        # since the previous branch or step ended
            if not ("owners_check" in branch and "handoff_hold_unless_override" in branch
                    and branch.rindex("handoff_hold_unless_override") > branch.rindex("owners_check")):
                unguarded.append(body_[m_.start():m_.end() + 20])
        check("every checkpoint change at Fly in bin/metacomputer (create, replace, delete, baseline, restore, reset) comes "
              "after the ownership check and then the dispatch lock, in its own branch", not unguarded, unguarded)
        r = b.run("--vm", "sp1", "checkpoint", "--delete", "ck1", "--yes", env=env)
        check("checkpoint --delete: gone at Fly", r.returncode == 0 and "ck1" not in b.run("--vm", "sp1", "checkpoints", env=env).stdout,
              r.stderr)
        r = b.run("--vm", "sp1", "volume", "list", env=env)
        check("volumes: refused, saying why", r.returncode != 0 and "volumes" in r.stderr)
        # secrets bind: the tunnel, its arguments, the provider's id and how to ask about it, from one checked call.
        # Found by the re-check: a lookup that failed after the first was skipped, and the Sprite's name was bound as an
        # address, straight over SSH, with neither the tunnel nor the id.
        _, conf_, _ = b.resolve("sp1", SPRITE_CLI=standin)
        sshd_ = conf_.get("SSH_DIR", "")
        os.makedirs(sshd_, exist_ok=True)
        for k_ in ("fill", "ops"):
            open(os.path.join(sshd_, k_), "w").write("not a key\n")
        blog = os.path.join(td, "bind-calls.log")
        bvault = os.path.join(td, "bind-vault")
        with open(bvault, "w") as fh:
            fh.write("#!/bin/sh\ncase \"$1\" in version) echo '{\"build\":\"release\"}'; exit 0 ;; esac\n"
                     f"printf '%s\\n' \"$@\" > {blog}\n")
        os.chmod(bvault, 0o755)
        env_b = dict(env, MCOMP_VAULT_BIN=bvault)
        n0 = standin_state(b.home).get("api_n", 0)
        r = b.run("--vm", "sp1", "secrets", "bind", env=env_b)
        got = open(blog).read().splitlines() if os.path.exists(blog) else []

        def after(flag):
            return got[got.index(flag) + 1] if flag in got[:-1] else None
        check("secrets bind: the Sprite's tunnel (Fly's command, its arguments), the id it was made with and how the same "
              "command asks Fly about it, handed to the vault together", r.returncode == 0 and got[:2] == ["bind", "sp1"]
              and after("--tunnel") == standin and after("--host") == "sp1" and after("--tunnel-id") == "sprite-0001"
              and [x for x in got if x.startswith("--tunnel-arg=")] == [f"--tunnel-arg={x}" for x in ("-o", "my-org", "-s", "sp1", "proxy", "-W", ":22")]
              and [x for x in got if x.startswith("--tunnel-check-arg=")]
              == [f"--tunnel-check-arg={x}" for x in ("-o", "my-org", "api", "/v1/sprites/sp1", "--", "-sS")], (r.stderr, got))
        asked = standin_state(b.home).get("api_n", 0) - n0
        if os.path.exists(blog):
            os.remove(blog)
        set_state(b.home, api_fail_after=n0 + asked + 1)        # the address's look answers; Fly says nothing after it
        r = b.run("--vm", "sp1", "secrets", "bind", env=env_b)
        check("...Fly answering the address's look and not the tunnel's: refused, nothing bound (never the name as an "
              "address, straight over SSH)", asked == 2 and r.returncode != 0 and "nothing was bound" in r.stderr
              and not os.path.exists(blog), (asked, r.stderr, open(blog).read() if os.path.exists(blog) else ""))
        set_state(b.home, api_fail_after=None)
        sp_ = standin_state(b.home)["sprites"]
        sp_["sp1"]["info"]["id"] = "sprite-7777"
        set_state(b.home, sprites=sp_)
        r = b.run("--vm", "sp1", "secrets", "bind", env=env_b)
        check("...nor a Sprite of that name made again", r.returncode != 0 and not os.path.exists(blog), r.stderr)
        sp_["sp1"]["info"]["id"] = "sprite-0001"
        set_state(b.home, sprites=sp_)
        # stop closes the vault's tunnel too (found by review: its API proxy's tunnel kept the Sprite awake), then checks
        # that no tunnel of this Mac's to the Sprite is left.
        vlog = os.path.join(td, "vault-calls.log")
        fake_vault = os.path.join(td, "fake-vault")
        with open(fake_vault, "w") as fh:
            fh.write(f"#!/bin/sh\necho \"$MCOMP_VAULT_DIR $*\" >> {vlog}\n")
        os.chmod(fake_vault, 0o755)
        vdir = os.path.join(b.app, "sp1")
        os.makedirs(vdir, exist_ok=True)
        import socket as _socket
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(os.path.join(vdir, "vault.sock"))
        env_v = dict(env, MCOMP_VAULT_BIN=fake_vault)
        r = b.run("--vm", "sp1", "stop", "--override", env=env_v)
        calls = open(vlog).read().splitlines() if os.path.exists(vlog) else []
        check("stop: this Mac's connections closed, the vault's tunnel to it included (disconnect --vm sp1, in its own "
              "vault); Fly pauses it by itself", r.returncode == 0 and "pauses it about 10 s later" in r.stdout
              and calls in ([f"{os.path.realpath(vdir)} disconnect --vm sp1"], [f"{vdir} disconnect --vm sp1"]),
              (r.stdout + r.stderr, calls))
        lingering = subprocess.Popen([PY, "-c", "import time; time.sleep(30)", "-o", "my-org", "-s", "sp1", "proxy", "-W", ":22"])
        try:
            r = b.run("--vm", "sp1", "stop", "--override", env=env_v, timeout=60)
            check("...and when a tunnel to it is still running, stop says it stays awake (exit 1), never that it sleeps",
                  r.returncode == 1 and "still open" in r.stderr and "pauses it about" not in r.stdout, r.stdout + r.stderr)
        finally:
            lingering.kill()
            lingering.wait()
            srv.close()
        r = b.run("cloud", "status", "sprites", "--json", env=env)
        try:
            listed = json.loads(r.stdout)["sprites"]
        except (ValueError, KeyError):
            listed = []
        check("cloud status sprites: the organization's Sprites, each marked when it's one of this Mac's computers",
              r.returncode == 0 and listed and listed[0]["name"] == "sp1" and listed[0]["here"] is True, r.stdout + r.stderr)
        # Found live: exec read its caller's standard input when it wasn't a terminal, and a pipe that never ends made
        # desktop-restart wait for good. Now only with --stdin.
        p_ = subprocess.Popen([PY, os.path.join(b.co, "lib", "remote.py"), "exec", "--vm", "sp1", "--", "true"], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p_.wait(timeout=20)
            err_ = p_.stderr.read().decode(errors="replace")
            ok_ = p_.returncode == 0
        except subprocess.TimeoutExpired:
            p_.kill()
            ok_, err_ = False, "timed out (it waited on its standard input)"
        execs = [e for e in standin_state(b.home).get("execs", []) if e["sprite"] == "sp1"]
        check("exec with an open, silent standard input (no --stdin): runs, and reads none of it", ok_
              and execs and execs[-1]["argv"] == ["true"] and execs[-1]["stdin"] == 0, (err_, execs[-1:]))
        r = b.run("cloud", "teardown", "sprites", env=env)
        check("cloud teardown sprites: nothing to tear down (each Sprite goes with vm delete)", r.returncode != 0
              and "vm delete" in r.stderr, r.stderr)
        r = b.run("vm", "delete", "sp1", "--confirm", "sp1", env=env)
        check("vm delete: its Sprite deleted at Fly (said first), its settings removed",
              r.returncode == 0 and "its Sprite on Fly" in r.stdout and "sp1" not in standin_state(b.home)["sprites"]
              and not os.path.exists(f), r.stdout[-400:] + r.stderr[-400:])
    finally:
        b.close()


def main():
    transport_and_policy()
    manifest()
    td = tempfile.mkdtemp(prefix="mcsprites-")
    try:
        adapter(td)
        journal(td)
        hub_guard()
        guest()
        cli(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
