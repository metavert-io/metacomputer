#!/usr/bin/env python3
"""The benchmark's session folder and the `claude` it runs (bench/bench.py), and the other agents' sessions (--agent
gemini|hermes|openclaw, against stand-ins: their command lines, the Hermes Agent profile, their events). No VM, no
API use: a stand-in `claude` records how it was started.

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

# Stand-ins for the other agents' headless modes: each logs its arguments (one JSON line per run, in $STUB_DIR) and
# prints what the real one prints (Gemini CLI 0.70.0's stream-json, Hermes Agent 0.23.3's, OpenClaw 2026.11.7's
# `agent exec --json` envelope).
STUB_HEAD = """import json, os, sys
a = sys.argv[1:]
d = os.environ["STUB_DIR"]
open(os.path.join(d, os.path.basename(sys.argv[0]) + ".argv"), "a").write(json.dumps({"argv": a, "cwd": os.getcwd()}) + "\\n")
def out(o): print(json.dumps(o), flush=True)
"""
GEMINI_STUB = STUB_HEAD + """
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
prof = os.path.join(d, "hermes-home", "profiles", "metacomputer-bench")
w = [x for x in a if x not in ("-p", "metacomputer-bench")] if a[:2] == ["-p", "metacomputer-bench"] else a
if a == ["--version"]: print("Hermes Agent v0.23.3"); sys.exit(0)
if w[:2] == ["profile", "create"]: os.makedirs(prof); sys.exit(0)
if w == ["config", "path"]:
    if not os.path.isdir(prof): sys.exit(1)
    print(os.path.join(prof, "config.yaml")); sys.exit(0)
if w[:2] == ["config", "set"]:
    open(os.path.join(prof, "set.jsonl"), "a").write(json.dumps(w[2:]) + "\\n"); sys.exit(0)
out({"type": "system", "subtype": "init", "model": "nous/hermes", "session_id": "h-1", "timestamp": 1})
out({"type": "tool_use", "name": "mcp__metacomputer__click", "tool_call_id": "c1", "input": {"element_token": "a:1"}})
out({"type": "tool_result", "name": "mcp__metacomputer__click", "tool_call_id": "c1", "output": "clicked", "duration_ms": 30, "is_error": False})
out({"type": "tool_use", "name": "terminal", "tool_call_id": "c2", "input": {"command": "metacomputer status"}})
out({"type": "tool_result", "name": "terminal", "tool_call_id": "c2", "output": "{}", "duration_ms": 5, "is_error": True})
out({"type": "text", "text": "Done."})
out({"type": "result", "session_id": "h-1", "exit_code": 0, "text": "Done.", "tokens": {"input": 900, "output": 40, "total": 940, "cache_read": 0, "cache_write": 0}, "duration_ms": 10})
"""
OPENCLAW_STUB = STUB_HEAD + """
if a == ["--version"]: print("OpenClaw 2026.11.7 (e31bbf0)"); sys.exit(0)
print(json.dumps({"ok": True, "status": "ok", "final": "Done.", "usage": {"input": 700, "output": 30}, "assistantTurns": 3,
                  "toolSummary": {"calls": 4, "tools": {"metacomputer__click": 4}}, "model": "m", "sessionId": "o-1"}, indent=2))
"""


def other_agents(td):
    """bench --agent gemini|hermes|openclaw against stand-ins: the switches, the work folder, the command line each
    session runs, the Hermes Agent profile, and their events read as the Claude ones are (tokens, no cost)."""
    import argparse
    stubs = os.path.join(td, "agents-bin")
    os.makedirs(stubs)
    for name, src in (("gemini", GEMINI_STUB), ("hermes", HERMES_STUB), ("openclaw", OPENCLAW_STUB)):
        with open(os.path.join(stubs, name), "w") as f:
            f.write(f"#!{sys.executable}\n" + src)
        os.chmod(os.path.join(stubs, name), 0o755)
    ns = lambda **k: argparse.Namespace(**dict({key: None for key in bench.DEFAULTS}, **k))   # noqa: E731
    for agent in ("gemini", "hermes", "openclaw"):
        sw = bench.switches_from(ns(agent=agent))
        check(f"--agent {agent} is a switch; the agent's own model unless --model names one; tokens, not dollars",
              sw["agent"] == agent and sw["model"] == agent + "-default" and bench.own_model(sw) is None
              and bench.tokens_only(agent) and not bench.tokens_only("claude")
              and bench.switches_from(ns(agent=agent, model="m-1"))["model"] == "m-1", sw)
    try:
        bench.switches_from(ns(agent="other"))
        refused = False
    except SystemExit:
        refused = True
    check("an agent the benchmark doesn't know is refused", refused)
    os.environ["STUB_DIR"] = td
    old_path = os.environ["PATH"]
    os.environ["PATH"] = stubs + os.pathsep + old_path
    try:
        base = {"skill": os.path.join(LAB, "skills", "metacomputer"), "coord_guard": "off", "shell": "allow",
                "budget": "1", "effort": "", "lessons": "off"}
        task = {"prompt": "Do the task.", "timeout_s": 60, "max_turns": 30}
        # Gemini CLI
        sw = dict(base, agent="gemini", model="gemini-default")
        work, cfg, shim = bench.prepare_workdir("g", sw)
        settings = json.load(open(os.path.join(work, ".gemini", "settings.json")))["mcpServers"]["metacomputer"]
        check("gemini: the skill in the work folder's .agents/skills, and its .gemini/settings.json server: the checkout's "
              "launcher, the run's environment, trusted, 10 minutes", os.path.isfile(os.path.join(
                  work, ".agents", "skills", "metacomputer", "SKILL.md")) and settings["command"] == bench.CLI
              and settings["args"] == ["mcp-lazy"] and settings["env"]["MCOMP_CONFIG"] == os.environ["MCOMP_CONFIG"]
              and settings["trust"] is True and settings["timeout"] == 600000, settings)
        tdir = os.path.join(td, "g-task")
        os.makedirs(tdir)
        events, _, sess = bench.run_session(task, sw, work, cfg, shim, tdir)
        run = json.loads(open(os.path.join(td, "gemini.argv")).read().splitlines()[-1])
        a = run["argv"]
        check("gemini: `gemini -p PROMPT -o stream-json --approval-mode yolo --skip-trust --allowed-mcp-server-names "
              "metacomputer` in the work folder, no -m for its own default", a[0] == "-p" and a[1].endswith("Do the task.")
              and a[2:] == ["-o", "stream-json", "--approval-mode", "yolo", "--skip-trust", "--allowed-mcp-server-names",
                            "metacomputer"] and os.path.realpath(run["cwd"]) == os.path.realpath(work), run)
        uses = [c for e in events if e.get("type") == "assistant" for c in e["message"]["content"] if c.get("type") == "tool_use"]
        res = [c for e in events if e.get("type") == "user" for c in e["message"]["content"] if c.get("type") == "tool_result"]
        fin = [e for e in events if e.get("type") == "result"]
        check("gemini: its events as the Claude ones: mcp_metacomputer_* as mcp__metacomputer__*, its shell as Bash, each "
              "result after its call with its error state, and the result with tokens and no cost",
              [u["name"] for u in uses] == ["mcp__metacomputer__list_windows", "Bash"]
              and [r["is_error"] for r in res] == [False, True] and fin and fin[0]["total_cost_usd"] is None
              and fin[0]["usage"]["input_tokens"] == 1200 and sess["exit"] == 0, (uses, res, fin))
        shutil.rmtree(work, ignore_errors=True)
        # Hermes Agent
        sw = dict(base, agent="hermes", model="hermes-default")
        work, cfg, shim = bench.prepare_workdir("h", sw)
        tdir = os.path.join(td, "h-task")
        os.makedirs(tdir)
        events, _, sess = bench.run_session(task, sw, work, cfg, shim, tdir)
        prof = os.path.join(td, "hermes-home", "profiles", "metacomputer-bench")
        sets = [json.loads(x) for x in open(os.path.join(prof, "set.jsonl"))]
        server = json.loads(next(v for k, v in sets if k == "mcp_servers.metacomputer"))
        a = json.loads(open(os.path.join(td, "hermes.argv")).read().splitlines()[-1])["argv"]
        check("hermes: a profile of its own (made once), given this run's server with its environment and a 600 s timeout, "
              "tool search off, and the skill under test linked in", server["command"] == bench.CLI
              and server["env"]["MCOMP_CONFIG"] == os.environ["MCOMP_CONFIG"] and server["timeout"] == 600
              and ["tools.tool_search.enabled", "off"] in sets
              and os.readlink(os.path.join(prof, "skills", "metacomputer")) == os.path.join(work, ".agents", "skills",
                                                                                               "metacomputer"), sets)
        check("hermes: `hermes -p metacomputer-bench chat -q PROMPT -Q --yolo --format stream-json --max-turns N`",
              a[:4] == ["-p", "metacomputer-bench", "chat", "-q"] and a[5:12] == ["-Q", "--yolo", "--format", "stream-json",
                                                                                    "--max-turns", "30", "--source"], a)
        uses = [c for e in events if e.get("type") == "assistant" for c in e["message"]["content"] if c.get("type") == "tool_use"]
        res = [c for e in events if e.get("type") == "user" for c in e["message"]["content"] if c.get("type") == "tool_result"]
        fin = [e for e in events if e.get("type") == "result"]
        check("hermes: its events as the Claude ones (terminal as Bash, errors kept), the result with tokens, no cost",
              [u["name"] for u in uses] == ["mcp__metacomputer__click", "Bash"] and [r["is_error"] for r in res] == [False, True]
              and fin and fin[0]["usage"]["output_tokens"] == 40 and fin[0]["total_cost_usd"] is None, (uses, res, fin))
        events, _, _ = bench.run_session(task, sw, work, cfg, shim, tdir)
        check("hermes: a second session reuses the profile (made once) and links the skill again",
              open(os.path.join(td, "hermes.argv")).read().count('"profile", "create"') == 1
              and os.path.islink(os.path.join(prof, "skills", "metacomputer")))
        shutil.rmtree(work, ignore_errors=True)
        # OpenClaw
        sw = dict(base, agent="openclaw", model="openclaw-default")
        work, cfg, shim = bench.prepare_workdir("o", sw)
        tdir = os.path.join(td, "o-task")
        os.makedirs(tdir)
        events, _, sess = bench.run_session(task, sw, work, cfg, shim, tdir)
        a = json.loads(open(os.path.join(td, "openclaw.argv")).read().splitlines()[-1])["argv"]
        over = json.loads(a[a.index("--config-override") + 1])
        srv = over["mcp"]["servers"]["metacomputer"]
        fin = [e for e in events if e.get("type") == "result"]
        check("openclaw: `openclaw agent exec PROMPT --json --cwd WORK` with the server (its environment, 600 s) and tool "
              "search off for this run only, the skill in the workspace's skills/",
              a[:2] == ["agent", "exec"] and "--json" in a and a[a.index("--cwd") + 1] == work
              and srv["command"] == bench.CLI and srv["env"]["MCOMP_CONFIG"] == os.environ["MCOMP_CONFIG"]
              and srv["requestTimeoutMs"] == 600000 and over["tools"]["toolSearch"] is False
              and os.path.isfile(os.path.join(work, "skills", "metacomputer", "SKILL.md")), a)
        check("openclaw: its one envelope (over several lines) read as init and the result: tokens, no cost, its tool "
              "summary", fin and fin[0]["usage"]["input_tokens"] == 700 and fin[0]["total_cost_usd"] is None
              and fin[0]["tool_summary_calls"] == 4 and not fin[0]["is_error"], (events, fin))
        shutil.rmtree(work, ignore_errors=True)
    finally:
        os.environ["PATH"] = old_path
        os.environ.pop("STUB_DIR", None)


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
