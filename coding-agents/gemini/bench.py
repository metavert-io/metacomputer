"""Gemini CLI's bench launcher (bench/bench.py --agent gemini; the harness calls these with itself as B)."""
import json
import os

SKILL_HOME = ".agents"          # the skill goes in the work folder's .agents/skills/
ISOLATED = True                 # each session in a throwaway HOME (isolate)
TOKENS_ONLY = True              # it reports tokens, never a cost
SHELL_TOOLS = ("run_shell_command",)   # its shell tool, read as Bash (bench/bench.py _shell_or_mcp)
TOOL_PREFIX = "mcp_metacomputer_"      # how it names metacomputer's tools (read as mcp__metacomputer__*)
# An isolated Gemini CLI session's policy (~/.gemini/policies in its throwaway HOME): metacomputer's tools and its
# command (toolName "*" with mcpName: every tool of that server); the rest keeps Gemini CLI's defaults.
GEMINI_POLICY = """# metacomputer's benchmark session: its tools and its command, without asking.
[[rule]]
mcpName = "metacomputer"
toolName = "*"
decision = "allow"
priority = 900

[[rule]]
toolName = "run_shell_command"
commandPrefix = %s
decision = "allow"
priority = 900
"""


def model(sw, default):
    """Its own default model unless --model names one."""
    return "gemini-default" if sw["model"] == default else sw["model"]


def isolate(B, sw, work, cfg, home, prog, env):
    """A policy file (Gemini CLI's policy engine, user tier) that allows metacomputer's server's tools and shell
    commands that start with metacomputer's command, and nothing more: every other tool keeps Gemini CLI's default
    rule, and what that asks for a headless session refuses (--approval-mode default, no yolo); folder trust and
    telemetry off in the throwaway settings; only metacomputer's server allowed."""
    prompt = B.prompt_of(sw.get("_task"), sw)
    os.makedirs(os.path.join(home, ".gemini"), 0o700)
    settings = {"privacy": {"usageStatisticsEnabled": False}, "telemetry": {"enabled": False},
                "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False},
                "security": {"folderTrust": {"enabled": False}},
                "mcpServers": {"metacomputer": dict(B.agent_server(cfg, "timeout", 600000), trust=True)}}
    json.dump(settings, open(os.path.join(home, ".gemini", "settings.json"), "w"), indent=2)
    os.makedirs(os.path.join(home, ".gemini", "policies"), 0o700)
    with open(os.path.join(home, ".gemini", "policies", "metacomputer-bench.toml"), "w") as f:
        f.write(GEMINI_POLICY % json.dumps([*B.agent_launchers(work), "metacomputer", "mcomp"]))
    args = ["-p", prompt, "-o", "stream-json", "--approval-mode", "default", "--skip-trust",
            "--allowed-mcp-server-names", "metacomputer"]
    return args + (["-m", B.own_model(sw)] if B.own_model(sw) else []), env


def events(B, ev, state):
    """One `gemini -o stream-json` event as stream-json events: init; a tool_use is held until its tool_result (by
    tool_id) and both become a finished call; assistant text; the result (tokens, no cost)."""
    t = ev.get("type")
    if t == "init":
        state["sid"] = ev.get("session_id")
        return [{"type": "system", "subtype": "init", "session_id": ev.get("session_id"), "model": ev.get("model"),
                 "cwd": state.get("cwd"), "tools": []}]
    if t == "message" and ev.get("role") == "assistant":
        return [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": ev.get("content") or ""}]}}]
    if t == "tool_use":
        state.setdefault("pending", {})[ev.get("tool_id")] = (ev.get("tool_name") or "",
                                                               ev.get("parameters") if isinstance(ev.get("parameters"), dict) else {})
        return []
    if t == "tool_result":
        name, inp = state.get("pending", {}).pop(ev.get("tool_id"), ("unknown", {}))
        name, inp = B._shell_or_mcp(name, inp)
        text = ev.get("output") if isinstance(ev.get("output"), str) else ""
        if not text and isinstance(ev.get("error"), dict):
            text = str(ev["error"].get("message") or "")
        return B._call(state, name, inp, [{"type": "text", "text": text[:20000]}], ev.get("status") == "error",
                       f"gemini_{ev.get('tool_id')}")
    if t == "result":
        st = ev.get("stats") or {}
        ok = ev.get("status") == "success"
        return [{"type": "result", "subtype": "success" if ok else "error", "is_error": not ok,
                 "num_turns": state.get("calls", 0), "total_cost_usd": None, "session_id": state.get("sid"),
                 "usage": {"input_tokens": st.get("input_tokens"), "output_tokens": st.get("output_tokens"),
                           "cache_read_input_tokens": st.get("cached")}}]
    return []
