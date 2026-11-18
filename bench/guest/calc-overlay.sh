#!/bin/bash
# Give a VM without LibreOffice Calc a user-level copy of LibreOffice that has it,
# for the benchmark's Calc tasks. Run INSIDE the VM as the desktop user (no root).
#
#   calc-overlay.sh DEB_DIR      DEB_DIR holds libreoffice-calc and
#                                libreoffice-uiconfig-calc .debs matching the
#                                VM's libreoffice-core version
#
# A VM built with EXTRA_PACKAGES="libreoffice-calc" has Calc already; then this
# does nothing and prints "system". Otherwise it copies /usr/lib/libreoffice to
# ~/.local/lib/lo-calc, adds the Calc files, points the copy's configuration at
# itself, and adds menu entries (~/.local/share/applications) so Calc, Writer
# and the Start Center all open the copy: one LibreOffice process then serves
# both apps, as on a VM with the system package. Prints "overlay".
# Everything is in the user's home, so a reset to the baseline removes it.
set -euo pipefail
DEBS="$1"
if [ -e /usr/lib/libreoffice/program/libsclo.so ]; then echo system; exit 0; fi
L="$HOME/.local/lib/lo-calc"
if [ ! -e "$L/program/libsclo.so" ]; then
  rm -rf "$L" /tmp/lo-calc-x
  mkdir -p "$(dirname "$L")" /tmp/lo-calc-x
  cp -a /usr/lib/libreoffice "$L"
  for d in "$DEBS"/*.deb; do dpkg-deb -x "$d" /tmp/lo-calc-x; done
  cp -a /tmp/lo-calc-x/usr/lib/libreoffice/. "$L/"
  # Debian links share/registry to /etc/libreoffice/registry; the copy gets its own.
  rm -f "$L/share/registry"; mkdir -p "$L/share/registry"
  cp -rL /etc/libreoffice/registry/. "$L/share/registry/"
  cp /tmp/lo-calc-x/usr/lib/libreoffice/share/.registry/*.xcd "$L/share/registry/"
  sed -i "s|file:///usr/lib/libreoffice|file://$L|g; s|/etc/libreoffice/registry|$L/share/registry|g" \
    "$L/program/fundamentalrc"
  rm -rf /tmp/lo-calc-x
fi
A="$HOME/.local/share/applications"; mkdir -p "$A"
for app in calc writer startcenter; do
  case "$app" in
    calc) name="LibreOffice Calc"; arg="--calc"; cmt="Perform calculations, analyze information and manage lists in spreadsheets." ; icon=libreoffice-calc ;;
    writer) name="LibreOffice Writer"; arg="--writer"; cmt="Create and edit text and graphics in letters, reports, documents and Web pages." ; icon=libreoffice-writer ;;
    startcenter) name="LibreOffice"; arg=""; cmt="The office productivity suite compatible to the open and standardized ODF document format." ; icon=libreoffice-startcenter ;;
  esac
  cat > "$A/libreoffice-$app.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Terminal=false
Name=$name
Comment=$cmt
Icon=$icon
Exec=$L/program/soffice $arg %U
Categories=Office;
StartupWMClass=libreoffice-$app
EOF
done
mkdir -p "$HOME/bin"
printf '#!/bin/sh\nexec %s/program/soffice --calc "$@"\n' "$L" > "$HOME/bin/localc"; chmod +x "$HOME/bin/localc"
# Apps launched by name (`libreoffice --calc FILE`) run the system LibreOffice, which
# has no Calc. LibreOffice hands every launch to the instance already running for
# the same user profile, so keep the copy running with no window (--nodefault),
# started with the desktop session's environment (accessibility, gtk3 plugin).
if ! pgrep -f "[l]o-calc/program/soffice.bin" >/dev/null; then
  python3 - "$L/program/soffice" <<'EOF'
import subprocess, sys
pid = subprocess.run(["pgrep", "-x", "xfce4-session"], capture_output=True, text=True).stdout.split()[0]
env = dict(l.split("=", 1) for l in open(f"/proc/{pid}/environ").read().split("\0") if "=" in l)
subprocess.Popen([sys.argv[1], "--nodefault", "--nologo", "--norestore"], env=env, stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
EOF
  for i in $(seq 1 30); do ls /tmp/OSL_PIPE_"$(id -u)"_SingleOfficeIPC_* >/dev/null 2>&1 && break; sleep 0.5; done
fi
echo overlay
