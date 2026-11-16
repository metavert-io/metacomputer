#!/bin/bash
# cua-lab guest finalize: run once as root (via the provisioning-time sudo)
# after the host has verified the desktop, before the baseline snapshot.
#  - replaces cloud-init's blanket NOPASSWD sudo with the narrow allowlist
#  - turns cloud-init off (the seed disk is gone after provisioning)
#  - clears caches and the cached user-data (it held the SSH host key)
set -Eeuo pipefail
ADMIN="${1:-cuaadmin}"
cat > /etc/sudoers.d/90-cua-lab <<EOF
# cua-lab: the maintenance account may run only this fixed-verb script as root.
$ADMIN ALL=(root) NOPASSWD: /usr/local/sbin/cua-lab-guest-admin
EOF
chmod 0440 /etc/sudoers.d/90-cua-lab
visudo -cf /etc/sudoers.d/90-cua-lab
rm -f /etc/sudoers.d/90-cloud-init-users
visudo -c
touch /etc/cloud/cloud-init.disabled
rm -f /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i \
      /var/lib/cloud/instances/*/user-data.txt /var/lib/cloud/instances/*/user-data.txt.i
rm -rf /opt/cua-lab-payload
apt-get -y clean
date -Is > /var/lib/cua-lab/finalized
echo "finalized: sudo narrowed, cloud-init disabled"
