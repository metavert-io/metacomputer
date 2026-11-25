"""Codex's bench launcher (bench/bench.py --agent codex; the harness calls these with itself as B)."""
import json
import os
import re
import stat

SKILL_HOME = ".agents"          # the skill goes in the work folder's .agents/skills/
ISOLATED = False                # its sessions run with your HOME (Codex's own sandbox; the work folder trusted)
TOKENS_ONLY = True              # it reports tokens, never a cost


def model(sw, default):
    """Codex runs its own default model unless --model names a GPT one."""
    return sw["model"] if sw["model"].startswith(("gpt", "o")) else "codex-default"


def prepare(B, work, shim):
    """Codex reads the project's rules once the folder is trusted (the session's -c says so): metacomputer's, for the
    commands as the skill and the shim name them (the work folder's launchers, which run the checkout's without the
    --pass-env variables), so they run outside its sandbox as an install sets up."""
    names = ["metacomputer", "mcomp", *B.agent_launchers(work), os.path.join(shim, "metacomputer"),
             os.path.join(shim, "mcomp")]
    rules = open(os.path.join(B.LAB, "config", "codex", "metacomputer.rules.in")).read()
    os.makedirs(os.path.join(work, ".codex", "rules"))
    open(os.path.join(work, ".codex", "rules", "metacomputer.rules"), "w").write(
        rules.replace("@COMMANDS@", json.dumps(names)))


def args(B, t, sw, cfg):
    """`codex exec` for one task: its MCP server as the Claude sessions get it (command, args, env) and as an install
    sets it up (a 600 s tool timeout, its tools approved: they act only on the VM, through the hub's guards), the work
    folder trusted (its skill and rules), Codex's default sandbox, no approvals to wait for."""
    srv = json.load(open(cfg))["mcpServers"]["metacomputer"]
    env = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in srv["env"].items()) + "}"
    work = os.path.dirname(cfg)
    a = ["exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-c", 'approval_policy="never"',
         "-c", f"mcp_servers.metacomputer.command={json.dumps(srv['command'])}",
         "-c", 'mcp_servers.metacomputer.args=["mcp-lazy"]', "-c", "mcp_servers.metacomputer.tool_timeout_sec=600",
         "-c", 'mcp_servers.metacomputer.default_tools_approval_mode="approve"',
         "-c", f"mcp_servers.metacomputer.env={env}", "-c", f'projects.{json.dumps(work)}.trust_level="trusted"']
    if sw["model"].startswith(("gpt", "o")):
        a += ["-m", sw["model"]]
    a.append(B.PREFIX + t["prompt"].strip() + (B.SHELL_FORBID if sw["shell"] == "forbid" else ""))
    return a, None             # no dollar budget: Codex has none to set and reports tokens, not a cost


def before(B):
    """Your config.toml before the session (after() puts it back)."""
    return B.read_or_none(codex_config_path())


def after(B, before_text, cfg):
    undo_codex_trust(B, before_text, os.path.dirname(cfg))


def events(B, ev, state):
    """One `codex exec --json` event as stream-json events (what metrics.py and transcripts.py read): a finished MCP
    call or shell command as an assistant tool_use plus the user's tool_result, an agent message as text, the end of
    the turn as the result (tokens, no cost)."""
    t = ev.get("type")
    it = ev.get("item") if isinstance(ev.get("item"), dict) else {}
    if t == "thread.started":
        state["sid"] = ev.get("thread_id")
        return [{"type": "system", "subtype": "init", "session_id": ev.get("thread_id"), "model": "codex",
                 "cwd": state.get("cwd"), "tools": []}]
    if t == "turn.completed":
        u = ev.get("usage") or {}
        state["turns"] = state.get("turns", 0) + 1
        return [{"type": "result", "subtype": "success", "is_error": False, "num_turns": state["calls"],
                 "total_cost_usd": None, "session_id": state.get("sid"),
                 "usage": {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                           "cache_read_input_tokens": u.get("cached_input_tokens")}}]
    if t == "turn.failed":
        return [{"type": "result", "subtype": "error", "is_error": True, "total_cost_usd": None,
                 "session_id": state.get("sid"), "result": str((ev.get("error") or {}).get("message"))[:300]}]
    if t != "item.completed":
        return []
    kind = it.get("type")
    if kind == "agent_message":
        return [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": it.get("text") or ""}]}}]
    if kind not in ("mcp_tool_call", "command_execution"):
        return []
    state["calls"] = state.get("calls", 0) + 1
    cid = f"codex_{it.get('id') or state['calls']}"
    if kind == "mcp_tool_call":
        name = f"mcp__{it.get('server')}__{it.get('tool')}"
        inp = it.get("arguments") if isinstance(it.get("arguments"), dict) else {}
        res = it.get("result") if isinstance(it.get("result"), dict) else {}
        content = res.get("content") if isinstance(res.get("content"), list) else []
        err = it.get("error")
        is_err = bool(err) or it.get("status") == "failed" or res.get("isError") is True or res.get("is_error") is True
        if err and not content:
            content = [{"type": "text", "text": str(err.get("message") if isinstance(err, dict) else err)}]
    else:
        name, inp = "Bash", {"command": it.get("command") or ""}
        content = [{"type": "text", "text": (it.get("aggregated_output") or "")[:20000]}]
        is_err = it.get("status") == "failed" or (isinstance(it.get("exit_code"), int) and it["exit_code"] != 0)
    return [{"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": cid, "name": name, "input": inp}]}, "session_id": state.get("sid")},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": cid, "content": content, "is_error": is_err}]},
             "session_id": state.get("sid")}]


def codex_config_path(env=None):
    env = os.environ if env is None else env
    return os.path.join(env.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "config.toml")


def undo_codex_trust(B, before, work, path=None):
    """Codex saves the work folder's trust (a `-c projects.<work>.trust_level` override) into your own config.toml when
    a session starts. Put the file back exactly as it was when that table is the only change; anything else changed
    in it is left alone, with a note. Returns what was done: "unchanged", "restored" or "left"."""
    path = path or codex_config_path()
    after_text = B.read_or_none(path)
    if after_text == before or after_text is None or before is None:
        return "unchanged"
    stripped = after_text
    for name in {work, os.path.realpath(work)}:
        stripped = re.sub(r"\n*\[projects\." + re.escape(json.dumps(name)) + r"\]\ntrust_level = \"trusted\"\n",
                          "\n", stripped)
    squeeze = lambda s: re.sub(r"\n\s*\n", "\n", s).strip()      # noqa: E731  (blank lines don't count)
    if squeeze(stripped) != squeeze(before):
        B.log(f"  note: Codex changed {path} during the run in other ways too; left as it is")
        return "left"
    if B.read_or_none(path) != after_text:     # changed again meanwhile: leave it
        return "left"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    tmp = path + ".mcomp-bench.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(before)
    os.replace(tmp, path)
    return "restored"
