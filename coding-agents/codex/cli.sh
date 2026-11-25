# Codex's part of bin/metacomputer (sourced by it, before any command runs; defines functions only).
ag_codex_cli_start() { # COMMAND: inside Codex's sandbox, stop before a command that needs this Mac
  # Inside Codex's sandbox (Codex sets CODEX_SANDBOX for every command it sandboxes), what metacomputer needs on this
  # Mac is blocked: its state folders, the VM's SSH port, the hub's and the vault's sockets. Commands would fail in
  # confusing ways (`status` would call a running VM not ready), so they stop here and say how to run outside it
  # (docs/codex.md). The MCP tools aren't affected: Codex starts its MCP servers outside the sandbox. A Codex setup that
  # grants all of that anyway can set MCOMP_CODEX_SANDBOX=allow.
  if [ -n "${CODEX_SANDBOX:-}" ] && [ "${MCOMP_CODEX_SANDBOX:-}" != allow ]; then
    case "${1:-help}" in
      help|-h|--help) ;;
      *) printf '%s\n' "metacomputer: this command is running inside Codex's sandbox ($CODEX_SANDBOX), which blocks what metacomputer needs on this Mac (its state folders, the VM's SSH port, the hub's and the vault's sockets). Nothing was run." \
           "Run it outside the sandbox: ask for escalated permissions for this command, or add metacomputer's Codex rules (install.sh does it; docs/codex.md) so everyday metacomputer commands run outside it. The metacomputer MCP tools aren't affected." >&2
         exit 77 ;;
    esac
  fi
}
