#!/usr/bin/env python3
"""lib/guest_profiles.py, which makes and deletes browser profile folders inside the VM (`metacomputer browser
profiles --new|--delete`), run here against a temporary home folder and a stand-in /proc. No VM.

    python3 tests/test_guest_profiles.py

It checks that a link anywhere on the way to the profiles folder, or a profile that is a link, is refused and nothing
outside is touched; that a running Firefox using the profile blocks the delete whichever way it names it
(`--profile DIR`, `-profile DIR`, `--profile=DIR`), and Chrome with `--user-data-dir`; that bad names are refused;
and that a delete removes the profile without following links inside it.
"""
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(LAB, "lib", "guest_profiles.py")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:400]})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


class World:
    def __init__(self):
        self.td = tempfile.mkdtemp(prefix="mcomp-gprof-")
        self.home = os.path.join(self.td, "home")
        self.proc = os.path.join(self.td, "proc")
        os.makedirs(self.home)
        os.makedirs(self.proc)
        self.ff = os.path.join(self.home, ".local", "state", "metacomputer", "firefox-profiles")
        self.cr = os.path.join(self.home, ".local", "state", "cua-driver", "browser-profiles")

    def run(self, *args):
        """The helper as the VM runs it: python3 -I with the script on standard input."""
        env = {"HOME": self.home, "MCOMP_GUEST_PROC": self.proc, "PATH": os.environ.get("PATH", "")}
        with open(HELPER, "rb") as f:
            return subprocess.run([sys.executable, "-I", "-", *args], stdin=f, capture_output=True, env=env,
                                  timeout=60).returncode

    def process(self, pid, *argv):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")

    def clear_procs(self):
        shutil.rmtree(self.proc)
        os.makedirs(self.proc)

    def close(self):
        shutil.rmtree(self.td, ignore_errors=True)


def main():
    w = World()
    try:
        rc = w.run("new", "firefox", "shop")
        p = os.path.join(w.ff, "shop")
        check("new makes a kept Firefox profile folder (0700), and the folders on the way",
              rc == 0 and os.path.isdir(p) and (os.stat(p).st_mode & 0o777) == 0o700, rc)
        check("new again is fine (the folder is kept)", w.run("new", "firefox", "shop") == 0 and os.path.isdir(p))
        for bad in ("..", ".", "a/b", ".hidden", "x" * 65, "", "a b"):
            if w.run("new", "firefox", bad) != 2 or w.run("delete", "firefox", bad) != 2:
                check(f"a bad name is refused ({bad!r})", False)
                break
        else:
            check("bad names ('..', '.', 'a/b', '.hidden', too long, empty, a space) are refused", True)
        check("check and delete of a profile that doesn't exist: 3", w.run("check", "firefox", "nope") == 3
              and w.run("delete", "chrome", "nope") == 3)

        # in use, each way of naming the profile
        for i, argv in enumerate((["/usr/lib/firefox/firefox", "--profile", p, "--no-remote"],
                                  ["/usr/lib/firefox/firefox", "-profile", p],
                                  ["/usr/lib/firefox/firefox", "--profile=" + p],
                                  ["/usr/lib/firefox/firefox", "-profile", os.path.join(w.ff, ".", "shop")])):
            w.clear_procs()
            w.process(100 + i, *argv)
            got = (w.run("check", "firefox", "shop"), w.run("delete", "firefox", "shop"))
            check(f"a running Firefox with {argv[1].split('=')[0]} {'(another spelling of the path) ' if i == 3 else ''}"
                  "blocks check and delete (4), and the profile stays", got == (4, 4) and os.path.isdir(p), got)
        w.clear_procs()
        os.makedirs(os.path.join(w.cr, "work"))
        w.process(200, "/opt/google/chrome/chrome", "--user-data-dir=" + os.path.join(w.cr, "work"))
        check("a running Chrome with --user-data-dir= blocks deleting its driver profile",
              w.run("delete", "chrome", "work") == 4 and os.path.isdir(os.path.join(w.cr, "work")))
        w.clear_procs()
        w.process(201, "/opt/google/chrome/chrome", "--user-data-dir", os.path.join(w.cr, "work"))
        check("…and with --user-data-dir DIR", w.run("delete", "chrome", "work") == 4)
        w.clear_procs()
        w.process(202, "/usr/lib/firefox/firefox", "--profile", os.path.join(w.ff, "other"))
        check("a Firefox on another profile doesn't block it", w.run("check", "firefox", "shop") == 0)

        # links: the profile itself, the profiles folder, a folder above it
        outside = os.path.join(w.td, "outside")
        os.makedirs(os.path.join(outside, "keep"))
        os.symlink(outside, os.path.join(w.ff, "linked"))
        check("a profile that is a link is refused (5) and what it points at stays",
              w.run("delete", "firefox", "linked") == 5 and os.path.isdir(os.path.join(outside, "keep")))
        docs = os.path.join(w.home, "Documents")
        os.makedirs(os.path.join(docs, "letters"))
        moved = w.ff + ".real"
        os.rename(w.ff, moved)
        os.symlink(w.home, w.ff)                      # firefox-profiles -> the home folder
        check("a profiles folder that is a link (to the home folder) is refused: deleting 'Documents' leaves it",
              w.run("check", "firefox", "Documents") == 5 and w.run("delete", "firefox", "Documents") == 5
              and os.path.isdir(os.path.join(docs, "letters")))
        check("…and new refuses to make a profile through it", w.run("new", "firefox", "shop2") == 5
              and not os.path.exists(os.path.join(w.home, "shop2")))
        os.unlink(w.ff)
        os.rename(moved, w.ff)
        state = os.path.join(w.home, ".local", "state")
        os.rename(state, state + ".real")
        os.symlink(state + ".real", state)            # a folder above it is a link
        check("a link further up the way (~/.local/state) is refused too", w.run("delete", "firefox", "shop") == 5
              and os.path.isdir(os.path.join(state + ".real", "metacomputer", "firefox-profiles", "shop")))
        os.unlink(state)
        os.rename(state + ".real", state)

        # a real delete: links inside the profile are removed, never followed
        os.symlink(outside, os.path.join(p, "link-out"))
        with open(os.path.join(p, "cookies.sqlite"), "w") as f:
            f.write("x")
        w.clear_procs()
        rc = w.run("delete", "firefox", "shop")
        check("delete removes the profile, and a link inside it doesn't take what it points at",
              rc == 0 and not os.path.exists(p) and os.path.isdir(os.path.join(outside, "keep")), rc)
        check("the profiles folder itself stays", os.path.isdir(w.ff))
    finally:
        w.close()
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
