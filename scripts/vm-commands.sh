#!/bin/bash
# `metacomputer vm list | create | delete`: metacomputer's VMs on this Mac, a VM of its own in one command, and
# deleting one safely. Sourced by bin/metacomputer (its functions and the settings scripts/lib.sh loaded are in
# scope); macOS /bin/bash 3.2. Settings: one file per VM, config/vms/NAME.env (scripts/lib.sh, "which VM").

# Settings that are the Mac's choices rather than one VM's: `vm create` copies the ones these settings set
# differently from config/defaults.env into the new VM's file (it doesn't read config/local.env later on).
# Never copied: what makes a VM itself (names, ports, keys, folders, vault), its storage (VOLUMES, REFDISKS: a
# volume is read-write in one VM at a time), its live-view password store (a password command would be shared),
# recordings and the skill's defaults.
VM_COPY_KEYS="LUME VM_CPU VM_MEMORY VM_DISK_BYTES VM_DISPLAY BROWSERS EXTRA_PACKAGES VIRTUAL_AUDIO
SHARE_CLIPBOARD SHARE_AUDIO SHARE_ROSETTA STORAGE_DIR ACTION_LOG ACTION_LOG_TEXT ACTION_LOG_KEY SSH_AUTH SSH_CERT_TTL
SSH_CA_KEY DRIVER_MODE DRIVER_PROFILE DRIVER_ORIGINS DRIVER_MANIFEST DRIVER_EXPIRES DRIVER_IDLE VM_SHELL SHELL_LOG
MCP_HUB COORD_GUARD"

do_vm() { # list [--json] | create NAME ... | delete NAME ...
  local sub="${1:-list}"; [ $# -gt 0 ] && shift
  case "$sub" in
    list)   vm_list "$@" ;;
    create) vm_create "$@" ;;
    delete) vm_delete "$@" ;;
    help|-h|--help) sed -n '/^#   --vm NAME/,/^#   start /p' "$SELF" | sed '$d' | sed 's/^# \{0,1\}//' ;;
    *) die "usage: metacomputer vm list [--json] | create NAME [--ssh-port P --vnc-port P] [--no-build] [--yes] | delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes]" ;;
  esac
}

# NAME CONFIG LABEL: one JSON line for the settings NAME / CONFIG select, resolved as every command resolves them
# (in a subshell: this command's own settings stay as they are), or the reason they're refused.
vm_settings_row() {
  ( if mc_load_settings "$1" "$2" >/dev/null 2>&1; then
      "$PY" -c 'import json, sys
k = ["file", "kind", "vm", "baseline", "ssh_port", "vnc_port", "vault", "spool", "volumes", "ssh_dir", "run_dir", "log_dir"]
print(json.dumps(dict(zip(k, sys.argv[1:]), ok=True)))' \
        "$MC_SETTINGS_LABEL" "$MC_SETTINGS_KIND" "$VM_NAME" "$BASELINE_NAME" "$SSH_PORT" "$VNC_PORT" "$(mc_vault_dir)" \
        "${EVENTS_SPOOL:-}" "${VOLUMES:-}" "$SSH_DIR" "${RUN_DIR:-$LAB_ROOT/.run}" "${LOG_DIR:-$LAB_ROOT/logs}"
    else
      "$PY" -c 'import json, sys; print(json.dumps({"file": sys.argv[1], "vm": sys.argv[2], "ok": False, "error": sys.argv[3]}))' \
        "$3" "${4:-}" "$MC_REFUSAL"
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

vm_list() { # [--json]
  local json=() a tmp rc=0 sel="$VM_NAME"
  for a in "$@"; do case "$a" in --json) json=(--json) ;; *) die "usage: metacomputer vm list [--json]" ;; esac; done
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/mcomp-vms.XXXXXX")" || die "couldn't make a temporary folder"
  vm_all_settings > "$tmp/settings.jsonl"
  local runner=(--runner-failed)
  if command -v "$LUME" >/dev/null 2>&1 && "$LUME" ls --format json > "$tmp/runner.json" 2>/dev/null; then runner=(--runner "$tmp/runner.json"); fi
  # With no settings file and no --vm, nothing is chosen when there's more than one VM to choose from.
  if [ "${MC_IMPLICIT:-0}" = 1 ]; then
    if [ -n "$(mc_vm_files)" ] || [ "${runner[0]}" = --runner-failed ] || [ -n "$(vm_working_names "$VM_NAME" 2>/dev/null)" ]; then sel=""; fi
  fi
  "$PY" "$LAB_ROOT/lib/vms.py" list --settings "$tmp/settings.jsonl" "${runner[@]}" --selected "$sel" ${json[@]+"${json[@]}"} || rc=$?
  rm -rf "$tmp"
  return $rc
}

vm_port_in_use() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

vm_create() { # NAME [--ssh-port P] [--vnc-port P] [--no-build] [--yes]
  local name="" ssh="" vnc="" build=1 a used p f vault spool rows k v def tmp rc
  while [ $# -gt 0 ]; do
    case "$1" in
      --ssh-port) [ $# -ge 2 ] || die "--ssh-port needs a port"; ssh="$2"; shift ;;
      --vnc-port) [ $# -ge 2 ] || die "--vnc-port needs a port"; vnc="$2"; shift ;;
      --ssh-port=*) ssh="${1#*=}" ;;
      --vnc-port=*) vnc="${1#*=}" ;;
      --no-build) build=0 ;;
      --yes|-y) ASSUME_YES=1 ;;
      -*) die "unknown option $1 (metacomputer vm create NAME [--ssh-port P --vnc-port P] [--no-build] [--yes])" ;;
      *) [ -z "$name" ] || die "one VM name, please (got '$name' and '$1')"; name="$1" ;;
    esac
    shift
  done
  [ -n "$name" ] || die "usage: metacomputer vm create NAME [--ssh-port P --vnc-port P] [--no-build] [--yes]"
  [[ "$name" =~ ^[a-z0-9][a-z0-9-]{0,23}$ ]] && [[ ! "$name" =~ (-baseline|-baseline-prev|-prev|-)$|-ckpt- ]] \
    || die "a new VM's name: up to 24 lowercase letters, digits and dashes, starting with a letter or digit, not ending in -baseline, -prev or a dash, and without -ckpt-"
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
  command -v "$LUME" >/dev/null 2>&1 || die "the VM runner $LUME isn't installed (./install.sh installs it)"
  for v in "$name" "$name-baseline"; do
    a="$(vm_lookup "$v")" || die "couldn't list the VMs with $LUME, so it can't tell whether $v exists; nothing was done"
    [ "$a" = absent ] || die "the runner has a VM named $v already; vm create makes new VMs only (metacomputer vm list)"
  done
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
  vault="$MC_APP_DIR/vms/$name"; spool="$vault/events/spool"
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
      "# config/local.env isn't read for this VM. Bash syntax. Its own names, ports, keys, folders and vault:" \
      "VM_NAME=$name" "BASELINE_NAME=$name-baseline" "SSH_PORT=$ssh" "VNC_PORT=$vnc"
    vm_q SSH_DIR "$HOME/.ssh/metacomputer-$name"
    vm_q VAULT_DIR "$vault"
    vm_q EVENTS_SPOOL "$spool"
    printf '%s\n' "# RUN_DIR and LOG_DIR: empty = .run/vms/$name and logs/vms/$name in this checkout."
    def="$( . "$LAB_ROOT/config/defaults.env"; for k in $VM_COPY_KEYS; do eval "printf '%s=%s\n' \"\$k\" \"\${$k:-}\""; done )"
    a=0
    for k in $VM_COPY_KEYS; do
      eval "v=\${$k:-}"
      [ "$k" != LUME ] || v="${LUME_CONFIGURED:-$LUME}"   # as configured, not the lume-secure fallback
      [ "$k=$v" != "$(printf '%s\n' "$def" | grep -m1 "^$k=" || true)" ] || continue
      case "$k" in ACTION_LOG_KEY|SSH_CA_KEY|DRIVER_MANIFEST|STORAGE_DIR) [ -n "$v" ] || continue ;; esac
      [ "$a" = 1 ] || { printf '%s\n' "# Copied from $MC_SETTINGS_LABEL when this file was made (they differ from the defaults):"; a=1; }
      vm_q "$k" "$v"
    done
    # A live-view password in the Keychain is per VM (its account is the VM's name); a password command isn't.
    [ "${VNC_PASSWORD_STORE:-file}" != keychain ] || printf '%s\n' "VNC_PASSWORD_STORE=keychain"
  } > "$tmp" || { rm -f "$tmp"; die "couldn't write $f"; }
  chmod 600 "$tmp" && mv "$tmp" "$f" || { rm -f "$tmp"; die "couldn't write $f"; }
  MCOMP_VM_NOTICE=1
  printf '%s\n' "metacomputer: new VM $name: config/vms/$name.env, SSH 127.0.0.1:$ssh, live view 127.0.0.1:$vnc, vault $(mc_tilde "$vault")" >&2
  emit_event lifecycle vm_created info "Settings for a new VM $name were made (config/vms/$name.env)" "metacomputer --vm $name build" "name=$name"
  if [ "$build" = 0 ]; then
    ok "settings written; build it with: metacomputer --vm $name build"
    return 0
  fi
  if [ "${ASSUME_YES:-0}" != 1 ]; then
    printf 'Build the VM %s now? About 1.5 GB of downloads the first time (kept for later builds), then a few minutes. [y/N] ' "$name"
    read -r a
    case "$a" in y|Y) ;; *) ok "not built. Its settings are in config/vms/$name.env: metacomputer --vm $name build builds it, metacomputer vm delete $name removes them"; return 0 ;; esac
  fi
  rc=0
  env -u MCOMP_CONFIG -u MCOMP_VM -u MCOMP_VM_NOTICE "$SELF" --vm "$name" build || rc=$?
  if [ "$rc" = 0 ]; then
    say "the VM $name is built. Next:"
    printf '%s\n' "  metacomputer --vm $name start" \
      "  metacomputer --vm $name secrets init        (its own vault, in your own terminal; then secrets bind)" \
      "  MCOMP_VM=$name claude                      (a Claude Code session that uses it)"
  else
    warn "the build of $name failed (above). Its settings stay in config/vms/$name.env: metacomputer --vm $name build --replace tries again; metacomputer vm delete $name removes it all"
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
  local name="" confirm_name="" force=0 del_vault=0 del_vols=0 yes_given=0 a args=("$@")
  while [ $# -gt 0 ]; do
    case "$1" in
      --confirm) [ $# -ge 2 ] || die "--confirm needs the VM's name"; confirm_name="$2"; shift ;;
      --confirm=*) confirm_name="${1#*=}" ;;
      --force) force=1 ;;
      --delete-vault) del_vault=1 ;;
      --delete-volumes) del_vols=1 ;;
      --yes|-y) yes_given=1 ;;
      -*) die "unknown option $1 (metacomputer vm delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes])" ;;
      *) [ -z "$name" ] || die "one VM at a time (got '$name' and '$1')"; name="$1" ;;
    esac
    shift
  done
  [ -n "$name" ] || die "usage: metacomputer vm delete NAME [--confirm NAME] [--force] [--delete-vault] [--delete-volumes]"
  mc_valid_vm_name "$name" || die "'$name' isn't a VM name"
  if [ "$VM_NAME" != "$name" ] || [ "${MC_IMPLICIT:-0}" = 1 ]; then
    [ -z "${MC_VM_DELETE_HOP:-}" ] || die "the settings for $name name the VM $VM_NAME; nothing was deleted"
    local main_vm=""; [ -f "$MC_MAIN_CONFIG" ] && main_vm="$(mc_file_vm_name "$MC_MAIN_CONFIG")"
    if [ ! -f "$MC_VMS_DIR/$name.env" ] && [ "$main_vm" != "$name" ] && { [ -f "$MC_MAIN_CONFIG" ] || [ "$name" != "$MC_DEFAULT_VM" ]; }; then
      die "this checkout has no settings for a VM named $name, so it doesn't know its baseline, keys or folders; nothing was deleted. metacomputer vm list shows the VMs it has settings for (a VM made with another settings file: MCOMP_CONFIG=FILE metacomputer vm delete $name)"
    fi
    exec env -u MCOMP_CONFIG -u MCOMP_VM -u MCOMP_VM_NOTICE MC_VM_DELETE_HOP=1 "$SELF" --vm "$name" vm delete ${args[@]+"${args[@]}"}
  fi
  vm_notice
  vm_lock_or_die "metacomputer vm delete $VM_NAME"
  command -v "$LUME" >/dev/null 2>&1 || die "the VM runner $LUME isn't installed; nothing was deleted"
  local out facts line vm=absent vm_state=absent vm_bytes="" base=absent base_bytes="" prev=absent prev_bytes="" ckpts=""
  out="$("$LUME" ls --format json 2>/dev/null)" || die "couldn't list the VMs with $LUME, so it can't tell what exists; nothing was deleted"
  facts="$(printf '%s' "$out" | "$PY" "$LAB_ROOT/lib/vms.py" facts "$VM_NAME" "$BASELINE_NAME")" \
    || die "couldn't read $LUME's list of VMs; nothing was deleted"
  while IFS= read -r line; do
    case "$line" in
      vm=*) vm="${line#*=}" ;; vm_state=*) vm_state="${line#*=}" ;; vm_bytes=*) vm_bytes="${line#*=}" ;;
      base=*) base="${line#*=}" ;; base_bytes=*) base_bytes="${line#*=}" ;;
      prev=*) prev="${line#*=}" ;; prev_bytes=*) prev_bytes="${line#*=}" ;; ckpts=*) ckpts="${line#*=}" ;;
    esac
  done <<< "$facts"
  [ -z "$ckpts" ] || die "$VM_NAME has checkpoints ($ckpts); delete them first (metacomputer --vm $VM_NAME checkpoint --delete NAME), or keep the VM. Nothing was deleted"
  local settings_file=""; [ "$MC_SETTINGS_KIND" = vm ] && settings_file="$LOCAL_CONFIG"
  if [ "$vm" = absent ] && [ "$base" = absent ] && [ "$prev" = absent ] && [ -z "$settings_file" ]; then
    die "there's no VM $VM_NAME, no baseline $BASELINE_NAME and no settings file of its own to delete"
  fi
  # What the flags ask for must be possible before anything goes.
  local own_vault="$MC_APP_DIR/vms/$VM_NAME"
  if [ "$del_vault" = 1 ]; then
    [ "$(mc_vault_dir)" = "$own_vault" ] \
      || die "--delete-vault deletes only a VM's own vault folder ($(mc_tilde "$own_vault")); $VM_NAME's vault is $(mc_tilde "$(mc_vault_dir)"), which may be shared: delete it yourself if you mean to. Nothing was deleted"
    [ ! -L "$own_vault" ] || die "$own_vault is a link; nothing was deleted"
  fi
  local vols="" v others
  if [ "$del_vols" = 1 ]; then
    vols="$(printf '%s' "${VOLUMES:-}" | tr ',' '\n' | sed 's/:.*//; s/^ *//; s/ *$//' | grep -v '^$' | tr '\n' ' ' || true)"
    others="$(vm_all_settings | "$PY" -c 'import json, sys
me, mine = sys.argv[1], set(sys.argv[2].split())
for line in sys.stdin:
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("ok") and d.get("vm") != me:
        shared = mine & {e.split(":")[0].strip() for e in (d.get("volumes") or "").split(",") if e.strip()}
        if shared: print(d.get("vm") + ": " + " ".join(sorted(shared)))' "$VM_NAME" "$vols")"
    [ -z "$others" ] || die "--delete-volumes: other VMs here attach some of the same volumes ($others); delete those yourself with metacomputer volume delete NAME. Nothing was deleted"
  fi
  # Never the VM in use without --force: the one this checkout's settings choose, one that runs, or one a session holds.
  local why=() main_id
  main_id="$(mc_main_identity | head -1)"
  [ "$main_id" != "$VM_NAME" ] || why+=("it's the VM this checkout uses without --vm ($([ -f "$MC_MAIN_CONFIG" ] && echo config/local.env || echo the defaults))")
  [ "$vm_state" != running ] || why+=("it's running")
  "$PY" "$LAB_ROOT/lib/owners.py" check >/dev/null 2>&1 || why+=("a session is using it (metacomputer --vm $VM_NAME who)")
  if [ ${#why[@]} -gt 0 ] && [ "$force" != 1 ]; then
    local w=""; for a in "${why[@]}"; do w="${w:+$w; }$a"; done
    die "not deleting $VM_NAME: $w. Add --force to delete it anyway; nothing was deleted"
  fi
  # The plan, then the name typed: never --yes.
  printf '%s\n' "This deletes:"
  [ "$vm" = absent ] || printf '%s\n' "  the VM $VM_NAME ($vm_state, $(vm_human "$vm_bytes") on disk)"
  [ "$base" = absent ] || printf '%s\n' "  its baseline $BASELINE_NAME ($(vm_human "$base_bytes"))"
  [ "$prev" = absent ] || printf '%s\n' "  its previous baseline $BASELINE_NAME-prev ($(vm_human "$prev_bytes"))"
  [ -z "$settings_file" ] || printf '%s\n' "  its settings, config/vms/$VM_NAME.env, and its runtime folder"
  [ "$del_vault" = 0 ] || printf '%s\n' "  its vault folder $(mc_tilde "$own_vault"), with every secret in it"
  [ -z "$vols" ] || printf '%s\n' "  its volumes: $vols(each asks again)"
  printf '%s\n' "It keeps:"
  [ "$del_vault" = 1 ] || printf '%s\n' "  the vault folder $(mc_tilde "$(mc_vault_dir)") (--delete-vault deletes a VM's own)"
  [ "$del_vols" = 1 ] || [ -z "${VOLUMES:-}" ] || printf '%s\n' "  its volumes, $VOLUMES (--delete-volumes)"
  printf '%s\n' "  its logs ($(vm_path "$LOG_DIR")) and SSH keys ($(mc_tilde "$SSH_DIR"))"
  [ -n "$settings_file" ] || printf '%s\n' "  its settings file, $MC_SETTINGS_LABEL"
  if [ -n "$confirm_name" ]; then
    [ "$confirm_name" = "$VM_NAME" ] || die "--confirm names '$confirm_name', not $VM_NAME; nothing was deleted"
  else
    [ -t 0 ] || die "deleting a VM needs its name typed: run this in a terminal, or add --confirm $VM_NAME$([ "$yes_given" = 0 ] || echo ' (--yes doesn'"'"'t count)'). Nothing was deleted"
    printf 'Type the name of the VM to delete it (%s): ' "$VM_NAME"
    IFS= read -r a || a=""
    [ "$a" = "$VM_NAME" ] || die "that isn't $VM_NAME; nothing was deleted"
  fi
  expect_guard
  # Stop what runs for it (the gateway, the hub, the event watcher), and the VM itself: it's going anyway.
  events_watcher_stop
  hub_stop
  if [ "$vm_state" = running ]; then
    say "stopping $VM_NAME (forced: it's being deleted)"
    vm_stop "$VM_NAME" --force
  fi
  netgw_stop
  storage_release --quiet
  local gone=()
  for v in "$VM_NAME" "$BASELINE_NAME" "$BASELINE_NAME-prev"; do
    a="$(vm_lookup "$v")" || die "couldn't list the VMs with $LUME before deleting $v; deleted so far: ${gone[*]:-nothing}"
    [ "$a" = present ] || continue
    "$LUME" delete "$v" --force >/dev/null || die "couldn't delete $v; deleted so far: ${gone[*]:-nothing}"
    gone+=("$v")
  done
  if [ -n "$settings_file" ]; then
    rm -f "$settings_file" && gone+=("config/vms/$VM_NAME.env")
    if [ "$RUN_DIR" = "$LAB_ROOT/.run/vms/$VM_NAME" ] && [ -d "$RUN_DIR" ] && [ ! -L "$RUN_DIR" ]; then rm -rf "$RUN_DIR"; fi
  fi
  if [ "$del_vault" = 1 ] && [ -e "$own_vault" ]; then
    # End an unlocked vault's process first (anyone may lock: it only restricts), then remove the folder.
    [ ! -S "$own_vault/vault.sock" ] || [ ! -x "$VAULT_BIN" ] || MCOMP_VAULT_DIR="$own_vault" "$VAULT_BIN" lock >/dev/null 2>&1 || true
    rm -rf "$own_vault" && gone+=("the vault folder $(mc_tilde "$own_vault")")
  fi
  emit_event lifecycle vm_deleted info "The VM $VM_NAME was deleted" "" "name=$VM_NAME" "baseline=$BASELINE_NAME"
  local list=""; for v in ${gone[@]+"${gone[@]}"}; do list="${list:+$list, }$v"; done
  ok "deleted: ${list:-nothing}"
  for v in $vols; do
    say "volume $v:"
    storage_py volume delete "$v" || warn "volume $v was kept (above)"
  done
  return 0
}
vm_path() { case "$1" in "$LAB_ROOT"/*) printf '%s' "${1#"$LAB_ROOT"/}" ;; *) mc_tilde "$1" ;; esac; }
vm_human() { "$PY" -c 'import sys
try: n = int(sys.argv[1])
except ValueError: print("size unknown"); sys.exit()
print(f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB")' "${1:-x}"; }
