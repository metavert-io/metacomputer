"""Antigravity CLI's sessions for `metacomputer learn` (lib/transcripts.py reads them: FORMAT, MARKER, files).
Antigravity CLI (agy) keeps a JSONL transcript of each conversation beside its own database (checked with agy 1.5.15):
<app data>/brain/<conversation id>/.system_generated/logs/transcript.jsonl, the app data folder being
~/.gemini/antigravity-cli (ANTIGRAVITY_APP_DATA_DIR, which agy gives the commands it runs). One step per line ({"step_index",
"source": "USER_EXPLICIT" | "MODEL" | "SYSTEM", "type": "USER_INPUT" | "PLANNER_RESPONSE" | ..., "status": "DONE" |
"ERROR", "created_at", "content", "error"}); a PLANNER_RESPONSE's "tool_calls" are [{"name", "args"}] with each argument
JSON-encoded as a string (this compact copy; transcript_full.jsonl beside it has them as they were), and the steps that
follow it, in order, are those calls' results. Its MCP tools are called through call_mcp_tool {"ServerName", "ToolName",
"Arguments"} (or, loaded eagerly, as mcp_<server>_<tool>), the server metacomputer, or metacomputer_metacomputer when a
plugin brought it; run_command is its shell, read as a Bash call. The project
folder comes from conversation_summaries.db's workspace_uris, read through lib/session_dbs.py (no write, no lock)."""
import json
import os
import re
import time

import wirelimits
from transcripts import (MAX_CALLS, TEXT_KEEP, TEXT_KEEP_WINDOWS, _str, bounded, mcp_tool)  # noqa: F401

KIND = "antigravity"                       # its sessions' Transcript kind
# Antigravity CLI's call_mcp_tool of the metacomputer server (its ServerName JSON-encoded in the compact transcript, as
# it was in the full one), as uses_tools (lib/transcripts.py) looks for it.
MARKER = rb'"ServerName":\s?"(?:\\")?metacomputer(?:_metacomputer)?(?:\\")?"'

AGY_SHELL = "run_command"
AGY_MCP = "call_mcp_tool"
# metacomputer's server as Antigravity CLI names it: registered by install.sh, or brought by a plugin (agy plugin install
# of the repository, converted from its Gemini CLI extension), whose servers it names <plugin>_<server>
AGY_SERVERS = ("metacomputer", "metacomputer_metacomputer")
AGY_EAGER = "mcp_metacomputer_"         # an MCP tool loaded eagerly: mcp_<server>_<tool>
AGY_STAMP = re.compile(r"^(?:Created|Completed) At: [^\n]{0,40}\n")
AGY_EXIT = re.compile(r"Command exited with code (-?\d+)")


def antigravity_dir(env=None):
    """Antigravity CLI's app data folder: ANTIGRAVITY_APP_DATA_DIR (agy sets it for the commands it runs), else
    ~/.gemini/antigravity-cli."""
    env = os.environ if env is None else env
    return env.get("ANTIGRAVITY_APP_DATA_DIR") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".gemini",
                                                                "antigravity-cli")


def antigravity_transcripts(env=None, limit=20000):
    """Every conversation's compact transcript: <app data>/brain/<id>/.system_generated/logs/transcript.jsonl (its chunks/
    copies and transcript_full.jsonl are left out: the same steps)."""
    brain = os.path.join(antigravity_dir(env), "brain")
    try:
        ids = sorted(os.listdir(brain))[:limit]
    except OSError:
        return []
    return [os.path.join(brain, i, ".system_generated", "logs", "transcript.jsonl") for i in ids
            if not i.startswith(".")]


_AGY_PROJECTS = {}


def antigravity_project(path):
    """The project folder of an Antigravity CLI conversation (the path of its transcript), from conversation_summaries.db
    beside brain/ (workspace_uris, a JSON list of file:// URIs: its first), read without a write or a lock
    (lib/session_dbs.py), once per database; None when it can't be told."""
    parts = os.path.abspath(path).split(os.sep)
    if "brain" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index("brain")
    if i + 1 >= len(parts):
        return None
    db = os.sep.join(parts[:i] + ["conversation_summaries.db"])
    if db not in _AGY_PROJECTS:
        found = {}
        try:
            import session_dbs              # here, not at the top: it imports this module
            con, _ = session_dbs.open_db(db, time.monotonic() + 30)
            try:
                for cid, uris in con.execute("SELECT conversation_id, workspace_uris FROM conversation_summaries "
                                             "LIMIT 100000"):
                    if isinstance(cid, str) and isinstance(uris, str):
                        found[cid] = uris[:8192]
            finally:
                con.close()
        except Exception:                   # no database, another layout, unreadable: no project known
            found = {}
        _AGY_PROJECTS[db] = found
    uris = _AGY_PROJECTS[db].get(parts[i + 1])
    try:
        first = json.loads(uris)[0] if uris else None
    except (ValueError, IndexError, TypeError):
        return None
    if not isinstance(first, str) or not first.startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse
    p = unquote(urlparse(first).path)
    return p[:4096] if p.startswith("/") else None


def _agy_args(args):
    """A tool call's arguments from the compact transcript: each value JSON-encoded as a string, decoded."""
    out = {}
    for k, v in (args.items() if isinstance(args, dict) else []):
        if isinstance(k, str) and isinstance(v, str):
            try:
                v = wirelimits.loads(v[:1 << 20])
            except (ValueError, RecursionError):
                pass
        out[k] = v
    return out


def _agy_call(tc, t, last_t, slim):
    """An Antigravity CLI tool call (from a PLANNER_RESPONSE) as a call, its result still to come: call_mcp_tool of the
    metacomputer server (or its eager mcp_metacomputer_<tool>) as mcp__metacomputer__<tool>, run_command as Bash, any
    other tool by its name."""
    raw = _str(tc.get("name"), 200) or ""
    args = _agy_args(tc.get("args"))
    if raw == AGY_SHELL:
        name, inp = "Bash", {"command": _str(args.get("CommandLine"), 2000 if slim else 4096) or ""}
    elif raw == AGY_MCP and args.get("ServerName") in AGY_SERVERS and isinstance(args.get("ToolName"), str):
        name = "mcp__metacomputer__" + args["ToolName"][:100]
        inp = args.get("Arguments")
        if isinstance(inp, str):
            try:
                inp = wirelimits.loads(inp[:1 << 20])
            except (ValueError, RecursionError):
                inp = {}
        inp = inp if isinstance(inp, dict) else {}
    elif raw.startswith(AGY_EAGER):
        rest = raw[len(AGY_EAGER):]
        rest = rest[len("metacomputer_"):] if rest.startswith("metacomputer_") else rest      # the plugin's server
        name, inp = "mcp__metacomputer__" + rest, args
    else:
        name, inp = raw, {}
    drv = mcp_tool(name)
    if slim and name != "Bash":
        inp = bounded(inp) if drv else {}
    return {"id": f"agy-{raw}", "name": name, "tool": drv, "input": inp, "ctx": "", "t_use": t, "t_prev": last_t,
            "error": None, "text": "", "images": 0, "t_result": None}


def _agy_result(call, ev, t, slim):
    """A step that carries a tool call's result: its status (ERROR), the error it gives, or its content without the
    Created At / Completed At lines agy puts first; a run_command that exited nonzero failed too."""
    content = _str(ev.get("content"), TEXT_KEEP_WINDOWS * 2) or ""
    while True:
        m = AGY_STAMP.match(content)
        if not m:
            break
        content = content[m.end():]
    err = ev.get("status") == "ERROR"
    text = _str(ev.get("error"), TEXT_KEEP_WINDOWS) if err and isinstance(ev.get("error"), str) else content
    if call["name"] == "Bash":
        m = AGY_EXIT.search(content[:200])
        err = err or bool(m and m.group(1) != "0")
    keep = TEXT_KEEP_WINDOWS if call["tool"] in ("list_windows", "launch_app") else TEXT_KEEP
    call.update(error=err, text="" if slim and call["name"] == "Bash" else (text or "")[:keep], t_result=t)


def files(env):
    """The session files lib/learn_mine.py reads: every conversation's compact transcript (antigravity_transcripts)."""
    return antigravity_transcripts(env)


def take(tr, ev, typ, t, st, slim):
    """One step of an Antigravity CLI conversation, read into tr (lib/transcripts.py parse: True when it's Antigravity
    CLI's). A step (step_index and source, a type, no message) makes the transcript its; every line after is read as
    one."""
    if not (tr.kind == "antigravity" or ("step_index" in ev and "source" in ev and isinstance(typ, str)
                                         and "message" not in ev)):
        return False
    if tr.kind != "antigravity":                                          # an Antigravity CLI conversation
        tr.kind = "antigravity"
        parts = os.path.abspath(tr.path).split(os.sep) if tr.path else []
        tr.session_id = parts[-4][:200] if len(parts) >= 4 and parts[-3] == ".system_generated" else None
        tr.cwd = antigravity_project(tr.path) if tr.path else None
        tr.agy_pending = []
    if typ == "USER_INPUT":
        st.last_t = t if t is not None else st.last_t
    elif typ == "PLANNER_RESPONSE":
        for tc in ev.get("tool_calls") if isinstance(ev.get("tool_calls"), list) else []:
            if not isinstance(tc, dict):
                continue
            if len(tr.calls) >= MAX_CALLS:
                tr.info["truncated"] = "calls"
                break
            call = _agy_call(tc, t, st.last_t, slim)
            call["id"] = f"{call['id']}-{len(tr.calls)}"
            tr.calls.append(call)
            tr.agy_pending.append(call)
        st.last_t = t if t is not None else st.last_t
    elif ev.get("source") == "MODEL" and tr.agy_pending:                  # the next call's result, in order
        _agy_result(tr.agy_pending.pop(0), ev, t, slim)
        st.last_t = t if t is not None else st.last_t
    return True


FORMAT = (20, take)     # tried first of the agents' formats: lib/transcripts.py formats()
