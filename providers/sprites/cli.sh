# Fly Sprites' own steps in metacomputer's commands (bin/metacomputer, scripts/vm-commands.sh, scripts/lib.sh): each
# pv_sprites_HOOK runs where the shared code finds the hook (scripts/lib.sh mc_pv_has / mc_pv_run) instead of its own
# step, in the same shell, with that command's variables and functions. Sourced (scripts/lib.sh mc_pv_load), never run.

pv_sprites_cli_path() { # SPRITE_CLI, else sprite on PATH, else ~/.local/bin/sprite (providers/sprites/adapter.py default_cli)
  if [ -n "${SPRITE_CLI:-}" ]; then printf '%s' "${SPRITE_CLI/#\~/$HOME}"
  elif command -v sprite >/dev/null 2>&1; then command -v sprite
  else printf '%s' "$HOME/.local/bin/sprite"; fi
}
pv_sprites_can_ask() { [ -x "$(pv_sprites_cli_path)" ]; }   # its sprite command, which keeps its own sign-in

# vm create --on sprites (scripts/vm-commands.sh vm_create: its variables).
pv_sprites_vm_create() {
  [ -z "$host$huser$hport$size$idle$region" ] || die "--host, --user, --port, --size, --region and --idle-stop aren't for a Sprite: Fly sizes it and chooses where it runs, and it sleeps by itself about 10 s after nothing is connected"
  [ -n "$opt_org" ] || die "a Sprite is made in one of your Fly organizations: --org ORG (or SPRITES_ORG in config/local.env)"
  opt_sprite="${opt_sprite:-$name}"
  a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import cloud; sprites = cloud.registry.module("sprites")
try: sprites.org_of({"SPRITE_ORG": sys.argv[2]}); print(sprites.sprite_of({"SPRITE_NAME": sys.argv[3]}))
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$opt_org" "$opt_sprite")" || die "$a"
  remote_can_ask_for sprites || die "Fly's sprite command isn't installed: install it and sign in (sprite login), then run this again (docs/sprites.md)"
  rlines=("SPRITE_ORG=$opt_org" "SPRITE_NAME=$opt_sprite" "IDLE_STOP=off")
  [ -z "${SPRITE_CLI:-}" ] || rlines+=("SPRITE_CLI=$(pv_sprites_cli_path)")
}
pv_sprites_vm_said() { echo "Fly Sprites, the Sprite $opt_sprite in $opt_org"; }
pv_sprites_vm_billing() {
  # Something billed: the rate and what it keeps, before anything is made (providers/sprites/adapter.py rates).
  a="$(MCOMP_VM="$name" remote_py rates --vm "$name" 2>/dev/null || true)"
  printf '%s\n' "Fly Sprites: ${a:-rates unknown}" "PERSIST=disk: its disk keeps everything while it sleeps. It sleeps by itself about 10 s after nothing is connected (the hub lets go after HUB_IDLE unused); asleep, only its disk is billed. Its network policy is EGRESS=${egress:-internet}, enforced by Fly outside the computer (docs/sprites.md)." >&2
}
pv_sprites_vm_delete_what() { printf '%s\n' "  its Sprite on Fly (${SPRITE_NAME:-$VM_NAME} in ${SPRITE_ORG:-?}): its disk (everything on it) and its checkpoints"; }

# stop (bin/metacomputer do_stop_remote, after the hub and a recording are closed).
pv_sprites_stop() {
  # Nothing to send: a Sprite pauses by itself about 10 s after its last connection closes. This Mac's are closed
  # here: the hub (above), a recording (above), the live view's tunnel, and the vault's (its API proxy's tunnel).
  # Every tunnel runs through a wrapper that ends Fly's proxy when ssh goes; then this checks none is left.
  view_tunnel_close
  vault_disconnect
  remote_py sleep --vm "$VM_NAME" >/dev/null 2>&1 || true
  local left; left="$(pv_sprites_tunnels_left)"
  if [ -n "$left" ]; then
    warn "$left connection(s) of this Mac's to $VM_NAME are still open (ps: sprite proxy -W for ${SPRITE_NAME:-$VM_NAME}): it stays awake, and billing, until they close"
    emit_event lifecycle stopped warning "The computer $VM_NAME was left to sleep, but $left connection(s) keep it awake" "" "forced=no"
    return 1
  fi
  emit_event lifecycle stopped info "The computer $VM_NAME was left to sleep" "" "forced=no"
  ok "this Mac's connections to $VM_NAME are closed (the hub's, the live view's, the vault's): Fly pauses it about 10 s later (asleep, only its disk is billed); the next command wakes it"
  return 0
}
# net egress internet|off (bin/metacomputer remote_net_egress, after its owners and handoff checks).
pv_sprites_net_egress() {
  local want="$1" got
  # Fly's network policy is the boundary (outside the VM): set, read back, then the setting.
  remote_can_ask || die "Fly's sprite command isn't installed (docs/sprites.md); nothing was changed"
  got="$(remote_py egress --vm "$VM_NAME" --set "$want")" || die "Fly's network policy wasn't changed (above); nothing else was"
  [ "$got" = "$want" ] || die "Fly's network policy reads back as '$got', not $want; the setting wasn't changed"
  ok "Fly's network policy: EGRESS=$want (in force now, outside the computer)"
  browser_set EGRESS "$want"
  ok "EGRESS=$want in $MC_SETTINGS_LABEL"
  return 0
}
# What its network policy lets out, read from Fly (the setting and what's in force, apart): net status.
pv_sprites_net_status() {
  local j
  remote_can_ask || { printf '%s\n' "network of $VM_NAME:" "  setting (in $MC_SETTINGS_LABEL)  EGRESS=${EGRESS:-internet}" "  Fly's network policy  not asked (the sprite command isn't installed)"; return 0; }
  j="$(remote_py allow --vm "$VM_NAME" --show 2>/dev/null)" || die "couldn't read $VM_NAME's network policy from Fly (above)"
  "$PY" - "$j" "${EGRESS:-internet}" "$VM_NAME" "$MC_SETTINGS_LABEL" <<'PYEOF'
import json, sys
d, setting, vm, label = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
kind, names = d.get("outside") or "unknown", d.get("allow") or []
words = {"internet": "every name (raw IP addresses, private ranges and the metadata service refused)",
         "off": "no name: nothing goes out", "none": "NOTHING ENFORCED (no rules): metacomputer always sets one",
         "other": "rules metacomputer didn't set"}
print(f"network of {vm}:")
print(f"  setting (in {label})  EGRESS={setting}")
print(f"  Fly's network policy (outside the computer)  " + (f"allowlist: {', '.join(names)}" if kind == "allowlist" else f"{kind}: {words.get(kind, '')}"))
if kind in ("internet", "off") and kind != setting:
    print(f"  ! what's in force isn't the setting: metacomputer --vm {vm} net egress {setting}")
if kind == "allowlist":
    print(f"  an allowlist is in force until net clear (back to EGRESS={setting}); a blocked name looks like a DNS failure (dnsNotFound)")
PYEOF
}
pv_sprites_net_list() { # allow NAME... [--exact] | remove NAME... | clear: Fly's network policy as an allowlist of names
  local sub="$1"; shift
  owners_check
  handoff_hold_unless_override
  remote_can_ask || die "Fly's sprite command isn't installed (docs/sprites.md); nothing was changed"
  local a args=() exact=() names=()
  for a in "$@"; do case "$a" in --exact) exact=(--exact) ;; --for|--for=*) die "a Sprite's allowlist stays until net clear: --for isn't available (docs/sprites.md)" ;; --override) ;; -*) die "usage: metacomputer net allow NAME... [--exact] | net remove NAME... | net clear" ;; *) names+=("$a") ;; esac; done
  case "$sub" in
    allow) args=(${exact[@]+"${exact[@]}"} ${names[@]+"${names[@]}"}) ;;
    remove) args=(--remove ${names[@]+"${names[@]}"}) ;;
    clear) [ ${#names[@]} = 0 ] || die "usage: metacomputer net clear"; args=(--clear) ;;
  esac
  remote_py allow --vm "$VM_NAME" ${args[@]+"${args[@]}"} >/dev/null || exit 3
  emit_event security net_allowlist_changed info "The network allowlist of $VM_NAME changed ($sub)" "" "change=$sub"
  pv_sprites_net_status
}
pv_sprites_tunnels_left() { # how many of this Mac's tunnels to this Sprite are still running, after up to 6 s
  local i n=0 pat="-o ${SPRITE_ORG:-} -s ${SPRITE_NAME:-$VM_NAME} proxy -W :22"
  for i in $(seq 1 24); do
    n="$(proc_commands | grep -F -- "$pat" | grep -cvE '^(ssh|grep) ' || true)"
    [ "${n:-0}" -gt 0 ] || { echo ""; return 0; }
    sleep 0.25
  done
  echo "$n"
}
# Checkpoints of the whole disk, through Fly (a restore restarts the Sprite's programs: the desktop comes
# back as a service; open apps close and connections are cut).
pv_sprites_wait_desktop() {
  MC_REMOTE_STATUS=""; MC_T_USER=""
  wait_ssh ops 180 || die "SSH to $VM_NAME didn't answer after the restore (metacomputer status)"
  wait_session 180 || die "the desktop didn't come back after the restore (metacomputer logs)"
}
pv_sprites_snapshot() { # reset|baseline|restore|checkpoint|checkpoints ARGS...
  local cmd="$1"; shift
  parse_flags "$@"
  set -- ${REST[@]+"${REST[@]}"}
  remote_can_ask || die "Fly's sprite command isn't installed (docs/sprites.md)"
  local t0 out
  case "$cmd" in
    checkpoints) remote_py checkpoint --vm "$VM_NAME" list; return $? ;;
    checkpoint)
      vm_change_lock "metacomputer checkpoint"
      if [ "${1:-}" = --delete ]; then
        ckpt_name "${2:-}"; [ "$2" != baseline ] || die "the baseline goes with metacomputer baseline (it replaces it)"
        owners_check
        confirm "Delete checkpoint '$2' of $VM_NAME at Fly?"
        handoff_hold_unless_override     # held through Fly's change, as every acting command's
        remote_py checkpoint --vm "$VM_NAME" delete "$2" >/dev/null || die "checkpoint '$2' wasn't deleted (above)"
        emit_event lifecycle checkpoint info "Checkpoint '$2' of $VM_NAME deleted" "" "name=$2" "change=deleted"
        ok "checkpoint '$2' deleted"; return 0
      fi
      local name="" replace=0 a
      for a in "$@"; do case "$a" in --replace) replace=1 ;; -*) die "usage: metacomputer checkpoint NAME [--replace]" ;; *) name="$a" ;; esac; done
      ckpt_name "$name"; [ "$name" != baseline ] || die "'baseline' is the baseline's name: metacomputer baseline"
      if remote_py checkpoint --vm "$VM_NAME" list 2>/dev/null | cut -f1 | grep -qx "$name"; then
        [ $replace = 1 ] || die "checkpoint '$name' exists (add --replace to overwrite it)"
      fi
      owners_check
      # A checkpoint of the running computer keeps what is on its disk: never once the user has it (a handoff), so the
      # dispatch lock is held from the last look for one until Fly has made it.
      handoff_hold_unless_override
      out="$(remote_py checkpoint --vm "$VM_NAME" create "$name" $([ $replace = 1 ] && echo --replace))" || die "the checkpoint wasn't made (above)"
      emit_event lifecycle checkpoint info "Checkpoint '$name' of $VM_NAME saved" "" "name=$name" "change=saved"
      ok "checkpoint '$name' saved while it runs, in $(printf '%s' "$out" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["seconds"])') s (restore with: metacomputer --vm $VM_NAME restore $name)"
      return 0 ;;
    baseline)
      vm_change_lock "metacomputer baseline"
      owners_check
      confirm "Make the current state of '$VM_NAME' its baseline (what metacomputer reset goes back to)?"
      handoff_hold_unless_override
      out="$(remote_py checkpoint --vm "$VM_NAME" create baseline --replace)" || die "the baseline wasn't made (above)"
      ok "baseline saved (a checkpoint at Fly, made while it runs; an earlier one deleted)"
      return 0 ;;
    reset|restore)
      local label=baseline what="a fresh copy of its baseline"
      if [ "$cmd" = restore ]; then label="${1:-}"; ckpt_name "$label"; what="checkpoint '$label'"; fi
      vm_change_lock "metacomputer $cmd"
      owners_check
      confirm "Put '$VM_NAME' back to $what? Its current state is discarded, its programs restart (open apps close) and its connections are cut (host recordings and results are kept)"
      handoff_hold_unless_override
      if [ -s "$RUN_DIR/manual-recording" ]; then
        say "finishing the desktop recording first"
        ( do_record stop ) || warn "could not finish the recording (metacomputer record stop)"
      fi
      hub_stop
      view_tunnel_close
      t0=$(date +%s)
      out="$(remote_py checkpoint --vm "$VM_NAME" restore "$label")" || die "Fly didn't restore '$label' (above)"
      ok "restored at Fly in $(( $(date +%s) - t0 )) s; waiting for the desktop"
      pv_sprites_wait_desktop
      ok "desktop back $(( $(date +%s) - t0 )) s after the restore began (open apps closed: launch them again)"
      emit_event lifecycle "$([ "$cmd" = reset ] && echo reset || echo restored)" info "The computer $VM_NAME was put back to $label" "" "from=$label"
      return 0 ;;
  esac
}

# desktop-restart: its desktop is two Sprites services, which only Fly's own channel reaches: made again, they start again.
pv_sprites_desktop_restart() {
  remote_py exec --vm "$VM_NAME" --timeout 120 -- sudo -n /opt/metacomputer/sprite/services desktop >/dev/null \
    || die "couldn't restart the desktop's services (above)"
}
# logs [LINES]: the services' logs are under /.sprite, which SSH sessions can't see: read through Fly's channel.
pv_sprites_logs() {
  local n="$1"
  remote_py exec --vm "$VM_NAME" --timeout 60 -- sudo -n sh -c "tail -n $n /var/log/metacomputer-provision.log; for f in /.sprite/logs/services/*.log; do echo \"== \$f\"; tail -n $n \"\$f\"; done" 2>/dev/null | term_safe
}
