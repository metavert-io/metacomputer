"""Claude Code's bench launcher (bench/bench.py --agent claude, the default; the harness calls these with itself as B).
Its stream-json output is the harness's own format (every other agent's events are turned into it), so it needs no
events() of its own."""
import os

SKILL_HOME = ".claude"          # the skill goes in the work folder's .claude/skills/ (a project skill)
ISOLATED = False                # its sessions run with your HOME (--setting-sources project, --strict-mcp-config)
TOKENS_ONLY = False             # it reports a cost: --budget and --cap apply
DEFAULT_MODEL = "claude-opus-5-5"   # the bench's default model (--model / BENCH_MODEL)
VERSION_KEY = "claude_code"     # run.json's older name for its version (agent_version has it too)


def args(B, t, sw, cfg):
    # The session's own --mcp-config server gives mcp__metacomputer__*; an older plugin copy's names are allowed too.
    # Its metacomputer commands: the work folder's launchers (the skill copy names the first; metacomputer and mcomp
    # are them, first on its PATH), which run the checkout's without the --pass-env variables, never the checkout's own.
    launchers = B.agent_launchers(os.path.dirname(cfg))
    allowed = ["Skill", "Read", "Glob", "Grep", "ToolSearch", "mcp__metacomputer", "mcp__plugin_metacomputer_metacomputer",
               *(f"Bash({c}:*)" for c in launchers), "Bash(metacomputer:*)", "Bash(mcomp:*)"]
    denied = []
    if sw["shell"] == "forbid":
        for c in (*launchers, B.CLI, "metacomputer", "mcomp"):
            denied += [f"Bash({c} ssh:*)", f"Bash({c} sh:*)"]
    budget = float(t.get("budget_usd") or sw["budget"])
    a = ["-p", B.PREFIX + t["prompt"].strip() + (B.SHELL_FORBID if sw["shell"] == "forbid" else ""),
         "--model", sw["model"], "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", cfg,
         "--max-turns", str(int(t.get("max_turns", 60))), "--max-budget-usd", f"{budget:.2f}",
         "--output-format", "stream-json", "--verbose", "--no-session-persistence",
         "--allowedTools", *allowed]
    if denied:
        a += ["--disallowedTools", *denied]
    if sw["effort"]:
        a += ["--effort", sw["effort"]]
    return a, budget
