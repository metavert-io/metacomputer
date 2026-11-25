# Hermes Agent's part of install.sh (sourced by it; defines functions only). Its install and uninstall are
# lib/agent_setup.py's (coding-agents/hermes/setup.py); this is what an uninstall reads without Python.
ag_hermes_config_files() { # every file that may hold its MCP servers: its home's, the root's, and every profile's
  AG_CONFIG_FILES=("${HERMES_HOME:-$HOME/.hermes}/config.yaml" "$HOME/.hermes/config.yaml")
  local d
  for d in "$HOME"/.hermes/profiles/*/; do AG_CONFIG_FILES+=("${d}config.yaml"); done
}
