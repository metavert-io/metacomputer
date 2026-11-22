#!/usr/bin/env python3
"""The agent-session computer guard (lib/sessionvm.py, bin/metacomputer's session_guard).

A session whose MCP tools act on computer A must not act on computer B from the shell because a command named no
computer: a login shell that reorders PATH can run the main install instead of a wrapper, and a default changed since
the session started points elsewhere. No VM: a copy of the checkout with two VMs' settings and a fake runner
(tests/test_multi_vm.py's Box), a small program named `codex` standing in for the agent session (tests/userterm.py),
and a private folder for the session records (MCOMP_SESSIONS_DIR). Hubs started here serve the copy's VMs (off: the
fake runner has none) and are stopped at the end. Nothing of this Mac's VMs, settings, vault or agents is read or
touched.

    python3 tests/test_session_guard.py
"""
import itertools
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
TD = tempfile.mkdtemp(prefix="mcsg-", dir="/tmp")          # short: the copy's hub sockets live under it
SESSIONS = os.path.join(TD, "sessions")
RUN = os.path.join(TD, "run")
os.environ.update(MCOMP_SESSIONS_DIR=SESSIONS, MCOMP_RUN_DIR=RUN, MCOMP_VM_NAME="unitvm", MCOMP_SSH_PORT="2999")
for _k in ("MCOMP_CONFIG", "MCOMP_VM", "MCOMP_NO_DEFAULT_VM", "MCOMP_GUARD_VM", "MCOMP_EXPECT_VM"):
    os.environ.pop(_k, None)
sys.path.insert(0, os.path.join(LAB, "lib"))
sys.path.insert(0, TESTS)
import owners  # noqa: E402
import sessionvm  # noqa: E402
import userterm  # noqa: E402
from test_multi_vm import Box, PY, write  # noqa: E402

FAILS = []
REFUSED_WORDS = "Nothing was done."


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[-700:]})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def comp(vm, port, lab=LAB, kind="vm", config=""):
    return {"vm": vm, "ssh_port": str(port), "run_dir": os.path.join(TD, "r-" + vm), "lab": lab, "config": config,
            "kind": kind}


def rec_files():
    try:
        return sorted(n for n in os.listdir(SESSIONS) if sessionvm.FILE_RE.fullmatch(n))
    except OSError:
        return []


def clear_records():
    for n in os.listdir(SESSIONS) if os.path.isdir(SESSIONS) else []:
        p = os.path.join(SESSIONS, n)
        if os.path.islink(p) or os.path.isfile(p):
            os.remove(p)


# --- which commands the guard looks at -------------------------------------------------------------------------
def dispatch_commands():
    """The command words of bin/metacomputer's main dispatch (its last `case "$cmd" in`)."""
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    block = src[src.rindex('case "$cmd" in'):]
    block = block[:block.index("\nesac")]
    words = set()
    for line in block.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and ")" in s and line.startswith("  ") and not line.startswith("   "):
            label = s.split(")", 1)[0]
            if label != "*":
                words.update(label.split("|"))
    return words


# Every command of the dispatch, with its forms that act, only read or are exempt, as bin/metacomputer (or the program
# it hands them to) parses them. ORDER_FREE commands read their arguments in any order; the others take a subcommand
# first, and the rows list the other orders they're given in. "act*": acts in this order only (bin/metacomputer or
# the program refuses it as given, so it's never taken for a read); in another order it's another form.
FORMS = [
    # whole commands that act, whatever their arguments
    ("start", [], "act"), ("start", ["--no-volumes"], "act"), ("stop", ["--force", "--override"], "act"),
    ("restart", ["--no-volumes"], "act"), ("reset", ["--yes", "--discard-unsynced"], "act"),
    ("restore", ["ck", "--yes"], "act"), ("baseline", ["--yes"], "act"), ("checkpoint", ["ck", "--replace"], "act"),
    ("checkpoint", ["--delete", "ck", "--yes"], "act"), ("build", ["--replace"], "act"),
    ("desktop-restart", [], "act"), ("test", ["A", "B"], "act"), ("call", ["--session", "s", "click", "{}"], "act"),
    ("call", ["list_windows"], "act"), ("web", ["read", "https://example.com"], "act"), ("put", ["f", "--to", "d"], "act"),
    ("rename", ["a", "b"], "act"), ("sh", ["-c", "true", "--read-only"], "act"), ("ssh", ["ops", "true"], "act"),
    ("task", ["start", "x"], "act"), ("task", ["finish", "x"], "act"), ("release", [], "act"),
    # whole commands that only read the VM or who controls it, whatever their arguments
    ("who", [], "read"), ("who", ["--json"], "read"), ("status", ["--json"], "read"),
    ("inspect", ["windows", "--json", "--top", "5"], "read"), ("logs", ["40"], "read"), ("get", ["x", "d", "--task", "t"], "read"),
    ("shot", ["out.png", "--task", "t"], "read"), ("view", ["--print"], "read"), ("view", ["--open", "--no-remember"], "read"),
    # commands that act on no one computer, name their own, or are plumbing
    ("list", ["--json"], "exempt"), ("vm", ["delete", "x", "--confirm", "x"], "exempt"), ("default", ["x"], "exempt"),
    ("each", ["a,b", "--", "stop"], "exempt"), ("mcp", [], "exempt"), ("mcp-lazy", [], "exempt"), ("help", [], "exempt"),
    ("-h", [], "exempt"), ("--help", [], "exempt"), ("where", [], "exempt"), ("version", ["--json"], "exempt"),
    ("update", ["--check"], "exempt"), ("setup", ["--yes"], "exempt"), ("events", ["status"], "exempt"),
    ("notify", ["x", "--why", "login"], "exempt"), ("learn", ["report"], "exempt"), ("actions", ["list"], "exempt"),
    ("agents", ["check"], "exempt"), ("volume", ["list"], "exempt"), ("refdisk", ["list"], "exempt"),
    ("contact-sheet", ["a.mp4"], "exempt"), ("checkpoints", [], "exempt"), ("_health", [], "exempt"),
    ("_runinfo", [], "exempt"), ("_record-pipe", ["n"], "exempt"), ("_task-sync", ["n"], "exempt"),
    ("_vnc-install", [], "exempt"), ("_vnc-push", [], "exempt"), ("frobnicate", ["stop"], "exempt"),
    # security: every argument is --json or apply, in any order; apply anywhere applies (F1)
    ("security", [], "read"), ("security", ["--json"], "read"), ("security", ["apply"], "act"),
    ("security", ["apply", "--json"], "act"), ("security", ["--json", "apply"], "act"), ("security", ["--bogus"], "act"),
    # recordings: --sync anywhere copies the VM's task runs; otherwise a list of the Mac's recordings
    ("recordings", [], "exempt"), ("recordings", ["5"], "exempt"), ("recordings", ["--sync"], "act"),
    ("recordings", ["5", "--sync"], "act"),
    # input-reset: only the first argument counts
    ("input-reset", [], "act"), ("input-reset", ["--check"], "read"), ("input-reset", ["x", "--check"], "act*"),
    # record: the first argument, status by default; moments reads a file on the Mac
    ("record", [], "read"), ("record", ["status"], "read"), ("record", ["moments", "r.mp4"], "exempt"),
    ("record", ["start"], "act"), ("record", ["start", "n", "20"], "act"), ("record", ["stop"], "act"),
    ("record", ["r.mp4", "moments"], "act*"),
    # browser: none or --json first shows the defaults; profiles alone lists the VM's
    ("browser", [], "exempt"), ("browser", ["--json"], "exempt"), ("browser", ["profiles"], "read"),
    ("browser", ["profiles", "--new", "p"], "act"), ("browser", ["profiles", "--delete", "p", "--yes"], "act"),
    ("browser", ["default", "chrome"], "act"), ("browser", ["mode", "page"], "act"), ("browser", ["pacing", "fast"], "act"),
    ("browser", ["--new", "profiles", "p"], "act*"),
    # hub
    ("hub", [], "read"), ("hub", ["status"], "read"), ("hub", ["status", "--json"], "read"), ("hub", ["env", "--json"], "exempt"),
    ("hub", ["stop"], "act"), ("hub", ["stop", "--if-idle"], "act"), ("hub", ["start"], "act"), ("hub", ["--json", "status"], "act*"),
    # claude
    ("claude", ["read", "--lines", "5"], "read"), ("claude", ["wait"], "read"), ("claude", ["status", "--json"], "read"),
    ("claude", ["--help"], "exempt"), ("claude", [], "act"), ("claude", ["--token", "k"], "act"),
    ("claude", ["send", "hi", "--wait"], "act"), ("claude", ["stop", "--revoke"], "act"), ("claude", ["--session", "s", "read"], "act*"),
    # handoff
    ("handoff", ["status", "--json"], "read"), ("handoff", ["wait"], "read"), ("handoff", ["--help"], "exempt"),
    ("handoff", ["--why", "login", "--say", "x"], "act"), ("handoff", ["done"], "act"), ("handoff", ["cancel"], "act"),
    ("handoff", ["--json", "status"], "act*"),
    # net
    ("net", [], "exempt"), ("net", ["status", "--json"], "exempt"), ("net", ["allow", "x.com", "--for", "1h"], "act"),
    ("net", ["remove", "x.com"], "act"), ("net", ["clear"], "act"), ("net", ["--json", "status"], "act*"),
    # trace
    ("trace", ["list"], "exempt"), ("trace", ["show", "t"], "exempt"), ("trace", ["export", "t", "--out", "f"], "exempt"),
    ("trace", ["start", "t"], "act"), ("trace", ["stop"], "act"), ("trace", ["delete", "t"], "act"),
    # secrets: what only reads the vault (on the Mac), nested reads included
    ("secrets", ["list"], "exempt"), ("secrets", ["status", "--json"], "exempt"), ("secrets", ["audit", "--verify"], "exempt"),
    ("secrets", ["tokens"], "exempt"), ("secrets", ["requests"], "exempt"), ("secrets", ["rule", "list"], "exempt"),
    ("secrets", ["audit-sink", "list"], "exempt"), ("secrets", ["audit-sink", "show", "s"], "exempt"),
    ("secrets", ["fill", "x", "--field", "password"], "act"), ("secrets", ["token", "k"], "act"),
    ("secrets", ["rule", "add", "code", "--site", "s"], "act"), ("secrets", ["rule", "remove", "r"], "act"),
    ("secrets", ["audit-sink", "test", "s"], "act"), ("secrets", ["session", "load", "n"], "act"),
    ("secrets", ["list", "rule"], "exempt"), ("secrets", ["--json", "list"], "act*"),
    # secrets with no subcommand is status; the vault's other reads; and every subcommand that changes something
    ("secrets", [], "exempt"), ("secrets", ["version"], "exempt"), ("secrets", ["version", "--json"], "exempt"),
    ("secrets", ["settle", "--timeout", "30"], "exempt"), ("secrets", ["selftest"], "exempt"), ("secrets", ["help"], "exempt"),
    ("secrets", ["-h"], "exempt"), ("secrets", ["--help"], "exempt"), ("secrets", ["rule", "list", "--json"], "exempt"),
    ("secrets", ["init"], "act"), ("secrets", ["set", "x", "--kind", "password"], "act"), ("secrets", ["delete", "x"], "act"),
    ("secrets", ["bind"], "act"), ("secrets", ["rekey"], "act"), ("secrets", ["recover"], "act"),
    ("secrets", ["rotate-session-key"], "act"), ("secrets", ["unlock", "--for", "30m"], "act"), ("secrets", ["lock"], "act"),
    ("secrets", ["login", "x", "--password-selector", "#p"], "act"), ("secrets", ["ask", "--site", "s"], "act"),
    ("secrets", ["give", "id"], "act"), ("secrets", ["code", "--site", "s", "--human"], "act"),
    ("secrets", ["relay", "--from", "a/b", "--to", "c/d"], "act"), ("secrets", ["requests"], "exempt"),
    ("secrets", ["approve", "id"], "act"), ("secrets", ["deny", "id"], "act"), ("secrets", ["policy", "x", "--approval", "each"], "act"),
    ("secrets", ["presence", "on"], "act"), ("secrets", ["import-passwords", "--csv", "f"], "act"),
    ("secrets", ["import-session", "--browser", "chrome"], "act"), ("secrets", ["session", "save", "n"], "act"),
    ("secrets", ["session", "policy", "n"], "act"), ("secrets", ["session", "delete", "n"], "act"),
    ("secrets", ["revoke", "--all"], "act"), ("secrets", ["audit-sink", "add", "s"], "act"),
    ("secrets", ["audit-sink", "remove", "s"], "act"), ("secrets", ["test-legacy-totp-link"], "act"),
]
ORDER_FREE = sessionvm.READS | {"security", "recordings"}


def orders(args):
    """Every order of the arguments (at most 24)."""
    return list(itertools.islice(itertools.permutations(args), 24))


def classification_checks():
    print("which commands the guard looks at")
    c = sessionvm.classify
    bad = [(w, a, want, c(w, a)) for w, a, want in FORMS if c(w, a) != want.rstrip("*")]
    check("every form in the table is told apart: acts, only reads the VM, or is exempt", not bad, bad)
    bad = [(w, list(o)) for w, a, want in FORMS if want == "act" for o in orders(a) if c(w, list(o)) != "act"]
    check("a form that acts acts in every order of its arguments (security --json apply included)", not bad, bad)
    bad = [(w, list(o), want, c(w, list(o))) for w, a, want in FORMS if w in ORDER_FREE
           for o in orders(a) if c(w, list(o)) != want]
    check("commands that read their arguments in any order are classified the same in every order", not bad, bad)
    words = dispatch_commands()
    covered = {w for w, _a, _k in FORMS}
    check("every command of bin/metacomputer's dispatch has rows here (a new command needs a decision)",
          len(words) > 40 and not sorted(words - covered), sorted(words - covered))
    # Every subcommand of the vault's own parser (vault/Commands.swift) and of bin/metacomputer's do_secrets has rows.
    swift = open(os.path.join(LAB, "vault", "Commands.swift")).read()
    block = swift[swift.index("switch cmd {"):]
    block = block[:block.index("default:")]
    vault_subs = {n for line in block.splitlines() for n in re.findall(r'"([a-z-]+)"', line.split(":")[0])
                  if line.strip().startswith("case ")}
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    fn = src[src.index("\ndo_secrets() {"):]
    fn = fn[:fn.index("\n}\n")]
    shell_subs = {w for line in fn.splitlines() if re.match(r"^    [a-z|-]+\)", line)
                  for w in line.strip().split(")")[0].split("|")}
    rows = {a[0] for w, a, _k in FORMS if w == "secrets" and a}
    check("every subcommand the vault and bin/metacomputer know for secrets has rows (with no subcommand: status)",
          len(vault_subs) > 30 and not sorted((vault_subs | shell_subs) - rows) and ("secrets", [], "exempt") in FORMS,
          sorted((vault_subs | shell_subs) - rows))
    known = sessionvm.EXEMPT | sessionvm.READS | set(getattr(sessionvm, "FORMS", {})) | sessionvm.ACTS
    unsorted = sorted(w for w in words if not w.startswith("_") and w not in known)
    check("...and is sorted in sessionvm.py on purpose", not unsorted, unsorted)
    src = open(os.path.join(LAB, "bin", "metacomputer")).read()
    end = src.index('*) session_guard "$@" ;;')
    sh_words = set()
    for line in src[src.rindex('case "$cmd" in', 0, end):end].splitlines()[1:]:
        if line.strip().endswith(") ;;"):
            sh_words.update(line.strip().split(")")[0].split("|"))
    sh_words.discard("_*")
    check("bin/metacomputer skips exactly the commands sessionvm.py calls exempt (no Python for those)",
          sh_words == sessionvm.EXEMPT, sorted(sh_words ^ sessionvm.EXEMPT))


# --- records -------------------------------------------------------------------------------------------------------
STARTS = {}          # pid -> start time, for the record checks (sessionvm.proc_start is the kernel's elsewhere)


def record_checks():
    print("session records")
    clear_records()
    real_start = getattr(sessionvm, "proc_start", None)
    sessionvm.proc_start = lambda pid: STARTS.get(int(pid))
    try:
        record_identity_checks()
        record_folder_checks()
    finally:
        sessionvm.proc_start = real_start
        os.environ["MCOMP_SESSIONS_DIR"] = SESSIONS
        getattr(sessionvm, "_STORE_CHECKED", {}).clear()
    record_words_checks()


def record_identity_checks():
    a, bridge = 70001, 70002
    STARTS.update({a: "100.000001", bridge: "200.000002", 70003: "300.000003"})
    table = {a: (1, "/opt/x/codex"), bridge: (a, "python3"), 70003: (1, "python3")}
    A, B = comp("alpha", 2401), comp("beta", 2402)
    check("a bridge's record is written (private folder, 0700), naming both processes by pid and start time",
          sessionvm.note_bridge(a, bridge, A, agent="Codex") and oct(os.stat(SESSIONS).st_mode & 0o777) == "0o700"
          and json.load(open(os.path.join(SESSIONS, sessionvm.bridge_name(a, bridge)))).get("holder_start") == "200.000002",
          rec_files())
    got = sessionvm.held(a, table)
    check("...and it stands while the bridge runs under that session", len(got) == 1 and got[0]["computer"]["vm"] == "alpha", got)
    check("...for the session's commands: on another computer they clash, on its own they don't",
          sessionvm.conflicts(B, got) and not sessionvm.conflicts(A, got))
    other = {**table, bridge: (70003, "python3")}
    check("a bridge that isn't under the session doesn't count, and its record goes",
          sessionvm.held(a, other) == [] and rec_files() == [], rec_files())
    sessionvm.note_bridge(a, bridge, A)
    STARTS[bridge] = "250.000000"                      # the bridge ended; another child of the session has its pid
    check("a bridge whose pid now names another process of the session (another start time) doesn't count, and its "
          "record goes", sessionvm.held(a, table) == [] and rec_files() == [], rec_files())
    STARTS[bridge] = "200.000002"
    sessionvm.note_bridge(a, bridge, A)
    STARTS[a] = "150.000000"                           # the session ended; another agent process has its pid
    check("a record of an earlier session with this pid (another start time) doesn't count, and goes",
          sessionvm.held(a, table) == [] and rec_files() == [], rec_files())
    STARTS[a] = "100.000001"
    sessionvm.note_bridge(a, bridge, A)
    STARTS[80001], STARTS[80002] = "400.0", "401.0"
    sessionvm.note_bridge(80001, 80002, B)
    sessionvm.held(a, table)
    check("records of a session that has ended are removed when the guard looks",
          rec_files() == [sessionvm.bridge_name(a, bridge)], rec_files())
    sessionvm.drop_bridge(a, bridge)
    check("a bridge removes its own record when it ends", rec_files() == [])

    # Claims: lib/owners.py write_claim leaves a record for the session; it stands while the claim does.
    owners.write_claim(a, a, "/proj", "sh", time.time(), extra={"claude_start": STARTS[a]})
    names = rec_files()
    check("an agent session's claim (owners.write_claim) leaves a record of where it is", len(names) == 1
          and "-claim-" in names[0], names)
    m0 = os.stat(os.path.join(SESSIONS, names[0])).st_mtime_ns
    time.sleep(0.02)
    owners.write_claim(a, a, "/proj", "sh", time.time(), extra={"claude_start": STARTS[a]})
    check("...written once (later claims for the same processes cost one small read)",
          os.stat(os.path.join(SESSIONS, names[0])).st_mtime_ns == m0)
    got = sessionvm.held(a, table)
    check("...and it stands while that session's claim is in that computer's owners folder, its holder alive",
          len(got) == 1 and got[0]["computer"]["vm"] == "unitvm" and got[0].get("last_tool") == "sh"
          and got[0]["computer"]["run_dir"] == os.path.realpath(RUN), got)
    owners.write_claim(70003, None, "/proj", "sh", time.time(), kind="shell", sid=1)
    check("a claim outside an agent session leaves no record", len(rec_files()) == 1, rec_files())
    claim = json.load(open(owners.claim_path(a)))
    write(owners.claim_path(a), json.dumps({**claim, "claude_pid": 70003}))
    check("a claim in that holder's name that's no longer the session's doesn't count",
          sessionvm.held(a, table, prune=False) == [])
    write(owners.claim_path(a), json.dumps(claim))
    STARTS[a] = "160.000000"
    check("a claim record whose session process is another one now (pid reused) doesn't count, and goes",
          sessionvm.held(a, table) == [] and rec_files() == [], rec_files())
    STARTS[a] = "100.000001"
    owners.write_claim(a, a, "/proj", "sh", time.time(), extra={"claude_start": STARTS[a]})
    os.remove(owners.claim_path(a))
    check("a claim that's gone doesn't count, and its record goes", sessionvm.held(a, table) == [] and rec_files() == [],
          rec_files())
    # A claim without the session's start time (one written before they were kept, or renewed from one) can't be
    # shown to be this session's: it never counts, and a refresh doesn't give it the current process's.
    keeper = 70005
    STARTS[keeper] = "700.000007"
    table_k = {**table, keeper: (1, "python3")}
    write(owners.claim_path(keeper), json.dumps({"bridge_pid": keeper, "claude_pid": a, "cwd": "/proj", "started": 1.0,
                                                 "last_call": time.time(), "last_tool": "claude", "kind": "inner"}))
    STARTS[a] = "185.000000"                           # the session that took it is gone; its pid is another's now
    owners.write_claim(keeper, a, "/proj", "claude", 1.0, kind="inner",
                       extra={"claude_start": json.load(open(owners.claim_path(keeper))).get("claude_start")})
    check("a claim kept from before start times were kept, renewed after its pid was reused, gets no start time and "
          "no record: the process that has the pid now holds nothing", sessionvm.held(a, table_k) == []
          and not json.load(open(owners.claim_path(keeper))).get("claude_start")
          and not any("-claim-" in n for n in rec_files()), rec_files())
    STARTS[a] = "100.000001"
    owners.write_claim(keeper, a, "/proj", "claude", 1.0, kind="inner", extra={"claude_start": STARTS[a]})
    stood = len(sessionvm.held(a, table_k, prune=False)) == 1
    claim = json.load(open(owners.claim_path(keeper)))
    write(owners.claim_path(keeper), json.dumps({k: v for k, v in claim.items() if k != "claude_start"}))
    check("a claim whose start time is gone (one written before they were kept) doesn't count, though its record is "
          "there", stood and sessionvm.held(a, table_k) == [], stood)
    os.remove(owners.claim_path(keeper))

    # A claim refreshed after its session has gone (a hub's lease for calls the session left running) keeps the
    # session's start time from when it was taken: a later process with the pid isn't tied to it.
    hub = 70004
    STARTS[hub] = "500.000005"
    table_h = {**table, hub: (1, "python3")}
    owners.write_claim(hub, a, "/proj", "click", time.time(), kind="lease", extra={"claude_start": STARTS[a]})
    stood = len(sessionvm.held(a, table_h, prune=False)) == 1
    STARTS[a] = "180.000000"                           # the session ended; another agent process has its pid now
    owners.write_claim(hub, a, "/proj", "click", time.time(), kind="lease", extra={"claude_start": "100.000001"})
    check("a lease refreshed after its session ended keeps that session's start time: the process that has the pid "
          "now doesn't hold the computer", stood and sessionvm.held(a, table_h) == []
          and json.load(open(owners.claim_path(hub))).get("claude_start") == "100.000001")
    STARTS[a] = "100.000001"
    os.remove(owners.claim_path(hub))
    for f in os.listdir(owners.OWNERS_DIR):
        if f.endswith(".json"):
            os.remove(os.path.join(owners.OWNERS_DIR, f))
    hub_lease_checks(a, table)


def hub_lease_checks(a, table):
    """The hub keeps its client's session start time from when it connected, and its lease carries it."""
    import socket
    import mcp_hub
    me = os.getpid()
    STARTS[me] = "600.000006"
    hub = mcp_hub.Hub(env=dict(os.environ), inject_sessions=False, local=False)
    hub._start_lease_keeper = lambda: None
    sa, sb = socket.socketpair()
    c = mcp_hub.Client(hub, sa, 70002)
    c.hello, c.kind, c.cwd, c.claude, c.holder = True, "claude-code", "/proj", a, 70002
    c.claude_start = STARTS[a]                         # as on_hello records it
    hub.pending["c1.1"] = {"client": c, "id": 1, "acting": True, "tool": "click", "gen": hub.up_gen, "t0": time.time()}
    table_h = {**table, me: (1, "python3")}
    try:
        hub.hand_over_claim(c, [("c1.1", "click")], drop_own=True)
        stood = len(sessionvm.held(a, table_h, prune=False)) == 1
        STARTS[a] = "190.000000"                       # the session's gone; its pid is another process's now
        hub._refresh_leases()
        check("the hub's lease, refreshed after its session ended, isn't tied to the process that has the pid now",
              stood and sessionvm.held(a, table_h) == []
              and json.load(open(owners.claim_path(me))).get("claude_start") == "100.000001", stood)
    finally:
        STARTS[a] = "100.000001"
        sa.close()
        sb.close()
        hub.pending.clear()
        for f in os.listdir(owners.OWNERS_DIR):
            if f.endswith(".json"):
                os.remove(os.path.join(owners.OWNERS_DIR, f))


def record_folder_checks():
    import safedelete
    import vms
    a, bridge = 70001, 70002
    table = {a: (1, "/opt/x/codex"), bridge: (a, "python3")}
    A = comp("alpha", 2401)
    # Files that aren't records this module wrote are never read further and never removed.
    os.makedirs(SESSIONS, exist_ok=True)
    target = os.path.join(TD, "elsewhere.json")
    write(target, json.dumps({"v": 1, "kind": "mcp", "agent_pid": a, "holder_pid": bridge, "selected": False,
                              "computer": A}))
    os.symlink(target, os.path.join(SESSIONS, sessionvm.bridge_name(a, bridge)))
    write(os.path.join(SESSIONS, f"{a}-mcp-70009.json"), "{not json")
    write(os.path.join(SESSIONS, f"{a}-claim-abc.json"), json.dumps({"v": 1, "kind": "claim", "agent_pid": 1,
                                                                     "computer": A}))
    write(os.path.join(SESSIONS, f"{a}-mcp-70010.json"), "x" * (sessionvm.MAX_RECORD + 10))
    write(os.path.join(SESSIONS, "99999-mcp-99998.json"), "not a record, and its session is long gone")
    before = rec_files()
    got = sessionvm.held(a, table)
    check("a file named like a record that isn't one (a link, not JSON, too big, another session's, or of a session "
          "that has ended): not counted, and left as it is", got == [] and rec_files() == before
          and os.path.exists(target) and len(before) == 5, (got, rec_files()))
    clear_records()

    # Pruning goes through lib/safedelete.py: when it refuses the folder, nothing is removed.
    STARTS[80001], STARTS[80002] = "400.0", "401.0"
    sessionvm.note_bridge(80001, 80002, A)
    del STARTS[80001]                                  # that session has ended
    real_protected = safedelete.protected
    safedelete.protected = lambda *a_, **k: (_ for _ in ()).throw(safedelete.Refused("protected folders unknown"))
    try:
        sessionvm.held(a, table)
        kept = rec_files()
    finally:
        safedelete.protected = real_protected
    sessionvm.held(a, table)
    check("removing an ended session's record goes through lib/safedelete.py: nothing goes when it refuses, and it "
          "goes once it doesn't", kept == [sessionvm.bridge_name(80001, 80002)] and rec_files() == [], (kept, rec_files()))

    # MCOMP_SESSIONS_DIR: a folder that's already there is used only as it is.
    shared = os.path.join(TD, "shared")
    os.makedirs(shared)
    os.chmod(shared, 0o775)
    os.environ["MCOMP_SESSIONS_DIR"] = shared
    import io
    import contextlib
    err = io.StringIO()
    real_ps = owners.ps_table
    owners.ps_table = lambda strict=False: (_ for _ in ()).throw(AssertionError("ps read"))
    try:
        with contextlib.redirect_stderr(err):
            rc_ = sessionvm.guard(["--vm", "beta", "--ssh-port", "2402", "--", "stop"])
    finally:
        owners.ps_table = real_ps
    check("a group-writable folder named by MCOMP_SESSIONS_DIR isn't used, and its permissions stay as they were",
          sessionvm.folder() is None and not sessionvm.note_bridge(a, bridge, A) and os.listdir(shared) == []
          and oct(os.stat(shared).st_mode & 0o777) == "0o775", oct(os.stat(shared).st_mode & 0o777))
    check("...and a command says the records aren't used (exit 2, going on)",
          rc_ == sessionvm.UNKNOWN and "isn't a private folder" in err.getvalue(), (rc_, err.getvalue()))
    link = os.path.join(TD, "sessions-link")
    os.symlink(SESSIONS, link)
    os.environ["MCOMP_SESSIONS_DIR"] = link
    check("a records folder that's a link isn't used (nothing read or written)",
          sessionvm.folder() is None and not sessionvm.note_bridge(a, bridge, A))
    vmfolder = os.path.join(TD, "a-vms-run-dir")
    os.makedirs(vmfolder, mode=0o700)
    os.environ["MCOMP_SESSIONS_DIR"] = vmfolder
    getattr(sessionvm, "_STORE_CHECKED", {}).clear()
    home = safedelete.account_home()
    safedelete.protected = lambda *a_, **k: [("a VM's RUN_DIR", vmfolder, *vms.physical(vmfolder, home, "x"), [], "RUN_DIR")]
    try:
        wrote = sessionvm.note_bridge(a, bridge, A)
    finally:
        safedelete.protected = real_protected
    check("a folder MCOMP_SESSIONS_DIR names that is a VM's folder (safedelete's protected list) gets no record",
          not wrote and os.listdir(vmfolder) == [], os.listdir(vmfolder))

    # Every write checks the folder again: one that became protected, or a link on the way that now leads elsewhere.
    store = os.path.join(TD, "n5-store")
    os.makedirs(store, mode=0o700)
    os.environ["MCOMP_SESSIONS_DIR"] = store
    getattr(sessionvm, "_STORE_CHECKED", {}).clear()
    safedelete.protected = lambda *a_, **k: []
    try:
        first = sessionvm.note_bridge(a, bridge, A)
        safedelete.protected = lambda *a_, **k: [("a VM's RUN_DIR", store, *vms.physical(store, home, "x"), [], "RUN_DIR")]
        os.remove(os.path.join(store, sessionvm.bridge_name(a, bridge)))
        second = sessionvm.note_bridge(a, bridge, A)
    finally:
        safedelete.protected = real_protected
    check("a write checks the folder again: once it's protected, nothing more is written there",
          first and not second and os.listdir(store) == [], (first, second, os.listdir(store)))
    real1, real2, alias = (os.path.join(TD, n) for n in ("n5-real1", "n5-real2", "n5-alias"))
    for r in (real1, real2):
        os.makedirs(os.path.join(r, "sessions"), mode=0o700)
    os.symlink(real1, alias)
    os.environ["MCOMP_SESSIONS_DIR"] = os.path.join(alias, "sessions")
    getattr(sessionvm, "_STORE_CHECKED", {}).clear()
    guarded = os.path.join(real2, "sessions")
    safedelete.protected = lambda *a_, **k: [("a VM's RUN_DIR", guarded, *vms.physical(guarded, home, "x"), [], "RUN_DIR")]
    try:
        first = sessionvm.note_bridge(a, bridge, A)
        os.remove(alias)
        os.symlink(real2, alias)                       # the same name now leads to a protected folder
        second = sessionvm.note_bridge(a, bridge, A)
    finally:
        safedelete.protected = real_protected
    check("a folder whose path now leads somewhere protected (a link on the way changed) gets nothing",
          first and not second and os.listdir(guarded) == [], (first, second, os.listdir(guarded)))
    os.environ["MCOMP_SESSIONS_DIR"] = SESSIONS
    getattr(sessionvm, "_STORE_CHECKED", {}).clear()


def record_words_checks():
    A, B = comp("alpha", 2401), comp("beta", 2402)
    # What decides: the session's MCP bridges, else its claims.
    rb = {"kind": "mcp", "selected": False, "computer": A, "holder_pid": 1}
    rc = {"kind": "claim", "computer": B, "last_tool": "sh"}
    check("its MCP tools decide when it has a bridge (an old claim elsewhere doesn't make their computer refuse)",
          not sessionvm.conflicts(A, [rb, rc]) and sessionvm.conflicts(B, [rb, rc]) == [rb])
    check("without a bridge, its claims decide: one computer is fine, any other clashes",
          not sessionvm.conflicts(B, [rc]) and sessionvm.conflicts(A, [rc]) == [rc])
    check("claims on two computers: a command that names neither refuses on both",
          sessionvm.conflicts(A, [rc, {**rc, "computer": A}]) and sessionvm.conflicts(B, [rc, {**rc, "computer": A}]))
    check("two bridges of one session (Codex's app server) on two computers: it can't tell, so it refuses",
          sessionvm.conflicts(A, [rb, {**rb, "computer": B}]))
    check("the same VM reached from two checkouts is one computer (its name and SSH port)",
          not sessionvm.conflicts(comp("alpha", 2401, lab="/other"), [rb]))

    # The words.
    text = sessionvm.refusal([rb], comp("beta", 2402), "stop", "default", "config/vms/beta.env", "Codex")
    check("a refusal names both computers, why it chose the other, and each way on",
          "this Codex session's MCP tools act on alpha" in text and "would act on beta" in text
          and "your default computer, config/default-vm" in text and REFUSED_WORDS in text
          and "--vm beta stop ..." in text and "--vm alpha stop ..." in text and "start a new agent session" in text, text)
    sel = sessionvm.refusal([rb], comp("beta", 2402), "sh", "main", "config/local.env", "Claude Code")
    check("...and says when the other is just the main VM",
          "beta (config/local.env's VM, as the command names none)" in sel, sel)
    held_text = sessionvm.refusal([rc], comp("alpha", 2401), "put", "main", "config/local.env", "Codex")
    check("...and what the session did there when it holds a computer by a claim",
          "this Codex session holds beta (it acted there: sh)" in held_text, held_text)
    far = comp("gamma", 2403, lab="/somewhere/else", kind="config", config="/somewhere/else/g.env")
    check("a computer outside this checkout is named by its own settings file",
          "MCOMP_CONFIG=/somewhere/else/g.env /somewhere/else/bin/metacomputer stop" in
          sessionvm.refusal([{**rb, "computer": far}], comp("beta", 2402), "stop", "main", "x", "Codex"))
    n = sessionvm.note([rb], comp("beta", 2402), "who", "main", "config/local.env", "Codex")
    check("a read gets a note instead", n.startswith("metacomputer: note:") and "this reads beta" in n
          and "--vm alpha who" in n, n)

    # The guard's own command, in this process (an agent session is whatever is above it here: none counts as one).
    clear_records()
    real = owners.ps_table
    try:
        sessionvm.note_bridge(os.getpid(), os.getpid(), A)
        owners.ps_table = lambda strict=False: (_ for _ in ()).throw(OSError("ps failed"))
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc_ = sessionvm.guard(["--vm", "beta", "--ssh-port", "2402", "--", "stop"])
        check("when the process table can't be read, it says so and lets the command go on (exit 2)",
              rc_ == sessionvm.UNKNOWN and "couldn't read the process table" in err.getvalue(), (rc_, err.getvalue()))
        owners.ps_table = lambda strict=False: (_ for _ in ()).throw(AssertionError("ps read"))
        clear_records()
        rc_ = sessionvm.guard(["--vm", "beta", "--ssh-port", "2402", "--", "stop"])
        check("with no records at all it answers without reading the process table", rc_ == 0)
        sessionvm.note_bridge(os.getpid(), os.getpid(), A)
        rc_ = sessionvm.guard(["--vm", "beta", "--ssh-port", "2402", "--", "list"])
        check("...nor for a command it never looks at", rc_ == 0)
    finally:
        owners.ps_table = real
        clear_records()


# --- end to end: a stand-in agent session over the checkout's copy ------------------------------------------------
DRIVER = r'''
import json, os, subprocess, sys, threading, time
bridges = {}

def start(name, argv, env):
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                         text=True, bufsize=1)
    out = []
    def rd():
        for line in p.stdout:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=rd, daemon=True).start()
    bridges[name] = (p, out)
    return {"pid": p.pid}

def mcp(name, msg, wait):
    p, out = bridges[name]
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()
    end = time.time() + wait
    while time.time() < end:
        hit = [m for m in out if m.get("id") == msg["id"]]
        if hit:
            return hit[0]
        time.sleep(0.02)
    return {"timeout": True}

def stop(name):
    p, _ = bridges.pop(name)
    p.stdin.close()
    try:
        p.wait(20)
    except subprocess.TimeoutExpired:
        p.kill()
    return {"rc": p.returncode}

for line in sys.stdin:
    req = json.loads(line)
    try:
        if req["op"] == "start":
            res = start(req["name"], req["argv"], req["env"])
        elif req["op"] == "mcp":
            res = mcp(req["name"], req["msg"], req.get("wait", 30))
        elif req["op"] == "stop":
            res = stop(req["name"])
        else:
            r = subprocess.run(req["argv"], env=req["env"], capture_output=True, text=True, timeout=req.get("timeout", 120),
                               stdin=subprocess.DEVNULL)
            res = {"rc": r.returncode, "out": r.stdout, "err": r.stderr}
    except Exception as e:
        res = {"error": repr(e)}
    print(json.dumps(res), flush=True)
for name in list(bridges):
    stop(name)
'''


class Session:
    """A stand-in agent session: a program named `codex` (userterm.fake_agent) running DRIVER, which starts the bridge
    and runs commands as Codex would, so everything below has that `codex` as its nearest agent session."""

    def __init__(self, fake, env):
        self.env = env
        path = os.path.join(TD, "driver.py")
        write(path, DRIVER)
        self.p = subprocess.Popen([fake, PY, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  env=env, bufsize=1)
        self.pid = self.p.pid
        self.mid = 0

    def ask(self, timeout=180, **req):
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        if not select.select([self.p.stdout], [], [], timeout)[0]:
            return {"error": "no answer"}
        line = self.p.stdout.readline()
        return json.loads(line) if line else {"error": "the session ended"}

    def run(self, argv, **extra):
        return self.ask(op="run", argv=argv, env=dict(self.env, **extra))

    def start_bridge(self, name, argv):
        return self.ask(op="start", name=name, argv=argv, env=self.env)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(60)
        except (OSError, subprocess.TimeoutExpired):
            self.p.kill()


def records_of(pid, kind=None):
    out = []
    for n in rec_files():
        if n.startswith(f"{pid}-") and (kind is None or f"-{kind}-" in n):
            r = sessionvm._read_json(os.path.join(SESSIONS, n))
            if r:
                out.append(r)
    return out


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def end_to_end(fake):
    print("end to end: MCP on one computer, a command that names none on another")
    b = Box(TD, "e2e")
    sessions = []
    try:
        co, home = b.co, b.home
        b.settings("config/local.env", "VM_NAME=mainvm\nBASELINE_NAME=mainvm-baseline\n")
        demo_cfg = b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        installed = os.path.join(home, ".local", "bin", "metacomputer")
        os.makedirs(os.path.dirname(installed), exist_ok=True)
        os.symlink(b.mc, installed)                                   # the main install, as install.sh links it
        write(os.path.join(home, ".zprofile"), 'export PATH="$HOME/.local/bin:$PATH"\n')   # as a profile often does
        wrap_dir = os.path.join(b.root, "wrapper")
        wrapper = os.path.join(wrap_dir, "metacomputer")
        write(wrapper, f'#!/bin/bash\nexport MCOMP_CONFIG="{demo_cfg}"\nexec "{b.mc}" "$@"\n', 0o755)
        env = b.env(MCOMP_SESSIONS_DIR=SESSIONS, ZDOTDIR=None, BASH_ENV=None, ENV=None,
                    PATH=f"{wrap_dir}:{os.path.dirname(PY)}:/usr/bin:/bin:/usr/sbin:/sbin")
        login = ('p="$(command -v metacomputer)"; [ "$p" = "$EXPECT_MC" ] || { echo "metacomputer here is $p, not the '
                 'test copy: not run" >&2; exit 99; }; metacomputer "$@"')

        def zl(s, *args, **extra):
            """A command in a login shell (zsh -l: /etc/zprofile's path_helper, then the profile), as Codex runs one."""
            return s.run(["/bin/zsh", "-lc", login, "zsh", *args], EXPECT_MC=installed, **extra)

        s = Session(fake, env)
        sessions.append(s)
        r = s.run(["/bin/bash", "-c", "command -v metacomputer"])
        r2 = s.run(["/bin/zsh", "-lc", "command -v metacomputer"])
        check("the session's own PATH finds the wrapper; a login shell's finds the main install ahead of it "
              "(path_helper and the profile reorder PATH)", r.get("out", "").strip() == wrapper
              and r2.get("out", "").strip() == installed, (r, r2))
        st = s.start_bridge("b", [wrapper, "mcp-lazy"])
        rec = wait_for(lambda: records_of(s.pid, "mcp"))
        check("the session's MCP bridge (started through the wrapper) records that its tools act on demo",
              rec and rec[0]["computer"]["vm"] == "demo" and rec[0]["holder_pid"] == st.get("pid")
              and rec[0]["agent"] == "Codex" and rec[0]["selected"] is False, rec)

        r = zl(s, "stop")
        err = r.get("err", "")
        check("MCP on demo, a login shell's `metacomputer stop` defaulting to mainvm: refused before it acts",
              r.get("rc") == 1 and REFUSED_WORDS in err and "MCP tools act on demo" in err and "would act on mainvm" in err
              and "no VM 'mainvm'" not in err, r)
        check("...naming the fixes: --vm mainvm, --vm demo, or a new session",
              "~/.local/bin/metacomputer --vm mainvm stop ..." in err and "--vm demo stop ..." in err
              and "start a new agent session" in err, err)
        r = s.run(["/bin/bash", "-c", 'metacomputer "$@"', "bash", "stop"])
        check("the same command through the wrapper (MCOMP_CONFIG names demo) goes on, to demo",
              REFUSED_WORDS not in r.get("err", "") and "no VM 'demo'" in r.get("err", ""), r)
        r = zl(s, "--vm", "mainvm", "stop")
        check("--vm mainvm from the login shell: it named its computer, so it goes on",
              REFUSED_WORDS not in r.get("err", "") and "no VM 'mainvm'" in r.get("err", ""), r)
        r = zl(s, "stop", MCOMP_VM="mainvm")
        check("...as does MCOMP_VM=mainvm", REFUSED_WORDS not in r.get("err", "") and "no VM 'mainvm'" in r.get("err", ""), r)
        r = zl(s, "who")
        check("a read (who) goes ahead with a note naming both",
              r.get("rc") == 0 and "metacomputer: note:" in r.get("err", "") and "this reads mainvm" in r.get("err", "")
              and "--vm demo who" in r.get("err", ""), r)
        r = zl(s, "list")
        check("a listing of every computer gets no note", r.get("rc") == 0 and "note:" not in r.get("err", ""), r)
        r = zl(s, "each", "main", "--", "stop")
        check("`each main -- stop` names the main VM: it goes on there",
              REFUSED_WORDS not in r.get("out", "") + r.get("err", "") and "no VM 'mainvm'" in r.get("out", "") + r.get("err", ""), r)

        s.ask(op="stop", name="b")
        check("the bridge's record goes when it ends", not records_of(s.pid, "mcp"))
        r = zl(s, "stop")
        check("with no bridge and nothing held, a command that names no computer goes on as before",
              REFUSED_WORDS not in r.get("err", "") and "no VM 'mainvm'" in r.get("err", ""), r)
        s.close()

        # A default changed after the session started: its tools keep the old one, its commands would follow the new.
        s = Session(fake, env)
        sessions.append(s)
        s.start_bridge("b", [installed, "mcp-lazy"])
        rec = wait_for(lambda: records_of(s.pid, "mcp"))
        check("a bridge started with no default computer is on the main VM", rec and rec[0]["computer"]["vm"] == "mainvm", rec)
        r = s.run([installed, "default", "demo"])
        r2 = zl(s, "stop")
        check("after `metacomputer default demo`, a command that names none (now demo) refuses: the session's tools "
              "act on mainvm", r.get("rc") == 0 and r2.get("rc") == 1 and "MCP tools act on mainvm" in r2.get("err", "")
              and "would act on demo (your default computer, config/default-vm)" in r2.get("err", ""), (r, r2))
        s.run([installed, "default", "mainvm"])
        s.close()

        # No MCP: a claim the session holds (as `metacomputer --vm demo sh` leaves) decides.
        s = Session(fake, env)
        sessions.append(s)
        henv = json.loads(b.run("--vm", "demo", "hub", "env", "--json").stdout)
        henv.pop("MCOMP_SESSIONS_DIR", None)          # the session's own folder, not the copy's
        r = s.run([PY, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import owners; owners.claim_for_shell('sh')",
                   os.path.join(co, "lib")], **henv)
        rec = records_of(s.pid, "claim")
        check("a claim on demo (metacomputer sh's, held by the session) leaves its record", r.get("rc") == 0 and rec
              and rec[0]["computer"]["vm"] == "demo", (r, rec))
        r = zl(s, "stop")
        check("...and a command that names no computer (mainvm) refuses, saying where the session acted",
              r.get("rc") == 1 and "holds demo (it acted there: sh)" in r.get("err", ""), r)
        r = zl(s, "--vm", "demo", "stop")
        check("...while one that names demo goes on", REFUSED_WORDS not in r.get("err", "") and "no VM 'demo'" in r.get("err", ""), r)
        for f in os.listdir(os.path.join(henv["MCOMP_RUN_DIR"], "owners")):
            if f.endswith(".json"):
                os.remove(os.path.join(henv["MCOMP_RUN_DIR"], "owners", f))
        r = zl(s, "stop")
        check("once the claim is gone (released), the command goes on and the record is removed",
              REFUSED_WORDS not in r.get("err", "") and not records_of(s.pid, "claim"), r)
        s.close()
    finally:
        for s in sessions:
            s.close()
        for v in ("mainvm", "demo"):
            b.run("--vm", v, "hub", "stop")
        b.close()


def main():
    try:
        classification_checks()
        record_checks()
        fake = userterm.fake_agent("codex")
        if fake:
            end_to_end(fake)
        else:
            check("a C compiler to build the stand-in agent session (cc)", False, "no /usr/bin/cc")
    finally:
        shutil.rmtree(TD, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
