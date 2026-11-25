# Gemini CLI's part of install.sh (sourced by it; defines functions only). Its install and uninstall are
# lib/agent_setup.py's (coding-agents/gemini/setup.py); this is what an uninstall reads without Python.
ag_gemini_config_files() { # every file that may hold its MCP servers: under GEMINI_CLI_HOME, then yours
  AG_CONFIG_FILES=("${GEMINI_CLI_HOME:-$HOME}/.gemini/settings.json" "$HOME/.gemini/settings.json")
}
