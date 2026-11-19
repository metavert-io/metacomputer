"""Who is using the virtual computer? One controller at a time, no daemon.

Two kinds of controller can drive the VM's desktop:

- Clients of the hub (mcp_hub.py: Claude Code's `metacomputer mcp-lazy`,
  `metacomputer call`, scripts through mcomp_sdk.py) or, with MCP_HUB=off,
  bridges with their own connection. Each one that has acted keeps
  .run/owners/<pid>.json up to date: its Claude Code process, the project
  folder, the time and name of its last tool call. The hub writes them for its
  clients; a one-shot `metacomputer call` is held by its Claude Code process,
  or else the shell that ran it.
- Direct connections (`metacomputer mcp` with MCOMP_DIRECT_BRIDGE=1: the
  transport tests, or any program running as you that sets it). They are found
  as live SSH processes using the MCP key. The hub's own connection (and a
  bridge's) isn't one of them.

The shell channel (`metacomputer sh`) acts too: each run (except --read-only)
keeps .run/owners/<pid>.json up to date, where pid is its Claude Code process,
or outside Claude Code the shell that ran it, since the command itself exits.
Such a claim (kind "shell") lasts the activity window from its last run even after
that shell exits, and runs from the same terminal session (sid) are the same controller.

Claude Code inside the VM (`metacomputer claude`) keeps working after the command that
started it returns, so a keeper process (vm_claude.py _keeper) holds a claim for it (kind
"inner", .run/owners/<keeper pid>.json) in the name of the session that started it, while
the inner session runs: refreshed each time its screen changes, so it is active while the
inner Claude works and for the activity window after. It ends with `claude stop`, the
inner session's end, `metacomputer release` (the keeper then exits) or the keeper's exit.

The hub holds a claim of its own (kind "lease", .run/owners/<hub pid>.json) while
acting calls that a client left running when it disconnected are still running in
the driver: until they end or the driver connection closes, at most the activity
window. It isn't another controller to that client's process or its Claude Code
session.

"Self" is the caller's own Claude Code session (its nearest `claude` ancestor),
or a shell it runs under, so a session never blocks itself.

A handoff (lib/handoff.py, `metacomputer handoff`) gives the computer to the user:
while one is open, others() lists it for every caller, its own session included,
so every acting call waits until the user hands the computer back. Its record,
.run/owners/handoff.state, is written under the owners lock; `release` never drops it.

CLI (used by bin/metacomputer):
  owners.py who [--json]      list controllers
  owners.py check             exit 3 (and explain) if another controller is active; 4 while the user has it (a handoff)
  owners.py release           forget other sessions' claims (after the user agrees)
"""
import contextlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flockwait  # noqa: E402

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# bin/metacomputer passes this VM's RUN_DIR (config/defaults.env), so two VMs keep separate owners.
OWNERS_DIR = os.path.join(os.environ.get("MCOMP_RUN_DIR") or os.path.join(LAB, ".run"), "owners")
ACTIVE_S = int(os.environ.get("MCOMP_ACTIVE_S", "600"))  # acted within 10 min = in use
# the bridge's SSH key is <SSH_DIR>/mcp (SSH_DIR is configurable), to the loopback forward
MCP_KEY_RE = re.compile(r"IdentityFile=\S*/mcp(?:\s|$)")
# This VM's loopback SSH port (bin/metacomputer passes SSH_PORT). With two VMs side by side,
# a connection to the other VM's port is not an owner of this one.
SSH_PORT = os.environ.get("MCOMP_SSH_PORT", "")


def for_this_vm(cmd):
    if not SSH_PORT:
        return True
    return re.search(r"(?:^|\s)-p\s*" + re.escape(SSH_PORT) + r"(?:\s|$)", cmd) is not None \
        or re.search(r"Port=" + re.escape(SSH_PORT) + r"(?:\s|$)", cmd) is not None


def cli():
    """How to name the metacomputer command in messages: the installed launcher
    (~/.local/bin/metacomputer) when it points at this checkout, else the path."""
    own = os.path.join(LAB, "bin", "metacomputer")
    link = os.path.expanduser("~/.local/bin/metacomputer")
    if os.path.exists(link) and os.path.realpath(link) == os.path.realpath(own):
        return "~/.local/bin/metacomputer"
    return own


def ps_table():
    out = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2:
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def full_command(pid):
    return subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def ancestors(pid, table):
    chain, seen = [], set()
    while pid in table and pid not in seen and pid > 1:
        seen.add(pid)
        chain.append(pid)
        pid = table[pid][0]
    return chain


# Claude Code's process: `claude` (npm, Homebrew, the IDE extensions' native-binary/claude) or,
# for the native installer, the version file itself: ~/.local/share/claude/versions/2.1.325.
_NATIVE_CLAUDE = re.compile(r"/claude/versions/\d+\.\d+\.\d+[^/]*$")


def is_claude(comm):
    """True when a process table command (ps comm=) is Claude Code."""
    return os.path.basename(comm or "") == "claude" or bool(_NATIVE_CLAUDE.search(comm or ""))


def claude_of(pid, table):
    """Nearest Claude Code process above pid (None outside Claude Code)."""
    for p in ancestors(pid, table)[1:]:
        if is_claude(table[p][1]):
            return p
    return None


def proc_cwd(pid):
    out = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def short(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and path.startswith(home) else path


# --- on-demand bridge claims -------------------------------------------------

def claim_path(bridge_pid):
    return os.path.join(OWNERS_DIR, f"{bridge_pid}.json")


@contextlib.contextmanager
def lock(timeout=10.0):
    """This VM's owners lock: hold it to check for other controllers and claim the
    computer as one step (metacomputer sh does; an acting bridge can too).
    TimeoutError if another process holds it for longer than timeout."""
    os.makedirs(OWNERS_DIR, exist_ok=True)
    fd = os.open(os.path.join(OWNERS_DIR, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            flockwait.exclusive(fd, timeout, 0.05)
        except BlockingIOError:
            raise TimeoutError("another process holds the owners lock")
        yield
    finally:
        os.close(fd)   # releases the lock


DISPATCH_FILE = ".dispatch"
DISPATCH_WAIT_S = 10.0     # an acting step waits this long for a handoff that is being opened


def dispatch_path():
    return os.path.join(OWNERS_DIR, DISPATCH_FILE)


@contextlib.contextmanager
def dispatch(shared=True, timeout=DISPATCH_WAIT_S):
    """This VM's dispatch lock, which makes "is a handoff open?" and the action it lets through one step.
    Whatever acts holds it shared from its last look for a handoff until the action has left the Mac (the hub
    across sending a call, `metacomputer sh` across starting its run, a vault fill or session load for the
    whole command, the hub's own cleanup), and opening a handoff holds it exclusively while it writes the
    record. So an action is either on its way before the handoff opens, or sees it. Never take it while holding
    lock(): the order is dispatch, then the owners lock. TimeoutError when it can't be had within timeout."""
    os.makedirs(OWNERS_DIR, exist_ok=True)
    fd = os.open(dispatch_path(), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            flockwait.take(fd, timeout, 0.02, shared=shared)
        except BlockingIOError:
            raise TimeoutError("a handoff is being opened" if shared else
                               "an action is still on its way to the virtual computer")
        yield
    finally:
        os.close(fd)   # releases the lock


def write_claim(bridge_pid, claude_pid, cwd, tool, started, kind=None, sid=None, extra=None):
    os.makedirs(OWNERS_DIR, exist_ok=True)
    rec = {"bridge_pid": bridge_pid, "claude_pid": claude_pid, "cwd": cwd, "started": started,
           "last_call": time.time(), "last_tool": tool}
    if kind:
        # "shell": metacomputer sh outside Claude Code (kept by its time, see claims()); "lease": the hub holding
        # the computer for acting calls a client left running when it disconnected (mcp_hub.Hub.hand_over_claim)
        rec["kind"] = kind
    if sid is not None:
        rec["sid"] = sid        # the terminal session it ran in: later runs from it are the same controller
    for k, v in (extra or {}).items():
        rec.setdefault(k, v)
    # A temporary file of its own: two writers for one claim (two runs under one session) don't collide.
    fd, tmp = tempfile.mkstemp(dir=OWNERS_DIR, prefix=".claim-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, claim_path(bridge_pid))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def my_sid():
    try:
        return os.getsid(0)
    except OSError:
        return None


def sid_of(pid):
    """Another process's terminal session (the hub asks for its clients')."""
    try:
        return os.getsid(int(pid))
    except (OSError, ValueError, TypeError):
        return None


def claim_for_shell(tool, who=None):
    """`metacomputer sh` acted. Its process ends with the run, so the claim lives
    as long as the caller's Claude Code session. Outside Claude Code it's held in
    the name of the shell that ran it, but that shell is often short-lived (bash -c,
    a script, another agent's tool runner): such a claim lasts the activity window
    (ACTIVE_S, ten minutes) from its last run whether or not that shell is still
    alive, and later runs from the same terminal session (its sid) count as the
    same controller. Returns who holds it, (holder pid, claude pid); passing that
    back (a refresh during a run) skips looking at the process table."""
    if who is None:
        table = ps_table()
        claude = claude_of(os.getpid(), table)
        who = (claude or os.getppid(), claude)
    holder, claude = who
    started = time.time()
    try:
        started = json.load(open(claim_path(holder))).get("started", started)
    except (OSError, ValueError):
        pass
    if claude:
        write_claim(holder, claude, os.getcwd(), tool, started)
    else:
        write_claim(holder, None, os.getcwd(), tool, started, kind="shell", sid=my_sid())
    return who


def drop_claim(bridge_pid):
    try:
        os.remove(claim_path(bridge_pid))
    except FileNotFoundError:
        pass


def claims():
    """Live claims; files of dead bridges are removed. A shell claim made outside Claude
    Code (kind "shell") is kept for the activity window after its last run even when the
    shell that ran it has exited, and removed after that."""
    out = []
    if not os.path.isdir(OWNERS_DIR):
        return out
    for f in os.listdir(OWNERS_DIR):
        if not f.endswith(".json"):
            continue
        p = os.path.join(OWNERS_DIR, f)
        try:
            rec = json.load(open(p))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        try:
            idle = int(time.time() - float(rec.get("last_call", 0)))
        except (TypeError, ValueError):
            idle = ACTIVE_S
        if not alive(rec.get("bridge_pid")):
            if rec.get("kind") == "shell" and not rec.get("claude_pid") and idle < ACTIVE_S:
                rec["holder_gone"] = True     # its shell exited; the claim holds by its time
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
                continue
        rec["idle_s"] = idle
        rec["active"] = idle < ACTIVE_S
        out.append(rec)
    return out


# --- the user has the computer: a handoff (lib/handoff.py) ---------------------------------------
# Its record sits beside the claims under a name claims() never reads (not *.json), and only lib/handoff.py
# writes it, under the owners lock (and, to open one, the dispatch lock held exclusively). While it's open
# (status "open", before its deadline), others() lists it for every caller, and every acting step looks again
# under dispatch() right before it leaves the Mac, so every acting call waits: the hub's clients and its own
# cleanup, `metacomputer sh`, the vault's fills and the lifecycle commands that check for other controllers.
HANDOFF_FILE = "handoff.state"
HANDOFF_MAX_S = 7200          # the longest time limit; a record that can't be read counts as open this long
HANDOFF_WHY = ("captcha", "login", "2fa", "other")
HANDOFF_STATUS = ("open", "done", "cancelled", "timed_out")
HANDOFF_ID_RE = re.compile(r"h-[0-9a-f]{12}")


def handoff_path():
    return os.path.join(OWNERS_DIR, HANDOFF_FILE)


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def handoff_valid(rec):
    """Every field the guards and their messages use, with its type (a record that isn't one fails closed)."""
    def opt(k, t):
        return rec.get(k) is None or (isinstance(rec.get(k), t) and not isinstance(rec.get(k), bool))
    return (isinstance(rec, dict) and rec.get("v") == 1 and isinstance(rec.get("id"), str)
            and HANDOFF_ID_RE.fullmatch(rec["id"]) is not None and rec.get("status") in HANDOFF_STATUS
            and rec.get("why") in HANDOFF_WHY and _finite(rec.get("opened")) and _finite(rec.get("deadline"))
            and 0 < rec["deadline"] - rec["opened"] <= HANDOFF_MAX_S + 1    # + 1: float rounding of a 2 h limit
            and opt("say", str) and opt("cwd", str) and opt("note", str) and opt("ended_from", str)
            and opt("claude_pid", int) and (rec.get("ended") is None or _finite(rec.get("ended"))))


UNREADABLE_FILE = "handoff-unreadable"     # in RUN_DIR: when the record was first found impossible to look at


def _unreadable_path():
    return os.path.join(os.path.dirname(OWNERS_DIR), UNREADABLE_FILE)


def _first_failure(now):
    """When the record was first found impossible even to look at (no metadata), kept in RUN_DIR so the 2 hours
    count from then for every process that looks; a time there that's missing, not a number or later than now
    starts over from now. If even that can't be written, nothing bounds the state, so it doesn't count at all:
    a time that has already passed (the pause ends rather than renewing itself with every look)."""
    p = _unreadable_path()
    try:
        with open(p) as f:
            t = float(f.read(64).strip())
        if _finite(t) and t <= now:
            return t
    except (OSError, ValueError):
        pass
    try:
        tmp = f"{p}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            f.write(repr(now))
        os.replace(tmp, p)
        return now
    except OSError:
        return now - HANDOFF_MAX_S - 1


def _forget_failure():
    try:
        os.remove(_unreadable_path())
    except OSError:
        pass


def handoff_record():
    """The handoff record as lib/handoff.py wrote it, whatever its status; None when there is none. A file
    that isn't a valid record fails closed, for a bounded time: it reads as an open handoff ("unreadable")
    for HANDOFF_MAX_S from when it last changed (its ctime, which the kernel sets: unlike its modification time,
    a program can't move it into the future), or, when even that can't be had, from when that was first
    noticed (`metacomputer handoff cancel` removes it)."""
    p = handoff_path()
    now = time.time()
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if os.path.lexists(_unreadable_path()):
            _forget_failure()
        return None
    except OSError:
        fd = None                                    # a link, or not ours to read: unreadable
    data, t = b"", None
    try:
        st = os.fstat(fd) if fd is not None else os.lstat(p)
        t = st.st_ctime
        if fd is not None and stat.S_ISREG(st.st_mode) and st.st_size <= 65536:
            data = os.read(fd, 65537)
    except FileNotFoundError:
        return None
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)
    if t is not None and os.path.lexists(_unreadable_path()):
        _forget_failure()
    try:
        rec = json.loads(data.decode("utf-8")) if data else None
    except (UnicodeDecodeError, ValueError):
        rec = None
    if handoff_valid(rec):
        return rec
    t = min(t, now) if _finite(t) else _first_failure(now)
    return {"v": 1, "id": None, "status": "open", "unreadable": True, "why": "other", "opened": t,
            "deadline": t + HANDOFF_MAX_S, "cwd": None, "claude_pid": None}


def open_handoff(now=None):
    """The open handoff (status open, before its deadline), or None."""
    rec = handoff_record()
    now = time.time() if now is None else now
    if rec and rec["status"] == "open" and now < rec["deadline"]:
        return rec
    return None


def fmt_s(s):
    s = max(0, int(s))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def handoff_describe(h, now=None):
    now = time.time() if now is None else now
    left = fmt_s(h["deadline"] - now)
    if h.get("unreadable"):
        return (f"a handoff record that can't be read ({short(handoff_path())}): it counts as an open handoff for "
                f"{left} more; `metacomputer handoff cancel` removes it [ACTIVE]")
    where = f" from {short(h['cwd'])}" if h.get("cwd") else ""
    return (f"the user has the computer: a handoff ({h['why']}) opened {fmt_s(now - h['opened'])} ago{where}, until "
            f"they run `metacomputer handoff done` (or `cancel`), {left} at most [ACTIVE]")


class HandoffOpen(Exception):
    """An acting step found the user holding the computer (rec: the open handoff)."""

    def __init__(self, rec):
        super().__init__("the user has the computer (metacomputer handoff)")
        self.rec = rec


@contextlib.contextmanager
def acting(timeout=DISPATCH_WAIT_S):
    """Wrap the step that sends an action to the VM: the dispatch lock held shared, and HandoffOpen raised
    instead when a handoff is open. TimeoutError (or OSError) when the lock can't be had: don't act then."""
    with dispatch(shared=True, timeout=timeout):
        h = open_handoff()
        if h:
            raise HandoffOpen(h)
        yield


def handoff_refusal(h):
    """What an acting call gets while a handoff is open (the hub, metacomputer sh, the vault's fills)."""
    return ("Paused: " + handoff_describe(h) + ". Acting calls wait until the user hands the computer back; "
            "read-only tools still work. Don't act on the desktop meanwhile, and don't reach it another way "
            "(`metacomputer call`, a script or the shell): every way in waits. If this session ran "
            "`metacomputer handoff`, wait for it to return (`metacomputer handoff wait` if it was stopped); "
            "never run `handoff done` for the user.")


# --- direct connections --------------------------------------------------------

def direct_connections(table):
    """Live `metacomputer mcp` SSH connections not owned by an on-demand bridge."""
    out = []
    for pid, (ppid, comm) in table.items():
        if os.path.basename(comm) != "ssh":
            continue
        cmd = full_command(pid)
        if not MCP_KEY_RE.search(cmd) or "@127.0.0.1" not in cmd or not for_this_vm(cmd):
            continue
        parent = full_command(ppid)
        if "mcp_lazy.py" in parent or "mcp_hub.py" in parent:
            continue  # an on-demand bridge's or the hub's own upstream: its clients are reported through claims
        if "mcp_relay.py" in parent and any(b in full_command(table.get(ppid, (0, ""))[0])
                                            for b in ("mcp_lazy.py", "mcp_hub.py")):
            continue  # the same, through the action-log relay (ACTION_LOG=on)
        chain = ancestors(pid, table)
        who = None
        for p in chain[1:]:
            claude = is_claude(table[p][1])
            if claude or "python" in os.path.basename(table[p][1]):
                who = p
                if claude:
                    break
        who = who or ppid
        out.append({"ssh_pid": pid, "via_pid": who, "via": full_command(who)[:120],
                    "cwd": proc_cwd(who), "chain": chain})
    return out


def others(self_pid=None, table=None, mine=None, my_claude=None, direct=None, sid=None):
    """Controllers other than the caller's own Claude Code session. The hub, which asks for its clients,
    passes what it already knows: the client's ancestors (mine), its Claude Code process, its terminal
    session (sid) and a recent list of direct connections, so no process table is read."""
    self_pid = self_pid or os.getpid()
    if mine is None or direct is None:
        table = table or ps_table()
    if mine is None:
        mine = set(ancestors(self_pid, table))
        my_claude = claude_of(self_pid, table)
    if sid is None:
        sid = my_sid() if self_pid == os.getpid() else sid_of(self_pid)
    found = []
    for c in claims():
        # mine: also a claim held by a shell above the caller (metacomputer sh outside Claude Code)
        if c.get("bridge_pid") == self_pid or c.get("bridge_pid") in mine or (my_claude and c.get("claude_pid") == my_claude):
            continue
        # mine: a shell claim (or an inner Claude started from a shell) from this same terminal session, outside
        # Claude Code (its shell may be gone)
        if (not my_claude and c.get("kind") in ("shell", "inner") and not c.get("claude_pid")
                and c.get("sid") is not None and c.get("sid") == sid):
            continue
        found.append({**c, "kind": "session", "claim": c.get("kind") or "bridge"})
    for d in (direct if direct is not None else direct_connections(table)):
        chain = set(d["chain"])
        if self_pid in chain or (my_claude and my_claude in chain) or (chain & mine - {1}):
            continue
        found.append({"kind": "direct", "active": True, **d})
    # The user has the computer: the same for every caller, the session that handed it over included.
    h = open_handoff()
    if h:
        found.append({"kind": "handoff", "active": True, "handoff_id": h.get("id"), "why": h["why"],
                      "opened": h["opened"], "deadline": h["deadline"], "cwd": h.get("cwd"),
                      "unreadable": bool(h.get("unreadable"))})
    return found


def describe(o):
    if o["kind"] == "handoff":
        return handoff_describe(o)
    if o["kind"] == "session":
        state = "ACTIVE" if o["active"] else "idle"
        if o.get("claim") == "pending":
            src = f"Claude Code session (pid {o['claude_pid']})" if o.get("claude_pid") else f"client (holder pid {o.get('bridge_pid')})"
            return (f"an unfinished {o.get('last_tool')} call from a {src}, still running in the driver, in "
                    f"{short(o.get('cwd'))}: started {o['idle_s'] // 60} min {o['idle_s'] % 60} s ago [ACTIVE]")
        if o.get("claim") == "inner":
            src = (f"a Claude Code session (pid {o['claude_pid']})" if o.get("claude_pid")
                   else f"a shell (terminal session {o.get('sid')})")
            s = o.get("session") or "?"
            return (f"Claude Code inside the VM (metacomputer claude, session {s}), started by {src} in "
                    f"{short(o.get('cwd'))}, held by its keeper (pid {o.get('bridge_pid')}): its screen last changed "
                    f"{o['idle_s'] // 60} min {o['idle_s'] % 60} s ago [{state}]; metacomputer claude stop"
                    f"{'' if s == 'claude' else ' --session ' + s} ends it")
        if o.get("claim") == "lease":
            src = f"Claude Code session (pid {o['claude_pid']})" if o.get("claude_pid") else f"client (pid {o.get('client_pid')})"
            return (f"unfinished call(s) ({o.get('last_tool')}) that a {src} left running when it disconnected, held by "
                    f"the hub (pid {o.get('bridge_pid')}) until they end, in {short(o.get('cwd'))}: "
                    f"{o['idle_s'] // 60} min {o['idle_s'] % 60} s ago [{state}]")
        who = (f"Claude Code session (pid {o['claude_pid']})" if o.get("claude_pid")
               else f"metacomputer sh outside Claude Code (shell pid {o.get('bridge_pid')}"
                    f"{', exited' if o.get('holder_gone') else ''}, terminal session {o.get('sid')})" if o.get("claim") == "shell"
               else f"MCP client outside Claude Code (bridge pid {o.get('bridge_pid')})")
        return (f"{who} in {short(o.get('cwd'))}: last action "
                f"{o['idle_s'] // 60} min {o['idle_s'] % 60} s ago ({o.get('last_tool')}) [{state}]")
    return f"direct connection (ssh pid {o['ssh_pid']}) from {o['via']} in {short(o.get('cwd'))} [ACTIVE]"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "who"
    if cmd == "who":
        table = ps_table()
        me = claude_of(os.getpid(), table)
        all_claims = claims()
        direct = direct_connections(table)
        hub = None
        try:   # the hub's own view: its clients (they act through it), idle time
            import mcp_hub
            hub = mcp_hub.query_status()
        except Exception:
            pass
        handoff = open_handoff()
        if "--json" in argv:
            print(json.dumps({"self_claude_pid": me, "sessions": all_claims, "direct": direct,
                              "active_s": ACTIVE_S, "hub": hub, "handoff": handoff}, indent=2, default=str))
            return 0
        if hub:
            print("  hub: " + mcp_hub.describe_status(hub).replace("\n", "\n  "))
        if handoff:
            print("  " + handoff_describe(handoff))
        if not all_claims and not direct:
            if not handoff:
                print("Nobody is using the virtual computer.")
            return 0
        for c in all_claims:
            tag = "  <- this session" if me and c.get("claude_pid") == me else ""
            print("  " + describe({**c, "kind": "session", "claim": c.get("kind") or "bridge"}) + tag)
        for d in direct:
            print("  " + describe({"kind": "direct", **d}))
        return 0
    if cmd == "check":
        found = others()
        busy = [o for o in found if o["active"]]
        idle = [o for o in found if not o["active"]]
        for o in idle:
            print("note: " + describe(o), file=sys.stderr)
        if busy:
            print("The virtual computer is in use by:", file=sys.stderr)
            for o in busy:
                print("  " + describe(o), file=sys.stderr)
            return 4 if any(o["kind"] == "handoff" for o in busy) else 3   # 4: the user has it (a handoff)
        return 0
    if cmd == "release":
        n = 0
        # Under the owners lock, which the inner Claude's keeper (vm_claude.py) holds to check and renew its claim:
        # a claim dropped here can't be written back by a renewal already under way, and the keeper then exits.
        try:
            with lock():
                for o in others():
                    if o["kind"] == "handoff":
                        print("not released (only the user ends a handoff, with `metacomputer handoff done` or "
                              "`cancel`): " + describe(o))
                        continue
                    if o["kind"] == "session":
                        drop_claim(o["bridge_pid"])
                        print("released: " + describe(o))
                        n += 1
                    else:
                        print("cannot release (stop that process instead): " + describe(o))
        except TimeoutError:
            print("couldn't take the owners lock (another process holds it); nothing was released. Try again.",
                  file=sys.stderr)
            return 1
        if not n:
            print("No other session's claim to release.")
        else:
            try:   # the optional event stream (docs/events.md); never fails the command
                import events
                events.emit("lifecycle", "controller_released",
                            f"{n} other session claim(s) on the virtual computer released (metacomputer release)",
                            data={"released": n}, project=events.caller_project())
            except Exception:
                pass
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
