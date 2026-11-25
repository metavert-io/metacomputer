"""Antigravity CLI's part of lib/agent_setup.py: its install, uninstall and status steps (an Agent: what's different about
its MCP server, its tool timeout and permission rule, and its skills). install.sh runs them through lib/agent_setup.py
install|uninstall|status antigravity."""
import json
import os

from agentkit import mcp_entry   # MCP_ARGS: the arguments metacomputer's server is registered with
from agent_setup import (Agent, manifest_words, NAME, TOOL_TIMEOUT, SETTINGS_CAP, home, _entry_res,  # the framework
                         jsonc_loads, classify, replace_if_unchanged, _nofollow)


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
    name, label, prog, tools, doc = manifest_words("antigravity")
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
        return [prog, "mcp", "add", NAME, link, *mcp_entry.MCP_ARGS]

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


AGENT = Antigravity()
