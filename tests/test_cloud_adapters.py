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
    out = [(n, cloud.adapter(n)) for n in cloud.registered()]
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
    check("no provider is registered: cloud is empty here (the stand-in and this Mac don't count)",
          cloud.providers() == {} and placement.PROVIDERS == {}, cloud.providers())
    check("an adapter that isn't registered is a KeyError", call(cloud.adapter, "nosuch")[0] == "error")
    # Static: the adapters' code imports nothing that reaches the network or starts programs.
    banned = {"socket", "urllib", "http", "subprocess", "requests", "ssl", "asyncio"}
    bad = []
    for f in sorted(os.listdir(os.path.join(LAB, "lib", "cloud"))):
        if not f.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(LAB, "lib", "cloud", f)).read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            bad += [f"{f}: {n}" for n in names if n.split(".")[0] in banned]
    check("lib/cloud imports no network or process module", not bad, bad)


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
        kind, msg = refusal(R, two, word, "metacomputer")
        check(f"'{word}': nothing's called that, and no cloud computers are set up", kind == "NotFound"
              and "has no cloud computers set up" in msg, msg)
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
    kind, msg = refusal(F, two, vm="demo", on="cloud")
    check("--vm and --on that disagree: refused, saying where it is and that there are no cloud computers",
          kind == "Refused" and "disagree" in msg and "this Mac" in msg and "no cloud computers set up" in msg, msg)
    kind, msg = refusal(F, two, on="demo")
    check("--on with a computer's name: refused, pointing at --vm", kind == "Refused" and "--vm demo" in msg, msg)
    for word in ("cloud", "fly", "cloudflare"):
        kind, msg = refusal(F, two, on=word, default="metacomputer")
        check(f"--on {word}: this metacomputer has no cloud computers set up", kind == "NotFound"
              and msg.startswith("this metacomputer has no cloud computers set up"), msg)
    kind, msg = refusal(F, two, on="")
    check("--on with nothing: refused", kind == "Refused", msg)
    # Computers named like places (made by hand: vm create refuses the names): never what a place means.
    look = [("cloud", "mac"), ("mac", "mac"), ("main", "mac")]
    kind, msg = refusal(F, look, on="cloud", default="cloud")
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
    kind, msg = refusal(R, look, "Cloud", "main")
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
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "placement.py"), "resolve", "--on", "cloud",
                        "--computer", "metacomputer=mac", "--default", "metacomputer"], capture_output=True, text=True)
    check("placement.py resolve --on cloud: exit 1, the reason on stderr, nothing on stdout", r.returncode == 1
          and r.stdout == "" and "no cloud computers set up" in r.stderr and "nothing was done" in r.stderr, r)
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "placement.py"), "resolve", "--on", "mac",
                        "--computer", "metacomputer=mac", "--computer", "demo=mac", "--default", "demo"],
                       capture_output=True, text=True)
    check("placement.py resolve --on mac: the default's name", r.returncode == 0 and r.stdout.strip() == "demo", r)


def main():
    registry()
    for label, a in subjects():
        contract(label, a)
    local_lume()
    fake_lifecycle()
    manifests()
    resolver()
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
