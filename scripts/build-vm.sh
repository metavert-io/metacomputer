#!/bin/bash
# Build the metacomputer guest from nothing, unattended:
#   official Ubuntu 24.04 arm64 cloud image -> raw Lume disk -> cloud-init seed
#   (users, SSH keys, pinned host key, payload) -> first boot provisions XFCE,
#   AT-SPI, apps and Cua Driver -> reboot into the desktop -> verify ->
#   finalize (narrow sudo, cloud-init off) -> stop -> clone the baseline.
#
#   scripts/build-vm.sh            refuses if the VM or baseline already exists
#   scripts/build-vm.sh --replace  deletes and rebuilds the working VM AND baseline
set -Eeuo pipefail
. "$(dirname "$0")/lib.sh"
trap 'die "failed at ${BASH_SOURCE[0]##*/} line $LINENO: $BASH_COMMAND"' ERR
vm_target_check   # which VM: never the defaults' VM on a guess (scripts/lib.sh)
vm_notice
# The optional event stream (docs/events.md): build_done or build_failed, however the build ends.
# Refusing to overwrite an existing VM sends nothing; an interrupted build (Ctrl-C) is info.
BUILD_QUIET_EXIT=0
trap 'rc=$?; if [ $rc = 0 ]; then emit_event lifecycle build_done info "The virtual computer $VM_NAME was built" "metacomputer start";
      elif [ $rc = 130 ] || [ $rc = 143 ]; then emit_event lifecycle build_failed info "Building the virtual computer $VM_NAME was interrupted" "metacomputer build --replace to start again" "exit=$rc";
      elif [ "$BUILD_QUIET_EXIT" = 0 ]; then emit_event lifecycle build_failed error "Building the virtual computer $VM_NAME failed (exit $rc)" "see ${BUILD_LOG:-the newest logs/build-*.log}" "exit=$rc"; fi' EXIT
REPLACE=0; [ "${1:-}" = "--replace" ] && REPLACE=1
TS="$(date +%Y%m%d-%H%M%S)"
BUILD_LOG="$LOG_DIR/build-$TS.log"
exec > >(tee -a "$BUILD_LOG") 2>&1
say "build $TS (log: ${BUILD_LOG#"$LAB_ROOT"/})"

# --- 0. prerequisites -------------------------------------------------------
# Every check that can refuse the build comes first: until the build is certain to go
# ahead, nothing of the existing VM's may change (its host key and known_hosts entry
# keep SSH to it working).
command -v "$LUME" >/dev/null || die "lume not found ($LUME)"
for p in $EXTRA_PACKAGES; do   # names go into the guest's provisioning script
  [[ "$p" =~ ^[a-z0-9][a-z0-9.+-]*$ ]] || die "EXTRA_PACKAGES: '$p' is not an Ubuntu package name"
done
case "${VIRTUAL_AUDIO:-off}" in on|off) ;; *) die "VIRTUAL_AUDIO must be on or off" ;; esac
# One build of a VM at a time, across checkouts and homes: this lock is held from before the first look at
# the VM until the build ends, so another build can't create or delete it in between.
lock_rc=0; lock_hold "$(vm_lock_name "$VM_NAME")" "${MCOMP_LOCK_WAIT:-10}" "a build of $VM_NAME" || lock_rc=$?
case "$lock_rc" in
  0) ;;
  1) BUILD_QUIET_EXIT=1; die "another build or change of the VM $VM_NAME is running ($LOCK_OWNER); nothing was changed. Wait for it to finish, then run this again" ;;
  *) die "couldn't make the private lock folder (MCOMP_LOCK_DIR, or the per-user temporary folder)" ;;
esac
# The identity rule, whichever VM this is (the main one too), before anything is deleted (the runner's VMs on
# --replace, the seed under RUN_DIR): every other settings file of this checkout reads cleanly, no name or folder of
# this VM's is another's, no folder of its is in another's or holds one, and no VM's folder is in the runner's VM
# storage or another place that gets deleted on its own (scripts/lib.sh mc_identity_check; it fails closed when the
# runner's storage can't be known). Checked here, again before the downloads, and again before the first deletion.
build_identity_check() { mc_identity_check --no-ports || die "not building $VM_NAME: $MC_REFUSAL. Nothing was deleted"; }
build_identity_check
# A listing that fails is never "absent": nothing is created or deleted on a guess.
for vm in "$VM_NAME" "$BASELINE_NAME"; do
  st="$(vm_lookup "$vm")" || die "couldn't list the VMs with $LUME, so it can't tell whether $vm exists; nothing was changed"
  if [ "$st" = present ] && [ $REPLACE -ne 1 ]; then
    BUILD_QUIET_EXIT=1; die "$vm already exists; use '$(mc_cmd) reset' to restore it, or build --replace to rebuild"
  fi
done
command -v ssh-keygen >/dev/null || die "ssh-keygen not found"
"$(dirname "$0")/build-tools.sh"
build_identity_check   # again before the downloads (fetch-artifacts.sh cleans up in .cache, a place no VM's folder is)
"$(dirname "$0")/fetch-artifacts.sh"
build_identity_check   # and before anything of the VM's is deleted: the settings may have changed meanwhile

# The build goes ahead. Until it finishes, this marker says that $VM_NAME is a build in progress (install.sh
# reads it: only then is a VM without its baseline an interrupted build, and even then it's replaced only on
# an explicit --replace-vm). It goes when the baseline exists.
BUILD_MARKER="$RUN_DIR/build-unfinished-$VM_NAME"
printf 'started=%s\nvm=%s\n' "$TS" "$VM_NAME" > "$BUILD_MARKER"
# Keys, then the host key's known_hosts entry.
for k in admin ops mcp fill proxy hostkey; do   # fill, proxy: the secrets accounts (mcfill, mcproxy)
  # A finished build deletes the host key's private half (below), so a rebuild makes a new
  # host key: drop the old .pub with it (bind the secrets vault again afterwards).
  [ -f "$SSH_DIR/$k" ] || { mkdir -p -m 700 "$SSH_DIR"; rm -f "$SSH_DIR/$k.pub"; ssh-keygen -q -t ed25519 -N '' -C "mcomp-$k" -f "$SSH_DIR/$k"; }
done
echo "[127.0.0.1]:$SSH_PORT $(cut -d' ' -f1,2 "$SSH_DIR/hostkey.pub")" > "$KNOWN_HOSTS"
# What the VM being built can do that older builds can't (bin/metacomputer checks it before attaching
# storage). Gone until this build finishes, so a failed build never claims support.
rm -f "$SSH_DIR/guest-features"

# --- 1. VM shell --------------------------------------------------------------
# A hub still serving the VM being replaced pinned its host key (lib/mcp_hub.py pin_identity) and would refuse the
# new one: it ends here, and the next client starts a hub for the new VM.
[ "$REPLACE" != 1 ] || ( export_options; "$PY" "$LAB_ROOT/lib/mcp_hub.py" stop --quiet ) >/dev/null 2>&1 || true
for vm in "$VM_NAME" "$BASELINE_NAME"; do
  st="$(vm_lookup "$vm")" || die "couldn't list the VMs with $LUME before replacing them; nothing was deleted"
  if [ "$st" = present ]; then
    # Deleted only on --replace, checked again here: a VM that appeared since the first look isn't ours to delete.
    [ $REPLACE -eq 1 ] || die "$vm appeared while this build was getting ready (made by something else); nothing was deleted"
    say "--replace: deleting $vm"
    vm_stop "$vm" --force
    runner_delete "$vm" || die "$vm wasn't deleted (above); nothing was replaced"
  fi
done
# Storage the old VM had attached (docs/storage.md): stop its watcher, sync S3 volumes once more, let go of
# the locks. Volumes and their files stay; the new VM attaches them at its first start.
( export_options; "$PY" "$LAB_ROOT/lib/storage.py" release --quiet ) || true
say "creating $VM_NAME ($VM_CPU vCPU, $VM_MEMORY, $VM_DISPLAY)"
"$LUME" create "$VM_NAME" --os linux --cpu "$VM_CPU" --memory "$VM_MEMORY" \
  --disk-size "$((VM_DISK_BYTES / 1024 / 1024 / 1024))GB" --display "$VM_DISPLAY" --network nat
# The new VM's folder, from Lume's own settings (~/.lume unless you configured another storage location).
# Its empty disk is replaced only when the folder is certainly this VM's: found without guessing, and with
# the MAC address Lume reports for $VM_NAME.
VMDIR="$(vm_dir "$VM_NAME")" || die "couldn't tell which folder holds $VM_NAME (above); nothing was replaced"
vm_dir_is "$VMDIR" "$VM_NAME" || die "$VMDIR isn't the folder of the VM just created ($VM_NAME: no disk.img, or another MAC address); nothing was replaced"
size="$(stat -f %z "$VMDIR/disk.img")"
rm -f "$VMDIR/disk.img"
"$QCOW2RAW_BIN" "$CACHE_DIR/ubuntu-$UBUNTU_RELEASE/$UBUNTU_IMAGE" "$VMDIR/disk.img" "$size"
ok "disk: Ubuntu $UBUNTU_RELEASE cloud image, $((size / 1024 / 1024 / 1024)) GiB sparse"
MAC="$(vm_mac "$VM_NAME")"

# --- 2. cloud-init seed (NoCloud, ISO9660 label cidata) ---------------------
# Every recursive delete goes through scripts/safe-rmtree.sh (lib/safedelete.py): never a folder another VM uses.
SEED="$RUN_DIR/seed"; "$LAB_ROOT/scripts/safe-rmtree.sh" "$SEED" "$RUN_DIR/seed.iso" || die "not deleting the old seed (above)"
mkdir -p "$SEED/payload"
cp -R "$LAB_ROOT/provisioning/guest/." "$SEED/payload/"
cp "$CACHE_DIR/cua-driver-$CUA_DRIVER_VERSION/$CUA_DRIVER_TARBALL" "$SEED/payload/"
cp "$CACHE_DIR/mozilla/pinned-key.asc" "$SEED/payload/mozilla-repo-signing-key.gpg"   # only the pinned key (fetch-artifacts.sh)
cp "$SSH_DIR/fill.pub" "$SEED/payload/fill.pub"   # the fill account's key (provision.sh installs it under /etc)
cp "$SSH_DIR/proxy.pub" "$SEED/payload/proxy.pub" # the proxy account's key (likewise)
# The desktop user's and the maintenance account's key lists, with the options cloud-init's bootstrap copies
# (user-data, below) carry: provision.sh installs them root-owned in /etc/ssh/metacomputer-keys/, the only
# place sshd reads keys from once it's provisioned (10-metacomputer.conf), and finalize.sh removes the copies
# in the homes. So nothing in the VM can add a key, or cut the Mac off by deleting ~/.ssh.
{ printf 'restrict,pty %s\n' "$(head -n 1 "$SSH_DIR/ops.pub")"
  printf 'restrict,command="/opt/metacomputer/bin/driver-mcp" %s\n' "$(head -n 1 "$SSH_DIR/mcp.pub")"
} > "$SEED/payload/keys-guest"
printf 'restrict,pty %s\n' "$(head -n 1 "$SSH_DIR/admin.pub")" > "$SEED/payload/keys-admin"
case " $BROWSERS " in *" chrome "*) cp "$CACHE_DIR/google/pinned-key.asc" "$SEED/payload/google-linux-signing-key.pub" ;; esac
{
  echo "GUEST_USER=$GUEST_USER"; echo "ADMIN_USER=$ADMIN_USER"
  echo "CUA_DRIVER_VERSION=$CUA_DRIVER_VERSION"; echo "CUA_DRIVER_TARBALL=$CUA_DRIVER_TARBALL"
  echo "CUA_DRIVER_SHA256=$CUA_DRIVER_SHA256"; echo "NET_GATEWAY_IP=$NET_GATEWAY_IP"
  echo "UBUNTU_IMAGE=$UBUNTU_IMAGE"; echo "UBUNTU_RELEASE=$UBUNTU_RELEASE"
  echo "LUME_VERSION=$LUME_VERSION"; echo "CUA_SRC_COMMIT=$CUA_SRC_COMMIT"; echo "BUILD_ID=$TS"
  echo "MCOMP_BROWSERS=\"$BROWSERS\""
  echo "MCOMP_EXTRA_PACKAGES=\"$(echo $EXTRA_PACKAGES)\""
  echo "MCOMP_VIRTUAL_AUDIO=${VIRTUAL_AUDIO:-off}"
  echo "SECRETS_PROXY_PORT=${SECRETS_PROXY_PORT:-18765}"
} > "$SEED/payload/versions.env"
(cd "$SEED/payload" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --uid 0 --gid 0 -czf ../payload.tgz .)
"$LAB_ROOT/scripts/safe-rmtree.sh" "$SEED/payload" || die "not deleting the seed's payload (above)"
TZ_NAME="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
"$PY" - "$SEED" "$SSH_DIR" "$TS" "$TZ_NAME" "$VM_HOSTNAME" "$GUEST_USER" "$ADMIN_USER" <<'EOF'
import json, sys, pathlib
seed, keys, ts, tz, host, guser, auser = sys.argv[1:]
k = lambda n: pathlib.Path(keys, n).read_text().strip()
def q(s): return json.dumps(s)  # JSON strings are valid YAML scalars
priv = k("hostkey")
ud = f"""#cloud-config
hostname: {host}
manage_etc_hosts: true
timezone: {q(tz)}
locale: en_US.UTF-8
ssh_pwauth: false
disable_root: true
ssh_deletekeys: true
ssh_genkeytypes: []
ssh_keys:
  ed25519_private: |
""" + "".join(f"    {l}\n" for l in priv.splitlines()) + f"""  ed25519_public: {q(k('hostkey.pub'))}
users:
  - name: {guser}
    gecos: metacomputer
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - {q('restrict,pty ' + k('ops.pub'))}
      - {q('restrict,command="/opt/metacomputer/bin/driver-mcp" ' + k('mcp.pub'))}
  - name: {auser}
    gecos: metacomputer maintenance
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - {q('restrict,pty ' + k('admin.pub'))}
growpart:
  mode: auto
  devices: ["/"]
runcmd:
  - [sh, -c, "set -e; mkdir -p /run/cua-seed /opt/metacomputer-payload; mount -o ro /dev/disk/by-label/cidata /run/cua-seed; tar -xzf /run/cua-seed/payload.tgz -C /opt/metacomputer-payload --no-same-owner; umount /run/cua-seed"]
  - [bash, /opt/metacomputer-payload/provision.sh, /opt/metacomputer-payload]
power_state:
  mode: reboot
  delay: now
  message: metacomputer provisioning finished, rebooting into the desktop
  timeout: 300
  condition: test -f /var/lib/metacomputer/provisioned
"""
p = pathlib.Path(seed)
(p / "user-data").write_text(ud)
(p / "meta-data").write_text(f"instance-id: metacomputer-{ts}\nlocal-hostname: {host}\n")
(p / "network-config").write_text(
    "version: 2\nethernets:\n  primary:\n    match:\n      name: \"en*\"\n    dhcp4: true\n    dhcp6: false\n")
EOF
chmod 600 "$SEED/user-data"
hdiutil makehybrid -quiet -o "$RUN_DIR/seed.iso" "$SEED" -iso -joliet -iso-volume-name cidata -joliet-volume-name cidata
"$LAB_ROOT/scripts/safe-rmtree.sh" "$SEED" || die "not deleting the seed (above)"
chmod 600 "$RUN_DIR/seed.iso"
ok "seed: $(du -h "$RUN_DIR/seed.iso" | cut -f1) (user-data, keys, payload)"

# --- 3. first boot: cloud-init provisions ----------------------------------
say "first boot (provisioning runs inside the guest: about 2 minutes on a fast connection, 10 or more on a slow one)"
vm_start "$VM_NAME" "$RUN_DIR/seed.iso"
wait_ssh admin 600 || die "admin SSH never came up (see logs/lume-$VM_NAME.log)"
ok "admin SSH up"
BOOT1="$(gssh admin 'cat /proc/sys/kernel/random/boot_id')"
last=""
start=$(date +%s)
while :; do
  st="$(gssh admin 'if test -f /var/lib/metacomputer/failed; then echo FAILED; elif test -f /var/lib/metacomputer/provisioned; then echo DONE; else grep "^=== " /var/log/metacomputer-provision.log 2>/dev/null | tail -1 || cloud-init status 2>/dev/null; fi' 2>/dev/null || echo "(ssh unavailable)")"
  [ "$st" != "$last" ] && { echo "  … $st"; last="$st"; }
  case "$st" in
    DONE) break ;;
    FAILED) gssh admin 'sudo tail -60 /var/log/metacomputer-provision.log' || true; die "guest provisioning failed (VM left running for inspection: metacomputer ssh admin)" ;;
    "(ssh unavailable)") gssh admin true 2>/dev/null || true ;;
  esac
  [ $(( $(date +%s) - start )) -gt 3600 ] && die "provisioning took over an hour"
  sleep 10
done
ok "provisioned in $(( ($(date +%s) - start) / 60 )) min"
gssh admin 'sudo cat /var/log/metacomputer-provision.log' > "$LOG_DIR/provision-$TS.log" 2>/dev/null || true

# --- 4. reboot into the desktop --------------------------------------------
say "waiting for the reboot into XFCE"
start=$(date +%s)
while :; do
  b="$(gssh admin 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null || true)"
  [ -n "$b" ] && [ "$b" != "$BOOT1" ] && break
  [ $(( $(date +%s) - start )) -gt 600 ] && die "guest did not reboot after provisioning"
  sleep 5
done
wait_session 300 || die "desktop session never became ready"
ok "desktop session ready"

# --- 5. live-view password (VNC_PASSWORD_STORE: 0600 file or your command) --
"$LAB_ROOT/bin/metacomputer" _vnc-install

# --- 6. verify, finalize ----------------------------------------------------
gssh ops /opt/metacomputer/bin/guest-status > "$LOG_DIR/guest-status-$TS.json"
"$PY" - "$LOG_DIR/guest-status-$TS.json" <<'EOF' || die "guest not ready (see the guest-status log)"
import json, sys
s = json.load(open(sys.argv[1]))
for k in ("xorg_running", "xfce_session_running", "session_env_published", "atspi_bus", "toolkit_accessibility"):
    print(f"  {'✓' if s.get(k) else '✗'} {k}")
print(f"  resolution {s.get('resolution')}, driver {s.get('driver_version')}, doctor rc {s.get('driver_doctor_rc')}")
sys.exit(0 if s.get("ready") else 1)
EOF
gssh admin "sudo bash -s cuaadmin '$GUEST_USER'" < "$LAB_ROOT/provisioning/guest/finalize.sh"
gssh admin 'sync; test -f /var/lib/metacomputer/finalized && sudo -n -l | grep -q metacomputer-admin' \
  || die "finalize did not take effect"
ok "finalized"

# --- 7. baseline -------------------------------------------------------------
say "stopping and cloning the clean baseline"
gssh ops 'rm -rf ~/metacomputer-runs/* 2>/dev/null; true'
vm_stop "$VM_NAME"
"$LUME" clone "$VM_NAME" "$BASELINE_NAME"
rm -f "$RUN_DIR/seed.iso"
ok "baseline $BASELINE_NAME created"
rm -f "$BUILD_MARKER"
# The VM's private host key now lives only in its disk and the baseline (reset restores
# it from there). On the Mac only the public half is needed (known_hosts, the secrets
# vault's pin); anything able to read the private half could pose as the VM.
rm -f "$SSH_DIR/hostkey"
# storage=1: storage-helper and `metacomputer-admin storage` (volumes, Mac folders, reference disks).
printf 'storage=1\nbuild=%s\n' "$TS" > "$SSH_DIR/guest-features"
ok "removed the VM's private host key from $SSH_DIR (the VM keeps it; a rebuild makes a new one)"
say "build complete. Start it with: $(mc_cmd) start"
