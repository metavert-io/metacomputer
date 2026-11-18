#!/bin/bash
# install.sh: set up metacomputer on this Mac (Apple silicon).
#
#   ./install.sh               interactive; asks at most four questions
#   ./install.sh --yes         accept the defaults (Chrome included, build now)
#   ./install.sh --no-chrome   Firefox only in the VM
#   ./install.sh --lume=standard   Cua's notarized Lume instead of mcomp-vm (the
#                              VM then always shares your clipboard, audio and Rosetta);
#                              (--lume=mcomp-vm, or --lume=secure as before the rename, picks mcomp-vm)
#   ./install.sh --no-build    set everything up but don't build the VM yet
#   ./install.sh --no-claude   don't touch Claude Code (skill, MCP server, metacomputer command)
#   ./install.sh --uninstall   remove the Claude Code hooks this installed (the VM stays)
#
# What it does (each step is skipped when already done):
#   1. checks the Mac, installs missing tools with Homebrew (asks first)
#   2. installs Lume, the VM runner: mcomp-vm (default; metacomputer's build of Lume
#      from the pinned tag, can leave out clipboard/audio/Rosetta sharing) or Cua's release
#   3. writes config/local.env, this Mac's settings (asks whether to add Chrome)
#   4. links the `metacomputer` command, the `metacomputer` skill and the
#      `metacomputer` MCP server into Claude Code, for every project
#   5. builds the secrets vault, mcomp-vault (`metacomputer secrets`), with swiftc
#   6. builds the VM (asks; about 10 minutes, unattended)
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
YES=0; CHROME=""; BUILD=""; CLAUDE=1; UNINSTALL=0; LUME_CHOICE=""
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;; --no-chrome) CHROME=no ;; --no-build) BUILD=no ;;
    --no-claude) CLAUDE=0 ;; --uninstall) UNINSTALL=1 ;;
    --lume=mcomp-vm|--lume=standard) LUME_CHOICE="${a#--lume=}" ;;
    --lume=secure) LUME_CHOICE=mcomp-vm ;;   # mcomp-vm's former name, lume-secure
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
  esac
done

b=$'\033[1m'; g=$'\033[32m'; y=$'\033[33m'; r=$'\033[31m'; o=$'\033[0m'
[ -t 1 ] || { b=; g=; y=; r=; o=; }
step() { printf '\n%s\n' "${b}==> $*${o}"; }
ok()   { printf '%s\n' "  ${g}✓${o} $*"; }
warn() { printf '%s\n' "  ${y}!${o} $*"; }
die()  { printf '%s\n' "${r}install: $*${o}" >&2; exit 1; }
ask() { # QUESTION DEFAULT(Y|N): 0 = yes
  local a
  if [ "$YES" = 1 ] || [ ! -t 0 ]; then [ "$2" = Y ]; return; fi
  read -r -p "  $1 [$([ "$2" = Y ] && echo Y/n || echo y/N)] " a || a=""
  case "${a:-$2}" in [Yy]*) return 0 ;; *) return 1 ;; esac
}
link_points_here() { [ -L "$1" ] && [ "$(readlink "$1")" = "$2" ]; }
# Is $1 (a link target or a command path) one of an earlier version's: inside this
# checkout, or an old name that points nowhere (the checkout moved or was renamed)?
old_target() {
  case "$1" in "$ROOT"/*) return 0 ;; esac
  [ ! -e "$1" ] || return 1
  case "$1" in */bin/vc|*/bin/cua-lab|*/bin/virtual-computer|*/skills/virtual-computer) return 0 ;; esac
  return 1
}
trap 'printf "%s\n" "${r}install: stopped at line $LINENO: $BASH_COMMAND${o}" "Details: the newest file in $ROOT/logs/ (for a build), or the messages above. Fix it and run ./install.sh again; finished steps are skipped." >&2' ERR

SKILL_LINK="$HOME/.claude/skills/metacomputer"
CMD_LINK="$HOME/.local/bin/metacomputer"
ALIAS_LINK="$HOME/.local/bin/mcomp"   # short alias for typing

# --- uninstall ---------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  step "Removing the Claude Code hooks for $ROOT"
  for l in "$CMD_LINK" "$ALIAS_LINK"; do link_points_here "$l" "$ROOT/bin/metacomputer" && rm "$l" && ok "removed $l"; done
  link_points_here "$SKILL_LINK" "$ROOT/skills/metacomputer" && rm "$SKILL_LINK" && ok "removed the metacomputer skill"
  [ -f "$HOME/.local/share/metacomputer/bin/mcomp-vault" ] && rm "$HOME/.local/share/metacomputer/bin/mcomp-vault" \
    && ok "removed the secrets vault program (your vault file stays: ~/Library/Application Support/metacomputer)"
  if command -v claude >/dev/null 2>&1 && claude mcp get metacomputer 2>/dev/null | grep -q "$ROOT/bin/metacomputer"; then
    claude mcp remove metacomputer -s user >/dev/null 2>&1 && ok "removed the metacomputer MCP server"
  fi
  # shellcheck source=/dev/null
  . "$ROOT/config/defaults.env"; [ -f "$ROOT/config/local.env" ] && . "$ROOT/config/local.env"
  # The runner this checkout uses (LUME in config/local.env; mcomp-vm by default), found as
  # scripts/lib.sh finds it: lume-secure, mcomp-vm's former name, while LUME's isn't installed.
  runner="${LUME:-$HOME/.local/bin/mcomp-vm}"
  command -v "$runner" >/dev/null 2>&1 || [ ! -x "$HOME/.local/bin/lume-secure" ] || runner="$HOME/.local/bin/lume-secure"
  name="$(basename "$runner")"
  echo "The VM is untouched. To delete it too, with the runner that made it ($runner):"
  echo "  $runner delete $VM_NAME && $runner delete $BASELINE_NAME"
  echo "  and any checkpoints ($VM_NAME-ckpt-*) and $BASELINE_NAME-prev that '$runner ls' lists"
  if [ "$runner" = "$HOME/.local/bin/$name" ]; then
    echo "(then its SSH keys: rm -r $SSH_DIR; and last, $name itself: rm -rf ~/.local/share/$name ~/.local/bin/$name)"
  else
    echo "(then its SSH keys: rm -r $SSH_DIR; and last, the runner itself, $runner)"
  fi
  if [ "$name" != lume-secure ] && [ -x "$HOME/.local/bin/lume-secure" ]; then
    echo "(lume-secure, mcomp-vm's former name, is installed too: rm -rf ~/.local/share/lume-secure ~/.local/bin/lume-secure)"
  fi
  exit 0
fi

# --- 1. this Mac -----------------------------------------------------------------
step "Checking this Mac"
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "needs macOS on Apple silicon (this is $(uname -s) $(uname -m))"
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 14 ] || die "needs macOS 14 or later (this is $(sw_vers -productVersion))"
ok "macOS $(sw_vers -productVersion) on Apple silicon"
free_gb=$(( $(df -k "$HOME" | awk 'NR==2{print $4}') / 1048576 ))
[ "$free_gb" -ge 20 ] && ok "${free_gb} GB free" || warn "only ${free_gb} GB free; the VM and its snapshot need about 15 GB"
mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))

need=(); for t in git curl go gpg ffmpeg python3; do command -v "$t" >/dev/null 2>&1 || need+=("$t"); done
if [ ${#need[@]} -gt 0 ]; then
  pkgs=(); for t in "${need[@]}"; do case "$t" in gpg) pkgs+=(gnupg) ;; python3) pkgs+=(python) ;; *) pkgs+=("$t") ;; esac; done
  command -v brew >/dev/null 2>&1 || die "missing: ${need[*]}. Install Homebrew (https://brew.sh), then run this again."
  if ask "Install missing tools with Homebrew: ${pkgs[*]}?" Y; then
    brew install "${pkgs[@]}"
  else
    die "these are needed: ${pkgs[*]}"
  fi
fi
ok "tools: git curl go gpg ffmpeg python3"

# shellcheck source=config/defaults.env
. "$ROOT/config/defaults.env"
CONF="$ROOT/config/local.env"
# This Mac's settings, if it has them, can change the pins, e.g. mcomp-vm's source. A
# local.env from before the rename may set that under the former names
# (LUME_SECURE_REPO, _TAG, _COMMIT): those still count; the new names win over them.
pin_repo="$MCOMP_VM_REPO" pin_tag="$MCOMP_VM_TAG" pin_commit="$MCOMP_VM_COMMIT"
unset MCOMP_VM_REPO MCOMP_VM_TAG MCOMP_VM_COMMIT
# shellcheck source=/dev/null
[ ! -f "$CONF" ] || . "$CONF"
MCOMP_VM_REPO="${MCOMP_VM_REPO:-${LUME_SECURE_REPO:-$pin_repo}}"
MCOMP_VM_TAG="${MCOMP_VM_TAG:-${LUME_SECURE_TAG:-$pin_tag}}"
MCOMP_VM_COMMIT="${MCOMP_VM_COMMIT:-${LUME_SECURE_COMMIT:-$pin_commit}}"

# --- 2. Lume ------------------------------------------------------------------------
step "Lume (runs the VM with Apple's Virtualization framework)"
if [ -z "$LUME_CHOICE" ] && [ -f "$CONF" ] && grep -q '^LUME=' "$CONF"; then
  case "$(grep '^LUME=' "$CONF" | tail -1)" in
    *mcomp-vm*) LUME_CHOICE=mcomp-vm; ok "keeping your choice: mcomp-vm" ;;
    *lume-secure*) LUME_CHOICE=mcomp-vm; ok "keeping your choice: mcomp-vm (formerly lume-secure)" ;;
    *) LUME_CHOICE=standard; ok "keeping your choice: standard" ;;
  esac
fi
if [ -z "$LUME_CHOICE" ]; then
  echo "  mcomp-vm (recommended) is metacomputer's build of Lume, with flags that keep your Mac's"
  echo "  clipboard, audio and Rosetta away from the VM; it's built from source (about a minute)."
  echo "  Standard Lume is Cua's notarized release, which always shares them with the VM."
  if ask "Use mcomp-vm?" Y; then LUME_CHOICE=mcomp-vm; else LUME_CHOICE=standard; fi
fi
if [ "$LUME_CHOICE" = mcomp-vm ]; then
  LUME_BIN="$HOME/.local/bin/mcomp-vm"
  if [ -x "$LUME_BIN" ] && grep -qs "upstream=lume-v$LUME_VERSION" "$HOME/.local/share/mcomp-vm/BUILD_INFO" \
     && "$LUME_BIN" run --help 2>&1 | grep -q -- "--no-host-sharing"; then
    ok "already installed: $LUME_BIN"
  else
    if ! xcode-select -p >/dev/null 2>&1 || ! command -v swift >/dev/null 2>&1; then
      die "mcomp-vm is built with Swift: install Apple's Command Line Tools (xcode-select --install), then run this again, or use ./install.sh --lume=standard"
    fi
    src="$ROOT/.cache/mcomp-vm"; rm -rf "$src"; mkdir -p "$ROOT/.cache"
    git clone -q --depth 1 --branch "$MCOMP_VM_TAG" "$MCOMP_VM_REPO" "$src"
    [ "$(git -C "$src" rev-parse HEAD)" = "$MCOMP_VM_COMMIT" ] || die "mcomp-vm $MCOMP_VM_TAG isn't the pinned commit $MCOMP_VM_COMMIT"
    "$src/install.sh"
    # A tag from before the rename (pinned in local.env) installs the runner as lume-secure.
    if [ ! -x "$LUME_BIN" ] && [ -x "$HOME/.local/bin/lume-secure" ]; then
      LUME_BIN="$HOME/.local/bin/lume-secure"
      warn "$MCOMP_VM_TAG installs the runner under its former name, lume-secure"
    fi
    [ -x "$LUME_BIN" ] || die "$src/install.sh didn't install $LUME_BIN"
    ok "installed $LUME_BIN (mcomp-vm $MCOMP_VM_TAG)"
  fi
  if [ "$LUME_BIN" != "$HOME/.local/bin/lume-secure" ] && [ -x "$HOME/.local/bin/lume-secure" ]; then
    ok "lume-secure, its former name, stays installed and sees the same VMs; once nothing uses it:"
    echo "    rm -rf ~/.local/share/lume-secure ~/.local/bin/lume-secure"
  fi
else
  LUME_BIN="$HOME/.local/bin/lume"
  if [ -x "$LUME_BIN" ] && "$LUME_BIN" --version 2>/dev/null | grep -q "$LUME_VERSION"; then
    ok "already installed: $LUME_BIN"
  else
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tgz="lume-$LUME_VERSION-darwin-arm64.tar.gz"
    curl -fsSL -o "$tmp/$tgz" "https://github.com/trycua/cua/releases/download/lume-v$LUME_VERSION/$tgz"
    echo "$LUME_TARBALL_SHA256  $tmp/$tgz" | shasum -a 256 -c - >/dev/null || die "Lume download failed its checksum"
    tar -xzf "$tmp/$tgz" -C "$tmp"
    mkdir -p "$HOME/.local/share/lume" "$HOME/.local/bin"
    rm -rf "$HOME/.local/share/lume/lume.app"; mv "$tmp/lume.app" "$HOME/.local/share/lume/"
    printf '#!/bin/sh\nLUME_TELEMETRY_ENABLED=0 exec "$HOME/.local/share/lume/lume.app/Contents/MacOS/lume" "$@"\n' > "$LUME_BIN"
    chmod +x "$LUME_BIN"
    ok "installed $LUME_BIN (checksum verified, telemetry off, no background service)"
  fi
  warn "with standard Lume the VM always shares your Mac's clipboard, audio and Rosetta"
fi

# --- 3. this Mac's settings -------------------------------------------------------
step "Settings (config/local.env)"
if [ -f "$CONF" ]; then
  ok "keeping the existing $CONF"
else
  cp "$ROOT/config/local.env.example" "$CONF"
  if [ "$CHROME" != no ] && ask "Install Google Chrome in the VM as well as Firefox? (Using Chrome means accepting Google's terms: https://www.google.com/chrome/terms/)" Y; then
    echo 'BROWSERS="firefox chrome"' >> "$CONF"
  else
    echo 'BROWSERS="firefox"' >> "$CONF"
  fi
  if [ "$mem_gb" -lt 16 ]; then echo "VM_MEMORY=4GB" >> "$CONF"; warn "this Mac has ${mem_gb} GB of memory; the VM gets 4 GB"; fi
  ok "wrote $CONF"
fi
# The Lume chosen above (a line of its own, replaced on a change).
lume_line="LUME=\"\$HOME/.local/bin/$(basename "$LUME_BIN")\""
if ! grep -qx "$lume_line" "$CONF"; then
  { grep -v '^LUME=' "$CONF"; echo "$lume_line"; } > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
  ok "Lume: $LUME_BIN"
fi
# shellcheck source=/dev/null
. "$CONF"

# --- 4. Claude Code ----------------------------------------------------------------
if [ "$CLAUDE" = 1 ]; then
  step "Claude Code"
  mkdir -p "$HOME/.local/bin" "$HOME/.claude/skills"
  ln -sfn "$ROOT/bin/metacomputer" "$CMD_LINK" && ok "command: $CMD_LINK"
  if [ -e "$ALIAS_LINK" ] && ! link_points_here "$ALIAS_LINK" "$ROOT/bin/metacomputer"; then
    warn "$ALIAS_LINK already exists and is something else; left alone (the full name works)"
  else
    ln -sfn "$ROOT/bin/metacomputer" "$ALIAS_LINK" && ok "alias: mcomp"
  fi
  # Names used by earlier versions (commands vc, cua-lab, virtual-computer, vcomp; the
  # virtual-computer skill).
  for old in "$HOME/.local/bin/vc" "$HOME/.local/bin/cua-lab" "$HOME/.local/bin/virtual-computer" \
             "$HOME/.local/bin/vcomp" "$HOME/.claude/skills/virtual-computer"; do
    if [ -L "$old" ] && old_target "$(readlink "$old")"; then
      rm "$old" && ok "removed the old name $old"
    fi
  done
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "~/.local/bin isn't on your PATH; add it to use 'metacomputer' directly (Claude uses the full path)" ;; esac
  if [ -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "$SKILL_LINK exists and isn't a link; left alone (move it away and run this again to install the skill)"
  else
    ln -sfn "$ROOT/skills/metacomputer" "$SKILL_LINK" && ok "skill: metacomputer"
  fi
  if command -v claude >/dev/null 2>&1; then
    for old in cua-linux virtual-computer; do  # MCP server names of earlier versions
      cmd="$(claude mcp get "$old" 2>/dev/null | sed -n 's/^ *Command: //p' | head -1 || true)"
      if [ -n "$cmd" ] && old_target "$cmd"; then
        claude mcp remove "$old" -s user >/dev/null 2>&1 && ok "removed the old MCP server name $old (now metacomputer)"
      fi
    done
    if claude mcp get metacomputer 2>/dev/null | grep -q "$ROOT/bin/metacomputer"; then
      ok "MCP server metacomputer already registered"
    else
      claude mcp remove metacomputer -s user >/dev/null 2>&1 || true
      claude mcp add -s user metacomputer "$ROOT/bin/metacomputer" mcp-lazy >/dev/null \
        && ok "MCP server metacomputer registered for every project (tools appear even while the VM is off)" \
        || warn "could not register the MCP server; run: claude mcp add -s user metacomputer $ROOT/bin/metacomputer mcp-lazy"
    fi
  else
    warn "Claude Code's 'claude' command isn't installed; after installing it, run: claude mcp add -s user metacomputer $ROOT/bin/metacomputer mcp-lazy"
  fi
fi

# --- 5. the secrets vault -------------------------------------------------------------
step "Secrets vault (metacomputer secrets)"
if command -v swiftc >/dev/null 2>&1 && [ "$major" -ge 15 ]; then
  "$ROOT/scripts/build-vault.sh" | sed 's/^/  /' \
    || warn "the vault didn't build; metacomputer secrets won't work until scripts/build-vault.sh succeeds"
else
  warn "skipped: the vault needs macOS 15 and swiftc (xcode-select --install); then run scripts/build-vault.sh. Everything else works without it"
fi

# --- 6. the VM ----------------------------------------------------------------------
step "The virtual computer ($VM_NAME)"
if "$LUME_BIN" get "$VM_NAME" >/dev/null 2>&1 && "$LUME_BIN" get "$BASELINE_NAME" >/dev/null 2>&1; then
  ok "already built (and its clean snapshot $BASELINE_NAME)"
elif "$LUME_BIN" get "$VM_NAME" >/dev/null 2>&1; then
  warn "$VM_NAME exists but has no clean snapshot: an earlier build didn't finish"
  if [ "$BUILD" != no ] && ask "Rebuild it from scratch (deletes the unfinished $VM_NAME)?" Y; then
    "$ROOT/bin/metacomputer" build --replace
  else
    echo "  Rebuild later with: metacomputer build --replace"
  fi
elif [ "$BUILD" != no ] && ask "Build it now? It downloads about 600 MB and runs unattended for about 10 minutes." Y; then
  "$ROOT/bin/metacomputer" build
else
  echo "  Build it later with: metacomputer build"
fi

step "Done"
cat <<EOF
  metacomputer start      boot the VM (about 12 s)
  metacomputer test       run the GUI checks (recorded)
  metacomputer view       watch or take over the desktop
  metacomputer help       everything else
In Claude Code (restart open sessions first), try:
  "Get yourself a virtual computer and tell me what's on its screen."
EOF
