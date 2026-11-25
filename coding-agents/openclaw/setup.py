"""OpenClaw's part of lib/agent_setup.py: its install, uninstall and status steps (an Agent: what's different about its
MCP server and skills). install.sh runs them through lib/agent_setup.py install|uninstall|status openclaw."""
import json
import os

from agentkit import mcp_entry   # MCP_ARGS: the arguments metacomputer's server is registered with
from agent_setup import Agent, manifest_words, NAME, TOOL_TIMEOUT, SKILLS, _distinct, entries, home, _entry_res  # the framework


class OpenClaw(Agent):
    """OpenClaw: mcp.servers in ~/.openclaw/openclaw.json (OPENCLAW_CONFIG_PATH, OPENCLAW_STATE_DIR move it), read with
    `openclaw mcp show metacomputer --json`, added with `openclaw mcp add --no-probe` (which refuses a name that's
    there) with a 600 s request timeout (its default is 60 s), removed with `openclaw mcp unset`. Skills: it reads
    ~/.agents/skills with its default state folder; `openclaw skills info` says whether it sees them."""
    name, label, prog, tools, doc = manifest_words("openclaw")

    def env(self, env):
        return dict(env, OPENCLAW_NO_AUTO_UPDATE="1")        # no update check for a quick command

    def homes(self, env):
        """The homes OpenClaw keeps its folders in: OPENCLAW_HOME (when set) and yours."""
        return _distinct([env.get("OPENCLAW_HOME") or home(env), home(env)])

    def config_files(self, env):
        # The one its CLI uses: OPENCLAW_CONFIG_PATH, else OPENCLAW_STATE_DIR's, else a named OPENCLAW_PROFILE's
        # (~/.openclaw-PROFILE), else ~/.openclaw's; then every other one: ~/.openclaw and every ~/.openclaw-NAME.
        base, prof = self.homes(env)[0], env.get("OPENCLAW_PROFILE") or ""
        state = env.get("OPENCLAW_STATE_DIR") or os.path.join(
            base, f".openclaw-{prof}" if prof and prof != "default" else ".openclaw")
        return _distinct([env.get("OPENCLAW_CONFIG_PATH") or os.path.join(state, "openclaw.json")] +
                         [f for h in self.homes(env) for f in [os.path.join(h, ".openclaw", "openclaw.json")] +
                          [os.path.join(d, "openclaw.json") for d in entries(h, ".openclaw-")]])

    def config_dirs(self, env):
        return self.homes(env)

    def read(self, prog, env):
        rc, out, err = self.run([prog, "mcp", "show", NAME, "--json"], env)
        try:
            d = json.loads(out)
        except ValueError:
            d = None
        # Missing: exit 1 and, with --json, {"ok": false, "error": {"message": "No MCP server named \"metacomputer\" in ..."}}
        failed = isinstance(d, dict) and d.get("ok") is False
        said = (d.get("error") or {}).get("message") if failed and isinstance(d.get("error"), dict) else ""
        if rc == 1 and failed and isinstance(said, str) and said.startswith(f'No MCP server named "{NAME}" in '):
            return {"kind": "absent"}
        if rc != 0 or failed:
            return {"kind": "error", "why": f"openclaw mcp show failed (exit {rc})"}
        if d is None:
            return {"kind": "error", "why": "openclaw mcp show answered in a shape this doesn't know"}
        res = _entry_res(d)
        res["raw"] = d
        return res

    def add_argv(self, prog, link):
        return [prog, "mcp", "add", NAME, "--command", link, *[x for a in mcp_entry.MCP_ARGS for x in ("--arg", a)],
                "--timeout", str(TOOL_TIMEOUT), "--no-probe"]

    def remove_argv(self, prog):
        return [prog, "mcp", "unset", NAME]

    def add_line(self, link_shown):
        return f"openclaw mcp add {NAME} --command {link_shown} --arg mcp-lazy --timeout {TOOL_TIMEOUT} --no-probe"

    def remove_line(self):
        return f"openclaw mcp unset {NAME}"

    def check_skill(self, prog, env):
        missing, hidden = [], []
        for name in SKILLS:
            rc, out, _ = self.run([prog, "skills", "info", name, "--json"], env)
            try:
                d = json.loads(out) if rc == 0 else None
            except ValueError:
                d = None
            if not isinstance(d, dict) or d.get("ok") is False:
                missing.append(name)
            elif d.get("eligible") is False:
                hidden.append(name)
        if missing:
            return (False, f"OpenClaw doesn't list the skill{'s' if len(missing) > 1 else ''} {' and '.join(missing)} "
                           "(with a state folder of its own, it doesn't read ~/.agents/skills: docs/openclaw.md)")
        if hidden:
            return (False, f"OpenClaw lists the skill{'s' if len(hidden) > 1 else ''} {' and '.join(hidden)} but doesn't "
                           f"offer {'them' if len(hidden) > 1 else 'it'} (openclaw skills info {hidden[0]} says why)")
        return (True, "OpenClaw sees both skills")


AGENT = OpenClaw()
