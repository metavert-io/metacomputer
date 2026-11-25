#!/usr/bin/env python3
"""scripts/release.sh against a throwaway repository: no network, no GitHub, nothing of this checkout's.

    python3 tests/test_release.py

A bare repository in a temporary folder stands in for origin, and a clone of it for the checkout, holding a copy
of scripts/release.sh, a CHANGELOG, and stand-ins for scripts/check-public.sh, get.sh and every suite in
release.sh's NO_VM_SUITES (each logs that it ran; the test makes any of them fail). A stand-in `gh` logs its
arguments and answers `pr create`, `pr list` (from a file the test writes) and `release create`. It checks:
- the refusals, each changing nothing: a VERSION that isn't MAJOR.MINOR.PATCH or isn't newer than every tag; not
  on main; a tree that isn't clean; main behind origin; an [Unreleased] section with no entries; check-public
  failing (then no suite runs); a suite failing; --skip-tests without --dry-run; a tag only origin has;
- the dry run: every check, the CHANGELOG change shown, nothing changed (no branch, commit, push or PR);
- the real run: [Unreleased] moved to [VERSION] - DATE under a new empty [Unreleased], committed on
  release-vVERSION, check-public run again before the push, the branch pushed (main untouched), the PR opened
  against main, and the checkout back on a clean main;
- --tag: refused before the PR is merged, or when the merge commit isn't on origin/main; its dry run changes
  nothing; then an annotated tag on the merge commit, pushed, get.sh's dry run (from origin/main, in a temporary
  HOME) picking it, and with --draft-release a GitHub Release made only as a draft; a second --tag is refused.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[-700:]})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


SUITES = re.findall(r"\b(test_[a-z0-9_]+)", re.search(r"NO_VM_SUITES=\((.*?)\)",
                                                       open(os.path.join(LAB, "scripts", "release.sh")).read(), re.S)[1])
EXTENSION = """{
  "name": "metacomputer",
  "version": "0.2.0",
  "description": "a stand-in",
  "mcpServers": {"metacomputer": {"command": "x", "args": ["mcp-lazy"], "version": "not this one"}}
}
"""
CHANGELOG = """# Changelog

All notable changes are recorded here.

## [Unreleased]

### Added
- A new thing.

### Fixed
- An old bug.

## [0.2.0] - 2026-11-18

### Added
- The first public release.
"""
GH = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:2] == ["pr", "create"]:
    print("https://example.invalid/metacomputer/pull/99")
elif a[:2] == ["pr", "list"]:
    p = os.environ.get("STUB_GH_PRS", "")
    print(open(p).read() if p and os.path.exists(p) else "[]")
elif a[:2] == ["release", "create"]:
    print("https://example.invalid/metacomputer/releases/tag/untagged-1")
else:
    sys.exit(1)
"""
GET_SH = r"""#!/bin/bash
# stand-in for get.sh --dry-run: names the newest vX.Y.Z tag of --repo and its commit, as get.sh does
repo=""; while [ $# -gt 0 ]; do [ "$1" = --repo ] && repo="$2"; shift; done
[ -n "$repo" ] || { echo "get.sh stand-in: no --repo"; exit 1; }
echo "HOME=$HOME" >> "$STUB_GETSH_LOG"
git ls-remote --tags "$repo" | python3 -c '
import re, sys
best = {}
for line in sys.stdin:
    sha, ref = line.split()
    m = re.fullmatch(r"refs/tags/v(\d+)\.(\d+)\.(\d+)(\^\{\})?", ref)
    if m:
        v = tuple(map(int, m.group(1, 2, 3)))
        if m.group(4) or v not in best:
            best[v] = sha
v = max(best)
print("  would check out release v%d.%d.%d, commit %s" % (v + (best[v][:12],)))'
"""
SUITE = """import os, sys
name = os.path.basename(__file__)[:-3]
with open(os.environ["STUB_SUITE_LOG"], "a") as f:
    f.write(name + "\\n")
sys.exit(1 if name in os.environ.get("STUB_FAIL_SUITES", "").split() else 0)
"""
CHECK_PUBLIC = """#!/bin/bash
echo "check-public $*" >> "$STUB_CHECK_LOG"
[ -z "${STUB_CHECK_FAIL:-}" ] || { echo "leak: something private"; exit 1; }
echo "0 findings"
"""


class World:
    def __init__(self, td):
        self.td = td
        self.origin = os.path.join(td, "origin.git")
        self.work = os.path.join(td, "work")
        self.logs = {k: os.path.join(td, f"{k}.log") for k in ("gh", "suite", "check", "getsh")}
        self.prs = os.path.join(td, "prs.json")
        fakebin = os.path.join(td, "bin")
        write(os.path.join(fakebin, "gh"), GH, 0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "MCOMP_"))}
        self.env.update(PATH=fakebin + ":" + os.environ.get("PATH", "/usr/bin:/bin"), HOME=os.path.join(td, "home"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid",
                        STUB_GH_LOG=self.logs["gh"], STUB_SUITE_LOG=self.logs["suite"],
                        STUB_CHECK_LOG=self.logs["check"], STUB_GETSH_LOG=self.logs["getsh"], STUB_GH_PRS=self.prs,
                        TMPDIR=os.path.join(td, "tmp"))
        os.makedirs(self.env["HOME"])
        os.makedirs(self.env["TMPDIR"])
        self.git("init", "-q", "--bare", "-b", "main", self.origin)
        self.git("clone", "-q", self.origin, self.work)
        self.git("-C", self.work, "checkout", "-q", "-b", "main")
        write(os.path.join(self.work, "CHANGELOG.md"), CHANGELOG)
        write(os.path.join(self.work, "gemini-extension.json"), EXTENSION)
        write(os.path.join(self.work, "scripts", "release.sh"),
              open(os.path.join(LAB, "scripts", "release.sh")).read(), 0o755)
        write(os.path.join(self.work, "scripts", "check-public.sh"), CHECK_PUBLIC, 0o755)
        for f in ("scripts/safe-rmtree.sh", "scripts/lib.sh", "config/defaults.env", "lib/safedelete.py", "lib/vms.py", "lib/termsafe.py",
                  "scripts/strictargs.sh", "lib/strictargs.py", "lib/cloud/providers.sh"):   # release.sh's temporary folder goes through it
            write(os.path.join(self.work, f), open(os.path.join(LAB, f)).read(), 0o755 if f.endswith(".sh") else 0o644)
        write(os.path.join(self.work, "get.sh"), GET_SH, 0o755)
        for s in SUITES:
            write(os.path.join(self.work, "tests", s + ".py"), SUITE)
        self.git("-C", self.work, "add", "-A")
        self.git("-C", self.work, "commit", "-q", "-m", "start")
        self.git("-C", self.work, "tag", "v0.2.0")
        self.git("-C", self.work, "push", "-q", "origin", "main", "v0.2.0")

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def release(self, *args, **env):
        for p in self.logs.values():
            if os.path.exists(p):
                os.remove(p)
        e = dict(self.env, **env)
        return subprocess.run([os.path.join(self.work, "scripts", "release.sh"), *args], cwd=self.work, env=e,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)

    def log(self, k):
        p = self.logs[k]
        return open(p).read().splitlines() if os.path.exists(p) else []

    def state(self):
        """What a refusal must leave alone: branches here and on origin, tags, the CHANGELOG, HEAD, the tree."""
        return (self.git("-C", self.work, "for-each-ref", "--format=%(refname) %(objectname)"),
                self.git("ls-remote", self.origin), open(os.path.join(self.work, "CHANGELOG.md")).read(),
                self.git("-C", self.work, "rev-parse", "--abbrev-ref", "HEAD"),
                self.git("-C", self.work, "status", "--porcelain"))


def refusals(w):
    print("refusals: each changes nothing")
    before = w.state()

    def refused(name, args, want, code=None, **env):
        r = w.release(*args, **env)
        out = r.stdout + r.stderr
        check(name, (r.returncode == code if code is not None else r.returncode != 0) and want in out
              and w.state() == before and not any(a[:2] == ["pr", "create"] for a in map(json.loads, w.log("gh"))),
              (r.returncode, out[-500:]))
        return r

    refused("a VERSION with a v, or not MAJOR.MINOR.PATCH", ["v0.3.0"], "MAJOR.MINOR.PATCH", code=2)
    refused("…--skip-tests without --dry-run", ["0.3.0", "--skip-tests"], "goes with --dry-run only", code=2)
    refused("a VERSION that isn't newer than every tag", ["0.1.9"], "isn't newer than the newest release tag, v0.2.0")
    refused("a VERSION whose tag exists", ["0.2.0"], "already exists")
    w.git("-C", w.work, "switch", "-q", "-c", "elsewhere")
    before_b = w.state()
    r = w.release("0.3.0")
    check("not on main", r.returncode != 0 and "not main" in r.stderr and w.state() == before_b, r.stderr[-300:])
    w.git("-C", w.work, "switch", "-q", "main")
    w.git("-C", w.work, "branch", "-q", "-D", "elsewhere")
    write(os.path.join(w.work, "notes.txt"), "an untracked file\n")
    r = w.release("0.3.0")
    check("a tree that isn't clean (an untracked file)", r.returncode != 0 and "isn't clean" in r.stderr
          and "notes.txt" in r.stderr, r.stderr[-300:])
    os.remove(os.path.join(w.work, "notes.txt"))
    # origin moves on: this checkout's main is behind
    other = os.path.join(w.td, "other")
    w.git("clone", "-q", w.origin, other)
    write(os.path.join(other, "more.txt"), "x\n")
    w.git("-C", other, "add", "more.txt")
    w.git("-C", other, "commit", "-q", "-m", "more")
    w.git("-C", other, "push", "-q", "origin", "main")
    r = w.release("0.3.0")
    check("main behind origin/main", r.returncode != 0 and "isn't origin/main" in r.stderr
          and not w.log("suite"), r.stderr[-300:])
    w.git("-C", w.work, "pull", "-q", "--ff-only", "origin", "main")
    before = w.state()
    refused("check-public failing: refused, and no suite runs", ["0.3.0"], "check-public.sh -a found something",
            STUB_CHECK_FAIL="1")
    check("…check-public ran with -a", w.log("check") == ["check-public -a"] and not w.log("suite"), w.log("check"))
    refused("a suite failing: refused, naming it", ["0.3.0"], "suite(s) failed: test_hub", STUB_FAIL_SUITES="test_hub")
    check("…after running every suite", sorted(w.log("suite")) == sorted(SUITES), w.log("suite"))
    # an [Unreleased] with no entries
    empty = CHANGELOG.replace("### Added\n- A new thing.\n\n### Fixed\n- An old bug.\n\n", "")
    write(os.path.join(w.work, "CHANGELOG.md"), empty)
    w.git("-C", w.work, "commit", "-q", "-am", "nothing to release")
    w.git("-C", w.work, "push", "-q", "origin", "main")
    before = w.state()
    refused("an [Unreleased] section with no entries", ["0.3.0"], "no entries")
    write(os.path.join(w.work, "CHANGELOG.md"), CHANGELOG)
    w.git("-C", w.work, "commit", "-q", "-am", "entries again")
    w.git("-C", w.work, "push", "-q", "origin", "main")
    # a tag only origin has (on a commit main doesn't hold, so fetching main doesn't bring it here)
    w.git("-C", other, "switch", "-q", "-c", "side")
    write(os.path.join(other, "side.txt"), "x\n")
    w.git("-C", other, "add", "side.txt")
    w.git("-C", other, "commit", "-q", "-m", "side")
    w.git("-C", other, "tag", "v0.2.5")
    w.git("-C", other, "push", "-q", "origin", "v0.2.5")
    r = w.release("0.2.5", "--dry-run", "--skip-tests")
    check("a VERSION whose tag only origin has is refused", r.returncode != 0
          and "already exists on origin" in r.stderr, r.stderr[-300:])
    r = w.release("0.2.4", "--dry-run", "--skip-tests")
    check("…and so is one older than origin's newest tag", r.returncode != 0
          and "isn't newer than the newest release tag, v0.2.5" in r.stderr, r.stderr[-300:])


def prepare(w):
    print("the dry run, then the real run")
    before = w.state()
    r = w.release("0.3.0", "--dry-run")
    date = time.strftime("%Y-%m-%d")
    check("the dry run passes its checks and shows the CHANGELOG change",
          r.returncode == 0 and f"+## [0.3.0] - {date}" in r.stdout and "Dry run: nothing was changed" in r.stdout,
          (r.returncode, (r.stdout + r.stderr)[-500:]))
    check("…ran check-public and every suite", w.log("check") == ["check-public -a"]
          and sorted(w.log("suite")) == sorted(SUITES), (w.log("check"), len(w.log("suite"))))
    check("…and changed nothing (no branch, commit, push or PR)", w.state() == before and not w.log("gh"), w.log("gh"))
    r = w.release("0.3.0", "--dry-run", "--skip-tests")
    check("--dry-run --skip-tests leaves out the suites", r.returncode == 0 and not w.log("suite"), r.stderr[-200:])

    main_before = w.git("ls-remote", w.origin, "refs/heads/main")
    r = w.release("0.3.0")
    out = r.stdout + r.stderr
    check("the real run finishes and names the next step", r.returncode == 0 and "scripts/release.sh 0.3.0 --tag" in out,
          out[-600:])
    branch = w.git("ls-remote", w.origin, "refs/heads/release-v0.3.0")
    check("…pushes release-v0.3.0, and main on origin is untouched",
          branch and w.git("ls-remote", w.origin, "refs/heads/main") == main_before, (branch, main_before))
    new = w.git("-C", w.work, "show", "origin/release-v0.3.0:CHANGELOG.md") + "\n"
    want = CHANGELOG.replace("## [Unreleased]\n\n", f"## [Unreleased]\n\n## [0.3.0] - {date}\n\n", 1)
    check("…with [Unreleased] moved to [0.3.0] - DATE under a new, empty [Unreleased]", new == want, new)
    msg = w.git("-C", w.work, "log", "-1", "--format=%s", "origin/release-v0.3.0")
    files = w.git("-C", w.work, "diff", "--name-only", "origin/main", "origin/release-v0.3.0")
    check("…in one commit that changes only the CHANGELOG and gemini-extension.json", msg == "Release 0.3.0: CHANGELOG"
          and files.split() == ["CHANGELOG.md", "gemini-extension.json"], (msg, files))
    ext = w.git("-C", w.work, "show", "origin/release-v0.3.0:gemini-extension.json") + "\n"
    check("…whose version line alone becomes 0.3.0", ext == EXTENSION.replace('"version": "0.2.0"', '"version": "0.3.0"'), ext)
    check("…check-public ran again before the push", w.log("check") == ["check-public -a", "check-public -a"], w.log("check"))
    gh = [json.loads(x) for x in w.log("gh")]
    pr = next((a for a in gh if a[:2] == ["pr", "create"]), [])
    check("…opens a PR from release-v0.3.0 against main",
          pr and pr[pr.index("--base") + 1] == "main" and pr[pr.index("--head") + 1] == "release-v0.3.0", gh)
    check("…and leaves the checkout on a clean main",
          w.git("-C", w.work, "rev-parse", "--abbrev-ref", "HEAD") == "main"
          and not w.git("-C", w.work, "status", "--porcelain")
          and w.git("-C", w.work, "rev-parse", "HEAD") == w.git("-C", w.work, "rev-parse", "origin/main"))
    r = w.release("0.3.0")
    check("a second run is refused (the branch exists)", r.returncode != 0 and "already exists" in r.stderr, r.stderr[-200:])


def tag(w):
    print("--tag after the merge")
    before = w.state()
    r = w.release("0.3.0", "--tag")
    check("--tag before the PR is merged is refused", r.returncode != 0 and "no merged pull request" in r.stderr
          and w.state() == before, r.stderr[-300:])
    # GitHub merges the PR: a merge commit on origin's main
    m = os.path.join(w.td, "merger")
    w.git("clone", "-q", w.origin, m)
    w.git("-C", m, "merge", "-q", "--no-ff", "-m", "Merge pull request #99", "origin/release-v0.3.0")
    w.git("-C", m, "push", "-q", "origin", "main")
    sha = w.git("-C", m, "rev-parse", "HEAD")
    write(w.prs, json.dumps([{"number": 99, "state": "MERGED", "mergeCommit": {"oid": "1" * 40},
                              "url": "https://example.invalid/pull/99"}]))
    r = w.release("0.3.0", "--tag")
    check("a merge commit that isn't on origin/main is refused", r.returncode != 0 and "isn't on origin/main" in r.stderr,
          r.stderr[-300:])
    write(w.prs, json.dumps([{"number": 99, "state": "MERGED", "mergeCommit": {"oid": sha},
                              "url": "https://example.invalid/pull/99"}]))
    before = w.state()
    r = w.release("0.3.0", "--tag", "--draft-release", "--dry-run")
    check("--tag --dry-run shows the tag and get.sh's pick today, and changes nothing",
          r.returncode == 0 and f"git tag -a v0.3.0 {sha[:12]}" in r.stdout and "would check out release v0.2.5" in r.stdout
          and w.state() == before and not any(a[:2] == ["release", "create"] for a in map(json.loads, w.log("gh"))),
          (r.returncode, (r.stdout + r.stderr)[-500:]))
    r = w.release("0.3.0", "--tag", "--draft-release")
    out = r.stdout + r.stderr
    check("--tag tags, pushes and checks get.sh", r.returncode == 0 and "Released v0.3.0" in out, out[-600:])
    kind = w.git("--git-dir", w.origin, "cat-file", "-t", "refs/tags/v0.3.0")
    peeled = w.git("--git-dir", w.origin, "rev-parse", "refs/tags/v0.3.0^{commit}")
    check("…an annotated tag v0.3.0 on origin, on the merge commit", kind == "tag" and peeled == sha, (kind, peeled, sha))
    check("…get.sh's dry run ran in a temporary HOME, not yours",
          w.log("getsh") and all(x != f"HOME={w.env['HOME']}" and "mcomp-release." in x for x in w.log("getsh")),
          w.log("getsh"))
    check("…and saw the new tag", f"get.sh would install v0.3.0 (commit {sha[:12]})" in out, out[-400:])
    rel = [a for a in map(json.loads, w.log("gh")) if a[:1] == ["release"]]
    check("…the GitHub Release is only drafted, never published",
          len(rel) == 1 and rel[0][:3] == ["release", "create", "v0.3.0"] and "--draft" in rel[0]
          and "--verify-tag" in rel[0] and not any("draft=false" in x or x == "--latest" for x in rel[0]), rel)
    notes = rel[0][rel[0].index("--notes-file") + 1] if rel and "--notes-file" in rel[0] else ""
    check("…from the CHANGELOG's section (the notes file is gone with the run's temporary folder)",
          notes and not os.path.exists(notes), notes)
    r = w.release("0.3.0", "--tag")
    check("a second --tag is refused (the tag exists)", r.returncode != 0 and "already exists" in r.stderr, r.stderr[-200:])


def bash32_arrays():
    """macOS's bash is 3.2, where "${a[@]}" of an empty array is an unbound variable under set -u: every array the
    release script expands must be guarded (${a[@]+"${a[@]}"}). v0.3.0's --tag stopped at get.sh's check because
    repo_arg is empty for the real repository, a case these tests (a local origin) don't reach."""
    print("bash 3.2 and empty arrays")
    src = open(os.path.join(LAB, "scripts", "release.sh")).read()
    bare = [m.group(0) for m in re.finditer(r'(?<!\+)"\$\{(\w+)\[@\]\}"', src)]
    check("release.sh expands no array without the empty-array guard", not bare, bare)


def main():
    td = tempfile.mkdtemp(prefix="mcomp-release-test-")
    try:
        w = World(td)
        refusals(w)
        prepare(w)
        tag(w)
        bash32_arrays()
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
