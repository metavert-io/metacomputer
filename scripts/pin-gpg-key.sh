#!/bin/bash
# scripts/pin-gpg-key.sh BUNDLE FPR OUT
#
# Writes to OUT (ASCII-armored) only the OpenPGP key whose primary fingerprint is FPR,
# with the user IDs and subkeys that key has bound to itself, taken from BUNDLE (a
# downloaded key file, armored or binary). Any other key in the bundle is left out and
# named on stderr; a bundle without FPR is refused (exit 1).
#
# Why: a vendor's key file can hold several keys, and APT trusts every key in the
# keyring a source's Signed-By names. Checking only the first fingerprint and
# installing the whole file would let an appended key sign packages. GnuPG drops
# subkeys without a valid binding signature from the primary key when it imports,
# so a subkey grafted onto the pinned key is left out too.
. "$(dirname "$0")/strictargs.sh"
if ! sa_check pin-gpg-key.sh "scripts/pin-gpg-key.sh BUNDLE FPR OUT" 3 3 "" "" "$@"; then sed -n '2,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0; fi   # usage only
set -Eeuo pipefail
src="$1" fpr="$(printf '%s' "$2" | tr 'a-f' 'A-F')" out="$3"
[[ "$fpr" =~ ^[0-9A-F]{40}$ ]] || { echo "pin-gpg-key: $2 isn't a full 40-digit fingerprint" >&2; exit 2; }
# A short private home (GnuPG's sockets fail on paths over ~104 characters).
home="$(mktemp -d /tmp/mcomp-pin.XXXXXX)"
cleanup() { gpgconf --homedir "$home" --kill all >/dev/null 2>&1 || true; "$(dirname "$0")/safe-rmtree.sh" --quiet "$home"; }
trap cleanup EXIT
gpg --batch --quiet --homedir "$home" --import "$src" >/dev/null 2>&1 \
  || { echo "pin-gpg-key: $src isn't a key file GnuPG can read" >&2; exit 1; }
primaries="$(gpg --batch --homedir "$home" --with-colons --list-keys 2>/dev/null \
  | awk -F: '$1=="pub"{p=1; next} $1=="fpr" && p {print $10; p=0}')"
printf '%s\n' "$primaries" | grep -qx "$fpr" \
  || { echo "pin-gpg-key: $src has no key with the pinned fingerprint $fpr" >&2; exit 1; }
others="$(printf '%s\n' "$primaries" | grep -vx "$fpr" | grep . || true)"
[ -z "$others" ] || echo "pin-gpg-key: left out $(printf '%s\n' "$others" | wc -l | tr -d ' ') other key(s) in $(basename "$src"): $(echo $others)" >&2
gpg --batch --homedir "$home" --armor --export-options export-clean --export "$fpr" > "$out.part"
# What was written must be exactly the pinned key.
got="$(gpg --batch --with-colons --show-keys "$out.part" 2>/dev/null | awk -F: '$1=="pub"{p=1; next} $1=="fpr" && p {print $10; p=0}')"
[ "$got" = "$fpr" ] || { rm -f "$out.part"; echo "pin-gpg-key: the exported key isn't exactly $fpr" >&2; exit 1; }
mv "$out.part" "$out"
