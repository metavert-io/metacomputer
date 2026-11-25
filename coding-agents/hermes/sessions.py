"""Hermes Agent's sessions for `metacomputer learn` (lib/session_dbs.py reads them: KIND, databases, read; its
docstring says how a database is opened without a write or a lock), checked with Hermes Agent 0.23.3:
state.db in its home (HERMES_HOME, else ~/.hermes) and in each profile (<root>/profiles/<name>/, the
root being ~/.hermes, or a HERMES_HOME outside it as Hermes Agent resolves it). Tables sessions (id, cwd) and messages
(session_id, role, content, tool_calls, tool_call_id, tool_name, timestamp in seconds; ordered by id): an assistant
row's tool_calls is the OpenAI list [{"id", "function": {"name", "arguments"}}], and a "tool" row holds a result,
wrapped in <untrusted_tool_result source="..."> around {"result": ...} or {"error": ...}. Its MCP tools are named
mcp__metacomputer__<tool>. With tool search on (its default) the model calls them through tool_call {"calls":
[{"name", "arguments"}]}; Hermes Agent splits a batch into one call each before it keeps it, and the tool row names the
tool itself. terminal is its shell."""
import json
import os
import re
import time

import transcripts
import wirelimits
from session_dbs import (MAX_AGENT_DIRS, MAX_ROW, SLOW, Layout, _args, _call, _columns, _distinct, _home,  # noqa: F401
                         _listdir, _new, _place, _recent, _result, _see, _when)

KIND = "hermes"                    # its sessions' Transcript kind, and the notes' key
UNTRUSTED_WRAP = re.compile(r'^<untrusted_tool_result source="[^"\n]{0,200}">\n', re.S)


def databases(env):
    """Hermes Agent's session databases: state.db in HERMES_HOME (when set), in its root (~/.hermes, or a HERMES_HOME
    outside it: that folder, or the folder above profiles/ when it is a profile) and in each profile under the root, as
    Places whose root is that home (a profile's: the root)."""
    native = os.path.join(_home(env), ".hermes")
    hh = os.path.abspath(os.path.expanduser(env["HERMES_HOME"])) if env.get("HERMES_HOME") else None
    root = native
    if hh and not (hh == native or hh.startswith(native.rstrip("/") + "/")):
        root = os.path.dirname(os.path.dirname(hh)) if os.path.basename(os.path.dirname(hh)) == "profiles" else hh
    places = [_place(h, "state.db") for h in ([hh] if hh else []) + [root]]
    places += [_place(root, "profiles", n, "state.db") for n in _listdir(os.path.join(root, "profiles"))[:MAX_AGENT_DIRS]
               if not n.startswith(".")]
    return [p for p in _distinct(places) if os.path.lexists(p)]


HE_BRIDGE = "tool_call"


def _hermes_result(content):
    """(error, text) of a Hermes Agent tool row: inside its <untrusted_tool_result> wrapper, the {"result"} or {"error"}
    JSON its tools answer with (an MCP tool's text, or its error)."""
    if not isinstance(content, str):
        return None, ""
    body = content
    m = UNTRUSTED_WRAP.match(body)
    if m:
        body = body[m.end():]
        if body.endswith("</untrusted_tool_result>"):
            body = body[:-len("</untrusted_tool_result>")]
        i = body.find("\n\n{")                         # after the wrapper's own notice
        body = body[i + 2:] if i >= 0 else body
    body = body.strip()
    try:
        d = wirelimits.loads(body[:MAX_ROW]) if body.startswith("{") else None
    except (ValueError, RecursionError):
        d = None
    if not isinstance(d, dict):
        return None, body[:transcripts.TEXT_KEEP_WINDOWS]
    if "error" in d:
        v = d["error"]
        return True, v if isinstance(v, str) else json.dumps(v)[:transcripts.TEXT_KEEP_WINDOWS]
    if "exit_code" in d and isinstance(d.get("exit_code"), int):         # its terminal tool
        return d["exit_code"] != 0, ""
    v = d.get("result", d)
    return False, (v if isinstance(v, str) else json.dumps(v))[:transcripts.TEXT_KEEP_WINDOWS]


def _hermes_calls(tc):
    """[(id, name, input)] of one OpenAI-style tool call in an assistant row: tool_call's entries one by one (a batch of
    more than one gets ids of their own, whose results can't be told apart), terminal as Bash."""
    if not isinstance(tc, dict):
        return []
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    name, cid = fn.get("name"), tc.get("id") or tc.get("call_id")
    if not isinstance(name, str):
        return []
    cid = cid[:200] if isinstance(cid, str) else None
    args = _args(fn.get("arguments"))
    if name == HE_BRIDGE:
        entries = args.get("calls")
        if entries is None and isinstance(args.get("name"), str):
            entries = [{"name": args["name"], "arguments": args.get("arguments")}]
        if isinstance(entries, str):
            try:
                entries = wirelimits.loads(entries[:MAX_ROW])
            except (ValueError, RecursionError):
                entries = []
        entries = [entries] if isinstance(entries, dict) else entries if isinstance(entries, list) else []
        out = []
        for k, e in enumerate(entries[:60]):
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                n = e["name"]
                out.append(((cid if len(entries) == 1 else f"{cid}#{k}") if cid else None,
                            "Bash" if n == "terminal" else n, _args(e.get("arguments"))))
        return out
    return [(cid, "Bash" if name == "terminal" else name, args)]


def hermes_session(path, sid, cwd, rows, slim=True):
    """One Hermes Agent session (its message rows, in order: role, content, tool_calls, tool_call_id, timestamp) as a
    Transcript."""
    tr = _new(path, "hermes", sid, cwd)
    by_id, last_t = {}, None
    for role, content, tool_calls, tcid, ts in rows:
        t = _when(ts)
        _see(tr, t)
        if role == "user":
            last_t = t if t is not None else last_t
        elif role == "assistant" and isinstance(tool_calls, str) and tool_calls.strip():
            try:
                tcs = wirelimits.loads(tool_calls[:MAX_ROW])
            except (ValueError, RecursionError):
                continue
            for tc in tcs if isinstance(tcs, list) else []:
                for cid, name, inp in _hermes_calls(tc):
                    if len(tr.calls) >= transcripts.MAX_CALLS:
                        tr.info["truncated"] = "calls"
                        break
                    call = _call(tr, cid, name, inp, t, last_t, slim)
                    if cid:
                        by_id[cid] = call
                    tr.calls.append(call)
            last_t = t if t is not None else last_t
        elif role == "tool" and isinstance(tcid, str):
            call = by_id.get(tcid[:200])
            if call is None or call["error"] is not None:
                continue
            err, text = _hermes_result(content)
            if err is None:
                err = False
            _result(call, err, text, 0, t, slim)
            last_t = t if t is not None else last_t
    return tr


def read(con, path, cutoff, notes, deadline, slim=True):
    mcols, scols = _columns(con, "messages"), _columns(con, "sessions")
    if not {"session_id", "role", "content", "tool_calls", "tool_call_id", "timestamp"} <= mcols:
        raise Layout()
    order = "id" if "id" in mcols else "rowid"
    named = "OR instr(coalesce(tool_name, ''), 'metacomputer') > 0" if "tool_name" in mcols else ""
    summary = "AND coalesce(_compressed_summary, 0) = 0" if "_compressed_summary" in mcols else ""
    for sid in _recent(con, "hermes", "messages", "timestamp", cutoff, notes):
        if time.monotonic() > deadline:
            notes.add("hermes", SLOW)
            return
        if not con.execute(f"SELECT 1 FROM messages WHERE session_id = ? AND (instr(coalesce(tool_calls, ''), "
                           f"'metacomputer') > 0 {named}) LIMIT 1", (sid,)).fetchone():
            continue
        cwd = None
        if {"id", "cwd"} <= scols:
            r = con.execute("SELECT cwd FROM sessions WHERE id = ?", (sid,)).fetchone()
            cwd = transcripts._str(r[0]) if r else None
        rows = con.execute(
            f"SELECT role, CASE WHEN length(content) > ? THEN NULL ELSE content END, "
            f"CASE WHEN length(tool_calls) > ? THEN NULL ELSE tool_calls END, tool_call_id, timestamp FROM messages "
            f"WHERE session_id = ? {summary} ORDER BY {order} LIMIT ?", (MAX_ROW, MAX_ROW, sid, transcripts.MAX_EVENTS))
        shown = sid[len("local-"):] if sid.startswith("local-") else sid
        yield hermes_session(path, shown[:200], cwd, rows, slim)
