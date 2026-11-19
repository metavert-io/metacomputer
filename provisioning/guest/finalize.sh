#!/bin/bash
# metacomputer guest finalize: run once as root (via the provisioning-time sudo)
# after the host has verified the desktop, before the baseline snapshot.
#  - replaces cloud-init's blanket NOPASSWD sudo with the narrow allowlist
#  - turns cloud-init off (the seed disk is gone after provisioning)
#  - clears caches and the cached user-data (it held the SSH host key)
#  - removes cloud-init's bootstrap copies of the SSH keys from the homes, once sshd is
#    shown to read the root-owned lists in /etc/ssh/metacomputer-keys/ instead
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
cat > /etc/sudoers.d/90-metacomputer <<EOF
# metacomputer: the maintenance account may run only this fixed-verb script as root.
$ADMIN ALL=(root) NOPASSWD: /usr/local/sbin/metacomputer-admin
EOF
chmod 0440 /etc/sudoers.d/90-metacomputer
visudo -cf /etc/sudoers.d/90-metacomputer
rm -f /etc/sudoers.d/90-cloud-init-users
visudo -c
touch /etc/cloud/cloud-init.disabled
rm -f /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i \
      /var/lib/cloud/instances/*/user-data.txt /var/lib/cloud/instances/*/user-data.txt.i
rm -rf /opt/metacomputer-payload
apt-get -y clean
date -Is > /var/lib/metacomputer/finalized
echo "finalized: sudo narrowed, cloud-init disabled"
