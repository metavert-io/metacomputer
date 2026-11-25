"""Hermes Agent's part of lib/agent_setup.py: its install, uninstall and status steps (an Agent: what's different about
its MCP server, its profiles and its skills). install.sh runs them through lib/agent_setup.py install|uninstall|status
hermes."""
import json
import os

from agentkit import mcp_entry   # MCP_ARGS: the arguments metacomputer's server is registered with
from agent_setup import Agent, manifest_words, NAME, TOOL_TIMEOUT, _distinct, entries, home, _entry_res  # the framework


class Hermes(Agent):
    """Hermes Agent: mcp_servers in $HERMES_HOME/config.yaml (~/.hermes; the active profile's), read with `hermes config
    get mcp_servers.metacomputer --json` and set with `hermes config set` (`hermes mcp add` starts the server and asks
    questions), which replaces an entry of the same name, so it's read right before. A timeout of 600 s (its default is
    300). Skills: $HERMES_HOME/skills (it doesn't read ~/.agents/skills unless told to)."""
    name, label, prog, tools, doc = manifest_words("hermes")

    profile = None          # the named profile this run acts on (None: the default one)

    def hermes_home(self, env):
        return env.get("HERMES_HOME") or os.path.join(home(env), ".hermes")

    def skills_dir(self, env):
        return os.path.join(self.hermes_home(env), "skills")

    def pin(self, prog, env):
        """The profile `hermes` acts on now (its sticky `hermes profile use` choice, or HERMES_HOME), resolved once with
        `hermes config path` and pinned for every later command: HERMES_HOME set to its folder, and `--profile default`
        for the default one (a root HERMES_HOME alone still follows the sticky choice). The skills go into the same
        folder."""
        self.prefix, self.profile = (), None
        rc, out, _ = self.run([prog, "config", "path"], env)
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        path = lines[-1] if rc == 0 and lines else ""
        if not (os.path.isabs(path) and os.path.basename(path) == "config.yaml"):
            return env, "couldn't tell which Hermes Agent profile is in use (hermes config path)"
        folder = os.path.dirname(path)
        named = os.path.basename(os.path.dirname(folder)) == "profiles"
        self.profile = os.path.basename(folder) if named else None
        self.prefix = () if named else ("--profile", "default")
        return dict(env, HERMES_HOME=folder), None

    def config_files(self, env):
        root = os.path.join(home(env), ".hermes")
        return _distinct(([os.path.join(env["HERMES_HOME"], "config.yaml")] if env.get("HERMES_HOME") else []) +
                         [os.path.join(root, "config.yaml")] +
                         [os.path.join(d, "config.yaml") for d in entries(os.path.join(root, "profiles"))])

    def config_dirs(self, env):
        return [os.path.join(home(env), ".hermes", "profiles")]

    def cli(self):
        return f"hermes -p {self.profile}" if self.profile else "hermes"

    def read(self, prog, env):
        rc, out, err = self.run([prog, "config", "get", f"mcp_servers.{NAME}", "--json"], env)
        if rc == 1 and f"Config key not set: mcp_servers.{NAME}" in (err + out):
            return {"kind": "absent"}
        if rc != 0:
            return {"kind": "error", "why": f"hermes config get failed (exit {rc})"}
        try:
            d = json.loads(out)
        except ValueError:
            return {"kind": "error", "why": "hermes config get answered in a shape this doesn't know"}
        res = _entry_res(d)
        res["raw"] = d
        return res

    def add_argv(self, prog, link):
        value = {"command": link, "args": list(mcp_entry.MCP_ARGS), "timeout": TOOL_TIMEOUT}
        return [prog, "config", "set", f"mcp_servers.{NAME}", json.dumps(value)]

    def remove_argv(self, prog):
        return [prog, "config", "unset", f"mcp_servers.{NAME}"]

    def add_line(self, link_shown):
        return (f"{self.cli()} config set mcp_servers.{NAME} '{{\"command\": \"{link_shown}\", \"args\": [\"mcp-lazy\"], "
                f"\"timeout\": {TOOL_TIMEOUT}}}'")

    def remove_line(self):
        return f"{self.cli()} config unset mcp_servers.{NAME}"


AGENT = Hermes()
