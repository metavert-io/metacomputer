#!/bin/bash
# virtual-computer host library: configuration, Lume + cua-netgw
# lifecycle, SSH. Sourced by bin/virtual-computer and scripts/*. macOS /bin/bash 3.2.

LAB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../config/defaults.env
. "$LAB_ROOT/config/defaults.env"
# This machine's settings (install.sh writes it; never committed). VC_CONFIG
# points at another file, e.g. for a separate test VM.
LOCAL_CONFIG="${VC_CONFIG:-$LAB_ROOT/config/local.env}"
# shellcheck source=/dev/null
[ -f "$LOCAL_CONFIG" ] && . "$LOCAL_CONFIG"
GUEST_RUN_DIR="/home/$GUEST_USER/virtual-computer-runs"

RUN_DIR="$LAB_ROOT/.run"
# Unix socket paths are limited to ~104 bytes, so the gateway's sockets live
# in a short private folder (per user, checkout and VM; the same from any
# shell, unlike $TMPDIR), not under a possibly deep checkout.
SOCK_DIR="/tmp/vc-$(id -u)-$(printf '%s' "$LAB_ROOT:$VM_NAME" | shasum | cut -c1-8)"
CACHE_DIR="$LAB_ROOT/.cache"
LOG_DIR="$LAB_ROOT/logs"
NETGW_BIN="$LAB_ROOT/vendor/bin/cua-netgw"
QCOW2RAW_BIN="$LAB_ROOT/vendor/bin/qcow2raw"
KNOWN_HOSTS="$SSH_DIR/known_hosts"
export LUME_TELEMETRY_ENABLED=0
LUME="${LUME:-$HOME/.local/bin/lume-secure}"
PY="${PY:-/opt/homebrew/bin/python3}"
[ -x "$PY" ] || PY=python3

mkdir -p "$RUN_DIR" "$LOG_DIR"
( umask 077; mkdir -p "$SOCK_DIR" )
{ [ -d "$SOCK_DIR" ] && [ ! -L "$SOCK_DIR" ] && [ -O "$SOCK_DIR" ]; } || { echo "virtual-computer: unsafe socket folder $SOCK_DIR" >&2; exit 1; }

c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_off=$'\033[0m'
[ -t 1 ] || { c_bold=; c_red=; c_grn=; c_yel=; c_off=; }
say()  { printf '%s\n' "${c_bold}virtual-computer:${c_off} $*"; }
ok()   { printf '%s\n' "  ${c_grn}✓${c_off} $*"; }
warn() { printf '%s\n' "  ${c_yel}!${c_off} $*" >&2; }
die()  { printf '%s\n' "${c_red}virtual-computer: $*${c_off}" >&2; exit 1; }

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
vm_status() { lume_field "$1" status; }
vm_running() { [ "$(vm_status "$1")" = "running" ]; }
vm_mac() { lume_field "$1" macAddress; }

# --- detached processes (own session, so they outlive the calling shell) ----
daemonize() { # LOGFILE CMD ARGS...
  local log="$1"; shift
  "$PY" -c 'import os,sys
os.setsid()
fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o644)
os.dup2(fd,1); os.dup2(fd,2)
n=os.open("/dev/null", os.O_RDONLY); os.dup2(n,0)
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
# the opt-out flags (lume-secure, or a Lume that adopted them) leaves out each
# one whose SHARE_* setting isn't "on"; any other Lume always shares them.
host_sharing_args() {
  if "$LUME" run --help 2>&1 | grep -q -- "--no-host-sharing"; then
    local a=""
    [ "${SHARE_CLIPBOARD:-off}" = on ] || a="$a --no-share-clipboard"
    [ "${SHARE_AUDIO:-off}" = on ] || a="$a --no-share-audio"
    [ "${SHARE_ROSETTA:-off}" = on ] || a="$a --no-share-rosetta"
    printf '%s' "$a"
  elif [ "${SHARE_CLIPBOARD:-off}${SHARE_AUDIO:-off}${SHARE_ROSETTA:-off}" != ononon ]; then
    warn "$LUME always shares your clipboard, audio and Rosetta with the VM; lume-secure can turn them off (README: Isolation)"
  fi
}

vm_start() { # NAME [seed.iso]
  local name="$1" seed="${2:-}" mac
  vm_running "$name" && { ok "$name already running"; return 0; }
  mac="$(vm_mac "$name")"; [ -n "$mac" ] || die "cannot read $name's MAC address from lume"
  netgw_start "$mac"
  local args="run $name --display none --vnc disabled --network gateway:$SOCK_DIR/net.sock"
  [ -n "$seed" ] && args="$args --mount $seed"
  args="$args$(host_sharing_args)"
  # shellcheck disable=SC2086
  daemonize "$LOG_DIR/lume-$name.log" "$LUME" $args
  local i=0; while ! vm_running "$name" && [ $i -lt 60 ]; do sleep 0.5; i=$((i+1)); done
  vm_running "$name" || { tail -5 "$LOG_DIR/lume-$name.log" >&2; netgw_stop; die "VM $name did not start"; }
}
vm_stop() { # NAME [--force]
  # `lume stop` on a detached run is an immediate power-off (Lume SIGTERMs the
  # run process, which calls VZVirtualMachine.stop): recent writes in the guest
  # are lost. So shut the guest down from inside first (sync + systemd
  # poweroff via the admin verb) and only fall back to Lume's stop after 90 s.
  local name="$1" i=0
  if vm_running "$name"; then
    if [ "${2:-}" != "--force" ]; then
      gssh admin 'sync; sudo -n /usr/local/sbin/virtual-computer-admin poweroff' >/dev/null 2>&1 || true
      while vm_running "$name" && [ $i -lt 90 ]; do sleep 1; i=$((i+1)); done
      vm_running "$name" && warn "guest did not power off within 90 s; forcing"
    fi
    vm_running "$name" && "$LUME" stop "$name" --force >/dev/null 2>&1
  fi
  i=0; while vm_running "$name" && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  netgw_stop
}

# --- SSH --------------------------------------------------------------------
ssh_base() { # ROLE(admin|ops|mcp) -> prints nothing; sets SSH_ARGS array
  local role="$1" user
  case "$role" in admin) user="$ADMIN_USER" ;; ops|mcp) user="$GUEST_USER" ;; *) die "bad ssh role $role" ;; esac
  SSH_ARGS=(-o BatchMode=yes -o IdentitiesOnly=yes -o IdentityFile="$SSH_DIR/$role"
            -o UserKnownHostsFile="$KNOWN_HOSTS" -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null
            -o ConnectTimeout=5 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 -o LogLevel=ERROR
            -p "$SSH_PORT" "$user@127.0.0.1")
}
gssh() { # ROLE CMD...  (non-interactive)
  local role="$1"; shift
  ssh_base "$role"; ssh -T "${SSH_ARGS[@]}" "$@"
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
    gssh ops 'test -s /run/user/$(id -u)/virtual-computer/session-ready' 2>/dev/null && return 0
    [ $(( $(date +%s) - start )) -ge "$t" ] && return 1
    sleep 3
  done
}
