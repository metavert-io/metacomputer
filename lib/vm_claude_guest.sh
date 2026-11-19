#!/bin/bash
# The VM's half of `metacomputer claude` (lib/vm_claude.py is the Mac's half).
#
# It runs INSIDE the virtual computer as the desktop user, sent by the Mac through the shell channel
# (lib/vm_shell.py), as
#
#   vm_claude_guest.sh ACTION SESSION [ARGS...]
#
# ACTION is status, start, send, keys, read, wait, show or stop. The Mac checked every argument. Two values
# never come as arguments, so they are on no command line in the VM either: the token (start) and the
# prompt (send). They arrive in the environment (MCOMP_CLAUDE_TOKEN, MCOMP_CLAUDE_TEXT), and are moved into
# unexported variables and out of the environment first thing, so nothing started from here inherits them.
#
# Claude Code runs in a tmux session on a socket of its own (tmux -L metacomputer-claude). It gets the token
# through a file only its launcher reads (0600, in a 0700 folder), which the launcher deletes as it starts.
# Its first-run choices (theme, onboarding, trust of its folder, the API-key prompt) are set in ~/.claude.json.
#
# Output: lines that start with "@mc " are fields for the Mac ("@mc NAME VALUE"); a screen comes between
# "@mc screen-begin" and "@mc screen-end". Everything else goes to stderr. The Mac treats all of it as the
# VM's text: untrusted, checked and escaped before it is shown.
#
# Exit status: 0 done; 2 usage; 6 no such session, or Claude Code in it has ended; 7 no tmux in the VM;
# 8 Claude Code missing and not installable; 9 the session already runs; 10 a step failed;
# 124 the wait timed out.
set -u
umask 022    # what Claude Code and the programs it starts inherit; the token's file is written under 077

TOKEN="${MCOMP_CLAUDE_TOKEN:-}"
TEXT="${MCOMP_CLAUDE_TEXT:-}"
unset MCOMP_CLAUDE_TOKEN MCOMP_CLAUDE_TEXT

ACTION="${1:-}"
SESSION="${2:-}"
[ $# -ge 2 ] || { echo "usage: vm_claude_guest.sh ACTION SESSION [ARGS...]" >&2; exit 2; }
shift 2
case "$SESSION" in ""|*[!a-z0-9-]*|-*) echo "bad session name" >&2; exit 2 ;; esac

SOCK=metacomputer-claude
BASE="$HOME/.local/state/metacomputer/claude"
STATE="$BASE/$SESSION"
CONF="$BASE/tmux.conf"
export PATH="$HOME/.local/bin:$PATH"

field() { printf '@mc %s %s\n' "$1" "$2"; }

find_tmux() {
  TMUX_BIN=""
  if command -v tmux >/dev/null 2>&1; then TMUX_BIN="$(command -v tmux)"; return 0; fi
  return 1
}

T() { "$TMUX_BIN" -L "$SOCK" "$@"; }
exists() { T has-session -t "=$SESSION" 2>/dev/null; }
pane() { T display-message -p -t "=$SESSION:" "$1" 2>/dev/null; }
alive() { exists && [ "$(pane '#{pane_dead}')" = 0 ]; }

screen() { # LINES: the last LINES lines of the pane (its history included); runs of blank lines (the empty
           # rows of a tall pane) are shown as one, and trailing ones left out
  local n="$1" out
  out="$(T capture-pane -p -J -t "=$SESSION:" -S "-$n" 2>/dev/null)" || return 1
  echo "@mc screen-begin"
  printf '%s\n' "$out" | awk '
    { blank = ($0 ~ /^[ \t]*$/) }
    blank && prev_blank { next }
    { line[++n] = $0; prev_blank = blank }
    END { last = n; while (last > 0 && line[last] ~ /^[ \t]*$/) last--; for (i = 1; i <= last; i++) print line[i] }' | tail -n "$n"
  echo "@mc screen-end"
}

describe() { # fields for the Mac: the session's state
  if ! exists; then field state missing; return 0; fi
  if [ "$(pane '#{pane_dead}')" = 1 ]; then
    field state exited
    field exit "$(pane '#{pane_dead_status}')"
  else
    field state running
  fi
  field viewers "$(T display-message -p -t "=$SESSION" '#{session_attached}' 2>/dev/null)"
  field size "$(pane '#{window_width}x#{window_height}')"
}

wait_quiet() { # QUIET TIMEOUT: until the screen has not changed for QUIET seconds; 6 if it ended, 124 past TIMEOUT
  local quiet="$1" limit="$2" start now prev="" cur same
  start="$(date +%s)"; same="$start"
  while :; do
    alive || return 6
    cur="$(T capture-pane -p -t "=$SESSION:" 2>/dev/null | cksum)"
    now="$(date +%s)"
    if [ "$cur" != "$prev" ]; then prev="$cur"; same="$now"; fi
    [ $((now - same)) -ge "$quiet" ] && return 0
    [ $((now - start)) -ge "$limit" ] && return 124
    sleep 0.5
  done
}

show_terminal() { # AGAIN: a terminal on the desktop attached to the session (in the live view and recordings)
  local again="$1" n
  n="$(T display-message -p -t "=$SESSION" '#{session_attached}' 2>/dev/null)"
  if [ "${n:-0}" -gt 0 ] && [ "$again" != 1 ]; then field viewer already; return 0; fi
  [ -n "${DISPLAY:-}" ] || { field viewer no-desktop; return 10; }
  command -v xfce4-terminal >/dev/null 2>&1 || { field viewer no-terminal; return 10; }
  if command -v setsid >/dev/null 2>&1; then
    setsid -f xfce4-terminal --disable-server --maximize --title "Claude in the metacomputer ($SESSION)" \
      -x "$TMUX_BIN" -L "$SOCK" attach -t "=$SESSION" </dev/null >/dev/null 2>&1
  else
    (nohup xfce4-terminal --disable-server --maximize --title "Claude in the metacomputer ($SESSION)" \
      -x "$TMUX_BIN" -L "$SOCK" attach -t "=$SESSION" </dev/null >/dev/null 2>&1 &)
  fi
  field viewer opened
}

write_config() { # WORKDIR AUTH SKIP: the inner Claude's first-run choices, merged into what is there
  KEY_SUFFIX=""
  [ "$2" = api-key ] && KEY_SUFFIX="$(printf '%s' "$TOKEN" | tail -c 20)"
  MCOMP_KEY_SUFFIX="$KEY_SUFFIX" python3 -I - "$1" "$2" "$3" <<'PY'
import json, os, sys, tempfile, time
workdir, auth, skip = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
suffix = os.environ.pop("MCOMP_KEY_SUFFIX", "")
home = os.path.expanduser("~")

def load(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        try:
            os.replace(path, f"{path}.unreadable-{int(time.time())}")   # kept, not lost
        except OSError:
            pass
        return {}

def save(path, d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mcomp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

p = os.path.join(home, ".claude.json")
d = load(p)
d.setdefault("theme", "dark")
d["hasCompletedOnboarding"] = True
projects = d.get("projects") if isinstance(d.get("projects"), dict) else {}
entry = projects.get(workdir) if isinstance(projects.get(workdir), dict) else {}
entry["hasTrustDialogAccepted"] = True
entry["hasCompletedProjectOnboarding"] = True
projects[workdir] = entry
d["projects"] = projects
if auth == "api-key" and suffix:
    # Claude Code asks once whether to use an API key it finds in its environment, and remembers the answer
    # as the key's last 20 characters. The key here is the proxy token (it ends when the token does).
    r = d.get("customApiKeyResponses") if isinstance(d.get("customApiKeyResponses"), dict) else {}
    approved = [x for x in r.get("approved") or [] if isinstance(x, str)]
    rejected = [x for x in r.get("rejected") or [] if isinstance(x, str) and x != suffix]
    d["customApiKeyResponses"] = {"approved": approved + ([suffix] if suffix not in approved else []), "rejected": rejected}
if skip:
    d["bypassPermissionsModeAccepted"] = True          # older Claude Code
save(p, d)
if skip:
    # newer Claude Code: the warning shown when --dangerously-skip-permissions is used. It only skips
    # that dialog; runs without the flag still ask for permissions as usual.
    sp = os.path.join(home, ".claude", "settings.json")
    s = load(sp)
    s["skipDangerousModePermissionPrompt"] = True
    save(sp, s)
PY
}

write_launcher() {
  cat > "$STATE/launch.sh" <<'LAUNCH' || return 1
#!/bin/bash
# metacomputer claude: starts Claude Code in this tmux pane (written by lib/vm_claude_guest.sh).
# The token comes from ./env, which this reads once and deletes.
d="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$d/env" ]; then
  echo "metacomputer claude: no token here (each start's token is used once). Start it again from the Mac." >&2
  exit 78
fi
. "$d/env"
rm -f "$d/env"
export PATH="$HOME/.local/bin:$PATH"
cd "$1" || exit 78
shift
exec claude "$@"
LAUNCH
  chmod 700 "$STATE/launch.sh"
}

write_tmux_conf() {
  mkdir -p "$BASE" && chmod 700 "$BASE" "$HOME/.local/state/metacomputer" 2>/dev/null
  cat > "$CONF" <<'CONF'
# tmux for metacomputer claude (its own socket: tmux -L metacomputer-claude)
set -g remain-on-exit on
set -g history-limit 50000
set -s escape-time 10
set -g focus-events on
set -g mouse on
set -g status-left "[#S] "
set -g status-left-length 40
set -g status-right "Claude in the metacomputer"
CONF
}

PATH_MARK='# metacomputer: Claude Code (~/.local/bin)'
ensure_path_line() {
  grep -qF "$PATH_MARK" "$HOME/.bashrc" 2>/dev/null && return 0
  printf '\ncase ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac  %s\n' "$PATH_MARK" >> "$HOME/.bashrc"
}

ensure_claude() { # INSTALL(0|1)
  if command -v claude >/dev/null 2>&1; then return 0; fi
  [ "$1" = 1 ] || return 1
  local work
  work="$(mktemp -d)" || return 1
  field installing claude-code
  # Anthropic's native installer, as this user (into ~/.local/bin and ~/.local/share/claude). Downloaded whole
  # before it runs, so a cut-off download never runs half a script.
  if curl -fsSL --proto '=https' --tlsv1.2 --max-time 120 https://claude.ai/install.sh -o "$work/install.sh" \
     && bash "$work/install.sh" >&2 </dev/null; then :; fi
  rm -rf "$work"
  hash -r
  command -v claude >/dev/null 2>&1
}

case "$ACTION" in
  status)
    if find_tmux; then field tmux "$TMUX_BIN"; else field tmux none; fi
    if command -v claude >/dev/null 2>&1; then
      field claude "$(claude --version 2>/dev/null | head -1 | cut -c1-80)"
    else
      field claude none
    fi
    if [ -n "$TMUX_BIN" ]; then
      describe
      for s in $(T list-sessions -F '#{session_name}' 2>/dev/null); do field session "$s"; done
    else
      field state missing
    fi
    ;;

  start)
    # start SESSION WORKDIR AUTH BASE_URL SKIP(0|1) SHOW(0|1) INSTALL(0|1) -- CLAUDE_ARGS...
    [ $# -ge 7 ] || { echo "start: missing arguments" >&2; exit 2; }
    WORKDIR="$1"; AUTH="$2"; BASE_URL="$3"; SKIP="$4"; SHOW="$5"; INSTALL="$6"; shift 6
    [ "${1:-}" = -- ] && shift
    case "$AUTH" in oauth) VAR=CLAUDE_CODE_OAUTH_TOKEN ;; api-key) VAR=ANTHROPIC_API_KEY ;; *) exit 2 ;; esac
    [ -n "$TOKEN" ] || { echo "start: no token in the environment" >&2; exit 2; }
    case "$WORKDIR" in "~") WORKDIR="$HOME" ;; "~/"*) WORKDIR="$HOME/${WORKDIR#\~/}" ;; esac

    find_tmux || exit 7
    field tmux "$TMUX_BIN"
    if exists; then
      if alive; then field state running; exit 9; fi
      T kill-session -t "=$SESSION" 2>/dev/null       # an ended one is replaced
    fi
    ensure_claude "$INSTALL" || exit 8
    field claude "$(claude --version 2>/dev/null | head -1 | cut -c1-80)"
    ensure_path_line

    mkdir -p "$WORKDIR" || exit 10
    WORKDIR="$(cd "$WORKDIR" && pwd -P)" || exit 10
    field workdir "$WORKDIR"
    write_config "$WORKDIR" "$AUTH" "$SKIP" || exit 10

    rm -rf "$STATE"
    mkdir -p "$STATE" && chmod 700 "$STATE" || exit 10
    write_launcher || exit 10
    write_tmux_conf || exit 10
    # The token goes only into this file (0600, folder 0700), written by the shell itself (printf is a
    # builtin: no command line holds it). The launcher reads it once and deletes it.
    (
      umask 077
      {
        echo 'unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN'
        printf 'export ANTHROPIC_BASE_URL=%q\n' "$BASE_URL"
        printf 'export %s=%q\n' "$VAR" "$TOKEN"
        echo 'export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1'
      } > "$STATE/env"
    ) || exit 10
    TOKEN=""

    T -f "$CONF" new-session -d -s "$SESSION" -x 200 -y 50 -c "$WORKDIR" "$STATE/launch.sh" "$WORKDIR" "$@" \
      || { rm -f "$STATE/env"; exit 10; }
    # Ready: Claude Code drew something and then held still for 2 s (or it ended). At most 40 s.
    prev=""; still=0; i=0
    while [ $i -lt 80 ]; do
      i=$((i + 1))
      alive || break
      cur="$(T capture-pane -p -t "=$SESSION:" 2>/dev/null)"
      if [ -n "$(printf '%s' "$cur" | tr -d '[:space:]')" ] && [ "$cur" = "$prev" ]; then
        still=$((still + 1)); [ $still -ge 4 ] && break
      else
        still=0
      fi
      prev="$cur"
      sleep 0.5
    done
    if ! alive; then rm -f "$STATE/env"; fi
    [ "$SHOW" = 1 ] && alive && show_terminal 0
    describe
    screen 40
    alive || exit 6
    ;;

  send)
    # send SESSION ENTER(0|1) WAIT(0|1) QUIET TIMEOUT LINES; the text in MCOMP_CLAUDE_TEXT
    [ $# -eq 5 ] || exit 2
    find_tmux || exit 6
    alive || { describe; exit 6; }
    if [ -n "$TEXT" ]; then
      # A bracketed paste when Claude Code asks for one (it does): the text arrives whole, newlines included,
      # and nothing in it is taken as Enter. Then Enter on its own.
      printf '%s' "$TEXT" | T load-buffer -b "send-$SESSION" - || exit 10
      T paste-buffer -p -r -d -b "send-$SESSION" -t "=$SESSION:" || exit 10
      TEXT=""
      sleep 0.5
    fi
    if [ "$1" = 1 ]; then T send-keys -t "=$SESSION:" Enter || exit 10; fi
    field sent yes
    rc=0
    if [ "$2" = 1 ]; then
      sleep 1
      wait_quiet "$3" "$4"; rc=$?
    fi
    describe
    screen "$5"
    exit $rc
    ;;

  keys)
    find_tmux || exit 6
    alive || { describe; exit 6; }
    for k in "$@"; do
      case "$k" in
        [0-9a-zA-Z]) T send-keys -t "=$SESSION:" -l "$k" || exit 10 ;;
        *) T send-keys -t "=$SESSION:" "$k" || exit 10 ;;
      esac
      sleep 0.15
    done
    field sent yes
    ;;

  read)
    # read SESSION LINES
    [ $# -eq 1 ] || exit 2
    find_tmux || exit 6
    exists || { field state missing; exit 6; }
    describe
    screen "$1"
    ;;

  wait)
    # wait SESSION QUIET TIMEOUT LINES
    [ $# -eq 3 ] || exit 2
    find_tmux || exit 6
    exists || { field state missing; exit 6; }
    wait_quiet "$1" "$2"; rc=$?
    describe
    screen "$3"
    exit $rc
    ;;

  show)
    # show SESSION AGAIN(0|1)
    [ $# -eq 1 ] || exit 2
    find_tmux || exit 6
    alive || { describe; exit 6; }
    show_terminal "$1" || exit 10
    ;;

  stop)
    find_tmux || { field stopped no; rm -rf "$STATE"; exit 0; }
    if exists; then T kill-session -t "=$SESSION" 2>/dev/null; field stopped yes; else field stopped no; fi
    rm -rf "$STATE"
    ;;

  *) echo "unknown action" >&2; exit 2 ;;
esac
exit 0
