#!/usr/bin/env python3
"""metacomputer for OpenClaw, Hermes Agent and Gemini CLI (docs/openclaw.md, docs/hermes.md, docs/gemini.md): install.sh's
step for each, its part of --uninstall, and what `metacomputer agents` reports about them.

  agent_setup.py install NAME --root CHECKOUT --link ~/.local/bin/metacomputer
  agent_setup.py uninstall NAME --root CHECKOUT --link ~/.local/bin/metacomputer
  agent_setup.py status NAME --root CHECKOUT --link ~/.local/bin/metacomputer     (JSON)

NAME is openclaw, hermes or gemini. For each agent the step registers metacomputer's MCP server, `~/.local/bin/metacomputer
mcp-lazy`, in the agent's own configuration, through the agent's own CLI (OpenClaw: `openclaw mcp add`; Hermes Agent:
`hermes config set`; Gemini CLI: `gemini mcp add -s user`), and links the skills metacomputer and cua-driver where the
agent reads them (OpenClaw and Gemini CLI: ~/.agents/skills, which Codex reads too; Hermes Agent: $HERMES_HOME/skills).

The MCP server named metacomputer follows the rule Claude Code's and Codex's do (lib/claude_mcp.py, lib/agents.py
codex_entry): it is metacomputer's own ("ok") only as a local command that is exactly the checkout's bin/metacomputer or
~/.local/bin/metacomputer, with exactly the argument mcp-lazy. Anything else ("foreign") is never changed or removed: the
run names it by its program and how many arguments it has, or its transport (never an argument, a variable or a URL),
says how to remove it yourself, and the step is unfinished. An entry that can't be read ("error") is never taken for a
missing one. The entry is read again right before any change (its digest, or that it's still missing), and read back
after it. The step's output, one line each, for install.sh: "ok TEXT", "warn TEXT", "say TEXT", and for --uninstall
"keep" when an entry that stays runs ~/.local/bin/metacomputer (or mcomp), so those stay too. Exit status: 0 finished,
1 unfinished (install.sh records that in installed.json's "incomplete", so `metacomputer update` runs the step again),
2 usage. Nothing printed holds a value from the agent's configuration but metacomputer's own command.

Stdlib only; Python 3.11 or later (as lib/agents.py).
"""
import json
import os
import sys

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import agents        # noqa: E402  (trusted, run, version, link_target, choices)
import claude_mcp    # noqa: E402  (words, digest, linked, MCP_ARGS)

NAME = "metacomputer"
TOOL_TIMEOUT = 600          # seconds: as Codex's (install.sh CODEX_TOOL_TIMEOUT); OpenClaw's default is 60 s, Hermes's 300
SKILLS = ("metacomputer", "cua-driver")


def skill_target(root, name):
    """What a skill link points at: the checkout's skills/metacomputer, or its .claude/skills/cua-driver."""
    return os.path.join(root, "skills", "metacomputer") if name == "metacomputer" else \
        os.path.join(root, ".claude", "skills", "cua-driver")


def home(env):
    return env.get("HOME") or os.path.expanduser("~")


# --- JSON with comments (Gemini CLI's settings.json) -------------------------------------------------------------
def jsonc_loads(text):
    """JSON with // and /* */ comments, as Gemini CLI reads its settings.json (it refuses trailing commas, and so does
    this); ValueError when it isn't that. Comment marks inside strings are kept."""
    out, i, n = [], 0, len(text)
    if text.startswith("\ufeff"):
        i = 1
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise ValueError("an unterminated string")
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise ValueError("an unterminated comment")
            out.append(" ")
            i = j + 2
        else:
            out.append(c)
            i += 1
    return json.loads("".join(out))


# --- the agents --------------------------------------------------------------------------------------------------
def _entry_res(d):
    """An MCP server entry (a dict, as the agent keeps it) as claude_mcp's result: {"kind", "entry"}; a local command
    is "stdio" with its command, arguments and variables; an error when its parts have the wrong types."""
    if not isinstance(d, dict):
        return {"kind": "error", "why": "its entry has an unexpected shape"}
    cmd, args, envs = d.get("command"), d.get("args"), d.get("env")
    url_kind = d.get("transport") or d.get("type") or ("http" if d.get("httpUrl") else "sse" if d.get("url") else None)
    if cmd is None and url_kind:
        return {"kind": url_kind if isinstance(url_kind, str) else "remote", "entry": d}
    if (not isinstance(cmd, str) or not cmd or not isinstance(args if args is not None else [], list)
            or not all(isinstance(a, str) for a in args or []) or not isinstance(envs if envs is not None else {}, dict)):
        return {"kind": "error", "why": "its entry has an unexpected shape"}
    if url_kind not in (None, "stdio"):
        return {"kind": url_kind if isinstance(url_kind, str) else "remote", "entry": d}
    return {"kind": "stdio", "entry": {"command": cmd, "args": list(args or []), "env": dict(envs or {})}}


class Agent:
    name = label = prog = ""
    tools = ""              # how the agent names the tools, for the messages
    doc = ""
    note = ""               # said once its MCP server is metacomputer's

    def env(self, env):
        return dict(env)

    def skills_dir(self, env):
        return os.path.join(home(env), ".agents", "skills")

    def read(self, prog, env):          # {"kind": absent|error|stdio|..., "entry": ..., "raw": ..., "why": ...}
        raise NotImplementedError

    def add_argv(self, prog, link):
        raise NotImplementedError

    def remove_argv(self, prog):
        raise NotImplementedError

    def add_line(self, link_shown):
        raise NotImplementedError

    def remove_line(self):
        raise NotImplementedError

    def run(self, argv, env):
        return agents.run(argv, env=self.env(env), cwd=home(env))

    def check_skill(self, prog, env):
        """None when nothing more is known, else (ok, words) about whether the agent sees the metacomputer skill."""
        return None


class Gemini(Agent):
    """Gemini CLI: user-scope MCP servers in ~/.gemini/settings.json (JSON with comments; GEMINI_CLI_HOME moves it), set
    with `gemini mcp add -s user`, which replaces an entry of the same name without asking, so the entry is read (from
    the file: the CLI has no lookup) right before. `--trust`: the tools act only on the virtual computer, through the
    hub's guards (Codex's `default_tools_approval_mode = "approve"` likewise). Its default tool timeout is 600 s."""
    name, label, prog, tools, doc = "gemini", "Gemini CLI", "gemini", "mcp_metacomputer_*", "docs/gemini.md"
    note = ("Gemini CLI starts MCP servers only in folders you trust: it asks the first time you start it in a folder "
            "(docs/gemini.md)")

    def env(self, env):
        return dict(env, GEMINI_CLI_NO_RELAUNCH="true")      # one process for a quick command, not two

    def base(self, env):
        return env.get("GEMINI_CLI_HOME") or home(env)

    def settings(self, env):
        return os.path.join(self.base(env), ".gemini", "settings.json")

    def skills_dir(self, env):
        return os.path.join(self.base(env), ".agents", "skills")

    def read(self, prog, env):
        path = self.settings(env)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read(4 << 20)
        except FileNotFoundError:
            return {"kind": "absent"}
        except (OSError, UnicodeDecodeError):
            return {"kind": "error", "why": "its settings.json couldn't be read"}
        if not text.strip():
            return {"kind": "absent"}
        try:
            d = jsonc_loads(text)
        except (ValueError, RecursionError):
            return {"kind": "error", "why": "its settings.json isn't JSON this can read"}
        servers = d.get("mcpServers") if isinstance(d, dict) else None
        if servers is None:
            return {"kind": "absent"}
        if not isinstance(servers, dict):
            return {"kind": "error", "why": "its mcpServers isn't a table"}
        if NAME not in servers:
            return {"kind": "absent"}
        res = _entry_res(servers[NAME])
        res["raw"] = servers[NAME]
        return res

    def add_argv(self, prog, link):
        return [prog, "mcp", "add", "-s", "user", "--trust", NAME, link, *claude_mcp.MCP_ARGS]

    def remove_argv(self, prog):
        return [prog, "mcp", "remove", "-s", "user", NAME]

    def add_line(self, link_shown):
        return f"gemini mcp add -s user --trust {NAME} {link_shown} mcp-lazy"

    def remove_line(self):
        return f"gemini mcp remove -s user {NAME}"


class Hermes(Agent):
    """Hermes Agent: mcp_servers in $HERMES_HOME/config.yaml (~/.hermes; the active profile's), read with `hermes config
    get mcp_servers.metacomputer --json` and set with `hermes config set` (`hermes mcp add` starts the server and asks
    questions), which replaces an entry of the same name, so it's read right before. A timeout of 600 s (its default is
    300). Skills: $HERMES_HOME/skills (it doesn't read ~/.agents/skills unless told to)."""
    name, label, prog, tools, doc = "hermes", "Hermes Agent", "hermes", "mcp__metacomputer__*", "docs/hermes.md"

    def hermes_home(self, env):
        return env.get("HERMES_HOME") or os.path.join(home(env), ".hermes")

    def skills_dir(self, env):
        return os.path.join(self.hermes_home(env), "skills")

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
        value = {"command": link, "args": list(claude_mcp.MCP_ARGS), "timeout": TOOL_TIMEOUT}
        return [prog, "config", "set", f"mcp_servers.{NAME}", json.dumps(value)]

    def remove_argv(self, prog):
        return [prog, "config", "unset", f"mcp_servers.{NAME}"]

    def add_line(self, link_shown):
        return (f"hermes config set mcp_servers.{NAME} '{{\"command\": \"{link_shown}\", \"args\": [\"mcp-lazy\"], "
                f"\"timeout\": {TOOL_TIMEOUT}}}'")

    def remove_line(self):
        return f"hermes config unset mcp_servers.{NAME}"


class OpenClaw(Agent):
    """OpenClaw: mcp.servers in ~/.openclaw/openclaw.json (OPENCLAW_CONFIG_PATH, OPENCLAW_STATE_DIR move it), read with
    `openclaw mcp show metacomputer --json`, added with `openclaw mcp add --no-probe` (which refuses a name that's
    there) with a 600 s request timeout (its default is 60 s), removed with `openclaw mcp unset`. Skills: it reads
    ~/.agents/skills with its default state folder; `openclaw skills info` says whether it sees them."""
    name, label, prog, tools, doc = "openclaw", "OpenClaw", "openclaw", "metacomputer__*", "docs/openclaw.md"

    def env(self, env):
        return dict(env, OPENCLAW_NO_AUTO_UPDATE="1")        # no update check for a quick command

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
        return [prog, "mcp", "add", NAME, "--command", link, *[x for a in claude_mcp.MCP_ARGS for x in ("--arg", a)],
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


AGENTS = {a.name: a for a in (OpenClaw(), Hermes(), Gemini())}


# --- ownership ---------------------------------------------------------------------------------------------------
def classify(res, root, link):
    """"ok" (metacomputer's own: a local command, exactly ROOT/bin/metacomputer or LINK, with exactly ["mcp-lazy"]),
    "foreign" (anything else), "absent" or "error"."""
    if res["kind"] in ("absent", "error"):
        return res["kind"]
    e = res["entry"]
    if (res["kind"] == "stdio" and e.get("args") == claude_mcp.MCP_ARGS
            and e.get("command") in (os.path.join(root, "bin", "metacomputer"), link)):
        return "ok"
    return "foreign"


def digest(res):
    return "" if res["kind"] == "absent" else claude_mcp.digest(res.get("raw"))


def describe(res, own, env):
    return claude_mcp.words(res, env, own)


def points_here(link, root):
    try:
        return os.readlink(link) == os.path.join(root, "bin", "metacomputer")
    except OSError:
        return False


def tilde(path, env):
    h = home(env)
    return "~" + path[len(h):] if h and path.startswith(h + os.sep) else path


# --- skill links -------------------------------------------------------------------------------------------------
def old_target(t, root):
    """A link target an earlier install left: inside this checkout, or a skill folder that's gone (a moved checkout)."""
    if t.startswith(root.rstrip("/") + "/"):
        return True
    return not os.path.exists(t) and (t.endswith("/skills/metacomputer") or t.endswith("/skills/cua-driver")
                                      or t.endswith("/skills/virtual-computer"))


def ensure_skill(path, target, root):
    """"ok" (already this link), "made", "foreign" (something else there: left alone) or "error"."""
    try:
        if os.path.islink(path):
            cur = os.readlink(path)
            if cur == target:
                return "ok"
            if not old_target(cur, root):
                return "foreign"
            os.unlink(path)
        elif os.path.lexists(path):
            return "foreign"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return "made"
    except OSError:
        return "error"


# --- the steps ---------------------------------------------------------------------------------------------------
class Out:
    def __init__(self):
        self.lines = []

    def __call__(self, kind, text=""):
        self.lines.append(f"{kind}\t{' '.join(str(text).split())}" if text else kind)

    def flush(self):
        for line in self.lines:
            print(line)
        sys.stdout.flush()


def install(agent, root, link, env, out):
    """Set metacomputer up for one agent: its MCP server and its skills. True when everything finished."""
    prog, why = agents.trusted(agent.prog)
    if not prog:
        out("warn", why or f"{agent.label}'s {agent.prog} command isn't on PATH; after installing it, run: "
                           f"./install.sh --only-{agent.name}")
        return False
    finished = registered = True
    link_shown = tilde(link, env)
    res = agent.read(prog, env)
    own = classify(res, root, link)
    again_line = f"./install.sh --only-{agent.name}"
    if own == "error":
        out("warn", f"couldn't read {agent.label}'s MCP server {NAME} ({res.get('why')}), so it was left as it is; "
                    f"try {again_line} again")
        finished = registered = False
    elif own == "foreign":
        out("warn", f"{agent.label} already has an MCP server named {NAME} that isn't metacomputer's (it runs "
                    f"{describe(res, own, env)}); it was left as it is.")
        out("warn", f"To use metacomputer's instead: {agent.remove_line()}, then {again_line}")
        finished = registered = False
    elif own == "ok":
        out("ok", f"MCP server {NAME} already registered (tools: {agent.tools})")
    else:
        # Missing: read again right before the add (Gemini's and Hermes's adds replace an entry of the same name), then
        # add it and read it back.
        if classify(agent.read(prog, env), root, link) != "absent":
            out("warn", f"{agent.label}'s MCP server {NAME} changed since it was read, so it was left as it is; run "
                        f"{again_line} again")
            finished = registered = False
        else:
            agent.run(agent.add_argv(prog, link), env)
            back = agent.read(prog, env)
            if classify(back, root, link) == "ok":
                out("ok", f"MCP server {NAME} for every {agent.label} session (tools: {agent.tools}, there even while "
                          "the VM is off)")
            else:
                out("warn", f"could not add the MCP server to {agent.label}; run: {agent.add_line(link_shown)}")
                finished = registered = False
    sdir = agent.skills_dir(env)
    for name in SKILLS:
        state = ensure_skill(os.path.join(sdir, name), skill_target(root, name), root)
        where = tilde(sdir, env)
        if state in ("ok", "made"):
            out("ok", f"skill {name} ({where}, read by {agent.label})")
        elif state == "foreign":
            out("warn", f"{where}/{name} is already there and isn't metacomputer's; left alone ({agent.label} won't get "
                        f"the {name} skill)")
        else:
            out("warn", f"couldn't link the {name} skill into {where} ({agent.label} won't get it)")
    if agent.note and registered:
        out("say", agent.note)
    seen = agent.check_skill(prog, env)
    if seen is not None:
        out("ok" if seen[0] else "warn", seen[1])
    return finished


def uninstall(agent, root, link, env, out):
    """Remove what this checkout set up for one agent: its MCP server when it's metacomputer's own and runs this
    checkout (read again right before, and checked after), and the skill links that point here. True when it finished."""
    prog, _ = agents.trusted(agent.prog)
    finished = True
    if prog:
        res = agent.read(prog, env)
        own = classify(res, root, link)
        if own == "error":
            out("warn", f"couldn't read {agent.label}'s MCP server {NAME} ({res.get('why')}), so it was left as it is;")
            out("warn", f"run ./install.sh --uninstall again, or remove it yourself: {agent.remove_line()}")
            out("keep", f"{agent.label}'s")
            finished = False
        elif own != "absent":
            here = own == "ok" and (res["entry"]["command"] != link or points_here(link, root))
            if not here:
                out("say", f"{agent.label}'s MCP server {NAME} stays: it runs {describe(res, own, env)}, which isn't "
                           "this checkout's")
                if claude_mcp.linked(res, link):
                    out("keep", f"{agent.label}'s")
            elif digest(agent.read(prog, env)) != digest(res):
                out("warn", f"{agent.label}'s MCP server {NAME} changed since it was read, so it was left as it is;")
                out("warn", f"run ./install.sh --uninstall again, or remove it yourself: {agent.remove_line()}")
                out("keep", f"{agent.label}'s")
                finished = False
            else:
                agent.run(agent.remove_argv(prog), env)
                if agent.read(prog, env)["kind"] == "absent":
                    out("ok", f"removed {agent.label}'s {NAME} MCP server")
                else:
                    out("warn", f"couldn't remove {agent.label}'s {NAME} MCP server (or confirm it's gone); remove it "
                                f"yourself: {agent.remove_line()}")
                    out("keep", f"{agent.label}'s")
                    finished = False
    sdir = agent.skills_dir(env)
    for name in SKILLS:
        path = os.path.join(sdir, name)
        try:
            if os.path.islink(path) and os.readlink(path) == skill_target(root, name):
                os.unlink(path)
                out("ok", f"removed the {name} skill link for {agent.label}")
        except OSError:
            out("warn", f"couldn't remove the {name} skill link in {tilde(sdir, env)}")
    return finished


def status(agent, root, link, env):
    """What `metacomputer agents` reports for one agent (no secrets: the server only in claude_mcp's words)."""
    prog, why = agents.trusted(agent.prog)
    st = {"installed": bool(prog or why), "version": agents.version(prog, env=agent.env(env)) if prog else None,
          "choice": agents.choices().get(agent.name)}
    if why:
        st["untrusted"] = why
    if not prog:
        return st
    res = agent.read(prog, env)
    own = classify(res, root, link)
    st["mcp_ok"] = own == "ok"
    st["mcp_server"] = describe(res, own, env) if own not in ("absent", "error") else None
    if own == "error":
        st["mcp_error"] = res.get("why")
    sdir = agent.skills_dir(env)
    st["skills_dir"] = tilde(sdir, env)
    st["skills"] = {n: agents.link_target(os.path.join(sdir, n)) for n in SKILLS}
    st["set_up"] = st["mcp_ok"]
    return st


def main(argv):
    if len(argv) != 6 or argv[0] not in ("install", "uninstall", "status") or argv[1] not in AGENTS \
            or argv[2] != "--root" or argv[4] != "--link":
        print("usage: agent_setup.py install|uninstall|status openclaw|hermes|gemini --root CHECKOUT --link "
              "~/.local/bin/metacomputer", file=sys.stderr)
        return 2
    what, agent, root, link = argv[0], AGENTS[argv[1]], argv[3], argv[5]
    env = dict(os.environ)
    if what == "status":
        print(json.dumps(status(agent, root, link, env), indent=2))
        return 0
    out = Out()
    try:
        done = (install if what == "install" else uninstall)(agent, root, link, env, out)
    except Exception as e:          # never a traceback that could quote a configuration: say what failed, by its type
        out("warn", f"the {agent.label} step stopped unexpectedly ({type(e).__name__})")
        done = False
    out.flush()
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
