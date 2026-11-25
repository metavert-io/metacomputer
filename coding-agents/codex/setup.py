"""Codex's part of `metacomputer agents` and of install.sh (its own steps are in setup.sh): what's set up in this Mac's
Codex (status, line), what metacomputer relies on in it (check: `metacomputer agents check`), and its MCP server
metacomputer read whole for install.sh (codex-entry):

    python3 coding-agents/codex/setup.py codex-entry --root CHECKOUT --link ~/.local/bin/metacomputer

Python 3.11 or later (tomllib); stdlib only."""
import json
import os
import sys
import tempfile
import tomllib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from agents import CLI, LAB, choices, link_target, run, trusted, version  # noqa: E402  (lib/agents.py's helpers)
from agentkit import mcp_entry  # noqa: E402  (the rule every agent's entry is read by)


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


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
            srv = {"type": t.get("type"),
                   "command": cmd if cmd == CLI else (mcp_entry.program_name(cmd) or "a program") if cmd else None}
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
    command, linked], by the rule install.sh uses for Claude Code's (lib/agentkit/mcp_entry.py):
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
    if kind == "stdio" and args == mcp_entry.MCP_ARGS:
        if cmd in (os.path.join(root, "bin", "metacomputer"), link):
            own = "ok"
        elif cmd in {os.path.join(r, "bin", n) for r in (root, os.path.realpath(root)) for n in mcp_entry.LEGACY_NAMES}:
            own = "ours"
    env = dict(os.environ)
    return [own, mcp_entry.words(res, env, own), mcp_entry.digest(d), cmd if own in ("ok", "ours") else "",
            "link" if mcp_entry.linked(res, link) else ""]


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


def line(s):
    """Its line in `metacomputer agents` (after its name)."""
    if not s["installed"]:
        return "isn't installed" + ("; you chose to leave it alone" if s.get("choice") == "off" else
                                    "; once it is: ./install.sh --only-agents (or metacomputer update)")
    if s.get("choice") == "off":
        return f"{s['version'] or 'installed'}: left alone (--no-codex; --codex sets it up)"
    if s.get("set_up"):
        way = "MCP server" + (", skills" if all(s.get("skills", {}).values()) else ", skills missing") + \
              (", rules" if s.get("rules") and s.get("rules") != "someone else's" else ", rules missing")
        return f"{s['version'] or 'installed'}: set up ({way})"
    return f"{s['version'] or 'installed'}: not set up; ./install.sh --only-agents sets it up"


def check(as_json, quiet):
    """`metacomputer agents check`: what metacomputer relies on in this Mac's Codex (codex_checks). Its exit status."""
    prog, why = trusted("codex")
    if why:
        print(json.dumps({"codex": None, "refused": why}) if as_json else f"Not checked: {why}")
        return 1
    if not prog:
        print(json.dumps({"codex": None}) if as_json else "Codex isn't installed: nothing to check.")
        return 0
    ver, res = codex_checks(prog)
    failed = [r for r in res if not r[1]]
    if as_json:
        print(json.dumps({"codex": ver, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in res]},
                         indent=2))
    else:
        for n, ok, d in res:
            if ok and quiet:
                continue
            print(f"  {'✓' if ok else '!'} Codex {n}: {d}")
        if failed and not quiet:
            print(f"{ver or 'This Codex'} changed something metacomputer relies on: see docs/codex.md "
                  "(\"If something doesn't work\"), and please report it.")
    return 1 if failed else 0


status = codex_status


def main(argv):
    import strictargs           # its command line, by metacomputer's rules, before anything is done
    p = strictargs.expect(argv, ["codex-entry --root CHECKOUT --link LINK"], prog="setup.py",
                          usage="usage: setup.py codex-entry --root CHECKOUT --link ~/.local/bin/metacomputer")
    prog, why = trusted("codex")
    fields = (codex_entry(prog, p.opts["--root"], p.opts["--link"]) if prog else
              ["error", why or "Codex's codex command isn't on PATH", "", "", ""])
    print("\x1f".join(str(f).replace("\x1f", " ").replace("\n", " ") for f in fields))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
