#!/usr/bin/env python3
"""Install Cua Driver's release skill pack as this lab's .claude/skills/cua-driver.

  scripts/vendor-driver-skill.py .cache/cua-driver-<ver>/cua-driver-rs-v<ver>-skills.tar.gz

- verifies the tarball against the release SHA256SUMS saved next to it;
- keeps the Linux guide only (what `cua-driver skills install` does on a Linux
  host) and copies the other files unchanged;
- edits SKILL.md for the lab: the description and a note point Claude at the
  VM (mcp__metacomputer tools) and at the lab's desktop notes, rows that are wrong
  in this VM are replaced, links to the removed platform guides are dropped.

Each edit asserts the upstream text it replaces, so an upstream rewrite fails
loudly here instead of shipping a half-edited skill. Run after changing
CUA_DRIVER_VERSION in config/defaults.env (fetch the skills tarball and SHA256SUMS
from the same release first).
"""
import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(LAB, ".claude", "skills", "cua-driver")
DROP = {"MACOS.md", "WINDOWS.md", "EMBEDDING.md"}
NOTES = "../../../skills/metacomputer/DESKTOP.md"   # relative to DEST

DESCRIPTION = (
    "description: Detailed Cua Driver {ver} guidance for operating the virtual computer (the metacomputer project, an Ubuntu "
    "24.04 XFCE VM) through the metacomputer MCP tools (mcp__metacomputer__*), i.e. Cua Driver running inside the "
    "VM: snapshot accessibility trees, act through element tokens, menu paths or pixel coordinates, batch "
    "known steps with run_steps, record, and verify from fresh state. Use for GUI work in that VM; the "
    "metacomputer skill decides when the VM is the right tool. Never install or run cua-driver on the "
    "Mac, and never use these tools for the user's own computer."
)
LAB_NOTE = """> **metacomputer's copy of the official Cua Driver {ver} skill pack** (Linux guide only; this
> file is edited, the others are unchanged). Here the driver runs **inside the lab
> VM** and is reached only through the `metacomputer` MCP tools (`mcp__metacomputer__*`);
> `cua-driver` CLI examples apply inside the guest only. Read the lab's desktop
> notes first: [DESKTOP.md]({notes}) (`skills/metacomputer/DESKTOP.md` in the project folder).
> They take precedence over this pack where the VM differs: Firefox page fields
> need foreground keys, Ctrl+L is fine in this disposable desktop, Firefox has no
> typed browser route, and several app quirks.

"""
EDITS = [
    # (regex on the upstream text, replacement)
    (r"^description: Drive a native GUI app .*$", None),  # filled with DESCRIPTION
    (r"^# cua-driver\n\n", None),                           # LAB_NOTE inserted after the heading
    (r"^\| Install, update, or grant macOS permissions \|.*$",
     "| Driver version and updates | Pinned in the VM image (`config/defaults.env`). Do not call "
     "`check_for_update`, `install_extension` or `update` from a task: an upgrade is a lab rebuild | "
     f"[Lab notes]({NOTES}) |"),
    (r"(^\| Browser tools refuse in the background +\| Not blocked:[^|]*)\|",
     r"\1**Firefox in this VM ignores `set_value` and background typing in page fields: use the foreground "
     r"route in the lab notes.** |"),
    (r", by the steps and guardrails in \[MACOS\.md\]\(MACOS\.md#install-and-approve-permissions\)\.",
     " (macOS only; it does not arise in this VM)."),
    (r"^\| Web page in a browser +\|.*$",
     "| Web page in a browser | Firefox (this VM's browser) has no typed browser route: `get_browser_state` "
     "refuses `browser_route_unavailable`. Use the native tree, token clicks and foreground keys; see the "
     f"[lab notes]({NOTES}) |"),
    (r"^\| LibreOffice, Preview and other macOS editors +\|.*$",
     f"| LibreOffice, Thunar, GTK dialogs in this VM | [Lab notes]({NOTES}): menus by `invoke_menu`, Save As, "
     "Thunar List View |"),
    (r"^- Current host only: \[MACOS\.md\].*$",
     "- Current host: [LINUX.md](LINUX.md). The macOS, Windows and embedding guides are not included in this "
     "Linux-only copy."),
    (r"^- \[EMBEDDING\.md\]\(EMBEDDING\.md\):.*\n", ""),
]


def main(tarball):
    name = os.path.basename(tarball)
    m = re.match(r"cua-driver-rs-v(\d+\.\d+\.\d+)-skills\.tar\.gz$", name)
    if not m:
        sys.exit(f"unexpected tarball name: {name}")
    ver = m.group(1)
    sums = os.path.join(os.path.dirname(tarball), "SHA256SUMS")
    want = next((ln.split()[0] for ln in open(sums) if ln.split()[-1].lstrip("*") == name), None)
    got = hashlib.sha256(open(tarball, "rb").read()).hexdigest()
    if want != got:
        sys.exit(f"{name}: sha256 {got} does not match the release SHA256SUMS ({want})")
    work = tempfile.mkdtemp(prefix="cua-skill-")
    with tarfile.open(tarball) as tf:
        tf.extractall(work, filter="data")
    src = next(os.path.join(d) for d, _, fs in os.walk(work) if "SKILL.md" in fs)
    text = open(os.path.join(src, "SKILL.md")).read()
    for pattern, repl in EDITS:
        rx = re.compile(pattern, re.M)
        if not rx.search(text):
            sys.exit(f"upstream SKILL.md changed: no match for {pattern!r}; update scripts/vendor-driver-skill.py")
        if pattern.startswith("^description"):
            text = rx.sub(lambda _: DESCRIPTION.format(ver=ver), text, count=1)
        elif pattern.startswith("^# cua-driver"):
            text = rx.sub(lambda mo: mo.group(0) + LAB_NOTE.format(ver=ver, notes=NOTES), text, count=1)
        else:
            text = rx.sub(repl, text, count=1)
    stage = DEST + ".new"
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    for f in sorted(os.listdir(src)):
        if f in DROP:
            continue
        if f == "SKILL.md":
            open(os.path.join(stage, f), "w").write(text)
        else:
            shutil.copy2(os.path.join(src, f), os.path.join(stage, f))
    shutil.rmtree(DEST, ignore_errors=True)
    os.replace(stage, DEST)
    shutil.rmtree(work, ignore_errors=True)
    print(f"installed Cua Driver {ver} skill pack -> {os.path.relpath(DEST, LAB)} ({', '.join(sorted(os.listdir(DEST)))})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
