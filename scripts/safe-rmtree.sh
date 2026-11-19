#!/bin/bash
# scripts/safe-rmtree.sh PATH...: rm -rf for metacomputer's scripts, through lib/safedelete.py: each PATH is deleted
# only when, as the folder it physically is, it neither is nor holds a folder some VM of this checkout uses (every
# VM's keys, runtime, logs, vault and spool, the account's home, the checkout). Status 1, with the reason, when one is
# refused or the VMs can't be read; then nothing more is deleted. --quiet: say a refusal and go on (status 0).
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
py="${PY:-/opt/homebrew/bin/python3}"
[ -x "$py" ] || py="$(command -v python3 2>/dev/null)" || { echo "metacomputer: no python3 to check what to delete; nothing was deleted" >&2; exit 1; }
quiet=()
[ "${1:-}" != --quiet ] || { quiet=(--quiet); shift; }
exec "$py" -B "$here/../lib/safedelete.py" rmtree ${quiet[@]+"${quiet[@]}"} -- "$@"
