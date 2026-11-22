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
#   Every run (a first install, `metacomputer update`, the install line again) sets metacomputer up for each coding
#   agent installed on this Mac, Claude Code and Codex, and for one installed since the last run:
#   ./install.sh --no-claude   leave Claude Code alone (plugin, skill, MCP server), this time and in later runs
#   ./install.sh --claude      set Claude Code up even if an earlier run had --no-claude
#   ./install.sh --no-plugin   Claude Code without the plugin: a skill link instead, and the same user-scope
#                              MCP server (tools: mcp__metacomputer__*)
#   ./install.sh --only-claude only the Claude Code step (no tools, Lume, settings, vault or VM)
#   ./install.sh --no-codex    leave Codex alone, this time and in later runs (docs/codex.md)
#   ./install.sh --codex       set Codex up even if an earlier run had --no-codex
#   ./install.sh --only-codex  only the Codex step: its MCP server, the skills in ~/.agents/skills and metacomputer's
#                              rules for its sandbox (no tools, Lume, settings, vault or VM)
#   ./install.sh --only-agents only those two steps, for whichever agents are installed (after installing one)
#   ./install.sh --uninstall   remove what this installed for Claude Code and Codex, and the command (the VM stays)
#   ./install.sh --switch      the metacomputer command runs another checkout: move it (and Claude Code's plugin
#                              and MCP server) to this one. Without a terminal, or with --yes, that's refused otherwise.
#                              A user-scope MCP server named metacomputer that isn't metacomputer's (another program,
#                              or other arguments) is never changed: the run says how to remove it yourself
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
#   5. links the `metacomputer` command; for each agent installed (Claude Code, Codex): adds this checkout to Claude Code as the
#      `metacomputer` plugin (its skills, /metacomputer:learn and the command, for every project) and registers
#      the user-scope MCP server `metacomputer`, ~/.local/bin/metacomputer mcp-lazy (tools: mcp__metacomputer__*;
#      the plugin declares no MCP server or hooks). Without `claude plugin`, or with --no-plugin: the
#      `metacomputer` skill link instead of the plugin, and the same server.
#      With Codex (see --codex): its MCP server metacomputer (tools mcp__metacomputer__*), the
#      skills in ~/.agents/skills and $CODEX_HOME/rules/metacomputer.rules
#   6. builds the secrets vault, mcomp-vault (`metacomputer secrets`), with swiftc
#   7. builds the VM: about 1.5 GB of downloads, then a few minutes unattended
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
YES=0; CHROME=""; BUILD=""; CLAUDE=auto; PLUGIN=1; ONLY_CLAUDE=0; UNINSTALL=0; LUME_CHOICE=""; REPLACE_VM=0; FRESH_SOURCES=0
SWITCH=0; ALLOW_DOWNGRADE=0; CODEX=auto; ONLY_CODEX=0; ONLY_AGENTS=0
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;; --no-chrome) CHROME=no ;; --no-build) BUILD=no ;; --replace-vm) REPLACE_VM=1 ;; --fresh-sources) FRESH_SOURCES=1 ;;
    --no-claude) CLAUDE=no ;; --claude) CLAUDE=yes ;; --only-agents) ONLY_AGENTS=1 ;; --no-plugin) PLUGIN=0 ;; --only-claude) ONLY_CLAUDE=1 ;; --uninstall) UNINSTALL=1 ;;
    --switch) SWITCH=1 ;; --allow-downgrade) ALLOW_DOWNGRADE=1 ;;
    --codex) CODEX=yes ;; --no-codex) CODEX=no ;; --only-codex) ONLY_CODEX=1 ;;
    --lume=mcomp-vm|--lume=standard) LUME_CHOICE="${a#--lume=}" ;;
    --lume=secure) LUME_CHOICE=mcomp-vm ;;   # mcomp-vm's former name, lume-secure
    -h|--help) sed -n '2,/^set -Eeuo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a (see --help)" >&2; exit 2 ;;
  esac
done
[ "$ONLY_CLAUDE" = 0 ] || [ "$CLAUDE" != no ] || { echo "--only-claude and --no-claude together leave nothing to do" >&2; exit 2; }
[ "$ONLY_CODEX" = 0 ] || [ "$CODEX" != no ] || { echo "--only-codex and --no-codex together leave nothing to do" >&2; exit 2; }
[ "$((ONLY_CODEX + ONLY_CLAUDE + ONLY_AGENTS))" -le 1 ] || { echo "--only-claude, --only-codex and --only-agents: one at a time (--only-agents does both)" >&2; exit 2; }

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
tildify() { case "$1" in "$HOME"/*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
resolve_link() { # PATH -> where it ends up, following links (relative ones from their own folder), at most 20 of them
  local p="$1" i=0 t
  while [ -L "$p" ] && [ $i -lt 20 ]; do
    t="$(readlink "$p")"; case "$t" in /*) p="$t" ;; *) p="$(dirname "$p")/$t" ;; esac; i=$((i + 1))
  done
  printf '%s\n' "$p"
}
real_dir() { (cd "$1" 2>/dev/null && pwd -P); }
is_checkout() { [ -f "$1/install.sh" ] && [ -f "$1/bin/metacomputer" ]; }   # a metacomputer checkout, as install.sh tells one
# Is $1 (a link target or a command path) one of an earlier version's: inside this
# checkout, or an old name that points nowhere (the checkout moved or was renamed)?
old_target() {
  case "$1" in "$ROOT"/*) return 0 ;; esac
  [ ! -e "$1" ] || return 1
  case "$1" in */bin/vc|*/bin/cua-lab|*/bin/virtual-computer|*/skills/virtual-computer) return 0 ;;
    */bin/metacomputer|*/skills/metacomputer) return 0 ;; esac
  return 1
}
# --- Claude Code: the plugin (skills) or, before plugins and with --no-plugin, a skill link; and a user-scope MCP server
MKT=metacomputer; PLUGIN_ID=metacomputer@metacomputer; TAB=$'\t'; US=$'\037'
CLAUDE_MCP=user    # this installer registers Claude Code's MCP server itself, at user scope; plugin/bin/metacomputer looks
                   # for this line (an install.sh without it relied on the plugin for the server)
SERVER_ADDED=0     # 1 once this run registered Claude Code's MCP server (a restart, or /mcp, loads its tools)
CLAUDE_FAILED=0    # 1 when Claude Code's setup didn't finish: the run ends with exit status 1, after everything else
CLAUDE_TRIED=0     # 1 once this run set Claude Code up (claude_step with a claude command): only then is its result
                   # recorded (agent_record); a run that skipped it leaves an earlier unfinished one recorded
CODEX_FAILED=0     # the same two for Codex's step (codex_step: its MCP server)
CODEX_TRIED=0
REG_LEFT=""        # register_server's: what's left after a registration that didn't work: "none" (no server), "unknown"
                   # (it can't be read), or "other" (something that was there stays)
has_claude() { command -v claude >/dev/null 2>&1; }
has_plugin_cli() { has_claude && claude plugin --help >/dev/null 2>&1; }
# The marketplace named metacomputer as "SOURCE<TAB>WHERE" ("directory<TAB>/path", "github<TAB>owner/repo"), "absent", or
# "error" (`claude plugin marketplace list --json` failed or answered in a shape this doesn't know): never read as absent.
mkt_lookup() {
  local out
  # failures are caught inside the substitution (with set -E, install.sh's ERR trap would fire in it otherwise)
  out="$(claude plugin marketplace list --json 2>/dev/null </dev/null || echo __mcomp_failed__)"
  case "$out" in *__mcomp_failed__) echo error; return 0 ;; esac
  printf '%s' "$out" | python3 -c 'import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = None
if not isinstance(rows, list) or not all(isinstance(m, dict) and isinstance(m.get("name"), str) for m in rows):
    print("error"); sys.exit(0)
mine = [m for m in rows if m["name"] == "metacomputer"]
src = mine[0].get("source", "") if len(mine) == 1 else None
where = (mine[0].get("path") or mine[0].get("repo") or mine[0].get("url") or "") if len(mine) == 1 else None
if len(mine) > 1 or (mine and not (isinstance(src, str) and isinstance(where, str))) or any(c in (src or "") + (where or "") for c in "\t\n"):
    print("error")
else:
    print(src + "\t" + where if mine else "absent")' 2>/dev/null || echo error
}
# Messages name a marketplace other than this checkout and the project's repository only by its name, metacomputer, and
# "another checkout" or "another source": never its folder or location (a URL, or a "repo" that is one, can carry
# credentials). This checkout's folder and the project's repository are named as constants.
# The plugin metacomputer@metacomputer at user scope, from `claude plugin list --json`, as "STATE SERVERS": STATE is
# "on", "off" (installed, turned off) or "absent"; SERVERS is "present" when it declares MCP servers of its own (a copy
# from before this step registered the server: installed from GitHub, not updated since), else "absent". The answer is
# "error" when the list failed, answered in a shape this doesn't know, has a record without its id or scope, or this
# plugin's record reports errors loading it, has no true/false "enabled", or declares its servers in a shape this
# doesn't know: never read as absent, on or off.
plugin_lookup() {
  local out
  out="$(claude plugin list --json 2>/dev/null </dev/null || echo __mcomp_failed__)"
  case "$out" in *__mcomp_failed__) echo error; return 0 ;; esac
  printf '%s' "$out" | python3 -c 'import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = None
ok = isinstance(rows, list) and all(isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("scope"), str)
                                    for p in rows)
mine = [p for p in rows if p.get("id") == "metacomputer@metacomputer" and p.get("scope") == "user"] if ok else []
if not ok or len(mine) > 1:
    print("error")
elif not mine:
    print("absent absent")
else:
    p = mine[0]; srv = p.get("mcpServers")
    if p.get("errors") or p.get("error") or not isinstance(p.get("enabled"), bool) or not isinstance(srv, (dict, type(None))):
        print("error")
    else:
        print("on" if p["enabled"] else "off", "present" if srv else "absent")' 2>/dev/null || echo error
}
plugin_state() { local s; s="$(plugin_lookup)"; printf '%s\n' "${s%% *}"; }    # on, off, absent or error
# Does the installed plugin declare MCP servers? present, absent (none, or no such plugin) or error (never read as
# absent). The plugin declares none; a copy of it from before (installed from GitHub, not updated since) brings its own.
plugin_servers_state() { local s; s="$(plugin_lookup)"; case "$s" in error) echo error ;; *) printf '%s\n' "${s#* }" ;; esac; }
# A program's name for a message about something that isn't metacomputer's: its file name when that's a plain short
# word, else "a program" (a path, or a name that's more than a name, isn't printed).
prog_name() {
  local n="${1%/}"; n="${n##*/}"
  case "$n" in
    ""|[!A-Za-z0-9]*|*[!A-Za-z0-9._+-]*) printf 'a program' ;;
    *) if [ "${#n}" -le 32 ]; then printf '%s' "$n"; else printf 'a program'; fi ;;
  esac
}
# Claude Code's user-scope MCP server NAME, read exactly (lib/claude_mcp.py: the entry in Claude Code's configuration,
# checked against what `claude mcp get` shows from a new empty folder, so no project or local server stands in for it,
# and read the same before and after that), on one line of fields split by $US: OWNER, KIND, WORDS, DIGEST.
# OWNER is what it is to this checkout (canonical command and exact arguments): "ok" (~/.local/bin/metacomputer
# mcp-lazy), "ours" (this checkout's own launcher, or a path earlier versions registered: repaired), "checkout" (another
# checkout's: --switch), "foreign" (anything else: never changed), "absent" or "error" (never read as absent: nothing is
# changed). WORDS says what it is, for messages: for one that isn't metacomputer's, only its program's name and how many
# arguments it has, or its transport (never its arguments, variables, URL or entry, which can carry credentials).
# DIGEST stands for the whole entry, for user_recheck.
user_server() { # NAME
  python3 "$ROOT/lib/claude_mcp.py" user-get "$1" --root "$ROOT" --link "$CMD_LINK" 2>/dev/null </dev/null \
    || printf 'error%serror%sclaude_mcp.py failed%s\n' "$US" "$US" "$US"
}
# Right before a user-scope entry is removed: read it again (from the configuration file alone, which is quick) and
# compare the whole entry, its variables included, with the one classified (DIGEST, from user_server): "same",
# "changed" (another Claude Code changed or removed it meanwhile: it's left as it is) or "error<US>WHY". Claude Code
# takes no lock this could share across a remove and an add, so a change in the moment between this read and
# `claude mcp remove` can still go unseen; only metacomputer's own entries are ever removed.
user_recheck() { # NAME DIGEST
  printf '%s\n' "$2" | python3 "$ROOT/lib/claude_mcp.py" user-recheck "$1" 2>/dev/null \
    || printf 'error%sclaude_mcp.py failed\n' "$US"
}
entry_unchanged() { # NAME DIGEST: 0 when the user-scope entry NAME is still exactly the one classified
  local st
  IFS=$US read -r st _ <<<"$(user_recheck "$1" "$2")"
  [ "$st" = same ]
}
# A project or local server named metacomputer in the folder this runs in takes precedence over the user-scope one
# there: said (its scope and program's name, never its arguments, variables or URL), never changed.
shadow_note() {
  local out scope cmd what
  out="$(claude mcp get metacomputer 2>/dev/null </dev/null || echo __mcomp_failed__)"
  case "$out" in *__mcomp_failed__) return 0 ;; esac
  scope="$(printf '%s\n' "$out" | sed -n 's/^  Scope: //p' | head -1)"
  case "$scope" in ""|"User config"*) return 0 ;; esac
  cmd="$(printf '%s\n' "$out" | sed -n 's/^  Command: //p' | head -1)"
  if [ -n "$cmd" ]; then what="it runs $(prog_name "$cmd")"; else what="not a local command"; fi
  warn "in $(tildify "$PWD"), Claude Code uses an MCP server named metacomputer from its $(prog_name "${scope%% *}") config"
  warn "instead of the user-scope one ($what); left as it is"
}
# An earlier install's skill link, removed only when it is this checkout's (or a moved checkout's that no longer
# exists), so the skill isn't listed twice.
remove_skill_link() {
  if [ -L "$SKILL_LINK" ] && old_target "$(readlink "$SKILL_LINK")"; then
    rm "$SKILL_LINK" && ok "removed the skill link $SKILL_LINK (the plugin brings the skill)"
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
# Is the installed plugin this checkout's? "ours" from its marketplace, or from the canonical GitHub repository (that
# copy runs this checkout through ~/.local/bin/metacomputer), or with no marketplace of that name; "other" from another
# checkout or any other source; "error" when the marketplaces can't be read (never taken for ours).
plugin_owner() {
  local mkt; mkt="$(mkt_lookup)"
  case "$mkt" in
    error) echo error ;;
    absent|"directory${TAB}$ROOT") echo ours ;;
    *) if mkt_is_canonical "$mkt"; then echo ours; else echo other; fi ;;
  esac
}
remove_plugin() { # uninstall the plugin; remove the marketplace when it is this checkout. Each is read before and
  # checked afterwards: 1 = something is still there, or whether it is can't be read (said, with the command that removes it)
  local mkt st rc=0; mkt="$(mkt_lookup)"; st="$(plugin_state)"
  if [ "$mkt" = error ] || [ "$st" = error ]; then
    warn "couldn't read Claude Code's plugins and marketplaces (claude plugin list --json, claude plugin marketplace list --json),"
    warn "so the plugin $PLUGIN_ID and the marketplace $MKT were left as they are (claude plugin uninstall $PLUGIN_ID --scope user)"
    return 1
  fi
  if [ "$st" != absent ]; then
    claude plugin uninstall "$PLUGIN_ID" --scope user >/dev/null 2>&1 || true
    case "$(plugin_state)" in
      absent) ok "uninstalled the plugin $PLUGIN_ID" ;;
      error) warn "couldn't read the installed plugins after uninstalling $PLUGIN_ID, so whether it's gone is unknown"
             warn "(claude plugin list; to remove it: claude plugin uninstall $PLUGIN_ID --scope user)"; rc=1 ;;
      *) warn "couldn't uninstall the plugin $PLUGIN_ID (claude plugin uninstall $PLUGIN_ID --scope user)"; rc=1 ;;
    esac
  fi
  if [ "$rc" != 0 ]; then
    :     # the marketplace stays too: removing it would take the plugin with it, unchecked
  elif [ "$mkt" = "directory${TAB}$ROOT" ]; then
    claude plugin marketplace remove "$MKT" >/dev/null 2>&1 || true
    case "$(mkt_lookup)" in
      absent) ok "removed the marketplace $MKT (this checkout)" ;;
      error) warn "couldn't read Claude Code's marketplaces after removing $MKT, so whether it's gone is unknown"
             warn "(claude plugin marketplace list; to remove it: claude plugin marketplace remove $MKT)"; rc=1 ;;
      *) warn "couldn't remove the marketplace $MKT (claude plugin marketplace remove $MKT)"; rc=1 ;;
    esac
  elif mkt_is_canonical "$mkt"; then
    echo "  The marketplace $MKT (github.com/$CANON_REPO) stays; to remove it: claude plugin marketplace remove $MKT"
  elif [ "$mkt" != absent ]; then
    echo "  The marketplace $MKT (another source's) stays; to remove it: claude plugin marketplace remove $MKT"
  fi
  return "$rc"
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
# Codex's MCP server named metacomputer, read whole (lib/agents.py codex-entry: `codex mcp get --json`), on one line of
# fields split by $US: OWNER, WORDS, DIGEST, COMMAND, LINKED. OWNER, by the rule Claude Code's follows: "ok" (a local
# command, exactly this checkout's bin/metacomputer or ~/.local/bin/metacomputer, with exactly ["mcp-lazy"]), "ours" (the
# same with a path earlier versions registered under this checkout's bin/: pointed at bin/metacomputer), "foreign"
# (anything else, a path that only starts with the checkout's included: never changed or removed), "absent" or "error"
# (never read as absent). WORDS names metacomputer's own by its command, anything else by its program and how many
# arguments it has, or its transport (never an argument, a variable or a URL). DIGEST stands for the whole entry, read
# again right before any change (codex_unchanged); COMMAND is given only for ok and ours; LINKED is "link" when it runs
# ~/.local/bin/metacomputer or mcomp themselves.
codex_server() {
  codex_python || { printf 'error%sthe Codex step needs Python 3.11 or later%s%s%s\n' "$US" "$US" "$US" "$US"; return 0; }
  "$CPY" "$ROOT/lib/agents.py" codex-entry --root "$ROOT" --link "$CMD_LINK" 2>/dev/null </dev/null \
    || printf 'error%sagents.py codex-entry failed%s%s%s\n' "$US" "$US" "$US" "$US"
}
codex_unchanged() { # DIGEST: 0 when Codex's MCP server metacomputer is still exactly the entry classified
  local sum
  IFS=$US read -r _ _ sum _ <<<"$(codex_server)"
  [ -n "$1" ] && [ "$sum" = "$1" ]
}
codex_rules_ours() { # rules this checkout wrote (or a moved checkout that's gone)
  [ -f "$CODEX_RULES" ] && [ "$(head -1 "$CODEX_RULES" 2>/dev/null)" = "$CODEX_MARK" ] || return 1
  local owner; owner="$(sed -n '4p' "$CODEX_RULES" 2>/dev/null)"
  # The first format (never released) had no checkout line: metacomputer's own, rewritten with the current rules.
  [ "$owner" = "#" ] && return 0
  case "$owner" in "$CODEX_OWNER"*) owner="${owner#"$CODEX_OWNER"}" ;; *) return 1 ;; esac
  [ "$owner" = "$ROOT" ] || { [ -n "$owner" ] && old_target "$owner/bin/metacomputer"; }
}
# The agents: each one installed is set up, by a first install and by every later run (so one installed since gets
# set up by the next update), unless you said --no-claude / --no-codex, which is remembered until --claude / --codex.
AGENTS_FILE="$HOME/.local/share/metacomputer/agents.json"
agent_choice() { # NAME: "on" (you asked for it), "off" (--no-NAME in an earlier run) or nothing
  [ -f "$AGENTS_FILE" ] || return 0
  sed -n -E "s/^ *\"$1\": *\"(on|off)\".*/\1/p" "$AGENTS_FILE" 2>/dev/null | head -1 || true
}
agent_remember() { # NAME on|off: an explicit --NAME / --no-NAME, for later runs
  local claude codex
  claude="$(agent_choice claude)"; codex="$(agent_choice codex)"
  case "$1" in claude) claude="$2" ;; codex) codex="$2" ;; esac
  mkdir -p "$(dirname "$AGENTS_FILE")"
  printf '{\n  "claude": "%s",\n  "codex": "%s"\n}\n' "${claude:-auto}" "${codex:-auto}" > "$AGENTS_FILE.tmp" \
    && mv "$AGENTS_FILE.tmp" "$AGENTS_FILE"
}
claude_wanted() { # set Claude Code up this run: --claude, or auto and not --no-claude before (installed or not)
  case "$CLAUDE" in yes) return 0 ;; no) return 1 ;; esac
  [ "$(agent_choice claude)" != off ]
}
codex_wanted() { # --codex, or Codex installed and not --no-codex before
  case "$CODEX" in yes) return 0 ;; no) return 1 ;; esac
  has_codex && [ "$(agent_choice codex)" != off ]
}
LINKED=0
link_command() { # ~/.local/bin/metacomputer and mcomp (every install, whichever agents), earlier names removed (once per run)
  [ "$LINKED" = 0 ] || return 0
  LINKED=1
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$ROOT/bin/metacomputer" "$CMD_LINK" && ok "command: $CMD_LINK"
  if [ -e "$ALIAS_LINK" ] && ! link_points_here "$ALIAS_LINK" "$ROOT/bin/metacomputer"; then
    warn "$ALIAS_LINK already exists and is something else; left alone (the full name works)"
  else
    ln -sfn "$ROOT/bin/metacomputer" "$ALIAS_LINK" && ok "alias: mcomp"
  fi
  # Command names used by earlier versions (vc, cua-lab, virtual-computer, vcomp). The old Claude Code skill name is
  # claude_step's: --no-claude leaves everything of Claude Code's alone.
  local old
  for old in "$HOME/.local/bin/vc" "$HOME/.local/bin/cua-lab" "$HOME/.local/bin/virtual-computer" \
             "$HOME/.local/bin/vcomp"; do
    if [ -L "$old" ] && old_target "$(readlink "$old")"; then
      rm -f "${old:?}" && ok "removed the old name $old"
    fi
  done
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "~/.local/bin isn't on your PATH; add it to use 'metacomputer' directly (agents use the full path)" ;; esac
}
agents_stage() { # step 5 (and --only-agents): each agent, as the switches and earlier choices say
  link_command
  if claude_wanted; then
    [ "$CLAUDE" = yes ] && agent_remember claude on
    if has_claude || [ "$CLAUDE" = yes ]; then
      printf '%s\n' "  Claude Code:"
      claude_step
    else
      printf '%s\n' "  Claude Code isn't installed; once it is: ./install.sh --only-agents (or metacomputer update)"
    fi
  else
    [ "$CLAUDE" = no ] && agent_remember claude off
    printf '%s\n' "  Claude Code: left alone (--no-claude$([ "$CLAUDE" = no ] || echo " in an earlier run"); --claude sets it up)"
  fi
  if codex_wanted; then
    [ "$CODEX" = yes ] && agent_remember codex on
    codex_step
  else
    [ "$CODEX" = no ] && agent_remember codex off
    if [ "$CODEX" = no ] || [ "$(agent_choice codex)" = off ]; then
      printf '%s\n' "  Codex: left alone (--no-codex$([ "$CODEX" = no ] || echo " in an earlier run"); --codex sets it up)"
    else
      printf '%s\n' "  Codex isn't installed; once it is: ./install.sh --only-agents (or metacomputer update)"
    fi
  fi
  if ! has_claude && ! has_codex; then
    warn "neither Claude Code nor Codex is installed: metacomputer works from either. Install one, then: ./install.sh --only-agents"
  fi
  printf '%s\n' "  (metacomputer agents shows this any time)"
}
codex_config_edit() { # MODE COMMAND: "settings" adds the timeout and approval mode where they're missing; "command"
  # points the registration at this checkout. Either only when the table still runs COMMAND with exactly ["mcp-lazy"]
  # (metacomputer's own, as classified). Only that table changes, and only as asked: the whole file is parsed again and
  # compared, or nothing is written. Everything you set stays.
  local cfg="$CODEX_DIR/config.toml" out
  [ -f "$cfg" ] || { echo missing; return 0; }
  # It prints ok, the notes, or failed, and exits 0: a failure inside the substitution would trip install.sh's ERR trap.
  out="$("$CPY" - "$cfg" "$1" "$CODEX_TOOL_TIMEOUT" "$CODEX_APPROVAL" "$ROOT/bin/metacomputer" "${2:-}" 2>/dev/null <<'PY' || echo failed
import copy, json, os, sys, tempfile, tomllib


def fail():
    print("failed")
    sys.exit(0)


path, mode, want_t, want_a, cmd, old = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6]
text = open(path, encoding="utf-8").read()
try:
    before = tomllib.loads(text)
except tomllib.TOMLDecodeError:
    fail()
srv = before.get("mcp_servers", {}).get("metacomputer")
if not isinstance(srv, dict):
    fail()
if srv.get("command") != old or srv.get("args") != ["mcp-lazy"]:
    fail()                           # not the entry classified as metacomputer-owned (changed since?): left alone
lines = text.split("\n")
heads = [i for i, l in enumerate(lines) if l.strip() == "[mcp_servers.metacomputer]"]
if len(heads) != 1:
    fail()
start = heads[0] + 1
end = next((i for i in range(start, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
notes = []
if mode == "command":
    cmds = [i for i in range(start, end) if lines[i].split("=", 1)[0].strip() == "command"]
    if len(cmds) != 1:
        fail()
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
    expected = copy.deepcopy(before)
    m = expected["mcp_servers"]["metacomputer"]
    if mode == "command":
        m["command"] = cmd
    else:
        for line in add:
            k, v = line.split(" = ", 1)
            m[k] = tomllib.loads(f"x = {v}")["x"]
    try:
        after = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        fail()
    if after != expected:
        fail()                       # anything else changed (a header inside a string, say): nothing is written
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.toml.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
print(" ".join(notes) or "ok")
PY
)"
  case "$out" in *failed*|"") echo failed ;; *) printf '%s\n' "$out" ;; esac
}
codex_server_settings() { # COMMAND: the one the entry runs (metacomputer's own)
  local out; out="$(codex_config_edit settings "$1")"
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
  link_command
  printf '%s\n' "  Codex:"
  if ! has_codex; then
    warn "Codex's 'codex' command isn't installed; after installing it, run: ./install.sh --only-codex"
    return 0
  fi
  if ! codex_python; then
    warn "the Codex step needs Python 3.11 or later (brew install python), so Codex was left alone; then: ./install.sh --only-codex"
    return 0
  fi
  local own what sum cmd name target link
  CODEX_TRIED=1
  IFS=$US read -r own what sum cmd _ <<<"$(codex_server)"
  case "$own" in
    error)
      warn "couldn't read Codex's MCP server metacomputer ($what), so it was left as it is; try ./install.sh --only-codex again"
      CODEX_FAILED=1 ;;
    foreign)
      warn "Codex already has an MCP server named metacomputer that isn't metacomputer's (it runs $what); it was left as it is."
      warn "To use metacomputer's instead: codex mcp remove metacomputer, then ./install.sh --only-codex"
      CODEX_FAILED=1 ;;
    absent)
      if codex mcp add metacomputer -- "$ROOT/bin/metacomputer" mcp-lazy >/dev/null 2>&1; then
        ok "MCP server metacomputer for every Codex session (tools: mcp__metacomputer__*, there even while the VM is off)"
        codex_server_settings "$ROOT/bin/metacomputer"
      else
        warn "could not add the MCP server to Codex; run: codex mcp add metacomputer -- $ROOT/bin/metacomputer mcp-lazy"
        CODEX_FAILED=1
      fi ;;
    ok)
      ok "MCP server metacomputer already registered (tools: mcp__metacomputer__*)"
      codex_server_settings "$cmd" ;;
    ours)
      # a path earlier versions registered under this checkout: only its command changes (anything you set on it stays),
      # and only when it's still exactly the entry read above; then it's read back
      if ! codex_unchanged "$sum"; then
        warn "Codex's MCP server metacomputer changed since it was read, so it was left as it is; run ./install.sh --only-codex again"
        CODEX_FAILED=1
      else
        codex_config_edit command "$cmd" >/dev/null
        IFS=$US read -r own _ _ _ _ <<<"$(codex_server)"
        if [ "$own" = ok ]; then
          ok "MCP server metacomputer now runs this checkout (your settings for it kept)"
          codex_server_settings "$ROOT/bin/metacomputer"
        else
          warn "couldn't point Codex's MCP server metacomputer at this checkout; run: codex mcp remove metacomputer, then ./install.sh --only-codex"
          CODEX_FAILED=1
        fi
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
  # What metacomputer relies on in this Codex (lib/agents.py check): an update to Codex that changes it shows here.
  local chk
  chk="$("$CPY" "$ROOT/lib/agents.py" check --quiet 2>&1 || echo "__check_failed__")"
  case "$chk" in
    *__check_failed__*) printf '%s\n' "${chk%__check_failed__}" | sed '/^$/d'
                        warn "this Codex works differently from the one metacomputer was checked with (above; docs/codex.md)" ;;
    *) ok "checked: Codex's rules checker, sandbox, MCP settings and session files work as metacomputer expects" ;;
  esac
  agent_record codex "$CODEX_TRIED" "$CODEX_FAILED"
}
keep_cmd_for() { # WHOSE ("Claude Code's", "Codex's"): an MCP server metacomputer that stays runs, or may run, the
  # command links, so --uninstall keeps them
  KEEP_CMD=1
  case "$KEEP_WHY" in "") KEEP_WHY="$1" ;; *"$1"*) ;; *) KEEP_WHY="$KEEP_WHY and $1" ;; esac
}
# --uninstall, before the command links are removed: Codex's MCP server metacomputer when it's this checkout's (ok or
# ours: its command this checkout's launcher, a path earlier versions registered under it, or ~/.local/bin/metacomputer
# while that is this checkout's; exactly ["mcp-lazy"]), read again right before its removal, and the removal checked.
# Anything else stays (said, by its program's name only). One that stays and runs ~/.local/bin/metacomputer or mcomp
# keeps those links (KEEP_CMD).
codex_server_remove() {
  has_codex || return 0
  codex_python || { warn "Codex left alone: the uninstall's Codex step needs Python 3.11 or later"; return 0; }
  local own what sum cmd lnk now
  IFS=$US read -r own what sum cmd lnk <<<"$(codex_server)"
  case "$own" in
    absent) return 0 ;;
    error) warn "couldn't read Codex's MCP server metacomputer ($what), so it was left as it is;"
           warn "run ./install.sh --uninstall again, or remove it yourself: codex mcp remove metacomputer"
           UNINSTALL_FAILED=1; keep_cmd_for "Codex's"; return 0 ;;
    ok|ours) if [ "$cmd" = "$CMD_LINK" ] && ! link_points_here "$CMD_LINK" "$ROOT/bin/metacomputer"; then own=other; fi ;;
  esac
  if [ "$own" != ok ] && [ "$own" != ours ]; then
    echo "  Codex's MCP server metacomputer stays: it runs $what, which isn't this checkout's"
    [ "$lnk" != link ] || { keep_cmd_for "Codex's"; }
    return 0
  fi
  if ! codex_unchanged "$sum"; then
    warn "Codex's MCP server metacomputer changed since it was read, so it was left as it is;"
    warn "run ./install.sh --uninstall again, or remove it yourself: codex mcp remove metacomputer"
    UNINSTALL_FAILED=1; keep_cmd_for "Codex's"; return 0
  fi
  codex mcp remove metacomputer >/dev/null 2>&1 || true
  IFS=$US read -r now _ _ _ _ <<<"$(codex_server)"
  if [ "$now" = absent ]; then ok "removed Codex's metacomputer MCP server"
  else
    warn "couldn't remove Codex's metacomputer MCP server (or confirm it's gone); remove it yourself: codex mcp remove metacomputer"
    UNINSTALL_FAILED=1; keep_cmd_for "Codex's"
  fi
}
codex_remove() { # --uninstall, after the command links: the skill links and rules that are this checkout's
  has_codex || return 0
  local name link
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
  step "Removing what $ROOT set up for Claude Code and Codex"
  # Claude Code's user-scope MCP server, read before anything is removed (the command link it runs among them). It is
  # this checkout's when it is ~/.local/bin/metacomputer mcp-lazy while that link is this checkout's, or this checkout's
  # own launcher ("ours": lib/claude_mcp.py). Anything else stays (said), and so do project and local servers. A lookup
  # that fails stops the uninstall before it changes anything; a removal that doesn't take ends it with exit status 1.
  # It goes first, while the command it runs is still there: read again right before its removal, it stays when it changed
  # meanwhile. Codex's goes next, the same way. The command links (~/.local/bin/metacomputer, mcomp) stay whenever an
  # entry that stays, Claude Code's or Codex's, owned or not, runs them (or may: changed, or not confirmed gone), so that
  # no entry is left running a program that's gone.
  srv_ours=0; UNINSTALL_FAILED=0; KEEP_CMD=0; KEEP_WHY=""; srv_words=""; srv_sum=""; srv_linked=""
  if has_claude; then
    IFS=$US read -r srv_own _ srv_words srv_sum srv_linked <<<"$(user_server metacomputer)"
    case "$srv_own" in
      error) die "couldn't read Claude Code's user-scope MCP server metacomputer ($srv_words), so nothing was removed." \
                 "Run ./install.sh --uninstall again, or remove it yourself: claude mcp remove metacomputer -s user" ;;
      ok) if link_points_here "$CMD_LINK" "$ROOT/bin/metacomputer"; then srv_ours=1; fi ;;
      ours) srv_ours=1 ;;
    esac
  fi
  if [ "$srv_ours" = 1 ] && ! entry_unchanged metacomputer "$srv_sum"; then
    warn "Claude Code's metacomputer MCP server changed since it was read (another Claude Code?), so it was left as it is;"
    warn "run ./install.sh --uninstall again, or remove it yourself: claude mcp remove metacomputer -s user"
    UNINSTALL_FAILED=1; keep_cmd_for "Claude Code's"
  elif [ "$srv_ours" = 1 ]; then
    claude mcp remove metacomputer -s user >/dev/null 2>&1 || true
    IFS=$US read -r srv_own _ _ _ <<<"$(user_server metacomputer)"
    if [ "$srv_own" = absent ]; then ok "removed Claude Code's metacomputer MCP server"
    else warn "couldn't remove Claude Code's metacomputer MCP server (or confirm it's gone); remove it yourself: claude mcp remove metacomputer -s user"
         UNINSTALL_FAILED=1; keep_cmd_for "Claude Code's"; fi
  elif [ -n "$srv_words" ] && [ "${srv_own:-absent}" != absent ]; then
    echo "  Claude Code's user-scope MCP server metacomputer stays: it runs $srv_words, which isn't this checkout's"
    [ "$srv_linked" != link ] || { keep_cmd_for "Claude Code's"; }
  fi
  codex_server_remove
  for l in "$CMD_LINK" "$ALIAS_LINK"; do
    if ! link_points_here "$l" "$ROOT/bin/metacomputer"; then :
    elif [ "$KEEP_CMD" = 1 ]; then warn "kept $l: an MCP server metacomputer that stays ($KEEP_WHY) runs, or may run, this command"
    else rm "$l" && ok "removed $l"; fi
  done
  link_points_here "$SKILL_LINK" "$ROOT/skills/metacomputer" && rm "$SKILL_LINK" && ok "removed the metacomputer skill"
  [ -f "$HOME/.local/share/metacomputer/bin/mcomp-vault" ] && rm "$HOME/.local/share/metacomputer/bin/mcomp-vault" \
    && ok "removed the secrets vault program (your vault file stays: ~/Library/Application Support/metacomputer)"
  if has_claude; then shadow_note; fi
  # The plugin: from this checkout's marketplace, or from GitHub (its copy runs this checkout
  # through ~/.local/bin/metacomputer, removed above, so it can't work without it).
  if has_plugin_cli; then
    case "$(plugin_owner)" in
      ours) remove_plugin || UNINSTALL_FAILED=1 ;;
      error) warn "couldn't read Claude Code's plugin marketplaces (claude plugin marketplace list --json), so whether the plugin"
             warn "$PLUGIN_ID is this checkout's is unknown; left as it is (if it is: claude plugin uninstall $PLUGIN_ID --scope user)"
             UNINSTALL_FAILED=1 ;;
    esac
  fi
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
  [ "$UNINSTALL_FAILED" = 0 ] || { printf '\n%s\n' "${y}Not everything was removed: the lines marked ! above say what's left and how to remove it.${o}" >&2; exit 1; }
  exit 0
fi

# --- the Claude Code step (4; --only-claude runs it alone) ------------------------------------
# Plugin mode: this checkout is the marketplace `metacomputer`, and its plugin (plugin/: copies of
# skills/metacomputer and the driver pack, /metacomputer:learn, and a launcher that runs ~/.local/bin/metacomputer)
# loads in place from here. The plugin declares no MCP server and no hooks (Anthropic's plugin directory holds a plugin
# for review when one runs a script it can't follow); the MCP server is registered here, at user scope, in both modes.
plugin_install() { # 0 = installed and on, 2 = installed but turned off (left so), 1 = failed, 3 = refused, 4 = another
  # checkout's marketplace whose plugin is on or not installed (left for plugin_switch, once the server is registered and
  # checked), 5 = Claude Code's marketplaces or plugins couldn't be read, or the plugin's record isn't one this knows
  # (everything left as it is)
  MKT_FOUND="$(mkt_lookup)"
  case "$MKT_FOUND" in
    error)
      warn "couldn't read Claude Code's plugin marketplaces (claude plugin marketplace list --json), so the plugin was left as it is"
      return 5 ;;
    "directory${TAB}$ROOT") ok "marketplace $MKT: this checkout (already added)" ;;
    directory"$TAB"*)
      # another checkout's: its plugin is read first. One turned off stays off, and so does the rest of Claude Code's
      # setup (as with this checkout's); one that can't be read stops the switch.
      case "$(plugin_state)" in
        on|absent) return 4 ;;
        off) warn "the plugin $PLUGIN_ID, from the marketplace $MKT (another checkout's), is installed but turned off; left so"
             warn "(to turn it on: claude plugin enable $PLUGIN_ID)"; return 2 ;;
        *) warn "couldn't read the installed plugins (claude plugin list --json), or the plugin's record there isn't one this"
           warn "knows, so the marketplace $MKT (another checkout's) and its plugin were left as they are"; return 5 ;;
      esac ;;
    absent)
      claude plugin marketplace add "$ROOT" >/dev/null 2>&1 \
        || { warn "could not add this checkout as a marketplace (claude plugin marketplace add $ROOT)"; return 1; }
      ok "marketplace $MKT: this checkout" ;;
    *)
      if ! mkt_is_canonical "$MKT_FOUND"; then
        warn "a marketplace named $MKT from another source is already registered: it's neither this checkout nor"
        warn "github.com/$CANON_REPO, so its plugin is NOT installed: its code would run on this Mac. If it isn't"
        warn "yours, remove it (claude plugin marketplace remove $MKT) and run ./install.sh --only-claude again."
        return 3
      fi
      ok "marketplace $MKT: github.com/$CANON_REPO (its plugin runs this checkout through $CMD_LINK)" ;;
  esac
  plugin_enable
}
plugin_enable() { # the plugin from the marketplace there now: 0 = on, 2 = turned off (left so), 1 = failed, 5 = unreadable
  case "$(plugin_state)" in
    on) ok "plugin $PLUGIN_ID already installed" ;;
    off) warn "the plugin $PLUGIN_ID is installed but turned off; left so (to turn it on: claude plugin enable $PLUGIN_ID)"; return 2 ;;
    absent)
      claude plugin install "$PLUGIN_ID" --scope user >/dev/null 2>&1 \
        || { warn "could not install the plugin (claude plugin install $PLUGIN_ID --scope user)"; return 1; }
      ok "plugin $PLUGIN_ID installed for every project: the skills metacomputer and cua-driver, /metacomputer:learn and the metacomputer command" ;;
    *) warn "couldn't read the installed plugins (claude plugin list --json), or the plugin's record there isn't one this"
       warn "knows (it may have failed to load), so the plugin $PLUGIN_ID was left as it is"
       return 5 ;;
  esac
}
# Another checkout's marketplace named metacomputer (from an install there, with its plugin on or not installed: read by
# plugin_install): its plugin may be what serves the tools now (a copy from before brings its own server), so
# claude_step calls this only once the user-scope server is registered and checked. Removing that marketplace takes its
# plugin with it; each step is checked. 0 = this checkout's plugin is on, 2 = installed but turned off, 1 = this
# checkout's couldn't be added (the old one is gone), 5 = the plugins couldn't be read, 6 = the old marketplace couldn't
# be removed, or whether it was can't be read (it stays).
plugin_switch() {
  claude plugin marketplace remove "$MKT" >/dev/null 2>&1 || true
  case "$(mkt_lookup)" in
    absent) ok "removed the marketplace $MKT, which was another checkout's" ;;
    error) warn "couldn't read Claude Code's marketplaces after removing $MKT (another checkout's), so whether it's gone is"
           warn "unknown (claude plugin marketplace list; to remove it: claude plugin marketplace remove $MKT)"; return 6 ;;
    *) warn "couldn't remove the marketplace $MKT (another checkout's), so it and its plugin stay"
       warn "(claude plugin marketplace remove $MKT, then ./install.sh --only-claude)"; return 6 ;;
  esac
  claude plugin marketplace add "$ROOT" >/dev/null 2>&1 \
    || { warn "could not add this checkout as a marketplace (claude plugin marketplace add $ROOT)"; return 1; }
  ok "marketplace $MKT: this checkout"
  plugin_enable
}
skill_link() { # without the plugin: the skill as a link into this checkout
  mkdir -p "$HOME/.claude/skills"
  if [ -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "$SKILL_LINK exists and isn't a link; left alone (move it away and run this again to install the skill)"
  else
    ln -sfn "$ROOT/skills/metacomputer" "$SKILL_LINK" && ok "skill: metacomputer"
  fi
}
# Claude Code's MCP server `metacomputer`, at user scope (every project): ~/.local/bin/metacomputer mcp-lazy, so it runs
# the checkout the command runs (--switch moves both). Only metacomputer's own registration is ever removed or rewritten
# (user_server's OWNER: canonical command, exact arguments): this checkout's is repaired, another checkout's moves here
# with --switch or a yes at a terminal. Anything else is left exactly as it is: the run names it (its program and how
# many arguments it has, or its transport; never its arguments, variables or URL), says how to remove it yourself, and
# the step counts as unfinished. metacomputer's own is read again right before it's removed and left as it is when it
# changed since; once the removal is done, the new one is always added and read back, and when that doesn't work the run
# says the server is missing and the command that adds it (nothing of yours is lost: that command makes it again).
# Returns 0 once the server is registered as it should be (read back and checked), else 1.
register_server() {
  if ! has_claude; then
    warn "Claude Code's 'claude' command isn't installed; after installing it, run: ./install.sh --only-claude"
    return 0
  fi
  local own kind what sum now nwhat removed="" add_cmd
  add_cmd="claude mcp add -s user metacomputer $(tildify "$CMD_LINK") mcp-lazy"
  REG_LEFT=""
  IFS=$US read -r own kind what sum _ <<<"$(user_server metacomputer)"
  case "$own" in
    ok)
      case "$what" in *", with variables of its own") what="; your variables on it are kept" ;; *) what="" ;; esac
      ok "MCP server metacomputer already registered for every project (tools: mcp__metacomputer__*$what)"
      shadow_note; return 0 ;;
    error)
      warn "couldn't read Claude Code's user-scope MCP server metacomputer ($what), so it was left as it is;"
      warn "run ./install.sh --only-claude again"
      REG_LEFT=unknown; return 1 ;;
    checkout)
      if [ "$SWITCH" = 1 ] \
         || ask "Claude Code's MCP server metacomputer runs another checkout: $what. Point it at this one?" N; then :
      else
        warn "Claude Code's MCP server metacomputer runs another checkout: $what; left as it is"
        warn "(to point it at this one: ./install.sh --only-claude --switch)"
        REG_LEFT=other; return 1
      fi ;;
    foreign)
      warn "Claude Code already has a user-scope MCP server named metacomputer that isn't metacomputer's (it runs $what);"
      warn "it was left as it is. To use metacomputer's instead: claude mcp remove metacomputer -s user, then ./install.sh --only-claude"
      REG_LEFT=other; return 1 ;;
  esac
  # absent, or metacomputer's own to repair or move here
  if [ "$own" != absent ]; then
    # still exactly the entry classified above, its variables included? (another Claude Code may have changed it
    # meanwhile, or while you answered)
    if ! entry_unchanged metacomputer "$sum"; then
      warn "Claude Code's MCP server metacomputer changed since it was read (another Claude Code?), so it was left as it is;"
      warn "run ./install.sh --only-claude again"
      REG_LEFT=other; return 1
    fi
    claude mcp remove metacomputer -s user >/dev/null 2>&1 || true
    IFS=$US read -r now _ _ _ <<<"$(user_server metacomputer)"
    case "$now" in
      absent|error) removed=" (the registration that ran $what was removed)" ;;  # gone, or can't be told now: the add goes ahead
      *) warn "couldn't remove the MCP server metacomputer that runs $what (metacomputer's own), so it was left as it is"
         REG_LEFT=other; return 1 ;;
    esac
  fi
  claude mcp add -s user metacomputer "$CMD_LINK" mcp-lazy >/dev/null 2>&1 || warn "could not register the MCP server ($add_cmd failed)"
  IFS=$US read -r now _ nwhat _ <<<"$(user_server metacomputer)"
  case "$now" in
    ok)
      SERVER_ADDED=1
      ok "MCP server metacomputer registered for every project: $(tildify "$CMD_LINK") mcp-lazy (tools: mcp__metacomputer__*, there even while the VM is off)"
      [ "$own" = absent ] || ok "(it replaces the registration that ran $what)"
      shadow_note; return 0 ;;
    absent)
      warn "Claude Code has no MCP server metacomputer now, so no metacomputer tools$removed. To add it:"
      warn "$add_cmd (or run ./install.sh --only-claude again)"
      REG_LEFT=none ;;
    error)
      warn "couldn't read Claude Code's MCP server metacomputer back ($nwhat), so whether it's there is unknown$removed."
      warn "If claude mcp get metacomputer shows none, add it: $add_cmd (or run ./install.sh --only-claude again)"
      REG_LEFT=unknown ;;
    *)
      warn "after the add, Claude Code's MCP server metacomputer runs $nwhat, not $(tildify "$CMD_LINK") mcp-lazy$removed;"
      warn "left as it is (run ./install.sh --only-claude again)"
      REG_LEFT=other ;;
  esac
  return 1
}
# After a registration that didn't work (register_server), when the plugin brings no MCP server of its own either (this
# checkout's plugin declares none; an update moves the checkout, and so the plugin's declaration, before install.sh
# runs): say plainly that new Claude Code sessions have no metacomputer tools until that's fixed, the line that fixes it,
# and that `metacomputer update` tries again (the step is recorded as unfinished).
tools_missing_note() {
  local plug=absent add_cmd
  add_cmd="claude mcp add -s user metacomputer $(tildify "$CMD_LINK") mcp-lazy"
  case "$REG_LEFT" in none|unknown) ;; *) return 0 ;; esac
  if has_plugin_cli; then plug="$(plugin_servers_state)"; fi
  [ "$plug" != present ] || return 0
  if [ "$REG_LEFT" = none ] && [ "$plug" = absent ]; then
    warn "metacomputer's tools are missing in new Claude Code sessions until this is fixed: no MCP server metacomputer is"
    warn "registered, and the plugin no longer brings one. To fix it: $add_cmd"
  else
    warn "metacomputer's tools may be missing in new Claude Code sessions until this is fixed: whether an MCP server"
    warn "metacomputer is registered, or the plugin brings one, can't be read. If claude mcp get metacomputer shows none: $add_cmd"
  fi
  warn "(metacomputer update tries this step again)"
}
# The server names earlier versions registered (cua-linux, virtual-computer): retired only once metacomputer's own
# server is registered and checked, and only when they're this checkout's ("ours"); each is read again right before its
# removal, and each removal is checked. One whose owner can't be read stays, and counts as not done.
retire_old_servers() { # 0 = none of this checkout's is left, and each name was read
  local old own sum rc=0
  has_claude || return 0
  for old in cua-linux virtual-computer; do
    IFS=$US read -r own _ _ sum _ <<<"$(user_server "$old")"
    case "$own" in
      ok|ours) ;;
      error) warn "couldn't read the old MCP server name $old, so whether it's this checkout's (the tools listed twice)"
             warn "is unknown; left as it is (run ./install.sh --only-claude again)"; rc=1; continue ;;
      *) continue ;;
    esac
    if ! entry_unchanged "$old" "$sum"; then
      warn "the old MCP server name $old changed since it was read, so it was left as it is (run ./install.sh --only-claude again)"
      rc=1; continue
    fi
    claude mcp remove "$old" -s user >/dev/null 2>&1 || true
    IFS=$US read -r own _ _ _ <<<"$(user_server "$old")"
    if [ "$own" = absent ]; then ok "removed the old MCP server name $old (now metacomputer)"
    else warn "couldn't remove the old MCP server name $old (claude mcp remove $old -s user)"; rc=1; fi
  done
  return "$rc"
}
# Runs only once the user-scope server is registered and checked. A copy of the plugin from before it left the MCP server
# to this step (installed from GitHub, not updated since) still brings its own, so Claude Code would list the tools
# twice: update it from the project's own repository, then read the plugin list again. 0 = it brings no server (read).
plugin_server_check() {
  local st upd=0
  st="$(plugin_servers_state)"
  case "$st" in
    absent) return 0 ;;
    error)
      warn "couldn't read the installed plugins (claude plugin list --json), so whether $PLUGIN_ID still brings an MCP"
      warn "server of its own is unknown; if Claude Code lists the tools twice: claude plugin update $PLUGIN_ID"
      return 1 ;;
  esac
  if mkt_is_canonical "$(mkt_lookup)"; then
    if claude plugin marketplace update "$MKT" </dev/null >/dev/null 2>&1 \
       && claude plugin update "$PLUGIN_ID" --scope user </dev/null >/dev/null 2>&1; then upd=1; fi
  fi
  st="$(plugin_servers_state)"
  case "$st" in
    absent)
      ok "plugin $PLUGIN_ID$([ "$upd" = 0 ] || echo " updated"): it no longer brings an MCP server of its own"
      return 0 ;;
    present)
      warn "the plugin $PLUGIN_ID here is an older copy that brings its own MCP server, so Claude Code lists the tools twice"
      warn "(mcp__metacomputer__* and mcp__plugin_metacomputer_metacomputer__*) until it's updated: claude plugin update $PLUGIN_ID" ;;
    *)
      warn "couldn't read the installed plugins after updating $PLUGIN_ID (claude plugin list --json), so whether it still"
      warn "brings an MCP server of its own is unknown; if Claude Code lists the tools twice: claude plugin update $PLUGIN_ID" ;;
  esac
  return 1
}
claude_step() {
  link_command
  # set up this run only with a claude command (without one, nothing of Claude Code's is read or changed): its result is
  # recorded at the end (agent_record), before anything else runs
  if has_claude; then CLAUDE_TRIED=1; fi
  # The skill name used by earlier versions, virtual-computer (their server names: retire_old_servers, after the server)
  local old rc=0 sw=0
  old="$HOME/.claude/skills/virtual-computer"
  if [ -L "$old" ] && old_target "$(readlink "$old")"; then rm -f "${old:?}" && ok "removed the old name $old"; fi
  if [ "$PLUGIN" = 1 ] && has_plugin_cli; then
    plugin_install || rc=$?
    case "$rc" in
      0) remove_skill_link
         # what served the tools before (an older plugin copy's own server, old server names) is retired only once the
         # user-scope server is there and checked
         if register_server; then
           retire_old_servers || CLAUDE_FAILED=1
           plugin_server_check || CLAUDE_FAILED=1
         else
           CLAUDE_FAILED=1
           [ "$(plugin_servers_state)" != present ] \
             || warn "the plugin's own MCP server (an older copy of the plugin) was left in place, so the tools are still there"
           tools_missing_note
         fi ;;
      4) # another checkout's marketplace, whose plugin may be what serves the tools now: replaced by this checkout's only
         # once the user-scope server is there and checked
         if register_server; then
           plugin_switch || sw=$?
           case "$sw" in
             0) remove_skill_link; plugin_server_check || CLAUDE_FAILED=1 ;;
             2) ;;
             1) warn "using a skill link instead of the plugin (as ./install.sh --no-plugin does)"; skill_link ;;
             *) CLAUDE_FAILED=1 ;;
           esac
           retire_old_servers || CLAUDE_FAILED=1
         else
           CLAUDE_FAILED=1
           warn "the marketplace $MKT (another checkout's) and its plugin were left in place until the MCP server is"
           warn "registered: an older copy of the plugin may be what serves the tools now"
           tools_missing_note
         fi ;;
      2) # turned off: left so, and so is everything else of Claude Code's
         case "$(user_server metacomputer)" in "absent$US"*)
           warn "so the MCP server metacomputer isn't added either (after claude plugin enable $PLUGIN_ID: ./install.sh --only-claude)" ;;
         esac ;;
      5) # Claude Code's marketplaces or plugins can't be read, or the plugin's record isn't one this knows: nothing of
         # Claude Code's is changed (a plugin turned off may be among them), and the step counts as unfinished
         CLAUDE_FAILED=1 ;;
      *) warn "using a skill link instead of the plugin (as ./install.sh --no-plugin does)"; skill_link
         if register_server; then retire_old_servers || CLAUDE_FAILED=1; else CLAUDE_FAILED=1; tools_missing_note; fi ;;
    esac
  else
    if [ "$PLUGIN" = 1 ] && has_claude; then
      warn "this Claude Code has no 'claude plugin' command (update Claude Code for the plugin); using a skill link instead"
    fi
    skill_link
    if register_server; then
      retire_old_servers || CLAUDE_FAILED=1
      # --no-plugin after the plugin: the skill link brings the skill instead (not listed twice). Removed only now that
      # the user-scope server is checked, since an older plugin copy may be what serves the tools until then; another
      # source's plugin isn't this install's to remove, and a plugin whose source can't be read stays (not done).
      if [ "$PLUGIN" = 0 ] && has_plugin_cli; then
        case "$(plugin_state):$(plugin_owner)" in
          absent:*|on:other|off:other) ;;
          on:ours|off:ours) remove_plugin || CLAUDE_FAILED=1 ;;
          *) warn "couldn't read Claude Code's plugins or marketplaces (claude plugin list --json), so the plugin $PLUGIN_ID"
             warn "was left as it is (if it's this checkout's: claude plugin uninstall $PLUGIN_ID --scope user)"; CLAUDE_FAILED=1 ;;
        esac
      fi
    else
      CLAUDE_FAILED=1
      if [ "$PLUGIN" = 0 ] && has_plugin_cli && [ "$(plugin_state)" != absent ]; then
        warn "the plugin $PLUGIN_ID stays until the MCP server is registered (it may be what serves the tools now)"
      fi
      tools_missing_note
    fi
  fi
  agent_record claude "$CLAUDE_TRIED" "$CLAUDE_FAILED"
}
# Whether an agent's step finished (claude: Claude Code's, codex: Codex's MCP server), kept in installed.json's
# "incomplete" (what `metacomputer update` reads): recorded by the step itself as its last action, so Claude Code's is
# recorded before Codex's step runs (which could end the run), and only when the step ran (TRIED): a run that skipped it
# (no claude or codex command, --no-claude, --no-codex) leaves an earlier unfinished one recorded. So an update runs the
# install again after a step that didn't finish (whichever way it ran), a run whose step finishes clears that, and a
# full install's last step follows the same rule (install_incomplete). Without installed.json, nothing's installed yet:
# update installs anyway.
agent_record() { # NAME TRIED FAILED
  local stamp="$HOME/.local/share/metacomputer/installed.json" how=done
  [ "$2" = 1 ] || return 0
  [ "$3" = 0 ] || how=failed
  [ -f "$stamp" ] || return 0
  python3 - "$stamp" "$1" "$how" 2>/dev/null <<'PY' || warn "couldn't record in $(tildify "$stamp") whether the $1 setup finished"
import json, os, sys, tempfile
path, name, how = sys.argv[1:4]
with open(path, encoding="utf-8") as f:
    d = json.load(f)
if not isinstance(d, dict):
    sys.exit(1)
was = d.get("incomplete") if isinstance(d.get("incomplete"), list) else []
now = [x for x in was if x != name] + ([name] if how == "failed" else [])
if now:
    d["incomplete"] = now
else:
    d.pop("incomplete", None)
fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
PY
}
stamp_incomplete() { # NAME: 0 when installed.json records NAME's step as unfinished
  python3 - "$HOME/.local/share/metacomputer/installed.json" "$1" 2>/dev/null <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if isinstance(d, dict) and isinstance(d.get("incomplete"), list) and sys.argv[2] in d["incomplete"] else 1)
PY
}
install_incomplete() { # what a full install's last step records as unfinished, as "claude", "codex" or "claude,codex": a
  # step that failed this run, or wasn't set up this run while installed.json already records it as unfinished
  local out=""
  if [ "$CLAUDE_FAILED" = 1 ] || { [ "$CLAUDE_TRIED" = 0 ] && stamp_incomplete claude; }; then out=claude; fi
  if [ "$CODEX_FAILED" = 1 ] || { [ "$CODEX_TRIED" = 0 ] && stamp_incomplete codex; }; then out="${out:+$out,}codex"; fi
  printf '%s\n' "$out"
}
agents_failed_exit() { # an agent's step didn't finish: say so, and end the run with exit status 1
  local which=""
  [ "$CLAUDE_FAILED" = 0 ] || which="Claude Code's"
  [ "$CODEX_FAILED" = 0 ] || which="${which:+$which and }Codex's"
  [ -n "$which" ] || return 0
  printf '\n%s\n' "${y}$which setup didn't finish: the lines marked ! above say why and what to run.${o}" >&2
  exit 1
}
claude_reload_note() { # how open Claude Code sessions pick up this run's changes
  if [ "$SERVER_ADDED" = 1 ]; then
    printf '%s' "Restart open Claude Code sessions (or run /mcp in them) to load metacomputer's tools, added in this run."
  else
    printf '%s' "Restart open Claude Code sessions (or run /reload-plugins in them) to pick this up."
  fi
}
# --- an install that's already here: another checkout's (switching needs a yes), or a newer one (downgrading needs --allow-downgrade)
stamp_field() { sed -n "s/^ *\"$1\": \"\\([^\"]*\\)\".*/\\1/p" "$HOME/.local/share/metacomputer/installed.json" 2>/dev/null | head -1 || true; }
existing_checks() { # main | claude: the plan's lines go into todo only for main
  local here other="" tgt od s_dir s_commit s_tag this_tag head
  here="$(real_dir "$ROOT")"
  if [ -L "$CMD_LINK" ]; then      # every run links the command, whichever agents it sets up
    tgt="$(resolve_link "$CMD_LINK")"
    case "$tgt" in */bin/metacomputer)
      od="$(real_dir "$(dirname "$tgt")/..")"
      if [ -n "$od" ] && [ "$od" != "$here" ] && is_checkout "$od"; then other="$od"; fi ;; esac
  fi
  if [ -n "$other" ]; then
    if [ "$SWITCH" = 1 ]; then
      [ "$1" != main ] || todo+=("switch the metacomputer command and Claude Code's plugin and MCP server from $(tildify "$other") to this checkout (--switch)")
    elif [ "$YES" = 1 ] || [ ! -t 0 ]; then
      die "the metacomputer command runs another checkout, $(tildify "$other"), and this run would switch it (and Claude Code's plugin and MCP server) to $(tildify "$ROOT")." \
          "To update that install instead: metacomputer update." \
          "To move to this checkout: ./install.sh --switch (that checkout's VM, settings and vault are left as they are)."
    elif ask "The metacomputer command runs another checkout, $(tildify "$other"). Switch it and Claude Code's plugin and MCP server to this one?" N; then
      [ "$1" != main ] || todo+=("switch the metacomputer command and Claude Code's plugin and MCP server from $(tildify "$other") to this checkout")
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
  step "Claude Code"
  CLAUDE=yes; agent_remember claude on
  claude_step
  printf '\n%s\n' "$(claude_reload_note)"
  agents_failed_exit
  exit 0
fi
if [ "$ONLY_CODEX" = 1 ]; then
  existing_checks claude
  step "Codex"
  CODEX=yes; agent_remember codex on
  codex_step
  printf '\n%s\n' "Start a new Codex session to pick this up."
  agents_failed_exit
  exit 0
fi
if [ "$ONLY_AGENTS" = 1 ]; then
  existing_checks claude
  step "Claude Code and Codex"
  agents_stage
  if [ "$SERVER_ADDED" = 1 ]; then
    printf '\n%s\n' "Start new sessions to pick this up (in an open Claude Code one, /mcp loads metacomputer's tools, added in this run)."
  else
    printf '\n%s\n' "Start new sessions (or run /reload-plugins in Claude Code) to pick this up."
  fi
  agents_failed_exit
  exit 0
fi

# --- 1. this Mac, what's already done, and the plan --------------------------------------
STAGES=7
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
if claude_wanted && { has_claude || [ "$CLAUDE" = yes ]; }; then
  todo+=("link the metacomputer command and set Claude Code up: the plugin (skills) and metacomputer's MCP server (tools), in every project; --no-claude leaves Claude Code alone")
else
  todo+=("link the metacomputer command")
fi
if codex_wanted; then
  todo+=("add metacomputer to Codex: its MCP server, the skills in ~/.agents/skills, and rules that let metacomputer commands run outside Codex's sandbox (--no-codex leaves Codex alone)")
fi
if ! has_claude && ! has_codex; then
  todo+=("note: neither Claude Code nor Codex is installed; metacomputer works from either (install one, then ./install.sh --only-agents)")
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

# --- 5. the agents: Claude Code and Codex -------------------------------------------------
step "Claude Code and Codex"
agents_stage      # each agent's result is recorded by its own step (agent_record), Claude Code's before Codex's runs

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
In Claude Code ($([ "$SERVER_ADDED" = 1 ] && echo "restart open sessions, or run /mcp in them, to load the tools added in this run" || echo "restart open sessions, or run /reload-plugins in them")), try:
  "Get yourself a virtual computer and tell me what's on its screen."
EOF
# What's running now, against this checkout: services on older code, a VM on an earlier gateway, a vault to bind
# again, a rebuild that would add something, and the version installed (lib/update.py; it never fails the install).
# An agent's step that didn't finish is recorded as such, so the next `metacomputer update` runs install.sh again.
incomplete="$(install_incomplete)"
if [ -f "$ROOT/lib/update.py" ] && { [ -n "${PY311:-}" ] || py311_ok; }; then
  MCOMP_INSTALL_INCOMPLETE="$incomplete" "$PY311" "$ROOT/lib/update.py" _after-install || true
fi
agents_failed_exit     # everything else is done; an agent's step that didn't finish still fails the run
