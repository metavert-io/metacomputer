#!/bin/bash
# Build the two small host tools into vendor/bin (needs Go):
#   mcomp-netgw  metacomputer's build of cua-netgw, Cua's user-mode network gateway: trycua/cua at
#              the pinned commit (libs/cua/netgw; MIT, built on gvisor-tap-vsock) with the patches in
#              patches/netgw/ applied to a clean copy of that source (it takes another name because it's
#              modified, as Cua's trademark policy asks). The released `cua` bundle does not ship it as a
#              separate binary.
#   qcow2raw   converts Ubuntu's qcow2 cloud image to the raw disk Lume boots
#              (tools/qcow2raw, using Lima's go-qcow2reader).
# Each binary has a stamp beside it (vendor/bin/.NAME.built): the source it was built from (the pinned
# commit, or a hash of tools/qcow2raw) and the binary's own hash. A tool is rebuilt whenever its binary is
# missing, its source changed or the binary isn't the one that was built. install.sh runs this on every
# install, whether or not it builds the VM.
#
#   scripts/build-tools.sh                    build what isn't up to date
#   scripts/build-tools.sh --check            exit 0 if both are up to date, 1 if not; builds and changes nothing
#   scripts/build-tools.sh --rebuild          build both again
#   scripts/build-tools.sh --rebuild --fresh-source
#                                             ... with the gateway's source in a new temporary clone, not
#                                             .cache/cua-src (what `metacomputer setup` from the plugin uses)
set -Eeuo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
CHECK=0 REBUILD=0 FRESH=0
for a in "$@"; do
  case "$a" in --check) CHECK=1 ;; --rebuild) REBUILD=1 ;; --fresh-source) FRESH=1 ;;
    *) echo "build-tools: unknown option $a" >&2; exit 2 ;; esac
done
BIN_DIR="$LAB/vendor/bin"
netgw_patches() { find "$LAB/patches/netgw" -maxdepth 1 -name '*.patch' -type f 2>/dev/null | LC_ALL=C sort; }
netgw_src() { # the pinned commit and the patches (their names and contents)
  local p sum=""; while IFS= read -r p; do [ -n "$p" ] && sum="$sum $(basename "$p"):$(shasum -a 256 < "$p" | cut -c1-16)"; done <<< "$(netgw_patches)"
  printf 'commit=%s patches=%s' "$CUA_SRC_COMMIT" "$(printf '%s' "$sum" | shasum -a 256 | cut -c1-32)"
}
qcow_src() { printf 'tree=%s' "$(cat "$LAB/tools/qcow2raw/go.mod" "$LAB/tools/qcow2raw/go.sum" "$LAB/tools/qcow2raw"/*.go | shasum -a 256 | cut -c1-32)"; }
stamp_ok() { # BIN SOURCE: the binary is there, built from SOURCE, and still the binary that was built
  local bin="$BIN_DIR/$1" st="$BIN_DIR/.$1.built"
  [ -x "$bin" ] && [ -f "$st" ] && [ ! -L "$bin" ] || return 1
  [ "$(sed -n 's/^source=//p' "$st")" = "$2" ] && [ "$(sed -n 's/^sha256=//p' "$st")" = "$(shasum -a 256 "$bin" | cut -d' ' -f1)" ]
}
stamp_write() { # BIN SOURCE
  printf 'source=%s\nsha256=%s\n' "$2" "$(shasum -a 256 "$BIN_DIR/$1" | cut -d' ' -f1)" > "$BIN_DIR/.$1.built"
}

if [ "$CHECK" = 1 ]; then   # settings only (no lib.sh: nothing is created)
  # shellcheck source=../config/defaults.env
  . "$LAB/config/defaults.env"
  # shellcheck source=/dev/null
  [ ! -f "${MCOMP_CONFIG:-$LAB/config/local.env}" ] || . "${MCOMP_CONFIG:-$LAB/config/local.env}"
  stamp_ok mcomp-netgw "$(netgw_src)" && stamp_ok qcow2raw "$(qcow_src)"
  exit
fi

. "$LAB/scripts/lib.sh"
trap 'die "failed at ${BASH_SOURCE[0]##*/} line $LINENO: $BASH_COMMAND"' ERR
command -v go >/dev/null || die "Go is required (brew install go)"
mkdir -p "$BIN_DIR"
export GOTOOLCHAIN=local CGO_ENABLED=0
# git here never runs a checkout's hooks or file-system monitor.
G() { git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }

TMPS=()
cleanup() { local d; for d in ${TMPS[@]+"${TMPS[@]}"}; do "$LAB/scripts/safe-rmtree.sh" --quiet "$d"; done; }
trap cleanup EXIT
if [ "$REBUILD" = 1 ] || ! stamp_ok mcomp-netgw "$(netgw_src)"; then
  if [ "$FRESH" = 1 ]; then
    tmp_src="$(mktemp -d "${TMPDIR:-/tmp}/mcomp-cua-src.XXXXXX")"; TMPS+=("$tmp_src")
    SRC="$tmp_src/cua-src"
  else
    SRC="$CACHE_DIR/cua-src"
  fi
  if [ ! -d "$SRC/.git" ]; then
    G clone -q --filter=blob:none --no-checkout https://github.com/trycua/cua.git "$SRC"
    G -C "$SRC" sparse-checkout set libs/cua/netgw
  fi
  G -C "$SRC" fetch -q --depth 1 origin "$CUA_SRC_COMMIT"
  G -C "$SRC" -c advice.detachedHead=false checkout -q "$CUA_SRC_COMMIT"
  [ "$(G -C "$SRC" rev-parse HEAD)" = "$CUA_SRC_COMMIT" ] || die "cua-netgw's source isn't the pinned commit $CUA_SRC_COMMIT"
  # A clean copy of the pinned source (never the cache's working tree), the patches applied in order.
  tree="$(mktemp -d "${TMPDIR:-/tmp}/mcomp-netgw.XXXXXX")"; TMPS+=("$tree")
  G -C "$SRC" archive "$CUA_SRC_COMMIT" libs/cua/netgw | tar -x -C "$tree"
  n=0
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    (cd "$tree" && G apply --whitespace=nowarn "$p") || die "patches/netgw/$(basename "$p") doesn't apply to cua-netgw at $CUA_SRC_COMMIT"
    n=$((n + 1))
  done <<< "$(netgw_patches)"
  [ "$n" -gt 0 ] || die "no patches in patches/netgw/: mcomp-netgw is cua-netgw with them applied"
  (cd "$tree/libs/cua/netgw" && go build -mod=readonly -trimpath -buildvcs=false \
     -ldflags "-s -w -X main.version=src-${CUA_SRC_COMMIT:0:12}+mcomp.$n" -o "$NETGW_BIN" ./cmd/cua-netgw)
  codesign -s - -f "$NETGW_BIN" 2>/dev/null
  stamp_write mcomp-netgw "$(netgw_src)"
fi
ok "$("$NETGW_BIN" -version)"

if [ "$REBUILD" = 1 ] || ! stamp_ok qcow2raw "$(qcow_src)"; then
  (cd "$LAB_ROOT/tools/qcow2raw" && go build -mod=readonly -trimpath -o "$QCOW2RAW_BIN" .)
  stamp_write qcow2raw "$(qcow_src)"
fi
ok "qcow2raw built from tools/qcow2raw"
