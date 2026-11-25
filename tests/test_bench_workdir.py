#!/usr/bin/env python3
"""The benchmark's session folder and the `claude` it runs (bench/bench.py), and the other agents' sessions (--agent
gemini|hermes|openclaw, against stand-ins: each in a throwaway HOME with only the --pass-env variables, approval for
metacomputer only, their command lines, their events). No VM, no API use: a stand-in `claude` records how it was
started.

    python3 tests/test_bench_workdir.py

It checks that the work folder is a fresh private folder (0700, yours, a new name each
time, not the old shared /tmp/mcomp-bench/<run>/work), that the shim folder holds only
the two links to the session's launchers (which run this checkout's), and that the session runs the `claude` found
on PATH before the shim folder went in front: one planted in the shim folder, in the old
shared path or in a relative PATH entry is never run, and one other accounts can write
is refused.

And what the item 10 benchmark run found: metacomputer's own commands that bench runs (reset, start and the daemons they
leave) and a session's metacomputer commands (its launchers on PATH, the one its skill names, its MCP server: every
agent's, Claude Code's and Codex's too) never get the --pass-env variables, while the agent does; OpenClaw's cached
prompt tokens are counted;
Hermes's gateway is stopped without ps, told by its executable, module and arguments' places (never a same-home process
that only names them), and only while it is the same process; a metacomputer command after `cd ... &&`, `env ...`, in
a variable or under `bash -lc` counts as one (and whatever the plain pattern counted still counts); and Hermes's version
is read without git, or said as unknown.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "bench"))
import bench  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fake_claude(path, marker):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                f"echo \"$0\" > '{marker}'\n"
                "echo '{\"type\":\"result\",\"subtype\":\"success\",\"total_cost_usd\":0,\"num_turns\":1}'\n")
    os.chmod(path, 0o755)


def test_codex_trust_undo():
    """Codex saves the bench's per-run trust override into your own config.toml when a session starts: the bench puts
    the file back exactly as it was when that table is the only change, and leaves any other change alone."""
    td = tempfile.mkdtemp(prefix="mcomp-trust-")
    try:
        cfg = os.path.join(td, "config.toml")
        work = os.path.join(td, "work")
        os.makedirs(work)
        before = 'model = "x"\n\n[tui]\nscreen_reader_detection_done = true\n'
        added = f'\n[projects."{os.path.realpath(work)}"]\ntrust_level = "trusted"\n'
        with open(cfg, "w") as f:
            f.write(before + added)
        os.chmod(cfg, 0o600)
        r = bench.hooks("codex").undo_codex_trust(bench, before, work, cfg)
        check("the run's own trust table is the only change: the file is put back exactly, its mode kept",
              r == "restored" and open(cfg).read() == before and stat.S_IMODE(os.stat(cfg).st_mode) == 0o600, r)
        with open(cfg, "w") as f:
            f.write(before + added + 'personality = "friendly"\n')
        r = bench.hooks("codex").undo_codex_trust(bench, before, work, cfg)
        check("...any other change too: the file is left as it is", r == "left" and "personality" in open(cfg).read(), r)
        other = f'\n[projects."{td}/someone-else"]\ntrust_level = "trusted"\n'
        with open(cfg, "w") as f:
            f.write(before + other)
        r = bench.hooks("codex").undo_codex_trust(bench, before, work, cfg)
        check("...another folder's trust (not this run's) is left too", r == "left" and "someone-else" in open(cfg).read(), r)
        with open(cfg, "w") as f:
            f.write(before)
        check("...and an unchanged file isn't touched", bench.hooks("codex").undo_codex_trust(bench, before, work, cfg) == "unchanged")
        check("no temporary file is left behind", sorted(os.listdir(td)) == ["config.toml", "work"], os.listdir(td))
    finally:
        shutil.rmtree(td, ignore_errors=True)

# Stand-ins for the other agents' headless modes: each logs its arguments, folder and environment (one JSON line per
# run, in the test's folder (STUB_DIR, written into the stand-in: the session's environment doesn't carry it), with what the session's throwaway HOME held at that moment: it is gone afterwards) and prints what
# the real one prints (Gemini CLI 0.70.0's stream-json, Hermes Agent 0.23.3's, OpenClaw 2026.11.7's `agent exec --json`
# envelope).
STUB_HEAD = """import json, os, sys, time
AMBIENT_KEYS = ("BENCH_TEST_CANARY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GEMINI_CLI_HOME", "HERMES_HOME",
                "OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "MCOMP_CLAUDE_TOKEN")
a = sys.argv[1:]
d = STUB_DIR
def snap():
    # Only a session's own throwaway HOME is looked through: never a real one (a session that has yours shows as such).
    h = os.environ.get("HOME", "")
    files = {}
    for top, dirs, names in (os.walk(h) if os.path.basename(h).startswith("mcomp-bench-home-") else ()):
        for n in names:
            p = os.path.join(top, n)
            try:
                if os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) < 200000:
                    files[os.path.relpath(p, h)] = open(p, errors="replace").read()
            except OSError:
                pass
    env = {k: v for k, v in os.environ.items() if k in ("HOME", "HERMES_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH",
                                                       "BENCH_TEST_MODEL_KEY", "PATH") or k in AMBIENT_KEYS}
    env["KEYS"] = sorted(os.environ)
    return {"argv": a, "cwd": os.getcwd(), "env": env, "files": files}
def log(rec): open(os.path.join(d, os.path.basename(sys.argv[0]) + ".argv"), "a").write(json.dumps(rec) + "\\n")
def out(o): print(json.dumps(o), flush=True)
"""
GEMINI_STUB = STUB_HEAD + """
log(snap())
if a == ["--version"]: print("0.70.0"); sys.exit(0)
out({"type": "init", "timestamp": "t", "session_id": "g-1", "model": "gemini-2.5-pro"})
out({"type": "message", "role": "user", "content": "x"})
out({"type": "tool_use", "tool_name": "mcp_metacomputer_list_windows", "tool_id": "t1", "parameters": {}})
out({"type": "tool_result", "tool_id": "t1", "status": "success", "output": "1 window"})
out({"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t2", "parameters": {"command": "metacomputer status"}})
out({"type": "tool_result", "tool_id": "t2", "status": "error", "output": "", "error": {"type": "x", "message": "exit 1"}})
out({"type": "message", "role": "assistant", "content": "Done.", "delta": True})
out({"type": "result", "status": "success", "stats": {"input_tokens": 1200, "output_tokens": 80, "cached": 10, "tool_calls": 2}})
"""
HERMES_STUB = STUB_HEAD + """
w, hh = a, os.environ.get("HERMES_HOME") or os.path.join(d, "hermes-default-home")
if a[:1] == ["-p"]:                 # a named profile (what the benchmark used before its sessions had homes of their own)
    w, hh = a[2:], os.path.join(d, "hermes-home", "profiles", a[1])
if a == ["--version"]:
    inst = os.path.join(d, "hermes-install")
    named = os.path.join(d, "hermes-install-says")
    first = os.path.join(d, "hermes-version-says")      # what Hermes says where git can't run: vunknown
    print(open(first).read().strip() if os.path.isfile(first) else "Hermes Agent v0.23.3 (2026.9.24)")
    print(open(named).read() if os.path.isfile(named) else f"Install directory: {inst}")
    sys.exit(0)
if w[:2] == ["profile", "create"]:
    log(snap()); os.makedirs(os.path.join(d, "hermes-home", "profiles", w[2]), exist_ok=True); sys.exit(0)
if w == ["config", "path"]:
    if not os.path.isdir(hh): sys.exit(1)
    print(os.path.join(hh, "config.yaml")); sys.exit(0)
os.makedirs(hh, exist_ok=True)
if w[:2] == ["config", "set"]:
    open(os.path.join(hh, "set.jsonl"), "a").write(json.dumps(w[2:]) + "\\n"); sys.exit(0)
if w[:2] == ["gateway", "stop"]:
    log(snap()); sys.exit(0)       # stops nothing: Hermes's own stop finds its gateway with ps (none in a sandbox)
# Its gateway for this home, as Hermes starts one: `python -m hermes_cli.main gateway run`, detached, HERMES_HOME set.
import subprocess
gw = subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "gateway", "run", "--quiet"],
                      env=dict(os.environ, HERMES_HOME=hh, PYTHONPATH=os.path.join(d, "gwpkg")), start_new_session=True,
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(os.path.join(d, "hermes-gateways.txt"), "a").write(f"{gw.pid}\\n")
sleep = os.path.join(d, "hermes-sleep")
time.sleep(float(open(sleep).read()) if os.path.isfile(sleep) else 0)     # concurrent sessions overlap here
rec = snap()
sets = os.path.join(hh, "set.jsonl")
rec["sets"] = [json.loads(x) for x in open(sets)] if os.path.isfile(sets) else []
skills = os.path.join(hh, "skills", "metacomputer")
rec["skill_link"] = os.readlink(skills) if os.path.islink(skills) else None
log(rec)
out({"type": "system", "subtype": "init", "model": "nous/hermes", "session_id": "h-1", "timestamp": 1})
out({"type": "tool_use", "name": "mcp__metacomputer__click", "tool_call_id": "c1", "input": {"element_token": "a:1"}})
out({"type": "tool_result", "name": "mcp__metacomputer__click", "tool_call_id": "c1", "output": "clicked", "duration_ms": 30, "is_error": False})
out({"type": "tool_use", "name": "terminal", "tool_call_id": "c2", "input": {"command": "metacomputer status"}})
out({"type": "tool_result", "name": "terminal", "tool_call_id": "c2", "output": "{}", "duration_ms": 5, "is_error": True})
out({"type": "text", "text": "Done."})
out({"type": "result", "session_id": "h-1", "exit_code": 0, "text": "Done.", "tokens": {"input": 900, "output": 40, "total": 940, "cache_read": 0, "cache_write": 0}, "duration_ms": 10})
"""
OPENCLAW_STUB = STUB_HEAD + """
log(snap())
if a == ["--version"]: print("OpenClaw 2026.11.7 (e31bbf0)"); sys.exit(0)
if a[:3] == ["approvals", "allowlist", "add"]: sys.exit(0)
print(json.dumps({"ok": True, "status": "ok", "final": "Done.", "usage": {"input": 700, "output": 30, "cacheRead": 50000,
                                                                         "cacheWrite": 1200}, "assistantTurns": 3,
                  "toolSummary": {"calls": 4, "tools": {"metacomputer__click": 4}}, "model": "m", "sessionId": "o-1"}, indent=2))
"""

# Your own environment as an agent session must never see it: a credential, and each agent's own overrides.
AMBIENT = {"BENCH_TEST_CANARY": "ambient-credential", "OPENAI_API_KEY": "ambient-openai", "GEMINI_API_KEY": "ambient-g",
           "GEMINI_CLI_HOME": "/ambient/gemini", "HERMES_HOME": "/ambient/hermes", "OPENCLAW_CONFIG_PATH": "/ambient/oc.json",
           "OPENCLAW_STATE_DIR": "/ambient/oc", "MCOMP_CLAUDE_TOKEN": "ambient-token"}


def records(td, name):
    p = os.path.join(td, name + ".argv")
    return [json.loads(x) for x in open(p)] if os.path.isfile(p) else []


# Everything an isolated session's environment may hold: what a program needs, metacomputer's own test-VM settings,
# the variable named with --pass-env, and the agent's own home and state folder inside the throwaway HOME.
SESSION_MAY_HAVE = set(bench.SESSION_ENV_KEEP) | set(bench.SESSION_MCOMP_KEEP) | {
    "HOME", "BENCH_TEST_MODEL_KEY", "MCOMP_COORD_GUARD", "MCOMP_MCP_HUB", "MCOMP_LESSONS", "HERMES_HOME",
    "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_NO_AUTO_UPDATE"} if hasattr(bench, "SESSION_ENV_KEEP") else set()


def isolated_env_ok(rec, td_real_home, passed):
    """The session's environment: a throwaway HOME (not yours), none of AMBIENT, the --pass-env variable, and nothing
    beyond SESSION_MAY_HAVE."""
    env = rec.get("env", {})
    home = env.get("HOME", "")
    extra = [k for k in env.get("KEYS", ["?"]) if k not in SESSION_MAY_HAVE and not k.startswith("__CF")]
    return bool(home and home != td_real_home and os.path.basename(home).startswith("mcomp-bench-home-")
                and not any(env.get(k) == v for k, v in AMBIENT.items()) and env.get("BENCH_TEST_MODEL_KEY") == passed
                and not extra)


def other_agents(td):
    """bench --agent gemini|hermes|openclaw against stand-ins: the switches, the work folder, each session's throwaway
    HOME (none of your configuration or credentials, only the --pass-env variables, deleted afterwards), its approvals
    (metacomputer's tools and command only; never a yolo), its command line, and its events read as the Claude ones are
    (tokens, no cost). Concurrent Hermes sessions each get a home of their own."""
    import argparse
    import threading
    stubs = os.path.join(td, "agents-bin")
    os.makedirs(stubs)
    pkg = os.path.join(td, "gwpkg", "hermes_cli")         # what the stand-in gateway runs: it waits
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    open(os.path.join(pkg, "main.py"), "w").write("import time\ntime.sleep(120)\n")
    # Another home's gateway (another Hermes session's, or yours): never this session's to stop.
    decoy = subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "gateway", "run", "--quiet"],
                             env=dict(os.environ, HERMES_HOME=os.path.join(td, "someone-elses-home", ".hermes"),
                                      PYTHONPATH=os.path.join(td, "gwpkg")), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for name, src in (("gemini", GEMINI_STUB), ("hermes", HERMES_STUB), ("openclaw", OPENCLAW_STUB)):
        with open(os.path.join(stubs, name), "w") as f:
            f.write(f"#!{sys.executable}\nSTUB_DIR = {td!r}\n" + src)
        os.chmod(os.path.join(stubs, name), 0o755)
    ns = lambda **k: argparse.Namespace(**dict({key: None for key in bench.DEFAULTS}, **k))   # noqa: E731
    for agent in ("gemini", "hermes", "openclaw"):
        sw = bench.switches_from(ns(agent=agent))
        check(f"--agent {agent} is a switch; the agent's own model unless --model names one; tokens, not dollars",
              sw["agent"] == agent and sw["model"] == agent + "-default" and bench.own_model(sw) is None
              and bench.tokens_only(agent) and not bench.tokens_only("claude")
              and bench.switches_from(ns(agent=agent, model="m-1"))["model"] == "m-1", sw)
    for bad in ("other",):
        try:
            bench.switches_from(ns(agent=bad))
            refused = False
        except SystemExit:
            refused = True
        check("an agent the benchmark doesn't know is refused", refused)
    refused = []
    for bad in ("KEY=value", "A,B=c", "1BAD", "$(x)"):
        try:
            bench.switches_from(ns(agent="gemini", pass_env=bad))
            refused.append(False)
        except SystemExit:
            refused.append(True)
    check("--pass-env takes variable names only: a value (NAME=...) or anything else is refused",
          all(refused) and bench.switches_from(ns(agent="gemini", pass_env="A_KEY, B_KEY"))["pass_env"] == "A_KEY, B_KEY"
          and bench.pass_env_names({"pass_env": "A_KEY, B_KEY"}) == ["A_KEY", "B_KEY"], refused)
    saved = {k: os.environ.get(k) for k in list(AMBIENT) + ["PATH", "BENCH_TEST_MODEL_KEY"]}
    os.environ.update(AMBIENT)
    os.environ["BENCH_TEST_MODEL_KEY"] = "passed-on-purpose"
    os.environ["PATH"] = stubs + os.pathsep + saved["PATH"]
    real_home = os.path.expanduser("~")
    tmp = os.path.realpath(tempfile.gettempdir())

    def leftover_homes():
        return sorted(p for p in os.listdir(tmp) if p.startswith("mcomp-bench-home-"))
    before_homes = leftover_homes()
    try:
        base = {"skill": os.path.join(LAB, "skills", "metacomputer"), "coord_guard": "off", "shell": "allow",
                "budget": "1", "effort": "", "lessons": "off", "pass_env": "BENCH_TEST_MODEL_KEY"}
        task = {"prompt": "Do the task.", "timeout_s": 60, "max_turns": 30}
        # Gemini CLI
        sw = dict(base, agent="gemini", model="gemini-default")
        work, cfg, shim = bench.prepare_workdir("g", sw)
        launcher = os.path.join(work, "agent-bin", "metacomputer")
        launchers = [launcher, os.path.join(work, "agent-bin", "mcomp")]
        skill = open(os.path.join(work, ".agents", "skills", "metacomputer", "SKILL.md")).read()
        check("an isolated session's metacomputer commands (metacomputer, mcomp): launchers in the work folder that run "
              "this checkout's with your HOME (its settings and keys are yours), the first named by the skill copy; no "
              ".gemini in the work folder", all(os.access(x, os.X_OK) and f"HOME={real_home}" in open(x).read()
                                                 .replace("'", "") and bench.CLI in open(x).read() for x in launchers)
              and launcher in skill and not os.path.exists(os.path.join(work, ".gemini")), launchers)
        tdir = os.path.join(td, "g-task")
        os.makedirs(tdir)
        events, _, sess = bench.run_session(task, sw, work, cfg, shim, tdir)
        recs = records(td, "gemini")
        rec = recs[-1] if recs else {}
        a = rec.get("argv", [])
        files = rec.get("files", {})
        settings = json.loads(files.get(os.path.join(".gemini", "settings.json"), "{}"))
        server = settings.get("mcpServers", {}).get("metacomputer", {})
        policy = files.get(os.path.join(".gemini", "policies", "metacomputer-bench.toml"), "")
        check("gemini: the session runs in a throwaway HOME, with none of your credentials or Gemini CLI overrides, only "
              "the --pass-env variable", isolated_env_ok(rec, real_home, "passed-on-purpose"),
              {k: rec.get("env", {}).get(k) for k in ("HOME", "BENCH_TEST_CANARY", "GEMINI_CLI_HOME", "BENCH_TEST_MODEL_KEY")})
        check("gemini: that HOME's settings: the server (the checkout's launcher, the run's environment with your HOME, "
              "trusted, 10 minutes), folder trust and telemetry off, no other server",
              list(settings.get("mcpServers", {})) == ["metacomputer"] and server.get("command") == launcher
              and server.get("env", {}).get("MCOMP_CONFIG") == os.environ["MCOMP_CONFIG"]
              and server.get("env", {}).get("HOME") == real_home and server.get("trust") is True
              and server.get("timeout") == 600000 and settings["security"]["folderTrust"]["enabled"] is False
              and settings["telemetry"]["enabled"] is False, settings)
        check("gemini: approval for metacomputer only, through a policy file: its server's tools and shell commands that "
              "start with its command; everything else as Gemini CLI asks (refused headless), no yolo anywhere",
              'mcpName = "metacomputer"' in policy and 'toolName = "*"' in policy
              and f"commandPrefix = {json.dumps([*launchers, 'metacomputer', 'mcomp'])}" in policy
              and policy.count('decision = "allow"') == 2 and "yolo" not in json.dumps(a) + policy + json.dumps(settings)
              and "tools" not in settings, policy)
        check("gemini: `gemini -p PROMPT -o stream-json --approval-mode default --skip-trust --allowed-mcp-server-names "
              "metacomputer` in the work folder, no -m for its own default", a[:1] == ["-p"] and a[1].endswith("Do the task.")
              and a[2:] == ["-o", "stream-json", "--approval-mode", "default", "--skip-trust", "--allowed-mcp-server-names",
                            "metacomputer"] and os.path.realpath(rec.get("cwd", "")) == os.path.realpath(work), a)
        uses = [c for e in events if e.get("type") == "assistant" for c in e["message"]["content"] if c.get("type") == "tool_use"]
        res = [c for e in events if e.get("type") == "user" for c in e["message"]["content"] if c.get("type") == "tool_result"]
        fin = [e for e in events if e.get("type") == "result"]
        check("gemini: its events as the Claude ones: mcp_metacomputer_* as mcp__metacomputer__*, its shell as Bash, each "
              "result after its call with its error state, and the result with tokens and no cost",
              [u["name"] for u in uses] == ["mcp__metacomputer__list_windows", "Bash"]
              and [r["is_error"] for r in res] == [False, True] and fin and fin[0]["total_cost_usd"] is None
              and fin[0]["usage"]["input_tokens"] == 1200 and sess["exit"] == 0, (uses, res, fin))
        check("gemini: the throwaway HOME is gone after the session",
              rec.get("env", {}).get("HOME") and not os.path.exists(rec["env"]["HOME"]), rec.get("env", {}).get("HOME"))
        shutil.rmtree(work, ignore_errors=True)
        # Hermes Agent
        sw = dict(base, agent="hermes", model="hermes-default")
        work, cfg, shim = bench.prepare_workdir("h", sw)
        tdir = os.path.join(td, "h-task")
        os.makedirs(tdir)
        events, _, sess = bench.run_session(task, sw, work, cfg, shim, tdir)
        recs = records(td, "hermes")
        chat = [r for r in recs if "chat" in r["argv"]]
        rec = chat[-1] if chat else {}
        a = rec.get("argv", [])
        hh = rec.get("env", {}).get("HERMES_HOME", "")
        sets = rec.get("sets", [])
        server = json.loads(next((v for k, v in sets if k == "mcp_servers.metacomputer"), "{}"))
        check("hermes: the session runs in a throwaway HOME (none of your credentials or Hermes overrides, only the "
              "--pass-env variable), with a Hermes home of its own inside it: no profile of yours, none made",
              isolated_env_ok(rec, real_home, "passed-on-purpose") and hh == os.path.join(rec["env"]["HOME"], ".hermes")
              and "-p" not in a and not any(r["argv"][:2] == ["profile", "create"] for r in recs), (hh, a))
        check("hermes: that home holds this run's server (its environment with your HOME, 600 s), tool search off, "
              "approvals asked for and a -q session's dangerous commands refused, and the skill under test linked in",
              server.get("command") == bench.agent_launcher(work)
              and server.get("env", {}).get("MCOMP_CONFIG") == os.environ["MCOMP_CONFIG"]
              and server.get("env", {}).get("HOME") == real_home and server.get("timeout") == 600
              and all(s in sets for s in (["tools.tool_search.enabled", "off"], ["approvals.mode", "manual"],
                                          ["approvals.single_query_mode", "deny"]))
              and rec.get("skill_link") == os.path.join(work, ".agents", "skills", "metacomputer"), sets)
        check("hermes: `hermes chat -q PROMPT -Q --format stream-json -t terminal,skills,metacomputer --max-turns N`, "
              "no --yolo", a[:2] == ["chat", "-q"] and a[3:11] == ["-Q", "--format", "stream-json", "-t",
                                                                    "terminal,skills,metacomputer", "--max-turns", "30",
                                                                    "--source"] and "--yolo" not in a, a)
        stops = [r for r in recs if r["argv"][:2] == ["gateway", "stop"]]
        check("hermes: afterwards the session home's gateway is stopped and the throwaway HOME is gone",
              stops and stops[-1]["env"].get("HERMES_HOME") == hh and hh and not os.path.exists(rec["env"]["HOME"]),
              (len(stops), hh))
        gws = gateway_pids(td)
        check("hermes: the gateway the session started for its home is stopped afterwards, though Hermes's own stop "
              "(which looks with ps) left it running", gws and not any(running(g) for g in gws), gws)
        check("...and another home's gateway is left alone", running(decoy.pid))
        uses = [c for e in events if e.get("type") == "assistant" for c in e["message"]["content"] if c.get("type") == "tool_use"]
        res = [c for e in events if e.get("type") == "user" for c in e["message"]["content"] if c.get("type") == "tool_result"]
        fin = [e for e in events if e.get("type") == "result"]
        check("hermes: its events as the Claude ones (terminal as Bash, errors kept), the result with tokens, no cost",
              [u["name"] for u in uses] == ["mcp__metacomputer__click", "Bash"] and [r["is_error"] for r in res] == [False, True]
              and fin and fin[0]["usage"]["output_tokens"] == 40 and fin[0]["total_cost_usd"] is None, (uses, res, fin))
        # Hermes Agent loads its installation's .env and the administrator-managed /etc/hermes into every run, whatever
        # its home (Codex's second re-check): while one is there the session doesn't start, and the file is named.
        inst = os.path.join(td, "hermes-install")
        managed = os.path.join(td, "etc-hermes")
        os.makedirs(inst, exist_ok=True)
        os.makedirs(managed, exist_ok=True)
        old_managed = getattr(bench.hooks("hermes"), "HERMES_MANAGED", None)
        bench.hooks("hermes").HERMES_MANAGED = managed
        refused = {}
        try:
            for label, path, says in (("its installation's .env", os.path.join(inst, ".env"), None),
                                      ("the managed .env", os.path.join(managed, ".env"), None),
                                      ("the managed config.yaml", os.path.join(managed, "config.yaml"), None),
                                      ("an install directory it doesn't name", None, "Install directory unknown")):
                if path:
                    open(path, "w").write("OPENAI_API_KEY=from-the-install\n")
                if says:
                    open(os.path.join(td, "hermes-install-says"), "w").write(says)
                n0 = len([r for r in records(td, "hermes") if "chat" in r["argv"]])
                try:
                    bench.run_session(task, sw, work, cfg, shim, tdir)
                    why = None
                except SystemExit as e:
                    why = str(e)
                ran = len([r for r in records(td, "hermes") if "chat" in r["argv"]]) > n0
                refused[label] = bool(why) and not ran and (path is None or path in why)
                for f in (path, os.path.join(td, "hermes-install-says")):
                    if f and os.path.exists(f):
                        os.remove(f)
        finally:
            bench.hooks("hermes").HERMES_MANAGED = old_managed
        check("hermes: a .env in Hermes's installation, a managed /etc/hermes .env or config.yaml (Hermes loads them "
              "whatever its home), or an install directory `hermes --version` doesn't name: no session starts, and the "
              "file is named", all(refused.values()) and len(refused) == 4, refused)
        shutil.rmtree(work, ignore_errors=True)
        # Two Hermes runs at once, each with its own test VM settings: each session sees its own server, never the other's.
        runs = []
        for i in (1, 2):
            os.environ["MCOMP_CONFIG"] = os.path.join(td, f"vm-{i}.env")
            w, c, s = bench.prepare_workdir(f"hc{i}", sw)
            d_ = os.path.join(td, f"hc{i}-task")
            os.makedirs(d_)
            runs.append((w, c, s, d_))
        open(os.path.join(td, "hermes-sleep"), "w").write("1.5")
        n0 = len([r for r in records(td, "hermes") if "chat" in r["argv"]])
        ths = [threading.Thread(target=bench.run_session, args=(task, sw, w, c, s, d_)) for w, c, s, d_ in runs]
        for th in ths:
            th.start()
        for th in ths:
            th.join(60)
        os.remove(os.path.join(td, "hermes-sleep"))
        os.environ["MCOMP_CONFIG"] = os.path.join(td, "test.env")
        chats = [r for r in records(td, "hermes") if "chat" in r["argv"]][n0:]
        seen = {}
        for r in chats:
            srv = json.loads(next((v for k, v in r.get("sets", []) if k == "mcp_servers.metacomputer"), "{}"))
            seen[os.path.realpath(r["cwd"])] = (r["env"].get("HERMES_HOME"), srv.get("env", {}).get("MCOMP_CONFIG"),
                                                r.get("skill_link"))
        want = {os.path.realpath(w): (os.path.join(td, f"vm-{i}.env"), os.path.join(w, ".agents", "skills", "metacomputer"))
                for i, (w, _, _, _) in enumerate(runs, 1)}
        check("hermes: two runs at once each get a Hermes home of their own, holding that run's server (its test VM) and "
              "skill, and both are removed afterwards",
              len(chats) == 2 and set(seen) == set(want) and len({v[0] for v in seen.values()}) == 2
              and all(seen[k][1:] == want[k] for k in want if k in seen)
              and not any(os.path.exists(v[0] or "/nonexistent") for v in seen.values()), seen)
        for w, _, _, _ in runs:
            shutil.rmtree(w, ignore_errors=True)
        gws = gateway_pids(td)
        check("hermes: every session's gateway is stopped, the two at once included; the other home's still runs",
              len(gws) >= 3 and not any(running(g) for g in gws) and running(decoy.pid), (gws, running(decoy.pid)))
        hermes_version_checks(td, stubs)
        # OpenClaw
        sw = dict(base, agent="openclaw", model="openclaw-default")
        work, cfg, shim = bench.prepare_workdir("o", sw)
        launchers = [os.path.join(work, "agent-bin", n) for n in ("metacomputer", "mcomp")]
        tdir = os.path.join(td, "o-task")
        os.makedirs(tdir)
        events, _, sess = bench.run_session(task, sw, work, cfg, shim, tdir)
        recs = records(td, "openclaw")
        allow = [r for r in recs if r["argv"][:3] == ["approvals", "allowlist", "add"]]
        ex = [r for r in recs if r["argv"][:2] == ["agent", "exec"]]
        rec = ex[-1] if ex else {}
        a = rec.get("argv", [])
        env = rec.get("env", {})
        state = env.get("OPENCLAW_STATE_DIR", "")
        conf = json.loads(rec.get("files", {}).get(os.path.join(".openclaw", "openclaw.json"), "{}"))
        srv = conf.get("mcp", {}).get("servers", {}).get("metacomputer", {})
        tools = conf.get("tools", {})
        check("openclaw: the session runs in a throwaway HOME (none of your credentials or OpenClaw overrides, only the "
              "--pass-env variable), with a state folder and a config file of its own inside it",
              isolated_env_ok(rec, real_home, "passed-on-purpose") and state == os.path.join(env["HOME"], ".openclaw")
              and env.get("OPENCLAW_CONFIG_PATH") == os.path.join(state, "openclaw.json"), env.get("HOME"))
        check("openclaw: that config: this run's server (its environment with your HOME, 600 s), no other; the minimal "
              "tool profile plus metacomputer's tools, exec, reading the work folder and the skill tools; tool search off",
              list(conf.get("mcp", {}).get("servers", {})) == ["metacomputer"] and srv.get("command") == launchers[0]
              and srv.get("env", {}).get("MCOMP_CONFIG") == os.environ["MCOMP_CONFIG"]
              and srv.get("env", {}).get("HOME") == real_home and srv.get("requestTimeoutMs") == 600000
              and tools.get("profile") == "minimal" and tools.get("alsoAllow") == bench.hooks("openclaw").OPENCLAW_TOOLS
              and tools.get("toolSearch") is False and tools.get("fs") == {"workspaceOnly": True}, conf)
        check("openclaw: exec in allowlist mode (misses refused) with only metacomputer's commands allowed, in the "
              "session's own state folder", tools.get("exec") == {"mode": "allowlist"}
              and [r["argv"][3:] for r in allow] == [["--agent", "*", x] for x in launchers]
              and all(r["env"].get("OPENCLAW_STATE_DIR") == state for r in allow), [r["argv"] for r in allow])
        check("openclaw: `openclaw agent exec PROMPT --json --cwd WORK --state-dir STATE --auth-env-only`, no override "
              "of your config (there is none to override), the skill in the workspace's skills/",
              a[:2] == ["agent", "exec"] and "--json" in a and a[a.index("--cwd") + 1] == work
              and a[a.index("--state-dir") + 1] == state and "--auth-env-only" in a and "--config-override" not in a
              and os.path.isfile(os.path.join(work, "skills", "metacomputer", "SKILL.md")), a)
        fin = [e for e in events if e.get("type") == "result"]
        check("openclaw: its one envelope (over several lines) read as init and the result: tokens, no cost, its tool "
              "summary", fin and fin[0]["usage"]["input_tokens"] == 700 and fin[0]["total_cost_usd"] is None
              and fin[0]["tool_summary_calls"] == 4 and not fin[0]["is_error"], (events, fin))
        m = bench.metrics.analyze(events)
        check("openclaw: the prompt tokens it read from and wrote to the cache are counted too (input tokens: input + "
              "cacheRead + cacheWrite)", fin and fin[0]["usage"].get("cache_read_input_tokens") == 50000
              and fin[0]["usage"].get("cache_creation_input_tokens") == 1200 and m["input_tokens"] == 51900,
              (fin and fin[0]["usage"], m["input_tokens"]))
        check("openclaw: the throwaway HOME is gone after the session", env.get("HOME") and not os.path.exists(env["HOME"]))
        shutil.rmtree(work, ignore_errors=True)
        check("no throwaway HOME is left behind by any session", leftover_homes() == before_homes,
              sorted(set(leftover_homes()) - set(before_homes)))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        decoy.kill()
        decoy.wait()
        for g in gateway_pids(td):
            try:
                os.kill(g, 9)
            except OSError:
                pass



def gateway_pids(td):
    p = os.path.join(td, "hermes-gateways.txt")
    return [int(x) for x in open(p).read().split()] if os.path.isfile(p) else []


def running(pid, wait=3.0):
    """Whether pid is a live process (not a zombie) after up to WAIT seconds for it to go."""
    import time
    end = time.monotonic() + wait
    while True:
        st = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not st or st.startswith("Z"):
            return False
        if time.monotonic() >= end:
            return True
        time.sleep(0.1)


def hermes_version_checks(td, stubs):
    """run.json's Hermes version: Hermes reads it with git, and says vunknown where git can't run (a sandbox). Then the
    release tag on its checkout's HEAD, read from the checkout's files; else the commit; else "unknown", with no v."""
    prog = os.path.join(stubs, "hermes")
    says = os.path.join(td, "hermes-version-says")
    inst = os.path.join(td, "hermes-install")
    git = os.path.join(inst, ".git")
    sha, tag_obj, other = "caafe5389ae585ef77387a962aee05b1bcf17029", "2861fe3744f8134b782659438961a4f69ab5d495", "1" * 40
    version = getattr(bench, "agent_version", None)
    got = {}
    try:
        got["with git"] = version("hermes", prog) if version else None
        open(says, "w").write("Hermes Agent vunknown (2026.9.24)")
        os.makedirs(os.path.join(git, "refs", "tags"))
        open(os.path.join(git, "HEAD"), "w").write(sha + "\n")
        open(os.path.join(git, "packed-refs"), "w").write(
            f"# pack-refs with: peeled fully-peeled sorted \n{tag_obj} refs/tags/v0.23.3\n^{sha}\n"
            f"{other} refs/tags/v0.24.0\n")
        open(os.path.join(git, "refs", "tags", "rc.1-v0.23.3"), "w").write(sha + "\n")
        got["tag"] = version("hermes", prog) if version else None
        open(os.path.join(git, "HEAD"), "w").write(other[:-1] + "2\n")
        got["no tag"] = version("hermes", prog) if version else None
        shutil.rmtree(git)
        got["no checkout"] = version("hermes", prog) if version else None
    finally:
        for f in (says,):
            if os.path.exists(f):
                os.remove(f)
        shutil.rmtree(git, ignore_errors=True)
    check("hermes: run.json's version is Hermes's own line where it can tell (git ran)",
          got.get("with git") == "Hermes Agent v0.23.3 (2026.9.24)", got)
    check("...where it says vunknown (git couldn't run): the release tag on its checkout's HEAD, read without git "
          "(an annotated tag, peeled; not another commit's tag, nor a pre-release)",
          got.get("tag") == "Hermes Agent v0.23.3 (2026.9.24)", got)
    check("...a checkout with no release tag on HEAD: its commit, as Hermes names one (git.SHORT)",
          got.get("no tag") == "Hermes Agent git.1111111 (2026.9.24)", got)
    check("...and no checkout to read: unknown, never vunknown", got.get("no checkout") == "Hermes Agent unknown (2026.9.24)",
          got)


def test_own_commands_env():
    """metacomputer's own commands that bench runs (reset, start: the VM runner, the gateway and the hub keep running
    after them) and a session's launchers get no --pass-env variable: a stand-in CLI that leaves a daemon behind, as
    start does, and a child; each writes down its environment, and the daemon's is read again from the kernel while it
    runs."""
    import time
    import uuid
    td = tempfile.mkdtemp(prefix="mcomp-bench-env-")
    key = "fake-model-key-" + uuid.uuid4().hex
    fake = os.path.join(td, "fake-metacomputer")
    dumps = os.path.join(td, "dumps")
    os.makedirs(dumps)
    with open(fake, "w") as f:
        f.write(f"#!{sys.executable}\n" + f"import json, os, subprocess, sys\nd = {dumps!r}\n" + r"""
tag = (sys.argv[1:] or ["none"])[0]
json.dump(dict(os.environ), open(os.path.join(d, f"cli-{tag}-{os.getpid()}.json"), "w"))
subprocess.run(["/bin/sh", "-c", 'env > "$0"', os.path.join(d, f"child-{tag}-{os.getpid()}.env")])
if os.fork() == 0:                      # a daemon, as start leaves one: its own session, still running afterwards
    os.setsid()
    if os.fork() == 0:
        n = os.open(os.devnull, os.O_RDWR)  # no descriptor of the command's (scripts/lib.sh daemonize)
        for fd in (0, 1, 2):
            os.dup2(n, fd)
        json.dump(dict(os.environ), open(os.path.join(d, f"daemon-{tag}-{os.getpid()}.json"), "w"))
        # (an interpreter, not one of Apple's programs: the kernel shows no other process Apple's programs' environment)
        os.execv(sys.executable, [sys.executable, "-c", "import time; time.sleep(30)"])
    os._exit(0)
""")
    os.chmod(fake, 0o755)
    saved_cli, saved_key, saved_cfg = bench.CLI, os.environ.get("BENCH_TEST_MODEL_KEY"), os.environ.get("MCOMP_CONFIG")
    os.environ["BENCH_TEST_MODEL_KEY"] = key
    os.environ.setdefault("MCOMP_CONFIG", os.path.join(td, "test.env"))
    bench.CLI = fake
    sw = {"skill": os.path.join(LAB, "skills", "metacomputer"), "coord_guard": "off", "shell": "allow", "budget": "1",
          "effort": "", "lessons": "off", "pass_env": "BENCH_TEST_MODEL_KEY", "agent": "hermes", "model": "hermes-default"}
    daemons, work = [], None
    try:
        if hasattr(bench, "sessions_only"):
            bench.sessions_only(sw)         # as bench run does, before any of metacomputer's commands
        bench.mc("reset", "--yes")
        bench.mc("start")
        work, _, _ = bench.prepare_workdir("env", sw)
        subprocess.run([os.path.join(work, "agent-bin", "metacomputer"), "session"], env=dict(os.environ),
                       capture_output=True, timeout=60)
        end = time.monotonic() + 10
        while time.monotonic() < end and len([f for f in os.listdir(dumps) if f.startswith("daemon-")]) < 3:
            time.sleep(0.1)
        seen, files = {}, sorted(os.listdir(dumps))
        for f in files:
            text = open(os.path.join(dumps, f)).read()
            seen[f] = key in text or "BENCH_TEST_MODEL_KEY" in text
            if f.startswith("daemon-"):
                daemons.append(int(f.rsplit("-", 1)[1].split(".")[0]))
        live = {}
        try:
            sys.path.insert(0, os.path.join(LAB, "lib"))
            import procinfo
            for pid in daemons:
                end = time.monotonic() + 10         # once it has started its program (the daemon's own, waiting)
                while time.monotonic() < end and (procinfo.argv(pid) or [""])[-1] != "import time; time.sleep(30)":
                    time.sleep(0.1)
                e = procinfo.environ(pid)
                live[pid] = bool(e) and "BENCH_TEST_MODEL_KEY" not in e and key not in e.values()
        except ImportError:
            live = {"no lib/procinfo.py": False}
        tags = {f.split("-")[1] for f in files}
        check("reset and start, as bench runs them, and a session's metacomputer launcher: neither the command, a child "
              "of it, nor the daemon it leaves running has a --pass-env variable (a fake key)",
              {"reset", "start", "session"} <= tags and len(files) == 9 and not any(seen.values()), seen)
        check("...nor has the daemon now, as the kernel has its environment", len(live) == 3 and all(live.values()), live)
        check("...while the session itself still gets it (only metacomputer's commands go without)",
              bench.session_env(sw, td).get("BENCH_TEST_MODEL_KEY") == key)
    finally:
        bench.CLI = saved_cli
        getattr(bench, "SESSION_ONLY", set()).clear()
        for k, v in (("BENCH_TEST_MODEL_KEY", saved_key), ("MCOMP_CONFIG", saved_cfg)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for pid in daemons:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        if work:
            shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(td, ignore_errors=True)


def test_session_launchers_env():
    """Every agent's session, Claude Code's and Codex's (which keep your environment) included: its metacomputer commands
    (metacomputer and mcomp on its PATH, the launcher its skill copy names) and its MCP server, as the agent starts them
    with the --pass-env variable (a fake key) in its environment, get no such variable; the agent itself still does."""
    import glob
    import re
    import uuid
    td = tempfile.mkdtemp(prefix="mcomp-bench-launch-")
    key = "fake-model-key-" + uuid.uuid4().hex
    dumps = os.path.join(td, "dumps")
    os.makedirs(dumps)
    fake = os.path.join(td, "fake-metacomputer")
    with open(fake, "w") as f:
        f.write(f"#!{sys.executable}\nimport json, os, sys\nd = {dumps!r}\n"
                "tag = (sys.argv[1:] or ['none'])[0]\n"
                "json.dump({'has': 'BENCH_TEST_MODEL_KEY' in os.environ or any(" + repr(key) + " in v for v in "
                "os.environ.values()), 'home': os.environ.get('HOME')}, open(os.path.join(d, tag + '.json'), 'w'))\n")
    os.chmod(fake, 0o755)
    agent_prog = os.path.join(td, "fake-agent")
    with open(agent_prog, "w") as f:
        f.write(f"#!{sys.executable}\nimport json, os, subprocess, sys\nd = {dumps!r}\na = sys.argv[1:]\n" + r"""
json.dump({"key": os.environ.get("BENCH_TEST_MODEL_KEY")}, open(os.path.join(d, "agent-env.json"), "w"))
subprocess.run(["/bin/sh", "-c", "metacomputer agent-shell"])            # its shell tool: PATH as the session has it
srv = None
if "--mcp-config" in a:                                                   # Claude Code: its --mcp-config file
    srv = json.load(open(a[a.index("--mcp-config") + 1]))["mcpServers"]["metacomputer"]["command"]
for x in a:                                                               # Codex: its -c override
    if x.startswith("mcp_servers.metacomputer.command="):
        srv = json.loads(x.split("=", 1)[1])
if srv:
    subprocess.run([srv, "agent-mcp"])                                    # its MCP server, with the agent's environment
print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0, "num_turns": 1}), flush=True)
""")
    os.chmod(agent_prog, 0o755)
    saved = (bench.CLI, os.environ.get("BENCH_TEST_MODEL_KEY"), os.environ.get("MCOMP_CONFIG"), os.environ.get("CODEX_HOME"))
    os.environ["BENCH_TEST_MODEL_KEY"] = key
    os.environ.setdefault("MCOMP_CONFIG", os.path.join(td, "test.env"))
    os.environ["CODEX_HOME"] = os.path.join(td, "codex-home")         # never your own Codex settings
    bench.CLI = fake
    try:
        for agent in sorted(bench.BENCH_AGENTS):
            h = bench.hooks(agent)
            sw = {"skill": os.path.join(LAB, "skills", "metacomputer"), "coord_guard": "off", "shell": "allow",
                  "budget": "1", "effort": "", "lessons": "off", "pass_env": "BENCH_TEST_MODEL_KEY", "agent": agent,
                  "model": f"{agent}-default"}
            for x in os.listdir(dumps):
                os.remove(os.path.join(dumps, x))
            work, cfg, shim = bench.prepare_workdir("k" + agent[:3], sw)
            try:
                first = os.path.dirname(bench.agent_launcher(work)) if h.ISOLATED else shim     # first on its PATH
                env = dict(os.environ, PATH=first + os.pathsep + os.environ["PATH"])
                text = "".join(open(os.path.join(top, n)).read() for top, _, names in os.walk(work) for n in names
                               if n == "SKILL.md")
                named = sorted(set(re.findall(r"(/[^\s`'\"]*/agent-bin/metacomputer)\b", text)))
                runs = [("path-metacomputer", ["/bin/sh", "-c", "metacomputer path-metacomputer"]),
                        ("path-mcomp", ["/bin/sh", "-c", "mcomp path-mcomp"])]
                runs += [(f"skill-{i}", [n, f"skill-{i}"]) for i, n in enumerate(named)]
                servers = [json.load(open(cfg))["mcpServers"]["metacomputer"]["command"]]
                if h.ISOLATED:
                    servers.append(bench.agent_server(cfg, "timeout", 600)["command"])     # the agent's own config's
                runs += [(f"mcp-{i}", [c, f"mcp-{i}"]) for i, c in enumerate(servers)]
                for _, argv in runs:
                    subprocess.run(argv, env=env, capture_output=True, timeout=60)
                if not h.ISOLATED:
                    # A session itself, with a stand-in agent: what it receives, and what it starts.
                    tdir = os.path.join(td, f"{agent}-task")
                    os.makedirs(tdir, exist_ok=True)
                    bench.run_session({"prompt": "x", "timeout_s": 60, "max_turns": 5}, sw, work, cfg, shim, tdir,
                                      prog=agent_prog)
                    runs += [("agent-shell", None), ("agent-mcp", None)]
                    got = json.load(open(os.path.join(dumps, "agent-env.json"))) if os.path.exists(
                        os.path.join(dumps, "agent-env.json")) else {}
                    check(f"{agent}: the agent's own session still gets the --pass-env variable (only metacomputer's "
                          "commands go without)", got.get("key") == key, got)
                seen = {}
                for tag, _ in runs:
                    pth = os.path.join(dumps, tag + ".json")
                    seen[tag] = json.load(open(pth)) if os.path.exists(pth) else None
                check(f"{agent}: its metacomputer commands (metacomputer and mcomp on its PATH, the launcher its skill "
                      "names, its MCP server) get no --pass-env variable (a fake key), and keep your HOME",
                      named and all(v is not None and not v["has"] and v["home"] == os.path.expanduser("~")
                                    for v in seen.values()), (named, seen))
            finally:
                shutil.rmtree(work, ignore_errors=True)
    finally:
        bench.CLI = saved[0]
        for k, v in zip(("BENCH_TEST_MODEL_KEY", "MCOMP_CONFIG", "CODEX_HOME"), saved[1:]):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(td, ignore_errors=True)


def test_hermes_gateway_identity():
    """Hermes's gateways for a session's home are told by their executable (a Python interpreter), `-m hermes_cli.main`
    and `gateway run` in their places, and that HERMES_HOME: a process of the same home whose arguments only name hermes,
    gateway and run (a script's, -c code's, another Hermes command's, another program's) is never signalled. And each
    signal, the first SIGTERM too, goes only to the same process (pid and start time, read fresh): never one that took
    the pid meanwhile, nor when that can't be told."""
    import time
    import types
    hh_mod = bench.hooks("hermes")
    td = tempfile.mkdtemp(prefix="mcomp-bench-gwid-")
    hh = os.path.join(td, "home", ".hermes")
    os.makedirs(hh)
    pkg = os.path.join(td, "pkg", "hermes_cli")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    open(os.path.join(pkg, "main.py"), "w").write("import time\ntime.sleep(120)\n")
    report = os.path.join(td, "report.py")
    open(report, "w").write("import time\ntime.sleep(120)\n")
    env = dict(os.environ, HERMES_HOME=hh, PYTHONPATH=os.path.join(td, "pkg"))
    spawn = lambda argv: subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: E731
                                          stderr=subprocess.DEVNULL, start_new_session=True)
    py = sys.executable
    decoys = {
        "a script with hermes gateway run as its arguments (Codex's)": [py, report, "hermes", "gateway", "run"],
        "-c code, then -m hermes_cli.main gateway run as its arguments": [py, "-c", "import time; time.sleep(120)", "-m",
                                                                          "hermes_cli.main", "gateway", "run"],
        "Hermes's own module, another command (chat ... gateway run)": [py, "-m", "hermes_cli.main", "chat", "-q",
                                                                         "gateway", "run"],
        "another program (sh) naming hermes_cli.main gateway run": ["/bin/sh", "-c", "sleep 120", "-m",
                                                                    "hermes_cli.main", "gateway", "run"],
    }
    procs = {k: spawn(v) for k, v in decoys.items()}
    gw = spawn([py, "-m", "hermes_cli.main", "--profile", "default", "gateway", "run", "--quiet", "--idle-exit"])
    gw2 = spawn([py, "-u", "-m", "hermes_cli.main", "gateway", "run", "--quiet"])
    try:
        time.sleep(1.0)
        hh_mod.stop_gateways(bench, hh)
        check("hermes: its gateways of the session's home are stopped (`-m hermes_cli.main [--profile NAME] gateway "
              "run`, a Python interpreter's)", not running(gw.pid) and not running(gw2.pid), (gw.pid, gw2.pid))
        alive = {k: running(p.pid, wait=0.5) for k, p in procs.items()}
        check("hermes: a process of the same home that only names hermes, gateway and run in its arguments is never "
              "signalled", all(alive.values()), alive)
    finally:
        for p in (*procs.values(), gw, gw2):
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
            p.wait()

    # PID reuse: the gateway found ended, and its pid is another process's when the signal would go. Each case has a
    # process of its own standing in for that other process: a signal reaches it if one is sent.
    import procinfo as real_pi
    found_start = (1000, 0)
    argv_ = [py, "-m", "hermes_cli.main", "gateway", "run", "--quiet"]

    def fake(pid, now):
        return types.SimpleNamespace(
            kernel_procs=lambda: [real_pi.Proc((pid, 1, pid, os.getuid(), False, b"Python", found_start))],
            argv=lambda p: argv_, exe=lambda p: py, environ=lambda p: {"HERMES_HOME": hh}, kernel_proc=now)

    def broken(pid):
        raise OSError("can't read")
    reused = lambda pid: real_pi.Proc((pid, 1, pid, os.getuid(), False, b"Python", (2000, 0)))  # noqa: E731
    same = lambda pid: real_pi.Proc((pid, 1, pid, os.getuid(), False, b"Python", found_start))  # noqa: E731
    stop_s = hh_mod.GATEWAY_STOP_S
    hh_mod.GATEWAY_STOP_S = 1                    # (a stand-in that never reports the process gone: its wait is short)
    try:
        for label, now, want_alive in (("another process has its pid now (a later start time)", reused, True),
                                       ("it can't be told (no answer for the pid)", lambda pid: None, True),
                                       ("it can't be told (the read fails)", broken, True),
                                       ("...and the same process (pid and start time as found)", same, False)):
            other = subprocess.Popen([py, "-c", "import time; time.sleep(120)"], start_new_session=True)
            try:
                try:
                    hh_mod.stop_gateways(types.SimpleNamespace(procinfo=fake(other.pid, now)), hh)
                except Exception:
                    pass
                alive = running(other.pid, wait=0.5)
            finally:
                try:
                    other.kill()
                except OSError:
                    pass
                other.wait()
            if want_alive:
                check(f"hermes: no signal, not even the first SIGTERM, when {label}", alive, label)
            else:
                check(f"hermes: {label} is stopped", not alive, label)
    finally:
        hh_mod.GATEWAY_STOP_S = stop_s
        shutil.rmtree(td, ignore_errors=True)


def test_cli_counting():
    """bench's metrics count a metacomputer command as one wherever a shell runs it: after cd ... &&, env, or a variable
    holding its path (Hermes's sessions: M=.../metacomputer; $M call ...), quoted, past its --vm option, in a pipeline,
    sh -c; the rest is other Bash."""
    def kinds(cmd):
        m = bench.metrics.analyze([
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "c1", "name": "Bash",
                                                            "input": {"command": cmd}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]}}])
        return m["cli_calls"], m["shell_calls"], m["bash_other"]
    w = "/private/tmp/x/mcomp-bench-r-ab12"
    cases = [
        (f"M={w}/agent-bin/metacomputer; $M call run_steps '{{\"pid\": 1}}'", ({"call": 1}, 0, 0)),
        (f"M={w}/agent-bin/metacomputer; printf '%s' '{{}}' | $M call run_steps -", ({"call": 1}, 0, 0)),
        (f"M={w}/agent-bin/metacomputer; $M sh --read-only -c 'ls ~'", ({}, 1, 0)),
        (f"M={w}/agent-bin/metacomputer\n\"${{M}}\" call run_steps - <<'JSON'\n{{\"steps\": [\"mcomp shot\"]}}\nJSON",
         ({"call": 1}, 0, 0)),
        (f"cd \"{w}\" && \"{w}/agent-bin/metacomputer\" call list_windows '{{}}'", ({"call": 1}, 0, 0)),
        (f"cd {w} && env -i HOME=/h ./agent-bin/metacomputer --vm test who", ({"who": 1}, 0, 0)),
        (f"cd {w} && agent-bin/metacomputer call get_window_state '{{}}'", ({"call": 1}, 0, 0)),
        ("timeout 60 mcomp --vm=test status --json", ({"status": 1}, 0, 0)),
        ("bash -c 'cd /w && metacomputer shot'", ({"shot": 1}, 0, 0)),
        ("ls -la; cat notes.txt", ({}, 0, 1)),
        ("grep -r metacomputer .", ({}, 0, 1)),
    ]
    got = [(c, kinds(c)) for c, _ in cases]
    bad = [(c[:60], g, want) for (c, g), (_, want) in zip(got, cases) if g != want]
    check(f"metrics: a metacomputer command after cd ... &&, env, or in a variable is a CLI call; the rest other Bash "
          f"({len(cases)} commands)", not bad, bad)
    # sh, bash and zsh read -c in a cluster of short options too (-lc, -ic, -ec), past their other options; and where
    # the parse is partial, what the plain regex counted (before the parse) still counts.
    shells = [
        ('metacomputer who; bash -lc "metacomputer sh -c true"', ({}, 1, 0)),     # Codex's: the shell use counts
        ('bash -lc "metacomputer call list_windows {}"', ({"call": 1}, 0, 0)),
        ("zsh -ic 'cd /w && mcomp sh ls'", ({}, 1, 0)),
        ("sh -ec 'metacomputer who'", ({"who": 1}, 0, 0)),
        ("bash -xec 'metacomputer ssh x'", ({}, 1, 0)),
        ('bash -eo pipefail -c "metacomputer status --json | head"', ({"status": 1}, 0, 0)),
        ("bash --norc -lc 'metacomputer shot'", ({"shot": 1}, 0, 0)),
        ("zsh -o errexit -fc 'metacomputer sh -c true'", ({}, 1, 0)),
        ("bash -c -e 'metacomputer who' zero", ({"who": 1}, 0, 0)),
        ("bash -l ./setup.sh", ({}, 0, 1)),
        ("metacomputer who; sudo -u me metacomputer sh ls", ({}, 1, 0)),           # sudo: not parsed, the regex has it
        ("metacomputer status; xargs -n1 metacomputer call < calls.txt", ({"status": 1}, 0, 0)),
    ]
    got = [(c, kinds(c)) for c, _ in shells]
    bad = [(c[:60], g, want) for (c, g), (_, want) in zip(got, shells) if g != want]
    check(f"metrics: sh/bash/zsh -lc, -ic, -ec and any cluster with c (past -o NAME, --norc...) run what follows; where "
          f"the parse is partial, nothing the regex counted is lost ({len(shells)} commands)", not bad, bad)


def main():
    test_codex_trust_undo()
    test_own_commands_env()
    test_session_launchers_env()
    test_hermes_gateway_identity()
    test_cli_counting()
    td = tempfile.mkdtemp(prefix="mcomp-bench-test-")
    run_id = "test-" + os.path.basename(td)
    shared = os.path.join("/tmp", "mcomp-bench", run_id)
    try:
        os.environ.setdefault("MCOMP_CONFIG", os.path.join(td, "test.env"))
        other_agents(td)
        return run(td, run_id, shared)
    finally:
        shutil.rmtree(td, ignore_errors=True)
        shutil.rmtree(shared, ignore_errors=True)


def run(td, run_id, shared):
    os.environ.setdefault("MCOMP_CONFIG", os.path.join(td, "test.env"))
    sw = {"skill": os.path.join(LAB, "skills", "metacomputer"), "coord_guard": "off", "shell": "allow",
          "model": "dummy-model", "budget": "1", "effort": ""}
    planted = os.path.join(td, "planted-ran")
    # Another account's trap at the old predictable path.
    os.makedirs(os.path.join(shared, "work", "bin"))
    fake_claude(os.path.join(shared, "work", "bin", "claude"), planted)

    work, cfg, shim = bench.prepare_workdir(run_id, sw)
    work2, _, _ = bench.prepare_workdir(run_id, sw)
    try:
        st = os.lstat(work)
        check("the work folder is new and private: 0700, yours, not a link", stat.S_ISDIR(st.st_mode)
              and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.getuid(), oct(st.st_mode))
        check("...not the old shared path, and a new name for each run", not work.startswith("/tmp/mcomp-bench")
              and work != work2 and os.path.realpath(shim).startswith(os.path.realpath(work)), (work, work2))
        check("...the shim folder holds only the links to the session's launchers (which run this checkout's)",
              sorted(os.listdir(shim)) == ["mcomp", "metacomputer"]
              and all(os.readlink(os.path.join(shim, n)) == os.path.join(work, "agent-bin", n) for n in ("mcomp", "metacomputer"))
              and all(bench.CLI in open(os.path.join(work, "agent-bin", n)).read() for n in ("mcomp", "metacomputer")))
        check("...with the skill under test and the MCP config", os.path.isfile(os.path.join(work, ".claude", "skills",
                                                                                               "metacomputer", "SKILL.md"))
              and os.path.isfile(cfg))

        good_dir = os.path.join(td, "trusted-bin")
        os.makedirs(good_dir)
        ran = os.path.join(td, "trusted-ran")
        fake_claude(os.path.join(good_dir, "claude"), ran)
        rel_dir = os.path.join(td, "rel")
        os.makedirs(rel_dir)
        fake_claude(os.path.join(rel_dir, "claude"), planted)
        old_cwd = os.getcwd()
        os.chdir(rel_dir)
        try:
            got = bench.trusted_prog("." + os.pathsep + good_dir)
        finally:
            os.chdir(old_cwd)
        check("trusted_claude: an absolute path, never a relative PATH entry", got == os.path.realpath(os.path.join(good_dir, "claude")),
              got)
        os.chmod(os.path.join(good_dir, "claude"), 0o777)
        try:
            bench.trusted_prog(good_dir)
            refused = False
        except SystemExit:
            refused = True
        os.chmod(os.path.join(good_dir, "claude"), 0o755)
        check("trusted_claude: a claude other accounts can write is refused", refused)

        # A session: the shim folder goes first on PATH, and a claude planted there isn't run.
        fake_claude(os.path.join(shim, "claude"), planted)
        tdir = os.path.join(td, "task")
        os.makedirs(tdir)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = good_dir + os.pathsep + old_path
        try:
            events, _, sess = bench.run_session({"prompt": "dummy", "timeout_s": 30}, sw, work, cfg, shim, tdir)
        finally:
            os.environ["PATH"] = old_path
        check("a session runs the claude found before the shim folder, by absolute path", sess["exit"] == 0
              and os.path.exists(ran) and open(ran).read().strip() == os.path.realpath(os.path.join(good_dir, "claude"))
              and not os.path.exists(planted), (sess, os.path.exists(planted)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(work2, ignore_errors=True)

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
