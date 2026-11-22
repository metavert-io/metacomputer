#!/usr/bin/env python3
"""Keep the Claude Code plugin's copies (plugin/) the same as the repository's own files.

    python3 scripts/sync-plugin.py           # copy what changed into plugin/
    python3 scripts/sync-plugin.py --check   # change nothing; exit 1 and list what differs

Anthropic's plugin directory takes regular files only (no symbolic links), and a plugin installed from GitHub gets
the plugin folder alone, so plugin/ holds copies of the files it carries: the metacomputer skill, Cua Driver's skill
pack, get.sh (what `metacomputer setup` runs before there's a checkout), the license and the third-party notices.
They are laid out as in the repository, so the driver pack's ../../../skills/metacomputer/ links resolve in both.
The repository's files are the ones to edit; run this after changing them (tests/test_plugin.py runs --check).

It never deletes a folder. A file in a copy that its source no longer has is reported, for `git rm`.
Works under any python3 (macOS's 3.9 included).
"""
import filecmp
import os
import shutil
import sys

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPIES = [  # (source in the repository, copy in plugin/)
    ("skills/metacomputer", "plugin/skills/metacomputer"),
    (".claude/skills/cua-driver", "plugin/.claude/skills/cua-driver"),
    ("get.sh", "plugin/get.sh"),
    ("LICENSE", "plugin/LICENSE"),
    ("THIRD_PARTY_NOTICES.md", "plugin/THIRD_PARTY_NOTICES.md"),
]


def files_under(path):
    """Relative names of the regular files under path (a file: just its own name); links are refused."""
    if os.path.islink(path):
        raise SystemExit(f"sync-plugin: {os.path.relpath(path, LAB)} is a link; the plugin takes regular files only")
    if os.path.isfile(path):
        return [""]
    out = []
    for d, dirs, names in os.walk(path):
        for n in dirs + names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                raise SystemExit(f"sync-plugin: {os.path.relpath(p, LAB)} is a link; the plugin takes regular files only")
        dirs.sort()
        out += [os.path.relpath(os.path.join(d, n), path) for n in sorted(names) if n != ".DS_Store"]
    return out


def differences():
    """[(kind, copy path, source path)]: kind is 'missing', 'differs', 'mode' or 'extra'."""
    out = []
    for src_rel, dst_rel in COPIES:
        src, dst = os.path.join(LAB, src_rel), os.path.join(LAB, dst_rel)
        names = files_under(src)
        for n in names:
            s, d = (src, dst) if n == "" else (os.path.join(src, n), os.path.join(dst, n))
            if os.path.islink(d) or not os.path.isfile(d):
                out.append(("missing", d, s))
            elif not filecmp.cmp(s, d, shallow=False):
                out.append(("differs", d, s))
            elif (os.stat(s).st_mode & 0o111) != (os.stat(d).st_mode & 0o111):
                out.append(("mode", d, s))
        if os.path.isdir(dst) and not os.path.islink(dst):
            for n in files_under(dst):
                if n not in names:
                    out.append(("extra", os.path.join(dst, n), None))
    return out


def main(argv):
    check = "--check" in argv
    if not check:
        for _, dst_rel in COPIES:    # an older layout's link in place of a copy: the link itself goes, never a folder
            dst = os.path.join(LAB, dst_rel)
            if os.path.islink(dst):
                os.unlink(dst)
                print(f"removed the link {dst_rel}")
    diffs = differences()
    if check:
        for kind, d, s in diffs:
            what = {"missing": "missing", "differs": "differs from", "mode": "has another mode than",
                    "extra": "isn't in the source"}[kind]
            print(f"plugin copy {os.path.relpath(d, LAB)} {what}" + (f" {os.path.relpath(s, LAB)}" if s and kind != "missing" else ""))
        if diffs:
            print("run: python3 scripts/sync-plugin.py")
        return 1 if diffs else 0
    extra = [d for kind, d, _ in diffs if kind == "extra"]
    for kind, d, s in diffs:
        if kind == "extra":
            continue
        if os.path.islink(d):
            os.unlink(d)            # a link in place of one copied file: the link only
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(s, d)
        shutil.copymode(s, d)
        print(f"copied {os.path.relpath(s, LAB)} -> {os.path.relpath(d, LAB)}")
    if extra:
        print("these copies have no source any more; remove them: git rm " + " ".join(os.path.relpath(d, LAB) for d in extra))
        return 1
    if not diffs:
        print("plugin/ is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
