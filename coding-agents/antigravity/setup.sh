# Antigravity CLI's part of install.sh (sourced by it; defines functions only). Its install and uninstall are
# lib/agent_setup.py's (coding-agents/antigravity/setup.py); this is what an uninstall reads without Python.
ag_antigravity_config_files() { # the file that holds its MCP servers
  AG_CONFIG_FILES=("$HOME/.gemini/config/mcp_config.json")
}
