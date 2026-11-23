#!/usr/bin/env python3
"""The computer an agent session holds, and the guard that keeps the session's commands on it.

An agent session (Claude Code or Codex: lib/owners.py claude_of) reaches a computer two ways: through its MCP tools,
which act on the computer its bridge (`metacomputer mcp-lazy`, lib/mcp_lazy.py) started on, or the one it chose with
`select_computer`; and through the commands it runs in a shell. Each command picks its computer for itself: `--vm
NAME`, MCOMP_VM, MCOMP_CONFIG, else the default computer (config/default-vm), else config/local.env's VM. So a session
can look at one computer through its tools and act on another from the shell: a login shell that reorders PATH runs
another checkout's command (or the main install's instead of a wrapper's), and a default changed since the session
started points elsewhere.

What ties a session to a computer leaves a record in a private folder of this account's (MCOMP_SESSIONS_DIR, else
`metacomputer-sessions` in the account's own temporary folder), one small file each:
  <agent pid>-mcp-<bridge pid>.json   the session's MCP bridge and the computer its tools act on ("selected": chosen
                                       with select_computer)
  <agent pid>-claim-<key>.json        a claim the session holds on a computer (lib/owners.py write_claim): where it is
Each names the session's process and the one holding the computer (the bridge, or the claim's holder) by pid and
start time (proc_start), and counts only while what it stands for is there: those same processes, the bridge still
under that session, the holder's claim still in that computer's owners folder in the session's name. The folder must
be this account's and writable by no one else (it's never changed to make it so), and, when MCOMP_SESSIONS_DIR names
it, must not be or hold a VM's folder, the home folder or the checkout. Only files that are records this module wrote
are ever removed (those whose session has ended, or that no longer stand), through lib/safedelete.py.

The guard (bin/metacomputer, for a command that names no computer): from an agent session that holds a computer, a
command that would act on another one refuses, naming both and how to go on; one that only reads goes ahead with a
note. A session's MCP tools decide (its bridges), else the claims it holds. A command that names its computer
(--vm, MCOMP_VM, MCOMP_CONFIG, a caller that pinned it) isn't checked. It guards against accidents, not a program set
on getting past it; when the process table can't be read, it says so and lets the command go on.

  sessionvm.py guard --vm NAME --ssh-port PORT [--chosen default|main] [--settings LABEL] -- COMMAND [ARGS...]
                     exit 0: go on (with a note on stderr when it only reads another computer); 3: refused (why, on
                     stderr); 2: couldn't tell (why, on stderr)
  sessionvm.py show [--json]         the computers this agent session holds, and how
"""
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import owners  # noqa: E402

LAB = owners.LAB
REFUSED = 3
UNKNOWN = 2
MAX_RECORD = 16384          # bytes read from one record
MAX_RECORDS = 4096          # records looked at in the folder
MAX_CLAIMS = 512            # claim files looked at in one computer's owners folder
NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")      # a VM's name (scripts/lib.sh mc_valid_vm_name)
PORT_RE = re.compile(r"[0-9]{0,5}")
FILE_RE = re.compile(r"([1-9][0-9]{0,9})-(mcp|claim)-([0-9a-f]{1,16})\.json")
KINDS = ("vm", "local", "main", "config")
_CS_DARWIN_USER_TEMP_DIR = 65537

# What each command does to the computer it resolves to (bin/metacomputer's commands). "exempt": it acts on no one
# computer, names its computers itself, is metacomputer's own plumbing, or only reads files and settings on the Mac;
# "read": it only reads the VM or who controls it; "act": anything else. A command whose forms differ is decided from
# its arguments the way bin/metacomputer (or the program it hands them to) parses them, in whatever order they come;
# a form it doesn't know as a read acts. A word that isn't a command is left to bin/metacomputer, which says so.
EXEMPT = {"help", "-h", "--help", "where", "setup", "update", "version", "vm", "list", "default", "events", "notify",
          "learn", "actions", "agents", "each", "mcp", "mcp-lazy", "volume", "refdisk", "contact-sheet",
          "checkpoints", "cloud"}       # checkpoints: the runner's list on the Mac; cloud: a provider's own, no one computer
READS = {"who", "status", "inspect", "logs", "get", "shot"}     # whatever their arguments
ACTS = {"start", "stop", "restart", "reset", "restore", "baseline", "checkpoint", "build", "desktop-restart", "test",
        "call", "web", "put", "rename", "sh", "ssh", "task", "release"}
HELP = ("-h", "--help", "help")


def _first(args):
    return args[0] if args else ""


def _second(args):
    return args[1] if len(args) > 1 else ""


def _security(a):       # do_security: each argument is --json or apply, in any order; apply anywhere applies
    return "read" if all(x == "--json" for x in a) else "act"


def _recordings(a):     # do_recordings: --sync anywhere copies (and then may remove) the VM's task runs; else a list
    return "act" if "--sync" in a else "exempt"


def _input_reset(a):    # do_input_reset: only the first argument counts; --check there checks and removes nothing
    return "read" if _first(a) == "--check" else "act"


def _view(a):           # do_view, any order: --open (and plain view, which opens at a terminal on the Mac's screen)
    if "--open" in a:   # first removes the agent's leftover input devices in the VM (lib/input_reset.py for_view)
        return "act"
    if "--print" in a or "--tailnet" in a or "--tunnel" in a:    # --tunnel: a computer elsewhere's live view, only its tunnel
        return "read"
    return "exempt" if "--forget" in a or "--remember" in a else "act"   # those two only touch the Mac's keychain


def _record(a):         # do_record: the first argument, status when there's none; moments reads a file on the Mac
    return {"status": "read", "moments": "exempt"}.get(_first(a) or "status", "act")


def _browser(a):        # do_browser: none or --json first shows the defaults (settings); profiles alone lists the VM's
    if _first(a) in ("", "--json"):
        return "exempt"
    return "read" if _first(a) == "profiles" and _second(a) == "" else "act"


def _hub(a):            # do_hub: the first argument, status when there's none; env prints the settings
    return {"status": "read", "env": "exempt"}.get(_first(a) or "status", "act")


def _claude(a):         # lib/vm_claude.py: the action is the first argument (start when it's an option or missing)
    return "read" if _first(a) in ("read", "wait", "status") else "exempt" if _first(a) in HELP else "act"


def _handoff(a):        # lib/handoff.py: status and wait, first, only look; anything else opens one or ends it
    return "read" if _first(a) in ("status", "wait") else "exempt" if _first(a) in HELP else "act"


def _net(a):            # lib/netpolicy.py: the subcommand first, status when there's none: what the computer can
    # reach now (its gateway's rules on this Mac; a computer elsewhere: read from it over SSH, and its provider's);
    # egress (a computer elsewhere), allow, remove and clear change it
    return "read" if _first(a) in ("", "status") else "exempt" if _first(a) in HELP else "act"


def _trace(a):          # lib/traces.py: list, show and export read the traces kept on the Mac
    return "exempt" if _first(a) in ("list", "show", "export") + HELP else "act"


# The vault's subcommands (vault/Commands.swift) that only read it, on the Mac, and change nothing; and its nested ones.
SECRETS_READS = ("status", "list", "audit", "tokens", "requests", "selftest", "settle", "version") + HELP
SECRETS_NESTED_READS = (("rule", "list"), ("audit-sink", "list"), ("audit-sink", "show"))


def _secrets(a):        # do_secrets: the vault's subcommand first, status when there's none
    s = _first(a) or "status"
    if s in SECRETS_READS or (s, _second(a)) in SECRETS_NESTED_READS:
        return "exempt"
    return "act"


FORMS = {"security": _security, "recordings": _recordings, "input-reset": _input_reset, "view": _view, "record": _record,
         "browser": _browser, "hub": _hub, "claude": _claude, "handoff": _handoff, "net": _net, "trace": _trace,
         "secrets": _secrets}


def classify(cmd, args=()):
    """"exempt", "read" or "act" for a metacomputer command and its arguments."""
    args = [str(a) for a in args]
    if not cmd or cmd.startswith("_") or cmd in EXEMPT:
        return "exempt"
    if cmd in READS:
        return "read"
    if cmd in FORMS:
        return FORMS[cmd](args)
    if cmd in ACTS:
        return "act"
    return "exempt"


# --- the folder -------------------------------------------------------------------------------------------------
def sessions_dir(env=None):
    """Where the records are: MCOMP_SESSIONS_DIR, else `metacomputer-sessions` in this account's own temporary
    folder (confstr(_CS_DARWIN_USER_TEMP_DIR), the same for every process of the account whatever HOME or TMPDIR say),
    else $XDG_RUNTIME_DIR's, else ~/.cache/metacomputer's."""
    env = os.environ if env is None else env
    d = env.get("MCOMP_SESSIONS_DIR")
    if d:
        return os.path.abspath(os.path.expanduser(d))
    try:
        t = os.confstr(_CS_DARWIN_USER_TEMP_DIR)
    except (ValueError, OSError, TypeError):
        t = None
    if t:
        return os.path.join(os.path.realpath(t), "metacomputer-sessions")
    if env.get("XDG_RUNTIME_DIR"):
        return os.path.join(env["XDG_RUNTIME_DIR"], "metacomputer-sessions")
    return os.path.join(os.path.expanduser("~"), ".cache", "metacomputer", "sessions")


def folder(create=False):
    """The records' folder when it's a private folder of this account's: a real folder (not a link), this account's,
    that no one else can write to; made (0700) with create when it isn't there. Anything else is None, and is left as
    it is (no permission is ever changed on a folder that's already there): nothing is read from it, written to it or
    removed from it."""
    d = sessions_dir()
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            st = os.lstat(d)
        except OSError:
            return None
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        return None
    return d


def store_ok(d):
    """Records are written to (and removed from) a folder MCOMP_SESSIONS_DIR names only when it neither is nor holds a
    folder of a VM's, the account's vault or spools, the home folder or the checkout: lib/safedelete.py's rule, checked
    again for every write (the folder, or a link on the way to it, can change). The default folder, the account's own
    temporary folder's, is made here."""
    if not os.environ.get("MCOMP_SESSIONS_DIR"):
        return True
    try:
        import safedelete
        safedelete.check(d, safedelete.protected())
        return True
    except Exception:
        return False


def _read_json(path, limit=MAX_RECORD):
    """A small JSON file, read without following a link or waiting on a FIFO; None for anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        data = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _open_store(d):
    """The records' folder opened as the folder it is (not following a link), and still the private folder of this
    account's that folder() and store_ok() checked by its name: the same device and inode. A descriptor, or None."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(d, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022 and store_ok(d)):
            now = os.stat(d)
            if (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
                return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _write(name, rec):
    """One record, written whole: a temporary file made in the checked folder through its descriptor, then renamed
    over the record there, so a folder (or a link on the way to it) replaced after the check gets nothing. False when
    it couldn't be."""
    d = folder(create=True)
    dfd = _open_store(d) if d else None
    if dfd is None:
        return False
    tmp = f".rec-{os.getpid()}-{os.urandom(6).hex()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600,
                     dir_fd=dfd)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(rec, f)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            return True
        except (OSError, ValueError, TypeError):
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            return False
    except OSError:
        return False
    finally:
        os.close(dfd)


def _prune(names):
    """Remove records of this module's, by name: only names of its own (FILE_RE) whose content was read and found to
    be a record it wrote, through lib/safedelete.py's remove_records (the folder checked like any delete's target, and
    opened without following a link; each file unlinked through its descriptor). Best effort: a refusal removes
    nothing."""
    d = folder()
    if not d or not names or not store_ok(d):
        return 0
    try:
        import safedelete
        return safedelete.remove_records(d, names, FILE_RE)
    except Exception:
        return 0


# --- processes ----------------------------------------------------------------------------------------------------
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID = 1, 14, 1


def proc_start(pid):
    """When a process started, as the kernel has it ("SECONDS.MICROSECONDS": macOS's kern.proc.pid, Linux's
    /proc/PID/stat), or None when there's no such process. With the pid, it names one process even after the pid is
    reused: a record keeps both for its session and for what holds the computer."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        try:
            import ctypes
            import ctypes.util
            import struct
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid)
            buf = ctypes.create_string_buffer(1024)              # struct kinfo_proc: 648 bytes
            size = ctypes.c_size_t(len(buf))
            if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 16:
                return None                                      # no such process
            sec, usec = struct.unpack_from("<qi", buf.raw, 0)    # kp_proc.p_starttime, a struct timeval, comes first
            return f"{sec}.{usec:06d}"
        except (OSError, AttributeError, ValueError):
            return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            return "jiffies." + f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


# --- computers ----------------------------------------------------------------------------------------------------
def computer_of(env, lab=LAB):
    """The computer a resolved environment (what bin/metacomputer exports: MCOMP_VM_NAME, MCOMP_SSH_PORT,
    MCOMP_RUN_DIR, and MCOMP_CONFIG for a VM chosen by name) acts on, or None without a VM name. kind says how a
    command names it: "vm" (config/vms/NAME.env), "local" (config/local.env), "main" (the checkout's main VM, no
    settings file named), "config" (an MCOMP_CONFIG file elsewhere)."""
    vm = env.get("MCOMP_VM_NAME") or ""
    port = str(env.get("MCOMP_SSH_PORT") or "")
    if not NAME_RE.fullmatch(vm) or not PORT_RE.fullmatch(port):
        return None
    lab = os.path.realpath(lab)
    cfg = env.get("MCOMP_CONFIG") or ""
    cfg = os.path.realpath(cfg) if cfg else ""
    if cfg and os.path.dirname(cfg) == os.path.realpath(os.path.join(lab, "config", "vms")):
        kind = "vm"
    elif cfg and cfg == os.path.realpath(os.path.join(lab, "config", "local.env")):
        kind = "local"
    elif cfg:
        kind = "config"
    else:
        kind = "main"
    run = env.get("MCOMP_RUN_DIR") or os.path.join(lab, ".run")
    return {"vm": vm, "ssh_port": port, "run_dir": os.path.realpath(run), "lab": lab, "config": cfg, "kind": kind}


def key(c):
    """Two records are the same computer when they name the same VM at the same SSH port (two checkouts can reach one
    VM; its folders on the Mac are each checkout's own)."""
    return (c.get("vm"), str(c.get("ssh_port") or ""))


def _pid(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _valid(rec, kind):
    """A record this module wrote, of that kind: every field the guard and its words use, with its type."""
    c = rec.get("computer") if isinstance(rec, dict) else None
    return (isinstance(rec, dict) and rec.get("v") == 1 and rec.get("kind") == kind and _pid(rec.get("agent_pid"))
            and _pid(rec.get("holder_pid")) and isinstance(rec.get("agent_start"), str)
            and isinstance(rec.get("holder_start"), str)
            and isinstance(c, dict) and isinstance(c.get("vm"), str) and NAME_RE.fullmatch(c["vm"]) is not None
            and isinstance(c.get("ssh_port"), str) and PORT_RE.fullmatch(c["ssh_port"]) is not None
            and all(isinstance(c.get(k), str) for k in ("run_dir", "lab", "config")) and c.get("kind") in KINDS
            and os.path.isabs(c["run_dir"]) and os.path.isabs(c["lab"])
            and (kind != "mcp" or isinstance(rec.get("selected"), bool)))


# --- writing records ----------------------------------------------------------------------------------------------
def bridge_name(agent_pid, bridge_pid):
    return f"{int(agent_pid)}-mcp-{int(bridge_pid)}.json"


def claim_name(agent_pid, holder_pid, run_dir):
    digest = hashlib.sha256((run_dir + "\0" + str(int(holder_pid))).encode()).hexdigest()[:12]
    return f"{int(agent_pid)}-claim-{digest}.json"


def note_bridge(agent_pid, bridge_pid, computer, selected=False, agent=None):
    """An agent session's MCP bridge acts on computer (selected: chosen with select_computer). The record names both
    processes by pid and start time (proc_start). Best effort."""
    if not agent_pid or not bridge_pid or not computer:
        return False
    a_start, b_start = proc_start(agent_pid), proc_start(bridge_pid)
    if not a_start or not b_start:
        return False
    rec = {"v": 1, "kind": "mcp", "agent_pid": int(agent_pid), "agent_start": a_start, "agent": agent,
           "holder_pid": int(bridge_pid), "holder_start": b_start, "selected": bool(selected), "since": time.time(),
           "computer": computer}
    return _write(bridge_name(agent_pid, bridge_pid), rec)


def drop_bridge(agent_pid, bridge_pid):
    """The bridge's own record goes when it ends: only when it's that bridge's (its pid and start time)."""
    d = folder()
    if not (d and agent_pid and bridge_pid):
        return
    name = bridge_name(agent_pid, bridge_pid)
    rec = _read_json(os.path.join(d, name))
    if _valid(rec, "mcp") and rec["holder_pid"] == int(bridge_pid) and rec["holder_start"] == proc_start(bridge_pid):
        _prune([name])


def note_claim(agent_pid, holder_pid, run_dir, agent_start, env=None):
    """An agent session claimed the computer whose owners folder is in run_dir, in holder_pid's name (lib/owners.py
    write_claim, on every claim: a record that's there for these same processes isn't written again). agent_start is
    the session's start time as the claim has it, from when its holder took it (a lease's refresh keeps the one it
    had); never the start of whatever process has that pid now. A claim without one can't be shown to be any
    session's: no record. Best effort; never raises."""
    try:
        env = os.environ if env is None else env
        comp = computer_of(dict(env, MCOMP_RUN_DIR=run_dir))
        if not comp or not agent_pid or not holder_pid or not isinstance(agent_start, str) or not agent_start:
            return False
        a_start, h_start = agent_start, proc_start(holder_pid)
        if not a_start or not h_start:
            return False
        name = claim_name(agent_pid, holder_pid, comp["run_dir"])
        d = folder()
        old = _read_json(os.path.join(d, name)) if d else None
        if _valid(old, "claim") and (old["agent_start"], old["holder_start"]) == (a_start, h_start):
            return True
        return _write(name, {"v": 1, "kind": "claim", "agent_pid": int(agent_pid), "agent_start": a_start,
                             "holder_pid": int(holder_pid), "holder_start": h_start, "since": time.time(),
                             "computer": comp})
    except Exception:
        return False


# --- reading them back --------------------------------------------------------------------------------------------
def _session_claim(run_dir, agent, holder, agent_start):
    """That holder's claim in that computer's owners folder, when it's in the agent session's name: its pid and its
    start time. A claim without a start time (one written before they were kept, or whose session's couldn't be read)
    can't be shown to be this session's, so it isn't one. None otherwise."""
    rec = _read_json(os.path.join(run_dir, "owners", f"{int(holder)}.json"), 65536)
    if (isinstance(rec, dict) and rec.get("claude_pid") == agent and rec.get("bridge_pid") == holder
            and isinstance(agent_start, str) and agent_start and rec.get("claude_start") == agent_start):
        return rec
    return None


def _stands(rec, agent, table):
    """Whether what a record of the agent session's stands for is still there: the same processes (pid and start
    time), the bridge still under the session, or the holder's claim still in the session's name."""
    h = rec["holder_pid"]
    if h not in table or proc_start(h) != rec["holder_start"]:
        return False
    if rec["kind"] == "mcp":
        return agent in owners.ancestors(h, table)
    claim = _session_claim(rec["computer"]["run_dir"], agent, h, rec["agent_start"])
    if claim is None:
        return False
    rec["last_tool"] = str(claim.get("last_tool") or "")[:40]
    return True


def held(agent, table, prune=True):
    """The records of the agent session's that stand: its live bridges and the claims it still holds. Only files that
    are records this module wrote count, and only those are ever removed (prune): the session's own that no longer
    stand, and other sessions' whose session has ended (its pid gone, or another process's now)."""
    d = folder()
    if not d:
        return []
    try:
        names = sorted(os.listdir(d))[:MAX_RECORDS]
    except OSError:
        return []
    me = proc_start(agent)
    out, stale = [], []
    for n in names:
        m = FILE_RE.fullmatch(n)
        if not m:
            continue
        rec = _read_json(os.path.join(d, n))
        pid = int(m.group(1))
        if not _valid(rec, m.group(2)) or rec["agent_pid"] != pid:
            continue                                # not a record of this module's: read no further, never removed
        if m.group(2) == "mcp" and n != bridge_name(pid, rec["holder_pid"]):
            continue
        if pid != agent:
            if pid not in table or proc_start(pid) != rec["agent_start"]:
                stale.append(n)                     # that session has ended
            continue
        if rec["agent_start"] == me and _stands(rec, agent, table):
            out.append(rec)
        else:
            stale.append(n)
    if prune and stale:
        _prune(stale)
    return out


def conflicts(target, recs):
    """The records that make the session's computer something other than target ([] when it may act there): its MCP
    bridges when it has any (the computer its tools act on), else the claims it holds."""
    t = key(target)
    bridges = [r for r in recs if r["kind"] == "mcp"]
    if bridges:
        return [] if {key(r["computer"]) for r in bridges} == {t} else bridges
    claims = [r for r in recs if r["kind"] == "claim"]
    if claims and {key(r["computer"]) for r in claims} != {t}:
        return claims
    return []


# --- what to say --------------------------------------------------------------------------------------------------
def cli_of(lab):
    """How to name a checkout's command: ~/.local/bin/metacomputer when it is that checkout's, else the path."""
    own = os.path.join(lab, "bin", "metacomputer")
    link = os.path.expanduser("~/.local/bin/metacomputer")
    try:
        if os.path.exists(link) and os.path.realpath(link) == os.path.realpath(own):
            return "~/.local/bin/metacomputer"
    except OSError:
        pass
    return shlex.quote(own)


def naming(comp):
    """The start of a command that acts on comp, run from this checkout."""
    if comp["lab"] == os.path.realpath(LAB) and comp["kind"] != "config":
        return f"{cli_of(LAB)} --vm {comp['vm']}"
    if comp["config"]:
        return f"MCOMP_CONFIG={shlex.quote(comp['config'])} {cli_of(comp['lab'])}"
    return f"{cli_of(comp['lab'])} --vm {comp['vm']}"


def _unique(recs):
    seen, out = set(), []
    for r in recs:
        if key(r["computer"]) not in seen:
            seen.add(key(r["computer"]))
            out.append(r)
    return out


def _names(recs):
    names = [r["computer"]["vm"] for r in _unique(recs)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def holding(recs, agent_name):
    """"this Codex session's MCP tools act on demo" and the like."""
    who = f"this {agent_name or 'agent'} session"
    if recs and recs[0]["kind"] == "mcp":
        if all(r["selected"] for r in recs):
            return f"{who} selected {_names(recs)} for its MCP tools (select_computer)"
        return f"{who}'s MCP tools act on {_names(recs)}"
    tools = sorted({r.get("last_tool") for r in recs if r.get("last_tool")})
    return f"{who} holds {_names(recs)}" + (f" (it acted there: {', '.join(tools)})" if tools else "")


def chosen_as(target, chosen, settings):
    if chosen == "default":
        return "your default computer, config/default-vm"
    return f"{settings or 'the main VM'}'s VM, as the command names none"


def refusal(recs, target, cmd, chosen, settings, agent_name):
    tv = target["vm"]
    mine = _unique(recs)
    lines = [f"metacomputer: {holding(recs, agent_name)}, but this command would act on {tv} "
             f"({chosen_as(target, chosen, settings)}). Nothing was done.",
             f"- To act on {tv} anyway, name it: {cli_of(LAB)} --vm {tv} {cmd} ..."]
    for r in mine:
        lines.append(f"- To act on {r['computer']['vm']}, this session's computer: {naming(r['computer'])} {cmd} ...")
    lines.append(f"- Or move this session to {tv}: the metacomputer MCP tool select_computer with name {tv}; then "
                 f"commands that name no computer act on it.")
    return "\n".join(lines)


def note(recs, target, cmd, chosen, settings, agent_name):
    mine = _unique(recs)
    return (f"metacomputer: note: {holding(recs, agent_name)}; this reads {target['vm']} "
            f"({chosen_as(target, chosen, settings)}). For {mine[0]['computer']['vm']}: "
            f"{naming(mine[0]['computer'])} {cmd}")


# --- the commands -------------------------------------------------------------------------------------------------
def _has_records():
    d = folder()
    if not d:
        return False
    try:
        with os.scandir(d) as it:
            return any(FILE_RE.fullmatch(e.name) for e in it)
    except OSError:
        return False


def guard(argv):
    vm = port = chosen = settings = None
    rest = []
    it = iter(argv)
    for a in it:
        if a == "--":
            rest = list(it)
            break
        if a in ("--vm", "--ssh-port", "--chosen", "--settings"):
            v = next(it, None)
            if v is None:
                break
            if a == "--vm":
                vm = v
            elif a == "--ssh-port":
                port = v
            elif a == "--chosen":
                chosen = v
            else:
                settings = v
        else:
            print(f"sessionvm.py guard: unknown argument {a!r}", file=sys.stderr)
            return UNKNOWN
    if not vm or not NAME_RE.fullmatch(vm) or not rest:
        print(__doc__, file=sys.stderr)
        return UNKNOWN
    cmd, args = rest[0], rest[1:]
    action = classify(cmd, args)
    if action == "exempt":
        return 0
    if os.path.lexists(sessions_dir()) and folder() is None:
        print(f"metacomputer: {sessions_dir()}, the folder of the agent sessions' records, isn't a private folder of "
              "this account's (a folder of its own that no one else can write to), so it isn't used and this command "
              "isn't checked against the computer this session holds; going on", file=sys.stderr)
        return UNKNOWN
    if not _has_records():
        return 0
    try:
        table = owners.ps_table(strict=True)
    except OSError as e:
        print(f"metacomputer: couldn't read the process table ({e}), so it can't tell which computer this agent "
              "session holds; going on", file=sys.stderr)
        return UNKNOWN
    agent = owners.claude_of(os.getpid(), table)
    if not agent:
        return 0
    recs = held(agent, table)
    target = {"vm": vm, "ssh_port": str(port or "")}
    clash = conflicts(target, recs)
    if not clash:
        return 0
    name = owners.agent_kind(table.get(agent, (0, ""))[1])
    if action == "read":
        print(note(clash, target, cmd, chosen, settings, name), file=sys.stderr)
        return 0
    print(refusal(clash, target, cmd, chosen, settings, name), file=sys.stderr)
    return REFUSED


def show(argv):
    try:
        table = owners.ps_table(strict=True)
    except OSError as e:
        print(f"couldn't read the process table: {e}", file=sys.stderr)
        return 1
    agent = owners.claude_of(os.getpid(), table)
    recs = held(agent, table) if agent else []
    if "--json" in argv:
        print(json.dumps({"agent_pid": agent, "records": recs, "folder": sessions_dir()}, indent=2))
        return 0
    if not agent:
        print("Not run from an agent session (Claude Code or Codex).")
        return 0
    if not recs:
        print("This agent session holds no computer yet.")
        return 0
    name = owners.agent_kind(table.get(agent, (0, ""))[1])
    for part in ([r for r in recs if r["kind"] == "mcp"], [r for r in recs if r["kind"] == "claim"]):
        if part:
            text = holding(part, name)
            print(text[0].upper() + text[1:] + ".")
    return 0


def main(argv):
    if argv[:1] == ["guard"]:
        return guard(argv[1:])
    if argv[:1] == ["show"]:
        return show(argv[1:])
    print(__doc__)
    return UNKNOWN


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
