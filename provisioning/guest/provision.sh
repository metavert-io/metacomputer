#!/bin/bash
# metacomputer guest provisioning. Runs once as root from cloud-init (runcmd) on the
# first boot of a fresh Ubuntu 24.04 cloud image. Everything it installs comes
# from Ubuntu's archive, Mozilla's APT repo, or the host-verified payload on
# the cloud-init seed disk (mounted at $PAYLOAD). It ends by writing
# /var/lib/metacomputer/provisioned; cloud-init's power_state then reboots into the
# XFCE desktop. On failure it writes /var/lib/metacomputer/failed and leaves the VM
# running so the host can collect /var/log/metacomputer-provision.log over SSH.
set -Eeuo pipefail
PAYLOAD="${1:-/opt/metacomputer-payload}"
LOG=/var/log/metacomputer-provision.log
STATE=/var/lib/metacomputer
mkdir -p "$STATE"
exec > >(tee -a "$LOG") 2>&1
trap 'echo "PROVISION FAILED at line $LINENO: $BASH_COMMAND"; date -Is > "$STATE/failed"' ERR
step() { echo; echo "=== [$(date -Is)] $*"; }

# shellcheck source=/dev/null
. "$PAYLOAD/versions.env"
GUSER="${GUEST_USER:-cua}"
AUSER="${ADMIN_USER:-cuaadmin}"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
# Cua Driver: no telemetry, no update checks - for every process in this
# guest (SSH sessions included), set before the driver binary ever runs.
export CUA_DRIVER_RS_TELEMETRY_ENABLED=0 CUA_TELEMETRY_ENABLED=0 CUA_DRIVER_RS_UPDATE_CHECK=0
for kv in CUA_DRIVER_RS_TELEMETRY_ENABLED=0 CUA_TELEMETRY_ENABLED=0 CUA_DRIVER_RS_UPDATE_CHECK=0; do
  grep -q "^${kv%%=*}=" /etc/environment || echo "$kv" >> /etc/environment
done
APT=(apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
     -o Acquire::Retries=5)

step "payload + versions"
cat "$PAYLOAD/versions.env"

step "remove snapd and lxd-installer (no snaps in this guest)"
systemctl stop snapd.service snapd.socket snapd.seeded.service 2>/dev/null || true
"${APT[@]}" purge snapd lxd-installer 2>/dev/null || true
rm -rf /snap /var/snap /var/lib/snapd /root/snap

step "install the guest file tree"
# rootfs/ mirrors target paths. The payload tarball was extracted with
# --no-same-owner, so everything copied here is root-owned; the desktop
# user's home is re-owned below.
cp -r --preserve=mode,timestamps "$PAYLOAD/rootfs/." /
chown -R "$GUSER:$GUSER" "/home/$GUSER"
install -d -m 0755 /etc/apt/keyrings
# Only the pinned key (scripts/pin-gpg-key.sh on the Mac); mozilla.sources names it in Signed-By.
install -m 0644 "$PAYLOAD/mozilla-repo-signing-key.gpg" /etc/apt/keyrings/packages.mozilla.org.asc

step "apt: update + full upgrade"
"${APT[@]}" update
"${APT[@]}" full-upgrade

step "apt: desktop, accessibility, apps, recording"
PKGS=(
  # Xorg (real X server on virtio-gpu via modesetting) + LightDM autologin
  xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-legacy
  x11-xserver-utils x11-utils xauth xinit
  lightdm lightdm-gtk-greeter
  # XFCE
  xfce4-session xfwm4 xfdesktop4 xfce4-panel xfce4-settings xfconf
  xfce4-terminal xfce4-notifyd xfce4-appfinder thunar tumbler gvfs
  adwaita-icon-theme hicolor-icon-theme gnome-themes-extra elementary-xfce-icon-theme
  fonts-dejavu-core fonts-liberation fonts-noto-core fonts-noto-cjk
  # session bus + AT-SPI accessibility (GTK2/3 bridge, registry, dconf)
  dbus-x11 dbus-user-session at-spi2-core libatk-adaptor libatk-bridge2.0-0 dconf-cli
  # applications
  firefox gnome-calculator libreoffice-writer libreoffice-calc libreoffice-gtk3 libreoffice-style-colibre
  mousepad
  # files a script can work with (metacomputer sh): spreadsheets, office documents, images, PDFs,
  # archives, SQLite, and a venv/pip for one-off Python tools
  python3-openpyxl python3-docx python3-odf python3-pil poppler-utils sqlite3 zip unzip
  python3-venv python3-pip
  # capture / diagnostics / clipboard / live view
  ffmpeg x11vnc xdotool wmctrl xclip imagemagick xinput
  # maintenance + containment
  unattended-upgrades ufw curl ca-certificates jq locales
)
"${APT[@]}" install --no-install-recommends "${PKGS[@]}"
# Nothing that locks the screen or syncs the host clipboard may be present.
purge_unwanted() {
  "${APT[@]}" purge light-locker xfce4-screensaver xscreensaver spice-vdagent 2>/dev/null || true
  "${APT[@]}" autoremove --purge
}
purge_unwanted
firefox --version || true
dpkg-query -W -f='${Package} ${Version}\n' firefox libreoffice-core libreoffice-calc gnome-calculator xserver-xorg-core xfce4-session lightdm ffmpeg python3-openpyxl | tee "$STATE/package-versions.txt"

# After Chrome's install, and after a run of its daily job (the same code), APT may
# reach Google's repo only through our entry and the pinned key.
chrome_sources_pinned() {
  if [ -x /etc/cron.daily/google-chrome ]; then /etc/cron.daily/google-chrome || echo "(Chrome's daily job failed; checking anyway)"; fi
  local bad=0 f
  for f in /etc/apt/sources.list.d/google-chrome.sources /etc/apt/sources.list.d/google-chrome.list; do
    if [ -e "$f" ]; then echo "Chrome's package created $f"; bad=1; fi
  done
  for f in $(grep -l -r -s 'dl\.google\.com' /etc/apt/sources.list /etc/apt/sources.list.d || true); do
    if [ "$f" != /etc/apt/sources.list.d/metacomputer-google-chrome.sources ]; then
      echo "unexpected APT source for Google's repo: $f"; bad=1
    fi
  done
  grep -qx 'Signed-By: /etc/apt/keyrings/google-chrome.asc' /etc/apt/sources.list.d/metacomputer-google-chrome.sources || bad=1
  if [ "$bad" -ne 0 ]; then echo "Google's APT repo is not limited to the pinned key"; return 1; fi
  echo "Google's APT repo: only metacomputer-google-chrome.sources, Signed-By the pinned key"
}

case " ${MCOMP_BROWSERS:-firefox} " in *" chrome "*)
  step "Google Chrome (optional; Firefox stays the default browser)"
  # Google's official arm64 .deb from its APT repo; the host verified the
  # pinned signing key (config/defaults.env) and kept only that key from Google's
  # download (scripts/pin-gpg-key.sh); Signed-By limits the repo to it.
  install -m 0644 "$PAYLOAD/google-linux-signing-key.pub" /etc/apt/keyrings/google-chrome.asc
  # Keep the repo definition ours. The package's postinst rewrites
  # sources.list.d/google-chrome.sources whenever that file exists (pointing it at
  # /usr/share/keyrings/google-chrome.gpg, a keyring the package's scripts write
  # from their own copy), and its daily cron job adds one when repo_add_once is
  # true. So our entry has a name the package never touches, google-chrome.sources
  # must not exist, and both settings stay false. The keyring the scripts write
  # is then referenced by nothing, so APT never trusts it.
  printf 'repo_add_once="false"\nrepo_reenable_on_distupgrade="false"\n' > /etc/default/google-chrome
  rm -f /etc/apt/sources.list.d/google-chrome.sources /etc/apt/sources.list.d/google-chrome.list
  cat > /etc/apt/sources.list.d/metacomputer-google-chrome.sources <<'SRC'
Types: deb
URIs: https://dl.google.com/linux/chrome/deb/
Suites: stable
Components: main
Architectures: arm64
Signed-By: /etc/apt/keyrings/google-chrome.asc
SRC
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends google-chrome-stable
  chrome_sources_pinned
  # Policies (rootfs: /etc/opt/chrome/policies/managed/metacomputer.json) turn off
  # sign-in, sync, metrics, password saving and default-browser prompts.
  for alt in x-www-browser gnome-www-browser; do
    ff="$(update-alternatives --list "$alt" 2>/dev/null | grep -m1 firefox || true)"
    if [ -n "$ff" ]; then update-alternatives --set "$alt" "$ff"; fi
  done
  google-chrome-stable --version || true
  dpkg-query -W -f='${Package} ${Version}\n' google-chrome-stable | tee -a "$STATE/package-versions.txt"
  ;;
esac

if [ -n "${MCOMP_EXTRA_PACKAGES:-}" ]; then
  step "extra packages (EXTRA_PACKAGES): $MCOMP_EXTRA_PACKAGES"
  read -r -a EXTRA <<< "$MCOMP_EXTRA_PACKAGES"
  for p in "${EXTRA[@]}"; do
    [[ "$p" =~ ^[a-z0-9][a-z0-9.+-]*$ ]] || { echo "not a package name: $p"; false; }
  done
  "${APT[@]}" install --no-install-recommends "${EXTRA[@]}"
  purge_unwanted
  dpkg-query -W -f='${Package} ${Version}\n' "${EXTRA[@]}" | tee -a "$STATE/package-versions.txt"
fi

if [ "${MCOMP_VIRTUAL_AUDIO:-off}" = on ]; then
  step "virtual sound card (VIRTUAL_AUDIO=on): one PulseAudio sink that plays into nothing"
  # No hardware is involved: virtio_snd stays blacklisted (rootfs/etc/modprobe.d),
  # so the only sink is this null sink, and desktop-record captures its monitor.
  "${APT[@]}" install --no-install-recommends pulseaudio pulseaudio-utils libasound2-plugins
  purge_unwanted
  install -d -o "$GUSER" -g "$GUSER" -m 0755 "/home/$GUSER/.config/pulse"
  cat > "/home/$GUSER/.config/pulse/default.pa" <<'PA'
# metacomputer (VIRTUAL_AUDIO=on): the system defaults plus a sound card that plays
# into nothing. Desktop recordings capture its monitor (metacomputer.monitor).
.include /etc/pulse/default.pa
load-module module-null-sink sink_name=metacomputer sink_properties=device.description=Virtual-sound-card
set-default-sink metacomputer
PA
  chown "$GUSER:$GUSER" "/home/$GUSER/.config/pulse/default.pa"
  # ALSA-only programs (PortAudio in Audacity, aplay) play through PulseAudio too.
  printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf
  dpkg-query -W -f='${Package} ${Version}\n' pulseaudio libasound2-plugins | tee -a "$STATE/package-versions.txt"
fi

step "Cua Driver ${CUA_DRIVER_VERSION} (linux-arm64) from the host-verified payload"
TARBALL="$PAYLOAD/$CUA_DRIVER_TARBALL"
echo "$CUA_DRIVER_SHA256  $TARBALL" | sha256sum -c -
install -d -m 0755 /opt/cua-driver
rm -rf "/opt/cua-driver/$CUA_DRIVER_VERSION"
mkdir -p "/opt/cua-driver/$CUA_DRIVER_VERSION"
tar -xzf "$TARBALL" -C "/opt/cua-driver/$CUA_DRIVER_VERSION" --strip-components=1 --no-same-owner
chmod -R go-w "/opt/cua-driver/$CUA_DRIVER_VERSION"
ln -sfn "/opt/cua-driver/$CUA_DRIVER_VERSION" /opt/cua-driver/current
ln -sfn /opt/cua-driver/current/cua-driver /usr/local/bin/cua-driver
runuser -u "$GUSER" -- env HOME="/home/$GUSER" /usr/local/bin/cua-driver telemetry disable
HOME=/root /usr/local/bin/cua-driver telemetry disable
/usr/local/bin/cua-driver --version
ldd /usr/local/bin/cua-driver | grep -i 'not found' && { echo "missing shared libraries"; false; } || true

step "users, groups, permissions"
getent group autologin >/dev/null || groupadd -r autologin
usermod -aG autologin,input,video,audio "$GUSER"
install -d -o "$GUSER" -g "$GUSER" -m 0750 "/home/$GUSER"
install -d -o "$GUSER" -g "$GUSER" -m 0755 "/home/$GUSER/metacomputer-runs" "/home/$GUSER/Documents"
# Desktop-user config shipped in rootfs/home/$GUSER (xfconf, autostart, mime).
chown -R "$GUSER:$GUSER" "/home/$GUSER"
install -d -m 0700 -o "$GUSER" -g "$GUSER" "/home/$GUSER/.config/metacomputer"
# XFCE panel: seed the default layout so first login has no "which panel?" prompt.
PANEL_XML="/home/$GUSER/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml"
if [ ! -f "$PANEL_XML" ] && [ -f /etc/xdg/xfce4/panel/default.xml ]; then
  install -o "$GUSER" -g "$GUSER" -m 0644 /etc/xdg/xfce4/panel/default.xml "$PANEL_XML"
fi
# LibreOffice: no tip-of-the-day / what's-new / format nags on first start.
LO_VER="$(dpkg-query -W -f='${Version}' libreoffice-core | sed -E 's/^[0-9]+://; s/^([0-9]+\.[0-9]+).*/\1/')"
LO_DIR="/home/$GUSER/.config/libreoffice/4/user"
install -d -o "$GUSER" -g "$GUSER" -m 0700 "$LO_DIR"
sed "s/@LO_VERSION@/$LO_VER/g" "$PAYLOAD/templates/registrymodifications.xcu" > "$LO_DIR/registrymodifications.xcu"
chown -R "$GUSER:$GUSER" "/home/$GUSER/.config/libreoffice"
chmod 0755 /opt/metacomputer/bin/* /usr/local/sbin/metacomputer-admin
chown root:root /usr/local/sbin/metacomputer-admin
chown -R root:root /opt/metacomputer

step "secrets fill account (mcfill; SECURITY.md, Secrets and logins)"
# A locked system account whose only use is sshd's forced command,
# /opt/metacomputer/bin/secret-fill (15-metacomputer-fill.conf). Its home is root-owned
# and empty, its shell is dash (no rc files for a non-interactive command), and its key
# list lives under /etc, root-owned: nothing the desktop user can write takes part.
FILL_HOME=/var/lib/metacomputer/fill-home
getent passwd mcfill >/dev/null || useradd --system --user-group --no-create-home \
  --home-dir "$FILL_HOME" --shell /bin/sh --comment "metacomputer secrets fill" mcfill
passwd -l mcfill >/dev/null
install -d -o root -g root -m 0755 "$FILL_HOME"
install -d -o root -g root -m 0755 /etc/ssh/metacomputer-fill
[ -s "$PAYLOAD/fill.pub" ] || { echo "the payload has no fill.pub (scripts/build-vm.sh makes it)"; false; }
printf 'restrict %s\n' "$(head -n 1 "$PAYLOAD/fill.pub")" > /etc/ssh/metacomputer-fill/authorized_keys
chmod 0644 /etc/ssh/metacomputer-fill/authorized_keys
install -d -m 0755 /etc/metacomputer
printf 'desktop_user=%s\n' "$GUSER" > /etc/metacomputer/fill.conf
chmod 0644 /etc/metacomputer/fill.conf

step "secrets proxy account (mcproxy; SECURITY.md, Secrets and logins)"
# Locked like mcfill. Its key may only open one reverse forward, 127.0.0.1:$PROXY_PORT
# inside the VM, to the vault's API proxy on the Mac (16-metacomputer-proxy.conf); a
# session gets nologin.
PROXY_PORT="${SECRETS_PROXY_PORT:-18765}"
case "$PROXY_PORT" in ''|*[!0-9]*) echo "SECRETS_PROXY_PORT must be a number"; false ;; esac
PROXY_HOME=/var/lib/metacomputer/proxy-home
getent passwd mcproxy >/dev/null || useradd --system --user-group --no-create-home \
  --home-dir "$PROXY_HOME" --shell /bin/sh --comment "metacomputer secrets proxy" mcproxy
passwd -l mcproxy >/dev/null
install -d -o root -g root -m 0755 "$PROXY_HOME"
install -d -o root -g root -m 0755 /etc/ssh/metacomputer-proxy
[ -s "$PAYLOAD/proxy.pub" ] || { echo "the payload has no proxy.pub (scripts/build-vm.sh makes it)"; false; }
printf 'restrict,port-forwarding,permitlisten="127.0.0.1:%s" %s\n' "$PROXY_PORT" "$(head -n 1 "$PAYLOAD/proxy.pub")" \
  > /etc/ssh/metacomputer-proxy/authorized_keys
chmod 0644 /etc/ssh/metacomputer-proxy/authorized_keys
sed -i "s/^\([[:space:]]*PermitListen 127\.0\.0\.1:\)[0-9][0-9]*[[:space:]]*$/\1$PROXY_PORT/" /etc/ssh/sshd_config.d/16-metacomputer-proxy.conf
grep -q "PermitListen 127\.0\.0\.1:$PROXY_PORT\$" /etc/ssh/sshd_config.d/16-metacomputer-proxy.conf \
  || { echo "16-metacomputer-proxy.conf: couldn't set the proxy port"; false; }
printf 'proxy_port=%s\n' "$PROXY_PORT" >> /etc/metacomputer/fill.conf

step "identity wallpaper (desktop screenshots and recordings say this is the virtual computer)"
# Applied at each login by session-init, with the Applications-menu title.
install -d -m 0755 /usr/share/backgrounds/metacomputer
WP=/usr/share/backgrounds/metacomputer/metacomputer.png
FB=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
FR=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
convert -size 1920x1080 xc:'#17324d' -gravity center \
    -font "$FB" -pointsize 112 -fill '#ffffff' -annotate +0-70 'VIRTUAL COMPUTER' \
    -font "$FR" -pointsize 38 -fill '#bcd4ea' -annotate +0+55 "metacomputer  |  Ubuntu 24.04 ARM64 VM  |  not the user's Mac" \
    "$WP" || convert -size 1920x1080 xc:'#17324d' "$WP"
chmod 0644 "$WP"

step "accessibility defaults (dconf system db)"
dconf update

step "display manager: LightDM autologin into XFCE on Xorg"
systemctl set-default graphical.target
systemctl enable lightdm.service
# Ubuntu's lightdm package expects this file to name the active DM.
echo /usr/sbin/lightdm > /etc/X11/default-display-manager

step "uinput for the driver's X11 virtual-pointer route"
udevadm control --reload-rules || true
modprobe uinput || true

step "ssh hardening + host firewall (defense in depth; the host gateway is the real boundary)"
sshd -t
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow in on enp0s1 from "$NET_GATEWAY_IP" to any port 22 proto tcp comment 'metacomputer ssh via host gateway'
ufw allow in on enp0s1 from "$NET_GATEWAY_IP" to any port 5900 proto tcp comment 'metacomputer vnc via host gateway'
ufw --force enable
ufw status verbose

step "trim services the lab doesn't need"
for unit in multipathd.service multipathd.socket ModemManager.service apport.service \
            motd-news.timer ua-timer.timer esm-cache.service; do
  systemctl disable --now "$unit" 2>/dev/null || true
done
pro config set apt_news=false 2>/dev/null || true
# The virtio sound device would pass host audio in/out; the lab has no use for it.
update-initramfs -u 2>/dev/null || true

step "automatic security updates (no automatic reboots)"
systemctl enable unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer
unattended-upgrade --dry-run 2>&1 | tail -3 || true

step "locale + versions"
mkdir -p /etc/metacomputer
locale-gen en_US.UTF-8 >/dev/null
update-locale LANG=en_US.UTF-8
{
  cat "$PAYLOAD/versions.env"
  echo "PROVISIONED_AT=$(date -Is)"
  echo "UBUNTU=$(. /etc/os-release; echo "$VERSION")"
  echo "KERNEL=$(uname -r)"
  echo "CUA_DRIVER_BINARY=$(/usr/local/bin/cua-driver --version 2>&1 | head -1)"
} > /etc/metacomputer/versions
chmod 0644 /etc/metacomputer/versions

date -Is > "$STATE/provisioned"
step "provisioning complete; cloud-init will reboot into the desktop"
