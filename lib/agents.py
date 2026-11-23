#!/usr/bin/env python3
"""The coding agents metacomputer is set up for on this Mac: Claude Code, Codex (docs/codex.md), OpenClaw
(docs/openclaw.md), Hermes Agent (docs/hermes.md) and Gemini CLI (docs/gemini.md).

  metacomputer agents [--json]          each agent: installed (and its version), how it's set up, your choice
                                        (install.sh --no-claude, --no-codex, ..., remembered for later runs). For
                                        Claude Code: the skills (the plugin, or a skill link with --no-plugin) and
                                        the tools (the user-scope MCP server metacomputer, read as install.sh reads
                                        it, lib/claude_mcp.py: its command and exact arguments checked; the plugin
                                        declares none, and an older copy of it that still brings its own is
                                        reported), and this checkout's command that adds them. For OpenClaw,
                                        Hermes Agent and Gemini CLI: the MCP server metacomputer as each keeps it
                                        (lib/agent_setup.py, read by the agent's own CLI, or Gemini CLI's
                                        settings.json) and the skill links
  metacomputer agents check [--json]    what metacomputer relies on in this Mac's Codex, checked without a model and
                                        without touching your Codex settings (a throwaway CODEX_HOME): its rules
                                        checker, the sandbox's marker, `codex mcp` and the two settings install.sh
                                        adds (by Codex's strict config loading), and the format of your session
                                        files, tool calls included (for `learn`). Only a `codex` from an absolute
                                        PATH entry, yours or root's and not writable by others, is run

install.sh runs the check after setting Codex up, so an update to Codex that changes one of these is reported
there, and `metacomputer update` runs install.sh. Exit status of `check`: 0 all passed (or no Codex), 1 a check
failed. Stdlib only; Python 3.11 or later (tomllib).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
AGENTS_FILE = os.path.expanduser("~/.local/share/metacomputer/agents.json")
CLI = os.path.join(LAB, "bin", "metacomputer")
TIMEOUT = 60


SAFE_PATH = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(d))


def trusted(name):
    """(absolute path, None) of the `claude` or `codex` command to run, or (None, why not). Only absolute PATH entries
    are searched (a `.` on PATH would let a downloaded folder's own `codex` run here), and the file must be yours or
    root's and not writable by other accounts. Everything this runs gets the same PATH."""
    p = shutil.which(name, path=SAFE_PATH)
    if not p:
        return None, None
    real = os.path.realpath(p)
    try:
        st = os.stat(real)
    except OSError as e:
        return None, f"{real}: {e}"
    if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
        return None, f"{real} is owned by another account or writable by others: not run"
    return real, None


def run(argv, env=None, timeout=TIMEOUT, cwd=None):
    """(exit status, stdout, stderr); 127 when the program can't be run. PATH is SAFE_PATH."""
    env = dict(env if env is not None else os.environ, PATH=SAFE_PATH)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
                           cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def choices():
    try:
        d = json.load(open(AGENTS_FILE))
        return {k: d.get(k) for k in ("claude", "codex", "openclaw", "hermes", "gemini") if d.get(k) in ("on", "off")}
    except (OSError, ValueError):
        return {}


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def version(prog, env=None):
    rc, out, _ = run([prog, "--version"], env=env, timeout=20)
    return out.strip().splitlines()[0][:80] if rc == 0 and out.strip() else None


def link_target(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def claude_status():
    prog, why = trusted("claude")
    st = {"installed": bool(prog or why), "version": version(prog) if prog else None, "choice": choices().get("claude")}
    if why:
        st["untrusted"] = why
    if not prog:
        return st
    rc, out, _ = run([prog, "plugin", "list", "--json"])
    plugin = None
    plugin_server = False      # an older copy of the plugin that still declares its own MCP server
    if rc == 0:
        try:
            for p in json.loads(out):
                if p.get("id") == "metacomputer@metacomputer" and p.get("scope") == "user":
                    plugin = "on" if p.get("enabled") else "off"
                    plugin_server = bool(p.get("mcpServers")) and plugin == "on"
        except (ValueError, AttributeError, TypeError):
            pass
    st["plugin"] = plugin
    st["plugin_mcp_server"] = plugin_server
    srv = user_server(prog)
    st["user_mcp_server"] = srv["kind"] not in ("absent", "error")
    st["mcp_server"] = srv["words"] if st["user_mcp_server"] else None
    st["mcp_ok"] = srv["owner"] == "ok"
    if srv["kind"] == "error":
        st["mcp_error"] = srv["words"]
    st["skill_link"] = link_target(os.path.expanduser("~/.claude/skills/metacomputer"))
    st["set_up"] = st["mcp_ok"] or plugin_server
    return st


def user_server(prog):
    """Claude Code's user-scope MCP server metacomputer, read as install.sh reads it (lib/claude_mcp.py: the entry in
    Claude Code's configuration, checked against `claude mcp get` from an empty folder; its exact arguments compared):
    {"owner", "kind", "words"}. The words are claude_mcp's: metacomputer's own command and arguments, or for anything
    else only its program's name and how many arguments it has, or its transport; never an argument, a variable, a URL
    or the entry (they can carry credentials)."""
    import claude_mcp
    env = dict(os.environ, PATH=SAFE_PATH)
    res = claude_mcp.user_entry(prog, "metacomputer", env)
    own = claude_mcp.owner(res, LAB, os.path.expanduser("~/.local/bin/metacomputer"))
    return {"owner": own, "kind": res["kind"], "words": claude_mcp.words(res, env, own)}


INSTALL_CLAUDE = f"{os.path.join(LAB, 'install.sh')} --only-claude"


def claude_skills(s):
    return ("the plugin's skills" if s.get("plugin") == "on" else
            "a skill link" if s.get("skill_link") else "no metacomputer skill")


def claude_way(s):
    """How Claude Code is set up, in words: where the skills come from and where the tools come from."""
    skills = claude_skills(s)
    if s.get("mcp_ok") and s.get("plugin_mcp_server"):
        return (f"{skills} and the MCP server metacomputer; the plugin is an older copy that brings its own too, so the "
                "tools are listed twice: claude plugin update metacomputer@metacomputer")
    if s.get("plugin_mcp_server"):
        return f"{skills}, and the tools from an older copy of the plugin; {INSTALL_CLAUDE} moves them to the " \
               "MCP server metacomputer"
    return f"{skills} and the MCP server metacomputer"


def claude_missing(s):
    """Claude Code installed, but without metacomputer's tools: why, and what this checkout runs to add them."""
    skills = claude_skills(s)
    if s.get("mcp_error"):
        return f"{skills}; the MCP server metacomputer couldn't be read ({s['mcp_error']})"
    if s.get("user_mcp_server"):
        return (f"{skills}, but the user-scope MCP server metacomputer runs {s['mcp_server']}, not "
                f"~/.local/bin/metacomputer mcp-lazy (no metacomputer tools); {INSTALL_CLAUDE} says how to replace it")
    return f"{skills}, but no MCP server metacomputer (no tools); {INSTALL_CLAUDE} adds it"


def codex_status():
    prog, why = trusted("codex")
    st = {"installed": bool(prog or why), "version": version(prog) if prog else None, "choice": choices().get("codex")}
    if why:
        st["untrusted"] = why
    if not prog:
        return st
    rc, out, err = run([prog, "mcp", "get", "metacomputer", "--json"])
    srv = None
    set_up = False
    if rc == 0:
        try:
            t = json.loads(out).get("transport") or {}
            cmd = t.get("command")
            set_up = bool(cmd)
            # this checkout's launcher by its path; anything else by its program's name only (never a path or an argument)
            import claude_mcp
            srv = {"type": t.get("type"),
                   "command": cmd if cmd == CLI else (claude_mcp.program_name(cmd) or "a program") if cmd else None}
        except (ValueError, AttributeError):
            srv = {"error": "unreadable"}
    st["mcp_server"] = srv
    try:
        cfg = tomllib.load(open(os.path.join(codex_home(), "config.toml"), "rb")).get("mcp_servers", {}).get("metacomputer") or {}
        st["tool_timeout_sec"] = cfg.get("tool_timeout_sec")
        st["approval_mode"] = cfg.get("default_tools_approval_mode")
    except (OSError, tomllib.TOMLDecodeError):
        pass
    skills = os.path.expanduser("~/.agents/skills")
    st["skills"] = {n: link_target(os.path.join(skills, n)) for n in ("metacomputer", "cua-driver")}
    rules = os.path.join(codex_home(), "rules", "metacomputer.rules")
    try:
        head = open(rules).read(400).split("\n")
        st["rules"] = rules if head[0].startswith("# metacomputer's rules for Codex.") else "someone else's"
    except OSError:
        st["rules"] = None
    st["set_up"] = set_up
    return st


def codex_entry(prog, root, link):
    """Codex's MCP server metacomputer, read whole (`codex mcp get --json`), for install.sh: [owner, words, digest,
    command, linked], by the rule install.sh uses for Claude Code's (lib/claude_mcp.py):
      ok       a local command, exactly ROOT/bin/metacomputer or LINK (~/.local/bin/metacomputer), with exactly
               ["mcp-lazy"] (whatever else you set on it is kept)
      ours     the same with a path earlier versions registered under ROOT/bin (its command is pointed at
               ROOT/bin/metacomputer, the rest kept)
      foreign  anything else, a path that only starts with the checkout's included: never changed or removed
      absent, error (never read as absent)
    The words name metacomputer's own by its command, anything else by its program and how many arguments it has, or
    its transport (never an argument, a variable or a URL); the digest stands for the whole entry (read again right
    before a change); the command is given only for ok and ours; linked is "link" when it runs LINK or the alias mcomp
    beside it, whoever's it is."""
    import claude_mcp
    rc, out, err = run([prog, "mcp", "get", "metacomputer", "--json"])
    if rc != 0:
        if "No MCP server named 'metacomputer'" in err:
            return ["absent", "nothing", "", "", ""]
        return ["error", f"codex mcp get metacomputer failed (exit {rc})", "", "", ""]
    try:
        d = json.loads(out)
    except ValueError:
        d = None
    t = d.get("transport") if isinstance(d, dict) else None
    if not isinstance(t, dict) or not isinstance(t.get("type"), str):
        return ["error", "codex mcp get metacomputer answered in a shape this doesn't know", "", "", ""]
    kind, cmd, args, envs = t["type"], t.get("command"), t.get("args") or [], t.get("env") or {}
    if kind == "stdio" and (not isinstance(cmd, str) or not cmd or not isinstance(args, list)
                            or not all(isinstance(a, str) for a in args) or not isinstance(envs, dict)):
        return ["error", "its entry has an unexpected shape", "", "", ""]
    res = {"kind": kind, "entry": {"command": cmd, "args": args, "env": envs} if kind == "stdio" else t}
    own = "foreign"
    if kind == "stdio" and args == claude_mcp.MCP_ARGS:
        if cmd in (os.path.join(root, "bin", "metacomputer"), link):
            own = "ok"
        elif cmd in {os.path.join(r, "bin", n) for r in (root, os.path.realpath(root)) for n in claude_mcp.LEGACY_NAMES}:
            own = "ours"
    env = dict(os.environ)
    return [own, claude_mcp.words(res, env, own), claude_mcp.digest(d), cmd if own in ("ok", "ours") else "",
            "link" if claude_mcp.linked(res, link) else ""]


def render_rules(path):
    t = open(os.path.join(LAB, "config", "codex", "metacomputer.rules.in"), encoding="utf-8").read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(t.replace("@COMMANDS@", json.dumps(["metacomputer", CLI])).replace("@CHECKOUT@", LAB))


def codex_checks(prog):
    """(version, [(name, ok, detail)]): the Codex behaviour metacomputer relies on, in a throwaway CODEX_HOME (every
    codex run here uses it: even `codex --version` writes into the CODEX_HOME it's given)."""
    out = []
    with tempfile.TemporaryDirectory(prefix="mcomp-codex-check-") as td:
        env = dict(os.environ, CODEX_HOME=os.path.join(td, "home"))
        os.makedirs(env["CODEX_HOME"])
        ver = version(prog, env)
        # 1. the rules checker reads metacomputer's rules and decides as they say
        rules = os.path.join(td, "metacomputer.rules")
        render_rules(rules)
        want = {("metacomputer", "status"): "allow", ("metacomputer", "reset"): "prompt",
                ("metacomputer", "secrets", "unlock"): "forbidden", ("metacomputer", "each", "x", "--", "reset"): "prompt"}
        got = {}
        for cmd in want:
            rc, o, e = run([prog, "execpolicy", "check", "-r", rules, *cmd], env=env)
            try:
                got[cmd] = json.loads(o).get("decision") if rc == 0 else f"exit {rc}: {(e or o).strip()[:120]}"
            except ValueError:
                got[cmd] = f"unreadable (exit {rc}): {(e or o).strip()[:120]}"
        bad = {" ".join(k): v for k, v in got.items() if v != want[k]}
        out.append(("rules", not bad, "metacomputer's rules decide as written" if not bad else f"decided otherwise: {bad}"))
        # 2. the sandbox marks the commands it runs (bin/metacomputer's exit 77 depends on it)
        clean = {k: v for k, v in env.items() if k != "CODEX_SANDBOX"}       # set by the sandbox, not passed down
        rc, o, e = run([prog, "sandbox", "-P", ":workspace", "-C", td, "--", "/usr/bin/env"], env=clean)
        marked = rc == 0 and any(l.startswith("CODEX_SANDBOX=") and len(l) > len("CODEX_SANDBOX=") for l in o.splitlines())
        out.append(("sandbox marker", marked, "CODEX_SANDBOX is set inside its sandbox" if marked
                    else f"no CODEX_SANDBOX inside `codex sandbox` (exit {rc}): {(e or o).strip()[:160]}"))
        # 3. codex mcp: add, the JSON install.sh reads, and the two settings it adds
        rc, o, e = run([prog, "mcp", "add", "metacomputer", "--", CLI, "mcp-lazy"], env=env)
        cfg = os.path.join(env["CODEX_HOME"], "config.toml")
        ok_add = rc == 0 and os.path.exists(cfg)
        detail = f"`codex mcp add` failed (exit {rc}): {(e or o).strip()[:160]}"
        if ok_add:
            with open(cfg, "a") as f:
                f.write("tool_timeout_sec = 600\ndefault_tools_approval_mode = \"approve\"\n")
            rc, o, e = run([prog, "mcp", "get", "metacomputer", "--json"], env=env)
            try:
                d = json.loads(o) if rc == 0 else None
                t = (d or {}).get("transport") or {}
                ok_add = (d is not None and t.get("type") == "stdio" and t.get("command") == CLI
                          and d.get("tool_timeout_sec") == 600)
                detail = ("`codex mcp get --json` reads the server and its timeout" if ok_add
                          else f"`codex mcp get --json` failed or changed shape (exit {rc}): {(e or o).strip()[:200]}")
            except ValueError:
                ok_add = False
                detail = f"`codex mcp get --json` isn't JSON (exit {rc}): {(e or o).strip()[:160]}"
        out.append(("mcp config", ok_add, detail))
        # 4. Codex knows both settings install.sh adds (tool_timeout_sec, default_tools_approval_mode = "approve"):
        # its strict config loading names an unknown key or value. Only `exec` loads config strictly, so this runs it
        # with a model provider that doesn't exist: it stops right after loading the config, before any session.
        if ok_add:
            rc, o, e = run([prog, "--strict-config", "-c", 'model_provider="mcomp-check-no-provider"', "exec",
                            "--skip-git-repo-check", "-C", td, "x"], env=env)
            said = (e + o).strip()
            ok_keys = "Error loading config" not in said and "provider `mcomp-check-no-provider` not found" in said
            out.append(("mcp settings", ok_keys, "Codex accepts the timeout and approval settings" if ok_keys
                        else f"Codex's strict config check didn't accept them: {said[:240]}"))
    out.append(session_check())
    return ver, out


MODEL_TOOL_CALLS = ("custom_tool_call", "function_call", "local_shell_call")   # not web_search_call and the like
TOOL_ITEMS = ("McpToolCall", "CommandExecution")                              # what learn reads


def session_evidence(path, max_events=50_000):
    """(the model called tools, the file has the tool items learn reads) for one session file, read with
    transcripts' bounded reader (regular files only, no links or FIFOs, size and line limits; lines that aren't
    JSON objects skipped). Nothing of it is kept."""
    import transcripts
    called = items = False
    try:
        for n, ev in enumerate(transcripts.read_events(path)):
            if n >= max_events or (called and items):
                break
            p = ev.get("payload")
            if not isinstance(p, dict):
                continue
            if ev.get("type") == "response_item" and p.get("type") in MODEL_TOOL_CALLS:
                called = True
            elif ev.get("type") == "event_msg" and p.get("type") == "item_completed":
                it = p.get("item")
                if isinstance(it, dict) and it.get("type") in TOOL_ITEMS:
                    items = True
    except (OSError, ValueError, EOFError):
        pass
    return called, items


def session_check(limit=20):
    """5. Codex's session files still read for `metacomputer learn`: the newest is a Codex session; in the newest one
    with the tool items learn reads (MCP calls, commands), learn finds calls; and if several recent sessions show the
    model calling tools but none has those items, Codex has changed how it records them. Regular files only (no links,
    FIFOs or devices), each read with transcripts' limits. Nothing of the files is kept or printed."""
    import stat as st_
    import transcripts
    files = []
    for d, _dirs, names in os.walk(os.path.join(codex_home(), "sessions")):
        for f in names:
            if f.startswith("rollout-") and f.endswith(".jsonl"):
                p = os.path.join(d, f)
                try:
                    s = os.lstat(p)
                except OSError:
                    continue
                if st_.S_ISREG(s.st_mode):
                    files.append((s.st_mtime, p))
    if not files:
        return ("session files", True, "no Codex session yet (nothing to check)")
    files = [p for _, p in sorted(files, reverse=True)[:limit]]
    try:
        tr = transcripts.parse(files[0], slim=True)
        ok = tr.kind == "codex" and bool(tr.session_id)
    except (OSError, ValueError, EOFError):
        ok = False
    if not ok:
        return ("session files", False, "the newest one doesn't read as a Codex session: metacomputer learn may miss "
                                        "Codex sessions")
    calling = 0                  # newest first: sessions with tool calls but none of the items, before any that has them
    for p in files:
        called, items = session_evidence(p)
        if called and not items:
            calling += 1
            if calling >= 3:     # an older session in the old format must not hide newer ones in a new format
                break
            continue
        if items:
            try:
                n = len(transcripts.parse(p, slim=True).calls)
            except (OSError, ValueError, EOFError):
                n = 0
            return ("session files", n > 0, "they read as Codex sessions, tool calls included (for metacomputer learn)"
                    if n else "a session with MCP calls or commands yields none: Codex changed its session format, "
                              "and metacomputer learn would miss what happened")
    if calling >= 3:
        return ("session files", False, f"the {calling} newest sessions with tool calls record none of the MCP calls or "
                                        "commands learn reads: Codex changed its session format")
    return ("session files", True, f"the newest reads as a Codex session; none of the newest {len(files)} has MCP calls "
                                   "or commands, so their format couldn't be checked further")


def main(argv):
    if argv[:1] == ["codex-entry"]:            # for install.sh (see codex_entry)
        if len(argv) != 5 or argv[1] != "--root" or argv[3] != "--link":
            print("usage: agents.py codex-entry --root CHECKOUT --link ~/.local/bin/metacomputer", file=sys.stderr)
            return 2
        prog, why = trusted("codex")
        fields = (codex_entry(prog, argv[2], argv[4]) if prog else
                  ["error", why or "Codex's codex command isn't on PATH", "", "", ""])
        print("\x1f".join(str(f).replace("\x1f", " ").replace("\n", " ") for f in fields))
        return 0
    ap = argparse.ArgumentParser(prog="metacomputer agents", description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="?", default="status", choices=("status", "check"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="check: print only what failed")
    a = ap.parse_args(argv)
    if a.what == "check":
        prog, why = trusted("codex")
        if why:
            print(json.dumps({"codex": None, "refused": why}) if a.json else f"Not checked: {why}")
            return 1
        if not prog:
            print(json.dumps({"codex": None}) if a.json else "Codex isn't installed: nothing to check.")
            return 0
        ver, res = codex_checks(prog)
        failed = [r for r in res if not r[1]]
        if a.json:
            print(json.dumps({"codex": ver, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in res]},
                             indent=2))
        else:
            for n, ok, d in res:
                if ok and a.quiet:
                    continue
                print(f"  {'✓' if ok else '!'} Codex {n}: {d}")
            if failed and not a.quiet:
                print(f"{ver or 'This Codex'} changed something metacomputer relies on: see docs/codex.md "
                      "(\"If something doesn't work\"), and please report it.")
        return 1 if failed else 0
    import agent_setup
    st = {"claude_code": claude_status(), "codex": codex_status()}
    more = agent_setup.AGENTS
    for name, ag in more.items():
        st[name] = agent_setup.status(ag, LAB, os.path.expanduser("~/.local/bin/metacomputer"), dict(os.environ))
    if a.json:
        print(json.dumps(st, indent=2))
        return 0
    for label, s in (("Claude Code", st["claude_code"]), ("Codex", st["codex"])):
        if s.get("untrusted"):
            print(f"{label:<12} {s['untrusted']}")
            continue
        if not s["installed"]:
            how = "isn't installed" + ("; you chose to leave it alone" if s.get("choice") == "off" else
                                       "; once it is: ./install.sh --only-agents (or metacomputer update)")
        elif s.get("choice") == "off":
            flag = "claude" if label == "Claude Code" else "codex"
            how = f"{s['version'] or 'installed'}: left alone (--no-{flag}; --{flag} sets it up)"
        elif s.get("set_up"):
            if label == "Claude Code":
                way = claude_way(s)
            else:
                way = "MCP server" + (", skills" if all(s.get("skills", {}).values()) else ", skills missing") + \
                      (", rules" if s.get("rules") and s.get("rules") != "someone else's" else ", rules missing")
            how = f"{s['version'] or 'installed'}: set up ({way})"
        elif label == "Claude Code" and (s.get("plugin") == "on" or s.get("skill_link") or s.get("user_mcp_server")
                                         or s.get("mcp_error")):
            how = f"{s['version'] or 'installed'}: {claude_missing(s)}"
        else:
            how = f"{s['version'] or 'installed'}: not set up; ./install.sh --only-agents sets it up"
        print(f"{label:<12} {how}")
    for name, ag in more.items():
        print(f"{ag.label:<12} {more_line(name, ag, st[name])}")
    return 0


def more_line(name, ag, s):
    """One line for OpenClaw, Hermes Agent or Gemini CLI (lib/agent_setup.py status)."""
    if s.get("untrusted"):
        return s["untrusted"]
    if not s["installed"]:
        return "isn't installed" + ("; you chose to leave it alone" if s.get("choice") == "off" else
                                    "; once it is: ./install.sh --only-agents (or metacomputer update)")
    ver = s.get("version") or "installed"
    if s.get("choice") == "off":
        return f"{ver}: left alone (--no-{name}; --{name} sets it up)"
    skills = all(s.get("skills", {}).values())
    if s.get("set_up"):
        return f"{ver}: set up (MCP server, {'skills' if skills else 'skills missing'}; tools {ag.tools})"
    if s.get("mcp_error"):
        return f"{ver}: its MCP server metacomputer couldn't be read ({s['mcp_error']})"
    if s.get("mcp_server"):
        return (f"{ver}: its MCP server metacomputer runs {s['mcp_server']}, not ~/.local/bin/metacomputer mcp-lazy (no "
                f"metacomputer tools); {os.path.join(LAB, 'install.sh')} --only-{name} says how to replace it")
    return f"{ver}: not set up; ./install.sh --only-{name} sets it up"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
