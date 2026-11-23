#!/bin/bash
# metacomputer bootstrap for an Ubuntu 24.04 VM reached over the internet. Runs once as root, before the setup:
#   - a VM a provider made: cloud-init runs it at first boot (lib/cloud/guest.py writes it into the user-data);
#   - your own server: metacomputer runs it over your own SSH connection, with sudo (lib/remote.py).
# It makes the two accounts metacomputer signs in with, each reached only with keys made on the Mac for this
# computer, and nothing else:
#   cua        the desktop user, no sudo (its keys: the shell, the driver, the live view's tunnel)
#   cuaadmin   maintenance: full sudo only until the setup ends (finalize.sh then narrows it to metacomputer-admin)
# Its folder ($1) holds keys-guest and keys-admin (authorized_keys lines, options included), guest-firewall and egress
# (internet or off); the account it was run for, if any ($2, your own server), keeps signing in as before; $3 is the
# SSH port metacomputer comes in on. The guest firewall is loaded first, before the accounts exist (SSH in on every
# port sshd listens on; out, no metadata, link-local or private ranges), so nothing runs unfiltered; provision.sh
# makes it permanent. It prints the host's ed25519 public key last, which the Mac pins (read over a connection you
# already trust). Running it again changes nothing that's already right.
# metacomputer's command-line rules (scripts/strictargs.sh; this runs in the VM on its own, so they're here):
# exactly -h or --help shows the usage; an option, anything shaped like one, or a form it doesn't take is
# refused (exit 2), and nothing is done.
mc_usage="bootstrap.sh DIR [OWNER] [SSH_PORT]"
case "$*" in -h|--help) echo "usage: $mc_usage"; exit 0 ;; esac
for mc_a in "$@"; do case "$mc_a" in -*) echo "bootstrap.sh: usage: $mc_usage (nothing was run)" >&2; exit 2 ;; esac; done
case "$#:${1:-}" in [123]:*) ;; *) echo "bootstrap.sh: usage: $mc_usage (nothing was run)" >&2; exit 2 ;; esac
set -Eeuo pipefail
# A failure says where, on the console and in cloud-init's log (a step that fails under set -e says nothing otherwise).
trap 'echo "bootstrap: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
DIR="${1:?usage: bootstrap.sh DIR [OWNER] [SSH_PORT]}"
OWNER="${2:-}"
PORT="${3:-22}"
GUSER=cua
AUSER=cuaadmin
fail() { echo "bootstrap: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "run it as root"
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 24.04 ] || fail "this is ${PRETTY_NAME:-not Ubuntu}: metacomputer sets up Ubuntu 24.04 only"
case "$(dpkg --print-architecture)" in arm64|amd64) ;; *) fail "$(dpkg --print-architecture) isn't arm64 or amd64" ;; esac
for f in keys-guest keys-admin guest-firewall egress; do [ -s "$DIR/$f" ] || fail "no $f in $DIR"; done
case "$PORT" in ''|*[!0-9]*) fail "the SSH port must be a number" ;; esac
case "$OWNER" in ''|"$GUSER"|"$AUSER"|root|mcfill|mcproxy) [ -z "$OWNER" ] || fail "$OWNER can't be the owner's account" ;;
  *) [[ "$OWNER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] && getent passwd "$OWNER" >/dev/null || fail "no account $OWNER here" ;; esac

# The guest firewall, before anything else (provision.sh installs its service, which loads it at every boot).
install -d -m 0755 /etc/metacomputer /opt/metacomputer/bin /var/lib/metacomputer
case "$(head -n 1 "$DIR/egress")" in internet|off) head -n 1 "$DIR/egress" > /etc/metacomputer/egress ;; *) fail "egress: internet or off" ;; esac
# sshd -T (and -t) need sshd's privilege separation folder, which nothing has made yet at a provider computer's first
# boot when ssh is socket-activated, as Ubuntu's is (found live on Hetzner Cloud: sshd -T failed, and with it this).
install -d -m 0755 /run/sshd
ports="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}')" \
  || fail "sshd -T couldn't read sshd's settings: $(sshd -T 2>&1 >/dev/null | head -n 2 | tr '\n' ' ')"
printf '%s\n' $ports "$PORT" | sort -un > /etc/metacomputer/ssh-ports
install -o root -g root -m 0755 "$DIR/guest-firewall" /opt/metacomputer/bin/guest-firewall
/opt/metacomputer/bin/guest-firewall mark-setup   # being set up: EGRESS=off lets apt's downloads out (this boot, 3 h)
if ! command -v nft >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get -y -q update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get -y -q install nftables >/dev/null \
    || fail "couldn't install nftables for the guest firewall"
fi
/opt/metacomputer/bin/guest-firewall apply

for u in "$GUSER" "$AUSER"; do
  if ! getent passwd "$u" >/dev/null; then
    useradd --create-home --shell /bin/bash --comment "metacomputer" "$u"
  fi
  passwd -l "$u" >/dev/null
done
# Nothing but metacomputer's own sudo rule: the desktop user is in no admin group.
for g in sudo admin adm wheel lxd docker; do gpasswd -d "$GUSER" "$g" >/dev/null 2>&1 || true; done
# A provider's computer: root signs in with nothing. The key the provider put in root's account (it makes a server
# without a root password only when it's given one: the computer's admin key, for this first boot) comes off, and
# root's password is locked (Vultr sets one, shown in its console). Your own server's root is yours: left as it is.
if [ -z "$OWNER" ]; then
  rm -f /root/.ssh/authorized_keys /root/.ssh/authorized_keys2
  passwd -l root >/dev/null
fi

# The key lists, root-owned, outside the homes (sshd reads only these once the setup has installed its settings),
# and the same lines in the homes until then (finalize.sh removes those).
install -d -o root -g root -m 0755 /etc/ssh/metacomputer-keys
install -o root -g root -m 0644 "$DIR/keys-guest" "/etc/ssh/metacomputer-keys/$GUSER"
install -o root -g root -m 0644 "$DIR/keys-admin" "/etc/ssh/metacomputer-keys/$AUSER"
for pair in "$GUSER:keys-guest" "$AUSER:keys-admin"; do
  u="${pair%%:*}"; home="$(getent passwd "$u" | cut -d: -f6)"
  install -d -o "$u" -g "$u" -m 0700 "$home/.ssh"
  install -o "$u" -g "$u" -m 0600 "$DIR/${pair#*:}" "$home/.ssh/authorized_keys"
done

# Full sudo for the maintenance account until the setup ends: finalize.sh replaces it with the narrow rule. Run again
# (your own server: you run it, with your sudo, to set the server up again), it opens that window again.
printf '%s\n' "# metacomputer setup (bootstrap.sh): removed by finalize.sh when the setup ends" \
  "$AUSER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/89-metacomputer-setup
chmod 0440 /etc/sudoers.d/89-metacomputer-setup
visudo -cf /etc/sudoers.d/89-metacomputer-setup >/dev/null
[ -z "$OWNER" ] || printf '%s\n' "$OWNER" > /etc/metacomputer/owner
# sshd reads the accounts' home key files until the setup's settings are in place: let it now.
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
[ -s /etc/ssh/ssh_host_ed25519_key.pub ] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
echo "bootstrap: accounts $GUSER and $AUSER ready"
echo "HOSTKEY $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
