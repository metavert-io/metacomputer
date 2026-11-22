#!/usr/bin/env python3
"""The benchmark's session folder and the `claude` it runs (bench/bench.py). No VM, no
API use: a stand-in `claude` records how it was started.

    python3 tests/test_bench_workdir.py

It checks that the work folder is a fresh private folder (0700, yours, a new name each
time, not the old shared /tmp/mcomp-bench/<run>/work), that the shim folder holds only
the two links to this checkout's launcher, and that the session runs the `claude` found
on PATH before the shim folder went in front: one planted in the shim folder, in the old
shared path or in a relative PATH entry is never run, and one other accounts can write
is refused.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "bench"))
import bench  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fake_claude(path, marker):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                f"echo \"$0\" > '{marker}'\n"
                "echo '{\"type\":\"result\",\"subtype\":\"success\",\"total_cost_usd\":0,\"num_turns\":1}'\n")
    os.chmod(path, 0o755)


def test_codex_trust_undo():
    """Codex saves the bench's per-run trust override into your own config.toml when a session starts: the bench puts
    the file back exactly as it was when that table is the only change, and leaves any other change alone."""
    td = tempfile.mkdtemp(prefix="mcomp-trust-")
    try:
        cfg = os.path.join(td, "config.toml")
        work = os.path.join(td, "work")
        os.makedirs(work)
        before = 'model = "x"\n\n[tui]\nscreen_reader_detection_done = true\n'
        added = f'\n[projects."{os.path.realpath(work)}"]\ntrust_level = "trusted"\n'
        with open(cfg, "w") as f:
            f.write(before + added)
        os.chmod(cfg, 0o600)
        r = bench.undo_codex_trust(before, work, cfg)
        check("the run's own trust table is the only change: the file is put back exactly, its mode kept",
              r == "restored" and open(cfg).read() == before and stat.S_IMODE(os.stat(cfg).st_mode) == 0o600, r)
        with open(cfg, "w") as f:
            f.write(before + added + 'personality = "friendly"\n')
        r = bench.undo_codex_trust(before, work, cfg)
        check("...any other change too: the file is left as it is", r == "left" and "personality" in open(cfg).read(), r)
        other = f'\n[projects."{td}/someone-else"]\ntrust_level = "trusted"\n'
        with open(cfg, "w") as f:
            f.write(before + other)
        r = bench.undo_codex_trust(before, work, cfg)
        check("...another folder's trust (not this run's) is left too", r == "left" and "someone-else" in open(cfg).read(), r)
        with open(cfg, "w") as f:
            f.write(before)
        check("...and an unchanged file isn't touched", bench.undo_codex_trust(before, work, cfg) == "unchanged")
        check("no temporary file is left behind", sorted(os.listdir(td)) == ["config.toml", "work"], os.listdir(td))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main():
    test_codex_trust_undo()
    td = tempfile.mkdtemp(prefix="mcomp-bench-test-")
    run_id = "test-" + os.path.basename(td)
    shared = os.path.join("/tmp", "mcomp-bench", run_id)
    try:
        return run(td, run_id, shared)
    finally:
        shutil.rmtree(td, ignore_errors=True)
        shutil.rmtree(shared, ignore_errors=True)


def run(td, run_id, shared):
    os.environ.setdefault("MCOMP_CONFIG", os.path.join(td, "test.env"))
    sw = {"skill": os.path.join(LAB, "skills", "metacomputer"), "coord_guard": "off", "shell": "allow",
          "model": "dummy-model", "budget": "1", "effort": ""}
    planted = os.path.join(td, "planted-ran")
    # Another account's trap at the old predictable path.
    os.makedirs(os.path.join(shared, "work", "bin"))
    fake_claude(os.path.join(shared, "work", "bin", "claude"), planted)

    work, cfg, shim = bench.prepare_workdir(run_id, sw)
    work2, _, _ = bench.prepare_workdir(run_id, sw)
    try:
        st = os.lstat(work)
        check("the work folder is new and private: 0700, yours, not a link", stat.S_ISDIR(st.st_mode)
              and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.getuid(), oct(st.st_mode))
        check("...not the old shared path, and a new name for each run", not work.startswith("/tmp/mcomp-bench")
              and work != work2 and os.path.realpath(shim).startswith(os.path.realpath(work)), (work, work2))
        check("...the shim folder holds only the links to this checkout's launcher",
              sorted(os.listdir(shim)) == ["mcomp", "metacomputer"]
              and all(os.readlink(os.path.join(shim, n)) == bench.CLI for n in ("mcomp", "metacomputer")))
        check("...with the skill under test and the MCP config", os.path.isfile(os.path.join(work, ".claude", "skills",
                                                                                               "metacomputer", "SKILL.md"))
              and os.path.isfile(cfg))

        good_dir = os.path.join(td, "trusted-bin")
        os.makedirs(good_dir)
        ran = os.path.join(td, "trusted-ran")
        fake_claude(os.path.join(good_dir, "claude"), ran)
        rel_dir = os.path.join(td, "rel")
        os.makedirs(rel_dir)
        fake_claude(os.path.join(rel_dir, "claude"), planted)
        old_cwd = os.getcwd()
        os.chdir(rel_dir)
        try:
            got = bench.trusted_claude("." + os.pathsep + good_dir)
        finally:
            os.chdir(old_cwd)
        check("trusted_claude: an absolute path, never a relative PATH entry", got == os.path.realpath(os.path.join(good_dir, "claude")),
              got)
        os.chmod(os.path.join(good_dir, "claude"), 0o777)
        try:
            bench.trusted_claude(good_dir)
            refused = False
        except SystemExit:
            refused = True
        os.chmod(os.path.join(good_dir, "claude"), 0o755)
        check("trusted_claude: a claude other accounts can write is refused", refused)

        # A session: the shim folder goes first on PATH, and a claude planted there isn't run.
        fake_claude(os.path.join(shim, "claude"), planted)
        tdir = os.path.join(td, "task")
        os.makedirs(tdir)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = good_dir + os.pathsep + old_path
        try:
            events, _, sess = bench.run_session({"prompt": "dummy", "timeout_s": 30}, sw, work, cfg, shim, tdir)
        finally:
            os.environ["PATH"] = old_path
        check("a session runs the claude found before the shim folder, by absolute path", sess["exit"] == 0
              and os.path.exists(ran) and open(ran).read().strip() == os.path.realpath(os.path.join(good_dir, "claude"))
              and not os.path.exists(planted), (sess, os.path.exists(planted)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(work2, ignore_errors=True)

    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
