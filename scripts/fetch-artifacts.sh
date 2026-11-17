#!/bin/bash
# Download and verify every external artifact the guest build uses, into
# .cache/. Nothing unverified is ever placed on the seed disk.
#   - Ubuntu 24.04 arm64 cloud image: SHA256SUMS signed by Ubuntu's cloud-image
#     key (fingerprint pinned in config/defaults.env), then the image's SHA-256
#   - Cua Driver linux-arm64 release tarball: SHA-256 pinned in config/defaults.env
#     and cross-checked against the release's SHA256SUMS
#   - Mozilla APT signing key: fingerprint pinned in config/defaults.env
set -Eeuo pipefail
. "$(dirname "$0")/lib.sh"
trap 'die "failed at ${BASH_SOURCE[0]##*/} line $LINENO: $BASH_COMMAND"' ERR

U="$CACHE_DIR/ubuntu-$UBUNTU_RELEASE"; mkdir -p "$U"
# GnuPG in a short temporary home: its agent socket lives there, and socket
# paths over ~104 characters fail (a checkout in a deep folder would).
# Signatures are checked with gpgv against a key file, which needs no agent.
GNUPGHOME="$(mktemp -d /tmp/vc-gpg.XXXXXX)"; export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT

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
if [ ! -f "$U/$UBUNTU_IMAGE" ] || ! echo "$UBUNTU_IMAGE_SHA256  $U/$UBUNTU_IMAGE" | shasum -a 256 -c - >/dev/null 2>&1; then
  curl -fL --progress-bar -o "$U/$UBUNTU_IMAGE.part" "$UBUNTU_BASE_URL/$UBUNTU_IMAGE"
  mv "$U/$UBUNTU_IMAGE.part" "$U/$UBUNTU_IMAGE"
fi
echo "$UBUNTU_IMAGE_SHA256  $U/$UBUNTU_IMAGE" | shasum -a 256 -c - >/dev/null || die "image digest mismatch"
ok "$UBUNTU_IMAGE sha256 verified"

say "Cua Driver $CUA_DRIVER_VERSION (linux-arm64)"
D="$CACHE_DIR/cua-driver-$CUA_DRIVER_VERSION"; mkdir -p "$D"
REL="https://github.com/trycua/cua/releases/download/cua-driver-rs-v$CUA_DRIVER_VERSION"
curl -fsSL -o "$D/SHA256SUMS" "$REL/SHA256SUMS"
listed="$(awk -v f="$CUA_DRIVER_TARBALL" '$2==f{print $1}' "$D/SHA256SUMS" | head -1)"
[ "$listed" = "$CUA_DRIVER_SHA256" ] || die "pinned driver digest differs from the release SHA256SUMS ($listed)"
if [ ! -f "$D/$CUA_DRIVER_TARBALL" ] || ! echo "$CUA_DRIVER_SHA256  $D/$CUA_DRIVER_TARBALL" | shasum -a 256 -c - >/dev/null 2>&1; then
  curl -fL --progress-bar -o "$D/$CUA_DRIVER_TARBALL" "$REL/$CUA_DRIVER_TARBALL"
fi
echo "$CUA_DRIVER_SHA256  $D/$CUA_DRIVER_TARBALL" | shasum -a 256 -c - >/dev/null || die "driver digest mismatch"
ok "$CUA_DRIVER_TARBALL sha256 verified"

say "Mozilla APT signing key"
M="$CACHE_DIR/mozilla"; mkdir -p "$M"
curl -fsSL -o "$M/repo-signing-key.gpg" "$MOZILLA_KEY_URL"
fpr="$(gpg --batch --with-colons --show-keys "$M/repo-signing-key.gpg" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')"
[ "$fpr" = "$MOZILLA_KEY_FPR" ] || die "Mozilla key fingerprint $fpr != pinned $MOZILLA_KEY_FPR"
ok "Mozilla key fingerprint $fpr"

case " $BROWSERS " in *" chrome "*)
  say "Google Linux packages signing key (BROWSERS includes chrome)"
  G="$CACHE_DIR/google"; mkdir -p "$G"
  curl -fsSL -o "$G/linux_signing_key.pub" "$GOOGLE_KEY_URL"
  fpr="$(gpg --batch --with-colons --show-keys "$G/linux_signing_key.pub" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')"
  [ "$fpr" = "$GOOGLE_KEY_FPR" ] || die "Google key fingerprint $fpr != pinned $GOOGLE_KEY_FPR"
  ok "Google key fingerprint $fpr"
esac
