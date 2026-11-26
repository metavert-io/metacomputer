# Cloudflare Containers' own steps in metacomputer's commands (bin/metacomputer, scripts/vm-commands.sh, scripts/lib.sh):
# each pv_cloudflare_HOOK runs where the shared code finds the hook (scripts/lib.sh mc_pv_has / mc_pv_run) instead of its
# own step, in the same shell, with that command's variables and functions. Sourced (scripts/lib.sh mc_pv_load), never
# run. The account's API token is for vm create and vm delete; everything else needs only the computer's own key
# (kept beside its SSH keys, read by lib/remote.py alone).

pv_cloudflare_has_token() { [ -n "${CLOUDFLARE_API_TOKEN:-}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; }

# Whether Cloudflare can be asked from here: the account's token (create, delete), or this computer's own key (start,
# stop, status, checkpoints, egress: its control Worker).
pv_cloudflare_can_ask() { pv_cloudflare_has_token || { [ -n "${SSH_DIR:-}" ] && [ -s "$SSH_DIR/cloudflare-key" ]; }; }

# vm create --on cloudflare (scripts/vm-commands.sh vm_create: its variables).
pv_cloudflare_vm_create() {
  [ -z "$host$huser$hport" ] || die "--host, --user and --port are for your own server; a computer on Cloudflare is reached through its control Worker"
  [ -z "$region" ] || die "--region: Cloudflare places a container itself (near the request that starts it)"
  local a
  a="$("$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import cloud; c = cloud.registry.module("cloudflare")
try: print(c.worker_of({"CLOUDFLARE_WORKER": sys.argv[2], "VM_NAME": sys.argv[3]}), c.Cloudflare.size_of(sys.argv[4] or None), c.Cloudflare.idle_of(sys.argv[5] or None), sep="\t")
except Exception as e: print(e); sys.exit(1)' "$LAB_ROOT/lib" "$opt_worker" "$name" "$size" "$idle")" || die "$a"
  IFS=$'\t' read -r opt_worker size idle <<<"$a"
  [ "$build" = 0 ] || pv_cloudflare_has_token \
    || die "making a computer on Cloudflare needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in this command's environment, from your secrets manager (docs/cloudflare.md); --no-build writes only its settings"
  rlines=("CLOUDFLARE_WORKER=$opt_worker" "SIZE=$size" "IDLE_STOP=$idle")
}
pv_cloudflare_vm_said() { echo "Cloudflare Containers, its control Worker $opt_worker ($size)"; }
pv_cloudflare_vm_billing() {
  # The rate, what a stop keeps and what Cloudflare keeps for 30 days, before anything is made
  # (providers/cloudflare/adapter.py).
  local a
  a="$(MCOMP_VM="$name" remote_py rates --vm "$name" 2>/dev/null || true)"
  printf '%s\n' "Cloudflare Containers: ${a:-rates unknown}" \
    "It needs Workers Paid on the account (\$5 a month; Containers aren't on the Free plan). metacomputer puts a control Worker for it into the account ($opt_worker, on workers.dev only, no route on your domains), with its Durable Object, its container application and a repository in the account's registry." \
    "PERSIST=disk: each stop snapshots its disk, then ends the container (files kept, open apps closed); each start restores that snapshot. It stops by itself ${idle:-15} minutes after its last connection closes (IDLE_STOP); Cloudflare can also stop it without warning, losing what changed since its last snapshot." \
    "Cloudflare keeps every snapshot 30 days after its last use and its API can't delete one: vm delete removes everything else, and says which snapshots stay and until when (free during Cloudflare's beta). Its egress is EGRESS=${egress:-internet}, held by Cloudflare outside the computer (docs/cloudflare.md)." >&2
}
pv_cloudflare_vm_delete_what() {
  # (vm delete runs this before it asks anything or changes anything: without the account's token, nothing is done,
  # unless its record shows nothing was ever made at Cloudflare: a record that can't be read counts as something.)
  if ! pv_cloudflare_has_token && [ "$("$PY" -c 'import json, sys
try: d = json.load(open(sys.argv[1]))
except FileNotFoundError: d = {}
except Exception: d = {"unreadable": 1}
print("something" if any(d.get(k) for k in ("instance", "launching", "worker_sent", "made_worker", "made_image", "deleting", "unreadable")) else "nothing")' "$RUN_DIR/remote.json" 2>/dev/null || echo something)" != nothing ]; then
    die "deleting $VM_NAME's control Worker needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in this command's environment (docs/cloudflare.md); nothing was deleted"
  fi
  printf '%s\n' "  its control Worker at Cloudflare (${CLOUDFLARE_WORKER:-metacomputer-$VM_NAME}), its container (what runs is ended) and container application, and its image in the account's registry" \
    "  (Cloudflare keeps its disk's snapshots until each expires, 30 days after its last use: its API can't delete them; vm delete lists them, with dates)"
}

# stop (bin/metacomputer do_stop_remote): what it does, said before it's done.
pv_cloudflare_stop_saying() { say "stopping $VM_NAME: its open apps are closed, its disk is snapshotted at Cloudflare (about 20 s for a desktop's), then the container is ended. Files are kept"; }

# net egress internet|off (bin/metacomputer remote_net_egress, after its owners and handoff checks).
pv_cloudflare_net_egress() {
  local want="$1" got
  remote_can_ask || die "changing $VM_NAME's egress needs its control Worker's key (made by vm create) on this Mac; nothing was changed"
  local cur
  cur="$(remote_py allow --vm "$VM_NAME" --show 2>/dev/null | "$PY" -c 'import json, sys
try: d = json.load(sys.stdin)
except ValueError: d = {}
print(("internet" if d.get("outside") == "internet" else "off") + (":running" if d.get("running") else ":stopped"))' 2>/dev/null || true)"
  case "$cur:$want" in
    internet:running:off|off:running:internet)
      say "Cloudflare sets a container's internet at its start: going between the internet and off restarts $VM_NAME (its disk is snapshotted first; open apps close)" ;;
  esac
  got="$(remote_py egress --vm "$VM_NAME" --set "$want")" || die "the egress wasn't changed (above); nothing else was"
  [ "$got" = "$want" ] || die "the egress reads back as '$got', not $want; the setting wasn't changed"
  ok "Cloudflare's outbound rules: EGRESS=$want (in force now, outside the computer; or from its next start if it's stopped)"
  browser_set EGRESS "$want"
  ok "EGRESS=$want in $MC_SETTINGS_LABEL"
  return 0
}
pv_cloudflare_net_status() {
  local j
  remote_can_ask || { printf '%s\n' "network of $VM_NAME:" "  setting (in $MC_SETTINGS_LABEL)  EGRESS=${EGRESS:-internet}" "  Cloudflare's outbound rules  not asked (no key for its control Worker on this Mac)"; return 0; }
  j="$(remote_py allow --vm "$VM_NAME" --show 2>/dev/null)" || die "couldn't read $VM_NAME's egress from its control Worker (above)"
  "$PY" - "$j" "${EGRESS:-internet}" "$VM_NAME" "$MC_SETTINGS_LABEL" <<'PYEOF'
import json, sys
d, setting, vm, label = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
kind, names = d.get("outside") or "unknown", d.get("allow") or []
words = {"internet": "every name and address but private, shared and link-local ranges (the metadata service), which Cloudflare refuses",
         "off": "nothing goes out",
         "other": "rules metacomputer didn't set"}
print(f"network of {vm}:")
print(f"  setting (in {label})  EGRESS={setting}")
where = "in force" if d.get("running") else "from its next start (it's stopped)"
print(f"  Cloudflare's outbound rules, {where}  " + (f"allowlist: {', '.join(names)} (HTTP and HTTPS only; HTTPS through Cloudflare's TLS interception)" if kind == "allowlist" else f"{kind}: {words.get(kind, '')}"))
if kind in ("internet", "off") and kind != setting:
    print(f"  ! that isn't the setting: metacomputer --vm {vm} net egress {setting}")
if kind == "allowlist":
    print(f"  an allowlist is in force until net clear (back to EGRESS={setting}); an unlisted name doesn't resolve")
PYEOF
}
pv_cloudflare_net_list() { # allow NAME... | remove NAME... | clear: an allowlist of exact names
  local sub="$1"; shift
  owners_check
  handoff_hold_unless_override
  remote_can_ask || die "changing $VM_NAME's egress needs its control Worker's key on this Mac; nothing was changed"
  local a args=() names=()
  for a in "$@"; do case "$a" in --exact) ;; --for|--for=*) die "an allowlist stays until net clear: --for isn't available here (docs/cloudflare.md)" ;; --override) ;; -*) die "usage: metacomputer net allow NAME... | net remove NAME... | net clear" ;; *) names+=("$a") ;; esac; done
  case "$sub" in
    allow) args=(${names[@]+"${names[@]}"}) ;;
    remove) args=(--remove ${names[@]+"${names[@]}"}) ;;
    clear) [ ${#names[@]} = 0 ] || die "usage: metacomputer net clear"; args=(--clear) ;;
  esac
  remote_py allow --vm "$VM_NAME" ${args[@]+"${args[@]}"} >/dev/null || exit 3
  emit_event security net_allowlist_changed info "The network allowlist of $VM_NAME changed ($sub)" "" "change=$sub"
  pv_cloudflare_net_status
}

# Checkpoints: snapshots of the whole disk, named by the computer's control Worker. Taking one doesn't interrupt the
# computer; restoring one restarts it (open apps close). Cloudflare can't delete a snapshot: one let go expires 30 days
# after its last use.
pv_cloudflare_wait_desktop() {
  MC_REMOTE_STATUS=""; MC_T_USER=""
  wait_ssh ops 180 || die "SSH to $VM_NAME didn't answer after it started again (metacomputer status)"
  wait_session 180 || die "the desktop didn't come back (metacomputer logs)"
}
pv_cloudflare_snapshot() { # reset|baseline|restore|checkpoint|checkpoints ARGS...
  local cmd="$1"; shift
  parse_flags "$@"
  set -- ${REST[@]+"${REST[@]}"}
  remote_can_ask || die "checkpoints of $VM_NAME need its control Worker's key on this Mac"
  local t0 out
  case "$cmd" in
    checkpoints) remote_py checkpoint --vm "$VM_NAME" list; return $? ;;
    checkpoint)
      vm_change_lock "metacomputer checkpoint"
      if [ "${1:-}" = --delete ]; then
        ckpt_name "${2:-}"; [ "$2" != baseline ] || die "the baseline goes with metacomputer baseline (it replaces it)"
        owners_check
        confirm "Forget checkpoint '$2' of $VM_NAME? Cloudflare keeps its snapshot until it expires (30 days after its last use): its API can't delete one"
        handoff_hold_unless_override
        remote_py checkpoint --vm "$VM_NAME" delete "$2" >/dev/null || die "checkpoint '$2' wasn't forgotten (above)"
        emit_event lifecycle checkpoint info "Checkpoint '$2' of $VM_NAME deleted" "" "name=$2" "change=deleted"
        ok "checkpoint '$2' forgotten (Cloudflare keeps its snapshot until it expires)"; return 0
      fi
      local name="" replace=0 a
      for a in "$@"; do case "$a" in --replace) replace=1 ;; -*) die "usage: metacomputer checkpoint NAME [--replace]" ;; *) name="$a" ;; esac; done
      ckpt_name "$name"; [ "$name" != baseline ] || die "'baseline' is the baseline's name: metacomputer baseline"
      if remote_py checkpoint --vm "$VM_NAME" list 2>/dev/null | cut -f1 | grep -qx "$name"; then
        [ $replace = 1 ] || die "checkpoint '$name' exists (add --replace to overwrite it)"
      fi
      owners_check
      confirm "Save checkpoint '$name' of $VM_NAME? A snapshot of its whole disk at Cloudflare (about 20 s; it keeps running), kept while it's a checkpoint and 30 days after"
      handoff_hold_unless_override
      t0=$(date +%s)
      out="$(remote_py checkpoint --vm "$VM_NAME" create "$name" $([ $replace = 1 ] && echo --replace))" || die "the checkpoint wasn't made (above)"
      emit_event lifecycle checkpoint info "Checkpoint '$name' of $VM_NAME saved" "" "name=$name" "change=saved"
      ok "checkpoint '$name' saved in $(( $(date +%s) - t0 )) s ($VM_NAME kept running; restore with: metacomputer --vm $VM_NAME restore $name)"
      return 0 ;;
    baseline)
      vm_change_lock "metacomputer baseline"
      owners_check
      confirm "Make the current state of '$VM_NAME' its baseline (what metacomputer reset goes back to)? A snapshot at Cloudflare (it keeps running); the earlier baseline's is kept by Cloudflare until it expires"
      handoff_hold_unless_override
      out="$(remote_py checkpoint --vm "$VM_NAME" create baseline --replace)" || die "the baseline wasn't made (above)"
      ok "baseline saved (a snapshot at Cloudflare); $VM_NAME kept running"
      return 0 ;;
    reset|restore)
      local label=baseline what="a fresh copy of its baseline"
      if [ "$cmd" = restore ]; then label="${1:-}"; ckpt_name "$label"; what="checkpoint '$label'"; fi
      vm_change_lock "metacomputer $cmd"
      owners_check
      confirm "Put '$VM_NAME' back to $what? Its current state is discarded, it's started again from the snapshot (open apps close) and its connections are cut (host recordings and results are kept)"
      handoff_hold_unless_override
      if [ -s "$RUN_DIR/manual-recording" ]; then
        say "finishing the desktop recording first"
        ( do_record stop ) || warn "could not finish the recording (metacomputer record stop)"
      fi
      hub_stop
      view_tunnel_close
      t0=$(date +%s)
      out="$(remote_py checkpoint --vm "$VM_NAME" restore "$label")" || die "Cloudflare didn't restore '$label' (above)"
      ok "restored at Cloudflare and started again in $(( $(date +%s) - t0 )) s; waiting for the desktop"
      pv_cloudflare_wait_desktop
      ok "desktop back $(( $(date +%s) - t0 )) s after the restore began (open apps closed: launch them again)"
      emit_event lifecycle "$([ "$cmd" = reset ] && echo reset || echo restored)" info "The computer $VM_NAME was put back to $label" "" "from=$label"
      return 0 ;;
  esac
}

# desktop-restart: the desktop is started by the boot script, which only the computer's control Worker runs (as root).
pv_cloudflare_desktop_restart() {
  remote_can_ask || die "restarting $VM_NAME's desktop needs its control Worker's key on this Mac"
  remote_py exec --vm "$VM_NAME" --timeout 180 -- /opt/metacomputer/cloudflare/boot restart-desktop >/dev/null \
    || die "couldn't restart the desktop (above)"
}
# logs [LINES]: the setup's log and the boot script's, read through the computer's control Worker.
pv_cloudflare_logs() {
  local n="$1"
  case "$n" in ''|*[!0-9]*) die "logs: a number of lines" ;; esac
  remote_can_ask || die "reading $VM_NAME's logs needs its control Worker's key on this Mac"
  remote_py exec --vm "$VM_NAME" --timeout 60 -- /opt/metacomputer/cloudflare/boot logs "$n" 2>/dev/null | term_safe
}
