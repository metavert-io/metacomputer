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

"Self" is the caller's own agent session (its nearest agent ancestor: Claude Code, Codex,
OpenClaw, Hermes Agent, Gemini CLI or Antigravity CLI; agent_of), or a shell it runs under, so a session never
blocks itself.

A handoff (lib/handoff.py, `metacomputer handoff`) gives the computer to the user:
while one is open, others() lists it for every caller, its own session included,
so every acting call waits until the user hands the computer back. Its record,
.run/owners/handoff.state, is written under the owners lock; `release` never drops it.

CLI (used by bin/metacomputer):
  owners.py who [--json]      list controllers
  owners.py check             exit 3 (and explain) if another controller is active; 4 while the user has it (a handoff)
  owners.py release           forget other sessions' claims (after the user agrees)
  owners.py at-terminal [--stderr]   exit 0 when stdin (and stderr) is the user's own terminal (at_user_terminal)
  owners.py internal-caller [--user] [cli:SUB|script:NAME...]   exit 0 when bin/metacomputer was started by one of
                              them (internal_caller), or, with --user, at the user's own terminal
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
import procinfo  # noqa: E402  (ps, or the kernel where ps can't start)
from agentkit import manifests as _manifests  # noqa: E402  (the agents' words)
from agentkit import recognition as _recognition  # noqa: E402  (every agent's recognition.json, merged)

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# bin/metacomputer passes this VM's RUN_DIR (config/defaults.env), so two VMs keep separate owners.
OWNERS_DIR = os.path.join(os.environ.get("MCOMP_RUN_DIR") or os.path.join(LAB, ".run"), "owners")
ACTIVE_S = int(os.environ.get("MCOMP_ACTIVE_S", "600"))  # acted within 10 min = in use
# the bridge's SSH key is <SSH_DIR>/mcp (SSH_DIR is configurable), to the loopback forward
MCP_KEY_RE = re.compile(r"IdentityFile=\S*/mcp(?:\s|$)")
# This VM's loopback SSH port (bin/metacomputer passes SSH_PORT). With two VMs side by side,
# a connection to the other VM's port is not an owner of this one.
SSH_PORT = os.environ.get("MCOMP_SSH_PORT", "")
# A computer elsewhere (scripts/lib.sh mc_hostkey_name): its connections carry HostKeyAlias=mcomp-NAME.
HOSTKEY_ALIAS = os.environ.get("MCOMP_HOSTKEY_NAME", "") if os.environ.get("MCOMP_HOSTKEY_NAME", "").startswith("mcomp-") else ""


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


def ps_table(strict=False):
    """{pid: (ppid, comm)} for every process (a ProcTable: an interpreter's arguments on request), as `/bin/ps -axo
    pid=,ppid=,comm=` lists them; where ps can't start (macOS's sandbox runs no setuid program), the same from the
    kernel (lib/procinfo.py), with the processes whose name the kernel can't give exactly as ps would listed as inexact
    (agent_of counts them as UNKNOWN_AGENT). strict: OSError when ps fails or lists nothing, instead of a table that
    would look like one without agents."""
    out, rc, inexact = procinfo.table()
    if strict and (rc != 0 or not out.strip()):
        raise OSError(f"ps failed (exit {rc})")
    table = ProcTable(live=True, inexact=inexact)
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2:
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def full_command(pid):
    """A process's command line as `/bin/ps -ww -o command=` prints it ("" for no such process, or when it can't be
    told: where ps can't start, another account's process or arguments beyond ASCII; lib/procinfo.py)."""
    return (procinfo.field(pid, "command") or "").strip()


# A process's exact arguments, executable and working folder: lib/procinfo.py reads them from the kernel.
proc_argv = procinfo.argv
proc_exe = procinfo.exe
proc_cwd = procinfo.cwd


class ProcTable(dict):
    """ps_table's {pid: (ppid, comm)}, and what agent_of reads of a process when asked, kept with the table: its
    executable (exe(): the realpath of its text image), its arguments (argv()) and its working folder (cwd()). An agent
    that runs natively is told by its executable; one that runs on an interpreter by the program it runs. live: read
    what isn't known yet from the
    process itself (ps_table's tables; None when it can't be read); a table made of known parts (the tests') never does:
    argvs, exes and cwds map a pid to what's known, None for what couldn't be read (an argv not given is [], an exe or
    cwd not given None). inexact: the pids whose name (comm) the table can't give as ps would (a table read from the
    kernel where ps can't start: lib/procinfo.py): agent_of counts each as UNKNOWN_AGENT."""

    def __init__(self, *args, argvs=None, exes=None, cwds=None, live=False, inexact=(), **kw):
        super().__init__(*args, **kw)
        self.argvs = dict(argvs or {})
        self.exes = dict(exes or {})
        self.cwds = dict(cwds or {})
        self.live = live
        self.inexact = frozenset(inexact)

    def _get(self, cache, pid, read, missing):
        if pid not in cache:
            cache[pid] = read(pid) if self.live and pid in self else (None if self.live else missing)
        return cache[pid]

    def argv(self, pid):
        return self._get(self.argvs, pid, proc_argv, [])

    def exe(self, pid):
        return self._get(self.exes, pid, proc_exe, None)

    def cwd(self, pid):
        return self._get(self.cwds, pid, proc_cwd, None)


def ancestors(pid, table):
    chain, seen = [], set()
    while pid in table and pid not in seen and pid > 1:
        seen.add(pid)
        chain.append(pid)
        pid = table[pid][0]
    return chain


# Agent sessions: the coding agent a command or bridge runs under. Its process owns the claims of everything it
# starts (bridge, `metacomputer call`, `sh`), so a session never blocks itself. How each agent's processes are told is
# its folder's (coding-agents/<name>/recognition.json, merged by lib/agentkit/recognition.py; the vault's build embeds the
# same tables): this file holds only the rules that read them.
_TABLES = _recognition.tables()
# A process by its own executable's file name (ps comm=, or its executable's path), never a script an interpreter runs,
# or a version file of its installer's (.../FOLDER/1.2.3).
EXECUTABLES = _TABLES["executables"]
VERSION_FILES = tuple((re.compile("/" + re.escape(folder) + r"/\d+\.\d+\.\d+[^/]*$"), kind)
                      for folder, kind in _TABLES["version_folders"])


def executable_agent(comm):
    """The agent a process table command (ps comm=) or an executable's path is by its own executable (EXECUTABLES,
    VERSION_FILES), else None."""
    got = EXECUTABLES.get(os.path.basename(comm or ""))
    if got:
        return got
    return next((kind for pat, kind in VERSION_FILES if pat.search(comm or "")), None)


# Every agent session metacomputer knows, by the name its messages, claims, events and handoffs use. UNKNOWN_AGENT is an
# interpreter (node, python) that can't be told: its arguments or its working folder couldn't be read, or the script it
# runs can't be found. It may be an agent, so it counts as one (fail closed): commands only the user runs refuse under
# it, and the session guard, claims and the vault treat it as a session.
UNKNOWN_AGENT = "unidentified agent"
AGENTS = _TABLES["kinds"] + (UNKNOWN_AGENT,)
# The agent a record without an "agent" field names (claims, handoffs and the vault's records are older than the other
# agents' support): the registry's DEFAULT, by name (never a place in its list), as the vault's build reads it too.
RECORD_DEFAULT_AGENT = _recognition.record_default()
# An interpreter's process (ps comm= is its program, often by its full path): the agent is told by what it runs.
_INTERPRETER = re.compile(r"(node|nodejs|bun|deno|python[0-9.]*|Python)")
# The agents' programs, by file name: a native executable, the name a process gives itself (a title), or the script an
# interpreter runs. Names are matched exactly.
AGENT_NAMES = _TABLES["names"]
# An app whose agent runs inside the app: its executables by their paths (app_agent).
AGENT_APPS = tuple(dict(a, bundle=re.compile(a["bundle"]), helper=re.compile(a["helper"]),
                        folders=tuple(a["folders"])) for a in _TABLES["apps"])
# ...and by a word in a script's path.
AGENT_PATH_WORDS = _TABLES["path_words"]
# python -m: the module's package.
AGENT_MODULES = _TABLES["modules"]
# The agents' packages, by identifiers specific to them (never a bare name, the agent's command alone): found in any argument of an
# interpreter, or in what an argument naming an existing file resolves to, they make it that agent (package_agent).
AGENT_PACKAGE_WORDS = _TABLES["package_words"]
# A folder of that name in a path, unless the agent's own helpers' path is below it (they only relay between the
# agent and a child it runs, an MCP server: the agent above them is the session).
AGENT_PACKAGE_FOLDERS = _TABLES["package_folders"]
# A package a preload names (node --require/--import PKG): its name.
AGENT_PACKAGES = _TABLES["packages"]
# The interpreters' options that take the next argument as their value (an unknown option takes none: it never moves
# the script's place), those that preload a module, and those whose value is the program.
NODE_VALUE_FLAGS = frozenset((
    "-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions", "--input-type", "--title",
    "--env-file", "--env-file-if-exists", "--watch-path", "--redirect-warnings", "--secure-heap", "--secure-heap-min",
    "--openssl-config", "--icu-data-dir", "--diagnostic-dir", "--report-dir", "--report-directory", "--report-filename",
    "--report-signal", "--cpu-prof-dir", "--cpu-prof-name", "--cpu-prof-interval", "--heap-prof-dir",
    "--heap-prof-name", "--heap-prof-interval", "--disable-warning", "--experimental-config-file", "--inspect-port",
    "--debug-port", "--max-http-header-size", "--experimental-sea-config", "--test-name-pattern", "--test-reporter",
    "--test-reporter-destination", "--localstorage-file", "--trace-event-categories", "--trace-event-file-pattern",
    "--tls-cipher-list", "--tls-keylog", "--use-largepages", "--watch-kill-signal", "--dns-result-order",
    "--disable-proto", "--unhandled-rejections", "--experimental-default-type", "--stack-size", "--max-old-space-size",
    "--max-semi-space-size",
    # bun
    "--preload", "--cwd", "--config", "--tsconfig-override", "-d", "--define", "-l", "--main-fields", "--extension-order"))
NODE_PRELOAD_FLAGS = frozenset(("-r", "--require", "--import", "--loader", "--experimental-loader", "--preload"))
NODE_CODE_FLAGS = frozenset(("-e", "--eval", "-p", "--print", "--run"))      # --run: a package.json script, by name
DENO_VALUE_FLAGS = frozenset(("-c", "--config", "--import-map", "--location", "--seed", "--cert", "--lock", "--ext",
                              "-L", "--log-level"))
PYTHON_VALUE_FLAGS = frozenset(("--check-hash-based-pycs",))
# bun's and deno's commands: after these comes the script (deno eval: code); any other runs no script of the agents'.
BUN_COMMANDS = frozenset(("run", "x", "exec", "test", "build", "install", "i", "add", "remove", "rm", "update",
                          "create", "init", "pm", "link", "unlink", "upgrade", "repl", "outdated", "publish", "patch"))
DENO_COMMANDS = frozenset(("run", "serve", "test", "bench", "check", "compile", "eval", "repl", "task", "install",
                           "uninstall", "fmt", "lint", "info", "doc", "upgrade", "types", "completions", "coverage",
                           "init", "jupyter", "publish", "add", "remove", "outdated", "clean", "lsp", "vendor"))
SCRIPT_COMMANDS = frozenset(("run", "serve", "test", "bench", "check", "compile"))


def is_interpreter(comm):
    return bool(_INTERPRETER.fullmatch(os.path.basename(comm or "").strip("()")))


def _interp_kind(name):
    base = os.path.basename(name or "").strip("()").lower()
    return "python" if base.startswith("python") else (base if base in ("bun", "deno") else "node")


def name_agent(path):
    """The agent a program is by its file name (AGENT_NAMES), else None."""
    return AGENT_NAMES.get(os.path.basename(path or ""))


def app_folder(path, prefixes):
    """Whether a folder on the path is an app's own: a component that is or starts with one of prefixes, case aside
    (Name.app, name-ide, ~/.name), never the word inside another name (not-name)."""
    return any(c.lower().startswith(prefixes) for c in (path or "").split("/"))


def app_agent(path):
    """The agent whose app an executable is (AGENT_APPS: in the app's bundle, or a helper of its in a folder of its
    own), else None."""
    p = path or ""
    for app in AGENT_APPS:
        if app["bundle"].search(p):
            return app["kind"]
        if app["helper"].search(os.path.basename(p)) and app_folder(os.path.dirname(p), app["folders"]):
            return app["kind"]
    return None


def path_agent(path):
    """The agent whose entry point a script's path is: by its file name, or a word in it (AGENT_PATH_WORDS)."""
    return name_agent(path) or next((a for w, a in AGENT_PATH_WORDS if w in (path or "")), None)


def package_agent(text, folder=False):
    """The agent whose package a text names, else None: an identifier of AGENT_PACKAGE_WORDS anywhere in it, or a folder
    of AGENT_PACKAGE_FOLDERS in it as a path (a part followed by `/`; the last part too when folder: the text is a
    folder's path), except a path to that agent's own helpers (its helpers' components right below that folder:
    wherever the agent is installed, even under another folder of that name)."""
    text = text or ""
    for w, a in AGENT_PACKAGE_WORDS:
        if w in text:
            return a
    parts = text.split("/")
    folders = parts if folder else parts[:-1]
    for name, a, helpers in AGENT_PACKAGE_FOLDERS:
        if name not in folders:
            continue
        helper = bool(helpers) and any(p == name and tuple(parts[i + 1:i + 1 + len(helpers)]) == helpers
                                       for i, p in enumerate(parts))
        if not helper:
            return a
    return None


_UNREADABLE = object()          # a relative path, and the process's working folder couldn't be read


def _resolve(arg, cwd, kind):
    """The file an interpreter's argument names, resolved against the process's own working folder: its path, None
    when there's none (node and bun also try .js, .mjs and .cjs, as node does), or _UNREADABLE."""
    if arg.startswith("/"):
        path = arg
    else:
        where = cwd()
        if not where:
            return _UNREADABLE
        path = os.path.join(where, arg)
    for p in (path,) + (() if kind in ("python", "exact") else (path + ".js", path + ".mjs", path + ".cjs")):
        try:
            if os.path.exists(p):
                return p
        except (OSError, ValueError):
            return _UNREADABLE
    return None


def _real(path):
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _file_agent(path):
    """The agent a script file is, as written and as it resolves (a link to it)."""
    real = _real(path)
    return path_agent(path) or path_agent(real) or package_agent(real, os.path.isdir(real))


def _package_rule(args, cwd):
    """An interpreter's arguments (and the values after an option's `=`) naming an agent's package, as written or as
    what one naming an existing file resolves to (against the process's working folder; none when it can't be read)."""
    for a in args:
        for v in (a, a.partition("=")[2]) if "=" in a else (a,):
            if not v:
                continue
            got = package_agent(v)
            if got:
                return got
            if v.startswith("-") or len(v) > 4096:
                continue
            res = _resolve(v, cwd, "exact")
            if res and res is not _UNREADABLE:
                real = _real(res)
                got = package_agent(real, os.path.isdir(real))
                if got:
                    return got
    return None


def _preload_agent(spec, cwd, kind):
    """A preload (node --require/--import SPEC): a file it names (an agent's script), or a package (AGENT_PACKAGES)."""
    res = _resolve(spec, cwd, kind) if spec.startswith((".", "/")) else None
    if res and res is not _UNREADABLE:
        return _file_agent(res)
    parts = spec.split("/")
    return AGENT_PACKAGES.get("/".join(parts[:2]) if spec.startswith("@") else parts[0])


def _script_at(arg, cwd, kind):
    res = _resolve(arg, cwd, kind)
    if res is _UNREADABLE or res is None:
        return UNKNOWN_AGENT                    # the script's place, and no file there (or no way to look): can't tell
    return _file_agent(res)


def script_agent(argv, cwd=None, kind="node"):
    """The agent an interpreter's exact arguments run, else None, or UNKNOWN_AGENT when it can't be told:
      - its title, argv[0] (an agent that names its processes: AGENT_NAMES);
      - an agent's package named by any argument, or by what one naming an existing file resolves to (relative ones
        from the process's own working folder: cwd(), its path or None when it can't be read): AGENT_PACKAGE_WORDS
        and AGENT_PACKAGE_FOLDERS, specific identifiers, never a bare name (so `node -pe "import('@scope/an-agent')"`
        and `--import @scope/an-agent` are that agent, and an editor opening a file named after one isn't);
      - the program in its place: a preload (--require/--import: a file or a package, AGENT_PACKAGES), python's -m
        module, or the script, the first argument that isn't an option or an option's value (only options known to take
        one: NODE_VALUE_FLAGS, DENO_VALUE_FLAGS, python's -W, -X; any other option takes none), checked as written and
        as it resolves against AGENT_NAMES and AGENT_PATH_WORDS. Code (-c, -e, -p and clusters like -pe) is no script:
        the package rule has read it.
    UNKNOWN_AGENT when the script's place names no file, the working folder can't be read for a relative one, or the
    program comes from standard input (`-`). No program but options (a REPL) is no agent."""
    if not argv:
        return None
    where = cwd if callable(cwd) else (lambda: cwd)
    title = name_agent(argv[0])
    if title:
        return title
    got = _package_rule(argv[1:], where)
    if got:
        return got
    i, n, command = 1, len(argv), False
    while i < n:
        a = argv[i]
        if a == "-":
            return UNKNOWN_AGENT
        if a == "--":
            return _script_at(argv[i + 1], where, kind) if i + 1 < n else None
        if a.startswith("-"):
            flag, eq, value = a.partition("=")
            if kind == "python":
                if not a.startswith("--"):
                    for j in range(1, len(a)):
                        if a[j] in "WX":
                            i += 0 if a[j + 1:] else 1        # -W ignore: the next argument is its value
                            break
                        if a[j] in "cm":
                            v = a[j + 1:] or (argv[i + 1] if i + 1 < n else "")
                            return AGENT_MODULES.get(v.split(".")[0]) if a[j] == "m" else None
                elif flag in PYTHON_VALUE_FLAGS and not eq:
                    i += 1
            else:
                if flag in NODE_CODE_FLAGS or (not a.startswith("--") and kind != "deno"
                                               and len(a) > 2 and ("e" in a[1:] or "p" in a[1:])):
                    return None                       # code (or a package script), which the package rule read
                if flag in NODE_PRELOAD_FLAGS and kind != "deno":
                    spec = value if eq else (argv[i + 1] if i + 1 < n else "")
                    got = _preload_agent(spec, where, kind) if spec else None
                    if got:
                        return got
                if not eq and (flag in (DENO_VALUE_FLAGS if kind == "deno" else NODE_VALUE_FLAGS)):
                    i += 1
            i += 1
            continue
        if kind in ("bun", "deno") and not command and a in (BUN_COMMANDS if kind == "bun" else DENO_COMMANDS):
            command = True
            if a not in SCRIPT_COMMANDS:
                return None                           # no script file of its own (deno eval: code, read above)
            i += 1
            continue
        return _script_at(a, where, kind)
    return None


def agent_kind(comm, argv=None):
    """The agent session a process is (one of AGENTS), or None, by its name: its process table command (ps comm=) or
    its executable: its own executable's (executable_agent), a program's file name, its title included (AGENT_NAMES),
    or an app's executables by their paths (app_agent). argv: an interpreter's arguments, read by script_agent (here
    without a working folder)."""
    got = executable_agent(comm)
    if got:
        return got
    got = name_agent(comm) or app_agent(comm)
    if got:
        return got
    return script_agent(argv, None, _interp_kind(comm)) if argv and is_interpreter(comm) else None


def process_agent(comm, exe, argv, cwd):
    """The agent session a process is, from what it executes: (a) its name (agent_kind) and its executable, the
    realpath of its text image (exe: None when it can't be read; an agent's native build, a node or bun single-file
    one named after it); (b) for an interpreter (by either), the program it runs (script_agent: argv() its exact
    arguments, None when they can't be read, which is UNKNOWN_AGENT; cwd() its working folder)."""
    got = agent_kind(comm) or (agent_kind(exe) if exe else None)
    if got:
        return got
    interp = comm if is_interpreter(comm) else (exe if exe and is_interpreter(exe) else None)
    if not interp:
        return None
    args = argv()
    if args is None:
        return UNKNOWN_AGENT
    return script_agent(args, cwd, _interp_kind(interp))


def agent_of(pid, table):
    """The agent session process pid is, in table (one of AGENTS), or None. A process's executable, arguments and
    working folder are read only when the table is ps_table's (ProcTable; the tests' ProcTable gives them); a plain
    dict (the tests' tables of known processes) has only names: an interpreter there isn't an agent."""
    if pid not in table:
        return None
    comm = table[pid][1]
    if not isinstance(table, ProcTable):
        return agent_kind(comm)
    if pid in table.inexact:
        return UNKNOWN_AGENT                    # its name as ps has it can't be told: it may be an agent (fail closed)
    return process_agent(comm, table.exe(pid), lambda: table.argv(pid), lambda: table.cwd(pid))


def claude_of(pid, table):
    """Nearest agent session process above pid: any of AGENTS (None outside them all). The name is older than the other
    agents' support; claims, events and the vault still call this process claude_pid (a field name of their records)."""
    for p in ancestors(pid, table)[1:]:
        if agent_of(p, table):
            return p
    return None


_AGENT_NAMES = {}


def agent_name(pid):
    """The agent (one of AGENTS) a live agent session process is (cached per pid), else None."""
    if not pid:
        return None
    if pid not in _AGENT_NAMES:
        try:
            comm = procinfo.field(int(pid), "comm", timeout=5)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if comm is None:                        # ps can't start, and the kernel can't give its name as ps would
            _AGENT_NAMES[pid] = UNKNOWN_AGENT
        else:
            _AGENT_NAMES[pid] = process_agent(comm.strip(), proc_exe(pid), lambda: proc_argv(pid),
                                              lambda: proc_cwd(pid))
    return _AGENT_NAMES[pid]


def at_user_terminal(fds=(0,)):
    """The bar for commands only the user runs: every fd in fds is a terminal and no agent session (any of AGENTS) is
    above this process. A terminal alone doesn't tell the user from an agent: an agent's command tool can give a command
    a pseudo-terminal of its own. Fails closed when the process table can't be read. Like every
    check against your own account, this stops an agent from passing by accident, not a program that sets out to
    (it can start a command outside the agent's process tree)."""
    try:
        if not all(os.isatty(fd) for fd in fds):
            return False
    except (OSError, ValueError):
        return False
    try:
        table = ps_table(strict=True)
        return ancestry_complete(os.getpid(), table) and claude_of(os.getpid(), table) is None
    except Exception:
        return False


def ancestry_complete(pid, table):
    """True when every process from pid up to launchd (pid 1) is in the table: a walk that stops early (a process
    ps didn't list, a loop) can't say there's no agent above, so it counts as unknown."""
    seen = set()
    while pid > 1:
        if pid not in table or pid in seen:
            return False
        seen.add(pid)
        pid = table[pid][0]
    return pid == 1


AT_TERMINAL_HINT = ("in your own terminal (Terminal, iTerm or an SSH session), not through an agent ("
                    + _manifests.in_words(_manifests.labels(), last="") + "), a script or a pipe")


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


CLAIM_NAME = re.compile(r"[1-9][0-9]{0,9}\.json")   # claim_path's names: a pid, .json


def drop_dead_claims(names):
    """Delete dead claims (claims() found their holders gone): only claim files (CLAIM_NAME), through
    lib/safedelete.py, which refuses when OWNERS_DIR is a link or is, holds or reaches a folder some VM uses. Best
    effort: a refusal keeps them (they're not counted as claims either way)."""
    try:
        import safedelete
        safedelete.remove_records(OWNERS_DIR, names, CLAIM_NAME)
    except Exception:
        pass


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


def write_claim(bridge_pid, agent_pid, cwd, tool, started, kind=None, sid=None, extra=None):
    os.makedirs(OWNERS_DIR, exist_ok=True)
    rec = {"bridge_pid": bridge_pid, "claude_pid": agent_pid, "cwd": cwd, "started": started,
           "last_call": time.time(), "last_tool": tool}
    if agent_pid and not (extra or {}).get("agent"):
        rec["agent"] = agent_name(agent_pid) or RECORD_DEFAULT_AGENT
    # Which process the session is: its pid and its start time (claude_start, lib/sessionvm.py proc_start), which the
    # caller passes in extra: taken when the session took the claim (a live ancestor's, the hub's client at its hello),
    # and kept through every refresh (a lease, `metacomputer claude`'s keeper). It's never looked up here: the process that
    # has the pid now may be another one. A claim without it can't be shown to be any session's (lib/sessionvm.py).
    if kind:
        # "shell": metacomputer sh outside an agent session (kept by its time, see claims()); "lease": the hub holding
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
    if agent_pid:
        # The agent session holds this computer now: a record of where, so its commands that name no VM stay on it
        # (lib/sessionvm.py, the session guard). Best effort; written once per session and computer.
        try:
            import sessionvm
            sessionvm.note_claim(agent_pid, bridge_pid, os.path.dirname(OWNERS_DIR), rec.get("claude_start"))
        except Exception:
            pass


def _session_start(pid):
    try:
        import sessionvm
        return sessionvm.proc_start(pid)
    except Exception:
        return None


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
    as long as the caller's agent session. Outside an agent session it's held in
    the name of the shell that ran it, but that shell is often short-lived (bash -c,
    a script, another agent's tool runner): such a claim lasts the activity window
    (ACTIVE_S, ten minutes) from its last run whether or not that shell is still
    alive, and later runs from the same terminal session (its sid) count as the
    same controller. Returns who holds it, (holder pid, agent pid, the agent session's
    start time, taken now while it's this process's ancestor); passing that back (a
    refresh during a run) skips looking at the process table, and keeps that start."""
    if who is None:
        table = ps_table()
        agent_pid = claude_of(os.getpid(), table)
        who = (agent_pid or os.getppid(), agent_pid, _session_start(agent_pid) if agent_pid else None)
    holder, agent_pid = who[0], who[1]
    started = time.time()
    try:
        started = json.load(open(claim_path(holder))).get("started", started)
    except (OSError, ValueError):
        pass
    if agent_pid:
        write_claim(holder, agent_pid, os.getcwd(), tool, started, extra={"claude_start": who[2] if len(who) > 2 else None})
    else:
        write_claim(holder, None, os.getcwd(), tool, started, kind="shell", sid=my_sid())
    return who


def drop_claim(bridge_pid, conn=None):
    """Remove the claim held in bridge_pid's name. With conn (the hub's id of one client connection), only a claim that
    connection wrote: one written since by another connection in the same name (a bridge that reconnected) stays.
    Callers that act on others' claims hold the owners lock."""
    p = claim_path(bridge_pid)
    if conn is not None:
        try:
            with open(p) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(rec, dict) or rec.get("conn") != conn:
            return
    try:
        os.remove(p)
    except FileNotFoundError:
        pass


def claims():
    """Live claims; files of dead bridges are removed. A shell claim made outside an agent
    session (kind "shell") is kept for the activity window after its last run even when the
    shell that ran it has exited, and removed after that."""
    out, dead = [], []
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
                if CLAIM_NAME.fullmatch(f) and rec.get("bridge_pid") == int(f[:-5]):   # a claim file of ours
                    dead.append(f)
                continue
        rec["idle_s"] = idle
        rec["active"] = idle < ACTIVE_S
        out.append(rec)
    if dead:
        drop_dead_claims(dead)
    return out


# --- the user has the computer: a handoff (lib/handoff.py) ---------------------------------------
# Its record sits beside the claims under a name claims() never reads (not *.json), and only lib/handoff.py
# writes it, under the owners lock (and, to open one, the dispatch lock held exclusively). While it's open
# (status "open", before its deadline), others() lists it for every caller, and every acting step looks again
# under dispatch() right before it leaves the Mac, so every acting call waits: the hub's clients and its own
# cleanup, `metacomputer sh`, the vault's fills and the lifecycle commands that check for other controllers.
HANDOFF_FILE = "handoff.state"
HANDOFF_MAX_S = 7200          # the longest time limit; a record that can't be read counts as open this long
HANDOFF_WHY = ("captcha", "login", "2fa", "payment", "other")
# The words every version has read since handoffs began: lib/handoff.py writes only these, and no agent name, while
# this VM's hub runs other code (lib/mcp_hub.py runs_other_code), so an older hub reads the record as it is rather than
# as one that can't be read (an open handoff for HANDOFF_MAX_S, even after it ends).
HANDOFF_WHY_EVERY_VERSION = ("captcha", "login", "2fa", "other")
# A word or agent name this version doesn't know (a newer version wrote it) reads as "other" or no agent: they only
# name things in messages, so the record's state (open or ended, and when) still counts.
HANDOFF_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
HANDOFF_AGENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
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
            and isinstance(rec.get("why"), str) and HANDOFF_WORD_RE.fullmatch(rec["why"]) is not None
            and _finite(rec.get("opened")) and _finite(rec.get("deadline"))
            and 0 < rec["deadline"] - rec["opened"] <= HANDOFF_MAX_S + 1    # + 1: float rounding of a 2 h limit
            and opt("say", str) and opt("cwd", str) and opt("note", str) and opt("ended_from", str)
            and opt("claude_pid", int) and opt("claude_start", str)
            and (rec.get("agent") is None or (isinstance(rec.get("agent"), str)
                                              and HANDOFF_AGENT_RE.fullmatch(rec["agent"]) is not None))
            and (rec.get("ended") is None or _finite(rec.get("ended"))))


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
    noticed (`metacomputer handoff cancel` at the user's own terminal removes it)."""
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
        if rec["why"] not in HANDOFF_WHY:
            rec["why"] = "other"            # a newer version's word (HANDOFF_WORD_RE)
        if rec.get("agent") is not None and rec["agent"] not in AGENTS:
            rec["agent"] = None             # a newer version's agent: messages then name none
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
                f"{left} more; `metacomputer handoff cancel` in your own terminal removes it [ACTIVE]")
    where = f" from {short(h['cwd'])}" if h.get("cwd") else ""
    return (f"the user has the computer: a handoff ({h['why']}) opened {fmt_s(now - h['opened'])} ago{where}, until "
            f"they run `metacomputer handoff done` (or `cancel`), {left} at most [ACTIVE]")


# --- the driver's virtual input devices were removed (lib/input_reset.py) -----------------------------
# Every reset writes the time here before it asks the guest (lib/input_reset.py reset_now: `input-reset`, opening a
# handoff, view --open, the hub's Chrome guard), under the dispatch lock held exclusively, and puts the earlier time
# back only when the guest confirmed it found nothing. A driver connection that held a pair keeps its id after the pair
# is gone, and its next pointer action through it ends the driver; the hub compares this time with when its connection
# began using the devices (Hub.reset_seen) and reconnects first.
INPUT_RESET_FILE = "input-reset.stamp"


def input_reset_path():
    return os.path.join(OWNERS_DIR, INPUT_RESET_FILE)


def mark_input_reset(now=None):
    """Record a reset of the guest's virtual input devices (before it's made: then it may have happened). Written
    whole, as a new 0600 file renamed over the old one (a link there is replaced, never followed). False when it
    couldn't be written: then the reset isn't made."""
    now = time.time() if now is None else now
    tmp = None
    try:
        os.makedirs(OWNERS_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=OWNERS_DIR, prefix=".input-reset-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(repr(float(now)))
        os.replace(tmp, input_reset_path())
        return True
    except (OSError, ValueError):
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def restore_input_reset(prev):
    """Put back the earlier record (prev from input_reset_at()) after a reset that found nothing to remove: the hub has
    nothing to renew for it. Only under the dispatch lock held exclusively, as the reset was made (lib/input_reset.py
    reset_now), so no hub read it meanwhile. Best effort."""
    return mark_input_reset(prev)


def input_reset_at():
    """When the last reset that removed a pair was recorded; 0.0 when never, or when it can't be read."""
    try:
        fd = os.open(input_reset_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return 0.0
    try:
        t = float(os.read(fd, 64).decode("ascii").strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return 0.0
    finally:
        os.close(fd)
    return t if _finite(t) else 0.0


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
        # This VM's: to its loopback port here, or (a computer elsewhere) pinned under its own host key alias.
        here = "@127.0.0.1" in cmd and for_this_vm(cmd)
        elsewhere = bool(HOSTKEY_ALIAS) and f"HostKeyAlias={HOSTKEY_ALIAS}" in cmd
        if not MCP_KEY_RE.search(cmd) or not (here or elsewhere):
            continue
        parent = full_command(ppid)
        if "mcp_lazy.py" in parent or "mcp_hub.py" in parent:
            continue  # an on-demand bridge's or the hub's own upstream: its clients are reported through claims
        if "mcp_relay.py" in parent and any(b in full_command(table.get(ppid, (0, ""))[0])
                                            for b in ("mcp_lazy.py", "mcp_hub.py")):
            continue  # the same, through the action-log relay (ACTION_LOG=on)
        chain = ancestors(pid, table)
        who = None
        for p in chain[1:]:      # the nearest process of the records' default agent's own executable, else python
            agent = executable_agent(table[p][1]) == RECORD_DEFAULT_AGENT
            if agent or "python" in os.path.basename(table[p][1]):
                who = p
                if agent:
                    break
        who = who or ppid
        out.append({"ssh_pid": pid, "via_pid": who, "via": full_command(who)[:120],
                    "cwd": proc_cwd(who), "chain": chain})
    return out


def others(self_pid=None, table=None, mine=None, my_claude=None, direct=None, sid=None):
    """Controllers other than the caller's own agent session. The hub, which asks for its clients,
    passes what it already knows: the client's ancestors (mine), its agent's process (my_claude), its terminal
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
        # mine: also a claim held by a shell above the caller (metacomputer sh outside an agent session)
        if c.get("bridge_pid") == self_pid or c.get("bridge_pid") in mine or (my_claude and c.get("claude_pid") == my_claude):
            continue
        # mine: a shell claim (or `metacomputer claude` started from a shell) from this same terminal session,
        # outside an agent session (its shell may be gone)
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


def _agent(o):
    return o.get("agent") if o.get("agent") in AGENTS else RECORD_DEFAULT_AGENT


def describe(o):
    if o["kind"] == "handoff":
        return handoff_describe(o)
    if o["kind"] == "session":
        state = "ACTIVE" if o["active"] else "idle"
        if o.get("claim") == "pending":
            src = f"{_agent(o)} session (pid {o['claude_pid']})" if o.get("claude_pid") else f"client (holder pid {o.get('bridge_pid')})"
            return (f"an unfinished {o.get('last_tool')} call from a {src}, still running in the driver, in "
                    f"{short(o.get('cwd'))}: started {o['idle_s'] // 60} min {o['idle_s'] % 60} s ago [ACTIVE]")
        if o.get("claim") == "inner":
            src = (f"a {_agent(o)} session (pid {o['claude_pid']})" if o.get("claude_pid")
                   else f"a shell (terminal session {o.get('sid')})")
            s = o.get("session") or "?"
            return (f"Claude Code inside the VM (metacomputer claude, session {s}), started by {src} in "
                    f"{short(o.get('cwd'))}, held by its keeper (pid {o.get('bridge_pid')}): its screen last changed "
                    f"{o['idle_s'] // 60} min {o['idle_s'] % 60} s ago [{state}]; metacomputer claude stop"
                    f"{'' if s == 'claude' else ' --session ' + s} ends it")
        if o.get("claim") == "lease":
            src = f"{_agent(o)} session (pid {o['claude_pid']})" if o.get("claude_pid") else f"client (pid {o.get('client_pid')})"
            return (f"unfinished call(s) ({o.get('last_tool')}) that a {src} left running when it disconnected, held by "
                    f"the hub (pid {o.get('bridge_pid')}) until they end, in {short(o.get('cwd'))}: "
                    f"{o['idle_s'] // 60} min {o['idle_s'] % 60} s ago [{state}]")
        who = (f"{_agent(o)} session (pid {o['claude_pid']})" if o.get("claude_pid")
               else f"metacomputer sh outside an agent session (shell pid {o.get('bridge_pid')}"
                    f"{', exited' if o.get('holder_gone') else ''}, terminal session {o.get('sid')})" if o.get("claim") == "shell"
               else f"MCP client outside an agent session (bridge pid {o.get('bridge_pid')})")
        return (f"{who} in {short(o.get('cwd'))}: last action "
                f"{o['idle_s'] // 60} min {o['idle_s'] % 60} s ago ({o.get('last_tool')}) [{state}]")
    return f"direct connection (ssh pid {o['ssh_pid']}) from {o['via']} in {short(o.get('cwd'))} [ACTIVE]"


CLI_OPTION_ARGS = ("--vm", "--on")     # bin/metacomputer's options before the command that take a value


def internal_caller(allowed, table=None):
    """Whether the bin/metacomputer process this runs under (this process's parent) was started by metacomputer itself
    for one of `allowed`: "cli:SUB" is this checkout's bin/metacomputer running the command SUB (after its --vm/--on
    options), "script:NAME" is this checkout's scripts/NAME. Read from the caller's own arguments (proc_argv) and
    compared by real path, so a copy elsewhere or another program doesn't count. Like every check against your own
    account, it stops an agent's ordinary command, not a program that sets out to imitate metacomputer."""
    lab = os.path.realpath(LAB)
    try:
        table = ps_table(strict=True) if table is None else table
        me = os.getppid()                                  # bin/metacomputer
        caller = table.get(me, (None,))[0]
        argv = proc_argv(caller) if caller and caller > 1 else None
    except Exception:
        return False
    if not argv or len(argv) < 2:
        return False
    if os.path.basename(argv[0]).lstrip("-") not in ("bash", "sh"):
        return False
    if os.path.isabs(argv[1]):
        script = os.path.realpath(argv[1])
    elif os.sep in argv[1] and proc_cwd(caller):               # relative to where the caller ran it
        script = os.path.realpath(os.path.join(proc_cwd(caller), argv[1]))
    else:
        return False
    for want in allowed:
        kind, _, name = want.partition(":")
        if kind == "script" and script == os.path.join(lab, "scripts", name):
            return True
        if kind == "cli" and script == os.path.join(lab, "bin", "metacomputer"):
            rest = argv[2:]
            while rest and (rest[0] in CLI_OPTION_ARGS or rest[0].split("=", 1)[0] in CLI_OPTION_ARGS):
                rest = rest[2:] if rest[0] in CLI_OPTION_ARGS else rest[1:]
            if rest and rest[0] == name:
                return True
    return False


def main(argv):
    # Its command line (lib/strictargs.py): anything else (`release --help`, an option it doesn't have, a word) is
    # refused, or the usage shown, before any claim is read or released.
    import strictargs
    strictargs.expect(argv[1:], ["[who] [--json]", "check", "release", "at-terminal [--stderr]",
                                 "internal-caller [--user] [CALLER...]"], prog="owners.py", usage=__doc__)
    cmd = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else "who"
    if cmd == "who":
        table = ps_table()
        me = claude_of(os.getpid(), table)
        all_claims = claims()
        direct = direct_connections(table)
        hub, older = None, False
        try:   # the hub's own view: its clients (they act through it), idle time
            import mcp_hub
            hub = mcp_hub.query_status()
        except Exception:
            pass
        try:
            import mcp_hub
            older = mcp_hub.runs_other_code()      # started before the code changed (an update, a pull)
        except Exception:
            older = None                           # can't be told: said as such
        handoff = open_handoff()
        if "--json" in argv:
            print(json.dumps({"self_claude_pid": me, "sessions": all_claims, "direct": direct,
                              "active_s": ACTIVE_S, "hub": hub, "hub_older_code": older, "handoff": handoff},
                             indent=2, default=str))
            return 0
        if hub:
            print("  hub: " + mcp_hub.describe_status(hub).replace("\n", "\n  "))
        if older is None:
            print("  hub: whether it runs this checkout's code couldn't be told")
        elif older:
            print("  hub: runs older code than this checkout (it started before the code changed); `metacomputer hub "
                  "stop --if-idle` restarts it on the current code once nothing uses it (`metacomputer update` does too)")
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
        # Under the owners lock, which `metacomputer claude`'s keeper (vm_claude.py) holds to check and renew its claim:
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
    if cmd == "internal-caller":
        # exit 0 when bin/metacomputer was started by metacomputer itself for one of the given callers (its internal
        # commands: _vnc-install from a build), or, with --user, at the user's own terminal
        rest = argv[2:]
        user = "--user" in rest
        allowed = [a for a in rest if a != "--user"]
        if any(not re.fullmatch(r"(cli|script):[A-Za-z0-9._-]{1,64}", a) for a in allowed):
            print("usage: owners.py internal-caller [--user] cli:SUB|script:NAME...", file=sys.stderr)
            return 2
        return 0 if internal_caller(allowed) or (user and at_user_terminal((0,))) else 1
    if cmd == "at-terminal":
        # exit 0 when standard input (and with --stderr, standard error) is the user's own terminal: a terminal
        # with no agent session above it (bin/metacomputer's user-only commands)
        return 0 if at_user_terminal((0, 2) if "--stderr" in argv else (0,)) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
