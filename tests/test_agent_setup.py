#!/usr/bin/env python3
"""install.sh's steps for OpenClaw, Hermes Agent, Gemini CLI and Antigravity CLI (lib/agent_setup.py), `metacomputer
agents` about them, and their packages: the Agent Plugins package (agent-plugin/) and the Gemini CLI extension
(gemini-extension.json).

    python3 tests/test_agent_setup.py

No VM, no network, no real agent: stand-in `openclaw`, `hermes`, `gemini` and `agy` commands answer the parts of each CLI
the step uses, as the real ones do (checked against OpenClaw 2026.11.7, Hermes Agent 0.23.3, Gemini CLI 0.70.0 and
Antigravity CLI 1.5.15 in throwaway homes: docs/openclaw.md, docs/hermes.md, docs/gemini.md, docs/antigravity.md), keep
what they're told in a temporary HOME, and log how they were called. Each runs this checkout's install.sh with a
temporary HOME.

- Registration: `--only-NAME` adds the MCP server metacomputer, ~/.local/bin/metacomputer mcp-lazy, with the agent's
  own command (Gemini CLI: `mcp add -s user --trust`; Hermes Agent: `config set` with a 600 s timeout; OpenClaw: `mcp
  add --no-probe` with a 600 s timeout), reads it back, links the skills where the agent reads them, remembers the
  choice, and clears an earlier unfinished step in installed.json; a second run changes nothing.
- Ownership: an entry named metacomputer that isn't metacomputer's (another program, other arguments) is never changed
  or removed: the run names it by its program and how many arguments it has (never an argument or a variable), says
  how to remove it, ends with exit 1 and records the step as unfinished; so does a lookup that fails, an entry that
  changed between the read and the change, and an add that didn't take. A skill folder that isn't metacomputer's stays.
- Uninstall: removes metacomputer's own entry (read again right before) and the skill links that point here; an entry
  that isn't this checkout's stays, and one that runs ~/.local/bin/metacomputer keeps the command links.
- --no-NAME is remembered until --NAME; --only-NAME with --no-NAME is refused.
- Nothing printed holds a value from the agent's configuration (the stand-ins' entries carry made-up secrets).
- `metacomputer agents --json` reports each agent: installed, its version, set up or not, its server in words.
- Gemini CLI's settings.json is JSON with comments (read here as it reads it: comments yes, trailing commas no);
  GEMINI_CLI_HOME moves it.
- Antigravity CLI's mcp_config.json (~/.gemini/config) is read as agy reads it (comments and trailing commas), while its
  own `agy mcp` command takes plain JSON only; metacomputer's entry gets timeoutSeconds 600 (one you set stays), written
  only over the file it read.
- agent-plugin/ is a valid Agent Plugins 1.0.0 package (its manifests, skill names and frontmatter as Hermes Agent's
  validator wants them; its launcher hands over to the installed checkout and to nothing else), and
  gemini-extension.json runs that launcher with the context file it names.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(LAB, "install.sh")
sys.path.insert(0, os.path.join(LAB, "lib"))
import agent_setup  # noqa: E402

FAILS = []
SECRET = "qqqq-made-up-qqqq"        # in the stand-ins' foreign entries: must never be printed


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[-900:]})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


# --- stand-ins ---------------------------------------------------------------------------------------------------
COMMON = r'''
import fcntl, json, os, sys
a = sys.argv[1:]
def lock_held():
    """Whether metacomputer's lock for this agent is held right now (lib/agent_setup.py agent_lock)."""
    base = os.path.basename(sys.argv[0])
    p = os.path.join(os.environ["HOME"], ".local", "share", "metacomputer", "locks",
                     "agent-" + {"agy": "antigravity"}.get(base, base) + ".lock")
    try:
        fd = os.open(p, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
log = os.environ.get("STUB_LOG")
if log:
    open(log, "a").write(json.dumps([os.path.basename(sys.argv[0])] + a + ["#locked" if lock_held() else "#unlocked"]) + "\n")
def count(name):
    """How many times this stand-in has been asked NAME (kept beside its state)."""
    p = os.path.join(os.environ["HOME"], ".stub-count-" + name)
    n = int(open(p).read()) + 1 if os.path.exists(p) else 1
    open(p, "w").write(str(n))
    return n
FOREIGN = {"command": "/opt/other/bin/server", "args": ["--token", "qqqq-made-up-qqqq"], "env": {"API_KEY": "qqqq-made-up-qqqq"}}
'''

# Gemini CLI: user-scope servers in settings.json (JSON with comments), `mcp add -s user [--trust] NAME CMD ARGS` replaces
# an entry of that name without asking, `mcp remove -s user NAME` exits 0 even when there's none.
GEMINI = COMMON + r'''
import re
home = os.environ.get("GEMINI_CLI_HOME") or os.environ["HOME"]
path = os.path.join(home, ".gemini", "settings.json")
def load():
    if not os.path.exists(path):
        return {}
    text = re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M)
    return json.loads(text) if text.strip() else {}
if a == ["--version"]:
    print("0.70.0"); sys.exit(0)
if a[:2] == ["mcp", "add"]:
    rest = a[2:]
    assert rest[:2] == ["-s", "user"], rest
    rest = rest[2:]
    trust = rest[:1] == ["--trust"]
    rest = rest[1:] if trust else rest
    if os.environ.get("STUB_FAIL_ADD"):
        print("Error: could not write settings", file=sys.stderr); sys.exit(1)
    name, cmd, args = rest[0], rest[1], rest[2:]
    if os.environ.get("STUB_ADD_OTHER"):
        cmd = "/opt/other/bin/server"
    d = load()
    e = {"command": cmd, "args": args}
    if trust:
        e["trust"] = True
    d.setdefault("mcpServers", {})[name] = e
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(d, open(path, "w"), indent=2)
    print(f'MCP server "{name}" added to user settings. (stdio)'); sys.exit(0)
if a[:2] == ["mcp", "remove"]:
    name = a[-1]
    d = load()
    if os.environ.get("STUB_FAIL_REMOVE") or name not in d.get("mcpServers", {}):
        print(f'Server "{name}" not found in user settings.'); sys.exit(0)
    del d["mcpServers"][name]
    json.dump(d, open(path, "w"), indent=2)
    print(f'Server "{name}" removed from user settings.'); sys.exit(0)
print("stub gemini: unhandled " + " ".join(a), file=sys.stderr); sys.exit(2)
'''

# Antigravity CLI: ~/.gemini/config/mcp_config.json; `mcp add NAME CMD ARGS` adds or replaces an entry of that name (the
# whole entry: what else it had goes) and writes the file again as plain JSON, sorted, with "disabled": false; `mcp
# remove NAME` exits 1 when there's none; both refuse a file with comments or trailing commas.
AGY = COMMON + r'''
path = os.path.join(os.environ["HOME"], ".gemini", "config", "mcp_config.json")
def load():
    if not os.path.exists(path):
        return {}
    text = open(path).read()
    return json.loads(text) if text.strip() else {}
def save(d):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(json.dumps(d, indent=2, sort_keys=True) + "\n")
if a == ["--version"]:
    print("1.5.15"); sys.exit(0)
if a[:2] in (["mcp", "add"], ["mcp", "remove"]):
    try:
        d = load()
    except ValueError:
        print(f"Error: failed to parse {path}: failed to parse JSON config", file=sys.stderr); sys.exit(1)
if a[:2] == ["mcp", "add"]:
    if os.environ.get("STUB_FAIL_ADD"):
        print("Error: could not write the configuration", file=sys.stderr); sys.exit(1)
    name, cmd, args = a[2], a[3], a[4:]
    if os.environ.get("STUB_ADD_OTHER"):
        cmd = "/opt/other/bin/server"
    d.setdefault("mcpServers", {})[name] = {"args": args, "command": cmd, "disabled": False}
    save(d)
    print(f'Added MCP server "{name}" (stdio)'); sys.exit(0)
if a[:2] == ["mcp", "remove"]:
    name = a[2]
    if os.environ.get("STUB_FAIL_UNSET"):
        print("Error: could not write the configuration", file=sys.stderr); sys.exit(1)
    if name not in d.get("mcpServers", {}):
        print(f'Error: MCP server "{name}" not found', file=sys.stderr); sys.exit(1)
    del d["mcpServers"][name]
    save(d)
    print(f'Removed MCP server "{name}"'); sys.exit(0)
print("stub agy: unhandled " + " ".join(a), file=sys.stderr); sys.exit(2)
'''

# Hermes Agent: config.yaml's mcp_servers through `hermes config get KEY --json` (exit 1 "Config key not set: KEY" on
# stderr), `config set KEY JSON` (replaces) and `config unset KEY` (exit 1 when there's none). Kept here as JSON.
HERMES = COMMON + r'''
# Profiles as Hermes keeps them: --profile NAME first, else a HERMES_HOME that is a profile folder as it is, else the
# sticky choice (~/.hermes/active_profile, `hermes profile use`), even under a root HERMES_HOME.
root = os.path.join(os.environ["HOME"], ".hermes")
named = None
if a[:1] in (["--profile"], ["-p"]):
    named, a = a[1], a[2:]
hh = os.environ.get("HERMES_HOME")
if hh and os.path.basename(os.path.dirname(hh)) == "profiles" and not named:
    home = hh
else:
    sticky = open(os.path.join(root, "active_profile")).read().strip() if os.path.exists(os.path.join(root, "active_profile")) else ""
    name = named or sticky or "default"
    home = (hh or root) if name == "default" else os.path.join(root, "profiles", name)
path = os.path.join(home, "stub-config.json")
def load():
    return json.load(open(path)) if os.path.exists(path) else {}
def save(d):                    # and as config.yaml (JSON is YAML), the file an uninstall without the CLI looks through
    os.makedirs(home, exist_ok=True); json.dump(d, open(path, "w")); json.dump(d, open(os.path.join(home, "config.yaml"), "w"))
def get(d, key):
    for k in key.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d
if a == ["--version"]:
    print("Hermes Agent v0.23.3 (2026.9.24) · stable · local caafe5389ae5"); print("Python: 3.14.9"); sys.exit(0)
if a == ["config", "path"]:
    if os.environ.get("STUB_FAIL_PATH"):
        sys.exit(1)
    print(os.path.join(home, "config.yaml"))
    if os.environ.get("STUB_SWITCH_AFTER_PATH"):        # someone runs `hermes profile use other` meanwhile
        open(os.path.join(root, "active_profile"), "w").write("other")
    sys.exit(0)
if a[:2] == ["config", "get"]:
    if os.environ.get("STUB_FAIL_GET"):
        print("Error: couldn't load the configuration", file=sys.stderr); sys.exit(2)
    d = load()
    n = count("get")
    if os.environ.get("STUB_MUTATE_AT_GET") == str(n):
        d.setdefault("mcp_servers", {})["metacomputer"] = FOREIGN; save(d)
    v = get(d, a[2])
    if v is None:
        print(f"Config key not set: {a[2]}", file=sys.stderr); sys.exit(1)
    print(json.dumps(v)); sys.exit(0)
if a[:2] == ["config", "set"]:
    if os.environ.get("STUB_FAIL_SET"):
        print("Error: couldn't write", file=sys.stderr); sys.exit(1)
    d = load(); k1, k2 = a[2].split(".")
    d.setdefault(k1, {})[k2] = json.loads(a[3]); save(d)
    print(f"✓ Set {a[2]} = {a[3]}"); sys.exit(0)
if a[:2] == ["config", "unset"]:
    d = load(); k1, k2 = a[2].split(".")
    if os.environ.get("STUB_FAIL_UNSET") or k2 not in d.get(k1, {}):
        print(f"Config key not set: {a[2]}"); sys.exit(1)
    del d[k1][k2]; save(d)
    print(f"✓ Unset {a[2]}"); sys.exit(0)
print("stub hermes: unhandled " + " ".join(a), file=sys.stderr); sys.exit(2)
'''

# OpenClaw: mcp.servers in openclaw.json; `mcp show NAME --json` (exit 1 with {"ok": false, "error": {...}} when
# there's none), `mcp add NAME --command C --arg A --timeout S --no-probe` (refuses a name that's there), `mcp unset NAME`,
# `skills info NAME --json` (from ~/.agents/skills).
OPENCLAW = COMMON + r'''
path = os.environ.get("OPENCLAW_CONFIG_PATH") or os.path.join(
    os.environ.get("OPENCLAW_STATE_DIR") or os.path.join(os.environ["HOME"], ".openclaw"), "openclaw.json")
def load():
    return json.load(open(path)) if os.path.exists(path) else {}
def save(d):
    os.makedirs(os.path.dirname(path), exist_ok=True); json.dump(d, open(path, "w"), indent=2)
def servers(d):
    return d.setdefault("mcp", {}).setdefault("servers", {})
def missing(name):
    print(json.dumps({"ok": False, "error": {"type": "cli_error", "message":
          f'No MCP server named "{name}" in {path}. Run openclaw mcp list to see configured servers.'}})); sys.exit(1)
if a == ["--version"]:
    print("OpenClaw 2026.11.7 (e31bbf0)"); sys.exit(0)
if a[:2] == ["mcp", "show"]:
    if os.environ.get("STUB_FAIL_SHOW"):
        print("Config invalid", file=sys.stderr); sys.exit(1)
    d = load()
    n = count("show")
    if os.environ.get("STUB_MUTATE_AT_GET") == str(n):
        servers(d)["metacomputer"] = FOREIGN; save(d)
    e = servers(d).get(a[2])
    if e is None:
        missing(a[2])
    shown = dict(e)
    if "env" in shown:
        shown["env"] = {k: "__OPENCLAW_REDACTED__" for k in shown["env"]}
    print(json.dumps(shown, indent=2)); sys.exit(0)
if a[:2] == ["mcp", "add"]:
    name = a[2]
    d = load()
    if name in servers(d):
        print(f'MCP server "{name}" already exists.', file=sys.stderr); sys.exit(1)
    if os.environ.get("STUB_FAIL_ADD"):
        print("Configuration was not saved.", file=sys.stderr); sys.exit(1)
    assert "--no-probe" in a, a
    e = {"command": a[a.index("--command") + 1], "args": [a[i + 1] for i, x in enumerate(a) if x == "--arg"]}
    if "--timeout" in a:
        e["requestTimeoutMs"] = int(a[a.index("--timeout") + 1]) * 1000
    servers(d)[name] = e; save(d)
    print(f'Saved MCP server "{name}" to {path}.'); sys.exit(0)
if a[:2] == ["mcp", "unset"]:
    d = load()
    if a[2] not in servers(d):
        missing(a[2])
    if os.environ.get("STUB_FAIL_UNSET"):
        print(f"Config write rejected: {path} (size-drop:1004->458)", file=sys.stderr); sys.exit(1)
    del servers(d)[a[2]]; save(d)
    print(f'Removed MCP server "{a[2]}" from {path}.'); sys.exit(0)
if a[:2] == ["skills", "info"]:
    p = os.path.join(os.environ["HOME"], ".agents", "skills", a[2], "SKILL.md")
    if not os.path.exists(p):
        print(json.dumps({"ok": False, "error": {"message": f'Skill "{a[2]}" not found.'}})); sys.exit(1)
    print(json.dumps({"name": a[2], "source": "agents-skills-personal", "eligible": not os.environ.get("STUB_HIDDEN")}))
    sys.exit(0)
print("stub openclaw: unhandled " + " ".join(a), file=sys.stderr); sys.exit(2)
'''


class Box:
    """A temporary HOME with stand-in openclaw, hermes, gemini and agy commands on PATH, running this checkout's
    install.sh."""

    def __init__(self, agents=("openclaw", "hermes", "gemini", "antigravity")):
        self.t = tempfile.mkdtemp(prefix="mcomp-agents-")
        self.home = os.path.join(self.t, "home")
        self.bin = os.path.join(self.t, "bin")
        self.log = os.path.join(self.t, "calls.log")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        for name in agents:
            write(os.path.join(self.bin, CMD.get(name, name)), f"#!{sys.executable}\n" + {
                "openclaw": OPENCLAW, "hermes": HERMES, "gemini": GEMINI, "antigravity": AGY}[name], 0o755)
        py = os.path.dirname(os.path.realpath(sys.executable))
        self.env = {"HOME": self.home, "PATH": f"{self.bin}:{py}:/usr/bin:/bin", "LANG": "en_US.UTF-8",
                    "MCOMP_BREW_PYTHON": sys.executable, "STUB_LOG": self.log,
                    "MCOMP_CONFIG": os.path.join(self.t, "none.env"), "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                    # install.sh's lock of its own: the account's (one install at a time, for every checkout and
                    # session) made these installs wait 10 s and stop, printing nothing, while another install.sh of
                    # this account ran (another suite's or session's, a real install)
                    "MCOMP_LOCK_DIR": os.path.join(self.t, "locks")}
        self.link = os.path.join(self.home, ".local", "bin", "metacomputer")
        self.stamp = os.path.join(self.home, ".local", "share", "metacomputer", "installed.json")

    def install(self, *args, **env):
        if os.path.exists(self.log):
            os.remove(self.log)
        p = subprocess.run([INSTALL, *args], env=dict(self.env, **env), capture_output=True, text=True, timeout=300,
                           cwd=self.t, stdin=subprocess.DEVNULL)
        check(f"install.sh {' '.join(args)}: nothing printed holds a configured value", SECRET not in p.stdout + p.stderr,
              (p.stdout + p.stderr)[-600:])
        if "install: stopped" in p.stderr:
            check(f"install.sh {' '.join(args)}: no ERR-trap message", False, p.stderr[-600:])
        return p

    def calls(self):
        return [json.loads(x) for x in open(self.log)] if os.path.exists(self.log) else []

    def agents_json(self, **env):
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "agents.py"), "--json"], env=dict(self.env, **env),
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        check("agents --json: nothing printed holds a configured value", SECRET not in r.stdout + r.stderr, r.stdout[-400:])
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"error": r.stdout[-300:] + r.stderr[-300:]}

    def agents_text(self):
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "agents.py")], env=self.env, capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
        return r.stdout + r.stderr

    def stamp_set(self, incomplete):
        write(self.stamp, json.dumps({"version": "v0.4.5", "commit": "0" * 40, "checkout": LAB, "incomplete": incomplete}))

    def stamp_incomplete(self):
        try:
            return json.load(open(self.stamp)).get("incomplete") or []
        except (OSError, ValueError):
            return None

    def choices(self):
        try:
            return json.load(open(os.path.join(self.home, ".local", "share", "metacomputer", "agents.json")))
        except (OSError, ValueError):
            return {}

    def close(self):
        shutil.rmtree(self.t, ignore_errors=True)


CMD = {"antigravity": "agy"}       # an agent's command, where it isn't its name


# How each stand-in keeps the entry, for the checks: (read, seed).
def entry(b, name):
    if name == "antigravity":
        p = os.path.join(b.home, ".gemini", "config", "mcp_config.json")
        d = agent_setup.jsonc_loads(open(p).read(), trailing_commas=True) if os.path.exists(p) else {}
        return (d.get("mcpServers") or {}).get("metacomputer")
    if name == "gemini":
        p = os.path.join(b.home, ".gemini", "settings.json")
        d = json.loads(re.sub(r"^\s*//.*$", "", open(p).read(), flags=re.M)) if os.path.exists(p) else {}
        return (d.get("mcpServers") or {}).get("metacomputer")
    if name == "hermes":
        p = os.path.join(b.home, ".hermes", "stub-config.json")
        return (json.load(open(p)).get("mcp_servers") or {}).get("metacomputer") if os.path.exists(p) else None
    p = os.path.join(b.home, ".openclaw", "openclaw.json")
    return ((json.load(open(p)).get("mcp") or {}).get("servers") or {}).get("metacomputer") if os.path.exists(p) else None


def seed(b, name, e):
    if name == "antigravity":
        write(os.path.join(b.home, ".gemini", "config", "mcp_config.json"),
              json.dumps({"mcpServers": {"metacomputer": e, "other": {"command": "/bin/echo"}}}, indent=2) + "\n")
    elif name == "gemini":
        write(os.path.join(b.home, ".gemini", "settings.json"),
              "{\n  // the user's own settings\n  \"mcpServers\": " + json.dumps({"metacomputer": e}) + "\n}\n")
    elif name == "hermes":
        write(os.path.join(b.home, ".hermes", "stub-config.json"), json.dumps({"mcp_servers": {"metacomputer": e}}))
    else:
        write(os.path.join(b.home, ".openclaw", "openclaw.json"), json.dumps({"mcp": {"servers": {"metacomputer": e}}}))


def config_file(b, name):
    return os.path.join(b.home, {"gemini": ".gemini/settings.json", "hermes": ".hermes/stub-config.json",
                                 "openclaw": ".openclaw/openclaw.json",
                                 "antigravity": ".gemini/config/mcp_config.json"}[name])


def skills_dir(b, name):
    if name == "antigravity":
        return os.path.join(b.home, ".gemini", "antigravity-cli", "skills")
    return os.path.join(b.home, ".hermes", "skills") if name == "hermes" else os.path.join(b.home, ".agents", "skills")


def words(c):
    """A logged call without the program, Hermes Agent's --profile NAME and the lock marker."""
    w = c[3:] if c[1:2] in (["--profile"], ["-p"]) else c[1:]
    return [x for x in w if x not in ("#locked", "#unlocked")]


def mutations(b, name):
    return [c for c in b.calls() if c[0] == CMD.get(name, name) and (words(c)[:2] in (["mcp", "add"], ["mcp", "remove"], ["mcp", "unset"],
                                                                       ["config", "set"], ["config", "unset"]))]


LABEL = {"openclaw": "OpenClaw", "hermes": "Hermes Agent", "gemini": "Gemini CLI", "antigravity": "Antigravity CLI"}
REMOVE = {"openclaw": "openclaw mcp unset metacomputer", "hermes": "hermes config unset mcp_servers.metacomputer",
          "gemini": "gemini mcp remove -s user metacomputer", "antigravity": "agy mcp remove metacomputer"}


def agent_cases(name):
    label = LABEL[name]
    print(f"{label}: install.sh --only-{name}")
    b = Box()
    try:
        b.stamp_set([name, "codex"])
        p = b.install(f"--only-{name}")
        e = entry(b, name)
        own = isinstance(e, dict) and e.get("command") == b.link and e.get("args") == ["mcp-lazy"]
        extra = {"gemini": isinstance(e, dict) and e.get("trust") is True,
                 "hermes": isinstance(e, dict) and e.get("timeout") == 600,
                 "openclaw": isinstance(e, dict) and e.get("requestTimeoutMs") == 600000,
                 "antigravity": isinstance(e, dict) and e.get("timeoutSeconds") == 600}[name]
        check(f"{label}: the MCP server metacomputer, ~/.local/bin/metacomputer mcp-lazy, added with its own CLI and read "
              f"back ({'--trust' if name == 'gemini' else 'a 600 s timeout'})", p.returncode == 0 and own and extra
              and f"MCP server metacomputer for every {label} session" in p.stdout, (p.returncode, e, p.stdout[-600:]))
        sk = skills_dir(b, name)
        check(f"{label}: the skills metacomputer and cua-driver linked where it reads them",
              os.readlink(os.path.join(sk, "metacomputer")) == os.path.join(LAB, "skills", "metacomputer")
              and os.readlink(os.path.join(sk, "cua-driver")) == os.path.join(LAB, ".claude", "skills", "cua-driver"),
              os.listdir(sk) if os.path.isdir(sk) else None)
        check(f"{label}: the choice is remembered (agents.json {name}: on), the command linked, and an earlier unfinished "
              "step is cleared (another agent's stays)", b.choices().get(name) == "on"
              and os.readlink(b.link) == os.path.join(LAB, "bin", "metacomputer") and b.stamp_incomplete() == ["codex"],
              (b.choices(), b.stamp_incomplete()))
        p = b.install(f"--only-{name}")
        check(f"{label}: a second run finds it registered and changes nothing", p.returncode == 0
              and "already registered" in p.stdout and not mutations(b, name), (mutations(b, name), p.stdout[-300:]))
        st = b.agents_json()
        s = st.get(name) or {}
        check(f"{label}: agents --json reports it installed, its version, set up, its server and skills",
              s.get("installed") is True and s.get("version") and s.get("set_up") is True and s.get("mcp_ok") is True
              and s.get("mcp_server") == "~/.local/bin/metacomputer mcp-lazy" and all((s.get("skills") or {}).values()),
              s)
        text = b.agents_text()
        check(f"{label}: agents (text) says it's set up", re.search(rf"^{re.escape(label)} +\S.*: set up \(MCP server, skills",
                                                                     text, re.M) is not None, text[-600:])
        # --uninstall: metacomputer's own entry and the skill links go, and the command links (nothing else runs them)
        p = b.install("--uninstall")
        check(f"{label}: --uninstall removes its MCP server (read again first) and the skill links that point here",
              p.returncode == 0 and entry(b, name) is None and f"removed {label}'s metacomputer MCP server" in p.stdout
              and not os.path.lexists(os.path.join(sk, "metacomputer")) and not os.path.lexists(b.link),
              (p.returncode, entry(b, name), p.stdout[-500:]))
    finally:
        b.close()

    print(f"{label}: entries that aren't metacomputer's")
    b = Box()
    try:
        foreign = {"command": "/opt/other/bin/server", "args": ["--token", SECRET], "env": {"API_KEY": SECRET}}
        seed(b, name, foreign)
        before = open(config_file(b, name)).read()
        b.stamp_set([])
        p = b.install(f"--only-{name}")
        out = p.stdout + p.stderr
        check(f"{label}: another program's server named metacomputer is left byte for byte, named by its program and how "
              "many arguments it has, with the command to remove it; exit 1, recorded as unfinished",
              p.returncode == 1 and open(config_file(b, name)).read() == before and not mutations(b, name)
              and "isn't metacomputer's (it runs server with 2 arguments (not shown), with variables of its own)" in out
              and REMOVE[name] in out and f"./install.sh --only-{name}" in out and b.stamp_incomplete() == [name],
              (p.returncode, out[-700:], b.stamp_incomplete()))
        st = (b.agents_json().get(name) or {})
        check(f"{label}: agents --json says it isn't set up, and names the server by its program only",
              st.get("set_up") is False and st.get("mcp_server") == "server with 2 arguments (not shown), with variables "
              "of its own", st)
        mine_elsewhere = {"command": b.link, "args": ["--vm", "other", "mcp-lazy"]}
        seed(b, name, mine_elsewhere)
        p = b.install(f"--only-{name}")
        check(f"{label}: ~/.local/bin/metacomputer with other arguments isn't metacomputer's own either: left, exit 1",
              p.returncode == 1 and entry(b, name) == mine_elsewhere and "isn't metacomputer's" in p.stdout, p.stdout[-400:])
        p = b.install("--uninstall")
        check(f"{label}: --uninstall leaves it, and keeps the command links it runs",
              entry(b, name) == mine_elsewhere and os.path.islink(b.link) and "stays" in p.stdout
              and f"kept {b.link}" in p.stdout and f"({label}'s)" in p.stdout, (p.stdout[-600:], entry(b, name)))
        os.remove(config_file(b, name))
        # a skill folder that isn't metacomputer's
        sk = skills_dir(b, name)
        write(os.path.join(sk, "metacomputer", "SKILL.md"), "---\nname: metacomputer\ndescription: someone else's\n---\n")
        b.stamp_set([])
        p = b.install(f"--only-{name}")
        check(f"{label}: a skill folder named metacomputer that isn't a link here is left alone, said, and the step is "
              "unfinished (exit 1, recorded)", p.returncode == 1 and os.path.isdir(os.path.join(sk, "metacomputer"))
              and not os.path.islink(os.path.join(sk, "metacomputer"))
              and "isn't metacomputer's; left alone" in p.stdout and b.stamp_incomplete() == [name], p.stdout[-500:])
    finally:
        b.close()

    print(f"{label}: lookups and changes that fail")
    b = Box()
    try:
        b.stamp_set([])
        if name == "gemini":
            write(config_file(b, name), "{\n  \"mcpServers\": {\"x\": {\"command\": \"/bin/true\"},},\n}\n")
            p = b.install(f"--only-{name}")
            check("Gemini CLI: settings.json it can't read itself (a trailing comma) is never taken for one without the "
                  "server: left, exit 1, recorded", p.returncode == 1 and "couldn't read" in p.stdout
                  and not mutations(b, name) and b.stamp_incomplete() == [name], p.stdout[-400:])
            os.remove(config_file(b, name))
        elif name == "antigravity":
            text = "{\n  // the user's own\n  \"mcpServers\": {\"x\": {\"command\": \"/bin/true\"},},\n}\n"
            write(config_file(b, name), text)
            p = b.install(f"--only-{name}")
            check("Antigravity CLI: a mcp_config.json with comments and trailing commas is read as agy reads it (no "
                  "server there), and when its own `agy mcp add` refuses that file, the run says how to add the entry by "
                  "hand (its whole path), leaves the file as it was, exit 1, recorded",
                  p.returncode == 1 and open(config_file(b, name)).read() == text
                  and f'"command": "{b.link}"' in p.stdout and b.stamp_incomplete() == [name], p.stdout[-600:])
            write(config_file(b, name), "not json " + SECRET)
            p = b.install(f"--only-{name}")
            check("Antigravity CLI: a mcp_config.json it can't read at all is never taken for one without the server: "
                  "left, exit 1", p.returncode == 1 and "couldn't read" in p.stdout and not mutations(b, name),
                  p.stdout[-400:])
            os.remove(config_file(b, name))
        else:
            p = b.install(f"--only-{name}", STUB_FAIL_SHOW="1", STUB_FAIL_GET="1")
            check(f"{label}: a lookup that fails is never taken for no server: nothing changed, exit 1, recorded",
                  p.returncode == 1 and "couldn't read" in p.stdout and not mutations(b, name)
                  and b.stamp_incomplete() == [name], p.stdout[-400:])
            for f in os.listdir(b.home):
                if f.startswith(".stub-count-"):
                    os.remove(os.path.join(b.home, f))
            p = b.install(f"--only-{name}", STUB_MUTATE_AT_GET="2")
            check(f"{label}: an entry that appears between the read and the add is left as it is (no add), exit 1",
                  p.returncode == 1 and "changed since it was read" in p.stdout and not mutations(b, name)
                  and (entry(b, name) or {}).get("command") == "/opt/other/bin/server", (p.stdout[-400:], mutations(b, name)))
            os.remove(config_file(b, name))
        p = b.install(f"--only-{name}", STUB_FAIL_ADD="1", STUB_FAIL_SET="1")
        check(f"{label}: an add that fails says so with the command to run; exit 1, recorded",
              p.returncode == 1 and f"could not add the MCP server to {label}; run:" in p.stdout
              and "metacomputer" in p.stdout.split("run:")[-1] and b.stamp_incomplete() == [name], p.stdout[-400:])
        if name in ("gemini", "antigravity"):
            p = b.install(f"--only-{name}", STUB_ADD_OTHER="1")
            check(f"{label}: an add that wrote something else isn't taken for metacomputer's: exit 1",
                  p.returncode == 1 and "could not add the MCP server" in p.stdout, p.stdout[-300:])
            os.remove(config_file(b, name))
        p = b.install(f"--only-{name}")
        check(f"{label}: the next run that finishes clears the unfinished step", p.returncode == 0
              and b.stamp_incomplete() == [], (p.stdout[-300:], b.stamp_incomplete()))
        if name != "gemini":
            p = b.install("--uninstall", STUB_FAIL_UNSET="1")
            check(f"{label}: a removal that doesn't take says how to remove it yourself, and keeps the command links",
                  p.returncode != 0 and REMOVE[name] in p.stdout + p.stderr and os.path.islink(b.link)
                  and entry(b, name) is not None, (p.returncode, (p.stdout + p.stderr)[-500:]))
    finally:
        b.close()


def link_cases():
    """Skill links: only a link into this checkout's own skill folders is metacomputer's (fix: foreign links)."""
    print("skill links that aren't metacomputer's")
    b = Box(agents=("gemini",))
    try:
        sk = os.path.join(b.home, ".agents", "skills")
        os.makedirs(sk)
        os.symlink("/unmounted/custom/skills/metacomputer", os.path.join(sk, "metacomputer"))   # broken, someone's
        os.makedirs(os.path.join(b.home, "mine", "skills", "cua-driver"))
        os.symlink("../../mine/skills/cua-driver", os.path.join(sk, "cua-driver"))              # relative, valid
        b.stamp_set([])
        p = b.install("--only-gemini")
        check("a broken link of someone else's and a relative one that works from its own folder are left as they are; "
              "exit 1, recorded", p.returncode == 1
              and os.readlink(os.path.join(sk, "metacomputer")) == "/unmounted/custom/skills/metacomputer"
              and os.readlink(os.path.join(sk, "cua-driver")) == "../../mine/skills/cua-driver"
              and b.stamp_incomplete() == ["gemini"], (p.stdout[-500:], os.listdir(sk)))
        p = b.install("--uninstall")
        check("--uninstall leaves them too", os.path.islink(os.path.join(sk, "metacomputer"))
              and os.readlink(os.path.join(sk, "cua-driver")) == "../../mine/skills/cua-driver", p.stdout[-300:])
        for n in ("metacomputer", "cua-driver"):
            if os.path.lexists(os.path.join(sk, n)):
                os.remove(os.path.join(sk, n))
        os.symlink(os.path.relpath(os.path.join(LAB, "skills", "metacomputer"), sk), os.path.join(sk, "cua-driver"))
        p = b.install("--only-gemini")
        check("a link into this checkout's own skill folders (relative, the wrong skill) is metacomputer's: put right",
              p.returncode == 0 and os.readlink(os.path.join(sk, "cua-driver")) == os.path.join(LAB, ".claude", "skills",
                                                                                                "cua-driver"),
              p.stdout[-300:])
        os.chmod(sk, 0o555)
        try:
            p = b.install("--uninstall")
        finally:
            os.chmod(sk, 0o755)
        check("a skill link that can't be removed leaves the uninstall unfinished (exit 1)", p.returncode != 0
              and "couldn't remove the" in p.stdout, (p.returncode, p.stdout[-300:]))
        for n in ("metacomputer", "cua-driver"):
            if os.path.lexists(os.path.join(sk, n)):
                os.remove(os.path.join(sk, n))
        write(os.path.join(sk + "-blocker"), "")
        os.rmdir(sk)
        os.symlink(sk + "-blocker", sk)                    # the skills folder is a file: no link can be made
        b.stamp_set([])
        p = b.install("--only-gemini")
        check("a skill that can't be linked leaves the step unfinished (exit 1, recorded), the MCP server set up all the "
              "same", p.returncode == 1 and "couldn't link the" in p.stdout and b.stamp_incomplete() == ["gemini"]
              and entry(b, "gemini"), p.stdout[-400:])
    finally:
        b.close()
    b = Box(agents=("openclaw",))
    try:
        b.stamp_set([])
        p = b.install("--only-openclaw", STUB_HIDDEN="1")
        check("OpenClaw: skills it lists but doesn't offer leave the step unfinished (exit 1, recorded)",
              p.returncode == 1 and "doesn't offer" in p.stdout and b.stamp_incomplete() == ["openclaw"], p.stdout[-300:])
    finally:
        b.close()


def lock_cases():
    """Each read, change and read-back runs under the agent's own lock; a lock someone holds stops the step."""
    print("the agents' locks")
    import fcntl
    b = Box()
    try:
        for name in ("openclaw", "hermes", "gemini", "antigravity"):
            p = b.install(f"--only-{name}")
            config_calls = [c for c in b.calls() if c[0] == CMD.get(name, name) and words(c)[:1] in (["mcp"], ["config"])
                            and words(c)[:2] != ["config", "path"]]
            check(f"{LABEL[name]}: every read and change of its configuration ran under its lock",
                  p.returncode == 0 and config_calls and all(c[-1] == "#locked" for c in config_calls), config_calls)
        path = os.path.join(b.home, ".local", "share", "metacomputer", "locks", "agent-hermes.lock")
        fd = os.open(path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.remove(os.path.join(b.home, ".hermes", "stub-config.json"))
            p = b.install("--only-hermes", MCOMP_AGENT_LOCK_WAIT="1")
        finally:
            os.close(fd)
        check("Hermes Agent: while another metacomputer run holds its lock, nothing is read or changed: exit 1",
              p.returncode == 1 and "another metacomputer install is changing" in p.stdout and not mutations(b, "hermes")
              and entry(b, "hermes") is None, p.stdout[-300:])
    finally:
        b.close()


def profile_cases():
    """Hermes Agent: the profile in use is resolved once, and the MCP server and the skills both go there."""
    print("Hermes Agent profiles")
    b = Box(agents=("hermes",))
    try:
        write(os.path.join(b.home, ".hermes", "active_profile"), "work")
        p = b.install("--only-hermes", STUB_SWITCH_AFTER_PATH="1")
        work = os.path.join(b.home, ".hermes", "profiles", "work")
        e = json.load(open(os.path.join(work, "stub-config.json")))["mcp_servers"]["metacomputer"]
        check("the sticky profile (work) gets the MCP server and the skills, and a profile switch during the run changes "
              "nothing (every command pinned to it)", p.returncode == 0 and e["command"] == b.link
              and os.path.islink(os.path.join(work, "skills", "metacomputer"))
              and not os.path.lexists(os.path.join(b.home, ".hermes", "skills", "metacomputer"))
              and not os.path.exists(os.path.join(b.home, ".hermes", "profiles", "other")), p.stdout[-400:])
        write(os.path.join(b.home, ".hermes", "active_profile"), "work")
        p = b.install("--uninstall")
        check("--uninstall removes them from that profile", not os.path.lexists(os.path.join(work, "skills", "metacomputer"))
              and "metacomputer" not in json.load(open(os.path.join(work, "stub-config.json"))).get("mcp_servers", {}),
              p.stdout[-300:])
        p = b.install("--only-hermes", STUB_FAIL_PATH="1")
        check("a profile that can't be told changes nothing: exit 1", p.returncode == 1 and "profile" in p.stdout
              and not mutations(b, "hermes"), p.stdout[-300:])
    finally:
        b.close()


def no_cli_cases():
    """Uninstall when an agent's CLI isn't there: what it can't read keeps the command links."""
    print("uninstall without the agents' commands")
    b = Box(agents=())
    try:
        p = b.install("--only-agents", "--no-claude", "--no-codex")
        seed(b, "gemini", {"command": b.link, "args": ["--vm", "other", "mcp-lazy"]})
        p = b.install("--uninstall")
        check("Gemini CLI not on PATH: its settings.json is read all the same, and an entry there that runs the command "
              "keeps the command links", os.path.islink(b.link) and f"kept {b.link}" in p.stdout, p.stdout[-400:])
        seed(b, "gemini", {"command": b.link, "args": ["mcp-lazy"]})
        p = b.install("--uninstall")
        check("...metacomputer's own entry there, with no gemini to remove it: said, links kept, exit 1",
              p.returncode != 0 and os.path.islink(b.link) and "isn't available to remove it" in p.stdout,
              (p.returncode, p.stdout[-400:]))
        if os.path.exists(config_file(b, "gemini")):
            os.remove(config_file(b, "gemini"))
        write(os.path.join(b.home, ".hermes", "config.yaml"), "mcp_servers:\n  mine:\n    command: " + b.link + "\n")
        p = b.install("--uninstall")
        check("Hermes Agent not on PATH, its config.yaml naming the command: links kept, said, exit 1",
              p.returncode != 0 and os.path.islink(b.link) and "couldn't be checked in full" in p.stdout
              and "names it" in p.stdout, p.stdout[-400:])
        p = b.install("--uninstall", MCOMP_BREW_PYTHON="/nonexistent", PATH="/usr/bin:/bin")
        check("...and with no Python 3.11 to read it at all: links kept, said, exit 1", p.returncode != 0
              and os.path.islink(b.link) and "needs Python 3.11" in p.stdout, (p.returncode, p.stdout[-400:]))
        if os.path.exists(os.path.join(b.home, ".hermes", "config.yaml")):
            os.remove(os.path.join(b.home, ".hermes", "config.yaml"))
        # Inspection that isn't complete means "maybe referenced" (Codex's re-check): an include, or a file too big to
        # read in full, keeps the command links, with Python and without it.
        oc = os.path.join(b.home, ".openclaw", "openclaw.json")
        write(oc, '{"mcp": {"servers": {"$include": "./servers.json5"}}}')
        write(os.path.join(b.home, ".openclaw", "servers.json5"), '{"metacomputer": {"command": "' + b.link + '"}}')
        p = b.install("--uninstall")
        check("OpenClaw not on PATH, its openclaw.json including another file ($include): what that holds wasn't read, "
              "so the links stay, said, exit 1", p.returncode != 0 and os.path.islink(b.link)
              and "includes other files" in p.stdout and f"kept {b.link}" in p.stdout, (p.returncode, p.stdout[-400:]))
        p = b.install("--uninstall", MCOMP_BREW_PYTHON="/nonexistent", PATH="/usr/bin:/bin")
        check("...and with no Python 3.11 (install.sh reads it itself): the same", p.returncode != 0
              and os.path.islink(b.link) and f"kept {b.link}" in p.stdout, (p.returncode, p.stdout[-400:]))
        os.remove(os.path.join(b.home, ".openclaw", "servers.json5"))
        with open(oc, "w") as f:
            f.write('{"mcp": {"servers": {}}, "notes": "' + " " * (16 << 20) + '", "x": "' + b.link + '"}')
        p = b.install("--uninstall")
        check("...a configuration larger than the 16 MB read, naming the command past it: the links stay, said, exit 1",
              p.returncode != 0 and os.path.islink(b.link) and "larger than 16 MB" in p.stdout,
              (p.returncode, p.stdout[-400:]))
        os.remove(oc)
        # A folder on the way that can't be searched or listed (Codex's second re-check): what's behind it wasn't looked
        # at, so the links stay, with Python and without.
        ocdir = os.path.join(b.home, ".openclaw")
        write(oc, '{"mcp": {"servers": {"metacomputer": {"command": "' + b.link + '", "args": ["mcp-lazy"]}}}}')
        prof = os.path.join(b.home, ".hermes", "profiles")
        write(os.path.join(prof, "work", "config.yaml"), "mcp_servers:\n  metacomputer:\n    command: " + b.link + "\n")
        seen = {}
        for label, folder, mode, said in (("OpenClaw's folder can't be searched", ocdir, 0o000, "can't be looked at"),
                                          ("Hermes Agent's profiles folder can't be listed", prof, 0o300,
                                           "can't be listed")):
            os.chmod(folder, mode)
            try:
                p = b.install("--uninstall")
                p2 = b.install("--uninstall", MCOMP_BREW_PYTHON="/nonexistent", PATH="/usr/bin:/bin")
            finally:
                os.chmod(folder, 0o700)
            seen[label] = (p.returncode != 0 and os.path.islink(b.link) and said in p.stdout
                           and f"kept {b.link}" in p.stdout, p2.returncode != 0 and os.path.islink(b.link)
                           and f"kept {b.link}" in p2.stdout)
            shutil.rmtree(folder)
        check("a configuration folder that can't be searched (OpenClaw's) or listed (Hermes Agent's profiles) may hold "
              "an entry that runs the command: the links stay, said, exit 1, with Python and without",
              seen and all(all(v) for v in seen.values()), seen)
        # One profile folder among others that can't be looked into (Codex's third re-check): a glob for the file in it
        # leaves it out; every profile is checked, so the links stay, with Python and without.
        seen = {}
        for label, folder, cfg, text in (
                ("a Hermes Agent profile", os.path.join(b.home, ".hermes", "profiles", "locked"), "config.yaml",
                 "mcp_servers:\n  metacomputer:\n    command: " + b.link + "\n"),
                ("an OpenClaw profile", os.path.join(b.home, ".openclaw-work"), "openclaw.json",
                 '{"mcp": {"servers": {"metacomputer": {"command": "' + b.link + '", "args": ["mcp-lazy"]}}}}')):
            write(os.path.join(folder, cfg), text)
            write(os.path.join(os.path.dirname(folder), "plain" if "profiles" in folder else ".openclaw-plain", cfg), "{}")
            os.chmod(folder, 0o000)
            try:
                p = b.install("--uninstall")
                p2 = b.install("--uninstall", MCOMP_BREW_PYTHON="/nonexistent", PATH="/usr/bin:/bin")
            finally:
                os.chmod(folder, 0o700)
            seen[label] = (p.returncode != 0 and os.path.islink(b.link) and "can't be looked at" in p.stdout
                           and f"kept {b.link}" in p.stdout, p2.returncode != 0 and os.path.islink(b.link)
                           and f"kept {b.link}" in p2.stdout)
            shutil.rmtree(os.path.join(b.home, ".hermes") if "profiles" in folder else folder)
            if "profiles" not in folder:
                shutil.rmtree(os.path.join(b.home, ".openclaw-plain"))
        check("one profile folder that can't be looked into, among others (a Hermes Agent profile, an OpenClaw "
              "~/.openclaw-NAME): it may hold an entry that runs the command, so the links stay, said, exit 1, with "
              "Python and without", seen and all(all(v) for v in seen.values()), seen)
        # Gemini CLI's settings.json past the 4 MB its reader takes: what's beyond wasn't read, so the links stay.
        gs = os.path.join(b.home, ".gemini", "settings.json")
        write(gs, " " * (4 << 20) + '{"mcpServers": {"metacomputer": {"command": "' + b.link + '", "args": ["mcp-lazy"]}}}')
        p = b.install("--uninstall")
        check("Gemini CLI's settings.json larger than the 4 MB read, its entry past it: not taken for absent; the links "
              "stay, said, exit 1", p.returncode != 0 and os.path.islink(b.link) and "larger than 4 MB" in p.stdout
              and f"kept {b.link}" in p.stdout, (p.returncode, p.stdout[-400:]))
        os.remove(gs)
        write(oc, '{"mcp": {"servers": {}}}')
        p = b.install("--uninstall")
        check("a configuration that doesn't name the command doesn't hold the links back", p.returncode == 0
              and not os.path.lexists(b.link), (p.returncode, p.stdout[-400:]))
    finally:
        b.close()


def other_profile_cases():
    """Codex's last check: metacomputer set up in one profile (Hermes Agent's `work`, OpenClaw's ~/.openclaw-work), then
    another selected (Hermes's unconfigured default, OpenClaw's own ~/.openclaw): --uninstall acts on the selected one,
    and must still see the other, which runs the command, so the command links stay. With the agent's CLI there, without
    it, and without Python."""
    print("uninstall: metacomputer in another profile")
    py = os.path.dirname(os.path.realpath(sys.executable))
    for name in ("hermes", "openclaw"):
        for how, extra in (("with its CLI", {}), ("without its CLI", {"PATH": f"{py}:/usr/bin:/bin"}),
                           ("without Python", {"PATH": "/usr/bin:/bin", "MCOMP_BREW_PYTHON": "/nonexistent"})):
            b = Box(agents=(name,))
            try:
                if name == "hermes":
                    write(os.path.join(b.home, ".hermes", "active_profile"), "work")
                    p = b.install("--only-hermes")
                    other = os.path.join(b.home, ".hermes", "profiles", "work", "config.yaml")
                    write(os.path.join(b.home, ".hermes", "active_profile"), "default")     # an unconfigured default
                else:
                    p = b.install("--only-openclaw", OPENCLAW_STATE_DIR=os.path.join(b.home, ".openclaw-work"))
                    other = os.path.join(b.home, ".openclaw-work", "openclaw.json")
                set_up = p.returncode == 0 and os.path.islink(b.link) and b.link in open(other).read()
                before = open(other).read()
                p = b.install("--uninstall", **extra)
                check(f"{LABEL[name]}, {how}: metacomputer set up in another profile than the one selected; the "
                      "uninstall keeps the command links that profile runs, says so, exit 1, and leaves that profile "
                      "as it was", set_up and p.returncode != 0 and os.path.islink(b.link)
                      and f"kept {b.link}" in p.stdout and open(other).read() == before,
                      (set_up, p.returncode, p.stdout[-600:]))
            finally:
                b.close()


def uninstall_error_cases():
    """Every unexpected error in an agent's uninstall step keeps the command links (its configuration wasn't checked),
    and says so: a lock file that is a link, a lock folder that can't be written."""
    print("uninstall: unexpected errors")
    for name in ("openclaw", "gemini", "antigravity"):
        b = Box(agents=(name,))
        try:
            p = b.install(f"--only-{name}")
            locks = os.path.join(b.home, ".local", "share", "metacomputer", "locks")
            lock = os.path.join(locks, f"agent-{name}.lock")
            ok_setup = p.returncode == 0 and os.path.islink(b.link)
            os.remove(lock)
            os.symlink(os.path.join(b.t, "elsewhere"), lock)
            p = b.install("--uninstall")
            check(f"{LABEL[name]}: its lock file is a link (refused): the step stops, the configuration isn't touched, "
                  "and the command links stay, said, exit 1", ok_setup and p.returncode != 0 and os.path.islink(b.link)
                  and "stopped unexpectedly" in p.stdout and f"kept {b.link}" in p.stdout and entry(b, name) is not None,
                  (p.returncode, p.stdout[-500:]))
            os.remove(lock)
            os.chmod(locks, 0o500)
            try:
                p = b.install("--uninstall")
            finally:
                os.chmod(locks, 0o700)
            check(f"{LABEL[name]}: its lock folder can't be written: the same", p.returncode != 0
                  and os.path.islink(b.link) and f"kept {b.link}" in p.stdout and entry(b, name) is not None,
                  (p.returncode, p.stdout[-500:]))
            p = b.install("--uninstall")
            check(f"{LABEL[name]}: once the lock works again, the uninstall finishes and the links go",
                  p.returncode == 0 and not os.path.lexists(b.link) and entry(b, name) is None,
                  (p.returncode, p.stdout[-500:]))
        finally:
            b.close()


def choice_cases():
    print("choices: --no-NAME, --NAME, --only-agents")
    b = Box()
    try:
        p = b.install("--only-openclaw", "--no-openclaw")
        check("--only-openclaw with --no-openclaw leaves nothing to do: exit 2", p.returncode == 2, p.stderr)
        p = b.install("--only-hermes", "--only-gemini")
        check("two --only-NAME at once: exit 2", p.returncode == 2 and "one at a time" in p.stderr, p.stderr)
        p = b.install("--only-agents", "--no-claude", "--no-codex", "--no-gemini")
        check("--only-agents sets up the agents installed here (OpenClaw, Hermes Agent), --no-gemini leaves Gemini CLI "
              "alone and is remembered", p.returncode == 0 and entry(b, "openclaw") and entry(b, "hermes")
              and entry(b, "gemini") is None and "Gemini CLI: left alone (--no-gemini;" in p.stdout
              and b.choices().get("gemini") == "off" and b.choices().get("claude") == "off", (p.stdout[-700:], b.choices()))
        p = b.install("--only-agents")
        check("...so a later run (an update) leaves it alone too, and says why", p.returncode == 0
              and entry(b, "gemini") is None and "--no-gemini in an earlier run" in p.stdout, p.stdout[-500:])
        p = b.install("--only-agents", "--gemini")
        check("--gemini sets it up again and remembers that", p.returncode == 0 and entry(b, "gemini")
              and b.choices().get("gemini") == "on", (p.stdout[-400:], b.choices()))
        st = b.agents_json()
        check("agents.json keeps every agent's choice (claude, codex, openclaw, hermes, gemini, antigravity)",
              set(b.choices()) == {"claude", "codex", "openclaw", "hermes", "gemini", "antigravity"}, b.choices())
        check("agents --json reports all six agents", all(k in st for k in ("claude_code", "codex", "openclaw", "hermes",
                                                                            "gemini", "antigravity")), list(st))
    finally:
        b.close()
    b = Box(agents=())
    try:
        p = b.install("--only-agents")
        check("with no agent on PATH: the command is linked, and it says how to add one later", p.returncode == 0
              and "no coding agent is installed" in p.stdout and os.path.islink(b.link)
              and "Not installed here: OpenClaw, Hermes Agent, Gemini CLI, Antigravity CLI" in p.stdout, p.stdout[-500:])
        st = b.agents_json()
        check("agents --json: each one not installed", all(st.get(k, {}).get("installed") is False
                                                           for k in ("openclaw", "hermes", "gemini", "antigravity")), st)
    finally:
        b.close()


def gemini_home_cases():
    print("Gemini CLI: settings.json as it reads it, GEMINI_CLI_HOME")
    check("JSON with comments reads (comment marks inside strings kept); a trailing comma doesn't",
          agent_setup.jsonc_loads('{"a": "x//y", // c\n "b": /* z */ [1]}') == {"a": "x//y", "b": [1]}
          and raises(lambda: agent_setup.jsonc_loads('{"a": 1,}')))
    b = Box(agents=("gemini",))
    try:
        gh = os.path.join(b.t, "gemhome")
        p = b.install("--only-gemini", GEMINI_CLI_HOME=gh)
        e = json.load(open(os.path.join(gh, ".gemini", "settings.json")))["mcpServers"]["metacomputer"]
        check("GEMINI_CLI_HOME moves the settings and the skills it reads", p.returncode == 0 and e["command"] == b.link
              and os.path.islink(os.path.join(gh, ".agents", "skills", "metacomputer"))
              and not os.path.exists(os.path.join(b.home, ".gemini")), p.stdout[-400:])
        check("Gemini CLI: the run says it starts MCP servers only in trusted folders", "only in folders you trust" in p.stdout,
              p.stdout[-400:])
    finally:
        b.close()


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def unit_cases():
    print("ownership, by the rule Claude Code's and Codex's follow")
    root, link = "/co", "/h/.local/bin/metacomputer"
    st = lambda c, a: {"kind": "stdio", "entry": {"command": c, "args": a, "env": {}}}  # noqa: E731
    check("metacomputer's own: exactly the checkout's launcher or ~/.local/bin/metacomputer, with exactly mcp-lazy",
          agent_setup.classify(st(link, ["mcp-lazy"]), root, link) == "ok"
          and agent_setup.classify(st("/co/bin/metacomputer", ["mcp-lazy"]), root, link) == "ok")
    check("...anything else isn't: another path in the checkout, a path that only starts the same, other arguments, "
          "a remote server", all(agent_setup.classify(r, root, link) == "foreign" for r in (
              st("/co/bin/other", ["mcp-lazy"]), st("/co/bin/metacomputer2", ["mcp-lazy"]), st(link, ["mcp-lazy", "x"]),
              st(link, []), {"kind": "http", "entry": {"url": "https://x"}})))
    check("entries of the wrong shape are errors, never metacomputer's", all(
        agent_setup._entry_res(d)["kind"] == "error" for d in ({"command": 3}, {"command": "x", "args": "mcp-lazy"},
                                                               {"command": "x", "args": [1]}, [])))

    class Fake(agent_setup.Gemini):
        """Gemini CLI whose settings change between the read and the add (another program writes them)."""
        def __init__(self, seq):
            self.seq = list(seq)
            self.added = False

        def read(self, prog, env):
            return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]

        def run(self, argv, env):
            self.added = True
            return 0, "", ""
    out = agent_setup.Out()
    fake = Fake([{"kind": "absent"}, {"kind": "stdio", "entry": {"command": "/x/other", "args": [], "env": {}}}])
    t = tempfile.mkdtemp(prefix="mcomp-agents-unit-")
    try:
        old = agent_setup.agents.trusted
        agent_setup.agents.trusted = lambda name: ("/bin/echo", None)
        try:
            done = agent_setup.install(fake, root, link, {"HOME": t}, out)
        finally:
            agent_setup.agents.trusted = old
    finally:
        shutil.rmtree(t, ignore_errors=True)
    check("Gemini CLI: a server that appears between the read and the add is left (its add replaces without asking)",
          not done and not fake.added and any("changed since it was read" in x for x in out.lines), out.lines)


def package_cases():
    print("the Agent Plugins package (agent-plugin/) and the Gemini CLI extension")
    pkg = os.path.join(LAB, "agent-plugin")
    p = subprocess.run([sys.executable, os.path.join(LAB, "scripts", "sync-plugin.py"), "--check"], capture_output=True,
                       text=True, timeout=60)
    check("agent-plugin/'s copies and manifests are what scripts/sync-plugin.py makes", p.returncode == 0, p.stdout)
    man = json.load(open(os.path.join(pkg, "plugin.json")))
    check("plugin.json: the Agent Plugins 1.0.0 schema, a name the format allows, the listing fields, no command",
          man.get("$schema") == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
          and re.fullmatch(r"[a-z0-9.-]{1,64}", man.get("name", "")) and man.get("license") == "MIT"
          and "repository" in man and "Claude" not in man.get("description", "")
          and man["extensions"]["com.nousresearch.hermes"]["servers"]["metacomputer"] == {"timeout": 600}, man)
    mcp = json.load(open(os.path.join(pkg, "mcp.json")))
    check("mcp.json: exactly its schema and one stdio server, metacomputer, running the package's launcher (the format "
          "takes no absolute path) with mcp-lazy", set(mcp) == {"$schema", "mcpServers"}
          and mcp["mcpServers"] == {"metacomputer": {"type": "stdio", "command": "./bin/metacomputer", "args": ["mcp-lazy"]}},
          mcp)
    bad = []
    for name in sorted(os.listdir(os.path.join(pkg, "skills"))):
        text = open(os.path.join(pkg, "skills", name, "SKILL.md")).read()
        fm = text.split("---\n")[1] if text.startswith("---\n") else ""
        nm = re.search(r"^name: (.*)$", fm, re.M)
        ds = re.search(r'^description: "([^"\n]*)"$', fm, re.M)
        meta = [ln for ln in fm.splitlines() if ln.startswith("  ")]
        if not (nm and nm.group(1) == name and re.fullmatch(r"[a-z0-9-]+", name)):
            bad.append(f"{name}: name")
        if not (ds and 1 <= len(ds.group(1)) <= 1024):
            bad.append(f"{name}: description (one double-quoted line, at most 1024 characters)")
        if any(re.match(r"^  \S[^:]*:\s*$", ln) for ln in meta):
            bad.append(f"{name}: metadata holds only plain strings")
        for rel in sorted(set(x.split(")")[0] for x in text.split("](")[1:] if x.startswith("../"))):
            if not os.path.isfile(os.path.normpath(os.path.join(pkg, "skills", name, rel.split("#")[0]))):
                bad.append(f"{name}: link {rel} leaves the package")
    check("its skills: one level under skills/, each named as its folder, a quoted description (strict YAML readers such "
          "as Hermes Agent's), metadata of plain strings, links that stay inside the package", not bad
          and sorted(os.listdir(os.path.join(pkg, "skills"))) == ["cua-driver", "metacomputer"], bad)
    launcher = os.path.join(pkg, "bin", "metacomputer")
    t = tempfile.mkdtemp(prefix="mcomp-launch-")
    try:
        env = {"HOME": t, "PATH": "/usr/bin:/bin"}
        r = subprocess.run([launcher, "status"], env=env, capture_output=True, text=True, timeout=30)
        check("the launcher with nothing installed says how to install and runs nothing (exit 1)", r.returncode == 1
              and "isn't set up on this Mac" in r.stderr and "get.sh" in r.stderr, r.stderr)
        fake_co = os.path.join(t, "co")
        write(os.path.join(fake_co, "bin", "metacomputer"), '#!/bin/sh\necho "checkout $*"\n', 0o755)
        os.makedirs(os.path.join(t, ".local", "bin"))
        os.symlink(os.path.join(fake_co, "bin", "metacomputer"), os.path.join(t, ".local", "bin", "metacomputer"))
        r = subprocess.run([launcher, "mcp-lazy"], env=env, capture_output=True, text=True, timeout=30)
        check("...and doesn't hand over to a command that isn't a checkout's (no install.sh beside its bin/)",
              r.returncode == 1 and "checkout" not in r.stdout, (r.stdout, r.stderr))
        write(os.path.join(fake_co, "install.sh"), "#!/bin/sh\n", 0o755)
        r = subprocess.run([launcher, "mcp-lazy"], env=env, capture_output=True, text=True, timeout=30)
        check("...and hands over to the installed checkout's command, with the same arguments",
              r.returncode == 0 and r.stdout.strip() == "checkout mcp-lazy", (r.stdout, r.stderr))
        r = subprocess.run([launcher, "x"], env=dict(env, MCOMP_HOME=os.path.join(t, "nowhere")), capture_output=True,
                           text=True, timeout=30)
        check("...MCOMP_HOME that isn't a checkout falls through to ~/.local/bin/metacomputer",
              r.stdout.strip() == "checkout x", r.stdout)
    finally:
        shutil.rmtree(t, ignore_errors=True)
    ext = json.load(open(os.path.join(LAB, "gemini-extension.json")))
    srv = (ext.get("mcpServers") or {}).get("metacomputer") or {}
    check("gemini-extension.json: name, a version, the context file inside the extension, and the MCP server metacomputer "
          "running the package's launcher with mcp-lazy (no absolute path, no ~)",
          ext.get("name") == "metacomputer" and re.fullmatch(r"\d+\.\d+\.\d+", ext.get("version", ""))
          and os.path.isfile(os.path.join(LAB, ext.get("contextFileName", "-")))
          and srv.get("command") == "${extensionPath}${/}agent-plugin${/}bin${/}metacomputer"
          and srv.get("args") == ["mcp-lazy"] and "trust" not in srv, ext)
    newest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", open(os.path.join(LAB, "CHANGELOG.md")).read(), re.M)
    check("gemini-extension.json's version is the newest release in the CHANGELOG (scripts/release.sh moves it)",
          newest and ext.get("version") == newest.group(1), (ext.get("version"), newest and newest.group(1)))
    check("no folder at the repository's root that Gemini CLI would load as the extension's commands, hooks, agents or "
          "policies", not any(os.path.exists(os.path.join(LAB, d)) for d in ("commands", "hooks", "agents", "policies")))


def antigravity_cases():
    """Antigravity CLI's tool timeout: written by the step (agy's CLI has no option for it) into metacomputer's own entry
    only, one you set stays, never over a file that changed since it was read; and a commented file keeps the step
    finished with a note (the server works; only long calls end at agy's 180 s)."""
    print("Antigravity CLI: the tool timeout")
    b = Box(agents=("antigravity",))
    try:
        seed(b, "antigravity", {"command": b.link, "args": ["mcp-lazy"], "timeoutSeconds": 1200})
        before = open(config_file(b, "antigravity")).read()
        p = b.install("--only-antigravity")
        check("a timeoutSeconds you set on metacomputer's entry stays, and the file isn't written", p.returncode == 0
              and open(config_file(b, "antigravity")).read() == before and not mutations(b, "antigravity"), p.stdout[-400:])
        seed(b, "antigravity", {"command": b.link, "args": ["mcp-lazy"]})
        p = b.install("--only-antigravity")
        d = json.load(open(config_file(b, "antigravity")))
        check("metacomputer's entry without one gets 600 s; the other server and every other key stay",
              p.returncode == 0 and d["mcpServers"]["metacomputer"].get("timeoutSeconds") == 600
              and d["mcpServers"]["other"] == {"command": "/bin/echo"} and "tool timeout 600 s" in p.stdout
              and not [f for f in os.listdir(os.path.dirname(config_file(b, "antigravity"))) if "metacomputer-new" in f],
              (p.stdout[-400:], d))
        text = ("{\n  // the user's own\n  \"mcpServers\": {\"metacomputer\": {\"command\": \"" + b.link +
                "\", \"args\": [\"mcp-lazy\"]},},\n}\n")
        write(config_file(b, "antigravity"), text)
        p = b.install("--only-antigravity")
        check("in a file with comments (agy reads it; its CLI and this step don't write it), metacomputer's entry is "
              "recognised, left as it is, and the run says how to add the timeout by hand; the step still finishes",
              p.returncode == 0 and open(config_file(b, "antigravity")).read() == text
              and "timeoutSeconds" in p.stdout and "already registered" in p.stdout, p.stdout[-500:])
    finally:
        b.close()
    print("Antigravity CLI: its permission rule (settings.json)")
    rule = "mcp(metacomputer/*)"

    def settings_path(b):
        return os.path.join(b.home, ".gemini", "antigravity-cli", "settings.json")

    def allow_of(b):
        try:
            return ((json.load(open(settings_path(b))).get("permissions") or {}).get("allow")) or []
        except (OSError, ValueError):
            return None
    b = Box(agents=("antigravity",))
    try:
        p = b.install("--only-antigravity")
        st = os.stat(settings_path(b)) if os.path.exists(settings_path(b)) else None
        check("a run that adds the server adds metacomputer's own rule, exactly mcp(metacomputer/*), to a new settings.json "
              "(0600), as Gemini CLI's server gets trust", p.returncode == 0 and allow_of(b) == [rule]
              and st and stat.S_IMODE(st.st_mode) == 0o600 and "allowed without asking" in p.stdout,
              (p.stdout[-400:], allow_of(b)))
        write(settings_path(b), json.dumps({"theme": "dark", "permissions": {"allow": ["command(git)"],
                                                                             "deny": ["command(rm -rf)"]}}, indent=2) + "\n")
        os.chmod(settings_path(b), 0o640)
        p = b.install("--only-antigravity")
        check("a later run (the server already there) doesn't add it back: taken out stays out", p.returncode == 0
              and allow_of(b) == ["command(git)"], allow_of(b))
        os.remove(config_file(b, "antigravity"))
        p = b.install("--only-antigravity")
        d = json.load(open(settings_path(b)))
        check("added next to the user's own rules and settings, which stay as they were, and the file keeps its mode",
              p.returncode == 0 and d == {"theme": "dark", "permissions": {"allow": ["command(git)", rule],
                                                                           "deny": ["command(rm -rf)"]}}
              and stat.S_IMODE(os.stat(settings_path(b)).st_mode) == 0o640, d)
        p = b.install("--uninstall")
        d = json.load(open(settings_path(b)))
        check("--uninstall takes out exactly that rule; everything else stays", p.returncode == 0
              and d == {"theme": "dark", "permissions": {"allow": ["command(git)"], "deny": ["command(rm -rf)"]}}
              and f'took "{rule}" out' in p.stdout, (d, p.stdout[-400:]))
        for theirs in (["mcp(metacomputer/click)"], ["mcp(*)"]):
            for key in ("ask", "deny"):
                write(settings_path(b), json.dumps({"permissions": {key: theirs}}) + "\n")
                p = b.install("--only-antigravity")
                check(f"a {key} rule of the user's ({theirs[0]}) wins: no allow rule is added, said",
                      p.returncode == 0 and allow_of(b) == [] and "no rule allowing them was added" in p.stdout,
                      (allow_of(b), p.stdout[-300:]))
                b.install("--uninstall")
        text = "{\n  // mine\n  \"theme\": \"dark\",\n}\n"
        write(settings_path(b), text)
        p = b.install("--only-antigravity")
        check("a settings.json with comments or trailing commas is left byte for byte, with the line to add by hand; the "
              "step still finishes (the server works; agy asks before each call)", p.returncode == 0
              and open(settings_path(b)).read() == text and f'add "{rule}" to permissions.allow' in p.stdout,
              p.stdout[-400:])
        write(settings_path(b), json.dumps({"permissions": {"allow": [rule]}}) + "\n")
        foreign = {"command": "/opt/other/bin/server", "args": ["--token", SECRET]}
        seed(b, "antigravity", foreign)
        p = b.install("--uninstall")
        check("an uninstall that leaves someone else's server named metacomputer leaves the rule too",
              allow_of(b) == [rule] and entry(b, "antigravity") == foreign, (allow_of(b), p.stdout[-300:]))
        os.remove(config_file(b, "antigravity"))
        p = b.install("--uninstall")
        check("...and with no server named metacomputer left, the same rule written by the user (not by this install) "
              "stays", allow_of(b) == [rule], (allow_of(b), p.stdout[-300:]))
        os.remove(settings_path(b))
        b.install("--only-antigravity")
        os.remove(config_file(b, "antigravity"))
        p = b.install("--uninstall")
        check("...while the rule this install added (its record says so) goes once no server named metacomputer is left, "
              "and the record with it", allow_of(b) == [] and f'took "{rule}" out' in p.stdout
              and not os.path.exists(os.path.join(b.home, ".local", "share", "metacomputer", "antigravity-rule.json")),
              (allow_of(b), p.stdout[-300:]))
    finally:
        b.close()
    # Provenance: a rule the user wrote before metacomputer was installed is the user's; the install finds it there,
    # doesn't note it as its own, and no uninstall takes it out.
    b = Box(agents=("antigravity",))
    try:
        mine = {"theme": "dark", "permissions": {"allow": ["command(git)", rule]}}
        write(settings_path(b), json.dumps(mine, indent=2) + "\n")
        p = b.install("--uninstall")
        check("an uninstall with no server named metacomputer leaves the user's own identical rule, and says so",
              p.returncode == 0 and json.load(open(settings_path(b))) == mine and "didn't add it" in p.stdout,
              (json.load(open(settings_path(b))), p.stdout[-300:]))
        p = b.install("--only-antigravity")
        p2 = b.install("--uninstall")
        check("...and so does one after an install that added the server but found the rule already there",
              p.returncode == 0 and p2.returncode == 0 and entry(b, "antigravity") is None
              and json.load(open(settings_path(b))) == mine, (json.load(open(settings_path(b))), p2.stdout[-300:]))
    finally:
        b.close()
    # A settings.json or mcp_config.json that is a link (a dotfile kept elsewhere) is never replaced: the link and the
    # file it points to stay byte for byte, and the run says what to change by hand.
    b = Box(agents=("antigravity",))
    try:
        dot = os.path.join(b.home, "dotfiles")
        os.makedirs(dot, exist_ok=True)
        os.makedirs(os.path.dirname(settings_path(b)), exist_ok=True)
        write(os.path.join(dot, "agy-settings.json"), json.dumps({"theme": "dark"}) + "\n")
        os.symlink(os.path.join(dot, "agy-settings.json"), settings_path(b))
        p = b.install("--only-antigravity")
        check("a settings.json that is a link stays a link, the file it points to unchanged, with the line to add by hand",
              p.returncode == 0 and os.path.islink(settings_path(b))
              and open(os.path.join(dot, "agy-settings.json")).read() == json.dumps({"theme": "dark"}) + "\n"
              and "is a link" in p.stdout and f'add "{rule}" to permissions.allow' in p.stdout, p.stdout[-500:])
        cfg = config_file(b, "antigravity")
        seed(b, "antigravity", {"command": b.link, "args": ["mcp-lazy"]})
        os.rename(cfg, os.path.join(dot, "mcp_config.json"))
        os.symlink(os.path.join(dot, "mcp_config.json"), cfg)
        before = open(os.path.join(dot, "mcp_config.json")).read()
        p = b.install("--only-antigravity")
        check("a mcp_config.json that is a link stays a link (no timeout written through or over it), with the line to "
              "add by hand", os.path.islink(cfg) and open(os.path.join(dot, "mcp_config.json")).read() == before
              and "is a link" in p.stdout and "timeoutSeconds" in p.stdout, p.stdout[-500:])
    finally:
        b.close()
    # The write happens only over the file that was read: one that changed meanwhile is left as it is (in-process).
    t = tempfile.mkdtemp(prefix="mcomp-agy-")
    try:
        f = os.path.join(t, "x.json")
        write(f, "{}\n")
        got = agent_setup.replace_if_unchanged(f, b"{ }\n", b'{"a": 1}\n')
        got_none = agent_setup.replace_if_unchanged(f, None, b'{"a": 1}\n')
        check("replace_if_unchanged: a file that isn't what was read (or one that appeared) is left, and nothing is left "
              "beside it", got == "changed" and got_none == "changed" and open(f).read() == "{}\n"
              and os.listdir(t) == ["x.json"], (got, got_none, os.listdir(t)))
    finally:
        shutil.rmtree(t, ignore_errors=True)
    # ...and the last check is as close to the replace as a file allows: another program's change that lands after the
    # content was read again but before the replace (a new file renamed into place, or the same file rewritten) is
    # caught by the file's identity, so it isn't lost. What remains is the instant between that check and the replace.
    t = tempfile.mkdtemp(prefix="mcomp-agy-")
    real_open = open
    try:
        f = os.path.join(t, "settings.json")
        theirs = '{"permissions": {"deny": ["mcp(*)"]}}\n'

        def by_rename():
            with real_open(f + ".theirs", "w") as w:
                w.write(theirs)
            os.replace(f + ".theirs", f)

        def in_place():
            with real_open(f, "r+") as w:
                w.write(theirs[:len("{}\n")])
        for label, change, expect in (("a new file renamed into place", by_rename, theirs),
                                      ("the same file rewritten in place", in_place, theirs[:3])):
            write(f, "{}\n")
            fired = []

            class Reader:
                """The file as replace_if_unchanged reads it again, another program changing it right after."""

                def __init__(self, fh):
                    self.fh = fh

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    self.fh.close()

                def fileno(self):
                    return self.fh.fileno()

                def read(self, *a):
                    got = self.fh.read(*a)
                    if not fired:
                        fired.append(1)
                        time.sleep(0.01)
                        change()
                    return got

            def opener(path, mode="r", *a, **kw):
                fh = real_open(path, mode, *a, **kw)
                return Reader(fh) if path == f and "r" in mode and "+" not in mode else fh
            agent_setup.open = opener
            try:
                got = agent_setup.replace_if_unchanged(f, b"{}\n", b'{"permissions": {"allow": ["mcp(metacomputer/*)"]}}\n')
            finally:
                del agent_setup.open
            check(f"replace_if_unchanged: another program's change right after the last read ({label}) isn't "
                  "overwritten", fired and got == "changed" and real_open(f).read() == expect
                  and sorted(os.listdir(t)) == ["settings.json"], (got, real_open(f).read(), os.listdir(t)))
    finally:
        shutil.rmtree(t, ignore_errors=True)
    # The write happens only over the file that was read: one that changed meanwhile is left as it is (in-process).
    t = tempfile.mkdtemp(prefix="mcomp-agy-")
    try:
        home = os.path.join(t, "home")
        link = os.path.join(home, ".local", "bin", "metacomputer")
        ag = agent_setup.AGENTS["antigravity"]
        path = ag.config({"HOME": home})
        write(path, json.dumps({"mcpServers": {"metacomputer": {"command": link, "args": ["mcp-lazy"]}}}) + "\n")
        real = ag._text
        changed = json.dumps({"mcpServers": {"metacomputer": {"command": link, "args": ["mcp-lazy"]}, "new": {}}})

        def text_then_change(p):
            got = real(p)
            write(path, changed)              # someone writes the file right after it was read
            return got
        ag._text = text_then_change
        out = agent_setup.Out()
        try:
            ag.settle("agy", LAB, link, {"HOME": home}, out)
        finally:
            ag._text = real
        check("a mcp_config.json that changed after it was read isn't replaced: said, and no file left beside it",
              open(path).read() == changed and any("changed while" in x for x in out.lines)
              and sorted(os.listdir(os.path.dirname(path))) == ["mcp_config.json"], (out.lines, os.listdir(os.path.dirname(path))))
    finally:
        shutil.rmtree(t, ignore_errors=True)


SKILL_BUDGET = 24000     # bytes of a skill's text Antigravity CLI loads (agy plugin validate 1.5.15); the rest it doesn't
SAFETY_SECTIONS = ("## Rules", "## Getting the user's attention", "## When a step is the user's",
                   "## Start: one round, then the safety check")
SAFETY_PHRASES = ("**Refusals are the user's controls,**", "**Destroying guest state needs the user's explicit OK",
                  "**Logins, 2FA codes and API keys", "**Paying for things", "**One-time codes and sign-in links",
                  "**Untrusted content.**", "**Hidden or AI-directed text", "**Keep the isolation",
                  "Never give a `metacomputer` command a terminal of your own",
                  "`health_report` before the first action")


def skill_budget_cases():
    """What keeps the agent safe stays within the part of SKILL.md every agent loads: Antigravity CLI loads only its first
    24,000 bytes, so the rules (refusals and the commands only the user runs, secrets and codes, payments, untrusted
    content and hidden text, isolation), handing a step to the user and the start's safety check come before the driving
    guidance and the references. Counted in the whole file's bytes (stricter than agy, which counts after the front
    matter), in the repository's skill and both packages' copies."""
    print("SKILL.md: the safety sections within the first 24,000 bytes")
    for rel in ("skills/metacomputer/SKILL.md", "plugin/skills/metacomputer/SKILL.md",
                "agent-plugin/skills/metacomputer/SKILL.md"):
        data = open(os.path.join(LAB, rel), "rb").read()
        heads = [m.start() + 1 for m in re.finditer(rb"\n## ", data)]

        def end_of(title):
            i = data.find(b"\n" + title.encode())
            if i < 0:
                return None
            later = [h for h in heads if h > i + 1]
            return later[0] if later else len(data)
        ends = {t: end_of(t) for t in SAFETY_SECTIONS}
        check(f"{rel}: the rules, the user's attention, handoffs and the start's safety check end before byte "
              f"{SKILL_BUDGET:,}", all(e is not None and e <= SKILL_BUDGET for e in ends.values()), ends)
        late = [ph for ph in SAFETY_PHRASES if data.find(ph.encode()) < 0
                or data.find(ph.encode()) + len(ph.encode()) > SKILL_BUDGET]
        check(f"{rel}: every safety rule is in that part (refusals, the user's own commands, secrets, codes, payments, "
              "untrusted and hidden text, isolation, the safety check)", not late, late)
        check(f"{rel}: the reference material comes after them", all(
            data.find(b"\n## " + t) > max(e for e in ends.values() if e) - 1 for t in (b"Also available", b"References")))
        check(f"{rel}: the skill tells Antigravity CLI to read the whole file",
              b"it loads only the first part of this file" in data and b"read the whole file" in data)

def main():
    unit_cases()
    for name in ("openclaw", "hermes", "gemini", "antigravity"):
        agent_cases(name)
    antigravity_cases()
    skill_budget_cases()
    link_cases()
    lock_cases()
    profile_cases()
    no_cli_cases()
    uninstall_error_cases()
    other_profile_cases()
    choice_cases()
    gemini_home_cases()
    package_cases()
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
