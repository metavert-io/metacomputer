"""Gemini CLI's sessions for `metacomputer learn` (lib/transcripts.py reads them: FORMAT, MARKER, roots). Gemini CLI keeps
them as ~/.gemini/tmp/<project>/chats/session-*.jsonl (GEMINI_CLI_HOME moves ~/.gemini; checked with Gemini CLI 0.70.0):
a first line with sessionId and projectHash, then one line per message ({"id", "timestamp", "type": "user" | "gemini" |
..., "toolCalls": [{"id", "name", "args", "status", "result", ...}]}), a message written again with the same id as its
tool calls move on (the last copy counts) and {"$set": ...} lines for the first one. Its MCP tools are named
mcp_<server>_<tool> (mcp_metacomputer_click); run_shell_command is read as a Bash call. The project folder comes from
~/.gemini/projects.json, which names each project's folder under tmp/."""
import json
import os

from transcripts import (MAX_CALLS, TEXT_KEEP, TEXT_KEEP_WINDOWS, _str, _ts, bounded, mcp_tool,  # noqa: F401
                         open_regular)

KIND = "gemini"                             # its sessions' Transcript kind
GEMINI_PREFIX = "mcp_metacomputer_"         # Gemini CLI's name for the metacomputer server's tools
GEMINI_DONE = ("success", "error")          # a tool call that ran (scheduled, executing, cancelled ... didn't, or not yet)
# A Gemini CLI tool call of the metacomputer server, as its session files write it (uses_tools: lib/transcripts.py).
MARKER = rb'"name":\s?"mcp_metacomputer_[a-z_]'


def gemini_sessions_dir(env=None):
    """Gemini CLI's per-project folders, each with its chats/: ~/.gemini/tmp ($GEMINI_CLI_HOME/.gemini/tmp)."""
    env = os.environ if env is None else env
    base = env.get("GEMINI_CLI_HOME") or env.get("HOME") or os.path.expanduser("~")
    return os.path.join(base, ".gemini", "tmp")


def roots(env):
    """The folders lib/learn_mine.py walks for its session files."""
    return [gemini_sessions_dir(env)]


def gemini_project(path):
    """The project folder of a Gemini CLI session file, from projects.json beside tmp/ ({"projects": {folder: name}},
    the name being the folder under tmp/ that holds chats/); None when it can't be told."""
    parts = os.path.abspath(path).split(os.sep)
    if "tmp" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index("tmp")
    if i + 1 >= len(parts):
        return None
    try:
        with open_regular(os.sep.join(parts[:i] + ["projects.json"])) as f:
            d = json.loads(f.read(1 << 20))
    except (OSError, ValueError):
        return None
    projects = d.get("projects") if isinstance(d, dict) else None
    hits = [k for k, v in (projects or {}).items() if v == parts[i + 1] and isinstance(k, str)] if isinstance(projects, dict) else []
    return hits[0][:4096] if len(hits) == 1 else None


def _gemini_text(result):
    """A Gemini CLI tool call's result parts as text (the output its functionResponse carries, without the
    <untrusted_context> wrapper Gemini CLI puts around an MCP result), and how many images they hold."""
    texts, imgs = [], 0
    for part in result if isinstance(result, list) else []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("inlineData"), dict):
            imgs += 1
        fr = part.get("functionResponse")
        resp = fr.get("response") if isinstance(fr, dict) else None
        if isinstance(resp, dict):
            v = resp.get("output") if isinstance(resp.get("output"), str) else resp.get("error")
            if isinstance(v, str):
                texts.append(v[:TEXT_KEEP_WINDOWS])
            parts = fr.get("parts")
            for x in parts if isinstance(parts, list) else []:
                if isinstance(x, dict) and isinstance(x.get("inlineData"), dict):
                    imgs += 1
    t = "\n".join(texts)
    if t.startswith("<untrusted_context>"):
        t = t[len("<untrusted_context>"):].removesuffix("</untrusted_context>").strip("\n")
    return t, imgs


def _gemini_call(tc, t, last_t, slim):
    """A finished Gemini CLI tool call as a call (or None): an MCP tool of the metacomputer server, run_shell_command
    as a Bash call, or another tool by its name."""
    if not isinstance(tc, dict) or tc.get("status") not in GEMINI_DONE:
        return None
    raw = _str(tc.get("name"), 200) or ""
    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    t_use = _ts({"timestamp": tc.get("timestamp")}) or t
    if raw == "run_shell_command":
        name, inp = "Bash", {"command": _str(args.get("command"), 2000 if slim else 4096) or ""}
    else:
        name = "mcp__metacomputer__" + raw[len(GEMINI_PREFIX):] if raw.startswith(GEMINI_PREFIX) else raw
        inp = (bounded(args) if mcp_tool(name) else {}) if slim else args
    drv = mcp_tool(name)
    text, imgs = _gemini_text(tc.get("result"))
    keep = TEXT_KEEP_WINDOWS if drv in ("list_windows", "launch_app") else TEXT_KEEP
    return {"id": _str(tc.get("id"), 200) or f"gemini-{raw}", "name": name, "tool": drv, "input": inp, "ctx": "",
            "t_use": t_use, "t_prev": last_t, "error": tc.get("status") == "error",
            "text": "" if slim and name == "Bash" else text[:keep], "images": imgs, "t_result": t}


def take(tr, ev, typ, t, st, slim):
    """One event of a Gemini CLI session file, read into tr (lib/transcripts.py parse: True when it's Gemini CLI's). Its
    first line (sessionId and projectHash, no type) makes the transcript Gemini CLI's; every line after is read as one."""
    if not (tr.kind == "gemini" or (typ is None and "projectHash" in ev and isinstance(ev.get("sessionId"), str))):
        return False
    if tr.kind != "gemini":                                               # a Gemini CLI session's first line
        tr.kind = "gemini"
        tr.session_id = _str(ev.get("sessionId"), 200)
        tr.cwd = gemini_project(tr.path) if tr.path else None
        tr.gemini_seen = set()
        return True
    if typ == "user" and t is not None:
        st.last_t = t
    elif typ == "gemini" and isinstance(ev.get("toolCalls"), list):
        for tc in ev["toolCalls"]:
            key = _str(tc.get("id"), 200) if isinstance(tc, dict) else None
            if not key or key in tr.gemini_seen:
                continue
            if len(tr.calls) >= MAX_CALLS:
                tr.info["truncated"] = "calls"
                break
            call = _gemini_call(tc, t, st.last_t, slim)
            if call is not None:
                tr.gemini_seen.add(key)              # the message is written again as its calls move on
                tr.calls.append(call)
                st.last_t = call["t_result"] or st.last_t
    return True


FORMAT = (30, take)     # tried after Antigravity CLI's (20), before Codex's (40): lib/transcripts.py formats()
