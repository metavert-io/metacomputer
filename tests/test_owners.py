#!/usr/bin/env python3
"""Claims on the virtual computer made outside Claude Code (lib/owners.py). No VM.

    python3 tests/test_owners.py

`metacomputer sh` run outside Claude Code holds its claim in the name of the shell
that ran it, which is often short-lived (bash -c, a script, another agent's tool
runner). It checks that such a claim:
- outlives that shell for the activity window (here 3 s instead of ten minutes);
- counts as the same controller for later runs from the same terminal session,
  and as another, active controller for a different session;
- is gone once the window has passed;
while a Claude Code session's claim and a bridge's claim still go with their process.
It also checks the owners lock: TimeoutError when another process holds it past the
timeout, with no file left open, and free again once the holder leaves.

And where /bin/ps can't start (macOS's sandbox runs no setuid program: here under sandbox-exec with a profile that
allows everything, so only that refusal is left), what lib/procinfo.py reads from the kernel instead: the same pid,
parent, start time, name, command line and executable as ps on this Mac's processes; claims, the process table and
the gateway check working there; and every process whose name the kernel can't give as ps would counted as an
unidentified agent (fail closed). A name that holds a newline and a row of its own (another account's process, whose
short name the kernel gives: "\\n300 1 sh\\n") adds no row there, and a table that lists a pid twice is refused (can't
tell): the agent above a command is found, or it can't be told; never "no agent".
"""
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runports  # noqa: E402  (this run's own port numbers: tests/runports.py)
SSH_PORT = runports.port()
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# Each step runs in a fresh Python with these settings: owners reads them at import. claude_of is
# replaced so the test behaves as outside Claude Code even when Claude Code runs it.
PRELUDE = ("import json, os, sys; sys.path.insert(0, os.path.join(%r, 'lib')); import owners; "
           "owners.claude_of = lambda *a: None; ") % LAB


def py(code, env, new_session=False):
    r = subprocess.run([sys.executable, "-c", PRELUDE + code], env=env, capture_output=True, text=True,
                       start_new_session=new_session, timeout=60)
    return r


def detached_pty(argv, timeout=30):
    """Run argv on a new pseudo-terminal in a process whose parent is launchd (pid 1): detached from whatever runs
    this test, so no agent session that started the suite is above it. Returns its exit code (None if it didn't
    report within the timeout)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            if os.fork() != 0:
                os._exit(0)                       # the middle process exits: its child goes to launchd
            for _ in range(500):
                if os.getppid() == 1:
                    break
                time.sleep(0.01)
            cpid, fd = pty.fork()
            if cpid == 0:
                os.execv(argv[0], argv)
            while True:                           # drain the terminal until the command closes it
                try:
                    if not os.read(fd, 4096):
                        break
                except OSError:
                    break
            _, st = os.waitpid(cpid, 0)
            os.write(w, str(os.waitstatus_to_exitcode(st)).encode())
        finally:
            os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)
    data, end = b"", time.time() + timeout
    while time.time() < end:
        if select.select([r], [], [], 1)[0]:
            chunk = os.read(r, 64)
            if not chunk:
                break
            data += chunk
    os.close(r)
    try:
        return int(data)
    except ValueError:
        return None


# --- where ps can't start: lib/procinfo.py ----------------------------------------------------------------------------
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# A profile that allows everything: under sandbox-exec, macOS still runs no setuid program, /bin/ps included. That
# refusal is all this reproduces (an agent session confined by a sandbox of its own).
ALLOW_ALL = "(version 1)(allow default)"

# Run inside (or, with --force-kernel, with ps made unstartable in-process): every process as metacomputer reads it
# there. owners.ps_table for pid, parent, name and which names can't be told; procinfo for the command line (exact or
# None), start time and executable.
DUMP = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import owners, procinfo
if "--force-kernel" in sys.argv:
    procinfo.PS = "/nonexistent/ps"
t = owners.ps_table(strict=True)
out = {}
for pid, (ppid, comm) in t.items():
    out[pid] = {"ppid": ppid, "comm": comm, "inexact": pid in t.inexact, "command": procinfo.kernel_field(pid, "command"),
                "start": procinfo.start(pid), "lstart": procinfo.kernel_field(pid, "lstart"), "exe": procinfo.exe(pid)}
me = os.getpid()
print(json.dumps({"procs": out, "me": me, "claude": owners.claude_of(me, t),
                  "claude_kind": owners.agent_of(owners.claude_of(me, t), t), "complete": owners.ancestry_complete(me, t)}))
"""


def sandboxed(argv, **kw):
    return subprocess.run([SANDBOX_EXEC, "-p", ALLOW_ALL, *argv], capture_output=True, text=True, timeout=120, **kw)


def ps_snapshot():
    """This Mac's processes as ps lists them: {pid: {ppid, comm, lstart (C locale), command}}."""
    env = dict(os.environ, LC_ALL="C")
    def rows(fmt, n):
        out = {}
        txt = subprocess.run(["/bin/ps", "-axww", "-o", fmt], capture_output=True, env=env).stdout.decode("utf-8", "replace")
        for line in txt.splitlines():
            parts = line.split(None, n)
            if len(parts) >= n:
                out[int(parts[0])] = parts[1:] + [""] * (n + 1 - len(parts))
        return out
    tab = rows("pid=,ppid=,comm=", 2)
    cmd = rows("pid=,command=", 1)
    lst = {}
    txt = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,lstart="], capture_output=True, env=env, text=True).stdout
    for line in txt.splitlines():
        a = line.split(None, 1)
        if len(a) == 2:
            lst[int(a[0])] = a[1].strip()
    return {pid: {"ppid": int(v[0]), "comm": v[1], "command": cmd.get(pid, [""])[0].strip(), "lstart": lst.get(pid)}
            for pid, v in tab.items()}


def proc_fn(name):
    """A function's text from scripts/lib.sh (`name() {` to its closing brace), to run by itself."""
    src = open(os.path.join(LAB, "scripts", "lib.sh")).read().split("\n")
    for i, line in enumerate(src):
        if line.startswith(name + "() {"):
            j = i
            while not src[j].startswith("}"):
                j += 1
            return "\n".join(src[i:j + 1])
    return ""


def procinfo_checks():
    lib = os.path.join(LAB, "lib")
    sandbox = os.path.exists(SANDBOX_EXEC) and sandboxed(["/usr/bin/true"]).returncode == 0
    r = sandboxed(["/bin/ps", "-o", "pid=", "-p", str(os.getpid())]) if sandbox else None
    check("under sandbox-exec (a profile allowing everything), /bin/ps can't start: macOS runs no setuid program there",
          sandbox and r.returncode != 0 and "Operation not permitted" in r.stderr, r and r.stderr[-200:])
    run = (lambda argv: sandboxed(argv)) if sandbox else (
        lambda argv: subprocess.run(argv + ["--force-kernel"], capture_output=True, text=True, timeout=120))

    # Every process of this Mac, as ps lists it and as the kernel gives it where ps can't start: the same answers. A
    # process that changed between the reads (one that renames itself, one started or ended meanwhile) is left out:
    # ps is read before and after, and only what it says twice the same way is compared.
    before = ps_snapshot()
    r = run([sys.executable, "-c", DUMP, lib])
    after = ps_snapshot()
    try:
        got = json.loads(r.stdout)
        kern = {int(k): v for k, v in got["procs"].items()}
    except (ValueError, KeyError):
        got, kern = {}, {}
    check("where ps can't start, the process table still reads (from the kernel), whole: this process's ancestry "
          "reaches launchd", r.returncode == 0 and got.get("complete") is True and len(kern) > 50, r.stderr[-300:])
    stable = {p: v for p, v in before.items() if after.get(p) == v and p in kern}
    me = os.getuid()
    diff = {"ppid": [], "lstart": [], "comm": [], "command": [], "exe": [], "start": []}
    exact = own = 0
    for pid, v in stable.items():
        k = kern[pid]
        if k["ppid"] != v["ppid"]:
            diff["ppid"].append(pid)
        if v["lstart"] and k["lstart"] != v["lstart"]:
            diff["lstart"].append((pid, v["lstart"], k["lstart"]))
        if k["start"] is None:
            diff["start"].append(pid)
        if not k["inexact"]:
            exact += 1
            if k["comm"] != v["comm"]:
                diff["comm"].append((pid, v["comm"][:60], k["comm"][:60]))
        if k["command"] is not None:
            own += 1
            if k["command"].strip() != v["command"]:
                diff["command"].append((pid, v["command"][:60], k["command"][:60]))
        if k["exe"] != owners_exe(pid):
            diff["exe"].append(pid)
    check(f"...the same pids as ps (all but those started or ended between the reads): {len(stable)} compared",
          len(stable) > 50 and len(set(before) & set(after) - set(kern)) <= 3, sorted(set(before) & set(after) - set(kern))[:5])
    check("...each one's parent the same as ps's", stable and not diff["ppid"], diff["ppid"][:5])
    check("...and its start time: the kernel's, as ps's lstart prints it", stable and not diff["lstart"] and not diff["start"],
          (diff["lstart"][:3], diff["start"][:3]))
    check(f"...and its name (ps comm=, the executable as the process names it) wherever the kernel can tell it: {exact} "
          "the same", exact > 20 and not diff["comm"], diff["comm"][:3])
    check(f"...and its command line (ps command=), for every one the kernel can render exactly: {own} the same",
          own > 20 and not diff["command"], diff["command"][:3])
    check("...and its executable (its text image, which recognition reads) the same inside as outside",
          stable and not diff["exe"], diff["exe"][:5])
    others_ = [p for p, v in kern.items() if p in stable and v["inexact"]]
    check("...and every name it can't tell (another account's process: only setuid-root ps reads those) marked so",
          kern.get(1, {}).get("inexact") is True and others_ and all(kern[p]["command"] is None for p in others_),
          others_[:5])

    # The session's agent, from inside: the same as ps finds, or (where a name can't be told) an unidentified agent.
    outside = subprocess.run([sys.executable, "-c", DUMP, lib], capture_output=True, text=True, timeout=120)
    try:
        o = json.loads(outside.stdout)
    except ValueError:
        o = {}
    check("...the agent session above a command is the one ps finds, or an unidentified agent where the kernel can't "
          "tell a name (fail closed): never none where ps finds one",
          got and o and (got.get("claude") == o.get("claude") or got.get("claude_kind") == "unidentified agent")
          and not (o.get("claude") and not got.get("claude")),
          (o.get("claude"), o.get("claude_kind"), got.get("claude"), got.get("claude_kind")))

    # The report's failures, inside a sandbox: a session's claim (`metacomputer sh`) and the gateway check (`status`).
    td = tempfile.mkdtemp(prefix="mcomp-procinfo-")
    try:
        env = dict(os.environ, MCOMP_RUN_DIR=td, MCOMP_SSH_PORT=str(SSH_PORT), MCOMP_SESSIONS_DIR=os.path.join(td, "s"))
        code = ("import json, os, sys; sys.path.insert(0, sys.argv[1]); import owners\n"
                "if '--force-kernel' in sys.argv:\n    import procinfo; procinfo.PS = '/nonexistent/ps'\n"
                "print(json.dumps(owners.claim_for_shell('vm_shell')))")
        r = (sandboxed([sys.executable, "-c", code, lib], env=env) if sandbox else
             subprocess.run([sys.executable, "-c", code, lib, "--force-kernel"], env=env, capture_output=True, text=True))
        claims_ = [f for f in os.listdir(os.path.join(td, "owners")) if f.endswith(".json")] \
            if os.path.isdir(os.path.join(td, "owners")) else []
        check("where ps can't start, metacomputer sh's claim is still recorded (it failed: Operation not permitted: "
              "'/bin/ps')", r.returncode == 0 and len(claims_) == 1, (r.returncode, r.stderr[-300:]))
        gwdir = os.path.join(td, "run")
        os.makedirs(gwdir)
        # a stand-in gateway: its command line names this VM's gw.json, as the real one's does
        gw = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-config", f"{gwdir}/gw.json"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with open(os.path.join(gwdir, "netgw.pid"), "w") as f:
                f.write(f"{gw.pid}\n")
            script = "\n".join([proc_fn("proc_field"), proc_fn("netgw_pid"), 'netgw_pid; proc_field command "$1"'])
            benv = dict(os.environ, RUN_DIR=gwdir, LAB_ROOT=LAB, PY=sys.executable)
            r = (sandboxed(["/bin/bash", "-c", script, "_", str(gw.pid)], env=benv) if sandbox else None)
            ps_cmd = subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(gw.pid)], capture_output=True,
                                    text=True).stdout.strip()
            lines = r.stdout.split("\n") if r else []
            check("...and the gateway check (scripts/lib.sh netgw_pid, `status`) finds this VM's gateway there",
                  r is not None and lines[:1] == [str(gw.pid)], r and (r.stdout[-200:], r.stderr[-200:]))
            check("...and proc_field gives ps's own words for it there (scripts/lib.sh, bin/metacomputer)",
                  r is not None and len(lines) > 1 and lines[1] == ps_cmd, r and (lines, ps_cmd))
        finally:
            gw.kill()
            gw.wait()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # In detail, with ps unstartable in this process (procinfo.PS: no such program).
    try:
        import procinfo
        import owners
    except ImportError as e:
        check(f"lib/procinfo.py: the kernel's reading where ps can't start ({e})", False)
        return
    kids = []
    try:
        def kid(argv0, *argv):
            p = subprocess.Popen([argv0, "-c", "sleep 60; :", *argv], executable="/bin/sh", stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            kids.append(p)
            return p
        inj = kid("x\n99999 1 claude")
        uni = kid("café")
        named = kid("claude")
        tab = kid("tab\there")
        time.sleep(0.5)
        ps_comm = lambda p: subprocess.run(["/bin/ps", "-ww", "-o", "comm=", "-p", str(p)], capture_output=True,
                                           text=True).stdout.rstrip("\n")
        ps_cmdline = lambda p: subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(p)], capture_output=True,
                                              text=True).stdout.strip()
        with_ps = owners.ps_table(strict=True)
        real = procinfo.PS
        procinfo.PS = "/nonexistent/ps"
        try:
            k = owners.ps_table(strict=True)
            kcmd = {p.pid: owners.full_command(p.pid) for p in (inj, uni, named, tab)}
            owners._AGENT_NAMES.clear()
            name1 = owners.agent_name(1)
            real_procs = procinfo.kernel_procs
            procinfo.kernel_procs = lambda: None
            try:
                try:
                    owners.ps_table(strict=True)
                    strict_raised = False
                except OSError:
                    strict_raised = True
                loose = owners.ps_table()
            finally:
                procinfo.kernel_procs = real_procs
        finally:
            procinfo.PS = real
            owners._AGENT_NAMES.clear()
        check("a process whose name holds a newline can't add a row to the kernel's table (written \\012, as ps does)",
              k.get(inj.pid, (0, ""))[1] == ps_comm(inj.pid) == "x\\01299999 1 claude"
              and (99999 not in k or 99999 in with_ps), (k.get(inj.pid), ps_comm(inj.pid)))
        check("...a tab, and the rest of ps's encoding, the same in the command line",
              k.get(tab.pid, (0, ""))[1] == ps_comm(tab.pid) and kcmd[tab.pid] == ps_cmdline(tab.pid) != "",
              (k.get(tab.pid), kcmd[tab.pid], ps_cmdline(tab.pid)))
        check("a name beyond ASCII (ps writes it by its locale) can't be told: an unidentified agent, its command "
              "line empty (as from a ps that fails)",
              uni.pid in k.inexact and owners.agent_of(uni.pid, k) == "unidentified agent" and kcmd[uni.pid] == ""
              and owners.agent_of(uni.pid, with_ps) is None, (owners.agent_of(uni.pid, k), kcmd[uni.pid]))
        check("another account's process (launchd, root's) is an unidentified agent there, never no agent; with ps it "
              "is none", 1 in k.inexact and owners.agent_of(1, k) == "unidentified agent"
              and owners.agent_of(1, with_ps) is None and not with_ps.inexact)
        check("...and so is agent_name's answer for it (a claim's agent field)", name1 == "unidentified agent", name1)
        check("an agent's name reads the same from the kernel as from ps (the session a claim goes with)",
              owners.agent_of(named.pid, k) == owners.agent_of(named.pid, with_ps) == "Claude Code"
              and kcmd[named.pid] == ps_cmdline(named.pid), (owners.agent_of(named.pid, k), kcmd[named.pid]))
        check("neither ps nor the kernel readable: the strict table fails (OSError), the loose one is empty, as "
              "when ps fails", strict_raised and len(loose) == 0)
        c_ps = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(named.pid)], capture_output=True, text=True,
                              env=dict(os.environ, LC_ALL="C")).stdout.strip()
        fr_ps = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(named.pid)], capture_output=True, text=True,
                               env=dict(os.environ, LC_ALL="fr_FR.UTF-8")).stdout.strip()
        sec = procinfo.kernel_proc(named.pid).start[0]
        check("a start time in ps's words where its locale writes the C locale's (\"%s\"); none where it writes "
              "another's (\"%s\": storage's records compare these words)" % (c_ps, fr_ps),
              procinfo.lstart_of(sec, {"LC_ALL": "C"}) == c_ps and procinfo.lstart_of(sec, {"LANG": "fr_FR.UTF-8"}) is None
              and fr_ps != c_ps)
        cli = [sys.executable, "-I", "-B", os.path.join(lib, "procinfo.py")]
        r1 = subprocess.run(cli + ["command", str(named.pid)], capture_output=True, text=True)
        r2 = subprocess.run(cli + ["comm", "1"], capture_output=True, text=True)
        r3 = subprocess.run(cli + ["pgid", str(2 ** 22 + 7)], capture_output=True, text=True)
        r4 = subprocess.run(cli + ["pgid", str(named.pid)], capture_output=True, text=True)
        ps_pgid = subprocess.run(["/bin/ps", "-o", "pgid=", "-p", str(named.pid)], capture_output=True, text=True).stdout.strip()
        check("procinfo.py (bash's fallback): ps's command line and process group for a process of yours; exit 3 for a "
              "name it can't tell, 1 for no such process, nothing printed for either",
              r1.returncode == 0 and r1.stdout.strip() == ps_cmdline(named.pid) and r4.stdout.strip() == ps_pgid
              and r2.returncode == 3 and not r2.stdout and r3.returncode == 1 and not r3.stdout,
              (r1.returncode, r1.stdout[:80], r2.returncode, r3.returncode, r4.stdout, ps_pgid))
    finally:
        for p in kids:
            try:
                os.killpg(p.pid, 9)          # its sleep too
            except OSError:
                pass
            p.wait()


# --- a name that forges a row ---------------------------------------------------------------------------------------
def kinfo(procinfo, pid, ppid, uid, comm, start=1):
    """A process's record as kern.proc.all gives it (struct kinfo_proc), read back by lib/procinfo.py's own parser."""
    import struct
    raw = bytearray(procinfo._KINFO_SIZE)
    struct.pack_into("<qi", raw, 0, start, 0)
    raw[procinfo._KP_STAT] = 2                                  # SRUN
    struct.pack_into("<i", raw, procinfo._KP_PID, pid)
    raw[procinfo._KP_COMM:procinfo._KP_COMM + min(len(comm), 16)] = comm[:16]
    struct.pack_into("<I", raw, procinfo._KP_UID, uid)
    struct.pack_into("<ii", raw, procinfo._KP_PPID, ppid, pid)
    return procinfo._parse_kinfo(bytes(raw))


def forged_row_checks():
    """Another account's process (its arguments unreadable: only setuid-root ps reads them) is named in the kernel's
    table by its short name, its executable's file name, which may hold a newline: "\\n300 1 sh\\n" would add a row giving
    the agent's shell (300) another parent. Where ps can't start: written escaped as ps writes it, never a row; and a
    table that lists a pid twice is refused, never read with one row winning."""
    sys.path.insert(0, os.path.join(LAB, "lib"))
    try:
        import procinfo
        import owners
    except ImportError as e:
        check(f"lib/procinfo.py: forged rows ({e})", False)
        return
    me, other = os.getuid(), os.getuid() + 1
    hostile = b"\n300 1 sh\n"
    # launchd (root's), an agent session (claude, 200), its shell (300), a command under it (500); the hostile process
    # either before the shell in pid order (100) or after it (400). Their arguments: this account's, readable.
    argvs = {200: ["claude"], 300: ["sh"], 500: ["python3", "task.py"]}

    def table_with(hostile_pid):
        procs = [kinfo(procinfo, 1, 0, 0, b"launchd"), kinfo(procinfo, 200, 1, me, b"claude"),
                 kinfo(procinfo, 300, 200, me, b"sh"), kinfo(procinfo, hostile_pid, 1, other, hostile),
                 kinfo(procinfo, 500, 300, me, b"python3")]
        saved = (procinfo.PS, procinfo.kernel_procs, procinfo.argv)
        procinfo.PS = "/nonexistent/ps"
        procinfo.kernel_procs = lambda: procs
        procinfo.argv = lambda pid: argvs.get(pid)
        try:
            out = procinfo.table()
            try:
                t = owners.ps_table(strict=True)
            except OSError as e:
                return out, None, str(e)
        finally:
            procinfo.PS, procinfo.kernel_procs, procinfo.argv = saved
        # What agent_of reads of each process, given (never this Mac's own processes of those pids).
        t.exes = {p: None for p in t}
        t.argvs = {p: argvs.get(p) for p in t}
        t.cwds = {p: None for p in t}
        return out, t, None

    for hostile_pid in (400, 100):
        out, t, refused = table_with(hostile_pid)
        lines = out[0].split("\n")
        found = None if t is None else owners.claude_of(500, t)
        none = t is not None and found is None and owners.ancestry_complete(500, t)
        check(f"a name holding a newline and a row ('\\n300 1 sh\\n', pid {hostile_pid}): the kernel's table writes it "
              "escaped as ps does, one line, no row of its own",
              f"{hostile_pid:5d}     1 (\\012300 1 sh\\012)" in lines and len([x for x in lines if x]) == 5
              and not any(x.split()[:3] == ["300", "1", "sh"] for x in lines), lines)
        check(f"...and the agent above a command under that shell is found (pid {hostile_pid}), or it can't be told; "
              "never no agent", (refused is not None or found == 200) and not none,
              (found, refused, t and t.get(300)))
    saved = procinfo.table
    try:
        procinfo.table = lambda ps=None: ("    1     0 launchd\n  200     1 claude\n  300   200 sh\n  300     1 sh\n"
                                          "  500   300 python3\n", 0, frozenset())
        got = []
        for strict in (True, False):
            try:
                owners.ps_table(strict=strict)
                got.append(False)
            except OSError:
                got.append(True)
        procinfo.table = lambda ps=None: ("  200     1 claude\n  300   200 sh\x1c  300     1 sh\n", 0, frozenset())
        t = owners.ps_table(strict=True)
    finally:
        procinfo.table = saved
    check("a table that lists a pid twice is refused as can't tell (OSError, strict or not), never read with a row "
          "winning", got == [True, True], got)
    check("...and rows are its lines: split at newlines only, not at other separators a name may hold",
          sorted(t) == [200, 300] and t[300] == (200, "sh\x1c  300     1 sh"), dict(t))
    k = kinfo(procinfo, 77, 1, other, bytes(range(0x80, 0x90)) + b"\t\x7f")
    check("the kernel's short name is kept as its bytes, and written as ps's C locale writes them (M-, M^, \\011, ^?)",
          k.kname == bytes(range(0x80, 0x90)) and getattr(procinfo, "vis_c", lambda b: None)(b"\x80\xa0\xe9\t\n\x7f")
          == "M^@\\240M-i\\011\\012^?", (k.kname, getattr(procinfo, "vis_c", None)))


def owners_exe(pid):
    sys.path.insert(0, os.path.join(LAB, "lib"))
    try:
        import procinfo
        return procinfo.exe(pid)
    except ImportError:
        return "no lib/procinfo.py"


def main():
    td = tempfile.mkdtemp(prefix="mcomp-owners-")
    # A port of this run's own: no real connection to any VM (a running hub, a test, `web`), nor another run's
    # stand-in, counts as a controller here.
    env = dict(os.environ, MCOMP_RUN_DIR=td, MCOMP_ACTIVE_S="3", MCOMP_SSH_PORT=str(SSH_PORT),
               MCOMP_SESSIONS_DIR=os.path.join(td, "sessions"))   # lib/sessionvm.py's records: never the account's
    odir = os.path.join(td, "owners")

    # A run of metacomputer sh from a short-lived shell (bash -c), as another agent's tool runner does.
    r = subprocess.run(["/bin/bash", "-c", f"{sys.executable} -c \"{PRELUDE}print(json.dumps(owners.claim_for_shell('vm_shell')))\"; exit 0"],
                       env=env, capture_output=True, text=True, timeout=60)
    who = json.loads(r.stdout or "[0, 0]")
    holder = who[0]
    time.sleep(0.2)
    files = [f for f in os.listdir(odir) if f.endswith(".json")] if os.path.isdir(odir) else []
    rec = json.load(open(os.path.join(odir, files[0]))) if files else {}
    check("a shell claim outside Claude Code: held for the shell that ran it, marked as a shell claim with its session",
          r.returncode == 0 and rec.get("bridge_pid") == holder and rec.get("kind") == "shell" and rec.get("sid") == os.getsid(0)
          and rec.get("claude_pid") is None, (r.stderr[-300:], rec))
    alive = subprocess.run(["/bin/kill", "-0", str(holder)], capture_output=True).returncode == 0
    check("...whose shell has already exited", not alive)

    r = py("print(json.dumps(owners.claims()))", env)
    cl = json.loads(r.stdout or "[]")
    check("the claim outlives its shell within the activity window: listed, active", len(cl) == 1 and cl[0].get("active")
          and cl[0].get("holder_gone"), (r.stderr[-300:], cl))
    r = py("print(json.dumps(owners.others()))", env)
    check("...a later run from the same terminal session is the same controller (not refused)", json.loads(r.stdout or "[1]") == [],
          (r.stdout, r.stderr[-300:]))
    r = py("print(json.dumps(owners.others()))", env, new_session=True)
    oth = json.loads(r.stdout or "[]")
    check("...a run from another session sees another, active controller (refused unless --override)", len(oth) == 1
          and oth[0].get("active") and oth[0].get("kind") == "session" and oth[0].get("claim") == "shell", (r.stdout, r.stderr[-300:]))
    r = py("print(owners.describe(owners.others()[0]))", env, new_session=True)
    check("...described as a shell outside an agent session", "metacomputer sh outside an agent session" in r.stdout, r.stdout)

    # A Claude Code session's claim and a bridge's claim still go with their process.
    dead = holder
    py(f"owners.write_claim({dead + 100000}, 4242, '/x', 't', 0)", env)              # a claude session claim, pid not alive
    py(f"owners.write_claim({dead + 100001}, None, '/x', 't', 0)", env)              # a bridge claim, pid not alive
    r = py("print(json.dumps(sorted(c['bridge_pid'] for c in owners.claims())))", env)
    check("a dead Claude Code claim and a dead bridge's claim are removed at once (only the shell claim stays)",
          json.loads(r.stdout or "[]") == [holder], r.stdout)

    time.sleep(3.3)
    r = py("print(json.dumps(owners.claims()))", env)
    check("once the activity window has passed, the shell claim is gone", json.loads(r.stdout or "[1]") == []
          and not [f for f in os.listdir(odir) if f.endswith(".json")], r.stdout)

    # A shell claim whose shell is still alive stays (idle after the window), as before.
    p = subprocess.Popen(["/bin/bash", "-c", f"{sys.executable} -c \"{PRELUDE}owners.claim_for_shell('vm_shell')\"; sleep 30"], env=env)
    try:
        time.sleep(1.0)
        time.sleep(3.3)
        r = py("print(json.dumps(owners.claims()))", env)
        cl = json.loads(r.stdout or "[]")
        check("a shell claim whose shell is still running stays, idle after the window", len(cl) == 1 and not cl[0].get("active")
              and cl[0].get("bridge_pid") == p.pid, cl)
    finally:
        p.kill()
        p.wait()

    # The owners lock (the hub and metacomputer sh check and claim under it): while another process holds it,
    # TimeoutError after the timeout with its file closed; free again as soon as the holder leaves.
    lib = os.path.join(LAB, "lib")
    probe = ("import json, os, sys, time; sys.path.insert(0, sys.argv[1]); import owners\n"
             "n, t = len(os.listdir('/dev/fd')), time.monotonic()\n"
             "try:\n    with owners.lock(timeout=0.3):\n        print(json.dumps(['acquired']))\n"
             "except Exception as e:\n"
             "    print(json.dumps([type(e).__name__, str(e), time.monotonic() - t, len(os.listdir('/dev/fd')) - n]))\n")
    hold = subprocess.Popen([sys.executable, "-c", "import sys, time; sys.path.insert(0, sys.argv[1]); import owners\n"
                             "with owners.lock():\n    print('held', flush=True); time.sleep(2.0)\n", lib],
                            env=env, stdout=subprocess.PIPE, text=True)
    try:
        held = hold.stdout.readline().strip() == "held"
        r = subprocess.run([sys.executable, "-c", probe, lib], env=env, capture_output=True, text=True, timeout=60)
        got = json.loads(r.stdout or "[]")
        check("while another process holds the owners lock: TimeoutError after the timeout, its file closed",
              held and len(got) == 4 and got[0] == "TimeoutError" and got[1] == "another process holds the owners lock"
              and 0.3 <= got[2] < 1.5 and got[3] == 0, (got, r.stderr[-300:]))
    finally:
        hold.wait(timeout=30)
        hold.stdout.close()
    r = subprocess.run([sys.executable, "-c", probe, lib], env=env, capture_output=True, text=True, timeout=60)
    check("...and free again once its holder has left", json.loads(r.stdout or "[]") == ["acquired"], (r.stdout, r.stderr[-300:]))

    # Which process is Claude Code: every way it's installed, from a synthetic process table.
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import owners
    native = "/Users/someone/.local/share/claude/versions/2.1.325"
    check("Claude Code is recognized however it's installed (npm/Homebrew, IDE native binary, native installer)",
          all(owners.executable_agent(c) == "Claude Code" for c in ("claude", "/opt/homebrew/bin/claude", native,
              "/Users/someone/.vscode/extensions/anthropic.claude-code-2.1.322-darwin-arm64/resources/native-binary/claude")))
    check("...and other processes aren't", not any(owners.executable_agent(c) == "Claude Code" for c in (
        "/usr/bin/python3", "/bin/zsh", "", "/x/claude/versions/notes.txt", "/x/claude/versions/2.1.325/helper", "claude-helper")))
    table = {300: (200, "/bin/bash"), 200: (100, native), 100: (1, "/bin/zsh")}
    check("claude_of finds a natively installed Claude Code above a tool's shell (its claim goes with that session)",
          owners.claude_of(300, table) == 200, owners.claude_of(300, table))

    # Codex: its native binary, however it's installed (npm/Homebrew start it from a node launcher; the IDE extension and
    # the desktop app run `codex app-server`)
    codex_native = "/opt/homebrew/lib/node_modules/@openai/codex/vendor/aarch64-apple-darwin/codex/codex"
    check("Codex is an agent session however it's installed (npm/Homebrew native binary, IDE extension, desktop app)",
          all(owners.agent_kind(c) == "Codex" for c in ("codex", codex_native,
              "/Users/someone/.vscode/extensions/openai.chatgpt-26.1106.51428-darwin-arm64/bin/macos-aarch64/codex",
              "/Applications/Codex.app/Contents/Resources/codex")))
    check("...and its launcher and look-alikes aren't", not any(owners.agent_kind(c) for c in (
        "node", "/opt/homebrew/bin/node", "/x/bin/codex.js", "codex-helper", "/x/codexify", "/x/codex/notes")))
    check("...and Claude Code is still Claude Code", owners.agent_kind(native) == "Claude Code")
    table = {300: (250, "/bin/zsh"), 250: (200, codex_native), 200: (150, "/opt/homebrew/bin/node"), 150: (1, "/bin/zsh")}
    check("claude_of finds Codex above a command's shell, below its node launcher (its claims go with that session)",
          owners.claude_of(300, table) == 250, owners.claude_of(300, table))
    table = {400: (300, "/bin/zsh"), 300: (250, codex_native), 250: (200, "/bin/zsh"), 200: (1, native)}
    check("the nearest agent session counts: Codex run from Claude Code is a session of its own",
          owners.claude_of(400, table) == 300, owners.claude_of(400, table))

    check("an ancestry that doesn't reach launchd is unknown, never the user's (a process ps didn't list, a loop)",
          owners.ancestry_complete(300, {300: (250, "zsh"), 250: (1, "zsh")})
          and not owners.ancestry_complete(300, {300: (250, "zsh")})
          and not owners.ancestry_complete(300, {300: (250, "zsh"), 250: (300, "zsh")}))
    real_ps = owners.ps_table
    try:
        owners.ps_table = lambda strict=False: (_ for _ in ()).throw(OSError("ps failed")) if strict else {}
        tty_r, tty_w = os.openpty()
        fails_closed = not owners.at_user_terminal((tty_w,))
        os.close(tty_r)
        os.close(tty_w)
    finally:
        owners.ps_table = real_ps
    check("...and at_user_terminal fails closed when ps fails", fails_closed)

    # The user's own terminal, end to end with real processes: a terminal with no agent session above it. The runs
    # below are detached from whatever runs this test (re-parented to launchd), so an agent running the suite isn't
    # above them; a small program named `codex` (built here: copies of Apple's binaries don't run) stands in for
    # Codex giving a command a terminal of its own, running it as its child.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import userterm
    fake = userterm.fake_agent("codex")
    at = [sys.executable, os.path.join(LAB, "lib", "owners.py"), "at-terminal"]
    rc_plain = detached_pty(at)
    rc_codex = detached_pty([fake, *at]) if fake else "no C compiler to build the stand-in"
    check("at-terminal: a terminal with no agent session above it is the user's (exit 0)", rc_plain == 0, rc_plain)
    check("...a terminal Codex gave its command isn't (exit 1)", rc_codex == 1, rc_codex)
    r = subprocess.run(at, stdin=subprocess.DEVNULL, capture_output=True)
    check("...nor is no terminal at all", r.returncode == 1, r.returncode)

    procinfo_checks()
    forged_row_checks()

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
