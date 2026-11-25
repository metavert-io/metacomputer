# OpenClaw's part of install.sh (sourced by it; defines functions only). Its install and uninstall are lib/agent_setup.py's
# (coding-agents/openclaw/setup.py); this is what an uninstall reads without Python.
ag_openclaw_config_files() { # every file that may hold its MCP servers: the one its CLI uses, then every profile's
  AG_CONFIG_FILES=("${OPENCLAW_CONFIG_PATH:-${OPENCLAW_STATE_DIR:-${OPENCLAW_HOME:-$HOME}/.openclaw}/openclaw.json}" \
                   "${OPENCLAW_HOME:-$HOME}/.openclaw/openclaw.json" "$HOME/.openclaw/openclaw.json")
  local d
  for d in "${OPENCLAW_HOME:-$HOME}"/.openclaw-*/ "$HOME"/.openclaw-*/; do AG_CONFIG_FILES+=("${d}openclaw.json"); done
}
