#!/bin/bash
# Set up a computer reached over SSH (docs/remote.md): an Ubuntu 24.04 VM that a provider makes (oci) or your own
# server (server). The same desktop, driver and accounts as the Mac's VM, set up the same way on both:
#   1. a provider's: its keys and host key made here, then its instance, whose cloud-init runs bootstrap.sh;
#      your own: bootstrap.sh run once over your own SSH connection (your account, with sudo), and the host key it
#      reports pinned;
#   2. the payload (provisioning/guest, provisioning/remote, Cua Driver's build for its architecture, the APT keys,
#      the key lists) uploaded over SSH as cuaadmin, and provision.sh run (MCOMP_TARGET=remote);
#   3. a reboot into the desktop, the live-view password, the desktop checked, then finalize.sh (cuaadmin's sudo
#      narrowed to metacomputer-admin, cloud-init off).
# A build that fails after making an instance deletes it again (it bills while it exists), unless --keep-on-failure:
# then it's stopped through the provider, and what still bills (its disk) and how to delete it are said. Your own
# server is checked with a fresh connection of your own after the setup and after the reboot; if your account can't
# sign in, the setup's SSH settings and firewall are taken back off (remote-uninstall rollback) and the build stops.
#
# Run by `metacomputer --vm NAME build [--keep-on-failure]` (bin/metacomputer). Your own server can be set up again
# this way (your account
# runs bootstrap.sh with your sudo again, then the same steps, each leaving what's right as it is). A provider's
# instance is made only when there's none, and one that's set up isn't set up again (cuaadmin's sudo is narrowed by
# then, and there's no other account): delete it and make a new one, as for a VM on the Mac.
#
#   scripts/remote-build.sh [--keep-on-failure]
set -Eeuo pipefail
# Provider credentials (lib/cloud/oci.py ENV_KEYS) in this command's environment reach lib/remote.py only: taken off
# the environment here, before any other program runs (bash builtins only), and exported again into remote_py's one
# child (scripts/lib.sh). The shell keeps them as unexported variables.
for mc_k in OCI_USER OCI_TENANCY OCI_FINGERPRINT OCI_REGION OCI_API_KEY_PEM OCI_COMPARTMENT; do export -n "$mc_k"; done; unset mc_k
. "$(dirname "$0")/lib.sh"
trap 'die "failed at ${BASH_SOURCE[0]##*/} line $LINENO: $BASH_COMMAND"' ERR
[ "${MC_REMOTE:-0}" = 1 ] || die "$VM_NAME is a VM on this Mac: scripts/build-vm.sh builds it"
vm_target_check
vm_notice
BUILD_QUIET_EXIT=0
KEEP_ON_FAILURE=0
for a in "$@"; do case "$a" in --keep-on-failure) KEEP_ON_FAILURE=1 ;; *) die "usage: metacomputer --vm NAME build [--keep-on-failure]" ;; esac; done
# This build's operation: its launch is tagged with it and journaled under it (lib/remote.py create --op), so a
# failure deletes only what this operation made (lib/remote.py abort --op), never an instance it found.
ABORT_OP=""
SETUP_OPEN=0       # the setup's egress may be open (EGRESS=off: apt's downloads inside, public addresses outside)
SETUP_DONE=0       # finalized: from here nothing is undone on a failure
ROLLED_BACK=0      # your own server's setup was taken back off (owner_check): its firewall stays off
WOKEN=0            # an instance this build started to set it up again (it was stopped): stopped again on a failure
# The setup's egress ends on every failure, whatever happens to the computer after: its own firewall drops the
# setup's downloads (the marker goes, the configured policy is loaded) and the provider's rules go back to EGRESS.
# The marker also expires by itself (guest-firewall), so a computer this can't reach closes too.
wanted_egress() { local w; w="$(plan egress 2>/dev/null)"; printf '%s' "${w:-${EGRESS:-internet}}"; }
close_guest_setup_egress() {
  [ "$SETUP_OPEN" = 1 ] && [ "$ROLLED_BACK" != 1 ] || return 0
  # sync: on the disk before anything stops it (a stop through the provider is a power-off; found live: the marker
  # came back after one)
  if gssh admin 'sudo rm -f /etc/metacomputer/setup-egress && sudo /opt/metacomputer/bin/guest-firewall apply && sync' >/dev/null 2>&1; then
    ok "its own firewall: the setup's downloads ended (EGRESS=$(wanted_egress))"
  else
    warn "couldn't reach $VM_NAME to end the setup's downloads in its own firewall: they end by themselves within 3 hours of the setup's start"
  fi
}
close_provider_setup_egress() {
  [ "$SETUP_OPEN" = 1 ] && [ "$KIND" = made ] || return 0
  if remote_py egress --vm "$VM_NAME" --set "$(wanted_egress)" >/dev/null; then ok "$WHERE's rules: EGRESS=$(wanted_egress)"
  else warn "$WHERE's rules couldn't be set back to EGRESS=$(wanted_egress): metacomputer --vm $VM_NAME net egress $(wanted_egress)"; fi
}
# A failed build that made an instance deletes it again (or, with --keep-on-failure, stops it through the provider),
# and says what's left billing. Your own server's rollback is done where its failure is found (owner_check).
cleanup_failed() { # RC
  [ "$1" != 0 ] && [ "$SETUP_DONE" = 0 ] || return 0
  if [ -n "$ABORT_OP" ] && [ "$KEEP_ON_FAILURE" != 1 ]; then
    say "the setup failed; deleting what this build made at $WHERE (an instance bills while it exists)"
    if remote_py abort --vm "$VM_NAME" --op "$ABORT_OP"; then
      ok "nothing this build made is left billing at $WHERE (metacomputer cloud status oci lists what's there)"
    else
      warn "what this build made could NOT be deleted: an instance of $VM_NAME may still be RUNNING and billing at $WHERE. Delete it: metacomputer vm delete $VM_NAME (or metacomputer cloud status oci, then delete it in Oracle's console)"
      close_guest_setup_egress; close_provider_setup_egress
    fi
    return 0
  fi
  close_guest_setup_egress          # while it still runs
  if [ -n "$ABORT_OP" ]; then
    say "the setup failed; keeping what it made (--keep-on-failure), stopped through $WHERE"
    local kept=0; remote_py abort --vm "$VM_NAME" --op "$ABORT_OP" --keep >/dev/null && kept=1
    close_provider_setup_egress     # its record names the kept instance now
    if [ "$kept" = 1 ]; then
      warn "an instance this build made is stopped: $WHERE bills its disk until you delete it: metacomputer vm delete $VM_NAME"
    else
      warn "it couldn't be stopped: an instance this build made may be RUNNING and billing at $WHERE. Stop it (metacomputer --vm $VM_NAME stop --force) or delete it (metacomputer vm delete $VM_NAME)"
    fi
    return 0
  fi
  close_provider_setup_egress
  if [ "$WOKEN" = 1 ]; then
    say "the setup failed; stopping $VM_NAME again (this build started it)"
    remote_py sleep --vm "$VM_NAME" --force >/dev/null && warn "it's stopped: $WHERE bills its disk until you delete it" \
      || warn "it couldn't be stopped: it is RUNNING and billing at $WHERE (metacomputer --vm $VM_NAME stop --force)"
  fi
}
trap 'rc=$?; trap - ERR; set +e; cleanup_failed $rc
      if [ $rc = 0 ]; then emit_event lifecycle build_done info "The computer $VM_NAME was set up" "$(mc_cmd) start";
      elif [ "$BUILD_QUIET_EXIT" = 0 ]; then emit_event lifecycle build_failed error "Setting up the computer $VM_NAME failed (exit $rc)" "see ${BUILD_LOG:-the newest logs build-*.log}" "exit=$rc"; fi' EXIT
# Interrupted (Ctrl-C, the terminal closed, killed): a failure like any other, so what it made is cleaned up (bash
# would otherwise run the EXIT trap with the status of whatever ran last, often 0).
trap 'exit 130' INT; trap 'exit 129' HUP; trap 'exit 143' TERM
TS="$(date +%Y%m%d-%H%M%S)"
BUILD_LOG="$LOG_DIR/build-$TS.log"
exec > >(tee -a "$BUILD_LOG") 2>&1
say "setting up $VM_NAME (log: $(mc_tilde "$BUILD_LOG"))"
T0=$(date +%s)

lock_rc=0; lock_hold "$(vm_lock_name "$VM_NAME")" "${MCOMP_LOCK_WAIT:-10}" "a build of $VM_NAME" || lock_rc=$?
case "$lock_rc" in
  0) ;;
  1) BUILD_QUIET_EXIT=1; die "another build or change of $VM_NAME is running ($LOCK_OWNER); nothing was changed" ;;
  *) die "couldn't make the private lock folder (MCOMP_LOCK_DIR, or the per-user temporary folder)" ;;
esac
for p in $EXTRA_PACKAGES; do [[ "$p" =~ ^[a-z0-9][a-z0-9.+-]*$ ]] || die "EXTRA_PACKAGES: '$p' is not an Ubuntu package name"; done
for b in $BROWSERS; do case "$b" in firefox|chrome|webkit) ;; *) die "BROWSERS: '$b' isn't firefox, chrome or webkit" ;; esac; done
[ -z "${VOLUMES:-}${REFDISKS:-}" ] || die "VOLUMES and REFDISKS reach only a VM on this Mac for now; remove them from $MC_SETTINGS_LABEL"
PLAN="$(remote_py plan --vm "$VM_NAME")" || die "can't set up $VM_NAME (above)"
plan() { printf '%s\n' "$PLAN" | sed -n "s/^$1=//p"; }
KIND="$(plan kind)"; ARCH="$(plan arch)"; WHERE="$(plan label)"
ok "$VM_NAME: $([ "$KIND" = made ] && echo "a computer $WHERE makes" || echo "your own server at ${SSH_HOST:-?}"), egress $(plan egress), idle stop $(plan idle_stop)"

# --- keys: one set for this computer, 0600 in its own folder ---------------------------------------------------
( umask 077; mkdir -p "$SSH_DIR" ); chmod 700 "$SSH_DIR"
for k in admin ops mcp view fill proxy; do
  [ -f "$SSH_DIR/$k" ] || { rm -f "$SSH_DIR/$k.pub"; ssh-keygen -q -t ed25519 -N '' -C "mcomp-$VM_NAME-$k" -f "$SSH_DIR/$k"; }
  chmod 600 "$SSH_DIR/$k"
done
HK_NAME="$(mc_hostkey_name)"

# --- 1. the instance, or the owner's bootstrap -----------------------------------------------------------------
STATE_HAS_INSTANCE=0
[ "$KIND" != made ] || ! "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("instance") else 1)' "$RUN_DIR/remote.json" 2>/dev/null || STATE_HAS_INSTANCE=1
if [ "$KIND" = made ]; then
  remote_can_ask || die "making a computer on $WHERE needs its credentials in this command's environment (docs/remote.md)"
  if [ "$STATE_HAS_INSTANCE" = 0 ]; then
    # Its host key, made here and given to cloud-init: the first connection is already checked against it.
    rm -f "$SSH_DIR/hostkey" "$SSH_DIR/hostkey.pub"
    ssh-keygen -q -t ed25519 -N '' -C "mcomp-$VM_NAME-host" -f "$SSH_DIR/hostkey"
    echo "$HK_NAME $(cut -d' ' -f1,2 "$SSH_DIR/hostkey.pub")" > "$KNOWN_HOSTS"
    chmod 600 "$KNOWN_HOSTS" "$SSH_DIR/hostkey"
    say "making the instance on $WHERE"
    op="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    [[ "$op" =~ ^[0-9a-f]{32}$ ]] || die "couldn't make an operation id"
    ABORT_OP="$op"           # from here, a failure deletes what this operation made, and only that (cleanup_failed)
    SETUP_OPEN=1
    remote_py create --vm "$VM_NAME" --op "$op" || die "the instance wasn't made (above); metacomputer --vm $VM_NAME status shows what's there"
    remote_py identity --vm "$VM_NAME" --write >/dev/null || die "couldn't record $VM_NAME's identity"
  else
    say "the instance exists: setting it up again"
    was="$(remote_py status --vm "$VM_NAME" --json 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("provider_state",""))' 2>/dev/null)"
    SETUP_OPEN=1
    remote_py wake --vm "$VM_NAME" >/dev/null || die "couldn't start the instance (above)"
    [ "$was" = RUNNING ] || WOKEN=1
  fi
  MC_T_USER=""   # its address is in its record now
  say "waiting for its first boot (cloud-init loads its firewall and makes the accounts)"
  wait_ssh admin "${MCOMP_REMOTE_SSH_WAIT:-900}" || die "SSH to $VM_NAME never answered (metacomputer --vm $VM_NAME status)"
  if gssh admin 'test -f /var/lib/metacomputer/finalized'; then
    BUILD_QUIET_EXIT=1; SETUP_OPEN=0; WOKEN=0      # set up already: nothing of a setup to close, and it stays as it was
    die "$VM_NAME is set up already (metacomputer --vm $VM_NAME status). To set it up from scratch: metacomputer vm delete $VM_NAME, then vm create it again"
  fi
  ARCH_SEEN="$(gssh admin 'dpkg --print-architecture')"
else
  [ -n "${SSH_HOST:-}" ] && [ -n "${SETUP_USER:-}" ] || die "your own server needs SSH_HOST and SETUP_USER in $MC_SETTINGS_LABEL (metacomputer vm create NAME --on server --host ADDRESS --user YOUR_ACCOUNT)"
  [[ "$SETUP_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "SETUP_USER=$SETUP_USER isn't an account name"
  # Your own SSH (your ssh config, agent and known_hosts: your trust in this server), with a key only: setup turns
  # passwords off. The host key you already trust is the one bootstrap.sh reports and this pins.
  # Never through a shared connection (ControlMaster/ControlPath in your ssh config): each check is a new process
  # that authenticates on its own, so what it says is what a new connection of yours gets now. And nothing of this
  # Mac's goes to the server through it, not even before its SSH settings change: no agent, X11 or forwardings
  # (scripts/lib.sh mc_owner_ssh_opts).
  mc_owner_ssh_opts "${SSH_HOST_PORT:-22}"
  OWN=(ssh -o BatchMode=yes "${MC_OWNER_OPTS[@]}" "$SETUP_USER@$SSH_HOST")
  "${OWN[@]}" true </dev/null || die "your own SSH to $SETUP_USER@$SSH_HOST (port ${SSH_HOST_PORT:-22}) didn't sign in with a key without asking anything. Sign in once yourself (ssh -p ${SSH_HOST_PORT:-22} $SETUP_USER@$SSH_HOST: check the host key it shows), with a key, then run this again"
  # Your access must survive the setup: checked with a fresh connection after it and after the reboot, and if it
  # doesn't, the setup's SSH settings and firewall come off again (cuaadmin still has the setup's sudo then).
  owner_check() { # WHEN
    "${OWN[@]}" true </dev/null 2>/dev/null && { ok "your own account still signs in ($1)"; return 0; }
    warn "your account $SETUP_USER can't sign in to $SSH_HOST any more ($1): taking the setup's SSH settings and firewall back off"
    ROLLED_BACK=1
    gssh admin 'sudo /opt/metacomputer/bin/remote-uninstall rollback' || warn "the rollback failed: sign in another way (your provider's console) and run sudo /opt/metacomputer/bin/remote-uninstall rollback"
    "${OWN[@]}" true </dev/null 2>/dev/null && ok "your account signs in again" || warn "your account still can't sign in: use your provider's console"
    BUILD_QUIET_EXIT=0
    die "the setup was rolled back because your own account couldn't sign in afterwards ($1); nothing of it is in force"
  }
  ARCH_SEEN="$("${OWN[@]}" 'dpkg --print-architecture' </dev/null)"
  BS="$(mktemp -d "$SOCK_DIR/bootstrap.XXXXXX")"
  cp "$LAB_ROOT/provisioning/remote/bootstrap.sh" "$BS/"
  remote_py key-lists --vm "$VM_NAME" --dir "$BS" || die "couldn't write the key lists"
  rdir="metacomputer-bootstrap-$TS"
  cp "$LAB_ROOT/provisioning/remote/rootfs/opt/metacomputer/bin/guest-firewall" "$BS/"
  printf '%s\n' "$(plan egress)" > "$BS/egress"
  ( cd "$BS" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -cf - bootstrap.sh keys-guest keys-admin guest-firewall egress ) \
    | "${OWN[@]}" "umask 077; mkdir -p ~/$rdir && tar -xf - -C ~/$rdir" || die "couldn't copy the bootstrap to $SSH_HOST"
  "$LAB_ROOT/scripts/safe-rmtree.sh" --quiet "$BS"
  say "bootstrap on $SSH_HOST as $SETUP_USER (with sudo)"
  SETUP_OPEN=1              # bootstrap.sh marks the setup's egress (EGRESS=off: apt's downloads) from here
  if "${OWN[@]}" 'sudo -n true' </dev/null 2>/dev/null; then
    out="$("${OWN[@]}" "sudo bash ~/$rdir/bootstrap.sh ~/$rdir $SETUP_USER ${SSH_HOST_PORT:-22}; rc=\$?; rm -rf ~/$rdir; exit \$rc" </dev/null)"
    printf '%s\n' "$out" | grep -v '^HOSTKEY ' || true
  else
    [ -t 0 ] || die "sudo on $SSH_HOST asks $SETUP_USER for a password: run this in your own terminal, where you can type it"
    # sudo asks on the server's terminal (ssh -tt): its prompt shows here as it comes; the password isn't echoed.
    bs_out="$SOCK_DIR/bootstrap-$TS.out"
    ssh -tt "${MC_OWNER_OPTS[@]}" "$SETUP_USER@$SSH_HOST" "sudo bash ~/$rdir/bootstrap.sh ~/$rdir $SETUP_USER ${SSH_HOST_PORT:-22}; rc=\$?; rm -rf ~/$rdir; exit \$rc" \
      | tr -d '\r' | tee "$bs_out" | grep -v '^HOSTKEY ' || true
    out="$(cat "$bs_out")"; rm -f "$bs_out"
  fi
  hk="$(printf '%s\n' "$out" | sed -n 's/^HOSTKEY \(ssh-ed25519 [A-Za-z0-9+/=]*\)$/\1/p' | tail -1)"
  [ -n "$hk" ] || die "the bootstrap didn't report the server's host key; nothing was pinned"
  echo "$HK_NAME $hk" > "$KNOWN_HOSTS"; chmod 600 "$KNOWN_HOSTS"
  ok "host key pinned as $HK_NAME: $(ssh-keygen -lf "$KNOWN_HOSTS" | cut -d' ' -f2)"
  remote_py identity --vm "$VM_NAME" --write >/dev/null || die "couldn't record $VM_NAME's identity"
  owner_check "after the bootstrap"
  wait_ssh admin 120 || die "metacomputer's own key doesn't sign in to $SSH_HOST as $ADMIN_USER"
fi
case "$ARCH_SEEN" in arm64) A=arm64 ;; amd64) A=x86_64 ;; *) die "$VM_NAME is $ARCH_SEEN: metacomputer sets up arm64 and amd64" ;; esac
[ "$ARCH" = detect ] || [ "$ARCH" = "$A" ] || die "$VM_NAME is $A, but its settings say $ARCH"
ARCH="$A"
ok "SSH as $ADMIN_USER with this computer's own key ($ARCH)"
T_SSH=$(date +%s)

# --- 2. the payload, then provision.sh ---------------------------------------------------------------------------
"$(dirname "$0")/fetch-artifacts.sh" remote "$ARCH"
if [ "$ARCH" = x86_64 ]; then CUA_DRIVER_TARBALL="$CUA_DRIVER_TARBALL_X86_64"; CUA_DRIVER_SHA256="$CUA_DRIVER_SHA256_X86_64"; fi
P="$(mktemp -d "$SOCK_DIR/payload.XXXXXX")"
mkdir -p "$P/payload/remote"
cp -R "$LAB_ROOT/provisioning/guest/." "$P/payload/"
cp -R "$LAB_ROOT/provisioning/remote/rootfs" "$P/payload/remote/"
cp "$LAB_ROOT/provisioning/remote/bootstrap.sh" "$P/payload/remote/"
cp "$CACHE_DIR/cua-driver-$CUA_DRIVER_VERSION/$CUA_DRIVER_TARBALL" "$P/payload/"
echo "$CUA_DRIVER_SHA256  $P/payload/$CUA_DRIVER_TARBALL" | shasum -a 256 -c - >/dev/null || die "the driver in the payload isn't the pinned one"
cp "$CACHE_DIR/mozilla/pinned-key.asc" "$P/payload/mozilla-repo-signing-key.gpg"
case " $BROWSERS " in *" chrome "*) cp "$CACHE_DIR/google/pinned-key.asc" "$P/payload/google-linux-signing-key.pub" ;; esac
cp "$SSH_DIR/fill.pub" "$SSH_DIR/proxy.pub" "$P/payload/"
remote_py key-lists --vm "$VM_NAME" --dir "$P/payload" || die "couldn't write the key lists"
{
  echo "GUEST_USER=$GUEST_USER"; echo "ADMIN_USER=$ADMIN_USER"
  echo "CUA_DRIVER_VERSION=$CUA_DRIVER_VERSION"; echo "CUA_DRIVER_TARBALL=$CUA_DRIVER_TARBALL"
  echo "CUA_DRIVER_SHA256=$CUA_DRIVER_SHA256"; echo "CUA_SRC_COMMIT=$CUA_SRC_COMMIT"; echo "BUILD_ID=$TS"
  echo "MCOMP_TARGET=remote"; echo "MCOMP_GUEST_ARCH=$([ "$ARCH" = x86_64 ] && echo amd64 || echo arm64)"
  echo "MCOMP_SSH_PORT=${SSH_HOST_PORT:-22}"
  echo "MCOMP_EGRESS=$(plan egress)"; echo "MCOMP_IDLE_STOP=$(plan idle_stop)"
  echo "MCOMP_BROWSERS=\"$BROWSERS\""
  echo "MCOMP_EXTRA_PACKAGES=\"$(echo $EXTRA_PACKAGES)\""
  echo "MCOMP_BROWSER_EXTENSIONS=\"$(echo ${BROWSER_EXTENSIONS:-})\""
  echo "MCOMP_VIRTUAL_AUDIO=${VIRTUAL_AUDIO:-off}"
  echo "SECRETS_PROXY_PORT=${SECRETS_PROXY_PORT:-18765}"
} > "$P/payload/versions.env"
(cd "$P/payload" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --uid 0 --gid 0 -czf ../payload.tgz .)
ok "payload: $(du -h "$P/payload.tgz" | cut -f1) ($CUA_DRIVER_TARBALL, sha256 checked)"
gssh admin 'sudo rm -rf /opt/metacomputer-payload && sudo install -d -m 0700 /opt/metacomputer-payload && sudo tar -xzf - -C /opt/metacomputer-payload --no-same-owner' \
  < "$P/payload.tgz" || die "couldn't upload the payload"
"$LAB_ROOT/scripts/safe-rmtree.sh" --quiet "$P"
BOOT1="$(gssh admin 'cat /proc/sys/kernel/random/boot_id')"
gssh admin "sudo rm -f /var/lib/metacomputer/provisioned /var/lib/metacomputer/failed; sudo systemd-run --quiet --collect --unit=metacomputer-setup-$TS bash /opt/metacomputer-payload/provision.sh /opt/metacomputer-payload" \
  || die "couldn't start the setup on $VM_NAME"
say "setting up the desktop on $VM_NAME (apt: about 5-10 minutes)"
last=""; start=$(date +%s)
while :; do
  st="$(gssh admin 'if test -f /var/lib/metacomputer/failed; then echo FAILED; elif test -f /var/lib/metacomputer/provisioned; then echo DONE; else sudo grep "^=== " /var/log/metacomputer-provision.log 2>/dev/null | tail -1; fi' 2>/dev/null || echo "(ssh unavailable)")"
  [ "$st" != "$last" ] && { echo "  … $st"; last="$st"; }
  case "$st" in
    DONE) break ;;
    FAILED) gssh admin 'sudo tail -60 /var/log/metacomputer-provision.log' || true
            [ "$KIND" = made ] || owner_check "after the failed setup"
            die "the setup failed (above)$([ "$KIND" = made ] || echo "; your server is left as the setup left it, your access checked: metacomputer --vm $VM_NAME ssh admin to look")" ;;
  esac
  [ $(( $(date +%s) - start )) -gt 3600 ] && die "the setup took over an hour"
  sleep 10
done
ok "set up in $(( ($(date +%s) - start) / 60 )) min"
[ "$KIND" = made ] || owner_check "after the setup"
gssh admin 'sudo cat /var/log/metacomputer-provision.log' > "$LOG_DIR/provision-$TS.log" 2>/dev/null || true

# --- 3. reboot into the desktop, check it, finalize ------------------------------------------------------------
say "rebooting into the desktop"
gssh admin 'sudo systemctl reboot' >/dev/null 2>&1 || true
start=$(date +%s)
while :; do
  b="$(gssh admin 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)"
  [ -n "$b" ] && [ "$b" != "$BOOT1" ] && break
  [ $(( $(date +%s) - start )) -gt 900 ] && die "$VM_NAME did not come back after the reboot"
  sleep 5
done
wait_session 300 || die "the desktop session never became ready (metacomputer --vm $VM_NAME ssh admin; sudo metacomputer-admin logs)"
ok "desktop session ready"
[ "$KIND" = made ] || owner_check "after the reboot, with the firewall loaded at boot"
"$LAB_ROOT/bin/metacomputer" _vnc-install
gssh ops /opt/metacomputer/bin/guest-status > "$LOG_DIR/guest-status-$TS.json"
"$PY" - "$LOG_DIR/guest-status-$TS.json" <<'EOF' || die "the desktop isn't ready (see the guest-status log)"
import json, sys
s = json.load(open(sys.argv[1]))
for k in ("xorg_running", "xfce_session_running", "session_env_published", "atspi_bus", "toolkit_accessibility"):
    print(f"  {'✓' if s.get(k) else '✗'} {k}")
print(f"  resolution {s.get('resolution')}, driver {s.get('driver_version')}, doctor rc {s.get('driver_doctor_rc')}")
sys.exit(0 if s.get("ready") else 1)
EOF
gssh admin "sudo bash -s $ADMIN_USER '$GUEST_USER'" < "$LAB_ROOT/provisioning/guest/finalize.sh"
gssh admin 'sync; test -f /var/lib/metacomputer/finalized && sudo -n -l | grep -q metacomputer-admin && ! sudo -n -l | grep -q "(ALL) NOPASSWD: ALL"' \
  || die "finalize did not take effect"
ok "finalized: $ADMIN_USER may run only metacomputer-admin with sudo; cloud-init is off"
# EGRESS as set, now that the setup's downloads are over: its own firewall, then the provider's rules (open to public
# addresses while it was set up), each read back. A computer this build made that can't be held to it is deleted.
want_egress="$(plan egress)"
got="$(gssh admin 'sudo /usr/local/sbin/metacomputer-admin egress status' 2>/dev/null | tail -1)"
[ "$got" = "$want_egress" ] || die "its own firewall reads back as '${got:-nothing}', not EGRESS=$want_egress"
if [ "$KIND" = made ]; then
  got="$(remote_py egress --vm "$VM_NAME" --set "$want_egress")" || die "$WHERE didn't take EGRESS=$want_egress (above)"
  [ "$got" = "$want_egress" ] || die "$WHERE's rules read back as '$got', not EGRESS=$want_egress"
fi
ok "EGRESS=$want_egress: in force in its own firewall$([ "$KIND" = made ] && echo " and in $WHERE's rules")"
ABORT_OP=""; SETUP_DONE=1; SETUP_OPEN=0   # set up: from here nothing is deleted or closed on a failure
# The computer's private host key now lives only in it; the Mac keeps the public half (known_hosts, the vault).
rm -f "$SSH_DIR/hostkey"
printf 'remote=1\nbuild=%s\narch=%s\n' "$TS" "$ARCH" > "$SSH_DIR/guest-features"
T1=$(date +%s)
launch="$("$PY" -c 'import json,sys
try: print(int(json.load(open(sys.argv[1])).get("launch_started") or 0))
except Exception: print(0)' "$RUN_DIR/remote.json" 2>/dev/null || echo 0)"
if [ "$KIND" = made ] && [ "$STATE_HAS_INSTANCE" = 0 ] && [ "$launch" -gt 0 ]; then
  ok "ready $(( T1 - launch )) s after the launch began (SSH after $(( T_SSH - launch )) s)"
else
  ok "ready in $(( T1 - T0 )) s"
fi
say "$VM_NAME is set up. Use it with: $(mc_cmd) status, then the metacomputer tools as on the Mac's VM"
