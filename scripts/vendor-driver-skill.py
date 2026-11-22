#!/usr/bin/env python3
"""Install Cua Driver's release skill pack as this lab's .claude/skills/cua-driver.

  scripts/vendor-driver-skill.py .cache/cua-driver-<ver>/cua-driver-rs-v<ver>-skills.tar.gz

- verifies the tarball against the release SHA256SUMS saved next to it;
- keeps the Linux guide only (what `cua-driver skills install` does on a Linux
  host) and copies the other guides unchanged;
- edits SKILL.md for the lab: the description and a note point Claude at the
  VM (mcp__metacomputer tools) and at the lab's desktop notes, rows that are wrong
  in this VM are replaced, links to the removed platform guides are dropped;
- edits README.md and RUNTIME.md: their host-install, permission and update
  instructions become notes that the driver runs only inside the VM and is set
  up by metacomputer, and their links to the removed guides go;
- writes LICENSE, Cua's MIT notice from the same release's linux-arm64 tarball
  (checked against SHA256SUMS too), so the pack carries its license wherever
  it's copied (the plugin's GitHub install copies it).

Each edit asserts the upstream text it replaces, so an upstream rewrite fails
loudly here instead of shipping a half-edited skill, and no file may still name
a removed guide. Run after changing CUA_DRIVER_VERSION in config/defaults.env
(scripts/fetch-artifacts.sh fetches the linux-arm64 tarball and SHA256SUMS; put
the skills tarball from the same release in that folder first).
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
sys.path.insert(0, os.path.join(LAB, "lib"))
import safedelete  # noqa: E402  (every recursive delete: never a folder a VM uses)
DROP = {"MACOS.md", "WINDOWS.md", "EMBEDDING.md"}
NOTES = "../../../skills/metacomputer/DESKTOP.md"   # relative to DEST

DESCRIPTION = (
    "description: Detailed Cua Driver {ver} guidance for operating the virtual computer (the metacomputer project, an Ubuntu "
    "24.04 XFCE VM) through the metacomputer MCP tools (mcp__metacomputer__*, or mcp__plugin_metacomputer_metacomputer__* "
    "from an older copy of the plugin), i.e. Cua Driver running inside the "
    "VM: snapshot accessibility trees, act through element tokens, menu paths or pixel coordinates, batch "
    "known steps with run_steps, record, and verify from fresh state. Use for GUI work in that VM; the "
    "metacomputer skill decides when the VM is the right tool. Never install or run cua-driver on the "
    "Mac, and never use these tools for the user's own computer."
)
LAB_NOTE = """> **metacomputer's copy of the official Cua Driver {ver} skill pack** (Linux guide only; this
> file, README.md and RUNTIME.md are edited, the other guides are unchanged; Cua's license is
> in [LICENSE](LICENSE)). Here the driver runs **inside the lab
> VM** and is reached only through the `metacomputer` MCP tools (`mcp__metacomputer__*`, or
> `mcp__plugin_metacomputer_metacomputer__*` from an older copy of the plugin);
> `cua-driver` CLI examples apply inside the guest only. Read the lab's desktop
> notes first: [DESKTOP.md]({notes}) (`skills/metacomputer/DESKTOP.md` in the project folder).
> They take precedence over this pack where the VM differs: Firefox page fields
> need foreground keys, Ctrl+L is fine in this disposable desktop, Firefox has no
> typed browser route, and several app quirks.

"""
SETUP_URL = "https://github.com/metavert-io/metacomputer#the-5-minute-install"
EDITS = [
    # SKILL.md: (regex on the upstream text, replacement)
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

# README.md and RUNTIME.md. Cua's text tells a person to install the driver on their own
# computer and grant it permissions there; in metacomputer the driver runs only inside the VM,
# so those parts point to metacomputer's setup instead. (regex, replacement); README_NOTE is
# inserted after its match instead of replacing it.
README_NOTE = (
    "> **metacomputer's copy** of Cua Driver {ver}'s skill pack, Linux guide only. Here Cua\n"
    "> Driver runs only inside metacomputer's VM, which metacomputer's installer builds:\n"
    f"> see [the 5-minute install]({SETUP_URL}).\n"
    "> Don't install Cua Driver on your Mac or give it permissions there. The install and\n"
    "> update sections below are adapted for this; the rest is Cua's text. Cua's license:\n"
    "> [LICENSE](LICENSE).\n\n"
)
README_INSTALL = (
    "## Install Cua Driver\n\n"
    "In metacomputer, Cua Driver runs only inside the VM. metacomputer's installer\n"
    "downloads the pinned release (`CUA_DRIVER_VERSION` in `config/defaults.env`),\n"
    "checks its SHA-256 and installs it in the VM when it builds the VM. Don't run\n"
    "Cua's installer on your Mac, and don't grant Cua Driver Accessibility or Screen\n"
    "Recording permission there. To set up metacomputer, see\n"
    f"[the 5-minute install]({SETUP_URL}).\n\n"
    "## Install the skill\n\n"
    "This pack comes with metacomputer's Claude Code plugin, as `metacomputer:cua-driver`.\n"
    "`clawhub install` and `cua-driver skills install` aren't needed.\n\n"
)
README_UPDATES = (
    "## Updates\n\n"
    "The skill is versioned with Cua Driver releases, and metacomputer pins both: when\n"
    "`CUA_DRIVER_VERSION` changes, `scripts/vendor-driver-skill.py` replaces this pack\n"
    "with that release's, and a rebuilt VM gets the new driver. Don't run `cua-driver\n"
    "skills install`, `skills update` or `update` from a task. The pack version does\n"
    "not identify the running daemon; inspect the live tool schema when versions differ.\n\n"
)
README_LICENSE = (
    "## License\n\n"
    "Cua Driver and this skill pack are MIT licensed, copyright Cua AI, Inc.; the full\n"
    "notice is in [LICENSE](LICENSE) beside this file. metacomputer's edits are under\n"
    "metacomputer's own MIT license.\n"
)
FILE_EDITS = {
    "README.md": [
        (r"^# Cua Driver agent skill\n\n", README_NOTE),
        (r"^## Install Cua Driver\n.*?(?=^## Reading order\n)", README_INSTALL),
        (r"^- `MACOS\.md`, `WINDOWS\.md`, or `LINUX\.md`: host-specific launch, capture,\n"
         r"  accessibility, input delivery, permissions, and refusal boundaries\.\n",
         "- `LINUX.md`: host-specific launch, capture, accessibility, input delivery,\n"
         "  permissions, and refusal boundaries (the macOS and Windows guides aren't in\n"
         "  this Linux-only copy).\n"),
        (r"^- `EMBEDDING\.md`: embedding the driver into another host application\.\n", ""),
        (r"^## Updates and source builds\n.*?(?=^## License\n)", README_UPDATES),
        (r"^## License\n.*\Z", README_LICENSE),
    ],
    "RUNTIME.md": [
        (r"^For missing software, use the installation instructions in \[README\.md\]\(README\.md\) "
         r"with user approval\.[^\n]*$",
         "In metacomputer, Cua Driver is installed only inside the VM, at the version metacomputer pins "
         "([README.md](README.md#install-cua-driver)). Missing or mismatched driver software there is "
         "fixed by rebuilding the VM, never by installing or updating from a task: don't run "
         "`update --apply` (`check-update` is read-only). Never install Cua Driver on the Mac or approve "
         "its macOS permissions."),
        (r"^`skills update` replaces the installed pack\.[^\n]*$",
         "This pack is metacomputer's copy: `scripts/vendor-driver-skill.py` replaces it when the pinned "
         "driver version changes. Don't run `skills install` or `skills update` from a task."),
        (r" It is separate from the macOS privacy approvals \(TCC\) that the app itself needs; "
         r"see \[MACOS\.md\]\(MACOS\.md#install-and-approve-permissions\)\.",
         " It is separate from the macOS privacy approvals (TCC) that the app itself needs on a Mac, "
         "which don't arise in this VM."),
        (r" See \[EMBEDDING\.md\]\(EMBEDDING\.md\)\.",
         " (The embedding guide isn't in this Linux-only copy.)"),
    ],
}


def edit_file(name, text, ver):
    """Apply FILE_EDITS[name] (multiline and dot-all: a line's own text is matched with [^\\n]*);
    each pattern must match the upstream text, and only its first match is replaced."""
    for pattern, repl in FILE_EDITS[name]:
        rx = re.compile(pattern, re.M | re.S)
        if not rx.search(text):
            sys.exit(f"upstream {name} changed: no match for {pattern!r}; update scripts/vendor-driver-skill.py")
        if repl is README_NOTE:
            text = rx.sub(lambda mo: mo.group(0) + README_NOTE.format(ver=ver), text, count=1)
        else:
            text = rx.sub(lambda _: repl, text, count=1)
    return text


def cua_license(skills_tarball, ver, sums):
    """Cua's LICENSE from the same release's linux-arm64 tarball, checked against SHA256SUMS."""
    name = f"cua-driver-rs-{ver}-linux-arm64.tar.gz"
    path = os.path.join(os.path.dirname(skills_tarball), name)
    if not os.path.isfile(path):
        sys.exit(f"{name} isn't next to the skills tarball (scripts/fetch-artifacts.sh fetches it); "
                 "its LICENSE becomes the pack's LICENSE")
    want = next((ln.split()[0] for ln in open(sums) if ln.split()[-1].lstrip("*") == name), None)
    got = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if want != got:
        sys.exit(f"{name}: sha256 {got} does not match the release SHA256SUMS ({want})")
    with tarfile.open(path) as tf:
        m = next((x for x in tf.getmembers() if x.isfile() and x.name.split("/")[-1] == "LICENSE"), None)
        if m is None:
            sys.exit(f"{name} has no LICENSE")
        text = tf.extractfile(m).read().decode("utf-8")
    if not text.startswith("MIT License") or "Cua AI, Inc." not in text:
        sys.exit(f"{name}'s LICENSE isn't the MIT notice expected: check it, then THIRD_PARTY_NOTICES.md")
    return text


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
    edited = {"SKILL.md": text}
    for f in FILE_EDITS:
        edited[f] = edit_file(f, open(os.path.join(src, f)).read(), ver)
    edited["LICENSE"] = cua_license(tarball, ver, sums)
    stage = DEST + ".new"
    safedelete.rmtree(stage, quiet=True)
    os.makedirs(stage)
    for f in sorted(os.listdir(src)):
        if f in DROP or f in edited:
            continue
        shutil.copy2(os.path.join(src, f), os.path.join(stage, f))
    for f, body in edited.items():
        open(os.path.join(stage, f), "w").write(body)
    named = [f"{f}: {d}" for f in sorted(os.listdir(stage)) for d in sorted(DROP)
             if d in open(os.path.join(stage, f)).read()]
    if named:
        safedelete.rmtree(stage, quiet=True)
        sys.exit("the pack still names a removed guide (" + "; ".join(named) + "); update scripts/vendor-driver-skill.py")
    try:
        safedelete.rmtree(DEST)
    except safedelete.Refused as e:
        sys.exit(f"not replacing the skill pack: {e}")
    os.replace(stage, DEST)
    safedelete.rmtree(work, quiet=True)
    print(f"installed Cua Driver {ver} skill pack -> {os.path.relpath(DEST, LAB)} ({', '.join(sorted(os.listdir(DEST)))})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
