"""Codex's sessions for `metacomputer learn` (lib/transcripts.py reads them: FORMAT, MARKER, roots). Codex keeps its sessions as $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl (else ~/.codex/sessions): a
session_meta line (id, cwd), then events; each finished MCP call is an item_completed event holding an McpToolCall
(server, tool, arguments, result, status, start and end in ms), and each shell command a CommandExecution, which
this module reads as a Bash call so the same counts apply."""
import os

from transcripts import (MAX_CALLS, TEXT_KEEP, TEXT_KEEP_WINDOWS, _ms, _str, bounded, mcp_tool,  # noqa: F401
                         result_text)

KIND = "codex"                             # its sessions' Transcript kind
# A Codex call of the metacomputer server, as its session files write it (uses_tools: lib/transcripts.py).
MARKER = rb'"server":\s?"metacomputer",\s?"tool":\s?"[a-z_]'


def codex_sessions_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(base, "sessions")


def roots(env):
    """The folders lib/learn_mine.py walks for its session files (rollout-*.jsonl)."""
    return [codex_sessions_dir(env)]


def _codex_call(tr, ev, item, t, last_t, slim):
    """A Codex item_completed event as a call (or None): an McpToolCall, or a CommandExecution read as a Bash call."""
    kind = item.get("type")
    t_use = _ms(ev.get("started_at_ms")) or t
    t_end = _ms(ev.get("completed_at_ms")) or t
    cid = _str(item.get("id"), 200) or f"codex-{len(tr.calls)}"
    if kind == "McpToolCall":
        server, tool = _str(item.get("server"), 100) or "", _str(item.get("tool"), 100) or ""
        name = f"mcp__{server}__{tool}"[:200]
        inp = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        drv = mcp_tool(name)
        if slim:
            inp = bounded(inp) if drv else {}
        res = item.get("result") if isinstance(item.get("result"), dict) else {}
        text, imgs = result_text(res.get("content"))
        failed = res.get("isError") is True or item.get("status") in ("failed", "error")
        if failed and not text and isinstance(item.get("error"), (str, dict)):
            err = item["error"]
            text = err if isinstance(err, str) else _str(err.get("message")) or ""
        keep = TEXT_KEEP_WINDOWS if drv in ("list_windows", "launch_app") else TEXT_KEEP
        return {"id": cid, "name": name, "tool": drv, "input": inp, "ctx": "", "t_use": t_use, "t_prev": last_t,
                "error": bool(failed), "text": text[:keep], "images": imgs, "t_result": t_end}
    if kind == "CommandExecution":
        cmd = item.get("command")
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] in ("-lc", "-c", "-l") and isinstance(cmd[-1], str):
            cmd = cmd[-1]
        elif isinstance(cmd, list):
            cmd = " ".join(x for x in cmd if isinstance(x, str))
        cmd = _str(cmd, 2000 if slim else 4096) or ""
        code = item.get("exit_code")
        failed = item.get("status") in ("failed", "error") or (isinstance(code, int) and code != 0)
        return {"id": cid, "name": "Bash", "tool": None, "input": {"command": cmd}, "ctx": "", "t_use": t_use,
                "t_prev": last_t, "error": bool(failed), "text": "" if slim else (_str(item.get("aggregated_output"))
                                                                                   or ""),
                "images": 0, "t_result": t_end}
    return None


def take(tr, ev, typ, t, st, slim):
    """One line of a Codex session file, read into tr (lib/transcripts.py parse: True when it's Codex's, its
    session_meta or an event_msg)."""
    if typ == "session_meta" and isinstance(ev.get("payload"), dict):        # a Codex session
        pl = ev["payload"]
        tr.kind = "codex"
        tr.session_id = tr.session_id or _str(pl.get("id"), 200) or _str(pl.get("session_id"), 200)
        tr.cwd = tr.cwd or _str(pl.get("cwd"))
        return True
    if typ == "event_msg" and isinstance(ev.get("payload"), dict):
        pl = ev["payload"]
        item = pl.get("item") if isinstance(pl.get("item"), dict) else {}
        if pl.get("type") == "item_completed" and item.get("type") == "UserMessage":
            st.last_t = _ms(pl.get("completed_at_ms")) or t
        elif pl.get("type") == "item_completed":
            if len(tr.calls) >= MAX_CALLS:
                tr.info["truncated"] = "calls"
                return True
            call = _codex_call(tr, pl, item, t, st.last_t, slim)
            if call is not None:
                tr.calls.append(call)
                st.last_t = call["t_result"] or st.last_t   # the model's next input arrives when this call ends
        return True
    return False


FORMAT = (40, take)     # tried after Gemini CLI's (30): lib/transcripts.py formats()
