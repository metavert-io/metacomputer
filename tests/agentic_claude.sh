#!/bin/bash
# Agentic check: a fresh, headless Claude Code session whose ONLY tools are the
# metacomputer MCP server (Cua Driver in the VM) performs a task by reasoning over
# accessibility trees and screenshots, recording itself. No scripted steps.
#
#   tests/agentic_claude.sh [task-file]     (default: tests/agentic/calculator.md)
# Output: results/agentic-<ts>/{transcript.json,summary.txt} and the run's
# recording, synced to recordings/guest-runs/<name>/.
set -euo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
. "$LAB/scripts/strictargs.sh"
if ! sa_check agentic_claude.sh "tests/agentic_claude.sh [TASK_FILE]" 0 1 "" "" "$@"; then
  sed -n '2,/^set -/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0
fi
PY="${PY:-/opt/homebrew/bin/python3}"; [ -x "$PY" ] || PY=python3
TASK_FILE="${1:-$LAB/tests/agentic/calculator.md}"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$LAB/results/agentic-$TS"; mkdir -p "$OUT"
NAME="agentic-$(basename "$TASK_FILE" .md)-$TS"
GUEST_RUNS="$("$LAB/bin/metacomputer" ssh ops 'printf %s "$HOME"')/metacomputer-runs"
PROMPT="$(sed "s|@RUN_DIR@|$GUEST_RUNS/$NAME|g" "$TASK_FILE")"
cd "$LAB"
"$LAB/bin/metacomputer" status >/dev/null || { echo "VM not ready (metacomputer start)"; exit 1; }
# env -u CLAUDECODE: allow a nested, independent Claude Code process.
CFG="$OUT/mcp-config.json"
printf '{"mcpServers":{"metacomputer":{"type":"stdio","command":"%s","args":["mcp-lazy"]}}}\n' "$LAB/bin/metacomputer" > "$CFG"
env -u CLAUDECODE claude -p "$PROMPT" \
  --mcp-config "$CFG" --strict-mcp-config \
  --allowedTools "mcp__metacomputer" "mcp__plugin_metacomputer_metacomputer" \
  --output-format json > "$OUT/transcript.json" 2> "$OUT/stderr.log" || true
"$PY" -c 'import json,sys
d=json.load(open(sys.argv[1]))
print("result:", d.get("result","")[:3000]); print("turns:", d.get("num_turns"), "cost_usd:", d.get("total_cost_usd"), "error:", d.get("is_error"))' "$OUT/transcript.json" 2>/dev/null | tee "$OUT/summary.txt" || cat "$OUT/stderr.log"
"$LAB/bin/metacomputer" recordings --sync >/dev/null || true
echo "recording: recordings/guest-runs/$NAME/ ; transcript: results/agentic-$TS/transcript.json"
