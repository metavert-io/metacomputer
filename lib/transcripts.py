"""Claude Code transcripts that used the metacomputer tools: find them, read them, and turn them
into an ordered list of tool calls with their results and times.

Two formats carry the same messages:
- a headless session's stream-json output (`claude -p --output-format stream-json`), as kept by
  tests/skill/run_headless.sh, the showcase runner and the benchmark (gzipped, compacted, with `t`);
- Claude Code's own session files, $CLAUDE_CONFIG_DIR/projects/<project>/<session>.jsonl (else
  ~/.claude/projects), including subagent files below a session's folder.

Everything a tool result says came from the VM or from the hub, and the VM's part (window titles,
page and document text) is untrusted. This module only parses; callers decide what to keep, and
lib/learn_mine.py keeps nothing but classes, counts, tool names and app names from a fixed list.

Used by tests/skill/analyze.py and `metacomputer learn` (lib/learn_mine.py). Stdlib only.
"""
import gzip
import json
import os
import re
from datetime import datetime

# The metacomputer tools: mcp__metacomputer__* (user-scope or --mcp-config server) or the plugin's.
MCP_PREFIXES = ("mcp__metacomputer__", "mcp__plugin_metacomputer_metacomputer__")
# A tool_use of a metacomputer tool, as both formats write it (compact JSON; a space allowed).
USE_MARKER = re.compile(rb'"name":\s?"mcp__(?:plugin_metacomputer_)?metacomputer__[a-z_]')
# a `metacomputer` command: bare, or ~/.local/bin/metacomputer / <checkout>/bin/metacomputer (or mcomp)
MCOMP_CMD = re.compile(r"(^|[/\s])metacomputer(\s|$)")
CLI_SUB = re.compile(r"(?:^|[\s/;&|(])(?:metacomputer|mcomp)\s+([a-z][a-z-]*)")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
MAX_LINE = 64 << 20             # a line longer than this is skipped, not parsed
MAX_FILE = 2 << 30              # a transcript larger than this is skipped
TEXT_KEEP = 4000                # characters of a tool result kept for classification
TEXT_KEEP_WINDOWS = 20000       # list_windows and launch_app: enough for every window's app and ids


def mcp_tool(name):
    """The driver tool's own name (e.g. "click") for a metacomputer MCP tool, under either prefix; else None."""
    for p in MCP_PREFIXES:
        if isinstance(name, str) and name.startswith(p):
            t = name[len(p):]
            return t if TOOL_NAME.match(t) else "unknown"
    return None


def claude_projects_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def uses_tools(path, chunk=1 << 20):
    """True when the file has a tool_use of a metacomputer tool. Reads it in chunks and keeps nothing:
    only files that pass this are parsed."""
    try:
        if os.path.getsize(path) > MAX_FILE:
            return False
        opener = gzip.open if path.endswith(".gz") else open
        tail = b""
        with opener(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    return False
                if USE_MARKER.search(tail + block):
                    return True
                tail = block[-80:]
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def read_events(path):
    """The JSON events of a transcript, in order (bad or oversized lines skipped)."""
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rb") as f:
            for raw in f:
                if len(raw) > MAX_LINE or not raw.strip():
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    yield ev
    except (OSError, EOFError, gzip.BadGzipFile):
        return


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
        return ("" if content is None else json.dumps(content)[:TEXT_KEEP]), 0
    texts, imgs = [], 0
    for x in content:
        if isinstance(x, dict):
            if x.get("type") == "image":
                imgs += 1
            elif x.get("type") == "text":
                texts.append(str(x.get("text") or ""))
    return " ".join(texts), imgs


class Transcript:
    """One transcript, parsed: `calls` in order, each a dict with
      id, name, tool (the driver tool, or None for other tools), input, ctx (the subagent's
      parent tool_use id, or ""), t_use, t_prev (the latest event before it), and, once its result
      came, error (bool), text (at most TEXT_KEEP characters), images, t_result;
    plus session_id, cwd, kind ("stream" or "project"), first/last (event times), final (the
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


def parse(path, events=None):
    """Read a transcript (or the given events) into a Transcript."""
    tr = Transcript(path)
    by_id = {}
    last_t = None
    for ev in (events if events is not None else read_events(path)):
        typ = ev.get("type")
        t = _ts(ev)
        if t is not None:
            tr.first = t if tr.first is None else min(tr.first, t)
            tr.last = t if tr.last is None else max(tr.last, t)
        if typ == "system" and ev.get("subtype") == "init":
            tr.init = ev
            tr.kind = "stream"
            tr.session_id = tr.session_id or ev.get("session_id")
            tr.cwd = tr.cwd or ev.get("cwd")
            continue
        if typ == "result":
            tr.final = ev
            tr.kind = "stream"
            continue
        if typ not in ("assistant", "user"):
            continue
        tr.session_id = tr.session_id or ev.get("sessionId") or ev.get("session_id")
        tr.agent_id = tr.agent_id or ev.get("agentId")
        if isinstance(ev.get("cwd"), str):
            tr.cwd = tr.cwd or ev["cwd"]
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        ctx = ev.get("parent_tool_use_id") or ""
        if typ == "user" and t is not None:
            last_t = t                          # the model's input arrived: its time runs from here
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if typ == "assistant" and c.get("type") == "tool_use" and isinstance(c.get("id"), str):
                    name = c.get("name") if isinstance(c.get("name"), str) else ""
                    call = {"id": c["id"], "name": name, "tool": mcp_tool(name),
                            "input": c.get("input") if isinstance(c.get("input"), dict) else {},
                            "ctx": ctx if isinstance(ctx, str) else "", "t_use": t, "t_prev": last_t,
                            "error": None, "text": "", "images": 0, "t_result": None}
                    by_id[c["id"]] = call
                    tr.calls.append(call)
                    if t is not None:
                        last_t = t              # a second call in the same turn took no model time of its own
                elif typ == "user" and c.get("type") == "tool_result":
                    call = by_id.get(c.get("tool_use_id"))
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
