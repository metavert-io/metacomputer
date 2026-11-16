#!/bin/bash
# Run one headless Claude Code session for the metacomputer skill tests and
# keep its full stream-json transcript (every tool call and result).
#
#   tests/skill/run_headless.sh OUT_DIR NAME WORKDIR "PROMPT" [--global-mcp]
#
# The session runs in WORKDIR (a folder unrelated to the lab). By default it
# sees only the metacomputer server (same definition as the user-scope one);
# --global-mcp uses the user's real MCP configuration instead.
set -uo pipefail
OUT="$1"; NAME="$2"; WORKDIR="$3"; PROMPT="$4"; MODE="${5:-}"
mkdir -p "$OUT" && OUT="$(cd "$OUT" && pwd)"
LAB="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$OUT"
MCP_ARGS=()
if [ "$MODE" != "--global-mcp" ]; then
  CFG="$OUT/mcp-cua-only.json"
  printf '{"mcpServers":{"metacomputer":{"type":"stdio","command":"%s","args":["mcp-lazy"]}}}\n' "$LAB/bin/metacomputer" > "$CFG"
  MCP_ARGS=(--strict-mcp-config --mcp-config "$CFG")
fi
vm_state() { "$LAB/bin/metacomputer" status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); v=d.get("vm"); print(v if isinstance(v,str) else (v or {}).get("status","?"))' 2>/dev/null; }
vm_before="$(vm_state)"
start=$(date +%s)
cd "$WORKDIR" || exit 1
env -u CLAUDECODE claude -p "$PROMPT" ${MCP_ARGS[@]+"${MCP_ARGS[@]}"} \
  --allowedTools "Skill" "Read" "Glob" "Grep" "WebFetch" "ToolSearch" "mcp__metacomputer" \
    "Bash($LAB/bin/metacomputer:*)" "Bash(~/.local/bin/metacomputer:*)" "Bash(metacomputer:*)" \
    "Bash(python3 $LAB/lib/mcp_client.py:*)" \
  --max-turns 80 --output-format stream-json --verbose > "$OUT/$NAME.jsonl" 2> "$OUT/$NAME.stderr"
vm_after="$(vm_state)"
printf '{"name":"%s","workdir":"%s","vm_before":"%s","vm_after":"%s","wall_s":%s}\n' \
  "$NAME" "$WORKDIR" "$vm_before" "$vm_after" "$(( $(date +%s) - start ))" > "$OUT/$NAME.meta.json"
"$LAB/tests/skill/analyze.py" "$OUT/$NAME.jsonl"
