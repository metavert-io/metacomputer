#!/usr/bin/env python3
"""Package metacomputer for OpenAI's plugin directory (ChatGPT and Codex): a skills-only plugin that needs this Mac,
as a ZIP to upload (nothing is uploaded or submitted here).

    python3 scripts/build-openai-plugin.py [--version X.Y.Z] [--out FILE.zip]   # build it (default: results/)
    python3 scripts/build-openai-plugin.py --check                              # build in memory, check, write nothing

It is made from the same files as the Claude Code plugin (plugin/, which scripts/sync-plugin.py keeps equal to the
repository's own files): the skills metacomputer and learn (plugin/skills/) and Cua Driver's pack
(plugin/.claude/skills/cua-driver/, its links to ../../../skills/metacomputer/ pointed at ../metacomputer/, as
agent-plugin/ has them), the icon, the license and the notices. The manifest is Codex's (plugin/.codex-plugin/plugin.json)
with what the directory adds: a version (the release's: gemini-extension.json's, which scripts/release.sh sets), the
skills folder as one path, the icon under assets/, and "requires_local_executor": true, which makes the plugin
"Desktop only" (installed from the ChatGPT desktop app, used in threads that run on the Mac). No MCP server, hooks,
commands, launcher or scripts: the skills run the metacomputer installed on the Mac (~/.local/bin/metacomputer), and say
how to install it when it isn't.

The layout (one folder, metacomputer/, nothing beside it):
    .codex-plugin/plugin.json   skills/metacomputer/   skills/learn/   skills/cua-driver/   assets/icon.png
    LICENSE   THIRD_PARTY_NOTICES.md

Its sources are regular files only: a skill folder, a file it takes, or a folder on their way that is a link is
refused (it would bring in what it points to). --check (tests/test_plugin.py runs it) checks what the directory's
validator is documented to refuse (OpenAI's
"plugins/deploy/submission-errors", checked 2026-11-23): regular files only, no hidden skill folder, each skill an
immediate child of skills/ with a SKILL.md whose front matter has a name (the folder's) and a description of at most
1,024 characters and whose body isn't empty, plugin:skill names of at most 64 characters, interface names of at most
30, a square PNG icon of at least 48 px, paths that start with ./ and stay inside, under 5,000 entries and 100 MB; and
that every relative link in a skill's Markdown names a file in the package. Entries are sorted and dated 1980-01-01, so
the same sources make the same ZIP. Stdlib only.
"""
import argparse
import io
import json
import os
import re
import stat
import struct
import sys
import zipfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "scripts"))
ROOT = "metacomputer"
SKILLS = [("plugin/skills/metacomputer", "skills/metacomputer"), ("plugin/skills/learn", "skills/learn"),
          ("plugin/.claude/skills/cua-driver", "skills/cua-driver")]
FILES = [("plugin/icon.png", "assets/icon.png"), ("plugin/LICENSE", "LICENSE"),
         ("plugin/THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md")]
DRIVER_LINK, PACKAGE_DRIVER_LINK = "../../../skills/metacomputer/", "../metacomputer/"
DATE = (1980, 1, 1, 0, 0, 0)
MAX_ENTRIES, MAX_BYTES, MAX_DESCRIPTION, MAX_SKILL_NAME, MAX_LABEL = 5000, 100 << 20, 1024, 64, 30
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import strictargs  # noqa: E402  (argparse that refuses what a command wouldn't use)


def sync_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sync_plugin", os.path.join(LAB, "scripts", "sync-plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def release_version():
    return json.load(open(os.path.join(LAB, "gemini-extension.json"), encoding="utf-8"))["version"]


def no_link(rel):
    """The repository's path rel (a skill folder, or a file the package takes) as an absolute path, refused when it or a
    folder on its way from the repository's root is a link (a linked skill folder or icon would bring in what it
    points to)."""
    p = LAB
    for part in rel.split("/"):
        p = os.path.join(p, part)
        if os.path.islink(p):
            raise SystemExit(f"build-openai-plugin: {os.path.relpath(p, LAB)} is a link; the package takes regular "
                             "files only")
    return p


def regular_file(rel):
    """The bytes of the repository's regular file rel (no_link; opened without following a link)."""
    path = no_link(rel)
    try:
        with open(path, "rb", opener=lambda f, flags: os.open(f, flags | os.O_NOFOLLOW)) as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise SystemExit(f"build-openai-plugin: {rel} isn't a regular file")
            return fh.read()
    except OSError as e:
        raise SystemExit(f"build-openai-plugin: {rel} can't be read ({e.strerror})") from None


def regular_files(src):
    """(relative name, absolute path) of every regular file under src, sorted; src a link, or a link anywhere under
    it, is refused."""
    if os.path.islink(src) or not os.path.isdir(src):
        raise SystemExit(f"build-openai-plugin: {os.path.relpath(src, LAB)} is a link or isn't a folder; the package "
                         "takes regular files only")
    out = []
    for d, dirs, names in os.walk(src):
        dirs.sort()
        for n in dirs + names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                raise SystemExit(f"build-openai-plugin: {os.path.relpath(p, LAB)} is a link; the package takes regular "
                                 "files only")
        out += [(os.path.relpath(os.path.join(d, n), src), os.path.join(d, n)) for n in sorted(names) if n != ".DS_Store"]
    return out


def manifest(version):
    """The package's .codex-plugin/plugin.json: Codex's manifest (sync-plugin.py's) for the directory."""
    d = json.loads(sync_plugin().codex_plugin_json())
    d["version"] = version
    d["skills"] = "./skills/"
    d["requires_local_executor"] = True
    d["interface"]["composerIcon"] = d["interface"]["logo"] = "./assets/icon.png"
    order = ["name", "version", "description", "author", "homepage", "repository", "license", "keywords", "skills",
             "requires_local_executor", "interface"]
    return (json.dumps({k: d[k] for k in order}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def contents(version):
    """[(path in the package, bytes, mode)] in order: the manifest, the skills, the other files."""
    out = [(".codex-plugin/plugin.json", manifest(version), 0o644)]
    for src_rel, dst_rel in SKILLS:
        for rel, path in regular_files(no_link(src_rel)):
            data = regular_file(os.path.relpath(path, LAB))
            if rel.endswith(".md") and dst_rel == "skills/cua-driver":
                data = data.replace(DRIVER_LINK.encode(), PACKAGE_DRIVER_LINK.encode())
            out.append((f"{dst_rel}/{rel}", data, 0o644))
    for src_rel, dst_rel in FILES:
        out.append((dst_rel, regular_file(src_rel), 0o644))
    return out


def build(version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data, mode in contents(version):
            info = zipfile.ZipInfo(f"{ROOT}/{name}", DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100000 | mode) << 16
            info.create_system = 3
            z.writestr(info, data)
    return buf.getvalue()


def front_matter(text):
    """(dict of its top-level keys -> raw values, body) of a SKILL.md, or (None, text) when it has none."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end < 0:
        return None, text
    keys = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z][\w-]*):\s?(.*)$", line)
        if m:
            keys[m.group(1)] = m.group(2)
    return keys, text[end + 4:].lstrip("\n")


def scalar(v):
    v = v.strip()
    if v.startswith('"'):
        return json.loads(v)
    return v.strip("'")


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def problems(zbytes):
    """What OpenAI's directory is documented to refuse in this ZIP, and links that lead nowhere: [str] (empty: none)."""
    out = []
    if len(zbytes) > MAX_BYTES:
        out.append("larger than 100 MB")
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES:
        out.append(f"more than {MAX_ENTRIES} entries")
    names = [i.filename for i in infos]
    tops = {n.split("/", 1)[0] for n in names}
    if tops != {ROOT}:
        out.append(f"not one top-level folder: {sorted(tops)}")
    files = {}
    for i in infos:
        n = i.filename
        if n.startswith("/") or "\\" in n or ".." in n.split("/") or n.count("/") > 20:
            out.append(f"a path that leaves the package or is too deep: {n}")
        kind = (i.external_attr >> 16) & 0o170000
        if kind not in (0, 0o100000) or n.endswith("/"):
            out.append(f"not a regular file: {n}")
        files[n.split("/", 1)[1]] = z.read(i)
    try:
        m = json.loads(files[".codex-plugin/plugin.json"])
    except (KeyError, ValueError):
        return out + ["no readable .codex-plugin/plugin.json"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", m.get("name") or ""):
        out.append("the plugin's name isn't 1-64 letters, digits, - or _")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(m.get("version") or "")):
        out.append("no semantic version")
    if m.get("requires_local_executor") is not True:
        out.append("requires_local_executor isn't true")
    if m.get("skills") != "./skills/":
        out.append("skills isn't ./skills/")
    if not m.get("description") or len(m["description"]) > MAX_DESCRIPTION:
        out.append("the plugin's description is empty or longer than 1,024 characters")
    for k in ("mcpServers", "apps", "hooks", "commands"):
        if k in m:
            out.append(f"the manifest declares {k} (a skills-only package doesn't)")
    it = m.get("interface") or {}
    for k in ("displayName", "shortDescription", "longDescription", "developerName", "category", "composerIcon", "logo"):
        if not it.get(k):
            out.append(f"interface.{k} is missing")
    for k in ("displayName", "shortDescription"):
        if len(it.get(k) or "") > MAX_LABEL:
            out.append(f"interface.{k} is longer than {MAX_LABEL} characters")
    for k in ("composerIcon", "logo"):
        p = it.get(k) or ""
        if not p.startswith("./") or p[2:] not in files:
            out.append(f"interface.{k} isn't a file in the package")
        else:
            size = png_size(files[p[2:]])
            if not size or size[0] != size[1] or size[0] < 48 or size[0] > 4096:
                out.append(f"interface.{k} isn't a square PNG of 48 to 4096 px")
    for k in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        if k in it and not str(it[k]).startswith("https://"):
            out.append(f"interface.{k} isn't an https URL")
    skills = sorted({n.split("/")[1] for n in files if n.startswith("skills/") and n.count("/") >= 2})
    if not skills:
        out.append("no skill")
    for s in skills:
        if s.startswith("."):
            out.append(f"a hidden skill folder: {s}")
        text = files.get(f"skills/{s}/SKILL.md")
        if text is None:
            out.append(f"skills/{s} has no SKILL.md")
            continue
        fm, body = front_matter(text.decode("utf-8"))
        if fm is None:
            out.append(f"skills/{s}/SKILL.md has no front matter")
            continue
        name, desc = scalar(fm.get("name", "")), scalar(fm.get("description", ""))
        if name != s:
            out.append(f"skills/{s}/SKILL.md is named {name!r}, not its folder's name")
        if not desc or len(desc) > MAX_DESCRIPTION:
            out.append(f"skills/{s}: its description is empty or longer than 1,024 characters ({len(desc)})")
        if len(f"{m.get('name')}:{name}") > MAX_SKILL_NAME:
            out.append(f"skills/{s}: plugin:skill is longer than {MAX_SKILL_NAME} characters")
        if not body.strip():
            out.append(f"skills/{s}/SKILL.md has no body")
    for n, data in files.items():
        if not (n.startswith("skills/") and n.endswith(".md")):
            continue
        for target in LINK.findall(data.decode("utf-8", "replace")):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("<"):
                continue
            path = os.path.normpath(os.path.join(os.path.dirname(n), target))
            if path.startswith("..") or not (path in files or any(f.startswith(path.rstrip("/") + "/") for f in files)):
                out.append(f"{n}: a link to {target} leads to nothing in the package")
    return out


def main(argv):
    ap = strictargs.Parser(description="Package metacomputer's skills for OpenAI's plugin directory.")
    ap.add_argument("--version", default=None, help="the package's version (default: gemini-extension.json's)")
    ap.add_argument("--out", default=None, help="the ZIP to write (default: results/openai-plugin/metacomputer-VERSION.zip)")
    ap.add_argument("--check", action="store_true", help="build in memory and check it; write nothing")
    a = ap.parse_args(argv)
    if a.check and a.out:                   # --check writes nothing: an --out would be left unread
        ap.error("--check writes nothing: --out goes with a build")
    sync = sync_plugin()
    stale = sync.differences()
    for kind, d, _, _ in stale:
        if kind == "link":
            print(f"build-openai-plugin: {os.path.relpath(d, LAB)} is a link; the package takes regular files only",
                  file=sys.stderr)
    if any(kind != "link" for kind, _, _, _ in stale):
        print("build-openai-plugin: plugin/ isn't what the sources make; run python3 scripts/sync-plugin.py first",
              file=sys.stderr)
    if stale:
        return 1
    version = a.version or release_version()
    data = build(version)
    found = problems(data)
    if found:
        for p in found:
            print(f"build-openai-plugin: {p}", file=sys.stderr)
        return 1
    if a.check:
        print(f"the OpenAI package ({len(zipfile.ZipFile(io.BytesIO(data)).infolist())} files, {len(data) // 1024} KB, "
              f"version {version}) builds and passes its checks")
        return 0
    out = a.out or os.path.join(LAB, "results", "openai-plugin", f"metacomputer-{version}.zip")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, out)
    print(f"wrote {out} ({len(data) // 1024} KB, version {version}); upload it in OpenAI's plugin portal yourself")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
