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
LUME="${LUME:-$HOME/.local/bin/lume-secure}"
PY="${PY:-/opt/homebrew/bin/python3}"
[ -x "$PY" ] || PY=python3

mkdir -p "$RUN_DIR" "$LOG_DIR"
export MCOMP_RUN_DIR="$RUN_DIR" MCOMP_LOG_DIR="$LOG_DIR"   # for the Python helpers
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
      gssh admin 'sync; sudo -n /usr/local/sbin/metacomputer-admin poweroff' >/dev/null 2>&1 || true
      while vm_running "$name" && [ $i -lt 90 ]; do sleep 1; i=$((i+1)); done
      vm_running "$name" && warn "guest did not power off within 90 s; forcing"
    fi
    vm_running "$name" && "$LUME" stop "$name" --force >/dev/null 2>&1
  fi
  i=0; while vm_running "$name" && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done
  netgw_stop
}

# --- SSH --------------------------------------------------------------------
# The optional hardening settings (SECURITY.md), resolved for the Python helpers.
export_options() {
  export MCOMP_ACTION_LOG="$ACTION_LOG" MCOMP_ACTION_LOG_TEXT="$ACTION_LOG_TEXT" MCOMP_ACTION_LOG_DIR="$LOG_DIR/actions" \
         MCOMP_ACTION_LOG_KEY="${ACTION_LOG_KEY:-$SSH_DIR/action-log}" MCOMP_SSH_DIR="$SSH_DIR" MCOMP_VM_NAME="$VM_NAME" \
         MCOMP_SSH_AUTH="$SSH_AUTH" MCOMP_SSH_CERT_TTL="$SSH_CERT_TTL" MCOMP_DRIVER_MODE="$DRIVER_MODE" MCOMP_DRIVER_PROFILE="$DRIVER_PROFILE" \
         MCOMP_DRIVER_MANIFEST="$DRIVER_MANIFEST" MCOMP_DRIVER_ORIGINS="$DRIVER_ORIGINS" MCOMP_DRIVER_EXPIRES="$DRIVER_EXPIRES" \
         MCOMP_DRIVER_IDLE="$DRIVER_IDLE" MCOMP_BROWSERS="$BROWSERS" MCOMP_GUEST_USER="$GUEST_USER"
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
  SSH_ARGS=(-o BatchMode=yes -o IdentitiesOnly=yes "${ids[@]}"
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
