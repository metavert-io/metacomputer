#!/opt/homebrew/bin/python3
"""Summarize a headless session transcript (stream-json) for the skill tests:
skill use, metacomputer commands, metacomputer tool calls, denied attempts, result.

  tests/skill/analyze.py RUN.jsonl      -> prints a summary, writes RUN.summary.json

The transcript reading is lib/transcripts.py, shared with `metacomputer learn`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "lib"))
import transcripts  # noqa: E402

path = sys.argv[1]
MCOMP_CMD = transcripts.MCOMP_CMD
meta_path = path[:-6] + ".meta.json"
meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
tr = transcripts.parse(path)
final = tr.final
calls = tr.calls
done = [c for c in calls if c["error"] is not None]          # calls that got a result

skills = [c["input"].get("skill") for c in calls if c["name"] == "Skill"]
bash = [c["input"].get("command", "") for c in calls if c["name"] == "Bash"]
vm_calls = tr.driver_calls
mcp = [c["tool"] for c in vm_calls]
denied = [{"tool": c["name"], "input": json.dumps(c["input"])[:160], "why": c["text"][:160]}
          for c in done if c["error"]
          and ("permission" in c["text"][:300].lower() or "denied" in c["text"][:300].lower()
               or "not allowed" in c["text"][:300].lower())]
mcp_errors = [{"tool": c["tool"], "text": c["text"][:160]} for c in vm_calls if c["error"]]
structural = transcripts.structural(tr)
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
