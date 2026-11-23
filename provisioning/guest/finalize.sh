#!/bin/bash
# metacomputer guest finalize: run once as root (via the provisioning-time sudo)
# after the host has verified the desktop, before the baseline snapshot.
#  - replaces cloud-init's blanket NOPASSWD sudo with the narrow allowlist
#  - turns cloud-init off (the seed disk is gone after provisioning)
#  - clears caches and the cached user-data (it held the SSH host key)
#  - removes cloud-init's bootstrap copies of the SSH keys from the homes, once sshd is
#    shown to read the root-owned lists in /etc/ssh/metacomputer-keys/ instead
# metacomputer's command-line rules (scripts/strictargs.sh; this runs in the VM on its own, so they're here):
# exactly -h or --help shows the usage; an option, anything shaped like one, or a form it doesn't take is
# refused (exit 2), and nothing is done.
mc_usage="finalize.sh [ADMIN [GUEST_USER]]"
case "$*" in -h|--help) echo "usage: $mc_usage"; exit 0 ;; esac
for mc_a in "$@"; do case "$mc_a" in -*) echo "finalize.sh: usage: $mc_usage (nothing was run)" >&2; exit 2 ;; esac; done
case "$#:${1:-}" in 0:|1:*|2:*) ;; *) echo "finalize.sh: usage: $mc_usage (nothing was run)" >&2; exit 2 ;; esac
# end of the command-line rules
set -Eeuo pipefail
ADMIN="${1:-cuaadmin}"
GUSER="${2:-cua}"
for u in "$GUSER" "$ADMIN"; do
  f="/etc/ssh/metacomputer-keys/$u"
  [ -s "$f" ] && [ "$(stat -c '%u %a' "$f")" = "0 644" ] || { echo "finalize: $f isn't a root-owned key list"; exit 1; }
  # grep reads all of it (no -q): sshd mustn't die of SIGPIPE, which pipefail would count as a failure.
  sshd -T -C "user=$u,host=localhost,addr=127.0.0.1" | grep -x 'authorizedkeysfile /etc/ssh/metacomputer-keys/%u' >/dev/null \
    || { echo "finalize: sshd doesn't read $u's keys from /etc/ssh/metacomputer-keys"; exit 1; }
  home="$(getent passwd "$u" | cut -d: -f6)"
  [ -n "$home" ] && rm -f "$home/.ssh/authorized_keys" "$home/.ssh/authorized_keys2"
done
# A VM set up over the internet: its setup sudo (bootstrap.sh) goes. On your own server, the account you set it
# up from keeps its own sudo (cloud-init's rule for it stays); a VM a provider made has no such account.
rm -f /etc/sudoers.d/89-metacomputer-setup
OWNER="$(head -n 1 /etc/metacomputer/owner 2>/dev/null || true)"
cat > /etc/sudoers.d/90-metacomputer <<EOF
# metacomputer: the maintenance account may run only this fixed-verb script as root.
$ADMIN ALL=(root) NOPASSWD: /usr/local/sbin/metacomputer-admin
EOF
chmod 0440 /etc/sudoers.d/90-metacomputer
visudo -cf /etc/sudoers.d/90-metacomputer
[ -n "$OWNER" ] || rm -f /etc/sudoers.d/90-cloud-init-users
# A VM a provider made (reached over SSH, no owner's account): no sudo rule but metacomputer's. Its image may carry
# rules for the provider's agent (OCI's Ubuntu images do, for its run-command account), whose agent isn't there.
# Not a Fly Sprite (/etc/metacomputer/sprite, no /etc/metacomputer/remote): its own `sprite` account's sudo is what
# Fly's exec and services run as (the controller's channel, like the Mac's owner), and stays.
if [ -e /etc/metacomputer/remote ] && [ -z "$OWNER" ]; then
  for f in /etc/sudoers.d/*; do
    case "$(basename "$f")" in 90-metacomputer|README) ;; *) rm -f -- "$f" ;; esac
  done
fi
visudo -c
if sudo -l -U "$GUSER" 2>/dev/null | grep -q '(.*) .*ALL'; then echo "finalize: $GUSER can still use sudo"; exit 1; fi
[ ! -d /etc/cloud ] || touch /etc/cloud/cloud-init.disabled     # a Fly Sprite has no cloud-init
rm -f /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i \
      /var/lib/cloud/instances/*/user-data.txt /var/lib/cloud/instances/*/user-data.txt.i
rm -rf /opt/metacomputer-payload
apt-get -y clean
# The setup's apt downloads end with it: EGRESS=off is off from now on (guest-firewall).
if [ -e /etc/metacomputer/remote ]; then
  rm -f /etc/metacomputer/setup-egress
  /opt/metacomputer/bin/guest-firewall apply
fi
date -Is > /var/lib/metacomputer/finalized
echo "finalized: sudo narrowed$([ -e /etc/metacomputer/sprite ] || echo ", cloud-init disabled")"
