"""Hermes Agent's bench launcher (bench/bench.py --agent hermes; the harness calls these with itself as B)."""
import json
import os
import re
import signal
import subprocess
import time

SKILL_HOME = ".agents"          # the skill goes in the work folder's .agents/skills/
ISOLATED = True                 # each session in a throwaway HOME (isolate)
TOKENS_ONLY = True              # it reports tokens, never a cost
SHELL_TOOLS = ("terminal",)            # its shell tool, read as Bash (bench/bench.py _shell_or_mcp)
# An isolated Hermes session's toolsets and settings: the terminal (for metacomputer's command), skills and
# metacomputer's server; approvals asked for (never auto-approved by its guardian model) and refused in a -q session;
# the gateway the session starts for its home ends a minute after it goes idle.
HERMES_TOOLSETS = "terminal,skills,metacomputer"
HERMES_SETTINGS = (("tools.tool_search.enabled", "off"), ("approvals.mode", "manual"),
                   ("approvals.single_query_mode", "deny"), ("gateway.unmanaged_idle_exit_seconds", "60"))
# Hermes Agent's administrator-managed scope: its .env and config.yaml apply to every Hermes run on the Mac.
HERMES_MANAGED = "/etc/hermes"
GATEWAY_STOP_S = 10             # a gateway left running gets SIGTERM (its own shutdown), then SIGKILL after this long
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")      # Hermes's release tags (its version reader's v[0-9]*)
# Hermes's gateway, as Hermes starts one for a home (hermes_cli/gateway_runtime_start.py spawn_unmanaged_gateway):
# [sys.executable, "-m", "hermes_cli.main", (--profile NAME), "gateway", "run", --quiet, ...]. A Python interpreter, by its
# executable (the kernel's, its text image) and by its first argument; CPython's options that may come before -m (the
# ones without a value, alone or clustered, and -W/-X with theirs).
PYTHON = re.compile(r"python(\d+(\.\d+)*t?)?|Python")
PY_FLAGS = frozenset("bBdEiIOPqRsSuvx")
PY_VALUE_OPTS = ("-W", "-X")
GATEWAY_MODULE = "hermes_cli.main"
PROFILE_OPTS = ("--profile", "-p")
SHA = re.compile(r"[0-9a-f]{40}")


def model(sw, default):
    """Its own default model unless --model names one."""
    return "hermes-default" if sw["model"] == default else sw["model"]


def refuse(B, sw, prog):
    """Before any VM is reset: refused now rather than at the first session (refuse_hermes_outside_files)."""
    home = B.tempfile.mkdtemp(prefix="mcomp-bench-home-")
    try:
        env = dict(B.session_env(sw, home), HERMES_HOME=os.path.join(home, ".hermes"))
        refuse_hermes_outside_files(prog, env, home)
    finally:
        B.safedelete.rmtree(home, quiet=True)


def isolate(B, sw, work, cfg, home, prog, env):
    """Refused while Hermes would load settings or keys from outside its home (refuse_hermes_outside_files: its
    installation's .env, the administrator-managed /etc/hermes); a Hermes home of the session's own (HERMES_HOME: its
    own default profile and gateway, so concurrent runs never share one) holding only metacomputer's server (Hermes
    runs a configured server's tools without asking) with tool search off, and the skill; only the terminal, skills and
    metacomputer toolsets (-t); no --yolo: approvals manual and a single-query session's dangerous commands refused.
    Hermes has no allowlist for its terminal: even with manual approvals, a command it doesn't rate dangerous runs
    without asking, as you, on this Mac (the throwaway HOME keeps Hermes's own settings out, it isn't a sandbox)."""
    prompt = B.prompt_of(sw.get("_task"), sw)
    hh = os.path.join(home, ".hermes")
    os.makedirs(os.path.join(hh, "skills"), 0o700)
    env = dict(env, HERMES_HOME=hh)
    refuse_hermes_outside_files(prog, env, home)
    # The first command in a new home can take a while: with Hermes's own installer, Hermes prepares the
    # dependencies of each new home the first time (here, before the session's clock starts).
    for i, (key, value) in enumerate(HERMES_SETTINGS + (("mcp_servers.metacomputer",
                                                           json.dumps(B.agent_server(cfg, "timeout", 600))),)):
        r = subprocess.run([prog, "config", "set", key, value], capture_output=True, text=True, env=env,
                           timeout=900 if i == 0 else 120, stdin=subprocess.DEVNULL, cwd=home)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't set {key} in the session's Hermes home")
    os.symlink(os.path.join(work, ".agents", "skills", "metacomputer"), os.path.join(hh, "skills", "metacomputer"))
    args = ["chat", "-q", prompt, "-Q", "--format", "stream-json", "-t", HERMES_TOOLSETS, "--max-turns",
            str(int(sw["_task"].get("max_turns", 60))), "--source", "tool"]
    return args + (["-m", B.own_model(sw)] if B.own_model(sw) else []), env


def hermes_outside_files(prog, env, cwd):
    """The files outside a Hermes Agent home that Hermes loads into every run, whatever its home: its installation's own
    .env (hermes_cli/main.py loads PROJECT_ROOT/.env, the folder `hermes --version` names as its install directory,
    over the environment when the home has no .env: a source or editable install's keys) and the
    administrator-managed scope (HERMES_MANAGED: its .env, loaded last over everything, and its config.yaml). Returns
    (the ones that are there, or that can't be looked at), or raises SystemExit when the install directory can't be
    told."""
    try:
        r = subprocess.run([prog, "--version"], capture_output=True, text=True, env=env, timeout=300,
                           stdin=subprocess.DEVNULL, cwd=cwd)
        out = r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        out = ""
    m = re.search(r"^Install directory: (/.+?)\s*$", out, re.M)
    if not m:
        raise SystemExit("couldn't tell where Hermes Agent is installed (`hermes --version` names no install directory), "
                         "so not whether it would load a .env of its installation into the session: not running it")
    there = []
    for f in (os.path.join(m[1], ".env"), os.path.join(HERMES_MANAGED, ".env"), os.path.join(HERMES_MANAGED, "config.yaml")):
        try:
            os.lstat(f)
            there.append(f)
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            there.append(f)                     # can't be looked at: it may be there
    return there


def refuse_hermes_outside_files(prog, env, cwd):
    """Stop the run when Hermes Agent would bring settings or keys of yours into a session (hermes_outside_files)."""
    there = hermes_outside_files(prog, env, cwd)
    if there:
        raise SystemExit(f"{there[0]} is there (or can't be looked at), and Hermes Agent loads it into every session "
                         "whatever its home, so a benchmark session would carry its settings or keys: not running Hermes "
                         "Agent. Move it away for the run (or use a Hermes Agent installed without one), then run again.")


def end(B, prog, home):
    """After an isolated session, however it ended: Hermes's gateway for the session's home stopped (a -q session starts
    one, which would otherwise stay up until it has been idle a minute, and write into the home bench deletes next).
    Hermes's own `gateway stop` first; it finds its gateway with ps, which can't start where Hermes runs in macOS's
    sandbox (no setuid program can), so then whatever gateway of this home is still there is stopped here
    (stop_gateways)."""
    hh = os.path.join(home, ".hermes")
    if os.path.isdir(hh):
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        env.update(HOME=home, HERMES_HOME=hh)
        try:
            subprocess.run([prog, "gateway", "stop"], capture_output=True, text=True, env=env, timeout=120,
                           stdin=subprocess.DEVNULL, cwd=home)
        except (OSError, subprocess.SubprocessError):
            pass
        stop_gateways(B, hh)


def is_gateway(args, exe):
    """Whether a process is Hermes's gateway as Hermes starts one: its executable (exe: the realpath of its text image)
    and its first argument a Python interpreter; then that interpreter's options and nothing else up to `-m
    hermes_cli.main` (the module it runs); then, after Hermes's profile selector (--profile NAME, -p NAME, --profile=NAME),
    `gateway run` in exactly those places. A word "hermes", "gateway" or "run" anywhere else (a script's arguments, -c
    code, another Hermes command's) never makes one."""
    if not exe or not args or not PYTHON.fullmatch(os.path.basename(exe)) or not PYTHON.fullmatch(os.path.basename(args[0])):
        return False
    i = 1
    while i < len(args) and args[i] != "-m":
        w = args[i]
        if w in PY_VALUE_OPTS:
            i += 2
        elif w.startswith(PY_VALUE_OPTS) or (len(w) > 1 and w[0] == "-" and set(w[1:]) <= PY_FLAGS):
            i += 1
        else:
            return False                # a script, -c code, or anything else before -m: not the module Hermes runs
    if args[i + 1:i + 2] != [GATEWAY_MODULE]:
        return False
    rest = args[i + 2:]
    if rest[:1] and rest[0] in PROFILE_OPTS:
        rest = rest[2:]
    elif rest[:1] and rest[0].startswith(tuple(o + "=" for o in PROFILE_OPTS)):
        rest = rest[1:]
    return rest[:2] == ["gateway", "run"]


def same_process(B, pid, start):
    """Whether pid is still the process that started at start (read fresh from the kernel, kern.proc.pid): False when
    it ended, is a zombie, its pid is another process's now, or that can't be told."""
    try:
        p = B.procinfo.kernel_proc(pid)
    except Exception:
        return False
    return p is not None and not p.zombie and p.start == start


def gateways(B, hh):
    """This session's Hermes gateways still running, [(pid, start)]: this account's processes that run Hermes's gateway
    (is_gateway: by their executable, the module and the arguments' places) with this session's Hermes home as
    HERMES_HOME in their environment (how Hermes starts one for a home). Read from the kernel (lib/procinfo.py), never
    ps: executable, arguments and environment exactly, whatever can or can't run; each read again after them to be the
    process they were read from (its pid and start time), or left out."""
    want = os.path.realpath(hh)
    found = []
    for p in B.procinfo.kernel_procs() or []:
        if p.pid <= 1 or p.uid != os.getuid() or p.zombie:
            continue
        args = B.procinfo.argv(p.pid)
        if not args or not is_gateway(args, B.procinfo.exe(p.pid)):
            continue
        home = (B.procinfo.environ(p.pid) or {}).get("HERMES_HOME") or ""
        if home and os.path.realpath(home.rstrip("/") or "/") == want and same_process(B, p.pid, p.start):
            found.append((p.pid, p.start))
    return found


def signal_if_same(B, pid, start, sig):
    """sig to pid only while it is still the process found (same_process, read fresh right before): never one that
    took its pid meanwhile, nor when that can't be told."""
    if not same_process(B, pid, start):
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def stop_gateways(B, hh):
    """Stop this session's gateways (gateways): SIGTERM, its own shutdown; after GATEWAY_STOP_S, SIGKILL to each still
    there, found again. Before every signal, the first SIGTERM too, it is checked to be the same process (its pid and
    start time, read fresh: signal_if_same)."""
    found = gateways(B, hh)
    for pid, start in found:
        signal_if_same(B, pid, start, signal.SIGTERM)
    end = time.monotonic() + GATEWAY_STOP_S
    while found and time.monotonic() < end and any(same_process(B, *g) for g in found):
        time.sleep(0.2)
    left = [g for g in found if same_process(B, *g)]
    if left:
        again = set(gateways(B, hh))
        for pid, start in left:
            if (pid, start) in again:
                signal_if_same(B, pid, start, signal.SIGKILL)


def version(B, line, out):
    """run.json's version of Hermes (bench's agent_version): its own first line, unless that says vunknown. Hermes reads
    its version with git (a checkout's release tag), which can't run everywhere (macOS's sandbox): then the tag is read
    from its checkout's own files (checkout_version, the install directory `hermes --version` names). Without one
    either, it stays unknown (bench drops the v)."""
    if not re.search(r"(?<![A-Za-z0-9])vunknown\b", line):
        return line
    m = re.search(r"^Install directory: (/.+?)\s*$", out, re.M)
    got = checkout_version(m[1]) if m else None
    return line.replace("vunknown", got, 1) if got else line


def _read(path, limit=1 << 20):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def checkout_version(root):
    """A git checkout's release, read from its files without git: the commit its HEAD names, and the release tag
    (vX.Y.Z) on exactly that commit: "vX.Y.Z" (the highest, when several), else "git.SHORT" (as Hermes names a checkout
    with no release tag), or None when the files don't say (no checkout, a HEAD that can't be followed)."""
    dot = os.path.join(root, ".git")
    if os.path.isfile(dot):                             # a worktree: "gitdir: PATH"
        m = re.match(r"gitdir: (.+)", (_read(dot, 4096) or "").strip())
        gitdir = os.path.join(root, m[1]) if m else None
    else:
        gitdir = dot if os.path.isdir(dot) else None
    if not gitdir:
        return None
    common = (_read(os.path.join(gitdir, "commondir"), 4096) or "").strip()
    common = os.path.join(gitdir, common) if common else gitdir
    packed = {}
    peeled_of, last = {}, None
    for ln in (_read(os.path.join(common, "packed-refs")) or "").splitlines():
        if ln.startswith("^") and last and SHA.fullmatch(ln[1:].strip()):
            peeled_of[last] = ln[1:].strip()
        elif not ln.startswith("#") and " " in ln:
            sha, ref = ln.split(" ", 1)
            if SHA.fullmatch(sha):
                packed[ref.strip()] = sha
                last = ref.strip()

    def ref_sha(ref):
        if not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", ref) or ".." in ref:
            return None
        loose = (_read(os.path.join(common, ref), 4096) or "").strip()
        return loose if SHA.fullmatch(loose) else packed.get(ref)
    head = (_read(os.path.join(gitdir, "HEAD"), 4096) or "").strip()
    commit = ref_sha(head[5:].strip()) if head.startswith("ref: ") else (head if SHA.fullmatch(head) else None)
    if not commit:
        return None
    tags = []
    tdir = os.path.join(common, "refs", "tags")
    loose = set(os.listdir(tdir)) if os.path.isdir(tdir) else set()
    for ref, sha in packed.items():                     # a loose tag of that name is newer than its packed line
        if ref.startswith("refs/tags/") and ref[10:] not in loose and peeled_of.get(ref, sha) == commit:
            tags.append(ref[len("refs/tags/"):])
    for name in sorted(loose):
        if (_read(os.path.join(tdir, name), 4096) or "").strip() == commit:     # a tag object can't be followed here
            tags.append(name)
    releases = sorted({t for t in tags if RELEASE_TAG.fullmatch(t)},
                      key=lambda t: tuple(int(x) for x in RELEASE_TAG.fullmatch(t).groups()))
    return releases[-1] if releases else "git." + commit[:7]


def events(B, ev, state):
    """One `hermes chat --format stream-json` event as stream-json events: init; a tool_use is held until its
    tool_result (by tool_call_id, else name) and both become a finished call (terminal as Bash); text; the result
    (tokens, no cost)."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        state["sid"] = ev.get("session_id")
        return [{"type": "system", "subtype": "init", "session_id": ev.get("session_id"), "model": ev.get("model"),
                 "cwd": state.get("cwd"), "tools": []}]
    if t == "text":
        return [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": ev.get("text") or ""}]}}]
    if t == "tool_use":
        state.setdefault("pending", {})[ev.get("tool_call_id") or ev.get("name")] = (
            ev.get("name") or "", ev.get("input") if isinstance(ev.get("input"), dict) else {})
        return []
    if t == "tool_result":
        key = ev.get("tool_call_id") or ev.get("name")
        name, inp = state.get("pending", {}).pop(key, (ev.get("name") or "unknown", {}))
        name, inp = B._shell_or_mcp(name, inp)
        return B._call(state, name, inp, [{"type": "text", "text": str(ev.get("output") or "")[:20000]}],
                       ev.get("is_error"), f"hermes_{key}" if ev.get("tool_call_id") else None)
    if t == "result":
        tok = ev.get("tokens") or {}
        ok = ev.get("exit_code") == 0 and not ev.get("error")
        return [{"type": "result", "subtype": "success" if ok else "error", "is_error": not ok,
                 "num_turns": state.get("calls", 0), "total_cost_usd": None, "session_id": ev.get("session_id") or state.get("sid"),
                 "usage": {"input_tokens": tok.get("input"), "output_tokens": tok.get("output"),
                           "cache_read_input_tokens": tok.get("cache_read")}}]
    return []
