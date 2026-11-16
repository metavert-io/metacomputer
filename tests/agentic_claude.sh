#!/bin/bash
# Agentic check: a fresh, headless Claude Code session whose ONLY tools are the
# cua-linux MCP server (Cua Driver in the VM) performs a task by reasoning over
# accessibility trees and screenshots, recording itself. No scripted steps.
#
#   tests/agentic_claude.sh [task-file]     (default: tests/agentic/calculator.md)
# Output: results/agentic-<ts>/{transcript.json,summary.txt} and the run's
# recording, synced to recordings/guest-runs/<name>/.
set -euo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-/opt/homebrew/bin/python3}"; [ -x "$PY" ] || PY=python3
TASK_FILE="${1:-$LAB/tests/agentic/calculator.md}"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$LAB/results/agentic-$TS"; mkdir -p "$OUT"
NAME="agentic-$(basename "$TASK_FILE" .md)-$TS"
GUEST_RUNS="$("$LAB/bin/claude-vc" ssh ops 'printf %s "$HOME"')/cua-lab-runs"
PROMPT="$(sed "s|@RUN_DIR@|$GUEST_RUNS/$NAME|g" "$TASK_FILE")"
cd "$LAB"
"$LAB/bin/claude-vc" status >/dev/null || { echo "VM not ready (claude-vc start)"; exit 1; }
# env -u CLAUDECODE: allow a nested, independent Claude Code process.
CFG="$OUT/mcp-config.json"
printf '{"mcpServers":{"cua-linux":{"type":"stdio","command":"%s","args":["mcp-lazy"]}}}\n' "$LAB/bin/claude-vc" > "$CFG"
env -u CLAUDECODE claude -p "$PROMPT" \
  --mcp-config "$CFG" --strict-mcp-config \
  --allowedTools "mcp__cua-linux" \
  --output-format json > "$OUT/transcript.json" 2> "$OUT/stderr.log" || true
"$PY" -c 'import json,sys
d=json.load(open(sys.argv[1]))
print("result:", d.get("result","")[:3000]); print("turns:", d.get("num_turns"), "cost_usd:", d.get("total_cost_usd"), "error:", d.get("is_error"))' "$OUT/transcript.json" 2>/dev/null | tee "$OUT/summary.txt" || cat "$OUT/stderr.log"
"$LAB/bin/claude-vc" recordings --sync >/dev/null || true
echo "recording: recordings/guest-runs/$NAME/ ; transcript: results/agentic-$TS/transcript.json"
