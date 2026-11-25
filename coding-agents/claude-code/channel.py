"""Claude Code's distribution files: plugin/, the Claude Code plugin (its marketplace is the repository's
.claude-plugin/marketplace.json). Anthropic's plugin directory takes regular files only (no symbolic links), and a
plugin installed from GitHub gets the plugin folder alone, so plugin/ holds copies of the files it carries, kept by
scripts/sync-plugin.py: the metacomputer skill, Cua Driver's skill pack, get.sh (what `metacomputer setup` runs before
there's a checkout), the license and the third-party notices. They are laid out as in the repository, so the driver
pack's ../../../skills/metacomputer/ links resolve in both. Its own files are its manifest (.claude-plugin/plugin.json,
whose listing fields every channel's manifest takes), the icon, the learn skill, README.md and bin/metacomputer. Codex
installs the same folder (coding-agents/codex/channel.py)."""

COPIES = [  # (source in the repository, copy, how the copy's bytes come from the source's: None = as they are)
    ("skills/metacomputer", "plugin/skills/metacomputer", None),
    (".claude/skills/cua-driver", "plugin/.claude/skills/cua-driver", None),
    ("get.sh", "plugin/get.sh", None),
    ("LICENSE", "plugin/LICENSE", None),
    ("THIRD_PARTY_NOTICES.md", "plugin/THIRD_PARTY_NOTICES.md", None),
]
WRITTEN = []
LINK_TOPS = ("plugin",)                 # its folder holds regular files only
