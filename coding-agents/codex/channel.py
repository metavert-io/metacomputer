"""Codex's distribution files. Codex installs the Claude Code plugin's folder, plugin/, from the repository's own
marketplace (`codex plugin marketplace add metavert-io/metacomputer`): .agents/plugins/marketplace.json and
plugin/.codex-plugin/plugin.json are written here (scripts/sync-plugin.py, called with S) from the plugin's listing
fields (Codex reads its own manifest first; a declared skills list replaces its default skills/ folder, so both skill
folders are named). scripts/build-openai-plugin.py packages the same files for OpenAI's plugin directory (OPENAI)."""
import json

COPIES = []
LINK_TOPS = (".agents",)                # its folder holds regular files only (plugin/ is the plugin's own)


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


def codex_plugin_json(S):
    """plugin/.codex-plugin/plugin.json: Codex's manifest for the same plugin folder. Codex reads it before Claude Code's;
    its skills list replaces the default skills/ folder, so it names both: skills/ (metacomputer, learn) and the driver
    pack's .claude/skills/. No version, as the Claude Code plugin has none (Codex then keeps it as "local")."""
    claude = S.listing()
    d = {"name": claude["name"], "description": NEUTRAL_DESCRIPTION, "author": claude["author"],
         "homepage": claude["homepage"], "repository": claude["repository"], "license": claude["license"],
         "keywords": claude["keywords"], "skills": ["./skills/", "./.claude/skills/"],
         "interface": codex_interface(claude, "./icon.png")}
    return (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def codex_marketplace_json(S):
    """.agents/plugins/marketplace.json: the repository as a Codex marketplace of one plugin, plugin/ (a path from the
    repository's root). Codex reads it before .claude-plugin/marketplace.json, which Claude Code reads."""
    d = {"name": "metacomputer", "interface": {"displayName": "metacomputer"},
         "plugins": [{"name": "metacomputer", "source": {"source": "local", "path": "./plugin"},
                      "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                      "category": "Developer Tools"}]}
    return (json.dumps(d, indent=2) + "\n").encode("utf-8")


WRITTEN = [("plugin/.codex-plugin/plugin.json", codex_plugin_json),
           (".agents/plugins/marketplace.json", codex_marketplace_json)]
# OpenAI's plugin directory's package (scripts/build-openai-plugin.py): its manifest, made from the plugin's Codex
# manifest above; the skills metacomputer and learn and Cua Driver's pack (its links pointed at ../metacomputer/, as
# agent-plugin/ has them); the icon, the license and the notices. Its sources are plugin/'s files.
OPENAI = {"manifest": ".codex-plugin/plugin.json", "from": "plugin/.codex-plugin/plugin.json",
          "skills": [("plugin/skills/metacomputer", "skills/metacomputer"), ("plugin/skills/learn", "skills/learn"),
                     ("plugin/.claude/skills/cua-driver", "skills/cua-driver")],
          "files": [("plugin/icon.png", "assets/icon.png"), ("plugin/LICENSE", "LICENSE"),
                    ("plugin/THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md")]}
