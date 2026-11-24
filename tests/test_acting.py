#!/usr/bin/env python3
"""Every entry that changes a computer waits while the user has it (a handoff), however it's run. No VM, no network.

The table is tests/acting.py: every program tests/entrypoints.py finds, each of its subcommands (its valid lines, and
one for each subcommand or form its parser declares), and every command of bin/metacomputer's dispatch, with what it
does to a computer. One that isn't classified fails here, so a new program or subcommand must be.

Then, on a copy of the checkout (tests/test_help.py's Lab: its own HOME, a stand-in for every program that reaches a
VM, the runner, a provider, the vault or the network, and Python's sockets recorded), with a handoff record open in the
right computer's RUN_DIR, each valid line of each entry that changes a computer is run by itself:
  - plainly (no terminal: as an agent runs it): exit 4, saying the user has the computer, with nothing called (the
    runner's lookups aside), connected or written;
  - at the user's own terminal (tests/userterm.py): past the gate (it says it goes ahead);
  - under a valid hold handed on by its caller (this VM's dispatch lock, held shared on an inherited descriptor, as
    bin/metacomputer's 6 and a build's 5): past the gate.
The input resets, the user's own commands (cloud teardown) and bin/metacomputer's acting commands are run the same way,
by their rules. And bin/metacomputer's and the scripts' session guards fail closed: a check that crashes refuses.

    python3 tests/test_acting.py
"""
import concurrent.futures
import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)
import acting  # noqa: E402
import entrypoints  # noqa: E402
import userterm  # noqa: E402
from test_help import FORMS, H, Lab, dispatch_commands, lookups_only, warm  # noqa: E402
from test_multi_vm import PY, write  # noqa: E402

FAILS = []
COUNT = [0]
WAITS = "the user has the computer"                 # owners.handoff_refusal's words (and owners_check's)
GOES = "this runs at your own terminal: going ahead"
# Where a caller's hold is handed on: bin/metacomputer's descriptor 6 (to the programs it runs, and to a metacomputer
# command run under it), a script's 5 (scripts/lib.sh script_handoff_hold).
HELD_FD_BIN, HELD_FD_SCRIPT = 6, 5
# The copy's runner: its computers exist and run (so a command gets as far as its handoff check), every call recorded;
# anything but a lookup is refused (97).
RUNNER = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ.get("MCOMP_HELP_CALLS") or os.devnull, "a") as f:
    f.write("standin mcomp-vm " + " ".join(a) + "\n")
vms = [{"name": n, "status": s, "locationName": "home", "macAddress": "aa:bb:cc:dd:ee:01"}
       for n, s in (("helpvm", "running"), ("helpvm-baseline", "stopped"), ("helpvm-ckpt-ck", "stopped"),
                    ("other", "running"), ("other-baseline", "stopped"), ("bnd", "running"))]
if a[:2] == ["config", "get"]:
    st = os.environ["FAKE_STORAGE"]
    print(f"Default VM storage: home ({st})\n\nConfigured VM storage locations:\n  - home: {st} (default)\n")
elif a[:1] == ["ls"]:
    print(json.dumps(vms))
elif a[:1] == ["get"]:
    d = [v for v in vms if v["name"] == (a[1] if len(a) > 1 else "")]
    if not d:
        sys.exit(1)
    print(json.dumps(d))
else:
    sys.exit(97)
"""
# ssh: every call recorded; it answers a probe (`... true`, how a command looks whether the guest answers) and refuses
# everything else (97), so a command gets as far as its handoff check and no further.
SSH = """#!/bin/sh
printf '%s\\n' "standin $(basename "$0") $*" >> "${MCOMP_HELP_CALLS:-/dev/null}"
for a in "$@"; do last="$a"; done
[ "$last" = true ] && exit 0
exit 97
"""


def make_lab(td, tag):
    """tests/test_help.py's Lab, with this test's runner and ssh, warmed (every command's own folders made first,
    each spelling of its socket folder too); None when the copy can't run a plain command."""
    lab = Lab(td, tag)
    write(os.path.join(lab.box.home, ".local", "bin", "mcomp-vm"), RUNNER.replace("/usr/bin/env python3", PY), 0o755)
    write(os.path.join(lab.standins, "ssh"), SSH, 0o755)
    if warm(lab):
        lab.close()
        return None
    for d in lab.watched():
        if d.startswith("/tmp/mcomp-"):
            os.makedirs(d, mode=0o700, exist_ok=True)
    return lab


def acting_calls(called):
    """The calls that act: anything but the runner's lookups and an ssh probe (`... true`)."""
    return [c for c in lookups_only(called) if not (c.split()[:2] == ["standin", "ssh"] and c.split()[-1:] == ["true"])]


# Runs a command with an alarm that ends it after N seconds (kept across exec): a line past the gate goes on with the
# stand-ins, and must not outlive the test.
ALARM = "import os, signal, sys; signal.alarm(int(sys.argv[1])); os.execv(sys.argv[2], sys.argv[2:])"
# The same, with a descriptor handed on as number N (moved there in the child: the test's threads share theirs).
ALARM_HELD = ("import os, signal, sys; signal.alarm(int(sys.argv[1])); fd, n = int(sys.argv[2]), int(sys.argv[3]); "
              "os.dup2(fd, n); fd != n and os.close(fd); os.execv(sys.argv[4], sys.argv[4:])")


def check(name, cond, detail=""):
    COUNT[0] += 1
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[-700:]})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


# --- the table ----------------------------------------------------------------------------------------------------
def all_rows():
    """{program: [valid line...]}: its callers' lines and one for each subcommand or form its parser declares."""
    out = {}
    for key, sp in sorted(entrypoints.PROGRAMS.items()):
        if sp == "FORMS":
            continue
        rows = [list(r) for r in sp["rows"]]
        if sp["synth"]:
            r = subprocess.run([PY, "-B", os.path.join(LAB, key.split("#")[0])],
                               env=dict(os.environ, MCOMP_PARSE_ONLY="describe"), capture_output=True, text=True,
                               timeout=60, stdin=subprocess.DEVNULL, cwd=LAB)
            line = next((ln for ln in r.stdout.splitlines() if ln.startswith("parse-only-describe: ")), None)
            check(f"{key}: its declaration describes itself (each subcommand gets a line to classify)", line is not None,
                  (r.returncode, r.stderr[-200:]))
            if line:
                rows += [x for x in entrypoints.synth_rows(json.loads(line.split(": ", 1)[1])) if x not in rows]
        out[key] = rows
    return out


def table_checks(rows):
    print("\nthe table (tests/acting.py): every program, subcommand and command classified")
    found = entrypoints.entry_points()
    progs = {k.split("#")[0] for k in acting.PROGRAMS}
    check(f"every program found in the tree ({len(found)}) is in the table, and the table names no other",
          found == progs and set(acting.PROGRAMS) == set(entrypoints.PROGRAMS),
          (sorted(found - progs), sorted(progs - found), sorted(set(acting.PROGRAMS) ^ set(entrypoints.PROGRAMS))))
    bad = [(k, r) for k, rs in rows.items() for r in rs if acting.classify(k, r) not in acting.LABELS]
    check(f"each of their {sum(len(r) for r in rows.values())} valid lines (every declared subcommand among them) has "
          "a label", not bad, bad[:8])
    cmds = dispatch_commands()
    missing = sorted(c for c in cmds if c not in acting.BIN)
    check(f"every command of bin/metacomputer's dispatch ({len(cmds)}) is classified", not missing, missing)
    check("...and the table names no command the dispatch doesn't have", not (set(acting.BIN) - cmds),
          sorted(set(acting.BIN) - cmds))
    bad = [(c, a) for _vm, c, a in FORMS if acting.classify_bin(c, [x for x in a if x != H]) not in acting.LABELS]
    check("each of bin/metacomputer's forms (tests/test_help.py FORMS) has a label", not bad, bad[:6])
    # Every SHARED line has a fixture of its own here (it waits for computers its line doesn't name).
    shared = {(k, acting.subcommand(k, r)) for k, rs in rows.items() for r in rs if acting.classify(k, r) == acting.SHARED}
    shared |= {("bin/metacomputer", c) for _vm, c, a in FORMS
               if acting.classify_bin(c, [x for x in a if x != H]) == acting.SHARED}
    check("every SHARED line (what several computers use) has its fixture here (SHARED_FIXTURES)",
          shared == set(SHARED_FIXTURES), (sorted(shared - set(SHARED_FIXTURES)), sorted(set(SHARED_FIXTURES) - shared)))
    # The programs' own lists of what changes a computer say what the table says (lib/remote.py, lib/storage.py):
    # imported here with a throwaway HOME and no runner, as tests/test_help.py imports them (nothing of this Mac's).
    sys.path.insert(0, os.path.join(LAB, "lib"))
    disagree, unparsed = [], []
    old_env = dict(os.environ)
    try:
        os.environ["HOME"] = tempfile.mkdtemp(prefix="mcact-parse-")
        for k in ("LUME", "MCOMP_CONFIG", "MCOMP_VM", "MCOMP_RUN_DIR"):
            os.environ.pop(k, None)
        for prog, mod in (("lib/remote.py", "remote"), ("lib/storage.py", "storage")):
            m = __import__(mod)
            if not hasattr(m, "changes_computer"):
                disagree.append((prog, "has no changes_computer()"))
                continue
            for row in rows[prog]:
                ns = _parse(m, row)
                if ns is None:
                    unparsed.append((prog, row))
                    continue
                said = m.changes_computer(ns)
                if said != (acting.classify(prog, row) == acting.ACTS):
                    disagree.append((prog, row, said))
    finally:
        shutil.rmtree(os.environ["HOME"], ignore_errors=True)
        os.environ.clear()
        os.environ.update(old_env)
    check("lib/remote.py's and lib/storage.py's own changes_computer() agree with the table on every line",
          not disagree and not unparsed, (disagree[:6], unparsed[:4]))


def _parse(mod, row):
    """The namespace MOD's parser makes of ROW, or None (its main(), stopped right after parsing)."""
    import argparse
    import contextlib
    import io
    got = []
    real = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        ns = real(self, args, namespace)
        got.append(ns)
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = grab
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                (mod.run if hasattr(mod, "run") else mod.main)(list(row))
            except SystemExit:
                pass
    finally:
        argparse.ArgumentParser.parse_args = real
    return got[0] if got else None


# --- running a line by itself -------------------------------------------------------------------------------------
def script_mark_checks(td):
    """script_handoff_hold's mark (MC_SCRIPT_HELD) is the script's own, never the environment's."""
    print("\nthe scripts' own hold mark isn't taken from the environment")
    lab = make_lab(td, "mark")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        run_dir = os.path.join(lab.box.co, ".run")
        handoff_on(run_dir)
        rc, out, err, called, _ = run_line(lab, ["/bin/bash", os.path.join(lab.box.co, "scripts", "build-vm.sh")],
                                           {"MC_SCRIPT_HELD": "1"}, "plain", run_dir)
        acted = acting_calls(called)
        check("scripts/build-vm.sh with MC_SCRIPT_HELD=1 in its environment while the handoff is open: waits (exit 4), "
              "nothing called", rc == 4 and not acted, (rc, acted[:3], err[-300:]))
    finally:
        lab.close()


def handoff_on(run_dir):
    """An open handoff record in RUN_DIR (as lib/handoff.py writes one), and its dispatch lock file."""
    d = os.path.join(run_dir, "owners")
    os.makedirs(d, exist_ok=True)
    now = time.time()
    rec = {"v": 1, "id": "h-%012x" % (int(now * 1000) & 0xFFFFFFFFFFFF), "status": "open", "why": "captcha",
           "say": "x", "opened": now, "deadline": now + 1800, "timeout_s": 1800, "cwd": run_dir, "claude_pid": None}
    write(os.path.join(d, "handoff.state"), json.dumps(rec), 0o600)
    p = os.path.join(d, ".dispatch")
    if not os.path.exists(p):
        write(p, "", 0o600)
    return p


def where(lab, key, row):
    """(extra environment, RUN_DIR) of the computer a line of KEY acts on, in the copy."""
    co = lab.box.co
    if key == "lib/remote.py" and "--vm" in row:
        return {}, os.path.join(co, ".run-vms", row[row.index("--vm") + 1])
    if key == "scripts/remote-build.sh":
        return {"MCOMP_VM": "far"}, os.path.join(co, ".run-vms", "far")
    if key == "scripts/build-vm.sh":
        return {}, os.path.join(co, ".run")
    return {"MCOMP_RUN_DIR": os.path.join(co, ".run")}, os.path.join(co, ".run")


def argv_of(lab, key, row):
    sp = entrypoints.PROGRAMS[key]
    path = os.path.join(lab.box.co, key.split("#")[0])
    fill = [a.replace("{BOX}", lab.box.root).replace("{CO}", lab.box.co) for a in row]
    return ([PY, "-B", path] if sp["run"] == "py" else ["/bin/bash", path]) + fill


def run_line(lab, argv, extra, mode, run_dir, secs=25, fd=HELD_FD_BIN):
    """ARGV in the copy, as MODE: "plain" (no terminal), "user" (the user's own terminal), "held" (under a hold on
    RUN_DIR's dispatch lock handed on as descriptor FD) or "unlocked" (a descriptor on that lock file handed on as FD
    that never took the lock, while this test holds it shared on another: someone holds it, not that descriptor).
    (rc, out, err, calls, written)."""
    calls = os.path.join(lab.calls_dir, f"acting-{lab.n}.log")
    lab.n += 1
    env = lab.env(calls)
    env.pop("MCOMP_PARSE_ONLY", None)
    env.update(extra)
    before = lab.snapshot()
    wrapped = [PY, "-c", ALARM, str(secs)] + argv
    rc, out, err = None, "", ""
    if mode == "user":
        m, s = pty.openpty()
        try:
            p = userterm.spawn(wrapped, env=env, cwd=lab.box.co, stdin=s, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.close(s)
            try:
                o, e = p.communicate(timeout=secs + 30)
                rc, out, err = p.returncode, o.decode("utf-8", "replace"), e.decode("utf-8", "replace")
            except subprocess.TimeoutExpired as x:
                rc, out, err = "timeout", (x.output or b"").decode("utf-8", "replace"), (x.stderr or b"").decode("utf-8", "replace")
        finally:
            os.close(m)
    else:
        fds, hold, other = (), None, None
        if mode in ("held", "unlocked"):
            import fcntl
            hold = os.open(os.path.join(run_dir, "owners", ".dispatch"), os.O_RDWR)
            if mode == "held":
                fcntl.flock(hold, fcntl.LOCK_SH)
            else:
                other = os.open(os.path.join(run_dir, "owners", ".dispatch"), os.O_RDWR)
                fcntl.flock(other, fcntl.LOCK_SH)
            fds = (hold,)
            wrapped = [PY, "-c", ALARM_HELD, str(secs), str(hold), str(fd)] + argv
        try:
            r = subprocess.run(wrapped, env=env, cwd=lab.box.co, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=secs + 30, start_new_session=True, pass_fds=fds)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as x:
            rc = "timeout"
            out = x.stdout.decode("utf-8", "replace") if isinstance(x.stdout, bytes) else (x.stdout or "")
            err = x.stderr.decode("utf-8", "replace") if isinstance(x.stderr, bytes) else (x.stderr or "")
        finally:
            for x in (hold, other):
                if x is not None:
                    os.close(x)
    after = lab.snapshot()
    called = open(calls).read().splitlines() if os.path.exists(calls) else []
    written = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p)
                     and "__pycache__" not in p and not os.path.isdir(os.path.join(p, "__pycache__")))
    return rc, out, err, called, written


def entry_checks(td, rows):
    """Every valid line of every entry that changes a computer by itself, run by itself during a handoff."""
    print("\nthe programs that change a computer, run by themselves while its handoff is open")
    lines = [(k, r, acting.classify(k, r)) for k, rs in sorted(rows.items()) for r in rs]
    todo = [(k, r, lab) for k, r, lab in lines if lab in (acting.ACTS, acting.RESET, acting.USER)]
    check("there are lines to run of each such label (the table isn't empty)",
          {x[2] for x in todo} == {acting.ACTS, acting.RESET, acting.USER}, sorted({x[2] for x in todo}))
    workers = max(2, min(6, os.cpu_count() or 4))
    parts = [todo[i::workers] for i in range(workers)]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for f in [ex.submit(run_entries, td, p, f"e{i}") for i, p in enumerate(parts)]:
            results.extend(f.result())
    for key, row, label, res in results:
        words = f"{key} {' '.join(row)}".strip()
        if res is None:
            check(f"the copy runs a plain command first ({words})", False, "warm-up failed")
            continue
        plain, user, held = res
        rc, out, err, called, written = plain
        acted = acting_calls(called)
        if rc == 2 and not acted and not written:
            # A line made from the declaration that isn't whole (`save x`, with no command after --): refused as a
            # usage error, before anything, which is all it does.
            check(f"{words}: not a whole line (a usage error): refused before anything is called or written", True)
            continue
        if label == acting.ACTS:
            check(f"{words}: while the user has the computer, by itself: exit 4, saying so; nothing called, connected or "
                  "written", rc == 4 and WAITS in err and not acted and not written, (rc, acted[:3], written[:3], err[-300:]))
            rc, out, err, called, _ = user
            check(f"{words}: ...at the user's own terminal it goes past the gate", GOES in err and rc not in (4,),
                  (rc, err[-300:]))
            held, unlocked = held
            n = HELD_FD_SCRIPT if key.startswith("scripts/") else HELD_FD_BIN
            rc, out, err, called, _ = held
            check(f"{words}: ...under its caller's hold (descriptor {n} on this VM's dispatch lock, as its callers hand it "
                  "on) it goes past the gate", rc != 4 and WAITS not in err and WAITS not in out, (rc, err[-300:]))
            rc, out, err, called, written = unlocked
            check(f"{words}: ...but a descriptor {n} on that lock file that never took it, while someone else holds it, "
                  "is no hold: exit 4, nothing called or written", rc == 4 and WAITS in err and not acting_calls(called)
                  and not written, (rc, acting_calls(called)[:3], written[:3], err[-300:]))
        elif label == acting.RESET:
            check(f"{words}: an input reset while the user has the computer, by itself: nothing done (exit 4, or view's "
                  "silent nothing); nothing called or written", rc in (0, 4) and not acted and not written
                  and (rc == 0 or WAITS in err), (rc, acted[:3], written[:3], err[-300:]))
            rc, out, err, called, _ = user
            # (reset says it goes ahead; view, which does nothing and says nothing for an agent during a handoff, goes
            # on to its in-flight check or the reset itself, and says what came of it either way)
            past = GOES in err or "virtual input devices" in (out + err)
            check(f"{words}: ...at the user's own terminal it goes past the handoff check", past and rc != 4,
                  (rc, out[-200:], err[-300:]))
        else:   # USER
            check(f"{words}: without the user's own terminal: refused, nothing called, connected or written",
                  rc == 3 and "own terminal" in err and not acted and not written, (rc, acted[:3], written[:3], err[-300:]))
            rc, out, err, called, _ = user
            check(f"{words}: ...at the user's own terminal it goes past that check", rc != 3 and "yours to run" not in err,
                  (rc, err[-300:]))


def run_entries(td, items, tag):
    lab = make_lab(td, tag)
    if lab is None:
        return [(k, r, label, None) for k, r, label in items]
    out = []
    try:
        for key, row, label in items:
            extra, run_dir = where(lab, key, row)
            handoff_on(run_dir)
            argv = argv_of(lab, key, row)
            plain = run_line(lab, argv, extra, "plain", run_dir)
            user = run_line(lab, argv, extra, "user", run_dir)
            handoff_on(run_dir)     # (a line past the gate may have changed what's there: open again)
            held = None
            if label == acting.ACTS:
                n = HELD_FD_SCRIPT if key.startswith("scripts/") else HELD_FD_BIN
                held = (run_line(lab, argv, extra, "held", run_dir, fd=n),)
                handoff_on(run_dir)
                held += (run_line(lab, argv, extra, "unlocked", run_dir, fd=n),)
            out.append((key, row, label, (plain, user, held)))
    finally:
        lab.close()
    return out


# --- bin/metacomputer's commands -----------------------------------------------------------------------------------
def bin_checks(td):
    print("\nbin/metacomputer's commands that change a computer, while its handoff is open")
    items = []
    seen = set()
    for vm, cmd, args in FORMS:
        a = [x for x in args if x != H]
        label = acting.classify_bin(cmd, a, remote=vm[1:2] in (["far"], ["spr"]))
        if label not in (acting.ACTS, acting.LIFECYCLE, acting.USER) or (tuple(vm), cmd, tuple(a)) in seen:
            continue
        seen.add((tuple(vm), cmd, tuple(a)))
        items.append((tuple(vm), cmd, tuple(a), label))
    workers = max(2, min(6, os.cpu_count() or 4))
    parts = [items[i::workers] for i in range(workers)]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for f in [ex.submit(run_bin, td, p, f"b{i}") for i, p in enumerate(parts)]:
            results.extend(f.result())
    for vm, cmd, a, label, res in results:
        words = " ".join(["metacomputer", *vm, cmd, *a])
        if res is None:
            check(f"the copy runs a plain command first ({words})", False, "warm-up failed")
            continue
        plain, user, held = res
        rc, out, err, called, _ = plain
        acted = acting_calls(called)
        if "--override" in a:
            # The user's say-so (SECURITY.md): it goes ahead, still holding the lock for what it runs.
            check(f"{words}: --override goes ahead while the user has the computer, as for another session",
                  rc != 4 and WAITS not in err, (rc, err[-300:]))
            continue
        why = incomplete(plain)
        if why:
            # A line that isn't whole here (a word missing, a question with no terminal to ask on, a feature that
            # computer doesn't have): refused before anything, which is all it does.
            check(f"{words}: not a whole line here ({why}): refused before anything is called", not acted,
                  (rc, acted[:3], err[-300:]))
            continue
        if label == acting.USER:
            check(f"{words}: without the user's own terminal: refused before anything is called",
                  rc not in (0, "timeout") and "own terminal" in err and not acted, (rc, acted[:3], err[-300:]))
            continue
        check(f"{words}: while the user has the computer: exit 4, saying so, before anything but a lookup is called",
              rc == 4 and WAITS in err and not acted, (rc, acted[:3], err[-300:]))
        if label != acting.ACTS:
            continue        # LIFECYCLE: even at the user's own terminal (their --override goes ahead): test_handoff's
        rc, out, err, called, _ = user
        check(f"{words}: ...at the user's own terminal it goes past the gate", GOES in err and rc != 4, (rc, err[-300:]))
        held, unlocked = held
        rc, out, err, called, _ = held
        check(f"{words}: ...under its caller's hold (descriptor 6, as a metacomputer command run under another's gets "
              "it) it goes past the gate", rc != 4 and WAITS not in err, (rc, err[-300:]))
        rc, out, err, called, _ = unlocked
        check(f"{words}: ...but a descriptor 6 on that lock file that never took it, while someone else holds it, is no "
              "hold: exit 4, nothing called", rc == 4 and WAITS in err and not acting_calls(called),
              (rc, acting_calls(called)[:3], err[-300:]))


def incomplete(res):
    """Why a run of a line refused it before its handoff check had anything to do (a usage error, a question with no
    terminal to ask on, what that computer doesn't have), or None."""
    rc, out, err, called, _ = res
    low = (out + err).lower()
    if rc == 2 or "usage:" in low:
        return "a usage error"
    if "refusing without a terminal" in low or "needs its name typed" in low:
        return "a question, and no terminal to ask on"
    if "isn't available for" in low:
        return "that computer doesn't have it"
    return None


def bin_run_dir(lab, vm):
    return os.path.join(lab.box.co, ".run-vms", vm[1]) if vm else os.path.join(lab.box.co, ".run")


def run_bin(td, items, tag):
    lab = make_lab(td, tag)
    if lab is None:
        return [(vm, c, a, label, None) for vm, c, a, label in items]
    out = []
    try:
        for vm, cmd, a, label in items:
            run_dir = bin_run_dir(lab, vm)
            if cmd == "vm" and a[:1] == ("delete",) and len(a) > 1:
                run_dir = bin_run_dir(lab, ("--vm", a[1]))     # vm delete NAME: that computer's handoff
            handoff_on(run_dir)
            argv = [lab.box.mc, *vm, cmd, *a]
            plain = run_line(lab, argv, {}, "plain", run_dir)
            user = held = None
            if label == acting.ACTS and not incomplete(plain):
                user = run_line(lab, argv, {}, "user", run_dir, secs=20)
                handoff_on(run_dir)
                held = (run_line(lab, argv, {}, "held", run_dir, secs=20, fd=HELD_FD_BIN),)
                handoff_on(run_dir)
                held += (run_line(lab, argv, {}, "unlocked", run_dir, secs=20, fd=HELD_FD_BIN),)
            out.append((vm, cmd, a, label, (plain, user, held)))
    finally:
        lab.close()
    return out


# --- the computer a program acts on is the one whose handoff it checks ----------------------------------------------
# The SHARED lines and the fixture that runs each (volume_checks, cloud_setup_checks; bin/metacomputer hands them on).
SHARED_FIXTURES = {("lib/storage.py", "volume delete"): "volume_checks", ("lib/remote.py", "cloud"): "cloud_setup_checks",
                   ("bin/metacomputer", "volume"): "volume_checks (storage.py)",
                   ("bin/metacomputer", "cloud"): "cloud_setup_checks (remote.py)"}


def storage_state(lab):
    return os.path.join(lab.box.root, "storage")        # MCOMP_STORAGE_HOME (tests/test_multi_vm.py Box.env)


def lstart(pid):
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def binding_checks(td):
    """lib/storage.py and lib/guest_put.py look at the handoff of the computer they act on (codex re-check, P1-2): its
    name and its run folder, or the SSH folder its transport names, and never the default folder for another."""
    print("\nthe direct helpers check the handoff of the computer they act on")
    lab = make_lab(td, "bind")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        co, st = lab.box.co, storage_state(lab)
        other_run, main_run = os.path.join(co, ".run-vms", "other"), os.path.join(co, ".run")
        man = os.path.join(st, "checkpoints", "other", "saved.json")
        write(man, json.dumps({"volumes": []}), 0o600)
        handoff_on(other_run)
        handoff_on(main_run)
        os.remove(os.path.join(main_run, "owners", "handoff.state"))     # (only other has one)
        sp = [PY, "-B", os.path.join(co, "lib", "storage.py"), "checkpoint", "delete", "saved"]
        for extra, how in (({"MCOMP_VM_NAME": "other"}, "named, no run folder given"),
                           ({"MCOMP_VM": "other"}, "chosen by MCOMP_VM, no run folder given"),
                           ({"MCOMP_VM_NAME": "other", "MCOMP_RUN_DIR": main_run}, "named, with the main VM's run folder")):
            rc, out, err, called, written = run_line(lab, sp, extra, "plain", other_run)
            check(f"storage.py checkpoint delete saved for VM other ({how}), while other has a handoff open and the main "
                  "VM doesn't: exit 4, its checkpoint kept", rc == 4 and os.path.exists(man) and not acting_calls(called),
                  (rc, os.path.exists(man), err[-300:]))
        os.remove(os.path.join(other_run, "owners", "handoff.state"))
        rc, out, err, called, written = run_line(lab, sp, {"MCOMP_VM_NAME": "other"}, "plain", other_run)
        check("...and with no handoff open there, the same deletes other's checkpoint (it acts on other's)",
              rc == 0 and not os.path.exists(man), (rc, os.path.exists(man), err[-300:]))
        handoff_on(other_run)
        kh = os.path.join(lab.box.home, ".ssh", "metacomputer-other", "known_hosts")
        put = [PY, "-B", os.path.join(co, "lib", "guest_put.py"), "--transport", "4", "ssh", "-o",
               f"UserKnownHostsFile={kh}", "-T", os.path.join(co, "install.sh")]
        for extra, how in (({}, "no run folder given"), ({"MCOMP_RUN_DIR": main_run}, "with the main VM's run folder")):
            rc, out, err, called, written = run_line(lab, put, extra, "plain", other_run)
            check(f"guest_put.py with a transport to VM other ({how}), while other has a handoff open: exit 4, nothing "
                  "called", rc == 4 and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
        # A transport that names none of this checkout's computers is checked at the run folder it's given (where such
        # a command goes can't be told here: the same-account limit, SECURITY.md).
        bare = [PY, "-B", os.path.join(co, "lib", "guest_put.py"), "--transport", "2", "ssh", "-T", os.path.join(co, "install.sh")]
        handoff_on(main_run)
        rc, out, err, called, written = run_line(lab, bare, {"MCOMP_RUN_DIR": main_run}, "plain", main_run)
        check("guest_put.py with a transport that names no computer is checked at the run folder it's given: with a "
              "handoff open there, exit 4, nothing called", rc == 4 and WAITS in err and not acting_calls(called),
              (rc, acting_calls(called)[:3], err[-300:]))
        # storage.py attach reaches the guest through the transport it's given: a transport to other is checked at
        # other (codex re-check 2, #1), whichever VM it was run for.
        os.remove(os.path.join(main_run, "owners", "handoff.state"))
        write(os.path.join(main_run, "storage-plan.json"), json.dumps({"items": []}))
        att = [PY, "-B", os.path.join(co, "lib", "storage.py"), "attach", "--transport", "4", "ssh", "-o",
               f"UserKnownHostsFile={kh}", "-T"]
        rc, out, err, called, written = run_line(lab, att, {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
        check("storage.py attach run for the main VM with a transport to VM other, while other has a handoff open: "
              "refused (exit 4), nothing called", rc == 4 and not acting_calls(called),
              (rc, acting_calls(called)[:3], err[-300:]))
        rc, out, err, called, written = run_line(lab, att, {"MCOMP_RUN_DIR": other_run, "MCOMP_VM_NAME": "other"},
                                                 "plain", other_run)
        check("...and run for other itself, its handoff open: exit 4, saying the user has it, nothing called",
              rc == 4 and WAITS in err and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
    finally:
        lab.close()


def volume_checks(td):
    """A volume sync waits for every computer that has the volume attached (P1-3), and a volume delete for every
    computer whose checkpoints keep it (P2-4), not only the one the command was run for."""
    print("\nwhat a volume's sync or delete changes: every computer that uses it")
    lab = make_lab(td, "vol")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    keeper = subprocess.Popen(["/bin/sleep", "600"])
    try:
        co, st = lab.box.co, storage_state(lab)
        other_run, main_run = os.path.join(co, ".run-vms", "other"), os.path.join(co, ".run")
        copy = os.path.join(lab.box.root, "s3copy")
        os.makedirs(copy, exist_ok=True)
        write(os.path.join(st, "registry", "volumes", "shared.json"),
              json.dumps({"name": "shared", "kind": "s3", "path": copy, "bucket": "b", "endpoint": "https://example.com"}))
        write(os.path.join(st, "locks", "s3-test-shared.json"),
              json.dumps({"kind": "s3", "name": "shared", "path": copy, "vm": "other", "lab": os.path.realpath(co),
                          "pid": keeper.pid, "pstart": lstart(keeper.pid), "since": "now", "state": "running"}), 0o600)
        handoff_on(other_run)
        handoff_on(main_run)
        os.remove(os.path.join(main_run, "owners", "handoff.state"))
        sync = [PY, "-B", os.path.join(co, "lib", "storage.py"), "volume", "sync", "shared"]
        rc, out, err, called, written = run_line(lab, sync, {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
        check("volume sync shared run for the main VM, while VM other has it attached and a handoff open: exit 4, nothing "
              "called (no keys read, no sync)", rc == 4 and WAITS in err and not acting_calls(called),
              (rc, acting_calls(called)[:3], err[-300:]))
        rec_shared = os.path.join(st, "locks", "s3-test-shared.json")
        os.remove(rec_shared)
        # (codex re-check 3, #1) other mounts only a folder inside the S3 copy (another volume): it sees what the sync
        # changes too.
        os.makedirs(os.path.join(copy, "docs"), exist_ok=True)
        sub = os.path.join(st, "locks", "folder-test-docs.json")
        write(sub, json.dumps({"kind": "folder", "name": "docs", "path": os.path.join(copy, "docs"), "vm": "other",
                               "lab": os.path.realpath(co), "pid": keeper.pid, "pstart": lstart(keeper.pid),
                               "since": "now", "state": "running", "rw": False, "exclusive": False}), 0o600)
        rc, out, err, called, written = run_line(lab, sync, {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
        check("volume sync shared while VM other has a folder inside its copy attached (another volume) and a handoff "
              "open: exit 4, nothing called", rc == 4 and WAITS in err and not acting_calls(called),
              (rc, acting_calls(called)[:3], err[-300:]))
        os.remove(sub)
        # (#2) a holder record that's there but can't be read, or a locks folder that can't be read: refused, never
        # read as nobody.
        bad = os.path.join(st, "locks", "s3-test-unreadable.json")
        for how in ("unreadable", "not JSON"):
            write(bad, "{" if how == "not JSON" else json.dumps({"vm": "other"}), 0o600)
            if how == "unreadable":
                os.chmod(bad, 0)
            rc, out, err, called, written = run_line(lab, sync, {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
            check(f"volume sync shared with a holder record that's {how}: refused (exit 4), nothing called",
                  rc == 4 and "can't be" in err and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
            os.chmod(bad, 0o600)
            os.remove(bad)
        os.chmod(os.path.join(st, "locks"), 0)
        try:
            rc, out, err, called, written = run_line(lab, sync, {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
        finally:
            os.chmod(os.path.join(st, "locks"), 0o700)
        check("volume sync shared with the storage locks folder unreadable: refused (exit 4), nothing called",
              rc == 4 and "can't be read" in err and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
        # (#3) a computer that attaches it after the gate, while this sync waits for its lock or transfers: read again
        # under the sync's own lock, before the exchange with the copy; refused (exit 4) before anything is written.
        drv = os.path.join(lab.box.root, "late_holder.py")
        write(drv, "import json, os, sys\nsys.path.insert(0, os.path.join(%r, 'lib'))\nimport storage\n"
                   "real = storage.gate_computers\n"
                   "def late(pairs):\n"
                   "    real(pairs)\n"
                   "    rec = dict(kind='s3', name='shared', path=%r, vm='other', lab=os.path.realpath(%r), pid=%d,\n"
                   "               pstart=%r, since='now', state='running')\n"
                   "    open(%r, 'w').write(json.dumps(rec))\n"
                   "storage.gate_computers = late\n"
                   "sys.exit(storage.main(['volume', 'sync', 'shared']))\n"
              % (co, copy, co, keeper.pid, lstart(keeper.pid), rec_shared))
        rc, out, err, called, written = run_line(lab, [PY, "-B", drv], {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
        os.remove(rec_shared)
        check("volume sync shared, VM other attaching it after the gate (its handoff open): exit 4 under the sync's lock, "
              "before the exchange; nothing called (no keys, no sync)", rc == 4 and "attached it meanwhile" in err
              and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
        img, snap = os.path.join(lab.box.root, "dv.img"), os.path.join(lab.box.root, ".dv.img.ckpt-other-ck")
        write(img, "x" * 4096)
        write(snap, "y" * 4096)
        write(os.path.join(st, "registry", "volumes", "dv.json"), json.dumps({"name": "dv", "kind": "disk", "path": img}))
        man = os.path.join(st, "checkpoints", "other", "ck.json")
        write(man, json.dumps({"volumes": [{"name": "dv", "path": img, "snapshot": snap}]}))
        before = open(man).read()
        dele = [PY, "-B", os.path.join(co, "lib", "storage.py"), "volume", "delete", "dv", "--yes"]
        rc, out, err, called, written = run_line(lab, dele, {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
        check("volume delete dv, which VM other's checkpoint keeps, while other has a handoff open: exit 4, the snapshot "
              "and the checkpoint kept", rc == 4 and os.path.exists(snap) and open(man).read() == before
              and os.path.exists(img), (rc, os.path.exists(snap), err[-300:]))
        rc, out, err, called, written = run_line(lab, dele, {"MCOMP_RUN_DIR": main_run}, "user", other_run)
        check("...at the user's own terminal it goes past the gate", GOES in err and rc != 4, (rc, err[-300:]))
    finally:
        keeper.kill()
        keeper.wait()
        lab.close()


def look_once_checks(td):
    """look_once closes every descriptor it has on the dispatch lock file (codex re-check 3, #4), with real flocks: a
    held description handed on twice (two numbers), the long-running program looks once, the caller lets go of its own
    copies, and an exclusive lock from another description must then be had (nothing kept it)."""
    print("\nlook_once: a long-running program keeps no copy of a hold handed on")
    import fcntl
    run = os.path.join(td, "lookonce-run")
    os.makedirs(os.path.join(run, "owners"))
    path = os.path.join(run, "owners", ".dispatch")
    write(path, "", 0o600)
    a = os.open(path, os.O_RDWR)
    fcntl.flock(a, fcntl.LOCK_SH)
    b = os.dup(a)
    code = ("import os, sys, time; sys.path.insert(0, %r); import handoff_gate\n"
            "print(handoff_gate.look_once(%r), flush=True)\ntime.sleep(10)\n" % (os.path.join(LAB, "lib"),
                                                                                    os.path.join(run, "owners")))
    p = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(a, b),
                         env=dict(os.environ, MCOMP_RUN_DIR=run))
    try:
        os.close(a)
        os.close(b)
        how = p.stdout.readline().strip()
        probe = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except BlockingIOError:
            free = False
        finally:
            os.close(probe)
        check("a hold handed on as two descriptors: look_once goes ahead on it (inherited) and keeps neither: once the "
              "caller lets go, an exclusive lock is had while the program still runs", how == "inherited" and free
              and p.poll() is None, (how, free, p.poll()))
    finally:
        p.kill()
        p.wait()


def default_name_checks(td):
    """With no config/defaults.env and no config/local.env, the main computer's name is the one every helper falls back
    on (labconfig.DEFAULT_VM_NAME), so a storage command for it with no run folder given goes ahead when no handoff is
    open (codex re-check 3, #5)."""
    print("\nno settings files: the main computer's default name")
    lab = make_lab(td, "noset")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        co = lab.box.co
        for f in ("defaults.env", "local.env"):
            os.remove(os.path.join(co, "config", f))
        rc, out, err, called, written = run_line(lab, [PY, "-B", os.path.join(co, "lib", "storage.py"), "kill-vm"], {},
                                                 "plain", os.path.join(co, ".run"))
        check("storage.py kill-vm with no settings files and no run folder given, no handoff open: it goes ahead "
              "(the main computer's default name, its .run)", rc == 0 and WAITS not in err and "can't be told" not in err,
              (rc, err[-300:]))
        handoff_on(os.path.join(co, ".run"))
        rc, out, err, called, written = run_line(lab, [PY, "-B", os.path.join(co, "lib", "storage.py"), "kill-vm"], {},
                                                 "plain", os.path.join(co, ".run"))
        check("...and with the main computer's handoff open: exit 4", rc == 4 and WAITS in err, (rc, err[-300:]))
    finally:
        lab.close()


def cloud_setup_checks(td):
    """A provider whose setup and creates rewrite a network its other computers share (lib/cloud/oci.py:
    SHARED_NETWORK_CHANGES) waits for every computer of this checkout's there (P2-6); one whose setup only reads
    doesn't."""
    print("\na provider's shared network: its setup and creates wait for every computer there")
    lab = make_lab(td, "cloud")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        co = lab.box.co
        for n, port in (("oc", 2405), ("oc2", 2407)):
            lab.box.settings(f"config/vms/{n}.env", f"SSH_PORT={port}\nVNC_PORT={port + 3700}\nPLACE=oci\nPROVIDER=oci\n"
                                                     "TRANSPORT=ssh\nEGRESS=internet\n")
        oc_run = os.path.join(co, ".run-vms", "oc")
        handoff_on(oc_run)
        handoff_on(os.path.join(co, ".run-vms", "oc2"))
        os.remove(os.path.join(co, ".run-vms", "oc2", "owners", "handoff.state"))
        rp = os.path.join(co, "lib", "remote.py")
        for args, what in ((["cloud", "setup", "oci"], "cloud setup oci"),
                           (["create", "--vm", "oc2", "--op", "0" * 32], "create of another computer there (oc2)")):
            rc, out, err, called, written = run_line(lab, [PY, "-B", rp, *args], {}, "plain", oc_run)
            check(f"{what}, while computer oc there has a handoff open: exit 4 before the provider is asked anything",
                  rc == 4 and WAITS in err and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
        rc, out, err, called, written = run_line(lab, [PY, "-B", rp, "cloud", "setup", "oci"], {}, "user", oc_run)
        check("...cloud setup oci at the user's own terminal goes past the gate", GOES in err and rc != 4, (rc, err[-300:]))
        rc, out, err, called, written = run_line(lab, [PY, "-B", rp, "cloud", "setup", "hetzner"], {}, "plain", oc_run)
        check("cloud setup hetzner (it only reads: each computer has its own firewall) doesn't wait for oc's handoff",
              rc != 4 and WAITS not in err, (rc, err[-300:]))
    finally:
        lab.close()


def mcp_manifest_checks(td):
    """With MCP_HUB=off and DRIVER_MODE=bounded, `metacomputer mcp` writes the driver's manifest to the guest before
    its connection when the guest hasn't got it (P2-7): that write waits during a handoff (but at the user's own
    terminal); the read that looks for it doesn't."""
    print("\nmetacomputer mcp's manifest, with no hub and a bounded driver")
    lab = make_lab(td, "mcp")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        co = lab.box.co
        lab.box.settings("config/vms/bnd.env", "SSH_PORT=2406\nVNC_PORT=6106\nDRIVER_MODE=bounded\nMCP_HUB=off\n")
        run_dir = os.path.join(co, ".run-vms", "bnd")
        handoff_on(run_dir)
        argv = [lab.box.mc, "--vm", "bnd", "mcp"]
        rc, out, err, called, written = run_line(lab, argv, {}, "plain", run_dir)
        wrote = [c for c in called if c.startswith("standin ssh") and "cat >" in c]
        check("metacomputer --vm bnd mcp while the user has it, the manifest not in the guest: exit 4 before the write",
              rc == 4 and WAITS in err and not wrote and any("test -f" in c for c in called), (rc, wrote[:2], err[-300:]))
        rc, out, err, called, written = run_line(lab, argv, {}, "user", run_dir)
        wrote = [c for c in called if c.startswith("standin ssh") and "cat >" in c]
        check("...at the user's own terminal it goes past the gate and writes it", GOES in err and wrote, (rc, err[-300:]))
    finally:
        lab.close()


# --- the session guards fail closed --------------------------------------------------------------------------------
def session_guard_checks(td):
    print("\nthe session guard fails closed: a check that crashes refuses")
    lab = make_lab(td, "sg")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        # lib/sessionvm.py that crashes (an exception: exit 1), as a broken install or a bug would.
        write(os.path.join(lab.box.co, "lib", "sessionvm.py"), "raise RuntimeError('a crash in the session check')\n")
        for args, what in ((["inspect", "windows"], "a read"), (["put", os.path.join(lab.box.co, "install.sh")], "put"),
                           (["start"], "start")):
            rc, out, err, called, _ = run_line(lab, [lab.box.mc, *args], {}, "plain", os.path.join(lab.box.co, ".run"))
            acted = acting_calls(called)
            check(f"metacomputer {args[0]} ({what}) with a session check that crashes: refused (exit 1), saying the check "
                  "wasn't made; nothing called", rc == 1 and "couldn't check which computer this agent session holds" in err
                  and "going on" not in err and not acted, (rc, acted[:3], err[-300:]))
        rc, out, err, called, _ = run_line(lab, ["/bin/bash", os.path.join(lab.box.co, "scripts", "build-vm.sh")], {},
                                           "plain", os.path.join(lab.box.co, ".run"))
        acted = acting_calls(called)
        check("scripts/build-vm.sh run by itself with a session check that crashes: refused before its handoff check, "
              "saying the check wasn't made; nothing called", rc == 1 and "couldn't check which computer" in err
              and "going on" not in err and not acted, (rc, acted[:3], err[-300:]))
        # One that's missing, or can't be read: Python says so with exit 2, the check's own "couldn't tell" (codex
        # re-check 2, #9). The program is looked at first: refused.
        sv = os.path.join(lab.box.co, "lib", "sessionvm.py")
        for how in ("missing", "unreadable"):
            if how == "missing":
                os.remove(sv)
            else:
                write(sv, "import sys\nsys.exit(0)\n")
                os.chmod(sv, 0)
            rc, out, err, called, _ = run_line(lab, [lab.box.mc, "inspect", "windows"], {}, "plain",
                                               os.path.join(lab.box.co, ".run"))
            acted = acting_calls(called)
            check(f"metacomputer inspect with the session check {how}: refused (exit 1), nothing called",
                  rc == 1 and "session check" in err and not acted, (rc, acted[:3], err[-300:]))
            rc, out, err, called, _ = run_line(lab, ["/bin/bash", os.path.join(lab.box.co, "scripts", "build-vm.sh")],
                                               {}, "plain", os.path.join(lab.box.co, ".run"))
            acted = acting_calls(called)
            check(f"scripts/build-vm.sh with the session check {how}: refused (exit 1) before its handoff check, nothing "
                  "called", rc == 1 and "session check" in err and not acted, (rc, acted[:3], err[-300:]))
        os.chmod(sv, 0o644)
    finally:
        lab.close()


def unreadable_settings_checks(td):
    """Settings that are there but can't be read are never read as none (codex re-check 2, #8): what chooses a handoff to
    check from them refuses (exit 4)."""
    print("\nsettings that can't be read: the gates refuse")
    lab = make_lab(td, "unread")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    vms = os.path.join(lab.box.co, "config", "vms")
    try:
        co = lab.box.co
        lab.box.settings("config/vms/oc.env", "SSH_PORT=2405\nVNC_PORT=6105\nPLACE=oci\nPROVIDER=oci\nTRANSPORT=ssh\n"
                                              "EGRESS=internet\n")
        oc_run = os.path.join(co, ".run-vms", "oc")
        handoff_on(oc_run)
        rp = [PY, "-B", os.path.join(co, "lib", "remote.py"), "cloud", "setup", "oci"]
        kh = os.path.join(lab.box.home, ".ssh", "metacomputer-other", "known_hosts")
        put = [PY, "-B", os.path.join(co, "lib", "guest_put.py"), "--transport", "4", "ssh", "-o", f"UserKnownHostsFile={kh}",
               "-T", os.path.join(co, "install.sh")]
        kill = [PY, "-B", os.path.join(co, "lib", "storage.py"), "kill-vm"]
        for what, target in (("the config/vms folder", vms), ("a config/vms file", os.path.join(vms, "oc.env"))):
            os.chmod(target, 0)
            try:
                rc, out, err, called, written = run_line(lab, rp, {}, "plain", oc_run)
                check(f"cloud setup oci with {what} unreadable: refused (exit 4) before the provider is asked anything",
                      rc == 4 and "can't be" in err and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
                rc, out, err, called, written = run_line(lab, put, {}, "plain", oc_run)
                check(f"guest_put.py with a transport to VM other, {what} unreadable: refused (exit 4), nothing called",
                      rc == 4 and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
                rc, out, err, called, written = run_line(lab, kill, {"MCOMP_VM_NAME": "other"}, "plain", oc_run)
                check(f"storage.py kill-vm for VM other, no run folder given, {what} unreadable: refused (exit 4)",
                      rc == 4 and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
            finally:
                os.chmod(target, 0o755 if os.path.isdir(target) else 0o644)
    finally:
        os.chmod(vms, 0o755)
        lab.close()


def strict_reader_checks(td):
    """labconfig.computers reads settings files of plain assignments only, or raises (codex re-check 4, #1; check r5,
    #1 and #3: a closed grammar, and HOME and LAB_ROOT never assigned): a setting it can't read is never taken as missing
    (a default: another computer's folder)."""
    print("\nthe settings a gate tells computers by: plain assignments only, anything else refused")
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import labconfig
    home = os.path.join(td, "strict-home")
    n = [0]

    def comps(local=None, vm=None, env=None):
        n[0] += 1
        root = os.path.join(td, f"strict-{n[0]}")
        os.makedirs(os.path.join(root, "config", "vms"))
        shutil.copy(os.path.join(LAB, "config", "defaults.env"), os.path.join(root, "config", "defaults.env"))
        if local is not None:
            write(os.path.join(root, "config", "local.env"), local)
        if vm is not None:
            write(os.path.join(root, "config", "vms", "oc.env"), vm)
        try:
            kw = {} if env is None else {"env": env}
            return root, {c["name"]: c for c in reversed(labconfig.computers(root, home, **kw))}, None
        except Exception as e:      # noqa: BLE001  (a raise is the answer here; an older reader's TypeError too)
            return root, None, e

    for line, want in (('export RUN_DIR="$LAB_ROOT/.custom-run"', "{root}/.custom-run"),
                       ("RUN_DIR=\"$HOME/.mc/run\"   # a comment", "{home}/.mc/run"),
                       ("RUN_DIR='/srv/mc run'", "/srv/mc run"),
                       ("RUN_DIR=~/mc-run", "{home}/mc-run"),
                       ('RUN_DIR=""', "{root}/.run"),
                       ('#RUN_DIR="$HOME/elsewhere"', "{root}/.run"),
                       ('RUN_DIR="~/mc-run"', None),
                       ('RUN_DIR="$HOMEX/mc-run"', None),
                       ('RUN_DIR="$OTHER/mc-run"', None),
                       ("RUN_DIR='/srv/Jon'\\''s run'", "/srv/Jon's run")):
        root, cs, e = comps(local="VM_NAME=mainvm\n" + line + "\n")
        exp = want and want.format(root=root, home=home)
        check(f"config/local.env {line!r}: the run folder is {want or 'not told (None: refused where it matters)'}, as "
              "bash reads it", cs is not None and cs["mainvm"]["run_dir"] == exp, (e, cs and cs["mainvm"]["run_dir"]))
    root, cs, e = comps(vm="SSH_PORT=2405\nVNC_PORT=6105\nPLACE=oci\nexport PROVIDER=oci\n")
    check("config/vms/oc.env 'export PROVIDER=oci': oc's provider is oci (it's in the shared-network gate)",
          cs is not None and cs["oc"]["provider"] == "oci", (e, cs and cs["oc"]))
    for line in ("RUN_DIR=$LAB_ROOT/.esc\\ run", 'RUN_DIR="$LAB_ROOT/.esc\\run"', "RUN_DIR+=/x", "declare RUN_DIR=/x",
                 "readonly RUN_DIR=/x", "local RUN_DIR=/x", "RUN_DIR=/x trailing", "RUN_DIR=/x#y", "RUN_DIR=/x; X=1",
                 'source "$LAB_ROOT/config/more.env"', '. "$LAB_ROOT/config/more.env"', "eval \"X=1\"", "set -a",
                 "if true; then\nRUN_DIR=/x\nfi", "cat >/dev/null <<EOF\nRUN_DIR=/x\nEOF", 'X="a\nRUN_DIR=/x\n"',
                 "RUN_DIR=/x\r", 'VM_NAME="$USER"'):
        root, cs, e = comps(local=line + "\n")
        check(f"config/local.env {line!r}: refused (raises), never read as unset", cs is None and e is not None,
              (cs and cs.get("metacomputer"), e))
    # Codex check r5, #1 and #3: a line bash reads across lines or runs (true || / &&, then an assignment it skips; a
    # $var command that reads another file), and an assignment to a variable the values are read with, all refuse: the
    # file is plain assignments only, and anything else on any line refuses.
    for text, what in (("true ||\nRUN_DIR=/unchecked-run\n", "'true ||', then an assignment bash skips"),
                       ("true &&\nRUN_DIR=/unchecked-run\n", "'true &&', then an assignment"),
                       ("loader=source\n$loader \"$LAB_ROOT/config/more.env\"\n", "'loader=source', then '$loader FILE'"),
                       ('HOME=/different-home\nRUN_DIR="$HOME/run"\n', "HOME reassigned, then $HOME used"),
                       ('export HOME=/different-home\nRUN_DIR=~/run\n', "HOME exported anew, then ~ used"),
                       ('LAB_ROOT=/elsewhere\nRUN_DIR="$LAB_ROOT/.run"\n', "LAB_ROOT reassigned, then used"),
                       ("X=1 |\nRUN_DIR=/unchecked-run\n", "a line ending in |"),
                       ("X=1 ;\n", "a line ending in ;"), ("X=1 &\n", "a line ending in &"),
                       ("X=1 \\\nRUN_DIR=/unchecked-run\n", "a line ending in a backslash"),
                       ("f() { RUN_DIR=/x; }\n", "a function"), ("$cmd\n", "a $var command"),
                       ("[ -x /nonexistent ] && PY=/nonexistent\n", "a test"), ("X=$(true)\n", "a command's output"),
                       ("X=a~b\n", "a ~ past a value's start"), ("X=a(b)\n", "a parenthesis in a value"),
                       ("X=a<b\n", "a redirection in a value")):
        root, cs, e = comps(local="VM_NAME=mainvm\n" + text)
        check(f"config/local.env with {what}: refused (raises), never read", cs is None and isinstance(e, ValueError),
              (cs and cs.get("mainvm"), e))
    ex = open(os.path.join(LAB, "config", "local.env.example")).read()
    uncommented = "\n".join(l[1:] if l[:1] == "#" and l[1:2].isupper() and "=" in l else l for l in ex.split("\n"))
    root, cs, e = comps(local=uncommented)
    check("config/defaults.env with config/local.env.example's settings uncommented: read with no refusal",
          cs is not None and e is None, e)
    second = os.path.join(td, "strict-second.env")
    write(second, 'VM_NAME=second\nSSH_DIR="$HOME/.ssh/metacomputer-second"\nRUN_DIR="$HOME/.metacomputer-second/run"\n')
    root, cs, e = comps(local="VM_NAME=mainvm\n", env={"MCOMP_CONFIG": second})
    check("MCOMP_CONFIG=second.env (another file): its computer, read as scripts/lib.sh reads it",
          cs is not None and cs.get("second", {}).get("run_dir") == os.path.join(home, ".metacomputer-second", "run")
          and "mainvm" in cs, (e, cs and sorted(cs)))
    for env, how in (({"MCOMP_CONFIG": second, "MCOMP_VM": "mainvm"}, "with MCOMP_VM naming another computer"),
                     ({"MCOMP_CONFIG": os.path.join(td, "strict-missing.env")}, "naming no file")):
        root, cs, e = comps(local="VM_NAME=mainvm\n", env=env)
        check(f"MCOMP_CONFIG {how}: refused (raises)", cs is None and e is not None and "TypeError" not in repr(e),
              (cs and sorted(cs), e))


def config_file_checks(td):
    """A computer chosen by MCOMP_CONFIG is told from that file (codex re-check 4, #3): config/local.env.example's
    second VM, run directly with MCOMP_CONFIG and nothing else."""
    print("\na computer chosen by MCOMP_CONFIG: its own handoff")
    lab = make_lab(td, "mcfg")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        co, st, home = lab.box.co, storage_state(lab), lab.box.home
        second = os.path.join(lab.box.root, "second.env")
        write(second, 'VM_NAME=second\nSSH_DIR="$HOME/.ssh/metacomputer-second"\n'
                      'RUN_DIR="$HOME/.metacomputer-second/run"\nSSH_PORT=2231\nVNC_PORT=5931\n')
        run2 = os.path.join(home, ".metacomputer-second", "run")
        man = os.path.join(st, "checkpoints", "second", "saved.json")
        sp = [PY, "-B", os.path.join(co, "lib", "storage.py"), "checkpoint", "delete", "saved"]
        write(man, json.dumps({"volumes": []}), 0o600)
        handoff_on(os.path.join(co, ".run"))         # the main computer's handoff, open: not second's
        rc, out, err, called, written = run_line(lab, sp, {"MCOMP_CONFIG": second}, "plain", run2)
        check("MCOMP_CONFIG=second.env storage.py checkpoint delete saved, no handoff open for second (the main "
              "computer's is): it goes ahead, second's checkpoint deleted", rc == 0 and not os.path.exists(man),
              (rc, os.path.exists(man), err[-300:]))
        write(man, json.dumps({"volumes": []}), 0o600)
        handoff_on(run2)
        rc, out, err, called, written = run_line(lab, sp, {"MCOMP_CONFIG": second}, "plain", run2)
        check("...and with second's handoff open: exit 4, saying the user has it, its checkpoint kept",
              rc == 4 and WAITS in err and os.path.exists(man), (rc, os.path.exists(man), err[-300:]))
        kh = os.path.join(home, ".ssh", "metacomputer-second", "known_hosts")
        put = [PY, "-B", os.path.join(co, "lib", "guest_put.py"), "--transport", "4", "ssh", "-o",
               f"UserKnownHostsFile={kh}", "-T", os.path.join(co, "install.sh")]
        rc, out, err, called, written = run_line(lab, put, {"MCOMP_CONFIG": second}, "plain", run2)
        check("MCOMP_CONFIG=second.env guest_put.py with a transport to second, no run folder given, second's handoff "
              "open: exit 4, saying the user has it, nothing called", rc == 4 and WAITS in err
              and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
        rc, out, err, called, written = run_line(lab, sp, {"MCOMP_CONFIG": second, "MCOMP_VM": "other"}, "plain", run2)
        check("MCOMP_CONFIG=second.env with MCOMP_VM=other (different settings, as scripts/lib.sh refuses): refused "
              "(exit 4), its checkpoint kept", rc == 4 and os.path.exists(man) and "choose different settings" in err,
              (rc, err[-300:]))
        rc, out, err, called, written = run_line(lab, [PY, "-B", os.path.join(co, "lib", "storage.py"), "kill-vm"],
                                                 {"MCOMP_CONFIG": os.path.join(co, "config", "local.env")}, "plain",
                                                 os.path.join(co, ".run"))
        check("MCOMP_CONFIG naming config/local.env itself: the main computer (one, not two): with its handoff open, "
              "exit 4 saying the user has it", rc == 4 and WAITS in err, (rc, err[-300:]))
    finally:
        lab.close()


def settings_syntax_checks(td):
    """The programs themselves, with settings bash reads that the old reader didn't (codex re-check 4, #1): `export
    RUN_DIR=...` is that folder, a value with a backslash and a `source` line refuse."""
    print("\nsettings written as bash reads them: the right handoff, or a refusal")
    lab = make_lab(td, "syntax")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    try:
        co = lab.box.co
        local = os.path.join(co, "config", "local.env")
        plain = open(local).read()
        custom = os.path.join(co, ".custom-run")
        kill = [PY, "-B", os.path.join(co, "lib", "storage.py"), "kill-vm"]
        kh = os.path.join(lab.box.home, ".ssh", "metacomputer", "known_hosts")
        put = [PY, "-B", os.path.join(co, "lib", "guest_put.py"), "--transport", "4", "ssh", "-o",
               f"UserKnownHostsFile={kh}", "-T", os.path.join(co, "install.sh")]
        write(local, plain + 'export RUN_DIR="$LAB_ROOT/.custom-run"\n')
        handoff_on(custom)
        rc, out, err, called, written = run_line(lab, put, {}, "plain", custom)
        check("config/local.env 'export RUN_DIR=\"$LAB_ROOT/.custom-run\"', its handoff open there: guest_put.py with "
              "the main computer's transport, no run folder given, exits 4 saying the user has it; nothing called",
              rc == 4 and WAITS in err and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
        rc, out, err, called, written = run_line(lab, kill, {}, "plain", custom)
        check("...and storage.py kill-vm, no run folder given: exit 4, saying the user has it",
              rc == 4 and WAITS in err, (rc, err[-300:]))
        os.remove(os.path.join(custom, "owners", "handoff.state"))
        rc, out, err, called, written = run_line(lab, kill, {}, "plain", custom)
        check("...with no handoff open there: it goes ahead", rc == 0 and WAITS not in err, (rc, err[-300:]))
        write(os.path.join(co, "config", "more.env"), 'RUN_DIR="$LAB_ROOT/.custom-run"\n')
        for line, what in (("RUN_DIR=$LAB_ROOT/.esc\\ run", "a value with a backslash"),
                           ('source "$LAB_ROOT/config/more.env"', "a source line"),
                           ("true ||\nRUN_DIR=$LAB_ROOT/.custom-run", "'true ||' then an assignment bash skips"),
                           ('loader=source\n$loader "$LAB_ROOT/config/more.env"', "'$loader FILE' (loader=source)"),
                           ('HOME=/different-home\nRUN_DIR="$HOME/run"', "HOME reassigned, then used")):
            write(local, plain + line + "\n")
            rc, out, err, called, written = run_line(lab, kill, {}, "plain", custom)
            check(f"config/local.env with {what}: storage.py kill-vm refuses (exit 4, its settings can't be read), no "
                  "handoff open anywhere", rc == 4 and "settings" in err and WAITS not in err, (rc, err[-300:]))
            rc, out, err, called, written = run_line(lab, put, {}, "plain", custom)
            check(f"...and guest_put.py with the main computer's transport: refused (exit 4), nothing called",
                  rc == 4 and not acting_calls(called), (rc, acting_calls(called)[:3], err[-300:]))
        write(local, plain)
    finally:
        lab.close()


def holder_liveness_checks(td):
    """A holder of a volume is left out of a manual sync's gate only when its process is certainly gone (codex re-check
    4, #2): a live pid that ps can't read, or a record without a start time, refuses (exit 4)."""
    print("\nvolume sync: a holder whose process can't be told alive or gone")
    lab = make_lab(td, "live")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    keeper = subprocess.Popen(["/bin/sleep", "600"])
    try:
        co, st = lab.box.co, storage_state(lab)
        other_run, main_run = os.path.join(co, ".run-vms", "other"), os.path.join(co, ".run")
        copy = os.path.join(lab.box.root, "s3copy")
        os.makedirs(copy, exist_ok=True)
        write(os.path.join(st, "registry", "volumes", "shared.json"),
              json.dumps({"name": "shared", "kind": "s3", "path": copy, "bucket": "b", "endpoint": "https://example.com"}))
        rec_path = os.path.join(st, "locks", "s3-test-shared.json")
        base = {"kind": "s3", "name": "shared", "path": copy, "vm": "other", "lab": os.path.realpath(co),
                "pid": keeper.pid, "pstart": lstart(keeper.pid), "since": "now", "state": "running"}
        sync = [PY, "-B", os.path.join(co, "lib", "storage.py"), "volume", "sync", "shared"]
        noisy = os.path.join(lab.box.root, "ps-fails")
        write(os.path.join(noisy, "ps"), "#!/bin/sh\nexit 1\n", 0o755)
        env = lab.env(os.devnull)
        for rec, extra, how in ((base, {"PATH": noisy + ":" + env["PATH"]}, "ps can't read its live process"),
                                ({k: v for k, v in base.items() if k != "pstart"}, {}, "a record with no start time"),
                                (dict(base, pid="12"), {}, "a record whose pid isn't a number")):
            write(rec_path, json.dumps(rec), 0o600)
            rc, out, err, called, written = run_line(lab, sync, dict(extra, MCOMP_RUN_DIR=main_run), "plain", other_run)
            check(f"volume sync shared, VM other holding it ({how}), no handoff open anywhere: refused (exit 4) before "
                  "anything is called", rc == 4 and "can't be told" in err and not acting_calls(called),
                  (rc, acting_calls(called)[:3], err[-300:]))
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        handoff_on(other_run)
        for rec, how in ((dict(base, pid=dead.pid), "its process gone"),
                         (dict(base, pstart="Mon Jan  1 00:00:00 2001"), "its pid now another process's")):
            write(rec_path, json.dumps(rec), 0o600)
            rc, out, err, called, written = run_line(lab, sync, {"MCOMP_RUN_DIR": main_run}, "plain", other_run)
            check(f"volume sync shared, the record of VM other ({how}; other's handoff open): other isn't waited for",
                  WAITS not in err and "can't be told" not in err, (rc, err[-300:]))
        os.remove(rec_path)
    finally:
        keeper.kill()
        keeper.wait()
        lab.close()


def held_records_checks(td):
    """`volume resize` changes no computer (the table's NONE) because a reservation keeps every computer off the volume
    while it works, and that reservation reads the holder records strictly (codex check r5, #2): a running VM's record
    that's there and can't be read or parsed, or a locks folder that can't be read, refuses, naming it; nothing is
    truncated. The program itself, run as an agent runs it."""
    print("\nvolume resize: holder records that can't be read refuse, never read as nobody")
    lab = make_lab(td, "held")
    check("the copy runs a plain command first", lab is not None)
    if lab is None:
        return
    keeper = subprocess.Popen(["/bin/sleep", "600"])
    try:
        co, st = lab.box.co, storage_state(lab)
        sp = [PY, "-B", os.path.join(co, "lib", "storage.py")]
        r = subprocess.run(sp + ["volume", "create", "dv", "--size", "16M"], env=lab.env(os.devnull), cwd=co,
                           capture_output=True, text=True, timeout=60)
        reg = os.path.join(st, "registry", "volumes", "dv.json")
        check("a disk volume made with the program (volume create dv --size 16M)", r.returncode == 0
              and os.path.isfile(reg), r.stderr[-300:])
        if r.returncode or not os.path.isfile(reg):
            return
        img = json.load(open(reg))["path"]
        size = os.path.getsize(img)
        locks = os.path.join(st, "locks")
        os.makedirs(locks, mode=0o700, exist_ok=True)
        sys.path.insert(0, os.path.join(LAB, "lib"))
        import storage as storage_mod
        rec_path = os.path.join(locks, storage_mod.lock_key("disk", img) + ".json")
        held = {"kind": "disk", "name": "dv", "path": img, "vm": "other", "lab": os.path.realpath(co),
                "pid": keeper.pid, "pstart": lstart(keeper.pid), "since": "now", "state": "running",
                "rw": True, "exclusive": True, "lock": storage_mod.lock_key("disk", img)}
        resize = sp + ["volume", "resize", "dv", "--size", "32M"]
        main_run = os.path.join(co, ".run")
        write(rec_path, json.dumps(held), 0o600)
        rc, out, err, called, written = run_line(lab, resize, {"MCOMP_RUN_DIR": main_run}, "plain", main_run)
        check("volume resize dv while VM other's readable record holds it: refused (in use), not truncated",
              rc == 1 and "in use by" in err and os.path.getsize(img) == size, (rc, err[-300:]))
        for how, text, mode in (("unreadable", json.dumps(held), 0), ("malformed (not JSON)", "{", 0o600)):
            write(rec_path, text, 0o600)
            os.chmod(rec_path, mode)
            try:
                rc, out, err, called, written = run_line(lab, resize, {"MCOMP_RUN_DIR": main_run}, "plain", main_run)
            finally:
                if os.path.lexists(rec_path):       # (an older reader took it over, then let go of it)
                    os.chmod(rec_path, 0o600)
            check(f"volume resize dv with VM other's holder record {how}: refused (exit 1), naming the record and how "
                  "to clear a stale one; nothing truncated", rc == 1 and "can't be read" in err and "stale" in err
                  and os.path.basename(rec_path) in err and os.path.getsize(img) == size and img not in written,
                  (rc, os.path.getsize(img), err[-300:]))
        if os.path.lexists(rec_path):
            os.remove(rec_path)
        for mode in (0, 0o300):
            os.chmod(locks, mode)
            try:
                rc, out, err, called, written = run_line(lab, resize, {"MCOMP_RUN_DIR": main_run}, "plain", main_run)
            finally:
                os.chmod(locks, 0o700)
            check(f"volume resize dv with the storage locks folder unreadable (mode {mode:o}): refused (exit 1), naming "
                  "the folder; nothing truncated", rc == 1 and "storage locks folder" in err and "can't be read" in err
                  and os.path.getsize(img) == size and img not in written, (rc, os.path.getsize(img), err[-300:]))
        rc, out, err, called, written = run_line(lab, resize, {"MCOMP_RUN_DIR": main_run}, "plain", main_run)
        check("...and with nothing held, volume resize grows it",
              rc == 0 and "16.0 MiB -> 32.0 MiB" in out and os.path.getsize(img) == 32 << 20, (rc, out[-200:], err[-300:]))
    finally:
        keeper.kill()
        keeper.wait()
        lab.close()


def main():
    td = tempfile.mkdtemp(prefix="mcact-", dir="/tmp")
    try:
        rows = all_rows()
        table_checks(rows)
        session_guard_checks(td)
        unreadable_settings_checks(td)
        strict_reader_checks(td)
        settings_syntax_checks(td)
        config_file_checks(td)
        holder_liveness_checks(td)
        held_records_checks(td)
        binding_checks(td)
        volume_checks(td)
        look_once_checks(td)
        default_name_checks(td)
        cloud_setup_checks(td)
        mcp_manifest_checks(td)
        script_mark_checks(td)
        entry_checks(td, rows)
        bin_checks(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{COUNT[0] - len(FAILS)}/{COUNT[0]} passed")
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
