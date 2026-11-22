#!/usr/bin/env python3
"""Network allowlists (lib/netpolicy.py, `metacomputer net`), no VM: names, narrowing and widening, the terminal
rule, the rules file the gateway reads (checked with the real cua-netgw when it's built here, else a stand-in that
reads it the same way), the end of a list, files it can't read, and the start path's wiring."""
import json
import os
import hashlib
import pty
import re
import select
import socket
import threading
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LAB, "lib"))
import netpolicy  # noqa: E402

NP = os.path.join(LAB, "lib", "netpolicy.py")
REAL_GW = os.path.join(LAB, "vendor", "bin", "mcomp-netgw")
TD = tempfile.mkdtemp(prefix="mcomp-np-")
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"   [{str(detail)[:300]}]"))


STUB_GW = os.path.join(TD, "stub-netgw")
with open(STUB_GW, "w") as f:
    f.write("#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if os.path.exists(os.path.join(os.path.dirname(sys.argv[0]), 'refuse')): print('stub: refused'); sys.exit(1)\n"
            "d = json.load(open(sys.argv[2])); assert sys.argv[1] == '-check-rules'\n"
            "assert isinstance(d['rules'], list) and isinstance(d['default_accept'], bool)\n"
            "print('ok')\n")
os.chmod(STUB_GW, 0o755)
GW = REAL_GW if os.access(REAL_GW, os.X_OK) else STUB_GW


def env(run, **extra):
    e = dict(os.environ, MCOMP_RUN_DIR=run, MCOMP_NETGW_BIN=extra.pop("gw", GW), MCOMP_NETGW_ALIVE="0",
             MCOMP_EVENTS_DIR=os.path.join(TD, "events"), MCOMP_EVENTS_SPOOL=os.path.join(TD, "spool"),
             MCOMP_VM_EVENTS_SPOOL=os.path.join(TD, "spool"))
    e.update(extra)
    return e


def net(run, *args, **extra):
    r = subprocess.run([sys.executable, NP, *args], capture_output=True, text=True, env=env(run, **extra),
                       stdin=subprocess.DEVNULL, timeout=60)
    return r.returncode, r.stdout + r.stderr


# netpolicy.py in a fresh Python, its view of the agent session above it set by FAKE_AGENT (a pid: an agent session such
# as Codex, which can give a command a terminal of its own; unset: none, as at the user's own terminal, even when an
# agent runs this test). tests/test_owners.py checks the process-table side end to end.
NP_AS = ("import os, runpy, sys; sys.path.insert(0, %r); import owners; "
         "_a = os.environ.get('FAKE_AGENT'); owners.claude_of = (lambda *a: int(_a)) if _a else (lambda *a: None); "
         "sys.argv = [%r] + sys.argv[1:]; runpy.run_path(%r, run_name='__main__')") % (os.path.join(LAB, "lib"), NP, NP)


def net_tty(run, *args, **extra):
    """Run with stdin and stderr on a terminal (a pty), as at the user's own terminal (FAKE_AGENT="pid": a terminal an
    agent session gave the command)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, "-c", NP_AS, *args], env(run, **extra))
    out, deadline = b"", time.time() + 60
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def run_dir(name):
    d = os.path.join(TD, name)
    os.makedirs(d, mode=0o700)
    return d


def state(run):
    return json.load(open(os.path.join(run, "net-allow.json")))


def rules(run):
    return json.load(open(os.path.join(run, "netgw-rules.json")))


def kill_timer(run):
    try:
        os.kill(int(open(os.path.join(run, "net-expiry.pid")).read()), 15)
    except (OSError, ValueError):
        pass


def names_checks():
    print("names")
    n = netpolicy.normalize
    check("a name covers itself and its subdomains", n("Example.COM.") == ["example.com", "*.example.com"])
    check("--exact: only the name", n("example.com", exact=True) == ["example.com"])
    check("*.name: only the subdomains", n("*.example.com") == ["*.example.com"])
    for bad in ("1.2.3.4", "localhost", "exa mple.com", "-bad.com", "a..com", "*", "*.com.", "x" * 64 + ".com",
                "ex_ample.com", "例え.jp", "http://example.com", "example.com/path", "example.com:443",
                "0x7f.1", "0x7f.0.0.1", "*.0x7f.0.0.1", "10.0x0.1", "1.0x7f", "2130706433.0"):
        try:
            n(bad)
            ok = False
        except netpolicy.Refused:
            ok = True
        check(f"refused as a name: {bad[:40]!r}", ok)
    check("names with digits or 0x in them are still names", n("0x7f.example.com", exact=True) == ["0x7f.example.com"]
          and n("123.example.com", exact=True) == ["123.example.com"] and n("localhost.example.com", exact=True)
          == ["localhost.example.com"])
    c = netpolicy.covered
    allow = ["example.com", "*.example.com"]
    check("a subdomain of an allowed wildcard is covered (adding it widens nothing)",
          c("www.example.com", allow) and c("*.cdn.example.com", allow))
    check("…a different name or a parent isn't", not c("example.org", allow) and not c("*.com", allow)
          and not c("badexample.com", allow))


def allow_checks():
    print("starting, narrowing and widening a list")
    run = run_dir("a")
    rc, out = net(run, "allow", "example.com", "Wikipedia.org", "--for", "30m")
    st = state(run)
    check("a list starts without a terminal (narrowing the open internet)", rc == 0 and "allowlist until" in out
          and sorted(st["allow"]) == ["*.example.com", "*.wikipedia.org", "example.com", "wikipedia.org"], (rc, out))
    check("…for the time asked", 1790 <= st["expires"] - st["started"] <= 1810, st)
    r = rules(run)
    check("the gateway's rules: nothing by default, only the listed names", r["default_accept"] is False
          and r["default_reason"] == netpolicy.REASON and sorted(r["domains"]["allow"]) == sorted(st["allow"]), r)
    base = json.load(open(netpolicy.BASE_RULES))
    check("…and every refusal of config/netgw-rules.json kept", r["rules"] == base["rules"]
          and r.get("block_host_addrs") == base.get("block_host_addrs"), r)
    mode = os.stat(os.path.join(run, "netgw-rules.json")).st_mode & 0o777
    check("…written private (0600)", mode == 0o600 and os.stat(os.path.join(run, "net-allow.json")).st_mode & 0o777 == 0o600,
          oct(mode))
    check("a timer that lifts it when it ends is running", os.path.exists(os.path.join(run, "net-expiry.pid")))
    rc, out = net(run, "allow", "www.example.com")
    check("a name the list already covers changes nothing, even without a terminal", rc == 0 and "nothing changed" in out
          and state(run) == st, out)
    rc, out = net(run, "allow", "github.com")
    check("adding a name without a terminal is refused (it widens the list), and nothing changes",
          rc == 3 and "your own terminal" in out and state(run) == st, (rc, out))
    rc, out = net(run, "allow", "example.com", "--for", "20h")
    check("more time without a terminal is refused too", rc == 3 and state(run) == st, (rc, out))
    rc, out = net(run, "clear")
    check("lifting it early without a terminal is refused", rc == 3 and os.path.exists(os.path.join(run, "net-allow.json")),
          (rc, out))
    rc, out = net(run, "remove", "wikipedia.org")
    st2 = state(run)
    check("removing a name works without a terminal (it narrows the list)", rc == 0
          and sorted(st2["allow"]) == ["*.example.com", "example.com"] and st2["expires"] == st["expires"], (rc, out))
    check("…and the gateway's rules follow", sorted(rules(run)["domains"]["allow"]) == ["*.example.com", "example.com"])
    rc, out = net_tty(run, "allow", "github.com", FAKE_AGENT="4242")
    check("a terminal under an agent session (Codex can give its commands one) doesn't widen the list",
          rc == 3 and "github.com" not in state(run)["allow"] and "at your own terminal" in out, (rc, out))
    rc, out = net_tty(run, "clear", FAKE_AGENT="4242")
    check("...or lift it", rc == 3 and os.path.exists(os.path.join(run, "net-allow.json")), (rc, out))
    rc, out = net_tty(run, "allow", "github.com")
    check("at a terminal, a name can be added", rc == 0 and "github.com" in state(run)["allow"]
          and "github.com" in rules(run)["domains"]["allow"], (rc, out))
    rc, out = net_tty(run, "clear")
    check("at a terminal, the list can be lifted: the rules are config/netgw-rules.json's again", rc == 0
          and not os.path.exists(os.path.join(run, "net-allow.json")) and rules(run) == base, (rc, out))
    check("…and its timer stops", not os.path.exists(os.path.join(run, "net-expiry.pid")))
    rc, out = net(run, "allow", "x.org", "--for", "25h")
    check("a list lasts at most 24 hours", rc == 3 and not os.path.exists(os.path.join(run, "net-allow.json")), out)
    names = [f"n{i}.example.org" for i in range(40)]
    rc, out = net(run, "allow", *names)
    check(f"at most {netpolicy.MAX_NAMES} patterns in a list", rc == 3 and not os.path.exists(os.path.join(run, "net-allow.json")),
          out)
    rc, out = net(run, "allow", "1.1.1.1")
    check("an address is refused (names only)", rc == 3 and "addresses aren't accepted" in out, out)
    for local in ("localhost", "127.0.0.1", "app.localhost"):
        rc, out = net(run, "allow", local)
        check(f"{local}: refused with why (the VM's own pages never go through the gateway)", rc == 3
              and "the virtual computer itself" in out and not os.path.exists(os.path.join(run, "net-allow.json")), out)


def failure_checks():
    print("what can't be checked or read")
    run = run_dir("f")
    net(run, "allow", "example.com")
    st, before = state(run), open(os.path.join(run, "netgw-rules.json")).read()
    kill_timer(run)
    stub_dir = os.path.join(TD, "refusing")
    os.makedirs(stub_dir)
    refusing = os.path.join(stub_dir, "gw")
    with open(STUB_GW) as f, open(refusing, "w") as g:
        g.write(f.read())
    os.chmod(refusing, 0o755)
    open(os.path.join(stub_dir, "refuse"), "w").close()
    rc, out = net(run, "remove", "example.com", gw=refusing)
    check("rules the gateway refuses never replace the ones in force; the narrower list is saved first (it applies at "
          "the next start), and the command fails", rc == 3 and "refused the rules" in out and "saved" in out
          and state(run)["allow"] == [] and open(os.path.join(run, "netgw-rules.json")).read() == before, (rc, out))
    json.dump(st, open(os.path.join(run, "net-allow.json"), "w"))
    rc, out = net(run, "remove", "example.com", gw=os.path.join(TD, "missing"))
    check("…and without the gateway's binary the rules aren't written (the narrower list waits for the next start)",
          rc == 3 and open(os.path.join(run, "netgw-rules.json")).read() == before, out)
    json.dump(st, open(os.path.join(run, "net-allow.json"), "w"))
    with open(os.path.join(run, "net-allow.json"), "w") as f:
        f.write("{not json")
    rc, out = net(run, "render")
    check("a list file that can't be read stops `render` (so the VM doesn't start with the internet open)",
          rc == 3 and "can't read" in out, out)
    rc, out = net(run, "status")
    check("…and status says so", rc == 3 and "can't read" in out, out)
    with open(os.path.join(run, "net-allow.json"), "w") as f:
        json.dump({"version": 1, "allow": ["evil com"], "started": 0, "expires": time.time() + 99}, f)
    rc, out = net(run, "render")
    check("a stored name the gateway wouldn't accept is refused too", rc == 3, out)
    os.unlink(os.path.join(run, "net-allow.json"))
    os.symlink(os.path.join(TD, "elsewhere.json"), os.path.join(run, "net-allow.json"))
    with open(os.path.join(TD, "elsewhere.json"), "w") as f:
        json.dump({"version": 1, "allow": ["example.com"], "started": 0, "expires": time.time() + 99}, f)
    rc, out = net(run, "status")
    check("a list file that's a link isn't followed", rc == 3, out)
    os.unlink(os.path.join(run, "net-allow.json"))
    run2 = run_dir("f2")
    base = os.path.join(TD, "base-open.json")
    b = json.load(open(netpolicy.BASE_RULES))
    b["rules"].insert(0, {"net": "192.168.1.0/24", "accept": True, "reason": "user:printer"})
    json.dump(b, open(base, "w"))
    old = netpolicy.BASE_RULES
    netpolicy.BASE_RULES = base
    try:
        netpolicy.compose(b, {"allow": ["example.com"], "expires": 0, "started": 0})
        ok = False
    except netpolicy.Refused as e:
        ok = "already opens" in str(e)
    finally:
        netpolicy.BASE_RULES = old
    check("a base file that opens an address range of its own isn't combined with a list (the list wouldn't hold)", ok)
    rc, out = net(run2, "allow", "example.com", MCOMP_NETGW_ALIVE="1")
    check("a running gateway that doesn't read RUN_DIR's rules (started by an earlier version): told to restart, "
          "exit 4", rc == 4 and "earlier version" in out, (rc, out))
    rc, out = net(run2, "allow", "example.com", MCOMP_NETGW_ALIVE="1")
    check("…and asking again for the same names says so again (not \"already allowed\" with exit 0)",
          rc == 4 and "earlier version" in out, (rc, out))
    kill_timer(run2)
    with open(os.path.join(run2, "gw.json"), "w") as f:
        json.dump({"rules": os.path.join(run2, "netgw-rules.json")}, f)
    rc, out = net(run2, "status", "--json")
    check("status --json: the list and whether the gateway reads it", rc == 0
          and json.loads(out)["gateway_reads_list"] is True and json.loads(out)["allowlist"]["allow"], out)
    rc, out = net("relative/dir", "status")
    check("no absolute run folder: refused", rc == 3, out)


def end_checks():
    print("the end of a list")
    run = run_dir("e")
    net(run, "allow", "example.com")
    kill_timer(run)
    st = state(run)
    st["expires"] = int(time.time()) + 2
    json.dump(st, open(os.path.join(run, "net-allow.json"), "w"))
    t0 = time.time()
    r = subprocess.run([sys.executable, NP, "_expire", str(st["expires"])], env=env(run), timeout=60,
                       capture_output=True, text=True)
    check("the timer lifts the list when its time is up, and the rules are the base file's again",
          r.returncode == 0 and not os.path.exists(os.path.join(run, "net-allow.json"))
          and rules(run) == json.load(open(netpolicy.BASE_RULES)) and time.time() - t0 >= 1.0, r.stderr)
    net(run, "allow", "example.com")
    kill_timer(run)
    st = state(run)
    r = subprocess.run([sys.executable, NP, "_expire", str(st["expires"] - 100)], env=env(run), timeout=30,
                       capture_output=True, text=True)
    check("a timer for a list that changed since (another end) leaves it alone",
          r.returncode == 0 and os.path.exists(os.path.join(run, "net-allow.json")))
    st["expires"] = int(time.time()) - 5
    json.dump(st, open(os.path.join(run, "net-allow.json"), "w"))
    rc, out = net(run, "status")
    check("a list whose time ran out while nothing watched counts as none", rc == 0 and "open:" in out, out)
    rc, out = net(run, "render")
    check("…and the next start (render) removes it", rc == 0 and not os.path.exists(os.path.join(run, "net-allow.json"))
          and rules(run) == json.load(open(netpolicy.BASE_RULES)), out)
    rc, out = net(run, "render")
    check("with no list, render writes config/netgw-rules.json's rules", rc == 0
          and rules(run) == json.load(open(netpolicy.BASE_RULES)), out)


def wiring_checks():
    print("the start path")
    lib = open(os.path.join(LAB, "scripts", "lib.sh")).read()
    check("the gateway reads this VM's rules file, not the checkout's", '"rules": "$RUN_DIR/netgw-rules.json",' in lib
          and '"rules": "$LAB_ROOT/config/netgw-rules.json"' not in lib)
    start = lib[lib.index("netgw_start() {"):lib.index("netgw_control() {")]
    check("starting the gateway writes the rules first, and a list it can't read stops the start",
          start.index("net_policy render || die") < start.index('netgw_render "$1"') < start.index("daemonize"))
    cli = open(os.path.join(LAB, "bin", "metacomputer")).read()
    check("`metacomputer net` reaches lib/netpolicy.py through net_policy (the gateway binary, whether it runs)",
          "net)        export_options; net_policy \"$@\"; exit $? ;;" in cli
          and 'MCOMP_NETGW_BIN="$NETGW_BIN" MCOMP_NETGW_ALIVE="$alive" MCOMP_NETGW_CONTROL="$SOCK_DIR/control.sock"' in lib)


class FakeControl:
    """A stand-in for the gateway's control socket: `rules` gets "ok <digest>" of the file it was told it loaded."""
    def __init__(self, reply):
        self.path = os.path.join(tempfile.mkdtemp(prefix="np-", dir="/tmp"), "c.sock")
        self.reply = reply
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                if c.recv(64).strip() == b"rules":
                    r = self.reply() if callable(self.reply) else self.reply
                    if r is not None:
                        c.sendall((r + "\n").encode())


def confirm_checks():
    print("in force: the gateway's own word")
    run = run_dir("c")
    with open(os.path.join(run, "gw.json"), "w") as f:
        json.dump({"rules": os.path.join(run, "netgw-rules.json")}, f)
    def loaded():
        try:
            return "ok " + hashlib.sha256(open(os.path.join(run, "netgw-rules.json"), "rb").read()).hexdigest()
        except OSError:
            return "ok " + "0" * 64
    good = FakeControl(loaded)
    rc, out = net(run, "allow", "example.com", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=good.path)
    check("a change the gateway reports loading: exit 0, \"in force\"", rc == 0 and "in force" in out, (rc, out))
    rc, out = net(run, "status", "--json", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=good.path)
    check("status --json says it's in force", rc == 0 and json.loads(out)["in_force"] is True, out)
    stale = FakeControl("ok " + "1" * 64)
    t0 = time.time()
    rc, out = net(run, "remove", "example.com", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=stale.path)
    check("a gateway still on other rules after the wait: exit 4, \"didn't take\" (the saved list stays)",
          rc == 4 and "didn't take" in out and state(run)["allow"] == [] and time.time() - t0 >= 3, (rc, out))
    rc, out = net(run, "status", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=stale.path)
    check("…and status says it isn't in force", rc == 0 and "not in force" in out, out)
    st_now = state(run)
    json.dump(dict(st_now, allow=["example.org"]), open(os.path.join(run, "net-allow.json"), "w"))
    rc, out = net(run, "status", "--json", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=good.path)
    check("a saved list whose rules were never written (a change cut off after saving it): status says not in force, "
          "even though the gateway has the file on disk in force", rc == 0 and json.loads(out)["in_force"] is False, out)
    json.dump(st_now, open(os.path.join(run, "net-allow.json"), "w"))
    old = FakeControl("no unknown request")
    rc, out = net(run, "remove", "example.com", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=old.path)
    check("a gateway that can't report its rules (cua-netgw before the patch): exit 4 at once, even for a change that "
          "changes nothing", rc == 4 and "earlier build" in out, (rc, out))
    silent = FakeControl(None)
    rc, out = net(run, "remove", "example.com", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=silent.path)
    check("a control socket that doesn't answer: exit 4, never success", rc == 4, (rc, out))
    rc, out = net(run, "remove", "example.com", MCOMP_NETGW_ALIVE="1", MCOMP_NETGW_CONTROL=os.path.join(TD, "none.sock"))
    check("no control socket at all: exit 4", rc == 4, (rc, out))
    kill_timer(run)
    src = open(NP).read()
    body = src[src.index("def cmd_clear("):src.index("def cmd_render(")]
    check("clear writes the rules and confirms them while it holds the lock (no render after it's released)",
          body.index("commit(p, None)") < body.index("rc = report(") < body.index("emit(")
          and "    emit(" in body.split("with Lock")[1].split("\n    emit(")[0] + "\n    emit(")
    for fn in ("cmd_allow", "cmd_remove", "cmd_render", "cmd_expire"):
        b2 = src[src.index(f"def {fn}("):]
        b2 = b2[b2.index("\n"):b2.index("\ndef ", 10)]       # the body, without the def line
        lock = b2.index("with Lock(")
        check(f"{fn}: every rules write is inside the lock", all(b2.index(c) > lock for c in ("commit(", "render(") if c in b2))
    com = src[src.index("def commit("):src.index("def cmd_allow(")]
    check("the list is saved before the rules are written", com.index('os.replace(write_private(p["state"]')
          < com.index("return render(p, new)"))


def patch_checks():
    """The gateway patch applies to the pinned cua-netgw source and its tests (with the patch's own) pass. Needs Go and
    the source build-tools.sh fetched (.cache/cua-src); without them this says SKIP, as build-tools.sh checks it too."""
    print("the gateway patch (patches/netgw/)")
    import shutil
    src = os.path.join(LAB, ".cache", "cua-src")
    commit = re.search(r"^CUA_SRC_COMMIT=(\w+)", open(os.path.join(LAB, "config", "defaults.env")).read(), re.M).group(1)
    if not shutil.which("go") or not os.path.isdir(os.path.join(src, ".git")):
        print("SKIP  Go or the pinned source (.cache/cua-src) isn't here: run scripts/build-tools.sh first")
        return
    tree = tempfile.mkdtemp(prefix="mcomp-netgw-test-")
    try:
        arc = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", src, "archive", commit, "libs/cua/netgw"],
                             capture_output=True, timeout=60)
        if arc.returncode != 0:
            print("SKIP  the pinned commit isn't in .cache/cua-src yet (scripts/build-tools.sh fetches it)")
            return
        subprocess.run(["tar", "-x", "-C", tree], input=arc.stdout, check=True)
        pats = sorted(f for f in os.listdir(os.path.join(LAB, "patches", "netgw")) if f.endswith(".patch"))
        ok = all(subprocess.run(["git", "apply", "--whitespace=nowarn", os.path.join(LAB, "patches", "netgw", f)],
                                cwd=tree, capture_output=True).returncode == 0 for f in pats)
        check("every patch applies to the pinned source, in order", ok and pats, pats)
        r = subprocess.run(["go", "test", "-count=1", "./..."], cwd=os.path.join(tree, "libs", "cua", "netgw"),
                           capture_output=True, text=True, timeout=600,
                           env=dict(os.environ, GOTOOLCHAIN="local", CGO_ENABLED="0", GOFLAGS="-mod=readonly"))
        check("the patched gateway's tests pass (Cua's and the patch's: allowed names resolve and bind under a default "
              "deny, PTR questions follow the name rules, the control socket reports the rules in force)",
              r.returncode == 0 and "internal/gateway" in r.stdout, (r.stdout + r.stderr)[-600:])
    finally:
        shutil.rmtree(tree, ignore_errors=True)


if __name__ == "__main__":
    print(f"(rules checked with {'the real mcomp-netgw' if GW == REAL_GW else 'a stand-in for the gateway'})")
    names_checks()
    allow_checks()
    failure_checks()
    end_checks()
    confirm_checks()
    wiring_checks()
    patch_checks()
    for d in os.listdir(TD):
        if os.path.isdir(os.path.join(TD, d)):
            kill_timer(os.path.join(TD, d))
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
