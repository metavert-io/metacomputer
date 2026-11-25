"""Turn a headless session (stream-json events: every launcher's are in that format) into benchmark
metrics, and make the compact transcript the benchmark keeps.

Metrics follow HCU's: steps (turns), tokens, time, cost, the tool-call mix
(observation vs action; accessibility tree vs pixels vs shell) and the calls
that were refused or rejected for malformed arguments.
"""
import json
import os
import re

# The metacomputer tools: from a user-scope or --mcp-config server named metacomputer, or from an older copy of the
# plugin, which brought its own server.
MCP_PREFIXES = ("mcp__metacomputer__", "mcp__plugin_metacomputer_metacomputer__")
TREE_READS = {"get_window_state", "get_accessibility_tree", "get_browser_state", "verify_state"}
OTHER_READS = {"list_windows", "list_apps", "get_screen_size", "get_cursor_position", "get_recording_state",
               "health_report", "get_session", "list_sessions", "get_config", "get_guide", "check_permissions",
               "get_agent_cursor_state"}
SCREEN_TOOLS = {"get_desktop_state", "zoom", "parse_visual_regions"}
POINTER = {"click", "double_click", "right_click", "drag", "scroll", "move_cursor", "mouse_button_down",
           "mouse_drag", "mouse_button_up", "parallel_mouse_drag"}
KEYS = {"type_text", "press_key", "hotkey"}
APP = {"launch_app", "kill_app", "bring_to_front", "set_window_frame"}
AX_TARGET_KEYS = ("element_token", "target", "role", "name", "element", "element_index", "selector", "label")
CLI_RE = re.compile(r"(?:^|[\s/;&|(])(?:metacomputer|mcomp)\s+([a-z][a-z-]*)")
SHELL_SUBCMDS = {"ssh", "sh"}
CLI_NAMES = ("metacomputer", "mcomp")
CLI_OPTIONS = ("--vm", "--on")          # bin/metacomputer's options before the command that take a value
SUBCOMMAND = re.compile(r"[a-z][a-z-]*")
ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
VAR = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
# Words before a command that run it (or start a list): env, command, exec, nohup, time, nice and timeout with their
# options; a list's keywords.
KEYWORDS = {"!", "{", "}", "then", "do", "else", "elif", "if", "while", "until", "time"}
SHELLS = ("sh", "bash", "zsh")
# The shells' options that take the next word as their value (bash's -o/+o NAME and -O/+O NAME, zsh's -o NAME; their
# long --rcfile/--init-file FILE): within a cluster too (bash -eo pipefail).
SHELL_VALUE_LETTERS = "oO"
SHELL_VALUE_LONG = ("--rcfile", "--init-file")


def shell_command_string(rest):
    """The command string `sh|bash|zsh OPTIONS... -c STRING` runs: -c alone or in a cluster of short options (-lc, -ic,
    -ec, -xec), past the shell's other options (-o NAME, +O NAME, -eo NAME, --norc, --rcfile FILE), as the shells read
    it: the first word after the options. None without -c, or when no word follows."""
    has_c, i = False, 0
    while i < len(rest):
        w = rest[i]
        if w in ("--", "-"):
            i += 1
            break
        if w.startswith("--"):
            i += 2 if w in SHELL_VALUE_LONG else 1
            continue
        if len(w) > 1 and w[0] in "-+" and w[1:].isalpha():
            has_c = has_c or (w[0] == "-" and "c" in w[1:])
            i += 1 + sum(w[1:].count(x) for x in SHELL_VALUE_LETTERS)
            continue
        break
    return rest[i] if has_c and i < len(rest) else None

BRIDGE_REFUSED = re.compile(r"Refused by the metacomputer bridge|in use by another controller|"
                            r"bounded session has ended", re.I)
PERMISSION = re.compile(r"(permission|permissions) to use .* (has been )?denied|haven't granted it|"
                        r"requested permissions|not allowed by (the )?permission", re.I)
DRIVER_REFUSED = re.compile(r"outside the capability manifest|not permitted|forbidden|refused|"
                            r"denied by (the )?(driver|policy)", re.I)
INVALID = re.compile(r"invalid (argument|param|input|value|type|request)|InputValidationError|"
                     r"missing (the )?(required )?(field|property|parameter|argument|key)|missing (the )?required|required (field|property|parameter|argument)|"
                     r"unknown (field|variant|parameter|argument|key)|unexpected (field|argument|property|key)|"
                     r"-32602|failed to (parse|deserialize)|deserializ|must be (a|an|one of)|is not (a valid|one of)|"
                     r"expected (a |an )?[a-z_]+,? (but )?(got|found)", re.I)


def mcp_tool(name):
    """The driver tool's own name (e.g. "click") for a metacomputer MCP tool, under either prefix; else None."""
    for p in MCP_PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return None


# --- compact transcript ---------------------------------------------------------------------
def _clip(s, n):
    return s if not isinstance(s, str) or len(s) <= n else s[:n] + f"... [{len(s) - n} more chars]"


def _compact_value(v, n=1500, depth=0):
    if isinstance(v, str):
        return _clip(v, n)
    if depth > 6:
        return "..."
    if isinstance(v, dict):
        return {k: _compact_value(x, n, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [_compact_value(x, n, depth + 1) for x in v[:60]]
    return v


def compact(ev):
    """A small copy of one stream-json event: images replaced by their size,
    long texts and tool inputs clipped, thinking dropped (its length kept)."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        keep = {k: ev.get(k) for k in ("type", "subtype", "model", "cwd", "permissionMode", "claude_code_version",
                                         "mcp_servers", "skills", "apiKeySource")}
        keep["tools"] = len(ev.get("tools") or [])
        return keep
    if t in ("assistant", "user"):
        msg = dict(ev.get("message") or {})
        content = msg.get("content")
        if isinstance(content, list):
            out = []
            for c in content:
                ct = c.get("type")
                if ct == "thinking":
                    out.append({"type": "thinking", "chars": len(c.get("thinking") or "")})
                elif ct == "text":
                    out.append({"type": "text", "text": _clip(c.get("text") or "", 3000)})
                elif ct == "tool_use":
                    out.append({"type": "tool_use", "id": c.get("id"), "name": c.get("name"),
                                "input": _compact_value(c.get("input") or {})})
                elif ct == "tool_result":
                    raw = c.get("content")
                    items = raw if isinstance(raw, list) else [{"type": "text", "text": raw if isinstance(raw, str) else json.dumps(raw)}]
                    small = []
                    for x in items:
                        if not isinstance(x, dict):
                            continue
                        if x.get("type") == "image":
                            small.append({"type": "image", "omitted_bytes": len(json.dumps(x.get("source") or {}))})
                        elif x.get("type") == "text":
                            small.append({"type": "text", "text": _clip(x.get("text") or "", 2500)})
                        else:
                            small.append({"type": x.get("type")})
                    out.append({"type": "tool_result", "tool_use_id": c.get("tool_use_id"),
                                "is_error": bool(c.get("is_error")), "content": small})
                else:
                    out.append({"type": ct})
            msg["content"] = out
        elif isinstance(content, str):
            msg["content"] = _clip(content, 3000)
        keep = {"type": t, "message": {k: msg.get(k) for k in ("role", "content", "usage", "model") if k in msg}}
        if ev.get("parent_tool_use_id"):
            keep["parent_tool_use_id"] = ev["parent_tool_use_id"]
        return keep
    if t == "result":
        return {k: (_clip(v, 4000) if isinstance(v, str) else v) for k, v in ev.items()}
    if t == "system" and ev.get("subtype") == "thinking_tokens":
        return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": ev.get("estimated_tokens")}
    return _compact_value(ev, 500)


# --- classification ---------------------------------------------------------------------------
def action_route(tool, args):
    """ax | pixel | keyboard | app | page | clipboard | other for one action."""
    args = args if isinstance(args, dict) else {}
    if tool.startswith("browser_") or tool == "page":
        return "page"
    if tool in APP:
        return "app"
    if tool in ("set_value", "invoke_menu"):
        return "ax"
    if tool.startswith("clipboard_"):
        return "clipboard"
    if tool in POINTER or tool in KEYS:
        # by element: a token from a read, or a role/name (or selector) target, as run_steps allows
        if any(args.get(k) not in (None, "") for k in AX_TARGET_KEYS):
            return "ax"
        if any(k in args for k in ("x", "y", "from_x", "to_x", "drags")):
            return "pixel"
        return "keyboard" if tool in KEYS else "pixel"
    return "other"


def steps_of(args):
    out = []
    for st in (args or {}).get("steps") or []:
        if not isinstance(st, dict):
            continue
        if "tool" in st:
            out.append((st.get("tool"), st.get("args") or {}))
        else:
            for k, v in st.items():
                if k not in ("wait_for", "expect") and isinstance(v, dict):
                    out.append((k, v))
    return out


def _segments(cmd):
    """A shell command's simple commands, as text: split at unquoted ; & | newline ( ) and backquotes, a here-document's
    lines left out. Quotes and backslashes are kept for shlex."""
    out, cur, i, n, q, heredocs = [], [], 0, len(cmd), None, []
    while i < n:
        c = cmd[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c
            cur.append(c)
        elif c == "\\" and i + 1 < n:
            cur.append(cmd[i:i + 2])
            i += 1
        elif c == "<" and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = re.match(r"<<-?\s*(['\"]?)([A-Za-z0-9_.-]+)\1", cmd[i:])
            if m:
                heredocs.append(m.group(2))
                i += m.end()
                continue
            cur.append(c)
        elif c in ";&|()`\n":
            out.append("".join(cur))
            cur = []
            if c == "\n" and heredocs:                    # the here-documents' bodies, each to its own last line
                for word in heredocs:
                    j = i + 1
                    while j <= n:
                        k = cmd.find("\n", j)
                        k = n if k < 0 else k
                        line = cmd[j:k]
                        j = k + 1
                        if line.lstrip("\t") == word:
                            break
                    i = j - 1
                heredocs = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [x for x in out if x.strip()]


def cli_subcommands(cmd, names=None, depth=0):
    """The metacomputer commands a shell command runs, in order (call, sh, who...): its program metacomputer or mcomp
    (by any path, quoted or not, or a variable it was put in: M=.../metacomputer; $M call), in any simple command
    (after cd ... &&, in a pipeline), after assignments and env/command/exec/nohup/time/nice/timeout, past its --vm and
    --on options; and inside sh/bash/zsh -c (-lc, -ic, any cluster of short options with c: shell_command_string)."""
    import shlex
    names = dict(names or {})
    subs = []
    for seg in _segments(cmd):
        try:
            words = shlex.split(seg, posix=True)
        except ValueError:
            words = seg.split()
        i, local = 0, {}
        while i < len(words) and ASSIGN.fullmatch(words[i]):
            k, v = ASSIGN.fullmatch(words[i]).groups()
            local[k] = v
            i += 1
        if i == len(words):
            names.update(local)                            # M=...; on its own: kept for what follows
            continue
        while i < len(words):
            w = words[i]
            if w in KEYWORDS:
                i += 1
            elif w == "env":
                i += 1
                while i < len(words) and (words[i].startswith("-") or ASSIGN.fullmatch(words[i])):
                    i += 2 if words[i] in ("-u", "-P", "-S", "-C") else 1
            elif w in ("command", "exec", "nohup", "builtin"):
                i += 1
                while i < len(words) and words[i].startswith("-"):
                    i += 1
            elif w == "nice":
                i += 1
                while i < len(words) and words[i].startswith("-"):
                    i += 2 if words[i] == "-n" else 1
            elif w == "timeout":
                i += 1
                while i < len(words) and words[i].startswith("-"):
                    i += 2 if words[i] in ("-s", "-k", "--signal", "--kill-after") else 1
                i += 1                                     # its duration
            else:
                break
        if i >= len(words):
            continue
        prog = VAR.sub(lambda m: names.get(m.group(1) or m.group(2), m.group(0)), words[i])
        base = os.path.basename(prog)
        rest = words[i + 1:]
        if base in CLI_NAMES:
            while rest and (rest[0] in CLI_OPTIONS or rest[0].split("=", 1)[0] in CLI_OPTIONS):
                rest = rest[2:] if rest[0] in CLI_OPTIONS else rest[1:]
            if rest and SUBCOMMAND.fullmatch(rest[0]):
                subs.append(rest[0])
        elif base in SHELLS and depth < 3:
            inner = shell_command_string(rest)
            if inner is not None:
                subs += cli_subcommands(inner, names, depth + 1)
    return subs


def command_subcommands(cmd):
    """The metacomputer commands a Bash call ran (cli_subcommands), and every one the plain regex finds (CLI_RE, what
    was counted before the parse) that the parse doesn't account for: where the parse is partial (a wrapper it doesn't
    know, a shell's option it can't read), it never drops what the regex would have counted."""
    from collections import Counter
    subs = cli_subcommands(cmd)
    # The regex reads only what the shell would run: never a comment, and never quoted text, which is either an
    # argument (an echo, an explanation) or a script the parse already read (sh -c "...").
    bare = _unquoted_code(cmd)
    extra = Counter(CLI_RE.findall(bare)) - Counter(subs)
    for s in CLI_RE.findall(bare):
        if extra[s] > 0:
            subs.append(s)
            extra[s] -= 1
    return subs


def _unquoted_code(cmd):
    """CMD with every quoted string's contents and every comment (a # that starts a word, to the end of its line)
    blanked, as the shell reads them; a backslash escapes the next character outside single quotes."""
    out, i, n, q = [], 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if q == "'":
            if c == "'":
                q = None
            out.append(" ")
        elif q == '"':
            if c == "\\" and i + 1 < n:
                out.append("  ")
                i += 2
                continue
            if c == '"':
                q = None
            out.append(" ")
        elif c == "\\" and i + 1 < n:
            out.append(cmd[i:i + 2])
            i += 2
            continue
        elif c in "'\"":
            q = c
            out.append(" ")
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            j = cmd.find("\n", i)
            out.append(" " * ((n if j < 0 else j) - i))
            i = n if j < 0 else j
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def result_text(res):
    items = res.get("content") or []
    return " ".join(x.get("text", "") for x in items if isinstance(x, dict) and x.get("type") == "text")


def analyze(events, times=None):
    """events: compacted stream-json events in order; times: arrival seconds per event."""
    times = times or [0.0] * len(events)
    calls, results, final, init = [], {}, {}, {}
    t_use = {}
    for ev, t in zip(events, times):
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            init = ev
        elif typ == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    calls.append(c)
                    t_use[c["id"]] = t
        elif typ == "user":
            content = (ev.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    results[c.get("tool_use_id")] = dict(c, t=t)
        elif typ == "result":
            final = ev

    m = {
        "tool_calls": len(calls),
        "mcp_calls": 0, "tree_reads": 0, "other_reads": 0, "screenshot_calls": 0, "screenshots": 0,
        "actions": {"ax": 0, "pixel": 0, "keyboard": 0, "app": 0, "page": 0, "clipboard": 0, "other": 0},
        "run_steps": 0, "run_steps_steps": 0, "run_script": 0,
        "shell_calls": 0, "cli_calls": {}, "bash_other": 0,
        "skill_calls": 0, "file_reads": 0, "toolsearch": 0, "subagents": 0,
        "refused_bridge": 0, "refused_driver": 0, "denied_permission": 0, "invalid_args": 0, "errors_other": 0,
        "mcp_tools": {}, "tool_time_s": {"mcp": 0.0, "bash": 0.0, "other": 0.0},
    }
    samples = []
    for c in calls:
        name, args = c.get("name") or "", c.get("input") or {}
        res = results.get(c.get("id")) or {}
        text = result_text(res)
        imgs = sum(1 for x in res.get("content") or [] if isinstance(x, dict) and x.get("type") == "image")
        m["screenshots"] += imgs
        m["screenshot_calls"] += 1 if imgs else 0
        if res and c.get("id") in t_use:
            dt = max(0.0, res["t"] - t_use[c["id"]])
            m["tool_time_s"]["mcp" if mcp_tool(name) else "bash" if name == "Bash" else "other"] += dt
        tool = mcp_tool(name)
        if tool:
            m["mcp_calls"] += 1
            m["mcp_tools"][tool] = m["mcp_tools"].get(tool, 0) + 1
            if tool in TREE_READS:
                if not (tool == "get_window_state" and args.get("include_accessibility_tree") is False):
                    m["tree_reads"] += 1
            elif tool in OTHER_READS:
                m["other_reads"] += 1
            elif tool in SCREEN_TOOLS:
                pass
            elif tool == "run_steps":
                m["run_steps"] += 1
                steps = steps_of(args)
                m["run_steps_steps"] += len(steps)
                for st, sa in steps:
                    if st in TREE_READS or st in OTHER_READS or st in SCREEN_TOOLS:
                        continue
                    m["actions"][action_route(st or "", sa)] += 1
            elif tool == "run_script":
                m["run_script"] += 1
            else:
                m["actions"][action_route(tool, args)] += 1
        elif name == "Bash":
            cmd = args.get("command") or ""
            subs = command_subcommands(cmd)       # the parse, and whatever the regex counted that it doesn't
            if any(s in SHELL_SUBCMDS for s in subs):
                m["shell_calls"] += 1
            elif subs:
                for s in subs[:1]:
                    m["cli_calls"][s] = m["cli_calls"].get(s, 0) + 1
            else:
                m["bash_other"] += 1
        elif name == "Skill":
            m["skill_calls"] += 1
        elif name == "Read":
            m["file_reads"] += 1
        elif name == "ToolSearch":
            m["toolsearch"] += 1
        elif name in ("Agent", "Task"):
            m["subagents"] += 1
        if res.get("is_error"):
            if BRIDGE_REFUSED.search(text):
                kind = "refused_bridge"
            elif PERMISSION.search(text):
                kind = "denied_permission"
            elif INVALID.search(text):
                kind = "invalid_args"
            elif mcp_tool(name) and DRIVER_REFUSED.search(text):
                kind = "refused_driver"
            else:
                kind = "errors_other"
            m[kind] += 1
            if len(samples) < 12:
                samples.append({"tool": "mcp:" + mcp_tool(name) if mcp_tool(name) else name, "kind": kind, "text": text[:160]})
        elif mcp_tool(name) and BRIDGE_REFUSED.search(text[:300]):
            m["refused_bridge"] += 1
    m["observation_calls"] = m["tree_reads"] + m["other_reads"] + sum(
        n for t, n in m["mcp_tools"].items() if t in SCREEN_TOOLS)
    m["action_calls"] = sum(m["actions"].values())
    m["error_samples"] = samples
    m["tool_time_s"] = {k: round(v, 1) for k, v in m["tool_time_s"].items()}
    usage = final.get("usage") or {}
    inp = sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    m.update({
        "result_subtype": final.get("subtype"), "is_error": final.get("is_error"),
        "api_error_status": final.get("api_error_status"),
        "cost_usd": final.get("total_cost_usd"), "turns": final.get("num_turns"),
        "duration_s": round((final.get("duration_ms") or 0) / 1000, 1),
        "api_s": round((final.get("duration_api_ms") or 0) / 1000, 1),
        "input_tokens": inp, "output_tokens": usage.get("output_tokens"),
        "tokens": {k: usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                              "cache_read_input_tokens", "output_tokens")},
        "models": sorted((final.get("modelUsage") or {}).keys()) or [init.get("model")],
        "final_text": (final.get("result") or "")[:600],
    })
    return m
