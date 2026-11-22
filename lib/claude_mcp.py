#!/usr/bin/env python3
"""Claude Code's user-scope MCP servers, read exactly, and what one is to a metacomputer checkout. Used by install.sh
(its Claude Code step and --uninstall) and `metacomputer agents`.

    python3 lib/claude_mcp.py user-get NAME --root CHECKOUT --link ~/.local/bin/metacomputer
    python3 lib/claude_mcp.py user-recheck NAME < DIGEST

user-get prints one line of five fields split by \\x1f: OWNER, KIND, WORDS, DIGEST, LINKED.
  OWNER  what the user-scope server NAME is to CHECKOUT (owner() below): ok, ours, checkout, foreign, absent or error
  KIND   stdio, another transport (http, sse, ...), absent or error
  WORDS  what it is, in words, for messages. metacomputer's own (ok, ours, checkout): its command and arguments. Anything
         else: its program's name and how many arguments it has, or its transport, and nothing more: never its
         arguments (`npx mcp-remote https://...?token=...` carries a credential in one), the values of its variables,
         a URL, headers or the entry; for an error, why
  DIGEST a digest of the whole entry (its variables included), for user-recheck
  LINKED "link" when it's a local command that runs the command link itself (LINK, or the alias mcomp beside it),
         whoever's it is, else empty: --uninstall keeps those links while an entry that stays runs them
install.sh removes or rewrites only metacomputer's own entries (ok, ours, checkout), never anyone else's. user-recheck
reads the entry again, from the configuration file alone (quick: no `claude mcp get`), right before install.sh removes
one, and prints "same" when it's still exactly the entry whose DIGEST it reads on stdin, "changed" when it isn't (or
is gone), or "error" and why, split by \\x1f.

Claude Code has no structured read of one server: `claude mcp get NAME` prints display text, with the arguments
joined by spaces (an argument's own spaces are lost). So the entry comes from the file Claude Code keeps user-scope
servers in (config_path(): the one Claude Code itself picks, its legacy `.config.json` or `.claude.json`), and is
checked against what `claude mcp get NAME` shows from a new empty folder, where no project or local server can stand
in for it: the user scope, the type, and the command and arguments as text. The file is read before and after that
command, and the entry must be the same in both reads. When anything disagrees, or can't be read, the answer is
"error", and install.sh changes nothing.

Python 3.9 or later (install.sh may run it with macOS's own python3); stdlib only.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

US = "\x1f"
MCP_ARGS = ["mcp-lazy"]                     # the only arguments metacomputer has registered its server with
# This checkout's launchers by the exact paths earlier versions registered them under (some of them renamed since, so
# they may be gone): bin/metacomputer, and the names before it.
LEGACY_NAMES = ("metacomputer", "claude-vc", "vc", "cua-lab", "virtual-computer")
SHOW_TIMEOUT = 90                           # `claude mcp get` checks the server's health first
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}")    # a name a message may carry about someone else's server


def config_path(env):
    """The file Claude Code keeps its user configuration in, chosen as Claude Code chooses it (checked against its
    2.1.330 build): `.claude.json` in CLAUDE_CONFIG_DIR, or the home folder (`.claude-custom-oauth.json` with
    CLAUDE_CODE_CUSTOM_OAUTH_URL), unless the legacy `.config.json` in CLAUDE_CONFIG_DIR, or ~/.claude, is a regular
    file and either
      - not empty: the legacy file, even beside a populated `.claude.json` (unless that one is populated and the
        folder doesn't list the legacy file under that exact name: a case-insensitive disk found another spelling)
      - empty but writable, and `.claude.json` is missing or can't be looked at (an empty legacy file beside an
        existing `.claude.json`: `.claude.json`; on Linux, unless that one is an empty read-only file with one link).
    A directory there, a dangling link, or an empty read-only file: `.claude.json`."""
    home = env.get("HOME") or os.path.expanduser("~")
    cfg_dir = env.get("CLAUDE_CONFIG_DIR")
    modern = os.path.join(cfg_dir or home, ".claude%s.json" % ("-custom-oauth" if env.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""))
    legacy = os.path.join(cfg_dir or os.path.join(home, ".claude"), ".config.json")
    try:
        ls = os.stat(legacy)
    except OSError:
        return modern
    if not stat.S_ISREG(ls.st_mode) or (ls.st_size == 0 and not ls.st_mode & 0o222):
        return modern
    try:
        ms = os.stat(modern)
    except FileNotFoundError:
        return legacy
    except OSError:
        if ls.st_size == 0:
            return legacy
        ms = None
    if ls.st_size == 0:
        linux_placeholder = (sys.platform.startswith("linux") and stat.S_ISREG(ms.st_mode) and ms.st_size == 0
                             and not ms.st_mode & 0o222 and ms.st_nlink == 1)
        return legacy if linux_placeholder else modern
    if ms is not None and (not stat.S_ISREG(ms.st_mode) or ms.st_size == 0):
        return legacy
    try:
        if os.path.basename(legacy) not in os.listdir(os.path.dirname(legacy)):
            return modern                   # a case-insensitive disk found it under another spelling
    except FileNotFoundError:
        return modern
    except OSError:
        pass
    return legacy


def read_servers(env):
    """(servers, None) from the user configuration: a dict of name -> entry ({} when there's no file yet), or
    (None, why). A UTF-8 byte-order mark is allowed, as Claude Code allows it."""
    path = config_path(env)
    try:
        with open(path, encoding="utf-8-sig") as f:
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


def kind_of(entry):
    return entry.get("type") or ("stdio" if "command" in entry else None)


def user_entry(prog, name, env=None):
    """{"kind": "absent" | "error" | TYPE, "entry": the user-scope entry (a dict) or None, "why": for an error}."""
    env = dict(os.environ if env is None else env)
    servers, why = read_servers(env)
    if servers is None:
        return {"kind": "error", "entry": None, "why": why}
    s = shown(prog, name, env)
    # read again: `claude mcp get` takes a while (it checks the server's health), and another Claude Code may change the
    # entry meanwhile, in a part the display doesn't show (its variables); what's classified is what both reads saw
    again, why = read_servers(env)
    if again is None:
        return {"kind": "error", "entry": None, "why": why}
    entry = servers.get(name)
    if again.get(name) != entry:
        return {"kind": "error", "entry": None, "why": "it changed while it was being read"}
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
    kind = kind_of(entry)
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


def program_name(cmd):
    """The file name of someone else's program, for a message: only a plain short word (never a path, or a name
    that's more than a name), else ""."""
    base = os.path.basename(cmd.rstrip("/")) if isinstance(cmd, str) else ""
    return base if PLAIN.fullmatch(base) else ""


def words(res, env, own):
    """What it is, in words. metacomputer's own (OWN ok, ours or checkout: its launcher, exactly ["mcp-lazy"], no
    variables but the ones you set on ~/.local/bin/metacomputer's): its command and arguments. Anything else: its
    program's name and the count of its arguments, or its transport; never an argument, a variable's value, a URL or
    a header, which can each carry a credential."""
    kind, e = res["kind"], res["entry"]
    if kind == "absent":
        return "nothing"
    if kind == "error":
        return res.get("why") or "unreadable"
    if kind != "stdio":
        return f"an MCP server of type {kind if PLAIN.fullmatch(kind) else 'another kind'}, not a local command"
    extra = ", with variables of its own" if e.get("env") else ""
    if own in ("ok", "ours", "checkout"):
        home = env.get("HOME") or os.path.expanduser("~")
        cmd = e["command"]
        shown_cmd = "~" + cmd[len(home):] if home and cmd.startswith(home + os.sep) else cmd
        return " ".join([shlex.quote(shown_cmd) if shown_cmd == cmd else shown_cmd] + [shlex.quote(a) for a in MCP_ARGS]) + extra
    n = len(e.get("args") or [])
    what = program_name(e["command"]) or "a local command"
    return what + (f" with {n} argument{'' if n == 1 else 's'} (not shown)" if n else ", without arguments") + extra


def digest(entry):
    """The whole entry, variables included, as a digest: what user-recheck compares."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def linked(res, link):
    """Does the entry run the command link itself (LINK, or the alias mcomp beside it), whoever's entry it is?"""
    e = res["entry"]
    if res["kind"] != "stdio" or not isinstance(e, dict) or not isinstance(e.get("command"), str):
        return False
    links = {os.path.normpath(link), os.path.normpath(os.path.join(os.path.dirname(link), "mcomp"))}
    return os.path.normpath(e["command"]) in links


def line(prog, name, root, link, env=None):
    env = dict(os.environ if env is None else env)
    if not prog:
        res = {"kind": "error", "entry": None, "why": "Claude Code's claude command isn't on PATH"}
    else:
        res = user_entry(prog, name, env)
    own = owner(res, root, link)
    e = res["entry"]
    fields = [own, res["kind"], words(res, env, own), digest(e) if e else "", "link" if linked(res, link) else ""]
    return US.join(f.replace(US, " ").replace("\n", " ") for f in fields)


def recheck(name, want, env=None):
    """The entry NAME again, right before it's removed: "same" when it's still the one whose digest is WANT, else
    "changed", or "error<US>why"."""
    env = dict(os.environ if env is None else env)
    servers, why = read_servers(env)
    if servers is None:
        return "error" + US + why
    e = servers.get(name)
    if not isinstance(e, dict) or not want or digest(e) != want:
        return "changed"
    return "same"


def main(argv):
    if len(argv) == 6 and argv[0] == "user-get" and argv[2] == "--root" and argv[4] == "--link":
        print(line(shutil.which("claude"), argv[1], argv[3], argv[5]))
        return 0
    if len(argv) == 2 and argv[0] == "user-recheck":
        print(recheck(argv[1], sys.stdin.read().strip()))
        return 0
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
