# metacomputer's coding agents, as bash reads them: made from coding-agents/<name>/manifest.json by
# scripts/gen-agents.py (don't edit by hand: its --check fails a copy that isn't current). Sourced by install.sh
# and bin/metacomputer; data and one lookup only, nothing runs.
MC_AGENTS='claude-code codex openclaw hermes gemini antigravity'
MC_AGENT_FLAGS='claude codex openclaw hermes gemini antigravity'
MC_AGENT_GENERIC='openclaw hermes gemini antigravity'
MC_AGENT_INSTALL_FLAGS='--claude --no-claude --only-claude --no-plugin --codex --no-codex --only-codex --openclaw --no-openclaw --only-openclaw --hermes --no-hermes --only-hermes --gemini --no-gemini --only-gemini --antigravity --no-antigravity --only-antigravity'
MC_AGENT_LABELS_OR='Claude Code, Codex, OpenClaw, Hermes Agent, Gemini CLI or Antigravity CLI'
MC_AGENT_RELEASE_FILE='gemini-extension.json'
MC_AGENT_SWITCH_WORDS='Claude Code'\''s plugin and MCP server'
mc_agent() { # FLAG KEY: that agent's dir (its folder under coding-agents/), label, prog, docs, missing (line or
  # listed), steps (generic or hooks) or only_note; status 1: no such agent
  case "$1:$2" in
    claude:dir) printf '%s' 'claude-code' ;;
    claude:label) printf '%s' 'Claude Code' ;;
    claude:prog) printf '%s' 'claude' ;;
    claude:docs) printf '%s' 'docs/install.md' ;;
    claude:missing) printf '%s' 'line' ;;
    claude:steps) printf '%s' 'hooks' ;;
    claude:only_note) printf '%s' 'Start a new Claude Code session to pick this up.' ;;
    codex:dir) printf '%s' 'codex' ;;
    codex:label) printf '%s' 'Codex' ;;
    codex:prog) printf '%s' 'codex' ;;
    codex:docs) printf '%s' 'docs/codex.md' ;;
    codex:missing) printf '%s' 'line' ;;
    codex:steps) printf '%s' 'hooks' ;;
    codex:only_note) printf '%s' 'Start a new Codex session to pick this up.' ;;
    openclaw:dir) printf '%s' 'openclaw' ;;
    openclaw:label) printf '%s' 'OpenClaw' ;;
    openclaw:prog) printf '%s' 'openclaw' ;;
    openclaw:docs) printf '%s' 'docs/openclaw.md' ;;
    openclaw:missing) printf '%s' 'listed' ;;
    openclaw:steps) printf '%s' 'generic' ;;
    openclaw:only_note) printf '%s' 'Start a new OpenClaw session to pick this up.' ;;
    hermes:dir) printf '%s' 'hermes' ;;
    hermes:label) printf '%s' 'Hermes Agent' ;;
    hermes:prog) printf '%s' 'hermes' ;;
    hermes:docs) printf '%s' 'docs/hermes.md' ;;
    hermes:missing) printf '%s' 'listed' ;;
    hermes:steps) printf '%s' 'generic' ;;
    hermes:only_note) printf '%s' 'Start a new Hermes Agent session to pick this up.' ;;
    gemini:dir) printf '%s' 'gemini' ;;
    gemini:label) printf '%s' 'Gemini CLI' ;;
    gemini:prog) printf '%s' 'gemini' ;;
    gemini:docs) printf '%s' 'docs/gemini.md' ;;
    gemini:missing) printf '%s' 'listed' ;;
    gemini:steps) printf '%s' 'generic' ;;
    gemini:only_note) printf '%s' 'Start a new Gemini CLI session to pick this up.' ;;
    antigravity:dir) printf '%s' 'antigravity' ;;
    antigravity:label) printf '%s' 'Antigravity CLI' ;;
    antigravity:prog) printf '%s' 'agy' ;;
    antigravity:docs) printf '%s' 'docs/antigravity.md' ;;
    antigravity:missing) printf '%s' 'listed' ;;
    antigravity:steps) printf '%s' 'generic' ;;
    antigravity:only_note) printf '%s' 'Start a new Antigravity CLI session to pick this up.' ;;
    *) return 1 ;;
  esac
}
