#!/bin/bash
# get.sh: install metacomputer on this Mac with one command.
#
#   curl -fsSL https://raw.githubusercontent.com/metavert-io/metacomputer/main/get.sh | bash
#   bash get.sh [OPTIONS]            (the same, from a downloaded copy; --help lists the options)
#
# What it does, in order:
#   1. Checks this Mac: Apple silicon, macOS 14 or later, free disk space, memory, the network.
#   2. Prerequisites. Homebrew (for Go, GnuPG and ffmpeg): if it's missing, it offers to run
#      Homebrew's official installer, which asks for your Mac password itself. Apple's Command
#      Line Tools (git, Swift): if they're missing, it opens Apple's installer dialog and waits.
#   3. Downloads metacomputer into ~/metacomputer (git clone), or updates the copy already there,
#      at the latest release (main until the first release).
#   4. Runs ./install.sh --yes in it: the missing Homebrew packages, the VM runner (mcomp-vm),
#      this Mac's settings, the Claude Code plugin, the secrets vault, and the VM itself.
#   5. Starts the VM, checks it (metacomputer status) and shows what to try in Claude Code.
#
# It never asks for, sees or stores a password and never runs sudo; Homebrew's installer and
# Apple's dialog ask for what they need themselves. Each step is skipped when it's already done,
# so running it again is safe and carries on where an interrupted run stopped.
# Everything runs from main(), on the last line, so a download cut short runs nothing.
# Source and license (MIT): https://github.com/metavert-io/metacomputer

set -uo pipefail

MC_REPO="https://github.com/metavert-io/metacomputer.git"
MC_SLUG="metavert-io/metacomputer"
MIN_MACOS=14          # mcomp-vm and the VM; the secrets vault needs 15
VAULT_MACOS=15
VM_FS_GB=10           # free space needed where Lume keeps VMs: the VM and its clean snapshot (about 5 GB, measured), and room to grow
CHECKOUT_FS_GB=2      # ... where the checkout goes: its downloads (about 1 GB, measured); plus 1 GB each in the home
                      # folder (build caches, ~/.local) and the temporary folders, and 2 for Homebrew's packages
GOOD_FREE_GB=30       # warned below this: the VM's disk grows as it's used (up to 50 GB, sparse)
CLT_WAIT_MIN=90       # how long to wait for Apple's Command Line Tools dialog to finish
HOMEBREW_INSTALL_URL="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"

usage() {
  cat <<'EOF'
get.sh: install metacomputer on this Mac (Apple silicon, macOS 14 or later).

  curl -fsSL https://raw.githubusercontent.com/metavert-io/metacomputer/main/get.sh | bash
  bash get.sh [OPTIONS]

Options:
  --dir DIR     where the checkout goes (default: the checkout ~/.local/bin/metacomputer
                already runs, else ~/metacomputer)
  --ref REF     a tag or branch instead of the latest release (main until the first release)
  --repo URL    clone from another URL or path: a fork, a mirror, a local clone
  --no-chrome   Firefox only in the VM (by default Google Chrome is added too, which means
                accepting Google's terms: https://www.google.com/chrome/terms/)
  --no-build    set everything up but don't build the VM yet (later: metacomputer build)
  --replace-vm  delete the VM and its clean snapshot and build them again (for a build that didn't
                finish, or a VM without its snapshot); never implied by --yes
  --confirm     explain what it will do and ask before changing anything
                (what `metacomputer setup` runs; with no terminal to ask on, it stops after
                explaining unless --yes is given too)
  -y, --yes     answer yes: go ahead, and run Homebrew's installer if Homebrew is missing
                (it still asks for your password itself)
  --dry-run     check this Mac and print what it would do; change nothing
  -h, --help    this text

Run it again at any time: finished steps are skipped. Uninstall: docs/install.md.
EOF
}

# --- output ---------------------------------------------------------------------------------
if [ -t 1 ]; then b=$'\033[1m'; d=$'\033[2m'; g=$'\033[32m'; y=$'\033[33m'; r=$'\033[31m'; o=$'\033[0m'
else b=; d=; g=; y=; r=; o=; fi
STEP_N=0; STEP_TOTAL=5; STEP_T0=$SECONDS; STEP_NAME=""; T_START=$SECONDS; TIMES=()
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s\n' "  ${g}✓${o} $*"; }
note() { printf '%s\n' "  ${d}$*${o}"; }
warn() { printf '%s\n' "  ${y}!${o} $*"; }
die()  { printf '\n%s\n' "${r}get.sh: $1${o}" >&2; shift; local l; for l in "$@"; do printf '%s\n' "  $l" >&2; done; exit 1; }
dur()  { local s=$1; if [ "$s" -ge 60 ]; then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
step_done() { [ -z "$STEP_NAME" ] || TIMES+=("$STEP_NAME|$((SECONDS - STEP_T0))"); STEP_NAME=""; }
step() { # TITLE [ESTIMATE]
  step_done
  STEP_N=$((STEP_N + 1)); STEP_T0=$SECONDS; STEP_NAME="$1"
  printf '\n%s%s\n' "${b}[$STEP_N/$STEP_TOTAL] $1${o}" "${2:+  ${d}($2)${o}}"
}
# A command, or in a dry run what it would run.
act() {
  if [ "$DRY" = 0 ]; then "$@"; return; fi
  printf '  %swould run:%s' "$d" "$o"; [ "$1" = git_net ] && { printf ' git'; shift; }; printf ' %q' "$@"; printf '\n'
}

HAVE_TTY=0
( : </dev/tty ) 2>/dev/null && HAVE_TTY=1
ask() { # QUESTION DEFAULT(Y|N) -> 0 = yes. --yes answers yes; with no terminal, the default.
  local a
  if [ "$YES" = 1 ]; then return 0; fi
  if [ "$HAVE_TTY" = 0 ]; then [ "$2" = Y ]; return; fi
  printf '  %s [%s] ' "$1" "$([ "$2" = Y ] && echo Y/n || echo y/N)" >/dev/tty
  read -r a </dev/tty || a=""
  case "${a:-$2}" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# git on a checkout never runs its hooks or a file-system monitor (a checkout's own settings could name them).
git_local() { git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
# git that also works for a private fork or mirror: when plain git can't reach it, retry
# with the GitHub CLI's credentials (gh auth login), if gh is installed and logged in.
git_net() {
  local err rc gh
  err="$(mktemp -t mcomp-get)"
  GIT_TERMINAL_PROMPT=0 git_local "$@" 2>"$err"; rc=$?
  if [ $rc != 0 ] && gh="$(command -v gh)" && "$gh" auth status >/dev/null 2>&1; then
    GIT_TERMINAL_PROMPT=0 git_local -c credential.helper= -c "credential.helper=!'$gh' auth git-credential" "$@" 2>"$err"; rc=$?
  fi
  [ $rc = 0 ] || sed 's/^/  /' "$err" >&2
  rm -f "$err"
  return $rc
}


# One install at a time for this user (any checkout, any $HOME): an exclusive flock on a file in a private
# per-user folder, taken on descriptor 8, which every process this run starts inherits: the lock lasts as long
# as the last of them (killing this script doesn't free it while its children work) and the kernel drops it
# when they've all exited. install.sh run by get.sh inherits it. A second install waits MCOMP_LOCK_WAIT
# seconds, then refuses. perl (part of macOS) takes the lock.
install_lock() { # WHAT
  local ld="${MCOMP_LOCK_DIR:-}" file
  if [ -z "$ld" ]; then
    ld="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)"; ld="${ld%/}"; [ -n "$ld" ] || ld="/tmp/mcomp-$(id -u)"
    ld="$ld/metacomputer-locks"
  fi
  ( umask 077; mkdir -p "$ld" ) 2>/dev/null
  [ -d "$ld" ] && [ ! -L "$ld" ] && [ -O "$ld" ] && chmod 700 "$ld" 2>/dev/null \
    || die "couldn't make the private lock folder $ld"
  file="$ld/install.lock"
  # Held already through a descriptor this process inherited (get.sh running install.sh)?
  /usr/bin/perl -e 'use Fcntl qw(:flock); open(my $fh, ">>&=", 8) or exit 1; my @a = stat($fh); my @b = stat($ARGV[0]);
    exit 1 unless @a && @b && $a[0] == $b[0] && $a[1] == $b[1]; exit(flock($fh, LOCK_EX | LOCK_NB) ? 0 : 1)' "$file" 2>/dev/null \
    && return 0
  exec 8>>"$file" || die "couldn't open $file"
  if ! /usr/bin/perl -e 'use Fcntl qw(:flock); open(my $fh, ">>&=", 8) or exit 2; my $end = time + $ARGV[0];
      until (flock($fh, LOCK_EX | LOCK_NB)) { exit 1 if time >= $end; select(undef, undef, undef, 0.2); } exit 0' \
      "${MCOMP_LOCK_WAIT:-10}" 2>/dev/null; then
    exec 8>&-
    die "another metacomputer install is running ($(sed -n 's/^what=//p' "$ld/install.owner" 2>/dev/null), pid $(sed -n 's/^pid=//p' "$ld/install.owner" 2>/dev/null), since $(sed -n 's/^since=//p' "$ld/install.owner" 2>/dev/null)); nothing was changed." \
        "Wait for it to finish, then run this again."
  fi
  printf 'pid=%s\nsince=%s\nwhat=%s\n' "$$" "$(date '+%H:%M:%S')" "$1" > "$ld/install.owner"
}

# --- 1. this Mac ------------------------------------------------------------------------------
check_mac() {
  step "Checking this Mac" "a few seconds"
  local os arch ver major mem_gb
  os="$(uname -s)"; arch="$(uname -m)"
  [ "$os" = Darwin ] || die "metacomputer runs on macOS; this is $os."
  if [ "$arch" != arm64 ]; then
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then
      die "this terminal runs under Rosetta (x86_64), so the tools would be built for Intel." \
          "Open a Terminal that runs natively (Get Info on Terminal: untick 'Open using Rosetta') and run this again."
    fi
    die "metacomputer needs a Mac with Apple silicon (M1 or later); this Mac is $arch." \
        "The VM is an ARM64 Ubuntu desktop run by Apple's Virtualization framework."
  fi
  ver="$(sw_vers -productVersion)"; major="${ver%%.*}"
  case "$major" in ''|*[!0-9]*) die "couldn't read the macOS version ($ver)" ;; esac
  [ "$major" -ge "$MIN_MACOS" ] || die "metacomputer needs macOS $MIN_MACOS or later; this Mac has $ver." \
      "Update macOS (System Settings > General > Software Update) and run this again."
  ok "macOS $ver on Apple silicon"
  [ "$major" -ge "$VAULT_MACOS" ] || warn "the secrets vault (metacomputer secrets) needs macOS $VAULT_MACOS; everything else works on $ver"
  check_space
  mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  if [ "$mem_gb" -lt 16 ]; then ok "$mem_gb GB of memory: the VM gets 4 GB of it while it runs"
  else ok "$mem_gb GB of memory: the VM gets 8 GB of it while it runs"; fi
  if curl -fsS -m 15 -o /dev/null https://github.com 2>/dev/null; then
    ok "network: github.com reachable"
  else
    die "can't reach github.com, so nothing can be downloaded." \
        "Check this Mac's internet connection (and any proxy or firewall that blocks github.com) and run this again."
  fi
}

# Free space, on every disk a fresh install writes to: where Lume keeps VMs, the checkout, the home folder
# (build caches, ~/.local), the temporary folders and, when tools are missing, Homebrew's prefix; summed per
# disk. An existing checkout is left to install.sh, which knows what's still to build.
fs_of() { # PATH (or its nearest existing parent) -> "DEVICE FREE_GB"
  local p="$1"
  while [ ! -e "$p" ] && [ "$p" != / ]; do p="$(dirname "$p")"; done
  df -k "$p" 2>/dev/null | awk 'NR==2 { print $1, int($4 / 1048576) }'
}
account_home() { # Lume's ~: the account's home folder, whatever $HOME says; status 1 if it can't be told
  local h; h="$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
  case "$h" in /*) printf '%s' "$h" ;; *) return 1 ;; esac
}
# Where Lume will create the VM: an installed runner's default storage location (`config get`), else the
# default in Lume's settings file (~/.config/lume/config.yaml), else Lume's own default, ~/.lume. Status 1
# when that can't be told for certain: no guessing.
vm_storage_dir() {
  local lb out path="" h n
  h="$(account_home)" || return 1
  for lb in "$HOME/.local/bin/mcomp-vm" "$HOME/.local/bin/lume-secure" "$HOME/.local/bin/lume"; do
    [ -x "$lb" ] || continue
    out="$("$lb" config get 2>/dev/null)" || return 1
    path="$(printf '%s\n' "$out" | awk '/^Configured VM storage locations:/ { f = 1; next } f && NF == 0 { f = 0 }
      f && /\(default\)$/ { l = $0; sub(/^[[:space:]]*- [^:]*: /, "", l); sub(/ \(default\)$/, "", l); print l }')"
    n="$(printf '%s' "$path" | grep -c '')"; [ "$n" = 1 ] || return 1
    break
  done
  if [ -z "$path" ]; then
    if [ -e "$h/.config/lume/config.yaml" ]; then
      path="$(awk '
        /^defaultLocationName:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); gsub(/"/, "", v); def = v }
        /^[[:space:]]*-[[:space:]]*name:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); gsub(/"/, "", v); cur = v }
        /^[[:space:]]+path:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); gsub(/"/, "", v); p[cur] = (cur in p) ? "\001" : v }
        END { if (def != "" && (def in p) && p[def] != "\001") print p[def] }' "$h/.config/lume/config.yaml")"
      [ -n "$path" ] || return 1
    else
      path="~/.lume"
    fi
  fi
  case "$path" in "~") path="$h" ;; "~/"*) path="$h/${path#\~/}" ;; esac
  case "$path" in /*) printf '%s' "$path" ;; *) return 1 ;; esac
}
tildify() { case "$1" in "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
check_space() {
  local vm_store line path gb what dev free devs="" dv sum names min_free=""
  local -a need=() rows=()
  pick_dir
  if is_checkout "$DIR" || [ "$NO_BUILD" = 1 ]; then
    note "free space: install.sh checks it, knowing what's still to build"
    return 0
  fi
  vm_store="$(vm_storage_dir)" || die "couldn't tell where Lume will keep the VM (its settings didn't say, or couldn't be read)." \
      "Check 'mcomp-vm config get' (or ~/.config/lume/config.yaml), then run this again (or use --no-build for now)."
  need=("$vm_store|$VM_FS_GB|the VM, where Lume keeps VMs" "$DIR|$CHECKOUT_FS_GB|the checkout and its downloads"
        "$HOME|1|build caches and tools, in your home folder" "${TMPDIR:-/tmp}|1|temporary files" "/tmp|1|mcomp-vm's build")
  command -v go >/dev/null 2>&1 && command -v gpg >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1 \
    || need+=("$( (command -v brew >/dev/null 2>&1 && brew --prefix) 2>/dev/null || echo /opt/homebrew)|2|Homebrew's packages")
  for line in "${need[@]}"; do
    path="${line%%|*}"; line="${line#*|}"; gb="${line%%|*}"; what="${line#*|}"
    read -r dev free <<<"$(fs_of "$path")"
    [ -n "${free:-}" ] || die "couldn't read the free space for $what ($(tildify "$path"))"
    rows+=("$dev|$free|$gb|$what ($(tildify "$path"))")
    case " $devs " in *" $dev "*) ;; *) devs="$devs $dev" ;; esac
  done
  for dv in $devs; do
    sum=0; names=""; free=0
    for line in "${rows[@]}"; do
      [ "${line%%|*}" = "$dv" ] || continue
      line="${line#*|}"; free="${line%%|*}"; line="${line#*|}"; sum=$(( sum + ${line%%|*} )); names="${names:+$names, }${line#*|}"
    done
    [ "$free" -ge "$sum" ] || die "only $free GB free on the disk that holds $names; metacomputer needs at least $sum GB there." \
        "The install uses about 6 GB (the VM and its clean snapshot share most of it), up to 4 GB more for Apple's" \
        "Command Line Tools and Homebrew's packages if they're new, and the VM's disk grows as it's used." \
        "Free some space there and run this again (or use --no-build for now)."
    ok "$free GB free on the disk with $names ($sum GB needed)"
    case "$names" in *"the VM,"*) min_free="$free" ;; esac
  done
  [ -z "$min_free" ] || [ "$min_free" -ge "$GOOD_FREE_GB" ] \
    || warn "$min_free GB free where the VM goes: enough to build, but the VM's disk grows as it's used (up to 50 GB)"
}

# --- 2. prerequisites ---------------------------------------------------------------------------
clt_ok() { # Apple's Command Line Tools (or Xcode) with Swift; never triggers the install dialog
  local dir
  dir="$(xcode-select -p 2>/dev/null)" && [ -d "$dir" ] || return 1
  [ -x "$dir/usr/bin/swiftc" ] || [ -x "$dir/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc" ]
}
brew_on_path() { # Homebrew installed but not on this shell's PATH yet (a fresh install)
  command -v brew >/dev/null 2>&1 && return 0
  local p; for p in ${GET_SH_BREW_PATHS:-/opt/homebrew/bin/brew /usr/local/bin/brew}; do   # (tests set GET_SH_BREW_PATHS)
    if [ -x "$p" ]; then eval "$("$p" shellenv)"; return 0; fi
  done
  return 1
}
prereqs() {
  step "Prerequisites: Homebrew and Apple's Command Line Tools" "seconds if they're here; otherwise 5-15 minutes"
  local need=() t
  brew_on_path || true
  for t in go gpg ffmpeg; do command -v "$t" >/dev/null 2>&1 || need+=("$t"); done
  if command -v brew >/dev/null 2>&1; then
    ok "Homebrew: $(command -v brew)"
    [ ${#need[@]} = 0 ] || note "install.sh adds the missing tools with it: ${need[*]} (a few minutes; ffmpeg brings many libraries)"
  elif [ ${#need[@]} = 0 ]; then
    ok "Homebrew isn't installed, and isn't needed: go, gpg and ffmpeg are already here"
  else
    say "  Homebrew, the package manager for macOS (https://brew.sh), installs the tools metacomputer"
    say "  builds with: ${need[*]}. Its official installer shows what it will do and asks for your Mac"
    say "  password itself (it needs an administrator account). It also installs Apple's Command Line"
    say "  Tools if they're missing. This script never sees the password. Takes about 2-5 minutes."
    if [ "$DRY" = 1 ]; then
      note "would offer to download Homebrew's installer ($HOMEBREW_INSTALL_URL) and run it"
    elif [ "$HAVE_TTY" = 0 ]; then
      die "Homebrew is needed, and there's no terminal here for its installer to ask for your password in." \
          "Install it in Terminal with the command on https://brew.sh, then run this again."
    elif ask "Run Homebrew's installer now?" Y; then
      # The whole installer first; only a download that finished is run (a cut-off script could run halfway).
      local brew_inst; brew_inst="$(mktemp -t mcomp-homebrew-install)" || die "couldn't make a temporary file"
      if ! curl -fsSL --retry 2 -o "$brew_inst" "$HOMEBREW_INSTALL_URL" || [ ! -s "$brew_inst" ]; then
        rm -f "$brew_inst"
        die "couldn't download Homebrew's installer ($HOMEBREW_INSTALL_URL); nothing was run." \
            "Check the network and run this again, or install Homebrew from https://brew.sh."
      fi
      /bin/bash "$brew_inst" </dev/tty || { rm -f "$brew_inst"; die "Homebrew's installer didn't finish (see its messages above)." \
          "It needs an administrator account and the internet. Install it from https://brew.sh, then run this again."; }
      rm -f "$brew_inst"
      brew_on_path || die "Homebrew's installer finished, but brew isn't in /opt/homebrew/bin." "Open a new Terminal and run this again."
      ok "Homebrew installed: $(command -v brew)"
      note "to use brew in new Terminal windows, follow the 'Next steps' Homebrew printed above"
    else
      die "metacomputer needs these tools: ${need[*]}." \
          "Install Homebrew (https://brew.sh), or install them another way, then run this again."
    fi
  fi
  if clt_ok; then
    ok "Command Line Tools: $(xcode-select -p)"
    return 0
  fi
  say "  Apple's Command Line Tools (git, Swift) build mcomp-vm and the secrets vault."
  if [ "$DRY" = 1 ]; then note "would run: xcode-select --install, then wait for Apple's dialog to finish"; return 0; fi
  say "  A macOS dialog now asks to install the \"command line developer tools\": click ${b}Install${o},"
  say "  then ${b}Agree${o}. Apple downloads them (several hundred MB; usually 5-15 minutes). This waits"
  say "  and carries on by itself. No screen to click on (an SSH session)? Press Ctrl-C and see"
  say "  docs/install.md, \"Command Line Tools without the dialog\"."
  xcode-select --install >/dev/null 2>&1 || true
  local waited=0
  while ! clt_ok; do
    sleep 10; waited=$((waited + 10))
    [ $((waited % 60)) = 0 ] && note "still waiting for the Command Line Tools ($((waited / 60)) min)"
    [ "$waited" -lt $((CLT_WAIT_MIN * 60)) ] || die "the Command Line Tools still aren't installed after $CLT_WAIT_MIN minutes." \
        "Run xcode-select --install yourself (or see docs/install.md), then run this again."
  done
  ok "Command Line Tools installed: $(xcode-select -p)"
}

# --- 3. the checkout -----------------------------------------------------------------------------
is_checkout() { [ -d "$1/.git" ] && [ -f "$1/install.sh" ] && [ -f "$1/bin/metacomputer" ] \
                && grep -qs '"name": *"metacomputer"' "$1/.claude-plugin/marketplace.json"; }
pick_dir() {
  [ -z "$DIR" ] || return 0
  local cur
  cur="$(readlink -f "$HOME/.local/bin/metacomputer" 2>/dev/null || true)"
  if [ -n "$cur" ] && [ -f "$cur" ] && is_checkout "$(dirname "$(dirname "$cur")")"; then
    DIR="$(dirname "$(dirname "$cur")")"; DIR_FROM="the checkout ~/.local/bin/metacomputer runs"
  else
    DIR="$HOME/metacomputer"
  fi
}
# Is DIR's origin the project's own repository (github.com/metavert-io/metacomputer, any URL form)?
origin_is_project() {
  local u gh=github.com; u="$(git_local -C "$1" remote get-url origin 2>/dev/null | tr '[:upper:]' '[:lower:]')"; u="${u%/}"; u="${u%.git}"
  case "$u" in "https://$gh/$MC_SLUG"|"git@$gh:$MC_SLUG"|"ssh://git@$gh/$MC_SLUG") return 0 ;; esac
  return 1
}
# --confirm (the plugin's setup) runs only the project's own code, with settings that only set values. An
# existing checkout is accepted only as a clean checkout of the project's repository on its default branch
# that holds nothing else that could run code or that the installer would replace: git settings beyond a
# plain clone's (read from the file, so reading them runs nothing), git hooks, files git doesn't know, build
# outputs (vendor/, .cache/cua-src, .cache/mcomp-vm, __pycache__), anything else in .cache but the verified
# downloads, a Lume storage location inside the checkout, or a config/local.env with settings that choose a
# command, a source or a pin. Nothing is ever deleted: such a checkout is refused, and its owner runs its
# ./install.sh or picks a fresh --dir. Prints the problems found, nothing if there are none.
env_is_data() { # FILE: blank lines, comments and NAME=value lines that only set values (see confirm_problems)
  awk '
    function plain(v) { return v ~ /^("[^"$`\\]*"|'\''[^'\'']*'\''|[A-Za-z0-9_.\/:@%+,=-]*|"\$HOME\/[^"$`\\]*")$/ }
    /^[[:space:]]*(#.*)?$/ { next }
    {
      line = $0; sub(/[[:space:]]+#.*$/, "", line)
      i = index(line, "="); if (i < 2) { bad = 1; exit }
      k = substr(line, 1, i - 1); v = substr(line, i + 1)
      if (k !~ /^[A-Za-z_][A-Za-z0-9_]*$/ || !plain(v)) { bad = 1; exit }
      # Settings that are only values. Not here (and so refused): anything naming a command (*_GET, *_SET,
      # PY), a source or a pin (*_REPO, *_TAG, *_COMMIT, *_URL, *_SHA256, *_FPR, *_VERSION, ...), keys,
      # manifests, guest accounts and the network.
      # Storage (docs/storage.md): where images go, what to attach, sync timing. The rclone pins (RCLONE_*) are refused.
      if (k ~ /^(VM_NAME|BASELINE_NAME|VM_HOSTNAME|VM_CPU|VM_MEMORY|VM_DISK_BYTES|VM_DISPLAY|IMPORT_MIN_FREE_GB|BROWSERS|EXTRA_PACKAGES|VIRTUAL_AUDIO|SHARE_CLIPBOARD|SHARE_AUDIO|SHARE_ROSETTA|SSH_DIR|SSH_PORT|VNC_PORT|SECRETS_PROXY_PORT|RUN_DIR|LOG_DIR|RECORD_FPS|RECORD_QUALITY|RECORD_TO|RECORD_DIR|RECORD_AUDIO|TASK_SYNC_INTERVAL|BROWSER|MODE|VM_SHELL|SHELL_LOG|MCP_HUB|HUB_IDLE|COORD_GUARD|ACTION_LOG|ACTION_LOG_TEXT|SSH_AUTH|SSH_CERT_TTL|DRIVER_MODE|DRIVER_PROFILE|DRIVER_ORIGINS|DRIVER_EXPIRES|DRIVER_IDLE|STORAGE_DIR|VOLUMES|REFDISKS|S3_SYNC_INTERVAL|S3_BACKUP_MAX_GB|VIEW_CLIPBOARD_S)$/) next
      gsub(/"/, "", v)
      if (k == "LUME" && (v == "$HOME/.local/bin/mcomp-vm" || v == "$HOME/.local/bin/lume" || v == "$HOME/.local/bin/lume-secure")) next
      if (k == "VNC_PASSWORD_STORE" && (v == "file" || v == "keychain")) next
      bad = 1; exit
    }
    END { exit bad }' "$1"
}
# A file in .cache/rclone-vVERSION/ (scripts/fetch-artifacts.sh rclone, for S3 volumes) that is checked again
# before every use: SHA256SUMS (downloaded again, then its signature by the pinned key), the key files (used
# only through that check) and the zip (its pinned SHA-256), with an unfinished download of it. Not the
# unpacked program (rclone) or a digest file beside it: storage checks the program against its pinned digest
# (RCLONE_BIN_SHA256) before every use and unpacks it again from the zip, so one in a checkout is never
# needed, and refusing it keeps this check from depending on that.
rclone_download() {
  case "$1" in
    */*) return 1 ;;
    SHA256SUMS|KEYS.asc|pinned-key.asc|signing-key.gpg|rclone-v*.zip|rclone-v*.zip.part) return 0 ;;
  esac
  return 1
}
# Every Lume storage location (an installed runner's, else Lume's settings file's), one absolute path a line.
lume_locations() {
  local lb h out=""
  h="$(account_home)" || return 0
  for lb in "$HOME/.local/bin/mcomp-vm" "$HOME/.local/bin/lume-secure" "$HOME/.local/bin/lume"; do
    [ -x "$lb" ] || continue
    out="$("$lb" config get 2>/dev/null | awk '/^Configured VM storage locations:/ { f = 1; next } f && NF == 0 { f = 0 }
      f && /^[[:space:]]*- / { l = $0; sub(/^[[:space:]]*- [^:]*: /, "", l); sub(/ \(default\)$/, "", l); print l }')"
    break
  done
  if [ -z "$out" ] && [ -f "$h/.config/lume/config.yaml" ]; then
    out="$(awk '/^[[:space:]]+path:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); gsub(/"/, "", v); print v }' "$h/.config/lume/config.yaml")"
  fi
  printf '%s\n' "$out" | while IFS= read -r l; do
    case "$l" in "~") printf '%s\n' "$h" ;; "~/"*) printf '%s\n' "$h/${l#\~/}" ;; /*) printf '%s\n' "$l" ;; esac
  done
}
confirm_problems() {
  local d="$1" p="" bad hooks others e real loc
  if [ ! -d "$d/.git" ] || [ -L "$d/.git" ]; then printf '%s' "it isn't a plain clone (.git isn't a folder)"; return 0; fi
  bad="$(git config --file "$d/.git/config" --name-only --list 2>/dev/null \
    | grep -Ev '^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode)|remote\.origin\.(url|fetch)|branch\.[^.]+\.(remote|merge))$' \
    | grep -v '^$' | head -3 | paste -sd, - || true)"
  [ -z "$bad" ] || p="${p:+$p; }git settings beyond a plain clone's ($bad)"
  hooks="$(find "$d/.git/hooks" -type f ! -name '*.sample' 2>/dev/null | head -3 | sed "s|^$d/||" | paste -sd, - || true)"
  if [ -n "$hooks" ] || [ -L "$d/.git/hooks" ]; then p="${p:+$p; }git hooks (${hooks:-.git/hooks is a link})"; fi
  others="$(git_local -C "$d" ls-files --others --exclude-standard 2>/dev/null | head -3 | paste -sd, - || true)"
  [ -z "$others" ] || p="${p:+$p; }files git doesn't know ($others)"
  real="$(cd "$d" && pwd -P)"
  while IFS= read -r loc; do
    [ -n "$loc" ] || continue
    case "$(cd "$loc" 2>/dev/null && pwd -P || printf '%s' "$loc")/" in
      "$real"/*) p="${p:+$p; }a Lume storage location inside it ($loc)" ;;
    esac
  done <<<"$(lume_locations)"
  bad=""
  while IFS= read -r e; do
    case "$e" in
      ''|logs/*|results/*|recordings/*|screenshots/*|.run/*|local/*) ;;
      .cache/ubuntu-*/*|.cache/cua-driver-*/*|.cache/mozilla/*|.cache/google/*) ;;
      .cache/rclone-v*/*) rclone_download "${e#.cache/rclone-v*/}" || bad="$bad$e"$'\n' ;;
      .DS_Store|*/.DS_Store|*.pem|*vnc-pass*|CLAUDE.local.md) ;;
      config/local.env|config/*.local.env) env_is_data "$d/$e" || p="${p:+$p; }$e has settings that choose a command, a source or a pin" ;;
      *) case "$e" in
           .cache/*) e=".cache/$(printf '%s' "${e#.cache/}" | cut -d/ -f1)" ;;
           */__pycache__/*) e="${e%%/__pycache__/*}/__pycache__" ;;
           */*) e="${e%%/*}/" ;;
         esac
         bad="$bad$e"$'\n' ;;
    esac
  done <<<"$(git_local -C "$d" ls-files --others --ignored --exclude-standard 2>/dev/null)"
  [ -z "$bad" ] || p="${p:+$p; }build outputs or other files the installer would use or replace ($(printf '%s' "$bad" | sort -u | grep -v '^$' | head -5 | paste -sd, -))"
  printf '%s' "$p"
}
# What to install, decided from the repository before anything is downloaded: REF and REF_KIND (given,
# release or default), and REF_COMMIT, the commit it names there, which the checkout must then be. A failed
# query stops here: it never looks like "no releases".
pick_ref() {
  local out branch
  REF_COMMIT=""; REF_BRANCH=""
  if [ -n "$REF" ]; then
    REF_KIND=given
    out="$(git_net ls-remote "$REPO" "refs/tags/$REF" "refs/tags/$REF^{}" "refs/heads/$REF")" \
      || die "couldn't ask $REPO about $REF (above)." "Check the network (and, for a private fork, 'gh auth status'), then run this again."
    REF_COMMIT="$(printf '%s\n' "$out" | awk -v t="refs/tags/$REF^{}" '$2 == t { print $1 }')"
    [ -n "$REF_COMMIT" ] || REF_COMMIT="$(printf '%s\n' "$out" | awk -v t="refs/tags/$REF" '$2 == t { print $1 }')"
    if [ -z "$REF_COMMIT" ]; then
      REF_COMMIT="$(printf '%s\n' "$out" | awk -v t="refs/heads/$REF" '$2 == t { print $1 }')"
      [ -n "$REF_COMMIT" ] || die "$REF is neither a tag nor a branch of $REPO"
      REF_BRANCH="$REF"
    fi
    return 0
  fi
  out="$(git_net ls-remote --sort=v:refname --tags "$REPO" 'v[0-9]*')" \
    || die "couldn't list $REPO's releases (above), so it can't tell which one to install." \
           "Check the network (and, for a private fork, 'gh auth status'), then run this again."
  REF="$(printf '%s\n' "$out" | awk '{ print $2 }' | sed 's|^refs/tags/||; s|\^{}$||' | grep -E '^v[0-9]+(\.[0-9]+)*$' | uniq | tail -1 || true)"
  if [ -n "$REF" ]; then
    REF_KIND=release
    REF_COMMIT="$(printf '%s\n' "$out" | awk -v t="refs/tags/$REF^{}" '$2 == t { print $1 }')"
    [ -n "$REF_COMMIT" ] || REF_COMMIT="$(printf '%s\n' "$out" | awk -v t="refs/tags/$REF" '$2 == t { print $1 }')"
    return 0
  fi
  REF_KIND=default
  out="$(git_net ls-remote --symref "$REPO" HEAD)" \
    || die "couldn't ask $REPO for its default branch (above)." "Check the network, then run this again."
  branch="$(printf '%s\n' "$out" | awk '$1 == "ref:" && $3 == "HEAD" { sub(/^refs\/heads\//, "", $2); print $2 }')"
  REF_COMMIT="$(printf '%s\n' "$out" | awk '$1 != "ref:" && $2 == "HEAD" { print $1 }')"
  [ -n "$branch" ] && [ -n "$REF_COMMIT" ] || die "$REPO didn't say what its default branch is"
  REF_BRANCH="$branch"; REF="$branch"
}
checkout() {
  step "Getting metacomputer" "under a minute"
  pick_dir
  local cur why=""
  if is_checkout "$DIR"; then
    ok "already here: $DIR${DIR_FROM:+ ($DIR_FROM)}"
    # From the plugin (--confirm), only the project's own code runs: a checkout from elsewhere, with local
    # changes or on a branch of its own is refused. Run directly, get.sh runs such a checkout as it is: that
    # is trusting the code in DIR, and it says so.
    [ "$CONFIRM" = 0 ] || why="$(confirm_problems "$DIR")"
    origin_is_project "$DIR" || [ "$REPO" != "$MC_REPO" ] \
      || why="${why:+$why; }its origin isn't github.com/$MC_SLUG ($(git_local -C "$DIR" remote get-url origin 2>/dev/null || echo none))"
    [ -z "$(git_local -C "$DIR" status --porcelain --untracked-files=no 2>/dev/null)" ] || why="${why:+$why; }it has local changes"
    cur="$(git_local -C "$DIR" symbolic-ref -q --short HEAD 2>/dev/null || true)"
    if [ -n "$cur" ] && [ -z "$REF" ]; then
      local def; def="$(git_local -C "$DIR" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||')"
      [ "$cur" = "${def:-main}" ] || why="${why:+$why; }it's on the branch $cur"
    fi
    if [ -n "$why" ]; then
      [ "$CONFIRM" = 0 ] || die "$DIR isn't a plain checkout of the project: $why. Nothing in it was changed." \
          "metacomputer setup runs only the project's own code, with settings that only set values. Either run that" \
          "checkout's installer yourself if you trust it ($DIR/install.sh), or set up a fresh copy: setup --dir <a new folder>"
      warn "$why: it isn't updated or checked against the repository; install.sh runs the code in $DIR as it is (you chose it)"
      return 0
    fi
    pick_ref
    act git_net -C "$DIR" fetch -q --tags --prune origin || die "couldn't update $DIR from its origin." \
        "Check the network (and, for a private fork, that 'gh auth status' is logged in), then run this again."
    if [ "$DRY" = 1 ]; then note "would switch it to $(ref_desc)"; return 0; fi
    switch_ref "$DIR"
    return 0
  fi
  if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "$DIR already exists and isn't a metacomputer checkout." "Move it away, or choose another folder with --dir DIR."
  fi
  pick_ref
  act git_net clone -q "$REPO" "$DIR" || die "couldn't download metacomputer from $REPO." \
      "Check the network. For a private fork, log in to GitHub first: gh auth login."
  if [ "$DRY" = 1 ]; then note "would check out $(ref_desc)"; return 0; fi
  switch_ref "$DIR"
  ok "downloaded to $DIR ($(ref_desc))"
}
ref_desc() {
  case "$REF_KIND" in release) printf 'release %s' "$REF" ;; given) printf '%s' "$REF" ;; *) printf '%s (no release yet)' "$REF" ;; esac
  printf ', commit %.12s' "$REF_COMMIT"
}
switch_ref() { # DIR: check out what pick_ref chose, then make sure HEAD is the commit it named
  local dir="$1" branch="$REF_BRANCH" head
  if [ -z "$branch" ]; then
    git_local -C "$dir" rev-parse -q --verify "refs/tags/$REF" >/dev/null || die "$REF isn't in $dir after the download"
    git_local -C "$dir" -c advice.detachedHead=false checkout -q "refs/tags/$REF" 2>/dev/null || die "couldn't check out $REF in $dir"
  else
    git_local -C "$dir" rev-parse -q --verify "refs/remotes/origin/$branch" >/dev/null || die "the branch $branch isn't in $dir after the download"
    if [ "$(git_local -C "$dir" symbolic-ref -q --short HEAD)" != "$branch" ]; then
      git_local -C "$dir" checkout -q "$branch" 2>/dev/null || git_local -C "$dir" checkout -q -b "$branch" "origin/$branch" \
        || die "couldn't check out $branch in $dir"
    fi
    if ! git_local -C "$dir" merge -q --ff-only "origin/$branch" >/dev/null 2>&1; then
      [ "$CONFIRM" = 0 ] || die "$branch in $dir has commits of its own that origin/$branch doesn't, so it isn't the project's code as the repository has it." \
          "metacomputer setup runs only that. If you mean to use those commits, run $dir/install.sh yourself."
      warn "$branch has commits of its own that origin/$branch doesn't, so it's left as it is, unchecked (install.sh runs it as it is)"
      return 0
    fi
  fi
  head="$(git_local -C "$dir" rev-parse HEAD)"
  [ "$head" = "$REF_COMMIT" ] || die "$dir is at $(printf '%.12s' "$head"), not the commit $REPO named for $REF ($(printf '%.12s' "$REF_COMMIT"))." \
      "The repository changed while this ran (a new push or a moved tag). Run this again; if it repeats, report it."
  ok "at $REF, commit $(printf '%.12s' "$head")"
}

# --- 4. install.sh -------------------------------------------------------------------------------
install() {
  step "Installing (./install.sh)" "$([ "$NO_BUILD" = 1 ] && echo "about 2 minutes" || echo "about 5 minutes on a fast connection, 10-15 on a slow one; unattended")"
  local args=(--yes)
  [ "$CONFIRM" = 0 ] || args+=(--fresh-sources)   # from the plugin: the host tools and mcomp-vm from new clones
  [ "$NO_CHROME" = 1 ] && args+=(--no-chrome)
  [ "$NO_BUILD" = 1 ] && args+=(--no-build)
  [ "$REPLACE_VM" = 1 ] && args+=(--replace-vm)
  if [ "$NO_CHROME" = 0 ]; then
    note "the VM gets Google Chrome next to Firefox, which means accepting Google's terms"
    note "(https://www.google.com/chrome/terms/). To leave it out, stop now (Ctrl-C) and run again with --no-chrome"
  fi
  if [ "$DRY" = 1 ]; then
    act "$DIR/install.sh" "${args[@]}"
    note "it installs the missing Homebrew packages, builds mcomp-vm (the VM runner) and the secrets vault,"
    note "writes config/local.env, adds the Claude Code plugin, and builds the VM (about 1.5 GB of downloads)"
    return 0
  fi
  # install.sh writes vm_built=1 to MCOMP_INSTALL_REPORT when it built the VM in this run: only then does
  # the health check below run its self-test, which opens and closes windows (a VM it kept may hold work).
  # An install.sh from before this handoff writes nothing, so its VM counts as kept.
  local report rc=0
  report="$(mktemp -t mcomp-install)" || die "couldn't make a temporary file"
  # stdin: when this script is piped into bash, the rest of the pipe is this script, not answers.
  MCOMP_INSTALL_REPORT="$report" "$DIR/install.sh" "${args[@]}" </dev/null || rc=$?
  grep -qx 'vm_built=1' "$report" 2>/dev/null && VM_NEW=1
  rm -f "$report"
  [ "$rc" = 0 ] \
    || die "install.sh stopped; its messages above say why and what to do." \
           "Fix that and run this again: everything that's finished is skipped."
}

# --- 5. health check -----------------------------------------------------------------------------
health() {
  local mc="$DIR/bin/metacomputer"
  if [ "$NO_BUILD" = 1 ]; then
    step "Checking it" "skipped: --no-build"
    note "build the VM when you're ready: $mc build  (about 1.5 GB of downloads, then a few minutes)"
    return 0
  fi
  if [ "$DRY" = 1 ]; then
    step "Checking it" "under half a minute"
    note "on a VM this run builds: start it, metacomputer status, and a quick self-test (test A opens and closes a window)"
    act "$mc" start; act "$mc" status; act "$mc" test A
    note "on a VM it keeps: only reads (start only if it's stopped), so nothing in it is closed or typed into"
    act "$mc" status; act "$mc" call list_windows '{}'
    return 0
  fi
  HEALTH=ok
  if [ "$VM_NEW" = 1 ]; then
    # A VM this run built holds nothing of the user's yet: the self-test may open and close windows in it.
    step "Checking it: start the VM, metacomputer status, a quick self-test" "under half a minute"
    "$mc" start || { HEALTH=failed; warn "the VM didn't start (above); try: $mc start, then $mc status"; return 0; }
    if "$mc" status; then ok "metacomputer status: ready"
    else HEALTH=failed; warn "metacomputer status says it isn't ready (above); docs/install.md, Troubleshooting"; return 0; fi
    if "$mc" test A >/dev/null 2>&1; then ok "self-test passed: Claude's tools read the VM's desktop (test A)"
    else HEALTH=partial; warn "the self-test (metacomputer test A) didn't pass; run it again to see why"; fi
    return 0
  fi
  # A VM this run kept may hold the user's work: only reads. No self-test (test A closes an editor and
  # answers No to its recovery prompt), and start only for a stopped VM: on a running one it can restart
  # the VM (when its network gateway is gone), which would lose what's open in it.
  step "Checking it: metacomputer status and a read-only look at the desktop" "under half a minute"
  note "this run kept your VM, so nothing in it is closed, restarted or typed into"
  local st
  st="$("$mc" status --json 2>/dev/null)"
  if grep -qx '  "vm": "stopped",' <<<"$st"; then
    "$mc" start || { HEALTH=failed; warn "the VM didn't start (above); try: $mc start, then $mc status"; return 0; }
  fi
  if "$mc" status; then ok "metacomputer status: ready"
  else HEALTH=failed; warn "metacomputer status says it isn't ready (above); docs/install.md, Troubleshooting"; return 0; fi
  # list_windows is one of the hub's read-only tools: it claims nothing, and its answer (the VM's text) isn't shown.
  if "$mc" call list_windows '{}' >/dev/null 2>&1; then ok "Claude's tools read the VM's desktop (a read-only check)"
  else HEALTH=partial; warn "the read-only check (metacomputer call list_windows) didn't answer; run it again to see why"; fi
}

finish() {
  step_done
  local total=$((SECONDS - T_START)) t
  printf '\n%s\n' "${b}Done in $(dur "$total")${o}"
  for t in ${TIMES[@]+"${TIMES[@]}"}; do printf '  %-62s %s\n' "${t%|*}" "$(dur "${t##*|}")"; done
  [ "$DRY" = 1 ] && { say ""; say "Dry run: nothing was changed."; return 0; }
  cat <<EOF

metacomputer is in $DIR, and its command is ~/.local/bin/metacomputer (alias mcomp).
EOF
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
    say "~/.local/bin isn't on your PATH: add it (echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zprofile),"
    say "or type ~/.local/bin/metacomputer. Claude Code always uses the full path." ;;
  esac
  if [ "$NO_BUILD" = 0 ] && [ "${HEALTH:-}" != failed ]; then
    cat <<EOF
The VM is running (metacomputer stop to stop it; Claude starts it again when it needs it).

Open Claude Code (restart any session that was open, or run /reload-plugins in it) and try:
  "Get yourself a virtual computer and tell me what's on its screen."
  "On your virtual computer, open news.ycombinator.com in Firefox and summarize the top three stories."
  "Is this link safe? Open it on your virtual computer and tell me what it does: <a link you're unsure of>"
EOF
  fi
  say ""
  say "More: $DIR/docs/install.md (updating, uninstalling, troubleshooting) and metacomputer help."
}

# --- the plan, for --confirm and --dry-run ----------------------------------------------------------
plan() {
  pick_dir
  cat <<EOF
${b}metacomputer setup${o} will:
  1. check this Mac (Apple silicon, macOS $MIN_MACOS+, $((VM_FS_GB + CHECKOUT_FS_GB)) GB free where the VM and checkout go, the network);
  2. offer to install Homebrew if it's needed and missing (its installer asks for your password
     itself), and Apple's Command Line Tools if they're missing (a dialog: click Install);
  3. download metacomputer into $DIR (git clone, about 3 MB), or update a clean checkout of
     github.com/$MC_SLUG there (any other checkout, or one with changes, is refused);
  4. run its ./install.sh: Go, GnuPG and ffmpeg from Homebrew if missing, mcomp-vm (the VM runner)
     and the secrets vault built from source, this Mac's settings, the Claude Code plugin, and
     $([ "$NO_BUILD" = 1 ] && echo "NOT the VM (--no-build)" || echo "the VM: an Ubuntu desktop with Firefox$([ "$NO_CHROME" = 1 ] || echo " and Google Chrome"), about 1.5 GB of downloads");
  5. check it: a VM it built gets started and a quick self-test; a VM you already had gets only
     read-only checks (started only if it's stopped), so nothing open in it is touched.
Nothing outside $DIR, ~/.local, ~/.ssh/metacomputer and Claude Code's plugin settings changes,
apart from Homebrew packages and the VM itself (Lume keeps it in ~/.lume). Expect a minute or two
of your time; about 5 minutes in all on a fast connection with the prerequisites in place, 15-30 on a
new Mac. If it's interrupted, run it again: it carries on.
EOF
}

main() {
  DIR=""; DIR_FROM=""; REF=""; REF_KIND=""; REF_COMMIT=""; REF_BRANCH=""; REPO="$MC_REPO"; NO_CHROME=0; NO_BUILD=0; REPLACE_VM=0
  DRY=0; YES=0; CONFIRM=0; HEALTH=""; VM_NEW=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir) [ $# -ge 2 ] && [ -n "$2" ] || { echo "get.sh: --dir needs a folder" >&2; exit 2; }; DIR="$2"; shift ;;
      --dir=*) DIR="${1#--dir=}" ;;
      --ref) [ $# -ge 2 ] && [ -n "$2" ] || { echo "get.sh: --ref needs a tag or branch" >&2; exit 2; }; REF="$2"; shift ;;
      --ref=*) REF="${1#--ref=}" ;;
      --repo) [ $# -ge 2 ] && [ -n "$2" ] || { echo "get.sh: --repo needs a URL or path" >&2; exit 2; }; REPO="$2"; shift ;;
      --repo=*) REPO="${1#--repo=}" ;;
      --no-chrome) NO_CHROME=1 ;;
      --no-build) NO_BUILD=1 ;;
      --replace-vm) REPLACE_VM=1 ;;
      --confirm) CONFIRM=1 ;;
      -y|--yes) YES=1 ;;
      --dry-run) DRY=1 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "get.sh: unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
  done
  case "$REF" in -*|*..*|*[[:space:]]*) echo "get.sh: --ref '$REF' isn't a tag or branch name" >&2; exit 2 ;; esac
  case "$DIR" in '') ;; /*) ;; *) DIR="$PWD/$DIR" ;; esac
  # When piped into bash, stdin is the rest of this script: nothing below may read it.
  [ -t 0 ] || exec </dev/null

  if [ "$CONFIRM" = 1 ] && [ "$DRY" = 0 ]; then
    plan
    if [ "$YES" = 0 ]; then
      if [ "$HAVE_TTY" = 0 ]; then
        say ""
        say "Nothing was changed. To go ahead, run the same command with --yes (or run it in Terminal to be asked)."
        exit 3
      fi
      ask "Set up metacomputer now?" Y || { say "Nothing was changed."; exit 0; }
    fi
  fi
  [ "$DRY" = 1 ] && say "${b}Dry run${o}: checks this Mac and prints what it would do; nothing is changed."
  [ "$DRY" = 1 ] || install_lock get.sh
  check_mac
  prereqs
  checkout
  install
  health
  finish
  [ "${HEALTH:-ok}" != failed ] || exit 1
  exit 0
}

main "$@"
