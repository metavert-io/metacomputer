#!/opt/homebrew/bin/python3
"""Summarize a headless session transcript (stream-json) for the skill tests:
skill use, metacomputer commands, metacomputer tool calls, denied attempts, result.

  tests/skill/analyze.py RUN.jsonl      -> prints a summary, writes RUN.summary.json
"""
import json
import re
import os
import sys

path = sys.argv[1]
# a `metacomputer` command: bare, or ~/.local/bin/metacomputer / <checkout>/bin/metacomputer
MCOMP_CMD = re.compile(r"(^|[/\s])metacomputer(\s|$)")
meta_path = path[:-6] + ".meta.json"
meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
calls, results, final = [], {}, {}
for line in open(path):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") == "assistant":
        for c in ev.get("message", {}).get("content", []):
            if c.get("type") == "tool_use":
                calls.append({"id": c["id"], "name": c["name"], "input": c.get("input", {})})
    elif ev.get("type") == "user":
        for c in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
            if c.get("type") == "tool_result":
                txt = c.get("content")
                if isinstance(txt, list):
                    txt = " ".join(x.get("text", "") for x in txt if isinstance(x, dict))
                raw = c.get("content")
                imgs = sum(1 for x in raw if isinstance(x, dict) and x.get("type") == "image") if isinstance(raw, list) else 0
                results[c.get("tool_use_id")] = {"error": bool(c.get("is_error")), "text": str(txt)[:300], "images": imgs}
    elif ev.get("type") == "result":
        final = ev

# The metacomputer tools: mcp__metacomputer__* (user-scope or --mcp-config server) or the plugin's.
MCP_PREFIXES = ("mcp__metacomputer__", "mcp__plugin_metacomputer_metacomputer__")
def _tool(c): return next((c["name"][len(p):] for p in MCP_PREFIXES if c["name"].startswith(p)), "")
skills = [c["input"].get("skill") for c in calls if c["name"] == "Skill"]
bash = [c["input"].get("command", "") for c in calls if c["name"] == "Bash"]
vm_calls = [c for c in calls if _tool(c)]
mcp = [_tool(c) for c in vm_calls]
denied = [{"tool": c["name"], "input": json.dumps(c["input"])[:160], "why": results[c["id"]]["text"][:160]}
          for c in calls if c["id"] in results and results[c["id"]]["error"]
          and ("permission" in results[c["id"]]["text"].lower() or "denied" in results[c["id"]]["text"].lower()
               or "not allowed" in results[c["id"]]["text"].lower())]
mcp_errors = [{"tool": _tool(c), "text": results[c["id"]]["text"][:160]}
              for c in vm_calls if c["id"] in results and results[c["id"]]["error"]]
reads = [c for c in vm_calls if _tool(c) == "get_window_state"]
named_steps = sum(1 for c in vm_calls if _tool(c) == "run_steps"
                  for st in c["input"].get("steps", []) if isinstance(st, dict) and "tool" not in st)
structural = {
    "images_returned": sum(results.get(c["id"], {}).get("images", 0) for c in vm_calls),
    "reads": len(reads),
    "reads_tree_only": sum(1 for c in reads if c["input"].get("include_screenshot") is False),
    "reads_since": sum(1 for c in reads if c["input"].get("since")),
    "run_steps": sum(1 for c in vm_calls if _tool(c) == "run_steps"),
    "steps_by_name": named_steps,
    "expect_checks": sum(json.dumps(c["input"]).count('"expect"') for c in vm_calls if _tool(c) == "run_steps"),
    "verify_state": sum(1 for c in vm_calls if _tool(c) == "verify_state"),
    "run_script": sum(1 for c in vm_calls if _tool(c) == "run_script"),
    "list_apps": sum(1 for c in vm_calls if _tool(c) == "list_apps"),
    "screenshots_desktop_or_zoom": sum(1 for c in vm_calls if _tool(c) in ("get_desktop_state", "zoom")),
    "inspect": sum(1 for b in bash if re.search(r"(^|[/\s])metacomputer inspect\b", b)),
    "page_tools": sum(1 for c in vm_calls if _tool(c).startswith("browser_") or _tool(c) == "get_browser_state"),
    "web_cmds": sum(1 for b in bash if re.search(r"(^|[/\s])metacomputer web\b", b)),
    "chrome_launch": sum(1 for c in vm_calls if "chrome" in json.dumps(c["input"]).lower()
                         and _tool(c) in ("launch_app", "browser_prepare")),
    "bridge_notes": sum(1 for c in vm_calls if "metacomputer bridge" in results.get(c["id"], {}).get("text", "")),
}
usage = final.get("usage") or {}
summary = {
    **meta,
    "structural": structural,
    "tokens": {k: usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")},
    "skill_invoked": skills,
    "mcomp_commands": [b for b in bash if MCOMP_CMD.search(b)],
    "other_bash": [b for b in bash if not MCOMP_CMD.search(b)],
    "mcp_tools": mcp,
    "mcp_errors": mcp_errors,
    "denied": denied,
    "other_tools": sorted({c["name"] for c in calls if c["name"] not in ("Bash", "Skill") and not c["name"].startswith("mcp__")}),
    "turns": final.get("num_turns"), "cost_usd": final.get("total_cost_usd"),
    "duration_s": round((final.get("duration_ms") or 0) / 1000, 1), "is_error": final.get("is_error"),
    "result": (final.get("result") or "")[:4000],
}
json.dump(summary, open(path[:-6] + ".summary.json", "w"), indent=2)
print(f"== {meta.get('name')}  VM {meta.get('vm_before')} -> {meta.get('vm_after')}  turns={summary['turns']} "
      f"cost=${summary['cost_usd']} {summary['duration_s']}s error={summary['is_error']}")
print(f"   skill: {skills or '-'}")
print(f"   structure: {structural}")
print(f"   metacomputer: {[b[:110] for b in summary['mcomp_commands']] or '-'}")
print(f"   mcp ({len(mcp)}): {', '.join(mcp) or '-'}")
if mcp_errors:
    print(f"   mcp errors: {mcp_errors}")
if summary["other_bash"]:
    print(f"   other bash: {[b[:110] for b in summary['other_bash']]}")
if denied:
    print(f"   denied: {denied}")
print("   result: " + summary["result"].replace("\n", "\n           ")[:2500])
