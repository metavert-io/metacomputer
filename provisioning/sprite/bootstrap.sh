#!/bin/bash
# metacomputer bootstrap for a Fly Sprite (docs/sprites.md). Runs once as root, through Fly's own authenticated channel
# (`sprite exec`, from lib/remote.py exec), before metacomputer's SSH exists; then everything else goes over SSH, as
# for any computer elsewhere (scripts/remote-build.sh).
# It makes the two accounts metacomputer signs in with, each reached only with keys made on the Mac for this computer:
#   cua        the desktop user, no sudo (its keys: the shell, the driver, the live view's tunnel)
#   cuaadmin   maintenance: full sudo only until the setup ends (finalize.sh then narrows it to metacomputer-admin)
# and the SSH server, as a Sprites service: listening on the Sprite's own loopback (reached through Fly's tunnel), in a
# private mount namespace where the Sprite's management API (/.sprite) is hidden (hide-sprite-api). The Sprite's own
# `sprite` account (Fly's exec and services, with sudo) stays as Fly made it: it's the controller's, like the owner
# of the Mac for a VM there.
# Its folder ($1) holds keys-guest and keys-admin (authorized_keys lines, options included) and rootfs/ (this folder's
# rootfs: the service scripts and the SSH listen settings). It prints the host's ed25519 public key last, which the
# Mac pins (read over Fly's authenticated channel, never from a first connection). Running it again changes nothing
# that's already right.
set -Eeuo pipefail
DIR="${1:?usage: bootstrap.sh DIR}"
GUSER=cua
AUSER=cuaadmin
fail() { echo "bootstrap: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "run it as root"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && case "${VERSION_ID:-}" in 24.04|26.04) true ;; *) false ;; esac \
  || fail "this is ${PRETTY_NAME:-not Ubuntu}: metacomputer sets up a Sprite on Ubuntu 24.04 or 26.04"
[ "$(dpkg --print-architecture)" = amd64 ] || fail "$(dpkg --print-architecture) isn't amd64 (Sprites are x86_64)"
for f in keys-guest keys-admin rootfs/opt/metacomputer/sprite/svc-sshd; do [ -s "$DIR/$f" ] || fail "no $f in $DIR"; done
[ -e /.sprite/api.sock ] || fail "this isn't a Sprite (no /.sprite/api.sock)"
export DEBIAN_FRONTEND=noninteractive

# The SSH server (Fly's image has none) and what the service scripts use (tini: the session's init).
if ! command -v sshd >/dev/null || ! command -v nsenter >/dev/null || [ ! -x /usr/bin/tini ]; then
  apt-get -y -q -o Acquire::Retries=5 update >/dev/null
  apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install --no-install-recommends \
    openssh-server util-linux procps tini >/dev/null || fail "couldn't install openssh-server and tini"
fi

install -d -o root -g root -m 0755 /etc/metacomputer /opt/metacomputer /opt/metacomputer/sprite /var/lib/metacomputer
cp -r --preserve=mode,timestamps "$DIR/rootfs/." /
chown -R root:root /opt/metacomputer/sprite /etc/ssh/sshd_config.d/05-metacomputer-sprite.conf
chmod 0755 /opt/metacomputer/sprite/*
date -Is > /etc/metacomputer/sprite

for u in "$GUSER" "$AUSER"; do
  getent passwd "$u" >/dev/null || useradd --create-home --shell /bin/bash --comment "metacomputer" "$u"
  passwd -l "$u" >/dev/null
done
for g in sudo admin adm wheel lxd docker; do gpasswd -d "$GUSER" "$g" >/dev/null 2>&1 || true; done

# The key lists, root-owned, outside the homes (sshd reads only these once the setup's settings are in), and the same
# lines in the homes until then (finalize.sh removes those).
install -d -o root -g root -m 0755 /etc/ssh/metacomputer-keys
install -o root -g root -m 0644 "$DIR/keys-guest" "/etc/ssh/metacomputer-keys/$GUSER"
install -o root -g root -m 0644 "$DIR/keys-admin" "/etc/ssh/metacomputer-keys/$AUSER"
for pair in "$GUSER:keys-guest" "$AUSER:keys-admin"; do
  u="${pair%%:*}"; home="$(getent passwd "$u" | cut -d: -f6)"
  install -d -o "$u" -g "$u" -m 0700 "$home/.ssh"
  install -o "$u" -g "$u" -m 0600 "$DIR/${pair#*:}" "$home/.ssh/authorized_keys"
done
# Until the setup's own SSH settings (provision.sh): keys only, these two accounts, no forwarding.
cat > /etc/ssh/sshd_config.d/09-metacomputer-bootstrap.conf <<CONF
# metacomputer bootstrap (Fly Sprites): until the setup's settings are in place (provision.sh removes this file).
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers $GUSER $AUSER
DisableForwarding yes
CONF

# Full sudo for the maintenance account until the setup ends: finalize.sh replaces it with the narrow rule.
printf '%s\n' "# metacomputer setup (bootstrap.sh): removed by finalize.sh when the setup ends" \
  "$AUSER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/89-metacomputer-setup
chmod 0440 /etc/sudoers.d/89-metacomputer-setup
visudo -cf /etc/sudoers.d/89-metacomputer-setup >/dev/null

[ -s /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
install -d -m 0755 /run/sshd
sshd -t || fail "sshd refused its settings"
# Not Ubuntu's own ssh service (it needs systemd): the Sprites services session and sshd, so every session runs in
# namespaces where /.sprite is covered and nothing outside them is visible.
/opt/metacomputer/sprite/services sshd >/dev/null || fail "couldn't create the session and sshd services"
for _ in $(seq 1 50); do
  if (exec 3<>/dev/tcp/127.0.0.1/22) 2>/dev/null; then break; fi
  sleep 0.2
done
(exec 3<>/dev/tcp/127.0.0.1/22) 2>/dev/null || fail "sshd isn't listening on 127.0.0.1:22"
# sshd must be in the session: its namespaces aren't this one's, and /.sprite is covered there.
sp="$(pgrep -x sshd | while read -r p; do [ "$(readlink "/proc/$p/ns/pid")" != "$(readlink /proc/self/ns/pid)" ] && echo "$p"; done | head -n 1)"
[ -n "$sp" ] && grep -q ' /.sprite rw,[^ ]* - tmpfs sprite-hidden ' "/proc/$sp/mountinfo" || fail "sshd isn't in the session's namespaces"
echo "bootstrap: accounts $GUSER and $AUSER ready; sshd on the Sprite's loopback, in the session (/.sprite covered)"
echo "HOSTKEY $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
