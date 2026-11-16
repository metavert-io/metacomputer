#!/bin/bash
# Build the virtual-computer guest from nothing, unattended:
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
REPLACE=0; [ "${1:-}" = "--replace" ] && REPLACE=1
TS="$(date +%Y%m%d-%H%M%S)"
BUILD_LOG="$LOG_DIR/build-$TS.log"
exec > >(tee -a "$BUILD_LOG") 2>&1
say "build $TS (log: logs/build-$TS.log)"

# --- 0. prerequisites -------------------------------------------------------
command -v "$LUME" >/dev/null || die "lume not found ($LUME)"
for k in admin ops mcp hostkey; do
  [ -f "$SSH_DIR/$k" ] || { mkdir -p -m 700 "$SSH_DIR"; ssh-keygen -q -t ed25519 -N '' -C "vc-$k" -f "$SSH_DIR/$k"; }
done
echo "[127.0.0.1]:$SSH_PORT $(cut -d' ' -f1,2 "$SSH_DIR/hostkey.pub")" > "$KNOWN_HOSTS"
"$(dirname "$0")/build-tools.sh"
"$(dirname "$0")/fetch-artifacts.sh"

# --- 1. VM shell --------------------------------------------------------------
for vm in "$VM_NAME" "$BASELINE_NAME"; do
  if vm_exists "$vm"; then
    [ $REPLACE -eq 1 ] || die "$vm already exists; use 'virtual-computer reset' to restore it, or build --replace to rebuild"
    say "--replace: deleting $vm"
    vm_stop "$vm" --force
    "$LUME" delete "$vm" --force
  fi
done
say "creating $VM_NAME ($VM_CPU vCPU, $VM_MEMORY, $VM_DISPLAY)"
"$LUME" create "$VM_NAME" --os linux --cpu "$VM_CPU" --memory "$VM_MEMORY" \
  --disk-size "$((VM_DISK_BYTES / 1024 / 1024 / 1024))GB" --display "$VM_DISPLAY" --network nat
VMDIR="$HOME/.lume/$VM_NAME"   # Lume's default ("home") storage location
[ -f "$VMDIR/disk.img" ] || die "unexpected Lume layout: no $VMDIR/disk.img"
size="$(stat -f %z "$VMDIR/disk.img")"
rm -f "$VMDIR/disk.img"
"$QCOW2RAW_BIN" "$CACHE_DIR/ubuntu-$UBUNTU_RELEASE/$UBUNTU_IMAGE" "$VMDIR/disk.img" "$size"
ok "disk: Ubuntu $UBUNTU_RELEASE cloud image, $((size / 1024 / 1024 / 1024)) GiB sparse"
MAC="$(vm_mac "$VM_NAME")"

# --- 2. cloud-init seed (NoCloud, ISO9660 label cidata) ---------------------
SEED="$RUN_DIR/seed"; rm -rf "$SEED" "$RUN_DIR/seed.iso"; mkdir -p "$SEED/payload"
cp -R "$LAB_ROOT/provisioning/guest/." "$SEED/payload/"
cp "$CACHE_DIR/cua-driver-$CUA_DRIVER_VERSION/$CUA_DRIVER_TARBALL" "$SEED/payload/"
cp "$CACHE_DIR/mozilla/repo-signing-key.gpg" "$SEED/payload/mozilla-repo-signing-key.gpg"
case " $BROWSERS " in *" chrome "*) cp "$CACHE_DIR/google/linux_signing_key.pub" "$SEED/payload/google-linux-signing-key.pub" ;; esac
{
  echo "GUEST_USER=$GUEST_USER"; echo "ADMIN_USER=$ADMIN_USER"
  echo "CUA_DRIVER_VERSION=$CUA_DRIVER_VERSION"; echo "CUA_DRIVER_TARBALL=$CUA_DRIVER_TARBALL"
  echo "CUA_DRIVER_SHA256=$CUA_DRIVER_SHA256"; echo "NET_GATEWAY_IP=$NET_GATEWAY_IP"
  echo "UBUNTU_IMAGE=$UBUNTU_IMAGE"; echo "UBUNTU_RELEASE=$UBUNTU_RELEASE"
  echo "LUME_VERSION=$LUME_VERSION"; echo "CUA_SRC_COMMIT=$CUA_SRC_COMMIT"; echo "BUILD_ID=$TS"
  echo "VC_BROWSERS=\"$BROWSERS\""
} > "$SEED/payload/versions.env"
(cd "$SEED/payload" && COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --uid 0 --gid 0 -czf ../payload.tgz .)
rm -rf "$SEED/payload"
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
    gecos: Cua automation
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - {q('restrict,pty ' + k('ops.pub'))}
      - {q('restrict,command="/opt/virtual-computer/bin/driver-mcp" ' + k('mcp.pub'))}
  - name: {auser}
    gecos: virtual-computer maintenance
    shell: /bin/bash
    lock_passwd: true
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - {q('restrict,pty ' + k('admin.pub'))}
growpart:
  mode: auto
  devices: ["/"]
runcmd:
  - [sh, -c, "set -e; mkdir -p /run/cua-seed /opt/virtual-computer-payload; mount -o ro /dev/disk/by-label/cidata /run/cua-seed; tar -xzf /run/cua-seed/payload.tgz -C /opt/virtual-computer-payload --no-same-owner; umount /run/cua-seed"]
  - [bash, /opt/virtual-computer-payload/provision.sh, /opt/virtual-computer-payload]
power_state:
  mode: reboot
  delay: now
  message: virtual-computer provisioning finished, rebooting into the desktop
  timeout: 300
  condition: test -f /var/lib/virtual-computer/provisioned
"""
p = pathlib.Path(seed)
(p / "user-data").write_text(ud)
(p / "meta-data").write_text(f"instance-id: virtual-computer-{ts}\nlocal-hostname: {host}\n")
(p / "network-config").write_text(
    "version: 2\nethernets:\n  primary:\n    match:\n      name: \"en*\"\n    dhcp4: true\n    dhcp6: false\n")
EOF
chmod 600 "$SEED/user-data"
hdiutil makehybrid -quiet -o "$RUN_DIR/seed.iso" "$SEED" -iso -joliet -iso-volume-name cidata -joliet-volume-name cidata
rm -rf "$SEED"
chmod 600 "$RUN_DIR/seed.iso"
ok "seed: $(du -h "$RUN_DIR/seed.iso" | cut -f1) (user-data, keys, payload)"

# --- 3. first boot: cloud-init provisions ----------------------------------
say "first boot (provisioning runs inside the guest; ~10-25 min)"
vm_start "$VM_NAME" "$RUN_DIR/seed.iso"
wait_ssh admin 600 || die "admin SSH never came up (see logs/lume-$VM_NAME.log)"
ok "admin SSH up"
BOOT1="$(gssh admin 'cat /proc/sys/kernel/random/boot_id')"
last=""
start=$(date +%s)
while :; do
  st="$(gssh admin 'if test -f /var/lib/virtual-computer/failed; then echo FAILED; elif test -f /var/lib/virtual-computer/provisioned; then echo DONE; else grep "^=== " /var/log/virtual-computer-provision.log 2>/dev/null | tail -1 || cloud-init status 2>/dev/null; fi' 2>/dev/null || echo "(ssh unavailable)")"
  [ "$st" != "$last" ] && { echo "  … $st"; last="$st"; }
  case "$st" in
    DONE) break ;;
    FAILED) gssh admin 'sudo tail -60 /var/log/virtual-computer-provision.log' || true; die "guest provisioning failed (VM left running for inspection: virtual-computer ssh admin)" ;;
    "(ssh unavailable)") gssh admin true 2>/dev/null || true ;;
  esac
  [ $(( $(date +%s) - start )) -gt 3600 ] && die "provisioning took over an hour"
  sleep 10
done
ok "provisioned in $(( ($(date +%s) - start) / 60 )) min"
gssh admin 'sudo cat /var/log/virtual-computer-provision.log' > "$LOG_DIR/provision-$TS.log" 2>/dev/null || true

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
"$LAB_ROOT/bin/virtual-computer" _vnc-install

# --- 6. verify, finalize ----------------------------------------------------
gssh ops /opt/virtual-computer/bin/guest-status > "$LOG_DIR/guest-status-$TS.json"
"$PY" - "$LOG_DIR/guest-status-$TS.json" <<'EOF' || die "guest not ready (see the guest-status log)"
import json, sys
s = json.load(open(sys.argv[1]))
for k in ("xorg_running", "xfce_session_running", "session_env_published", "atspi_bus", "toolkit_accessibility"):
    print(f"  {'✓' if s.get(k) else '✗'} {k}")
print(f"  resolution {s.get('resolution')}, driver {s.get('driver_version')}, doctor rc {s.get('driver_doctor_rc')}")
sys.exit(0 if s.get("ready") else 1)
EOF
gssh admin 'sudo bash -s cuaadmin' < "$LAB_ROOT/provisioning/guest/finalize.sh"
gssh admin 'sync; test -f /var/lib/virtual-computer/finalized && sudo -n -l | grep -q virtual-computer-admin' \
  || die "finalize did not take effect"
ok "finalized"

# --- 7. baseline -------------------------------------------------------------
say "stopping and cloning the clean baseline"
gssh ops 'rm -rf ~/virtual-computer-runs/* 2>/dev/null; true'
vm_stop "$VM_NAME"
"$LUME" clone "$VM_NAME" "$BASELINE_NAME"
rm -f "$RUN_DIR/seed.iso"
ok "baseline $BASELINE_NAME created"
say "build complete. Start it with: virtual-computer start"
