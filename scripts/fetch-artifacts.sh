#!/bin/bash
# Download and verify every external artifact the guest build uses, into
# .cache/. Nothing unverified is ever placed on the seed disk.
#   - Ubuntu 24.04 arm64 cloud image: SHA256SUMS signed by Ubuntu's cloud-image
#     key (fingerprint pinned in config/defaults.env), then the image's SHA-256
#   - Cua Driver linux-arm64 release tarball: SHA-256 pinned in config/defaults.env
#     and cross-checked against the release's SHA256SUMS
#   - Mozilla (and Google) APT signing keys: fingerprints pinned in config/defaults.env;
#     only the pinned key is kept from each download (scripts/pin-gpg-key.sh)
#
#   scripts/fetch-artifacts.sh rclone   only rclone, for S3-backed folder volumes (Mac side):
#     SHA256SUMS signed by rclone's release key (fingerprint pinned; only that key kept),
#     the zip's SHA-256 pinned and cross-checked, installed to .cache/rclone-vVERSION/rclone
set -Eeuo pipefail
. "$(dirname "$0")/lib.sh"
trap 'die "failed at ${BASH_SOURCE[0]##*/} line $LINENO: $BASH_COMMAND"' ERR

U="$CACHE_DIR/ubuntu-$UBUNTU_RELEASE"; mkdir -p "$U"
# GnuPG in a short temporary home: its agent socket lives there, and socket
# paths over ~104 characters fail (a checkout in a deep folder would).
# Signatures are checked with gpgv against a key file, which needs no agent.
GNUPGHOME="$(mktemp -d /tmp/mcomp-gpg.XXXXXX)"; export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT

fetch_rclone() {
  local R="$CACHE_DIR/rclone-v$RCLONE_VERSION" listed got
  say "rclone $RCLONE_VERSION (S3-backed folder volumes, on the Mac)"
  mkdir -p "$R"
  curl -fsSL -o "$R/SHA256SUMS" "$RCLONE_BASE_URL/SHA256SUMS" || die "could not download rclone's SHA256SUMS"
  if [ ! -s "$R/signing-key.gpg" ]; then
    curl -fsSL -o "$R/KEYS.asc" "$RCLONE_KEYS_URL" || die "could not download rclone's signing keys"
    rm -f "$R/pinned-key.asc"
    "$LAB_ROOT/scripts/pin-gpg-key.sh" "$R/KEYS.asc" "$RCLONE_SIGNING_KEY_FPR" "$R/pinned-key.asc" \
      || die "rclone's key file has no key with the pinned fingerprint $RCLONE_SIGNING_KEY_FPR"
    gpg --batch --dearmor < "$R/pinned-key.asc" > "$R/signing-key.gpg.part" && mv "$R/signing-key.gpg.part" "$R/signing-key.gpg"
  fi
  gpgv --keyring "$R/signing-key.gpg" --status-fd 1 "$R/SHA256SUMS" 2>/dev/null \
    | grep -q "VALIDSIG $RCLONE_SIGNING_KEY_FPR" || die "rclone's SHA256SUMS is not signed by $RCLONE_SIGNING_KEY_FPR"
  ok "SHA256SUMS signed by $RCLONE_SIGNING_KEY_FPR"
  listed="$(awk -v f="$RCLONE_ZIP" '$2==f{print $1}' "$R/SHA256SUMS" | head -1)"
  [ "$listed" = "$RCLONE_SHA256" ] || die "pinned rclone digest differs from the signed SHA256SUMS (${listed:-not listed})"
  if [ ! -f "$R/$RCLONE_ZIP" ] || ! echo "$RCLONE_SHA256  $R/$RCLONE_ZIP" | shasum -a 256 -c - >/dev/null 2>&1; then
    rm -f "$R/$RCLONE_ZIP"
    curl -fL --progress-bar -o "$R/$RCLONE_ZIP.part" "$RCLONE_BASE_URL/$RCLONE_ZIP" || die "could not download $RCLONE_ZIP"
    mv "$R/$RCLONE_ZIP.part" "$R/$RCLONE_ZIP"
  fi
  echo "$RCLONE_SHA256  $R/$RCLONE_ZIP" | shasum -a 256 -c - >/dev/null || { rm -f "$R/$RCLONE_ZIP"; die "rclone zip digest mismatch"; }
  ok "$RCLONE_ZIP sha256 verified"
  rm -rf "$R/x"; mkdir -p "$R/x"
  unzip -q -o "$R/$RCLONE_ZIP" -d "$R/x" || die "could not unpack $RCLONE_ZIP"
  got="$(find "$R/x" -type f -name rclone -perm -u+x | head -1)"
  [ -n "$got" ] || die "$RCLONE_ZIP has no rclone program"
  install -m 0755 "$got" "$R/rclone.part" && mv "$R/rclone.part" "$R/rclone"
  rm -rf "$R/x"
  shasum -a 256 "$R/rclone" | cut -d' ' -f1 > "$R/rclone.sha256"
  "$R/rclone" version 2>/dev/null | head -1 | grep -q "rclone v$RCLONE_VERSION\$" \
    || die "the unpacked rclone doesn't report version $RCLONE_VERSION"
  ok "rclone $RCLONE_VERSION installed at $R/rclone"
}
if [ "${1:-}" = rclone ]; then fetch_rclone; exit 0; fi

say "Ubuntu cloud image ($UBUNTU_RELEASE)"
curl -fsSL -o "$U/SHA256SUMS" "$UBUNTU_BASE_URL/SHA256SUMS"
curl -fsSL -o "$U/SHA256SUMS.gpg" "$UBUNTU_BASE_URL/SHA256SUMS.gpg"
KEYRING="$U/ubuntu-signing-key.gpg"
if [ ! -s "$KEYRING" ]; then
  curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x$UBUNTU_SIGNING_KEY_FPR" -o "$U/signing-key.asc" \
    || die "could not download Ubuntu's signing key"
  gpg --batch --dearmor < "$U/signing-key.asc" > "$KEYRING.part" || die "could not read Ubuntu's signing key"
  mv "$KEYRING.part" "$KEYRING"
fi
kfpr="$(gpg --batch --with-colons --show-keys "$KEYRING" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')"
[ "$kfpr" = "$UBUNTU_SIGNING_KEY_FPR" ] || die "Ubuntu signing key fingerprint $kfpr != pinned $UBUNTU_SIGNING_KEY_FPR"
gpgv --keyring "$KEYRING" --status-fd 1 "$U/SHA256SUMS.gpg" "$U/SHA256SUMS" 2>/dev/null \
  | grep -q "VALIDSIG $UBUNTU_SIGNING_KEY_FPR" || die "SHA256SUMS signature is not from $UBUNTU_SIGNING_KEY_FPR"
ok "SHA256SUMS signed by $UBUNTU_SIGNING_KEY_FPR"
listed="$(awk -v f="*$UBUNTU_IMAGE" '$2==f{print $1}' "$U/SHA256SUMS")"
[ "$listed" = "$UBUNTU_IMAGE_SHA256" ] || die "pinned image digest differs from the signed SHA256SUMS ($listed)"
image_ok() { echo "$UBUNTU_IMAGE_SHA256  $1" | shasum -a 256 -c - >/dev/null 2>&1; }
if [ ! -f "$U/$UBUNTU_IMAGE" ] || ! image_ok "$U/$UBUNTU_IMAGE"; then
  # An interrupted download resumes from its .part file (-C -); one that can't resume starts again.
  part="$U/$UBUNTU_IMAGE.part"
  if [ ! -f "$part" ] || ! image_ok "$part"; then
    say "downloading the image (about 600 MB$([ -s "$part" ] && echo "; resuming the earlier download"))"
    curl -fL --retry 3 -C - --progress-bar -o "$part" "$UBUNTU_BASE_URL/$UBUNTU_IMAGE" \
      || { rm -f "$part"; curl -fL --retry 3 --progress-bar -o "$part" "$UBUNTU_BASE_URL/$UBUNTU_IMAGE"; } \
      || die "the download of Ubuntu's image stopped (network?): run the build again to resume it"
  fi
  mv "$part" "$U/$UBUNTU_IMAGE"
fi
image_ok "$U/$UBUNTU_IMAGE" || { rm -f "$U/$UBUNTU_IMAGE"; die "image digest mismatch (the file is removed; the next build downloads it again)"; }
ok "$UBUNTU_IMAGE sha256 verified"

say "Cua Driver $CUA_DRIVER_VERSION (linux-arm64)"
D="$CACHE_DIR/cua-driver-$CUA_DRIVER_VERSION"; mkdir -p "$D"
REL="https://github.com/trycua/cua/releases/download/cua-driver-rs-v$CUA_DRIVER_VERSION"
curl -fsSL -o "$D/SHA256SUMS" "$REL/SHA256SUMS"
listed="$(awk -v f="$CUA_DRIVER_TARBALL" '$2==f{print $1}' "$D/SHA256SUMS" | head -1)"
[ "$listed" = "$CUA_DRIVER_SHA256" ] || die "pinned driver digest differs from the release SHA256SUMS ($listed)"
if [ ! -f "$D/$CUA_DRIVER_TARBALL" ] || ! echo "$CUA_DRIVER_SHA256  $D/$CUA_DRIVER_TARBALL" | shasum -a 256 -c - >/dev/null 2>&1; then
  curl -fL --retry 3 --progress-bar -o "$D/$CUA_DRIVER_TARBALL" "$REL/$CUA_DRIVER_TARBALL"
fi
echo "$CUA_DRIVER_SHA256  $D/$CUA_DRIVER_TARBALL" | shasum -a 256 -c - >/dev/null || die "driver digest mismatch"
ok "$CUA_DRIVER_TARBALL sha256 verified"

say "Mozilla APT signing key"
M="$CACHE_DIR/mozilla"; mkdir -p "$M"
curl -fsSL -o "$M/repo-signing-key.gpg" "$MOZILLA_KEY_URL"
# Only the pinned key (and the subkeys it has bound) goes to the guest: APT trusts every
# key in a Signed-By keyring, so an extra key in the download must not come along.
rm -f "$M/pinned-key.asc"
"$LAB_ROOT/scripts/pin-gpg-key.sh" "$M/repo-signing-key.gpg" "$MOZILLA_KEY_FPR" "$M/pinned-key.asc" \
  || die "Mozilla's key file has no key with the pinned fingerprint $MOZILLA_KEY_FPR"
ok "Mozilla key $MOZILLA_KEY_FPR (only that key kept)"

case " $BROWSERS " in *" chrome "*)
  say "Google Linux packages signing key (BROWSERS includes chrome)"
  G="$CACHE_DIR/google"; mkdir -p "$G"
  curl -fsSL -o "$G/linux_signing_key.pub" "$GOOGLE_KEY_URL"
  rm -f "$G/pinned-key.asc"
  "$LAB_ROOT/scripts/pin-gpg-key.sh" "$G/linux_signing_key.pub" "$GOOGLE_KEY_FPR" "$G/pinned-key.asc" \
    || die "Google's key file has no key with the pinned fingerprint $GOOGLE_KEY_FPR"
  ok "Google key $GOOGLE_KEY_FPR (only that key kept)"
esac
