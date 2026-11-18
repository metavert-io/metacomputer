"""Turn a headless Claude Code session (stream-json events) into benchmark
metrics, and make the compact transcript the benchmark keeps.

Metrics follow HCU's: steps (turns), tokens, time, cost, the tool-call mix
(observation vs action; accessibility tree vs pixels vs shell) and the calls
that were refused or rejected for malformed arguments.
"""
import json
import re

MCP = "mcp__metacomputer__"
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
            m["tool_time_s"]["mcp" if name.startswith(MCP) else "bash" if name == "Bash" else "other"] += dt
        if name.startswith(MCP):
            tool = name[len(MCP):]
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
            subs = CLI_RE.findall(cmd)
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
            elif name.startswith(MCP) and DRIVER_REFUSED.search(text):
                kind = "refused_driver"
            else:
                kind = "errors_other"
            m[kind] += 1
            if len(samples) < 12:
                samples.append({"tool": name.replace(MCP, "mcp:"), "kind": kind, "text": text[:160]})
        elif name.startswith(MCP) and BRIDGE_REFUSED.search(text[:300]):
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
