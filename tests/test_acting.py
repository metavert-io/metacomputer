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
HELD_FD = 21                                        # where the inherited hold is handed on (a script's own 5-9 stay free)
# The copy's runner: its computers exist and run (so a command gets as far as its handoff check), every call recorded;
# anything but a lookup is refused (97).
RUNNER = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ.get("MCOMP_HELP_CALLS") or os.devnull, "a") as f:
    f.write("standin mcomp-vm " + " ".join(a) + "\n")
vms = [{"name": n, "status": s, "locationName": "home", "macAddress": "aa:bb:cc:dd:ee:01"}
       for n, s in (("helpvm", "running"), ("helpvm-baseline", "stopped"), ("helpvm-ckpt-ck", "stopped"),
                    ("other", "running"), ("other-baseline", "stopped"))]
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
# The same, with the hold handed on as descriptor HELD_FD (moved there in the child: the test's threads share theirs).
ALARM_HELD = ("import os, signal, sys; signal.alarm(int(sys.argv[1])); fd = int(sys.argv[2]); "
              "os.dup2(fd, %d); os.close(fd); os.execv(sys.argv[3], sys.argv[3:])" % HELD_FD)


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


def run_line(lab, argv, extra, mode, run_dir, secs=25):
    """ARGV in the copy, as MODE: "plain" (no terminal), "user" (the user's own terminal) or "held" (under a hold on
    RUN_DIR's dispatch lock handed on as descriptor HELD_FD). (rc, out, err, calls, written)."""
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
        fds, hold = (), None
        if mode == "held":
            import fcntl
            hold = os.open(os.path.join(run_dir, "owners", ".dispatch"), os.O_RDWR)
            fcntl.flock(hold, fcntl.LOCK_SH)
            fds = (hold,)
            wrapped = [PY, "-c", ALARM_HELD, str(secs), str(hold)] + argv
        try:
            r = subprocess.run(wrapped, env=env, cwd=lab.box.co, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=secs + 30, start_new_session=True, pass_fds=fds)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as x:
            rc = "timeout"
            out = x.stdout.decode("utf-8", "replace") if isinstance(x.stdout, bytes) else (x.stdout or "")
            err = x.stderr.decode("utf-8", "replace") if isinstance(x.stderr, bytes) else (x.stderr or "")
        finally:
            if hold is not None:
                os.close(hold)
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
        if label == acting.ACTS:
            check(f"{words}: while the user has the computer, by itself: exit 4, saying so; nothing called, connected or "
                  "written", rc == 4 and WAITS in err and not acted and not written, (rc, acted[:3], written[:3], err[-300:]))
            rc, out, err, called, _ = user
            check(f"{words}: ...at the user's own terminal it goes past the gate", GOES in err and rc not in (4,),
                  (rc, err[-300:]))
            rc, out, err, called, _ = held
            check(f"{words}: ...under its caller's hold (an inherited descriptor on this VM's dispatch lock) it goes past "
                  "the gate", rc != 4 and WAITS not in err and WAITS not in out, (rc, err[-300:]))
        elif label == acting.RESET:
            check(f"{words}: an input reset while the user has the computer, by itself: nothing done (exit 4, or view's "
                  "silent nothing); nothing called or written", rc in (0, 4) and not acted and not written
                  and (rc == 0 or WAITS in err), (rc, acted[:3], written[:3], err[-300:]))
            rc, out, err, called, _ = user
            past = GOES in err or "couldn't tell whether an agent call is running" in (out + err)
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
            held = run_line(lab, argv, extra, "held", run_dir) if label == acting.ACTS else None
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
        rc, out, err, called, _ = held
        check(f"{words}: ...under its caller's hold it goes past the gate", rc != 4 and WAITS not in err,
              (rc, err[-300:]))


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
                held = run_line(lab, argv, {}, "held", run_dir, secs=20)
            out.append((vm, cmd, a, label, (plain, user, held)))
    finally:
        lab.close()
    return out


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
    finally:
        lab.close()


def main():
    td = tempfile.mkdtemp(prefix="mcact-", dir="/tmp")
    try:
        rows = all_rows()
        table_checks(rows)
        session_guard_checks(td)
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
