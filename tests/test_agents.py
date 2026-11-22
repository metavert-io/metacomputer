#!/usr/bin/env python3
"""`metacomputer agents` (lib/agents.py): which coding agents are set up, and the check of what metacomputer relies
on in Codex. No VM. The real check runs against this Mac's codex when it's installed (in a throwaway CODEX_HOME:
your Codex settings aren't touched); a stand-in codex that behaves differently must fail it. Claude Code's setup is
read from a stand-in claude: the plugin's skills and the user-scope MCP server (the tools), a skill link without the
plugin, a plugin with no server registered, and an older plugin copy that brings its own server (alone, or beside
the user-scope one: the tools listed twice).

  python3 tests/test_agents.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AG = os.path.join(LAB, "lib", "agents.py")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def run(args, env):
    return subprocess.run([sys.executable, AG, *args], capture_output=True, text=True, env=env, timeout=300,
                          stdin=subprocess.DEVNULL)


# A codex that answers but has changed: its rules checker allows everything, its sandbox sets no marker, and
# `codex mcp` doesn't work.
FAKE_CODEX = """#!/bin/sh
case "$1" in
  --version) echo "codex-cli 99.0.0" ;;
  execpolicy) echo '{"decision":"allow","matchedRules":[]}' ;;
  sandbox) while [ "$1" != "--" ] && [ $# -gt 0 ]; do shift; done; shift; exec env -u CODEX_SANDBOX "$@" ;;
  mcp) echo "unknown subcommand" >&2; exit 2 ;;
  *) exit 2 ;;
esac
"""


# A codex that answers in the right shape but has changed underneath: its rules checker gives the right decisions
# with a failing exit status, its sandbox sets no marker of its own (one inherited from outside must not count),
# `codex mcp` works, and its strict config loading rejects the approval setting.
FAKE_CODEX2 = """#!/bin/sh
case "$1" in
  --version) echo "codex-cli 98.0.0" ;;
  execpolicy) shift 3; case "$*" in
      "metacomputer status") d=allow ;; "metacomputer secrets unlock") d=forbidden ;; *) d=prompt ;; esac
    echo "{\\"decision\\":\\"$d\\",\\"matchedRules\\":[]}"; exit 1 ;;
  sandbox) while [ "$1" != "--" ] && [ $# -gt 0 ]; do shift; done; shift; exec "$@" ;;
  mcp) if [ "$2" = add ]; then printf '[mcp_servers.metacomputer]\\ncommand = "%s"\\n' "$5" > "$CODEX_HOME/config.toml"; echo "$5" > "$CODEX_HOME/cmd"
       else printf '{"transport":{"type":"stdio","command":"%s"},"tool_timeout_sec":600.0}\\n' "$(cat "$CODEX_HOME/cmd")"; fi ;;
  --strict-config) echo "Error loading config.toml: unknown configuration field default_tools_approval_mode" >&2; exit 1 ;;
  *) exit 2 ;;
esac
"""


# A claude that answers `plugin list --json` with $FAKE_PLUGINS and `mcp get metacomputer` with a user-scope server
# running $FAKE_MCP $FAKE_ARGS (default mcp-lazy); "No MCP server" on stderr without one, as the real one; FAKE_MCP_FAIL:
# the lookup fails.
FAKE_CLAUDE = """#!/bin/sh
case "$1 $2" in
  "--version ") echo "2.1.999 (Claude Code)" ;;
  "plugin list") printf '%s\\n' "${FAKE_PLUGINS:-[]}" ;;
  "mcp get") [ -z "${FAKE_MCP_FAIL:-}" ] || { echo "config unreadable" >&2; exit 2; }
             [ -n "${FAKE_MCP:-}" ] || { echo "No MCP server named \\"metacomputer\\"." >&2; exit 1; }
             printf 'metacomputer:\\n  Scope: User config (available in all your projects)\\n  Type: stdio\\n  Command: %s\\n  Args: %s\\n' \\
               "$FAKE_MCP" "${FAKE_ARGS-mcp-lazy}" ;;
  *) exit 2 ;;
esac
"""


def claude_cases(t, base):
    fake = os.path.join(t, "fakeclaude")
    os.makedirs(fake)
    with open(os.path.join(fake, "claude"), "w") as f:
        f.write(FAKE_CLAUDE)
    os.chmod(os.path.join(fake, "claude"), 0o755)
    cmd = os.path.join(base["HOME"], ".local", "bin", "metacomputer")
    plug = {"id": "metacomputer@metacomputer", "scope": "user", "enabled": True}
    old = dict(plug, mcpServers={"metacomputer": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/metacomputer"}})
    link = os.path.join(base["HOME"], ".claude", "skills", "metacomputer")

    def agents(plugins, mcp, *args, **extra):
        env = dict(base, PATH=fake + ":/usr/bin:/bin", FAKE_PLUGINS=json.dumps(plugins), FAKE_MCP=mcp, **extra)
        return run(list(args), env)
    fix = os.path.join(LAB, "install.sh") + " --only-claude"

    r = agents([plug], cmd, "--json")
    st = json.loads(r.stdout or "{}").get("claude_code", {})
    check("Claude Code with the plugin and the user-scope server: set up, the server's command reported, no plugin server",
          st.get("set_up") is True and st.get("plugin") == "on" and st.get("user_mcp_server") is True
          and st.get("mcp_command") == cmd and st.get("plugin_mcp_server") is False, st)
    r = agents([plug], cmd)
    check("...said as the plugin's skills and the MCP server metacomputer",
          "set up (the plugin's skills and the MCP server metacomputer)" in r.stdout, r.stdout)
    r = agents([plug], "")
    check("the plugin without the server: not set up (no tools), and this checkout's command that adds it",
          "no MCP server metacomputer (no tools)" in r.stdout and fix in r.stdout
          and json.loads(agents([plug], "", "--json").stdout)["claude_code"]["set_up"] is False, r.stdout)
    r = agents([plug], "/opt/other/bin/thing")
    check("a user-scope server that runs something else: no metacomputer tools, and what it runs",
          "runs /opt/other/bin/thing mcp-lazy, not ~/.local/bin/metacomputer mcp-lazy" in r.stdout and fix in r.stdout, r.stdout)
    r = agents([plug], cmd, FAKE_ARGS="status")
    check("metacomputer's command with other arguments: not counted as set up",
          f"runs {cmd} status" in r.stdout and json.loads(agents([plug], cmd, "--json", FAKE_ARGS="status").stdout)
          ["claude_code"]["set_up"] is False, r.stdout)
    r = agents([plug], cmd, FAKE_MCP_FAIL="1")
    check("a lookup that fails is said, not taken for no server", "couldn't be read" in r.stdout, r.stdout)
    r = agents([old], "")
    check("an older plugin copy that brings its own server: its tools counted, and how to move them to the user-scope server",
          "an older copy of the plugin" in r.stdout and fix in r.stdout, r.stdout)
    r = agents([old], cmd)
    check("an older plugin copy beside the user-scope server: says the tools are listed twice, and how to update it",
          "listed twice" in r.stdout and "claude plugin update metacomputer@metacomputer" in r.stdout, r.stdout)
    os.makedirs(os.path.dirname(link))
    os.symlink(os.path.join(t, "checkout", "skills", "metacomputer"), link)
    r = agents([], cmd)
    check("without the plugin (--no-plugin): a skill link and the MCP server metacomputer",
          "set up (a skill link and the MCP server metacomputer)" in r.stdout, r.stdout)
    os.remove(link)


def session_file(path, sid, mtime, *, calls=(), items=(), extra_lines=()):
    """A Codex rollout file: session_meta, the model's calls (response_item types), the recorded items
    (item_completed item types), any raw extra lines; its mtime set so the newest is known."""
    lines = [{"type": "session_meta", "payload": {"id": sid, "cwd": "/tmp/x", "cli_version": "0.167.0"}}]
    lines += [{"type": "response_item", "payload": {"type": c, "name": "exec", "call_id": f"c{i}"}} for i, c in enumerate(calls)]
    for i, kind in enumerate(items):
        item = ({"type": "McpToolCall", "id": f"m{i}", "server": "metacomputer", "tool": "list_windows", "arguments": {},
                 "status": "completed", "result": {"content": [{"type": "text", "text": "ok"}], "isError": False}}
                if kind == "McpToolCall" else {"type": kind, "id": f"i{i}"})
        lines.append({"type": "event_msg", "payload": {"type": "item_completed", "started_at_ms": 1000 + i,
                                                       "completed_at_ms": 1001 + i, "item": item}})
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
        for raw in extra_lines:
            f.write(raw + "\n")
    os.utime(path, (mtime, mtime))


def session_result(home_env, fake_bin):
    r = run(["check", "--json"], dict(home_env, PATH=fake_bin + ":/usr/bin:/bin"))
    d = {c["name"]: c for c in json.loads(r.stdout or "{}").get("checks", [])}
    return d.get("session files") or {}, r


def sessions_cases(t, base):
    """The session check reads only regular files with limits, counts only the calls learn reads, and fails when
    sessions show the model calling tools but the items it records have changed. Runs against the stand-in codex
    (its other checks fail; only the session item is looked at)."""
    import time
    fake_bin = os.path.join(t, "fakebin")
    sess = os.path.join(base["CODEX_HOME"], "sessions", "2026", "11", "21")
    os.makedirs(sess, exist_ok=True)
    now = time.time()
    session_file(os.path.join(sess, "rollout-a.jsonl"), "s-a", now - 50, calls=["custom_tool_call"],
                 items=["McpToolCall"])
    session_file(os.path.join(sess, "rollout-b.jsonl"), "s-b", now - 10, calls=["web_search_call"],
                 extra_lines=['["response_item","function_call"]', '{"type": "event_msg", "payload": "x"}', "not json"])
    os.mkfifo(os.path.join(sess, "rollout-fifo.jsonl"))
    os.utime(os.path.join(sess, "rollout-fifo.jsonl"), (now - 5, now - 5))
    t0 = time.time()
    res, r = session_result(base, fake_bin)
    check("a session with only a web search, malformed lines and a FIFO among them: no false alarm, no crash, no "
          "wait (an older session's MCP calls are found and read)", res.get("ok") is True
          and "tool calls included" in res.get("detail", "") and time.time() - t0 < 60, (res, r.stderr[-300:]))
    for i, name in enumerate("cde"):
        session_file(os.path.join(sess, f"rollout-{name}.jsonl"), f"s-{name}", now - 1 + i * 0.1,
                     calls=["custom_tool_call"], items=["McpToolCallV2", "AgentMessage"])
    res, r = session_result(base, fake_bin)
    check("three newest sessions where the model called tools but the items learn reads are renamed: the check fails "
          "and says Codex changed its format, though an older session still has the old items",
          res.get("ok") is False and "changed its session format" in res.get("detail", ""), res)
    shutil.rmtree(os.path.join(base["CODEX_HOME"], "sessions"))


def main():
    t = tempfile.mkdtemp(prefix="mcomp-agents-")
    try:
        home = os.path.join(t, "home")
        os.makedirs(os.path.join(home, ".codex"))
        base = {"HOME": home, "CODEX_HOME": os.path.join(home, ".codex"), "LANG": "en_US.UTF-8"}

        print("status")
        env = dict(base, PATH="/usr/bin:/bin")
        r = run(["--json"], env)
        st = json.loads(r.stdout or "{}")
        check("with neither agent on PATH: both reported as not installed", r.returncode == 0
              and st.get("claude_code", {}).get("installed") is False and st.get("codex", {}).get("installed") is False, r.stdout)
        r = run([], env)
        check("...and said plainly, with how to set one up later", "isn't installed" in r.stdout
              and "--only-agents" in r.stdout, r.stdout)
        os.makedirs(os.path.join(home, ".local", "share", "metacomputer"))
        json.dump({"claude": "auto", "codex": "off"}, open(os.path.join(home, ".local", "share", "metacomputer", "agents.json"), "w"))
        r = run(["--json"], env)
        check("your remembered choice (install.sh --no-codex) is reported", json.loads(r.stdout)["codex"].get("choice") == "off",
              r.stdout)

        print("Claude Code's setup")
        claude_cases(t, base)

        print("check")
        r = run(["check"], env)
        check("check without Codex: nothing to check, exit 0", r.returncode == 0 and "isn't installed" in r.stdout, r.stdout)
        fake_bin = os.path.join(t, "fakebin")
        os.makedirs(fake_bin)
        with open(os.path.join(fake_bin, "codex"), "w") as f:
            f.write(FAKE_CODEX)
        os.chmod(os.path.join(fake_bin, "codex"), 0o755)
        r = run(["check", "--json"], dict(base, PATH=fake_bin + ":/usr/bin:/bin"))
        res = {c["name"]: c["ok"] for c in json.loads(r.stdout or "{}").get("checks", [])}
        check("a Codex that changed fails the check (exit 1): its rules decisions, its sandbox marker, codex mcp",
              r.returncode == 1 and res.get("rules") is False and res.get("sandbox marker") is False
              and res.get("mcp config") is False, (r.returncode, res, r.stderr[-300:]))
        r = run(["check"], dict(base, PATH=fake_bin + ":/usr/bin:/bin"))
        check("...and says what changed and where to look", "changed something metacomputer relies on" in r.stdout
              and "docs/codex.md" in r.stdout, r.stdout)
        fake2 = os.path.join(t, "fakebin2")
        os.makedirs(fake2)
        with open(os.path.join(fake2, "codex"), "w") as f:
            f.write(FAKE_CODEX2)
        os.chmod(os.path.join(fake2, "codex"), 0o755)
        r = run(["check", "--json"], dict(base, PATH=fake2 + ":/usr/bin:/bin", CODEX_SANDBOX="seatbelt"))
        res = {c["name"]: c["ok"] for c in json.loads(r.stdout or "{}").get("checks", [])}
        check("right-looking answers aren't enough: a failing exit status, a sandbox marker only inherited from outside, "
              "and settings its strict config rejects all fail", r.returncode == 1 and res.get("rules") is False
              and res.get("sandbox marker") is False and res.get("mcp config") is True and res.get("mcp settings") is False,
              (res, r.stdout[-400:]))
        os.chmod(os.path.join(fake2, "codex"), 0o777)
        r = run(["check"], dict(base, PATH=fake2 + ":/usr/bin:/bin"))
        check("a codex other accounts can write isn't run", r.returncode == 1 and "writable by others" in r.stdout, r.stdout)
        os.chmod(os.path.join(fake2, "codex"), 0o755)
        r = subprocess.run([sys.executable, AG, "check", "--json"], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, cwd=fake2, env=dict(base, PATH=".:/usr/bin:/bin"))
        check("a codex found only through a relative PATH entry (a downloaded folder's own) isn't run",
              r.returncode == 0 and json.loads(r.stdout).get("codex") is None, r.stdout)
        print("session files (for learn)")
        sessions_cases(t, base)
        codex = shutil.which("codex")
        if codex:
            env = dict(base, PATH=os.path.dirname(codex) + ":/usr/bin:/bin")
            before = sorted(os.listdir(base["CODEX_HOME"]))
            r = run(["check", "--json"], env)
            d = json.loads(r.stdout or "{}")
            check(f"this Mac's Codex ({d.get('codex')}) passes every check", r.returncode == 0
                  and d.get("checks") and all(c["ok"] for c in d["checks"]), r.stdout[-600:])
            check("...without touching its Codex settings (a throwaway CODEX_HOME)",
                  sorted(os.listdir(base["CODEX_HOME"])) == before, os.listdir(base["CODEX_HOME"]))
        else:
            print("SKIP the real Codex check: no codex command on this Mac")
    finally:
        shutil.rmtree(t, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
