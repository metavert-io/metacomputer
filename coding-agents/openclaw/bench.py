"""OpenClaw's bench launcher (bench/bench.py --agent openclaw; the harness calls these with itself as B)."""
import json
import os
import subprocess

SKILL_HOME = ""                 # the skill goes in the work folder's skills/ (OpenClaw's workspace skills)
ISOLATED = True                 # each session in a throwaway HOME (isolate)
TOKENS_ONLY = True              # it reports tokens, never a cost
SHELL_TOOLS = ("exec",)                # its shell tool, read as Bash (bench/bench.py _shell_or_mcp)
ENVELOPE = True                 # its output is one JSON envelope, possibly over several lines (envelope_events)
# An isolated OpenClaw session's tools, on top of the minimal profile: metacomputer's, exec (allowlisted to its
# command), reading files in the work folder (tools.fs.workspaceOnly) and the skill tools.
OPENCLAW_TOOLS = ["metacomputer__*", "exec", "read", "skills_search", "skills_read"]


def model(sw, default):
    """Its own default model unless --model names one."""
    return "openclaw-default" if sw["model"] == default else sw["model"]


def isolate(B, sw, work, cfg, home, prog, env):
    """A state folder and a config file of the session's own (OPENCLAW_STATE_DIR, OPENCLAW_CONFIG_PATH: none of yours
    applies): the minimal tool profile plus metacomputer's tools, `exec`, reading files in the work folder and its
    skills, nothing else; exec in allowlist mode (misses refused, never asked) with an allowlist that holds only
    metacomputer's command; tool search off; model credentials from the environment only (--auth-env-only: no stored
    or other CLIs' credentials, as from the keychain)."""
    prompt = B.prompt_of(sw.get("_task"), sw)
    state = os.path.join(home, ".openclaw")
    os.makedirs(state, 0o700)
    conf = os.path.join(state, "openclaw.json")
    env = dict(env, OPENCLAW_STATE_DIR=state, OPENCLAW_CONFIG_PATH=conf, OPENCLAW_NO_AUTO_UPDATE="1")
    json.dump({"mcp": {"servers": {"metacomputer": B.agent_server(cfg, "requestTimeoutMs", 600000)}},
               "tools": {"profile": "minimal", "alsoAllow": OPENCLAW_TOOLS, "toolSearch": False,
                         "fs": {"workspaceOnly": True}, "exec": {"mode": "allowlist"}}},
              open(conf, "w"), indent=2)
    for launcher in B.agent_launchers(work):
        r = subprocess.run([prog, "approvals", "allowlist", "add", "--agent", "*", launcher], capture_output=True,
                           text=True, env=env, timeout=120, stdin=subprocess.DEVNULL, cwd=home)
        if r.returncode != 0:
            raise RuntimeError("couldn't allow metacomputer's command in the session's OpenClaw state folder")
    args = ["agent", "exec", prompt, "--json", "--cwd", work, "--state-dir", state, "--auth-env-only",
            "--timeout", str(int(sw["_task"].get("timeout_s", 600)))]
    return args + (["--model", B.own_model(sw)] if B.own_model(sw) else []), env


def _count(u, *keys):
    """The first of keys that usage has, else None."""
    return next((u[k] for k in keys if k in u), None)


def envelope_events(env_obj, state):
    """`openclaw agent exec --json`'s one envelope as stream-json events: init and the result (tokens, no cost). It
    reports a summary of the tool calls, not each one, so the per-call metrics of these runs are empty. OpenClaw counts
    the prompt tokens read from and written to the provider's cache apart from `input` (cacheRead, cacheWrite): they
    are the result's cache_read_input_tokens and cache_creation_input_tokens, which bench's input tokens add up."""
    if not isinstance(env_obj, dict):
        return []
    u = env_obj.get("usage") if isinstance(env_obj.get("usage"), dict) else {}
    ok = env_obj.get("ok") is True and env_obj.get("status") == "ok"
    summary = env_obj.get("toolSummary") if isinstance(env_obj.get("toolSummary"), dict) else {}
    return [{"type": "system", "subtype": "init", "session_id": env_obj.get("sessionId"), "model": env_obj.get("model"),
             "cwd": state.get("cwd"), "tools": []},
            {"type": "result", "subtype": "success" if ok else "error", "is_error": not ok,
             "num_turns": env_obj.get("assistantTurns"), "total_cost_usd": None, "session_id": env_obj.get("sessionId"),
             "tool_summary_calls": summary.get("calls"),
             "usage": {"input_tokens": _count(u, "input", "inputTokens"),
                       "output_tokens": _count(u, "output", "outputTokens"),
                       "cache_read_input_tokens": _count(u, "cacheRead"),
                       "cache_creation_input_tokens": _count(u, "cacheWrite")}}]
