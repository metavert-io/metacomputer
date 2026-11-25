"""The rule every agent's MCP server named metacomputer is read by (lib/agent_setup.py, and Claude Code's and Codex's
steps in their folders): the arguments metacomputer registers its server with, the paths earlier versions registered,
and how an entry is named in a message (words: metacomputer's own by its command; anyone else's by its program and how
many arguments it has, or its transport, never an argument, a variable or a URL), stood for (digest) and found to run
the command link itself (linked). Python 3.9 or later; stdlib only."""
import hashlib
import json
import os
import re
import shlex

US = "\x1f"
MCP_ARGS = ["mcp-lazy"]                     # the only arguments metacomputer has registered its server with
# This checkout's launchers by the exact paths earlier versions registered them under (some of them renamed since, so
# they may be gone): bin/metacomputer, and the names before it.
LEGACY_NAMES = ("metacomputer", "claude-vc", "vc", "cua-lab", "virtual-computer")
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}")    # a name a message may carry about someone else's server


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
