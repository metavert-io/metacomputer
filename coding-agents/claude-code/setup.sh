# Claude Code's part of install.sh (sourced by it; defines functions only, ag_claude_HOOK, and its settings): the plugin
# (skills) or, before plugins and with --no-plugin, a skill link; and a user-scope MCP server (read by claude_mcp.py,
# beside this file, by lib/agentkit/mcp_entry.py's rule); and what --uninstall removes of them. Each runs in
# install.sh, with its helpers (ok, warn, ask, tildify, old_target, link_command, link_points_here, keep_cmd_for,
# agent_record).
MKT=metacomputer; PLUGIN_ID=metacomputer@metacomputer
PLUGIN=1           # --no-plugin: 0 (a skill link instead of the plugin, and the same server)
SERVER_ADDED=0     # 1 once this run registered Claude Code's MCP server (a restart, or /mcp, loads its tools)
REG_LEFT=""        # register_server's: what's left after a registration that didn't work: "none" (no server), "unknown"
                   # (it can't be read), or "other" (something that was there stays)
SKILL_LINK="$HOME/.claude/skills/metacomputer"
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
# Claude Code's user-scope MCP server NAME, read exactly (coding-agents/claude-code/claude_mcp.py: the entry in Claude Code's configuration,
# checked against what `claude mcp get` shows from a new empty folder, so no project or local server stands in for it,
# and read the same before and after that), on one line of fields split by $US: OWNER, KIND, WORDS, DIGEST.
# OWNER is what it is to this checkout (canonical command and exact arguments): "ok" (~/.local/bin/metacomputer
# mcp-lazy), "ours" (this checkout's own launcher, or a path earlier versions registered: repaired), "checkout" (another
# checkout's: --switch), "foreign" (anything else: never changed), "absent" or "error" (never read as absent: nothing is
# changed). WORDS says what it is, for messages: for one that isn't metacomputer's, only its program's name and how many
# arguments it has, or its transport (never its arguments, variables, URL or entry, which can carry credentials).
# DIGEST stands for the whole entry, for user_recheck.
user_server() { # NAME
  python3 "$ROOT/coding-agents/claude-code/claude_mcp.py" user-get "$1" --root "$ROOT" --link "$CMD_LINK" 2>/dev/null </dev/null \
    || printf 'error%serror%sclaude_mcp.py failed%s\n' "$US" "$US" "$US"
}
# Right before a user-scope entry is removed: read it again (from the configuration file alone, which is quick) and
# compare the whole entry, its variables included, with the one classified (DIGEST, from user_server): "same",
# "changed" (another Claude Code changed or removed it meanwhile: it's left as it is) or "error<US>WHY". Claude Code
# takes no lock this could share across a remove and an add, so a change in the moment between this read and
# `claude mcp remove` can still go unseen; only metacomputer's own entries are ever removed.
user_recheck() { # NAME DIGEST
  printf '%s\n' "$2" | python3 "$ROOT/coding-agents/claude-code/claude_mcp.py" user-recheck "$1" 2>/dev/null \
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
ag_claude_wanted() { # set Claude Code up this run: --claude, or auto and not --no-claude before (installed or not)
  case "$CLAUDE" in yes) return 0 ;; no) return 1 ;; esac
  [ "$(agent_choice claude)" != off ]
}
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
ag_claude_step() {
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
ag_claude_only_note() { # how open Claude Code sessions pick up this run's changes
  if [ "$SERVER_ADDED" = 1 ]; then
    printf '%s' "Restart open Claude Code sessions (or run /mcp in them) to load metacomputer's tools, added in this run."
  else
    printf '%s' "Restart open Claude Code sessions (or run /reload-plugins in them) to pick this up."
  fi
}
ag_claude_uninstall_server() { # --uninstall, before anything else is removed
  # Claude Code's user-scope MCP server, read before anything is removed (the command link it runs among them). It is
  # this checkout's when it is ~/.local/bin/metacomputer mcp-lazy while that link is this checkout's, or this checkout's
  # own launcher ("ours": claude_mcp.py, beside this file). Anything else stays (said), and so do project and local
  # servers. A lookup that fails stops the uninstall before it changes anything; a removal that doesn't take ends it
  # with exit status 1.
  # It goes first, while the command it runs is still there: read again right before its removal, it stays when it changed
  # meanwhile. Codex's goes next, the same way. The command links (~/.local/bin/metacomputer, mcomp) stay whenever an
  # entry that stays, Claude Code's or Codex's, owned or not, runs them (or may: changed, or not confirmed gone), so that
  # no entry is left running a program that's gone.
  local srv_ours=0 srv_own="" srv_words="" srv_sum="" srv_linked=""
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
}
ag_claude_uninstall_after_links() { # --uninstall, right after the command links: the skill link
  link_points_here "$SKILL_LINK" "$ROOT/skills/metacomputer" && rm "$SKILL_LINK" && ok "removed the metacomputer skill"
  return 0
}
ag_claude_uninstall_rest() { # --uninstall, after the vault's program
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
}
ag_claude_option() { # ARG: its own install option
  case "$1" in --no-plugin) PLUGIN=0; return 0 ;; esac
  return 1
}
ag_claude_stage() { # its part of the agents' step: its step when its command is there (or --claude), else said
  if has_claude || [ "$CLAUDE" = yes ]; then
    printf '%s\n' "  Claude Code:"
    ag_claude_step
  else
    printf '%s\n' "  Claude Code isn't installed; once it is: ./install.sh --only-agents (or metacomputer update)"
  fi
}
ag_claude_plan_link() { # its line in the plan, which links the command too; 1: none (the plan says the link alone)
  if ag_claude_wanted && { has_claude || [ "$CLAUDE" = yes ]; }; then
    todo+=("link the metacomputer command and set Claude Code up: the plugin (skills) and metacomputer's MCP server (tools), in every project; --no-claude leaves Claude Code alone")
    return 0
  fi
  return 1
}
ag_claude_agents_note() { # what --only-agents says last
  if [ "$SERVER_ADDED" = 1 ]; then
    printf '%s' "Start new sessions to pick this up (in an open Claude Code one, /mcp loads metacomputer's tools, added in this run)."
  else
    printf '%s' "Start new sessions (or run /reload-plugins in Claude Code) to pick this up."
  fi
}
ag_claude_done() { # the line before the example a full install ends with
  printf '%s' "In Claude Code ($([ "$SERVER_ADDED" = 1 ] && echo "restart open sessions, or run /mcp in them, to load the tools added in this run" || echo "restart open sessions, or run /reload-plugins in them")), try:"
}
