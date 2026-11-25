"""OpenClaw's sessions for `metacomputer learn` (lib/session_dbs.py reads them: KIND, databases, read; its docstring
says how a database is opened without a write or a lock), checked with OpenClaw 2026.11.7:
<state>/agents/<agent>/agent/openclaw-agent.sqlite in each state folder: OPENCLAW_STATE_DIR when set, then
~/.openclaw and every ~/.openclaw-<profile> (OPENCLAW_HOME moves ~ for it). Table transcript_events (session_id, seq,
created_at in ms, the event as event_json, or zstd-compressed as event_zstd with its size in event_utf8_bytes;
message_role in newer versions): one event per row, in the order of seq. The first is {"type": "session", "id",
"cwd", "timestamp"}; a message is {"type": "message", "timestamp", "message": {"role": "user" | "assistant" |
"toolResult", ...}}, an assistant's content holding {"type": "toolCall", "id", "name", "arguments"} items and a
toolResult {"toolCallId", "toolName", "content", "isError", "details"}. Its MCP tools are named <server>__<tool>
(metacomputer__click). With tool search on (OpenClaw's default) the model calls them through dispatch_action
{"id": "mcp:metacomputer:metacomputer__click" or the tool's name, "args": {...}}, and the result's details carry the
tool and its own result ({"tool": {"mcp": {"serverName", "toolName"}}, "result": {"content"}}). exec is its shell.
Compressed rows are read with Python 3.14's compression.zstd; with an older Python they're counted, not read."""
import os
import time

import transcripts
import wirelimits
from session_dbs import (MAX_AGENT_DIRS, MAX_ROW, SLOW, Layout, _args, _call, _columns, _distinct, _home, _iso,  # noqa: F401
                         _listdir, _new, _place, _recent, _result, _see)

try:                                    # Python 3.14+: OpenClaw's compressed rows
    from compression import zstd as _zstd
except ImportError:                     # older Pythons: those rows are counted, not read
    _zstd = None

KIND = "openclaw"                  # its sessions' Transcript kind, and the notes' key
MAX_ZSTD = 4 << 20              # OpenClaw's own limit for a compressed row (expanded size)


def databases(env):
    """OpenClaw's session databases: <state>/agents/<agent>/agent/openclaw-agent.sqlite in OPENCLAW_STATE_DIR (when set),
    ~/.openclaw and each ~/.openclaw-<profile> (~ being OPENCLAW_HOME when set), as Places whose root is the state
    folder."""
    base = os.path.expanduser(env.get("OPENCLAW_HOME") or _home(env))
    states = [os.path.expanduser(env["OPENCLAW_STATE_DIR"])] if env.get("OPENCLAW_STATE_DIR") else []
    states += [os.path.join(base, ".openclaw")] + [os.path.join(base, n) for n in _listdir(base)
                                                    if n.startswith(".openclaw-")]
    out = []
    for s in _distinct(states):
        adir = os.path.join(s, "agents")
        for a in _listdir(adir)[:MAX_AGENT_DIRS]:
            p = _place(s, "agents", a, "agent", "openclaw-agent.sqlite")
            if os.path.lexists(p):
                out.append(p)
    return _distinct(out)


def _content_text(content):
    """OpenClaw's result content ([{"type": "text", "text"}, {"type": "image", ...}] or a string): (text, images)."""
    if isinstance(content, str):
        return content[:transcripts.TEXT_KEEP_WINDOWS], 0
    texts, imgs, size = [], 0, 0
    for x in content if isinstance(content, list) else []:
        if not isinstance(x, dict):
            continue
        if x.get("type") == "image":
            imgs += 1
        elif x.get("type") == "text" and isinstance(x.get("text"), str) and size < transcripts.TEXT_KEEP_WINDOWS:
            texts.append(x["text"][:transcripts.TEXT_KEEP_WINDOWS])
            size += len(texts[-1])
    return "\n".join(texts), imgs


OC_BRIDGE = "dispatch_action"
OC_MCP_ID = "mcp:metacomputer:"


def _openclaw_name(name, args):
    """(name as lib/transcripts.py names it, input) for an OpenClaw tool call: metacomputer__<tool> (direct, or through
    dispatch_action) -> mcp__metacomputer__<tool>; exec -> Bash; anything else by its own name."""
    if name == OC_BRIDGE:
        target = args.get("id") if isinstance(args.get("id"), str) else args.get("name")
        target = target[len(OC_MCP_ID):] if isinstance(target, str) and target.startswith(OC_MCP_ID) else target
        if isinstance(target, str) and target.startswith("metacomputer__"):
            return "mcp__" + target[:200], args.get("args") if isinstance(args.get("args"), dict) else {}
        return name, {}
    if name.startswith("metacomputer__"):
        return "mcp__" + name, args
    if name == "exec":
        return "Bash", args
    return name, {}


def openclaw_session(path, sid, events, slim=True):
    """One OpenClaw session (its events, in order) as a Transcript."""
    tr = _new(path, "openclaw", sid, None)
    by_id, last_t = {}, None
    for ev in events:
        t = _iso(ev.get("timestamp"))
        _see(tr, t)
        if ev.get("type") == "session":
            tr.cwd = tr.cwd or transcripts._str(ev.get("cwd"))
            continue
        msg = ev.get("message") if ev.get("type") == "message" else None
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            last_t = t if t is not None else last_t
        elif role == "assistant" and isinstance(msg.get("content"), list):
            for c in msg["content"]:
                if not (isinstance(c, dict) and c.get("type") == "toolCall" and isinstance(c.get("name"), str)):
                    continue
                if len(tr.calls) >= transcripts.MAX_CALLS:
                    tr.info["truncated"] = "calls"
                    break
                name, inp = _openclaw_name(c["name"], _args(c.get("arguments")))
                call = _call(tr, transcripts._str(c.get("id"), 200), name, inp, t, last_t, slim)
                by_id[call["id"]] = call
                tr.calls.append(call)
                last_t = t if t is not None else last_t
        elif role == "toolResult":
            cid = msg.get("toolCallId")
            call = by_id.get(cid[:200]) if isinstance(cid, str) else None
            if call is None or call["error"] is not None:
                continue
            det = msg.get("details") if isinstance(msg.get("details"), dict) else {}
            inner = det.get("result") if isinstance(det.get("result"), dict) else None
            text, imgs = _content_text(inner.get("content") if inner is not None else msg.get("content"))
            _result(call, msg.get("isError") is True, text, imgs, t, slim)
            last_t = t if t is not None else last_t
    return tr


def _openclaw_rows(con, sid, cols, info, deadline):
    """The session's events, in order (compressed ones expanded when this Python can, and only those that can hold a
    tool call or its result when the table says which)."""
    has_z = {"event_zstd", "event_utf8_bytes"} <= cols
    has_role = "message_role" in cols
    q = ("SELECT seq, CASE WHEN length(event_json) > ? THEN NULL ELSE event_json END, length(event_json) > ?, "
         + ("event_zstd, event_utf8_bytes, " if has_z else "NULL, NULL, ")
         + ("message_role " if has_role else "NULL ")
         + "FROM transcript_events WHERE session_id = ? ORDER BY seq LIMIT ?")
    first = True
    for n, (seq, text, big, blob, size, role) in enumerate(con.execute(q, (MAX_ROW, MAX_ROW, sid,
                                                                             transcripts.MAX_EVENTS))):
        if n % 500 == 0 and time.monotonic() > deadline:
            info["truncated"] = "time"
            return
        wanted = first or not has_role or role in ("assistant", "toolResult", "user")
        first = False
        if big:
            info["skipped"] = info.get("skipped", 0) + 1
            continue
        if text is None and blob is not None:
            if not wanted:
                continue
            if _zstd is None:
                info["compressed"] = info.get("compressed", 0) + 1
                continue
            if not isinstance(size, int) or not 0 < size <= MAX_ZSTD or not isinstance(blob, bytes):
                info["skipped"] = info.get("skipped", 0) + 1
                continue
            try:
                raw = _zstd.ZstdDecompressor().decompress(blob, max_length=size + 1)
                text = raw.decode("utf-8") if len(raw) == size else None
            except (Exception,):            # a corrupt frame (zstd's own error type) is one skipped row
                text = None
            if text is None:
                info["skipped"] = info.get("skipped", 0) + 1
                continue
        if not isinstance(text, str):
            continue
        try:
            ev = wirelimits.loads(text)
        except (ValueError, RecursionError):
            info["skipped"] = info.get("skipped", 0) + 1
            continue
        if isinstance(ev, dict):
            yield ev


def read(con, path, cutoff, notes, deadline, slim=True):
    cols = _columns(con, "transcript_events")
    if not {"session_id", "seq", "event_json", "created_at"} <= cols:
        raise Layout()
    for sid in _recent(con, "openclaw", "transcript_events", "created_at", cutoff, notes):
        if time.monotonic() > deadline:
            notes.add("openclaw", SLOW)
            return
        info = {}
        tr = openclaw_session(path, sid[:200], _openclaw_rows(con, sid, cols, info, deadline), slim)
        tr.info.update(info)
        if info.get("compressed"):
            notes.add("openclaw", "compressed entries weren't read (they need Python 3.14 or later)",
                      info["compressed"])
        yield tr
