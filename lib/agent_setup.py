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
missing one. Each read, change and read-back happens under a lock of the agent's own (agent_lock: two metacomputer
installs never interleave on one agent), the entry is read again right before any change (its digest, or that it's
still missing), and read back after it. What's left (docs/commands.md, "The other agents", says the same): these agents'
CLIs have no compare-and-swap, and the lock binds only metacomputer, so another program (the agent itself, an editor,
another tool) that writes an entry named metacomputer between that last read and the CLI's own write, a window of one
CLI call, isn't seen: an add replaces it (Gemini CLI's `mcp add`, Hermes Agent's `config set`; OpenClaw's `mcp add`
refuses an existing name instead), and a removal, which these CLIs make by name only, removes it. The read-back after
an add sees what won; the one after a removal can only confirm the name is gone. A skill link swapped for someone else's between metacomputer's
check and its unlink is removed the same way.

Skill links are metacomputer's only when they point into this checkout's own skill folders (skills/, .claude/skills/;
a relative target is read from the link's folder): only those are ever replaced or removed. A skill that can't be linked
(something else there, or an error), or that OpenClaw doesn't offer, leaves the step unfinished. Hermes Agent's profile is
resolved once (`hermes config path`) and every command and the skills folder use it. Uninstall keeps
~/.local/bin/metacomputer and mcomp whenever an agent's configuration can't be inspected in full and may still run them
(its CLI missing or refused: Gemini CLI's settings.json is read directly, all of it or it counts as maybe; the others'
files are looked through for the names, and a file that includes others, is past the read, can't be read, or sits
behind a folder that can't be searched or listed counts as maybe too), and on any unexpected error. The step's output, one line each, for install.sh: "ok TEXT", "warn TEXT", "say TEXT", and for --uninstall
"keep" when an entry that stays runs ~/.local/bin/metacomputer (or mcomp), so those stay too. Exit status: 0 finished,
1 unfinished (install.sh records that in installed.json's "incomplete", so `metacomputer update` runs the step again),
2 usage. Nothing printed holds a value from the agent's configuration but metacomputer's own command.

Stdlib only; Python 3.11 or later (as lib/agents.py).
"""
import contextlib
import json
import os
import stat
import sys

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import agents        # noqa: E402  (trusted, run, version, link_target, choices)
import claude_mcp    # noqa: E402  (words, digest, linked, MCP_ARGS)
import flockwait     # noqa: E402  (the lock's wait)

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
    reads_files = False     # read() works without the agent's CLI (Gemini CLI's settings.json)
    prefix = ()             # arguments every command gets after the program (Hermes Agent's --profile)

    def env(self, env):
        return dict(env)

    def pin(self, prog, env):
        """(env, None), or (env, why) when the configuration this run acts on can't be told."""
        return env, None

    def config_files(self, env):
        """The files that may hold its MCP servers, for an uninstall that can't run its CLI."""
        return []

    def config_dirs(self, env):
        """The folders config_files lists to find some of them (profiles): one that can't be listed hides what's in it."""
        return []

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
        return agents.run([argv[0], *self.prefix, *argv[1:]], env=self.env(env), cwd=home(env))

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

    reads_files = True

    def skills_dir(self, env):
        return os.path.join(self.base(env), ".agents", "skills")

    def config_files(self, env):
        return [self.settings(env)]

    def read(self, prog, env):
        path = self.settings(env)
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read(SETTINGS_CAP + 1)
        except (FileNotFoundError, NotADirectoryError):
            return {"kind": "absent"}
        except (OSError, UnicodeDecodeError):
            return {"kind": "error", "why": "its settings.json couldn't be read"}
        if len(text) > SETTINGS_CAP:          # what's past the read could be anything, metacomputer's entry included
            return {"kind": "error", "why": "its settings.json is larger than 4 MB, so it wasn't read in full"}
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
        return ([os.path.join(env["HERMES_HOME"], "config.yaml")] if env.get("HERMES_HOME") else []) + \
            [os.path.join(root, "config.yaml")] + [os.path.join(d, "config.yaml") for d in entries(os.path.join(root, "profiles"))]

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
        value = {"command": link, "args": list(claude_mcp.MCP_ARGS), "timeout": TOOL_TIMEOUT}
        return [prog, "config", "set", f"mcp_servers.{NAME}", json.dumps(value)]

    def remove_argv(self, prog):
        return [prog, "config", "unset", f"mcp_servers.{NAME}"]

    def add_line(self, link_shown):
        return (f"{self.cli()} config set mcp_servers.{NAME} '{{\"command\": \"{link_shown}\", \"args\": [\"mcp-lazy\"], "
                f"\"timeout\": {TOOL_TIMEOUT}}}'")

    def remove_line(self):
        return f"{self.cli()} config unset mcp_servers.{NAME}"


class OpenClaw(Agent):
    """OpenClaw: mcp.servers in ~/.openclaw/openclaw.json (OPENCLAW_CONFIG_PATH, OPENCLAW_STATE_DIR move it), read with
    `openclaw mcp show metacomputer --json`, added with `openclaw mcp add --no-probe` (which refuses a name that's
    there) with a 600 s request timeout (its default is 60 s), removed with `openclaw mcp unset`. Skills: it reads
    ~/.agents/skills with its default state folder; `openclaw skills info` says whether it sees them."""
    name, label, prog, tools, doc = "openclaw", "OpenClaw", "openclaw", "metacomputer__*", "docs/openclaw.md"

    def env(self, env):
        return dict(env, OPENCLAW_NO_AUTO_UPDATE="1")        # no update check for a quick command

    def config_files(self, env):
        if env.get("OPENCLAW_CONFIG_PATH"):
            return [env["OPENCLAW_CONFIG_PATH"]]
        state = env.get("OPENCLAW_STATE_DIR") or os.path.join(home(env), ".openclaw")
        return [os.path.join(state, "openclaw.json")] + [os.path.join(d, "openclaw.json")
                                                          for d in entries(home(env), ".openclaw-")]

    def config_dirs(self, env):
        return [] if env.get("OPENCLAW_CONFIG_PATH") else [home(env)]

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
def _target(path):
    """Where a link points, its relative target read from the link's own folder, normalized (not followed)."""
    t = os.readlink(path)
    return os.path.normpath(t if os.path.isabs(t) else os.path.join(os.path.dirname(path), t))


def _roots(root):
    return {os.path.normpath(root), os.path.realpath(root)}


def ours_link(path, root):
    """A link metacomputer made: it points into this checkout's own skill folders (skills/, .claude/skills/)."""
    try:
        t = _target(path)
    except OSError:
        return False
    return any(os.path.dirname(t) in (os.path.join(r, "skills"), os.path.join(r, ".claude", "skills"))
               for r in _roots(root))


def ensure_skill(path, target, root):
    """"ok" (already this link), "made", "foreign" (something else there, a link metacomputer didn't make included:
    left alone) or "error"."""
    try:
        if os.path.islink(path):
            want = {os.path.normpath(target)} | {os.path.join(r, os.path.relpath(target, root)) for r in _roots(root)}
            if _target(path) in want:
                return "ok"
            if not ours_link(path, root):
                return "foreign"
            os.unlink(path)
        elif os.path.lexists(path):
            return "foreign"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return "made"
    except OSError:
        return "error"


# --- locks and files ---------------------------------------------------------------------------------------------
class Busy(Exception):
    pass


def lock_path(name, env):
    return os.path.join(home(env), ".local", "share", "metacomputer", "locks", f"agent-{name}.lock")


@contextlib.contextmanager
def agent_lock(name, env):
    """The agent's own lock (a flock on a file of this account's, MCOMP_AGENT_LOCK_WAIT seconds at most, 60 by default),
    held around each read, change and read-back of its configuration and skill links: two metacomputer runs never
    interleave on one agent. The agent's own CLI doesn't take it (it has none of its own to honor)."""
    path = lock_path(name, env)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        flockwait.exclusive(fd, float(os.environ.get("MCOMP_AGENT_LOCK_WAIT", "60")), 0.1)
    except BlockingIOError:
        os.close(fd)
        raise Busy() from None
    try:
        yield
    finally:
        os.close(fd)


def entries(folder, prefix=""):
    """Every entry of a folder whose name starts with prefix (profiles), whether or not it can be looked into: one that
    can't is checked as a place a configuration may be (mentions), never left out as a glob would."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []                   # a folder that can't be listed is unlisted()'s to report
    return [os.path.join(folder, n) for n in names if n.startswith(prefix) and n not in (".", "..")]


MENTIONS_CAP = 16 << 20
SETTINGS_CAP = 4 << 20          # Gemini CLI's settings.json, read whole
# What makes a configuration file pull in others, which an uninstall without the agent's CLI doesn't follow: OpenClaw's
# `$include` (JSON5), and YAML's `!include` tag.
INCLUDES = (b"$include", b"!include")


def mentions(path):
    """Why a configuration file may run metacomputer's command, or None when it plainly doesn't (it isn't there, its
    folder included). Anything not inspected in full counts as maybe: "names it" (metacomputer or mcomp), "can't be
    looked at" (a folder on its way can't be searched: it may be there), "can't be read", "is larger than 16 MB" (only
    the start was read) or "includes other files" (INCLUDES: what they hold wasn't read)."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return "can't be looked at"
    try:
        with open(path, "rb") as f:
            data = f.read(MENTIONS_CAP + 1)
    except FileNotFoundError:
        return None
    except OSError:
        return "can't be read"
    if b"metacomputer" in data or b"mcomp" in data:
        return "names it"
    if len(data) > MENTIONS_CAP:
        return "is larger than 16 MB"
    if any(i in data for i in INCLUDES):
        return "includes other files"
    return None


def unlisted(folder):
    """Why a folder searched for configuration files (profiles) may hide one, or None: "can't be listed" (it can't be
    looked at, or isn't readable and searchable for this account). Checked without listing it."""
    try:
        if not stat.S_ISDIR(os.stat(folder).st_mode):
            return None
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return "can't be listed"
    return None if os.access(folder, os.R_OK | os.X_OK) else "can't be listed"


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


def _register(agent, prog, root, link, env, out):
    """The MCP server, under the agent's lock: (finished, registered)."""
    link_shown = tilde(link, env)
    again_line = f"./install.sh --only-{agent.name}"
    res = agent.read(prog, env)
    own = classify(res, root, link)
    if own == "error":
        out("warn", f"couldn't read {agent.label}'s MCP server {NAME} ({res.get('why')}), so it was left as it is; "
                    f"try {again_line} again")
        return False, False
    if own == "foreign":
        out("warn", f"{agent.label} already has an MCP server named {NAME} that isn't metacomputer's (it runs "
                    f"{describe(res, own, env)}); it was left as it is.")
        out("warn", f"To use metacomputer's instead: {agent.remove_line()}, then {again_line}")
        return False, False
    if own == "ok":
        out("ok", f"MCP server {NAME} already registered (tools: {agent.tools})")
        return True, True
    # Missing: read again right before the add (Gemini's and Hermes's adds replace an entry of the same name), then add
    # it and read it back.
    if classify(agent.read(prog, env), root, link) != "absent":
        out("warn", f"{agent.label}'s MCP server {NAME} changed since it was read, so it was left as it is; run "
                    f"{again_line} again")
        return False, False
    agent.run(agent.add_argv(prog, link), env)
    if classify(agent.read(prog, env), root, link) == "ok":
        out("ok", f"MCP server {NAME} for every {agent.label} session (tools: {agent.tools}, there even while the VM is "
                  "off)")
        return True, True
    out("warn", f"could not add the MCP server to {agent.label}; run: {agent.add_line(link_shown)}")
    return False, False


def _link_skills(agent, root, env, out):
    """The skill links, under the agent's lock: True when both are there."""
    done = True
    sdir = agent.skills_dir(env)
    where = tilde(sdir, env)
    for name in SKILLS:
        state = ensure_skill(os.path.join(sdir, name), skill_target(root, name), root)
        if state in ("ok", "made"):
            out("ok", f"skill {name} ({where}, read by {agent.label})")
            continue
        done = False
        if state == "foreign":
            out("warn", f"{where}/{name} is already there and isn't metacomputer's; left alone ({agent.label} won't get "
                        f"the {name} skill; move it away, then ./install.sh --only-{agent.name})")
        else:
            out("warn", f"couldn't link the {name} skill into {where} ({agent.label} won't get it)")
    return done


def install(agent, root, link, env, out):
    """Set metacomputer up for one agent: its MCP server and its skills. True when everything finished."""
    prog, why = agents.trusted(agent.prog)
    if not prog:
        out("warn", why or f"{agent.label}'s {agent.prog} command isn't on PATH; after installing it, run: "
                           f"./install.sh --only-{agent.name}")
        return False
    env, why = agent.pin(prog, env)
    if why:
        out("warn", f"{why}, so {agent.label} was left as it is; try ./install.sh --only-{agent.name} again")
        return False
    try:
        with agent_lock(agent.name, env):
            finished, registered = _register(agent, prog, root, link, env, out)
            finished = _link_skills(agent, root, env, out) and finished
    except Busy:
        out("warn", f"another metacomputer install is changing {agent.label}'s setup right now, so nothing was changed; "
                    f"run ./install.sh --only-{agent.name} again")
        return False
    if agent.note and registered:
        out("say", agent.note)
    seen = agent.check_skill(prog, env)
    if seen is not None:
        out("ok" if seen[0] else "warn", seen[1])
        finished = finished and seen[0]
    return finished


def _unregister(agent, prog, root, link, env, out):
    """The MCP server, under the agent's lock: True when it finished. prog None: the agent's CLI isn't available."""
    if prog is None and not agent.reads_files:
        files = [(f, mentions(f)) for f in agent.config_files(env)] + \
            [(d, unlisted(d)) for d in agent.config_dirs(env)]
        files = [(f, why) for f, why in files if why]
        if files:
            out("warn", f"{agent.label}'s {agent.prog} command isn't available, so its configuration couldn't be checked "
                        f"in full ({'; '.join(tilde(f, env) + ' ' + why for f, why in files)}): it may run "
                        "metacomputer's command, so that stays")
            out("keep", f"{agent.label}'s")
            return False
        return True
    res = agent.read(prog, env)
    own = classify(res, root, link)
    if own == "absent":
        return True
    if own == "error":
        out("warn", f"couldn't read {agent.label}'s MCP server {NAME} ({res.get('why')}), so it was left as it is;")
        out("warn", f"run ./install.sh --uninstall again, or remove it yourself: {agent.remove_line()}")
        out("keep", f"{agent.label}'s")
        return False
    here = own == "ok" and (res["entry"]["command"] != link or points_here(link, root))
    if not here:
        out("say", f"{agent.label}'s MCP server {NAME} stays: it runs {describe(res, own, env)}, which isn't this "
                   "checkout's")
        if claude_mcp.linked(res, link):
            out("keep", f"{agent.label}'s")
        return True
    if prog is None:
        out("warn", f"{agent.label}'s MCP server {NAME} is metacomputer's, but its {agent.prog} command isn't available "
                    f"to remove it: remove the {NAME} entry from {tilde(agent.config_files(env)[0], env)} yourself")
        out("keep", f"{agent.label}'s")
        return False
    if digest(agent.read(prog, env)) != digest(res):
        out("warn", f"{agent.label}'s MCP server {NAME} changed since it was read, so it was left as it is;")
        out("warn", f"run ./install.sh --uninstall again, or remove it yourself: {agent.remove_line()}")
        out("keep", f"{agent.label}'s")
        return False
    agent.run(agent.remove_argv(prog), env)
    if agent.read(prog, env)["kind"] == "absent":
        out("ok", f"removed {agent.label}'s {NAME} MCP server")
        return True
    out("warn", f"couldn't remove {agent.label}'s {NAME} MCP server (or confirm it's gone); remove it yourself: "
                f"{agent.remove_line()}")
    out("keep", f"{agent.label}'s")
    return False


def uninstall(agent, root, link, env, out):
    """Remove what this checkout set up for one agent: its MCP server when it's metacomputer's own and runs this
    checkout (read again right before, and checked after), and the skill links metacomputer made. An entry that can't be
    read keeps the command links (keep). True when it finished."""
    prog, _ = agents.trusted(agent.prog)
    if prog:
        env, why = agent.pin(prog, env)
        if why:
            out("warn", f"{why}, so {agent.label}'s setup was left as it is; run ./install.sh --uninstall again")
            out("keep", f"{agent.label}'s")
            return False
    try:
        with agent_lock(agent.name, env):
            finished = _unregister(agent, prog, root, link, env, out)
            sdir = agent.skills_dir(env)
            for name in SKILLS:
                path = os.path.join(sdir, name)
                try:
                    if os.path.islink(path) and ours_link(path, root):
                        os.unlink(path)
                        out("ok", f"removed the {name} skill link for {agent.label}")
                except OSError:
                    out("warn", f"couldn't remove the {name} skill link in {tilde(sdir, env)}")
                    finished = False
    except Busy:
        out("warn", f"another metacomputer install is changing {agent.label}'s setup right now, so it was left as it "
                    "is; run ./install.sh --uninstall again")
        out("keep", f"{agent.label}'s")
        return False
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
    env, why = agent.pin(prog, env)
    if why:
        st.update(mcp_ok=False, mcp_server=None, mcp_error=why, set_up=False)
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
    if argv[:1] == ["link-ours"]:          # install.sh's Codex step: the same ownership rule for its skill links
        if len(argv) != 4 or argv[2] != "--root":
            print("usage: agent_setup.py link-ours PATH --root CHECKOUT", file=sys.stderr)
            return 2
        return 0 if os.path.islink(argv[1]) and ours_link(argv[1], argv[3]) else 1
    if len(argv) != 6 or argv[0] not in ("install", "uninstall", "status") or argv[1] not in AGENTS \
            or argv[2] != "--root" or argv[4] != "--link":
        print("usage: agent_setup.py install|uninstall|status openclaw|hermes|gemini --root CHECKOUT --link "
              "~/.local/bin/metacomputer\n       agent_setup.py link-ours PATH --root CHECKOUT", file=sys.stderr)
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
        if what == "uninstall":     # its configuration wasn't checked: it may run the command, which stays
            out("warn", f"{agent.label}'s configuration wasn't checked, so the command stays; run ./install.sh "
                        "--uninstall again")
            out("keep", f"{agent.label}'s")
        done = False
    out.flush()
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
