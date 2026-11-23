#!/usr/bin/env python3
"""The benchmark's session folder and the `claude` it runs (bench/bench.py), and the other agents' sessions (--agent
gemini|hermes|openclaw, against stand-ins: each in a throwaway HOME with only the --pass-env variables, approval for
metacomputer only, their command lines, their events). No VM, no API use: a stand-in `claude` records how it was
started.

    python3 tests/test_bench_workdir.py

It checks that the work folder is a fresh private folder (0700, yours, a new name each
time, not the old shared /tmp/mcomp-bench/<run>/work), that the shim folder holds only
the two links to this checkout's launcher, and that the session runs the `claude` found
on PATH before the shim folder went in front: one planted in the shim folder, in the old
shared path or in a relative PATH entry is never run, and one other accounts can write
is refused.
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
        r = bench.undo_codex_trust(before, work, cfg)
        check("the run's own trust table is the only change: the file is put back exactly, its mode kept",
              r == "restored" and open(cfg).read() == before and stat.S_IMODE(os.stat(cfg).st_mode) == 0o600, r)
        with open(cfg, "w") as f:
            f.write(before + added + 'personality = "friendly"\n')
        r = bench.undo_codex_trust(before, work, cfg)
        check("...any other change too: the file is left as it is", r == "left" and "personality" in open(cfg).read(), r)
        other = f'\n[projects."{td}/someone-else"]\ntrust_level = "trusted"\n'
        with open(cfg, "w") as f:
            f.write(before + other)
        r = bench.undo_codex_trust(before, work, cfg)
        check("...another folder's trust (not this run's) is left too", r == "left" and "someone-else" in open(cfg).read(), r)
        with open(cfg, "w") as f:
            f.write(before)
        check("...and an unchanged file isn't touched", bench.undo_codex_trust(before, work, cfg) == "unchanged")
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
    print("Hermes Agent v0.23.3 (2026.9.24)")
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
    log(snap()); sys.exit(0)
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
print(json.dumps({"ok": True, "status": "ok", "final": "Done.", "usage": {"input": 700, "output": 30}, "assistantTurns": 3,
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
              list(settings.get("mcpServers", {})) == ["metacomputer"] and server.get("command") == bench.CLI
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
              server.get("command") == bench.CLI and server.get("env", {}).get("MCOMP_CONFIG") == os.environ["MCOMP_CONFIG"]
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
        old_managed = getattr(bench, "HERMES_MANAGED", None)
        bench.HERMES_MANAGED = managed
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
            bench.HERMES_MANAGED = old_managed
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
              list(conf.get("mcp", {}).get("servers", {})) == ["metacomputer"] and srv.get("command") == bench.CLI
              and srv.get("env", {}).get("MCOMP_CONFIG") == os.environ["MCOMP_CONFIG"]
              and srv.get("env", {}).get("HOME") == real_home and srv.get("requestTimeoutMs") == 600000
              and tools.get("profile") == "minimal" and tools.get("alsoAllow") == bench.OPENCLAW_TOOLS
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


def main():
    test_codex_trust_undo()
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
        check("...the shim folder holds only the links to this checkout's launcher",
              sorted(os.listdir(shim)) == ["mcomp", "metacomputer"]
              and all(os.readlink(os.path.join(shim, n)) == bench.CLI for n in ("mcomp", "metacomputer")))
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
            got = bench.trusted_claude("." + os.pathsep + good_dir)
        finally:
            os.chdir(old_cwd)
        check("trusted_claude: an absolute path, never a relative PATH entry", got == os.path.realpath(os.path.join(good_dir, "claude")),
              got)
        os.chmod(os.path.join(good_dir, "claude"), 0o777)
        try:
            bench.trusted_claude(good_dir)
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
