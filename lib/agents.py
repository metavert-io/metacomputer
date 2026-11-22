#!/usr/bin/env python3
"""The coding agents metacomputer is set up for on this Mac: Claude Code and Codex (docs/codex.md).

  metacomputer agents [--json]          each agent: installed (and its version), how it's set up, your choice
                                        (install.sh --no-claude / --no-codex, remembered for later runs)
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


def run(argv, env=None, timeout=TIMEOUT):
    """(exit status, stdout, stderr); 127 when the program can't be run. PATH is SAFE_PATH."""
    env = dict(env if env is not None else os.environ, PATH=SAFE_PATH)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def choices():
    try:
        d = json.load(open(AGENTS_FILE))
        return {k: d.get(k) for k in ("claude", "codex") if d.get(k) in ("on", "off")}
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
    if rc == 0:
        try:
            for p in json.loads(out):
                if p.get("id") == "metacomputer@metacomputer" and p.get("scope") == "user":
                    plugin = "on" if p.get("enabled") else "off"
        except (ValueError, AttributeError, TypeError):
            pass
    st["plugin"] = plugin
    rc, out, _ = run([prog, "mcp", "get", "metacomputer"])
    st["user_mcp_server"] = rc == 0 and "Command:" in out
    st["skill_link"] = link_target(os.path.expanduser("~/.claude/skills/metacomputer"))
    st["set_up"] = plugin == "on" or st["user_mcp_server"]
    return st


def codex_status():
    prog, why = trusted("codex")
    st = {"installed": bool(prog or why), "version": version(prog) if prog else None, "choice": choices().get("codex")}
    if why:
        st["untrusted"] = why
    if not prog:
        return st
    rc, out, err = run([prog, "mcp", "get", "metacomputer", "--json"])
    srv = None
    if rc == 0:
        try:
            t = json.loads(out).get("transport") or {}
            srv = {"type": t.get("type"), "command": t.get("command")}
        except ValueError:
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
    st["set_up"] = bool(srv and srv.get("command"))
    return st


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
    calling = 0
    for p in files:
        called, items = session_evidence(p)
        calling += called
        if items:
            try:
                n = len(transcripts.parse(p, slim=True).calls)
            except (OSError, ValueError, EOFError):
                n = 0
            return ("session files", n > 0, "they read as Codex sessions, tool calls included (for metacomputer learn)"
                    if n else "a session with MCP calls or commands yields none: Codex changed its session format, "
                              "and metacomputer learn would miss what happened")
    if calling >= 3:
        return ("session files", False, f"{calling} recent sessions show the model calling tools, but none records the "
                                        "MCP calls or commands learn reads: Codex changed its session format")
    return ("session files", True, f"the newest reads as a Codex session; none of the newest {len(files)} has MCP calls "
                                   "or commands, so their format couldn't be checked further")


def main(argv):
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
    st = {"claude_code": claude_status(), "codex": codex_status()}
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
                way = "the plugin" if s.get("plugin") == "on" else "a user-scope MCP server and skill link"
            else:
                way = "MCP server" + (", skills" if all(s.get("skills", {}).values()) else ", skills missing") + \
                      (", rules" if s.get("rules") and s.get("rules") != "someone else's" else ", rules missing")
            how = f"{s['version'] or 'installed'}: set up ({way})"
        else:
            how = f"{s['version'] or 'installed'}: not set up; ./install.sh --only-agents sets it up"
        print(f"{label:<12} {how}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
