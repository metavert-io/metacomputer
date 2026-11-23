"""Claude Code transcripts (and Codex's and Gemini CLI's sessions) that used the metacomputer tools: find them, read
them, and turn them into an ordered list of tool calls with their results and times.

Two Claude Code formats carry the same messages:
- a headless session's stream-json output (`claude -p --output-format stream-json`), as kept by
  tests/skill/run_headless.sh, the showcase runner and the benchmark (gzipped, compacted, with `t`);
- Claude Code's own session files, $CLAUDE_CONFIG_DIR/projects/<project>/<session>.jsonl (else
  ~/.claude/projects), including subagent files below a session's folder.
Codex keeps its sessions as $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl (else ~/.codex/sessions): a
session_meta line (id, cwd), then events; each finished MCP call is an item_completed event holding an McpToolCall
(server, tool, arguments, result, status, start and end in ms), and each shell command a CommandExecution, which
this module reads as a Bash call so the same counts apply.
Gemini CLI keeps its sessions as ~/.gemini/tmp/<project>/chats/session-*.jsonl (GEMINI_CLI_HOME moves ~/.gemini; checked
with Gemini CLI 0.70.0): a first line with sessionId and projectHash, then one line per message ({"id", "timestamp",
"type": "user" | "gemini" | ..., "toolCalls": [{"id", "name", "args", "status", "result", ...}]}), a message written again
with the same id as its tool calls move on (the last copy counts) and {"$set": ...} lines for the first one. Its MCP tools
are named mcp_<server>_<tool> (mcp_metacomputer_click); run_shell_command is read as a Bash call. The project folder comes
from ~/.gemini/projects.json, which names each project's folder under tmp/.
OpenClaw and Hermes Agent keep theirs in SQLite databases, and route MCP calls through a tool-search bridge by default;
`learn` doesn't read them yet (docs/learn.md): their calls reach it through the hub's action log (ACTION_LOG=on).

Everything a tool result says came from the VM or from the hub, and the VM's part (window titles,
page and document text) is untrusted. This module only parses; callers decide what to keep, and
lib/learn_mine.py keeps nothing but classes, counts, tool names and app names from a fixed list.

Used by tests/skill/analyze.py and `metacomputer learn` (lib/learn_mine.py). Stdlib only.
"""
import gzip
import json
import os
import re
import stat
import time
import sys
import zlib
from datetime import datetime

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import wirelimits  # noqa: E402  (JSON depth and size limits, shared with the hub and its clients)
# The metacomputer tools: mcp__metacomputer__* (user-scope or --mcp-config server), or the names an older copy of the
# plugin gave them (it brought its own server), in sessions recorded with it.
MCP_PREFIXES = ("mcp__metacomputer__", "mcp__plugin_metacomputer_metacomputer__")
# A tool_use of a metacomputer tool, as both Claude Code formats write it (compact JSON; a space allowed), a Codex call
# of the metacomputer server, or a Gemini CLI tool call (mcp_metacomputer_<tool>).
USE_MARKER = re.compile(rb'"name":\s?"mcp__(?:plugin_metacomputer_)?metacomputer__[a-z_]'
                        rb'|"server":\s?"metacomputer",\s?"tool":\s?"[a-z_]'
                        rb'|"name":\s?"mcp_metacomputer_[a-z_]')
GEMINI_PREFIX = "mcp_metacomputer_"         # Gemini CLI's name for the metacomputer server's tools
GEMINI_DONE = ("success", "error")          # a tool call that ran (scheduled, executing, cancelled ... didn't, or not yet)
# a `metacomputer` command: bare, or ~/.local/bin/metacomputer / <checkout>/bin/metacomputer (or mcomp)
MCOMP_CMD = re.compile(r"(^|[/\s])metacomputer(\s|$)")
CLI_SUB = re.compile(r"(?:^|[\s/;&|(])(?:metacomputer|mcomp)\s+([a-z][a-z-]*)")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
UNKNOWN_TOOL = "unknown_tool"
# Limits on what one transcript may cost to read (module globals, read at call time).
MAX_LINE = 32 << 20             # a line longer than this is skipped without being kept in memory
MAX_BYTES = 768 << 20           # at most this much of a file is read (decompressed, for .gz)
MAX_FILE = 2 << 30              # a transcript file larger than this on disk is skipped
MAX_EVENTS = 400_000            # events read from one transcript
MAX_CALLS = 60_000              # tool calls kept from one transcript
MAX_SECONDS = 120.0             # time spent on one transcript
TEXT_KEEP = 4000                # characters of a tool result kept for classification
TEXT_KEEP_WINDOWS = 20000       # list_windows and launch_app: enough for every window's app and ids

# Cua Driver's tools (0.53.0), the trusted vocabulary: any other name a transcript carries becomes
# UNKNOWN_TOOL, so a made-up tool name can't carry words into a report or a drafting prompt. The seed cache
# (config/driver-mcp-cache.json, which follows the pinned driver) adds the names of a newer driver.
DRIVER_TOOLS = frozenset("""
bring_to_front browser_click browser_dialog browser_download browser_motion_policy browser_navigate browser_pointer
browser_prepare browser_select browser_set_input_files browser_type check_for_update check_permissions click
clipboard_read clipboard_write double_click drag end_session get_accessibility_tree get_agent_cursor_state
get_browser_state get_config get_cursor_position get_desktop_state get_guide get_recording_state get_screen_size
get_session get_window_state health_report hotkey install_extension install_ffmpeg invoke_menu kill_app launch_app
list_apps list_sessions list_windows mouse_button_down mouse_button_up mouse_drag move_cursor page
parallel_mouse_drag parse_visual_regions press_key replay_trajectory right_click run_script run_steps scroll
set_agent_cursor_enabled set_agent_cursor_motion set_agent_cursor_theme set_config set_value set_window_frame
start_recording start_session stop_recording type_text verify_state zoom
""".split())


def _seed_tools():
    try:
        with open(os.path.join(LAB, "config", "driver-mcp-cache.json")) as f:
            tools = (json.load(f).get("tools") or {}).get("tools") or []
        return {t["name"] for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)
                and TOOL_NAME.match(t["name"])}
    except (OSError, ValueError, AttributeError):
        return set()


KNOWN_TOOLS = frozenset(DRIVER_TOOLS | _seed_tools())


def known_tool(name):
    """name when it is one of the driver's tools, else UNKNOWN_TOOL."""
    return name if isinstance(name, str) and name in KNOWN_TOOLS else UNKNOWN_TOOL


def mcp_tool(name):
    """The driver tool's own name (e.g. "click") for a metacomputer MCP tool, under either prefix (UNKNOWN_TOOL
    for a name the driver doesn't have); None for other tools."""
    for p in MCP_PREFIXES:
        if isinstance(name, str) and name.startswith(p):
            return known_tool(name[len(p):])
    return None


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


def claude_projects_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def codex_sessions_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(base, "sessions")


def gemini_sessions_dir(env=None):
    """Gemini CLI's per-project folders, each with its chats/: ~/.gemini/tmp ($GEMINI_CLI_HOME/.gemini/tmp)."""
    env = os.environ if env is None else env
    base = env.get("GEMINI_CLI_HOME") or env.get("HOME") or os.path.expanduser("~")
    return os.path.join(base, ".gemini", "tmp")


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


def open_regular(path):
    """A binary reader for a regular file (no link, FIFO or device), gunzipped for .gz; raises OSError."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {path}")
        if st.st_size > MAX_FILE:
            raise OSError(f"larger than {MAX_FILE >> 20} MB: {path}")
        f = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    return gzip.GzipFile(fileobj=f, mode="rb") if path.endswith(".gz") else f


def uses_tools(path, chunk=1 << 20):
    """True when the file has a tool_use of a metacomputer tool, False when it hasn't, None when it can't be read
    (not a regular file, corrupt gzip, ...). Reads it in chunks (at most MAX_BYTES, decompressed) and keeps
    nothing: only files that pass this are parsed."""
    try:
        tail, total = b"", 0
        with open_regular(path) as f:
            while total < MAX_BYTES:
                block = f.read(chunk)
                if not block:
                    return False
                total += len(block)
                if USE_MARKER.search(tail + block):
                    return True
                tail = block[-80:]
        return False
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error, ValueError):
        return None


def read_events(path, info=None):
    """The JSON events of a transcript, in order. Bounded: a line over MAX_LINE is skipped without being held
    in memory, reading stops after MAX_BYTES (decompressed), MAX_EVENTS events or MAX_SECONDS, and lines that
    aren't JSON objects (or nest too deeply) are skipped. `info` (a dict) gets "truncated" and "skipped"."""
    info = info if info is not None else {}
    info.setdefault("skipped", 0)
    deadline = time.monotonic() + MAX_SECONDS
    try:
        with open_regular(path) as f:
            total = n = 0
            while True:
                raw = f.readline(MAX_LINE + 1)
                if not raw:
                    return
                total += len(raw)
                if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
                    info["skipped"] += 1
                    while total <= MAX_BYTES:            # the rest of this line, read and dropped
                        rest = f.readline(1 << 20)
                        total += len(rest)
                        if not rest or rest.endswith(b"\n"):
                            break
                if total > MAX_BYTES:
                    info["truncated"] = "size"
                    return
                if len(raw) > MAX_LINE or not raw.strip():
                    continue
                n += 1
                if n > MAX_EVENTS:
                    info["truncated"] = "events"
                    return
                if n % 1000 == 0 and time.monotonic() > deadline:
                    info["truncated"] = "time"
                    return
                try:
                    ev = wirelimits.loads(raw)
                except (ValueError, RecursionError):
                    info["skipped"] += 1
                    continue
                if isinstance(ev, dict):
                    yield ev
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error, ValueError):
        info["truncated"] = info.get("truncated") or "unreadable"
        return


def bounded(v, depth=0):
    """A small copy of a tool input: strings cut to 300 characters, lists to 60 items, dicts to 40 keys,
    nesting to 5 levels. Enough for targets, routes and checks; never whole scripts or typed text."""
    if isinstance(v, str):
        return v[:300]
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return v
    if depth >= 5:
        return None
    if isinstance(v, dict):
        return {k: bounded(x, depth + 1) for k, x in list(v.items())[:40] if isinstance(k, str)}
    if isinstance(v, list):
        return [bounded(x, depth + 1) for x in v[:60]]
    return None


def _ts(ev):
    """Seconds for an event: `t` (the benchmark's), else its ISO `timestamp`; None when it has neither."""
    t = ev.get("t")
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return float(t)
    s = ev.get("timestamp")
    if isinstance(s, str) and s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def result_text(content):
    """A tool result's text (its text items joined), and how many images it holds."""
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return "", 0
    texts, imgs, size = [], 0, 0
    for x in content:
        if isinstance(x, dict):
            if x.get("type") == "image":
                imgs += 1
            elif x.get("type") == "text" and isinstance(x.get("text"), str) and size < TEXT_KEEP_WINDOWS:
                texts.append(x["text"][:TEXT_KEEP_WINDOWS])
                size += len(texts[-1])
    return " ".join(texts), imgs


class Transcript:
    """One transcript, parsed: `calls` in order, each a dict with
      id, name, tool (the driver tool, or None for other tools), input, ctx (the subagent's
      parent tool_use id, or ""), t_use, t_prev (the latest event before it), and, once its result
      came, error (bool), text (at most TEXT_KEEP characters), images, t_result;
    plus session_id, cwd, kind ("stream", "project", "codex" or "gemini"), first/last (event times), final (the
    stream's result event) and init (its init event). Results are cut to TEXT_KEEP characters
    (TEXT_KEEP_WINDOWS for list_windows and launch_app)."""

    def __init__(self, path):
        self.path = path
        self.calls = []
        self.final = {}
        self.init = {}
        self.session_id = None
        self.agent_id = None
        self.cwd = None
        self.kind = "project"
        self.first = None
        self.last = None

    @property
    def driver_calls(self):
        return [c for c in self.calls if c["tool"]]


def _str(v, n=4096):
    return v[:n] if isinstance(v, str) else None


def _ms(v):
    return v / 1000.0 if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


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


def parse(path, events=None, slim=False):
    """Read a transcript (or the given events) into a Transcript. Fields of the wrong type are ignored, at most
    MAX_CALLS tool calls are kept, and with slim=True each call keeps only a bounded() copy of its input (and a
    Bash call its first 2,000 characters of command). `tr.info` says whether reading was cut short."""
    tr = Transcript(path)
    tr.info = {}
    by_id = {}
    last_t = None
    for ev in (events if events is not None else read_events(path, tr.info)):
        typ = ev.get("type")
        t = _ts(ev)
        if t is not None:
            tr.first = t if tr.first is None else min(tr.first, t)
            tr.last = t if tr.last is None else max(tr.last, t)
        if typ == "system" and ev.get("subtype") == "init":
            tr.init = ev
            tr.kind = "stream"
            tr.session_id = tr.session_id or _str(ev.get("session_id"), 200)
            tr.cwd = tr.cwd or _str(ev.get("cwd"))
            continue
        if typ == "result":
            tr.final = ev
            tr.kind = "stream"
            continue
        if tr.kind == "gemini" or (typ is None and "projectHash" in ev and isinstance(ev.get("sessionId"), str)):
            if tr.kind != "gemini":                                               # a Gemini CLI session's first line
                tr.kind = "gemini"
                tr.session_id = _str(ev.get("sessionId"), 200)
                tr.cwd = gemini_project(path) if path else None
                tr.gemini_seen = set()
                continue
            if typ == "user" and t is not None:
                last_t = t
            elif typ == "gemini" and isinstance(ev.get("toolCalls"), list):
                for tc in ev["toolCalls"]:
                    key = _str(tc.get("id"), 200) if isinstance(tc, dict) else None
                    if not key or key in tr.gemini_seen:
                        continue
                    if len(tr.calls) >= MAX_CALLS:
                        tr.info["truncated"] = "calls"
                        break
                    call = _gemini_call(tc, t, last_t, slim)
                    if call is not None:
                        tr.gemini_seen.add(key)              # the message is written again as its calls move on
                        tr.calls.append(call)
                        last_t = call["t_result"] or last_t
            continue
        if typ == "session_meta" and isinstance(ev.get("payload"), dict):        # a Codex session
            pl = ev["payload"]
            tr.kind = "codex"
            tr.session_id = tr.session_id or _str(pl.get("id"), 200) or _str(pl.get("session_id"), 200)
            tr.cwd = tr.cwd or _str(pl.get("cwd"))
            continue
        if typ == "event_msg" and isinstance(ev.get("payload"), dict):
            pl = ev["payload"]
            item = pl.get("item") if isinstance(pl.get("item"), dict) else {}
            if pl.get("type") == "item_completed" and item.get("type") == "UserMessage":
                last_t = _ms(pl.get("completed_at_ms")) or t
            elif pl.get("type") == "item_completed":
                if len(tr.calls) >= MAX_CALLS:
                    tr.info["truncated"] = "calls"
                    continue
                call = _codex_call(tr, pl, item, t, last_t, slim)
                if call is not None:
                    tr.calls.append(call)
                    last_t = call["t_result"] or last_t   # the model's next input arrives when this call ends
            continue
        if typ not in ("assistant", "user"):
            continue
        tr.session_id = tr.session_id or _str(ev.get("sessionId"), 200) or _str(ev.get("session_id"), 200)
        tr.agent_id = tr.agent_id or _str(ev.get("agentId"), 200)
        tr.cwd = tr.cwd or _str(ev.get("cwd"))
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        ctx = _str(ev.get("parent_tool_use_id"), 200) or ""
        if typ == "user" and t is not None:
            last_t = t                          # the model's input arrived: its time runs from here
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if typ == "assistant" and c.get("type") == "tool_use" and isinstance(c.get("id"), str):
                    if len(tr.calls) >= MAX_CALLS:
                        tr.info["truncated"] = "calls"
                        continue
                    name = _str(c.get("name"), 200) or ""
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    if slim:
                        inp = ({"command": _str(inp.get("command"), 2000) or ""} if name == "Bash"
                               else bounded(inp) if mcp_tool(name) else {})
                    call = {"id": c["id"][:200], "name": name, "tool": mcp_tool(name), "input": inp, "ctx": ctx,
                            "t_use": t, "t_prev": last_t, "error": None, "text": "", "images": 0, "t_result": None}
                    by_id[call["id"]] = call
                    tr.calls.append(call)
                    if t is not None:
                        last_t = t              # a second call in the same turn took no model time of its own
                elif typ == "user" and c.get("type") == "tool_result":
                    tid = c.get("tool_use_id")
                    call = by_id.get(tid[:200]) if isinstance(tid, str) else None
                    if call is None or call["error"] is not None:
                        continue
                    text, imgs = result_text(c.get("content"))
                    keep = TEXT_KEEP_WINDOWS if call["tool"] in ("list_windows", "launch_app") else TEXT_KEEP
                    call.update(error=bool(c.get("is_error")), text=text[:keep], images=imgs, t_result=t)
    return tr


def structural(tr):
    """The skill tests' structural counts (tests/skill/analyze.py) for one transcript."""
    vm = tr.driver_calls
    bash = [c["input"].get("command", "") for c in tr.calls if c["name"] == "Bash"]
    reads = [c for c in vm if c["tool"] == "get_window_state"]
    named_steps = sum(1 for c in vm if c["tool"] == "run_steps"
                      for st in (c["input"].get("steps") or []) if isinstance(st, dict) and "tool" not in st)
    return {
        "images_returned": sum(c["images"] for c in vm),
        "reads": len(reads),
        "reads_tree_only": sum(1 for c in reads if c["input"].get("include_screenshot") is False),
        "reads_since": sum(1 for c in reads if c["input"].get("since")),
        "run_steps": sum(1 for c in vm if c["tool"] == "run_steps"),
        "steps_by_name": named_steps,
        "expect_checks": sum(json.dumps(c["input"]).count('"expect"') for c in vm if c["tool"] == "run_steps"),
        "verify_state": sum(1 for c in vm if c["tool"] == "verify_state"),
        "run_script": sum(1 for c in vm if c["tool"] == "run_script"),
        "list_apps": sum(1 for c in vm if c["tool"] == "list_apps"),
        "screenshots_desktop_or_zoom": sum(1 for c in vm if c["tool"] in ("get_desktop_state", "zoom")),
        "inspect": sum(1 for b in bash if re.search(r"(^|[/\s])metacomputer inspect\b", b)),
        "page_tools": sum(1 for c in vm if c["tool"].startswith("browser_") or c["tool"] == "get_browser_state"),
        "web_cmds": sum(1 for b in bash if re.search(r"(^|[/\s])metacomputer web\b", b)),
        "chrome_launch": sum(1 for c in vm if "chrome" in json.dumps(c["input"]).lower()
                             and c["tool"] in ("launch_app", "browser_prepare")),
        "bridge_notes": sum(1 for c in vm if "metacomputer bridge" in c["text"][:300]),
    }
