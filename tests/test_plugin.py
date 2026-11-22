#!/usr/bin/env python3
"""The Claude Code plugin: its manifests and layout, the plugin's launcher (plugin/bin/metacomputer) handing
over to the installed checkout, install.sh's plugin mode, and the two tool-name prefixes. No VM;
install.sh runs against a stub `claude` that records its arguments, in a temporary HOME, so this
Mac's own Claude Code setup is never touched.

    python3 tests/test_plugin.py                 # stub claude only
    python3 tests/test_plugin.py --real-claude   # also the real `claude` CLI, with a temporary
                                                 # HOME and CLAUDE_CONFIG_DIR (no network)

- .claude-plugin/marketplace.json lists the plugin at ./plugin. plugin/ holds regular files only (Anthropic's
  plugin directory takes no links): copies of skills/metacomputer, .claude/skills/cua-driver, get.sh, LICENSE and
  THIRD_PARTY_NOTICES.md, the same as the repository's (scripts/sync-plugin.py --check), laid out as in the repo
  so the driver pack's links to ../../../skills/metacomputer/DESKTOP.md resolve in both; and its own README,
  icon and launcher. The driver pack carries its own LICENSE, Cua's MIT notice, the same text as in the notices.
- The directory's file rules: a README of 40+ words outside code blocks, a license, an icon (a PNG in the plugin)
  and https listing URLs in plugin.json, no file over 256 KiB but images, at most 512 files, only text files and
  PNG/SVG images, no system files, and no path in plugin.json or the launcher that climbs out with "..".
- `claude plugin validate` (when claude is installed): no errors; the only warning is the missing version
  (on purpose: users track commits).
- plugin/bin/metacomputer (the MCP server command and the `metacomputer` command on Claude Code's PATH)
  execs $MCOMP_HOME/bin/metacomputer, else ~/.local/bin/metacomputer, never one inside Claude Code's plugin folder
  ($CLAUDE_CODE_PLUGIN_CACHE_DIR, else $CLAUDE_CONFIG_DIR/plugins or ~/.claude/plugins); with neither it prints how
  to set up and exits 1 (loaded in place from a checkout: run ./install.sh there).
- `metacomputer setup` in a plugin copy with no checkout runs only /bin/bash with the get.sh at
  that copy's root (a regular file in the plugin folder, never a link, never one named by a
  variable), always with --confirm, passing only --dir/--no-chrome/--no-build/--yes/--dry-run/--help;
  with a checkout it hands over like any command. The real get.sh with no terminal explains and exits 3.
- install.sh: plugin mode by default (marketplace + plugin at user scope; earlier installs'
  skill link and user-scope MCP server removed only when they are this checkout's), idempotent;
  --no-plugin keeps the skill link + server and removes this checkout's plugin; no `claude
  plugin` falls back to the skill link + server; the project's own GitHub marketplace is kept, one
  named metacomputer from any other source is refused (its plugin isn't installed); another
  checkout's marketplace is replaced; a plugin turned off is left so; --uninstall removes both.
- The skill and the driver pack name both prefixes; bench and the skill-test analyzer count both.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(LAB, "bin", "metacomputer")
PLUGIN_CLI = os.path.join(LAB, "plugin", "bin", "metacomputer")
FAILS = []
REAL = "--real-claude" in sys.argv
PLUGIN_PREFIX = "mcp__plugin_metacomputer_metacomputer__"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def base_env(home, **extra):
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "MCOMP_LOCK_DIR": os.path.join(home, ".locks")}   # install.sh's lock: never the real per-user folder
    env.update(extra)
    return env


# --- manifests and layout ------------------------------------------------------------------
def test_layout():
    mkt = json.load(open(os.path.join(LAB, ".claude-plugin", "marketplace.json")))
    plug = json.load(open(os.path.join(LAB, "plugin", ".claude-plugin", "plugin.json")))
    entry = (mkt.get("plugins") or [{}])[0]
    check("marketplace: name metacomputer, one plugin metacomputer at ./plugin",
          mkt.get("name") == "metacomputer" and len(mkt["plugins"]) == 1
          and entry.get("name") == "metacomputer" and entry.get("source") == "./plugin", mkt)
    check("plugin.json: name metacomputer, no version (users track commits), license MIT",
          plug.get("name") == "metacomputer" and "version" not in plug and "version" not in entry
          and plug.get("license") == "MIT")
    srv = (plug.get("mcpServers") or {}).get("metacomputer") or {}
    check("plugin.json: MCP server metacomputer runs ${CLAUDE_PLUGIN_ROOT}/bin/metacomputer mcp-lazy",
          srv.get("command") == "${CLAUDE_PLUGIN_ROOT}/bin/metacomputer" and srv.get("args") == ["mcp-lazy"], srv)
    check("plugin.json: skills from skills/ and ./.claude/skills", plug.get("skills") == ["./.claude/skills"])
    check("no .mcp.json at the repo root (it would become a project server for the repo)",
          not os.path.exists(os.path.join(LAB, ".mcp.json")))
    links = []
    for d, dirs, files in os.walk(os.path.join(LAB, "plugin")):
        links += [os.path.relpath(os.path.join(d, n), LAB) for n in dirs + files if os.path.islink(os.path.join(d, n))]
    check("plugin/ holds no links (the plugin directory takes regular files only)", not links, links)
    p = subprocess.run([sys.executable, os.path.join(LAB, "scripts", "sync-plugin.py"), "--check"],
                       capture_output=True, text=True, timeout=60)
    check("plugin/'s copies are the repository's files (scripts/sync-plugin.py --check)", p.returncode == 0, p.stdout + p.stderr)
    launcher = open(PLUGIN_CLI).read()
    check("plugin/bin/metacomputer is its own small launcher (regular, executable, not bin/metacomputer)",
          os.path.isfile(PLUGIN_CLI) and os.access(PLUGIN_CLI, os.X_OK) and len(launcher) < 8192
          and "mc_in_plugins" in launcher and launcher != open(CLI).read())
    check("bin/metacomputer no longer carries the plugin's hand-over", "mc_in_plugins" not in open(CLI).read())
    test_directory_rules(plug)
    # The driver pack's links out of its folder resolve from the repo's copy and the plugin's.
    bad = []
    for base in (".claude/skills/cua-driver", "plugin/.claude/skills/cua-driver"):
        text = open(os.path.join(LAB, base, "SKILL.md")).read()
        for rel in sorted(set(x.split(")")[0] for x in text.split("](")[1:] if x.startswith("../"))):
            if not os.path.isfile(os.path.normpath(os.path.join(LAB, base, rel.split("#")[0]))):
                bad.append(f"{base}: {rel}")
    check("the driver pack's ../ links resolve (lexically) from both copies", not bad, bad)
    # License notices travel with the plugin copy and with the driver pack (Cua's MIT notice).
    notices = open(os.path.join(LAB, "THIRD_PARTY_NOTICES.md")).read()
    cua = notices.split("```text\n", 1)[-1].split("```", 1)[0] if "```text\n" in notices else ""
    pack_license = os.path.join(LAB, ".claude", "skills", "cua-driver", "LICENSE")
    check("THIRD_PARTY_NOTICES.md holds Cua's MIT notice",
          cua.startswith("MIT License") and "Copyright (c) 2025 Cua AI, Inc." in cua and "THE SOFTWARE IS PROVIDED" in cua)
    check("the driver pack carries LICENSE, the same Cua notice",
          os.path.isfile(pack_license) and open(pack_license).read() == cua)
    check("plugin/LICENSE is metacomputer's MIT license",
          open(os.path.join(LAB, "plugin", "LICENSE")).read().startswith("MIT License"))
    dropped = [f"{n}: {d}" for n in sorted(os.listdir(os.path.join(LAB, ".claude", "skills", "cua-driver")))
               for d in ("MACOS.md", "WINDOWS.md", "EMBEDDING.md", "cua.ai/driver/install")
               if d in open(os.path.join(LAB, ".claude", "skills", "cua-driver", n)).read()]
    check("the driver pack names no guide it doesn't include, and no host installer", not dropped, dropped)


def test_directory_rules(plug):
    """What Anthropic's plugin directory checks in the plugin folder (claude.com/docs/plugins/pre-submission-checklist)."""
    root = os.path.join(LAB, "plugin")
    readme = open(os.path.join(root, "README.md")).read()
    prose = readme.split("```")[::2]
    words = sum(len(part.split()) for part in prose)
    check("plugin/README.md: 40+ words outside code blocks", words >= 40, words)
    check("plugin/README.md says what it sends and links the privacy policy",
          "## What it sends and where" in readme and plug.get("privacyPolicyUrl", "") in readme)
    check("a license: plugin/LICENSE and license MIT", os.path.isfile(os.path.join(root, "LICENSE")) and plug.get("license") == "MIT")
    icon = plug.get("icon") or ""
    icon_path = os.path.normpath(os.path.join(root, icon))
    check("plugin.json icon: a PNG inside the plugin", icon.startswith("./") and icon_path.startswith(root + os.sep)
          and os.path.isfile(icon_path) and open(icon_path, "rb").read(8) == b"\x89PNG\r\n\x1a\n", icon)
    urls = {k: plug.get(k) for k in ("documentationUrl", "supportUrl", "privacyPolicyUrl")}
    check("plugin.json listing URLs: documentation, support and privacy policy, all https",
          all(isinstance(v, str) and v.startswith("https://") for v in urls.values()), urls)
    privacy = os.path.join(LAB, "PRIVACY.md")
    check("the privacy policy it names is Metavert's, and PRIVACY.md (what it keeps and sends) links to it",
          urls["privacyPolicyUrl"] == "https://www.metavert.io/privacy-policy"
          and os.path.isfile(privacy)
          and urls["privacyPolicyUrl"] in open(privacy, encoding="utf-8").read())
    files, big, odd, system = [], [], [], []
    for d, dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, LAB)
            files.append(rel)
            if n in (".DS_Store", "Thumbs.db", "desktop.ini") or "__MACOSX" in p:
                system.append(rel)
            data = open(p, "rb").read()
            image = data[:8] == b"\x89PNG\r\n\x1a\n" or n.endswith(".svg")
            if not image and len(data) >= 256 * 1024:
                big.append(rel)
            if not image:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    odd.append(rel)
    check("plugin/: at most 512 files", len(files) <= 512, len(files))
    check("plugin/: no file over 256 KiB but images", not big, big)
    check("plugin/: only text files and PNG/SVG images", not odd, odd)
    check("plugin/: no system files", not system, system)
    climbs = [k for k, v in plug.items() if ".." in json.dumps(v)]
    check("plugin.json and the launcher name no path with '..'", not climbs and ".." not in open(PLUGIN_CLI).read(), climbs)


def test_validate():
    claude = shutil.which("claude")
    if not claude:
        print("SKIP claude plugin validate (claude isn't installed)")
        return
    allowed = ("No version specified",)
    for target in (LAB, os.path.join(LAB, "plugin")):
        p = subprocess.run([claude, "plugin", "validate", target, "--json"], capture_output=True, text=True, timeout=120)
        try:
            rep = json.loads(p.stdout)
        except ValueError:
            check(f"validate {os.path.relpath(target, LAB) or '.'}: JSON report", False, p.stdout[-300:] + p.stderr[-300:])
            continue
        parts = [rep.get("manifest") or {}] + (rep.get("contents") or [])
        errors = [e for c in parts for e in c.get("errors") or []]
        warns = [w for c in parts for w in c.get("warnings") or []]
        other = [w for w in warns if not any(a in w.get("message", "") for a in allowed)]
        name = os.path.relpath(target, LAB)
        check(f"validate {name}: passes (exit 0) with no errors", p.returncode == 0 and rep.get("success") and not errors, errors)
        check(f"validate {name}: only the known warning (no version)", not other, other)


# --- the plugin copy hands over ---------------------------------------------------------------
def run(argv, env, **kw):
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, **kw)


def test_handoff():
    t = tempfile.mkdtemp(prefix="mcomp-plugin-")
    try:
        home = os.path.join(t, "home")
        cached = os.path.join(home, ".claude", "plugins", "cache", "metacomputer", "metacomputer", "abc", "bin", "metacomputer")
        os.makedirs(os.path.dirname(cached))
        shutil.copy2(PLUGIN_CLI, cached)
        fake = os.path.join(t, "checkout", "bin", "metacomputer")
        write(fake, '#!/bin/bash\necho "CHECKOUT $*"\n', 0o755)
        other = os.path.join(t, "other", "bin", "metacomputer")
        write(other, '#!/bin/bash\necho "OTHER $*"\n', 0o755)
        env = base_env(home)
        p = run([cached, "status"], env)
        check("plugin copy, nothing set up: exit 1 and how to set up (on stderr): its own setup, or get.sh in Terminal",
              p.returncode == 1 and f"{cached} setup" in p.stderr and "get.sh | bash" in p.stderr and not p.stdout, p.stderr)
        local = os.path.join(home, ".local", "bin", "metacomputer")
        os.makedirs(os.path.dirname(local))
        os.symlink(fake, local)
        p = run([cached, "call", "list_windows", "{}"], env)
        check("plugin copy runs ~/.local/bin/metacomputer with the same arguments",
              p.returncode == 0 and p.stdout.strip() == "CHECKOUT call list_windows {}", p.stdout + p.stderr)
        p = run([cached], env)
        check("plugin copy with no arguments hands over too", p.returncode == 0 and p.stdout.strip() == "CHECKOUT", p.stdout + p.stderr)
        p = run([cached, "who"], base_env(home, MCOMP_HOME=os.path.dirname(os.path.dirname(other))))
        check("MCOMP_HOME wins over ~/.local/bin", p.stdout.strip() == "OTHER who", p.stdout + p.stderr)
        p = run([cached, "who"], base_env(home, MCOMP_HOME=os.path.join(t, "missing")))
        check("a MCOMP_HOME without bin/metacomputer falls back to ~/.local/bin", p.stdout.strip() == "CHECKOUT who", p.stdout + p.stderr)
        os.remove(local)
        os.symlink(cached, local)
        p = run([cached, "who"], env)
        check("never hands over to a copy inside the plugin folder (no loop)", p.returncode == 1 and " setup" in p.stderr,
              p.stdout + p.stderr)
        os.remove(local)
        os.symlink(fake, local)
        cfg_copy = os.path.join(t, "cfg", "plugins", "cache", "x", "bin", "metacomputer")
        os.makedirs(os.path.dirname(cfg_copy))
        shutil.copy2(PLUGIN_CLI, cfg_copy)
        p = run([cfg_copy, "where"], base_env(home, CLAUDE_CONFIG_DIR=os.path.join(t, "cfg")))
        check("a copy under $CLAUDE_CONFIG_DIR/plugins hands over", p.stdout.strip() == "CHECKOUT where", p.stdout + p.stderr)
        alt_copy = os.path.join(t, "pc", "cache", "x", "bin", "metacomputer")
        os.makedirs(os.path.dirname(alt_copy))
        shutil.copy2(PLUGIN_CLI, alt_copy)
        p = run([alt_copy, "where"], base_env(home, CLAUDE_CODE_PLUGIN_CACHE_DIR=os.path.join(t, "pc")))
        check("a copy under $CLAUDE_CODE_PLUGIN_CACHE_DIR hands over", p.stdout.strip() == "CHECKOUT where", p.stdout + p.stderr)
        # The checkout's command runs as always (here: help).
        p = run([CLI, "help"], base_env(home, MCOMP_CONFIG=os.path.join(t, "none.env")))
        check("bin/metacomputer help runs this checkout", p.returncode == 0 and "Usage: metacomputer [--vm NAME] COMMAND" in p.stdout,
              p.stdout[-200:] + p.stderr[-200:])
        # The plugin loaded in place (install.sh's local marketplace) hands over the same way; with nothing set up,
        # it says to run ./install.sh in that checkout (no setup from a checkout's plugin folder).
        bare = os.path.join(t, "bare-home")
        os.makedirs(bare)
        p = run([PLUGIN_CLI, "setup"], base_env(bare))
        check("in place, nothing set up: exit 1, says to run ./install.sh there, runs no get.sh",
              p.returncode == 1 and "./install.sh" in p.stderr and "GET" not in p.stdout, p.stdout + p.stderr)
        p = run([PLUGIN_CLI, "who"], env)
        check("in place: hands over to ~/.local/bin/metacomputer", p.stdout.strip() == "CHECKOUT who", p.stdout + p.stderr)
        p = run([CLI, "help"], base_env(home, MCOMP_CONFIG=os.path.join(t, "none.env")))
        check("help doesn't show the hand-over block", "mc_" not in p.stdout and "plugin folder" not in p.stdout)
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --- `metacomputer setup` from the plugin copy, before any checkout exists ---------------------------
def test_setup():
    """The plugin copy's only way to run something other than a checkout's bin/metacomputer: `setup` runs
    /bin/bash with the get.sh at that copy's own root (a regular file inside the plugin folder), always with
    --confirm, and passes through only --dir DIR, --no-chrome, --no-build, --yes/-y, --dry-run, --help."""
    t = tempfile.mkdtemp(prefix="mcomp-plugin-")
    try:
        home = os.path.join(t, "home")
        root = os.path.join(home, ".claude", "plugins", "cache", "metacomputer", "metacomputer", "abc")
        cached = os.path.join(root, "bin", "metacomputer")
        os.makedirs(os.path.dirname(cached))
        shutil.copy2(PLUGIN_CLI, cached)
        get = os.path.join(root, "get.sh")
        argv_log = os.path.join(t, "get-argv")
        stub = f'#!/bin/bash\nprintf "%s\\n" "$@" > {argv_log}\necho "GET.SH RAN"\n'
        write(get, stub)    # not executable on purpose: it's run as `/bin/bash get.sh`
        env = base_env(home)

        def args():
            return open(argv_log).read().splitlines() if os.path.exists(argv_log) else None

        def fresh():
            if os.path.exists(argv_log):
                os.remove(argv_log)

        p = run([cached, "setup"], env)
        check("setup, no checkout: runs the plugin copy's own get.sh with --confirm",
              p.returncode == 0 and "GET.SH RAN" in p.stdout and args() == ["--confirm"], (p.stdout, p.stderr, args()))
        fresh()
        p = run([cached, "setup", "--yes", "--no-chrome", "--no-build", "--dir", "/tmp/x y", "--dry-run"], env)
        check("setup passes --yes --no-chrome --no-build --dir DIR --dry-run through, after --confirm",
              args() == ["--confirm", "--yes", "--no-chrome", "--no-build", "--dir", "/tmp/x y", "--dry-run"], (p.stderr, args()))
        for bad in (["--repo", "https://example.com/other.git"], ["--ref", "main"], ["--repo=x"], ["anything"]):
            fresh()
            p = run([cached, "setup", *bad], env)
            check(f"setup refuses {bad[0]} (exit 2) and runs nothing", p.returncode == 2 and args() is None, (p.returncode, p.stderr))
        fresh()
        p = run([cached, "setup", "--dir"], env)
        check("setup --dir without a folder: exit 2, nothing run", p.returncode == 2 and args() is None, p.stderr)
        # Settings and variables can't point it elsewhere.
        elsewhere = os.path.join(t, "elsewhere")
        write(os.path.join(elsewhere, "get.sh"), '#!/bin/bash\necho "WRONG GET.SH"\n', 0o755)
        fresh()
        p = run([cached, "setup"], base_env(home, MCOMP_HOME=elsewhere, PATH=f"{elsewhere}:/usr/bin:/bin"))
        check("setup ignores MCOMP_HOME and PATH: still the plugin copy's get.sh",
              "WRONG" not in p.stdout and args() == ["--confirm"], (p.stdout, p.stderr))
        # A get.sh that is a link (here: to a script outside the plugin folder) is refused.
        os.remove(get)
        os.symlink(os.path.join(elsewhere, "get.sh"), get)
        fresh()
        p = run([cached, "setup"], env)
        check("setup refuses a get.sh that's a link out of the plugin folder (exit 1, says what to do)",
              p.returncode == 1 and "WRONG" not in p.stdout and "get.sh | bash" in p.stderr, (p.stdout, p.stderr))
        os.remove(get)
        p = run([cached, "setup"], env)
        check("setup without a get.sh in the plugin copy: exit 1, says to update the plugin or use get.sh in Terminal",
              p.returncode == 1 and "claude plugin update" in p.stderr and "get.sh | bash" in p.stderr, p.stderr)
        # A get.sh in bin/ (beside the command, not at the copy's root) isn't used either.
        write(os.path.join(root, "bin", "get.sh"), stub)
        fresh()
        p = run([cached, "setup"], env)
        check("setup never runs a get.sh from bin/ or anywhere but the copy's root", p.returncode == 1 and args() is None, p.stderr)
        os.remove(os.path.join(root, "bin", "get.sh"))
        # With a checkout set up, setup is that checkout's (its install.sh), not the plugin's get.sh.
        write(get, stub)
        fake = os.path.join(t, "checkout", "bin", "metacomputer")
        write(fake, '#!/bin/bash\necho "CHECKOUT $*"\n', 0o755)
        os.makedirs(os.path.join(home, ".local", "bin"))
        os.symlink(fake, os.path.join(home, ".local", "bin", "metacomputer"))
        fresh()
        p = run([cached, "setup", "--yes"], env)
        check("with a checkout, setup hands over to it (no get.sh)", p.stdout.strip() == "CHECKOUT setup --yes" and args() is None,
              (p.stdout, p.stderr))
        os.remove(os.path.join(home, ".local", "bin", "metacomputer"))
        # The real get.sh through the plugin, with no terminal: explains, changes nothing, exit 3 (asks for --yes).
        os.remove(get)
        shutil.copy2(os.path.join(LAB, "get.sh"), get)
        p = subprocess.run([cached, "setup"], env=env, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, start_new_session=True)
        check("real get.sh via setup, no terminal: shows the plan, changes nothing, exit 3 with how to go ahead",
              p.returncode == 3 and "will:" in p.stdout and "--yes" in p.stdout and "Nothing was changed" in p.stdout
              and not os.path.exists(os.path.join(home, "metacomputer")), (p.returncode, p.stdout[-600:], p.stderr[-300:]))
        p = run([cached, "setup", "--help"], env)
        check("setup --help shows get.sh's options", p.returncode == 0 and "--no-build" in p.stdout and "--dry-run" in p.stdout,
              p.stdout[-300:])
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --- install.sh with a stub claude --------------------------------------------------------------
STUB = r'''#!/usr/bin/env python3
"""A stand-in for the claude CLI's plugin and mcp commands: state in $STUB_STATE, argv logged to $STUB_LOG."""
import json, os, sys
a = sys.argv[1:]
st_path = os.environ["STUB_STATE"]
st = json.load(open(st_path)) if os.path.exists(st_path) else {"mkts": {}, "plugins": {}, "mcp": {}}
open(os.environ["STUB_LOG"], "a").write(json.dumps(a) + "\n")
def save(): json.dump(st, open(st_path, "w"))
def out(x): print(json.dumps(x))
if a[:1] == ["plugin"] and os.environ.get("STUB_NO_PLUGIN"):
    print("error: unknown command 'plugin'", file=sys.stderr); sys.exit(1)
if a[:2] == ["plugin", "--help"]:
    print("Usage: claude plugin"); sys.exit(0)
if a[:3] == ["plugin", "marketplace", "list"]:
    out([dict(name=n, **m) for n, m in st["mkts"].items()]); sys.exit(0)
if a[:3] == ["plugin", "marketplace", "add"]:
    path = a[3]; name = json.load(open(os.path.join(path, ".claude-plugin", "marketplace.json")))["name"]
    if os.environ.get("STUB_FAIL_ADD"): sys.exit(1)
    if name in st["mkts"] and st["mkts"][name].get("path") != path:
        print("already exists with another source", file=sys.stderr); sys.exit(1)
    st["mkts"][name] = {"source": "directory", "path": path, "installLocation": path}; save(); sys.exit(0)
if a[:3] == ["plugin", "marketplace", "remove"]:
    if a[3] not in st["mkts"]: sys.exit(1)
    del st["mkts"][a[3]]
    st["plugins"] = {k: v for k, v in st["plugins"].items() if not k.endswith("@" + a[3])}; save(); sys.exit(0)
if a[:2] == ["plugin", "list"]:
    out([dict(id=k, **v) for k, v in st["plugins"].items()]); sys.exit(0)
if a[:2] == ["plugin", "install"]:
    pid = a[2]
    if pid.split("@")[1] not in st["mkts"]: sys.exit(1)
    st["plugins"][pid] = {"scope": "user", "enabled": True}; save(); sys.exit(0)
if a[:2] == ["plugin", "uninstall"]:
    if a[2] not in st["plugins"]: sys.exit(1)
    del st["plugins"][a[2]]; save(); sys.exit(0)
if a[:2] == ["mcp", "get"]:
    if a[2] not in st["mcp"]: print(f'No MCP server named "{a[2]}".'); sys.exit(1)
    print(f"{a[2]}:\n  Scope: User config\n  Type: stdio\n  Command: {st['mcp'][a[2]][0]}\n  Args: {' '.join(st['mcp'][a[2]][1:])}"); sys.exit(0)
if a[:2] == ["mcp", "add"]:
    i = a.index("-s") + 2 if "-s" in a else 2
    st["mcp"][a[i]] = a[i + 1:]; save(); sys.exit(0)
if a[:2] == ["mcp", "remove"]:
    if a[2] not in st["mcp"]: sys.exit(1)
    del st["mcp"][a[2]]; save(); sys.exit(0)
print("stub: unhandled " + " ".join(a), file=sys.stderr); sys.exit(2)
'''


class Box:
    """A temporary HOME with a stub claude on PATH, running this checkout's install.sh."""

    def __init__(self, real=False):
        self.t = tempfile.mkdtemp(prefix="mcomp-plugin-")
        self.home = os.path.join(self.t, "home")
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self.state = os.path.join(self.t, "state.json")
        self.log = os.path.join(self.t, "claude.log")
        self.real = real
        py = os.path.dirname(os.path.realpath(sys.executable))
        if real:
            os.symlink(shutil.which("claude"), os.path.join(self.bin, "claude"))
        else:
            write(os.path.join(self.bin, "claude"), STUB.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1), 0o755)
        self.env = base_env(self.home, PATH=f"{self.bin}:{py}:/usr/bin:/bin", STUB_STATE=self.state, STUB_LOG=self.log,
                            MCOMP_CONFIG=os.path.join(self.t, "none.env"))
        if real:
            self.env["CLAUDE_CONFIG_DIR"] = os.path.join(self.home, ".claude")

    def install(self, *args, **env):
        e = dict(self.env, **env)
        if os.path.exists(self.log):
            os.remove(self.log)
        p = subprocess.run([os.path.join(LAB, "install.sh"), *args], env=e, capture_output=True, text=True, timeout=300)
        calls = [json.loads(x) for x in open(self.log)] if os.path.exists(self.log) else []
        return p, calls

    def claude(self, *args):
        return subprocess.run(["claude", *args], env=self.env, capture_output=True, text=True, timeout=120)

    def mkts(self):
        return {m["name"]: m.get("path") or m.get("repo") for m in json.loads(self.claude("plugin", "marketplace", "list", "--json").stdout or "[]")}

    def plugins(self):
        return {p["id"]: p for p in json.loads(self.claude("plugin", "list", "--json").stdout or "[]") if p.get("scope") == "user"}

    def server(self):
        out = self.claude("mcp", "get", "metacomputer").stdout
        return next((x.split("Command:", 1)[1].strip() for x in out.splitlines() if "Command:" in x), None)

    def seed(self, mkts=None, plugins=None, mcp=None):
        if self.real:
            raise RuntimeError("seed is for the stub")
        json.dump({"mkts": mkts or {}, "plugins": plugins or {}, "mcp": mcp or {}}, open(self.state, "w"))

    def link(self, name):
        p = os.path.join(self.home, name)
        return os.readlink(p) if os.path.islink(p) else None

    def close(self):
        shutil.rmtree(self.t, ignore_errors=True)


def test_install(real=False):
    tag = "real claude: " if real else ""
    skill_link = ".claude/skills/metacomputer"
    b = Box(real)
    try:
        p, calls = b.install("--only-claude")
        check(tag + "fresh: install.sh --only-claude exits 0", p.returncode == 0, p.stdout[-400:] + p.stderr[-400:])
        check(tag + "fresh: this checkout is the marketplace metacomputer", b.mkts().get("metacomputer") == LAB, b.mkts())
        pl = b.plugins().get("metacomputer@metacomputer")
        check(tag + "fresh: plugin metacomputer@metacomputer installed at user scope and on", bool(pl and pl.get("enabled")), b.plugins())
        if real:
            check(tag + "fresh: it loads in place from plugin/", pl and pl.get("readFromFolder") == os.path.join(LAB, "plugin"), pl)
        check(tag + "fresh: the command links point at bin/metacomputer",
              b.link(".local/bin/metacomputer") == CLI and b.link(".local/bin/mcomp") == CLI)
        check(tag + "fresh: no skill link and no user-scope server", b.link(skill_link) is None and b.server() is None)
        check(tag + "fresh: it says what it did", "marketplace metacomputer: this checkout" in p.stdout
              and "plugin metacomputer@metacomputer installed" in p.stdout and PLUGIN_PREFIX in p.stdout, p.stdout)
        if not real:
            check("fresh: claude plugin marketplace add <checkout>, then install --scope user",
                  ["plugin", "marketplace", "add", LAB] in calls
                  and ["plugin", "install", "metacomputer@metacomputer", "--scope", "user"] in calls, calls)
        p, calls = b.install("--only-claude")
        check(tag + "again: exits 0 and says it's already done", p.returncode == 0 and "(already added)" in p.stdout
              and "already installed" in p.stdout, p.stdout + p.stderr)
        if not real:
            check("again: changes nothing (no add, install or remove)",
                  not [c for c in calls if c[:2] in (["plugin", "install"], ["plugin", "uninstall"])
                       or c[:3] in (["plugin", "marketplace", "add"], ["plugin", "marketplace", "remove"])
                       or c[:2] in (["mcp", "add"], ["mcp", "remove"])], calls)

        # An earlier install: the skill link and a user-scope server pointing here; the plugin not there yet.
        b.claude("plugin", "uninstall", "metacomputer@metacomputer", "--scope", "user")
        b.claude("plugin", "marketplace", "remove", "metacomputer")
        os.makedirs(os.path.join(b.home, ".claude", "skills"), exist_ok=True)
        os.symlink(os.path.join(LAB, "skills", "metacomputer"), os.path.join(b.home, skill_link))
        b.claude("mcp", "add", "-s", "user", "metacomputer", CLI, "mcp-lazy")
        p, _ = b.install("--only-claude")
        check(tag + "upgrade: plugin installed, this checkout's skill link and server removed",
              "metacomputer@metacomputer" in b.plugins() and b.link(skill_link) is None and b.server() is None,
              (b.plugins(), b.link(skill_link), b.server()))
        check(tag + "upgrade: it says so, with the new tool names", "removed the skill link" in p.stdout
              and "removed the user-scope MCP server metacomputer" in p.stdout, p.stdout)

        p, _ = b.install("--only-claude", "--no-plugin")
        check(tag + "--no-plugin: skill link + user-scope server, this checkout's plugin and marketplace removed",
              b.link(skill_link) == os.path.join(LAB, "skills", "metacomputer") and b.server() == CLI
              and not b.plugins() and "metacomputer" not in b.mkts(), (b.link(skill_link), b.server(), b.plugins(), b.mkts()))
        check(tag + "--no-plugin: names the mcp__metacomputer__ tools", "mcp__metacomputer__*" in p.stdout, p.stdout)
        p, _ = b.install("--only-claude", "--no-plugin")
        check(tag + "--no-plugin again: already registered", p.returncode == 0 and "already registered" in p.stdout, p.stdout)

        b.install("--only-claude")
        p, _ = b.install("--uninstall")
        check(tag + "--uninstall: plugin, marketplace, command links removed",
              p.returncode == 0 and not b.plugins() and "metacomputer" not in b.mkts()
              and b.link(".local/bin/metacomputer") is None and b.link(".local/bin/mcomp") is None,
              p.stdout + p.stderr)
        b.install("--only-claude", "--no-plugin")
        p, _ = b.install("--uninstall")
        check(tag + "--uninstall: skill link and server removed too", b.link(skill_link) is None and b.server() is None,
              p.stdout + p.stderr)
    finally:
        b.close()
    if real:
        return

    # Someone else's skill link and server (another checkout that exists) stay.
    b = Box()
    try:
        elsewhere = os.path.join(b.t, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "skills", "metacomputer"))
        write(os.path.join(elsewhere, "bin", "metacomputer"), "#!/bin/sh\n", 0o755)
        os.makedirs(os.path.join(b.home, ".claude", "skills"))
        os.symlink(os.path.join(elsewhere, "skills", "metacomputer"), os.path.join(b.home, skill_link))
        b.seed(mcp={"metacomputer": [os.path.join(elsewhere, "bin", "metacomputer"), "mcp-lazy"]})
        b.install("--only-claude")
        check("another checkout's skill link and user-scope server are left alone",
              b.link(skill_link) == os.path.join(elsewhere, "skills", "metacomputer")
              and b.server() == os.path.join(elsewhere, "bin", "metacomputer"))
        # A moved checkout's (dangling) ones are this install's to clean up.
        os.remove(os.path.join(b.home, skill_link))
        os.symlink(os.path.join(b.t, "gone", "skills", "metacomputer"), os.path.join(b.home, skill_link))
        b.seed(mkts={"metacomputer": {"source": "directory", "path": LAB}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}},
               mcp={"metacomputer": [os.path.join(b.t, "gone", "bin", "metacomputer"), "mcp-lazy"]})
        b.install("--only-claude")
        check("a moved checkout's dangling skill link and server are removed", b.link(skill_link) is None and b.server() is None)
    finally:
        b.close()

    # The GitHub marketplace is kept; its plugin runs this checkout through ~/.local/bin.
    b = Box()
    try:
        b.seed(mkts={"metacomputer": {"source": "github", "repo": "metavert-io/metacomputer"}})
        p, calls = b.install("--only-claude")
        check("GitHub marketplace: kept, plugin installed from it",
              ["plugin", "marketplace", "add", LAB] not in calls and b.mkts() == {"metacomputer": "metavert-io/metacomputer"}
              and "metacomputer@metacomputer" in b.plugins() and "github metavert-io/metacomputer" in p.stdout, p.stdout)
        p, calls = b.install("--uninstall")
        check("GitHub marketplace: --uninstall removes the plugin, keeps the marketplace and says how to remove it",
              not b.plugins() and "metacomputer" in b.mkts() and "claude plugin marketplace remove metacomputer" in p.stdout,
              p.stdout)
    finally:
        b.close()

    # Its git URL is the same repository: kept too.
    b = Box()
    try:
        b.seed(mkts={"metacomputer": {"source": "git", "url": "https://github.com/Metavert-io/metacomputer.git"}})
        p, calls = b.install("--only-claude")
        check("the canonical repository by git URL: kept, plugin installed from it",
              p.returncode == 0 and "metacomputer@metacomputer" in b.plugins()
              and ["plugin", "marketplace", "add", LAB] not in calls, p.stdout + p.stderr)
    finally:
        b.close()

    # A marketplace named metacomputer from anywhere else (another repository, a fork, a URL) is never used:
    # its plugin would run its own code on this Mac. Refused with a clear message; the skill link and a
    # user-scope server (this checkout) instead; that marketplace and its plugin are left alone.
    for src in ({"source": "github", "repo": "someone-else/metacomputer"},
                {"source": "git", "url": "https://github.com/metavert-io/metacomputer-fork.git"},
                {"source": "url", "url": "https://example.com/marketplace.json"}):
        b = Box()
        try:
            b.seed(mkts={"metacomputer": dict(src)})
            p, calls = b.install("--only-claude")
            out = p.stdout + p.stderr
            check(f"a marketplace named metacomputer from {src.get('repo') or src.get('url')}: plugin NOT installed, "
                  "says why and how to remove it, falls back to the skill link + server",
                  p.returncode == 0 and "metacomputer@metacomputer" not in b.plugins()
                  and not [c for c in calls if c[:2] == ["plugin", "install"]]
                  and "NOT installed" in out and "claude plugin marketplace remove metacomputer" in out
                  and b.link(".claude/skills/metacomputer") == os.path.join(LAB, "skills", "metacomputer")
                  and b.server() == CLI and "metacomputer" in b.mkts(), out)
            b.seed(mkts={"metacomputer": dict(src)}, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
            p, _ = b.install("--uninstall")
            check("…and --uninstall leaves that marketplace's plugin alone", "metacomputer@metacomputer" in b.plugins(),
                  p.stdout)
        finally:
            b.close()

    # Another checkout's marketplace (same name) is replaced by this one.
    b = Box()
    try:
        b.seed(mkts={"metacomputer": {"source": "directory", "path": os.path.join(b.t, "old")}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        p, _ = b.install("--only-claude")
        check("another checkout's marketplace is replaced by this one, plugin reinstalled",
              b.mkts() == {"metacomputer": LAB} and "metacomputer@metacomputer" in b.plugins()
              and "removed the marketplace metacomputer, which was" in p.stdout, p.stdout)
        b.seed(mkts={"metacomputer": {"source": "directory", "path": os.path.join(b.t, "old")}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        p, _ = b.install("--uninstall")
        check("--uninstall leaves another checkout's marketplace and plugin alone",
              b.mkts() == {"metacomputer": os.path.join(b.t, "old")} and "metacomputer@metacomputer" in b.plugins(), p.stdout)
    finally:
        b.close()

    # Turned off: left so, and the earlier skill link + server stay (else nothing would work).
    b = Box()
    try:
        os.makedirs(os.path.join(b.home, ".claude", "skills"))
        os.symlink(os.path.join(LAB, "skills", "metacomputer"), os.path.join(b.home, skill_link))
        b.seed(mkts={"metacomputer": {"source": "directory", "path": LAB}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": False}},
               mcp={"metacomputer": [CLI, "mcp-lazy"]})
        p, calls = b.install("--only-claude")
        check("plugin turned off: left off, skill link and server kept, says how to turn it on",
              p.returncode == 0 and not b.plugins()["metacomputer@metacomputer"]["enabled"]
              and b.link(skill_link) and b.server() == CLI and "claude plugin enable metacomputer@metacomputer" in p.stdout + p.stderr,
              p.stdout + p.stderr)
    finally:
        b.close()

    # No `claude plugin` (an older Claude Code), or the plugin fails: the skill link + server instead.
    for why, env in (("no 'claude plugin'", {"STUB_NO_PLUGIN": "1"}), ("marketplace add fails", {"STUB_FAIL_ADD": "1"})):
        b = Box()
        try:
            p, _ = b.install("--only-claude", **env)
            check(f"{why}: falls back to the skill link and a user-scope server, exit 0",
                  p.returncode == 0 and b.link(skill_link) == os.path.join(LAB, "skills", "metacomputer")
                  and b.server() == CLI, p.stdout + p.stderr)
        finally:
            b.close()

    # No claude at all: the command is linked, and it says what to run later.
    b = Box()
    try:
        os.remove(os.path.join(b.bin, "claude"))
        p, _ = b.install("--only-claude")
        check("no claude: command linked, skill linked, says to run ./install.sh --only-claude later",
              p.returncode == 0 and b.link(".local/bin/metacomputer") == CLI and "--only-claude" in p.stdout + p.stderr,
              p.stdout + p.stderr)
        p, _ = b.install("--only-claude", "--no-claude")
        check("--only-claude with --no-claude is refused", p.returncode == 2)
    finally:
        b.close()


# --- the two tool-name prefixes -----------------------------------------------------------------
def test_prefixes():
    skill = open(os.path.join(LAB, "skills", "metacomputer", "SKILL.md")).read()
    pack = open(os.path.join(LAB, ".claude", "skills", "cua-driver", "SKILL.md")).read()
    check("the skill names both prefixes and says to use the one the tool list shows",
          "`mcp__metacomputer__*`" in skill and f"`{PLUGIN_PREFIX}*`" in skill and "prefix your tool list shows" in skill)
    check("the driver pack names both prefixes (description and note)", pack.count(PLUGIN_PREFIX + "*") >= 2)
    sys.path.insert(0, os.path.join(LAB, "bench"))
    import metrics  # noqa: E402
    import bench  # noqa: E402
    check("bench metrics: both prefixes count as metacomputer tools",
          metrics.mcp_tool("mcp__metacomputer__click") == "click" and metrics.mcp_tool(PLUGIN_PREFIX + "zoom") == "zoom"
          and metrics.mcp_tool("mcp__other__click") is None)
    args, _ = bench.session_args({"prompt": "x"}, {"shell": "allow", "budget": 1, "model": "m", "effort": ""}, "cfg.json")
    allowed = args[args.index("--allowedTools") + 1:]
    check("bench sessions allow both servers' tools",
          "mcp__metacomputer" in allowed and "mcp__plugin_metacomputer_metacomputer" in allowed, allowed)
    t = tempfile.mkdtemp(prefix="mcomp-plugin-")
    try:
        run_file = os.path.join(t, "run.jsonl")
        evs = [{"type": "assistant", "message": {"content": [
                   {"type": "tool_use", "id": "1", "name": PLUGIN_PREFIX + "run_steps", "input": {"steps": [{"click": {}}]}},
                   {"type": "tool_use", "id": "2", "name": "mcp__metacomputer__get_window_state", "input": {"include_screenshot": False}}]}},
               {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1", "content": "ok"},
                                                         {"type": "tool_result", "tool_use_id": "2", "content": "ok"}]}},
               {"type": "result", "result": "done", "num_turns": 2}]
        with open(run_file, "w") as f:
            f.write("\n".join(json.dumps(e) for e in evs) + "\n")
        subprocess.run([sys.executable, os.path.join(LAB, "tests", "skill", "analyze.py"), run_file],
                       capture_output=True, text=True, timeout=60)
        summ = json.load(open(os.path.join(t, "run.summary.json")))
        st = summ.get("structural") or {}
        check("skill-test analyzer: counts the plugin's calls with the others",
              summ.get("mcp_tools") == ["run_steps", "get_window_state"] and st.get("run_steps") == 1
              and st.get("reads_tree_only") == 1 and st.get("steps_by_name") == 1, json.dumps(summ)[:400])
    finally:
        shutil.rmtree(t, ignore_errors=True)


test_layout()
test_validate()
test_handoff()
test_setup()
test_install()
if REAL:
    if shutil.which("claude"):
        test_install(real=True)
    else:
        print("SKIP --real-claude (claude isn't installed)")
test_prefixes()
print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
