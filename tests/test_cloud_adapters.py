#!/usr/bin/env python3
"""The provider layer (lib/cloud/) and where computers are (lib/placement.py). No VM, no network.

    python3 tests/test_cloud_adapters.py

- The contract every registered adapter passes (and the test stand-in with other manifests): a sound manifest; every
  method of the interface answers, with a result or a refusal that says why (NotSupported for what the provider
  lacks, HandledByCLI naming a real metacomputer command), never a crash; what the manifest lacks is refused; bad
  names are refused before anything; rates and estimates; no socket and no process is used while it runs, and the
  adapters' code imports nothing that reaches the network or starts programs.
- PERSIST: auto picks the best the manifest offers, a mode it doesn't offer is refused saying which it does; the
  stand-in keeps files across sleep with a keeping mode and starts fresh with none.
- The feature map: every feature gives None or a plain reason for every adapter.
- The resolver: names, providers, places and the default, in that order; refusals when it's unclear or empty;
  --vm and --on together; --on cloud (or any provider's name) says this metacomputer has no cloud computers.
- The registered providers (your own server, and each provider's folder, providers/<name>/) make up `cloud`; their
  adapters pass the same contract with no credentials and no network (tests/test_remote.py covers their transport,
  guest setup and the drivers on stand-in APIs; tests/test_sprites.py the Sprites adapter against a stand-in `sprite`
  command).
- Provider packages (providers/README.md): each registered folder has what a provider's folder holds and a sound
  command-line manifest (CLI); what's made from the manifests is current (scripts/gen-providers.py --check); and no
  shared code names a provider (PROVIDER_WORDS, against the lines of LINT_ALLOW, each allowed for its reason).
"""
import ast
import copy
import os
import re
import socket
import subprocess
import sys

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import cloud  # noqa: E402
import placement  # noqa: E402
from cloud import capabilities as caps  # noqa: E402
from cloud.adapter import INTERFACE, AdapterError, HandledByCLI, NotSupported  # noqa: E402
from cloud.fake import MANIFEST as FAKE_MANIFEST, Fake  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# The commands bin/metacomputer dispatches (its `case "$cmd" in` labels): what a HandledByCLI command must name.
def cli_commands():
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    block = src[src.rindex('case "$cmd" in'):]
    out = set()
    for m in re.finditer(r"^  ([a-z][a-z|_-]*)\)", block, re.M):
        out.update(m.group(1).split("|"))
    return out


CLI = cli_commands()

# A provider with little: an ephemeral disk and nothing else (sessions only), to see every refusal.
BARE = dict(copy.deepcopy(FAKE_MANIFEST), name="fake", label="a bare stand-in", volumes=None, snapshots=None,
            backup=None, bucket_mount=None, sleep_wake={"kind": "none", "keeps_processes": False},
            egress={"model": "open", "allowlist": False, "private_ranges_blocked": False}, port_forward=False,
            sizes=(), regions=())
# One that keeps its root disk (like this Mac), with backups too.
KEEPER = dict(copy.deepcopy(FAKE_MANIFEST), label="a keeping stand-in", root_disk="persistent", fresh_start=False)


def subjects():
    """(label, adapter) for every registered adapter, and the stand-in with the other manifests."""
    # A provider's adapter is made with no credentials (env={}): the contract never reaches a provider.
    takes_env = ("oci", "hetzner", "digitalocean", "vultr")
    out = [(n, cloud.adapter(n, env={}) if n in takes_env else cloud.adapter(n)) for n in cloud.registered()]
    out += [("fake (bare)", Fake(BARE)), ("fake (keeper)", Fake(KEEPER))]
    return out


class NoOutside:
    """No socket and no process while the contract runs: either would mean a network call or a program started."""

    def __enter__(self):
        self.saved = (socket.socket, socket.create_connection, subprocess.Popen)

        def refuse(*a, **k):
            raise AssertionError("an adapter tried to open a socket or start a program")
        socket.socket = socket.create_connection = subprocess.Popen = refuse
        return self

    def __exit__(self, *exc):
        socket.socket, socket.create_connection, subprocess.Popen = self.saved
        return False


def call(fn, *args):
    """('ok', result) | ('refused', AdapterError) | ('error', other exception)."""
    try:
        return "ok", fn(*args)
    except AdapterError as e:
        return "refused", e
    except Exception as e:          # noqa: BLE001  (anything else is a contract failure)
        return "error", e


def contract(label, a):
    m = a.manifest
    print(f"\n{label}")
    check(f"{label}: the manifest is sound", caps.check_manifest(m) == [], caps.check_manifest(m))
    check(f"{label}: its name is its registered one", m["name"] == a.name and (a.name not in cloud.ADAPTERS
                                                                              or cloud.ADAPTERS[a.name] is type(a)), m["name"])
    check(f"{label}: its place is this Mac or a provider's", m["place"] == placement.MAC or m["place"] not in
          (placement.CLOUD, ""), m["place"])
    for group, names in INTERFACE.items():
        check(f"{label}: every {group} method is there", all(callable(getattr(a, n, None)) for n in names), names)
    with NoOutside():
        if m["sizes"] and m["regions"]:
            first = call(a.create, "box", m["regions"][0], m["sizes"][0])
        else:
            first = call(a.create, "box")
        results = {"create": first}
        results["status"] = call(a.status, "box")
        results["list"] = call(a.list)
        results["ssh_proxy_command"] = call(a.ssh_proxy_command, "box")
        results["port_forward"] = call(a.port_forward, "box", 5900)
        results["snapshot"] = call(a.snapshot, "box", "one")
        results["snapshots"] = call(a.snapshots, "box")
        results["restore"] = call(a.restore, "box", "one")
        results["delete_snapshot"] = call(a.delete_snapshot, "box", "one")
        results["egress"] = call(a.egress, "box")
        results["set_egress"] = call(a.set_egress, "box", ["example.com"])
        results["sleep"] = call(a.sleep, "box")
        results["wake"] = call(a.wake, "box")
        results["setup"] = call(a.setup)
        results["doctor"] = call(a.doctor)
        results["rates"] = call(a.rates)
        results["estimate"] = call(a.estimate, 2.0, 10.0)
        results["delete"] = call(a.delete, "box")
        bad_names = [call(a.create, n) for n in ("", "../x", "-x", "a b", "x" * 65)]
    crashed = {k: repr(v[1]) for k, v in results.items() if v[0] == "error"}
    check(f"{label}: every method answers or refuses with a reason, never a crash", not crashed, crashed)
    check(f"{label}: every refusal says why", all(str(v[1]).strip() for v in results.values() if v[0] == "refused"),
          {k: str(v[1]) for k, v in results.items() if v[0] == "refused"})
    cli = {k: v[1].command for k, v in results.items() if v[0] == "refused" and isinstance(v[1], HandledByCLI)}
    check(f"{label}: what's handed to the CLI names a metacomputer command that exists",
          all(c and c[0] == "metacomputer" and all(isinstance(x, str) for x in c)
              and next((x for x in c[1:] if not x.startswith("-") and x != "box"), None) in CLI
              for c in [[x for i, x in enumerate(cmd) if not (i > 0 and cmd[i - 1] == "--vm")] for cmd in cli.values()]),
          cli)
    check(f"{label}: names that aren't names are refused before anything (ValueError)",
          all(r[0] == "error" and isinstance(r[1], ValueError) for r in bad_names), bad_names)

    def refused_as_missing(k):
        return results[k][0] == "refused" and isinstance(results[k][1], NotSupported)
    snap = ("snapshot", "snapshots", "restore", "delete_snapshot")
    if m["snapshots"]:
        check(f"{label}: it has snapshots, so none of the snapshot methods says it hasn't",
              not any(refused_as_missing(k) for k in snap), {k: results[k] for k in snap})
    else:
        check(f"{label}: no snapshots in its manifest: every snapshot method refuses (NotSupported)",
              all(refused_as_missing(k) for k in snap), {k: results[k] for k in snap})
    check(f"{label}: port forwards as its manifest says", refused_as_missing("port_forward") != m["port_forward"],
          results["port_forward"])
    check(f"{label}: sleep as its manifest says", refused_as_missing("sleep") == (m["sleep_wake"]["kind"] == "none"),
          results["sleep"])
    check(f"{label}: an egress allowlist as its manifest says", refused_as_missing("set_egress") != m["egress"]["allowlist"],
          results["set_egress"])
    st, r = results["rates"]
    check(f"{label}: rates are non-negative numbers with a currency", st == "ok" and r.get("currency")
          and all(isinstance(r.get(k), (int, float)) and r[k] >= 0 for k in ("awake_per_hour", "asleep_per_hour")), r)
    st, e = results["estimate"]
    check(f"{label}: an estimate from use is a non-negative number", st == "ok" and isinstance(e, (int, float)) and e >= 0, e)
    if st == "ok":
        check(f"{label}: ...that grows with use, and refuses negative hours",
              a.estimate(10, 10) >= a.estimate(1, 1) and call(a.estimate, -1)[0] == "error")
    # PERSIST
    have = caps.persist_modes(m)
    check(f"{label}: PERSIST=auto is the best it offers ({have[0] if have else 'none'})",
          bool(have) and caps.choose_persist(m) == have[0] == caps.choose_persist(m, "auto"), have)
    check(f"{label}: every mode it offers can be chosen", all(caps.choose_persist(m, p) == p for p in have), have)
    for p in caps.PERSIST_MODES:
        if p in have:
            continue
        try:
            caps.choose_persist(m, p)
            ok, why = False, "accepted"
        except caps.Unavailable as e:
            ok, why = all(h in str(e) for h in have) and f"PERSIST={p}" in str(e), str(e)
        check(f"{label}: PERSIST={p} isn't offered: refused, saying what is", ok, why)
    # The feature map: None or a reason, for every feature.
    out = {f: caps.missing(m, f) for f in caps.FEATURES}
    check(f"{label}: every feature is possible or has a plain reason", all(v is None or (isinstance(v, str) and m["label"]
                                                                                         in v) for v in out.values()), out)
    check(f"{label}: recordings never depend on the provider", out["recordings"] is None)
    check(f"{label}: reset, checkpoint and restore follow its snapshots",
          all((out[f] is None) == bool(m["snapshots"]) for f in ("reset", "checkpoint", "restore")), out)


def registry():
    print("the registry")
    check("local-lume and the test stand-in are registered", {"local-lume", "fake"} <= set(cloud.registered()),
          cloud.registered())
    check("every registered adapter is an Adapter, under its own name",
          all(issubclass(c, cloud.Adapter) and c.name == n for n, c in cloud.ADAPTERS.items()))
    check("the providers are your own server, Oracle Cloud, Fly Sprites, Hetzner Cloud, DigitalOcean and Vultr (the "
          "stand-in and this Mac don't count)",
          cloud.providers() == {"server": "your own server", "oci": "Oracle Cloud", "sprites": "Fly Sprites",
                                "hetzner": "Hetzner Cloud", "digitalocean": "DigitalOcean", "vultr": "Vultr"}
          == placement.PROVIDERS, cloud.providers())
    check("...each provider's place is its own name", all(cloud.by_place(p).name == p for p in
                                                           ("oci", "sprites", "hetzner", "digitalocean", "vultr")))
    check("by_place finds each place's adapter, and none for a place there isn't",
          cloud.by_place("mac").name == "local-lume" and cloud.by_place("oci").name == "oci"
          and cloud.by_place("server").name == "ubuntu-vm" and cloud.by_place("sprites").name == "sprites"
          and call(cloud.by_place, "fake")[0] == "error")
    check("an adapter that isn't registered is a KeyError", call(cloud.adapter, "nosuch")[0] == "error")
    # Static: the adapters' code imports nothing that reaches the network or starts programs: the framework's
    # (lib/cloud) and each registered provider's adapter and manifest (providers/<name>/).
    banned = {"socket", "urllib", "http", "subprocess", "requests", "ssl", "asyncio"}
    bad = []
    files = [os.path.join("lib", "cloud", f) for f in sorted(os.listdir(os.path.join(LAB, "lib", "cloud")))]
    files += [os.path.join("providers", n, f + ".py") for n in cloud.registry.PROVIDERS for f in ("adapter", "manifest")]
    for f in files:
        if not f.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(LAB, f)).read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            bad += [f"{f}: {n}" for n in names if n.split(".")[0] in banned]
    check("lib/cloud and the providers' adapters and manifests import no network or process module", not bad and
          len(files) > 2 * len(cloud.registry.PROVIDERS), bad)


def local_lume():
    print("\nthis Mac's adapter")
    a = cloud.adapter("local-lume")
    m = a.manifest
    check("local-lume: its computers are on this Mac, with a disk that keeps everything",
          m["place"] == "mac" and m["root_disk"] == "persistent" and caps.choose_persist(m) == "disk", m["place"])
    check("...PERSIST=disk is the one mode it offers", caps.persist_modes(m) == ("disk",), caps.persist_modes(m))
    check("...SSH needs no proxy (each VM's port is on the loopback)", a.ssh_proxy_command("demo") is None)
    check("...and nothing costs money", a.estimate(100, 100) == 0)
    for meth, args, want in (("create", ("demo",), ["metacomputer", "vm", "create", "demo"]),
                             ("wake", ("demo",), ["metacomputer", "--vm", "demo", "start"]),
                             ("sleep", ("demo",), ["metacomputer", "--vm", "demo", "stop"]),
                             ("delete", ("demo",), ["metacomputer", "vm", "delete", "demo"]),
                             ("snapshot", ("demo", "before"), ["metacomputer", "--vm", "demo", "checkpoint", "before"]),
                             ("restore", ("demo", "before"), ["metacomputer", "--vm", "demo", "restore", "before"]),
                             ("list", (), ["metacomputer", "vm", "list", "--json"])):
        st, e = call(getattr(a, meth), *args)
        check(f"...{meth} is the existing command: {' '.join(want)}", st == "refused" and isinstance(e, HandledByCLI)
              and e.command == want, (st, getattr(e, "command", e)))
    st, e = call(a.restore, "demo", "../x")
    check("...a checkpoint name that isn't one is refused", st == "error" and isinstance(e, ValueError), (st, e))


def fake_lifecycle():
    print("\nthe stand-in, end to end")
    f = Fake()
    c = f.create("one")
    check("create: awake, with PERSIST=auto as the best it offers (volume)", c["state"] == "awake" and c["persist"] == "volume", c)
    check("...in its first region and size", c["region"] == "here" and c["size"] == "2x4", c)
    check("...twice: refused", call(f.create, "one")[0] == "refused")
    check("...in a region it hasn't: refused (NotSupported)", isinstance(call(f.create, "x", "mars")[1], NotSupported))
    check("...with a size it hasn't: refused", isinstance(call(f.create, "x", None, "64x512")[1], NotSupported))
    st, e = call(f.create, "x", None, None, "disk")
    check("...with PERSIST=disk, which it doesn't offer: refused (NotSupported), saying what it offers",
          isinstance(e, NotSupported) and "volume, backup, none" in str(e) and "x" not in f.computers, (st, e))
    f.put("one", "/home/u/notes.txt", "hello")
    f.sleep("one")
    check("sleep: asleep", f.status("one")["state"] == "asleep")
    f.wake("one")
    check("wake: awake, and with PERSIST=volume its files are still there", f.status("one")["state"] == "awake"
          and f.files("one") == {"/home/u/notes.txt": "hello"}, f.files("one"))
    f.create("session", persist="none")
    f.put("session", "/tmp/x", "1")
    f.sleep("session")
    f.wake("session")
    check("PERSIST=none: a session computer starts fresh", f.files("session") == {}, f.files("session"))
    f.snapshot("one", "a")
    f.put("one", "/home/u/notes.txt", "changed")
    f.restore("one", "a")
    check("snapshot and restore: the files as they were", f.files("one") == {"/home/u/notes.txt": "hello"})
    f.snapshot("one", "b")
    f.snapshot("one", "c")
    check("...it keeps at most its manifest's count (3)", call(f.snapshot, "one", "d")[0] == "refused" and f.snapshots("one") == ["a", "b", "c"])
    f.delete_snapshot("one", "a")
    check("...and deletes one", f.snapshots("one") == ["b", "c"])
    check("egress: an allowlist", f.set_egress("one", ["example.com"])["allow"] == ["example.com"])
    check("list: both", [x["name"] for x in f.list()] == ["one", "session"])
    f.delete("one")
    check("delete: gone (absent)", f.status("one")["state"] == "absent" and [x["name"] for x in f.list()] == ["session"])
    check("...and a computer it hasn't: refused", call(f.delete, "one")[0] == "refused")
    b = Fake(BARE)
    check("a provider that keeps nothing: PERSIST=auto is none, and keeping files has its reason",
          caps.choose_persist(BARE) == "none" and "PERSIST=disk, volume or backup" in (caps.missing(BARE, "keep_files") or ""),
          caps.missing(BARE, "keep_files"))
    check("...reset says it needs snapshots", "needs snapshots" in (caps.missing(BARE, "reset") or ""), caps.missing(BARE, "reset"))
    b.create("s")
    check("...and its computer can't sleep (NotSupported, saying so)", isinstance(call(b.sleep, "s")[1], NotSupported))
    check("a computer that chose PERSIST=none where files could be kept: the reason names its setting",
          "this computer has PERSIST=none" in (caps.missing(FAKE_MANIFEST, "keep_files", persist="none") or ""),
          caps.missing(FAKE_MANIFEST, "keep_files", persist="none"))


def manifests():
    print("\nthe manifest check")
    check("a manifest missing a key is refused", "missing sizes" in caps.check_manifest({k: v for k, v in FAKE_MANIFEST.items() if k != "sizes"}))
    check("...one with a key it doesn't know", any("unknown key" in p for p in caps.check_manifest(dict(FAKE_MANIFEST, cost=1))))
    check("...a root disk that's neither persistent nor ephemeral",
          any(p.startswith("root_disk") for p in caps.check_manifest(dict(FAKE_MANIFEST, root_disk="sometimes"))))
    check("...volumes without their details", any(p.startswith("volumes.") for p in caps.check_manifest(dict(FAKE_MANIFEST, volumes={}))))
    check("...an egress model it doesn't know", any(p.startswith("egress") for p in caps.check_manifest(
        dict(FAKE_MANIFEST, egress={"model": "magic", "allowlist": True, "private_ranges_blocked": True}))))
    check("...and the storage capabilities are separate keys", all(k in caps.KEYS for k in
                                                                     ("root_disk", "volumes", "snapshots", "backup", "bucket_mount")))
    check("PERSIST words: one line for each mode", set(caps.PERSIST_WORDS) == set(caps.PERSIST_MODES))
    # What's enforced outside the computer is said apart from its own firewall, and never claimed when nothing is.
    nothing = {"enforced": False, "private_ranges": False, "metadata": False, "off": False}
    check("...outside egress rules claimed while nothing is enforced outside: refused",
          any("nothing is enforced outside" in p for p in caps.check_manifest(
              dict(FAKE_MANIFEST, outside_egress=dict(nothing, metadata=True)))))
    check("...outside egress that isn't its four yes/no answers: refused",
          any(p.startswith("outside_egress") for p in caps.check_manifest(
              dict(FAKE_MANIFEST, outside_egress={"enforced": "yes"}))))
    check("...billing it doesn't say (what a stopped computer bills, what an idle stop saves): refused",
          any(p.startswith("billing") for p in caps.check_manifest(
              dict(FAKE_MANIFEST, billing={"stopped": "sometimes", "guest_poweroff": "nothing", "note": "x"})))
          and any(p.startswith("billing") for p in caps.check_manifest(
              dict(FAKE_MANIFEST, billing={"stopped": "storage", "guest_poweroff": "nothing"}))))
    truth = {n: (c.manifest["outside_egress"], c.manifest["billing"]["stopped"], c.manifest["billing"]["guest_poweroff"])
             for n, c in cloud.ADAPTERS.items() if n != "fake"}
    check("each provider's cost and egress truth: Oracle Cloud bills a stopped computer's disk, and a guest that "
          "powered itself off as running; its rules outside refuse private ranges but not the metadata service; your "
          "own server: nothing enforced outside, its billing unknown; this Mac: nothing billed",
          truth["oci"] == ({"enforced": True, "private_ranges": True, "metadata": False, "off": True}, "storage", "nothing")
          and truth["sprites"] == ({"enforced": True, "private_ranges": True, "metadata": True, "off": True}, "storage", "n/a")
          and truth["ubuntu-vm"] == (nothing, "unknown", "n/a") and truth["local-lume"][1:] == ("none", "n/a"), truth)


def resolver():
    print("\nwhich computer (lib/placement.py)")
    R, F = placement.resolve, placement.resolve_flags
    one = [("metacomputer", "mac")]
    two = [("metacomputer", "mac"), ("demo", "mac")]

    def refusal(fn, *a, **k):
        try:
            return None, fn(*a, **k)
        except placement.Refused as e:
            return type(e).__name__, str(e)
    check("one computer: nothing said is it, with no default needed", R(one) == "metacomputer")
    check("...and on mac, it", R(one, "mac") == "metacomputer")
    check("several: nothing said is the default", R(two, None, "demo") == "demo" and R(two, "", "metacomputer") == "metacomputer")
    check("...a name is that computer, whatever the default", R(two, "demo", "metacomputer") == "demo")
    check("...mac is the default when it's there", R(two, "mac", "demo") == "demo")
    kind, msg = refusal(R, two)
    check("...no default and nothing said: unclear, listing them", kind == "Unclear" and "demo, metacomputer" in msg, msg)
    kind, msg = refusal(R, two, "mac")
    check("...mac with no default: unclear, listing them", kind == "Unclear" and "demo, metacomputer" in msg, msg)
    kind, msg = refusal(R, two, None, "gone")
    check("...a default that isn't a computer: refused, never another one", kind == "NotFound" and "gone" in msg, msg)
    check("...but a name said is still that computer", R(two, "demo", "gone") == "demo")
    for word in ("cloud", "Cloud"):
        kind, msg = refusal(R, two, word, "metacomputer")
        check(f"{word}: no cloud computers set up, said plainly", kind == "NotFound" and msg == placement.NO_CLOUD, msg)
    for word in ("fly", "cloudflare", "elsewhere"):
        kind, msg = refusal(R, two, word, "metacomputer", {})
        check(f"'{word}' with no provider: nothing's called that, and no cloud computers are set up", kind == "NotFound"
              and "has no cloud computers set up" in msg, msg)
    kind, msg = refusal(R, two, "oci", "metacomputer")
    check("a registered provider with no computer there yet: says so", kind == "NotFound"
          and "no computer on Oracle Cloud yet" in msg, msg)
    kind, msg = refusal(R, [("metacomputer", "mac"), ("box", "server")], "fly", "metacomputer")
    check("a word that's no place, with a computer elsewhere: refused, listing the computers and places", kind == "NotFound"
          and "box" in msg and "server" in msg and "cloud" in msg, msg)
    kind, msg = refusal(R, [], None)
    check("no computers at all: says so", kind == "NotFound" and "no computer yet" in msg, msg)
    # With providers (as data: none is registered here).
    pr = {"p1": "Provider One", "p2": "Provider Two"}
    many = [("mac1", "mac"), ("a", "p1"), ("b", "p1"), ("c", "p2")]
    check("a provider with one computer: it", R(many, "p2", "mac1", pr) == "c")
    check("...with several: the default if it's one of them", R(many, "p1", "b", pr) == "b")
    kind, msg = refusal(R, many, "p1", "mac1", pr)
    check("...else unclear, listing them", kind == "Unclear" and "a, b" in msg and "Provider One" in msg, msg)
    check("cloud: the default if it's in the cloud", R(many, "cloud", "c", pr) == "c")
    kind, msg = refusal(R, many, "cloud", "mac1", pr)
    check("...else unclear across providers", kind == "Unclear" and "a, b, c" in msg and "in the cloud" in msg, msg)
    check("mac among providers: the one on this Mac", R(many, "mac", "a", pr) == "mac1")
    kind, msg = refusal(R, [("mac1", "mac")], "p1", None, pr)
    check("a provider with no computer yet: says so", kind == "NotFound" and "no computer on Provider One yet" in msg, msg)
    # The CLI's flags.
    check("--vm and --on that agree: the VM", F(two, vm="demo", on="mac") == "demo")
    check("--on mac alone: the default", F(two, on="mac", default="demo") == "demo")
    kind, msg = refusal(F, two, vm="demo", on="cloud", providers={})
    check("--vm and --on that disagree: refused, saying where it is and that there are no cloud computers",
          kind == "Refused" and "disagree" in msg and "this Mac" in msg and "no cloud computers set up" in msg, msg)
    kind, msg = refusal(F, two, on="demo")
    check("--on with a computer's name: refused, pointing at --vm", kind == "Refused" and "--vm demo" in msg, msg)
    for word in ("cloud", "fly", "cloudflare"):
        kind, msg = refusal(F, two, on=word, default="metacomputer", providers={})
        check(f"--on {word} with no provider: this metacomputer has no cloud computers set up", kind == "NotFound"
              and msg.startswith("this metacomputer has no cloud computers set up"), msg)
    kind, msg = refusal(F, two, on="cloud", default="metacomputer")
    check("--on cloud with the providers registered and every computer on this Mac: no cloud computers",
          kind == "NotFound" and msg == placement.NO_CLOUD, msg)
    mixed = [("metacomputer", "mac"), ("box", "server"), ("arm", "oci")]
    check("--on oci: the one computer there", F(mixed, on="oci", default="metacomputer") == "arm")
    check("--on server: the one there", F(mixed, on="server", default="metacomputer") == "box")
    kind, msg = refusal(F, mixed, on="cloud", default="metacomputer")
    check("--on cloud with one on each provider and the default on this Mac: unclear, listing them",
          kind == "Unclear" and "arm, box" in msg, msg)
    check("--on cloud with the default in the cloud: it", F(mixed, on="cloud", default="arm") == "arm")
    kind, msg = refusal(F, mixed, vm="box", on="oci")
    check("--vm box --on oci: they disagree (it's your own server)", kind == "Refused" and "your own server" in msg, msg)
    kind, msg = refusal(F, two, on="")
    check("--on with nothing: refused", kind == "Refused", msg)
    # Computers named like places (made by hand: vm create refuses the names): never what a place means.
    look = [("cloud", "mac"), ("mac", "mac"), ("main", "mac")]
    kind, msg = refusal(F, look, on="cloud", default="cloud", providers={})
    check("--on cloud with a local computer named cloud as the default: no cloud computers (never that one)",
          kind == "NotFound" and msg == placement.NO_CLOUD, msg)
    kind, msg = refusal(F, look, vm="cloud", on="cloud")
    check("--vm cloud --on cloud: they disagree (it's on this Mac)", kind == "Refused" and "disagree" in msg, msg)
    check("--on mac: the default, not the computer named mac", F(look, on="mac", default="main") == "main")
    check("--on mac with the computer named cloud as the default: it (it is on this Mac)", F(look, on="mac", default="cloud") == "cloud")
    for word in ("cloud", "mac"):
        kind, msg = refusal(R, look, word, "main")
        check(f"resolve('{word}') with a computer of that name: unclear, naming --vm and --on",
              kind == "Unclear" and "--vm" in msg and "--on" in msg, (kind, msg))
    kind, msg = refusal(R, look, "Cloud", "main", {})
    check("resolve('Cloud'): the place (names keep their case): no cloud computers", kind == "NotFound"
          and msg == placement.NO_CLOUD, (kind, msg))
    named = [("p1", "mac"), ("a", "p1"), ("b", "p2")]
    check("--on a provider with a local computer of that provider's name: the provider's computer",
          F(named, on="p1", default="p1", providers=pr) == "a")
    check("place_name: mac, cloud and a provider's name are places' names; another isn't",
          all(placement.place_name(n, pr) for n in ("mac", "cloud", "MAC", "p1")) and placement.place_name("demo", pr) is None)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "placement.py"), "name-ok", "cloud"], capture_output=True, text=True)
    r2 = subprocess.run([sys.executable, os.path.join(LAB, "lib", "placement.py"), "name-ok", "demo"], capture_output=True, text=True)
    check("placement.py name-ok: exit 1 with why for cloud, 0 for demo", r.returncode == 1 and "place's name" in r.stdout
          and r2.returncode == 0, (r, r2))
    r3 = subprocess.run([sys.executable, os.path.join(LAB, "lib", "placement.py"), "name-ok", "oci"], capture_output=True, text=True)
    check("...and for a provider's place (oci): a computer can't be called that", r3.returncode == 1, r3)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "placement.py"), "resolve", "--on", "cloud",
                        "--computer", "metacomputer=mac", "--default", "metacomputer"], capture_output=True, text=True)
    check("placement.py resolve --on cloud: exit 1, the reason on stderr, nothing on stdout", r.returncode == 1
          and r.stdout == "" and "no cloud computers set up" in r.stderr and "nothing was done" in r.stderr, r)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "placement.py"), "resolve", "--on", "mac",
                        "--computer", "metacomputer=mac", "--computer", "demo=mac", "--default", "demo"],
                       capture_output=True, text=True)
    check("placement.py resolve --on mac: the default's name", r.returncode == 0 and r.stdout.strip() == "demo", r)


# --- provider packages: their folders, their CLI manifests, what's made from them, and shared code that names none ----
# Where shared code is: everything the CLI, the scripts and the helpers run, lib/cloud's framework included. Not in it:
# the providers' folders, tests (each provider's checks name it), docs and config (people's pages), the guest's side
# (provisioning/: what runs inside a computer, which this lint leaves to the guest packages), and the vault (vault/,
# Swift: unchanged by the provider packages).
SHARED = ("bin", "scripts", "lib", "install.sh", "get.sh")
# Whole files allowed: the registry (the one list) and what scripts/gen-providers.py makes (checked current below).
LINT_FILES = {"lib/cloud/registry.py": "the registry: the explicit list of providers",
              "lib/cloud/providers.sh": "made from the manifests by scripts/gen-providers.py (checked current here)"}
# Lines allowed, by file and a piece of the line, each for its reason. An entry that matches nothing fails, so a line
# that's gone doesn't keep its permission.
LINT_ALLOW = [
    ("bin/metacomputer", "#                                  there), server (your own server), oci (Oracle Cloud), sprites (Fly Sprites),",
     "usage text (`metacomputer help` prints these comment lines as they are): which places get a label isn't data"),
    ("bin/metacomputer", "#                                  hetzner (Hetzner Cloud), digitalocean, vultr, or cloud (anywhere but this Mac).",
     "usage text, as above"),
    ("bin/metacomputer", "#   vm create NAME --on server --host ADDRESS --user ACCOUNT [--port P] | --on oci [--size SIZE] [--egress internet|off]",
     "usage text: its grouping of providers (Oracle Cloud alone, the hourly three together, Sprites apart) isn't "
     "the registry's order, so it can't be made from the manifests without changing it"),
    ("bin/metacomputer", "#             | --on hetzner|digitalocean|vultr [--size SIZE] [--region REGION] [--egress internet|off]",
     "usage text, as above"),
    ("bin/metacomputer", "#   vm create NAME --on sprites --org ORG [--sprite SPRITE] [--egress internet|off]", "usage text, as above"),
    ("bin/metacomputer", "#                                  a Sprite on Fly.io, reached through Fly's tunnel (docs/sprites.md)",
     "usage text, as above"),
    ("bin/metacomputer", "#                                  what metacomputer made at a provider (oci, sprites, hetzner, digitalocean, vultr),",
     "usage text, as above"),
    ("scripts/vm-commands.sh", '*) die "usage: metacomputer vm list [--on PLACE] [--json] | create NAME',
     "a usage message: it groups Oracle Cloud with the hourly three and lists Sprites last, which isn't the "
     "registry's order, so it can't be made from the manifests without changing it"),
    ("scripts/vm-commands.sh", '-*) die "unknown option $1 (metacomputer vm create NAME',
     "a usage message whose grouping (Oracle Cloud, the hourly three, then Sprites) isn't the registry's order, as above"),
    ("scripts/release.sh", "test_traces test_page_guest test_update test_agents test_cloud_adapters test_session_guard test_remote test_sprites",
     "a test suite's name in the no-VM suites a release runs (the list CLAUDE.md and the release's tests check)"),
    ("scripts/check-public.sh", "domain), phone numbers, home-directory paths, Fly.io machine ids, and every deny-list term;",
     "the publish check looks for any Fly.io app's machine ids in any repository: not the Sprites provider"),
    ("scripts/check-public.sh", "# A Fly.io machine id has letters; fourteen digits alone are a number",
     "the publish check, as above"),
    ("lib/mcp_hub.py", '"/dev/uinput, as on Fly Sprites; keys and typing always go in the foreground there, clicks on an "',
     "the hub's note to the agent (user-facing text): making it from the manifests would load the provider registry "
     "into the hub, which the provider packages leave unchanged"),
    ("lib/mcp_hub.py", '"""On a computer without the driver\'s background input (Fly Sprites): input that would need its virtual devices is',
     "the hub, left unchanged by the provider packages (as above)"),
    ("lib/mcp_hub.py", "# A computer without the driver's background input (no /dev/uinput: Fly Sprites; its provider's manifest says",
     "the hub, left unchanged by the provider packages (as above)"),
]
MARK_BEGIN, MARK_END = "# >>> generated by scripts/gen-providers.py", "# <<< generated by scripts/gen-providers.py"


def provider_words():
    """{provider: a pattern for the words that name it}: its name, place and transport in any case, its label's words
    (less the generic "Cloud"), its credentials and the settings it alone reads, each as a whole word (letters and
    digits around it don't count; an underscore does, so OCI_USER names oci)."""
    from cloud import manifests as mf
    out = {}
    for n in cloud.registry.PROVIDERS:
        m, c = mf.manifest(n), mf.cli(n)
        ci = {n, m["place"]} | ({c["transport"]} if c["transport"] else set())
        cs = ({w for w in m["label"].split() if w != "Cloud"} | set(c["credentials"]) | set(c["transport_settings"])
              | ({c["python"]} if c["python"] else set()) | {e for _o, e in c["cloud_options"]})
        alts = [f"(?i:{re.escape(w)})" for w in sorted(ci)] + [re.escape(w) for w in sorted(cs)]
        out[n] = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])")
    return out


def shared_files():
    out = []
    for top in SHARED:
        full = os.path.join(LAB, top)
        if os.path.isfile(full):
            out.append(top)
            continue
        for d, ds, fs in os.walk(full):
            ds[:] = sorted(x for x in ds if x != "__pycache__")
            out += [os.path.relpath(os.path.join(d, f), LAB) for f in sorted(fs)]
    return out


def naming_lines(words, files):
    """[(file, line number, line, providers it names)] of the shared files, less the allowed files and the generated
    blocks; lines of files that aren't text are skipped."""
    hits = []
    for rel in files:
        if rel in LINT_FILES:
            continue
        try:
            with open(os.path.join(LAB, rel), encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        generated = False
        for i, line in enumerate(text.split("\n"), 1):
            if line.startswith(MARK_BEGIN):
                generated = True
            if generated:
                generated = not line.startswith(MARK_END)
                continue
            named = [n for n, p in words.items() if p.search(line)]
            if named:
                hits.append((rel, i, line, named))
    return hits


def packages():
    print("\nprovider packages (providers/<name>/)")
    from cloud import manifests as mf
    reg = cloud.registry
    check("the registry is an explicit list, in lib/cloud/registry.py, of names that are folders here",
          isinstance(reg.PROVIDERS, tuple) and reg.PROVIDERS and len(set(reg.PROVIDERS)) == len(reg.PROVIDERS)
          and all(os.path.isdir(os.path.join(LAB, "providers", n)) for n in reg.PROVIDERS), reg.PROVIDERS)
    folders = sorted(d for d in os.listdir(os.path.join(LAB, "providers"))
                     if os.path.isdir(os.path.join(LAB, "providers", d)) and d != "__pycache__")
    check("...and every folder in providers/ is registered (a folder isn't loaded by being there)",
          folders == sorted(reg.PROVIDERS), folders)
    need = ("__init__.py", "adapter.py", "manifest.py", "settings.env", "README.md", "claims.tsv")
    missing = [f"{n}/{f}" for n in reg.PROVIDERS for f in need if not os.path.isfile(os.path.join(LAB, "providers", n, f))]
    check("each provider's folder has its adapter, manifest, settings, page and doc-claims", not missing, missing)
    check("...its adapter registers itself under its folder's name (ADAPTER), and lib/cloud has it in the registry's "
          "order, after this Mac's, the stand-in's and your own server's",
          all(reg.adapter_class(n).name == n for n in reg.PROVIDERS)
          and list(cloud.ADAPTERS)[3:] == list(reg.PROVIDERS), list(cloud.ADAPTERS))
    bad = {n: mf.check_cli(n) for n in reg.PROVIDERS if mf.check_cli(n)}
    check("each provider's CLI manifest is sound (lib/cloud/manifests.py check_cli)", not bad, bad)
    check("...and a broken one is refused (an unknown key, a credential that isn't a variable's name, a shared option)",
          bool(_cli_problems({"owners": ("@x",), "nope": 1})) and bool(_cli_problems({"owners": ("@x",), "credentials": ("bad name",)}))
          and bool(_cli_problems({"owners": ("@x",), "create_options": (("--size", "a size", "", False),)}))
          and not _cli_problems({"owners": ("@x",)}))
    check("...an idle stop default that isn't minutes (1-999) or off is refused, so it can't break vm create later "
          "(review of item 5, C4)", bool(_cli_problems({"owners": ("@x",), "idle_stop_default": "never"}))
          and bool(_cli_problems({"owners": ("@x",), "idle_stop_default": 0}))
          and not _cli_problems({"owners": ("@x",), "idle_stop_default": "off"})
          and not _cli_problems({"owners": ("@x",), "idle_stop_default": 30}))
    check("no two providers declare one option differently (manifests.option_conflicts)", not mf.option_conflicts(),
          mf.option_conflicts())
    orig_cli, (p0, p1) = mf.cli, reg.PROVIDERS[:2]
    mf.cli = lambda n: dict(orig_cli(n), cloud_options=((("--org", "A_ORG"),) if n == p0 else (("--org", "B_ORG"),)
                                                       if n == p1 else orig_cli(n)["cloud_options"]))
    try:
        conflicts, listed = mf.option_conflicts(), mf.cloud_options()
    finally:
        mf.cli = orig_cli
    check("...two that do are named, and the option is listed once (the command line would take it twice: review "
          "of item 5, C1)", len(conflicts) == 1 and "--org" in conflicts[0] and [o[0] for o in listed].count("--org") == 1,
          (conflicts, listed))
    rbs = open(os.path.join(LAB, "scripts", "remote-build.sh")).read()
    check("remote-build.sh copies a provider's guest parts into its target's folder (their contents: a folder that's "
          "there already gets them beside its own, never a guest/ inside it; review of item 5, C2)",
          'mkdir -p "$pv_dest" && cp -R "$LAB_ROOT/providers/$PROVIDER/guest/." "$pv_dest/"' in rbs)
    settings_bad = [n for n in reg.PROVIDERS
                    if any(ln.strip() and not ln.startswith("#")
                           for ln in open(os.path.join(LAB, "providers", n, "settings.env")).read().splitlines())]
    check("...its settings.env holds comments only (nothing in it is read as a setting)", not settings_bad, settings_bad)
    r = subprocess.run([sys.executable, os.path.join(LAB, "scripts", "gen-providers.py"), "--check"],
                       capture_output=True, text=True, timeout=60)
    check("what's made from the manifests is current: docs/providers.md, lib/cloud/providers.sh, the credential lines "
          "and CODEOWNERS (scripts/gen-providers.py --check)", r.returncode == 0, r.stdout + r.stderr)
    owners = open(os.path.join(LAB, ".github", "CODEOWNERS")).read().splitlines()
    check("CODEOWNERS has each provider's folder, with its owners", all(
        f"/providers/{n}/ {' '.join(mf.cli(n)['owners'])}" in owners for n in reg.PROVIDERS))
    # The credential lines: every provider's credentials, in the three files, exactly what the generator writes.
    creds = " ".join(mf.credentials())
    lines = {f: [ln for ln in open(os.path.join(LAB, f)).read().splitlines() if 'do export -n "$mc_k"; done' in ln]
             for f in ("bin/metacomputer", "scripts/lib.sh", "scripts/remote-build.sh")}
    check("bin/metacomputer, scripts/lib.sh and scripts/remote-build.sh each take every provider's credentials off the "
          "environment first thing, as the manifests name them", all(
              v == [f'for mc_k in {creds}; do export -n "$mc_k"; done; unset mc_k'] for v in lines.values())
          and __import__("remote").PROVIDER_ENV == mf.credentials(), lines)
    # The lint: its pattern, tried on known hits and known misses first (a pattern that matches nothing passes all).
    words = provider_words()
    probes = {"PROVIDER=oci": {"oci"}, "export -n OCI_USER": {"oci"}, "Fly's sprite command": {"sprites"},
              "SPRITE_ORG=x": {"sprites"}, "on Hetzner Cloud": {"hetzner"}, "HCLOUD_TOKEN": {"hetzner"},
              "DIGITALOCEAN_TOKEN": {"digitalocean"}, "--on vultr": {"vultr"}, "Oracle Cloud": {"oci"},
              "the associate's social voice": set(), "on the fly": set(), "spritecli-free text": set()}
    seen = {t: {n for n, p in words.items() if p.search(t)} for t in probes}
    check("the lint's pattern finds a provider's words and nothing else (tried on known lines first)", seen == probes, seen)
    files = shared_files()
    check("...it reads the shared code (bin/metacomputer, lib/remote.py, scripts/lib.sh, lib/cloud/vps.py among them)",
          all(f in files for f in ("bin/metacomputer", "lib/remote.py", "scripts/lib.sh", "scripts/vm-commands.sh",
                                   "scripts/remote-build.sh", "lib/cloud/vps.py")) and len(files) > 50, len(files))
    hits = naming_lines(words, files)
    used, unallowed = set(), []
    for rel, i, line, named in hits:
        k = next((j for j, (f, piece, _why) in enumerate(LINT_ALLOW) if f == rel and piece in line), None)
        if k is None:
            unallowed.append(f"{rel}:{i}: {line.strip()[:140]} ({', '.join(named)})")
        else:
            used.add(k)
    check("no shared code names a provider outside its folder (allowed: the registry, the generated files and blocks, "
          "and LINT_ALLOW's lines, each for its reason)", not unallowed, unallowed)
    stale = [LINT_ALLOW[j][:2] for j in range(len(LINT_ALLOW)) if j not in used]
    check("...and every allowed line is still there (a permission goes with its line)", not stale, stale)
    check("...each allowed line says why", all(len(why) >= 15 for _f, _p, why in LINT_ALLOW))


def _cli_problems(cli_dict):
    """check_cli's problems for a made-up provider's CLI (the oci folder's manifest module, its CLI swapped for a moment)."""
    from cloud import manifests as mf
    mod = cloud.registry.manifest("oci")
    real = mod.CLI
    try:
        mod.CLI = cli_dict
        return mf.check_cli("oci")
    finally:
        mod.CLI = real


def main():
    registry()
    for label, a in subjects():
        contract(label, a)
    local_lume()
    fake_lifecycle()
    manifests()
    resolver()
    packages()
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
