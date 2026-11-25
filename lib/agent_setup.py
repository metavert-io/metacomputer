#!/usr/bin/env python3
"""metacomputer for OpenClaw, Hermes Agent, Gemini CLI and Antigravity CLI (docs/openclaw.md, docs/hermes.md, docs/gemini.md,
docs/antigravity.md): install.sh's step for each, its part of --uninstall, and what `metacomputer agents` reports about
them.

  agent_setup.py install NAME --root CHECKOUT --link ~/.local/bin/metacomputer
  agent_setup.py uninstall NAME --root CHECKOUT --link ~/.local/bin/metacomputer
  agent_setup.py status NAME --root CHECKOUT --link ~/.local/bin/metacomputer     (JSON)

NAME is openclaw, hermes, gemini or antigravity. For each agent the step registers metacomputer's MCP server,
`~/.local/bin/metacomputer mcp-lazy`, in the agent's own configuration, through the agent's own CLI (OpenClaw: `openclaw mcp
add`; Hermes Agent: `hermes config set`; Gemini CLI: `gemini mcp add -s user`; Antigravity CLI: `agy mcp add`), and links
the skills metacomputer and cua-driver where the agent reads them (OpenClaw and Gemini CLI: ~/.agents/skills, which Codex
reads too; Hermes Agent: $HERMES_HOME/skills; Antigravity CLI: ~/.gemini/antigravity-cli/skills). Antigravity CLI's entry
also gets a tool timeout of 600 s, and its settings.json metacomputer's permission rule when the server is added (taken
out by --uninstall), written by this step because its CLI has no option for either (Antigravity.settle, added, removed).

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
CLI call, isn't seen: an add replaces it (Gemini CLI's `mcp add`, Hermes Agent's `config set`, Antigravity CLI's `mcp add`;
OpenClaw's `mcp add` refuses an existing name instead), and a removal, which these CLIs make by name only, removes it. The read-back after
an add sees what won; the one after a removal can only confirm the name is gone. A skill link swapped for someone else's between metacomputer's
check and its unlink is removed the same way.

Skill links are metacomputer's only when they point into this checkout's own skill folders (skills/, .claude/skills/;
a relative target is read from the link's folder): only those are ever replaced or removed. A skill that can't be linked
(something else there, or an error), or that OpenClaw doesn't offer, leaves the step unfinished. Hermes Agent's profile is
resolved once (`hermes config path`) and every command and the skills folder use it. Uninstall keeps
~/.local/bin/metacomputer and mcomp whenever an agent's configuration can't be inspected in full and may still run them
(its CLI missing or refused: Gemini CLI's settings.json is read directly, all of it or it counts as maybe; the others'
files are looked through for the names, and a file that includes others, is past the read, can't be read, or sits
behind a folder that can't be searched or listed counts as maybe too), and on any unexpected error. With the CLI
there, the profile it selects is acted on and every other profile and location is looked through the same way
(_mentioned_elsewhere): metacomputer set up in another one keeps them. The step's output, one line each, for install.sh: "ok TEXT", "warn TEXT", "say TEXT", and for --uninstall
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

if __name__ == "__main__":         # the agents' setup.py import this module by its name: the same module, not a second
    sys.modules.setdefault("agent_setup", sys.modules[__name__])
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
def jsonc_loads(text, trailing_commas=False):
    """JSON with // and /* */ comments, as Gemini CLI reads its settings.json (it refuses trailing commas, and so does
    this unless trailing_commas: Antigravity CLI reads them); ValueError when it isn't that. Comment marks inside strings
    are kept."""
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
    text = "".join(out)
    if trailing_commas:                 # a comma before a closing bracket or brace (outside strings) is dropped
        out, i, n = [], 0, len(text)
        while i < n:
            c = text[i]
            if c == '"':
                j = i + 1
                while j < n and text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
                out.append(text[i:j + 1])
                i = j + 1
                continue
            if c == ",":
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j < n and text[j] in "]}":
                    i += 1
                    continue
            out.append(c)
            i += 1
        text = "".join(out)
    return json.loads(text)


# --- the agents --------------------------------------------------------------------------------------------------
def _entry_res(d):
    """An MCP server entry (a dict, as the agent keeps it) as claude_mcp's result: {"kind", "entry"}; a local command
    is "stdio" with its command, arguments and variables; an error when its parts have the wrong types."""
    if not isinstance(d, dict):
        return {"kind": "error", "why": "its entry has an unexpected shape"}
    cmd, args, envs = d.get("command"), d.get("args"), d.get("env")
    url_kind = d.get("transport") or d.get("type") or ("http" if d.get("httpUrl") or d.get("serverUrl")
                                                       else "sse" if d.get("url") else None)
    if cmd is None and url_kind:
        return {"kind": url_kind if isinstance(url_kind, str) else "remote", "entry": d}
    if (not isinstance(cmd, str) or not cmd or not isinstance(args if args is not None else [], list)
            or not all(isinstance(a, str) for a in args or []) or not isinstance(envs if envs is not None else {}, dict)):
        return {"kind": "error", "why": "its entry has an unexpected shape"}
    if url_kind not in (None, "stdio"):
        return {"kind": url_kind if isinstance(url_kind, str) else "remote", "entry": d}
    return {"kind": "stdio", "entry": {"command": cmd, "args": list(args or []), "env": dict(envs or {})}}


def manifest_words(folder):
    """(name, label, prog, tools, doc) of an agent, from its manifest (coding-agents/FOLDER/manifest.json): its install
    word (its flag), its name, its command, how it names metacomputer's tools, its page."""
    from agentkit import manifests
    m = manifests.manifest(folder)
    return m["flag"], m["label"], m["prog"], m["tools"], m["docs"]


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
        """Every file that may hold its MCP servers, the one its CLI acts on first (the profile in use), then every other
        profile and configuration location: what an uninstall checks before the command may go."""
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

    def settle(self, prog, root, link, env, out):
        """After its MCP server is metacomputer's (under the agent's lock): what the agent's CLI couldn't set."""
        return None

    def added(self, prog, root, link, env, out):
        """After this run added metacomputer's MCP server and read it back (under the agent's lock)."""
        return None

    def removed(self, env, out):
        """After an uninstall finds the agent with no MCP server named metacomputer, or removed metacomputer's (under the
        agent's lock): True when what goes with the server is gone too."""
        return True


class Antigravity(Agent):
    """Antigravity CLI (`agy`, Google's successor to Gemini CLI for its free and AI Pro/Ultra users; a Go executable):
    MCP servers in ~/.gemini/config/mcp_config.json, which the Antigravity app and IDE read too, added with `agy mcp add`
    (it adds or replaces an entry of the same name without asking, so the entry is read right before: from the file,
    since `agy mcp list` prints only a table) and removed with `agy mcp remove`. Its CLI rewrites the whole file as plain
    JSON (sorted keys, two-space indent) and refuses one with comments or trailing commas, which agy itself reads, so the
    file is read here the way agy reads it. Its tool timeout is 180 s by default and its CLI has no option for one:
    settle() gives metacomputer's own entry timeoutSeconds 600. It asks before every MCP tool call unless a permission
    rule allows it: when this run adds the server, added() puts metacomputer's own rule, mcp(metacomputer/*), in
    permissions.allow of ~/.gemini/antigravity-cli/settings.json (as Gemini CLI's server gets "trust"; it has no CLI for
    it) and notes that it did (record()), and an uninstall takes that exact rule out again only then (removed()): a rule
    of the user's stays. A file of these that is a link is never replaced. Skills: ~/.gemini/antigravity-cli/skills, the
    CLI's own folder (it doesn't read ~/.agents/skills). Checked with agy 1.5.15 in a throwaway home."""
    name, label, prog, tools, doc = ("antigravity", "Antigravity CLI", "agy", "call_mcp_tool with server metacomputer",
                                     "docs/antigravity.md")
    reads_files = True
    allow = f"mcp({NAME}/*)"            # metacomputer's own permission rule: its server's tools, without asking

    def env(self, env):
        return dict(env, AGY_CLI_DISABLE_AUTO_UPDATE="true")     # no update for a quick command

    def config(self, env):
        return os.path.join(home(env), ".gemini", "config", "mcp_config.json")

    def skills_dir(self, env):
        return os.path.join(home(env), ".gemini", "antigravity-cli", "skills")

    def config_files(self, env):
        return [self.config(env)]

    def settings(self, env):
        return os.path.join(home(env), ".gemini", "antigravity-cli", "settings.json")

    def _text(self, path):
        """The file's text (None: not there), or raises Unreadable-like ValueError with why."""
        try:
            with open(path, "rb") as f:
                data = f.read(SETTINGS_CAP + 1)
        except (FileNotFoundError, NotADirectoryError):
            return None, None
        except OSError:
            raise ValueError("its mcp_config.json couldn't be read")
        if len(data) > SETTINGS_CAP:
            raise ValueError("its mcp_config.json is larger than 4 MB, so it wasn't read in full")
        try:
            return data, data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("its mcp_config.json isn't text")

    def read(self, prog, env):
        try:
            _, text = self._text(self.config(env))
        except ValueError as e:
            return {"kind": "error", "why": str(e)}
        if text is None or not text.strip():
            return {"kind": "absent"}
        plain = True
        try:
            d = json.loads(text)
        except ValueError:
            plain = False
            try:
                d = jsonc_loads(text, trailing_commas=True)
            except (ValueError, RecursionError):
                return {"kind": "error", "why": "its mcp_config.json isn't JSON this can read"}
        except RecursionError:
            return {"kind": "error", "why": "its mcp_config.json isn't JSON this can read"}
        servers = d.get("mcpServers") if isinstance(d, dict) else None
        if servers is None:
            return {"kind": "absent", "plain": plain}
        if not isinstance(servers, dict):
            return {"kind": "error", "why": "its mcpServers isn't a table"}
        if NAME not in servers:
            return {"kind": "absent", "plain": plain}
        res = _entry_res(servers[NAME])
        res["raw"], res["plain"] = servers[NAME], plain
        return res

    def add_argv(self, prog, link):
        return [prog, "mcp", "add", NAME, link, *claude_mcp.MCP_ARGS]

    def remove_argv(self, prog):
        return [prog, "mcp", "remove", NAME]

    def add_line(self, link_shown):
        full = os.path.expanduser(link_shown)          # in the file, the command's whole path (no shell expands ~ there)
        return (f"agy mcp add {NAME} {link_shown} mcp-lazy (its mcp command refuses a mcp_config.json with comments or "
                f"trailing commas: then add \"{NAME}\": {{\"command\": \"{full}\", \"args\": [\"mcp-lazy\"], "
                f"\"timeoutSeconds\": {TOOL_TIMEOUT}}} under mcpServers in ~/.gemini/config/mcp_config.json yourself)")

    def remove_line(self):
        return f"agy mcp remove {NAME}"

    def settle(self, prog, root, link, env, out):
        """metacomputer's own entry gets timeoutSeconds 600 when it has none (one you set stays): the file read whole, the
        entry checked to be metacomputer's, the file written again as agy's own CLI writes it (plain JSON, sorted keys,
        two-space indent) to a new file beside it, which replaces it only if the file is still exactly what was read and
        isn't a link (replace_if_unchanged); then read back. Anything else leaves it as it is, said."""
        path = self.config(env)
        by_hand = (f"add \"timeoutSeconds\": {TOOL_TIMEOUT} to metacomputer's entry in ~/.gemini/config/mcp_config.json "
                   "yourself (without it, Antigravity CLI ends a tool call after 180 s)")
        try:
            data, text = self._text(path)
            d = json.loads(text) if text is not None else None
        except (ValueError, RecursionError):
            d = None
        entry = ((d or {}).get("mcpServers") or {}).get(NAME) if isinstance(d, dict) else None
        if not isinstance(entry, dict) or classify(_entry_res(entry), root, link) != "ok":
            out("warn", f"couldn't give metacomputer's MCP server a longer tool timeout (its mcp_config.json isn't plain "
                        f"JSON, or changed); {by_hand}")
            return
        if "timeoutSeconds" in entry:
            return
        entry["timeoutSeconds"] = TOOL_TIMEOUT
        got = replace_if_unchanged(path, data, (json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
                                   .encode("utf-8"))
        if got == "changed":
            out("warn", f"Antigravity CLI's mcp_config.json changed while metacomputer's tool timeout was added, so it was "
                        f"left as it is; run ./install.sh --only-{self.name} again, or {by_hand}")
            return
        if got == "link":
            out("warn", f"Antigravity CLI's mcp_config.json is a link, so it was left as it is (a file kept elsewhere "
                        f"stays linked); {by_hand}")
            return
        if got != "ok":
            out("warn", f"couldn't write Antigravity CLI's mcp_config.json; {by_hand}")
            return
        res = self.read(prog, env)
        if classify(res, root, link) == "ok" and (res.get("raw") or {}).get("timeoutSeconds") == TOOL_TIMEOUT:
            out("ok", f"tool timeout {TOOL_TIMEOUT} s for metacomputer's MCP server (Antigravity CLI's is 180 s)")
        else:
            out("warn", f"couldn't confirm metacomputer's tool timeout in Antigravity CLI's mcp_config.json; {by_hand}")

    def _rules(self, d):
        """(permissions, allow list) of a settings dict, or None when they have another shape."""
        perms = d.get("permissions", {})
        if not isinstance(perms, dict):
            return None
        allow = perms.get("allow", [])
        if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
            return None
        for k in ("ask", "deny"):
            v = perms.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return None
        return perms, allow

    def _settings_change(self, env, out, add):
        """Add (add=True) or take out metacomputer's own permission rule, exactly `mcp(metacomputer/*)`, in
        permissions.allow of Antigravity CLI's settings.json; nothing else in the file changes. Only plain JSON is
        written (agy keeps it so; one with comments or trailing commas is left as it is, said), to a new file beside it
        that replaces it only if the file is still exactly what was read, with its mode, and never over a link (a
        settings.json kept elsewhere stays linked, said); then read back. An ask or deny rule of the user's for
        metacomputer's tools (or all MCP tools) wins: no allow rule is added next to it. "added", "removed", "there"
        (already as wanted: present, or absent), "theirs" (the user's ask or deny rule) or None (left as it is, said)."""
        path, rule = self.settings(env), self.allow
        by_hand = (f"{'add' if add else 'take'} \"{rule}\" {'to' if add else 'out of'} permissions.allow in "
                   "~/.gemini/antigravity-cli/settings.json yourself")
        try:
            with open(path, "rb") as f:
                data = f.read(SETTINGS_CAP + 1)
        except (FileNotFoundError, NotADirectoryError):
            data = None
        except OSError:
            out("say", f"Antigravity CLI's settings.json couldn't be read, so it was left as it is; {by_hand}")
            return None
        if data is None:
            if not add:
                return "there"
            d = {}
        else:
            try:
                d = json.loads(data.decode("utf-8")) if len(data) <= SETTINGS_CAP else None
            except (ValueError, UnicodeDecodeError, RecursionError):
                d = None
            got = self._rules(d) if isinstance(d, dict) else None
            if got is None:
                out("say", "Antigravity CLI's settings.json isn't plain JSON this can change (comments, trailing commas, "
                           f"another shape), so it was left as it is; {by_hand}")
                return None
        perms, allow = self._rules(d)
        if add:
            if rule in allow:
                return "there"
            theirs = [x for x in perms.get("ask", []) + perms.get("deny", [])
                      if x == "mcp(*)" or x.startswith(f"mcp({NAME}/") or x == f"mcp({NAME})"]
            if theirs:
                out("say", "your Antigravity CLI settings ask about or deny metacomputer's tools (or every MCP tool), so "
                           "no rule allowing them was added")
                return "theirs"
            d.setdefault("permissions", perms)["allow"] = allow + [rule]
        else:
            if rule not in allow:
                return "there"
            perms["allow"] = [x for x in allow if x != rule]
        new = (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        got = replace_if_unchanged(path, data, new)
        if got != "ok":
            why = {"changed": "changed while it was being changed",
                   "link": "is a link (a file kept elsewhere stays linked)"}.get(got, "couldn't be written")
            out("say", f"Antigravity CLI's settings.json {why}, so it was left as it is; {by_hand}")
            return None
        try:
            with open(path, "rb") as f:
                back = json.loads(f.read(SETTINGS_CAP + 1).decode("utf-8"))
            allow_back = ((back.get("permissions") or {}).get("allow") or []) if isinstance(back, dict) else []
            ok = (rule in allow_back) == add
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
            ok = False
        if not ok:
            out("say", f"couldn't confirm the change to Antigravity CLI's settings.json; {by_hand}")
            return None
        return "added" if add else "removed"

    def record(self, env):
        """metacomputer's own note that its install added the permission rule (to this settings.json): only then does an
        uninstall take the rule out again; a rule of the user's, the same words included, stays."""
        return os.path.join(home(env), ".local", "share", "metacomputer", "antigravity-rule.json")

    def _recorded(self, env):
        try:
            with open(self.record(env), "rb", opener=_nofollow) as f:
                d = json.loads(f.read(65536).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        return isinstance(d, dict) and d.get("rule") == self.allow and d.get("settings") == self.settings(env)

    def _remember(self, env, on):
        """Write (on) or remove the record: a new file (0600, in a 0700 folder) put in its place, never through a link.
        False when it couldn't be."""
        path = self.record(env)
        try:
            if not on:
                if os.path.lexists(path):
                    os.unlink(path)
                return True
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            tmp = path + ".metacomputer-new"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, (json.dumps({"rule": self.allow, "settings": self.settings(env)}) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, path)
            return True
        except OSError:
            return False

    def added(self, prog, root, link, env, out):
        got = self._settings_change(env, out, add=True)
        if got == "added" and not self._remember(env, True):
            out("say", f"couldn't note that this install added \"{self.allow}\" to Antigravity CLI's settings.json, so "
                       "--uninstall will leave it: take it out yourself then")
        if got in ("added", "there"):
            out("ok", f"its tools allowed without asking (\"{self.allow}\" in ~/.gemini/antigravity-cli/settings.json; "
                      "take it out to be asked before each call)")

    def removed(self, env, out):
        """The permission rule goes only when this install added it (its record), and the record with it; a rule of the
        user's stays."""
        if not self._recorded(env):
            try:
                with open(self.settings(env), "rb") as f:
                    theirs = f'"{self.allow}"'.encode() in f.read(SETTINGS_CAP + 1)
            except OSError:
                theirs = False
            if theirs:
                out("say", f"\"{self.allow}\" in Antigravity CLI's settings.json stays: metacomputer's install didn't add "
                           "it (take it out yourself to be asked before each call)")
            return True
        got = self._settings_change(env, out, add=False)
        if got is None:
            return False
        self._remember(env, False)
        if got == "removed":
            out("ok", f"took \"{self.allow}\" out of Antigravity CLI's settings.json (this install had added it)")
        return True


_HERE = {"antigravity": Antigravity}    # not in their folders yet
_AGENTS = None


def _agents():
    """{name: Agent} of the agents whose install and uninstall are these steps (agents/<name>/setup.py's AGENT), in the
    registry's order."""
    global _AGENTS
    if _AGENTS is None:
        from agentkit import manifests, registry
        out = {}
        for n in manifests.generic():
            mod = registry.hooks(n, "setup")
            a = mod.AGENT if mod is not None else _HERE[n]()
            out[a.name] = a
        _AGENTS = out
    return _AGENTS


def __getattr__(name):              # AGENTS, made when first asked for (an agent's setup.py imports this module)
    if name == "AGENTS":
        return _agents()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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


def _distinct(paths):
    seen, out = set(), []
    for p in paths:
        k = os.path.normpath(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


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


def _nofollow(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)


def _identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _still(path, data):
    """Whether path still holds exactly data (None: still missing) and is still the file that holds it: its content
    read again through a descriptor (never through a link), with that file's identity (device, inode, size, modification
    and change times) the same before and after the read and the same as what's at path now."""
    if data is None:
        return not os.path.lexists(path)
    try:
        with open(path, "rb", opener=_nofollow) as f:
            before = os.fstat(f.fileno())
            again = f.read(SETTINGS_CAP + 1)
            after = os.fstat(f.fileno())
        now = os.lstat(path)
    except OSError:
        return False
    return again == data and stat.S_ISREG(now.st_mode) and _identity(before) == _identity(after) == _identity(now)


def replace_if_unchanged(path, data, new):
    """Write new to a file beside path, then put it in path's place only if path still holds exactly data (None: path
    must still be missing), with path's mode (a new one: 0600). A path that is a link is never replaced (a dotfile kept
    elsewhere stays linked): "link", and the caller says what to change by hand. "ok", "changed" (left as it is),
    "link" or "error".

    The check is as close to the replace as a file allows: right before it, path's content is read again and its
    identity compared (_still). A program that writes the file in the very instant between that check and the replace
    can still lose its change: a file can't be compared and swapped against any writer (docs/install.md)."""
    tmp = path + ".metacomputer-new"
    try:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        if st is not None and stat.S_ISLNK(st.st_mode):
            return "link"
        if (st is None) != (data is None):
            return "changed"
        if data is None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            mode = 0o600
        else:
            mode = stat.S_IMODE(st.st_mode)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            os.write(fd, new)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not _still(path, data):
            os.unlink(tmp)
            return "changed"
        os.replace(tmp, path)
        return "ok"
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
        agent.settle(prog, root, link, env, out)
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
        agent.settle(prog, root, link, env, out)
        agent.added(prog, root, link, env, out)
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
        agent.removed(env, out)         # no server named metacomputer: what went with metacomputer's goes too
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
        agent.removed(env, out)
        return True
    out("warn", f"couldn't remove {agent.label}'s {NAME} MCP server (or confirm it's gone); remove it yourself: "
                f"{agent.remove_line()}")
    out("keep", f"{agent.label}'s")
    return False


def _mentioned_elsewhere(agent, env):
    """The agent's other configuration (every profile and location but the one its CLI just acted on) that may run
    metacomputer's command (mentions) or hides what it holds (unlisted): [(path, why)]."""
    files = agent.config_files(env)
    active = os.path.normpath(files[0]) if files else None
    found = [(f, mentions(f)) for f in files if os.path.normpath(f) != active]
    found += [(d, unlisted(d)) for d in agent.config_dirs(env)]
    return [(f, why) for f, why in found if why]


def uninstall(agent, root, link, env, out):
    """Remove what this checkout set up for one agent: its MCP server when it's metacomputer's own and runs this
    checkout (read again right before, and checked after), and the skill links metacomputer made. An entry that can't be
    read keeps the command links (keep), and so does any other profile or configuration location of the agent's that
    may run the command or can't be read, whether or not its CLI is there. True when it finished."""
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
            # Whatever profile the CLI acted on, every other one is checked too (without the CLI, _unregister looked
            # through them all): one that may still run the command keeps it.
            elsewhere = _mentioned_elsewhere(agent, env) if (prog is not None or agent.reads_files) else []
            if elsewhere:
                out("warn", f"{agent.label}'s other configuration may run metacomputer's command "
                            f"({'; '.join(tilde(f, env) + ' ' + why for f, why in elsewhere)}), so it stays; remove "
                            "metacomputer's MCP server there too (that profile selected), then run ./install.sh "
                            "--uninstall again")
                out("keep", f"{agent.label}'s")
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
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import strictargs           # its command line, by metacomputer's rules, before anything is done
    strictargs.expect(argv, ["link-ours PATH --root CHECKOUT", "install|uninstall|status AGENT --root CHECKOUT --link LINK"],
                      prog="agent_setup.py")
    if argv[:1] == ["link-ours"]:          # install.sh's Codex step: the same ownership rule for its skill links
        if len(argv) != 4 or argv[2] != "--root":
            print("usage: agent_setup.py link-ours PATH --root CHECKOUT", file=sys.stderr)
            return 2
        return 0 if os.path.islink(argv[1]) and ours_link(argv[1], argv[3]) else 1
    if len(argv) != 6 or argv[0] not in ("install", "uninstall", "status") or argv[1] not in _agents() \
            or argv[2] != "--root" or argv[4] != "--link":
        print(f"usage: agent_setup.py install|uninstall|status {'|'.join(_agents())} --root CHECKOUT --link "
              "~/.local/bin/metacomputer\n       agent_setup.py link-ours PATH --root CHECKOUT", file=sys.stderr)
        return 2
    what, agent, root, link = argv[0], _agents()[argv[1]], argv[3], argv[5]
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
