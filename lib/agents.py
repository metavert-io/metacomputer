#!/usr/bin/env python3
"""The coding agents metacomputer is set up for on this Mac: Claude Code and Codex (docs/codex.md).

  metacomputer agents [--json]          each agent: installed (and its version), how it's set up, your choice
                                        (install.sh --no-claude / --no-codex, remembered for later runs)
  metacomputer agents check [--json]    what metacomputer relies on in this Mac's Codex, checked without a model and
                                        without touching your Codex settings (a throwaway CODEX_HOME): its rules
                                        checker, the sandbox's marker, `codex mcp` and the config keys install.sh
                                        sets, and the format of your newest session file (for `learn`)

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


def run(argv, env=None, timeout=TIMEOUT):
    """(exit status, stdout, stderr); 127 when the program can't be run."""
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
    prog = shutil.which("claude")
    st = {"installed": bool(prog), "version": version(prog) if prog else None, "choice": choices().get("claude")}
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
    prog = shutil.which("codex")
    st = {"installed": bool(prog), "version": version(prog) if prog else None, "choice": choices().get("codex")}
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
                got[cmd] = json.loads(o).get("decision")
            except ValueError:
                got[cmd] = f"unreadable (exit {rc}): {(e or o).strip()[:120]}"
        bad = {" ".join(k): v for k, v in got.items() if v != want[k]}
        out.append(("rules", not bad, "metacomputer's rules decide as written" if not bad else f"decided otherwise: {bad}"))
        # 2. the sandbox marks the commands it runs (bin/metacomputer's exit 77 depends on it)
        rc, o, e = run([prog, "sandbox", "-P", ":workspace", "-C", td, "--", "/usr/bin/env"], env=env)
        marked = rc == 0 and any(l.startswith("CODEX_SANDBOX=") for l in o.splitlines())
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
            rc, o, e = run([prog, "--strict-config", "mcp", "get", "metacomputer", "--json"], env=env)
            if rc != 0:      # an older Codex without --strict-config for mcp: the same without it
                rc, o, e = run([prog, "mcp", "get", "metacomputer", "--json"], env=env)
            try:
                d = json.loads(o)
                t = d.get("transport") or {}
                ok_add = (t.get("type") == "stdio" and t.get("command") == CLI and d.get("tool_timeout_sec") == 600)
                detail = ("`codex mcp get --json` reads the server and its settings" if ok_add
                          else f"`codex mcp get --json` changed shape: {json.dumps(d)[:200]}")
            except ValueError:
                ok_add = False
                detail = f"`codex mcp get --json` isn't JSON (exit {rc}): {(e or o).strip()[:160]}"
        out.append(("mcp config", ok_add, detail))
    # 4. the newest session file reads as a Codex session (metacomputer learn); nothing of it is kept
    sess = os.path.join(codex_home(), "sessions")
    newest = None
    for d, _dirs, files in os.walk(sess):
        for f in files:
            if f.startswith("rollout-") and f.endswith(".jsonl"):
                p = os.path.join(d, f)
                try:
                    if newest is None or os.path.getmtime(p) > os.path.getmtime(newest):
                        newest = p
                except OSError:
                    pass
    if newest is None:
        out.append(("session files", True, "no Codex session yet (nothing to check)"))
    else:
        import transcripts
        tr = transcripts.parse(newest, slim=True)
        ok = tr.kind == "codex" and bool(tr.session_id)
        out.append(("session files", ok, "the newest one reads as a Codex session (for metacomputer learn)" if ok
                    else "the newest one doesn't read as a Codex session: metacomputer learn may miss Codex sessions"))
    return ver, out


def main(argv):
    ap = argparse.ArgumentParser(prog="metacomputer agents", description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="?", default="status", choices=("status", "check"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="check: print only what failed")
    a = ap.parse_args(argv)
    if a.what == "check":
        prog = shutil.which("codex")
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
