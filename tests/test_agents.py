#!/usr/bin/env python3
"""`metacomputer agents` (lib/agents.py): which coding agents are set up, and the check of what metacomputer relies
on in Codex. No VM. The real check runs against this Mac's codex when it's installed (in a throwaway CODEX_HOME:
your Codex settings aren't touched); a stand-in codex that behaves differently must fail it. Claude Code's setup is
read from a stand-in claude: the plugin's skills and the user-scope MCP server (the tools), a skill link without the
plugin, a plugin with no server registered, and an older plugin copy that brings its own server (alone, or beside
the user-scope one: the tools listed twice).

The agents' packages (coding-agents/README.md): each registered folder has what an agent's folder holds and a sound
manifest and recognition tables; what's made from the manifests is current (scripts/gen-agents.py --check: docs/agents.md,
lib/agentkit/agents.sh, CODEOWNERS); and no shared code names an agent (agent_words, against what's allowed: LINT_FILES,
PRINTED, LINT_REGIONS, LINT_WORDS and LINT_ALLOW, each for its reason).

  python3 tests/test_agents.py
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AG = os.path.join(LAB, "lib", "agents.py")
sys.path.insert(0, os.path.join(LAB, "lib"))
from agentkit import manifests, recognition, registry  # noqa: E402
sys.path.insert(0, os.path.join(LAB, "tests"))
import hostile_manifests  # noqa: E402  (every manifest value that reaches a generated file, tried with what breaks out)
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
# the lookup fails. The same server is in ~/.claude.json, where Claude Code keeps it (the test writes it), since
# agents reads the exact entry from there and checks it against `mcp get`.
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

    def agents(plugins, mcp, *args, argv=("mcp-lazy",), **extra):
        with open(os.path.join(base["HOME"], ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"metacomputer": {"type": "stdio", "command": mcp, "args": list(argv), "env": {}}}
                       if mcp else {}}, f)
        env = dict(base, PATH=fake + ":/usr/bin:/bin", FAKE_PLUGINS=json.dumps(plugins), FAKE_MCP=mcp,
                   FAKE_ARGS=" ".join(argv))
        env.update(extra)            # FAKE_ARGS here: what `mcp get` shows, against what the configuration holds
        return run(list(args), env)
    fix = os.path.join(LAB, "install.sh") + " --only-claude"

    r = agents([plug], cmd, "--json")
    st = json.loads(r.stdout or "{}").get("claude_code", {})
    check("Claude Code with the plugin and the user-scope server: set up, the server's command reported, no plugin server",
          st.get("set_up") is True and st.get("plugin") == "on" and st.get("user_mcp_server") is True
          and st.get("mcp_server") == "~/.local/bin/metacomputer mcp-lazy" and st.get("plugin_mcp_server") is False, st)
    r = agents([plug], cmd)
    check("...said as the plugin's skills and the MCP server metacomputer",
          "set up (the plugin's skills and the MCP server metacomputer)" in r.stdout, r.stdout)
    r = agents([plug], "")
    check("the plugin without the server: not set up (no tools), and this checkout's command that adds it",
          "no MCP server metacomputer (no tools)" in r.stdout and fix in r.stdout
          and json.loads(agents([plug], "", "--json").stdout)["claude_code"]["set_up"] is False, r.stdout)
    r = agents([plug], "/opt/other/bin/thing")
    check("a user-scope server that runs something else: no metacomputer tools, and what it is (its program, not its "
          "path or arguments)", "runs thing with 1 argument (not shown), not ~/.local/bin/metacomputer mcp-lazy" in r.stdout
          and "/opt/other" not in r.stdout and fix in r.stdout, r.stdout)
    for argv in (["status"], [" mcp-lazy"], ["mcp-lazy "], []):
        r = agents([plug], cmd, argv=argv)
        check(f"metacomputer's command with the arguments {argv}: not counted as set up (exact arguments compared)",
              "runs metacomputer" in r.stdout and "/.local/bin" not in r.stdout.split("not ~/.local")[0]
              and "no metacomputer tools" in r.stdout
              and json.loads(agents([plug], cmd, "--json", argv=argv).stdout)["claude_code"]["set_up"] is False, r.stdout)
    token = "SYNTHETIC-TOKEN-4f9c2e71d0"
    remote = ["-y", "mcp-remote", f"https://mcp.example.invalid/mcp?token={token}"]
    r, rj = agents([plug], "npx", argv=remote), agents([plug], "npx", "--json", argv=remote)
    check("a server whose arguments carry a token (npx mcp-remote URL?token=…): named by its program and how many "
          "arguments it has; the token is in neither the text nor the JSON",
          "runs npx with 3 arguments (not shown)" in r.stdout and token not in r.stdout + r.stderr + rj.stdout + rj.stderr
          and json.loads(rj.stdout)["claude_code"]["mcp_server"] == "npx with 3 arguments (not shown)", r.stdout + rj.stdout)
    r = agents([plug], cmd, FAKE_ARGS="other")
    check("the configuration and `claude mcp get` disagree: said as unreadable, not set up", "couldn't be read" in r.stdout,
          r.stdout)
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



# --- agent packages: their folders, their manifests, what's made from them, and shared code that names none -----------
# Where shared code is: everything the CLI, the scripts, the helpers and the bench harness run, and the event sinks'
# folders (sinks/; lib/agentkit, the framework, holds the registry and reads the folders; it's checked by its own rules
# below). Not in it: the agents' folders, tests (each agent's checks name it), docs and config (people's pages), the guest's side (provisioning/), the
# vault (vault/, Swift: it reads the agents' recognition tables, compiled in by scripts/build-vault.sh, and keeps its
# rules) and the distribution folders (plugin/, agent-plugin/, .agents/: made by scripts/sync-plugin.py from the
# agents' channel files, and kept byte for byte).
SHARED = ("bin", "scripts", "lib", "install.sh", "get.sh", "bench", "sinks")
# Words of a label that name no agent by themselves ("Claude Code" is named by "Claude", "Hermes Agent" by "Hermes").
GENERIC_WORDS = {"Code", "CLI", "Agent"}
# Names an agent is known by that its manifest doesn't hold: the variable Claude Code sets in what it runs.
EXTRA_WORDS = {"claude-code": ("CLAUDECODE",)}
# Whole files allowed, each for its reason.
LINT_FILES = {
    "lib/vm_claude.py": "`metacomputer claude`, Claude Code inside the VM: a feature of its own, named for the agent it "
                        "runs (its command, options, messages and records)",
    "lib/vm_claude_guest.sh": "the guest's half of `metacomputer claude` (as above)",
    "lib/mcp_hub.py": "the hub and its guards, which this refactor leaves unchanged (its words for the agent, and "
                      "owners.claude_of and the claim fields it calls by name)",
    "get.sh": "plugin/get.sh is its byte copy (plugin/ stays byte for byte), and it runs before a checkout exists, so "
              "it can't read the manifests",
}
# Text printed as it is (usage): each file's printed part may name agents, since a message stays word for word; a
# Python module's docstring when it prints __doc__ (only its first paragraph when that's all it prints), a shell
# script's header when it prints it (`sed -n '2,/^set -...`).
PRINTED = {
    "bin/metacomputer": "`metacomputer help` prints its header as it is",
    "install.sh": "`install.sh --help` prints its header as it is",
    "scripts/release.sh": "`release.sh --help` prints its header as it is",
    "lib/agents.py": "`metacomputer agents --help` prints its docstring's first paragraph",
    "lib/learn.py": "`metacomputer learn --help` prints its docstring",
    "lib/owners.py": "owners.py's usage is its docstring",
    "lib/sessionvm.py": "sessionvm.py's usage is its docstring",
    "lib/mcp_relay.py": "mcp_relay.py's usage is its docstring",
    "lib/mcp_lazy.py": "mcp_lazy.py's usage is its docstring",
    "bench/bench.py": "bench.py --help prints its docstring",
    "scripts/sync-plugin.py": "sync-plugin.py's usage is its docstring",
    "scripts/vendor-driver-skill.py": "vendor-driver-skill.py's usage is its docstring",
}
# Pieces allowed anywhere in shared code, each for its reason: taken out of a line before it's looked at.
LINT_WORDS = [
    ("self_claude_pid", "a field of `owners.py who --json`'s output: renaming it changes what the command prints"),
    ("claude_pid", "a field of the claims, the handoff record, events and the vault's records (older than the other "
                   "agents' support): renaming it changes their format"),
    ("claude_start", "a field of the claims and the handoff record, as above"),
    ("claude_session", "a value of the handoff record's ended_from, as above"),
    ("claude_of", "owners.py's nearest-agent lookup, which the hub (lib/mcp_hub.py, unchanged) calls by this name"),
    ("my_claude", "owners.others' parameter, which the hub (lib/mcp_hub.py, unchanged) passes by this name"),
    ('"declared_by": "claude"', "a needs_you event's field, as `metacomputer notify` and handoffs write it (the "
                                "events' format)"),
    ('origin="claude"', "an event's origin for what the agent declared (lib/events.py ORIGINS: the events' format)"),
    ("metacomputer claude", "the command that runs Claude Code inside the VM (lib/vm_claude.py), in messages and notes"),
    ("vm_claude", "the module of `metacomputer claude` (lib/vm_claude.py), and its test suite's name"),
    ("do_claude", "bin/metacomputer's function for `metacomputer claude`, as above"),
    ("When Claude needs you", "a heading of docs/guide.md, which these lines point at"),
    ("Hand back to Claude", "the handoff panel's button (lib/handback_panel.js), in the words people see"),
    ("draft-with-claude", "learn's option that drafts lessons with Claude Code (`claude -p`): an option people type"),
    ("draft_with_claude", "that option's value and function, as above"),
    ("CLAUDE.md", "the repository's guide for agents, by its file name"),
    (".claude/skills", "the repository's skill folder (.claude/skills/cua-driver), by its path"),
    ('".claude", "skills"', "the repository's skill folder, as above, joined in Python"),
    (".claude-plugin", "the plugin's own folder (plugin/.claude-plugin), by its path: plugin/ stays byte for byte"),
    (".codex-plugin", "a channel's folder in the plugin (plugin/.codex-plugin), by its path, as above"),
    ("not through Claude Code or Codex", "a message people see, kept word for word (the agents whose commands run where "
                                         "a person might)"),
    ("not through Claude Code, Codex or another agent", "a message people see, as above"),
    ("not through Claude Code, Codex, a script or a pipe", "a message people see, as above"),
]
# Lines allowed, by file and a piece of the line, each for its reason. An entry that matches nothing fails, so a line
# that's gone doesn't keep its permission.
LINT_ALLOW = [
    # `metacomputer claude` (lib/vm_claude.py): a command of its own, named for the agent it runs inside the VM
    ("bin/metacomputer", 'claude) echo "--session --token --ttl --model --cwd --file --timeout --quiet --lines" ;;',
     "`metacomputer claude`'s options for the strict parser: a command's name, not an agent's setup"),
    ("bin/metacomputer", "vm:help|claude:help|secrets:help) return 0 ;; esac",
     "`metacomputer claude help` is that command's own word for its usage, as above"),
    ("bin/metacomputer", "claude)          mod=vm_claude; margs=(--help) ;;", "`metacomputer claude --help`, as above"),
    ("bin/metacomputer", 'claude) case "${1:-}" in read|wait|status|_probe|_keeper', "`metacomputer claude`'s session-"
     "guard notice (its token is the vault's), as above"),
    ("bin/metacomputer", "# Claude Code inside the VM (lib/vm_claude.py): each step goes through",
     "the comment on `metacomputer claude`'s function, as above"),
    ("bin/metacomputer", 'claude)     do_claude "$@" ;;', "`metacomputer claude`'s dispatch, as above"),
    ("lib/owners.py", 'return (f"Claude Code inside the VM (metacomputer claude, session {s}), started by {src} in "',
     "how `owners.py who` describes `metacomputer claude`'s claim (a message people see)"),
    ("lib/owners.py", "f\"{'' if s == 'claude' else ' --session ' + s} ends it\")",
     "`metacomputer claude`'s default session name, in that message"),
    ("lib/sessionvm.py", "def _claude(a):         # lib/vm_claude.py:", "the session guard's rule for `metacomputer "
     "claude`'s arguments (which of its actions act on a computer)"),
    ("lib/sessionvm.py", '"claude": _claude,', "the session guard's table of commands: `metacomputer claude` is one"),
    ("lib/vm_shell.py", "16 MCOMP_CLAUDE_*, CLAUDE_* or ANTHROPIC_* names", "the guest shell's allowed environment names, "
     "how `metacomputer claude` hands its token over (a usage message, word for word)"),
    ("lib/vm_shell_guest.py", "names from an allowlist: MCOMP_CLAUDE_*,", "the guest shell's allowed environment names, as above"),
    ("lib/vm_shell_guest.py", "CLAUDE_*, ANTHROPIC_*; how `metacomputer claude` hands over a token without",
     "the guest shell's allowed environment names, as above"),
    ("lib/vm_shell_guest.py", 'ENV_NAME = re.compile(r"(?:MCOMP_CLAUDE|CLAUDE|ANTHROPIC)_', "the guest shell's allowed "
     "environment names, as above (a rule of the guest's, kept as it is)"),
    ("lib/vm_shell_guest.py", "16 MCOMP_CLAUDE_*, CLAUDE_* or ANTHROPIC_* names, text values without NUL",
     "the guest shell's allowed environment names, as above (its message)"),
    # learn's other features that run Claude Code itself
    ("lib/learn.py", '"claude-draft": "drafted by Claude from counts only"', "a lesson's source as --draft-with-claude "
     "records it (the lessons' format) and how `learn review` says it"),
    ("lib/learn.py", "headless Claude Code sessions on your own Claude account", "`learn bench` runs the benchmark with "
     "Claude Code, bench's default agent, on the user's account: what it costs, said before it starts"),
    ("lib/learn.py", 'die("it spends your Claude usage, so it asks first:', "`learn bench`'s refusal without a terminal, as above"),
    # words people see, kept word for word: the agent in a handoff, a notice or a message
    ("lib/handoff.py", "the handoff stays open (Claude's actions stay paused) until the user hands",
     "a message people see: the docs call the agent Claude (docs/guide.md, \"When Claude needs you\")"),
    ("lib/handoff.py", 'help="what Claude should know (at most 500 characters)"', "`handoff done --note`'s help, as above"),
    ("lib/handback_panel.js", "var who = String(argv[1] || 'Claude')", "the panel's \"Hand back to Claude\" when no "
     "agent's short name is passed (handoff.py passes the asker's)"),
    ("lib/handback_panel.js", "who = 'Claude';", "the same default for a name the panel won't show, as above"),
    ("lib/botcheck.py", 'return (f"Claude is stopped at {LABEL.get(kind,', "the needs_you.captcha notice people get, "
     "word for word (the docs call the agent Claude)"),
    ("lib/hidden_text.py", '"; the hub marked them for Claude"', "the hidden-text event's message, word for word, as above"),
    ("lib/events.py", '"claude": "[from Claude] "}', "how a notice the agent declared is marked for people (origin "
     "\"claude\": the events' format)"),
    ("lib/events.py", 'ORIGINS = ("cli", "bridge", "watcher", "spool", "script", "claude")', "the events' origins: "
     "\"claude\" is what the agent declared (`metacomputer notify`, handoffs), a value of the events' format"),
    ("scripts/lib.sh", "so Claude can't fill a login", "a message people see, word for word, as above"),
    ("scripts/vm-commands.sh", "a Claude Code session on it: docs/guide.md,", "`vm create`'s closing line, word for word: "
     "how to start a session on the new VM (the guide's section)"),
    ("lib/storage.py", '"Claude Code, Codex or a script (keys never go on a command line)")', "a message people see, "
     "word for word (where a key mustn't come from)"),
    ("bin/metacomputer", 'echo "codes:    $([ "${CODES_VIA:-vault}" = claude ] && echo "Claude may read and type one-time',
     "the setting CODES_VIA=claude (config/defaults.env) and `status`'s line about it: a setting's value people write"),
    ("install.sh", "CLAUDE_MCP=user    # this installer registers Claude Code's MCP server itself",
     "plugin/bin/metacomputer looks for this exact line in install.sh (plugin/ stays byte for byte)"),
    # the hub's protocol and records (lib/mcp_hub.py is left unchanged)
    ("lib/mcp_lazy.py", 'kind="claude-code"', "the hub's connection kind for the agents' bridge, which the hub "
     "(lib/mcp_hub.py, unchanged) and its records read"),
    ("lib/mcp_relay.py", 'via = ("hub" if "mcp_hub" in parent else "claude-code"', "a claim's \"via\" for the agents' "
     "bridge, as above (the claims' format)"),
    # the hidden-text guard's words: what a page may say to address an AI, not which agent runs
    ("lib/hidden_text.py", '_AI = r"(?:ai|a\\.i\\.|llms?|gpt', "the hidden-text guard's names of AI models a page may "
     "address (a guard rule, unchanged)"),
    ("lib/hidden_text.py", '("addresses_ai", re.compile(r"\\b(?:claude|chatgpt|gpt|gemini|copilot)', "the guard's "
     "pattern for a page addressing an AI by name, as above"),
    ("lib/hidden_text.py", "authorized_keys)|\\.claude\\.json|\\.claude/\\.credentials", "the guard's credential "
     "files a page may point at (Claude Code's among them), as above"),
    ("lib/hidden_text.py", 'TRIGGERS = ("ignore", "disregard", "forget",', "the guard's trigger words, as above"),
    ("lib/hidden_text.py", '"gemini", "copilot", "language model", "bot"', "the guard's trigger words, as above"),
    ("lib/hidden_text.py", '"claude.json", "credentials", ".netrc"', "the guard's trigger words, as above"),
    # Claude Code's environment variable, taken out for every agent alike
    ("bench/bench.py", 'env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}', "the variable Claude Code "
     "sets in what it runs, taken out of every agent's session so a bench run from a Claude Code session isn't nested"),
    ("scripts/release.sh", "env -u MCOMP_CONFIG -u CLAUDECODE MCOMP_SESSIONS_DIR=", "the same variable, taken out of "
     "the release's test runs (a release is often made from a Claude Code session)"),
    ("bench/metrics.py", '"permissionMode", "claude_code_version",', "a field of the stream-json init event, the "
     "events' format bench reads (every launcher's events are in it)"),
    # distribution: upstream's words and the files the channels keep
    ("scripts/build-openai-plugin.py", "for OpenAI's plugin directory (ChatGPT and Codex):", "what OpenAI's directory "
     "serves: its products' names, not an agent's setup"),
    ("scripts/vendor-driver-skill.py", "# The frontmatter's OpenClaw block: it asks for a cua-driver command",
     "why the vendored skill drops upstream's block for that host: upstream's text, read as it is"),
    ("scripts/vendor-driver-skill.py", "so OpenClaw would never offer the skill on the Mac.", "as above"
     " (the same comment's second line)"),
    ("scripts/vendor-driver-skill.py", 'r"^  openclaw:\\n(?: {4}.*\\n)+"', "the pattern for upstream's block, as above"),
    ("scripts/vendor-driver-skill.py", "This pack comes with metacomputer's Claude Code plugin, as",
     "the vendored SKILL.md's words (.claude/skills/cua-driver, copied into plugin/ byte for byte)"),
]
# Parts of files allowed, from the line holding the first piece to the one before the line holding the second.
LINT_REGIONS = [
    ("lib/learn.py", "# --- drafting with Claude ---", "# --- measuring: the benchmark",
     "learn's --draft-with-claude: it runs Claude Code itself (`claude -p`, on the user's Claude account) to draft "
     "lessons, a feature named for the agent it runs"),
]


def agent_words():
    """{agent: a pattern for the words that name it}: its folder, flag, command and JSON key in any case, its label's
    words but the generic ones, and EXTRA_WORDS, each as a whole word (a letter or digit next to it makes it another
    word; an underscore, a hyphen or a dot doesn't, so CODEX_HOME, claude_pid and .gemini name theirs)."""
    out = {}
    for n in registry.AGENTS:
        m = manifests.manifest(n)
        ws = {n, m["flag"], m["prog"], m["json_key"]} | {w for w in m["label"].split() if w not in GENERIC_WORDS}
        alts = [f"(?i:{re.escape(w)})" for w in sorted(ws, key=lambda w: (-len(w), w))]
        alts += [re.escape(w) for w in EXTRA_WORDS.get(n, ())]
        out[n] = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])")
    return out


def shared_files():
    """The shared code's files (SHARED, less lib/agentkit; of bench/, its harness: the .py files at its top)."""
    out = []
    for top in SHARED:
        full = os.path.join(LAB, top)
        if os.path.isfile(full):
            out.append(top)
            continue
        if top == "bench":
            out += [f"bench/{f}" for f in sorted(os.listdir(full)) if f.endswith(".py")]
            continue
        for d, ds, fs in os.walk(full):
            ds[:] = sorted(x for x in ds if x != "__pycache__" and os.path.relpath(os.path.join(d, x), LAB) != "lib/agentkit")
            out += [os.path.relpath(os.path.join(d, f), LAB) for f in sorted(fs)]
    return out


def printed_lines(rel, text):
    """The line numbers of rel's text that it prints as it is (its usage), or an empty set: a Python module's docstring
    when the module prints __doc__ (its first paragraph when that's all it prints), a shell script's header (line 2 up
    to its `set -` line) when it prints that with sed."""
    lines = text.split("\n")
    if rel.endswith(".py"):
        uses = re.findall(r"__doc__(\.split\(\"\\n\\n\"\)\[0\])?", text)
        try:
            body = ast.parse(text).body
        except SyntaxError:
            return set()
        if not uses or not body or not (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                                        and isinstance(body[0].value.value, str)):
            return set()
        a, b = body[0].lineno, body[0].end_lineno
        if all(uses):
            b = next((i for i in range(a, b + 1) if not lines[i - 1].strip()), b + 1) - 1
        return set(range(a, b + 1))
    m = re.search(r"sed -n '2,/\^(set -[A-Za-z]+)/p' \"\$(?:0|SELF)\"", text)
    if not m:
        return set()
    end = next((i for i, ln in enumerate(lines, 1) if ln.startswith(m.group(1))), 0)
    return set(range(2, end))


def naming_lines(words, files):
    """[(file, line number, line, agents it names, printed)] of the shared files, less LINT_FILES and LINT_REGIONS, with
    LINT_WORDS taken out of each line first; lines of files that aren't text are skipped. Also {LINT_WORDS index},
    {LINT_REGIONS index} that were used."""
    hits, used_words, used_regions = [], set(), set()
    for rel in files:
        if rel in LINT_FILES:
            continue
        try:
            with open(os.path.join(LAB, rel), encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        printed, region = printed_lines(rel, text), None
        for i, line in enumerate(text.split("\n"), 1):
            if region is None:
                region = next((k for k, (f, a, _b, _w) in enumerate(LINT_REGIONS) if f == rel and a in line), None)
            elif LINT_REGIONS[region][2] in line:
                region = None
            if region is not None:
                if any(p.search(line) for p in words.values()):
                    used_regions.add(region)
                continue
            rest = line
            for k, (piece, _why) in enumerate(LINT_WORDS):
                if piece in rest:
                    rest = rest.replace(piece, " ")
                    used_words.add(k)
            named = [n for n, p in words.items() if p.search(rest)]
            if named:
                hits.append((rel, i, line, named, i in printed))
    return hits, used_words, used_regions


def bash_source(path):
    """What sourcing path in bash does: (the commands it runs besides setting variables, the functions it defines). Run
    with nothing on PATH and a HOME that isn't there, traced (set -x)."""
    r = subprocess.run(["/bin/bash", "-c", 'set -x; . "$1"; { set +x; } 2>/dev/null; declare -F', "bash_source", path],
                       capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                       env={"PATH": "/nonexistent", "HOME": "/nonexistent"})
    did = [ln for ln in r.stderr.splitlines() if not re.match(r"\++ (\. |[A-Za-z_][A-Za-z0-9_]*=)", ln)]
    return did + ([f"exit status {r.returncode}"] if r.returncode else []), \
        [ln.split()[-1] for ln in r.stdout.splitlines() if ln.startswith("declare -f")]


# The defaults that name an agent for a record that names none, read with the registry's order reversed (argv LAB).
REORDERED_DRIVER = r'''
import json, os, sys
lab = sys.argv[1]
sys.path[:0] = [os.path.join(lab, "lib"), os.path.join(lab, "bench")]
from agentkit import registry
registry.AGENTS = tuple(reversed(registry.AGENTS))
import owners, handoff, report, bench
from agentkit import recognition
print(json.dumps({"owners": [owners.RECORD_DEFAULT_AGENT, owners._agent({})], "handoff": handoff.DEFAULT_SHORT,
                  "bench": [bench.DEFAULT_AGENT, bench.DEFAULTS["agent"], bench.DEFAULTS["model"]],
                  "report": [report.default_agent(), report.agent_name({})],
                  "swift": [ln for ln in recognition.swift().splitlines() if "recordDefaultAgent" in ln]}))
'''


def records_default_checks():
    """Claims, handoffs, the vault's records and bench's runs without an agent field are Claude Code's (they're older
    than the other agents' support): named in the registry (DEFAULT), never by a position in its list, which only orders
    listings; and the vault's build takes the same name from the same place."""
    sys.path.insert(0, os.path.join(LAB, "bench"))
    import owners
    import bench
    t = tempfile.mkdtemp(prefix="mcomp-agents-order-")
    try:
        driver = os.path.join(t, "reordered.py")
        with open(driver, "w") as f:
            f.write(REORDERED_DRIVER)
        r = subprocess.run([sys.executable, driver, LAB], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, env=dict(os.environ, HOME=t))
    finally:
        shutil.rmtree(t, ignore_errors=True)
    try:
        got = json.loads(r.stdout)
    except ValueError:
        got = {"error": r.stderr.strip().splitlines()[-1:]}
    want = {"owners": ["Claude Code", "Claude Code"], "handoff": "Claude",
            "bench": ["claude", "claude", bench.DEFAULTS["model"]], "report": ["claude", "Claude Code"],
            "swift": ['let recordDefaultAgent = "Claude Code"']}
    check("a record without an agent (a claim, a handoff, the vault's records, a bench run) is Claude Code's whatever "
          "the registry's order: with the list reversed, owners, handoffs, bench, its reports and the vault's tables "
          "all still say so", got == want, got)
    swift = recognition.swift()
    util = open(os.path.join(LAB, "vault", "Util.swift")).read()
    code = "\n".join(ln.split("//")[0] for ln in util.splitlines())
    check("...and the vault has it from the same place as lib/owners.py: the tables its build compiles in name "
          "owners.RECORD_DEFAULT_AGENT, and vault/Util.swift's records read that, with no name of its own",
          f'let recordDefaultAgent = "{owners.RECORD_DEFAULT_AGENT}"' in swift.splitlines()
          and code.count("recordDefaultAgent") >= 2 and f'"{owners.RECORD_DEFAULT_AGENT}"' not in code,
          [ln for ln in util.splitlines() if "recordDefaultAgent" in ln or f'"{owners.RECORD_DEFAULT_AGENT}"' in ln])


def portable_checks(real, app):
    """An app's bundle and helper are read by Python's re (lib/owners.py) and by the vault's ICU: recognition.py check
    keeps them to what both read alike (test_vault_ops runs every shipped one through the vault itself)."""
    unportable = getattr(recognition, "unportable", None)
    shipped = [(n, i, f, a[f]) for n in registry.AGENTS for i, a in enumerate(real(n, "recognition").get("apps") or ())
               for f in ("bundle", "helper")]
    alike = ["[a-z0-9_]+", "^(?:x|y){1,3}$", "\\d{2,}[^/]*\\.app", "\\/\\-\\(\\)\\[\\]\\{\\}\\^\\$\\|\\?\\*\\+\\\\",
             "[\\]\\-\\\\^$.|?*+(){}/]", "a|", "(|b)c?"]
    got = {p: unportable(p) for _n, _i, _f, p in shipped + [(0, 0, 0, p) for p in alike]} if unportable else None
    check("every shipped app pattern (and the forms of the shared subset: classes, ranges, escapes, repeats, groups, "
          "alternatives, anchors) is one Python and the vault's ICU read alike (recognition.unportable)",
          shipped and got is not None and not any(got.values()), (shipped, got))

    def refused(field, pattern):
        broken = dict(real(app, "recognition"))
        broken["apps"] = [dict(broken["apps"][0], **{field: pattern})]
        try:
            registry.data = lambda n, part: broken if (n, part) == (app, "recognition") else real(n, part)
            return recognition.check()
        finally:
            registry.data = real
    # Codex's reproduction: Python matches /Applications/Antigravity.app/Contents/MacOS/Electron with it; ICU doesn't
    # compile it (U_REGEX_INVALID_CAPTURE_GROUP_NAME), so the vault would stop counting that executable as the agent's
    caps = refused("bundle", "/(?P<app>Antigravity)[^/]*\\.app/Contents/MacOS/[^/]+$")
    check("...a Python-only pattern is refused, naming the agent, the field and what it uses (a (?P<name>...) group in "
          "an app's bundle)", any(b.startswith(f"{app}: apps[0].bundle uses ") and "(?P<name>...)" in b for b in caps),
          caps)
    tried = {}
    for pattern, words in (("(?P=x)", "(?P=name)"), ("(?#note)language_server", "(?#...)"),
                           ("(?<=/)language_server", "lookbehind"), ("(?<!x)language_server", "lookbehind"),
                           ("(?i)language_server", "inline flags"), ("language_(?i:server)", "inline flags"),
                           ("(?<n>language_server)", "(?<name>...)"), ("(?'n'language_server)", "(?'name'...)"),
                           ("(?=l)language_server", "lookahead"), ("^\\w+$", "\\w"), ("^language.server$", "unescaped ."),
                           ("^language_server_?\\s?$", "\\s"), ("^language_server.*?$", "unescaped ."),
                           ("^language_server_*?$", "repeat after a repeat"), ("^language_server{,2}$", "{"),
                           ("^[[:alpha:]_]+$", "[ inside a class"),
                           ("^language_server_[:digit:]+$", "first character is :"), ("^x[^:a]$", "first character is :"), ("^[a-z&&x]+$", "&& inside a class"),
                           ("^language_server\\Z", "\\Z"), ("", "empty pattern"), ("^lánguage_server$", "printable ASCII")):
        caps = refused("helper", pattern)
        tried[pattern] = [b for b in caps if b.startswith(f"{app}: apps[0].helper uses ") and words in b]
    check("...as is every other construct outside it, by name: (?P=name), (?#...), lookbehinds and lookaheads, inline "
          "flags, named groups in either syntax, \\w, \\s, the dot, a lazy repeat, a { that isn't a repeat, nested "
          "classes and set operations, \\Z, a letter outside ASCII and the empty pattern (in an app's helper)",
          all(tried.values()), {p: v for p, v in tried.items() if not v})


def packages():
    print("\nagent packages (coding-agents/<name>/)")
    root = os.path.join(LAB, "coding-agents")
    check("the registry is an explicit list, in lib/agentkit/registry.py, of names that are folders in coding-agents/",
          isinstance(registry.AGENTS, tuple) and registry.AGENTS and len(set(registry.AGENTS)) == len(registry.AGENTS)
          and all(os.path.isdir(os.path.join(root, n)) for n in registry.AGENTS), registry.AGENTS)
    folders = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and d != "__pycache__")
    check("...and every folder in coding-agents/ is registered (a folder isn't loaded by being there)",
          folders == sorted(registry.AGENTS), folders)
    check("the folder isn't agents/: Gemini CLI loads the repository's root as its extension (gemini-extension.json), "
          "and would load a root agents/ folder as the extension's own agents", not os.path.exists(os.path.join(LAB, "agents")))
    need = ("manifest.json", "recognition.json", "setup.py", "README.md", "claims.tsv")
    missing = [f"{n}/{f}" for n in registry.AGENTS for f in need if not os.path.isfile(os.path.join(root, n, f))]
    check("each agent's folder has its manifest, recognition tables, setup steps, page and doc-claims", not missing, missing)
    bad = {n: manifests.check(n) for n in registry.AGENTS if manifests.check(n)}
    check("each agent's manifest is sound (lib/agentkit/manifests.py check)", not bad, bad)
    check("...and no two agents claim one word (a flag, a label, a JSON key, a command; one release_file at most)",
          not manifests.conflicts(), manifests.conflicts())
    real = registry.data
    probe = registry.AGENTS[0]
    try:
        tried = []
        for broken in ({"label": "X"}, dict(real(probe, "manifest"), nope=1), dict(real(probe, "manifest"), flag="Two Words"),
                       dict(real(probe, "manifest"), owners=["jon"]), dict(real(probe, "manifest"), docs="README.md"),
                       dict(real(probe, "manifest"), install={"steps": "other"})):
            registry.data = lambda n, part, b=broken: b if (n, part) == (probe, "manifest") else real(n, part)
            tried.append(bool(manifests.check(probe)))
    finally:
        registry.data = real
    check("...a broken one is refused (keys missing, an unknown key, a flag that isn't a word, owners that aren't "
          "@names, a page outside docs/, install steps it doesn't know)", all(tried) and len(tried) == 6, tried)
    rec = recognition.check()
    check("each agent's recognition tables are sound and its own (lib/agentkit/recognition.py check)", not rec, rec)
    app = next(n for n in registry.AGENTS if real(n, "recognition").get("apps"))
    try:
        broken = dict(real(app, "recognition"))
        broken["apps"] = [dict(broken["apps"][0], folders=["MyApp"])]
        registry.data = lambda n, part: broken if (n, part) == (app, "recognition") else real(n, part)
        caps = recognition.check()
    finally:
        registry.data = real
    check("...an app's folder that isn't lowercase is refused (lib/owners.py and the vault lowercase each folder of a "
          "path before they compare it, so it would never match)", any("lowercase" in b for b in caps), caps)
    portable_checks(real, app)
    records_default_checks()
    with_hooks = [n for n in registry.AGENTS if manifests.manifest(n)["install"]["steps"] == "hooks"]
    no_sh = [n for n in with_hooks if not registry.has(n, "setup.sh")]
    check("an agent whose install steps are its own brings them (setup.sh)", not no_sh, no_sh)
    no_status = [n for n in with_hooks if not (registry.has(n, "setup.py") and all(
        callable(getattr(registry.module(n, "setup"), f, None)) for f in ("status", "line")))]
    no_agent = [n for n in registry.AGENTS if n not in with_hooks and not (
        registry.has(n, "setup.py") and getattr(registry.module(n, "setup"), "AGENT", None) is not None)]
    check("...and its part of `metacomputer agents` (setup.py status and line); an agent whose steps are the "
          "framework's brings its Agent (setup.py AGENT)", not no_status and not no_agent, (no_status, no_agent))
    moved = [("lib/" + "claude_mcp.py", "Claude Code's MCP entry reader, now coding-agents/claude-code/claude_mcp.py"),
             ("(?<![-/A-Za-z0-9_.])" + "agents/<", "the agents' folders, coding-agents/<name>/"),
             ("lib/agents.py " + "codex[-_]entry", "Codex's entry reader, now coding-agents/codex/setup.py codex-entry"),
             ("[(]lib/agents.py " + "check[)]", "Codex's check, now its setup.py check"),
             ("lib/agent_setup.py " + "(?:OpenClaw|Hermes|Gemini|Antigravity)", "an agent's Agent, now its setup.py")]
    tracked = subprocess.run(["git", "ls-files"], cwd=LAB, capture_output=True, text=True).stdout.split()
    stale_refs = []
    for rel in tracked:
        if rel in ("CHANGELOG.md", "tests/test_agents.py") or rel.startswith(("plugin/", "agent-plugin/", ".agents/")):
            continue                        # what the changelog says moved, this check, the packages kept byte for byte
        try:
            with open(os.path.join(LAB, rel), encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        for pat, what in moved:
            for m in re.finditer(pat, text):
                stale_refs.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {what}")
    probe = [bool(re.search(p, s)) for (p, _w), s in zip(moved, ("(lib/" + "claude_mcp.py's rule", "(agents/" + "<name>/x)",
                                                                  "lib/agents.py " + "codex-entry", "(lib/agents.py " + "check)",
                                                                  "lib/agent_setup.py " + "Antigravity"))]
    check("no file points at where an agent's code was before it moved to its folder (lib/claude_mcp.py, agents/<name>/, "
          "lib/agents.py codex-entry or check, lib/agent_setup.py's agents; the patterns tried on a known hit each)",
          not stale_refs and all(probe), (stale_refs, probe))
    ran, bad_names, defs = {}, {}, {}
    for n in registry.AGENTS:
        flag = manifests.manifest(n)["flag"]
        for part in ("setup.sh", "cli.sh"):
            if registry.has(n, part):
                did, funcs = bash_source(os.path.join(root, n, part))
                if did:
                    ran[f"{n}/{part}"] = did[:5]
                bad = [f for f in funcs if f.startswith("ag_") and not f.startswith(f"ag_{flag}_")]
                if bad:
                    bad_names[f"{n}/{part}"] = bad
                for f in funcs:
                    defs.setdefault((part, f), []).append(n)
    t = tempfile.mkdtemp(prefix="mcomp-agents-sh-")
    try:
        quiet, loud = os.path.join(t, "quiet.sh"), os.path.join(t, "loud.sh")
        with open(quiet, "w") as f:
            f.write('A=1; B="$HOME/x"\nag_x_hook() {\n  echo hi\n}\n')
        with open(loud, "w") as f:
            f.write('A="$(date)"\necho hi\n')
        probe = (bash_source(quiet), bash_source(loud))
    finally:
        shutil.rmtree(t, ignore_errors=True)
    check("an agent's bash (setup.sh, cli.sh), sourced, runs nothing: it sets variables and defines functions only "
          "(the trace tried on a file that does and one that doesn't first)", not ran and probe[0] == ([], ["ag_x_hook"])
          and len(probe[1][0]) >= 2, (ran, probe))
    check("...its hooks are named for its own flag (ag_FLAG_HOOK, never another agent's)", not bad_names, bad_names)
    hosts = {"setup.sh": ("install.sh",), "cli.sh": ("bin/metacomputer", "scripts/lib.sh")}
    theirs = {part: {f for h in files for f in re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) *\{",
                                                         open(os.path.join(LAB, h)).read(), re.M)}
              for part, files in hosts.items()}
    clash = {f"{part}: {f}": ns for (part, f), ns in defs.items() if len(ns) > 1 or f in theirs[part]}
    check("...and no function of one clashes with another agent's or with what sources it (install.sh; "
          "bin/metacomputer and scripts/lib.sh)", not clash and all(theirs.values()), clash)
    r = subprocess.run([sys.executable, os.path.join(LAB, "scripts", "gen-agents.py"), "--check"], capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    check("what's made from the manifests is current: docs/agents.md, lib/agentkit/agents.sh and CODEOWNERS "
          "(scripts/gen-agents.py --check)", r.returncode == 0, r.stdout + r.stderr)
    owners = open(os.path.join(LAB, ".github", "CODEOWNERS")).read().splitlines()
    check("CODEOWNERS has each agent's folder, with its owners", all(
        f"/coding-agents/{n}/ {' '.join(manifests.manifest(n)['owners'])}" in owners for n in registry.AGENTS))
    index = open(os.path.join(LAB, "docs", "agents.md")).read()
    check("docs/agents.md lists each agent with its folder and its page", all(
        f"../coding-agents/{n}/" in index and manifests.manifest(n)["docs"].split("/", 1)[1] in index
        for n in registry.AGENTS))

    # The lint: its pattern, tried on known hits and known misses first (a pattern that matches nothing passes all).
    words = agent_words()
    probes = {"--only-claude": {"claude-code"}, "claude_pid": {"claude-code"}, "Claude Code's plugin": {"claude-code"},
              "env -u CLAUDECODE": {"claude-code"}, "CODEX_HOME=x": {"codex"}, "inside Codex's sandbox": {"codex"},
              "openclaw mcp add": {"openclaw"}, "~/.openclaw-work": {"openclaw"}, "Hermes Agent": {"hermes"},
              "HERMES_HOME": {"hermes"}, "~/.gemini/settings.json": {"gemini"},
              "agy mcp add": {"antigravity"}, "Antigravity.app": {"antigravity"}, "--no-antigravity": {"antigravity"},
              "claude_code": {"claude-code"},
              "the coding agents": set(), "Claudette's codexes": set(), "hermetic": set(), "geminid": set(),
              "pragya": set(), "an agent's command line interface": set()}
    seen = {t: {n for n, p in words.items() if p.search(t)} for t in probes}
    check("the lint's pattern finds an agent's words and nothing else (tried on known lines first)", seen == probes,
          {t: s for t, s in seen.items() if s != probes[t]})
    files = shared_files()
    check("...it reads the shared code (bin/metacomputer, install.sh, lib/owners.py, lib/learn_mine.py, scripts/"
          "sync-plugin.py and bench/bench.py among them, not lib/agentkit)",
          all(f in files for f in ("bin/metacomputer", "install.sh", "lib/owners.py", "lib/learn_mine.py",
                                   "scripts/sync-plugin.py", "bench/bench.py", "lib/agent_setup.py"))
          and not any(f.startswith("lib/agentkit/") for f in files) and len(files) > 80, len(files))
    p_text = 'x\nprint(__doc__.split("\\n\\n")[0])\n'
    check("...and tells what's printed as it is: a whole docstring, its first paragraph, a shell script's header",
          printed_lines("a.py", '"""one\ntwo\n\nthree"""\n' + p_text) == {1, 2}
          and printed_lines("a.py", '"""one\ntwo\n\nthree"""\nprint(__doc__)\n') == {1, 2, 3, 4}
          and printed_lines("a.sh", "#!/bin/bash\n# usage\n# more\nset -uo pipefail\nsed -n '2,/^set -uo/p' \"$0\"\n") == {2, 3}
          and printed_lines("a.py", '"""one"""\n') == set())
    hits, used_words, used_regions = naming_lines(words, files)
    used, unallowed, printed_used = set(), [], set()
    for rel, i, line, named, printed in hits:
        k = next((j for j, (f, piece, _why) in enumerate(LINT_ALLOW) if f == rel and piece in line), None)
        if k is not None:
            used.add(k)
        elif printed and rel in PRINTED:
            printed_used.add(rel)
        else:
            unallowed.append(f"{rel}:{i}: {line.strip()[:140]} ({', '.join(named)})")
    check("no shared code names an agent outside its folder (allowed: LINT_FILES, the printed usage of PRINTED's files, "
          "LINT_REGIONS, LINT_WORDS and LINT_ALLOW's lines, each for its reason)", not unallowed, unallowed)
    stale = ([LINT_ALLOW[j][:2] for j in range(len(LINT_ALLOW)) if j not in used]
             + [LINT_WORDS[j][0] for j in range(len(LINT_WORDS)) if j not in used_words]
             + [LINT_REGIONS[j][:2] for j in range(len(LINT_REGIONS)) if j not in used_regions]
             + sorted(set(PRINTED) - printed_used) + sorted(f for f in LINT_FILES if not os.path.isfile(os.path.join(LAB, f))))
    check("...and every allowance is still needed (a permission goes with what it allowed)", not stale, stale)
    reasons = ([w for _f, _p, w in LINT_ALLOW] + [w for _p, w in LINT_WORDS] + [w for *_x, w in LINT_REGIONS]
               + list(PRINTED.values()) + list(LINT_FILES.values()))
    check("...each one says why", all(isinstance(w, str) and len(w) >= 15 for w in reasons))


# Runs `metacomputer agents` (lib/agents.py main) in a fresh interpreter with Gemini CLI's setup.py changed as an agent's
# folder might change it: argv LAB, how ("imported-status": it imports the framework's status, as a
# `from agent_setup import ... status` would; "second-check": it brings an `agents check` of its own), and the
# command's arguments.
HOOKS_DRIVER = r'''
import os, sys
lab, how = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(lab, "lib"))
from agentkit import registry
import agent_setup
g = registry.module("gemini", "setup")
if how == "imported-status":
    g.status = agent_setup.status
elif how == "second-check":
    exec("import json\ndef check(as_json, quiet):\n    print(json.dumps({'gemini': None}) if as_json else 'nothing')\n"
         "    return 0\n", g.__dict__)
import agents
sys.exit(agents.main(sys.argv[3:]))
'''


def hooks_cases(t, env):
    """What `metacomputer agents` takes from an agent's setup.py is decided by its manifest and by what the file defines
    itself, never by a name it happens to import; and `agents check --json` prints one JSON document whatever number of
    agents bring a check."""
    driver = os.path.join(t, "hooks_driver.py")
    with open(driver, "w") as f:
        f.write(HOOKS_DRIVER)

    def drive(how, *args):
        return subprocess.run([sys.executable, driver, LAB, how, *args], capture_output=True, text=True, env=env,
                              timeout=120, stdin=subprocess.DEVNULL)
    r = drive("imported-status", "--json")
    try:
        st = json.loads(r.stdout)
    except ValueError:
        st = {}
    check("an agent whose steps are the framework's is reported by the framework's status even when its setup.py "
          "imports a function named status (its manifest's install steps decide, not the names in the file)",
          r.returncode == 0 and (st.get("gemini") or {}).get("installed") is False, (r.returncode, r.stderr[-300:]))
    r = drive("second-check", "check", "--json")
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        doc = None
    check("two agents with a check of their own: `agents check --json` prints one JSON document, each agent's under "
          "its JSON key", r.returncode == 0 and doc == {"codex": {"codex": None}, "gemini": {"gemini": None}},
          (r.returncode, r.stdout[-300:], r.stderr[-300:]))
    r = drive("none", "check", "--json")
    check("...and with one (today's: Codex's), its document as it was", r.returncode == 0
          and json.loads(r.stdout or "null") == {"codex": None}, r.stdout)


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
        with open(os.path.join(base["CODEX_HOME"], "cmd"), "w") as f:
            f.write("/opt/someone/private-tools/bin/server\n")
        r = run(["--json"], dict(base, PATH=fake2 + ":/usr/bin:/bin"))
        srv = (json.loads(r.stdout or "{}").get("codex") or {}).get("mcp_server") or {}
        check("Codex's server that isn't this checkout's: reported by its program's name, not its path",
              srv.get("command") == "server" and "/opt/someone" not in r.stdout, r.stdout)
        os.remove(os.path.join(base["CODEX_HOME"], "cmd"))
        os.chmod(os.path.join(fake2, "codex"), 0o777)
        r = run(["check"], dict(base, PATH=fake2 + ":/usr/bin:/bin"))
        check("a codex other accounts can write isn't run", r.returncode == 1 and "writable by others" in r.stdout, r.stdout)
        os.chmod(os.path.join(fake2, "codex"), 0o755)
        r = subprocess.run([sys.executable, AG, "check", "--json"], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, cwd=fake2, env=dict(base, PATH=".:/usr/bin:/bin"))
        check("a codex found only through a relative PATH entry (a downloaded folder's own) isn't run",
              r.returncode == 0 and json.loads(r.stdout).get("codex") is None, r.stdout)
        hooks_cases(t, dict(base, PATH="/usr/bin:/bin"))
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
    packages()
    hostile_manifests.agents(check)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
