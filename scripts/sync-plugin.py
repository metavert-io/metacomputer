#!/usr/bin/env python3
"""Keep the plugins' copies the same as the repository's own files: the Claude Code plugin (plugin/), which is Codex's
plugin too, and the Agent Plugins package (agent-plugin/, what Hermes Agent installs).

    python3 scripts/sync-plugin.py           # copy what changed into plugin/ and agent-plugin/
    python3 scripts/sync-plugin.py --check   # change nothing; exit 1 and list what differs

Anthropic's plugin directory takes regular files only (no symbolic links), and a plugin installed from GitHub gets
the plugin folder alone, so plugin/ holds copies of the files it carries: the metacomputer skill, Cua Driver's skill
pack, get.sh (what `metacomputer setup` runs before there's a checkout), the license and the third-party notices.
They are laid out as in the repository, so the driver pack's ../../../skills/metacomputer/ links resolve in both.

Codex installs the same plugin/ folder from the repository's marketplace (`codex plugin marketplace add
metavert-io/metacomputer`): .agents/plugins/marketplace.json and plugin/.codex-plugin/plugin.json are written here from
the Claude Code plugin's manifest (Codex reads its own manifest first; a declared skills list replaces its default
skills/ folder, so both skill folders are named). scripts/build-openai-plugin.py packages the same files for OpenAI's
plugin directory.

agent-plugin/ is the portable Agent Plugins 1.0.0 package (https://agent-plugins.org/specification): plugin.json and
mcp.json (written here), the two skills one level under skills/ as the format wants (the driver pack's links to
../../../skills/metacomputer/ become ../metacomputer/ in its copy), the license and the notices. Its own files are
README.md and bin/metacomputer (the launcher its MCP server runs; gemini-extension.json runs it too).

The repository's files are the ones to edit; run this after changing them (tests/test_plugin.py runs --check).
It never deletes a folder. A file in a copy that its source no longer has is reported, for `git rm`.
Works under any python3 (macOS's 3.9 included).
"""
import json
import os
import shutil
import sys

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER_LINK, AGENT_DRIVER_LINK = "../../../skills/metacomputer/", "../metacomputer/"


def relink(data):
    """The driver pack's links to the metacomputer skill, as agent-plugin/skills/cua-driver/ reaches it."""
    return data.replace(DRIVER_LINK.encode(), AGENT_DRIVER_LINK.encode())


COPIES = [  # (source in the repository, copy, how the copy's bytes come from the source's: None = as they are)
    ("skills/metacomputer", "plugin/skills/metacomputer", None),
    (".claude/skills/cua-driver", "plugin/.claude/skills/cua-driver", None),
    ("get.sh", "plugin/get.sh", None),
    ("LICENSE", "plugin/LICENSE", None),
    ("THIRD_PARTY_NOTICES.md", "plugin/THIRD_PARTY_NOTICES.md", None),
    ("skills/metacomputer", "agent-plugin/skills/metacomputer", None),
    (".claude/skills/cua-driver", "agent-plugin/skills/cua-driver", relink),
    ("LICENSE", "agent-plugin/LICENSE", None),
    ("THIRD_PARTY_NOTICES.md", "agent-plugin/THIRD_PARTY_NOTICES.md", None),
]
SERVER = "metacomputer"
TOOL_TIMEOUT = 600          # seconds, as install.sh gives the MCP server in Codex, OpenClaw and Hermes Agent


def plugin_json():
    """agent-plugin/plugin.json: the Claude Code plugin's listing fields, with words for any agent."""
    claude = json.load(open(os.path.join(LAB, "plugin", ".claude-plugin", "plugin.json"), encoding="utf-8"))
    d = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": claude["name"],
        "description": ("Gives your agent a disposable computer of its own: an isolated Ubuntu 24.04 desktop VM on your "
                        "Mac that it operates like a person, through Cua Driver's MCP tools, with skills for when and how "
                        "to use it. Needs macOS on Apple silicon and metacomputer installed (its install script builds "
                        "the VM)."),
        "author": claude["author"],
        "homepage": claude["homepage"],
        "repository": claude["repository"],
        "license": claude["license"],
        "keywords": claude["keywords"],
        # Hermes Agent's settings for the server: a tool-call timeout as Codex's (its default is 300 s)
        "extensions": {"com.nousresearch.hermes": {"servers": {SERVER: {"timeout": TOOL_TIMEOUT}}}},
    }
    return (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def mcp_json():
    """agent-plugin/mcp.json: the MCP server runs the package's launcher (the format takes a command in the package or
    a bare name, never an absolute path), which runs the metacomputer installed on this Mac."""
    d = {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
         "mcpServers": {SERVER: {"type": "stdio", "command": "./bin/metacomputer", "args": ["mcp-lazy"]}}}
    return (json.dumps(d, indent=2) + "\n").encode("utf-8")


# Words for any agent, shared by Codex's manifest and the OpenAI package (scripts/build-openai-plugin.py)
NEUTRAL_DESCRIPTION = ("Gives your agent a disposable computer of its own: an isolated Ubuntu 24.04 desktop VM on your Mac "
                       "that it operates like a person, through Cua Driver's MCP tools, with skills for when and how to "
                       "use it. Needs macOS on Apple silicon and metacomputer installed (its install script builds the VM "
                       "and adds the MCP tools to your agent).")
SHORT_DESCRIPTION = "Your agent's own computer"
LONG_DESCRIPTION = (
    "metacomputer gives your coding agent a computer of its own: a disposable Ubuntu 24.04 desktop in a virtual machine "
    "on your Mac, with Firefox, Chrome, LibreOffice and other apps. The agent reads the screen through the accessibility "
    "tree, clicks and types like a person, and works in a real browser, without touching your own screen, apps or "
    "accounts. Sign-ins come from your own vault without the agent seeing the values; at a CAPTCHA, a sign-in or a "
    "payment the agent hands the step to you. These skills tell the agent when and how to use it; metacomputer itself "
    "(the VM, its MCP tools and the command) is installed on your Mac by its install script "
    "(https://github.com/metavert-io/metacomputer), which runs locally and sends nothing to Metavert.")


TERMS_URL = "https://www.metavert.io/terms-of-service"


def codex_interface(claude, icon):
    """The listing fields Codex and OpenAI's plugin directory read (interface): names, descriptions, the icon."""
    return {"displayName": claude["name"], "shortDescription": SHORT_DESCRIPTION, "longDescription": LONG_DESCRIPTION,
            "developerName": claude["author"]["name"], "category": "Developer Tools", "capabilities": [],
            "websiteURL": claude["homepage"], "supportURL": claude["supportUrl"],
            "privacyPolicyURL": claude["privacyPolicyUrl"], "termsOfServiceURL": TERMS_URL, "composerIcon": icon,
            "logo": icon}


def claude_manifest():
    return json.load(open(os.path.join(LAB, "plugin", ".claude-plugin", "plugin.json"), encoding="utf-8"))


def codex_plugin_json():
    """plugin/.codex-plugin/plugin.json: Codex's manifest for the same plugin folder. Codex reads it before Claude Code's;
    its skills list replaces the default skills/ folder, so it names both: skills/ (metacomputer, learn) and the driver
    pack's .claude/skills/. No version, as the Claude Code plugin has none (Codex then keeps it as "local")."""
    claude = claude_manifest()
    d = {"name": claude["name"], "description": NEUTRAL_DESCRIPTION, "author": claude["author"],
         "homepage": claude["homepage"], "repository": claude["repository"], "license": claude["license"],
         "keywords": claude["keywords"], "skills": ["./skills/", "./.claude/skills/"],
         "interface": codex_interface(claude, "./icon.png")}
    return (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def codex_marketplace_json():
    """.agents/plugins/marketplace.json: the repository as a Codex marketplace of one plugin, plugin/ (a path from the
    repository's root). Codex reads it before .claude-plugin/marketplace.json, which Claude Code reads."""
    d = {"name": "metacomputer", "interface": {"displayName": "metacomputer"},
         "plugins": [{"name": "metacomputer", "source": {"source": "local", "path": "./plugin"},
                      "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                      "category": "Developer Tools"}]}
    return (json.dumps(d, indent=2) + "\n").encode("utf-8")


WRITTEN = [("agent-plugin/plugin.json", plugin_json), ("agent-plugin/mcp.json", mcp_json),
           ("plugin/.codex-plugin/plugin.json", codex_plugin_json),
           (".agents/plugins/marketplace.json", codex_marketplace_json)]


def files_under(path):
    """Relative names of the regular files under path (a file: just its own name); links are refused."""
    if os.path.islink(path):
        raise SystemExit(f"sync-plugin: {os.path.relpath(path, LAB)} is a link; the plugins take regular files only")
    if os.path.isfile(path):
        return [""]
    out = []
    for d, dirs, names in os.walk(path):
        for n in dirs + names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                raise SystemExit(f"sync-plugin: {os.path.relpath(p, LAB)} is a link; the plugins take regular files only")
        dirs.sort()
        out += [os.path.relpath(os.path.join(d, n), path) for n in sorted(names) if n != ".DS_Store"]
    return out


def links_in(top):
    """Every link at or under the repository's folder top (itself, a folder or a file in it): plugin/ and agent-plugin/
    hold regular files only, their own files (the icon, the learn skill, README.md) as much as the copies."""
    path = os.path.join(LAB, top)
    if os.path.islink(path):
        return [path]
    out = []
    for d, dirs, names in os.walk(path):
        dirs.sort()
        out += [os.path.join(d, n) for n in sorted(dirs + names) if os.path.islink(os.path.join(d, n))]
    return out


def wanted(s, how):
    data = open(s, "rb").read()
    return how(data) if how and s.endswith(".md") else data


def differences():
    """[(kind, copy path, source path or None, bytes it should hold)]: kind is 'missing', 'differs', 'mode', 'extra'
    or 'link': a link in plugin/, agent-plugin/ or .agents/, in one copied or written file's place (it holds the bytes
    the file should, and sync puts the file there) or anywhere else (a folder, or a file of the plugin's own: None, which
    sync can't mend)."""
    out = []
    for src_rel, dst_rel, how in COPIES:
        src, dst = os.path.join(LAB, src_rel), os.path.join(LAB, dst_rel)
        names = files_under(src)
        for n in names:
            s, d = (src, dst) if n == "" else (os.path.join(src, n), os.path.join(dst, n))
            want = wanted(s, how)
            if os.path.islink(d):
                out.append(("link", d, s, want))
            elif not os.path.isfile(d):
                out.append(("missing", d, s, want))
            elif open(d, "rb").read() != want:
                out.append(("differs", d, s, want))
            elif (os.stat(s).st_mode & 0o111) != (os.stat(d).st_mode & 0o111):
                out.append(("mode", d, s, want))
        if os.path.isdir(dst) and not os.path.islink(dst):
            for n in files_under(dst):
                if n not in names:
                    out.append(("extra", os.path.join(dst, n), None, None))
    for dst_rel, make in WRITTEN:
        d = os.path.join(LAB, dst_rel)
        want = make()
        if os.path.islink(d):
            out.append(("link", d, None, want))
        elif not os.path.isfile(d):
            out.append(("missing", d, None, want))
        elif open(d, "rb").read() != want:
            out.append(("differs", d, None, want))
    seen = {d for _, d, _, _ in out}
    for top in ("plugin", "agent-plugin", ".agents"):
        out += [("link", p, None, None) for p in links_in(top) if p not in seen]
    return out


def main(argv):
    check = "--check" in argv
    if not check:
        for _, dst_rel, _ in COPIES:    # an older layout's link in place of a copy: the link itself goes, never a folder
            dst = os.path.join(LAB, dst_rel)
            if os.path.islink(dst):
                os.unlink(dst)
                print(f"removed the link {dst_rel}")
    diffs = differences()
    if check:
        for kind, d, s, _ in diffs:
            what = {"missing": "missing", "differs": "differs from", "mode": "has another mode than",
                    "extra": "isn't in the source", "link": "is a link; the plugins take regular files only"}[kind]
            src = os.path.relpath(s, LAB) if s else "what scripts/sync-plugin.py writes"
            print(f"plugin copy {os.path.relpath(d, LAB)} {what}"
                  + (f" {src}" if kind not in ("missing", "extra", "link") else ""))
        if diffs:
            print("run: python3 scripts/sync-plugin.py")
        return 1 if diffs else 0
    links = [d for kind, d, _, want in diffs if kind == "link" and want is None]
    if links:                       # a linked folder (or a file of the plugin's own): nothing is written through it
        print("these are links; the plugins take regular files only, so nothing was written: replace each with what it "
              "points to: " + " ".join(os.path.relpath(d, LAB) for d in links))
        return 1
    extra = [d for kind, d, _, _ in diffs if kind == "extra"]
    for kind, d, s, want in diffs:
        if kind == "extra":
            continue
        if os.path.islink(d):
            os.unlink(d)            # a link in place of one copied file: the link only
        os.makedirs(os.path.dirname(d), exist_ok=True)
        with open(d, "wb") as f:
            f.write(want)
        if s:
            shutil.copymode(s, d)
        print(f"wrote {os.path.relpath(d, LAB)}" + (f" from {os.path.relpath(s, LAB)}" if s else ""))
    if extra:
        print("these copies have no source any more; remove them: git rm " + " ".join(os.path.relpath(d, LAB) for d in extra))
        return 1
    if not diffs:
        print("plugin/ and agent-plugin/ are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
