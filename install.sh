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
#   agent installed on this Mac (Claude Code, Codex, OpenClaw, Hermes Agent, Gemini CLI, Antigravity CLI), and for one
#   installed since
#   the last run:
#   ./install.sh --no-claude   leave Claude Code alone (plugin, skill, MCP server), this time and in later runs
#   ./install.sh --claude      set Claude Code up even if an earlier run had --no-claude
#   ./install.sh --no-plugin   Claude Code without the plugin: a skill link instead, and the same user-scope
#                              MCP server (tools: mcp__metacomputer__*)
#   ./install.sh --only-claude only the Claude Code step (no tools, Lume, settings, vault or VM)
#   ./install.sh --no-codex    leave Codex alone, this time and in later runs (docs/codex.md)
#   ./install.sh --codex       set Codex up even if an earlier run had --no-codex
#   ./install.sh --only-codex  only the Codex step: its MCP server, the skills in ~/.agents/skills and metacomputer's
#                              rules for its sandbox (no tools, Lume, settings, vault or VM)
#   ./install.sh --no-openclaw, --no-hermes, --no-gemini, --no-antigravity   leave OpenClaw, Hermes Agent, Gemini CLI
#                              or Antigravity CLI alone, this time and in later runs (docs/openclaw.md, docs/hermes.md,
#                              docs/gemini.md, docs/antigravity.md)
#   ./install.sh --openclaw, --hermes, --gemini, --antigravity   set it up even if an earlier run said --no-...
#   ./install.sh --only-openclaw, --only-hermes, --only-gemini, --only-antigravity   only that agent's step: its MCP
#                              server (registered with its own CLI) and the skills where it reads them (no tools, Lume,
#                              settings, vault or VM)
#   ./install.sh --only-agents only the agents' steps, for whichever agents are installed (after installing one)
#   ./install.sh --uninstall   remove what this installed for the agents, and the command (the VM stays)
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
#      skills in ~/.agents/skills and $CODEX_HOME/rules/metacomputer.rules. With OpenClaw, Hermes Agent, Gemini CLI
#      or Antigravity CLI: its MCP server metacomputer, ~/.local/bin/metacomputer mcp-lazy (lib/agent_setup.py), and the
#      skills
#   6. builds the secrets vault, mcomp-vault (`metacomputer secrets`), with swiftc
#   7. builds the VM: about 1.5 GB of downloads, then a few minutes unattended
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
YES=0; CHROME=""; BUILD=""; UNINSTALL=0; LUME_CHOICE=""; REPLACE_VM=0; FRESH_SOURCES=0
SWITCH=0; ALLOW_DOWNGRADE=0; ONLY_AGENTS=0
# The command-line rules every metacomputer program follows (scripts/strictargs.sh): each option once, nothing else.
. "$ROOT/scripts/strictargs.sh"
# The coding agents (lib/agentkit/agents.sh, made from their manifests by scripts/gen-agents.py) and each one's own
# steps (coding-agents/NAME/setup.sh: functions only, ag_FLAG_HOOK), for the registered ones.
. "$ROOT/lib/agentkit/agents.sh" || { echo "install.sh: couldn't read lib/agentkit/agents.sh: this checkout is incomplete" >&2; exit 1; }
for mc_d in $MC_AGENTS; do
  if [ -f "$ROOT/coding-agents/$mc_d/setup.sh" ]; then . "$ROOT/coding-agents/$mc_d/setup.sh"; fi
done
# Each agent's switches: FLAG (auto, yes for --FLAG, no for --no-FLAG) and ONLY_FLAG (1 for --only-FLAG), by its flag
# in capitals (CLAUDE, ONLY_CLAUDE); its step's FLAG_TRIED and FLAG_FAILED (agent_record).
for mc_d in $MC_AGENT_FLAGS; do
  mc_u="$(printf '%s' "$mc_d" | tr '[:lower:]' '[:upper:]')"
  eval "$mc_u=auto; ONLY_$mc_u=0; ${mc_u}_TRIED=0; ${mc_u}_FAILED=0"
done
unset mc_d mc_u
agent_option() { # ARG: 0 when it's an agent's install option (--FLAG, --no-FLAG, --only-FLAG, or one of its own:
  # its setup.sh's ag_FLAG_option), which is now set
  local f u
  for f in $MC_AGENT_FLAGS; do
    u="$(printf '%s' "$f" | tr '[:lower:]' '[:upper:]')"
    case "$1" in
      "--$f") eval "$u=yes"; return 0 ;;
      "--no-$f") eval "$u=no"; return 0 ;;
      "--only-$f") eval "ONLY_$u=1"; return 0 ;;
    esac
    if declare -F "ag_${f}_option" >/dev/null && "ag_${f}_option" "$1"; then return 0; fi
  done
  return 1
}
SA_DEFER=1
if ! sa_check install.sh "./install.sh [OPTIONS]   (./install.sh --help lists them)" 0 0 "--yes -y --no-chrome --no-build \
       --replace-vm --fresh-sources --only-agents --uninstall --switch --allow-downgrade $MC_AGENT_INSTALL_FLAGS" "--lume=" "$@"; then
  sed -n '2,/^set -Eeuo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0
fi
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;; --no-chrome) CHROME=no ;; --no-build) BUILD=no ;; --replace-vm) REPLACE_VM=1 ;; --fresh-sources) FRESH_SOURCES=1 ;;
    --only-agents) ONLY_AGENTS=1 ;; --uninstall) UNINSTALL=1 ;;
    --switch) SWITCH=1 ;; --allow-downgrade) ALLOW_DOWNGRADE=1 ;;
    --lume=mcomp-vm|--lume=standard) LUME_CHOICE="${a#--lume=}" ;;
    --lume=secure) LUME_CHOICE=mcomp-vm ;;   # mcomp-vm's former name, lume-secure
    -h|--help) sed -n '2,/^set -Eeuo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) agent_option "$a" || { echo "unknown option: $a (see --help)" >&2; exit 2; } ;;
  esac
done
mc_only=0; mc_onlys=""
for mc_d in $MC_AGENT_FLAGS; do
  mc_u="$(printf '%s' "$mc_d" | tr '[:lower:]' '[:upper:]')"
  eval "mc_o=\$ONLY_$mc_u; mc_v=\$$mc_u"
  [ "$mc_o" = 0 ] || [ "$mc_v" != no ] || { echo "--only-$mc_d and --no-$mc_d together leave nothing to do" >&2; exit 2; }
  mc_only=$((mc_only + mc_o)); mc_onlys="${mc_onlys:+$mc_onlys, }--only-$mc_d"
done
[ "$((mc_only + ONLY_AGENTS))" -le 1 ] || { echo "$mc_onlys and --only-agents: one at a time (--only-agents does them all)" >&2; exit 2; }
unset mc_d mc_u mc_o mc_v mc_only mc_onlys
sa_parsed install.sh

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
TAB=$'\t'; US=$'\037'
CLAUDE_MCP=user    # this installer registers Claude Code's MCP server itself, at user scope; plugin/bin/metacomputer looks
                   # for this line (an install.sh without it relied on the plugin for the server)
# An agent's FLAG_FAILED is 1 when its setup didn't finish: the run ends with exit status 1, after everything else;
# FLAG_TRIED is 1 once this run set it up (its step, with its command there): only then is its result recorded
# (agent_record); a run that skipped it leaves an earlier unfinished one recorded.
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
CMD_LINK="$HOME/.local/bin/metacomputer"
ALIAS_LINK="$HOME/.local/bin/mcomp"   # short alias for typing

CPY=""                     # Python 3.11 or later (tomllib) for an agent's step: agent_python
agent_python() { # found as install.sh's py311_ok finds it (Homebrew's, then python3 on PATH; never Apple's 3.9)
  local p
  [ -z "$CPY" ] || return 0
  for p in "${MCOMP_BREW_PYTHON:-/opt/homebrew/bin/python3}" "$(command -v python3 2>/dev/null)"; do
    [ -n "$p" ] && [ -x "$p" ] && [ "$p" != /usr/bin/python3 ] || continue
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { CPY="$p"; return 0; }
  done
  return 1
}
# The agents: each one installed is set up, by a first install and by every later run (so one installed since gets
# set up by the next update), unless you said --no-NAME (an agent's flag), which is remembered until --NAME.
AGENTS_FILE="$HOME/.local/share/metacomputer/agents.json"
agent_choice() { # NAME: "on" (you asked for it), "off" (--no-NAME in an earlier run) or nothing
  [ -f "$AGENTS_FILE" ] || return 0
  sed -n -E "s/^ *\"$1\": *\"(on|off)\".*/\1/p" "$AGENTS_FILE" 2>/dev/null | head -1 || true
}
agent_remember() { # NAME on|off: an explicit --NAME / --no-NAME, for later runs
  local n v body="" sep=""
  for n in $MC_AGENT_FLAGS; do
    if [ "$n" = "$1" ]; then v="$2"; else v="$(agent_choice "$n")"; fi
    body="$body$sep  \"$n\": \"${v:-auto}\""; sep=$',\n'
  done
  mkdir -p "$(dirname "$AGENTS_FILE")"
  printf '{\n%s\n}\n' "$body" > "$AGENTS_FILE.tmp" && mv "$AGENTS_FILE.tmp" "$AGENTS_FILE"
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
  # Command names used by earlier versions (vc, cua-lab, virtual-computer, vcomp). An agent's own old names are its
  # step's: --no-FLAG leaves everything of that agent's alone.
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
  local a missing=""
  for a in $MC_AGENT_FLAGS; do
    if agent_wanted "$a"; then
      if [ "$(agent_flag "$a")" = yes ]; then agent_remember "$a" on; fi
      if declare -F "ag_${a}_stage" >/dev/null; then "ag_${a}_stage"; else agent_run_step "$a"; fi
    else
      if [ "$(agent_flag "$a")" = no ]; then agent_remember "$a" off; fi
      if [ "$(agent_flag "$a")" = no ] || [ "$(agent_choice "$a")" = off ]; then
        printf '%s\n' "  $(agent_label "$a"): left alone (--no-$a$([ "$(agent_flag "$a")" = no ] || echo " in an earlier run"); --$a sets it up)"
      elif [ "$(mc_agent "$a" missing)" = line ]; then
        printf '%s\n' "  $(agent_label "$a") isn't installed; once it is: ./install.sh --only-agents (or metacomputer update)"
      else
        missing="${missing:+$missing, }$(agent_label "$a")"
      fi
    fi
  done
  [ -z "$missing" ] || printf '%s\n' "  Not installed here: $missing; once one is: ./install.sh --only-agents (or metacomputer update)"
  if ! any_agent; then
    warn "no coding agent is installed ($MC_AGENT_LABELS_OR): metacomputer works from any of them. Install one, then: ./install.sh --only-agents"
  fi
  printf '%s\n' "  (metacomputer agents shows this any time)"
}
agent_run_step() { # FLAG: the agent's step (its setup.sh's ag_FLAG_step, else lib/agent_setup.py's: agent_step)
  if declare -F "ag_${1}_step" >/dev/null; then "ag_${1}_step"; else agent_step "$1"; fi
}
agent_hook_line() { # HOOK DEFAULT: what the first agent with ag_FLAG_HOOK says (a line), else DEFAULT
  local a
  for a in $MC_AGENT_FLAGS; do
    if declare -F "ag_${a}_$1" >/dev/null; then "ag_${a}_$1"; return 0; fi
  done
  printf '%s' "$2"
}
skill_link_ours() { # PATH: 0 when it's a link metacomputer made, one that points into this checkout's own skill folders
  # (skills/, .claude/skills/; a relative target read from the link's own folder): lib/agent_setup.py's rule, for every
  # agent. Anything else, a broken link or one into another folder of the same name included, isn't.
  [ -L "$1" ] && "$CPY" "$ROOT/lib/agent_setup.py" link-ours "$1" --root "$ROOT" >/dev/null 2>&1 </dev/null
}
keep_cmd_for() { # WHOSE (an agent's label, "Codex's"): an MCP server metacomputer that stays runs, or may run, the
  # command links, so --uninstall keeps them
  KEEP_CMD=1
  case "$KEEP_WHY" in "") KEEP_WHY="$1" ;; *"$1"*) ;; *) KEEP_WHY="$KEEP_WHY and $1" ;; esac
}
# --- the generic agents (their manifests' install steps "generic": coding-agents/NAME/setup.py) ----------------------
# Each one's MCP server metacomputer (~/.local/bin/metacomputer mcp-lazy), registered with the agent's own CLI, and the
# skills where the agent reads them: lib/agent_setup.py. Its MCP server follows the rule every agent's does: only
# metacomputer's own entry (exactly ~/.local/bin/metacomputer or this checkout's launcher, with exactly mcp-lazy) is
# ever changed or removed, read again right before; anything else is named by its program and left, and the step counts
# as unfinished (recorded by agent_record, so `metacomputer update` runs it again).
agent_label() { mc_agent "$1" label; echo; }                    # FLAG: the agent's name
agent_cmd() { mc_agent "$1" prog; echo; }                        # FLAG: the agent's command
agent_upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }
agent_var() { eval "printf '%s' \"\${$1:-}\""; }                # VARNAME: its value
agent_flag() { local v; v="$(agent_var "$(agent_upper "$1")")"; printf '%s' "${v:-auto}"; }   # auto, yes, no
has_agent() { command -v "$(agent_cmd "$1")" >/dev/null 2>&1; }   # NAME
any_agent() { # is any coding agent installed?
  local a
  for a in $MC_AGENT_FLAGS; do if has_agent "$a"; then return 0; fi; done
  return 1
}
agent_wanted() { # NAME: --NAME, or installed and not --no-NAME before (or the agent's own rule: ag_NAME_wanted)
  if declare -F "ag_${1}_wanted" >/dev/null; then "ag_${1}_wanted"; return; fi
  case "$(agent_flag "$1")" in yes) return 0 ;; no) return 1 ;; esac
  has_agent "$1" && [ "$(agent_choice "$1")" != off ]
}
agent_lines() { # agent_setup.py's lines on stdin: ok, warn, say; "keep" (uninstall) keeps the command links
  local kind text
  while IFS=$'\t' read -r kind text; do
    case "$kind" in ok) ok "$text" ;; warn) warn "$text" ;; say) printf '%s\n' "  $text" ;; keep) keep_cmd_for "$text" ;; esac
  done
}
agent_step() { # NAME: a generic agent's flag
  local up label out rc=0
  up="$(agent_upper "$1")"; label="$(agent_label "$1")"
  link_command
  printf '%s\n' "  $label:"
  if ! has_agent "$1"; then
    warn "$label's '$(agent_cmd "$1")' command isn't installed; after installing it, run: ./install.sh --only-$1"
    return 0
  fi
  if ! agent_python; then
    warn "the $label step needs Python 3.11 or later (brew install python), so $label was left alone; then: ./install.sh --only-$1"
    return 0
  fi
  eval "${up}_TRIED=1"
  # a failure is caught inside the substitution (with set -E, install.sh's ERR trap would fire in it otherwise)
  out="$("$CPY" "$ROOT/lib/agent_setup.py" install "$1" --root "$ROOT" --link "$CMD_LINK" 2>/dev/null </dev/null \
         || echo __step_unfinished__)"
  case "$out" in *__step_unfinished__*) rc=1; out="${out%__step_unfinished__}" ;; esac
  agent_lines <<<"$out"
  if [ "$rc" != 0 ]; then
    [ -n "${out//[[:space:]]/}" ] || warn "the $label step stopped; try ./install.sh --only-$1 again"
    eval "${up}_FAILED=1"
  fi
  agent_record "$1" "$(agent_var "${up}_TRIED")" "$(agent_var "${up}_FAILED")"
}
# --uninstall, before the command links: the agent's MCP server metacomputer when it's this checkout's (read again right
# before its removal, and checked after), and the skill links that point here. One that stays and runs
# ~/.local/bin/metacomputer or mcomp keeps those links (KEEP_CMD).
agent_config_mentions() { # NAME: 0 when a file of the agent's configuration may run metacomputer's command (it names
  # metacomputer or mcomp, can't be read or looked at, or includes other files: $include, !include): read without
  # Python, for an uninstall that can't run agent_setup.py
  local f g d
  # Every profile and location (profile folders by */: those it can't look into too, which a glob for the file inside
  # would leave out), whichever one the agent's CLI uses: the agent's own list (its setup.sh's ag_NAME_config_files)
  AG_CONFIG_FILES=()
  if declare -F "ag_${1}_config_files" >/dev/null; then "ag_${1}_config_files"; fi
  set -- ${AG_CONFIG_FILES[@]+"${AG_CONFIG_FILES[@]}"}
  for f in "$@"; do
    if ! [ -e "$f" ] && ! [ -L "$f" ]; then
      # Not there, or behind a folder that can't be searched or listed (a profiles/* pattern left as it was): the
      # nearest folder on its way that exists must be both, else it may be there.
      d="${f%/*}"
      while [ -n "$d" ] && ! [ -e "$d" ] && ! [ -L "$d" ]; do d="${d%/*}"; done
      [ -z "$d" ] || { [ -x "$d" ] && [ -r "$d" ]; } || return 0
      continue
    fi
    [ -r "$f" ] || return 0
    g=0; grep -q -F -e metacomputer -e mcomp -e '$include' -e '!include' "$f" 2>/dev/null || g=$?
    [ "$g" = 1 ] || return 0      # found, or grep couldn't read it all (2): may run it
  done
  return 1
}
agent_uninstall() { # NAME
  local label out rc=0
  label="$(agent_label "$1")"
  if ! agent_python; then
    if agent_config_mentions "$1"; then
      warn "$label's configuration may run metacomputer's command, and reading it needs Python 3.11 or later: it stays, and so does the command"
      keep_cmd_for "$label's"; UNINSTALL_FAILED=1
    fi
    return 0
  fi
  out="$("$CPY" "$ROOT/lib/agent_setup.py" uninstall "$1" --root "$ROOT" --link "$CMD_LINK" 2>/dev/null </dev/null \
         || echo __step_unfinished__)"
  case "$out" in *__step_unfinished__*) rc=1; out="${out%__step_unfinished__}" ;; esac
  agent_lines <<<"$out"
  if [ "$rc" != 0 ]; then     # unfinished, however it ended: its configuration may still run the command, which stays
    UNINSTALL_FAILED=1
    if [ -z "${out//[[:space:]]/}" ]; then warn "the uninstall's $label step stopped"; fi
    keep_cmd_for "$label's"
  fi
  return 0
}

# --- uninstall ---------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  step "Removing what $ROOT set up for the coding agents"
  UNINSTALL_FAILED=0; KEEP_CMD=0; KEEP_WHY=""
  # Each agent's MCP server first, while the command it runs is still there (its hook, or lib/agent_setup.py's step).
  for a in $MC_AGENT_FLAGS; do
    if declare -F "ag_${a}_uninstall_server" >/dev/null; then "ag_${a}_uninstall_server"
    elif [ "$(mc_agent "$a" steps)" = generic ]; then agent_uninstall "$a"; fi
  done
  for l in "$CMD_LINK" "$ALIAS_LINK"; do
    if ! link_points_here "$l" "$ROOT/bin/metacomputer"; then :
    elif [ "$KEEP_CMD" = 1 ]; then warn "kept $l: an MCP server metacomputer that stays ($KEEP_WHY) runs, or may run, this command"
    else rm "$l" && ok "removed $l"; fi
  done
  for a in $MC_AGENT_FLAGS; do if declare -F "ag_${a}_uninstall_after_links" >/dev/null; then "ag_${a}_uninstall_after_links"; fi; done
  [ -f "$HOME/.local/share/metacomputer/bin/mcomp-vault" ] && rm "$HOME/.local/share/metacomputer/bin/mcomp-vault" \
    && ok "removed the secrets vault program (your vault file stays: ~/Library/Application Support/metacomputer)"
  for a in $MC_AGENT_FLAGS; do if declare -F "ag_${a}_uninstall_rest" >/dev/null; then "ag_${a}_uninstall_rest"; fi; done
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

# Whether an agent's step finished (by its flag), kept in installed.json's "incomplete" (what `metacomputer update`
# reads): recorded by the step itself as its last action, so one agent's is recorded before the next one's step runs
# (which could end the run), and only when the step ran (TRIED): a run that skipped it (no command for it, --no-FLAG)
# leaves an earlier unfinished one recorded. So an update runs the
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
install_incomplete() { # what a full install's last step records as unfinished, the agents' flags ("a,b"): a
  # step that failed this run, or wasn't set up this run while installed.json already records it as unfinished
  local out="" a up
  for a in $MC_AGENT_FLAGS; do
    up="$(agent_upper "$a")"
    if [ "$(agent_var "${up}_FAILED")" = 1 ] || { [ "$(agent_var "${up}_TRIED")" = 0 ] && stamp_incomplete "$a"; }; then
      out="${out:+$out,}$a"
    fi
  done
  printf '%s\n' "$out"
}
agents_failed_exit() { # an agent's step didn't finish: say so, and end the run with exit status 1
  local which="" a
  for a in $MC_AGENT_FLAGS; do
    [ "$(agent_var "$(agent_upper "$a")_FAILED")" = 0 ] || which="${which:+$which and }$(agent_label "$a")'s"
  done
  [ -n "$which" ] || return 0
  printf '\n%s\n' "${y}$which setup didn't finish: the lines marked ! above say why and what to run.${o}" >&2
  exit 1
}
# --- an install that's already here: another checkout's (switching needs a yes), or a newer one (downgrading needs --allow-downgrade)
stamp_field() { sed -n "s/^ *\"$1\": \"\\([^\"]*\\)\".*/\\1/p" "$HOME/.local/share/metacomputer/installed.json" 2>/dev/null | head -1 || true; }
existing_checks() { # main | agent: the plan's lines go into todo only for main
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
      [ "$1" != main ] || todo+=("switch the metacomputer command${MC_AGENT_SWITCH_WORDS:+ and $MC_AGENT_SWITCH_WORDS} from $(tildify "$other") to this checkout (--switch)")
    elif [ "$YES" = 1 ] || [ ! -t 0 ]; then
      die "the metacomputer command runs another checkout, $(tildify "$other"), and this run would switch it${MC_AGENT_SWITCH_WORDS:+ (and $MC_AGENT_SWITCH_WORDS)} to $(tildify "$ROOT")." \
          "To update that install instead: metacomputer update." \
          "To move to this checkout: ./install.sh --switch (that checkout's VM, settings and vault are left as they are)."
    elif ask "The metacomputer command runs another checkout, $(tildify "$other"). Switch it${MC_AGENT_SWITCH_WORDS:+ and $MC_AGENT_SWITCH_WORDS} to this one?" N; then
      [ "$1" != main ] || todo+=("switch the metacomputer command${MC_AGENT_SWITCH_WORDS:+ and $MC_AGENT_SWITCH_WORDS} from $(tildify "$other") to this checkout")
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
for a in $MC_AGENT_FLAGS; do       # --only-FLAG: that agent's step alone
  if [ "$(agent_var "ONLY_$(agent_upper "$a")")" = 1 ]; then
    existing_checks agent
    step "$(agent_label "$a")"
    eval "$(agent_upper "$a")=yes"; agent_remember "$a" on
    agent_run_step "$a"
    if declare -F "ag_${a}_only_note" >/dev/null; then printf '\n%s\n' "$("ag_${a}_only_note")"
    else printf '\n%s\n' "$(mc_agent "$a" only_note)"; fi
    agents_failed_exit
    exit 0
  fi
done
if [ "$ONLY_AGENTS" = 1 ]; then
  existing_checks agent
  step "Coding agents"
  agents_stage
  printf '\n%s\n' "$(agent_hook_line agents_note "Start new sessions to pick this up.")"
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
mc_linked=0                    # an agent's line that links the command too (ag_FLAG_plan_link), else a line of its own
for a in $MC_AGENT_FLAGS; do
  if [ "$mc_linked" = 0 ] && declare -F "ag_${a}_plan_link" >/dev/null && "ag_${a}_plan_link"; then mc_linked=1; fi
done
[ "$mc_linked" = 1 ] || todo+=("link the metacomputer command")
for a in $MC_AGENT_FLAGS; do
  if agent_wanted "$a"; then
    if declare -F "ag_${a}_plan" >/dev/null; then "ag_${a}_plan"
    elif [ "$(mc_agent "$a" steps)" = generic ]; then
      todo+=("add metacomputer to $(agent_label "$a"): its MCP server and the skills (--no-$a leaves it alone)")
    fi
  fi
done
if ! any_agent; then
  todo+=("note: no coding agent is installed ($MC_AGENT_LABELS_OR); metacomputer works from any of them (install one, then ./install.sh --only-agents)")
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

# --- 5. the agents (lib/agentkit/registry.py's, in its order) ------------------------------------------------------
step "Coding agents"
agents_stage      # each agent's result is recorded by its own step (agent_record), before the next one's runs

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
  # config/local.env's VM, whichever computer config/default-vm makes the default for other commands.
  MCOMP_NO_DEFAULT_VM=1 "$ROOT/bin/metacomputer" build "$@" || rc=$?
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
$(agent_hook_line done "Try:")
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
