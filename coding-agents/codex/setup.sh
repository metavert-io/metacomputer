# Codex's part of install.sh (sourced by it; defines functions only, ag_codex_HOOK, and its settings): its MCP server
# metacomputer, the skills in ~/.agents/skills, metacomputer's rules for its sandbox (docs/codex.md), and what
# --uninstall removes of them. Each runs in install.sh, with its helpers (ok, warn, agent_python, skill_link_ours,
# link_command, keep_cmd_for, agent_record).
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_RULES="$CODEX_DIR/rules/metacomputer.rules"
CODEX_MARK="# metacomputer's rules for Codex. install.sh writes this file from config/codex/metacomputer.rules.in, and rewrites or"
CODEX_OWNER="# metacomputer checkout: "     # the rules file's 4th line names the checkout that wrote it
AGENT_SKILLS="$HOME/.agents/skills"
CODEX_TOOL_TIMEOUT=600     # seconds: Codex's default (60) is shorter than a long run_steps or a slowly typed text
# Codex asks before every MCP tool not marked read-only (in `codex exec`, it refuses them). metacomputer's tools act only
# on the isolated VM, through the hub's guards, so its server's tools are approved (docs/codex.md).
CODEX_APPROVAL=approve
has_codex() { command -v codex >/dev/null 2>&1; }
# Codex's MCP server named metacomputer, read whole (coding-agents/codex/setup.py codex-entry: `codex mcp get --json`),
# on one line of fields split by $US: OWNER, WORDS, DIGEST, COMMAND, LINKED. OWNER, by the rule Claude Code's follows: "ok" (a local
# command, exactly this checkout's bin/metacomputer or ~/.local/bin/metacomputer, with exactly ["mcp-lazy"]), "ours" (the
# same with a path earlier versions registered under this checkout's bin/: pointed at bin/metacomputer), "foreign"
# (anything else, a path that only starts with the checkout's included: never changed or removed), "absent" or "error"
# (never read as absent). WORDS names metacomputer's own by its command, anything else by its program and how many
# arguments it has, or its transport (never an argument, a variable or a URL). DIGEST stands for the whole entry, read
# again right before any change (codex_unchanged); COMMAND is given only for ok and ours; LINKED is "link" when it runs
# ~/.local/bin/metacomputer or mcomp themselves.
codex_server() {
  agent_python || { printf 'error%sthe Codex step needs Python 3.11 or later%s%s%s\n' "$US" "$US" "$US" "$US"; return 0; }
  "$CPY" "$ROOT/coding-agents/codex/setup.py" codex-entry --root "$ROOT" --link "$CMD_LINK" 2>/dev/null </dev/null \
    || printf 'error%scoding-agents/codex/setup.py codex-entry failed%s%s%s\n' "$US" "$US" "$US" "$US"
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
ag_codex_step() {
  link_command
  printf '%s\n' "  Codex:"
  if ! has_codex; then
    warn "Codex's 'codex' command isn't installed; after installing it, run: ./install.sh --only-codex"
    return 0
  fi
  if ! agent_python; then
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
    if skill_link_ours "$link"; then
      rm -f "${link:?}"
    elif [ -e "$link" ] || [ -L "$link" ]; then
      warn "~/.agents/skills/$name is already there and isn't metacomputer's (not a link into this checkout's skills); left"
      warn "alone, so Codex won't get the $name skill: move it away, then ./install.sh --only-codex"
      CODEX_FAILED=1; continue
    fi
    if ln -s "$target" "$link" 2>/dev/null; then ok "skill $name (~/.agents/skills, read by Codex)"
    else warn "couldn't link the $name skill into ~/.agents/skills; try ./install.sh --only-codex again"; CODEX_FAILED=1; fi
  done
  if [ -e "$CODEX_RULES" ] && ! codex_rules_ours; then
    warn "$CODEX_RULES is there and isn't this checkout's; left alone (docs/codex.md says what it should hold)"
  elif codex_rules_write; then
    ok "rules: everyday metacomputer commands run outside Codex's sandbox; changes to the VM ask you; user-only ones are refused"
  else
    warn "couldn't write $CODEX_RULES (Codex's checker didn't accept it); approve metacomputer commands when Codex asks instead"
  fi
  # What metacomputer relies on in this Codex (its setup.py check, which lib/agents.py check runs): an update to Codex
  # that changes it shows here.
  local chk
  chk="$("$CPY" "$ROOT/lib/agents.py" check --quiet 2>&1 || echo "__check_failed__")"
  case "$chk" in
    *__check_failed__*) printf '%s\n' "${chk%__check_failed__}" | sed '/^$/d'
                        warn "this Codex works differently from the one metacomputer was checked with (above; docs/codex.md)" ;;
    *) ok "checked: Codex's rules checker, sandbox, MCP settings and session files work as metacomputer expects" ;;
  esac
  agent_record codex "$CODEX_TRIED" "$CODEX_FAILED"
}
# --uninstall, before the command links are removed: Codex's MCP server metacomputer when it's this checkout's (ok or
# ours: its command this checkout's launcher, a path earlier versions registered under it, or ~/.local/bin/metacomputer
# while that is this checkout's; exactly ["mcp-lazy"]), read again right before its removal, and the removal checked.
# Anything else stays (said, by its program's name only). One that stays and runs ~/.local/bin/metacomputer or mcomp
# keeps those links (KEEP_CMD).
ag_codex_uninstall_server() {
  has_codex || return 0
  agent_python || { warn "Codex left alone: the uninstall's Codex step needs Python 3.11 or later"; return 0; }
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
ag_codex_uninstall_rest() { # --uninstall, after the command links: the skill links and rules that are this checkout's
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
ag_codex_plan() { # its line in the plan install.sh shows first
  todo+=("add metacomputer to Codex: its MCP server, the skills in ~/.agents/skills, and rules that let metacomputer commands run outside Codex's sandbox (--no-codex leaves Codex alone)")
}
