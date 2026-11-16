#!/bin/bash
# Build the two small host tools into vendor/bin (needs Go):
#   cua-netgw  Cua's user-mode network gateway, from trycua/cua at the pinned
#              commit (libs/cua/netgw; MIT, built on gvisor-tap-vsock).
#              The released `cua` bundle does not ship it as a separate binary.
#   qcow2raw   converts Ubuntu's qcow2 cloud image to the raw disk Lume boots
#              (tools/qcow2raw, using Lima's go-qcow2reader).
set -Eeuo pipefail
. "$(dirname "$0")/lib.sh"
trap 'die "failed at ${BASH_SOURCE[0]##*/} line $LINENO: $BASH_COMMAND"' ERR
command -v go >/dev/null || die "Go is required (brew install go)"
mkdir -p "$LAB_ROOT/vendor/bin"
export GOTOOLCHAIN=local CGO_ENABLED=0

if [ ! -x "$NETGW_BIN" ] || [ "${1:-}" = "--rebuild" ]; then
  SRC="$CACHE_DIR/cua-src"
  if [ ! -d "$SRC/.git" ]; then
    git clone --filter=blob:none --no-checkout https://github.com/trycua/cua.git "$SRC"
    git -C "$SRC" sparse-checkout set libs/cua/netgw
  fi
  git -C "$SRC" fetch --depth 1 origin "$CUA_SRC_COMMIT"
  git -C "$SRC" checkout -q "$CUA_SRC_COMMIT"
  (cd "$SRC/libs/cua/netgw" && go build -mod=readonly -trimpath \
     -ldflags "-s -w -X main.version=src-${CUA_SRC_COMMIT:0:12}" -o "$NETGW_BIN" ./cmd/cua-netgw)
  codesign -s - -f "$NETGW_BIN" 2>/dev/null
fi
ok "$("$NETGW_BIN" -version)"

if [ ! -x "$QCOW2RAW_BIN" ] || [ "${1:-}" = "--rebuild" ]; then
  (cd "$LAB_ROOT/tools/qcow2raw" && go build -mod=readonly -trimpath -o "$QCOW2RAW_BIN" .)
fi
ok "qcow2raw built"
