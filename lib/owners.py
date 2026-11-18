"""Who is using the virtual computer? One controller at a time, no daemon.

Two kinds of controller can drive the VM's desktop:

- On-demand bridges (`metacomputer mcp-lazy`, what Claude Code sessions use). Each
  one that has acted keeps .run/owners/<bridge-pid>.json up to date: its Claude
  Code process, the project folder, the time and name of its last tool call.
- Direct connections (`metacomputer mcp`: the GUI tests, mcp_client.py, older
  configs). They are found as live SSH processes using the MCP key.

The shell channel (`metacomputer sh`) acts too: each run (except --read-only)
keeps .run/owners/<pid>.json up to date, where pid is its Claude Code process,
or outside Claude Code the shell that ran it, since the command itself exits.

"Self" is the caller's own Claude Code session (its nearest `claude` ancestor),
or a shell it runs under, so a session never blocks itself.

CLI (used by bin/metacomputer):
  owners.py who [--json]      list controllers
  owners.py check             exit 3 (and explain) if another controller is active
  owners.py release           forget other sessions' claims (after the user agrees)
"""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time

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


def claude_of(pid, table):
    """Nearest Claude Code process above pid (None outside Claude Code)."""
    for p in ancestors(pid, table)[1:]:
        if os.path.basename(table[p][1]) == "claude":
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
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another process holds the owners lock")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)   # releases the lock


def write_claim(bridge_pid, claude_pid, cwd, tool, started):
    os.makedirs(OWNERS_DIR, exist_ok=True)
    rec = {"bridge_pid": bridge_pid, "claude_pid": claude_pid, "cwd": cwd, "started": started,
           "last_call": time.time(), "last_tool": tool}
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


def claim_for_shell(tool):
    """`metacomputer sh` acted. Its process ends with the run, so the claim lives
    as long as the caller's Claude Code session, or else the shell that ran it."""
    table = ps_table()
    claude = claude_of(os.getpid(), table)
    holder = claude or os.getppid()
    started = time.time()
    try:
        started = json.load(open(claim_path(holder))).get("started", started)
    except (OSError, ValueError):
        pass
    write_claim(holder, claude, os.getcwd(), tool, started)


def drop_claim(bridge_pid):
    try:
        os.remove(claim_path(bridge_pid))
    except FileNotFoundError:
        pass


def claims():
    """Live claims; files of dead bridges are removed."""
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
        if not alive(rec.get("bridge_pid")):
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        rec["idle_s"] = int(time.time() - rec.get("last_call", 0))
        rec["active"] = rec["idle_s"] < ACTIVE_S
        out.append(rec)
    return out


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
        if "mcp_lazy.py" in full_command(ppid):
            continue  # an on-demand bridge's own upstream: reported through its claim
        chain = ancestors(pid, table)
        who = None
        for p in chain[1:]:
            base = os.path.basename(table[p][1])
            if base == "claude" or "python" in base:
                who = p
                if base == "claude":
                    break
        who = who or ppid
        out.append({"ssh_pid": pid, "via_pid": who, "via": full_command(who)[:120],
                    "cwd": proc_cwd(who), "chain": chain})
    return out


def others(self_pid=None, table=None):
    """Controllers other than the caller's own Claude Code session."""
    table = table or ps_table()
    self_pid = self_pid or os.getpid()
    mine = set(ancestors(self_pid, table))
    my_claude = claude_of(self_pid, table)
    found = []
    for c in claims():
        # mine: also a claim held by a shell above the caller (metacomputer sh outside Claude Code)
        if c.get("bridge_pid") == self_pid or c.get("bridge_pid") in mine or (my_claude and c.get("claude_pid") == my_claude):
            continue
        found.append({"kind": "session", **c})
    for d in direct_connections(table):
        chain = set(d["chain"])
        if self_pid in chain or (my_claude and my_claude in chain) or (chain & mine - {1}):
            continue
        found.append({"kind": "direct", "active": True, **d})
    return found


def describe(o):
    if o["kind"] == "session":
        state = "ACTIVE" if o["active"] else "idle"
        who = (f"Claude Code session (pid {o['claude_pid']})" if o.get("claude_pid")
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
        if "--json" in argv:
            print(json.dumps({"self_claude_pid": me, "sessions": all_claims, "direct": direct,
                              "active_s": ACTIVE_S}, indent=2, default=str))
            return 0
        if not all_claims and not direct:
            print("Nobody is using the virtual computer.")
            return 0
        for c in all_claims:
            tag = "  <- this session" if me and c.get("claude_pid") == me else ""
            print("  " + describe({"kind": "session", **c}) + tag)
        for d in direct:
            print("  " + describe({"kind": "direct", **d}))
        return 0
    if cmd == "check":
        busy = [o for o in others() if o["active"]]
        idle = [o for o in others() if not o["active"]]
        for o in idle:
            print("note: " + describe(o), file=sys.stderr)
        if busy:
            print("The virtual computer is in use by:", file=sys.stderr)
            for o in busy:
                print("  " + describe(o), file=sys.stderr)
            return 3
        return 0
    if cmd == "release":
        n = 0
        for o in others():
            if o["kind"] == "session":
                drop_claim(o["bridge_pid"])
                print("released: " + describe(o))
                n += 1
            else:
                print("cannot release (stop that process instead): " + describe(o))
        if not n:
            print("No other session's claim to release.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
