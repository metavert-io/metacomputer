# Vercel Sandbox's own steps in metacomputer's commands (bin/metacomputer, scripts/vm-commands.sh, scripts/lib.sh): each
# pv_vercel_HOOK runs where the shared code finds the hook (scripts/lib.sh mc_pv_has / mc_pv_run) instead of its own
# step, in the same shell, with that command's variables and functions. Sourced (scripts/lib.sh mc_pv_load), never run.

# vm create --on vercel (scripts/vm-commands.sh vm_create: its variables).
pv_vercel_vm_create() {
  [ -z "$host$huser$hport" ] || die "--host, --user and --port are for your own server; Vercel reaches a sandbox through its own API"
  opt_sandbox="${opt_sandbox:-$name}"
  a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import cloud; v = cloud.registry.module("vercel")
try: print(v.sandbox_of({"VERCEL_SANDBOX": sys.argv[2]}), v.Vercel.size_of(sys.argv[3] or None), v.Vercel.region_of(sys.argv[4] or None) or "-", sep="\t")
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$opt_sandbox" "$size" "$region")" || die "$a"
  IFS=$'\t' read -r opt_sandbox size region <<<"$a"; [ "$region" != - ] || region=""
  case "${idle:-15}" in off|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) ;; *) die "--idle-stop: minutes (1-999), or off" ;; esac
  [ "$build" = 0 ] || remote_can_ask_for vercel \
    || die "making a computer on Vercel Sandbox needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this command's environment, from your secrets manager (docs/vercel.md); --no-build writes only its settings"
  rlines=("VERCEL_SANDBOX=$opt_sandbox" "SIZE=$size" "IDLE_STOP=${idle:-15}")
  [ -z "$region" ] || rlines+=("REGION=$region")
}
pv_vercel_vm_said() { echo "Vercel Sandbox, the sandbox $opt_sandbox ($size${region:+ in $region})"; }
pv_vercel_vm_billing() {
  # The rate, the plan's session cap and what a stop keeps, before anything is made (providers/vercel/adapter.py).
  local a p
  a="$(MCOMP_VM="$name" remote_py rates --vm "$name" 2>/dev/null || true)"
  p="$(PROVIDER=vercel remote_py cloud status vercel --json 2>/dev/null | "$PY" -c 'import json, sys
try: d = json.load(sys.stdin)
except ValueError: d = {}
print(d.get("plan") or "")' 2>/dev/null || true)"
  printf '%s\n' "Vercel Sandbox: ${a:-rates unknown}" >&2
  case "$p" in
    hobby) printf '%s\n' "This Vercel team is on Hobby: every session ends 45 minutes after it starts, in use or not (open apps close then; metacomputer start starts a new one). Pro raises that to 24 hours." >&2 ;;
    "") printf '%s\n' "The Vercel team's plan couldn't be read: on Hobby every session ends after 45 minutes, on Pro after 24 hours." >&2 ;;
    *) printf '%s\n' "This Vercel team is on ${p}: a session ends 24 hours after it starts, at the latest." >&2 ;;
  esac
  printf '%s\n' "PERSIST=disk: each stop snapshots its disk and powers it off (files kept, open apps closed); the next start boots that snapshot. It stops by itself ${idle:-15} minutes after it was last used (IDLE_STOP). Stopped, only its snapshot is billed; its snapshots never expire by themselves, and vm delete deletes them. Its network policy is EGRESS=${egress:-internet}, enforced by Vercel outside the computer (docs/vercel.md)." >&2
}
pv_vercel_vm_delete_what() { printf '%s\n' "  its sandbox at Vercel (${VERCEL_SANDBOX:-$VM_NAME}): its disk, its sessions and every snapshot of it (checkpoints and the baseline too)"; }

# stop (bin/metacomputer do_stop_remote): what it does, said before it's done.
pv_vercel_stop_saying() { say "stopping $VM_NAME: Vercel snapshots its disk, then powers it off (about 15 s). Files are kept; every open app closes"; }

# net egress internet|off (bin/metacomputer remote_net_egress, after its owners and handoff checks).
pv_vercel_net_egress() {
  local want="$1" got
  remote_can_ask || die "changing $VM_NAME's network policy needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this command's environment; nothing was changed"
  got="$(remote_py egress --vm "$VM_NAME" --set "$want")" || die "Vercel's network policy wasn't changed (above); nothing else was"
  [ "$got" = "$want" ] || die "Vercel's network policy reads back as '$got', not $want; the setting wasn't changed"
  ok "Vercel's network policy: EGRESS=$want (in force now, outside the computer)"
  browser_set EGRESS "$want"
  ok "EGRESS=$want in $MC_SETTINGS_LABEL"
  return 0
}
pv_vercel_net_status() {
  local j
  remote_can_ask || { printf '%s\n' "network of $VM_NAME:" "  setting (in $MC_SETTINGS_LABEL)  EGRESS=${EGRESS:-internet}" "  Vercel's network policy  not asked (no Vercel credentials in this command)"; return 0; }
  j="$(remote_py allow --vm "$VM_NAME" --show 2>/dev/null)" || die "couldn't read $VM_NAME's network policy from Vercel (above)"
  "$PY" - "$j" "${EGRESS:-internet}" "$VM_NAME" "$MC_SETTINGS_LABEL" <<'PYEOF'
import json, sys
d, setting, vm, label = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
kind, names = d.get("outside") or "unknown", d.get("allow") or []
words = {"internet": "every name and address but private, shared, link-local (the metadata service) and reserved ranges",
         "off": "nothing goes out (deny-all)",
         "open": "EVERYTHING, private ranges included (Vercel's allow-all): metacomputer always sets its own",
         "other": "rules metacomputer didn't set"}
print(f"network of {vm}:")
print(f"  setting (in {label})  EGRESS={setting}")
print("  Vercel's network policy (outside the computer)  " + (f"allowlist: {', '.join(names)}" if kind == "allowlist" else f"{kind}: {words.get(kind, '')}"))
if kind in ("internet", "off") and kind != setting:
    print(f"  ! what's in force isn't the setting: metacomputer --vm {vm} net egress {setting}")
if kind == "allowlist":
    print(f"  an allowlist is in force until net clear (back to EGRESS={setting}); a blocked name looks like a DNS failure, a blocked address like a reset connection")
PYEOF
}
pv_vercel_net_list() { # allow NAME... [--exact] | remove NAME... | clear: Vercel's network policy as an allowlist of names
  local sub="$1"; shift
  owners_check
  handoff_hold_unless_override
  remote_can_ask || die "changing $VM_NAME's network policy needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this command's environment; nothing was changed"
  local a args=() exact=() names=()
  for a in "$@"; do case "$a" in --exact) exact=(--exact) ;; --for|--for=*) die "a sandbox's allowlist stays until net clear: --for isn't available (docs/vercel.md)" ;; --override) ;; -*) die "usage: metacomputer net allow NAME... [--exact] | net remove NAME... | net clear" ;; *) names+=("$a") ;; esac; done
  case "$sub" in
    allow) args=(${exact[@]+"${exact[@]}"} ${names[@]+"${names[@]}"}) ;;
    remove) args=(--remove ${names[@]+"${names[@]}"}) ;;
    clear) [ ${#names[@]} = 0 ] || die "usage: metacomputer net clear"; args=(--clear) ;;
  esac
  remote_py allow --vm "$VM_NAME" ${args[@]+"${args[@]}"} >/dev/null || exit 3
  emit_event security net_allowlist_changed info "The network allowlist of $VM_NAME changed ($sub)" "" "change=$sub"
  pv_vercel_net_status
}

# Checkpoints: Vercel snapshots of the whole disk. Taking one stops the computer (Vercel's snapshot ends the session),
# and so does a restore: metacomputer starts it again, but open apps close and connections are cut.
pv_vercel_wait_desktop() {
  MC_REMOTE_STATUS=""; MC_T_USER=""
  wait_ssh ops 180 || die "SSH to $VM_NAME didn't answer after it started again (metacomputer status)"
  wait_session 180 || die "the desktop didn't come back (metacomputer logs)"
}
pv_vercel_snapshot() { # reset|baseline|restore|checkpoint|checkpoints ARGS...
  local cmd="$1"; shift
  parse_flags "$@"
  set -- ${REST[@]+"${REST[@]}"}
  remote_can_ask || die "checkpoints of $VM_NAME need VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this command's environment"
  local t0 out
  case "$cmd" in
    checkpoints) remote_py checkpoint --vm "$VM_NAME" list; return $? ;;
    checkpoint)
      vm_change_lock "metacomputer checkpoint"
      if [ "${1:-}" = --delete ]; then
        ckpt_name "${2:-}"; [ "$2" != baseline ] || die "the baseline goes with metacomputer baseline (it replaces it)"
        owners_check
        confirm "Delete checkpoint '$2' of $VM_NAME at Vercel?"
        handoff_hold_unless_override
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
      confirm "Save checkpoint '$name' of $VM_NAME? Vercel stops it to take the snapshot: open apps close and connections are cut (it's started again)"
      handoff_hold_unless_override
      hub_stop
      view_tunnel_close
      t0=$(date +%s)
      out="$(remote_py checkpoint --vm "$VM_NAME" create "$name" $([ $replace = 1 ] && echo --replace))" || die "the checkpoint wasn't made (above)"
      pv_vercel_wait_desktop
      emit_event lifecycle checkpoint info "Checkpoint '$name' of $VM_NAME saved" "" "name=$name" "change=saved"
      ok "checkpoint '$name' saved, and $VM_NAME is running again, in $(( $(date +%s) - t0 )) s (taking it stopped the computer: open apps were closed; restore with: metacomputer --vm $VM_NAME restore $name)"
      return 0 ;;
    baseline)
      vm_change_lock "metacomputer baseline"
      owners_check
      confirm "Make the current state of '$VM_NAME' its baseline (what metacomputer reset goes back to)? Vercel stops it to take the snapshot: open apps close (it's started again)"
      handoff_hold_unless_override
      hub_stop
      view_tunnel_close
      out="$(remote_py checkpoint --vm "$VM_NAME" create baseline --replace)" || die "the baseline wasn't made (above)"
      pv_vercel_wait_desktop
      ok "baseline saved (a snapshot at Vercel; an earlier one deleted); $VM_NAME is running again (open apps were closed)"
      return 0 ;;
    reset|restore)
      local label=baseline what="a fresh copy of its baseline"
      if [ "$cmd" = restore ]; then label="${1:-}"; ckpt_name "$label"; what="checkpoint '$label'"; fi
      vm_change_lock "metacomputer $cmd"
      owners_check
      confirm "Put '$VM_NAME' back to $what? Its current state is discarded, it's stopped and started again (open apps close) and its connections are cut (host recordings and results are kept)"
      handoff_hold_unless_override
      if [ -s "$RUN_DIR/manual-recording" ]; then
        say "finishing the desktop recording first"
        ( do_record stop ) || warn "could not finish the recording (metacomputer record stop)"
      fi
      hub_stop
      view_tunnel_close
      t0=$(date +%s)
      out="$(remote_py checkpoint --vm "$VM_NAME" restore "$label")" || die "Vercel didn't restore '$label' (above)"
      ok "restored at Vercel and started again in $(( $(date +%s) - t0 )) s; waiting for the desktop"
      pv_vercel_wait_desktop
      ok "desktop back $(( $(date +%s) - t0 )) s after the restore began (open apps closed: launch them again)"
      emit_event lifecycle "$([ "$cmd" = reset ] && echo reset || echo restored)" info "The computer $VM_NAME was put back to $label" "" "from=$label"
      return 0 ;;
  esac
}

# desktop-restart: the desktop is started by the boot script, which only Vercel's own API runs (as root).
pv_vercel_desktop_restart() {
  remote_can_ask || die "restarting $VM_NAME's desktop needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this command's environment"
  remote_py exec --vm "$VM_NAME" --timeout 180 -- /opt/metacomputer/vercel/boot restart-desktop >/dev/null \
    || die "couldn't restart the desktop (above)"
}
# logs [LINES]: the setup's log and the boot script's, read through Vercel's API.
pv_vercel_logs() {
  local n="$1"
  remote_can_ask || die "reading $VM_NAME's logs needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this command's environment"
  remote_py exec --vm "$VM_NAME" --timeout 60 -- sh -c "tail -n $n /var/log/metacomputer-provision.log; echo '== /var/log/metacomputer-vercel-boot.log'; tail -n $n /var/log/metacomputer-vercel-boot.log" 2>/dev/null | term_safe
}
