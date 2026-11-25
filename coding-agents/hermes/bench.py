"""Hermes Agent's bench launcher (bench/bench.py --agent hermes; the harness calls these with itself as B)."""
import json
import os
import re
import subprocess

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
    one, which would otherwise stay up until it has been idle a minute)."""
    if os.path.isdir(os.path.join(home, ".hermes")):
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        env.update(HOME=home, HERMES_HOME=os.path.join(home, ".hermes"))
        try:
            subprocess.run([prog, "gateway", "stop"], capture_output=True, text=True, env=env, timeout=120,
                           stdin=subprocess.DEVNULL, cwd=home)
        except (OSError, subprocess.SubprocessError):
            pass


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
