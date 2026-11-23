#!/usr/bin/env python3
"""The Claude Code plugin: its manifests and layout, the plugin's launcher (plugin/bin/metacomputer) handing
over to the installed checkout, install.sh's Claude Code step (the plugin and the user-scope MCP server), and the
two tool-name prefixes. No VM;
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
  PNG/SVG images, no system files, and no path in plugin.json or the launcher that climbs out with "..". The
  plugin declares no MCP server and no hooks (no mcpServers or hooks in plugin.json, no .mcp.json or
  hooks/hooks.json), and nothing in plugin.json runs a command or a script: the directory holds a version for a
  reviewer when a hook or MCP server runs a script it can't follow.
- `claude plugin validate` (when claude is installed): no errors; the only warning is the missing version
  (on purpose: users track commits).
- plugin/bin/metacomputer (the `metacomputer` command on Claude Code's PATH)
  execs $MCOMP_HOME/bin/metacomputer, else ~/.local/bin/metacomputer, never one inside Claude Code's plugin folder
  ($CLAUDE_CODE_PLUGIN_CACHE_DIR, else $CLAUDE_CONFIG_DIR/plugins or ~/.claude/plugins); with neither it prints how
  to set up and exits 1 (loaded in place from a checkout: run ./install.sh there).
- `metacomputer setup` in a plugin copy with no checkout runs only /bin/bash with the get.sh at
  that copy's root (a regular file in the plugin folder, never a link, never one named by a
  variable), always with --confirm, passing only --dir/--no-chrome/--no-build/--yes/--dry-run/--help;
  with a checkout it hands over like any command. The real get.sh with no terminal explains and exits 3.
- install.sh: by default the plugin (marketplace + plugin at user scope) and the user-scope MCP server
  metacomputer, ~/.local/bin/metacomputer mcp-lazy, read back as type, command and arguments (an earlier install's
  skill link removed only when it is this checkout's), idempotent. Only metacomputer's own registration (its exact
  entry from Claude Code's configuration: canonical command, exactly ["mcp-lazy"]) is repaired or replaced; another
  checkout's moves with --switch; any other registration is never removed or changed (the install names it without
  its arguments, variables or URL and says how to remove it yourself); a failed lookup changes nothing; once
  metacomputer's own was removed the add always runs, and a failed add or read-back says the server is missing and the
  command that adds it; a failed remove changes nothing; local and project servers are reported, never changed; any of
  these ends with exit status 1. --no-plugin: the skill link + the same server,
  this checkout's plugin removed; no `claude plugin` falls back to the skill link + server; the project's own
  GitHub marketplace is kept, one named metacomputer from any other source is refused (its plugin isn't
  installed); another checkout's marketplace is replaced; a plugin turned off is left so, and no server is added
  for it; --no-claude changes nothing of Claude Code's; --uninstall reads the user-scope server before it removes
  anything, then removes the plugin, the skill link and the server. An update from the layout where the plugin brought the server (plugin.json's mcpServers
  and no user-scope server, as v0.4.4 and earlier installed it) ends with the user-scope server alone; an older plugin copy from
  GitHub that still brings one is updated, only after the new server is read back (and, if that fails, or the
  plugin list can't be read, the install says so and fails). The plugin updated on its own over an older checkout:
  its launcher and the skill say to run `metacomputer update`.
- The skill and the driver pack name both prefixes; bench and the skill-test analyzer count both.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(LAB, "bin", "metacomputer")
PLUGIN_CLI = os.path.join(LAB, "plugin", "bin", "metacomputer")
FAILS = []
REAL = "--real-claude" in sys.argv
PLUGIN_PREFIX = "mcp__plugin_metacomputer_metacomputer__"   # the names an older copy of the plugin gave the tools
PREFIX = "mcp__metacomputer__"                              # Claude Code's user-scope server's, and Codex's


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
    check("plugin.json declares no MCP server and no hooks (install.sh registers the server)",
          "mcpServers" not in plug and "hooks" not in plug, sorted(plug))
    check("plugin/ has no .mcp.json and no hooks/hooks.json",
          not os.path.exists(os.path.join(LAB, "plugin", ".mcp.json"))
          and not os.path.exists(os.path.join(LAB, "plugin", "hooks")), os.listdir(os.path.join(LAB, "plugin")))

    def runs(v, path=""):   # every value in plugin.json that names something to run
        if isinstance(v, dict):
            return [x for k, w in v.items() for x in (([f"{path}.{k}"] if k in ("command", "args", "hooks",
                                                                                  "mcpServers", "lspServers") else [])
                                                      + runs(w, f"{path}.{k}"))]
        if isinstance(v, list):
            return [x for i, w in enumerate(v) for x in runs(w, f"{path}[{i}]")]
        return [path] if isinstance(v, str) and ("CLAUDE_PLUGIN_ROOT" in v or v.endswith(".sh") or "bin/" in v) else []
    check("nothing in plugin.json runs a command or a script", not runs(plug), runs(plug))
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
        check("bin/metacomputer help runs this checkout", p.returncode == 0 and "Usage: metacomputer [--vm NAME | --on PLACE] COMMAND" in p.stdout,
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
        p = run([cached, "setup", "--no-codex", "--no-openclaw", "--no-hermes", "--no-gemini"], env)
        check("...and each agent's --no-AGENT (get.sh passes them on to install.sh)",
              args() == ["--confirm", "--no-codex", "--no-openclaw", "--no-hermes", "--no-gemini"], (p.stderr, args()))
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
"""A stand-in for the claude CLI's plugin and mcp commands: state in $STUB_STATE, argv logged to $STUB_LOG.
MCP servers live in three scopes as Claude Code keeps them: user-scope ones in the user configuration
(.claude.json in CLAUDE_CONFIG_DIR or the home folder, where install.sh reads the exact entry), local and project ones
keyed by the folder the command runs in; `mcp get` shows the one that takes precedence there (local, project, user),
with the arguments joined by spaces, as the real one does (the configuration may start with a byte-order mark, as
Claude Code allows). Failures on demand: STUB_FAIL_MCP_GET (or STUB_FAIL_MCP_GET_FOR=NAME: only that server's),
STUB_GET_LIES (`mcp get` shows another command than the configuration has), STUB_MUTATE_ON_GET (another Claude Code adds
a variable to the entry while `mcp get` runs), STUB_FAIL_GET_AFTER_REMOVE=once|always (`mcp get` fails after a removal:
the first time, or every time), STUB_FAIL_MCP_ADD (add and add-json; or STUB_FAIL_ADD_CMD=PATH: only that
command), STUB_FAIL_MCP_REMOVE, STUB_FAIL_UPDATE, STUB_FAIL_PLUGIN_UNINSTALL, STUB_PLUGIN_LIST=exit|json|shape|records|
noscope|errors|noenabled|strenabled (STUB_PLUGIN_LIST_WHEN=after-update|after-uninstall: only once a plugin update, or uninstall, has run),
STUB_MKT_LIST=exit|json|shape (STUB_MKT_LIST_WHEN=after-remove: only once a marketplace removal has run)."""
import json, os, sys
a = sys.argv[1:]
st_path = os.environ["STUB_STATE"]
st = json.load(open(st_path)) if os.path.exists(st_path) else {}
st.setdefault("mkts", {}); st.setdefault("plugins", {})
st.setdefault("mcp", {}); [st["mcp"].setdefault(k, {}) for k in ("local", "project")]
cfg_path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.environ["HOME"], ".claude.json")
cfg = json.load(open(cfg_path, encoding="utf-8-sig")) if os.path.exists(cfg_path) else {}
st["mcp"]["user"] = cfg.setdefault("mcpServers", {})
open(os.environ["STUB_LOG"], "a").write(json.dumps(a) + "\n")
cwd = os.path.realpath(os.getcwd())
def save():
    json.dump({k: v for k, v in st.items() if k != "mcp"} | {"mcp": {k: v for k, v in st["mcp"].items() if k != "user"}},
              open(st_path, "w"))
    json.dump(cfg, open(cfg_path, "w"))
def out(x): print(json.dumps(x))
def err(m, rc=1): print(m, file=sys.stderr); sys.exit(rc)
if a[:1] == ["plugin"] and os.environ.get("STUB_NO_PLUGIN"):
    err("error: unknown command 'plugin'")
if a[:2] == ["plugin", "--help"]:
    print("Usage: claude plugin"); sys.exit(0)
if a[:3] == ["plugin", "marketplace", "list"]:
    bad = os.environ.get("STUB_MKT_LIST")
    if bad and (os.environ.get("STUB_MKT_LIST_WHEN") != "after-remove" or st.get("mkt_removed")):
        if bad == "exit": err("stub: marketplace list failed", 3)
        print({"json": "[{not json", "shape": '{"marketplaces": []}'}[bad]); sys.exit(0)
    out([dict(name=n, **m) for n, m in st["mkts"].items()]); sys.exit(0)
if a[:3] == ["plugin", "marketplace", "add"]:
    path = a[3]; name = json.load(open(os.path.join(path, ".claude-plugin", "marketplace.json")))["name"]
    if os.environ.get("STUB_FAIL_ADD"): sys.exit(1)
    if name in st["mkts"] and st["mkts"][name].get("path") != path:
        err("already exists with another source")
    st["mkts"][name] = {"source": "directory", "path": path, "installLocation": path}; save(); sys.exit(0)
if a[:3] == ["plugin", "marketplace", "remove"]:
    st["mkt_removed"] = True; save()
    if a[3] not in st["mkts"]: sys.exit(1)
    del st["mkts"][a[3]]
    st["plugins"] = {k: v for k, v in st["plugins"].items() if not k.endswith("@" + a[3])}; save(); sys.exit(0)
if a[:3] == ["plugin", "marketplace", "update"]:
    sys.exit(0 if a[3] in st["mkts"] else 1)
if a[:2] == ["plugin", "list"]:
    bad = os.environ.get("STUB_PLUGIN_LIST")
    when = os.environ.get("STUB_PLUGIN_LIST_WHEN")
    if bad and (not when or st.get({"after-update": "updated", "after-uninstall": "uninstalled"}[when])):
        if bad == "exit": err("stub: plugin list failed", 3)
        rec = {"id": "metacomputer@metacomputer", "scope": "user", "enabled": True}
        print({"json": "[{not json", "shape": '{"plugins": []}', "records": "[{}]",
               "noscope": json.dumps([{"id": rec["id"], "enabled": True}]),
               "errors": json.dumps([dict(rec, errors=["the plugin failed to load"])]),
               "noenabled": json.dumps([{"id": rec["id"], "scope": "user", "errors": ["the plugin failed to load"]}]),
               "strenabled": json.dumps([dict(rec, enabled="false")])}[bad]); sys.exit(0)
    # As the real one: a plugin from a directory marketplace is read in place, so its mcpServers are that folder's
    # plugin.json's; one from anywhere else is the copy installed (its mcpServers kept in the state).
    rows = []
    for k, v in st["plugins"].items():
        row = dict(id=k, **v)
        m = st["mkts"].get(k.split("@")[1]) or {}
        if m.get("source") == "directory":
            row.pop("mcpServers", None)
            try:
                srv = json.load(open(os.path.join(m["path"], "plugin", ".claude-plugin", "plugin.json"))).get("mcpServers")
            except (OSError, ValueError):
                srv = None
            if srv:
                row["mcpServers"] = srv
        rows.append(row)
    out(rows); sys.exit(0)
if a[:2] == ["plugin", "install"]:
    pid = a[2]
    if pid.split("@")[1] not in st["mkts"]: sys.exit(1)
    st["plugins"][pid] = {"scope": "user", "enabled": True}; save(); sys.exit(0)
if a[:2] == ["plugin", "update"]:
    st["updated"] = True; save()
    if a[2] not in st["plugins"] or os.environ.get("STUB_FAIL_UPDATE"): sys.exit(1)
    st["plugins"][a[2]].pop("mcpServers", None); save(); sys.exit(0)
if a[:2] == ["plugin", "uninstall"]:
    st["uninstalled"] = True; save()
    if a[2] not in st["plugins"] or os.environ.get("STUB_FAIL_PLUGIN_UNINSTALL"): sys.exit(1)
    del st["plugins"][a[2]]; save(); sys.exit(0)
SCOPE_WORDS = {"local": "Local config (private to you in this project)", "project": "Project config (shared via .mcp.json)",
               "user": "User config (available in all your projects)"}
def table(scope):
    return st["mcp"]["user"] if scope == "user" else st["mcp"][scope].setdefault(cwd, {})
if a[:2] == ["mcp", "get"]:
    if os.environ.get("STUB_FAIL_MCP_GET") or os.environ.get("STUB_FAIL_MCP_GET_FOR") == a[2]:
        err("stub: couldn't read the configuration", 2)
    after = os.environ.get("STUB_FAIL_GET_AFTER_REMOVE")
    if after and st.get("removed") and (after == "always" or not st.get("failed_after_remove")):
        st["failed_after_remove"] = True; save()
        err("stub: couldn't read the configuration", 2)
    if os.environ.get("STUB_MUTATE_ON_GET") and a[2] in st["mcp"]["user"]:
        import atexit
        def changed():         # after it printed what it read: the entry now has a variable more
            st["mcp"]["user"][a[2]]["env"] = dict(st["mcp"]["user"][a[2]].get("env") or {}, ADDED_ELSEWHERE="1")
            save()
        atexit.register(changed)
    for scope in ("local", "project", "user"):
        e = table(scope).get(a[2])
        if e:
            lines = [f"{a[2]}:", f"  Scope: {SCOPE_WORDS[scope]}", "  Status: x Failed to connect", f"  Type: {e['type']}"]
            if e["type"] == "stdio":
                shown_cmd = "/stub/other/command" if os.environ.get("STUB_GET_LIES") else e["command"]
                lines += [f"  Command: {shown_cmd}", "  Args:" + "".join(" " + x for x in [" ".join(e.get("args", []))] if x),
                          "  Environment:"]
                lines += [f"    {k}={v}" for k, v in (e.get("env") or {}).items()]
            else:
                lines += [f"  URL: {e['url']}"]
            print("\n".join(lines + ["", f"To remove this server, run: claude mcp remove {a[2]} -s {scope}"])); sys.exit(0)
    err(f'No MCP server named "{a[2]}". Configured servers: none')
if a[:2] == ["mcp", "add"]:
    rest, scope, env = a[2:], "local", {}
    while rest and rest[0].startswith("-") and rest[0] != "--":
        if rest[0] in ("-s", "--scope"): scope = rest[1]; rest = rest[2:]
        elif rest[0] in ("-e", "--env"): k, _, v = rest[1].partition("="); env[k] = v; rest = rest[2:]
        else: err("stub: unknown option " + rest[0], 2)
    name, rest = rest[0], rest[1:]
    if rest[:1] == ["--"]: rest = rest[1:]
    if os.environ.get("STUB_FAIL_MCP_ADD") or os.environ.get("STUB_FAIL_ADD_CMD") == rest[0]: err("stub: couldn't write the configuration")
    if name in table(scope): err(f"MCP server {name} already exists in {scope} config")
    table(scope)[name] = {"type": "stdio", "command": rest[0], "args": rest[1:], "env": env}; save(); sys.exit(0)
if a[:2] == ["mcp", "add-json"]:
    rest, scope = a[2:], "local"
    if rest[0] in ("-s", "--scope"): scope = rest[1]; rest = rest[2:]
    name, e = rest[0], json.loads(rest[1])
    if os.environ.get("STUB_FAIL_MCP_ADD") or os.environ.get("STUB_FAIL_ADD_CMD") == e.get("command"):
        err("stub: couldn't write the configuration")
    if name in table(scope): err(f"MCP server {name} already exists in {scope} config")
    table(scope)[name] = e; save(); sys.exit(0)
if a[:2] == ["mcp", "remove"]:
    if os.environ.get("STUB_FAIL_MCP_REMOVE"): err("stub: couldn't write the configuration")
    st["removed"] = True
    scopes = [a[a.index("-s") + 1]] if "-s" in a else ["local", "project", "user"]
    for scope in scopes:
        if a[2] in table(scope):
            del table(scope)[a[2]]; save(); sys.exit(0)
    err(f'No MCP server named "{a[2]}" in {scopes[0]} scope')
err("stub: unhandled " + " ".join(a), 2)
'''


class Box:
    """A temporary HOME with a stub claude on PATH, running this checkout's install.sh."""

    def __init__(self, real=False):
        self.t = tempfile.mkdtemp(prefix="mcomp-plugin-")
        self.home = os.path.join(self.t, "home")
        self.bin = os.path.join(self.t, "bin")
        self.work = os.path.join(self.t, "work")          # where install.sh runs unless a test says otherwise
        self.neutral = os.path.join(self.t, "neutral")    # an empty folder: no project or local server applies there
        for d in (self.home, self.bin, self.work, self.neutral):
            os.makedirs(d)
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

    def install(self, *args, cwd=None, script=None, **env):
        e = dict(self.env, **env)
        if os.path.exists(self.log):
            os.remove(self.log)
        p = subprocess.run([script or os.path.join(LAB, "install.sh"), *args], env=e, capture_output=True, text=True,
                           timeout=300, cwd=cwd or self.work, stdin=subprocess.DEVNULL)
        calls = [json.loads(x) for x in open(self.log)] if os.path.exists(self.log) else []
        if "install: stopped" in p.stderr:   # install.sh's ERR trap: a failure it meant to catch escaped somewhere
            check(f"install.sh {' '.join(args)}: no ERR-trap message", False, p.stderr[-600:])
        return p, calls

    def claude(self, *args, cwd=None):
        return subprocess.run(["claude", *args], env=self.env, capture_output=True, text=True, timeout=120,
                              cwd=cwd or self.neutral, stdin=subprocess.DEVNULL)

    def mkts(self):
        return {m["name"]: m.get("path") or m.get("repo") for m in json.loads(self.claude("plugin", "marketplace", "list", "--json").stdout or "[]")}

    def plugins(self):
        return {p["id"]: p for p in json.loads(self.claude("plugin", "list", "--json").stdout or "[]") if p.get("scope") == "user"}

    def entry(self, cwd=None):
        """The server named metacomputer that Claude Code uses in CWD (default: an empty folder, so the user-scope one),
        as `claude mcp get` shows it: {"scope", "type", "command", "args", "env"}, or None."""
        p = self.claude("mcp", "get", "metacomputer", cwd=cwd)
        if p.returncode != 0:
            return None
        f, env = {}, []
        for ln in p.stdout.splitlines():
            if ln.startswith("    ") and "=" in ln:
                env.append(ln.strip())
            elif ln.startswith("  ") and ":" in ln:
                k, _, v = ln.strip().partition(":")
                f.setdefault(k.lower(), v.strip())
        return {"scope": f.get("scope", "").split(" ")[0].lower(), "type": f.get("type"), "command": f.get("command"),
                "args": f.get("args", ""), "env": env}

    def server(self):
        """The user-scope server's command, or None."""
        e = self.entry()
        return e and e["scope"] == "user" and e["command"] or None

    def user_cfg(self):
        """The user-scope servers as Claude Code keeps them (the exact entries, arguments as lists)."""
        path = os.path.join(self.env.get("CLAUDE_CONFIG_DIR") or self.home, ".claude.json")
        try:
            return json.load(open(path)).get("mcpServers") or {}
        except (OSError, ValueError):
            return {}

    def registered(self):
        """Is the user-scope server exactly metacomputer's: stdio, ~/.local/bin/metacomputer, arguments ["mcp-lazy"]?"""
        e = self.user_cfg().get("metacomputer") or {}
        return e.get("type", "stdio") == "stdio" and e.get("command") == self.cmd and e.get("args") == ["mcp-lazy"]

    def add_user(self, entry, name="metacomputer"):
        """Add a user-scope server with its exact entry, through the CLI (the stub's or the real one)."""
        return self.claude("mcp", "add-json", "-s", "user", name, json.dumps(entry))

    def plugin_servers(self):
        """The MCP servers the installed plugin declares (none, from this checkout)."""
        return (self.plugins().get("metacomputer@metacomputer") or {}).get("mcpServers") or {}

    def sources(self):
        """Where Claude Code gets metacomputer's tools: "user" (the user-scope server, as it should be), "plugin" (an
        older plugin copy's own server). Two means the tools are listed twice; none, that there are none."""
        return (["user"] if self.registered() else []) + (["plugin"] if self.plugin_servers() else [])

    @property
    def cmd(self):
        return os.path.join(self.home, ".local", "bin", "metacomputer")

    def seed(self, mkts=None, plugins=None, mcp=None, local=None, project=None):
        """Stub state. mcp: user-scope servers, NAME -> [COMMAND, ARGS...] (stdio) or an entry dict; local and
        project: FOLDER -> {NAME: entry}."""
        if self.real:
            raise RuntimeError("seed is for the stub")

        def ent(v):
            return v if isinstance(v, dict) else {"type": "stdio", "command": v[0], "args": v[1:], "env": {}}
        scoped = {s: {os.path.realpath(d): {n: ent(v) for n, v in names.items()} for d, names in (m or {}).items()}
                  for s, m in (("local", local), ("project", project))}
        json.dump({"mkts": mkts or {}, "plugins": plugins or {}, "mcp": scoped}, open(self.state, "w"))
        json.dump({"mcpServers": {n: ent(v) for n, v in (mcp or {}).items()}},
                  open(os.path.join(self.home, ".claude.json"), "w"))

    def stub_state(self):
        st = json.load(open(self.state))
        st["mcp"]["user"] = self.user_cfg()
        return st

    def link(self, name):
        p = os.path.join(self.home, name)
        return os.readlink(p) if os.path.islink(p) else None

    def close(self):
        shutil.rmtree(self.t, ignore_errors=True)


def ran(calls, *prefix):
    return [c for c in calls if c[:len(prefix)] == list(prefix)]


def mutations(calls):
    return [c for c in calls if c[:2] in (["mcp", "add"], ["mcp", "add-json"], ["mcp", "remove"], ["plugin", "update"],
                                          ["plugin", "uninstall"])
            or c[:3] in (["plugin", "marketplace", "update"], ["plugin", "marketplace", "remove"])]


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
        check(tag + "fresh: the user-scope MCP server is stdio ~/.local/bin/metacomputer mcp-lazy; no skill link",
              b.registered() and b.link(skill_link) is None, (b.entry(), b.link(skill_link)))
        check(tag + "fresh: the plugin declares no MCP server, so the tools come from one place", b.sources() == ["user"],
              b.sources())
        check(tag + "fresh: it says what it did, with the tool names and how to load them",
              "marketplace metacomputer: this checkout" in p.stdout and "plugin metacomputer@metacomputer installed" in p.stdout
              and "MCP server metacomputer registered for every project" in p.stdout and PREFIX + "*" in p.stdout
              and PLUGIN_PREFIX not in p.stdout and "/mcp" in p.stdout.splitlines()[-1], p.stdout)
        if not real:
            check("fresh: claude plugin marketplace add <checkout>, install --scope user, mcp add -s user",
                  ["plugin", "marketplace", "add", LAB] in calls
                  and ["plugin", "install", "metacomputer@metacomputer", "--scope", "user"] in calls
                  and ["mcp", "add", "-s", "user", "metacomputer", b.cmd, "mcp-lazy"] in calls, calls)
        p, calls = b.install("--only-claude")
        check(tag + "again: exits 0 and says it's already done", p.returncode == 0 and "(already added)" in p.stdout
              and "already installed" in p.stdout and "already registered" in p.stdout, p.stdout + p.stderr)
        check(tag + "again: nothing was added, so no /mcp note", "/mcp" not in p.stdout, p.stdout[-300:])
        if not real:
            check("again: changes nothing (no add, install, update or remove)",
                  not mutations(calls) and not ran(calls, "plugin", "install")
                  and not ran(calls, "plugin", "marketplace", "add"), calls)

        # ~/.local/bin/metacomputer with other arguments (a space more or less included) isn't "already registered"
        # (the exact argument list is compared), and is never replaced: it's no proof of whose it is. The install says
        # how to remove it yourself; after that, it registers metacomputer's.
        for args in (["status"], [], [" mcp-lazy"], ["mcp-lazy "]):
            mine = {"type": "stdio", "command": b.cmd, "args": args, "env": {}}
            b.claude("mcp", "remove", "metacomputer", "-s", "user")
            b.add_user(mine)
            p, _ = b.install("--only-claude")
            check(tag + f"~/.local/bin/metacomputer with arguments {args}: not taken as registered, left exactly as it is, "
                  "says how to remove it yourself, exit 1", p.returncode == 1 and "already registered" not in p.stdout
                  and "claude mcp remove metacomputer -s user, then ./install.sh --only-claude" in p.stdout
                  and b.user_cfg().get("metacomputer") == mine, (b.user_cfg(), p.stdout[-400:]))
            b.claude("mcp", "remove", "metacomputer", "-s", "user")
            p, _ = b.install("--only-claude")
            check(tag + "…and once you've removed it, metacomputer's is registered", p.returncode == 0 and b.registered(),
                  (b.user_cfg(), p.stdout[-400:]))

        # An earlier install without the plugin: the skill link and a user-scope server running this checkout's
        # bin/metacomputer; the plugin not there yet.
        b.claude("plugin", "uninstall", "metacomputer@metacomputer", "--scope", "user")
        b.claude("plugin", "marketplace", "remove", "metacomputer")
        b.claude("mcp", "remove", "metacomputer", "-s", "user")
        os.makedirs(os.path.join(b.home, ".claude", "skills"), exist_ok=True)
        os.symlink(os.path.join(LAB, "skills", "metacomputer"), os.path.join(b.home, skill_link))
        b.claude("mcp", "add", "-s", "user", "metacomputer", CLI, "mcp-lazy")
        p, _ = b.install("--only-claude")
        check(tag + "upgrade: plugin installed, this checkout's skill link removed, the server now ~/.local/bin's",
              "metacomputer@metacomputer" in b.plugins() and b.link(skill_link) is None and b.registered(),
              (b.plugins(), b.link(skill_link), b.entry()))
        check(tag + "upgrade: it says so, and what the server ran before", "removed the skill link" in p.stdout
              and "it replaces the registration that ran" in p.stdout, p.stdout)

        p, _ = b.install("--only-claude", "--no-plugin")
        check(tag + "--no-plugin: skill link + the same user-scope server, this checkout's plugin and marketplace removed",
              b.link(skill_link) == os.path.join(LAB, "skills", "metacomputer") and b.registered()
              and not b.plugins() and "metacomputer" not in b.mkts(), (b.link(skill_link), b.entry(), b.plugins(), b.mkts()))
        check(tag + "--no-plugin: names the mcp__metacomputer__ tools", "mcp__metacomputer__*" in p.stdout, p.stdout)
        p, _ = b.install("--only-claude", "--no-plugin")
        check(tag + "--no-plugin again: already registered", p.returncode == 0 and "already registered" in p.stdout, p.stdout)

        b.install("--only-claude")
        p, _ = b.install("--uninstall")
        check(tag + "--uninstall: plugin, marketplace, MCP server and command links removed",
              p.returncode == 0 and not b.plugins() and "metacomputer" not in b.mkts() and b.entry() is None
              and b.link(".local/bin/metacomputer") is None and b.link(".local/bin/mcomp") is None,
              p.stdout + p.stderr)
        b.install("--only-claude", "--no-plugin")
        p, _ = b.install("--uninstall")
        check(tag + "--uninstall: skill link and server removed too", b.link(skill_link) is None and b.entry() is None,
              p.stdout + p.stderr)
    finally:
        b.close()
    test_update_from_plugin_server(real)
    if real:
        return

    # --no-claude leaves everything of Claude Code's as it is: nothing added to a fresh one, nothing removed later.
    b = Box()
    try:
        p, calls = b.install("--only-agents", "--no-claude", "--no-codex")
        check("--no-claude: no plugin, no marketplace, no MCP server, no claude command run",
              p.returncode == 0 and not b.plugins() and not b.mkts() and b.entry() is None and not calls
              and "Claude Code: left alone (--no-claude" in p.stdout, (calls, p.stdout[-400:]))
        b.install("--only-claude")
        p, calls = b.install("--only-agents", "--no-claude", "--no-codex")
        check("--no-claude after a setup: the plugin and the server stay as they are",
              p.returncode == 0 and "metacomputer@metacomputer" in b.plugins() and b.registered() and not mutations(calls),
              (calls, p.stdout[-300:]))
    finally:
        b.close()

    test_ownership()
    test_registration_failures()
    test_plugin_list_failures()
    test_scopes()
    test_no_secrets_in_output()
    test_switch_marketplace()
    test_switch_reads_plugin_first()
    test_marketplace_location_unprinted()
    test_retirement_unknown()
    test_incomplete_marker()
    test_entry_changed()
    test_uninstall_server_first()
    test_owned_replace_lookup_fails()

    # The GitHub marketplace is kept; its plugin runs this checkout through ~/.local/bin.
    b = Box()
    try:
        b.seed(mkts={"metacomputer": {"source": "github", "repo": "metavert-io/metacomputer"}})
        p, calls = b.install("--only-claude")
        check("GitHub marketplace: kept, plugin installed from it, the user-scope server registered",
              ["plugin", "marketplace", "add", LAB] not in calls and b.mkts() == {"metacomputer": "metavert-io/metacomputer"}
              and "metacomputer@metacomputer" in b.plugins() and "github.com/metavert-io/metacomputer" in p.stdout
              and b.registered(), p.stdout)
        p, calls = b.install("--uninstall")
        check("GitHub marketplace: --uninstall removes the plugin and the server, keeps the marketplace and says how to "
              "remove it", not b.plugins() and b.entry() is None and "metacomputer" in b.mkts()
              and "claude plugin marketplace remove metacomputer" in p.stdout, p.stdout)
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
    # its plugin would run its own code on this Mac. Refused with a clear message; the skill link and the
    # user-scope server instead; that marketplace and its plugin are left alone.
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
                  and not [c for c in calls if c[:2] in (["plugin", "install"], ["plugin", "update"])]
                  and "NOT installed" in out and "claude plugin marketplace remove metacomputer" in out
                  and b.link(".claude/skills/metacomputer") == os.path.join(LAB, "skills", "metacomputer")
                  and b.registered() and "metacomputer" in b.mkts(), out)
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

    # Turned off: left so, and so is everything else (an earlier skill link and server stay; none is added).
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
              and b.link(skill_link) and b.server() == CLI and "claude plugin enable metacomputer@metacomputer" in p.stdout + p.stderr
              and not mutations(calls), p.stdout + p.stderr)
        b.seed(mkts={"metacomputer": {"source": "directory", "path": LAB}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": False}})
        p, _ = b.install("--only-claude")
        check("plugin turned off and no server: none is added, and it says what to run after turning it on",
              p.returncode == 0 and b.entry() is None and "isn't added either" in p.stdout
              and "./install.sh --only-claude" in p.stdout, p.stdout)
    finally:
        b.close()

    # No `claude plugin` (an older Claude Code), or the plugin fails: the skill link + server instead.
    for why, env in (("no 'claude plugin'", {"STUB_NO_PLUGIN": "1"}), ("marketplace add fails", {"STUB_FAIL_ADD": "1"})):
        b = Box()
        try:
            p, _ = b.install("--only-claude", **env)
            check(f"{why}: falls back to the skill link and the user-scope server, exit 0",
                  p.returncode == 0 and b.link(skill_link) == os.path.join(LAB, "skills", "metacomputer")
                  and b.registered(), p.stdout + p.stderr)
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


OLD_SRV = {"metacomputer": {"type": "stdio", "command": "${CLAUDE_PLUGIN_ROOT}/bin/metacomputer", "args": ["mcp-lazy"]}}
GITHUB = {"metacomputer": {"source": "github", "repo": "metavert-io/metacomputer"}}


def other_checkout(t, name="other"):
    """Another metacomputer checkout, as install.sh tells one (install.sh and bin/metacomputer)."""
    d = os.path.join(t, name)
    write(os.path.join(d, "install.sh"), "#!/bin/sh\n", 0o755)
    write(os.path.join(d, "bin", "metacomputer"), "#!/bin/sh\n", 0o755)
    return d


def test_ownership():
    """Item 1 and N1: only metacomputer's own user-scope registration, by its canonical command and exact arguments,
    is ever replaced. Another program's, an interpreter, a path that only starts with the checkout's, a launcher that
    doesn't exist, an HTTP server and a custom one stay exactly as they are (with how to remove them yourself), never
    replaced, at a terminal or not; another checkout's moves only with --switch; a failed or inconsistent lookup
    changes nothing."""
    skill_link = ".claude/skills/metacomputer"
    b = Box()
    try:
        # A program that isn't a metacomputer checkout (a bin/metacomputer and a skill, no install.sh).
        elsewhere = os.path.join(b.t, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "skills", "metacomputer"))
        write(os.path.join(elsewhere, "bin", "metacomputer"), "#!/bin/sh\n", 0o755)
        os.makedirs(os.path.join(b.home, ".claude", "skills"))
        os.symlink(os.path.join(elsewhere, "skills", "metacomputer"), os.path.join(b.home, skill_link))
        theirs = os.path.join(elsewhere, "bin", "metacomputer")
        for extra in ((), ("--yes",)):
            b.seed(mcp={"metacomputer": [theirs, "mcp-lazy"]})
            p, calls = b.install("--only-claude", *extra)
            check(f"a registration that isn't metacomputer's ({' '.join(extra) or 'no terminal'}): left alone, says how to "
                  "remove it yourself, exit 1; the plugin is installed and the other skill link stays",
                  p.returncode == 1 and b.server() == theirs and "isn't metacomputer's" in p.stdout
                  and "claude mcp remove metacomputer -s user, then ./install.sh --only-claude" in p.stdout
                  and "didn't finish" in p.stderr and "metacomputer with 1 argument (not shown)" in p.stdout
                  and theirs not in p.stdout
                  and not ran(calls, "mcp", "add") and not ran(calls, "mcp", "remove")
                  and "metacomputer@metacomputer" in b.plugins()
                  and b.link(skill_link) == os.path.join(elsewhere, "skills", "metacomputer"), (p.stdout[-600:], calls))

        # Commands that only look like the checkout's (N1): an interpreter or another file under it, a path that
        # climbs out of it, a launcher that doesn't exist, the checkout's own launcher with other arguments.
        outside = os.path.join(b.t, "outside", "bin", "server")
        write(outside, "#!/bin/sh\n", 0o755)
        climbs = os.path.join(LAB, "..", os.path.relpath(outside, os.path.dirname(LAB)))
        for why, entry in (("another file under the checkout", [os.path.join(LAB, "lib", "agents.py"), "mcp-lazy"]),
                           ("an interpreter", [sys.executable, "-m", "some_mcp"]),
                           ("a path that starts with the checkout's and climbs out of it", [climbs, "mcp-lazy"]),
                           ("a launcher that doesn't exist (a moved checkout's? no proof)",
                            [os.path.join(b.t, "gone", "bin", "metacomputer"), "mcp-lazy"]),
                           ("this checkout's launcher with other arguments", [CLI, "status"])):
            b.seed(mcp={"metacomputer": entry})
            p, calls = b.install("--only-claude", "--yes")
            check(f"{why}: not metacomputer's, left as it is (--yes), exit 1",
                  p.returncode == 1 and b.user_cfg()["metacomputer"]["command"] == entry[0]
                  and not ran(calls, "mcp", "remove") and "claude mcp remove metacomputer -s user" in p.stdout,
                  (p.stdout[-400:], calls))
        for why, cmd in (("this checkout's launcher by a path with ..", os.path.join(LAB, "bin", "..", "bin", "metacomputer")),
                         ("a legacy name earlier versions registered (gone since)", os.path.join(LAB, "bin", "claude-vc"))):
            b.seed(mcp={"metacomputer": [cmd, "mcp-lazy"]})
            p, _ = b.install("--only-claude", "--yes")
            check(f"{why}: metacomputer's own, repaired without asking", p.returncode == 0 and b.registered()
                  and "it replaces the registration that ran" in p.stdout, p.stdout[-400:])

        # Another metacomputer checkout's registration: moved here only with --switch.
        other = other_checkout(b.t)
        for extra in ((), ("--yes",)):
            b.seed(mcp={"metacomputer": [os.path.join(other, "bin", "metacomputer"), "mcp-lazy"]})
            p, calls = b.install("--only-claude", *extra)
            check(f"another checkout's registration ({' '.join(extra) or 'no terminal'}): left alone, says --switch, exit 1",
                  p.returncode == 1 and b.server() == os.path.join(other, "bin", "metacomputer")
                  and "runs another checkout" in p.stdout and "--switch" in p.stdout and not ran(calls, "mcp", "add"),
                  p.stdout[-500:])
        p, _ = b.install("--only-claude", "--switch")
        check("…and --switch points it at this one", p.returncode == 0 and b.registered(), p.stdout[-400:])
        b.seed(mcp={"metacomputer": [os.path.join(other, "bin", "metacomputer"), "serve"]})
        p, _ = b.install("--only-claude", "--switch")
        check("another checkout's bin/metacomputer with other arguments is a configuration of yours: --switch leaves it",
              p.returncode == 1 and b.server() == os.path.join(other, "bin", "metacomputer")
              and "claude mcp remove metacomputer -s user" in p.stdout,
              p.stdout[-400:])

        # Not a local command (HTTP), or one that sets variables: never replaced either.
        b.seed(mcp={"metacomputer": {"type": "http", "url": "http://127.0.0.1:9/mcp?token=secret"}})
        for extra in ((), ("--yes",)):
            p, calls = b.install("--only-claude", *extra)
            e = b.entry()
            check(f"an HTTP server named metacomputer ({' '.join(extra) or 'as is'}): left alone, says to remove it "
                  "yourself, never prints its URL, exit 1", p.returncode == 1 and e and e["type"] == "http"
                  and "not a local command" in p.stdout and "claude mcp remove metacomputer -s user" in p.stdout
                  and "secret" not in p.stdout + p.stderr and not ran(calls, "mcp", "remove"), (e, p.stdout[-500:]))
        b.seed(mcp={"metacomputer": {"type": "stdio", "command": CLI, "args": ["status"], "env": {"TOKEN": "secret"}}})
        p, calls = b.install("--only-claude", "--yes")
        check("a registration with variables of its own and other arguments: left alone, its values never printed", p.returncode == 1 and b.server() == CLI and "variables of its own" in p.stdout
              and "secret" not in p.stdout + p.stderr and not ran(calls, "mcp", "remove"), p.stdout[-400:])
        b.seed(mcp={"metacomputer": {"type": "stdio", "command": b.cmd, "args": ["mcp-lazy"], "env": {"MCOMP_VM": "x"}}})
        p, calls = b.install("--only-claude")
        check("metacomputer's own registration with variables you set: already registered, the variables kept",
              p.returncode == 0 and "already registered" in p.stdout and "your variables on it are kept" in p.stdout
              and b.user_cfg()["metacomputer"]["env"] == {"MCOMP_VM": "x"} and not ran(calls, "mcp", "add"), p.stdout[-400:])

        # A lookup that fails, or a configuration that disagrees with what `claude mcp get` shows, changes nothing.
        for why, env in (("a failed lookup (claude mcp get)", {"STUB_FAIL_MCP_GET": "1"}),
                         ("claude mcp get and the configuration disagree", {"STUB_GET_LIES": "1"})):
            b.seed(mkts=GITHUB, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True, "mcpServers": OLD_SRV}},
                   mcp={"metacomputer": [CLI, "mcp-lazy"]})
            p, calls = b.install("--only-claude", **env)
            check(f"{why}: nothing removed or added, no plugin update, says so, exit 1",
                  p.returncode == 1 and "couldn't read Claude Code's user-scope MCP server" in p.stdout and not mutations(calls)
                  and b.user_cfg()["metacomputer"]["command"] == CLI and b.plugin_servers(), (calls, p.stdout[-500:]))
        b.seed(mcp={"metacomputer": [CLI, "mcp-lazy"]})
        with open(os.path.join(b.home, ".claude.json"), "w") as f:
            f.write("{not json")
        p, calls = b.install("--only-claude")
        check("a configuration file that isn't JSON: nothing changed, exit 1", p.returncode == 1
              and "couldn't read Claude Code's user-scope MCP server" in p.stdout and not ran(calls, "mcp", "add")
              and not ran(calls, "mcp", "remove"), p.stdout[-400:])

        # A moved checkout's dangling skill link is this install's to clean up (its server isn't: see above).
        os.remove(os.path.join(b.home, skill_link))
        os.symlink(os.path.join(b.t, "gone", "skills", "metacomputer"), os.path.join(b.home, skill_link))
        b.seed(mkts={"metacomputer": {"source": "directory", "path": LAB}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        p, _ = b.install("--only-claude")
        check("a moved checkout's dangling skill link is removed", p.returncode == 0 and b.link(skill_link) is None
              and b.registered(), p.stdout[-400:])
        # --uninstall from a checkout the command no longer runs leaves the server, which serves that other one.
        os.remove(b.cmd)
        os.symlink(os.path.join(other, "bin", "metacomputer"), b.cmd)
        p, _ = b.install("--uninstall")
        check("--uninstall here, while the command runs another checkout: the server (through that link) stays, and "
              "it says so", p.returncode == 0 and b.registered() and "stays: it runs" in p.stdout, p.stdout[-400:])
    finally:
        b.close()


def test_registration_failures():
    """Item 2 and N2: what served the tools before (an older GitHub plugin copy's own server, the plugin itself with
    --no-plugin, an old server name) is retired only once the user-scope server is registered and read back; a failed
    add after metacomputer's own was removed says plainly that the server is missing and the exact command that adds it;
    a failed remove changes nothing; removals are checked; every one of these ends with exit status 1, and the count of
    servers is what it says."""
    old_plugin = {"metacomputer@metacomputer": {"scope": "user", "enabled": True, "mcpServers": OLD_SRV}}
    b = Box()
    try:
        b.seed(mkts=GITHUB, plugins=old_plugin)
        p, calls = b.install("--only-claude", STUB_FAIL_MCP_ADD="1")
        check("add fails, the plugin's own server is the only one: no plugin update, it stays, exit 1, and it says so",
              p.returncode == 1 and b.sources() == ["plugin"] and not ran(calls, "plugin", "update")
              and not ran(calls, "plugin", "marketplace", "update") and "could not register" in p.stdout
              and "tools are still there" in p.stdout, (b.sources(), calls, p.stdout[-500:]))
        b.seed(mkts=GITHUB, plugins=old_plugin, mcp={"metacomputer": [CLI, "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_FAIL_ADD_CMD=b.cmd)
        check("add fails after metacomputer's own registration was removed: it says the server is missing and the exact "
              "command that adds it; no plugin update (its own server stays), exit 1",
              p.returncode == 1 and "metacomputer" not in b.user_cfg() and "has no MCP server metacomputer now" in p.stdout
              and "claude mcp add -s user metacomputer ~/.local/bin/metacomputer mcp-lazy" in p.stdout
              and not ran(calls, "mcp", "add-json") and not ran(calls, "plugin", "update") and b.sources() == ["plugin"],
              (b.user_cfg(), calls, p.stdout[-500:]))
        b.seed(mkts=GITHUB, plugins=old_plugin, mcp={"metacomputer": [CLI, "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_FAIL_MCP_REMOVE="1")
        check("remove fails: nothing changed (no add, no plugin update), exit 1",
              p.returncode == 1 and b.server() == CLI and not ran(calls, "mcp", "add") and not ran(calls, "plugin", "update"),
              (calls, p.stdout[-400:]))
        for fail in (False, True):
            b.seed(mkts=GITHUB, plugins=old_plugin)
            p, calls = b.install("--only-claude", **({"STUB_FAIL_UPDATE": "1"} if fail else {}))
            updated = (["plugin", "marketplace", "update", "metacomputer"] in calls
                       and ["plugin", "update", "metacomputer@metacomputer", "--scope", "user"] in calls)
            if not fail:
                check("an older GitHub copy with its own server: updated once the user-scope server is checked; one server, "
                      "exit 0", p.returncode == 0 and updated and b.sources() == ["user"]
                      and "updated: it no longer brings" in p.stdout
                      and calls.index(["plugin", "update", "metacomputer@metacomputer", "--scope", "user"])
                      > calls.index(["mcp", "add", "-s", "user", "metacomputer", b.cmd, "mcp-lazy"]), (calls, p.stdout[-500:]))
            else:
                check("…and when that update fails: both servers, it says the tools are listed twice and how to fix it, "
                      "exit 1", p.returncode == 1 and updated and b.sources() == ["user", "plugin"]
                      and "lists the tools twice" in p.stdout and "claude plugin update metacomputer@metacomputer" in p.stdout,
                      p.stdout[-500:])

        # --no-plugin (N2): the plugin, possibly what serves the tools now, is removed only after the server is checked.
        here = {"metacomputer": {"source": "directory", "path": LAB}}
        b.seed(mkts=GITHUB, plugins=old_plugin)
        p, calls = b.install("--only-claude", "--no-plugin", STUB_FAIL_MCP_ADD="1")
        check("--no-plugin whose add fails: the plugin (an older copy, the only server) stays, nothing uninstalled, exit 1",
              p.returncode == 1 and b.sources() == ["plugin"] and not ran(calls, "plugin", "uninstall")
              and "stays until the MCP server is registered" in p.stdout, (calls, p.stdout[-400:]))
        b.seed(mkts=GITHUB, plugins=old_plugin)
        p, calls = b.install("--only-claude", "--no-plugin")
        check("--no-plugin: the server is registered and checked first, then the plugin is uninstalled; one server",
              p.returncode == 0 and b.sources() == ["user"] and not b.plugins()
              and calls.index(["plugin", "uninstall", "metacomputer@metacomputer", "--scope", "user"])
              > calls.index(["mcp", "add", "-s", "user", "metacomputer", b.cmd, "mcp-lazy"]), (calls, p.stdout[-400:]))
        b.seed(mkts=here, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        p, calls = b.install("--only-claude", "--no-plugin", STUB_FAIL_PLUGIN_UNINSTALL="1")
        check("--no-plugin whose plugin uninstall fails: said (checked, not assumed), exit 1",
              p.returncode == 1 and "couldn't uninstall the plugin" in p.stdout and "metacomputer@metacomputer" in b.plugins()
              and b.registered(), p.stdout[-400:])
        b.install("--only-claude")
        p, _ = b.install("--uninstall", STUB_FAIL_PLUGIN_UNINSTALL="1")
        check("--uninstall whose plugin uninstall fails: said, exit 1", p.returncode == 1
              and "couldn't uninstall the plugin" in p.stdout and "Not everything was removed" in p.stderr, p.stdout[-400:])

        # An old server name (cua-linux, this checkout's legacy launcher) is retired only after the server is checked.
        legacy = {"cua-linux": [os.path.join(LAB, "bin", "claude-vc"), "mcp-lazy"]}
        b.seed(mkts=here, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}}, mcp=dict(legacy))
        p, calls = b.install("--only-claude", STUB_FAIL_MCP_ADD="1")
        check("an old server name while the add fails: kept (it may be what serves the tools), exit 1",
              p.returncode == 1 and "cua-linux" in b.user_cfg() and not ran(calls, "mcp", "remove"), (calls, p.stdout[-300:]))
        p, calls = b.install("--only-claude")
        check("…and once the server is registered and checked: removed, after it", p.returncode == 0
              and "cua-linux" not in b.user_cfg() and b.registered()
              and calls.index(["mcp", "remove", "cua-linux", "-s", "user"])
              > calls.index(["mcp", "add", "-s", "user", "metacomputer", b.cmd, "mcp-lazy"]), (calls, p.stdout[-300:]))
        b.seed(mkts=here, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}},
               mcp={"cua-linux": ["/opt/someone/else", "mcp-lazy"]})
        p, calls = b.install("--only-claude")
        check("an old server name that isn't this checkout's stays", p.returncode == 0 and "cua-linux" in b.user_cfg()
              and not ran(calls, "mcp", "remove"), calls)
    finally:
        b.close()


def test_plugin_list_failures():
    """Item 5, N4 and F3: `claude plugin list --json` that fails, isn't JSON, has another shape, a record without its id
    or scope, reports errors loading this plugin, or has an "enabled" that isn't true or false, is never read as "no
    server", nor as on or off: before the update (nothing updated, registered or installed, exit 1) or after it (no
    claim that it worked, exit 1)."""
    old_plugin = {"metacomputer@metacomputer": {"scope": "user", "enabled": True, "mcpServers": OLD_SRV}}
    b = Box()
    try:
        for bad in ("exit", "json", "shape", "records", "noscope", "errors", "noenabled", "strenabled"):
            b.seed(mkts=GITHUB, plugins=old_plugin)
            p, calls = b.install("--only-claude", STUB_PLUGIN_LIST=bad)
            check(f"plugin list {bad} before the update: no update, no claim the plugin brings no server, exit 1",
                  p.returncode == 1 and not ran(calls, "plugin", "update") and "no longer brings" not in p.stdout
                  and "couldn't read the installed plugins" in p.stdout, (calls, p.stdout[-400:]))
            if bad in ("errors", "noenabled", "strenabled"):     # F3: a record this can't read is an error, never on or off
                check(f"…{bad}: not taken for on or off: nothing installed, registered or changed", b.entry() is None
                      and not mutations(calls) and not ran(calls, "plugin", "install") and "turned off" not in p.stdout,
                      (calls, p.stdout[-300:]))
            for upd in ({}, {"STUB_FAIL_UPDATE": "1"}):
                b.seed(mkts=GITHUB, plugins=old_plugin)
                p, calls = b.install("--only-claude", STUB_PLUGIN_LIST=bad, STUB_PLUGIN_LIST_WHEN="after-update", **upd)
                check(f"plugin list {bad} after the update{' (which failed)' if upd else ''}: never reported as done, "
                      "exit 1", p.returncode == 1 and ran(calls, "plugin", "update") and "no longer brings" not in p.stdout
                      and "after updating" in p.stdout, p.stdout[-400:])
    finally:
        b.close()


def test_scopes():
    """Item 4: the user-scope server is read and changed as such. A local or project server named metacomputer in the
    folder install.sh runs in doesn't count as registered; it is reported and never changed, at install and uninstall."""
    b = Box()
    try:
        proj = os.path.join(b.t, "proj")
        os.makedirs(proj)
        mine = {"type": "stdio", "command": b.cmd, "args": ["mcp-lazy"], "env": {}}
        b.seed(local={proj: {"metacomputer": dict(mine)}}, project={proj: {"metacomputer": dict(mine)}})
        p, calls = b.install("--only-claude", cwd=proj)
        st = b.stub_state()["mcp"]
        rp = os.path.realpath(proj)
        check("a local and a project server with the same command don't count: the user-scope one is registered, "
              "the others are reported and left as they are",
              p.returncode == 0 and b.registered() and "already registered" not in p.stdout
              and "from its Local config" in p.stdout and st["local"][rp]["metacomputer"] == mine
              and st["project"][rp]["metacomputer"] == mine
              and all(c[:5] == ["mcp", "add", "-s", "user", "metacomputer"] for c in ran(calls, "mcp", "add"))
              and all("-s" in c and c[c.index("-s") + 1] == "user" for c in ran(calls, "mcp", "remove")),
              (p.stdout[-500:], st, calls))
        theirs = {"type": "stdio", "command": "/opt/elsewhere/bin/thing", "args": [], "env": {}}
        b.seed(mkts={"metacomputer": {"source": "directory", "path": LAB}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}},
               mcp={"metacomputer": [b.cmd, "mcp-lazy"]}, project={proj: {"metacomputer": dict(theirs)}})
        os.makedirs(os.path.dirname(b.cmd), exist_ok=True)
        if not os.path.islink(b.cmd):
            os.symlink(CLI, b.cmd)
        p, calls = b.install("--uninstall", cwd=proj)
        st = b.stub_state()["mcp"]
        check("uninstall where a project server shadows it: the user-scope one is removed, the project one stays and is "
              "reported", p.returncode == 0 and "metacomputer" not in st["user"]
              and st["project"][rp]["metacomputer"] == theirs and "from its Project config" in p.stdout
              and ["mcp", "remove", "metacomputer", "-s", "user"] in calls, (p.stdout[-500:], st))
        b.install("--only-claude")
        p, calls = b.install("--uninstall", STUB_FAIL_MCP_GET="1")
        check("uninstall whose lookup fails: stops before removing anything (links, plugin, server), exit 1",
              p.returncode == 1 and b.link(".local/bin/metacomputer") == CLI and "metacomputer@metacomputer" in b.plugins()
              and not mutations(calls) and "nothing was removed" in p.stdout + p.stderr, (p.stdout[-300:], p.stderr[-300:]))
    finally:
        b.close()


def old_layout_checkout(t):
    """A checkout as v0.4.4 had it for Claude Code: its plugin declares the MCP server (plugin.json's mcpServers).
    Only what install.sh's Claude Code step reads: install.sh, lib/claude_mcp.py, the command, the skill and the plugin."""
    co = os.path.join(t, "checkout")
    os.makedirs(co)
    shutil.copy2(os.path.join(LAB, "install.sh"), co)
    write(os.path.join(co, "bin", "metacomputer"), "#!/bin/sh\nexit 0\n", 0o755)
    os.makedirs(os.path.join(co, "lib"))
    shutil.copy2(os.path.join(LAB, "lib", "claude_mcp.py"), os.path.join(co, "lib"))
    shutil.copy2(os.path.join(LAB, "lib", "strictargs.py"), os.path.join(co, "lib"))       # the rules of its command lines
    os.makedirs(os.path.join(co, "scripts"), exist_ok=True)
    shutil.copy2(os.path.join(LAB, "scripts", "strictargs.sh"), os.path.join(co, "scripts"))
    shutil.copytree(os.path.join(LAB, "skills", "metacomputer"), os.path.join(co, "skills", "metacomputer"))
    shutil.copytree(os.path.join(LAB, ".claude-plugin"), os.path.join(co, ".claude-plugin"))
    shutil.copytree(os.path.join(LAB, "plugin"), os.path.join(co, "plugin"))
    pj = os.path.join(co, "plugin", ".claude-plugin", "plugin.json")
    d = json.load(open(pj))
    d["mcpServers"] = OLD_SRV
    json.dump(d, open(pj, "w"), indent=2)
    return co


def test_update_from_plugin_server(real=False):
    """`metacomputer update` from an install whose plugin brought the MCP server: the checkout moves to this version
    (its plugin.json no longer declares one) and install.sh runs; afterwards the user-scope server is the only one."""
    tag = "real claude: " if real else ""
    b = Box(real)
    try:
        co = old_layout_checkout(b.t)
        if real:
            b.claude("plugin", "marketplace", "add", co)
            b.claude("plugin", "install", "metacomputer@metacomputer", "--scope", "user")
        else:
            b.seed(mkts={"metacomputer": {"source": "directory", "path": co}},
                   plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        os.makedirs(os.path.join(b.home, ".local", "bin"))
        os.symlink(os.path.join(co, "bin", "metacomputer"), b.cmd)
        check(tag + "before the update: the plugin's MCP server, no user-scope one", b.sources() == ["plugin"]
              and b.entry() is None, (b.plugin_servers(), b.entry()))
        # The update: the checkout moves to this version, then install.sh's agents step runs (update runs install.sh).
        shutil.copy2(os.path.join(LAB, "plugin", ".claude-plugin", "plugin.json"),
                     os.path.join(co, "plugin", ".claude-plugin", "plugin.json"))
        stamp = os.path.join(b.home, ".local", "share", "metacomputer", "installed.json")
        write(stamp, json.dumps({"version": "v0.4.4", "tag": "v0.4.4", "commit": "0" * 40, "checkout": co}))
        if not real:
            # F9: the registration fails (the update moved the checkout first, so the plugin's declaration is gone):
            # said plainly, with the line that fixes it and that update tries again; unfinished; nothing else removed.
            p, calls = b.install("--only-agents", "--no-codex", script=os.path.join(co, "install.sh"), STUB_FAIL_MCP_ADD="1")
            out = p.stdout + p.stderr
            check("the update whose registration fails: it says plainly that new Claude Code sessions have no "
                  "metacomputer tools until it's fixed, the exact line that fixes it, and that `metacomputer update` tries "
                  "again; recorded as unfinished (exit 1)", p.returncode == 1
                  and "metacomputer's tools are missing in new Claude Code sessions until this is fixed" in out
                  and "claude mcp add -s user metacomputer ~/.local/bin/metacomputer mcp-lazy" in out
                  and "metacomputer update tries this step again" in out
                  and json.load(open(stamp)).get("incomplete") == ["claude"], (out[-700:], json.load(open(stamp))))
            check("…and nothing else was removed: the marketplace, the plugin and the command stay; the add was the only "
                  "change tried", b.mkts() == {"metacomputer": co} and "metacomputer@metacomputer" in b.plugins()
                  and os.path.islink(b.cmd) and b.entry() is None
                  and all(c[:2] == ["mcp", "add"] for c in mutations(calls)) and ran(calls, "mcp", "add"),
                  (mutations(calls), b.mkts(), b.plugins()))
        p, _ = b.install("--only-agents", "--no-codex", script=os.path.join(co, "install.sh"))
        check(tag + "after: the plugin still installed, declaring no server; the user-scope server is "
              "~/.local/bin/metacomputer mcp-lazy: exactly one", p.returncode == 0 and "metacomputer@metacomputer" in b.plugins()
              and b.sources() == ["user"], (p.stdout[-600:], p.stderr[-300:], b.sources(), b.entry()))
        check(tag + "after: it says to restart Claude Code (or /mcp) to load the tools, and the record is cleared",
              "/mcp" in p.stdout and "added in this run" in p.stdout and "incomplete" not in json.load(open(stamp)),
              p.stdout[-300:])
        p, _ = b.install("--only-agents", "--no-codex", script=os.path.join(co, "install.sh"))
        check(tag + "a second run: already registered, still one server", p.returncode == 0
              and "already registered" in p.stdout and b.sources() == ["user"], p.stdout[-400:])
    finally:
        b.close()


def test_plugin_only_update():
    """Item 3: an install from v0.4.4 or earlier whose plugin was updated on its own (from GitHub) while the checkout
    kept its old installer, which relied on the plugin for the MCP server: no tools anywhere. The plugin's launcher
    says so and names `metacomputer update`, never the old checkout's install.sh; the skill says the same; and the
    install step of the update (the checkout's new install.sh) brings the tools back."""
    b = Box()
    try:
        co = old_layout_checkout(b.t)
        legacy = '#!/bin/bash\n# metacomputer install.sh from v0.4.4: the plugin brings the MCP server\necho "OLD INSTALL $*"\n'
        write(os.path.join(co, "install.sh"), legacy, 0o755)
        write(os.path.join(co, "bin", "metacomputer"), '#!/bin/sh\necho "CHECKOUT $*"\n', 0o755)
        os.makedirs(os.path.join(b.home, ".local", "bin"))
        os.symlink(os.path.join(co, "bin", "metacomputer"), b.cmd)
        # The plugin updated from GitHub: this version's copy, no mcpServers; no user-scope server.
        b.seed(mkts=GITHUB, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        cache = os.path.join(b.home, ".claude", "plugins", "cache", "metacomputer", "metacomputer", "new")
        shutil.copytree(os.path.join(LAB, "plugin"), cache)
        launcher = os.path.join(cache, "bin", "metacomputer")
        check("plugin-only update: no tools anywhere (no plugin server, no user-scope one)", b.sources() == [], b.sources())
        for cmd in (["status"], ["agents"], []):
            p = run([launcher, *cmd], base_env(b.home))
            check(f"…the launcher's `metacomputer {' '.join(cmd)}` runs the checkout and says to run `metacomputer update`",
                  p.stdout.startswith("CHECKOUT") and "metacomputer update" in p.stderr and "install.sh" not in p.stderr
                  and "restart Claude Code" in p.stderr, p.stderr)
        p = run([launcher, "call", "list_windows", "{}"], base_env(b.home))
        check("…other commands just run, without the notice", p.stdout.strip() == "CHECKOUT call list_windows {}"
              and not p.stderr, p.stderr)
        skill = open(os.path.join(LAB, "skills", "metacomputer", "SKILL.md")).read()
        para = next((ln for ln in skill.splitlines() if "**No `mcp__metacomputer__*` tools?**" in ln), "")
        check("…the skill's no-tools step names `metacomputer update` for an older metacomputer, and no install.sh",
              "`metacomputer update`" in para and "install.sh" not in para and "metacomputer agents" in para, para)
        # The update moves the checkout to this version and runs its install.sh, which registers the server.
        shutil.copy2(os.path.join(LAB, "install.sh"), os.path.join(co, "install.sh"))
        p, _ = b.install("--yes", "--only-agents", "--no-codex", script=os.path.join(co, "install.sh"))
        check("…and the update's install step brings the tools back: the user-scope server, once",
              p.returncode == 0 and b.sources() == ["user"], (p.stdout[-500:], b.sources()))
        p = run([launcher, "status"], base_env(b.home))
        check("…after which the launcher says nothing more", p.stdout.startswith("CHECKOUT") and not p.stderr, p.stderr)
    finally:
        b.close()


TOKEN = "SYNTHETIC-TOKEN-4f9c2e71d0"      # a stand-in credential: must never reach the install's output


def test_no_secrets_in_output():
    """R2: a server that isn't metacomputer's is left exactly as it is, byte for byte, and described by its program's
    name and how many arguments it has, or its transport: a credential in an argument, a variable or a URL never
    reaches the output (at install, with --yes, at uninstall, and for a project server that shadows it)."""
    remote = {"type": "stdio", "command": "npx", "args": ["-y", "mcp-remote", f"https://mcp.example.invalid/mcp?token={TOKEN}"],
              "env": {}}
    b = Box()
    try:
        def clean(p, why):
            out = p.stdout + p.stderr
            check(f"{why}: the token in its arguments isn't in the output", TOKEN not in out, out[-600:])
            return out
        for extra in ((), ("--yes",)):
            b.seed(mcp={"metacomputer": dict(remote)})
            before = open(os.path.join(b.home, ".claude.json"), "rb").read()
            p, calls = b.install("--only-claude", *extra)
            out = clean(p, f"a server running npx mcp-remote URL?token=… ({' '.join(extra) or 'no terminal'})")
            check("…it's left exactly as it is (nothing removed or added), named by its program and how many arguments it "
                  "has, with the command to remove it yourself; exit 1", p.returncode == 1
                  and "npx with 3 arguments (not shown)" in out and "claude mcp remove metacomputer -s user" in out
                  and open(os.path.join(b.home, ".claude.json"), "rb").read() == before
                  and not ran(calls, "mcp", "remove") and not ran(calls, "mcp", "add") and not ran(calls, "mcp", "add-json"),
                  (calls, out[-500:]))
        b.seed(mkts={"metacomputer": {"source": "directory", "path": LAB}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}}, mcp={"metacomputer": dict(remote)})
        os.makedirs(os.path.dirname(b.cmd), exist_ok=True)
        if not os.path.islink(b.cmd):
            os.symlink(CLI, b.cmd)
        p, _ = b.install("--uninstall")
        out = clean(p, "--uninstall, with that server there")
        check("…it stays, said by its program's name", "stays: it runs npx with 3 arguments (not shown)" in out
              and b.user_cfg()["metacomputer"] == remote, out[-400:])
        keyed = {"type": "stdio", "command": "/opt/vendor/bin/server", "args": ["--key", TOKEN], "env": {"API_KEY": TOKEN}}
        b.seed(mcp={"metacomputer": dict(keyed)})
        for extra in ((), ("--yes",)):
            p, _ = b.install("--only-claude", *extra)
            out = clean(p, f"a server with the token in an argument and a variable ({' '.join(extra) or 'as is'})")
            check("…left as it is, named as server with variables of its own", p.returncode == 1
                  and "server with 2 arguments (not shown), with variables of its own" in out
                  and "/opt/vendor" not in out, out[-400:])
        proj = os.path.join(b.t, "proj")
        os.makedirs(proj)
        b.seed(project={proj: {"metacomputer": dict(remote)}})
        p, _ = b.install("--only-claude", cwd=proj)
        out = clean(p, "a project server with the token, shadowing the user-scope one")
        check("…reported by its scope and program, never changed", p.returncode == 0 and "from its Project config" in out
              and "it runs npx" in out and b.registered(), out[-400:])
    finally:
        b.close()


def test_switch_marketplace():
    """R1: another checkout's marketplace, whose older plugin is what serves the tools, is replaced only once the
    user-scope server is registered and read back. When the registration fails it stays, with its plugin and server."""
    b = Box()
    try:
        co = old_layout_checkout(b.t)
        b.seed(mkts={"metacomputer": {"source": "directory", "path": co}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        os.makedirs(os.path.dirname(b.cmd))
        os.symlink(os.path.join(co, "bin", "metacomputer"), b.cmd)
        check("another checkout's plugin brings the only server", b.sources() == ["plugin"], b.sources())
        p, calls = b.install("--only-claude", "--switch", STUB_FAIL_MCP_ADD="1")
        check("--only-claude --switch whose add fails: that checkout's marketplace and plugin stay (its server too), no "
              "marketplace removed or added, exit 1", p.returncode == 1 and b.mkts() == {"metacomputer": co}
              and "metacomputer@metacomputer" in b.plugins() and b.sources() == ["plugin"]
              and not ran(calls, "plugin", "marketplace", "remove") and not ran(calls, "plugin", "marketplace", "add")
              and "left in place until the MCP" in p.stdout, (b.sources(), calls, p.stdout[-500:]))
        p, calls = b.install("--only-claude", "--switch")
        check("…and when it can be registered: the server first, then that marketplace replaced by this one; one server",
              p.returncode == 0 and b.mkts() == {"metacomputer": LAB} and "metacomputer@metacomputer" in b.plugins()
              and b.sources() == ["user"]
              and calls.index(["plugin", "marketplace", "remove", "metacomputer"])
              > calls.index(["mcp", "add", "-s", "user", "metacomputer", b.cmd, "mcp-lazy"]), (calls, p.stdout[-500:]))
        b.seed(mkts={"metacomputer": {"source": "directory", "path": co}},
               plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}}, mcp={"metacomputer": [b.cmd, "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_MKT_LIST="exit", STUB_MKT_LIST_WHEN="after-remove")
        check("…and when the marketplaces can't be read after removing that one: not reported as removed, exit 1",
              p.returncode == 1 and "removed the marketplace" not in p.stdout and "whether it's" in p.stdout
              and not ran(calls, "plugin", "marketplace", "add"), (calls, p.stdout[-400:]))
    finally:
        b.close()


def test_switch_reads_plugin_first():
    """F2 and F8: before another checkout's marketplace is switched, its plugin is read. One turned off stays off: no
    server is registered over it and the marketplace stays. One whose state can't be read stops the switch (nothing
    changed, exit 1). No message names that checkout's folder."""
    b = Box()
    try:
        co = old_layout_checkout(b.t)
        mkt = {"metacomputer": {"source": "directory", "path": co}}
        b.seed(mkts=mkt, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": False}})
        p, calls = b.install("--only-claude")
        out = p.stdout + p.stderr
        check("another checkout's plugin, turned off: left off, its marketplace stays, no server registered over it",
              p.returncode == 0 and b.mkts() == {"metacomputer": co}
              and b.plugins()["metacomputer@metacomputer"]["enabled"] is False and b.entry() is None
              and not mutations(calls) and not ran(calls, "plugin", "install") and "turned off; left so" in out
              and "isn't added either" in out and co not in out, (calls, out[-500:]))
        for bad in ("exit", "errors", "noenabled"):
            b.seed(mkts=mkt, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
            p, calls = b.install("--only-claude", STUB_PLUGIN_LIST=bad)
            out = p.stdout + p.stderr
            check(f"another checkout's marketplace, plugin list {bad}: no switch and no server (nothing changed), said, "
                  "exit 1", p.returncode == 1 and b.mkts() == {"metacomputer": co} and b.entry() is None
                  and not mutations(calls) and not ran(calls, "plugin", "marketplace", "add")
                  and "couldn't read the installed plugins" in out and co not in out, (calls, out[-500:]))
        b.seed(mkts=mkt, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
        p, calls = b.install("--only-claude")
        out = p.stdout + p.stderr
        check("…and switched (on): the messages say another checkout's, never its folder", p.returncode == 0
              and "which was another checkout's" in out and co not in out, out[-500:])
    finally:
        b.close()


def test_marketplace_location_unprinted():
    """F8: a marketplace named metacomputer from another source is refused by its name alone: its location (a "repo" or
    URL with a credential in it, whatever its source label says) never reaches the output, at install or uninstall."""
    for src in ({"source": "github", "repo": f"https://someone:{TOKEN}@git.example.invalid/x/metacomputer.git"},
                {"source": "git", "url": f"https://someone:{TOKEN}@git.example.invalid/x/metacomputer.git"},
                {"source": "url", "url": f"https://example.invalid/marketplace.json?token={TOKEN}"}):
        b = Box()
        try:
            b.seed(mkts={"metacomputer": dict(src)})
            p, _ = b.install("--only-claude")
            out = p.stdout + p.stderr
            check(f"a {src['source']}-labelled marketplace with a credential in its location: refused by name, the "
                  "credential isn't in the output", p.returncode == 0 and "NOT installed" in out and "from another source" in out
                  and TOKEN not in out and "example.invalid" not in out, out[-500:])
            b.seed(mkts={"metacomputer": dict(src)}, plugins={"metacomputer@metacomputer": {"scope": "user", "enabled": True}})
            p, _ = b.install("--uninstall")
            check("…nor at --uninstall", TOKEN not in p.stdout + p.stderr, (p.stdout + p.stderr)[-400:])
        finally:
            b.close()


def test_retirement_unknown():
    """R6: retirement goes by explicit present/absent/error answers. A plugin or marketplace list that can't be read,
    before or after an uninstall, is never taken for "gone": the plugin and marketplace stay, the run ends with exit
    status 1 (incomplete). An old server name whose owner can't be read isn't skipped as if it weren't there."""
    here = {"metacomputer": {"source": "directory", "path": LAB}}
    mine = {"metacomputer@metacomputer": {"scope": "user", "enabled": True}}
    b = Box()
    try:
        b.seed(mkts=here, plugins=mine)
        p, calls = b.install("--only-claude", "--no-plugin", STUB_PLUGIN_LIST="exit")
        check("--no-plugin whose plugin list fails from the start: no uninstall, the marketplace stays, said, exit 1",
              p.returncode == 1 and not ran(calls, "plugin", "uninstall") and not ran(calls, "plugin", "marketplace", "remove")
              and "metacomputer@metacomputer" in b.plugins() and "couldn't read" in p.stdout, (calls, p.stdout[-400:]))
        b.seed(mkts=here, plugins=mine)
        p, calls = b.install("--only-claude", "--no-plugin", STUB_FAIL_PLUGIN_UNINSTALL="1", STUB_PLUGIN_LIST="exit",
                             STUB_PLUGIN_LIST_WHEN="after-uninstall")
        check("--no-plugin whose uninstall fails and whose check after it can't be read: no claim it's gone, the "
              "marketplace stays, exit 1", p.returncode == 1 and "uninstalled the plugin" not in p.stdout
              and "whether it's gone is unknown" in p.stdout and not ran(calls, "plugin", "marketplace", "remove")
              and "metacomputer@metacomputer" in b.plugins() and b.mkts() == {"metacomputer": LAB}, p.stdout[-400:])
        for env in ({"STUB_PLUGIN_LIST": "exit"}, {"STUB_MKT_LIST": "exit"}, {"STUB_MKT_LIST": "shape"}):
            b.seed(mkts=here, plugins=mine)
            p, calls = b.install("--uninstall", **env)
            check(f"--uninstall with {env}: the plugin and marketplace stay, said, exit 1", p.returncode == 1
                  and not ran(calls, "plugin", "uninstall") and not ran(calls, "plugin", "marketplace", "remove")
                  and "metacomputer@metacomputer" in b.plugins() and "Not everything was removed" in p.stderr, p.stdout[-400:])
        b.seed(mkts=here, plugins=mine)
        p, calls = b.install("--uninstall", STUB_MKT_LIST="exit", STUB_MKT_LIST_WHEN="after-remove")
        check("--uninstall whose marketplace check after the removal can't be read: no claim it's gone, exit 1",
              p.returncode == 1 and "removed the marketplace" not in p.stdout and "whether it's gone is unknown" in p.stdout,
              p.stdout[-400:])
        b.seed(mkts=here, plugins=mine, mcp={"cua-linux": [os.path.join(LAB, "bin", "claude-vc"), "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_FAIL_MCP_GET_FOR="cua-linux")
        check("an old server name whose lookup fails: kept, said, not counted as done (exit 1)", p.returncode == 1
              and "cua-linux" in b.user_cfg() and b.registered() and not ran(calls, "mcp", "remove")
              and "couldn't read the old MCP server name cua-linux" in p.stdout, (calls, p.stdout[-400:]))
    finally:
        b.close()


def test_incomplete_marker():
    """R7, F4 and F5: --only-claude and --only-agents record a Claude Code step that didn't finish in installed.json's
    "incomplete" (so `metacomputer update` runs the install again: test_update), and a run whose step finishes clears it;
    the rest of the record stays as it was. Without a record, none is made (update installs then anyway). It's recorded
    right after Claude Code's step, before Codex's (which could stop the run). A run that doesn't set Claude Code up (no
    claude command on PATH, --no-claude) leaves an unfinished one recorded, and a full install's last step goes by the
    same rule."""
    b = Box()
    try:
        stamp = os.path.join(b.home, ".local", "share", "metacomputer", "installed.json")
        p, _ = b.install("--only-claude", STUB_FAIL_MCP_ADD="1")
        check("no installed.json: a failed --only-claude makes none", p.returncode == 1 and not os.path.exists(stamp))
        base = {"version": "v0.4.4", "tag": "v0.4.4", "commit": "0" * 40, "checkout": LAB, "incomplete": ["codex"]}
        write(stamp, json.dumps(base))
        p, _ = b.install("--only-claude", STUB_FAIL_MCP_ADD="1")
        got = json.load(open(stamp))
        check("--only-claude that fails: recorded as incomplete, the rest kept", p.returncode == 1
              and got == dict(base, incomplete=["codex", "claude"]), got)
        p, _ = b.install("--only-claude")
        got = json.load(open(stamp))
        check("…and the next run that finishes clears it", p.returncode == 0 and got == base, got)
        for args in (("--only-agents", "--no-codex"), ("--only-claude",)):
            write(stamp, json.dumps(dict(base, incomplete=[])))
            p, _ = b.install(*args, STUB_FAIL_MCP_GET="1")
            got = json.load(open(stamp))
            check(f"{' '.join(args)} whose lookup fails: recorded as incomplete", p.returncode == 1
                  and got.get("incomplete") == ["claude"], (got, p.stdout[-300:]))
            p, _ = b.install(*args)
            got = json.load(open(stamp))
            check("…cleared once it finishes", p.returncode == 0 and "incomplete" not in got, got)

        # F4: Claude Code's step fails, then Codex's stops the run (a file where its skills folder goes): already recorded.
        write(stamp, json.dumps(dict(base, incomplete=[])))
        codex = os.path.join(b.bin, "codex")
        write(codex, '#!/bin/sh\ncase "$1 $2" in "mcp get") echo "No MCP server named \'metacomputer\'" >&2; exit 1 ;; esac\nexit 0\n', 0o755)
        write(os.path.join(b.home, ".agents"), "a file where Codex's skills folder goes\n")
        try:
            r = subprocess.run([os.path.join(LAB, "install.sh"), "--only-agents", "--codex"], env=dict(b.env, STUB_FAIL_MCP_GET="1"),
                               capture_output=True, text=True, timeout=300, cwd=b.work, stdin=subprocess.DEVNULL)
        finally:
            os.remove(codex)
            os.remove(os.path.join(b.home, ".agents"))
        got = json.load(open(stamp))
        check("a Claude Code step that fails, then a Codex step that stops the run: recorded before Codex's step ran",
              r.returncode != 0 and "install: stopped" in r.stderr and got.get("incomplete") == ["claude"], (got, r.stderr[-400:]))

        # F5: not set up this run (no claude command on PATH, --no-claude): an unfinished one stays recorded.
        noclaude = {"PATH": f"{os.path.dirname(os.path.realpath(sys.executable))}:/usr/bin:/bin"}
        for args, env in ((("--only-agents", "--no-codex"), noclaude), (("--only-claude",), noclaude),
                          (("--only-agents", "--no-claude", "--no-codex"), {})):
            write(stamp, json.dumps(dict(base, incomplete=["claude"])))
            p, _ = b.install(*args, **env)
            got = json.load(open(stamp))
            check(f"{' '.join(args)}{' with no claude command on PATH' if env else ''}: Claude Code isn't set up, so its "
                  "unfinished step stays recorded", got.get("incomplete") == ["claude"], (got, p.stdout[-300:]))
        # A full install's last step (install_incomplete, run here on its own): the same rule.
        src = open(os.path.join(LAB, "install.sh")).read()

        def fn(name):
            i = src.index(f"\n{name}() {{")
            return src[i:src.index("\n}\n", i) + 3]
        more = src[src.index("\nMORE_AGENTS="):].split("\n", 2)[1] + "\n"
        script = ("set -Eeuo pipefail\ntrap 'echo ERR-TRAP >&2' ERR\n" + more + fn("agent_upper") + fn("agent_var")
                  + fn("stamp_incomplete") + fn("install_incomplete") + 'printf "[%s]" "$(install_incomplete)"\n')
        # (CLAUDE_FAILED, CLAUDE_TRIED, CODEX_FAILED, CODEX_TRIED), and the other agents' (FAILED, TRIED): each agent by
        # the same rule
        for recorded, state, others, want in (
                (["claude"], (0, 0, 0, 1), {}, "claude"), (["claude"], (0, 1, 0, 1), {}, ""),
                ([], (1, 1, 0, 1), {}, "claude"), ([], (0, 0, 0, 0), {}, ""), (["codex"], (0, 1, 0, 0), {}, "codex"),
                (["codex"], (0, 1, 0, 1), {}, ""), ([], (1, 1, 1, 1), {}, "claude,codex"),
                (["claude", "codex"], (0, 0, 0, 0), {}, "claude,codex"),
                (["gemini"], (0, 1, 0, 1), {}, "gemini"), (["gemini"], (0, 1, 0, 1), {"GEMINI": (0, 1)}, ""),
                ([], (0, 1, 0, 1), {"OPENCLAW": (1, 1), "HERMES": (0, 1)}, "openclaw"),
                (["hermes", "codex"], (0, 1, 0, 0), {"GEMINI": (1, 1)}, "codex,hermes,gemini")):
            write(stamp, json.dumps(dict(base, incomplete=recorded)))
            env = dict({"HOME": b.home, "PATH": noclaude["PATH"]},
                       **dict(zip(("CLAUDE_FAILED", "CLAUDE_TRIED", "CODEX_FAILED", "CODEX_TRIED"), map(str, state))))
            for a in ("OPENCLAW", "HERMES", "GEMINI"):
                f, t = others.get(a, (0, 0))
                env.update({f"{a}_FAILED": str(f), f"{a}_TRIED": str(t)})
            r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
            check(f"full install's record: earlier {recorded}, this run {state} {others} -> {want or 'nothing'}",
                  r.stdout == f"[{want}]" and "ERR-TRAP" not in r.stderr, (r.stdout, r.stderr))
        check("…and the full install records what install_incomplete says", 'incomplete="$(install_incomplete)"' in src
              and 'MCOMP_INSTALL_INCOMPLETE="$incomplete"' in src)
    finally:
        b.close()


def install_pty(b, args, prompt, before_answer, answer, timeout=120):
    """install.sh at a terminal (a pty): when PROMPT appears, BEFORE_ANSWER() runs, then ANSWER is typed."""
    import fcntl, pty, select, termios, time
    master, slave = pty.openpty()

    def pre():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    if os.path.exists(b.log):
        os.remove(b.log)
    p = subprocess.Popen([os.path.join(LAB, "install.sh"), *args], env=b.env, cwd=b.work, stdin=slave, stdout=slave,
                         stderr=slave, preexec_fn=pre, close_fds=True)
    os.close(slave)
    out, done, end = b"", False, time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not done and prompt.encode() in out:
                done = True
                before_answer()
                os.write(master, answer.encode())
        elif p.poll() is not None:
            break
    if p.poll() is None:
        p.kill()
    p.wait()
    os.close(master)
    calls = [json.loads(x) for x in open(b.log)] if os.path.exists(b.log) else []
    return p.returncode, out.decode(errors="replace"), calls


def test_entry_changed():
    """R3: metacomputer's own entry is classified from two reads of the configuration around `claude mcp get`, and read
    again right before it's removed: a change in between (another Claude Code adding a variable, while it was read or
    while you answered) leaves it as it is, exit 1."""
    b = Box()
    try:
        b.seed(mcp={"metacomputer": [CLI, "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_MUTATE_ON_GET="1")
        check("a variable added while `claude mcp get` reads it: not classified (\"changed while it was being read\"), "
              "nothing removed or added, exit 1", p.returncode == 1 and "changed while it was being read" in p.stdout
              and not ran(calls, "mcp", "remove") and not ran(calls, "mcp", "add")
              and b.user_cfg()["metacomputer"].get("env") == {"ADDED_ELSEWHERE": "1"}, (calls, p.stdout[-400:]))
        other = other_checkout(b.t)
        theirs = {"type": "stdio", "command": os.path.join(other, "bin", "metacomputer"), "args": ["mcp-lazy"], "env": {}}
        b.seed(mcp={"metacomputer": dict(theirs)})

        def another_claude():
            cfg_path = os.path.join(b.home, ".claude.json")
            cfg = json.load(open(cfg_path))
            cfg["mcpServers"]["metacomputer"]["env"] = {"ADDED_ELSEWHERE": "1"}
            json.dump(cfg, open(cfg_path, "w"))
        rc, out, calls = install_pty(b, ["--only-claude"], "Point it at this one?", another_claude, "y\n")
        check("a yes to moving another checkout's registration here, but the entry changed while you answered: left as it "
              "is (the change kept), nothing removed, exit 1", rc == 1 and "changed since it was read" in out
              and not ran(calls, "mcp", "remove")
              and b.user_cfg()["metacomputer"] == dict(theirs, env={"ADDED_ELSEWHERE": "1"}), (calls, out[-500:]))
        b.seed(mcp={"metacomputer": dict(theirs)})
        rc, out, calls = install_pty(b, ["--only-claude"], "Point it at this one?", lambda: None, "y\n")
        check("…and unchanged: moved here", rc == 0 and b.registered() and ran(calls, "mcp", "remove"), out[-400:])
    finally:
        b.close()


def changing_python(b):
    """A python3 first on PATH that, when install.sh reads an entry again right before removing it (user-recheck), first
    plays another Claude Code adding a variable to the user-scope entry metacomputer; anything else it just runs."""
    shim = os.path.join(b.bin, "python3")
    write(shim, "#!/bin/sh\n"
                'case " $* " in *" user-recheck "*) "%s" -c \'import json, os; p = os.path.join(os.environ["HOME"], ".claude.json"); '
                'c = json.load(open(p)); c["mcpServers"]["metacomputer"]["env"] = {"ADDED_ELSEWHERE": "1"}; '
                'json.dump(c, open(p, "w"))\' ;; esac\nexec "%s" "$@"\n' % (sys.executable, sys.executable), 0o755)
    return shim


def test_uninstall_server_first():
    """F6 and F11: --uninstall removes metacomputer's own server, read again and checked, before the command it runs.
    When the entry changed since it was read, or its removal can't be confirmed, the server stays and so does the
    command (~/.local/bin/metacomputer and mcomp), so it never runs a program that's gone; exit 1. A registration that
    stays because it isn't metacomputer's keeps the command too when it runs it."""
    b = Box()
    try:
        b.install("--only-claude")
        shim = changing_python(b)
        try:
            p, calls = b.install("--uninstall")
        finally:
            os.remove(shim)
        check("--uninstall where the entry changes right before its removal: the server stays (the change kept), and so "
              "does the command it runs; said, exit 1", p.returncode == 1 and "changed since it was read" in p.stdout
              and not ran(calls, "mcp", "remove") and b.user_cfg()["metacomputer"].get("env") == {"ADDED_ELSEWHERE": "1"}
              and b.link(".local/bin/metacomputer") == CLI and b.link(".local/bin/mcomp") == CLI
              and "kept" in p.stdout, (calls, p.stdout[-500:]))
        p, calls = b.install("--uninstall", STUB_FAIL_MCP_REMOVE="1")
        check("--uninstall whose removal doesn't take: the server and the command stay, exit 1", p.returncode == 1
              and "metacomputer" in b.user_cfg() and b.link(".local/bin/metacomputer") == CLI, p.stdout[-400:])
        p, calls = b.install("--uninstall")
        check("…and when it can be removed: the server, then the command", p.returncode == 0 and not b.user_cfg()
              and b.link(".local/bin/metacomputer") is None and b.link(".local/bin/mcomp") is None, p.stdout[-400:])
        # F11: an entry that stays because it isn't metacomputer's own, and runs the command link: the links stay too.
        b.install("--only-claude")
        custom = {"type": "stdio", "command": b.cmd, "args": ["--vm", "other", "mcp-lazy"], "env": {}}
        b.claude("mcp", "remove", "metacomputer", "-s", "user")
        b.add_user(custom)
        p, calls = b.install("--uninstall")
        check("--uninstall with a registration of yours that runs ~/.local/bin/metacomputer --vm other mcp-lazy: it stays, "
              "and so do the command links it runs (said); exit 0", p.returncode == 0
              and b.user_cfg()["metacomputer"] == custom and b.link(".local/bin/metacomputer") == CLI
              and b.link(".local/bin/mcomp") == CLI and "kept" in p.stdout and not ran(calls, "mcp", "remove"),
              (calls, p.stdout[-500:]))
    finally:
        b.close()


def test_owned_replace_lookup_fails():
    """F1: once metacomputer's own registration (here an older path of this checkout's) is removed, the add always
    runs, also when the lookup after the removal fails; it's then read back. When that read can't confirm it, the run
    says so with the exact command that adds it, and the step counts as unfinished."""
    legacy = os.path.join(LAB, "bin", "claude-vc")
    b = Box()
    try:
        b.seed(mcp={"metacomputer": [legacy, "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_FAIL_GET_AFTER_REMOVE="once")
        check("an older path of this checkout's, the lookup after its removal failing once: the add still runs, and the "
              "read-back confirms it (exit 0)", p.returncode == 0 and b.registered()
              and calls.index(["mcp", "add", "-s", "user", "metacomputer", b.cmd, "mcp-lazy"])
              > calls.index(["mcp", "remove", "metacomputer", "-s", "user"]), (calls, p.stdout[-500:]))
        b.seed(mcp={"metacomputer": [legacy, "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_FAIL_GET_AFTER_REMOVE="always")
        check("…every lookup after it failing: the add still runs; the run says whether it's there is unknown, gives the "
              "exact command that adds it, and fails (exit 1)", p.returncode == 1 and b.registered()
              and ran(calls, "mcp", "add") and "whether it's there is unknown" in p.stdout
              and "claude mcp add -s user metacomputer ~/.local/bin/metacomputer mcp-lazy" in p.stdout
              and "was left as it is" not in p.stdout, (calls, p.stdout[-500:]))
        b.seed(mcp={"metacomputer": [legacy, "mcp-lazy"]})
        p, calls = b.install("--only-claude", STUB_FAIL_MCP_ADD="1")
        check("…an add that fails after the removal: the server is missing, said plainly, with the command; exit 1",
              p.returncode == 1 and "metacomputer" not in b.user_cfg() and "has no MCP server metacomputer now" in p.stdout
              and "claude mcp add -s user metacomputer ~/.local/bin/metacomputer mcp-lazy" in p.stdout, p.stdout[-500:])
    finally:
        b.close()


def test_claude_mcp_unit():
    """lib/claude_mcp.py on its own: the configuration file Claude Code picks (R4), a byte-order mark (R5), the read
    right before a removal (R3) and the words for a server that isn't metacomputer's (R2)."""
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import claude_mcp
    t = tempfile.mkdtemp(prefix="mcomp-cmcp-")
    try:
        def fresh(name):
            home = os.path.join(t, name)
            os.makedirs(os.path.join(home, ".claude"))
            return home, os.path.join(home, ".claude", ".config.json"), os.path.join(home, ".claude.json")
        full = json.dumps({"mcpServers": {"probe": {"type": "stdio", "command": "/bin/echo", "args": [], "env": {}}}})
        cases = (("only .claude.json", None, full, "modern"),
                 ("a populated legacy .config.json and no .claude.json", full, None, "legacy"),
                 ("an empty legacy file beside a populated .claude.json", "", full, "modern"),
                 ("a folder named .config.json beside a populated .claude.json", "dir", full, "modern"),
                 ("both populated", full, full, "legacy"),
                 ("a populated legacy file and an empty .claude.json", full, "", "legacy"),
                 ("an empty read-only legacy file and no .claude.json", "ro", None, "modern"),
                 ("an empty writable legacy file and no .claude.json", "", None, "legacy"),
                 ("a dangling link named .config.json", "dangling", full, "modern"))
        for i, (why, legacy, modern, want) in enumerate(cases):
            home, lp, mp = fresh(f"h{i}")
            if legacy == "dir":
                os.makedirs(lp)
            elif legacy == "dangling":
                os.symlink(os.path.join(t, "nowhere.json"), lp)
            elif legacy == "ro":
                write(lp, "", 0o444)
            elif legacy is not None:
                write(lp, legacy)
            if modern is not None:
                write(mp, modern)
            got = claude_mcp.config_path({"HOME": home})
            check(f"config file: {why} -> {want}", got == (lp if want == "legacy" else mp), got)
            if want == "modern" and modern:
                servers, why_not = claude_mcp.read_servers({"HOME": home})
                check(f"…and it's read from there ({why})", servers is not None and "probe" in servers, why_not)
        home, lp, mp = fresh("cfgdir")
        cfg = os.path.join(t, "cfg")
        check("CLAUDE_CONFIG_DIR: its .claude.json", claude_mcp.config_path({"HOME": home, "CLAUDE_CONFIG_DIR": cfg})
              == os.path.join(cfg, ".claude.json"))
        write(os.path.join(cfg, ".config.json"), full)
        check("…or its .config.json, when that's the legacy file in use",
              claude_mcp.config_path({"HOME": home, "CLAUDE_CONFIG_DIR": cfg}) == os.path.join(cfg, ".config.json"))
        check("CLAUDE_CODE_CUSTOM_OAUTH_URL: .claude-custom-oauth.json, as Claude Code names it",
              claude_mcp.config_path({"HOME": home, "CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://x.invalid"})
              == os.path.join(home, ".claude-custom-oauth.json"))

        home, lp, mp = fresh("bom")
        with open(mp, "w", encoding="utf-8-sig") as f:
            f.write(full)
        servers, why_not = claude_mcp.read_servers({"HOME": home})
        check("a configuration that starts with a UTF-8 byte-order mark is read (Claude Code reads it)",
              servers is not None and "probe" in servers, why_not)

        home, lp, mp = fresh("recheck")
        e = {"type": "stdio", "command": "/bin/echo", "args": ["a b"], "env": {}}
        write(mp, json.dumps({"mcpServers": {"probe": e}}))
        env = {"HOME": home}
        d = claude_mcp.digest(e)
        got = claude_mcp.recheck("probe", d, env)
        check("the read before a removal: the same entry -> same (and nothing of the entry in its answer)", got == "same", got)
        write(mp, json.dumps({"mcpServers": {"probe": dict(e, env={"K": "v"})}}))
        check("…a variable added since -> changed", claude_mcp.recheck("probe", d, env) == "changed")
        write(mp, json.dumps({"mcpServers": {}}))
        check("…gone -> changed", claude_mcp.recheck("probe", d, env) == "changed")
        write(mp, "{not json")
        check("…unreadable -> error", claude_mcp.recheck("probe", d, env).startswith("error" + claude_mcp.US))

        res = {"kind": "stdio", "entry": {"command": "/opt/x/bin/npx", "args": ["mcp-remote", f"https://h.invalid/?t={TOKEN}"],
                                          "env": {"K": TOKEN}}}
        w = claude_mcp.words(res, env, "foreign")
        check("words for someone else's server: its program, how many arguments, that it has variables; no value",
              w == "npx with 2 arguments (not shown), with variables of its own" and TOKEN not in w, w)
        res = {"kind": "stdio", "entry": {"command": f"/opt/{TOKEN}/bin/{TOKEN}-{TOKEN}", "args": []}}
        w = claude_mcp.words(res, env, "foreign")
        check("…a program name that's more than a plain word isn't printed either", TOKEN not in w
              and w == "a local command, without arguments", w)
        w = claude_mcp.words({"kind": "http", "entry": {"type": "http", "url": f"https://h.invalid/?t={TOKEN}"}}, env, "foreign")
        check("…an HTTP server: its transport only", w == "an MCP server of type http, not a local command", w)
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_config_choice_real():
    """R4 and R5 against the real `claude` (a throwaway HOME and CLAUDE_CONFIG_DIR): for each arrangement of the legacy
    `.config.json` and `.claude.json`, the file claude_mcp reads is the one Claude Code uses (`claude mcp get` shows that
    file's server), and a byte-order mark doesn't stop either of them."""
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import claude_mcp
    entry = lambda c: {"type": "stdio", "command": c, "args": ["probe"], "env": {}}
    for why, legacy, modern, want in (
            ("an empty legacy file beside a populated .claude.json", "", entry("/bin/echo"), "/bin/echo"),
            ("a folder named .config.json beside a populated .claude.json", "dir", entry("/bin/echo"), "/bin/echo"),
            ("both populated", entry("/bin/cat"), entry("/bin/echo"), "/bin/cat"),
            (".claude.json with a byte-order mark", None, entry("/bin/echo"), "/bin/echo")):
        b = Box(real=True)
        try:
            cfg = b.env["CLAUDE_CONFIG_DIR"]
            os.makedirs(cfg, exist_ok=True)
            lp, mp = os.path.join(cfg, ".config.json"), os.path.join(cfg, ".claude.json")
            if legacy == "dir":
                os.makedirs(lp)
            elif legacy == "":
                write(lp, "")
            elif legacy is not None:
                write(lp, json.dumps({"mcpServers": {"probe": legacy}}))
            with open(mp, "w", encoding="utf-8-sig" if legacy is None else "utf-8") as f:
                f.write(json.dumps({"mcpServers": {"probe": modern}}))
            p = b.claude("mcp", "get", "probe")
            shown_cmd = next((ln.split(":", 1)[1].strip() for ln in p.stdout.splitlines() if ln.startswith("  Command:")), None)
            res = claude_mcp.user_entry(os.path.join(b.bin, "claude"), "probe", dict(b.env, PATH=b.env["PATH"]))
            check(f"real claude: {why}: Claude Code and claude_mcp read the same file",
                  shown_cmd == want and res["kind"] == "stdio" and res["entry"]["command"] == want,
                  (shown_cmd, res.get("why"), p.stderr[-200:]))
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


# --- Codex's marketplace and OpenAI's plugin directory ---------------------------------------------------------------
def test_codex_openai():
    """The same plugin/ folder for Codex (.agents/plugins/marketplace.json and plugin/.codex-plugin/plugin.json, which
    scripts/sync-plugin.py writes: the --check above covers them) and its package for OpenAI's plugin directory
    (scripts/build-openai-plugin.py): skills only, Desktop only; the learn skill in place of the old command; the
    launcher in Codex's plugin cache."""
    mkt = json.load(open(os.path.join(LAB, ".agents", "plugins", "marketplace.json")))
    codex = json.load(open(os.path.join(LAB, "plugin", ".codex-plugin", "plugin.json")))
    claude = json.load(open(os.path.join(LAB, "plugin", ".claude-plugin", "plugin.json")))
    entry = (mkt.get("plugins") or [{}])[0]
    check("Codex marketplace: metacomputer, one plugin at ./plugin (a local source from the repository's root), "
          "available, authenticated on install", mkt.get("name") == "metacomputer" and len(mkt["plugins"]) == 1
          and entry.get("name") == codex.get("name") == claude.get("name") == "metacomputer"
          and entry.get("source") == {"source": "local", "path": "./plugin"}
          and entry.get("policy") == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, mkt)
    paths = [codex["skills"]] if isinstance(codex.get("skills"), str) else list(codex.get("skills") or [])
    paths += [codex["interface"].get("composerIcon"), codex["interface"].get("logo")]
    check("Codex's manifest names both skill folders (its list replaces the default skills/), and its paths start with "
          "./ and are in plugin/", codex.get("skills") == ["./skills/", "./.claude/skills/"]
          and all(isinstance(x, str) and x.startswith("./") and ".." not in x
                  and os.path.exists(os.path.join(LAB, "plugin", x[2:])) for x in paths), paths)
    check("Codex's manifest declares no MCP server, hooks, apps or commands, and no version (as Claude Code's)",
          not {"mcpServers", "hooks", "apps", "commands", "version"} & set(codex), sorted(codex))
    check("Codex's manifest speaks of the agent, not Claude", "Claude" not in json.dumps(codex), codex.get("description"))
    learn_md = os.path.join(LAB, "plugin", "skills", "learn", "SKILL.md")
    text = open(learn_md).read() if os.path.isfile(learn_md) else ""
    fm = text.split("---")[1] if text.startswith("---") else ""
    check("/metacomputer:learn is a skill (plugin/skills/learn): named learn, a quoted one-line description, the old "
          "command gone", re.search(r"^name: learn$", fm, re.M) and re.search(r'^description: ".+"$', fm, re.M)
          and not os.path.exists(os.path.join(LAB, "plugin", "commands")), fm[:300])
    allowed = re.search(r"^allowed-tools: (.*)$", fm, re.M)
    check("...it may run only learn report, list and review without asking (in Claude Code), and has no $ARGUMENTS "
          "(Codex doesn't fill it)", allowed and set(re.findall(r"Bash\(([^)]*)\)", allowed.group(1))) == {
              "~/.local/bin/metacomputer learn report:*", "~/.local/bin/metacomputer learn list:*",
              "~/.local/bin/metacomputer learn review:*"} and "$ARGUMENTS" not in text, allowed and allowed.group(1))
    check("...and approves only the IDs the user names", "with exactly the IDs the user named" in text)
    skills_text = "".join(open(os.path.join(LAB, "skills", "metacomputer", f)).read()
                          for f in os.listdir(os.path.join(LAB, "skills", "metacomputer")))
    check("the skills speak of the agent where they meant any agent ('this file says Claude' is gone)",
          "this file says Claude" not in skills_text and "What Claude does" not in skills_text
          and "tell Claude to carry on" not in skills_text, None)
    # The OpenAI package
    build = os.path.join(LAB, "scripts", "build-openai-plugin.py")
    p = subprocess.run([sys.executable, build, "--check"], capture_output=True, text=True, timeout=120)
    check("build-openai-plugin.py --check: the package builds and passes its checks", p.returncode == 0,
          p.stdout + p.stderr)
    # Their sources are regular files only: a linked skill folder, a linked icon (or license, or notices) would bring
    # in what it points to, so the package build and sync-plugin's check refuse each (in a copy of the files they read).
    t = tempfile.mkdtemp(prefix="mcomp-openai-links-")
    try:
        lab = os.path.join(t, "lab")
        for rel in ("scripts/build-openai-plugin.py", "scripts/sync-plugin.py", "get.sh", "LICENSE",
                    "THIRD_PARTY_NOTICES.md", "gemini-extension.json", "lib/strictargs.py"):   # (their command lines' rules)
            os.makedirs(os.path.dirname(os.path.join(lab, rel)), exist_ok=True)
            shutil.copy2(os.path.join(LAB, rel), os.path.join(lab, rel))
        for rel in ("plugin", "agent-plugin", ".agents", "skills/metacomputer", ".claude/skills/cua-driver"):
            shutil.copytree(os.path.join(LAB, rel), os.path.join(lab, rel), symlinks=True)
        outside = os.path.join(t, "outside")
        os.makedirs(outside)

        def both():
            return [subprocess.run([sys.executable, os.path.join(lab, "scripts", x), "--check"], capture_output=True,
                                   text=True, timeout=120) for x in ("build-openai-plugin.py", "sync-plugin.py")]
        ok = both()
        check("(a copy of the files the package and the plugins are made from builds and checks clean)",
              [r.returncode for r in ok] == [0, 0], [r.stdout[-300:] + r.stderr[-300:] for r in ok])
        for what, rel in (("a linked icon", "plugin/icon.png"), ("a linked skills folder", "plugin/skills/learn"),
                          ("a linked license", "plugin/LICENSE")):
            src, away = os.path.join(lab, rel), os.path.join(outside, os.path.basename(rel))
            os.rename(src, away)
            os.symlink(away, src)
            code = ("import importlib.util, sys\nspec = importlib.util.spec_from_file_location('b', sys.argv[1])\n"
                    "b = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(b)\n"
                    "try:\n    b.contents('1.0.0')\n    print('PACKAGED')\nexcept SystemExit as e:\n    print(e)\n")
            try:
                got = both()
                own = subprocess.run([sys.executable, "-c", code, os.path.join(lab, "scripts", "build-openai-plugin.py")],
                                     capture_output=True, text=True, timeout=120)
            finally:
                os.remove(src)
                os.rename(away, src)
            check(f"{what} ({rel}) is refused by build-openai-plugin.py --check and by sync-plugin.py --check, and by "
                  "the package's own reading of its sources (nothing is packaged from it)",
                  [r.returncode for r in got] == [1, 1] and "is a link" in got[0].stdout + got[0].stderr
                  and "is a link" in got[1].stdout and "is a link" in own.stdout and "PACKAGED" not in own.stdout,
                  [(r.returncode, (r.stdout + r.stderr)[-300:]) for r in got] + [own.stdout[-200:] + own.stderr[-200:]])
    finally:
        shutil.rmtree(t, ignore_errors=True)
    t = tempfile.mkdtemp(prefix="mcomp-openai-")
    try:
        a, b = os.path.join(t, "a.zip"), os.path.join(t, "b.zip")
        for out in (a, b):
            subprocess.run([sys.executable, build, "--out", out, "--version", "9.8.7"], capture_output=True, text=True,
                           timeout=120)
        z = zipfile.ZipFile(a)
        names = z.namelist()
        m = json.loads(z.read("metacomputer/.codex-plugin/plugin.json"))
        check("the same sources make the same ZIP", os.path.isfile(a) and open(a, "rb").read() == open(b, "rb").read())
        check("one folder, metacomputer/, with the manifest, the skills metacomputer, learn and cua-driver, the icon, the "
              "license and the notices, and nothing else (no launcher, get.sh or commands)",
              {n.split("/", 1)[0] for n in names} == {"metacomputer"}
              and {n.split("/")[2] for n in names if n.startswith("metacomputer/skills/")} == {"metacomputer", "learn",
                                                                                               "cua-driver"}
              and {n.split("/")[1] for n in names} == {".codex-plugin", "skills", "assets", "LICENSE",
                                                       "THIRD_PARTY_NOTICES.md"}, sorted(set(n.split("/")[1] for n in names)))
        check("its manifest: the version given, skills ./skills/, requires_local_executor true (Desktop only), the icon "
              "under assets/, no MCP server", m.get("version") == "9.8.7" and m.get("skills") == "./skills/"
              and m.get("requires_local_executor") is True and m["interface"]["logo"] == "./assets/icon.png"
              and "mcpServers" not in m, m)
        urls = {k: m["interface"].get(k) for k in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL")}
        check("...and all four listing URLs, https: the website, support, Metavert's privacy policy and terms of service",
              urls["privacyPolicyURL"] == "https://www.metavert.io/privacy-policy"
              and urls["termsOfServiceURL"] == "https://www.metavert.io/terms-of-service"
              and all(isinstance(v, str) and v.startswith("https://") for v in urls.values()), urls)
        check("every entry a regular file", all(((i.external_attr >> 16) & 0o170000) == 0o100000 for i in z.infolist()))
        pack = z.read("metacomputer/skills/cua-driver/SKILL.md").decode()
        check("the driver pack's links point at ../metacomputer/ in the package", "../../../skills/metacomputer/" not in pack
              and "../metacomputer/" in pack)
        same = all(z.read(f"metacomputer/skills/metacomputer/{f}") == open(os.path.join(LAB, "skills", "metacomputer", f), "rb").read()
                   for f in os.listdir(os.path.join(LAB, "skills", "metacomputer")))
        check("the metacomputer skill in it is the repository's, byte for byte", same)
        # What its checks refuse: a link to nothing, a skill named otherwise, a long description, no
        # requires_local_executor, a link entry
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_openai", build)
        bo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bo)

        def broken(change):
            buf = io.BytesIO()
            with zipfile.ZipFile(a) as src, zipfile.ZipFile(buf, "w") as dst:
                for i in src.infolist():
                    data = change(i, src.read(i))
                    if data is not None:
                        dst.writestr(i, data)
            return bo.problems(buf.getvalue())

        def edit(name, f):
            return lambda i, d: f(d) if i.filename == name else d
        sk = "metacomputer/skills/learn/SKILL.md"
        cases = {
            "a link to nothing": edit(sk, lambda d: d + b"\nSee [this](NOWHERE.md).\n"),
            "a skill named otherwise": edit(sk, lambda d: d.replace(b"name: learn", b"name: other")),
            "a description over 1,024 characters": edit(sk, lambda d: d.replace(b'description: "', b'description: "' + b"x" * 1100)),
            "no requires_local_executor": edit("metacomputer/.codex-plugin/plugin.json",
                                               lambda d: d.replace(b'"requires_local_executor": true', b'"requires_local_executor": false')),
        }
        for what, change in cases.items():
            check(f"its checks refuse {what}", broken(change), what)
        link = zipfile.ZipInfo("metacomputer/skills/learn/evil.md")
        link.external_attr = (0o120777) << 16
        buf = io.BytesIO()
        with zipfile.ZipFile(a) as src, zipfile.ZipFile(buf, "w") as dst:
            for i in src.infolist():
                dst.writestr(i, src.read(i))
            dst.writestr(link, "/etc/passwd")
        check("...and a link entry", any("not a regular file" in x for x in bo.problems(buf.getvalue())))
        # The launcher in Codex's plugin cache: it hands over, and with nothing set up runs only its own get.sh
        home = os.path.join(t, "home")
        root = os.path.join(home, ".codex", "plugins", "cache", "metacomputer", "metacomputer", "local")
        cached = os.path.join(root, "bin", "metacomputer")
        os.makedirs(os.path.dirname(cached))
        shutil.copy2(PLUGIN_CLI, cached)
        write(os.path.join(root, "get.sh"), '#!/bin/bash\necho "GET.SH RAN $*"\n')
        p = run([cached, "setup", "--no-antigravity"], base_env(home))
        check("a copy in Codex's plugin cache (~/.codex/plugins/cache), nothing set up: setup runs its own get.sh with "
              "--confirm, --no-antigravity passed on", p.returncode == 0 and "GET.SH RAN --confirm --no-antigravity" in p.stdout,
              p.stdout + p.stderr)
        alt = os.path.join(t, "cx", "plugins", "cache", "m", "local", "bin", "metacomputer")
        os.makedirs(os.path.dirname(alt))
        shutil.copy2(PLUGIN_CLI, alt)
        fake = os.path.join(t, "checkout", "bin", "metacomputer")
        write(fake, '#!/bin/bash\necho "CHECKOUT $*"\n', 0o755)
        local = os.path.join(home, ".local", "bin", "metacomputer")
        os.makedirs(os.path.dirname(local))
        os.symlink(fake, local)
        p = run([alt, "where"], base_env(home, CODEX_HOME=os.path.join(t, "cx")))
        check("a copy under $CODEX_HOME/plugins/cache hands over to ~/.local/bin/metacomputer", p.stdout.strip() == "CHECKOUT where",
              p.stdout + p.stderr)
        os.remove(local)
        os.symlink(alt, local)
        p = run([cached, "who"], base_env(home, CODEX_HOME=os.path.join(t, "cx")))
        check("...and never to a copy inside Codex's plugin cache (no loop)", p.returncode == 1 and " setup" in p.stderr,
              p.stdout + p.stderr)
    finally:
        shutil.rmtree(t, ignore_errors=True)


test_layout()
test_codex_openai()
test_validate()
test_handoff()
test_setup()
test_install()
test_plugin_only_update()
test_claude_mcp_unit()
if REAL:
    if shutil.which("claude"):
        test_install(real=True)
        test_config_choice_real()
    else:
        print("SKIP --real-claude (claude isn't installed)")
test_prefixes()
print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
