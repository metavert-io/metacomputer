#!/bin/bash
# metacomputer host library: configuration, Lume + cua-netgw
# lifecycle, SSH. Sourced by bin/metacomputer and scripts/*. macOS /bin/bash 3.2.

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- which VM: its settings (docs/commands.md, "More than one VM") -------------------
# config/defaults.env, then ONE settings file, never two layered:
#   --vm NAME (bin/metacomputer) or MCOMP_VM=NAME   config/vms/NAME.env; config/local.env when that names NAME;
#                                                   the defaults alone for the default name with no local.env
#   MCOMP_CONFIG=FILE                               that file (as before); with a name too, both must be the same file
#   neither                                         config/local.env (this checkout's main VM), else the defaults
# A file in config/vms/ is one VM's own: its VM_NAME is its file name, it sets its own ports, and the rest of its
# identity (baseline, keys, runtime and log folders, vault folder, event spool) is its own too, never the main VM's.
# Anything unclear (a name with no settings, two files for one VM, a file that borrows the main VM's ports) is
# refused, never guessed. Nothing here writes anything.
MC_VMS_DIR="$LAB_ROOT/config/vms"
MC_MAIN_CONFIG="$LAB_ROOT/config/local.env"
# The account's home folder as the system has it (the vault and Lume use it, whatever $HOME says).
mc_sys_home() {
  local u h=""; u="$(id -un 2>/dev/null)"
  case "$u" in ''|*[!A-Za-z0-9._-]*) ;; *) eval "h=~$u" ;; esac
  case "$h" in /*) printf '%s' "$h" ;; *) printf '%s' "$HOME" ;; esac
}
MC_SYS_HOME="$(mc_sys_home)"
# Where a config/vms/ VM's vault folder (NAME, its event spool inside) goes: beside the account's own vault folder
# (.../metacomputer, the main VM's), never in it, so no VM's folder is inside another's. MCOMP_VMS_APP_DIR moves it
# (tests). The account's own vault folder, which the main VM and MCOMP_CONFIG VMs share unless they set VAULT_DIR:
MC_VMS_APP_DIR="${MCOMP_VMS_APP_DIR:-$MC_SYS_HOME/Library/Application Support/metacomputer-vms}"
MC_SHARED_VAULT="$MC_SYS_HOME/Library/Application Support/metacomputer"
mc_valid_vm_name() { case "$1" in ''|[-.]*|*[!A-Za-z0-9._-]*) return 1 ;; esac; [ ${#1} -le 64 ]; }
mc_realpath() { # PATH -> absolute, with its folder's links resolved (the file itself as named)
  local d; d="$(cd "$(dirname "$1")" 2>/dev/null && pwd -P)" || return 1
  printf '%s/%s' "$d" "$(basename "$1")"
}
mc_tilde() { case "$1" in "$MC_SYS_HOME"/*) printf '~/%s' "${1#"$MC_SYS_HOME"/}" ;; *) printf '%s' "$1" ;; esac; }
mc_file_vm_name() { # FILE -> the VM_NAME it ends with, after the defaults (sourced in a subshell)
  ( . "$LAB_ROOT/config/defaults.env"; . "$1" >/dev/null 2>&1; printf '%s' "$VM_NAME" ) 2>/dev/null
}
mc_main_identity() { # the main VM's VM_NAME BASELINE_NAME SSH_DIR SSH_PORT VNC_PORT RUN_DIR LOG_DIR, one per line
  ( . "$LAB_ROOT/config/defaults.env"; [ ! -f "$MC_MAIN_CONFIG" ] || . "$MC_MAIN_CONFIG" >/dev/null 2>&1
    printf '%s\n' "$VM_NAME" "$BASELINE_NAME" "$SSH_DIR" "$SSH_PORT" "$VNC_PORT" "${RUN_DIR:-$LAB_ROOT/.run}" "${LOG_DIR:-$LAB_ROOT/logs}" ) 2>/dev/null
}
mc_vm_files() { # the VM names config/vms/ has settings for, one per line
  local f; for f in "$MC_VMS_DIR"/*.env; do [ -f "$f" ] && basename "$f" .env; done; return 0
}
# FILE: source a settings file here, strictly: the one way every settings file is read (the settings loaded for a
# command, `vm list`'s rows, the identities of the other VMs). It must be a readable file that parses (bash -n), and
# reading it must say nothing on stderr (a file it sources but can't read, a command that fails). Otherwise status 1
# with the reason in MC_REFUSAL: what's set may be guesses (the defaults), so nothing may rely on it.
mc_source_strict() {
  local __mcss_f="$1" __mcss_e
  { [ -f "$__mcss_f" ] && [ -r "$__mcss_f" ] && /bin/bash -n "$__mcss_f"; } >/dev/null 2>&1 \
    || { MC_REFUSAL="can't read the settings file $(mc_tilde "$__mcss_f") (or it doesn't parse); nothing was done"; return 1; }
  __mcss_e="$(mktemp "${TMPDIR:-/tmp}/mcomp-settings.XXXXXX" 2>/dev/null)" \
    || { MC_REFUSAL="can't read the settings file $(mc_tilde "$__mcss_f") (no temporary file); nothing was done"; return 1; }
  # shellcheck source=/dev/null
  . "$__mcss_f" 2>"$__mcss_e" || :
  if [ -s "$__mcss_e" ]; then
    MC_REFUSAL="reading the settings file $(mc_tilde "$__mcss_f") went wrong ($(head -c 160 "$__mcss_e" | tr '\n' ' ')); nothing was done"
    rm -f "$__mcss_e"; return 1
  fi
  rm -f "$__mcss_e"
}
mc_abs_dir() { # SETTING VALUE -> MC_ABS: VALUE as an absolute folder (~/ is the account's home); status 1 otherwise
  case "$2" in "~") MC_ABS="$MC_SYS_HOME" ;; "~/"*) MC_ABS="$MC_SYS_HOME/${2#\~/}" ;; /*) MC_ABS="$2" ;;
    *) MC_REFUSAL="$1 must be an absolute folder, not '$2'"; return 1 ;; esac
}
# SETTING VALUE -> MC_CANON: VALUE as the folder it really is: absolute, no . or .. in it, with the links in the part
# that exists resolved (a link to another VM's vault becomes that vault's path, and is caught as the same folder).
mc_canon() {
  local p head rest="" c
  mc_abs_dir "$1" "$2" || return 1
  p="$MC_ABS"
  case "$p/" in */./*|*/../*) MC_REFUSAL="$1 can't have . or .. in it ($2)"; return 1 ;; esac
  while :; do case "$p" in *//*) p="${p//\/\//\/}" ;; *) break ;; esac; done
  p="${p%/}"; [ -n "$p" ] || p=/
  head="$p"
  while [ "$head" != / ] && [ ! -d "$head" ]; do
    c="${head##*/}"; rest="$c${rest:+/$rest}"; head="${head%/*}"; [ -n "$head" ] || head=/
  done
  head="$(cd "$head" 2>/dev/null && pwd -P)" || { MC_REFUSAL="$1: can't read $head"; return 1; }
  MC_CANON="${head%/}/$rest"; MC_CANON="${MC_CANON%/}"; [ -n "$MC_CANON" ] || MC_CANON=/
}
# A config/vms/ VM's own folders when its file doesn't set them (scripts/lib.sh and vm create): none is inside the
# main VM's (.run, logs, the account's vault folder) or another VM's.
mc_vm_default() { # KEY NAME
  case "$1" in
    SSH_DIR) printf '%s' "$HOME/.ssh/metacomputer-$2" ;;
    RUN_DIR) printf '%s' "$LAB_ROOT/.run-vms/$2" ;;
    LOG_DIR) printf '%s' "$LAB_ROOT/logs-vms/$2" ;;
    VAULT_DIR) printf '%s' "$MC_VMS_APP_DIR/$2" ;;
    EVENTS_SPOOL) printf '%s' "$MC_VMS_APP_DIR/$2/events/spool" ;;
  esac
}
# VAULT_DIR EVENTS_SPOOL (as loaded) of a VM that isn't a config/vms/ one: OWNER KEY VALUE records, the account's
# shared vault and spool (SHARED_*: the vault's and the event stream's own defaults, the latter from $HOME and from
# the system's home) when it doesn't set its own.
mc_shared_records() { # OWNER VAULT_DIR EVENTS_SPOOL
  if [ -z "$2" ] || [ "$2" = "$MC_SHARED_VAULT" ]; then printf '%s\0' "$1" SHARED_VAULT_DIR "$MC_SHARED_VAULT"
  else printf '%s\0' "$1" VAULT_DIR "$2"; fi
  if [ -z "$3" ] || [ "$3" = "$MC_SHARED_VAULT/events/spool" ]; then
    printf '%s\0' "$1" SHARED_EVENTS_SPOOL "$HOME/Library/Application Support/metacomputer/events/spool" \
      "$1" SHARED_EVENTS_SPOOL "$MC_SHARED_VAULT/events/spool"
  else printf '%s\0' "$1" EVENTS_SPOOL "$3"; fi
}
# Every place the runner keeps VMs, one absolute path a line: Lume's own default (~/.lume, ~ being the account's home
# as Lume has it) always, the storage locations the runner reports (`config get`), and those in Lume's settings file.
# More than there may be is fine: it's where no VM's folder may be (lib/vms.py identity).
mc_runner_storage() {
  local lb l h
  lb="${LUME:-$HOME/.local/bin/mcomp-vm}"
  command -v "$lb" >/dev/null 2>&1 || lb="$HOME/.local/bin/lume-secure"
  {
    printf '%s\n' "~/.lume"
    if command -v "$lb" >/dev/null 2>&1; then
      "$lb" config get 2>/dev/null </dev/null | awk '/^Configured VM storage locations:/ { f = 1; next } f && NF == 0 { f = 0 }
        f && /^[[:space:]]*- / { l = $0; sub(/^[[:space:]]*- [^:]*: /, "", l); sub(/ \(default\)$/, "", l); print l }' || :
    fi
    for h in "$MC_SYS_HOME" "$HOME"; do
      [ ! -f "$h/.config/lume/config.yaml" ] \
        || awk '/^[[:space:]]+path:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); gsub(/"/, "", v); print v }' "$h/.config/lume/config.yaml" 2>/dev/null || :
    done
  } | while IFS= read -r l; do
    case "$l" in "~") printf '%s\n' "$MC_SYS_HOME" ;; "~/"*) printf '%s\n' "$MC_SYS_HOME/${l#\~/}" ;; /*) printf '%s\n' "$l" ;; esac
  done
}
# OWNER KIND [NAME|FILE]: one VM's identity as its file says it (in a subshell, unchecked), NUL-separated OWNER KEY
# VALUE records for `lib/vms.py identity` and error rows: names, ports and folders. KIND main: config/local.env (or the
# defaults), with the account's shared vault and spools unless it sets its own (mc_shared_records); file: that
# settings file, the same way; vm: config/vms/NAME.env with its own defaults.
mc_identity_records() {
  ( owner="$1"; ikind="$2"; iname="${3:-}"
    . "$LAB_ROOT/config/defaults.env"
    ifile="$MC_MAIN_CONFIG"
    [ "$ikind" != file ] || ifile="$iname"
    [ "$ikind" != vm ] || ifile="$MC_VMS_DIR/$iname.env"
    # A file that should be read and can't be, completely and without complaint (mc_source_strict, the same reading
    # as every command's), has no known identity: say so (UNREADABLE) instead of the defaults, which would only be
    # guesses. (The main VM's config/local.env may be absent: then the defaults are what it is.)
    unreadable() { printf '%s\0' "$owner" UNREADABLE 1; exit 0; }
    if [ "$ikind" = vm ]; then
      VM_NAME=""; BASELINE_NAME=""; SSH_DIR=""; SSH_PORT=""; VNC_PORT=""; RUN_DIR=""; LOG_DIR=""
      mc_source_strict "$ifile" >/dev/null || unreadable
      [ -n "$VM_NAME" ] || VM_NAME="$iname"
      [ -n "$BASELINE_NAME" ] || BASELINE_NAME="$VM_NAME-baseline"
      for k in SSH_DIR RUN_DIR LOG_DIR VAULT_DIR EVENTS_SPOOL; do
        eval "[ -n \"\${$k:-}\" ] || $k=\"\$(mc_vm_default $k \"\$VM_NAME\")\""
      done
      ks="VAULT_DIR EVENTS_SPOOL"
    else
      if [ -e "$ifile" ] || [ "$ikind" != main ]; then mc_source_strict "$ifile" >/dev/null || unreadable; fi
      RUN_DIR="${RUN_DIR:-$LAB_ROOT/.run}"; LOG_DIR="${LOG_DIR:-$LAB_ROOT/logs}"
      mc_shared_records "$owner" "${VAULT_DIR:-}" "${EVENTS_SPOOL:-}"
      ks=""
    fi
    for k in VM_NAME BASELINE_NAME SSH_PORT VNC_PORT SSH_DIR RUN_DIR LOG_DIR $ks VOLUMES; do
      eval "printf '%s\0' \"\$owner\" \"\$k\" \"\${$k:-}\""
    done ) 2>/dev/null
}
# MAIN_VM MAIN_LABEL: the records for `lib/vms.py identity`: this VM's (self, from the variables loaded, any kind), the
# main VM's (unless this is the main VM), every other config/vms/ VM's and the runner's VM storage (@runner).
mc_identity_input() {
  local k
  for k in VM_NAME BASELINE_NAME SSH_PORT VNC_PORT SSH_DIR; do
    eval "printf '%s\0' self \"\$k\" \"\${$k:-}\""
  done
  printf '%s\0' self RUN_DIR "${RUN_DIR:-$LAB_ROOT/.run}" self LOG_DIR "${LOG_DIR:-$LAB_ROOT/logs}"
  if [ "${MC_SETTINGS_KIND:-}" = vm ]; then
    printf '%s\0' self VAULT_DIR "$VAULT_DIR" self EVENTS_SPOOL "$EVENTS_SPOOL"
  else
    mc_shared_records self "${VAULT_DIR:-}" "${EVENTS_SPOOL:-}"
  fi
  [ "$1" = "$VM_NAME" ] || mc_identity_records "$2" main
  for k in $(mc_vm_files); do [ "$k" = "$VM_NAME" ] || mc_identity_records "config/vms/$k.env" vm "$k"; done
  mc_runner_storage | while IFS= read -r k; do printf '%s\0' @runner STORAGE "$k"; done
}
# [--no-ports]: the identity rule (lib/vms.py identity) for the VM these settings are, of any kind, against every other
# VM this checkout has settings for (each must read cleanly) and the runner's VM storage: no name, port (unless
# --no-ports) or folder of one is another's, no folder of one is inside another's or holds one, and no VM's folder
# is in the runner's VM storage or holds it. 0 with lib/vms.py's output in MC_IDENTITY_OUT (the vault folder and
# spool, canonical), or 1 with the reason in MC_REFUSAL. Every config/vms/ VM's settings pass it when they're loaded;
# vm delete and build-vm.sh run it for whichever VM they change, before they delete anything.
mc_identity_check() {
  local main_vm label
  main_vm="$MC_DEFAULT_VM"; [ ! -f "$MC_MAIN_CONFIG" ] || main_vm="$(mc_file_vm_name "$MC_MAIN_CONFIG")"
  label="the main VM (the defaults)"; [ ! -f "$MC_MAIN_CONFIG" ] || label="the main VM (config/local.env)"
  # The refusal is a marker, not a failing command, so an ERR trap (build-vm.sh's) doesn't fire in the subshell.
  MC_IDENTITY_OUT="$(mc_identity_input "$main_vm" "$label" | "$MC_PY" "$LAB_ROOT/lib/vms.py" identity --home "$MC_SYS_HOME" "$@" 2>&1 \
    || printf '\n@mc-refused')"
  case "$MC_IDENTITY_OUT" in
    *@mc-refused)
      MC_IDENTITY_OUT="$(printf '%s' "${MC_IDENTITY_OUT%@mc-refused}")"   # without the trailing newlines
      MC_REFUSAL="${MC_IDENTITY_OUT:-its identity could not be checked}"; return 1 ;;
  esac
}
# The resolution's own Python (scripts/lib.sh sets PY again once the settings are read: a settings file may set it).
MC_PY="${PY:-/opt/homebrew/bin/python3}"; [ -x "$MC_PY" ] || MC_PY=python3
# The per-VM folders' root, as the folder it really is (so a per-VM vault's path is its canonical one).
if mc_canon MCOMP_VMS_APP_DIR "$MC_VMS_APP_DIR"; then MC_VMS_APP_DIR="$MC_CANON"; fi
# NAME CONFIG: load the settings that NAME (--vm / MCOMP_VM) and CONFIG (MCOMP_CONFIG) select. 0 with them loaded
# (LOCAL_CONFIG, MC_SETTINGS_KIND vm|local|config|defaults, MC_SETTINGS_LABEL, MC_IMPLICIT, VAULT_DIR, EVENTS_SPOOL),
# or 1 with the reason in MC_REFUSAL.
mc_load_settings() {
  local name="$1" cfg="$2" file="" kind="" main_vm="" x_vault=0 x_spool=0 k v
  MC_REFUSAL=""; MC_IMPLICIT=0; MC_MISSING_CONFIG=""
  # shellcheck source=../config/defaults.env
  . "$LAB_ROOT/config/defaults.env"
  MC_DEFAULT_VM="$VM_NAME"
  if [ -n "$name" ]; then
    mc_valid_vm_name "$name" || { MC_REFUSAL="'$name' isn't a VM name (letters, digits, '.', '-' and '_')"; return 1; }
    [ -f "$MC_MAIN_CONFIG" ] && main_vm="$(mc_file_vm_name "$MC_MAIN_CONFIG")"
    if [ -f "$MC_VMS_DIR/$name.env" ]; then
      [ "$main_vm" != "$name" ] || { MC_REFUSAL="config/local.env and config/vms/$name.env are both settings for the VM $name; keep one"; return 1; }
      file="$MC_VMS_DIR/$name.env"
    elif [ -n "$main_vm" ] && [ "$main_vm" = "$name" ]; then
      file="$MC_MAIN_CONFIG"
    elif [ ! -f "$MC_MAIN_CONFIG" ] && [ "$name" = "$MC_DEFAULT_VM" ]; then
      file=""
    else
      MC_REFUSAL="this checkout has no settings for a VM named $name (no config/vms/$name.env). metacomputer vm list shows its VMs; metacomputer vm create $name makes one"
      return 1
    fi
    if [ -n "$cfg" ] && [ "$(mc_realpath "$cfg")" != "$(mc_realpath "${file:-/nonexistent/none}")" ]; then
      MC_REFUSAL="MCOMP_CONFIG ($cfg) and the VM name $name choose different settings; use one of them"
      return 1
    fi
  elif [ -n "$cfg" ]; then
    # A file that isn't there means the defaults (as before), but no command acts on a VM with them
    # (vm_target_check): a mistyped name must not land on the default VM.
    if [ -f "$cfg" ]; then file="$cfg"; else MC_MISSING_CONFIG="$cfg"; MC_IMPLICIT=1; fi
  elif [ -f "$MC_MAIN_CONFIG" ]; then
    file="$MC_MAIN_CONFIG"
  else
    MC_IMPLICIT=1    # the defaults, unasked for: vm_target_check decides whether that's clear enough
  fi
  # A settings file that can't be read (or read cleanly) is refused, never replaced by the defaults (another VM).
  if [ -n "$file" ] && [ ! -r "$file" ]; then MC_REFUSAL="can't read the settings file $(mc_tilde "$file"); nothing was done"; return 1; fi
  # A file in config/vms/ (however it was chosen) is one VM's own settings.
  kind=defaults; [ -n "$file" ] && kind=config
  [ "$file" = "$MC_MAIN_CONFIG" ] && kind=local
  if [ -n "$file" ] && [ -d "$MC_VMS_DIR" ]; then
    case "$(mc_realpath "$file")" in "$(cd "$MC_VMS_DIR" && pwd -P)"/*.env) kind=vm; name="$(basename "$file" .env)" ;; esac
  fi
  if [ "$kind" = vm ]; then
    mc_valid_vm_name "$name" || { MC_REFUSAL="config/vms/$name.env: '$name' isn't a VM name"; return 1; }
    # What makes a VM itself starts empty, so nothing of the default VM's is used by accident.
    VM_NAME=""; BASELINE_NAME=""; SSH_DIR=""; SSH_PORT=""; VNC_PORT=""; RUN_DIR=""; LOG_DIR=""
    # shellcheck source=/dev/null
    mc_source_strict "$file" || return 1
    [ -n "$VM_NAME" ] || VM_NAME="$name"
    [ "$VM_NAME" = "$name" ] || { MC_REFUSAL="config/vms/$name.env names the VM '$VM_NAME'; a file in config/vms/ is for the VM of its own name"; return 1; }
    # A name shaped like a VM's copy (a baseline, its previous one, a checkpoint) could be another VM's: refused.
    case "$VM_NAME" in *-baseline|*-baseline-prev|*-ckpt-*)
      MC_REFUSAL="config/vms/$name.env: a VM's name can't end in -baseline or -baseline-prev or have -ckpt- in it (those are VMs' copies)"; return 1 ;; esac
    # Its baseline is always NAME-baseline: never a name another VM uses (the baseline command replaces it).
    [ -z "$BASELINE_NAME" ] || [ "$BASELINE_NAME" = "$VM_NAME-baseline" ] \
      || { MC_REFUSAL="config/vms/$name.env: BASELINE_NAME must be $VM_NAME-baseline (or left out), not '$BASELINE_NAME'"; return 1; }
    BASELINE_NAME="$VM_NAME-baseline"
    for k in SSH_PORT VNC_PORT; do
      eval "v=\$$k"
      [[ "$v" =~ ^[0-9]{4,5}$ ]] && [ "$v" -ge 1024 ] && [ "$v" -le 65535 ] \
        || { MC_REFUSAL="config/vms/$name.env must set $k to a port of its own (1024-65535)"; return 1; }
    done
    [ "$SSH_PORT" != "$VNC_PORT" ] || { MC_REFUSAL="config/vms/$name.env: SSH_PORT and VNC_PORT are the same"; return 1; }
    [ -z "${VAULT_DIR:-}" ] || x_vault=1
    [ -z "${EVENTS_SPOOL:-}" ] || x_spool=1
    # Its own folders where the file doesn't say (mc_vm_default): .run-vms/NAME and logs-vms/NAME in this checkout,
    # ~/.ssh/metacomputer-NAME, and .../metacomputer-vms/NAME for its vault (its spool in it).
    for k in SSH_DIR RUN_DIR LOG_DIR VAULT_DIR EVENTS_SPOOL; do
      eval "[ -n \"\${$k:-}\" ] || $k=\"\$(mc_vm_default $k \"\$VM_NAME\")\""
    done
    # MCOMP_VAULT_DIR / MCOMP_EVENTS_SPOOL from the environment come first, so the check below is of the folders this
    # VM will really use (one that isn't the folder the file itself sets is refused).
    if [ -n "${MCOMP_VAULT_DIR:-}" ]; then
      mc_canon MCOMP_VAULT_DIR "$MCOMP_VAULT_DIR" || return 1; v="$MC_CANON"
      if [ "$x_vault" = 1 ]; then
        mc_canon VAULT_DIR "$VAULT_DIR" || return 1
        [ "$MC_CANON" = "$v" ] || { MC_REFUSAL="MCOMP_VAULT_DIR ($MCOMP_VAULT_DIR) and VAULT_DIR in config/vms/$name.env ($VAULT_DIR) name different vault folders; unset one"; return 1; }
      fi
      VAULT_DIR="$v"
    fi
    if [ -n "${MCOMP_EVENTS_SPOOL:-}" ]; then
      mc_canon MCOMP_EVENTS_SPOOL "$MCOMP_EVENTS_SPOOL" || return 1; v="$MC_CANON"
      if [ "$x_spool" = 1 ]; then
        mc_canon EVENTS_SPOOL "$EVENTS_SPOOL" || return 1
        [ "$MC_CANON" = "$v" ] || { MC_REFUSAL="MCOMP_EVENTS_SPOOL ($MCOMP_EVENTS_SPOOL) and EVENTS_SPOOL in config/vms/$name.env ($EVENTS_SPOOL) name different spools; unset one"; return 1; }
      fi
      EVENTS_SPOOL="$v"
    fi
    # The identity rule (mc_identity_check): nothing that makes this VM may be another's, the main VM's or another
    # config/vms/ VM's, and none of its folders may be in another's, hold one, or be in the runner's VM storage.
    # Folders are compared as the folders they are (device and inode, links and . resolved), names with each VM's
    # copies, and ports. lib/vms.py identity prints the vault folder and spool as their canonical paths.
    main_vm="$MC_DEFAULT_VM"; [ ! -f "$MC_MAIN_CONFIG" ] || main_vm="$(mc_file_vm_name "$MC_MAIN_CONFIG")"
    if [ "$main_vm" = "$VM_NAME" ] && [ -f "$MC_MAIN_CONFIG" ]; then
      MC_REFUSAL="config/local.env and config/vms/$name.env are both settings for the VM $name; keep one"; return 1
    fi
    MC_SETTINGS_KIND=vm
    mc_identity_check || { MC_REFUSAL="config/vms/$name.env: $MC_REFUSAL"; return 1; }
    v="$MC_IDENTITY_OUT"
    VAULT_DIR="$(printf '%s\n' "$v" | sed -n 's/^VAULT_DIR=//p')"; EVENTS_SPOOL="$(printf '%s\n' "$v" | sed -n 's/^EVENTS_SPOOL=//p')"
    [ -n "$VAULT_DIR" ] && [ -n "$EVENTS_SPOOL" ] || { MC_REFUSAL="config/vms/$name.env: its identity couldn't be checked"; return 1; }
  elif [ -n "$file" ]; then
    # shellcheck source=/dev/null
    mc_source_strict "$file" || return 1
  fi
  # The vault folder and the event spool (handed to mcomp-vault as MCOMP_VAULT_DIR and MCOMP_EVENTS_SPOOL), as the
  # folders they really are. Empty for the main VM and MCOMP_CONFIG files unless set: the vault's and the event
  # stream's own defaults, as before. A config/vms/ VM has folders of its own, and the environment's were applied
  # before its identity check (above). MCOMP_VAULT_DIR / MCOMP_EVENTS_SPOOL in the environment override every VM's, as
  # before (absolute, like the settings); one that isn't the same folder as the file's own setting is refused.
  if [ "$kind" != vm ]; then
    [ -z "${VAULT_DIR:-}" ] || x_vault=1
    [ -z "${EVENTS_SPOOL:-}" ] || x_spool=1
    if [ -n "${VAULT_DIR:-}" ]; then mc_canon VAULT_DIR "$VAULT_DIR" || return 1; VAULT_DIR="$MC_CANON"; fi
    if [ -n "${EVENTS_SPOOL:-}" ]; then mc_canon EVENTS_SPOOL "$EVENTS_SPOOL" || return 1; EVENTS_SPOOL="$MC_CANON"; fi
    VAULT_DIR="${VAULT_DIR:-}"; EVENTS_SPOOL="${EVENTS_SPOOL:-}"
    if [ -n "${MCOMP_VAULT_DIR:-}" ]; then
      mc_canon MCOMP_VAULT_DIR "$MCOMP_VAULT_DIR" || return 1
      [ "$x_vault" = 0 ] || [ "$VAULT_DIR" = "$MC_CANON" ] \
        || { MC_REFUSAL="MCOMP_VAULT_DIR ($MCOMP_VAULT_DIR) and VAULT_DIR in $(mc_tilde "$file") ($VAULT_DIR) name different vault folders; unset one"; return 1; }
      VAULT_DIR="$MC_CANON"
    fi
    if [ -n "${MCOMP_EVENTS_SPOOL:-}" ]; then
      mc_canon MCOMP_EVENTS_SPOOL "$MCOMP_EVENTS_SPOOL" || return 1
      [ "$x_spool" = 0 ] || [ "$EVENTS_SPOOL" = "$MC_CANON" ] \
        || { MC_REFUSAL="MCOMP_EVENTS_SPOOL ($MCOMP_EVENTS_SPOOL) and EVENTS_SPOOL in $(mc_tilde "$file") ($EVENTS_SPOOL) name different spools; unset one"; return 1; }
      EVENTS_SPOOL="$MC_CANON"
    fi
  fi
  # This machine's settings file: what `browser default` and the like write to (config/local.env when none exists).
  LOCAL_CONFIG="${file:-$MC_MAIN_CONFIG}"
  MC_SETTINGS_KIND="$kind"
  case "$kind" in
    vm) MC_SETTINGS_LABEL="config/vms/$VM_NAME.env" ;;
    local) MC_SETTINGS_LABEL="config/local.env" ;;
    config) MC_SETTINGS_LABEL="$(mc_tilde "$file")" ;;
    *) MC_SETTINGS_LABEL="the defaults; no config/local.env" ;;
  esac
  if [ -n "$MC_MISSING_CONFIG" ]; then
    LOCAL_CONFIG="$MC_MISSING_CONFIG"; MC_SETTINGS_LABEL="the defaults; MCOMP_CONFIG's $(mc_tilde "$MC_MISSING_CONFIG") doesn't exist"
  fi
}
# The vault folder in effect: VAULT_DIR, else the vault's own default (the account's, whatever $HOME says).
mc_vault_dir() { printf '%s' "${VAULT_DIR:-$MC_SYS_HOME/Library/Application Support/metacomputer}"; }

mc_sel_name="${MCOMP_VM_ARG:-${MCOMP_VM:-}}"
mc_load_settings "$mc_sel_name" "${MCOMP_CONFIG:-}" || { printf '%s\n' "metacomputer: $MC_REFUSAL" >&2; exit 1; }
# Everything this command starts (scripts, the hub, the Python helpers, metacomputer itself) gets the same settings:
# a VM chosen by name is passed on as its settings file.
if [ -n "$mc_sel_name" ]; then
  if [ "$LOCAL_CONFIG" = "$MC_MAIN_CONFIG" ] && [ ! -f "$MC_MAIN_CONFIG" ]; then
    export MCOMP_VM="$VM_NAME"; unset MCOMP_CONFIG
  else
    export MCOMP_CONFIG="$(mc_realpath "$LOCAL_CONFIG")"; unset MCOMP_VM
  fi
fi
unset MCOMP_VM_ARG mc_sel_name
GUEST_RUN_DIR="/home/$GUEST_USER/metacomputer-runs"

RUN_DIR="${RUN_DIR:-$LAB_ROOT/.run}"   # RUN_DIR / LOG_DIR: config/defaults.env
# Unix socket paths are limited to ~104 bytes, so the gateway's sockets live
# in a short private folder (per user, checkout and VM; the same from any
# shell, unlike $TMPDIR), not under a possibly deep checkout.
SOCK_DIR="/tmp/mcomp-$(id -u)-$(printf '%s' "$LAB_ROOT:$VM_NAME" | shasum | cut -c1-8)"
CACHE_DIR="$LAB_ROOT/.cache"
LOG_DIR="${LOG_DIR:-$LAB_ROOT/logs}"
NETGW_BIN="$LAB_ROOT/vendor/bin/cua-netgw"
QCOW2RAW_BIN="$LAB_ROOT/vendor/bin/qcow2raw"
KNOWN_HOSTS="$SSH_DIR/known_hosts"
export LUME_TELEMETRY_ENABLED=0
# The VM runner. mcomp-vm was called lume-secure, and a Mac set up before the
# rename may have only ~/.local/bin/lume-secure (the same build; it sees the
# same VMs). While LUME's runner isn't installed and that one is, use it and say
# so once per command. With neither, LUME stays as set and the usual errors apply.
LUME="${LUME:-$HOME/.local/bin/mcomp-vm}"
LUME_CONFIGURED="$LUME"   # as set, before the fallback below (`vm create` copies this one)
LUME_FORMER="$HOME/.local/bin/lume-secure"
if ! command -v "$LUME" >/dev/null 2>&1 && [ -x "$LUME_FORMER" ]; then
  [ -n "${MCOMP_LUME_NOTICE:-}" ] || printf '%s\n' "metacomputer: $LUME isn't installed, so this uses $LUME_FORMER, mcomp-vm's former name (./install.sh installs mcomp-vm)" >&2
  export MCOMP_LUME_NOTICE=1   # scripts this command runs don't repeat it
  LUME="$LUME_FORMER"
fi
# Python on the Mac: 3.11 or later (the benchmark reads TOML with tomllib; Apple's /usr/bin/python3 is 3.9,
# where parts of metacomputer fail). PY if it's set, else Homebrew's (MCOMP_BREW_PYTHON), else python3 on PATH:
# the first that's new enough. Checked once per command: scripts it runs inherit the answer.
py_ok() { # never runs Apple's /usr/bin/python3 stand-in without the Command Line Tools (it opens an install dialog)
  local p; [ -n "$1" ] && p="$(command -v "$1" 2>/dev/null)" || return 1
  case "$p" in /usr/bin/python3) xcode-select -p >/dev/null 2>&1 || return 1 ;; esac
  "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null
}
if [ -z "${PY:-}" ] || [ "${MCOMP_PY_CHECKED:-}" != "$PY" ]; then
  if [ -n "${PY:-}" ]; then
    py_ok "$PY" || { printf '%s\n' "metacomputer: PY=$PY isn't Python 3.11 or later" >&2; exit 1; }
  else
    for PY in "${MCOMP_BREW_PYTHON:-/opt/homebrew/bin/python3}" python3 ""; do py_ok "$PY" && break; done
    [ -z "$PY" ] || PY="$(command -v "$PY")"   # a full path, so scripts inheriting it don't depend on their PATH
    [ -n "$PY" ] || { printf '%s\n' "metacomputer: needs Python 3.11 or later on this Mac (python3 here is older, or missing). Install it with Homebrew: brew install python. Then try again." >&2; exit 1; }
  fi
  export PY MCOMP_PY_CHECKED="$PY"
fi

mkdir -p "$RUN_DIR" "$LOG_DIR"
export MCOMP_RUN_DIR="$RUN_DIR" MCOMP_LOG_DIR="$LOG_DIR" MCOMP_SSH_PORT="$SSH_PORT"   # for the Python helpers
( umask 077; mkdir -p "$SOCK_DIR" )
{ [ -d "$SOCK_DIR" ] && [ ! -L "$SOCK_DIR" ] && [ -O "$SOCK_DIR" ]; } || { echo "metacomputer: unsafe socket folder $SOCK_DIR" >&2; exit 1; }

c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_off=$'\033[0m'
[ -t 1 ] || { c_bold=; c_red=; c_grn=; c_yel=; c_off=; }
say()  { printf '%s\n' "${c_bold}metacomputer:${c_off} $*"; }
ok()   { printf '%s\n' "  ${c_grn}✓${c_off} $*"; }
warn() { printf '%s\n' "  ${c_yel}!${c_off} $*" >&2; }
die()  { printf '%s\n' "${c_red}metacomputer: $*${c_off}" >&2; exit 1; }

# --- which VM, before acting on one ---------------------------------------------------
# The VM names in Lume's list that are VMs of their own (not a baseline, its previous copy or a checkpoint);
# status 1 when the list can't be had or read. EXCEPT is left out. Space-separated.
vm_working_names() { # [EXCEPT]
  local out
  out="$("$LUME" ls --format json 2>/dev/null)" || return 1
  printf '%s' "$out" | "$PY" -c 'import json, re, sys
try:
    names = [v["name"] for v in json.load(sys.stdin)]
except Exception:
    sys.exit(1)
print(" ".join(sorted(n for n in names if isinstance(n, str) and n != sys.argv[1]
                      and not re.search(r"(-baseline(-prev)?|-ckpt-.*)$", n))))' "${1:-}"
}
# With no settings file and no VM named (the defaults, unasked for), go ahead only when that can't be the wrong
# VM: no config/vms/ files to choose from, and no VM in the runner but the default one (or no runner yet). Then
# the choice is pinned (MCOMP_VM) for everything this command starts. Otherwise refuse, saying how to choose.
vm_target_check() {
  [ "${MC_IMPLICIT:-0}" = 1 ] || return 0
  local files names
  [ -z "${MC_MISSING_CONFIG:-}" ] || die "MCOMP_CONFIG names $MC_MISSING_CONFIG, which doesn't exist; nothing was done"
  files="$(mc_vm_files | tr '\n' ' ')"
  [ -z "$files" ] || die "this checkout has no config/local.env, and config/vms/ has settings for: ${files% }. Say which VM: metacomputer --vm NAME ... (or MCOMP_VM=NAME); nothing was done"
  if command -v "$LUME" >/dev/null 2>&1; then
    names="$(vm_working_names "$VM_NAME")" || die "couldn't list the VMs with $LUME, so it can't tell which VM a checkout without config/local.env means; nothing was done. Say which: metacomputer --vm NAME ..."
    [ -z "$names" ] || die "this checkout has no settings of its own (config/local.env) and the runner has VMs besides $VM_NAME ($names), so a bare metacomputer command could act on the wrong one; nothing was done. Say which: metacomputer --vm NAME ... (metacomputer vm list), or write config/local.env"
  fi
  export MCOMP_VM="$VM_NAME"; MC_IMPLICIT=0
}
# The command to type for this VM, in hints: `metacomputer --vm NAME` for a VM in config/vms/.
mc_cmd() { if [ "${MC_SETTINGS_KIND:-}" = vm ]; then printf 'metacomputer --vm %s' "$VM_NAME"; else printf 'metacomputer'; fi; }
# One line on stderr, once per command, before a command acts on a VM: which VM and settings; "vault" adds the
# vault folder. Scripts and commands it starts don't repeat it.
vm_notice() { # [vault]
  [ -z "${MCOMP_VM_NOTICE:-}" ] || return 0
  export MCOMP_VM_NOTICE=1
  if [ "${1:-}" = vault ]; then
    printf '%s\n' "metacomputer: VM $VM_NAME ($MC_SETTINGS_LABEL), vault $(mc_tilde "$(mc_vault_dir)")" >&2
  else
    printf '%s\n' "metacomputer: VM $VM_NAME ($MC_SETTINGS_LABEL)" >&2
  fi
}

# --- Lume -------------------------------------------------------------------
lume_json() { "$LUME" get "$1" --format json 2>/dev/null; }
lume_field() { # NAME FIELD
  lume_json "$1" | "$PY" -c 'import json,sys
d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d
v=d.get(sys.argv[1]); print("" if v is None else v)' "$2" 2>/dev/null
}
vm_exists() { "$LUME" ls --format json 2>/dev/null | "$PY" -c 'import json,sys
names=[v.get("name") for v in json.load(sys.stdin)]
sys.exit(0 if sys.argv[1] in names else 1)' "$1"; }
# NAME -> "present" or "absent"; status 1 (nothing printed) when Lume's list can't be had or read. Unlike
# vm_exists, an error never looks like "absent": the build relies on this before it deletes or creates.
vm_lookup() {
  local out
  out="$("$LUME" ls --format json 2>/dev/null)" || return 1
  printf '%s' "$out" | "$PY" -c 'import json, sys
try:
    names = {v["name"] for v in json.load(sys.stdin)}
except Exception:
    sys.exit(1)
print("present" if sys.argv[1] in names else "absent")' "$1"
}
# NAME FIELD -> FIELD of `lume get NAME`, which must succeed and describe exactly NAME; status 1 otherwise.
vm_info() {
  local out
  out="$("$LUME" get "$1" --format json 2>/dev/null)" || return 1
  printf '%s' "$out" | "$PY" -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
d = d if isinstance(d, list) else [d]
if len(d) != 1 or not isinstance(d[0], dict) or d[0].get("name") != sys.argv[1]:
    sys.exit(1)
v = d[0].get(sys.argv[2])
if v is None or v == "":
    sys.exit(1)
print(v)' "$1" "$2"
}
vm_status() { lume_field "$1" status; }
vm_running() { [ "$(vm_status "$1")" = "running" ]; }
vm_mac() { lume_field "$1" macAddress; }
# NAME -> the VM's folder, from Lume itself: the storage location `lume get` names (locationName), looked up
# in `lume config get` by its exact name, listed exactly once, and an existing absolute folder (canonical).
# Nothing is guessed: any doubt is status 1, with the reason on stderr.
vm_dir() {
  local name="$1" loc cfg path home n
  loc="$(vm_info "$name" locationName)" || { echo "Lume didn't say which storage location holds $name" >&2; return 1; }
  cfg="$("$LUME" config get 2>/dev/null)" || { echo "'$LUME config get' failed" >&2; return 1; }
  path="$(printf '%s\n' "$cfg" | MCOMP_LOC="$loc" awk '
    /^Configured VM storage locations:/ { inlist = 1; next }
    inlist && /^[[:space:]]*- / {
      line = $0; sub(/^[[:space:]]*- /, "", line); i = index(line, ": ")
      if (i > 0 && substr(line, 1, i - 1) == ENVIRON["MCOMP_LOC"]) { p = substr(line, i + 2); sub(/ \(default\)$/, "", p); print p }
      next }
    inlist && NF == 0 { inlist = 0 }')"
  n="$(printf '%s' "$path" | grep -c '')"
  [ "$n" = 1 ] || { echo "Lume's settings list the storage location '$loc' $n times (expected once)" >&2; return 1; }
  # Lume expands ~ to the account's home folder, whatever $HOME says.
  home="$("$PY" -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')" || return 1
  case "$path" in "~") path="$home" ;; "~/"*) path="$home/${path#\~/}" ;; esac
  case "$path" in /*) ;; *) echo "the storage location '$loc' isn't an absolute folder: $path" >&2; return 1 ;; esac
  path="$(cd "$path" 2>/dev/null && pwd -P)" || { echo "the storage location '$loc' isn't a folder" >&2; return 1; }
  printf '%s/%s\n' "$path" "$name"
}
# DIR NAME: DIR is NAME's own folder: a real folder of yours (not a link) holding a disk.img and a config.json
# of yours (not links), whose MAC address is the one Lume reports for NAME (random for each VM).
vm_dir_is() {
  local dir="$1" name="$2" mac f
  [ -d "$dir" ] && [ ! -L "$dir" ] && [ -O "$dir" ] || return 1
  for f in disk.img config.json; do [ -f "$dir/$f" ] && [ ! -L "$dir/$f" ] && [ -O "$dir/$f" ] || return 1; done
  mac="$(vm_info "$name" macAddress)" || return 1
  "$PY" -c 'import json, sys
sys.exit(0 if str(json.load(open(sys.argv[1])).get("macAddress", "")).lower() == sys.argv[2].lower() else 1)' "$dir/config.json" "$mac" 2>/dev/null
}

# --- locks ------------------------------------------------------------------------
# An exclusive flock on a file in a private per-user folder, taken on a file descriptor this shell keeps
# open (7 for a VM's lock, 8 for the install lock). Every process this shell starts inherits that
# descriptor, so the lock lasts as long as the last of them (killing the shell doesn't release it while its
# children work), and the kernel drops it once they've all exited: no stale lock survives. daemonize()
# closes inherited descriptors, so a long-lived daemon never keeps a lock. A process that inherited a held
# lock's descriptor holds that lock already. perl (part of macOS) takes it. The folder doesn't depend on
# $HOME, because Lume keeps VMs in the account's home whatever $HOME says; MCOMP_LOCK_DIR moves it (tests).
mc_lock_dir() {
  local d="${MCOMP_LOCK_DIR:-}"
  if [ -z "$d" ]; then
    d="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)"; d="${d%/}"; [ -n "$d" ] || d="/tmp/mcomp-$(id -u)"
    d="$d/metacomputer-locks"
  fi
  ( umask 077; mkdir -p "$d" ) 2>/dev/null
  [ -d "$d" ] && [ ! -L "$d" ] && [ -O "$d" ] && chmod 700 "$d" 2>/dev/null || return 1
  printf '%s' "$d"
}
# FD FILE: 0 when FD is already open on FILE and holds its lock (inherited from the process that took it).
lock_fd_held() {
  /usr/bin/perl -e 'use Fcntl qw(:flock); my ($fd, $file) = @ARGV;
    open(my $fh, ">>&=", $fd) or exit 1; my @a = stat($fh); my @b = stat($file);
    exit 1 unless @a && @b && $a[0] == $b[0] && $a[1] == $b[1];
    exit(flock($fh, LOCK_EX | LOCK_NB) ? 0 : 1)' "$1" "$2" 2>/dev/null
}
# FD WAIT: take the lock on the file FD is open on, waiting up to WAIT seconds; 0 = taken.
lock_fd_take() {
  /usr/bin/perl -e 'use Fcntl qw(:flock); my ($fd, $wait) = @ARGV;
    open(my $fh, ">>&=", $fd) or exit 2; my $end = time + $wait;
    until (flock($fh, LOCK_EX | LOCK_NB)) { exit 1 if time >= $end; select(undef, undef, undef, 0.2); }
    exit 0' "$1" "$2" 2>/dev/null
}
lock_hold() { # NAME WAIT_SECONDS WHAT [FD] -> 0 held; 1 another process holds it (LOCK_OWNER says which); 2 no lock folder
  local name="$1" wait="$2" what="$3" fd="${4:-7}" dir file
  LOCK_OWNER=""
  case "$fd" in 7|8|9) ;; *) return 2 ;; esac   # 9: vm create's short lock on choosing ports and writing settings
  dir="$(mc_lock_dir)" || return 2
  file="$dir/$name.lock"
  lock_fd_held "$fd" "$file" && return 0
  eval "exec $fd>>\"\$file\"" || return 2
  if ! lock_fd_take "$fd" "$wait"; then
    eval "exec $fd>&-"
    LOCK_OWNER="$(sed -n 's/^what=//p' "$dir/$name.owner" 2>/dev/null), pid $(sed -n 's/^pid=//p' "$dir/$name.owner" 2>/dev/null), since $(sed -n 's/^since=//p' "$dir/$name.owner" 2>/dev/null)"
    return 1
  fi
  printf 'pid=%s\nsince=%s\nwhat=%s\n' "$$" "$(date '+%H:%M:%S')" "$what" > "$dir/$name.owner"
}
vm_lock_name() { # VM NAME -> its lock's name
  case "$1" in ''|*[!A-Za-z0-9._-]*) printf 'vm-%s' "$(printf '%s' "$1" | shasum | cut -c1-16)" ;; *) printf 'vm-%s' "$1" ;; esac
}
# The VM lock for a command that deletes, replaces, restores or snapshots VM_NAME, its baseline or its
# checkpoints: the same lock as a build's, so none of them can run during a build or each other.
vm_lock_or_die() { # WHAT
  local rc=0
  lock_hold "$(vm_lock_name "$VM_NAME")" "${MCOMP_LOCK_WAIT:-10}" "$1" || rc=$?
  case "$rc" in
    0) ;;
    1) die "another build or change of the VM $VM_NAME is running ($LOCK_OWNER); nothing was changed. Run this again when it's done" ;;
    *) die "couldn't make the private lock folder (MCOMP_LOCK_DIR, or the per-user temporary folder)" ;;
  esac
}

# --- detached processes (own session, so they outlive the calling shell) ----
daemonize() { # LOGFILE CMD ARGS...: in its own session, with no descriptors but 0-2 (so it never keeps a lock)
  local log="$1"; shift
  "$PY" -c 'import os,sys
os.setsid()
fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o644)
os.dup2(fd,1); os.dup2(fd,2)
n=os.open("/dev/null", os.O_RDONLY); os.dup2(n,0)
os.closerange(3, 4096)
os.execvp(sys.argv[2], sys.argv[2:])' "$log" "$@" &
}

# --- cua-netgw (host-enforced egress policy, loopback-only forwards) -------
netgw_pid() { if [ -s "$RUN_DIR/netgw.pid" ]; then cat "$RUN_DIR/netgw.pid"; fi; return 0; }
netgw_alive() { local p; p="$(netgw_pid)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
netgw_stop() {
  local p; p="$(netgw_pid)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    kill "$p" 2>/dev/null
    local i=0; while kill -0 "$p" 2>/dev/null && [ $i -lt 10 ]; do sleep 0.5; i=$((i+1)); done
    kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
  fi
  rm -f "$RUN_DIR/netgw.pid" "$SOCK_DIR/net.sock" "$SOCK_DIR/control.sock" "$SOCK_DIR"/lume-*.sock
}
netgw_render() { # MAC -> writes $RUN_DIR/gw.json
  local mac="$1"
  cat > "$RUN_DIR/gw.json" <<EOF
{
  "subnet": "$NET_SUBNET",
  "gateway_ip": "$NET_GATEWAY_IP",
  "gateway_mac": "$NET_GATEWAY_MAC",
  "dhcp": true,
  "static_leases": { "$NET_GUEST_IP": "$mac" },
  "dns_upstream": [ $NET_DNS_UPSTREAM ],
  "forwards": [
    { "host": "127.0.0.1:$SSH_PORT", "guest": "$NET_GUEST_IP:22" },
    { "host": "127.0.0.1:$VNC_PORT", "guest": "$NET_GUEST_IP:5900" }
  ],
  "control": "unix://$SOCK_DIR/control.sock",
  "listen": { "vfkit": "unixgram://$SOCK_DIR/net.sock" },
  "rules": "$LAB_ROOT/config/netgw-rules.json",
  "events": "$LOG_DIR/netgw-blocked.jsonl",
  "pid_file": "$RUN_DIR/netgw.pid",
  "log_level": "warning"
}
EOF
}
netgw_start() { # MAC
  [ -x "$NETGW_BIN" ] || die "missing $NETGW_BIN (run scripts/build-tools.sh)"
  netgw_stop
  netgw_render "$1"
  "$NETGW_BIN" -check-rules "$LAB_ROOT/config/netgw-rules.json" >/dev/null || die "bad netgw rules"
  daemonize "$LOG_DIR/netgw.log" "$NETGW_BIN" -config "$RUN_DIR/gw.json"
  local i=0; while [ ! -S "$SOCK_DIR/net.sock" ] && [ $i -lt 40 ]; do sleep 0.25; i=$((i+1)); done
  [ -S "$SOCK_DIR/net.sock" ] || { tail -5 "$LOG_DIR/netgw.log" >&2; die "cua-netgw did not start"; }
}
netgw_control() { # "lease MAC" | "probe IP:PORT"
  "$PY" - "$SOCK_DIR/control.sock" "$1" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(12)
try:
    s.connect(sys.argv[1]); s.sendall((sys.argv[2] + "\n").encode())
    print(s.recv(256).decode().strip())
except Exception as e:
    print("no " + str(e))
EOF
}

# --- VM run/stop ------------------------------------------------------------
# Host-sharing devices: Lume attaches the SPICE clipboard port, a sound device
# (host speakers + microphone) and the Rosetta share to every VM. A Lume with
# the opt-out flags (mcomp-vm, or a Lume that adopted them) leaves out each
# one whose SHARE_* setting isn't "on"; any other Lume always shares them.
host_sharing_args() {
  if "$LUME" run --help 2>&1 | grep -q -- "--no-host-sharing"; then
    local a=""
    [ "${SHARE_CLIPBOARD:-off}" = on ] || a="$a --no-share-clipboard"
    [ "${SHARE_AUDIO:-off}" = on ] || a="$a --no-share-audio"
    [ "${SHARE_ROSETTA:-off}" = on ] || a="$a --no-share-rosetta"
    printf '%s' "$a"
  elif [ "${SHARE_CLIPBOARD:-off}${SHARE_AUDIO:-off}${SHARE_ROSETTA:-off}" != ononon ]; then
    warn "$LUME always shares your clipboard, audio and Rosetta with the VM; mcomp-vm can turn them off (README: Isolation)"
  fi
}

STORAGE_RUN_ARGS=()   # extra `lume run` arguments for this VM's storage (bin/metacomputer storage_prepare)
vm_start() { # NAME [seed.iso]
  local name="$1" seed="${2:-}" mac
  vm_running "$name" && { ok "$name already running"; return 0; }
  mac="$(vm_mac "$name")"; [ -n "$mac" ] || die "cannot read $name's MAC address from lume"
  netgw_start "$mac"
  local args="run $name --display none --vnc disabled --network gateway:$SOCK_DIR/net.sock"
  [ -n "$seed" ] && args="$args --mount $seed"
  args="$args$(host_sharing_args)"
  # shellcheck disable=SC2086
  daemonize "$LOG_DIR/lume-$name.log" "$LUME" $args ${STORAGE_RUN_ARGS[@]+"${STORAGE_RUN_ARGS[@]}"}
  # The run process is the VM (Virtualization.framework runs in it): storage locks name it as their holder.
  echo "$!" > "$RUN_DIR/lume.pid"
  local i=0; while ! vm_running "$name" && [ $i -lt 60 ]; do sleep 0.5; i=$((i+1)); done
  vm_running "$name" || { tail -5 "$LOG_DIR/lume-$name.log" >&2; netgw_stop; die "VM $name did not start"; }
}
vm_stop() { # NAME [--force]
  # `lume stop` on a detached run is an immediate power-off (Lume SIGTERMs the
  # run process, which calls VZVirtualMachine.stop): recent writes in the guest
  # are lost. So shut the guest down from inside first (sync + systemd
  # poweroff via the admin verb) and only fall back to Lume's stop after 90 s.
  # The 90 s run from the start, whatever the guest does with the request: the
  # SSH command runs in the background, so a guest that keeps it open without
  # ever powering off can't hold the Mac's side up.
  local name="$1" i=0 sp deadline="${MCOMP_STOP_DEADLINE:-90}"
  if vm_running "$name"; then
    if [ "${2:-}" != "--force" ]; then
      gssh admin 'sync; sudo -n /usr/local/sbin/metacomputer-admin poweroff' >/dev/null 2>&1 &
      sp=$!
      while vm_running "$name" && [ $i -lt "$deadline" ]; do sleep 1; i=$((i+1)); done
      kill "$sp" 2>/dev/null || true; wait "$sp" 2>/dev/null || true   # the helper may be gone or fail (set -E callers)
      vm_running "$name" && warn "guest did not power off within $deadline s; forcing"
    fi
    vm_running "$name" && vm_force_stop "$name"
  fi
  i=0; while vm_running "$name" && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  netgw_stop
}
vm_force_stop() { # NAME: Lume's immediate power-off, itself bounded (30 s)
  local name="$1" i=0 sp
  "$LUME" stop "$name" --force >/dev/null 2>&1 &
  sp=$!
  while kill -0 "$sp" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  kill "$sp" 2>/dev/null || true; wait "$sp" 2>/dev/null || true
  return 0
}
vm_kill() { # NAME: power the VM off from the Mac now, asking the guest nothing (storage-security failures)
  # The VM's own `lume run` process (checked to be this VM's) gets SIGTERM, then SIGKILL after 10 s
  # (lib/storage.py kill-vm); then Lume's stop as a fallback. Every step has a host deadline.
  local name="$1" i=0
  ( export_options; MCOMP_LAB_ROOT="$LAB_ROOT" "$PY" "$LAB_ROOT/lib/storage.py" kill-vm --grace 10 ) >/dev/null 2>&1 || true
  vm_running "$name" && vm_force_stop "$name"
  while vm_running "$name" && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  netgw_stop
}

# --- SSH --------------------------------------------------------------------
# The optional hardening settings (SECURITY.md), resolved for the Python helpers.
export_options() {
  export MCOMP_ACTION_LOG="$ACTION_LOG" MCOMP_ACTION_LOG_TEXT="$ACTION_LOG_TEXT" MCOMP_ACTION_LOG_DIR="$LOG_DIR/actions" \
         MCOMP_ACTION_LOG_KEY="${ACTION_LOG_KEY:-$SSH_DIR/action-log}" MCOMP_SSH_DIR="$SSH_DIR" MCOMP_VM_NAME="$VM_NAME" \
         MCOMP_SSH_AUTH="$SSH_AUTH" MCOMP_SSH_CERT_TTL="$SSH_CERT_TTL" MCOMP_DRIVER_MODE="$DRIVER_MODE" MCOMP_DRIVER_PROFILE="$DRIVER_PROFILE" \
         MCOMP_DRIVER_MANIFEST="$DRIVER_MANIFEST" MCOMP_DRIVER_ORIGINS="$DRIVER_ORIGINS" MCOMP_DRIVER_EXPIRES="$DRIVER_EXPIRES" \
         MCOMP_DRIVER_IDLE="$DRIVER_IDLE" MCOMP_BROWSERS="$BROWSERS" MCOMP_GUEST_USER="$GUEST_USER" \
         MCOMP_VM_SHELL="${VM_SHELL:-auto}" MCOMP_SHELL_LOG="${SHELL_LOG:-auto}" \
         MCOMP_STORAGE_DIR="${STORAGE_DIR:-}" MCOMP_VOLUMES="${VOLUMES:-}" MCOMP_REFDISKS="${REFDISKS:-}" \
         MCOMP_VM_EVENTS_SPOOL="${EVENTS_SPOOL:-}"   # this VM's spool (empty: lib/events.py's default)
  # The hub (lib/mcp_hub.py), its coordinate guard and its hidden-text marks. MCOMP_MCP_HUB / MCOMP_COORD_GUARD /
  # MCOMP_HIDDEN_TEXT already in the environment win over the config (the benchmark's switches).
  export MCOMP_MCP_HUB="${MCOMP_MCP_HUB:-${MCP_HUB:-on}}" MCOMP_COORD_GUARD="${MCOMP_COORD_GUARD:-${COORD_GUARD:-on}}" \
         MCOMP_HIDDEN_TEXT="${MCOMP_HIDDEN_TEXT:-${HIDDEN_TEXT:-on}}" MCOMP_HUB_IDLE="${HUB_IDLE:-10m}"
  # Approved lessons (metacomputer learn, lib/learn_store.py): the environment wins here too (the benchmark's
  # lessons switch, an isolated store for tests).
  export MCOMP_LESSONS="${MCOMP_LESSONS:-${LESSONS:-on}}" MCOMP_LESSONS_DIR="${MCOMP_LESSONS_DIR:-${LESSONS_DIR:-}}"
}

# --- the optional event stream (docs/events.md; off until a sink is added) ------
# Best effort: it never fails the command, and waits at most about 3 s per sink.
emit_event() { # FAMILY TYPE SEVERITY MESSAGE ACTION [KEY=VALUE ...] (ACTION may be "")
  local fam="$1" typ="$2" sev="$3" msg="$4" act="$5" kv f=()
  shift 5
  for kv in "$@"; do f+=(--field "$kv"); done
  ( export_options
    "$PY" "$LAB_ROOT/lib/events.py" emit "$fam" "$typ" --severity "$sev" --message "$msg" \
      ${act:+--action "$act"} ${f[@]+"${f[@]}"} --quiet ) >/dev/null 2>&1 || true
}
# EVENTS HOOK (secrets): `metacomputer secrets` exits 3 when the vault isn't unlocked (or
# doesn't exist yet). bin/metacomputer calls this from an EXIT trap for that command (see
# there). Quiet when you ran it yourself at a terminal, and at most once in 5 minutes.
events_hook_secrets_exit() { # RC
  [ "${1:-0}" = 3 ] || return 0
  [ -t 0 ] && return 0
  ( export_options
    "$PY" "$LAB_ROOT/lib/events.py" emit needs_you vault_locked --severity warn --debounce 300 --quiet \
      --message "The secrets vault isn't unlocked (or doesn't exist yet), so Claude can't fill a login" \
      --action "In your own terminal: metacomputer secrets unlock (or secrets init, if there's no vault yet)" \
      --field exit=3 ) >/dev/null 2>&1 || true
}

# --- SSH_AUTH=cert: a fresh key and a short-lived certificate per connection ---
CA_KEY="${SSH_CA_KEY:-$SSH_DIR/ca}"
ca_pub() { case "$CA_KEY" in *.pub) echo "$CA_KEY" ;; *) echo "$CA_KEY.pub" ;; esac; }
ensure_ca() { # the default CA is a key file made on first use; a .pub means a key you keep in ssh-agent
  case "$CA_KEY" in *.pub) [ -f "$CA_KEY" ] || die "SSH_CA_KEY $CA_KEY not found" ; return 0 ;; esac
  [ -f "$CA_KEY" ] && return 0
  ( umask 077; mkdir -p "$(dirname "$CA_KEY")"; ssh-keygen -q -t ed25519 -N '' -C metacomputer-ca -f "$CA_KEY" ) \
    || die "could not create the SSH CA key $CA_KEY"
}
CERT_ops=""; CERT_ops_at=0; CERT_mcp=""; CERT_mcp_at=0
cert_ttl_s() { local t="${SSH_CERT_TTL:-5m}"; case "$t" in *m) echo $(( ${t%m} * 60 )) ;; *h) echo $(( ${t%h} * 3600 )) ;; *s) echo "${t%s}" ;; *) echo 300 ;; esac; }
cert_for() { # ROLE -> sets CERT_KEY (KEY-cert.pub beside it); reused for half its lifetime in this process
  local role="$1" now dir key id agent=() opts=() cached at
  now="$(date +%s)"; eval "cached=\$CERT_$role; at=\$CERT_${role}_at"
  if [ -n "$cached" ] && [ -f "$cached-cert.pub" ] && [ $(( now - at )) -lt $(( $(cert_ttl_s) / 2 )) ]; then CERT_KEY="$cached"; return 0; fi
  ensure_ca
  dir="$SOCK_DIR/certs"; ( umask 077; mkdir -p "$dir" )
  find "$dir" -type f -mmin +60 -delete 2>/dev/null
  key="$(mktemp "$dir/$role.XXXXXX")" && rm -f "$key"
  ssh-keygen -q -t ed25519 -N '' -C "mcomp-$role" -f "$key" || return 1
  # The key ID appears in the VM's auth log: who asked, from which project, which process.
  id="mcomp:$role:$(id -un):$(basename "$PWD" | tr -cd 'A-Za-z0-9._-' | cut -c1-40):pid$$"
  case "$CA_KEY" in *.pub) agent=(-U) ;; esac
  opts=(-O clear -O source-address="$NET_GATEWAY_IP/32")
  case "$role" in
    mcp) opts+=(-O force-command=/opt/metacomputer/bin/driver-mcp) ;;
    ops) opts+=(-O permit-pty) ;;
  esac
  ssh-keygen -q -s "$CA_KEY" ${agent[@]+"${agent[@]}"} -I "$id" -n "$role" -V "-2m:+${SSH_CERT_TTL:-5m}" \
    -z "$now$(printf '%05d' "$RANDOM")" "${opts[@]}" "$key.pub" 2>/dev/null || { rm -f "$key" "$key.pub"; return 1; }
  rm -f "$key.pub"
  eval "CERT_$role=\$key; CERT_${role}_at=\$now"
  CERT_KEY="$key"
}

ssh_base() { # ROLE(admin|ops|mcp) -> prints nothing; sets SSH_ARGS array
  local role="$1" user
  case "$role" in admin) user="$ADMIN_USER" ;; ops|mcp) user="$GUEST_USER" ;; *) die "bad ssh role $role" ;; esac
  local ids=(-o IdentityFile="$SSH_DIR/$role")
  if [ "$role" != admin ]; then
    if [ "${SSH_AUTH:-key}" = cert ]; then
      # The certificate first; the static key stays as a fallback for a VM that
      # hasn't switched yet (start switches it). A switched VM refuses static keys.
      cert_for "$role" || die "could not make an SSH certificate for $role (SSH_CA_KEY: $CA_KEY)"
      ids=(-o IdentityFile="$CERT_KEY" -o CertificateFile="$CERT_KEY-cert.pub" -o IdentityFile="$SSH_DIR/$role")
    elif [ -f "$RUN_DIR/guest-options-$VM_NAME" ] && [ -f "$(ca_pub)" ] && cert_for "$role" 2>/dev/null; then
      # Back on static keys in the config, but the VM may still want a certificate
      # until start (or security apply) switches it back.
      ids+=(-o IdentityFile="$CERT_KEY" -o CertificateFile="$CERT_KEY-cert.pub")
    fi
  fi
  # The VM is untrusted. No ssh config file applies (-F /dev/null: no Host blocks from
  # ~/.ssh/config or /etc/ssh, so no ProxyCommand, forwards or multiplexing from there),
  # and nothing of the Mac's is forwarded to it: no agent, no X11, no ports, no tunnel.
  SSH_ARGS=(-F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes "${ids[@]}"
            -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null
            -o UpdateHostKeys=no -o ForwardAgent=no -o ForwardX11=no -o ForwardX11Trusted=no
            -o ClearAllForwardings=yes -o Tunnel=no -o ControlMaster=no -o ControlPath=none
            -o PermitLocalCommand=no -o ProxyCommand=none
            -o ConnectTimeout=5 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o LogLevel=ERROR
            -p "$SSH_PORT" "$user@127.0.0.1")
}
gssh() { # ROLE CMD...  (non-interactive)
  local role="$1"; shift
  ssh_base "$role"
  if [ -t 2 ]; then
    # The remote side writes this stderr, and the VM is untrusted: on a terminal it's shown escaped (and at
    # most 1 MB of it), never raw (lib/termsafe.py). Pipes and files get it exactly; `metacomputer ssh` is raw.
    ssh -T "${SSH_ARGS[@]}" "$@" 2> >("$PY" "$LAB_ROOT/lib/termsafe.py" --max 1048576 >&2)
  else
    ssh -T "${SSH_ARGS[@]}" "$@"
  fi
}
wait_ssh() { # ROLE TIMEOUT_S
  local role="$1" t="${2:-300}" start; start=$(date +%s)
  while :; do
    gssh "$role" true 2>/dev/null && return 0
    [ $(( $(date +%s) - start )) -ge "$t" ] && return 1
    sleep 3
  done
}
wait_session() { # TIMEOUT_S
  local t="${1:-240}" start; start=$(date +%s)
  while :; do
    gssh ops 'test -s /run/user/$(id -u)/metacomputer/session-ready' 2>/dev/null && return 0
    [ $(( $(date +%s) - start )) -ge "$t" ] && return 1
    sleep 3
  done
}

# --- DRIVER_MODE=bounded: upload the manifest, ask for it by hash -------------
driver_request() { # prints the SSH command line for the MCP role ("" = Cua's default)
  [ "${DRIVER_MODE:-standard}" = bounded ] || return 0
  local m sha
  m="$(export_options; "$PY" "$LAB_ROOT/lib/driver_profiles.py" render)" || exit 1
  sha="$(printf '%s\n' "$m" | shasum -a 256 | cut -d' ' -f1)"
  printf '%s\n' "$m" | gssh ops "d=~/.config/metacomputer/manifests; f=\$d/$sha.yaml; [ -f \$f ] || { mkdir -p \$d && cat > \$f.tmp && mv \$f.tmp \$f; }" \
    || die "could not upload the driver manifest to the VM"
  echo "bounded $sha"
}

# --- keep the VM's side of SSH_AUTH and DRIVER_MODE in step with the config ----
guest_option() { # ssh-auth|driver-policy -> the VM's current setting
  gssh admin "sudo /usr/local/sbin/metacomputer-admin $1 status" 2>/dev/null | tail -1
}
sync_guest_options() { # [all]: quiet unless something changes; skips the VM in the default setup unless "all"
  local mark="$RUN_DIR/guest-options-$VM_NAME" have all="${1:-}"
  if [ "${SSH_AUTH:-key}" = cert ] || [ -f "$mark" ] || [ "$all" = all ]; then
    have="$(guest_option ssh-auth)"
    case "$have" in cert|key) ;; *) warn "this VM can't switch SSH_AUTH (built by an earlier version?); rebuild it to use certificates"; have="${SSH_AUTH:-key}" ;; esac
    if [ "$have" != "${SSH_AUTH:-key}" ]; then
      if [ "${SSH_AUTH:-key}" = cert ]; then
        ensure_ca
        gssh admin "sudo /usr/local/sbin/metacomputer-admin ssh-auth cert" < "$(ca_pub)" >/dev/null \
          && ok "SSH: the VM now accepts only short-lived certificates for ops and mcp (SSH_AUTH=cert)" \
          || die "could not switch the VM to SSH certificates (metacomputer logs)"
      else
        gssh admin "sudo /usr/local/sbin/metacomputer-admin ssh-auth key" >/dev/null \
          && ok "SSH: the VM is back on its static keys (SSH_AUTH=key)" || warn "could not switch the VM back to static keys"
      fi
    fi
  fi
  if [ "${DRIVER_MODE:-standard}" = bounded ] || [ -f "$mark" ] || [ "$all" = all ]; then
    have="$(guest_option driver-policy)"
    case "$have" in bounded|standard) ;; *) warn "this VM can't hold a driver policy (built by an earlier version?); bounded sessions still work, but standard ones aren't refused"; have="${DRIVER_MODE:-standard}" ;; esac
    if [ "$have" != "${DRIVER_MODE:-standard}" ]; then
      gssh admin "sudo /usr/local/sbin/metacomputer-admin driver-policy ${DRIVER_MODE:-standard}" >/dev/null \
        && ok "driver: the VM now starts Cua Driver only in ${DRIVER_MODE:-standard} mode" || warn "could not set the VM's driver policy"
    fi
  fi
  if [ "${SSH_AUTH:-key}" = cert ] || [ "${DRIVER_MODE:-standard}" = bounded ]; then : > "$mark"; else rm -f "$mark"; fi
}
