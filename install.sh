#!/bin/bash
# install.sh: set up metacomputer on this Mac (Apple silicon). New here? get.sh does this
# and the steps before it (Homebrew, Apple's Command Line Tools, the download): docs/install.md.
#
#   ./install.sh               shows what it will do and asks once (Enter = yes)
#   ./install.sh --yes         no question: the defaults (mcomp-vm, Chrome included, build now)
#   ./install.sh --no-chrome   Firefox only in the VM
#   ./install.sh --lume=standard   Cua's notarized Lume instead of mcomp-vm (the
#                              VM then always shares your clipboard, audio and Rosetta);
#                              (--lume=mcomp-vm, or --lume=secure as before the rename, picks mcomp-vm)
#   ./install.sh --no-build    set everything up but don't build the VM yet
#   ./install.sh --replace-vm  delete the VM (and its clean snapshot) and build it again: for a build that
#                              didn't finish, or a VM without its snapshot. Never implied by --yes
#   ./install.sh --fresh-sources   build the host tools and mcomp-vm from new temporary clones, rebuilding
#                              the tools even if they're up to date; the checkout's .cache is left alone
#                              (what `metacomputer setup` from the plugin uses)
#   ./install.sh --no-claude   don't touch Claude Code (plugin, skill, MCP server, metacomputer command)
#   ./install.sh --no-plugin   Claude Code without the plugin: a skill link and a user-scope MCP
#                              server, as before the plugin (tools: mcp__metacomputer__*)
#   ./install.sh --only-claude only the Claude Code step (no tools, Lume, settings, vault or VM)
#   ./install.sh --codex       also set metacomputer up for Codex (docs/codex.md): done anyway on a first install when
#                              Codex is installed, and refreshed by later runs once it's set up
#   ./install.sh --no-codex    don't touch Codex
#   ./install.sh --only-codex  only the Codex step: its MCP server, the skills in ~/.agents/skills and metacomputer's
#                              rules for its sandbox (no tools, Lume, settings, vault or VM)
#   ./install.sh --uninstall   remove the Claude Code and Codex hooks this installed (the VM stays)
#   ./install.sh --switch      the metacomputer command runs another checkout: move it (and the Claude Code
#                              plugin) to this one. Without a terminal, or with --yes, that's refused otherwise
#   ./install.sh --allow-downgrade   install this checkout though it's older than what's installed
#
# Updating: `metacomputer update` (or the install line again). Run by hand after `git pull`, this ends the
# same way: the hub and event watcher on the new code, a VM on an earlier gateway restarted (it asks at a
# terminal), what's left to do, and what a VM rebuild would add (lib/update.py).
#
# What it does (each step is skipped when already done, so running it again is safe and
# carries on where an interrupted run stopped):
#   1. checks the Mac (disk, network, Command Line Tools) and says what it will do
#   2. installs missing tools with Homebrew: git curl go gpg ffmpeg python3
#   3. installs Lume, the VM runner: mcomp-vm (default; metacomputer's build of Lume
#      from the pinned tag, can leave out clipboard/audio/Rosetta sharing) or Cua's release
#   4. writes this Mac's settings (config/local.env, or the file MCOMP_CONFIG names)
#   5. links the `metacomputer` command and adds this checkout to Claude Code as the
#      `metacomputer` plugin (its skills and MCP server, for every project; tools:
#      mcp__plugin_metacomputer_metacomputer__*). Without `claude plugin`, or with
#      --no-plugin: the `metacomputer` skill link and a user-scope MCP server instead.
#      With Codex (see --codex): its MCP server metacomputer (tools mcp__metacomputer__*), the
#      skills in ~/.agents/skills and $CODEX_HOME/rules/metacomputer.rules
#   6. builds the secrets vault, mcomp-vault (`metacomputer secrets`), with swiftc
#   7. builds the VM: about 1.5 GB of downloads, then a few minutes unattended
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
YES=0; CHROME=""; BUILD=""; CLAUDE=1; PLUGIN=1; ONLY_CLAUDE=0; UNINSTALL=0; LUME_CHOICE=""; REPLACE_VM=0; FRESH_SOURCES=0
SWITCH=0; ALLOW_DOWNGRADE=0; CODEX=auto; ONLY_CODEX=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;; --no-chrome) CHROME=no ;; --no-build) BUILD=no ;; --replace-vm) REPLACE_VM=1 ;; --fresh-sources) FRESH_SOURCES=1 ;;
    --no-claude) CLAUDE=0 ;; --no-plugin) PLUGIN=0 ;; --only-claude) ONLY_CLAUDE=1 ;; --uninstall) UNINSTALL=1 ;;
    --switch) SWITCH=1 ;; --allow-downgrade) ALLOW_DOWNGRADE=1 ;;
    --codex) CODEX=yes ;; --no-codex) CODEX=no ;; --only-codex) ONLY_CODEX=1 ;;
    --lume=mcomp-vm|--lume=standard) LUME_CHOICE="${a#--lume=}" ;;
    --lume=secure) LUME_CHOICE=mcomp-vm ;;   # mcomp-vm's former name, lume-secure
    -h|--help) sed -n '2,/^set -Eeuo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
  esac
done
[ "$ONLY_CLAUDE" = 0 ] || [ "$CLAUDE" = 1 ] || { echo "--only-claude and --no-claude together leave nothing to do" >&2; exit 2; }
[ "$ONLY_CODEX" = 0 ] || [ "$CODEX" != no ] || { echo "--only-codex and --no-codex together leave nothing to do" >&2; exit 2; }
[ "$ONLY_CODEX" = 0 ] || [ "$ONLY_CLAUDE" = 0 ] || { echo "--only-codex and --only-claude: run them one at a time" >&2; exit 2; }

b=$'\033[1m'; d=$'\033[2m'; g=$'\033[32m'; y=$'\033[33m'; r=$'\033[31m'; o=$'\033[0m'
[ -t 1 ] || { b=; d=; g=; y=; r=; o=; }
# Stages print "==> [2/7] Title  (estimate)" and how long the one before took; STAGES=0: unnumbered.
STAGE=0; STAGES=0; STAGE_T0=$SECONDS; STAGE_NAME=""; STAGE_TIMES=(); FAIL_HINT=()
dur() { local s=$1; if [ "$s" -ge 60 ]; then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
stage_done() {
  [ -n "$STAGE_NAME" ] || return 0
  local t=$((SECONDS - STAGE_T0)); STAGE_TIMES+=("$STAGE_NAME|$t")
  [ "$t" -lt 5 ] || printf '%s\n' "  ${d}(took $(dur "$t"))${o}"
  STAGE_NAME=""
}
step() { # TITLE [ESTIMATE]
  stage_done
  if [ "$STAGES" -gt 0 ]; then STAGE=$((STAGE + 1)); printf '\n%s%s\n' "${b}==> [$STAGE/$STAGES] $1${o}" "${2:+  ${d}($2)${o}}"
  else printf '\n%s\n' "${b}==> $1${o}"; fi
  STAGE_NAME="$1"; STAGE_T0=$SECONDS; FAIL_HINT=()
}
ok()   { printf '%s\n' "  ${g}✓${o} $*"; }
warn() { printf '%s\n' "  ${y}!${o} $*"; }
die()  { local l; printf '%s\n' "${r}install: $1${o}" >&2; shift; for l in "$@"; do printf '%s\n' "  $l" >&2; done; exit 1; }
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
  case "$1" in */bin/vc|*/bin/cua-lab|*/bin/virtual-computer|*/skills/virtual-computer) return 0 ;;
    */bin/metacomputer|*/skills/metacomputer) return 0 ;; esac
  return 1
}
# --- Claude Code: the plugin, or (before plugins, --no-plugin) a skill link and a user-scope MCP server
MKT=metacomputer; PLUGIN_ID=metacomputer@metacomputer; TAB=$'\t'
has_claude() { command -v claude >/dev/null 2>&1; }
has_plugin_cli() { has_claude && claude plugin --help >/dev/null 2>&1; }
# The marketplace named metacomputer as "SOURCE<TAB>WHERE" ("directory<TAB>/path", "github<TAB>owner/repo"), or nothing.
mkt_info() {
  claude plugin marketplace list --json 2>/dev/null | python3 -c 'import json,sys
for m in json.load(sys.stdin):
    if m.get("name") == "metacomputer":
        print(m.get("source", ""), m.get("path") or m.get("repo") or m.get("url") or "", sep="\t")' 2>/dev/null || true
}
# The plugin metacomputer@metacomputer at user scope: "on", "off" (installed, turned off) or nothing.
plugin_state() {
  claude plugin list --json 2>/dev/null | python3 -c 'import json,sys
for p in json.load(sys.stdin):
    if p.get("id") == "metacomputer@metacomputer" and p.get("scope") == "user":
        print("on" if p.get("enabled") else "off")' 2>/dev/null || true
}
mcp_server_cmd() { # NAME: the command of the MCP server NAME that `claude mcp get` shows, or nothing
  claude mcp get "$1" 2>/dev/null | sed -n 's/^ *Command: //p' | head -1 || true
}
# Earlier installs' skill link and user-scope MCP server, removed only when they are this checkout's
# (or a moved checkout's that no longer exists), so the tools aren't listed twice.
remove_skill_link_and_server() {
  if [ -L "$SKILL_LINK" ] && old_target "$(readlink "$SKILL_LINK")"; then
    rm "$SKILL_LINK" && ok "removed the skill link $SKILL_LINK (the plugin brings the skill)"
  fi
  local cmd; cmd="$(mcp_server_cmd metacomputer)"
  if [ -n "$cmd" ] && old_target "$cmd"; then
    claude mcp remove metacomputer -s user >/dev/null 2>&1 \
      && ok "removed the user-scope MCP server metacomputer (the plugin brings it; tools are now mcp__plugin_metacomputer_metacomputer__*)"
  fi
  return 0
}
# The project's own repository: a marketplace named metacomputer from anywhere else (another repository, a
# URL, a fork) could hand Claude Code plugin code that runs on this Mac, so it isn't used.
CANON_REPO=metavert-io/metacomputer
# Is "SOURCE<TAB>WHERE" the canonical GitHub repository? (github owner/repo, or its git URL)
mkt_is_canonical() {
  local src="${1%%"$TAB"*}" w gh=github.com
  w="$(printf '%s' "${1#*"$TAB"}" | tr '[:upper:]' '[:lower:]')"; w="${w%/}"; w="${w%.git}"
  case "$src" in
    github) [ "$w" = "$CANON_REPO" ] ;;
    git) case "$w" in "https://$gh/$CANON_REPO"|"git@$gh:$CANON_REPO"|"ssh://git@$gh/$CANON_REPO") return 0 ;; esac
         return 1 ;;
    *) return 1 ;;
  esac
}
# Is the installed plugin this checkout's? From its marketplace, or from the canonical GitHub repository (that
# copy runs this checkout through ~/.local/bin/metacomputer), but not from another checkout or any other source.
plugin_is_ours() {
  local mkt; mkt="$(mkt_info)"
  [ -z "$mkt" ] || [ "$mkt" = "directory${TAB}$ROOT" ] || mkt_is_canonical "$mkt"
}
remove_plugin() { # uninstall the plugin; remove the marketplace when it is this checkout
  local mkt; mkt="$(mkt_info)"
  if [ -n "$(plugin_state)" ]; then
    claude plugin uninstall "$PLUGIN_ID" --scope user >/dev/null 2>&1 && ok "uninstalled the plugin $PLUGIN_ID"
  fi
  if [ "$mkt" = "directory${TAB}$ROOT" ]; then
    claude plugin marketplace remove "$MKT" >/dev/null 2>&1 && ok "removed the marketplace $MKT (this checkout)"
  elif [ -n "$mkt" ]; then
    echo "  The marketplace $MKT (${mkt//$TAB/ }) stays; to remove it: claude plugin marketplace remove $MKT"
  fi
  return 0
}
# What to do when something fails: the stage's own advice (FAIL_HINT, set by each stage), then
# the two causes behind most failures, a full disk and no network, when they're the case now.
on_error() { # LINE COMMAND
  local l free
  trap - ERR
  printf '\n%s\n' "${r}install: stopped${STAGE_NAME:+ in \"$STAGE_NAME\"} (line $1: $2)${o}" >&2
  for l in ${FAIL_HINT[@]+"${FAIL_HINT[@]}"}; do printf '%s\n' "  $l" >&2; done
  free="$(df -k "$HOME" 2>/dev/null | awk 'NR==2{print int($4 / 1048576)}' || true)"
  if [ -n "$free" ] && [ "$free" -lt 5 ]; then
    printf '%s\n' "  The disk is nearly full ($free GB free): free some space (the install uses about 6 GB)." >&2
  fi
  if ! curl -fsS -m 10 -o /dev/null https://github.com 2>/dev/null; then
    printf '%s\n' "  This Mac can't reach github.com right now: check the network (or a proxy or firewall)." >&2
  fi
  printf '%s\n' "  Fix it and run ./install.sh again: finished steps are skipped. docs/install.md has a troubleshooting table." >&2
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

CONF="${MCOMP_CONFIG:-$ROOT/config/local.env}"   # this Mac's settings (MCOMP_CONFIG: another file, as scripts/lib.sh reads it)
# install.sh sets up config/local.env's VM; a VM chosen by name in the environment would send its steps elsewhere.
[ -z "${MCOMP_VM:-}" ] || die "MCOMP_VM is set ($MCOMP_VM): install.sh sets up this Mac's main VM (config/local.env). Unset MCOMP_VM and run it again; a second VM is: metacomputer vm create NAME"

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
install_lock "install.sh in $ROOT"
SKILL_LINK="$HOME/.claude/skills/metacomputer"
CMD_LINK="$HOME/.local/bin/metacomputer"
ALIAS_LINK="$HOME/.local/bin/mcomp"   # short alias for typing

# --- Codex (docs/codex.md): its MCP server, the skills in ~/.agents/skills, and rules for its sandbox -------------
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_RULES="$CODEX_DIR/rules/metacomputer.rules"
CODEX_MARK="# metacomputer's rules for Codex. install.sh writes this file from config/codex/metacomputer.rules.in, and rewrites or"
CODEX_OWNER="# metacomputer checkout: "     # the rules file's 4th line names the checkout that wrote it
AGENT_SKILLS="$HOME/.agents/skills"
CODEX_TOOL_TIMEOUT=600     # seconds: Codex's default (60) is shorter than a long run_steps or a slowly typed text
# Codex asks before every MCP tool not marked read-only (in `codex exec`, it refuses them). metacomputer's tools act only
# on the isolated VM, through the hub's guards, so its server's tools are approved (docs/codex.md).
CODEX_APPROVAL=approve
CPY=""                     # Python 3.11 or later (tomllib) for the Codex step: codex_python
has_codex() { command -v codex >/dev/null 2>&1; }
codex_python() { # found as install.sh's py311_ok finds it (Homebrew's, then python3 on PATH; never Apple's 3.9)
  local p
  [ -z "$CPY" ] || return 0
  for p in "${MCOMP_BREW_PYTHON:-/opt/homebrew/bin/python3}" "$(command -v python3 2>/dev/null)"; do
    [ -n "$p" ] && [ -x "$p" ] && [ "$p" != /usr/bin/python3 ] || continue
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { CPY="$p"; return 0; }
  done
  return 1
}
codex_server() { # Codex's MCP server named metacomputer: "absent", "stdio<TAB>COMMAND", "other" (not a local
  # command, e.g. a URL) or "error" (the lookup failed: never read as absent)
  local out rc=0 err
  codex_python || { echo error; return 0; }
  err="$(mktemp)" || { echo error; return 0; }
  out="$(codex mcp get metacomputer --json 2>"$err")" || rc=$?
  if [ "$rc" != 0 ]; then
    if grep -q "No MCP server named 'metacomputer'" "$err" 2>/dev/null; then echo absent; else echo error; fi
    rm -f "${err:?}"; return 0
  fi
  rm -f "${err:?}"
  printf '%s' "$out" | "$CPY" -c 'import json, sys
try:
    t = json.load(sys.stdin).get("transport") or {}
except Exception:
    print("error"); sys.exit(0)
cmd = t.get("command")
print("stdio\t" + cmd if t.get("type") == "stdio" and isinstance(cmd, str) and cmd and "\n" not in cmd else "other")' \
    2>/dev/null || echo error
}
codex_server_cmd() { local s; s="$(codex_server)"; case "$s" in stdio"$TAB"*) printf '%s\n' "${s#*"$TAB"}" ;; esac; }
codex_rules_ours() { # rules this checkout wrote (or a moved checkout that's gone)
  [ -f "$CODEX_RULES" ] && [ "$(head -1 "$CODEX_RULES" 2>/dev/null)" = "$CODEX_MARK" ] || return 1
  local owner; owner="$(sed -n '4p' "$CODEX_RULES" 2>/dev/null)"
  case "$owner" in "$CODEX_OWNER"*) owner="${owner#"$CODEX_OWNER"}" ;; *) return 1 ;; esac
  [ "$owner" = "$ROOT" ] || { [ -n "$owner" ] && old_target "$owner/bin/metacomputer"; }
}
codex_set_up() { # anything of this checkout's in Codex already (a later run refreshes it)
  local c; c="$(codex_server_cmd)"
  { [ -n "$c" ] && old_target "$c"; } || codex_rules_ours \
    || link_points_here "$AGENT_SKILLS/metacomputer" "$ROOT/skills/metacomputer"
}
codex_wanted() { # --codex, or Codex installed and either a first install or one that has set Codex up before
  case "$CODEX" in yes) return 0 ;; no) return 1 ;; esac
  has_codex || return 1
  [ -f "$HOME/.local/share/metacomputer/installed.json" ] || return 0
  codex_set_up
}
codex_config_edit() { # MODE: "settings" adds the timeout and approval mode where they're missing; "command" points
  # an existing registration at this checkout. Only metacomputer's own table changes; everything you set stays.
  local cfg="$CODEX_DIR/config.toml" out rc=0
  [ -f "$cfg" ] || { echo missing; return 0; }
  out="$("$CPY" - "$cfg" "$1" "$CODEX_TOOL_TIMEOUT" "$CODEX_APPROVAL" "$ROOT/bin/metacomputer" <<'PY'
import json, os, sys, tempfile, tomllib
path, mode, want_t, want_a, cmd = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
text = open(path, encoding="utf-8").read()
try:
    srv = tomllib.loads(text).get("mcp_servers", {}).get("metacomputer")
except tomllib.TOMLDecodeError:
    sys.exit(2)
if not isinstance(srv, dict):
    sys.exit(2)
lines = text.split("\n")
heads = [i for i, l in enumerate(lines) if l.strip() == "[mcp_servers.metacomputer]"]
if len(heads) != 1:
    sys.exit(2)
start = heads[0] + 1
end = next((i for i in range(start, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
notes = []
if mode == "command":
    cmds = [i for i in range(start, end) if lines[i].split("=", 1)[0].strip() == "command"]
    if len(cmds) != 1:
        sys.exit(2)
    lines[cmds[0]] = "command = " + json.dumps(cmd, ensure_ascii=False)
else:
    add = []
    if "tool_timeout_sec" not in srv:
        add.append(f"tool_timeout_sec = {want_t}")
    elif not isinstance(srv["tool_timeout_sec"], (int, float)) or srv["tool_timeout_sec"] < want_t:
        notes.append("timeout-short")
    if "default_tools_approval_mode" not in srv:
        add.append(f'default_tools_approval_mode = "{want_a}"')
    elif srv["default_tools_approval_mode"] != want_a:
        notes.append("approval-yours")
    lines[start:start] = add
new = "\n".join(lines)
if new != text:
    got = tomllib.loads(new).get("mcp_servers", {}).get("metacomputer") or {}
    keep = {k: v for k, v in srv.items() if k != "command"}
    if any(got.get(k) != v for k, v in keep.items()) or (mode == "command" and got.get("command") != cmd):
        sys.exit(2)                       # only what was meant to change changed, or nothing is written
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.toml.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
print(" ".join(notes) or "ok")
PY
)" || rc=$?
  [ "$rc" = 0 ] && printf '%s\n' "$out" || echo failed
}
codex_server_settings() {
  local out; out="$(codex_config_edit settings)"
  case "$out" in
    failed|missing)
      warn "couldn't set tool_timeout_sec = $CODEX_TOOL_TIMEOUT and default_tools_approval_mode = \"$CODEX_APPROVAL\" for metacomputer in $CODEX_DIR/config.toml (add them under [mcp_servers.metacomputer]: docs/codex.md)"
      return 0 ;;
  esac
  case " $out " in *" timeout-short "*) warn "your tool_timeout_sec for metacomputer is shorter than $CODEX_TOOL_TIMEOUT s; left as you set it (long steps may time out)" ;;
    *) ok "tool calls of the metacomputer server may take up to $CODEX_TOOL_TIMEOUT s" ;; esac
  case " $out " in *" approval-yours "*) ok "its tools' approval: left as you set it (default_tools_approval_mode)" ;;
    *) ok "its tools run without asking each time (they act only on the virtual computer, through the hub's guards)" ;; esac
}
codex_rules_write() { # metacomputer.rules from the template, naming every way the command is spelled
  local cmds tmp
  case "$ROOT" in *$'\n'*) return 1 ;; esac
  cmds="$("$CPY" -c 'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' metacomputer mcomp "$CMD_LINK" "$ALIAS_LINK" "$ROOT/bin/metacomputer")" || return 1
  mkdir -p "$CODEX_DIR/rules"
  tmp="$(mktemp "$CODEX_DIR/rules/.metacomputer.rules.XXXXXX")" || return 1
  if ! "$CPY" - "$ROOT/config/codex/metacomputer.rules.in" "$cmds" "$ROOT" > "$tmp" <<'PY'
import sys
t = open(sys.argv[1], encoding="utf-8").read()
assert t.count("@COMMANDS@") == 1 and t.count("@CHECKOUT@") == 1
sys.stdout.write(t.replace("@COMMANDS@", sys.argv[2]).replace("@CHECKOUT@", sys.argv[3]))
PY
  then
    rm -f "${tmp:?}"; return 1
  fi
  # Codex's own checker must read it and allow a plain command; a file it can't use would break every rule it loads.
  if ! codex execpolicy check -r "$tmp" metacomputer status 2>/dev/null | grep -q '"decision": *"allow"'; then
    rm -f "${tmp:?}"; return 1
  fi
  chmod 644 "$tmp" && mv "$tmp" "$CODEX_RULES"
}
codex_skill_target() { # NAME: what ~/.agents/skills/NAME links to
  if [ "$1" = metacomputer ]; then printf '%s\n' "$ROOT/skills/metacomputer"; else printf '%s\n' "$ROOT/.claude/skills/cua-driver"; fi
}
codex_step() {
  printf '%s\n' "  Codex:"
  if ! has_codex; then
    warn "Codex's 'codex' command isn't installed; after installing it, run: ./install.sh --only-codex"
    return 0
  fi
  if ! codex_python; then
    warn "the Codex step needs Python 3.11 or later (brew install python), so Codex was left alone; then: ./install.sh --only-codex"
    return 0
  fi
  local srv cmd name target link
  srv="$(codex_server)"
  case "$srv" in
    error)
      warn "couldn't read Codex's MCP servers (codex mcp get metacomputer failed), so they were left alone; try ./install.sh --only-codex again" ;;
    other)
      warn "Codex already has an MCP server named metacomputer that isn't a local command (a URL?); left alone (codex mcp remove metacomputer, then ./install.sh --only-codex)" ;;
    absent)
      if codex mcp add metacomputer -- "$ROOT/bin/metacomputer" mcp-lazy >/dev/null 2>&1; then
        ok "MCP server metacomputer for every Codex session (tools: mcp__metacomputer__*, there even while the VM is off)"
        codex_server_settings
      else
        warn "could not add the MCP server to Codex; run: codex mcp add metacomputer -- $ROOT/bin/metacomputer mcp-lazy"
      fi ;;
    *)
      cmd="${srv#*"$TAB"}"
      if [ "$cmd" = "$ROOT/bin/metacomputer" ]; then
        ok "MCP server metacomputer already registered (tools: mcp__metacomputer__*)"
        codex_server_settings
      elif old_target "$cmd"; then
        # an earlier (moved) checkout's: only its command changes; anything you set on it stays
        if [ "$(codex_config_edit command)" != failed ] && [ "$(codex_server_cmd)" = "$ROOT/bin/metacomputer" ]; then
          ok "MCP server metacomputer now runs this checkout (your settings for it kept)"
          codex_server_settings
        else
          warn "couldn't point Codex's MCP server metacomputer at this checkout; run: codex mcp remove metacomputer, then ./install.sh --only-codex"
        fi
      else
        warn "Codex already has an MCP server named metacomputer that runs $cmd; left alone (codex mcp remove metacomputer, then ./install.sh --only-codex)"
      fi ;;
  esac
  mkdir -p "$AGENT_SKILLS"
  for name in metacomputer cua-driver; do
    target="$(codex_skill_target "$name")"
    link="$AGENT_SKILLS/$name"
    if link_points_here "$link" "$target"; then ok "skill $name (~/.agents/skills)"; continue; fi
    if [ -L "$link" ] && old_target "$(readlink "$link")"; then
      rm -f "${link:?}"
    elif [ -e "$link" ] || [ -L "$link" ]; then
      warn "$link is already there and isn't metacomputer's; left alone (Codex won't get the $name skill)"; continue
    fi
    ln -s "$target" "$link" && ok "skill $name (~/.agents/skills, read by Codex)"
  done
  if [ -e "$CODEX_RULES" ] && ! codex_rules_ours; then
    warn "$CODEX_RULES is there and isn't this checkout's; left alone (docs/codex.md says what it should hold)"
  elif codex_rules_write; then
    ok "rules: everyday metacomputer commands run outside Codex's sandbox; changes to the VM ask you; user-only ones are refused"
  else
    warn "couldn't write $CODEX_RULES (Codex's checker didn't accept it); approve metacomputer commands when Codex asks instead"
  fi
}
codex_remove() { # --uninstall: only what is this checkout's
  has_codex || return 0
  codex_python || { warn "Codex left alone: the uninstall's Codex step needs Python 3.11 or later"; return 0; }
  local cmd name link
  cmd="$(codex_server_cmd)"
  if [ -n "$cmd" ] && old_target "$cmd"; then
    codex mcp remove metacomputer >/dev/null 2>&1 && ok "removed Codex's metacomputer MCP server"
  fi
  for name in metacomputer cua-driver; do
    link="$AGENT_SKILLS/$name"
    if link_points_here "$link" "$(codex_skill_target "$name")"; then
      rm -f "${link:?}" && ok "removed the $name skill link for Codex"
    fi
  done
  if codex_rules_ours; then rm -f "${CODEX_RULES:?}" && ok "removed metacomputer's rules for Codex"; fi
  return 0
}

# --- uninstall ---------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  step "Removing the Claude Code and Codex hooks for $ROOT"
  for l in "$CMD_LINK" "$ALIAS_LINK"; do link_points_here "$l" "$ROOT/bin/metacomputer" && rm "$l" && ok "removed $l"; done
  link_points_here "$SKILL_LINK" "$ROOT/skills/metacomputer" && rm "$SKILL_LINK" && ok "removed the metacomputer skill"
  [ -f "$HOME/.local/share/metacomputer/bin/mcomp-vault" ] && rm "$HOME/.local/share/metacomputer/bin/mcomp-vault" \
    && ok "removed the secrets vault program (your vault file stays: ~/Library/Application Support/metacomputer)"
  if has_claude; then
    cmd="$(mcp_server_cmd metacomputer)"
    if [ -n "$cmd" ] && old_target "$cmd"; then
      claude mcp remove metacomputer -s user >/dev/null 2>&1 && ok "removed the metacomputer MCP server"
    fi
  fi
  # The plugin: from this checkout's marketplace, or from GitHub (its copy runs this checkout
  # through ~/.local/bin/metacomputer, removed above, so it can't work without it).
  if has_plugin_cli && plugin_is_ours; then remove_plugin; fi
  codex_remove
  # shellcheck source=/dev/null
  . "$ROOT/config/defaults.env"; [ -f "$CONF" ] && . "$CONF"
  # The runner this checkout uses (LUME in config/local.env; mcomp-vm by default), found as
  # scripts/lib.sh finds it: lume-secure, mcomp-vm's former name, while LUME's isn't installed.
  runner="${LUME:-$HOME/.local/bin/mcomp-vm}"
  command -v "$runner" >/dev/null 2>&1 || [ ! -x "$HOME/.local/bin/lume-secure" ] || runner="$HOME/.local/bin/lume-secure"
  name="$(basename "$runner")"
  echo "The VM is untouched. To delete it too, with the runner that made it ($runner):"
  echo "  $runner delete $VM_NAME && $runner delete $BASELINE_NAME"
  echo "  and any checkpoints ($VM_NAME-ckpt-*) and $BASELINE_NAME-prev that '$runner ls' lists"
  echo "  and its live-view password, if it's in the login Keychain: security delete-generic-password -s metacomputer-vnc -a $VM_NAME"
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

# --- the Claude Code step (4; --only-claude runs it alone) ------------------------------------
# Plugin mode: this checkout is the marketplace `metacomputer`, and its plugin (plugin/: links to
# skills/metacomputer, the driver pack and bin/metacomputer) loads in place from here.
plugin_install() { # 0 = installed and on, 2 = installed but turned off (left so), 1 = failed, 3 = refused
  local mkt; mkt="$(mkt_info)"
  case "$mkt" in
    "directory${TAB}$ROOT") ok "marketplace $MKT: this checkout (already added)" ;;
    directory*|"")
      if [ -n "$mkt" ]; then
        claude plugin marketplace remove "$MKT" >/dev/null 2>&1 || true
        ok "removed the marketplace $MKT, which was ${mkt#*"$TAB"}"
      fi
      claude plugin marketplace add "$ROOT" >/dev/null 2>&1 \
        || { warn "could not add this checkout as a marketplace (claude plugin marketplace add $ROOT)"; return 1; }
      ok "marketplace $MKT: this checkout" ;;
    *)
      if ! mkt_is_canonical "$mkt"; then
        warn "a marketplace named $MKT is already registered from ${mkt//$TAB/ }, which is neither this checkout nor"
        warn "github.com/$CANON_REPO, so its plugin is NOT installed: its code would run on this Mac. If it isn't"
        warn "yours, remove it (claude plugin marketplace remove $MKT) and run ./install.sh --only-claude again."
        return 3
      fi
      ok "marketplace $MKT: ${mkt//$TAB/ } (its plugin runs this checkout through $CMD_LINK)" ;;
  esac
  case "$(plugin_state)" in
    on) ok "plugin $PLUGIN_ID already installed" ;;
    off) warn "the plugin $PLUGIN_ID is installed but turned off; left so (to turn it on: claude plugin enable $PLUGIN_ID)"; return 2 ;;
    *) claude plugin install "$PLUGIN_ID" --scope user >/dev/null 2>&1 \
         || { warn "could not install the plugin (claude plugin install $PLUGIN_ID --scope user)"; return 1; }
       ok "plugin $PLUGIN_ID installed for every project: skills metacomputer and cua-driver, MCP server metacomputer (tools: mcp__plugin_metacomputer_metacomputer__*, there even while the VM is off)" ;;
  esac
}
skill_link_and_server() { # without the plugin: the skill link and a user-scope MCP server
  mkdir -p "$HOME/.claude/skills"
  if [ -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "$SKILL_LINK exists and isn't a link; left alone (move it away and run this again to install the skill)"
  else
    ln -sfn "$ROOT/skills/metacomputer" "$SKILL_LINK" && ok "skill: metacomputer"
  fi
  if ! has_claude; then
    warn "Claude Code's 'claude' command isn't installed; after installing it, run: ./install.sh --only-claude"
    return 0
  fi
  if [ "$(mcp_server_cmd metacomputer)" = "$ROOT/bin/metacomputer" ]; then
    ok "MCP server metacomputer already registered (tools: mcp__metacomputer__*)"
  else
    claude mcp remove metacomputer -s user >/dev/null 2>&1 || true
    if claude mcp add -s user metacomputer "$ROOT/bin/metacomputer" mcp-lazy >/dev/null 2>&1; then
      ok "MCP server metacomputer registered for every project (tools: mcp__metacomputer__*, there even while the VM is off)"
    else
      warn "could not register the MCP server; run: claude mcp add -s user metacomputer $ROOT/bin/metacomputer mcp-lazy"
    fi
  fi
}
claude_step() {
  step "Claude Code"
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$ROOT/bin/metacomputer" "$CMD_LINK" && ok "command: $CMD_LINK"
  if [ -e "$ALIAS_LINK" ] && ! link_points_here "$ALIAS_LINK" "$ROOT/bin/metacomputer"; then
    warn "$ALIAS_LINK already exists and is something else; left alone (the full name works)"
  else
    ln -sfn "$ROOT/bin/metacomputer" "$ALIAS_LINK" && ok "alias: mcomp"
  fi
  # Names used by earlier versions (commands vc, cua-lab, virtual-computer, vcomp; the
  # virtual-computer skill; MCP servers cua-linux, virtual-computer).
  local old cmd rc=0
  for old in "$HOME/.local/bin/vc" "$HOME/.local/bin/cua-lab" "$HOME/.local/bin/virtual-computer" \
             "$HOME/.local/bin/vcomp" "$HOME/.claude/skills/virtual-computer"; do
    if [ -L "$old" ] && old_target "$(readlink "$old")"; then
      rm "$old" && ok "removed the old name $old"
    fi
  done
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "~/.local/bin isn't on your PATH; add it to use 'metacomputer' directly (Claude uses the full path)" ;; esac
  if has_claude; then
    for old in cua-linux virtual-computer; do
      cmd="$(mcp_server_cmd "$old")"
      if [ -n "$cmd" ] && old_target "$cmd"; then
        claude mcp remove "$old" -s user >/dev/null 2>&1 && ok "removed the old MCP server name $old (now metacomputer)"
      fi
    done
  fi
  if [ "$PLUGIN" = 1 ] && has_plugin_cli; then
    plugin_install || rc=$?
    case "$rc" in
      0) remove_skill_link_and_server ;;
      2) ;;
      *) warn "using a skill link and a user-scope MCP server instead (as ./install.sh --no-plugin does)"; skill_link_and_server ;;
    esac
  else
    if [ "$PLUGIN" = 1 ] && has_claude; then
      warn "this Claude Code has no 'claude plugin' command (update Claude Code for the plugin); using a skill link and a user-scope MCP server"
    elif [ "$PLUGIN" = 0 ] && has_plugin_cli && [ -n "$(plugin_state)" ] && plugin_is_ours; then
      remove_plugin   # --no-plugin after the plugin: don't list the tools twice
    fi
    skill_link_and_server
  fi
}
tildify() { case "$1" in "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
# --- an install that's already here: another checkout's (switching needs a yes), or a newer one (downgrading needs --allow-downgrade)
resolve_link() { # PATH -> where it ends up, following links (relative ones from their own folder), at most 20 of them
  local p="$1" i=0 t
  while [ -L "$p" ] && [ $i -lt 20 ]; do
    t="$(readlink "$p")"; case "$t" in /*) p="$t" ;; *) p="$(dirname "$p")/$t" ;; esac; i=$((i + 1))
  done
  printf '%s\n' "$p"
}
real_dir() { (cd "$1" 2>/dev/null && pwd -P); }
stamp_field() { sed -n "s/^ *\"$1\": \"\\([^\"]*\\)\".*/\\1/p" "$HOME/.local/share/metacomputer/installed.json" 2>/dev/null | head -1 || true; }
existing_checks() { # main | claude: the plan's lines go into todo only for main
  local here other="" tgt od s_dir s_commit s_tag this_tag head
  here="$(real_dir "$ROOT")"
  if [ "$CLAUDE" = 1 ] && [ -L "$CMD_LINK" ]; then
    tgt="$(resolve_link "$CMD_LINK")"
    case "$tgt" in */bin/metacomputer)
      od="$(real_dir "$(dirname "$tgt")/..")"
      if [ -n "$od" ] && [ "$od" != "$here" ] && [ -f "$od/install.sh" ] && [ -f "$od/bin/metacomputer" ]; then other="$od"; fi ;; esac
  fi
  if [ -n "$other" ]; then
    if [ "$SWITCH" = 1 ]; then
      [ "$1" != main ] || todo+=("switch the metacomputer command and the Claude Code plugin from $(tildify "$other") to this checkout (--switch)")
    elif [ "$YES" = 1 ] || [ ! -t 0 ]; then
      die "the metacomputer command runs another checkout, $(tildify "$other"), and this run would switch it (and the Claude Code plugin) to $(tildify "$ROOT")." \
          "To update that install instead: metacomputer update." \
          "To move to this checkout: ./install.sh --switch (that checkout's VM, settings and vault are left as they are)."
    elif ask "The metacomputer command runs another checkout, $(tildify "$other"). Switch it and the Claude Code plugin to this one?" N; then
      [ "$1" != main ] || todo+=("switch the metacomputer command and the Claude Code plugin from $(tildify "$other") to this checkout")
    else
      echo "  Nothing was changed. To update the install that's there: metacomputer update."; exit 0
    fi
  fi
  # Older than what's installed from this checkout: by history when both commits are here, else by release tag.
  [ "$ALLOW_DOWNGRADE" = 0 ] && [ -f "$HOME/.local/share/metacomputer/installed.json" ] || return 0
  s_dir="$(stamp_field checkout)"; [ -n "$s_dir" ] && [ "$(real_dir "$s_dir")" = "$here" ] || return 0
  s_commit="$(stamp_field commit)"; head="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
  [ -n "$s_commit" ] && [ -n "$head" ] && [ "$s_commit" != "$head" ] || return 0
  if git -C "$ROOT" merge-base --is-ancestor "$s_commit" "$head" 2>/dev/null; then return 0; fi     # forward: an update
  if git -C "$ROOT" merge-base --is-ancestor "$head" "$s_commit" 2>/dev/null; then
    die "this checkout ($(git -C "$ROOT" describe --tags --always 2>/dev/null)) is older than what's installed from it ($(stamp_field version)): nothing was changed." \
        "To update: metacomputer update. To go back anyway: ./install.sh --allow-downgrade."
  fi
  s_tag="$(stamp_field tag)"; [ -n "$s_tag" ] || s_tag="$(stamp_field after)"
  this_tag="$(git -C "$ROOT" describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)"
  if [ -n "$s_tag" ] && [ -n "$this_tag" ] && [ "$s_tag" != "$this_tag" ] \
     && [ "$(printf '%s\n%s\n' "$s_tag" "$this_tag" | sort -V | tail -1)" = "$s_tag" ]; then
    die "this checkout ($this_tag) is older than what's installed from it ($s_tag): nothing was changed." \
        "To update: metacomputer update. To go back anyway: ./install.sh --allow-downgrade."
  fi
  return 0
}
if [ "$ONLY_CLAUDE" = 1 ]; then
  existing_checks claude
  claude_step
  printf '\n%s\n' "Restart open Claude Code sessions (or run /reload-plugins in them) to pick this up."
  exit 0
fi
if [ "$ONLY_CODEX" = 1 ]; then
  step "Codex"
  codex_step
  printf '\n%s\n' "Start a new Codex session to pick this up."
  exit 0
fi

# --- 1. this Mac, what's already done, and the plan --------------------------------------
STAGES=7; [ "$CLAUDE" = 1 ] || STAGES=6
step "Checking this Mac" "a few seconds"
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "needs macOS on Apple silicon (this is $(uname -s) $(uname -m))"
major="$(sw_vers -productVersion | cut -d. -f1)"
[ "$major" -ge 14 ] || die "needs macOS 14 or later (this is $(sw_vers -productVersion))" \
  "Update macOS (System Settings > General > Software Update), then run ./install.sh again."
ok "macOS $(sw_vers -productVersion) on Apple silicon"
# Free space, on the disks that will hold it (measured: the VM and its clean snapshot about 5 GB, which grow as
# the VM is used; the checkout's downloads and build caches about 1 GB): VM_FS_GB where Lume keeps VMs and
# CHECKOUT_FS_GB under this checkout; their sum when that's one disk.
VM_FS_GB=10; CHECKOUT_FS_GB=2
mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
ok "${mem_gb} GB of memory"

# shellcheck source=config/defaults.env
. "$ROOT/config/defaults.env"
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

# Apple's Command Line Tools, checked without running /usr/bin/git, swift or python3: without the
# tools, those are stand-ins that open Apple's install dialog.
tildify() { case "$1" in "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
clt_ok() { xcode-select -p >/dev/null 2>&1; }
tool_ok() {
  local p; p="$(command -v "$1" 2>/dev/null)" || return 1
  case "$p" in /usr/bin/git|/usr/bin/python3|/usr/bin/swift|/usr/bin/swiftc) clt_ok ;; *) return 0 ;; esac
}
# The filesystem holding PATH (or its nearest existing parent): "DEVICE FREE_GB".
fs_of() {
  local p="$1"
  while [ ! -e "$p" ] && [ "$p" != / ]; do p="$(dirname "$p")"; done
  df -k "$p" 2>/dev/null | awk 'NR==2 { print $1, int($4 / 1048576) }'
}
# This run built the VM: say so to get.sh, in the file it named in MCOMP_INSTALL_REPORT (a regular file of this
# user's, not a link). get.sh runs its self-test, which opens and closes windows, only on a VM built this run;
# a VM an install kept may hold the user's work and gets read-only checks.
report_vm_built() {
  local f="${MCOMP_INSTALL_REPORT:-}"
  [ -n "$f" ] && [ -f "$f" ] && [ ! -L "$f" ] && [ -O "$f" ] || return 0
  printf 'vm_built=1\n' >> "$f" 2>/dev/null || true
}
account_home() { # Lume's ~: the account's home folder, whatever $HOME says; status 1 if it can't be told
  local h; h="$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
  case "$h" in /*) printf '%s' "$h" ;; *) return 1 ;; esac
}
# Where Lume will create the VM: the default storage location of the runner that's installed (`config get`),
# or, before one is, the default location in Lume's settings file (~/.config/lume/config.yaml) if there is
# one, else Lume's own default, ~/.lume. Status 1 when any of that can't be told for certain: no guessing.
vm_storage_dir() {
  local lb out path="" h n
  h="$(account_home)" || return 1
  for lb in "$LUME_BIN" "$HOME/.local/bin/mcomp-vm" "$HOME/.local/bin/lume-secure" "$HOME/.local/bin/lume"; do
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
# Free space on every disk this run writes to: each "PATH GB WHAT" in SPACE_NEED, summed per disk (one disk
# holding several needs them all). Refuses, naming the disk's folders, when one falls short.
space_check() {
  local line path gb what dev free devs="" dv sum names
  local -a rows=()
  for line in "${SPACE_NEED[@]}"; do
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
    [ "$free" -ge "$sum" ] || die "only $free GB free on the disk that holds $names; this run needs $sum GB there." \
      "The VM and its clean snapshot take about 5 GB and grow as it's used; downloads, build caches and tools take a few GB more." \
      "Free some space there and run ./install.sh again, or set up without the VM for now: ./install.sh --no-build"
    ok "$free GB free on the disk with $names ($sum GB needed)"
  done
}
# git that also works for a private fork or mirror: when plain git can't reach it, retry with the
# GitHub CLI's credentials (gh auth login), if gh is installed and logged in.
git_net() {
  local gh
  GIT_TERMINAL_PROMPT=0 git "$@" && return 0
  gh="$(command -v gh)" && "$gh" auth status >/dev/null 2>&1 || return 1
  GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c "credential.helper=!'$gh' auth git-credential" "$@"
}
# The builds of mcomp-vm, the host tools and the vault print hundreds of lines (Swift's compiler warnings, Go's
# downloads) that are harmless and nothing to act on. quiet_build sends all of it, both streams, to a log in this
# install's logs folder (LOG_DIR, else logs/ in the checkout, where the VM build's logs go) and shows one line for it:
# then a ✓ naming the log, or, when the build fails, the log's last lines (escaped: lib/termsafe.py) and its path.
# It returns the command's exit status, so the caller's die or warn is what it was.
quiet_build() { # NAME WHAT FINISHED COMMAND [ARG...]: the log is build-NAME-TIME.log
  local name="$1" what="$2" finished="$3" dir="${LOG_DIR:-$ROOT/logs}" log rc=0; shift 3
  mkdir -p "$dir" 2>/dev/null || dir="${TMPDIR:-/tmp}"
  log="$dir/build-$name-$(date +%Y%m%d-%H%M%S).log"
  # a logs folder that exists but can't be written: the log goes to a temporary file, so the build still runs
  { : > "$log"; } 2>/dev/null || log="$(mktemp "${TMPDIR:-/tmp}/mcomp-build-$name.XXXXXX")" || log=/dev/null
  printf '%s\n' "  $what..."
  "$@" > "$log" 2>&1 < /dev/null || rc=$?
  if [ "$rc" = 0 ]; then ok "$finished ${d}(log: $(tildify "$log"))${o}"; return 0; fi
  { printf '%s\n' "  ${r}the build failed (exit $rc); the end of its log:${o}"
    tail -n 40 "$log" 2>/dev/null | term_escape | sed 's/^/    /'
    printf '%s\n' "  the whole log: $(tildify "$log")"; } >&2 || true
  return "$rc"
}
term_escape() { # stdin to stdout, every control character shown as an escape (lib/termsafe.py); without Python, dropped
  if [ -n "$PY311" ]; then "$PY311" "$ROOT/lib/termsafe.py"; else LC_ALL=C tr -d '\000-\010\013-\037\177'; fi
}
# Python 3.11 or later: Homebrew's, or python3 on PATH (Apple's /usr/bin/python3, 3.9, isn't enough; the
# same order scripts/lib.sh uses). An older one counts as missing, so Homebrew adds python. PY311: the one found.
PY311=""
py311_ok() {
  local p
  for p in "${MCOMP_BREW_PYTHON:-/opt/homebrew/bin/python3}" "$(command -v python3 2>/dev/null)"; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    case "$p" in /usr/bin/python3) clt_ok || continue ;; esac
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { PY311="$p"; return 0; }
  done
  return 1
}
need=(); pkgs=()
for t in git curl go gpg ffmpeg python3; do
  if [ "$t" = python3 ]; then py311_ok || need+=("$t"); else tool_ok "$t" || need+=("$t"); fi
done
for t in ${need[@]+"${need[@]}"}; do case "$t" in gpg) pkgs+=(gnupg) ;; python3) pkgs+=(python) ;; *) pkgs+=("$t") ;; esac; done
if [ -z "$LUME_CHOICE" ] && [ -f "$CONF" ] && grep -q '^LUME=' "$CONF"; then
  case "$(grep '^LUME=' "$CONF" | tail -1)" in
    *mcomp-vm*) LUME_CHOICE=mcomp-vm; LUME_KEPT="mcomp-vm" ;;
    *lume-secure*) LUME_CHOICE=mcomp-vm; LUME_KEPT="mcomp-vm (formerly lume-secure)" ;;
    *) LUME_CHOICE=standard; LUME_KEPT=standard ;;
  esac
fi
LUME_CHOICE="${LUME_CHOICE:-mcomp-vm}"
if [ "$LUME_CHOICE" = mcomp-vm ]; then
  LUME_BIN="$HOME/.local/bin/mcomp-vm"
  # Current when the installed build is the pinned release (BUILD_INFO's version= line, written since
  # 0.7.2-mcomp.2); an older build, or one without that line, is rebuilt from the pin.
  lume_ready() { [ -x "$LUME_BIN" ] && grep -qsx "version=${MCOMP_VM_TAG#v}" "$HOME/.local/share/mcomp-vm/BUILD_INFO" \
                 && "$LUME_BIN" run --help 2>&1 | grep -q -- "--no-host-sharing"; }
else
  LUME_BIN="$HOME/.local/bin/lume"
  lume_ready() { [ -x "$LUME_BIN" ] && "$LUME_BIN" --version 2>/dev/null | grep -q "$LUME_VERSION"; }
fi
lume_have=0; lume_ready && lume_have=1
# The VM, through the first runner that's installed (they all see the same VMs). A listing that fails is
# "unknown", never "none". A VM without its clean snapshot is an interrupted build only when this checkout's
# marker says a build of it was under way (scripts/build-vm.sh writes it at the start and removes it at the
# end); otherwise it may hold work. Neither is ever replaced without --replace-vm (--yes doesn't imply it).
# States: none | built | interrupted | nobaseline | snapshotonly (the baseline, no VM) | unknown
VM_MARKER="${RUN_DIR:-$ROOT/.run}/build-unfinished-$VM_NAME"
vm_lookup() { # RUNNER -> the state of VM_NAME and BASELINE_NAME
  local out
  tool_ok python3 || { echo unknown; return 0; }
  out="$("$1" ls --format json 2>/dev/null)" || { echo unknown; return 0; }
  printf '%s' "$out" | python3 -c 'import json, sys
try:
    names = {v.get("name") for v in json.load(sys.stdin)}
except Exception:
    print("unknown"); sys.exit(0)
vm, base = sys.argv[1] in names, sys.argv[2] in names
print("built" if vm and base else "snapshotonly" if base else "vmonly" if vm else "none")' "$VM_NAME" "$BASELINE_NAME" 2>/dev/null \
    || echo unknown
}
vm_state=none; vm_runner=""
for lb in "$LUME_BIN" "$HOME/.local/bin/mcomp-vm" "$HOME/.local/bin/lume-secure" "$HOME/.local/bin/lume"; do
  [ -x "$lb" ] || continue
  vm_runner="$lb"; vm_state="$(vm_lookup "$lb")"
  break
done
if [ "$vm_state" = vmonly ]; then
  if [ -f "$VM_MARKER" ]; then vm_state=interrupted; else vm_state=nobaseline; fi
fi
vault_ok=0
if [ "$major" -ge 15 ] && clt_ok && tool_ok swiftc; then vault_ok=1; fi
# The host tools (mcomp-netgw, qcow2raw): built on every install that finds them missing, out of date or not
# the binaries that were built (scripts/build-tools.sh --check), whether or not the VM is built.
tools_ok=0
[ "$FRESH_SOURCES" = 0 ] && "$ROOT/scripts/build-tools.sh" --check >/dev/null 2>&1 && tools_ok=1
build_vm=0; vm_replace=0
if [ "$BUILD" != no ]; then
  case "$vm_state" in
    none) build_vm=1; [ "$REPLACE_VM" = 0 ] || vm_replace=1 ;;
    unknown) ;;
    *) [ "$REPLACE_VM" = 0 ] || { build_vm=1; vm_replace=1; } ;;
  esac
fi

# Refusals come first, before anything changes.
if [ ${#need[@]} -gt 0 ] && ! command -v brew >/dev/null 2>&1; then
  die "missing: ${need[*]}, and Homebrew isn't installed to add them." \
      "Install Homebrew (https://brew.sh), or the tools another way, then run ./install.sh again." \
      "Or run get.sh, which offers to install Homebrew for you (docs/install.md)."
fi
if [ "$LUME_CHOICE" = mcomp-vm ] && [ "$lume_have" = 0 ] && ! { clt_ok && tool_ok swift; }; then
  die "mcomp-vm, the VM runner, is built with Swift, and Apple's Command Line Tools aren't installed." \
      "Install them (xcode-select --install opens Apple's dialog: click Install), then run ./install.sh again." \
      "Or use Cua's notarized Lume, which shares your clipboard, audio and Rosetta with the VM: ./install.sh --lume=standard"
fi
if [ "$vm_state" = unknown ] && [ "$BUILD" != no ]; then
  die "couldn't list the VMs with $vm_runner, so it can't tell whether $VM_NAME exists; nothing was changed." \
      "Check that '$vm_runner ls' works, then run ./install.sh again (or set up without the VM for now: --no-build)."
fi
SPACE_NEED=()
if [ "$build_vm" = 1 ]; then
  vm_store="$(vm_storage_dir)" || die "couldn't tell where Lume will keep the VM (its settings didn't say, or couldn't be read); nothing was changed." \
    "Check '$([ -n "$vm_runner" ] && echo "$vm_runner" || echo mcomp-vm) config get' (or ~/.config/lume/config.yaml), then run ./install.sh again, or use --no-build for now."
  SPACE_NEED+=("$vm_store|$VM_FS_GB|the VM, where Lume keeps VMs" "$ROOT|$CHECKOUT_FS_GB|downloads, in this checkout")
elif [ "$lume_have" = 0 ] || [ "$tools_ok" = 0 ]; then
  SPACE_NEED+=("$ROOT|1|sources and tools built in this checkout")
fi
if [ "$build_vm" = 1 ] || [ "$lume_have" = 0 ] || [ "$vault_ok" = 1 ] || [ "$tools_ok" = 0 ]; then
  SPACE_NEED+=("$HOME|1|build caches and tools, in your home folder" "${TMPDIR:-/tmp}|1|temporary files")
  [ "$lume_have" = 1 ] || SPACE_NEED+=("/tmp|1|mcomp-vm's build")
fi
if [ ${#need[@]} -gt 0 ]; then SPACE_NEED+=("$(brew --prefix 2>/dev/null || echo /opt/homebrew)|2|Homebrew's packages"); fi
[ ${#SPACE_NEED[@]} = 0 ] || space_check
if [ ${#need[@]} -gt 0 ] || [ "$lume_have" = 0 ] || [ "$build_vm" = 1 ] || [ "$tools_ok" = 0 ]; then
  curl -fsS -m 15 -o /dev/null https://github.com 2>/dev/null || die "can't reach github.com, and this run downloads from it." \
    "Check this Mac's internet connection (and any proxy or firewall), then run ./install.sh again."
  if [ "$build_vm" = 1 ]; then
    curl -fsS -m 15 -o /dev/null "$UBUNTU_BASE_URL/SHA256SUMS" 2>/dev/null || die "can't reach Ubuntu's image server (${UBUNTU_BASE_URL%%/releases*})." \
      "The VM is built from Ubuntu's official cloud image. Check the network, then run ./install.sh again (or use --no-build for now)."
  fi
  ok "network: the download sites are reachable"
fi

# The plan: one question (Enter = yes), none with --yes or without a terminal.
chrome_in=1
if [ -f "$CONF" ]; then case " $BROWSERS " in *" chrome "*) ;; *) chrome_in=0 ;; esac
elif [ "$CHROME" = no ]; then chrome_in=0; fi
todo=(); done_=()
[ ${#need[@]} = 0 ] && done_+=("tools") || todo+=("install with Homebrew: ${pkgs[*]}  ${d}(a few minutes; ffmpeg brings many libraries)${o}")
[ "$tools_ok" = 1 ] && done_+=("host tools") || todo+=("build the host tools (mcomp-netgw, qcow2raw) from their pinned sources  ${d}(under a minute)${o}")
if [ "$lume_have" = 1 ]; then done_+=("$(basename "$LUME_BIN")")
elif [ "$LUME_CHOICE" = mcomp-vm ]; then todo+=("build mcomp-vm, the VM runner, from source  ${d}(about 1 minute; --lume=standard: Cua's release instead)${o}")
else todo+=("download Cua's standard Lume, the VM runner (it always shares your clipboard, audio and Rosetta with the VM)"); fi
if [ -f "$CONF" ]; then done_+=("settings")
else todo+=("write this Mac's settings to $(tildify "$CONF"): Firefox$([ "$chrome_in" = 1 ] && echo " and Google Chrome") in the VM$([ "$chrome_in" = 1 ] && echo " (Chrome means accepting Google's terms: https://www.google.com/chrome/terms/; --no-chrome leaves it out)")"); fi
[ "$CLAUDE" = 1 ] && todo+=("link the metacomputer command and add the Claude Code plugin (skills and tools in every project)")
if codex_wanted; then
  todo+=("add metacomputer to Codex: its MCP server, the skills in ~/.agents/skills, and rules that let metacomputer commands run outside Codex's sandbox (--no-codex leaves Codex alone)")
fi
[ "$vault_ok" = 1 ] && todo+=("build the secrets vault (metacomputer secrets)  ${d}(about 1 minute; skipped if it's up to date)${o}")
case "$vm_state:$build_vm:$vm_replace" in
  built:0:*) done_+=("the VM $VM_NAME") ;;
  none:1:*) todo+=("build the VM $VM_NAME: about 1.5 GB of downloads, then a few minutes unattended") ;;
  *:1:1) todo+=("${b}delete${o} the VM $VM_NAME and its clean snapshot (--replace-vm), then build them again: about 1.5 GB of downloads, then a few minutes") ;;
  interrupted:0:*) todo+=("leave the VM $VM_NAME alone: an earlier build of it didn't finish (--replace-vm rebuilds it)") ;;
  nobaseline:0:*) todo+=("leave the VM $VM_NAME alone: it has no clean snapshot and may hold your work (see the last step)") ;;
  snapshotonly:0:*) todo+=("leave the VM alone: $VM_NAME is missing, its clean snapshot is there (metacomputer reset restores it)") ;;
  unknown:*) todo+=("leave the VM alone (Lume couldn't list the VMs)") ;;
  *) todo+=("not build the VM yet (--no-build; later: metacomputer build)") ;;
esac
existing_checks main
echo "  This run will:"
for t in "${todo[@]}"; do printf '%s\n' "    - $t"; done
[ ${#done_[@]} = 0 ] || printf '%s\n' "  Already done: $(IFS=,; echo "${done_[*]}" | sed 's/,/, /g')."
if [ ${#need[@]} -gt 0 ] || [ "$lume_have" = 0 ] || [ "$build_vm" = 1 ]; then
  ask "Go ahead?" Y || { echo "  Nothing was changed. Options: --no-chrome, --no-build, --lume=standard (./install.sh --help)."; exit 0; }
fi

# --- 2. tools -------------------------------------------------------------------------
step "Tools: git curl go gpg ffmpeg python3" "$([ ${#need[@]} = 0 ] && echo "already here" || echo "a few minutes")"
if [ ${#need[@]} -gt 0 ]; then
  brew install "${pkgs[@]}" || die "Homebrew couldn't install ${pkgs[*]} (its messages are above)." \
    "Try: brew update, then brew doctor, and fix what it names; then run ./install.sh again." \
    "Or install those tools another way (they just need to be on PATH) and run ./install.sh again."
  for t in "${need[@]}"; do tool_ok "$t" || die "$t still isn't on PATH after brew install" \
    "Open a new Terminal (Homebrew's PATH), check 'brew doctor', then run ./install.sh again."; done
  py311_ok || die "Python 3.11 or later still isn't there after brew install python" \
    "Check 'brew doctor' and that ${MCOMP_BREW_PYTHON:-/opt/homebrew/bin/python3} exists, then run ./install.sh again."
fi
ok "tools: git curl go gpg ffmpeg python3"
if [ "$tools_ok" = 1 ]; then
  ok "host tools (mcomp-netgw, qcow2raw): up to date"
else
  FAIL_HINT=("The host tools didn't build (scripts/build-tools.sh). They need Go and github.com; run ./install.sh again.")
  what="building the host tools (mcomp-netgw, qcow2raw) with Go"; finished="host tools (mcomp-netgw, qcow2raw): built"
  if [ "$FRESH_SOURCES" = 1 ]; then quiet_build tools "$what" "$finished" "$ROOT/scripts/build-tools.sh" --rebuild --fresh-source
  else quiet_build tools "$what" "$finished" "$ROOT/scripts/build-tools.sh"; fi
fi

# --- 3. Lume ------------------------------------------------------------------------
step "Lume, the VM runner (Apple's Virtualization framework)" "$([ "$lume_have" = 1 ] && echo "already installed" || echo "about 1 minute")"
[ -z "${LUME_KEPT:-}" ] || ok "keeping your choice: $LUME_KEPT"
if [ "$LUME_CHOICE" = mcomp-vm ]; then
  if [ "$lume_have" = 1 ]; then
    ok "already installed: $LUME_BIN (mcomp-vm ${MCOMP_VM_TAG#v})"
  else
    FAIL_HINT=("mcomp-vm didn't build. It needs Apple's Command Line Tools (xcode-select -p shows them) and github.com."
               "Run ./install.sh again, or use Cua's standard Lume instead: ./install.sh --lume=standard")
    if [ "$FRESH_SOURCES" = 1 ]; then   # a new temporary clone; nothing in the checkout is touched
      src="$(mktemp -d "${TMPDIR:-/tmp}/mcomp-vm-src.XXXXXX")/mcomp-vm"
    else
      src="$ROOT/.cache/mcomp-vm"; "$ROOT/scripts/safe-rmtree.sh" "$src" || die "not deleting $src (above)"; mkdir -p "$ROOT/.cache"
    fi
    git_net clone -q -c advice.detachedHead=false --depth 1 --branch "$MCOMP_VM_TAG" "$MCOMP_VM_REPO" "$src" \
      || die "couldn't download mcomp-vm's source ($MCOMP_VM_REPO, $MCOMP_VM_TAG)" "${FAIL_HINT[@]}"
    [ "$(git -C "$src" rev-parse HEAD)" = "$MCOMP_VM_COMMIT" ] || die "mcomp-vm $MCOMP_VM_TAG isn't the pinned commit $MCOMP_VM_COMMIT"
    quiet_build mcomp-vm "building mcomp-vm $MCOMP_VM_TAG with Swift" "mcomp-vm built" "$src/install.sh"
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
  if [ "$lume_have" = 1 ]; then
    ok "already installed: $LUME_BIN"
  else
    FAIL_HINT=("Lume's download from github.com/trycua/cua didn't finish or didn't match its pinned checksum. Run ./install.sh again.")
    tmp="$(mktemp -d)"; trap '"$ROOT/scripts/safe-rmtree.sh" --quiet "$tmp"' EXIT
    tgz="lume-$LUME_VERSION-darwin-arm64.tar.gz"
    curl -fsSL --retry 3 -o "$tmp/$tgz" "https://github.com/trycua/cua/releases/download/lume-v$LUME_VERSION/$tgz"
    echo "$LUME_TARBALL_SHA256  $tmp/$tgz" | shasum -a 256 -c - >/dev/null || die "Lume download failed its checksum"
    tar -xzf "$tmp/$tgz" -C "$tmp"
    mkdir -p "$HOME/.local/share/lume" "$HOME/.local/bin"
    "$ROOT/scripts/safe-rmtree.sh" "$HOME/.local/share/lume/lume.app" || die "not replacing lume.app (above)"
    mv "$tmp/lume.app" "$HOME/.local/share/lume/"
    printf '#!/bin/sh\nLUME_TELEMETRY_ENABLED=0 exec "$HOME/.local/share/lume/lume.app/Contents/MacOS/lume" "$@"\n' > "$LUME_BIN"
    chmod +x "$LUME_BIN"
    ok "installed $LUME_BIN (checksum verified, telemetry off, no background service)"
  fi
  warn "with standard Lume the VM always shares your Mac's clipboard, audio and Rosetta"
fi

# --- 4. this Mac's settings -------------------------------------------------------
step "Settings ($(tildify "$CONF"))"
if [ -f "$CONF" ]; then
  ok "keeping the existing $CONF"
else
  cp "$ROOT/config/local.env.example" "$CONF"
  if [ "$chrome_in" = 1 ]; then
    echo 'BROWSERS="firefox chrome"' >> "$CONF"
    ok "Google Chrome goes in the VM next to Firefox (Google's terms: https://www.google.com/chrome/terms/)"
  else
    echo 'BROWSERS="firefox"' >> "$CONF"
  fi
  if [ "$mem_gb" -lt 16 ]; then echo "VM_MEMORY=4GB" >> "$CONF"; warn "this Mac has ${mem_gb} GB of memory; the VM gets 4 GB"; fi
  echo 'VNC_PASSWORD_STORE=keychain' >> "$CONF"   # the live-view password: the login Keychain
  ok "wrote $CONF (the live-view password will go in your login Keychain)"
fi
# The Lume chosen above (a line of its own, replaced on a change).
lume_line="LUME=\"\$HOME/.local/bin/$(basename "$LUME_BIN")\""
if ! grep -qx "$lume_line" "$CONF"; then
  { grep -v '^LUME=' "$CONF"; echo "$lume_line"; } > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
  ok "Lume: $LUME_BIN"
fi
# shellcheck source=/dev/null
. "$CONF"

# --- 5. Claude Code (and Codex) ----------------------------------------------------
[ "$CLAUDE" = 0 ] || claude_step
if codex_wanted; then
  [ "$CLAUDE" = 1 ] || step "Codex"
  codex_step
elif [ "$CODEX" = auto ] && has_codex; then
  printf '%s\n' "  Codex is installed too; to use metacomputer from it: ./install.sh --only-codex (docs/codex.md)"
fi

# --- 6. the secrets vault -------------------------------------------------------------
step "Secrets vault (metacomputer secrets)" "$([ "$vault_ok" = 1 ] && echo "about 1 minute, unless it's up to date" || echo "skipped")"
if [ "$vault_ok" = 1 ]; then
  quiet_build vault "building the secrets vault (mcomp-vault) with Swift, unless it's up to date" \
    "secrets vault: $(tildify "$HOME/.local/share/metacomputer/bin/mcomp-vault") is up to date" "$ROOT/scripts/build-vault.sh" \
    || warn "the vault didn't build; metacomputer secrets won't work until scripts/build-vault.sh succeeds"
else
  warn "skipped: the vault needs macOS 15 and swiftc (xcode-select --install); then run scripts/build-vault.sh. Everything else works without it"
fi

# --- 7. the VM ----------------------------------------------------------------------
step "The virtual computer ($VM_NAME)" "$([ "$build_vm" = 1 ] && echo "about 3 minutes on a fast connection, 10 or more on a slow one; about 1.5 GB of downloads" || echo "no build")"
FAIL_HINT=("The VM build stopped; its log is the newest build-*.log in ${LOG_DIR:-$ROOT/logs}."
           "Finished downloads are kept. To delete the unfinished VM and build it again: ./install.sh --replace-vm"
           "If provisioning failed inside the VM, the VM is left running so you can look: metacomputer ssh admin")
# metacomputer build [--replace]. A build that stops before it begins (no build marker: a refusal, such as settings whose
# folders are another VM's, or a failed download) makes and deletes no VM, so --replace-vm and the VM to look at aren't
# offered; its own message says why it stopped.
vm_build() {
  local rc=0
  "$ROOT/bin/metacomputer" build "$@" || rc=$?
  [ "$rc" != 0 ] || return 0
  [ -e "${RUN_DIR:-$ROOT/.run}/build-unfinished-$VM_NAME" ] \
    || FAIL_HINT=("The VM build stopped before it began, so no VM was made or deleted; the reason is above (and in the newest build-*.log in ${LOG_DIR:-$ROOT/logs}).")
  return "$rc"
}
if [ "$BUILD" = no ]; then
  # --no-build: whatever the VM's state, it's left as it is and this run succeeds; say what's there.
  case "$vm_state" in
    built) ok "already built (and its clean snapshot $BASELINE_NAME)" ;;
    none) echo "  Build it later with: metacomputer build" ;;
    interrupted) warn "left as it is (--no-build): an earlier build of $VM_NAME didn't finish; when you're ready: ./install.sh --replace-vm" ;;
    nobaseline) warn "left as it is (--no-build): $VM_NAME has no clean snapshot (it may hold your work); keep it with metacomputer baseline" ;;
    snapshotonly) warn "left as it is (--no-build): $VM_NAME is missing, its clean snapshot is there; restore it with metacomputer reset" ;;
    *) warn "left as it is (--no-build): Lume couldn't list the VMs" ;;
  esac
else
  if [ "$build_vm" = 1 ]; then
    vm_store_now="$(vm_storage_dir)" || die "couldn't tell where $LUME_BIN keeps VMs ('$LUME_BIN config get'); nothing was built"
    if [ "$vm_store_now" != "${vm_store:-}" ]; then SPACE_NEED=("$vm_store_now|$VM_FS_GB|the VM, where Lume keeps VMs"); space_check; fi
  fi
  case "$vm_state:$build_vm:$vm_replace" in
    built:0:*) ok "already built (and its clean snapshot $BASELINE_NAME)" ;;
    *:1:1)
      warn "--replace-vm: deleting $VM_NAME and $BASELINE_NAME, then building them again"
      vm_build --replace
      report_vm_built ;;
    none:1:0) vm_build; report_vm_built ;;
    interrupted:*)
      die "an earlier build of $VM_NAME didn't finish ($(sed -n 's/^started=/started /p' "$VM_MARKER" 2>/dev/null | head -1)), so it was left as it is." \
          "To delete it and build it again: ./install.sh --replace-vm (or metacomputer build --replace)." \
          "Everything else is set up." ;;
    nobaseline:*)
      die "$VM_NAME exists without its clean snapshot ($BASELINE_NAME), and nothing says a build of it was under way," \
          "so it may hold your work: it was left as it is." \
          "To keep it and make its current state the clean snapshot: metacomputer baseline" \
          "To delete it and build a new one: ./install.sh --replace-vm" ;;
    snapshotonly:*)
      die "$VM_NAME is missing, but its clean snapshot $BASELINE_NAME is there; it was left as it is." \
          "To restore the VM from the snapshot: metacomputer reset" \
          "To delete the snapshot and build both again: ./install.sh --replace-vm" ;;
    *) die "couldn't list the VMs with ${vm_runner:-the runner}; the VM was left alone" ;;
  esac
fi
stage_done
FAIL_HINT=()

echo
echo "${b}==> Done${o}"
for t in ${STAGE_TIMES[@]+"${STAGE_TIMES[@]}"}; do [ "${t##*|}" -lt 5 ] || printf '  %-58s %s\n' "${t%|*}" "$(dur "${t##*|}")"; done
cat <<EOF
  metacomputer start      boot the VM (about 12 s)
  metacomputer status     check every layer; exit code 0 means ready
  metacomputer view       watch or take over the desktop
  metacomputer help       everything else
In Claude Code (restart open sessions, or run /reload-plugins in them), try:
  "Get yourself a virtual computer and tell me what's on its screen."
EOF
# What's running now, against this checkout: services on older code, a VM on an earlier gateway, a vault to bind
# again, a rebuild that would add something, and the version installed (lib/update.py; it never fails the install).
if [ -f "$ROOT/lib/update.py" ] && { [ -n "${PY311:-}" ] || py311_ok; }; then "$PY311" "$ROOT/lib/update.py" _after-install || true; fi
