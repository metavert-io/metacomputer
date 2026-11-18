#!/bin/bash
# metacomputer host library: configuration, Lume + cua-netgw
# lifecycle, SSH. Sourced by bin/metacomputer and scripts/*. macOS /bin/bash 3.2.

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../config/defaults.env
. "$LAB_ROOT/config/defaults.env"
# This machine's settings (install.sh writes it; never committed). MCOMP_CONFIG
# points at another file, e.g. for a separate test VM.
LOCAL_CONFIG="${MCOMP_CONFIG:-$LAB_ROOT/config/local.env}"
# shellcheck source=/dev/null
[ -f "$LOCAL_CONFIG" ] && . "$LOCAL_CONFIG"
GUEST_RUN_DIR="/home/$GUEST_USER/metacomputer-runs"

RUN_DIR="${RUN_DIR:-$LAB_ROOT/.run}"   # RUN_DIR / LOG_DIR: config/defaults.env
# Unix socket paths are limited to ~104 bytes, so the gateway's sockets live
# in a short private folder (per user, checkout and VM; the same from any
# shell, unlike $TMPDIR), not under a possibly deep checkout.
SOCK_DIR="/tmp/mcomp-$(id -u)-$(printf '%s' "$LAB_ROOT:$VM_NAME" | shasum | cut -c1-8)"
CACHE_DIR="$LAB_ROOT/.cache"
LOG_DIR="${LOG_DIR:-$LAB_ROOT/logs}"
NETGW_BIN="$LAB_ROOT/vendor/bin/cua-netgw"
QCOW2RAW_BIN="$LAB_ROOT/vendor/bin/qcow2raw"
KNOWN_HOSTS="$SSH_DIR/known_hosts"
export LUME_TELEMETRY_ENABLED=0
# The VM runner. mcomp-vm was called lume-secure, and a Mac set up before the
# rename may have only ~/.local/bin/lume-secure (the same build; it sees the
# same VMs). While LUME's runner isn't installed and that one is, use it and say
# so once per command. With neither, LUME stays as set and the usual errors apply.
LUME="${LUME:-$HOME/.local/bin/mcomp-vm}"
LUME_FORMER="$HOME/.local/bin/lume-secure"
if ! command -v "$LUME" >/dev/null 2>&1 && [ -x "$LUME_FORMER" ]; then
  [ -n "${MCOMP_LUME_NOTICE:-}" ] || printf '%s\n' "metacomputer: $LUME isn't installed, so this uses $LUME_FORMER, mcomp-vm's former name (./install.sh installs mcomp-vm)" >&2
  export MCOMP_LUME_NOTICE=1   # scripts this command runs don't repeat it
  LUME="$LUME_FORMER"
fi
PY="${PY:-/opt/homebrew/bin/python3}"
[ -x "$PY" ] || PY=python3

mkdir -p "$RUN_DIR" "$LOG_DIR"
export MCOMP_RUN_DIR="$RUN_DIR" MCOMP_LOG_DIR="$LOG_DIR" MCOMP_SSH_PORT="$SSH_PORT"   # for the Python helpers
( umask 077; mkdir -p "$SOCK_DIR" )
{ [ -d "$SOCK_DIR" ] && [ ! -L "$SOCK_DIR" ] && [ -O "$SOCK_DIR" ]; } || { echo "metacomputer: unsafe socket folder $SOCK_DIR" >&2; exit 1; }

c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_off=$'\033[0m'
[ -t 1 ] || { c_bold=; c_red=; c_grn=; c_yel=; c_off=; }
say()  { printf '%s\n' "${c_bold}metacomputer:${c_off} $*"; }
ok()   { printf '%s\n' "  ${c_grn}✓${c_off} $*"; }
warn() { printf '%s\n' "  ${c_yel}!${c_off} $*" >&2; }
die()  { printf '%s\n' "${c_red}metacomputer: $*${c_off}" >&2; exit 1; }

# --- Lume -------------------------------------------------------------------
lume_json() { "$LUME" get "$1" --format json 2>/dev/null; }
lume_field() { # NAME FIELD
  lume_json "$1" | "$PY" -c 'import json,sys
d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d
v=d.get(sys.argv[1]); print("" if v is None else v)' "$2" 2>/dev/null
}
vm_exists() { "$LUME" ls --format json 2>/dev/null | "$PY" -c 'import json,sys
names=[v.get("name") for v in json.load(sys.stdin)]
sys.exit(0 if sys.argv[1] in names else 1)' "$1"; }
# NAME -> "present" or "absent"; status 1 (nothing printed) when Lume's list can't be had or read. Unlike
# vm_exists, an error never looks like "absent": the build relies on this before it deletes or creates.
vm_lookup() {
  local out
  out="$("$LUME" ls --format json 2>/dev/null)" || return 1
  printf '%s' "$out" | "$PY" -c 'import json, sys
try:
    names = {v["name"] for v in json.load(sys.stdin)}
except Exception:
    sys.exit(1)
print("present" if sys.argv[1] in names else "absent")' "$1"
}
# NAME FIELD -> FIELD of `lume get NAME`, which must succeed and describe exactly NAME; status 1 otherwise.
vm_info() {
  local out
  out="$("$LUME" get "$1" --format json 2>/dev/null)" || return 1
  printf '%s' "$out" | "$PY" -c 'import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
d = d if isinstance(d, list) else [d]
if len(d) != 1 or not isinstance(d[0], dict) or d[0].get("name") != sys.argv[1]:
    sys.exit(1)
v = d[0].get(sys.argv[2])
if v is None or v == "":
    sys.exit(1)
print(v)' "$1" "$2"
}
vm_status() { lume_field "$1" status; }
vm_running() { [ "$(vm_status "$1")" = "running" ]; }
vm_mac() { lume_field "$1" macAddress; }
# NAME -> the VM's folder, from Lume itself: the storage location `lume get` names (locationName), looked up
# in `lume config get` by its exact name, listed exactly once, and an existing absolute folder (canonical).
# Nothing is guessed: any doubt is status 1, with the reason on stderr.
vm_dir() {
  local name="$1" loc cfg path home n
  loc="$(vm_info "$name" locationName)" || { echo "Lume didn't say which storage location holds $name" >&2; return 1; }
  cfg="$("$LUME" config get 2>/dev/null)" || { echo "'$LUME config get' failed" >&2; return 1; }
  path="$(printf '%s\n' "$cfg" | MCOMP_LOC="$loc" awk '
    /^Configured VM storage locations:/ { inlist = 1; next }
    inlist && /^[[:space:]]*- / {
      line = $0; sub(/^[[:space:]]*- /, "", line); i = index(line, ": ")
      if (i > 0 && substr(line, 1, i - 1) == ENVIRON["MCOMP_LOC"]) { p = substr(line, i + 2); sub(/ \(default\)$/, "", p); print p }
      next }
    inlist && NF == 0 { inlist = 0 }')"
  n="$(printf '%s' "$path" | grep -c '')"
  [ "$n" = 1 ] || { echo "Lume's settings list the storage location '$loc' $n times (expected once)" >&2; return 1; }
  # Lume expands ~ to the account's home folder, whatever $HOME says.
  home="$("$PY" -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')" || return 1
  case "$path" in "~") path="$home" ;; "~/"*) path="$home/${path#\~/}" ;; esac
  case "$path" in /*) ;; *) echo "the storage location '$loc' isn't an absolute folder: $path" >&2; return 1 ;; esac
  path="$(cd "$path" 2>/dev/null && pwd -P)" || { echo "the storage location '$loc' isn't a folder" >&2; return 1; }
  printf '%s/%s\n' "$path" "$name"
}
# DIR NAME: DIR is NAME's own folder: a real folder of yours (not a link) holding a disk.img and a config.json
# of yours (not links), whose MAC address is the one Lume reports for NAME (random for each VM).
vm_dir_is() {
  local dir="$1" name="$2" mac f
  [ -d "$dir" ] && [ ! -L "$dir" ] && [ -O "$dir" ] || return 1
  for f in disk.img config.json; do [ -f "$dir/$f" ] && [ ! -L "$dir/$f" ] && [ -O "$dir/$f" ] || return 1; done
  mac="$(vm_info "$name" macAddress)" || return 1
  "$PY" -c 'import json, sys
sys.exit(0 if str(json.load(open(sys.argv[1])).get("macAddress", "")).lower() == sys.argv[2].lower() else 1)' "$dir/config.json" "$mac" 2>/dev/null
}

# --- locks ------------------------------------------------------------------------
# An exclusive flock on a file in a private per-user folder, taken on a file descriptor this shell keeps
# open (7 for a VM's lock, 8 for the install lock). Every process this shell starts inherits that
# descriptor, so the lock lasts as long as the last of them (killing the shell doesn't release it while its
# children work), and the kernel drops it once they've all exited: no stale lock survives. daemonize()
# closes inherited descriptors, so a long-lived daemon never keeps a lock. A process that inherited a held
# lock's descriptor holds that lock already. perl (part of macOS) takes it. The folder doesn't depend on
# $HOME, because Lume keeps VMs in the account's home whatever $HOME says; MCOMP_LOCK_DIR moves it (tests).
mc_lock_dir() {
  local d="${MCOMP_LOCK_DIR:-}"
  if [ -z "$d" ]; then
    d="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)"; d="${d%/}"; [ -n "$d" ] || d="/tmp/mcomp-$(id -u)"
    d="$d/metacomputer-locks"
  fi
  ( umask 077; mkdir -p "$d" ) 2>/dev/null
  [ -d "$d" ] && [ ! -L "$d" ] && [ -O "$d" ] && chmod 700 "$d" 2>/dev/null || return 1
  printf '%s' "$d"
}
# FD FILE: 0 when FD is already open on FILE and holds its lock (inherited from the process that took it).
lock_fd_held() {
  /usr/bin/perl -e 'use Fcntl qw(:flock); my ($fd, $file) = @ARGV;
    open(my $fh, ">>&=", $fd) or exit 1; my @a = stat($fh); my @b = stat($file);
    exit 1 unless @a && @b && $a[0] == $b[0] && $a[1] == $b[1];
    exit(flock($fh, LOCK_EX | LOCK_NB) ? 0 : 1)' "$1" "$2" 2>/dev/null
}
# FD WAIT: take the lock on the file FD is open on, waiting up to WAIT seconds; 0 = taken.
lock_fd_take() {
  /usr/bin/perl -e 'use Fcntl qw(:flock); my ($fd, $wait) = @ARGV;
    open(my $fh, ">>&=", $fd) or exit 2; my $end = time + $wait;
    until (flock($fh, LOCK_EX | LOCK_NB)) { exit 1 if time >= $end; select(undef, undef, undef, 0.2); }
    exit 0' "$1" "$2" 2>/dev/null
}
lock_hold() { # NAME WAIT_SECONDS WHAT [FD] -> 0 held; 1 another process holds it (LOCK_OWNER says which); 2 no lock folder
  local name="$1" wait="$2" what="$3" fd="${4:-7}" dir file
  LOCK_OWNER=""
  case "$fd" in 7|8) ;; *) return 2 ;; esac
  dir="$(mc_lock_dir)" || return 2
  file="$dir/$name.lock"
  lock_fd_held "$fd" "$file" && return 0
  eval "exec $fd>>\"\$file\"" || return 2
  if ! lock_fd_take "$fd" "$wait"; then
    eval "exec $fd>&-"
    LOCK_OWNER="$(sed -n 's/^what=//p' "$dir/$name.owner" 2>/dev/null), pid $(sed -n 's/^pid=//p' "$dir/$name.owner" 2>/dev/null), since $(sed -n 's/^since=//p' "$dir/$name.owner" 2>/dev/null)"
    return 1
  fi
  printf 'pid=%s\nsince=%s\nwhat=%s\n' "$$" "$(date '+%H:%M:%S')" "$what" > "$dir/$name.owner"
}
vm_lock_name() { # VM NAME -> its lock's name
  case "$1" in ''|*[!A-Za-z0-9._-]*) printf 'vm-%s' "$(printf '%s' "$1" | shasum | cut -c1-16)" ;; *) printf 'vm-%s' "$1" ;; esac
}
# The VM lock for a command that deletes, replaces, restores or snapshots VM_NAME, its baseline or its
# checkpoints: the same lock as a build's, so none of them can run during a build or each other.
vm_lock_or_die() { # WHAT
  local rc=0
  lock_hold "$(vm_lock_name "$VM_NAME")" "${MCOMP_LOCK_WAIT:-10}" "$1" || rc=$?
  case "$rc" in
    0) ;;
    1) die "another build or change of the VM $VM_NAME is running ($LOCK_OWNER); nothing was changed. Run this again when it's done" ;;
    *) die "couldn't make the private lock folder (MCOMP_LOCK_DIR, or the per-user temporary folder)" ;;
  esac
}

# --- detached processes (own session, so they outlive the calling shell) ----
daemonize() { # LOGFILE CMD ARGS...: in its own session, with no descriptors but 0-2 (so it never keeps a lock)
  local log="$1"; shift
  "$PY" -c 'import os,sys
os.setsid()
fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o644)
os.dup2(fd,1); os.dup2(fd,2)
n=os.open("/dev/null", os.O_RDONLY); os.dup2(n,0)
os.closerange(3, 4096)
os.execvp(sys.argv[2], sys.argv[2:])' "$log" "$@" &
}

# --- cua-netgw (host-enforced egress policy, loopback-only forwards) -------
netgw_pid() { if [ -s "$RUN_DIR/netgw.pid" ]; then cat "$RUN_DIR/netgw.pid"; fi; return 0; }
netgw_alive() { local p; p="$(netgw_pid)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
netgw_stop() {
  local p; p="$(netgw_pid)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    kill "$p" 2>/dev/null
    local i=0; while kill -0 "$p" 2>/dev/null && [ $i -lt 10 ]; do sleep 0.5; i=$((i+1)); done
    kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
  fi
  rm -f "$RUN_DIR/netgw.pid" "$SOCK_DIR/net.sock" "$SOCK_DIR/control.sock" "$SOCK_DIR"/lume-*.sock
}
netgw_render() { # MAC -> writes $RUN_DIR/gw.json
  local mac="$1"
  cat > "$RUN_DIR/gw.json" <<EOF
{
  "subnet": "$NET_SUBNET",
  "gateway_ip": "$NET_GATEWAY_IP",
  "gateway_mac": "$NET_GATEWAY_MAC",
  "dhcp": true,
  "static_leases": { "$NET_GUEST_IP": "$mac" },
  "dns_upstream": [ $NET_DNS_UPSTREAM ],
  "forwards": [
    { "host": "127.0.0.1:$SSH_PORT", "guest": "$NET_GUEST_IP:22" },
    { "host": "127.0.0.1:$VNC_PORT", "guest": "$NET_GUEST_IP:5900" }
  ],
  "control": "unix://$SOCK_DIR/control.sock",
  "listen": { "vfkit": "unixgram://$SOCK_DIR/net.sock" },
  "rules": "$LAB_ROOT/config/netgw-rules.json",
  "events": "$LOG_DIR/netgw-blocked.jsonl",
  "pid_file": "$RUN_DIR/netgw.pid",
  "log_level": "warning"
}
EOF
}
netgw_start() { # MAC
  [ -x "$NETGW_BIN" ] || die "missing $NETGW_BIN (run scripts/build-tools.sh)"
  netgw_stop
  netgw_render "$1"
  "$NETGW_BIN" -check-rules "$LAB_ROOT/config/netgw-rules.json" >/dev/null || die "bad netgw rules"
  daemonize "$LOG_DIR/netgw.log" "$NETGW_BIN" -config "$RUN_DIR/gw.json"
  local i=0; while [ ! -S "$SOCK_DIR/net.sock" ] && [ $i -lt 40 ]; do sleep 0.25; i=$((i+1)); done
  [ -S "$SOCK_DIR/net.sock" ] || { tail -5 "$LOG_DIR/netgw.log" >&2; die "cua-netgw did not start"; }
}
netgw_control() { # "lease MAC" | "probe IP:PORT"
  "$PY" - "$SOCK_DIR/control.sock" "$1" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(12)
try:
    s.connect(sys.argv[1]); s.sendall((sys.argv[2] + "\n").encode())
    print(s.recv(256).decode().strip())
except Exception as e:
    print("no " + str(e))
EOF
}

# --- VM run/stop ------------------------------------------------------------
# Host-sharing devices: Lume attaches the SPICE clipboard port, a sound device
# (host speakers + microphone) and the Rosetta share to every VM. A Lume with
# the opt-out flags (mcomp-vm, or a Lume that adopted them) leaves out each
# one whose SHARE_* setting isn't "on"; any other Lume always shares them.
host_sharing_args() {
  if "$LUME" run --help 2>&1 | grep -q -- "--no-host-sharing"; then
    local a=""
    [ "${SHARE_CLIPBOARD:-off}" = on ] || a="$a --no-share-clipboard"
    [ "${SHARE_AUDIO:-off}" = on ] || a="$a --no-share-audio"
    [ "${SHARE_ROSETTA:-off}" = on ] || a="$a --no-share-rosetta"
    printf '%s' "$a"
  elif [ "${SHARE_CLIPBOARD:-off}${SHARE_AUDIO:-off}${SHARE_ROSETTA:-off}" != ononon ]; then
    warn "$LUME always shares your clipboard, audio and Rosetta with the VM; mcomp-vm can turn them off (README: Isolation)"
  fi
}

STORAGE_RUN_ARGS=()   # extra `lume run` arguments for this VM's storage (bin/metacomputer storage_prepare)
vm_start() { # NAME [seed.iso]
  local name="$1" seed="${2:-}" mac
  vm_running "$name" && { ok "$name already running"; return 0; }
  mac="$(vm_mac "$name")"; [ -n "$mac" ] || die "cannot read $name's MAC address from lume"
  netgw_start "$mac"
  local args="run $name --display none --vnc disabled --network gateway:$SOCK_DIR/net.sock"
  [ -n "$seed" ] && args="$args --mount $seed"
  args="$args$(host_sharing_args)"
  # shellcheck disable=SC2086
  daemonize "$LOG_DIR/lume-$name.log" "$LUME" $args ${STORAGE_RUN_ARGS[@]+"${STORAGE_RUN_ARGS[@]}"}
  # The run process is the VM (Virtualization.framework runs in it): storage locks name it as their holder.
  echo "$!" > "$RUN_DIR/lume.pid"
  local i=0; while ! vm_running "$name" && [ $i -lt 60 ]; do sleep 0.5; i=$((i+1)); done
  vm_running "$name" || { tail -5 "$LOG_DIR/lume-$name.log" >&2; netgw_stop; die "VM $name did not start"; }
}
vm_stop() { # NAME [--force]
  # `lume stop` on a detached run is an immediate power-off (Lume SIGTERMs the
  # run process, which calls VZVirtualMachine.stop): recent writes in the guest
  # are lost. So shut the guest down from inside first (sync + systemd
  # poweroff via the admin verb) and only fall back to Lume's stop after 90 s.
  # The 90 s run from the start, whatever the guest does with the request: the
  # SSH command runs in the background, so a guest that keeps it open without
  # ever powering off can't hold the Mac's side up.
  local name="$1" i=0 sp deadline="${MCOMP_STOP_DEADLINE:-90}"
  if vm_running "$name"; then
    if [ "${2:-}" != "--force" ]; then
      gssh admin 'sync; sudo -n /usr/local/sbin/metacomputer-admin poweroff' >/dev/null 2>&1 &
      sp=$!
      while vm_running "$name" && [ $i -lt "$deadline" ]; do sleep 1; i=$((i+1)); done
      kill "$sp" 2>/dev/null; wait "$sp" 2>/dev/null
      vm_running "$name" && warn "guest did not power off within $deadline s; forcing"
    fi
    vm_running "$name" && vm_force_stop "$name"
  fi
  i=0; while vm_running "$name" && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  netgw_stop
}
vm_force_stop() { # NAME: Lume's immediate power-off, itself bounded (30 s)
  local name="$1" i=0 sp
  "$LUME" stop "$name" --force >/dev/null 2>&1 &
  sp=$!
  while kill -0 "$sp" 2>/dev/null && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  kill "$sp" 2>/dev/null; wait "$sp" 2>/dev/null
  return 0
}
vm_kill() { # NAME: power the VM off from the Mac now, asking the guest nothing (storage-security failures)
  # The VM's own `lume run` process (checked to be this VM's) gets SIGTERM, then SIGKILL after 10 s
  # (lib/storage.py kill-vm); then Lume's stop as a fallback. Every step has a host deadline.
  local name="$1" i=0
  ( export_options; MCOMP_LAB_ROOT="$LAB_ROOT" "$PY" "$LAB_ROOT/lib/storage.py" kill-vm --grace 10 ) >/dev/null 2>&1 || true
  vm_running "$name" && vm_force_stop "$name"
  while vm_running "$name" && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  netgw_stop
}

# --- SSH --------------------------------------------------------------------
# The optional hardening settings (SECURITY.md), resolved for the Python helpers.
export_options() {
  export MCOMP_ACTION_LOG="$ACTION_LOG" MCOMP_ACTION_LOG_TEXT="$ACTION_LOG_TEXT" MCOMP_ACTION_LOG_DIR="$LOG_DIR/actions" \
         MCOMP_ACTION_LOG_KEY="${ACTION_LOG_KEY:-$SSH_DIR/action-log}" MCOMP_SSH_DIR="$SSH_DIR" MCOMP_VM_NAME="$VM_NAME" \
         MCOMP_SSH_AUTH="$SSH_AUTH" MCOMP_SSH_CERT_TTL="$SSH_CERT_TTL" MCOMP_DRIVER_MODE="$DRIVER_MODE" MCOMP_DRIVER_PROFILE="$DRIVER_PROFILE" \
         MCOMP_DRIVER_MANIFEST="$DRIVER_MANIFEST" MCOMP_DRIVER_ORIGINS="$DRIVER_ORIGINS" MCOMP_DRIVER_EXPIRES="$DRIVER_EXPIRES" \
         MCOMP_DRIVER_IDLE="$DRIVER_IDLE" MCOMP_BROWSERS="$BROWSERS" MCOMP_GUEST_USER="$GUEST_USER" \
         MCOMP_VM_SHELL="${VM_SHELL:-auto}" MCOMP_SHELL_LOG="${SHELL_LOG:-auto}" \
         MCOMP_STORAGE_DIR="${STORAGE_DIR:-}" MCOMP_VOLUMES="${VOLUMES:-}" MCOMP_REFDISKS="${REFDISKS:-}"
  # The hub (lib/mcp_hub.py) and its coordinate guard. MCOMP_MCP_HUB / MCOMP_COORD_GUARD already in the
  # environment win over the config (the benchmark's switches).
  export MCOMP_MCP_HUB="${MCOMP_MCP_HUB:-${MCP_HUB:-on}}" MCOMP_COORD_GUARD="${MCOMP_COORD_GUARD:-${COORD_GUARD:-on}}" \
         MCOMP_HUB_IDLE="${HUB_IDLE:-10m}"
  # Approved lessons (metacomputer learn, lib/learn_store.py): the environment wins here too (the benchmark's
  # lessons switch, an isolated store for tests).
  export MCOMP_LESSONS="${MCOMP_LESSONS:-${LESSONS:-on}}" MCOMP_LESSONS_DIR="${MCOMP_LESSONS_DIR:-${LESSONS_DIR:-}}"
}

# --- the optional event stream (docs/events.md; off until a sink is added) ------
# Best effort: it never fails the command, and waits at most about 3 s per sink.
emit_event() { # FAMILY TYPE SEVERITY MESSAGE ACTION [KEY=VALUE ...] (ACTION may be "")
  local fam="$1" typ="$2" sev="$3" msg="$4" act="$5" kv f=()
  shift 5
  for kv in "$@"; do f+=(--field "$kv"); done
  ( export_options
    "$PY" "$LAB_ROOT/lib/events.py" emit "$fam" "$typ" --severity "$sev" --message "$msg" \
      ${act:+--action "$act"} ${f[@]+"${f[@]}"} --quiet ) >/dev/null 2>&1 || true
}
# EVENTS HOOK (secrets): `metacomputer secrets` exits 3 when the vault isn't unlocked (or
# doesn't exist yet). bin/metacomputer calls this from an EXIT trap for that command (see
# there). Quiet when you ran it yourself at a terminal, and at most once in 5 minutes.
events_hook_secrets_exit() { # RC
  [ "${1:-0}" = 3 ] || return 0
  [ -t 0 ] && return 0
  ( export_options
    "$PY" "$LAB_ROOT/lib/events.py" emit needs_you vault_locked --severity warn --debounce 300 --quiet \
      --message "The secrets vault isn't unlocked (or doesn't exist yet), so Claude can't fill a login" \
      --action "In your own terminal: metacomputer secrets unlock (or secrets init, if there's no vault yet)" \
      --field exit=3 ) >/dev/null 2>&1 || true
}

# --- SSH_AUTH=cert: a fresh key and a short-lived certificate per connection ---
CA_KEY="${SSH_CA_KEY:-$SSH_DIR/ca}"
ca_pub() { case "$CA_KEY" in *.pub) echo "$CA_KEY" ;; *) echo "$CA_KEY.pub" ;; esac; }
ensure_ca() { # the default CA is a key file made on first use; a .pub means a key you keep in ssh-agent
  case "$CA_KEY" in *.pub) [ -f "$CA_KEY" ] || die "SSH_CA_KEY $CA_KEY not found" ; return 0 ;; esac
  [ -f "$CA_KEY" ] && return 0
  ( umask 077; mkdir -p "$(dirname "$CA_KEY")"; ssh-keygen -q -t ed25519 -N '' -C metacomputer-ca -f "$CA_KEY" ) \
    || die "could not create the SSH CA key $CA_KEY"
}
CERT_ops=""; CERT_ops_at=0; CERT_mcp=""; CERT_mcp_at=0
cert_ttl_s() { local t="${SSH_CERT_TTL:-5m}"; case "$t" in *m) echo $(( ${t%m} * 60 )) ;; *h) echo $(( ${t%h} * 3600 )) ;; *s) echo "${t%s}" ;; *) echo 300 ;; esac; }
cert_for() { # ROLE -> sets CERT_KEY (KEY-cert.pub beside it); reused for half its lifetime in this process
  local role="$1" now dir key id agent=() opts=() cached at
  now="$(date +%s)"; eval "cached=\$CERT_$role; at=\$CERT_${role}_at"
  if [ -n "$cached" ] && [ -f "$cached-cert.pub" ] && [ $(( now - at )) -lt $(( $(cert_ttl_s) / 2 )) ]; then CERT_KEY="$cached"; return 0; fi
  ensure_ca
  dir="$SOCK_DIR/certs"; ( umask 077; mkdir -p "$dir" )
  find "$dir" -type f -mmin +60 -delete 2>/dev/null
  key="$(mktemp "$dir/$role.XXXXXX")" && rm -f "$key"
  ssh-keygen -q -t ed25519 -N '' -C "mcomp-$role" -f "$key" || return 1
  # The key ID appears in the VM's auth log: who asked, from which project, which process.
  id="mcomp:$role:$(id -un):$(basename "$PWD" | tr -cd 'A-Za-z0-9._-' | cut -c1-40):pid$$"
  case "$CA_KEY" in *.pub) agent=(-U) ;; esac
  opts=(-O clear -O source-address="$NET_GATEWAY_IP/32")
  case "$role" in
    mcp) opts+=(-O force-command=/opt/metacomputer/bin/driver-mcp) ;;
    ops) opts+=(-O permit-pty) ;;
  esac
  ssh-keygen -q -s "$CA_KEY" ${agent[@]+"${agent[@]}"} -I "$id" -n "$role" -V "-2m:+${SSH_CERT_TTL:-5m}" \
    -z "$now$(printf '%05d' "$RANDOM")" "${opts[@]}" "$key.pub" 2>/dev/null || { rm -f "$key" "$key.pub"; return 1; }
  rm -f "$key.pub"
  eval "CERT_$role=\$key; CERT_${role}_at=\$now"
  CERT_KEY="$key"
}

ssh_base() { # ROLE(admin|ops|mcp) -> prints nothing; sets SSH_ARGS array
  local role="$1" user
  case "$role" in admin) user="$ADMIN_USER" ;; ops|mcp) user="$GUEST_USER" ;; *) die "bad ssh role $role" ;; esac
  local ids=(-o IdentityFile="$SSH_DIR/$role")
  if [ "$role" != admin ]; then
    if [ "${SSH_AUTH:-key}" = cert ]; then
      # The certificate first; the static key stays as a fallback for a VM that
      # hasn't switched yet (start switches it). A switched VM refuses static keys.
      cert_for "$role" || die "could not make an SSH certificate for $role (SSH_CA_KEY: $CA_KEY)"
      ids=(-o IdentityFile="$CERT_KEY" -o CertificateFile="$CERT_KEY-cert.pub" -o IdentityFile="$SSH_DIR/$role")
    elif [ -f "$RUN_DIR/guest-options-$VM_NAME" ] && [ -f "$(ca_pub)" ] && cert_for "$role" 2>/dev/null; then
      # Back on static keys in the config, but the VM may still want a certificate
      # until start (or security apply) switches it back.
      ids+=(-o IdentityFile="$CERT_KEY" -o CertificateFile="$CERT_KEY-cert.pub")
    fi
  fi
  # The VM is untrusted. No ssh config file applies (-F /dev/null: no Host blocks from
  # ~/.ssh/config or /etc/ssh, so no ProxyCommand, forwards or multiplexing from there),
  # and nothing of the Mac's is forwarded to it: no agent, no X11, no ports, no tunnel.
  SSH_ARGS=(-F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes "${ids[@]}"
            -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null
            -o UpdateHostKeys=no -o ForwardAgent=no -o ForwardX11=no -o ForwardX11Trusted=no
            -o ClearAllForwardings=yes -o Tunnel=no -o ControlMaster=no -o ControlPath=none
            -o PermitLocalCommand=no -o ProxyCommand=none
            -o ConnectTimeout=5 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o LogLevel=ERROR
            -p "$SSH_PORT" "$user@127.0.0.1")
}
gssh() { # ROLE CMD...  (non-interactive)
  local role="$1"; shift
  ssh_base "$role"
  if [ -t 2 ]; then
    # The remote side writes this stderr, and the VM is untrusted: on a terminal it's shown escaped (and at
    # most 1 MB of it), never raw (lib/termsafe.py). Pipes and files get it exactly; `metacomputer ssh` is raw.
    ssh -T "${SSH_ARGS[@]}" "$@" 2> >("$PY" "$LAB_ROOT/lib/termsafe.py" --max 1048576 >&2)
  else
    ssh -T "${SSH_ARGS[@]}" "$@"
  fi
}
wait_ssh() { # ROLE TIMEOUT_S
  local role="$1" t="${2:-300}" start; start=$(date +%s)
  while :; do
    gssh "$role" true 2>/dev/null && return 0
    [ $(( $(date +%s) - start )) -ge "$t" ] && return 1
    sleep 3
  done
}
wait_session() { # TIMEOUT_S
  local t="${1:-240}" start; start=$(date +%s)
  while :; do
    gssh ops 'test -s /run/user/$(id -u)/metacomputer/session-ready' 2>/dev/null && return 0
    [ $(( $(date +%s) - start )) -ge "$t" ] && return 1
    sleep 3
  done
}

# --- DRIVER_MODE=bounded: upload the manifest, ask for it by hash -------------
driver_request() { # prints the SSH command line for the MCP role ("" = Cua's default)
  [ "${DRIVER_MODE:-standard}" = bounded ] || return 0
  local m sha
  m="$(export_options; "$PY" "$LAB_ROOT/lib/driver_profiles.py" render)" || exit 1
  sha="$(printf '%s\n' "$m" | shasum -a 256 | cut -d' ' -f1)"
  printf '%s\n' "$m" | gssh ops "d=~/.config/metacomputer/manifests; f=\$d/$sha.yaml; [ -f \$f ] || { mkdir -p \$d && cat > \$f.tmp && mv \$f.tmp \$f; }" \
    || die "could not upload the driver manifest to the VM"
  echo "bounded $sha"
}

# --- keep the VM's side of SSH_AUTH and DRIVER_MODE in step with the config ----
guest_option() { # ssh-auth|driver-policy -> the VM's current setting
  gssh admin "sudo /usr/local/sbin/metacomputer-admin $1 status" 2>/dev/null | tail -1
}
sync_guest_options() { # [all]: quiet unless something changes; skips the VM in the default setup unless "all"
  local mark="$RUN_DIR/guest-options-$VM_NAME" have all="${1:-}"
  if [ "${SSH_AUTH:-key}" = cert ] || [ -f "$mark" ] || [ "$all" = all ]; then
    have="$(guest_option ssh-auth)"
    case "$have" in cert|key) ;; *) warn "this VM can't switch SSH_AUTH (built by an earlier version?); rebuild it to use certificates"; have="${SSH_AUTH:-key}" ;; esac
    if [ "$have" != "${SSH_AUTH:-key}" ]; then
      if [ "${SSH_AUTH:-key}" = cert ]; then
        ensure_ca
        gssh admin "sudo /usr/local/sbin/metacomputer-admin ssh-auth cert" < "$(ca_pub)" >/dev/null \
          && ok "SSH: the VM now accepts only short-lived certificates for ops and mcp (SSH_AUTH=cert)" \
          || die "could not switch the VM to SSH certificates (metacomputer logs)"
      else
        gssh admin "sudo /usr/local/sbin/metacomputer-admin ssh-auth key" >/dev/null \
          && ok "SSH: the VM is back on its static keys (SSH_AUTH=key)" || warn "could not switch the VM back to static keys"
      fi
    fi
  fi
  if [ "${DRIVER_MODE:-standard}" = bounded ] || [ -f "$mark" ] || [ "$all" = all ]; then
    have="$(guest_option driver-policy)"
    case "$have" in bounded|standard) ;; *) warn "this VM can't hold a driver policy (built by an earlier version?); bounded sessions still work, but standard ones aren't refused"; have="${DRIVER_MODE:-standard}" ;; esac
    if [ "$have" != "${DRIVER_MODE:-standard}" ]; then
      gssh admin "sudo /usr/local/sbin/metacomputer-admin driver-policy ${DRIVER_MODE:-standard}" >/dev/null \
        && ok "driver: the VM now starts Cua Driver only in ${DRIVER_MODE:-standard} mode" || warn "could not set the VM's driver policy"
    fi
  fi
  if [ "${SSH_AUTH:-key}" = cert ] || [ "${DRIVER_MODE:-standard}" = bounded ]; then : > "$mark"; else rm -f "$mark"; fi
}
