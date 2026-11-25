# Vercel Sandbox's own steps in scripts/remote-build.sh (the plan's kind=provider): each pv_vercel_build_HOOK runs where
# the build finds the hook, in the build's own shell (its variables, functions and set -Eeuo pipefail). Sourced by
# scripts/remote-build.sh, never run.

pv_vercel_build_what() { printf '%s' "a sandbox at Vercel (${VERCEL_SANDBOX:-$VM_NAME}, ${SIZE:-2x4}${REGION:+ in $REGION})"; }

# 1. The sandbox (made, or set up again), then its first step through Vercel's own API.
pv_vercel_build_first_step() {
  remote_can_ask || die "making a computer on Vercel Sandbox needs VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID in this command's environment (docs/vercel.md)"
  if [ "$STATE_HAS_INSTANCE" = 0 ]; then
    op="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    [[ "$op" =~ ^[0-9a-f]{32}$ ]] || die "couldn't make an operation id"
    ABORT_OP="$op"           # from here, a failure deletes what this operation made, and only that (cleanup_failed)
    open_setup
    remote_py create --vm "$VM_NAME" --op "$op" || die "the sandbox wasn't made (above); metacomputer --vm $VM_NAME status shows what's there"
  else
    say "the sandbox exists: setting it up again"
    remote_py wake --vm "$VM_NAME" >/dev/null || die "couldn't start the sandbox (above)"
    WOKEN=1                   # a failure stops it again (cleanup_failed)
    # One that's set up isn't set up again (and its policy isn't opened): asked through Vercel's API, before anything.
    if remote_py exec --vm "$VM_NAME" --timeout 60 -- test -f /var/lib/metacomputer/finalized >/dev/null 2>&1; then
      BUILD_QUIET_EXIT=1
      die "$VM_NAME is set up already (metacomputer --vm $VM_NAME status). To set it up from scratch: metacomputer vm delete $VM_NAME, then vm create it again"
    fi
    open_setup
    RESUMING=1
    remote_py egress --vm "$VM_NAME" --set internet --setup >/dev/null || die "couldn't open Vercel's network policy for the setup's downloads (above)"
  fi
  # The first step through Vercel's own API (as root): the accounts, their key lists, the boot script and the relay;
  # the boot script keeps Vercel's agent port from every account but root and starts sshd on the sandbox's loopback.
  # The host key it reports came over that authenticated API: pinned here, never learned from a first connection.
  BS="$(mktemp -d "$SOCK_DIR/bootstrap.XXXXXX")"
  cp "$LAB_ROOT/providers/vercel/guest/bootstrap.sh" "$BS/"
  cp -R "$LAB_ROOT/providers/vercel/guest/rootfs" "$BS/"
  remote_py key-lists --vm "$VM_NAME" --dir "$BS" || die "couldn't write the key lists"
  say "bootstrap through Vercel's API: accounts, the agent port blocked for all but root, sshd on the sandbox's loopback"
  out="$( (cd "$BS" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -cf - bootstrap.sh keys-guest keys-admin rootfs) \
    | remote_py exec --vm "$VM_NAME" --timeout 900 --stdin -- bash -c 'd="$(mktemp -d)"; tar -xf - -C "$d" --no-same-owner; bash "$d/bootstrap.sh" "$d"; rc=$?; rm -rf "$d"; exit $rc')" \
    || { printf '%s\n' "$out" | grep -v '^HOSTKEY ' || true; die "the bootstrap failed (above)"; }
  "$LAB_ROOT/scripts/safe-rmtree.sh" --quiet "$BS"
  printf '%s\n' "$out" | grep -v '^HOSTKEY ' || true
  hk="$(printf '%s\n' "$out" | sed -n 's/^HOSTKEY \(ssh-ed25519 [A-Za-z0-9+/=]*\)$/\1/p' | tail -1)"
  [ -n "$hk" ] || die "the bootstrap didn't report the sandbox's host key; nothing was pinned"
  if [ "$RESUMING" = 1 ] && [ -s "$KNOWN_HOSTS" ] && ! grep -qxF "$HK_NAME $hk" "$KNOWN_HOSTS"; then
    die "the sandbox's host key isn't the one pinned for $VM_NAME: it isn't the computer this Mac set up (metacomputer vm delete $VM_NAME, then vm create it again)"
  fi
  echo "$HK_NAME $hk" > "$KNOWN_HOSTS"; chmod 600 "$KNOWN_HOSTS"
  ok "host key pinned as $HK_NAME (read through Vercel's API): $(ssh-keygen -lf "$KNOWN_HOSTS" | cut -d' ' -f2)"
  remote_py identity --vm "$VM_NAME" --write >/dev/null || die "couldn't record $VM_NAME's identity"
  MC_T_USER=""
  wait_ssh admin 120 || die "metacomputer's own key doesn't sign in to the sandbox as $ADMIN_USER (through the broker)"
  if gssh admin 'test -f /var/lib/metacomputer/finalized'; then
    BUILD_QUIET_EXIT=1; SETUP_OPEN=0
    die "$VM_NAME is set up already (metacomputer --vm $VM_NAME status). To set it up from scratch: metacomputer vm delete $VM_NAME, then vm create it again"
  fi
  keep_awake_start            # a connection held open: the broker keeps the session going while one is
  setup_deadline_check
  ARCH_SEEN="$(gssh admin 'dpkg --print-architecture')"
}

# 3. No reboot: the boot script starts the desktop (Xvfb, then XFCE), through Vercel's API, as at every start.
pv_vercel_build_desktop() {
  say "starting the desktop (the boot script, through Vercel's API, as at every start)"
  remote_py exec --vm "$VM_NAME" --timeout 180 -- /opt/metacomputer/vercel/boot >/dev/null \
    || die "the boot script didn't start the desktop (above; metacomputer --vm $VM_NAME logs)"
}

# EGRESS, set up: Vercel's network policy, outside it, is the one layer (a sandbox has no egress firewall of its own).
pv_vercel_build_egress_ok() { ok "EGRESS=$1: in force in Vercel's network policy, outside the computer"; }

pv_vercel_build_finish() {
  # Set up: the setup's exception to the network policy's rule (widening only at your own terminal) is over for good.
  remote_py setup-done --vm "$VM_NAME" || die "couldn't record that $VM_NAME is set up"
  keep_awake_stop || true     # (the connection it held ends with a status of its own)
  # Its baseline (metacomputer reset goes back to it): a snapshot at Vercel, which stops the sandbox; it's started again.
  remote_py checkpoint --vm "$VM_NAME" create baseline --replace >/dev/null || die "couldn't make the baseline snapshot (above)"
  MC_REMOTE_STATUS=""; MC_T_USER=""
  wait_session 180 || die "the desktop didn't come back after the baseline snapshot (metacomputer --vm $VM_NAME logs)"
  ok "baseline: a snapshot at Vercel (metacomputer --vm $VM_NAME reset goes back to it); taking it restarted the computer"
}
