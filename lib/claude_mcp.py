#!/usr/bin/env python3
"""Claude Code's user-scope MCP servers, read exactly, and what one is to a metacomputer checkout. Used by install.sh
(its Claude Code step and --uninstall) and `metacomputer agents`.

    python3 lib/claude_mcp.py user-get NAME --root CHECKOUT --link ~/.local/bin/metacomputer

prints one line of four fields split by \\x1f: OWNER, KIND, WORDS, ENTRY.
  OWNER  what the user-scope server NAME is to CHECKOUT (owner() below): ok, ours, checkout, foreign, absent or error
  KIND   stdio, another transport (http, sse, ...), absent or error
  WORDS  what it runs, in words, for messages: the command and its arguments quoted as a shell would need them (never
         the values of its variables, a URL or headers); for an error, why
  ENTRY  the entry's exact JSON, for `claude mcp add-json` to put back after a failed replacement: only for a local
         command without variables of its own (anything else isn't replaced, so it's never needed or printed)

Claude Code has no structured read of one server: `claude mcp get NAME` prints display text, with the arguments
joined by spaces (an argument's own spaces are lost). So the entry comes from the file Claude Code keeps user-scope
servers in (`.config.json` in CLAUDE_CONFIG_DIR, or ~/.claude, when that legacy file exists; else `.claude.json` in
CLAUDE_CONFIG_DIR, or the home folder), and is checked against what `claude mcp get NAME` shows from a new empty folder,
where no project or local server can stand in for it: the user scope, the type, and the command and arguments as text.
When the two disagree, or either can't be read, the answer is "error", and install.sh changes nothing.

Python 3.9 or later (install.sh may run it with macOS's own python3); stdlib only.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

US = "\x1f"
MCP_ARGS = ["mcp-lazy"]                     # the only arguments metacomputer has registered its server with
# This checkout's launchers by the exact paths earlier versions registered them under (some of them renamed since, so
# they may be gone): bin/metacomputer, and the names before it.
LEGACY_NAMES = ("metacomputer", "claude-vc", "vc", "cua-lab", "virtual-computer")
SHOW_TIMEOUT = 90                           # `claude mcp get` checks the server's health first


def config_path(env):
    home = env.get("HOME") or os.path.expanduser("~")
    cfg_dir = env.get("CLAUDE_CONFIG_DIR")
    legacy = os.path.join(cfg_dir or os.path.join(home, ".claude"), ".config.json")
    return legacy if os.path.exists(legacy) else os.path.join(cfg_dir or home, ".claude.json")


def read_servers(env):
    """(servers, None) from the user configuration: a dict of name -> entry ({} when there's no file yet), or
    (None, why)."""
    path = config_path(env)
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return None, f"couldn't read {os.path.basename(path)} ({type(e).__name__})"
    if not isinstance(cfg, dict):
        return None, f"{os.path.basename(path)} isn't a JSON object"
    servers = cfg.get("mcpServers", {})
    if not isinstance(servers, dict):
        return None, f"mcpServers in {os.path.basename(path)} isn't an object"
    return servers, None


def shown(prog, name, env):
    """What `claude mcp get NAME` shows from a new empty folder: {"state": "absent" | "shown" | "error", ...}."""
    try:
        with tempfile.TemporaryDirectory(prefix="mcomp-mcp-") as td:
            r = subprocess.run([prog, "mcp", "get", name], cwd=td, env=env, capture_output=True, text=True,
                               timeout=SHOW_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return {"state": "error", "why": f"claude mcp get didn't run ({type(e).__name__})"}
    lines = (r.stdout + "\n" + r.stderr).splitlines()
    if r.returncode != 0:
        if any(ln.startswith(f'No MCP server named "{name}"') for ln in lines):
            return {"state": "absent"}
        return {"state": "error", "why": f"claude mcp get {name} failed (exit {r.returncode})"}
    f = {}
    for ln in r.stdout.splitlines():
        if ln.startswith("  ") and not ln.startswith("   ") and ":" in ln:
            k, _, v = ln[2:].partition(":")
            f.setdefault(k, v[1:] if v.startswith(" ") else v)
    return {"state": "shown", "scope": f.get("Scope", ""), "type": f.get("Type"), "command": f.get("Command"),
            "args": f.get("Args"), "url": f.get("URL")}


def user_entry(prog, name, env=None):
    """{"kind": "absent" | "error" | TYPE, "entry": the user-scope entry (a dict) or None, "why": for an error}."""
    env = dict(os.environ if env is None else env)
    servers, why = read_servers(env)
    if servers is None:
        return {"kind": "error", "entry": None, "why": why}
    s = shown(prog, name, env)
    entry = servers.get(name)
    if s["state"] == "error":
        return {"kind": "error", "entry": None, "why": s["why"]}
    if s["state"] == "absent":
        if entry is not None:
            return {"kind": "error", "entry": None, "why": "claude mcp get shows none, but the configuration has one"}
        return {"kind": "absent", "entry": None, "why": ""}
    if not str(s.get("scope") or "").startswith("User config"):
        return {"kind": "error", "entry": None, "why": "claude mcp get shows another scope's server from an empty folder"}
    if not isinstance(entry, dict):
        return {"kind": "error", "entry": None, "why": "the configuration has no user-scope entry for it"}
    kind = entry.get("type") or ("stdio" if "command" in entry else None)
    if not isinstance(kind, str) or kind != s.get("type"):
        return {"kind": "error", "entry": None, "why": "its type differs between claude mcp get and the configuration"}
    if kind == "stdio":
        cmd, args, envs = entry.get("command"), entry.get("args", []), entry.get("env", {})
        if (not isinstance(cmd, str) or not cmd or not isinstance(args, list)
                or not all(isinstance(a, str) for a in args) or not isinstance(envs, (dict, type(None)))):
            return {"kind": "error", "entry": None, "why": "its entry in the configuration has an unexpected shape"}
        # the display drops trailing whitespace: compared without it (the exact arguments are the configuration's)
        if cmd.rstrip() != (s.get("command") or "").rstrip() or " ".join(args).rstrip() != (s.get("args") or "").rstrip():
            return {"kind": "error", "entry": None,
                    "why": "its command or arguments differ between claude mcp get and the configuration"}
    elif s.get("url") is not None and entry.get("url") != s.get("url"):
        return {"kind": "error", "entry": None, "why": "its URL differs between claude mcp get and the configuration"}
    return {"kind": kind, "entry": entry, "why": ""}


def is_checkout(d):
    """A metacomputer checkout, as install.sh tells one: its install.sh and bin/metacomputer."""
    return os.path.isfile(os.path.join(d, "install.sh")) and os.path.isfile(os.path.join(d, "bin", "metacomputer"))


def owner(res, root, link):
    """What the user-scope entry is to the checkout ROOT, whose command link is LINK (~/.local/bin/metacomputer):
      ok        stdio, exactly LINK with exactly ["mcp-lazy"] (variables you set on it are kept)
      ours      stdio, ["mcp-lazy"], no variables, and its command is this checkout's own launcher: one that resolves
                (every link followed) to ROOT/bin/metacomputer, or one of the exact paths earlier versions registered
                (ROOT/bin/metacomputer and the names before it, which may be gone). Repaired without asking.
      checkout  the same, but the launcher of another existing checkout: moved here only with --switch (or a yes)
      foreign   anything else: other arguments (a space more or less included), variables of its own, another program
                or interpreter, a path that only starts with the checkout's, a launcher that doesn't exist (that's no
                proof of whose it was), a server that isn't a local command
      absent, error"""
    kind, e = res["kind"], res["entry"]
    if kind in ("absent", "error"):
        return kind
    if kind != "stdio":
        return "foreign"
    cmd, args, envs = e["command"], e.get("args", []), e.get("env") or {}
    if args != MCP_ARGS:
        return "foreign"
    if cmd == link:
        return "ok"
    if envs or not os.path.isabs(cmd):
        return "foreign"
    roots = {root, os.path.realpath(root)}
    if cmd in {os.path.join(r, "bin", n) for r in roots for n in LEGACY_NAMES}:
        return "ours"
    if not os.path.exists(cmd):
        return "foreign"
    real = os.path.realpath(cmd)
    if os.path.basename(real) != "metacomputer" or os.path.basename(os.path.dirname(real)) != "bin":
        return "foreign"
    d = os.path.dirname(os.path.dirname(real))
    if not is_checkout(d):
        return "foreign"
    return "ours" if d == os.path.realpath(root) else "checkout"


def words(res, env):
    """What it runs, in words: no variable values, URLs or headers."""
    kind, e = res["kind"], res["entry"]
    if kind == "absent":
        return "nothing"
    if kind == "error":
        return res.get("why") or "unreadable"
    if kind != "stdio":
        return f"an MCP server of type {kind}, not a local command"
    home = env.get("HOME") or os.path.expanduser("~")
    cmd = e["command"]
    shown_cmd = "~" + cmd[len(home):] if home and cmd.startswith(home + os.sep) else cmd
    text = " ".join([shlex.quote(shown_cmd) if shown_cmd == cmd else shown_cmd] + [shlex.quote(a) for a in e.get("args", [])])
    return text + (", with variables of its own" if e.get("env") else "")


def restorable(res):
    """The entry's exact JSON when the installer could put it back as it was: a local command without variables."""
    e = res["entry"]
    if res["kind"] != "stdio" or e.get("env"):
        return ""
    return json.dumps(e, separators=(",", ":"), sort_keys=True)


def line(prog, name, root, link, env=None):
    env = dict(os.environ if env is None else env)
    if not prog:
        res = {"kind": "error", "entry": None, "why": "Claude Code's claude command isn't on PATH"}
    else:
        res = user_entry(prog, name, env)
    fields = [owner(res, root, link), res["kind"], words(res, env), restorable(res) if res["entry"] else ""]
    return US.join(f.replace(US, " ").replace("\n", " ") for f in fields)


def main(argv):
    if len(argv) != 6 or argv[0] != "user-get" or argv[2] != "--root" or argv[4] != "--link":
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    print(line(shutil.which("claude"), argv[1], argv[3], argv[5]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
