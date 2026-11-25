#!/bin/bash
# metacomputer bootstrap for a Vercel sandbox (docs/vercel.md). Runs once as root, through Vercel's own authenticated
# command API (lib/remote.py exec, sudo), before metacomputer's SSH exists; then everything else goes over SSH, as for
# any computer elsewhere (scripts/remote-build.sh).
# It makes the two accounts metacomputer signs in with, each reached only with keys made on the Mac for this computer:
#   cua        the desktop user, no sudo (its keys: the shell, the driver, the live view's tunnel)
#   cuaadmin   maintenance: full sudo only until the setup ends (finalize.sh then narrows it to metacomputer-admin)
# installs the boot script and the relay (/opt/metacomputer/vercel/) and runs the boot script once: it keeps Vercel's
# agent port (23456) from every account but root, then starts sshd on the sandbox's own loopback. The sandbox's own
# `ubuntu` account (what Vercel's API runs commands as, with sudo) stays as Vercel made it: it's the controller's, like
# the owner of the Mac for a VM there.
# Its folder ($1) holds keys-guest and keys-admin (authorized_keys lines, options included) and rootfs/ (this folder's
# rootfs). It prints the host's ed25519 public key last, which the Mac pins (read over Vercel's authenticated API,
# never from a first connection). Running it again changes nothing that's already right.
# metacomputer's command-line rules (scripts/strictargs.sh; this runs in the VM on its own, so they're here):
# exactly -h or --help shows the usage; an option, anything shaped like one, or a form it doesn't take is
# refused (exit 2), and nothing is done.
mc_usage="bootstrap.sh DIR"
case "$*" in -h|--help) echo "usage: $mc_usage"; exit 0 ;; esac
for mc_a in "$@"; do case "$mc_a" in -*) echo "bootstrap.sh: usage: $mc_usage (nothing was run)" >&2; exit 2 ;; esac; done
case "$#:${1:-}" in 1:*) ;; *) echo "bootstrap.sh: usage: $mc_usage (nothing was run)" >&2; exit 2 ;; esac
# end of the command-line rules
set -Eeuo pipefail
DIR="${1:?usage: bootstrap.sh DIR}"
GUSER=cua
AUSER=cuaadmin
fail() { echo "bootstrap: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "run it as root"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && case "${VERSION_ID:-}" in 24.04|26.04) true ;; *) false ;; esac \
  || fail "this is ${PRETTY_NAME:-not Ubuntu}: metacomputer sets up a Vercel sandbox on Ubuntu 24.04 or 26.04"
[ "$(dpkg --print-architecture)" = amd64 ] || fail "$(dpkg --print-architecture) isn't amd64 (Vercel's sandboxes are x86_64)"
for f in keys-guest keys-admin rootfs/opt/metacomputer/vercel/boot rootfs/opt/metacomputer/vercel/relay; do
  [ -s "$DIR/$f" ] || fail "no $f in $DIR"
done
[ -S /run/vercel/share/init.sock ] || fail "this isn't a Vercel sandbox (no /run/vercel/share/init.sock)"
export DEBIAN_FRONTEND=noninteractive

# The SSH server, Python for the relay, and nftables for the agent port's block (Vercel's image has none of them).
if ! command -v sshd >/dev/null || ! command -v python3 >/dev/null || ! command -v nft >/dev/null; then
  apt-get -y -q -o Acquire::Retries=5 update >/dev/null
  apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install --no-install-recommends \
    openssh-server python3 nftables procps iproute2 >/dev/null || fail "couldn't install openssh-server, python3 and nftables"
fi

install -d -o root -g root -m 0755 /etc/metacomputer /opt/metacomputer /opt/metacomputer/vercel /var/lib/metacomputer
cp -r --preserve=mode,timestamps "$DIR/rootfs/." /
chown -R root:root /opt/metacomputer/vercel /etc/ssh/sshd_config.d/05-metacomputer-vercel.conf
chmod 0755 /opt/metacomputer/vercel/*
date -Is > /etc/metacomputer/vercel
[ -s /etc/metacomputer/vercel.env ] || printf 'GUEST_USER=%s\nDISPLAY_NUM=0\nSCREEN=1920x1080\n' "$GUSER" > /etc/metacomputer/vercel.env

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
# metacomputer bootstrap (Vercel Sandbox): until the setup's settings are in place (provision.sh removes this file).
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
sshd -t || fail "sshd refused its settings"
# The boot script: Vercel's agent port kept from every account but root, then sshd (not the desktop: it isn't
# installed yet). The Mac runs it at every session from now on.
/opt/metacomputer/vercel/boot || fail "the boot script failed (above)"
echo "bootstrap: accounts $GUSER and $AUSER ready; sshd on the sandbox's loopback; Vercel's agent port blocked for all but root"
echo "HOSTKEY $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
