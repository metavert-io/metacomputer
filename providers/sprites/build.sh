# Fly Sprites' own steps in scripts/remote-build.sh (the plan's kind=provider): each pv_sprites_build_HOOK runs where
# the build finds the hook, in the build's own shell (its variables, functions and set -Eeuo pipefail). Sourced by
# scripts/remote-build.sh, never run.

pv_sprites_build_what() { printf '%s' "a Sprite on Fly (${SPRITE_NAME:-$VM_NAME} in ${SPRITE_ORG:-?})"; }

# 1. The Sprite (made, or set up again), then its first step through Fly's own channel.
pv_sprites_build_first_step() {
  remote_can_ask || die "Fly's sprite command isn't installed: install it and sign in (sprite login), then run this again (docs/sprites.md)"
  if [ "$STATE_HAS_INSTANCE" = 0 ]; then
    op="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    [[ "$op" =~ ^[0-9a-f]{32}$ ]] || die "couldn't make an operation id"
    ABORT_OP="$op"           # from here, a failure deletes what this operation made, and only that (cleanup_failed)
    open_setup
    remote_py create --vm "$VM_NAME" --op "$op" || die "the Sprite wasn't made (above); metacomputer --vm $VM_NAME status shows what's there"
  else
    say "the Sprite exists: setting it up again"
    # One that's set up isn't set up again (and its policy isn't opened): asked through Fly's channel, before anything.
    if remote_py exec --vm "$VM_NAME" --timeout 60 -- test -f /var/lib/metacomputer/finalized >/dev/null 2>&1; then
      BUILD_QUIET_EXIT=1
      die "$VM_NAME is set up already (metacomputer --vm $VM_NAME status). To set it up from scratch: metacomputer vm delete $VM_NAME, then vm create it again"
    fi
    open_setup
    RESUMING=1
    # The setup's downloads: Fly's network policy lets every name out until the setup ends (then EGRESS).
    remote_py egress --vm "$VM_NAME" --set internet --setup >/dev/null || die "couldn't open Fly's network policy for the setup's downloads (above)"
  fi
  # The first step through Fly's own channel (sprite exec, as the Sprite's `sprite` account, with sudo): the accounts,
  # their key lists and sshd as a Sprites service on the Sprite's loopback, with /.sprite hidden from its sessions.
  # The host key it reports came over that authenticated channel: pinned here, never learned from a first connection.
  BS="$(mktemp -d "$SOCK_DIR/bootstrap.XXXXXX")"
  cp "$LAB_ROOT/providers/sprites/guest/bootstrap.sh" "$BS/"
  cp -R "$LAB_ROOT/providers/sprites/guest/rootfs" "$BS/"
  remote_py key-lists --vm "$VM_NAME" --dir "$BS" || die "couldn't write the key lists"
  say "bootstrap through Fly's own channel (sprite exec): accounts, and sshd on the Sprite's loopback with /.sprite hidden"
  out="$( (cd "$BS" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -cf - bootstrap.sh keys-guest keys-admin rootfs) \
    | remote_py exec --vm "$VM_NAME" --timeout 900 --stdin -- sudo -n bash -c 'd="$(mktemp -d)"; tar -xf - -C "$d" --no-same-owner; bash "$d/bootstrap.sh" "$d"; rc=$?; rm -rf "$d"; exit $rc')" \
    || { printf '%s\n' "$out" | grep -v '^HOSTKEY ' || true; die "the bootstrap failed (above)"; }
  "$LAB_ROOT/scripts/safe-rmtree.sh" --quiet "$BS"
  printf '%s\n' "$out" | grep -v '^HOSTKEY ' || true
  hk="$(printf '%s\n' "$out" | sed -n 's/^HOSTKEY \(ssh-ed25519 [A-Za-z0-9+/=]*\)$/\1/p' | tail -1)"
  [ -n "$hk" ] || die "the bootstrap didn't report the Sprite's host key; nothing was pinned"
  if [ "$RESUMING" = 1 ] && [ -s "$KNOWN_HOSTS" ] && ! grep -qxF "$HK_NAME $hk" "$KNOWN_HOSTS"; then
    die "the Sprite's host key isn't the one pinned for $VM_NAME: it isn't the computer this Mac set up (metacomputer vm delete $VM_NAME, then vm create it again)"
  fi
  echo "$HK_NAME $hk" > "$KNOWN_HOSTS"; chmod 600 "$KNOWN_HOSTS"
  ok "host key pinned as $HK_NAME (read through Fly's channel): $(ssh-keygen -lf "$KNOWN_HOSTS" | cut -d' ' -f2)"
  remote_py identity --vm "$VM_NAME" --write >/dev/null || die "couldn't record $VM_NAME's identity"
  MC_T_USER=""
  wait_ssh admin 120 || die "metacomputer's own key doesn't sign in to the Sprite as $ADMIN_USER (through Fly's tunnel)"
  if gssh admin 'test -f /var/lib/metacomputer/finalized'; then
    BUILD_QUIET_EXIT=1; SETUP_OPEN=0
    die "$VM_NAME is set up already (metacomputer --vm $VM_NAME status). To set it up from scratch: metacomputer vm delete $VM_NAME, then vm create it again"
  fi
  keep_awake_start
  setup_deadline_check
  ARCH_SEEN="$(gssh admin 'dpkg --print-architecture')"
}

# 3. No reboot: the desktop is two Sprites services (xvfb, then desktop), made through Fly's own channel (sessions
# can't reach the Sprite's API); Fly starts them again after a restore or a restart.
pv_sprites_build_desktop() {
  say "starting the desktop as Sprites services (xvfb, desktop)"
  remote_py exec --vm "$VM_NAME" --timeout 120 -- sudo -n /opt/metacomputer/sprite/services desktop >/dev/null \
    || die "couldn't create the desktop's services (above)"
}

# EGRESS, set up: Fly's network policy, outside it, is the one layer (a Sprite has no firewall of its own).
pv_sprites_build_egress_ok() { ok "EGRESS=$1: in force in Fly's network policy, outside the computer"; }

pv_sprites_build_finish() {
  # Set up: the setup's exception to the network policy's rule (widening only at your own terminal) is over for good.
  remote_py setup-done --vm "$VM_NAME" || die "couldn't record that $VM_NAME is set up"
  # Its baseline (metacomputer reset goes back to it): a checkpoint at Fly, taken while it runs.
  remote_py checkpoint --vm "$VM_NAME" create baseline --replace >/dev/null || die "couldn't make the baseline checkpoint (above)"
  ok "baseline: a checkpoint at Fly (metacomputer --vm $VM_NAME reset goes back to it)"
}
