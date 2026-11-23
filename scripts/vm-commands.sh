#!/bin/bash
# `metacomputer vm list | create | delete`, `list` and `default`: metacomputer's VMs on this Mac, a VM of its own in
# one command, deleting one safely, and the default computer. Sourced by bin/metacomputer (its functions and the
# settings scripts/lib.sh loaded are in scope); macOS /bin/bash 3.2. Settings: one file per VM, config/vms/NAME.env,
# and config/default-vm naming the default (scripts/lib.sh, "which VM").
if [ "${BASH_SOURCE[0]}" = "$0" ]; then    # run on its own: it's sourced, and does nothing (exit 2; -h: this line)
  case "$*" in -h|--help) echo "scripts/vm-commands.sh: sourced by bin/metacomputer (metacomputer vm ...), never run"; exit 0 ;; esac
  echo "scripts/vm-commands.sh: sourced by bin/metacomputer (metacomputer vm ...); nothing was run" >&2; exit 2
fi

# Settings that are the Mac's choices rather than one VM's: `vm create` copies the ones these settings set
# differently from config/defaults.env into the new VM's file (it doesn't read config/local.env later on).
# Never copied: what makes a VM itself (names, ports, keys, folders, vault), its storage (VOLUMES, REFDISKS: a
# volume is read-write in one VM at a time), its live-view password store (a password command would be shared),
# recordings and the skill's defaults.
VM_COPY_KEYS="LUME VM_CPU VM_MEMORY VM_DISK_BYTES VM_DISPLAY BROWSERS EXTRA_PACKAGES BROWSER_EXTENSIONS VIRTUAL_AUDIO
SHARE_CLIPBOARD SHARE_AUDIO SHARE_ROSETTA STORAGE_DIR ACTION_LOG ACTION_LOG_TEXT ACTION_LOG_KEY SSH_AUTH SSH_CERT_TTL
SSH_CA_KEY DRIVER_MODE DRIVER_PROFILE DRIVER_ORIGINS DRIVER_MANIFEST DRIVER_EXPIRES DRIVER_IDLE VM_SHELL SHELL_LOG
MCP_HUB COORD_GUARD VIEW_REMEMBER INTERACTIVE_PROMPTS OCI_PYTHON"

# A value option is given once: a second one is a usage error (nothing a command doesn't use is let through).
vc_once() { # OPTION: dies when OPTION was given already in this command (VC_SEEN)
  case " ${VC_SEEN:-} " in *" $1 "*) die "${1%%=*} given twice (one value, please)" ;; esac
  VC_SEEN="${VC_SEEN:-} $1"
}
do_vm() { # list [--on PLACE] [--json] | create NAME ... | delete NAME ...
  local sub="${1:-list}"; [ $# -gt 0 ] && shift
  case "$sub" in
    list)   vm_list "$@" ;;
    create) vm_create "$@" ;;
    delete) vm_delete "$@" ;;
    help|-h|--help) sed -n '/^#   --vm NAME/,/^#   start /p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//' ;;
    *) die "usage: metacomputer vm list [--on PLACE] [--json] | create NAME [--ssh-port P --vnc-port P] [--no-build] [--yes] | create NAME --on server --host ADDRESS --user ACCOUNT [--port P] | create NAME --on oci|hetzner|digitalocean|vultr [--size SIZE] [--region REGION] [--egress internet|off] | create NAME --on sprites --org ORG [--sprite SPRITE] [--egress internet|off] | delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes] [--checked-provider]" ;;
  esac
}

# NAME CONFIG LABEL: one JSON line for the settings NAME / CONFIG select, resolved as every command resolves them
# (in a subshell: this command's own settings stay as they are), or the reason they're refused.
vm_settings_row() {
  ( if mc_load_settings "$1" "$2" >/dev/null 2>&1; then
      # A computer elsewhere (PLACE): whether its SSH answers now (a few seconds at most; its provider isn't asked,
      # except Fly's API for a Sprite, which says asleep or awake without waking it).
      local rstate=""
      if [ "${PLACE:-mac}" != mac ]; then
        rstate="$("$PY" "$LAB_ROOT/lib/remote.py" probe --vm "$VM_NAME" --run-dir "${RUN_DIR:-$LAB_ROOT/.run-vms/$VM_NAME}" \
                   --transport "${TRANSPORT:-ssh}" --host "${SSH_HOST:-}" --port "${SSH_HOST_PORT:-22}" \
                   --sprite-org "${SPRITE_ORG:-}" --sprite-name "${SPRITE_NAME:-}" --sprite-cli "${SPRITE_CLI:-}" \
                   --timeout 2 2>/dev/null || echo unknown)"
        grep -q "^mcomp-$VM_NAME " "$SSH_DIR/known_hosts" 2>/dev/null || rstate="not set up"
      fi
      "$PY" -c 'import json, sys
k = ["file", "kind", "vm", "baseline", "ssh_port", "vnc_port", "vault", "spool", "volumes", "ssh_dir", "run_dir", "log_dir",
     "place", "remote_state", "provider"]
print(json.dumps(dict(zip(k, sys.argv[1:]), ok=True)))' \
        "$MC_SETTINGS_LABEL" "$MC_SETTINGS_KIND" "$VM_NAME" "$BASELINE_NAME" "$SSH_PORT" "$VNC_PORT" "$(mc_vault_dir)" \
        "${EVENTS_SPOOL:-}" "${VOLUMES:-}" "$SSH_DIR" "${RUN_DIR:-$LAB_ROOT/.run}" "${LOG_DIR:-$LAB_ROOT/logs}" \
        "${PLACE:-mac}" "$rstate" "${PROVIDER:-local-lume}"
    else
      # Refused, but still a VM's settings: its names and folders as the file says them (unchecked) go in the row,
      # so nothing that's that VM's can be deleted with another; `vm delete` refuses folders it can't know.
      local refusal="$MC_REFUSAL"
      { if [ -n "${4:-}" ] && [ -f "$MC_VMS_DIR/$4.env" ]; then mc_identity_records raw vm "$4"
        elif [ -n "$2" ]; then mc_identity_records raw file "$2"
        else mc_identity_records raw main; fi; } \
        | "$PY" "$LAB_ROOT/lib/vms.py" error-row --file "$3" --vm "${4:-}" --error "$refusal"
    fi )
}
# Every settings file of this checkout, one JSON line each: config/local.env (or the defaults), config/vms/*.env,
# and the MCOMP_CONFIG file this command was given when it's another one.
vm_all_settings() {
  local n
  if [ -f "$MC_MAIN_CONFIG" ]; then vm_settings_row "" "" config/local.env
  else vm_settings_row "$MC_DEFAULT_VM" "" "the defaults (no config/local.env)" "$MC_DEFAULT_VM"; fi
  for n in $(mc_vm_files); do vm_settings_row "$n" "" "config/vms/$n.env" "$n"; done
  if [ "${MC_SETTINGS_KIND:-}" = config ]; then vm_settings_row "" "$LOCAL_CONFIG" "$MC_SETTINGS_LABEL"; fi
}

# `metacomputer list` and `vm list` (the same): every VM, where it is (lib/placement.py; every one is on this Mac),
# the one these settings act on (*), and the default computer.
vm_list() { # [--on PLACE] [--json]
  local json=() on=() a tmp rc=0 sel="$VM_NAME" def="" problem="${MC_DEFAULT_PROBLEM:-}"
  local usage="usage: metacomputer list [--on PLACE] [--json]   (or: metacomputer vm list ...)"
  sa_check metacomputer "${usage#usage: }" 0 0 "--json" "--on=" "$@" || return 0   # (-h/--help: bin/metacomputer's)
  while [ $# -gt 0 ]; do
    case "$1" in
      --json) json=(--json) ;;
      --on) vc_once --on; [ $# -ge 2 ] && [ -n "$2" ] || die "--on needs a place: mac (this Mac) or cloud ($usage)"; on=(--on "$2"); shift ;;
      --on=*) vc_once --on; [ -n "${1#--on=}" ] || die "--on needs a place: mac (this Mac) or cloud ($usage)"; on=(--on "${1#--on=}") ;;
      *) die "$usage" ;;
    esac
    shift
  done
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/mcomp-vms.XXXXXX")" || die "couldn't make a temporary folder"
  vm_all_settings > "$tmp/settings.jsonl"
  local runner=(--runner-failed)
  if command -v "$LUME" >/dev/null 2>&1 && "$LUME" ls --format json > "$tmp/runner.json" 2>/dev/null; then runner=(--runner "$tmp/runner.json"); fi
  # With no settings file and no --vm, nothing is chosen when there's more than one VM to choose from; nor when the
  # default computer (config/default-vm) doesn't resolve (scripts/lib.sh loaded the main VM's settings for this
  # listing only).
  if [ "${MC_IMPLICIT:-0}" = 1 ]; then
    if [ -n "$(mc_vm_files)" ] || [ "${runner[0]}" = --runner-failed ] || [ -n "$(vm_working_names "$VM_NAME" 2>/dev/null)" ]; then sel=""; fi
  fi
  [ -z "$problem" ] || sel=""
  # The default computer as stored: the VM config/default-vm names, else the main VM when a command that names no VM
  # would act on it. Read here whatever chose this command's VM (--vm, MCOMP_VM, MCOMP_CONFIG, MCOMP_NO_DEFAULT_VM
  # choose the selection, *, not the default); what overrode it is listed too.
  if [ -n "$problem" ]; then
    def="${MC_DEFAULT_PTR:-}"      # the name it holds, if it holds one: reported with why it doesn't resolve
  elif [ "${MC_FROM_DEFAULT:-0}" = 1 ]; then
    def="$VM_NAME"
  elif ! mc_read_default; then
    problem="$MC_REFUSAL"
  elif [ -n "$MC_DEFAULT_PTR" ]; then
    def="$MC_DEFAULT_PTR"
  elif [ -f "$MC_MAIN_CONFIG" ] || { [ -z "$(mc_vm_files)" ] && [ "${runner[0]}" != --runner-failed ] \
       && [ -z "$(vm_working_names "$MC_DEFAULT_VM" 2>/dev/null)" ]; }; then
    def="$(mc_main_identity | head -1)"
  fi
  [ -z "$problem" ] || problem="$problem; metacomputer default NAME chooses another"
  local ovr=""
  if [ -n "${MC_VM_SAID:-}" ]; then ovr="--vm $MC_VM_SAID"
  elif [ -n "${MC_ENV_VM:-}" ]; then ovr="MCOMP_VM=$MC_ENV_VM"
  elif [ -n "${MC_ENV_CONFIG:-}" ]; then ovr="MCOMP_CONFIG=$(mc_tilde "$MC_ENV_CONFIG")"
  elif [ -n "${MC_ENV_NODEF:-}" ] && { [ -n "${MC_DEFAULT_PTR:-}" ] || [ -n "$problem" ]; }; then ovr="MCOMP_NO_DEFAULT_VM=$MC_ENV_NODEF"
  fi
  "$PY" "$LAB_ROOT/lib/vms.py" list --settings "$tmp/settings.jsonl" "${runner[@]}" --selected "$sel" --default "$def" \
    --default-problem "$problem" --overridden-by "$ovr" ${on[@]+"${on[@]}"} ${json[@]+"${json[@]}"} || rc=$?
  "$LAB_ROOT/scripts/safe-rmtree.sh" --quiet "$tmp"
  return $rc
}

# `metacomputer default [NAME] [--json]`: the default computer, the one a command acts on when no VM is named
# (config/default-vm; scripts/lib.sh reads it as --vm NAME). Without NAME: which it is and where its settings are.
# With NAME: NAME must resolve to settings exactly as --vm NAME would (config/vms/NAME.env, checked like every load;
# or the main VM's own name, which removes config/default-vm: config/local.env's VM again, as before there was one).
# The pointer is written in one step: a temporary file in config/, renamed over it. Only that file changes: no VM
# is started, stopped or changed, and a hub or session already running keeps the VM it started with.
vm_default() { # [NAME] [--json]
  local name="" json=0 a info kind label usage="usage: metacomputer default [NAME] [--json]"
  sa_check metacomputer "${usage#usage: }" 0 1 "--json" "" "$@" || return 0
  for a in "$@"; do
    case "$a" in
      --json) json=1 ;;
      -*) die "$usage" ;;
      *) [ -z "$name" ] || die "one computer, please (got '$name' and '$a'); $usage"; name="$a" ;;
    esac
  done
  if [ -z "$name" ]; then vm_default_show "$json"; return; fi
  mc_valid_vm_name "$name" || die "'$name' isn't a VM name (letters, digits, '.', '-' and '_'); config/default-vm was left as it was"
  # NAME's lock, the one a build, vm delete, reset, restore and start of it hold, from the check below to the write:
  # NAME can't be deleted or replaced in between (vm delete reads config/default-vm under the same lock, so it either
  # sees NAME as the default and refuses without --force, or has deleted it before this check refuses it).
  local rc=0
  lock_hold "$(vm_lock_name "$name")" "${MCOMP_LOCK_WAIT:-10}" "metacomputer default $name" || rc=$?
  case "$rc" in
    0) ;;
    1) die "the VM $name is being built, deleted or changed ($LOCK_OWNER); config/default-vm was left as it was. Run this again when it's done" ;;
    *) die "couldn't make the private lock folder (MCOMP_LOCK_DIR, or the per-user temporary folder); config/default-vm was left as it was" ;;
  esac
  # The settings NAME resolves to, as every command would load them for --vm NAME (in a subshell: only a check).
  info="$(mc_load_settings "$name" "" >/dev/null 2>&1 || { printf '%s' "$MC_REFUSAL"; exit 1; }
          printf '%s\n%s\n%s' "$MC_SETTINGS_KIND" "${PLACE:-mac}" "$MC_SETTINGS_LABEL")" \
    || die "$name can't be your default computer: $info. config/default-vm was left as it was"
  kind="${info%%$'\n'*}"; info="${info#*$'\n'}"; MC_DEFAULT_PLACE="${info%%$'\n'*}"; label="${info#*$'\n'}"
  local had="" ok=1
  mc_read_default 2>/dev/null && had="$MC_DEFAULT_PTR" || ok=0
  case "$kind" in
    vm)
      if [ "$ok" = 1 ] && [ "$had" = "$name" ]; then
        vm_default_said "$json" "$name" "$label" "already your default computer" unchanged config/default-vm
        return 0
      fi
      vm_default_write "$name"
      vm_default_said "$json" "$name" "$label" "now your default computer: commands that name no VM act on it" set config/default-vm ;;
    local|defaults)
      # The main VM: no pointer at all, so a command that names no VM reads config/local.env as before. Only the
      # reader's "no file" counts as none (a lookup that failed may hide one): anything else is removed below.
      if [ "$ok" = 1 ] && [ -z "$had" ]; then
        vm_default_said "$json" "$name" "$label" "already your default computer (the main VM)" unchanged ""
        return 0
      fi
      [ ! -d "$MC_DEFAULT_FILE" ] || [ -L "$MC_DEFAULT_FILE" ] || die "config/default-vm is a folder, not the file metacomputer writes; nothing was changed (look at it, then remove it yourself)"
      rm -f "$MC_DEFAULT_FILE" || die "couldn't remove config/default-vm; nothing was changed"
      if vm_main_is_default; then
        vm_default_said "$json" "$name" "$label" "now your default computer again (the main VM): config/default-vm removed" removed ""
      else
        vm_default_said "$json" "$name" "$label" "the main VM; config/default-vm removed, so commands that name no VM act as before: here, with no config/local.env and more than one VM, they refuse until you name one (--vm NAME) or choose a default" removed ""
      fi ;;
    *) die "$name can't be your default computer: its settings are $label, not config/vms/$name.env or the main VM's; config/default-vm was left as it was" ;;
  esac
}
vm_default_write() { # NAME: config/default-vm holds NAME, written whole (a rename over the old one)
  local tmp
  [ ! -d "$MC_DEFAULT_FILE" ] || [ -L "$MC_DEFAULT_FILE" ] || die "config/default-vm is a folder, not the file metacomputer writes; nothing was changed (look at it, then remove it yourself)"
  tmp="$(mktemp "$LAB_ROOT/config/.default-vm.XXXXXX" 2>/dev/null)" || die "couldn't write config/default-vm (no temporary file in config/); it was left as it was"
  if printf '%s\n' "$1" > "$tmp" && "$PY" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp" "$MC_DEFAULT_FILE"; then
    return 0
  fi
  rm -f "$tmp"
  die "couldn't write config/default-vm; it was left as it was"
}
vm_default_said() { # JSON NAME LABEL WORDS CHANGE(set|unchanged|removed) POINTER; where it is: MC_DEFAULT_PLACE (mac
  # unless its settings say another place: a computer elsewhere)
  local where
  where="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import placement; print(placement.label(sys.argv[2]))' \
           "$LAB_ROOT/lib" "${MC_DEFAULT_PLACE:-mac}" 2>/dev/null)" || where="this Mac"
  if [ "$1" = 1 ]; then
    "$PY" -c 'import json, sys
n, label, change, pointer, place, where = sys.argv[1:7]
print(json.dumps({"default": n, "place": place, "where": where, "settings": label, "pointer": pointer or None,
                  "change": change, "ok": True}))' "$2" "$3" "$5" "$6" "${MC_DEFAULT_PLACE:-mac}" "${where:-this Mac}"
  else
    printf '%s\n' "$2 (${where:-this Mac}, $3) is $4."
  fi
}
# Whether a command that names no VM acts on the main VM when there's no config/default-vm: always with
# config/local.env; without it, only where vm_target_check lets it (no config/vms/ files, and no other VM in the
# runner, or no runner yet).
vm_main_is_default() {
  local names
  [ -f "$MC_MAIN_CONFIG" ] && return 0
  [ -z "$(mc_vm_files)" ] || return 1
  command -v "$LUME" >/dev/null 2>&1 || return 0
  names="$(vm_working_names "$MC_DEFAULT_VM")" || return 1
  [ -z "$names" ]
}
# Which computer is the default, and where its settings are; refused (exit 1) when config/default-vm doesn't resolve,
# or when there's none (no config/local.env, and more than one VM to choose from).
vm_default_show() { # JSON
  local name="" label="" pointer="" err="" env="" place=mac
  if ! mc_read_default; then
    err="$MC_REFUSAL"
  elif [ -n "$MC_DEFAULT_PTR" ]; then
    name="$MC_DEFAULT_PTR"; pointer="config/default-vm"
    label="$(mc_load_settings "$name" "" >/dev/null 2>&1 || { printf '%s' "$MC_REFUSAL"; exit 1; }; printf '%s\n%s' "$MC_SETTINGS_LABEL" "${PLACE:-mac}")" \
      || { err="config/default-vm makes $name your default computer, but: $label"; label=""; }
    [ -n "$err" ] || { place="$(printf '%s\n' "$label" | tail -1)"; label="$(printf '%s\n' "$label" | head -1)"; }
  elif vm_main_is_default; then
    name="$(mc_main_identity | head -1)"
    label="$(mc_load_settings "$name" "" >/dev/null 2>&1 || { printf '%s' "$MC_REFUSAL"; exit 1; }; printf '%s' "$MC_SETTINGS_LABEL")" \
      || { err="the main VM, $name, doesn't resolve: $label"; label=""; }
  else
    err="there's no default computer here: this checkout has no config/local.env and more than one VM to choose from"
  fi
  # A VM chosen in this shell's environment wins over the default for commands run here: say so.
  [ -z "${MC_ENV_NODEF:-}" ] || [ -z "$pointer" ] || env="MCOMP_NO_DEFAULT_VM=$MC_ENV_NODEF"
  [ -z "${MC_ENV_CONFIG:-}" ] || env="MCOMP_CONFIG=$(mc_tilde "$MC_ENV_CONFIG")"
  [ -z "${MC_ENV_VM:-}" ] || env="MCOMP_VM=$MC_ENV_VM"
  if [ "$1" = 1 ]; then
    "$PY" -c 'import json, sys
sys.path.insert(0, sys.argv[7])
import placement
n, label, pointer, err, env, place = sys.argv[1:7]
print(json.dumps({"default": n or None, "place": place if n and not err else None,
                  "where": placement.label(place) if n and not err else None,
                  "settings": label or None, "pointer": pointer or None, "ok": not err, "error": err or None,
                  "overridden_by": env or None}))' "$name" "$label" "$pointer" "$err" "$env" "$place" "$LAB_ROOT/lib"
    [ -z "$err" ]; return
  fi
  if [ -n "$err" ]; then
    printf '%s\n' "metacomputer: $err." "Commands that name no VM refuse until it's fixed (it never falls back to another VM): metacomputer default NAME chooses one (metacomputer list shows them; metacomputer default $(mc_main_identity | head -1) goes back to the main VM)." >&2
    return 1
  fi
  local where; where="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import placement; print(placement.label(sys.argv[2]))' "$LAB_ROOT/lib" "$place")"
  if [ -n "$pointer" ]; then
    printf '%s\n' "$name ($where, $label) is your default computer (config/default-vm): commands that name no VM act on it$([ "$place" = mac ] || echo ", on $where's hardware, not this Mac")."
  else
    printf '%s\n' "$name ($where, $label) is your default computer: the main VM, as there's no config/default-vm."
  fi
  case "$env" in
    '') ;;
    MCOMP_NO_DEFAULT_VM=*) printf '%s\n' "In this shell, $env leaves it out: commands here act on the main VM (unset it to use the default)." ;;
    *) printf '%s\n' "In this shell, $env chooses the VM instead (unset it to use the default)." ;;
  esac
  return 0
}

vm_port_in_use() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

vm_create() { # NAME [--ssh-port P] [--vnc-port P] [--no-build] [--yes] | NAME --on PLACE [--host H --user U --port P] [--size S] [--region R] [--egress E]
  local name="" ssh="" vnc="" build=1 a a2 used p f vault rows k v def tmp rc
  local on="${MC_ON_ARG:-}" host="" huser="" hport="" size="" egress="" idle="" org="${SPRITES_ORG:-}" sprite="" region="" rlabel=""
  # Every option once, a value that's never an option (`--org --help` took the request for usage as the organization).
  sa_check metacomputer "vm create NAME [--on PLACE] [OPTIONS]   (metacomputer help: vm create)" 1 1 "--no-build --yes -y" \
    "--on= --host --user --port --size --region --egress --idle-stop --org --sprite --ssh-port= --vnc-port=" "$@" || return 0
  while [ $# -gt 0 ]; do
    case "$1" in
      --on) vc_once --on; [ $# -ge 2 ] || die "--on needs a place: server (your own server), oci (Oracle Cloud), sprites (Fly Sprites), hetzner, digitalocean or vultr"; on="$2"; shift ;;
      --on=*) vc_once --on; on="${1#*=}" ;;
      --host) vc_once --host; [ $# -ge 2 ] || die "--host needs your server's address"; host="$2"; shift ;;
      --user) vc_once --user; [ $# -ge 2 ] || die "--user needs your account on the server"; huser="$2"; shift ;;
      --port) vc_once --port; [ $# -ge 2 ] || die "--port needs your server's SSH port"; hport="$2"; shift ;;
      --size) vc_once --size; [ $# -ge 2 ] || die "--size needs a size"; size="$2"; shift ;;
      --region) vc_once --region; [ $# -ge 2 ] || die "--region needs a region"; region="$2"; shift ;;
      --egress) vc_once --egress; [ $# -ge 2 ] || die "--egress needs internet or off"; egress="$2"; shift ;;
      --idle-stop) vc_once --idle-stop; [ $# -ge 2 ] || die "--idle-stop needs minutes, or off"; idle="$2"; shift ;;
      --org) vc_once --org; [ $# -ge 2 ] || die "--org needs the Fly organization's name"; org="$2"; shift ;;
      --sprite) vc_once --sprite; [ $# -ge 2 ] || die "--sprite needs the Sprite's name"; sprite="$2"; shift ;;
      --ssh-port) vc_once --ssh-port; [ $# -ge 2 ] || die "--ssh-port needs a port"; ssh="$2"; shift ;;
      --vnc-port) vc_once --vnc-port; [ $# -ge 2 ] || die "--vnc-port needs a port"; vnc="$2"; shift ;;
      --ssh-port=*) vc_once --ssh-port; ssh="${1#*=}" ;;
      --vnc-port=*) vc_once --vnc-port; vnc="${1#*=}" ;;
      --no-build) build=0 ;;
      --yes|-y) ASSUME_YES=1 ;;
      -*) die "unknown option $1 (metacomputer vm create NAME [--ssh-port P --vnc-port P] [--no-build] [--yes]; elsewhere: --on server --host ADDRESS --user ACCOUNT [--port P], --on oci [--size SIZE], --on hetzner|digitalocean|vultr [--size SIZE] [--region REGION], or --on sprites --org ORG [--sprite SPRITE]; all: [--egress internet|off])" ;;
      *) [ -z "$name" ] || die "one VM name, please (got '$name' and '$1')"; name="$1" ;;
    esac
    shift
  done
  [ -n "$name" ] || die "usage: metacomputer vm create NAME [--ssh-port P --vnc-port P] [--no-build] [--yes]"
  [[ "$name" =~ ^[a-z0-9][a-z0-9-]{0,23}$ ]] && [[ ! "$name" =~ (-baseline|-baseline-prev|-prev|-)$|-ckpt- ]] \
    || die "a new VM's name: up to 24 lowercase letters, digits and dashes, starting with a letter or digit, not ending in -baseline, -prev or a dash, and without -ckpt-"
  # Not a place's name (mac, cloud): --on PLACE and a request for that place must never mean a VM (lib/placement.py).
  a="$("$PY" "$LAB_ROOT/lib/placement.py" name-ok "$name")" || die "${a:-$name is the name of a place}; choose another name for the new VM"
  # Where: this Mac (a Lume VM, as before), or an Ubuntu VM elsewhere (docs/remote.md): your own server, or a provider's.
  local rprov="" rlines=()
  case "${on:-mac}" in
    mac) [ -z "$host$huser$hport$size$egress$idle$region$sprite" ] || die "--host, --user, --port, --size, --region, --egress, --idle-stop and --sprite are for a computer elsewhere (--on server, oci, sprites, hetzner, digitalocean or vultr)" ;;
    server)
      rprov=ubuntu-vm
      [ -n "$host" ] && [ -n "$huser" ] || die "your own server: --host ADDRESS --user YOUR_ACCOUNT (an account that signs in with a key and may use sudo)"
      [ -z "$size$idle$region" ] || die "--size, --region and --idle-stop are for a computer a provider makes; metacomputer never powers your own server off"
      a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from cloud import transport
try: print(transport.check_host(sys.argv[2])); transport.check_port(sys.argv[3] or 22)
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$host" "$hport")" || die "$a"
      host="$a"
      [[ "$huser" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "--user: an account name"
      rlines=("SSH_HOST=$host" "SSH_HOST_PORT=${hport:-22}" "SETUP_USER=$huser" "IDLE_STOP=off") ;;
    oci)
      rprov=oci; rlabel="Oracle Cloud"
      [ -z "$host$huser$hport" ] || die "--host, --user and --port are for your own server; Oracle Cloud gives the computer its address"
      [ -z "$region" ] || die "--region: an Oracle Cloud computer is made in your credentials' region (OCI_REGION)"
      size="${size:-arm-2x12}"
      a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from cloud import oci
try: oci.size_of(sys.argv[2])
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$size")" || die "$a"
      case "${idle:-15}" in off|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) ;; *) die "--idle-stop: minutes (1-999), or off" ;; esac
      rlines=("SIZE=$size" "IDLE_STOP=${idle:-15}") ;;
    sprites)
      rprov=sprites; rlabel="Fly Sprites"
      [ -z "$host$huser$hport$size$idle$region" ] || die "--host, --user, --port, --size, --region and --idle-stop aren't for a Sprite: Fly sizes it and chooses where it runs, and it sleeps by itself about 10 s after nothing is connected"
      [ -n "$org" ] || die "a Sprite is made in one of your Fly organizations: --org ORG (or SPRITES_ORG in config/local.env)"
      sprite="${sprite:-$name}"
      a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from cloud import sprites
try: sprites.org_of({"SPRITE_ORG": sys.argv[2]}); print(sprites.sprite_of({"SPRITE_NAME": sys.argv[3]}))
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$org" "$sprite")" || die "$a"
      remote_can_ask_for sprites || die "Fly's sprite command isn't installed: install it and sign in (sprite login), then run this again (docs/sprites.md)"
      rlines=("SPRITE_ORG=$org" "SPRITE_NAME=$sprite" "IDLE_STOP=off")
      [ -z "${SPRITE_CLI:-}" ] || rlines+=("SPRITE_CLI=$(mc_sprite_cli)") ;;
    hetzner|digitalocean|vultr)
      # A provider that bills servers by the hour (lib/cloud/vps.py): its size and region checked, its defaults filled in.
      rprov="$on"
      [ -z "$host$huser$hport" ] || die "--host, --user and --port are for your own server; the provider gives the computer its address"
      a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import cloud
c = cloud.ADAPTERS[sys.argv[2]]
try: print(c.size_of(sys.argv[3] or None), c.region_of(sys.argv[4] or None), c.manifest["label"], sep="\t")
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$on" "$size" "$region")" || die "$a"
      IFS=$'\t' read -r size region rlabel <<<"$a"
      # Vultr's idle stop is off unless asked: it saves nothing there, and Vultr doesn't see a guest power itself off
      # (it still counts it as running, and its start would restart a running one).
      [ "$on" != vultr ] || idle="${idle:-off}"
      case "${idle:-15}" in off|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) ;; *) die "--idle-stop: minutes (1-999), or off" ;; esac
      rlines=("SIZE=$size" "REGION=$region" "IDLE_STOP=${idle:-15}") ;;
    *) die "--on $on: vm create makes a VM on this Mac (mac), sets up your own server (server), or makes one on Oracle Cloud (oci), Fly Sprites (sprites), Hetzner Cloud (hetzner), DigitalOcean (digitalocean) or Vultr (vultr)" ;;
  esac
  [ "$rprov" = sprites ] || [ -z "$sprite" ] || die "--sprite is for --on sprites"
  case "${egress:-internet}" in internet|off) ;; *) die "--egress: internet or off" ;; esac
  [ -z "$rprov" ] || rlines=("PLACE=$on" "PROVIDER=$rprov" "TRANSPORT=$([ "$rprov" = sprites ] && echo sprite || echo ssh)" "EGRESS=${egress:-internet}" "${rlines[@]}")
  # The new VM's lock, from the first look until the build ends (the build inherits it): one change per VM at a time.
  rc=0; lock_hold "$(vm_lock_name "$name")" "${MCOMP_LOCK_WAIT:-10}" "metacomputer vm create $name" || rc=$?
  case "$rc" in
    0) ;;
    1) die "another build or change of the VM $name is running ($LOCK_OWNER); nothing was done" ;;
    *) die "couldn't make the private lock folder (MCOMP_LOCK_DIR, or the per-user temporary folder)" ;;
  esac
  # One vm create at a time from the first look at the settings until the new file is written (descriptor 9,
  # released before the build): two creates never pick the same free ports or write one name twice.
  rc=0; lock_hold "vms-create" "${MCOMP_LOCK_WAIT:-10}" "metacomputer vm create $name" 9 || rc=$?
  case "$rc" in
    0) ;;
    1) die "another metacomputer vm create is choosing ports ($LOCK_OWNER); nothing was done. Run this again when it's done" ;;
    *) die "couldn't make the private lock folder (MCOMP_LOCK_DIR, or the per-user temporary folder)" ;;
  esac
  f="$MC_VMS_DIR/$name.env"
  [ ! -e "$f" ] || die "config/vms/$name.env exists already: that VM's settings (metacomputer --vm $name ..., or vm delete $name)"
  rows="$(vm_all_settings)"
  # The name must be free everywhere: this checkout's settings and the runner (a VM, or its baseline).
  printf '%s\n' "$rows" | "$PY" -c 'import json, sys
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("ok") and sys.argv[1] in (d.get("vm"), d.get("baseline")): sys.exit(1)' "$name" \
    || die "this checkout's settings already use the name $name (metacomputer vm list)"
  if [ -z "$rprov" ]; then
  command -v "$LUME" >/dev/null 2>&1 || die "the VM runner $LUME isn't installed (./install.sh installs it)"
  for v in "$name" "$name-baseline"; do
    a="$(vm_lookup "$v")" || die "couldn't list the VMs with $LUME, so it can't tell whether $v exists; nothing was done"
    [ "$a" = absent ] || die "the runner has a VM named $v already; vm create makes new VMs only (metacomputer vm list)"
  done
  fi
  # Ports: the ones given, else the first free pair from 2300 (live view = SSH + 3700, like 2277/5977). Never one
  # another VM's settings here use, or that something on this Mac listens on.
  used=" $(printf '%s\n' "$rows" | "$PY" -c 'import json, sys
out = []
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("ok"): out += [str(d.get("ssh_port")), str(d.get("vnc_port"))]
print(" ".join(out))') "
  for p in "$ssh" "$vnc"; do
    [ -z "$p" ] && continue
    [[ "$p" =~ ^[0-9]{4,5}$ ]] && [ "$p" -ge 1024 ] && [ "$p" -le 65535 ] || die "ports: 1024-65535 (got '$p')"
    case "$used" in *" $p "*) die "port $p is already another VM's here (metacomputer vm list)" ;; esac
    vm_port_in_use "$p" && die "something on this Mac listens on port $p already"
  done
  if [ -z "$ssh" ] || [ -z "$vnc" ]; then
    [ -z "$ssh$vnc" ] || die "give both --ssh-port and --vnc-port, or neither (then free ones are picked)"
    p=2300
    while [ $p -lt 3000 ]; do
      case "$used" in *" $p "*|*" $((p + 3700)) "*) ;; *)
        vm_port_in_use "$p" || vm_port_in_use "$((p + 3700))" || break ;; esac
      p=$((p + 1))
    done
    [ $p -lt 3000 ] || die "no free pair of ports from 2300; choose them with --ssh-port and --vnc-port"
    ssh="$p"; vnc="$((p + 3700))"
  fi
  [ "$ssh" != "$vnc" ] || die "--ssh-port and --vnc-port must differ"
  vault="$(mc_vm_default VAULT_DIR "$name")"
  [ $(( ${#vault} + 11 )) -le 103 ] || die "the vault's socket path would be too long for macOS ($vault/vault.sock); choose a shorter name"
  if [ "$build" = 1 ] && [ "${ASSUME_YES:-0}" != 1 ] && [ ! -t 0 ]; then
    die "building the VM needs your OK, and there's no terminal to ask on: add --yes (or --no-build to write only its settings)"
  fi
  # The file: who it is, then the Mac's choices that this checkout's settings make differently from the defaults.
  ( umask 077; mkdir -p "$MC_VMS_DIR" ) || die "couldn't make config/vms/"
  tmp="$f.tmp.$$"
  {
    printf '%s\n' "# metacomputer settings for the VM $name, made by \`metacomputer vm create\` on $(date '+%Y-%m-%d')." \
      "# Used by: metacomputer --vm $name COMMAND (or MCOMP_VM=$name). Read after config/defaults.env, on its own:" \
      "# config/local.env isn't read for this VM. Bash syntax. Its own names and ports:" \
      "VM_NAME=$name" "BASELINE_NAME=$name-baseline" "SSH_PORT=$ssh" "VNC_PORT=$vnc" \
      ${rprov:+"# Where it is: a computer reached over SSH (docs/remote.md, docs/sprites.md). SSH_PORT is kept only so no two"} \
      ${rprov:+"# computers here share a port; VNC_PORT is where its live view's SSH tunnel listens on this Mac."} \
      ${rlines[@]+"${rlines[@]}"} \
      "# Its folders are fixed by its name and can't be set here (SSH_DIR, RUN_DIR, LOG_DIR, VAULT_DIR, EVENTS_SPOOL):" \
      "#   keys $(mc_tilde "$(mc_vm_default SSH_DIR "$name")"), runtime .run-vms/$name and logs logs-vms/$name in this checkout," \
      "#   vault $(mc_tilde "$vault") (its event spool in it)."
    def="$( . "$LAB_ROOT/config/defaults.env"; for k in $VM_COPY_KEYS; do eval "printf '%s=%s\n' \"\$k\" \"\${$k:-}\""; done )"
    a=0
    for k in $VM_COPY_KEYS; do
      eval "v=\${$k:-}"
      [ "$k" != LUME ] || v="${LUME_CONFIGURED:-$LUME}"   # as configured, not the lume-secure fallback
      [ "$k=$v" != "$(printf '%s\n' "$def" | grep -m1 "^$k=" || true)" ] || continue
      case "$k" in ACTION_LOG_KEY|SSH_CA_KEY|DRIVER_MANIFEST|STORAGE_DIR) [ -n "$v" ] || continue ;; esac
      [ "$a" = 1 ] || { printf '%s\n' "# Copied from $MC_SETTINGS_LABEL when this file was made (they differ from the defaults):"; a=1; }
      # A runner in your home is written as install.sh writes it ("$HOME/..."), a plain value get.sh accepts.
      case "$k=$v" in LUME="$HOME"/.local/bin/*)
        case "${v#"$HOME"/}" in *[!A-Za-z0-9._/-]*) ;; *) printf 'LUME="$HOME/%s"\n' "${v#"$HOME"/}"; continue ;; esac ;; esac
      vm_q "$k" "$v"
    done
    # A live-view password in the Keychain is per VM (its account is the VM's name); a password command isn't.
    [ "${VNC_PASSWORD_STORE:-file}" != keychain ] || printf '%s\n' "VNC_PASSWORD_STORE=keychain"
  } > "$tmp" || { rm -f "$tmp"; die "couldn't write $f"; }
  chmod 600 "$tmp" && mv "$tmp" "$f" || { rm -f "$tmp"; die "couldn't write $f"; }
  # The new file must resolve like every other: its names, ports and folders against every VM's (scripts/lib.sh).
  a="$(mc_load_settings "$name" "" >/dev/null 2>&1 || { printf '%s' "$MC_REFUSAL"; exit 1; })" \
    || { rm -f "$f"; die "the new settings were refused, so they were removed: $a"; }
  exec 9>&-   # the settings are written: the next vm create sees them (the VM's own lock stays, through the build)
  MCOMP_VM_NOTICE=1
  if [ -n "$rprov" ]; then
    printf '%s\n' "metacomputer: new computer $name ($(case "$rprov" in (sprites) echo "Fly Sprites, the Sprite $sprite in $org" ;; (ubuntu-vm) echo "your own server, $huser@$host" ;; (*) echo "$rlabel, $size${region:+ in $region}" ;; esac)): config/vms/$name.env, live view through SSH on 127.0.0.1:$vnc, vault $(mc_tilde "$vault")" >&2
  else
  printf '%s\n' "metacomputer: new VM $name: config/vms/$name.env, SSH 127.0.0.1:$ssh, live view 127.0.0.1:$vnc, vault $(mc_tilde "$vault")" >&2
  fi
  emit_event lifecycle vm_created info "Settings for a new VM $name were made (config/vms/$name.env)" "metacomputer --vm $name build" "name=$name"
  if [ "$build" = 0 ]; then
    ok "settings written; build it with: metacomputer --vm $name build"
    return 0
  fi
  if [ "$rprov" = sprites ]; then
    # Something billed: the rate and what it keeps, before anything is made (lib/cloud/sprites.py rates).
    a="$(MCOMP_VM="$name" remote_py rates --vm "$name" 2>/dev/null || true)"
    printf '%s\n' "Fly Sprites: ${a:-rates unknown}" "PERSIST=disk: its disk keeps everything while it sleeps. It sleeps by itself about 10 s after nothing is connected (the hub lets go after HUB_IDLE unused); asleep, only its disk is billed. Its network policy is EGRESS=${egress:-internet}, enforced by Fly outside the computer (docs/sprites.md)." >&2
  elif [ "$rprov" = oci ]; then
    # Something billed: the rate and what it keeps, before anything is made (lib/cloud/oci.py rates).
    a="$(MCOMP_VM="$name" remote_py rates --vm "$name" 2>/dev/null || true)"
    printf '%s\n' "Oracle Cloud: ${a:-rates unknown}" "PERSIST=disk: its disk keeps everything while it's stopped. It powers itself off after ${idle:-15} minutes unused (IDLE_STOP), which saves nothing at Oracle: a billed size is billed as running until it's stopped there (metacomputer --vm $name stop); stopped, only its disk is billed (docs/remote.md, What a stopped computer costs)." >&2
  elif [ -n "$rprov" ] && [ "$rprov" != ubuntu-vm ]; then
    # Billed by the hour, stopped or not (lib/cloud/vps.py): the rate, and that only deleting it stops the charges.
    a="$(MCOMP_VM="$name" remote_py rates --vm "$name" 2>/dev/null || true)"
    if [ "${idle:-15}" = off ]; then a2="Its idle stop is off (IDLE_STOP=off): it would save nothing there."
    else a2="It powers itself off after ${idle:-15} minutes unused (IDLE_STOP), which saves nothing there."; fi
    printf '%s\n' "$rlabel: ${a:-rates unknown}" "PERSIST=disk: its disk keeps everything while it's stopped. But $rlabel bills a stopped server in full, the same as a running one: only deleting it (metacomputer vm delete $name) stops the charges. $a2 (docs/remote.md, What a stopped computer costs)" >&2
  fi
  if [ "${ASSUME_YES:-0}" != 1 ]; then
    if [ -n "$rprov" ]; then
      printf 'Set up %s now? %s [y/N] ' "$name" "$(case "$rprov" in (oci) echo "This makes the instance on Oracle Cloud, then installs the desktop (about 10 minutes)." ;; (sprites) echo "This makes the Sprite on Fly, then installs the desktop (about 5 minutes)." ;; (ubuntu-vm) echo "This takes your server over for metacomputer: its firewall, accounts and desktop (about 10 minutes)." ;; (*) echo "This makes the server on $rlabel, then installs the desktop (about 10 minutes)." ;; esac)"
    else
    printf 'Build the VM %s now? About 1.5 GB of downloads the first time (kept for later builds), then a few minutes. [y/N] ' "$name"
    fi
    read -r a
    case "$a" in y|Y) ;; *) ok "not built. Its settings are in config/vms/$name.env: metacomputer --vm $name build builds it, metacomputer vm delete $name removes them"; return 0 ;; esac
  fi
  rc=0
  # The build may make a provider's instance: the credentials go to it (metacomputer takes them off its environment
  # first thing), and the selection it was given doesn't (bash builtins only: no other program sees them).
  ( unset MCOMP_CONFIG MCOMP_VM MCOMP_VM_NOTICE; mc_export_provider_env; exec "$SELF" --vm "$name" build ) || rc=$?
  if [ "$rc" = 0 ]; then
    say "the VM $name is built. Next:"
    printf '%s\n' "  metacomputer --vm $name start" \
      "  metacomputer --vm $name secrets init        (its own vault, in your own terminal; then secrets bind)" \
      "  a Claude Code session on it: docs/guide.md, \"Run a second VM side by side\""
  else
    if [ -z "$on" ] || [ "$on" = mac ]; then
      warn "the build of $name failed (above). Its settings stay in config/vms/$name.env: metacomputer --vm $name build --replace tries again; metacomputer vm delete $name removes it all"
    else
      warn "the build of $name failed (above). Its settings stay in config/vms/$name.env: metacomputer --vm $name build tries again; metacomputer vm delete $name removes it all"
    fi
  fi
  return "$rc"
}
vm_q() { # KEY VALUE: KEY='VALUE' for a settings file (quoted for bash)
  local v="${2//\'/\'\\\'\'}"
  printf "%s='%s'\n" "$1" "$v"
}

# `vm delete NAME` runs with NAME's own settings (its baseline, lock, runtime folders): when this command's settings
# are another VM's, it runs itself again with --vm NAME, and the checks below happen there.
vm_delete() { # NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes]
  local name="" confirm_name="" force=0 del_vault=0 del_vols=0 yes_given=0 remove_from_server=0 checked_provider="" a args=("$@")
  sa_check metacomputer "vm delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes] [--remove-from-server] [--checked-provider] [--yes]" \
    1 1 "--force --delete-vault --delete-volumes --remove-from-server --checked-provider --yes -y" "--confirm=" "$@" || return 0
  while [ $# -gt 0 ]; do
    case "$1" in
      --confirm) vc_once --confirm; [ $# -ge 2 ] || die "--confirm needs the VM's name"; confirm_name="$2"; shift ;;
      --confirm=*) vc_once --confirm; confirm_name="${1#*=}" ;;
      --force) force=1 ;;
      --delete-vault) del_vault=1 ;;
      --delete-volumes) del_vols=1 ;;
      --remove-from-server) remove_from_server=1 ;;
      --checked-provider) checked_provider=--checked-provider ;;   # an unresolved launch: you've looked at the provider (your own terminal)
      --yes|-y) yes_given=1 ;;
      -*) die "unknown option $1 (metacomputer vm delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes] [--remove-from-server] [--checked-provider])" ;;
      *) [ -z "$name" ] || die "one VM at a time (got '$name' and '$1')"; name="$1" ;;
    esac
    shift
  done
  [ -n "$name" ] || die "usage: metacomputer vm delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes]"
  mc_valid_vm_name "$name" || die "'$name' isn't a VM name"
  # --checked-provider says the person has looked at the provider for a launch that got no answer: only they can say
  # that, at their own terminal (an agent tells them the command instead).
  [ -z "$checked_provider" ] || user_tty || die "vm delete $name --checked-provider says you've looked at the provider for a server a launch may have made, so it's yours to run: in your own terminal (not through Claude Code, Codex or another agent). Nothing was deleted"
  if [ "$VM_NAME" != "$name" ] || [ "${MC_IMPLICIT:-0}" = 1 ]; then
    [ -z "${MC_VM_DELETE_HOP:-}" ] || die "the settings for $name name the VM $VM_NAME; nothing was deleted"
    local main_vm=""; [ -f "$MC_MAIN_CONFIG" ] && main_vm="$(mc_file_vm_name "$MC_MAIN_CONFIG")"
    if [ ! -f "$MC_VMS_DIR/$name.env" ] && [ "$main_vm" != "$name" ] && { [ -f "$MC_MAIN_CONFIG" ] || [ "$name" != "$MC_DEFAULT_VM" ]; }; then
      die "this checkout has no settings for a VM named $name, so it doesn't know its baseline, keys or folders; nothing was deleted. metacomputer vm list shows the VMs it has settings for (a VM made with another settings file: MCOMP_CONFIG=FILE metacomputer vm delete $name)"
    fi
    unset MCOMP_CONFIG MCOMP_VM MCOMP_VM_NOTICE; export MC_VM_DELETE_HOP=1
    mc_export_provider_env   # a computer elsewhere: its provider deletes the instance (only metacomputer gets them)
    exec "$SELF" --vm "$name" vm delete ${args[@]+"${args[@]}"}
  fi
  vm_notice
  # The VM's lock from here to the end: no build, reset, restore or start of it (start takes it too) meanwhile.
  vm_lock_or_die "metacomputer vm delete $VM_NAME"
  if [ "${MC_REMOTE:-0}" = 1 ]; then vm_delete_remote; return $?; fi
  [ "$remove_from_server" = 0 ] || die "--remove-from-server is for your own server (a computer reached over SSH); nothing was deleted"
  command -v "$LUME" >/dev/null 2>&1 || die "the VM runner $LUME isn't installed; nothing was deleted"
  local settings_file="" own_vault="$MC_VMS_APP_DIR/$VM_NAME" lab_p first v
  [ "$MC_SETTINGS_KIND" = vm ] && settings_file="$LOCAL_CONFIG"
  lab_p="$(cd "$LAB_ROOT" && pwd -P)" || die "can't read the checkout's folder; nothing was deleted"
  vm_delete_plan
  # The plan, then the name typed: never --yes.
  printf '%s\n' "This deletes:"
  [ "$DP_VM" = absent ] || printf '%s\n' "  the VM $VM_NAME ($DP_STATE, $(vm_human "$DP_VM_BYTES") on disk)"
  [ "$DP_BASE" = absent ] || printf '%s\n' "  its baseline $BASELINE_NAME ($(vm_human "$DP_BASE_BYTES"))"
  [ "$DP_PREV" = absent ] || printf '%s\n' "  its previous baseline $BASELINE_NAME-prev ($(vm_human "$DP_PREV_BYTES"))"
  [ -z "$settings_file" ] || printf '%s\n' "  its settings, config/vms/$VM_NAME.env, and its runtime folder"
  [ "$del_vault" = 0 ] || printf '%s\n' "  its vault folder $(mc_tilde "$own_vault"), with every secret in it"
  [ -z "$DP_VOLS" ] || printf '%s\n' "  its volumes: $DP_VOLS(each asks again)"
  printf '%s\n' "It keeps:"
  [ "$del_vault" = 1 ] || printf '%s\n' "  the vault folder $(mc_tilde "$(mc_vault_dir)") (--delete-vault deletes a VM's own)"
  [ "$del_vols" = 1 ] || [ -z "${VOLUMES:-}" ] || printf '%s\n' "  its volumes, $VOLUMES (--delete-volumes)"
  printf '%s\n' "  its logs ($(vm_path "$LOG_DIR")) and SSH keys ($(mc_tilde "$SSH_DIR"))"
  [ -n "$settings_file" ] || printf '%s\n' "  its settings file, $MC_SETTINGS_LABEL"
  # The typed name guards against a slip, not against a program: anything with a terminal can type it (docs).
  if [ -n "$confirm_name" ]; then
    [ "$confirm_name" = "$VM_NAME" ] || die "--confirm names '$confirm_name', not $VM_NAME; nothing was deleted"
  else
    [ -t 0 ] || die "deleting a VM needs its name typed: run this in a terminal, or add --confirm $VM_NAME$([ "$yes_given" = 0 ] || echo ' (--yes doesn'"'"'t count)'). Nothing was deleted"
    printf 'Type the name of the VM to delete it (%s): ' "$VM_NAME"
    IFS= read -r v || v=""
    [ "$v" = "$VM_NAME" ] || die "that isn't $VM_NAME; nothing was deleted"
  fi
  expect_guard
  # Its owners lock from here to the end, so no session can claim it; then everything is checked again, before
  # anything stops: whatever changed while you were confirming refuses, and leaves what runs for it running.
  ( umask 077; mkdir -p "$RUN_DIR/owners" ) 2>/dev/null
  exec 6>>"$RUN_DIR/owners/.lock" || die "can't open $VM_NAME's owners lock; nothing was deleted"
  lock_fd_take 6 "${MCOMP_LOCK_WAIT:-10}" || die "a session holds $VM_NAME's owners lock; nothing was deleted (metacomputer --vm $VM_NAME who)"
  first="$DP_SIG"
  vm_delete_plan
  [ "$DP_SIG" = "$first" ] || die "$VM_NAME changed while you were confirming ($first, now $DP_SIG); nothing was deleted. Run this again"
  # Only now what runs for it (the event watcher, the hub) stops, then the VM.
  events_watcher_stop
  hub_stop
  if [ "$DP_STATE" = running ]; then
    say "stopping $VM_NAME (forced: it's being deleted)"
    vm_stop "$VM_NAME" --force
  fi
  netgw_stop
  storage_release --quiet
  local gone=() a
  for v in "$VM_NAME" "$BASELINE_NAME" "$BASELINE_NAME-prev"; do
    a="$(vm_lookup "$v")" || die "couldn't list the VMs with $LUME before deleting $v; deleted so far: ${gone[*]:-nothing}"
    [ "$a" = present ] || continue
    runner_delete "$v" >/dev/null || die "couldn't delete $v (above); deleted so far: ${gone[*]:-nothing}"
    gone+=("$v")
  done
  if [ -n "$settings_file" ]; then
    rm -f "$settings_file" && gone+=("config/vms/$VM_NAME.env")
    if [ "$DP_RUN" = 1 ]; then
      a="$(vm_rmtree "$lab_p" ".run-vms/$VM_NAME")" || warn "kept its runtime folder: $a"
    fi
  fi
  if [ "$del_vault" = 1 ]; then
    # End an unlocked vault's process first (anyone may lock: it only restricts), then remove the folder.
    [ ! -S "$own_vault/vault.sock" ] || [ ! -x "$VAULT_BIN" ] || MCOMP_VAULT_DIR="$own_vault" "$VAULT_BIN" lock >/dev/null 2>&1 || true
    if a="$(vm_rmtree "$MC_VMS_APP_DIR" "$VM_NAME")"; then
      [ "$a" = absent ] || gone+=("the vault folder $(mc_tilde "$own_vault")")
    else
      warn "kept the vault folder: $a"
    fi
  fi
  emit_event lifecycle vm_deleted info "The VM $VM_NAME was deleted" "" "name=$VM_NAME" "baseline=$BASELINE_NAME"
  local list=""; for v in ${gone[@]+"${gone[@]}"}; do list="${list:+$list, }$v"; done
  ok "deleted: ${list:-nothing}"
  if ! mc_read_default; then
    warn "config/default-vm can't be read ($MC_REFUSAL), so commands that name no VM refuse until you choose a default: metacomputer default NAME"
  elif [ "$MC_DEFAULT_PTR" = "$VM_NAME" ]; then
    warn "config/default-vm still makes $VM_NAME your default computer, so commands that name no VM refuse until you choose another: metacomputer default NAME"
  fi
  for v in $DP_VOLS; do
    say "volume $v:"
    storage_py volume delete "$v" || warn "volume $v was kept (above)"
  done
  return 0
}
# vm delete of a computer elsewhere (MC_REMOTE): a provider's instance (and its boot volume) is deleted at the
# provider, and its network with the last one; your own server is left exactly as it is (its accounts and desktop stay:
# what to remove there is said). Here: its settings file and runtime folder; its keys and logs stay, as for any VM.
# The typed name, the owners lock and the in-use checks are vm delete's own.
# What vm delete of a computer elsewhere checks, before the prompt and again under its owners lock right before
# anything is stopped or deleted: in use (a session, a handoff), the default computer, its folders, and what its
# provider record says (RDP_SIG sums it up, to compare). Dies with the reason; uses vm_delete_remote's variables.
remote_delete_plan() {
  local why=() a v
  if [ "$RUN_DIR" = "$LAB_ROOT/.run-vms/$VM_NAME" ]; then
    a="$(vm_rmtree "$lab_p" ".run-vms/$VM_NAME" --check)" || die "not deleting $VM_NAME: $a. Nothing was deleted"
    [ "$a" = absent ] && run=0 || run=1
  fi
  [ "$del_vault" = 0 ] || { a="$(vm_rmtree "$MC_VMS_APP_DIR" "$VM_NAME" --check)" || die "--delete-vault: $a. Nothing was deleted"; }
  if ! mc_read_default; then why+=("which VM is your default computer can't be told, so it may be this one ($MC_REFUSAL)")
  elif [ "$MC_DEFAULT_PTR" = "$VM_NAME" ]; then why+=("it's your default computer (metacomputer default NAME chooses another first)"); fi
  "$PY" "$LAB_ROOT/lib/owners.py" check >/dev/null 2>&1 || why+=("a session or a handoff is using it (metacomputer --vm $VM_NAME who)")
  if [ ${#why[@]} -gt 0 ] && [ "$force" != 1 ]; then
    a=""; for v in "${why[@]}"; do a="${a:+$a; }$v"; done
    die "not deleting $VM_NAME: $a. Add --force to delete it anyway; nothing was deleted"
  fi
  RDP_SIG="in-use=${#why[@]} run=$run record=$(shasum -a 256 "$RUN_DIR/remote.json" 2>/dev/null | cut -c1-16) settings=$(shasum -a 256 "$settings_file" 2>/dev/null | cut -c1-16)"
}
vm_delete_remote() {
  local settings_file="$LOCAL_CONFIG" own_vault="$MC_VMS_APP_DIR/$VM_NAME" lab_p v a run=0 gone=() first
  [ "$MC_SETTINGS_KIND" = vm ] || die "$VM_NAME's settings aren't a file of its own; nothing was deleted"
  lab_p="$(cd "$LAB_ROOT" && pwd -P)" || die "can't read the checkout's folder; nothing was deleted"
  if remote_made; then remote_can_ask || die "deleting $VM_NAME's instance needs $(remote_where)'s credentials ($(remote_cred_names)) in this command's environment (docs/remote.md); nothing was deleted"; fi
  [ "$remove_from_server" = 0 ] || ! remote_made || die "--remove-from-server is for your own server: $(remote_where) deletes its computer whole; nothing was deleted"
  remote_delete_plan
  printf '%s\n' "This deletes:"
  if [ "${PROVIDER:-}" = sprites ]; then printf '%s\n' "  its Sprite on Fly (${SPRITE_NAME:-$VM_NAME} in ${SPRITE_ORG:-?}): its disk (everything on it) and its checkpoints"
  elif remote_made && [ "${PROVIDER:-}" = oci ]; then printf '%s\n' "  its instance on $(remote_where) and the instance's disk (everything on it), its security group, and metacomputer's network there once no computer of yours uses it"
  elif remote_made; then printf '%s\n' "  its server on $(remote_where) and the server's disk (everything on it), its firewall and its first-boot SSH key there"
  elif [ "$remove_from_server" = 1 ]; then printf '%s\n' "  metacomputer's setup on your server at ${SSH_HOST:-?}, through your own SSH and sudo: its SSH settings (yours put back), its firewall, its sudo rules and its accounts' access (their homes and the packages stay)"
  else printf '%s\n' "  nothing on your server: $VM_NAME is your own server at ${SSH_HOST:-?}, which stays as it is (--remove-from-server takes metacomputer's setup off it)"; fi
  printf '%s\n' "  its identity, so its vault binding and every API token made for it stop working (a computer made again under this name is bound again); its own vault is locked if it's unlocked"
  printf '%s\n' "  its settings, config/vms/$VM_NAME.env, and its runtime folder"
  [ "$del_vault" = 0 ] || printf '%s\n' "  its vault folder $(mc_tilde "$own_vault"), with every secret in it"
  printf '%s\n' "It keeps:" "  its logs ($(vm_path "$LOG_DIR")) and SSH keys ($(mc_tilde "$SSH_DIR"))"
  [ "$del_vault" = 1 ] || printf '%s\n' "  the vault folder $(mc_tilde "$(mc_vault_dir)") (--delete-vault deletes a VM's own)"
  if [ -n "$confirm_name" ]; then
    [ "$confirm_name" = "$VM_NAME" ] || die "--confirm names '$confirm_name', not $VM_NAME; nothing was deleted"
  else
    [ -t 0 ] || die "deleting $VM_NAME needs its name typed: run this in a terminal, or add --confirm $VM_NAME$([ "$yes_given" = 0 ] || echo ' (--yes doesn'"'"'t count)'). Nothing was deleted"
    printf 'Type the name of the computer to delete it (%s): ' "$VM_NAME"
    IFS= read -r v || v=""
    [ "$v" = "$VM_NAME" ] || die "that isn't $VM_NAME; nothing was deleted"
  fi
  expect_guard
  ( umask 077; mkdir -p "$RUN_DIR/owners" ) 2>/dev/null
  exec 6>>"$RUN_DIR/owners/.lock" || die "can't open $VM_NAME's owners lock; nothing was deleted"
  lock_fd_take 6 "${MCOMP_LOCK_WAIT:-10}" || die "a session holds $VM_NAME's owners lock; nothing was deleted (metacomputer --vm $VM_NAME who)"
  # Everything again, under the lock: a session or handoff that started while you were confirming, a default set
  # meanwhile, or a record that changed refuses here, before anything stops.
  first="$RDP_SIG"
  remote_delete_plan
  [ "$RDP_SIG" = "$first" ] || die "$VM_NAME changed while you were confirming ($first, now $RDP_SIG); nothing was deleted. Run this again"
  events_watcher_stop
  hub_stop
  if [ "$remove_from_server" = 1 ]; then
    say "taking metacomputer's setup off $SSH_HOST (as ${SETUP_USER:-?}, with your sudo)"
    # Never through a shared connection (your ssh config's ControlMaster): the check after the removal is a new
    # process that authenticates on its own; nothing of this Mac's is forwarded (scripts/lib.sh mc_owner_ssh_opts).
    mc_owner_ssh_opts "${SSH_HOST_PORT:-22}"
    local own=(ssh -o BatchMode=yes "${MC_OWNER_OPTS[@]}" "${SETUP_USER:-}@${SSH_HOST:-}")
    "${own[@]}" 'sudo -n /opt/metacomputer/bin/remote-uninstall all' </dev/null \
      || die "it couldn't be taken off (above: your own SSH, with sudo that doesn't ask, is needed); nothing here was deleted"
    "${own[@]}" true </dev/null || die "after the removal your own account can't sign in to ${SSH_HOST:-?}: use your provider's console. Nothing here was deleted"
    ok "your own account still signs in"
    gone+=("metacomputer's setup on your server")
  fi
  remote_py identity --vm "$VM_NAME" --remove >/dev/null 2>&1 || true
  # Its identity is gone, so its vault binding refuses, and every token made for it stops at its next use (the vault
  # checks the identity then, and again while an answer streams). Its tokens are revoked now, in whatever vault it
  # uses (anyone may revoke: it only restricts); its own vault is locked too, which also closes its tunnel now,
  # whether or not --delete-vault removes the folder after.
  if [ -S "$(mc_vault_dir)/vault.sock" ] && [ -x "$VAULT_BIN" ]; then
    MCOMP_VAULT_DIR="$(mc_vault_dir)" "$VAULT_BIN" revoke --vm "$VM_NAME" >/dev/null 2>&1 || true
  fi
  if [ "$(mc_vault_dir)" = "$own_vault" ] && [ -S "$own_vault/vault.sock" ] && [ -x "$VAULT_BIN" ]; then
    MCOMP_VAULT_DIR="$own_vault" "$VAULT_BIN" lock >/dev/null 2>&1 && gone+=("its vault's live tokens and tunnel (the vault is locked)")
  fi
  if remote_made; then
    local had=0
    "$PY" -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])).get("instance") else 1)' "$RUN_DIR/remote.json" 2>/dev/null && had=1
    say "deleting what $(remote_where) has for $VM_NAME"
    remote_py delete --vm "$VM_NAME" --network-too $checked_provider || die "it wasn't deleted at $(remote_where) (above); its settings stay, so you can try again"
    [ "$had" = 0 ] || gone+=("its $([ "${PROVIDER:-}" = sprites ] && echo Sprite || echo instance) on $(remote_where)")
  fi
  rm -f "$settings_file" && gone+=("config/vms/$VM_NAME.env")
  if [ "$run" = 1 ]; then a="$(vm_rmtree "$lab_p" ".run-vms/$VM_NAME")" || warn "kept its runtime folder: $a"; fi
  if [ "$del_vault" = 1 ]; then
    [ ! -S "$own_vault/vault.sock" ] || [ ! -x "$VAULT_BIN" ] || MCOMP_VAULT_DIR="$own_vault" "$VAULT_BIN" lock >/dev/null 2>&1 || true
    if a="$(vm_rmtree "$MC_VMS_APP_DIR" "$VM_NAME")"; then [ "$a" = absent ] || gone+=("the vault folder $(mc_tilde "$own_vault")")
    else warn "kept the vault folder: $a"; fi
  fi
  emit_event lifecycle vm_deleted info "The computer $VM_NAME was deleted" "" "name=$VM_NAME"
  local list=""; for v in ${gone[@]+"${gone[@]}"}; do list="${list:+$list, }$v"; done
  ok "deleted: ${list:-nothing}"
  if ! remote_made && [ "$remove_from_server" = 0 ]; then
    printf '%s\n' "Your server still has metacomputer's setup: its accounts (cua, cuaadmin, mcfill, mcproxy), its desktop, its" \
      "SSH settings and its firewall, and its keys here still sign in to it. To take it off: on the server, with sudo," \
      "/opt/metacomputer/bin/remote-uninstall all (it puts your SSH settings back and turns the firewall off); or delete" \
      "$(mc_tilde "$SSH_DIR") here so this Mac can't sign in."
  fi
  return 0
}

# Every settings file of this checkout but this VM's own, as identity records (scripts/lib.sh mc_identity_records):
# the main VM's (unless this is it) and every other config/vms/ VM's.
vm_other_identities() {
  local n
  case "$MC_SETTINGS_KIND" in local|defaults) ;; *) mc_identity_records "the main VM's settings (config/local.env)" main ;; esac
  for n in $(mc_vm_files); do
    [ "$MC_SETTINGS_KIND" = vm ] && [ "$n" = "$VM_NAME" ] && continue
    mc_identity_records "config/vms/$n.env" vm "$n"
  done
}
# BASE REL [--check]: this VM's own folder BASE/REL deleted through lib/safedelete.py (`own`): never through a link,
# and checked against the same protected folders as every other delete (every VM's, the account's own vault and
# spools, the home folder, the checkout) less this VM's own. Prints present|absent, or the refusal.
vm_rmtree() {
  "$PY" -B "$LAB_ROOT/lib/safedelete.py" own --base "$1" --rel "$2" --target-vm "$VM_NAME" \
    --own "$SSH_DIR" --own "$RUN_DIR" --own "$LOG_DIR" --own "$(mc_vault_dir)" --own "${EVENTS_SPOOL:-}" ${3:+"$3"}
}
# Everything vm delete checks, before the prompt and again after it (under the owners lock): what exists (DP_*), that
# nothing it would delete is another VM's, that its folders can go, its volumes, and that it isn't in use (without
# --force). Dies with the reason; DP_SIG sums up what was found, to compare. Uses vm_delete's flags.
vm_delete_plan() {
  local out facts line w why=() a
  DP_VM=absent; DP_STATE=absent; DP_VM_BYTES=""; DP_BASE=absent; DP_BASE_BYTES=""; DP_PREV=absent; DP_PREV_BYTES=""
  DP_CKPTS=""; DP_VOLS=""; DP_RUN=0
  out="$("$LUME" ls --format json 2>/dev/null)" || die "couldn't list the VMs with $LUME, so it can't tell what exists; nothing was deleted"
  facts="$(printf '%s' "$out" | "$PY" "$LAB_ROOT/lib/vms.py" facts "$VM_NAME" "$BASELINE_NAME")" \
    || die "couldn't read $LUME's list of VMs; nothing was deleted"
  while IFS= read -r line; do
    case "$line" in
      vm=*) DP_VM="${line#*=}" ;; vm_state=*) DP_STATE="${line#*=}" ;; vm_bytes=*) DP_VM_BYTES="${line#*=}" ;;
      base=*) DP_BASE="${line#*=}" ;; base_bytes=*) DP_BASE_BYTES="${line#*=}" ;;
      prev=*) DP_PREV="${line#*=}" ;; prev_bytes=*) DP_PREV_BYTES="${line#*=}" ;; ckpts=*) DP_CKPTS="${line#*=}" ;;
    esac
  done <<< "$facts"
  [ -z "$DP_CKPTS" ] || die "$VM_NAME has checkpoints ($DP_CKPTS); delete them first (metacomputer --vm $VM_NAME checkpoint --delete NAME), or keep the VM. Nothing was deleted"
  if [ "$DP_VM" = absent ] && [ "$DP_BASE" = absent ] && [ "$DP_PREV" = absent ] && [ -z "$settings_file" ]; then
    die "there's no VM $VM_NAME, no baseline $BASELINE_NAME and no settings file of its own to delete"
  fi
  # Every delete, whichever VM this is (the main one included) and with or without --delete-vault/--delete-volumes:
  # first, every other settings file must read cleanly, the strict way every command reads them (what can't be known
  # can't be kept safe), and the identity rule must hold (scripts/lib.sh mc_identity_check): no folder of this VM's
  # is another's, is in one or holds one, and no VM's folder is in the runner's VM storage, which the runner's delete
  # below reaches. Ports don't count here: deleting a VM ends a clash.
  w="$(vm_other_identities | "$PY" "$LAB_ROOT/lib/vms.py" all-known)" \
    || die "not deleting $VM_NAME: ${w:-another settings file} can't be read cleanly, so its folders aren't known and none could be kept safe; fix it first. Nothing was deleted"
  mc_identity_check --no-ports || die "not deleting $VM_NAME: $MC_REFUSAL. Nothing was deleted"
  # Nothing it would delete may be another VM's: not a VM, baseline, previous baseline or checkpoint of any VM this
  # checkout has settings for (config/local.env, config/vms/*).
  DP_ROWS="$(vm_all_settings)"
  w="$(printf '%s\n' "$DP_ROWS" | "$PY" "$LAB_ROOT/lib/vms.py" delete-check --target "$VM_NAME" --baseline "$BASELINE_NAME")" \
    || die "not deleting $VM_NAME: $w"
  # Its runtime folder (the default one in this checkout) and its vault folder (--delete-vault): only its own,
  # reached without links, holding no other VM's folder.
  if [ -n "$settings_file" ] && [ "$RUN_DIR" = "$LAB_ROOT/.run-vms/$VM_NAME" ]; then
    w="$(vm_rmtree "$lab_p" ".run-vms/$VM_NAME" --check)" || die "not deleting $VM_NAME: $w"
    [ "$w" = absent ] || DP_RUN=1
  fi
  if [ "$del_vault" = 1 ]; then
    [ "$(mc_vault_dir)" = "$own_vault" ] \
      || die "--delete-vault deletes only a VM's own vault folder ($(mc_tilde "$own_vault")); $VM_NAME's vault is $(mc_tilde "$(mc_vault_dir)"), which may be shared: delete it yourself if you mean to. Nothing was deleted"
    w="$(vm_rmtree "$MC_VMS_APP_DIR" "$VM_NAME" --check)" || die "--delete-vault: $w. Nothing was deleted"
  fi
  if [ "$del_vols" = 1 ]; then
    DP_VOLS="$(printf '%s' "${VOLUMES:-}" | tr ',' '\n' | sed 's/:.*//; s/^ *//; s/ *$//' | grep -v '^$' | tr '\n' ' ' || true)"
    # Every other settings file counts, refused ones by the VOLUMES they say; one whose volumes can't be read refuses.
    w="$(printf '%s\n' "$DP_ROWS" | "$PY" -c 'import json, sys
me, mine = sys.argv[1], set(sys.argv[2].split())
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("ok") and d.get("vm") == me:
        continue
    if not d.get("ok") and not d.get("known"):
        print("UNKNOWN " + str(d.get("file")))
        continue
    shared = mine & {e.split(":")[0].strip() for e in (d.get("volumes") or "").split(",") if e.strip()}
    if shared: print((d.get("vm") or str(d.get("file"))) + ": " + " ".join(sorted(shared)))' "$VM_NAME" "$DP_VOLS")"
    case "$w" in *UNKNOWN*) die "--delete-volumes: $(printf '%s' "$w" | sed -n 's/^UNKNOWN //p' | head -1) can't be read, so it isn't known which volumes it attaches; nothing was deleted" ;; esac
    [ -z "$w" ] || die "--delete-volumes: other VMs here attach some of the same volumes ($w); delete those yourself with metacomputer volume delete NAME. Nothing was deleted"
  fi
  # Never the VM in use without --force: the one this checkout's settings choose, one that runs, or one a session holds.
  [ "$(mc_main_identity | head -1)" != "$VM_NAME" ] || why+=("it's the VM this checkout uses without --vm ($([ -f "$MC_MAIN_CONFIG" ] && echo config/local.env || echo the defaults))")
  # ...and your default computer: commands that name no VM act on it (config/default-vm, whatever this command read).
  # A pointer that can't be read could be naming this VM: that counts as in use too, with the reader's reason.
  if ! mc_read_default; then
    why+=("which VM is your default computer can't be told, so it may be this one ($MC_REFUSAL; metacomputer default shows it)")
  elif [ "$MC_DEFAULT_PTR" = "$VM_NAME" ]; then
    why+=("it's your default computer (config/default-vm; metacomputer default NAME chooses another first)")
  fi
  [ "$DP_STATE" != running ] || why+=("it's running")
  "$PY" "$LAB_ROOT/lib/owners.py" check >/dev/null 2>&1 || why+=("a session is using it (metacomputer --vm $VM_NAME who)")
  if [ ${#why[@]} -gt 0 ] && [ "$force" != 1 ]; then
    w=""; for a in "${why[@]}"; do w="${w:+$w; }$a"; done
    die "not deleting $VM_NAME: $w. Add --force to delete it anyway; nothing was deleted"
  fi
  DP_SIG="vm=$DP_VM/$DP_STATE base=$DP_BASE prev=$DP_PREV in-use=${#why[@]}"
}
vm_path() { case "$1" in "$LAB_ROOT"/*) printf '%s' "${1#"$LAB_ROOT"/}" ;; *) mc_tilde "$1" ;; esac; }
vm_human() { "$PY" -c 'import sys
try: n = int(sys.argv[1])
except ValueError: print("size unknown"); sys.exit()
print(f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB")' "${1:-x}"; }
