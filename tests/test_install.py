#!/usr/bin/env python3
"""The installers without installing anything, and offline: get.sh (the one-command bootstrap), install.sh's
checks and VM decisions, the VM folder lookup the build relies on, and the live-view password's stores.
No VM, no network, no Homebrew. Every command that looks at the Mac or reaches out (uname, sw_vers, sysctl,
df, curl, xcode-select, brew, ssh, security, the VM runner) is a stub on PATH that logs how it was called,
in a temporary HOME; git is the real one behind a wrapper that refuses network URLs (and GIT_ALLOW_PROTOCOL=
file), so repositories are local folders. One check uses the real `security`, on a throwaway keychain that
exists only inside a temporary HOME.

    python3 tests/test_install.py

- get.sh: --help; option errors (exit 2); the refusals (Intel, Rosetta, not macOS, macOS 13, no network); free
  space on the disks that hold the VM (Lume's storage) and the checkout, counted once when they're the same
  disk; the dry run (changes nothing, runs nothing); the release it picks (newest vX.Y.Z, annotated or not,
  else the default branch) and a failed lookup that stops instead of falling back; a full offline run that
  clones, checks out and verifies the commit before running install.sh, and one that refuses when the
  checkout isn't the commit the repository named; the health check's self-test only on a VM install.sh
  reports it built this run, and on a kept VM only reads (status, the hub's read-only list_windows, start
  only when it's stopped); existing checkouts (local changes, a branch of its own, a
  folder that isn't a checkout); with --confirm (the plugin's setup) only a clean checkout of the project's
  own repository runs; Homebrew's installer is downloaded whole and run only then (on a terminal, through a
  pty), and never without a terminal. No sudo, no password reading of its own.
- install.sh: refusals before changing anything (no Homebrew, no Command Line Tools, too little disk where the
  VM or the checkout goes, no network, a VM listing that fails); a stage that fails partway gives its advice;
  the VM: built is left alone, missing is built, and a VM without its clean snapshot (interrupted build or
  not) or a snapshot without its VM is never replaced without --replace-vm, which --yes doesn't imply; the builds
  of mcomp-vm, the host tools and the vault keep their output in a log and show its end only when they fail; a VM
  build that stopped before it began isn't followed by --replace-vm advice.
- scripts/lib.sh vm_dir / vm_dir_is: the VM's folder comes only from Lume's settings, matched literally and
  exactly once, absolute and existing, and belongs to the VM (its MAC address) before anything is replaced.
- The live-view password: through stdin only (never a command line or an environment, including ssh's and
  security's); the Keychain store uses `security -i` and checks the item, models `security`'s getpass (which
  reads the terminal), ends within its wait with TERM then KILL, falls back to an atomically written 0600
  file, keeps a usable earlier file (made 0600), and never writes through a link; `view` says how to read it.
- metacomputer setup in a checkout with no terminal asks for --yes; the installers name no one's machine.
"""
import fcntl
import json
import os
import pty
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GET = os.path.join(LAB, "get.sh")
INSTALL = os.path.join(LAB, "install.sh")
CLI = os.path.join(LAB, "bin", "metacomputer")
PY = sys.executable
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[-900:]})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


# Stubs: each logs its argv as one JSON line to $STUB_LOG, then behaves as the variables below say.
STUB_HEAD = ('#!/bin/bash\nprintf "%s\\n" "$("$STUB_PY" -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' '
             '"$(basename "$0")" "$@")" >> "$STUB_LOG"\n')
STUBS = {
    "uname": 'case "$1" in -s) echo "${STUB_OS:-Darwin}" ;; -m) echo "${STUB_ARCH:-arm64}" ;; *) echo Darwin ;; esac\n',
    "sw_vers": 'echo "${STUB_MACOS:-15.1}"\n',
    "sysctl": 'case "$2" in hw.memsize) echo "${STUB_MEM:-17179869184}" ;; hw.optional.arm64) echo "${STUB_ARM64:-1}" ;; *) echo 0 ;; esac\n',
    # df PATH: the disk and free space for PATH; STUB_DF="prefix|device|GB;..." (longest match last), else
    # disk-home with STUB_FREE_GB.
    "df": ('for p; do :; done\ndev=disk-home; gb="${STUB_FREE_GB:-100}"\nIFS=";" read -ra maps <<< "${STUB_DF:-}"\n'
           'for m in ${maps[@]+"${maps[@]}"}; do pre="${m%%|*}"; rest="${m#*|}"\n'
           '  case "$p" in "$pre"*) dev="${rest%%|*}"; gb="${rest#*|}" ;; esac; done\n'
           'echo "Filesystem 1024-blocks Used Available Capacity Mounted"\n'
           'echo "$dev 999999999 1 $(( gb * 1048576 )) 1% /"\n'),
    # curl: Homebrew's installer is a script that says it ran (to -o FILE, or stdout); STUB_BREW_CURL_RC makes
    # that download fail after writing it (a cut-off download). Anything else: STUB_CURL_RC.
    "curl": ('out=""; prev=""; for a; do [ "$prev" = -o ] && out="$a"; prev="$a"; done\n'
             'for a; do case "$a" in *Homebrew/install*)\n'
             '  if [ -n "$out" ]; then echo "echo HOMEBREW-INSTALLER-RAN" > "$out"; else echo "echo HOMEBREW-INSTALLER-RAN"; fi\n'
             '  exit "${STUB_BREW_CURL_RC:-0}" ;; esac; done\nexit "${STUB_CURL_RC:-0}"\n'),
    "xcode-select": 'case "$1" in -p) [ -n "${STUB_CLT:-}" ] && echo "$STUB_CLT" && exit 0; exit 2 ;; --install) exit 0 ;; esac\n',
    "brew": 'case "$1" in shellenv) exit 0 ;; esac\nexit 1\n',
    "go": "exit 0\n", "gpg": "exit 0\n", "ffmpeg": "exit 0\n", "swift": "exit 0\n", "swiftc": "exit 0\n",
    "sudo": 'echo "SUDO WAS RUN" >&2\nexit 1\n',
    "dscl": '[ -n "${STUB_NO_ACCT:-}" ] && exit 1\necho "NFSHomeDirectory: $STUB_ACCT_HOME"\n',
    # git: the real one, without the network. STUB_GIT_FAIL_LSREMOTE makes ls-remote fail; STUB_GIT_FAKE_SHA
    # makes it name other commits (a tag that moved, or a push in between).
    "git": ('for a; do case "$a" in http://*|https://*|git://*|ssh://*|git@*) echo "test: no network ($a)" >&2; exit 128 ;; esac; done\n'
            'for a; do [ "$a" = ls-remote ] || continue\n'
            '  [ -z "${STUB_GIT_FAIL_LSREMOTE:-}" ] || { echo "test: ls-remote failed" >&2; exit 128; }\n'
            '  if [ -n "${STUB_GIT_FAKE_SHA:-}" ]; then /usr/bin/git "$@" | sed -E "s/^[0-9a-f]{40}/1111111111111111111111111111111111111111/"; exit "${PIPESTATUS[0]}"; fi\n'
            'done\nexec /usr/bin/git "$@"\n'),
}


class Box:
    """A temporary HOME, a stub folder first on PATH, and a log of every stub call."""

    def __init__(self, without=(), real_git=True, old_python=False):
        self.t = tempfile.mkdtemp(prefix="mcomp-install-")
        self.home = os.path.join(self.t, "home")
        self.stubs = os.path.join(self.t, "stubs")
        self.log = os.path.join(self.t, "calls.log")
        os.makedirs(self.home)
        os.makedirs(self.stubs)
        for name, body in STUBS.items():
            if name not in without:
                write(os.path.join(self.stubs, name), STUB_HEAD + body, 0o755)
        if not real_git:
            write(os.path.join(self.stubs, "git"), STUB_HEAD + "exit 1\n", 0o755)
        if old_python:   # only a python3 older than 3.11 (Apple's 3.9): it fails the version check, else works
            write(os.path.join(self.stubs, "python3"), STUB_HEAD + 'for a; do case "$a" in *version_info*) exit 1 ;; esac; done\n'
                  'exec "$STUB_PY" "$@"\n', 0o755)
        # Apple's Command Line Tools: a folder with usr/bin/swiftc, as xcode-select -p prints it.
        self.clt = os.path.join(self.t, "CommandLineTools")
        write(os.path.join(self.clt, "usr", "bin", "swiftc"), "#!/bin/sh\n", 0o755)
        py = os.path.dirname(os.path.realpath(PY))
        self.path = f"{self.stubs}:{py}:/usr/bin:/bin"
        self.acct = os.path.join(self.t, "acct")
        os.makedirs(self.acct)
        self.env = {"HOME": self.home, "PATH": self.path, "LANG": "C.UTF-8", "STUB_LOG": self.log, "STUB_PY": PY,
                    "STUB_CLT": self.clt, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_ALLOW_PROTOCOL": "file", "GET_SH_BREW_PATHS": os.path.join(self.t, "no-brew"),
                    "TMPDIR": self.t, "MCOMP_CONFIG": os.path.join(self.t, "local.env"),
                    "STUB_ACCT_HOME": self.acct, "MCOMP_LOCK_DIR": os.path.join(self.t, "locks"), "MCOMP_LOCK_WAIT": "1",
                    "MCOMP_BREW_PYTHON": PY if not old_python else os.path.join(self.t, "no-brew-python3")}
        self._repo = None

    def repo(self):
        """A local repository that looks like a metacomputer checkout, made once."""
        if not self._repo:
            self._repo = os.path.join(self.t, "repo")
            make_repo(self._repo, tags=["v0.1.0", "v0.2.0", "v0.10.0", ("v0.11.0", "annotated"), "v1.0.0-rc1", "backup/old"])
        return self._repo

    def environ(self, env):
        e = dict(self.env)
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return e

    def run(self, argv, **env):
        if os.path.exists(self.log):
            os.remove(self.log)
        p = subprocess.run(argv, env=self.environ(env), capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL, start_new_session=True)   # no terminal: nothing can prompt
        calls = [json.loads(x) for x in open(self.log)] if os.path.exists(self.log) else []
        return p, calls

    def get(self, *args, **env):
        return self.run(["/bin/bash", GET, *args], **env)

    def listing(self):
        out = []
        for d, dirs, files in os.walk(self.home):
            out += [os.path.relpath(os.path.join(d, n), self.home) for n in dirs + files]
        return sorted(out)

    def close(self):
        shutil.rmtree(self.t, ignore_errors=True)


def ran(calls, *prefix):
    return [c for c in calls if c[:len(prefix)] == list(prefix)]


GENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


def git(*args, cwd=None):
    return subprocess.run(["git", *args], env=dict(GENV, HOME=tempfile.gettempdir()), check=True, capture_output=True,
                          text=True, cwd=cwd).stdout.strip()


def make_repo(path, tags=(), branch="main"):
    """A small git repository that looks like a metacomputer checkout, with the given tags
    (a name, or (name, "annotated"))."""
    os.makedirs(path)
    # install.sh: says it ran; with STUB_BUILT it reports a VM built this run, as the real one does after a build.
    write(os.path.join(path, "install.sh"),
          '#!/bin/bash\necho "INSTALL-RAN $*"\n'
          '[ -z "${STUB_BUILT:-}" ] || [ -z "${MCOMP_INSTALL_REPORT:-}" ] || echo vm_built=1 >> "$MCOMP_INSTALL_REPORT"\n', 0o755)
    # bin/metacomputer: logs each call to STUB_MC_LOG; `status --json` names the VM's state (STUB_VM_STATE).
    write(os.path.join(path, "bin", "metacomputer"),
          '#!/bin/bash\n[ -z "${STUB_MC_LOG:-}" ] || echo "$*" >> "$STUB_MC_LOG"\n'
          'if [ "${1:-}" = status ] && [ "${2:-}" = --json ]; then\n'
          '  printf \'{\\n  "vm": "%s",\\n  "ready": false\\n}\\n\' "${STUB_VM_STATE:-running}"; exit 1\nfi\n'
          '[ "${1:-}" = call ] && [ -n "${STUB_CALL_FAIL:-}" ] && exit 1\nexit 0\n', 0o755)
    write(os.path.join(path, ".claude-plugin", "marketplace.json"), '{"name": "metacomputer"}\n')
    shutil.copy(os.path.join(LAB, ".gitignore"), os.path.join(path, ".gitignore"))   # what a real checkout ignores
    git("-C", path, "init", "-q", "-b", branch)
    git("-C", path, "add", "-A")
    git("-C", path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
    for tag in tags:
        if isinstance(tag, tuple):
            git("-C", path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "tag", "-a", "-m", "release", tag[0])
        else:
            git("-C", path, "tag", tag)


def run_pty(argv, env, answers, timeout=60):
    """Run with a controlling terminal (a pty); answer each (prompt, reply) once it appears. -> (rc, output)."""
    master, slave = pty.openpty()

    def pre():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    p = subprocess.Popen(argv, env=env, stdin=slave, stdout=slave, stderr=slave, preexec_fn=pre, close_fds=True)
    os.close(slave)
    out, pending, end = b"", list(answers), time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            while pending and pending[0][0].encode() in out:
                os.write(master, pending.pop(0)[1].encode())
        elif p.poll() is not None:
            break
    if p.poll() is None:
        p.kill()
    p.wait()
    os.close(master)
    return p.returncode, out.decode(errors="replace")


# --- get.sh ------------------------------------------------------------------------------------------
def test_get_args():
    b = Box()
    try:
        p, calls = b.get("--help")
        check("get.sh --help: exit 0, lists the options, changes nothing",
              p.returncode == 0 and all(o in p.stdout for o in ("--dir", "--ref", "--repo", "--no-chrome", "--no-build",
                                                                 "--replace-vm", "--confirm", "--yes", "--dry-run"))
              and not b.listing(), p.stdout)
        for bad, why in ((["--bogus"], "unknown option"), (["--dir"], "--dir without a folder"), (["--ref"], "--ref without a name"),
                         (["--repo"], "--repo without a URL"), (["--ref", "-x"], "--ref that looks like an option"),
                         (["--ref", "a..b"], "--ref with '..'")):
            p, calls = b.get(*bad)
            check(f"get.sh {why}: exit 2, nothing run", p.returncode == 2 and not calls and not b.listing(), (p.returncode, p.stderr))
        src = open(GET).read()
        head = " ".join(l.lstrip("#").strip() for l in src.split("set -uo pipefail")[0].splitlines())
        check("get.sh: the top of the file says what it does (steps, password, sudo, safe to rerun)",
              all(w in head for w in ("What it does", "Homebrew", "Command Line Tools", "install.sh", "password", "sudo",
                                      "running it again is safe")), head[:300])
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        check("get.sh: never runs sudo or reads a password itself",
              not re.search(r"(^|[;&|(]\s*)sudo\b", code, re.M) and "read -s" not in code and "find-generic-password" not in code)
        # Truncated anywhere before its last line, the script defines functions and runs nothing.
        lines = src.splitlines(keepends=True)
        ran_any = []
        for cut in (len(lines) // 3, len(lines) // 2, len(lines) - 1):
            t = os.path.join(b.t, f"cut{cut}.sh")
            write(t, "".join(lines[:cut]))
            p, calls = b.run(["/bin/bash", t])
            if calls or p.stdout.strip():
                ran_any.append((cut, calls, p.stdout[:200]))
        check("get.sh cut off before its last line (a broken download) runs nothing", not ran_any, ran_any)
    finally:
        b.close()


def test_get_refusals():
    cases = [
        ("an Intel Mac", {"STUB_ARCH": "x86_64", "STUB_ARM64": "0"}, "Apple silicon"),
        ("a terminal under Rosetta", {"STUB_ARCH": "x86_64", "STUB_ARM64": "1"}, "Rosetta"),
        ("not macOS", {"STUB_OS": "Linux"}, "runs on macOS"),
        ("macOS 13", {"STUB_MACOS": "13.6.1"}, "macOS 14 or later"),
        ("10 GB free on the one disk", {"STUB_FREE_GB": "10"}, "at least 15 GB"),
        ("no network", {"STUB_CURL_RC": "6"}, "can't reach github.com"),
    ]
    for name, env, words in cases:
        b = Box()
        try:
            p, calls = b.get("--dry-run", "--repo", b.repo(), **env)
            p2, calls2 = b.get("--repo", b.repo(), **env)
            check(f"get.sh refuses {name}: exit 1, says why, changes nothing (dry run or not)",
                  p.returncode == 1 and words in p.stderr and p2.returncode == 1 and words in p2.stderr
                  and not ran(calls2, "git", "clone") and not ran(calls2, "xcode-select", "--install") and not b.listing(),
                  (p.returncode, p.stderr, p2.stderr))
        finally:
            b.close()
    b = Box()
    try:
        r = b.repo()
        p, _ = b.get("--dry-run", "--repo", r, "--no-build", STUB_FREE_GB="10")
        check("get.sh with 10 GB free but --no-build: goes on (the disk check is for the VM)", p.returncode == 0, p.stderr)
        p, _ = b.get("--dry-run", "--repo", r, STUB_FREE_GB="25")
        check("get.sh with 25 GB free: goes on, with a warning that the VM's disk grows",
              p.returncode == 0 and "grows as it's used" in p.stdout, p.stdout + p.stderr)
        p, _ = b.get("--dry-run", "--repo", r, STUB_MACOS="14.7")
        check("get.sh on macOS 14: goes on, and says the secrets vault needs 15", p.returncode == 0 and "needs macOS 15" in p.stdout, p.stdout)
        p, _ = b.get("--dry-run", "--repo", r, STUB_MEM=str(8 << 30))
        check("get.sh on an 8 GB Mac: the VM gets 4 GB", p.returncode == 0 and "the VM gets 4 GB" in p.stdout, p.stdout)
    finally:
        b.close()


def test_get_space():
    """Free space is checked on every disk a fresh install writes to: where Lume keeps VMs (an installed runner's
    default location, else the default in ~/.config/lume/config.yaml, else ~/.lume, in the account's home),
    the checkout, the home folder, the temporary folders; summed per disk. Failed discovery refuses."""
    b = Box()
    try:
        r = b.repo()
        big, big2 = os.path.join(b.t, "big"), os.path.join(b.t, "big2")
        os.makedirs(big)
        os.makedirs(big2)
        p, _ = b.get("--dry-run", "--repo", r, STUB_FREE_GB="14")
        check("space: everything on one disk with 14 GB: refused (10 + 2 + 1 + 1 + 1 needed there)",
              p.returncode == 1 and "15 GB" in p.stderr, p.stderr)
        p, _ = b.get("--dry-run", "--repo", r, STUB_FREE_GB="15")
        check("space: everything on one disk with 15 GB: goes on", p.returncode == 0, p.stderr)
        p, _ = b.get("--dry-run", "--repo", r, STUB_DF=f"{b.acct}|disk-vm|9")
        check("space: Lume's VMs (~/.lume in the account's home) on a disk with 9 GB: refused, naming it",
              p.returncode == 1 and "where Lume keeps VMs" in p.stderr and "10 GB" in p.stderr, p.stderr)
        p, _ = b.get("--dry-run", "--repo", r, "--dir", os.path.join(big, "mc"), STUB_FREE_GB="2",
                     STUB_DF=f"{b.acct}|disk-vm|100;{big}|disk-big|100")
        check("space: the VM and checkout on big disks, the home folder's disk with 2 GB: refused, naming the home folder",
              p.returncode == 1 and "in your home folder" in p.stderr and "3 GB" in p.stderr, p.stderr)
        p, _ = b.get("--dry-run", "--repo", r, "--dir", os.path.join(big2, "mc"), STUB_DF=f"{big2}|disk-two|1")
        check("space: the checkout's disk with 1 GB: refused (its downloads need 2)",
              p.returncode == 1 and "the checkout and its downloads" in p.stderr, p.stderr)
        # An installed runner says where Lume keeps VMs: a big disk.
        vms = os.path.join(big, "vms")
        os.makedirs(vms)
        runner = os.path.join(b.home, ".local", "bin", "mcomp-vm")
        write(runner, '#!/bin/bash\n[ "$1" = config ] || exit 0\n[ -n "${STUB_CONFIG_FAIL:-}" ] && exit 1\n'
                      f'printf "Configured VM storage locations:\\n  - ext: {vms} (default)\\n"\n', 0o755)
        p, _ = b.get("--dry-run", "--repo", r, "--dir", os.path.join(big2, "mc"), STUB_FREE_GB="4",
                     STUB_DF=f"{big}|disk-big|100;{big2}|disk-two|5")
        check("space: an installed runner keeps VMs on a big disk; checkout 5 GB elsewhere; home 4 GB: goes on",
              p.returncode == 0 and "100 GB free on the disk with the VM, where Lume keeps VMs" in p.stdout, p.stdout + p.stderr)
        p, _ = b.get("--dry-run", "--repo", r, STUB_CONFIG_FAIL="1")
        check("space: the runner's 'config get' fails: refused, never a guessed ~/.lume",
              p.returncode == 1 and "couldn't tell where Lume will keep the VM" in p.stderr, p.stderr)
        os.remove(runner)
        # No runner yet: Lume's settings file in the account's home names the default location.
        write(os.path.join(b.acct, ".config", "lume", "config.yaml"),
              f'defaultLocationName: "ext"\nvmLocations:\n  - name: "home"\n    path: "~/.lume"\n  - name: "ext"\n    path: "{vms}"\n')
        p, _ = b.get("--dry-run", "--repo", r, STUB_FREE_GB="20", STUB_DF=f"{b.acct}|disk-acct|1;{big}|disk-big|100")
        check("space: no runner yet, ~/.config/lume/config.yaml puts VMs on a big disk: that disk is checked, not ~/.lume",
              p.returncode == 0 and f"the VM, where Lume keeps VMs ({vms})" in p.stdout, p.stdout + p.stderr)
        write(os.path.join(b.acct, ".config", "lume", "config.yaml"), 'defaultLocationName: "nowhere"\nvmLocations:\n  - name: "home"\n    path: "~/.lume"\n')
        p, _ = b.get("--dry-run", "--repo", r)
        check("space: a settings file whose default location isn't listed: refused", p.returncode == 1
              and "couldn't tell where Lume will keep the VM" in p.stderr, p.stderr)
        os.remove(os.path.join(b.acct, ".config", "lume", "config.yaml"))
        p, _ = b.get("--dry-run", "--repo", r, STUB_NO_ACCT="1")
        check("space: the account's home folder can't be told: refused", p.returncode == 1
              and "couldn't tell where Lume will keep the VM" in p.stderr, p.stderr)
    finally:
        b.close()


def test_get_dry_run():
    b = Box()
    try:
        repo = b.repo()
        before = b.listing()
        p, calls = b.get("--dry-run", "--repo", repo)
        out = p.stdout
        check("dry run: exit 0, ends with 'nothing was changed'", p.returncode == 0 and "Dry run: nothing was changed." in out,
              out[-500:] + p.stderr[-300:])
        check("dry run: picks the newest release tag (here an annotated one), not a pre-release, and names its commit",
              "release v0.11.0, commit " in out, out)
        check("dry run: would clone into ~/metacomputer and run install.sh --yes, then start, status and test A",
              f"git clone -q {repo} {b.home}/metacomputer" in out and f"{b.home}/metacomputer/install.sh --yes" in out
              and "metacomputer start" in out and "metacomputer status" in out and "metacomputer test A" in out, out)
        check("dry run: on a VM it keeps, only reads (status and the read-only list_windows), no self-test",
              "on a VM it keeps: only reads" in out and "metacomputer call list_windows" in out, out)
        check("dry run: the VM gets Chrome unless --no-chrome, and it says Google's terms apply", "Google's terms" in out, out)
        check("dry run: numbered steps with time estimates", "[1/5] Checking this Mac" in out and "[5/5]" in out and "minute" in out, out)
        check("dry run: changes nothing in HOME", b.listing() == before, b.listing())
        check("dry run: no clone, install, xcode-select --install or Homebrew installer ran",
              not os.path.exists(os.path.join(b.home, "metacomputer")) and "INSTALL-RAN" not in out
              and not ran(calls, "git", "clone") and not ran(calls, "xcode-select", "--install")
              and "HOMEBREW-INSTALLER-RAN" not in out, calls)
        p, _ = b.get("--dry-run", "--repo", repo, "--no-chrome", "--no-build", "--replace-vm")
        check("dry run --no-chrome --no-build --replace-vm: passes them on, and skips the VM checks",
              "install.sh --yes --no-chrome --no-build --replace-vm" in p.stdout and "skipped: --no-build" in p.stdout
              and "Google's terms" not in p.stdout, p.stdout)
        p, _ = b.get("--dry-run", "--repo", repo, "--ref", "v0.1.0", "--dir", os.path.join(b.t, "elsewhere"))
        check("dry run --ref --dir: uses them", "would check out v0.1.0, commit" in p.stdout and f"{b.t}/elsewhere" in p.stdout, p.stdout)
        p, _ = b.get("--dry-run", "--repo", repo, "--ref", "nonesuch")
        check("dry run --ref that's neither a tag nor a branch: refused", p.returncode == 1 and "neither a tag nor a branch" in p.stderr,
              p.stderr)
        repo2 = os.path.join(b.t, "repo2")
        make_repo(repo2, tags=["v1.0.0-beta"])
        p, _ = b.get("--dry-run", "--repo", repo2)
        check("dry run: with no release yet, the default branch and its commit", "main (no release yet), commit" in p.stdout, p.stdout)
        p, calls = b.get("--dry-run", "--repo", repo, STUB_CLT="")
        check("dry run without the Command Line Tools: says it would run xcode-select --install, doesn't",
              p.returncode == 0 and "xcode-select --install" in p.stdout and not ran(calls, "xcode-select", "--install"), p.stdout)
        # A failed release lookup stops; it never falls back to the default branch.
        p, calls = b.get("--dry-run", "--repo", repo, STUB_GIT_FAIL_LSREMOTE="1")
        check("a release lookup that fails: stops (exit 1), says so, no fallback to main",
              p.returncode == 1 and "couldn't list" in p.stderr and "no release yet" not in p.stdout, p.stdout[-300:] + p.stderr)
    finally:
        b.close()


def test_get_full_offline():
    """get.sh run for real against a local repository: clone, checkout, commit check, install.sh, health check."""
    b = Box()
    try:
        repo = b.repo()
        dest = os.path.join(b.t, "inst")
        p, calls = b.get("--repo", repo, "--dir", dest)
        tag_commit = git("-C", repo, "rev-parse", "v0.11.0^{commit}")
        check("full run: exit 0; clones, checks out the newest release and verifies its commit, then runs install.sh --yes",
              p.returncode == 0 and f"at v0.11.0, commit {tag_commit[:12]}" in p.stdout and "INSTALL-RAN --yes" in p.stdout
              and git("-C", dest, "rev-parse", "HEAD") == tag_commit, p.stdout[-800:] + p.stderr[-400:])
        p, _ = b.get("--repo", repo, "--dir", dest)
        check("full run again: the checkout is found and checked again, exit 0", p.returncode == 0 and "already here" in p.stdout
              and f"at v0.11.0, commit {tag_commit[:12]}" in p.stdout, p.stdout[-600:] + p.stderr[-300:])
        dest2 = os.path.join(b.t, "inst2")
        p, _ = b.get("--repo", repo, "--dir", dest2, STUB_GIT_FAKE_SHA="1")
        check("the checkout isn't the commit the repository named (a moved tag): refused before install.sh runs",
              p.returncode == 1 and "not the commit" in p.stderr and "INSTALL-RAN" not in p.stdout, p.stdout[-400:] + p.stderr)
        repo2 = os.path.join(b.t, "repo2")
        make_repo(repo2)
        dest3 = os.path.join(b.t, "inst3")
        p, _ = b.get("--repo", repo2, "--dir", dest3)
        head = git("-C", repo2, "rev-parse", "HEAD")
        check("full run with no release: the default branch at the commit the repository named",
              p.returncode == 0 and f"at main, commit {head[:12]}" in p.stdout, p.stdout[-500:] + p.stderr[-300:])
        dest4 = os.path.join(b.t, "inst4")
        p, _ = b.get("--repo", repo, "--dir", dest4, STUB_GIT_FAIL_LSREMOTE="1")
        check("full run, the release lookup fails: stops before cloning", p.returncode == 1 and not os.path.exists(dest4), p.stderr)
    finally:
        b.close()


def test_get_health_kept_vm():
    """P1: get.sh's health check runs its self-test (test A closes an editor and answers No to its recovery
    prompt) only on a VM this run built, as install.sh reports in MCOMP_INSTALL_REPORT. A VM the run kept may
    hold the user's work: only metacomputer status, the hub's read-only list_windows, and start only when the
    VM is stopped (on a running one start can restart it)."""
    b = Box()
    try:
        ro = json.loads(subprocess.run([PY, "-c", "import json, sys; sys.path.insert(0, sys.argv[1]); import mcp_hub; "
                                        "print(json.dumps(sorted(mcp_hub.READ_ONLY)))", os.path.join(LAB, "lib")],
                                       env=b.environ({}), capture_output=True, text=True, check=True).stdout)
        repo = b.repo()
        mc_log = os.path.join(b.t, "mc-calls.log")

        def go(n, **env):
            if os.path.exists(mc_log):
                os.remove(mc_log)
            p, _ = b.get("--repo", repo, "--dir", os.path.join(b.t, f"inst{n}"), STUB_MC_LOG=mc_log, **env)
            return p, (open(mc_log).read().splitlines() if os.path.exists(mc_log) else [])

        def reads_only(log):   # what a kept VM may get: status, a stopped VM's start, and the hub's read-only tools
            return all(c in ("status", "status --json", "start")
                       or (len(c.split()) > 1 and c.split()[0] == "call" and c.split()[1] in ro) for c in log)
        p, log = go(1)
        check("kept VM, running: status and the read-only list_windows only; no test A, no start; exit 0",
              p.returncode == 0 and log == ["status --json", "status", "call list_windows {}"] and "list_windows" in ro
              and reads_only(log) and "kept your VM" in p.stdout, (log, p.stdout[-600:], p.stderr[-300:]))
        p, log = go(2, STUB_VM_STATE="stopped")
        check("kept VM, stopped: started (nothing is open in a stopped VM), then status and the read-only check; no test A",
              p.returncode == 0 and log == ["status --json", "start", "status", "call list_windows {}"] and reads_only(log),
              (log, p.stderr[-300:]))
        p, log = go(3, STUB_CALL_FAIL="1")
        check("kept VM, the read-only check fails: says so, exit 0, and still no test A (no fallback to a mutating test)",
              p.returncode == 0 and "didn't answer" in p.stderr + p.stdout and reads_only(log)
              and not any(c.startswith("test") for c in log), (log, p.stderr[-300:]))
        seeded = os.path.join(b.t, "seeded-report")
        write(seeded, "vm_built=1\n")
        p, log = go(4, MCOMP_INSTALL_REPORT=seeded)
        check("an MCOMP_INSTALL_REPORT from the environment is ignored (get.sh makes its own): the VM counts as kept",
              p.returncode == 0 and log and reads_only(log), log)
        p, log = go(5, STUB_BUILT="1")
        check("VM built this run (install.sh reported it): start, status, then the self-test test A",
              p.returncode == 0 and log == ["start", "status", "test A"], (log, p.stderr[-300:]))
        left = [n for n in os.listdir(b.t) if n.startswith("mcomp-install")]
        check("the report file get.sh made is removed afterwards", not left, left)
    finally:
        b.close()


def test_get_homebrew_and_confirm():
    b = Box(without=("brew", "go", "gpg", "ffmpeg"))
    try:
        r = b.repo()
        p, calls = b.get("--dry-run", "--repo", r)
        check("dry run, no Homebrew and tools missing: would offer Homebrew's installer, doesn't run it",
              p.returncode == 0 and "Homebrew's installer" in p.stdout and "HOMEBREW-INSTALLER-RAN" not in p.stdout
              and "password" in p.stdout, p.stdout)
        p, calls = b.get("--yes", "--repo", r)
        check("no Homebrew, no terminal (even with --yes): stops, says to install it in Terminal, changes nothing",
              p.returncode == 1 and "brew.sh" in p.stderr and "HOMEBREW-INSTALLER-RAN" not in p.stdout
              and not ran(calls, "git", "clone") and not b.listing(), (p.stdout[-300:], p.stderr))
        # On a terminal: the installer is downloaded to a file first and run only when the download finished.
        env = b.environ({})
        rc, out = run_pty(["/bin/bash", GET, "--repo", r], dict(env, STUB_BREW_CURL_RC="18"),
                          [("Run Homebrew's installer now?", "y\n")])
        check("Homebrew's download cut off (curl fails after writing part of it): nothing of it runs, exit 1",
              rc == 1 and "couldn't download Homebrew's installer" in out and "nothing was run" in out
              and "HOMEBREW-INSTALLER-RAN" not in out, out[-600:])
        rc, out = run_pty(["/bin/bash", GET, "--repo", r], env, [("Run Homebrew's installer now?", "y\n")])
        check("Homebrew's download finished: its installer runs (on the terminal), then get.sh looks for brew",
              "HOMEBREW-INSTALLER-RAN" in out and "isn't in /opt/homebrew/bin" in out, out[-600:])
        rc, out = run_pty(["/bin/bash", GET, "--repo", r], env, [("Run Homebrew's installer now?", "n\n")])
        check("Homebrew declined: stops, says what's needed, runs nothing", rc == 1 and "HOMEBREW-INSTALLER-RAN" not in out
              and "needs these tools" in out, out[-500:])
    finally:
        b.close()
    b = Box()
    try:
        p, calls = b.get("--confirm")
        check("--confirm with no terminal: explains the plan, changes nothing, exit 3 (rerun with --yes)",
              p.returncode == 3 and "will:" in p.stdout and "--yes" in p.stdout and not calls and not b.listing(),
              (p.returncode, p.stdout[-400:]))
        check("--confirm's plan: says where it goes, the downloads, Homebrew's password and the time",
              f"{b.home}/metacomputer" in p.stdout and "1.5 GB" in p.stdout and "password" in p.stdout and "minutes" in p.stdout, p.stdout)
    finally:
        b.close()


def test_get_existing():
    b = Box()
    try:
        repo = b.repo()
        dest = os.path.join(b.t, "checkout")
        git("clone", "-q", repo, dest)
        p, calls = b.get("--dry-run", "--repo", repo, "--dir", dest)
        check("existing checkout: found, would fetch and switch to the release",
              p.returncode == 0 and "already here" in p.stdout and f"git -C {dest} fetch" in p.stdout
              and "would switch it to release v0.11.0" in p.stdout, p.stdout)
        os.makedirs(os.path.join(b.home, ".local", "bin"))
        os.symlink(os.path.join(dest, "bin", "metacomputer"), os.path.join(b.home, ".local", "bin", "metacomputer"))
        p, _ = b.get("--dry-run", "--repo", repo)
        check("default folder: the checkout ~/.local/bin/metacomputer already runs",
              f"already here: {os.path.realpath(dest)}" in p.stdout, p.stdout)
        os.remove(os.path.join(b.home, ".local", "bin", "metacomputer"))
        write(os.path.join(dest, "install.sh"), "#!/bin/bash\necho changed\n", 0o755)
        p, calls = b.get("--dry-run", "--repo", repo, "--dir", dest)
        check("existing checkout with local changes, run directly: left as it is, says it runs that code as it is",
              p.returncode == 0 and "local changes" in p.stdout and "as it is" in p.stdout and "fetch" not in p.stdout, p.stdout)
        git("-C", dest, "checkout", "-q", "--", "install.sh")
        git("-C", dest, "checkout", "-q", "-b", "my-work")
        p, _ = b.get("--dry-run", "--repo", repo, "--dir", dest)
        check("existing checkout on a branch of its own: not switched or updated", "on the branch my-work" in p.stdout
              and "fetch" not in p.stdout, p.stdout)
        other = os.path.join(b.t, "notes")
        write(os.path.join(other, "todo.txt"), "x\n")
        p, calls = b.get("--dry-run", "--repo", repo, "--dir", other)
        check("a folder that isn't a checkout: refused, nothing touched",
              p.returncode == 1 and "isn't a metacomputer checkout" in p.stderr and os.listdir(other) == ["todo.txt"], p.stderr)
    finally:
        b.close()


def test_get_confirm_provenance():
    """With --confirm (what the plugin's setup runs), only a clean checkout of github.com/metavert-io/metacomputer
    on its default branch is used; any other checkout is refused, so its code never runs."""
    b = Box()
    try:
        repo = b.repo()
        look = os.path.join(b.t, "lookalike")
        git("clone", "-q", repo, look)
        write(os.path.join(look, "install.sh"), "#!/bin/bash\necho LOOKALIKE-RAN\n", 0o755)
        git("-C", look, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-am", "changed")
        p, _ = b.get("--confirm", "--yes", "--dir", look)
        check("--confirm with a checkout from another origin: refused, its install.sh never runs",
              p.returncode == 1 and "isn't a plain checkout of the project" in p.stderr and "origin isn't github.com/metavert-io/metacomputer" in p.stderr
              and "LOOKALIKE-RAN" not in p.stdout, p.stdout[-300:] + p.stderr)
        p, _ = b.get("--dry-run", "--dir", look)
        check("the same checkout run directly (no --confirm): used as it is, and it says it trusts that code",
              p.returncode == 0 and "runs the code in" in p.stdout, p.stdout[-400:] + p.stderr[-300:])
        git("-C", look, "remote", "set-url", "origin", "https://github.com/metavert-io/metacomputer.git")
        write(os.path.join(look, "install.sh"), "#!/bin/bash\necho EDITED\n", 0o755)
        p, _ = b.get("--confirm", "--yes", "--dir", look)
        check("--confirm with the project's origin but local changes: refused", p.returncode == 1 and "local changes" in p.stderr
              and "EDITED" not in p.stdout, p.stderr)
        git("-C", look, "checkout", "-q", "--", "install.sh")
        p, _ = b.get("--confirm", "--yes", "--dir", look)
        check("--confirm with a clean checkout of the project's origin: accepted (here it then needs the network, which tests refuse)",
              "isn't a plain checkout" not in p.stderr and "couldn't list" in p.stderr, p.stderr)
    finally:
        b.close()


# --- one install, and one build of a VM, at a time -------------------------------------------------------
def test_install_lock():
    """Two real concurrent install.sh runs (different checkouts and homes, the same user): the second waits
    MCOMP_LOCK_WAIT, then refuses without changing anything; the first finishes. The same for get.sh."""
    locks = tempfile.mkdtemp(prefix="mcomp-install-locks-")
    a, root_a = vm_box("")
    b, root_b = vm_box("")
    try:
        for box in (a, b):
            box.env["MCOMP_LOCK_DIR"] = locks
        # A's build takes a while (its stand-in bin/metacomputer sleeps), so B starts while A holds the lock.
        write(os.path.join(root_a, "bin", "metacomputer"), f'#!/bin/bash\nsleep 4\necho "$*" >> "{a.t}/mc.log"\n', 0o755)
        pa = subprocess.Popen([os.path.join(root_a, "install.sh"), "--yes", "--no-claude"], env=a.environ({}),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(2)
        pb, _ = b.run([os.path.join(root_b, "install.sh"), "--yes", "--no-claude"])
        out_a, err_a = pa.communicate(timeout=120)
        log_a = open(os.path.join(a.t, "mc.log")).read().split() if os.path.exists(os.path.join(a.t, "mc.log")) else []
        check("two installs at once: the second waits, then refuses (exit 1) saying another install is running, and builds nothing",
              pb.returncode == 1 and "another metacomputer install is running" in pb.stderr and not os.path.exists(os.path.join(b.t, "mc.log"))
              and not os.path.exists(b.env["MCOMP_CONFIG"].replace("local.env", "never")), (pb.returncode, pb.stderr[-400:]))
        check("... and the first finishes its build", pa.returncode == 0 and log_a == ["build"], (pa.returncode, err_a[-300:], log_a))
        pb, _ = b.run([os.path.join(root_b, "install.sh"), "--yes", "--no-claude"])
        check("... once the first is done, the second runs", pb.returncode == 0 and open(os.path.join(b.t, "mc.log")).read().split() == ["build"],
              (pb.returncode, pb.stderr[-300:]))
    finally:
        a.close()
        b.close()
    # get.sh: the first holds the lock through its whole run (its install.sh is slow); a second get.sh refuses.
    a, b = Box(), Box()
    try:
        for box in (a, b):
            box.env["MCOMP_LOCK_DIR"] = locks
        slow = os.path.join(a.t, "slowrepo")
        make_repo(slow, tags=["v0.1.0"])
        write(os.path.join(slow, "install.sh"), "#!/bin/bash\nsleep 4\necho \"INSTALL-RAN $*\"\n", 0o755)
        git("-C", slow, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-am", "slow")
        git("-C", slow, "tag", "-f", "v0.1.0")
        pa = subprocess.Popen(["/bin/bash", GET, "--repo", slow, "--dir", os.path.join(a.t, "inst")], env=a.environ({}),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(2.5)
        pb, _ = b.get("--repo", b.repo(), "--dir", os.path.join(b.t, "inst"))
        out_a, _ = pa.communicate(timeout=120)
        check("two get.sh runs at once: the second refuses before cloning; the first finishes",
              pb.returncode == 1 and "another metacomputer install is running" in pb.stderr and not os.path.exists(os.path.join(b.t, "inst"))
              and pa.returncode == 0 and "INSTALL-RAN" in out_a, (pb.returncode, pb.stderr[-300:], pa.returncode, out_a[-300:]))
    finally:
        a.close()
        b.close()
        shutil.rmtree(locks, ignore_errors=True)


def build_root(t):
    """A copy of what scripts/build-vm.sh reads, with stand-ins for its downloads and tools, and a runner whose
    VMs live in a state file (its calls logged)."""
    root = os.path.join(t, "lab")
    for d in ("scripts", "config", "lib"):
        os.makedirs(os.path.join(root, d))
    for f in ("scripts/lib.sh", "scripts/build-vm.sh", "config/defaults.env", "lib/vms.py", "lib/termsafe.py",
              "lib/safedelete.py", "scripts/safe-rmtree.sh"):
        shutil.copy2(os.path.join(LAB, f), os.path.join(root, f))
    write(os.path.join(root, "scripts", "build-tools.sh"), '#!/bin/bash\nsleep "${STUB_TOOLS_SLEEP:-0}"\n', 0o755)
    write(os.path.join(root, "scripts", "fetch-artifacts.sh"), "#!/bin/bash\nexit 0\n", 0o755)
    runner = os.path.join(t, "runner")
    write(runner, f"""#!{PY}
import json, os, sys
st = os.environ["STUB_RUNNER_STATE"]
vms = json.load(open(st)) if os.path.exists(st) else []
a = sys.argv[1:]
open(st + ".log", "a").write(" ".join(a[:2]) + "\\n")
if a[:1] == ["ls"]:
    if os.environ.get("STUB_LS_FAIL"):
        sys.exit(1)
    n = int(open(st + ".n").read()) if os.path.exists(st + ".n") else 0
    open(st + ".n", "w").write(str(n + 1))
    late = os.environ.get("STUB_LATE_VM")          # a VM that shows up after the first look
    shown = vms + ([late] if late and n >= 2 else [])
    print(json.dumps([{{"name": v}} for v in shown])); sys.exit(0)
if a[:1] == ["delete"]:
    sys.exit(0)
if a[:2] == ["config", "get"]:                     # where it keeps VMs (build-vm.sh's identity check needs it)
    os.makedirs(os.path.dirname(st) + "/vms", exist_ok=True)
    print("Configured VM storage locations:\\n  - home: " + os.path.dirname(st) + "/vms (default)"); sys.exit(0)
if a[:1] == ["create"]:
    sys.exit(1)                                     # the test stops each build here
if a[:1] == ["get"]:                                # where a VM is (runner_delete checks its folder before a delete)
    print(json.dumps([{{"name": a[1], "status": "stopped", "locationName": "home"}}])); sys.exit(0)
if a[:1] == ["stop"]:
    sys.exit(1)
sys.exit(0)
""", 0o755)
    return root, runner


def test_build_vm_locks():
    """scripts/build-vm.sh: one build of a VM at a time (a lock held from before the first look to the end),
    a failed listing never taken as 'absent', and nothing deleted without --replace."""
    t = tempfile.mkdtemp(prefix="mcomp-install-")
    try:
        root, runner = build_root(t)
        locks = os.path.join(t, "locks")

        def env(n, **extra):
            conf = os.path.join(t, f"b{n}.env")
            write(conf, f'VM_NAME=mcbuild\nBASELINE_NAME=mcbuild-baseline\nLUME="{runner}"\nSSH_DIR="{t}/keys{n}"\n'
                        f'RUN_DIR="{t}/run{n}"\nLOG_DIR="{t}/logs{n}"\nSSH_PORT=2998\n')
            e = {"HOME": os.path.join(t, "home"), "PATH": f"{os.path.dirname(os.path.realpath(PY))}:/usr/bin:/bin:/usr/sbin:/sbin",
                 "MCOMP_CONFIG": conf, "PY": PY, "MCOMP_LOCK_DIR": locks, "MCOMP_LOCK_WAIT": "1",
                 "STUB_RUNNER_STATE": os.path.join(t, f"vms{n}.json"), "LANG": "C.UTF-8"}
            e.update(extra)
            return e

        def calls(n):
            q = os.path.join(t, f"vms{n}.json.log")
            return open(q).read().splitlines() if os.path.exists(q) else []
        build = os.path.join(root, "scripts", "build-vm.sh")
        pa = subprocess.Popen([build], env=env(1, STUB_TOOLS_SLEEP="4"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(2)
        pb = subprocess.run([build, "--replace"], env=env(2), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        out_a, _ = pa.communicate(timeout=120)
        check("two builds of one VM at once: the second (even with --replace) waits, then refuses, listing, deleting and creating nothing",
              pb.returncode != 0 and "another build or change of the VM mcbuild is running" in pb.stdout + pb.stderr and calls(2) == [],
              (pb.returncode, (pb.stdout + pb.stderr)[-300:], calls(2)))
        check("... while the first goes on to create its VM", "create mcbuild" in calls(1), (calls(1), out_a[-300:]))
        p = subprocess.run([build], env=env(3, STUB_LS_FAIL="1"), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        check("a failed VM listing: the build stops, creating and deleting nothing (it's never taken as 'no VM')",
              p.returncode != 0 and "couldn't list the VMs" in p.stdout + p.stderr
              and not any(c.split()[0] in ("create", "delete") for c in calls(3)), (calls(3), (p.stdout + p.stderr)[-300:]))
        p = subprocess.run([build], env=env(4, STUB_LATE_VM="mcbuild"), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        check("a VM that appears after the first look, without --replace: not deleted, the build stops",
              p.returncode != 0 and "appeared while this build was getting ready" in p.stdout + p.stderr
              and not any(c.startswith("delete") for c in calls(4)), (calls(4), (p.stdout + p.stderr)[-300:]))
        json.dump(["mcbuild", "mcbuild-baseline"], open(os.path.join(t, "vms5.json"), "w"))
        p = subprocess.run([build, "--replace"], env=env(5), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        check("with --replace, an existing VM and baseline are deleted, then the VM is created",
              "delete mcbuild" in calls(5) and "delete mcbuild-baseline" in calls(5) and "create mcbuild" in calls(5),
              (calls(5), (p.stdout + p.stderr)[-600:]))
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_lock_lifetime():
    """A lock lasts as long as the last process doing the work: killing the install that took it doesn't free it
    while its child still runs; it's free once everything has exited; a daemon started under it doesn't keep it."""
    locks = tempfile.mkdtemp(prefix="mcomp-install-locks-")
    a, root_a = vm_box("")
    b, root_b = vm_box("")
    try:
        for box in (a, b):
            box.env["MCOMP_LOCK_DIR"] = locks
        write(os.path.join(root_a, "bin", "metacomputer"), f'#!/bin/bash\nsleep 6\necho "$*" >> "{a.t}/mc.log"\n', 0o755)
        pa = subprocess.Popen([os.path.join(root_a, "install.sh"), "--yes", "--no-claude"], env=a.environ({}),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        time.sleep(2.5)
        pa.kill()                     # the install itself, not its child (its build goes on)
        pa.wait()
        pb, _ = b.run([os.path.join(root_b, "install.sh"), "--yes", "--no-claude"])
        check("the install that took the lock is killed while its child works: a second install still refuses",
              pb.returncode == 1 and "another metacomputer install is running" in pb.stderr and not os.path.exists(os.path.join(b.t, "mc.log")),
              (pb.returncode, pb.stderr[-300:]))
        time.sleep(5)
        pb, _ = b.run([os.path.join(root_b, "install.sh"), "--yes", "--no-claude"])
        check("... once the child is done too, the lock is free (nothing stale)", pb.returncode == 0
              and open(os.path.join(a.t, "mc.log")).read().split() == ["build"], (pb.returncode, pb.stderr[-300:]))
    finally:
        a.close()
        b.close()
    t = tempfile.mkdtemp(prefix="mcomp-install-")
    try:
        conf = os.path.join(t, "c.env")
        write(conf, f'LUME="{t}/no-lume"\nRUN_DIR="{t}/run"\nLOG_DIR="{t}/logs"\n')
        env = {"HOME": t, "PATH": "/usr/bin:/bin", "MCOMP_CONFIG": conf, "MCOMP_LOCK_DIR": locks, "PY": PY}
        lib = os.path.join(LAB, "scripts", "lib.sh")
        subprocess.run(["/bin/bash", "-c", f'. "{lib}"; lock_hold vm-d 1 holder; daemonize "{t}/d.log" sleep 6'], env=env, timeout=30)
        p = subprocess.run(["/bin/bash", "-c", f'. "{lib}"; lock_hold vm-d 1 next && echo TAKEN'], env=env, capture_output=True, text=True, timeout=30)
        check("a daemon started while a lock was held doesn't keep it (daemonize closes inherited descriptors)", "TAKEN" in p.stdout, p.stderr[-200:])
        p = subprocess.run(["/bin/bash", "-c", f'. "{lib}"; lock_hold vm-e 1 outer; bash -c ". \\"{lib}\\"; lock_hold vm-e 1 inner && echo INHERITED"'],
                           env=env, capture_output=True, text=True, timeout=30)
        check("a child of the holder holds the lock already (its inherited descriptor)", "INHERITED" in p.stdout, p.stderr[-200:])
    finally:
        shutil.rmtree(t, ignore_errors=True)
        shutil.rmtree(locks, ignore_errors=True)


def test_vm_mutation_locks():
    """Every command that deletes, replaces, restores or snapshots the VM, its baseline or its checkpoints takes
    the build's VM lock: while it's held (here by a build, and by a plain holder), each refuses and touches nothing."""
    t = tempfile.mkdtemp(prefix="mcomp-install-")
    try:
        root, runner = build_root(t)
        os.makedirs(os.path.join(root, "bin"))
        shutil.copy(CLI, os.path.join(root, "bin", "metacomputer"))
        locks = os.path.join(t, "locks")
        conf = os.path.join(t, "m.env")
        write(conf, f'VM_NAME=mcbuild\nBASELINE_NAME=mcbuild-baseline\nLUME="{runner}"\nSSH_DIR="{t}/keys"\n'
                    f'RUN_DIR="{t}/run"\nLOG_DIR="{t}/logs"\nSSH_PORT=2997\n')
        state = os.path.join(t, "vms.json")
        json.dump(["mcbuild", "mcbuild-baseline", "mcbuild-ckpt-one"], open(state, "w"))
        env = {"HOME": os.path.join(t, "home"), "PATH": f"{os.path.dirname(os.path.realpath(PY))}:/usr/bin:/bin:/usr/sbin:/sbin",
               "MCOMP_CONFIG": conf, "PY": PY, "MCOMP_LOCK_DIR": locks, "MCOMP_LOCK_WAIT": "1", "STUB_RUNNER_STATE": state, "LANG": "C.UTF-8"}
        mc = os.path.join(root, "bin", "metacomputer")

        def mutations():
            q = state + ".log"
            return [c for c in (open(q).read().splitlines() if os.path.exists(q) else []) if c.split()[0] in ("delete", "clone", "create")]
        # Restore racing a build: the build holds the VM's lock from its first look; the restore refuses.
        pa = subprocess.Popen([os.path.join(root, "scripts", "build-vm.sh"), "--replace"], env=dict(env, STUB_TOOLS_SLEEP="5"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        time.sleep(2)
        before = mutations()
        p = subprocess.run([mc, "restore", "one", "--yes"], env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        check("restore while a build of the same VM runs: refused, nothing deleted or cloned by it",
              p.returncode != 0 and "another build or change of the VM mcbuild is running" in p.stderr and mutations() == before,
              (p.returncode, p.stderr[-300:], mutations()))
        pa.wait(timeout=120)
        # A plain holder of the lock; each mutating command refuses.
        holder = subprocess.Popen(["/bin/bash", "-c", f'. "{root}/scripts/lib.sh"; lock_hold vm-mcbuild 1 "the test holder"; sleep 15'],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        if os.path.exists(state + ".log"):
            os.remove(state + ".log")
        for cmd in (["reset", "--yes"], ["restore", "one", "--yes"], ["checkpoint", "two"], ["checkpoint", "--delete", "one", "--yes"],
                    ["baseline", "--yes"], ["build", "--replace"], ["_vnc-install"]):
            p = subprocess.run([mc, *cmd], env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            check(f"metacomputer {' '.join(cmd)} while the VM's lock is held: refused, naming the holder, touching nothing",
                  p.returncode != 0 and "the test holder" in p.stdout + p.stderr and not mutations(), (p.returncode, (p.stdout + p.stderr)[-300:], mutations()))
        holder.kill()
        holder.wait()
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_build_tools_stamps():
    """scripts/build-tools.sh rebuilds a host tool whenever its binary is missing, its pinned source changed or
    the binary isn't the one that was built; --check builds and changes nothing; --rebuild --fresh-source builds
    from a new clone, never the checkout's .cache/cua-src; git never runs a checkout's hooks."""
    t = tempfile.mkdtemp(prefix="mcomp-install-")
    try:
        root = os.path.join(t, "lab")
        patches = sorted(f"patches/netgw/{n}" for n in os.listdir(os.path.join(LAB, "patches", "netgw")) if n.endswith(".patch"))
        for f in ("scripts/build-tools.sh", "scripts/lib.sh", "config/defaults.env", "tools/qcow2raw/go.mod", "tools/qcow2raw/go.sum",
                  "tools/qcow2raw/main.go", "scripts/safe-rmtree.sh", "lib/safedelete.py", "lib/vms.py", "lib/termsafe.py",
                  *patches):
            os.makedirs(os.path.dirname(os.path.join(root, f)), exist_ok=True)
            shutil.copy(os.path.join(LAB, f), os.path.join(root, f))
        stubs = os.path.join(t, "stubs")
        log = os.path.join(t, "calls.log")
        commit = re.search(r"^CUA_SRC_COMMIT=(\w+)", open(os.path.join(LAB, "config", "defaults.env")).read(), re.M).group(1)
        write(os.path.join(stubs, "go"), '#!/bin/bash\necho "go $*" >> "$STUB_LOG"\nout=""; prev=""; for a; do [ "$prev" = -o ] && out="$a"; prev="$a"; done\n'
                                          'printf "#!/bin/sh\\necho built-%s\\n" "$RANDOM" > "$out"; chmod +x "$out"\n', 0o755)
        tree = os.path.join(t, "tree")
        os.makedirs(os.path.join(tree, "libs", "cua", "netgw", "cmd", "cua-netgw"))
        write(os.path.join(stubs, "git"), '#!/bin/bash\necho "git $*" >> "$STUB_LOG"\nfor a; do last="$a"; done\n'
                                           'case " $* " in *" clone "*) mkdir -p "$last/.git" "$last/libs/cua/netgw" ;; *" rev-parse HEAD"*) echo "${STUB_COMMIT}" ;;\n'
                                           '  *" archive "*) tar -cf - -C "$STUB_TREE" libs ;; *" apply "*) [ ! -e "$STUB_TREE/refuse-patch" ] || exit 1 ;; esac\nexit 0\n', 0o755)
        write(os.path.join(stubs, "codesign"), "#!/bin/sh\nexit 0\n", 0o755)
        conf = os.path.join(t, "c.env")
        write(conf, f'RUN_DIR="{t}/run"\nLOG_DIR="{t}/logs"\nLUME="{t}/no-lume"\n')
        env = {"HOME": t, "PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": log, "STUB_COMMIT": commit, "MCOMP_CONFIG": conf, "PY": PY,
               "TMPDIR": t, "STUB_TREE": tree}
        bt = os.path.join(root, "scripts", "build-tools.sh")

        def run(*args, **extra):
            if os.path.exists(log):
                os.remove(log)
            p = subprocess.run([bt, *args], env=dict(env, **extra), capture_output=True, text=True, timeout=60)
            return p, (open(log).read().splitlines() if os.path.exists(log) else [])
        p, _ = run("--check")
        check("build-tools --check with nothing built: not up to date (exit 1)", p.returncode == 1)
        p, calls = run()
        gos = [c for c in calls if c.startswith("go build")]
        check("build-tools: builds both tools and stamps them", p.returncode == 0 and len(gos) == 2
              and os.path.exists(os.path.join(root, "vendor", "bin", ".mcomp-netgw.built")), (p.stderr[-300:], calls))
        check("build-tools: the gateway is cua-netgw's pinned source exported (never the cache's working tree) with every "
              "patch in patches/netgw/ applied, built as mcomp-netgw", any(" archive " in f" {c} " and commit in c for c in calls)
              and len([c for c in calls if " apply " in f" {c} "]) == len(patches) >= 1
              and any(c.startswith("go build") and "vendor/bin/mcomp-netgw" in c and "+mcomp." in c for c in calls), calls)
        check("build-tools: git runs with hooks and fsmonitor off", all("core.hooksPath=/dev/null" in c for c in calls if c.startswith("git ")), calls)
        p, calls = run("--check")
        check("build-tools --check after a build: up to date (exit 0), nothing run", p.returncode == 0 and not calls, calls)
        p, calls = run()
        check("build-tools again: nothing rebuilt", p.returncode == 0 and not [c for c in calls if c.startswith("go build")], calls)
        write(os.path.join(root, "vendor", "bin", "mcomp-netgw"), "#!/bin/sh\necho swapped\n", 0o755)
        p, calls = run()
        check("an mcomp-netgw that isn't the binary that was built: rebuilt (only it)", p.returncode == 0
              and len([c for c in calls if c.startswith("go build")]) == 1 and "mcomp-netgw" in " ".join(calls), calls)
        with open(os.path.join(root, patches[0]), "a") as fh:
            fh.write("\n")
        p, calls = run()
        check("a patch that changed: the gateway rebuilt", p.returncode == 0
              and len([c for c in calls if c.startswith("go build") and "mcomp-netgw" in c]) == 1, calls)
        write(os.path.join(tree, "refuse-patch"), "x\n")
        with open(os.path.join(root, patches[0]), "a") as fh:
            fh.write("\n")
        p, calls = run()
        check("a patch that doesn't apply: the build stops, nothing built", p.returncode != 0 and "doesn't apply" in p.stderr
              and not [c for c in calls if c.startswith("go build") and "mcomp-netgw" in c], (p.stderr[-200:], calls))
        os.remove(os.path.join(tree, "refuse-patch"))
        saved = {f: open(os.path.join(root, f)).read() for f in patches}
        for f in patches:
            os.remove(os.path.join(root, f))
        p, calls = run()
        check("no patches at all: refused (mcomp-netgw is never plain cua-netgw under another name)",
              p.returncode != 0 and "no patches" in p.stderr, p.stderr[-200:])
        for f, text in saved.items():
            write(os.path.join(root, f), text)
        with open(conf, "a") as fh:
            fh.write("CUA_SRC_COMMIT=0123456789abcdef0123456789abcdef01234567\n")
        p, calls = run(STUB_COMMIT="0123456789abcdef0123456789abcdef01234567")
        check("a new pinned commit: the gateway rebuilt from it", p.returncode == 0
              and any("fetch" in c and "0123456789abcdef" in c for c in calls), calls)
        write(os.path.join(root, ".cache", "cua-src", "MINE"), "keep\n")
        p, calls = run("--rebuild", "--fresh-source", STUB_COMMIT="0123456789abcdef0123456789abcdef01234567")
        check("--rebuild --fresh-source: both rebuilt, from a new temporary clone; the checkout's .cache/cua-src untouched",
              p.returncode == 0 and len([c for c in calls if c.startswith("go build")]) == 2
              and any(" clone " in f" {c} " and os.path.join(root, ".cache") not in c for c in calls)
              and not any(os.path.join(root, ".cache", "cua-src") in c for c in calls)
              and open(os.path.join(root, ".cache", "cua-src", "MINE")).read() == "keep\n", calls)
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_install_tools_and_space():
    """install.sh builds the host tools on every run that finds them out of date, whether or not it builds the VM
    (from new clones with --fresh-sources), and checks the checkout's disk whenever it builds anything there."""
    for stale, extra, want in (("1", (), ["tools"]), ("1", ("--fresh-sources",), ["tools --rebuild --fresh-source"]), ("", (), [])):
        b, root = vm_box("mctest,mctest-baseline")
        try:
            if stale:
                b.env["STUB_TOOLS_STALE"] = "1"
            p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude", *extra])
            calls = open(os.path.join(b.t, "tools.log")).read().splitlines() if os.path.exists(os.path.join(b.t, "tools.log")) else []
            check(f"VM already built, host tools {'out of date' if stale else 'up to date'}{' with --fresh-sources' if extra else ''}: "
                  f"{'built' if want else 'not built'}, and no VM build", p.returncode == 0 and calls == want
                  and not os.path.exists(os.path.join(b.t, "mc.log")), (p.returncode, calls, p.stderr[-300:]))
        finally:
            b.close()
    # The runner isn't installed and --no-build: mcomp-vm's source and the tools are built in the checkout, so its disk counts.
    b = Box(real_git=False)
    try:
        root = install_root(b)
        b.env["STUB_TOOLS_STALE"] = "1"
        p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude", "--no-build"], STUB_DF=f"{root}|disk-co|0")
        check("--no-build with the runner and tools to build: the checkout's disk is checked (0 GB: refused before any change)",
              p.returncode == 1 and "sources and tools built in this checkout" in p.stderr and not os.path.exists(os.path.join(b.t, "tools.log")),
              (p.returncode, p.stderr[-400:]))
    finally:
        b.close()


def test_get_confirm_local_state():
    """--confirm never deletes anything in an existing checkout. It accepts only tracked files, the installer's
    data (logs, run state, verified downloads) and a local.env whose settings only set values; anything else
    (build outputs, other .cache entries, a Lume storage location inside the checkout, git settings beyond a
    plain clone's, hooks, unknown files, settings that choose a command, a source or a pin) is refused."""
    b = Box()
    try:
        repo = b.repo()

        def clean_clone(name):
            d = os.path.join(b.t, name)
            git("clone", "-q", repo, d)
            git("-C", d, "remote", "set-url", "origin", "https://github.com/metavert-io/metacomputer.git")
            return d

        def tree(d):
            out = []
            for root, dirs, files in os.walk(d):
                if ".git" in dirs:
                    dirs.remove(".git")
                out += [os.path.relpath(os.path.join(root, f), d) for f in files]
            return sorted(out)
        cases = [
            ("a config/local.env that runs a command", lambda d: write(os.path.join(d, "config", "local.env"),
                                                                       'BROWSERS="firefox"\nVM_NAME=$(touch /tmp/pwned)\n'), "choose a command, a source or a pin"),
            ("a VNC_PASSWORD_GET command in local.env", lambda d: write(os.path.join(d, "config", "local.env"),
                                                                        "VNC_PASSWORD_STORE=command\nVNC_PASSWORD_GET='touch /tmp/x'\n"),
             "choose a command, a source or a pin"),
            ("a source override (MCOMP_VM_REPO) in local.env", lambda d: write(os.path.join(d, "config", "local.env"),
                                                                              "MCOMP_VM_REPO=https://example.invalid/x.git\n"),
             "choose a command, a source or a pin"),
            ("a runner elsewhere (LUME) in local.env", lambda d: write(os.path.join(d, "config", "local.env"), 'LUME="/tmp/other-runner"\n'),
             "choose a command, a source or a pin"),
            ("an unknown setting in local.env", lambda d: write(os.path.join(d, "config", "local.env"), "SOMETHING=1\n"),
             "choose a command, a source or a pin"),
            ("a git hook", lambda d: write(os.path.join(d, ".git", "hooks", "post-checkout"), "#!/bin/sh\necho hook\n", 0o755), "git hooks"),
            ("a file-system monitor in its git settings", lambda d: git("-C", d, "config", "core.fsmonitor", "/bin/echo"), "git settings beyond"),
            ("a URL rewrite in its git settings", lambda d: git("-C", d, "config", "url.https://example.invalid/.insteadOf", "https://github.com/"),
             "git settings beyond"),
            ("a file git doesn't know", lambda d: write(os.path.join(d, "extra.sh"), "#!/bin/sh\n"), "files git doesn't know"),
            ("build outputs in vendor/", lambda d: write(os.path.join(d, "vendor", "bin", "cua-netgw"), "#!/bin/sh\n", 0o755), "vendor/"),
            ("an unknown .cache entry", lambda d: write(os.path.join(d, ".cache", "mystuff", "notes.txt"), "mine\n"), ".cache/mystuff"),
            ("cached sources (.cache/cua-src)", lambda d: write(os.path.join(d, ".cache", "cua-src", "x"), "x\n"), ".cache/cua-src"),
            ("Python bytecode (__pycache__)", lambda d: write(os.path.join(d, "lib", "__pycache__", "m.pyc"), "x"), "lib/__pycache__"),
            # rclone's download: the unpacked program and a digest file beside it aren't needed (storage checks the
            # program against its pinned digest and unpacks it again from the zip), so get.sh refuses them.
            ("the unpacked rclone program and its recorded digest", lambda d: [
                write(os.path.join(d, ".cache", "rclone-v1.75.2", "SHA256SUMS"), "x\n"),
                write(os.path.join(d, ".cache", "rclone-v1.75.2", "rclone"), "#!/bin/sh\n", 0o755),
                write(os.path.join(d, ".cache", "rclone-v1.75.2", "rclone.sha256"), "x\n")], ".cache/rclone-v1.75.2/rclone"),
            ("a program in a subfolder of rclone's download", lambda d: write(os.path.join(d, ".cache", "rclone-v1.75.2", "x", "SHA256SUMS"),
                                                                             "#!/bin/sh\n", 0o755), ".cache/rclone-v1.75.2/x/SHA256SUMS"),
        ] + [(f"an rclone pin or source ({k}) in local.env", lambda d, k=k, v=v: write(os.path.join(d, "config", "local.env"),
                                                                                      f'VOLUMES="work"\n{k}={v}\n'),
              "choose a command, a source or a pin")
             for k, v in (("RCLONE_VERSION", "1.75.1"), ("RCLONE_BASE_URL", "https://example.invalid/r"),
                          ("RCLONE_ZIP", "other.zip"), ("RCLONE_SHA256", "00ff"), ("RCLONE_KEYS_URL", "https://example.invalid/k"),
                          ("RCLONE_SIGNING_KEY_FPR", "0000000000000000000000000000000000000000"))]
        for i, (why, prep, words) in enumerate(cases):
            d = clean_clone(f"c{i}")
            prep(d)
            before = tree(d)
            p, _ = b.get("--confirm", "--yes", "--dir", d)
            check(f"--confirm refuses a checkout with {why}, changing nothing in it",
                  p.returncode == 1 and words in p.stderr and "INSTALL-RAN" not in p.stdout and tree(d) == before
                  and "fresh copy" in p.stderr, (p.returncode, p.stderr[-400:]))
        # A Lume storage location inside the checkout (here in .cache), holding a VM: refused, and nothing is deleted.
        d = clean_clone("vmstore")
        vms = os.path.join(d, ".cache", "vms")
        write(os.path.join(vms, "work", "disk.img"), "my VM's disk\n")
        write(os.path.join(vms, "work", "config.json"), "{}\n")
        write(os.path.join(b.home, ".local", "bin", "mcomp-vm"),
              f'#!/bin/bash\n[ "$1" = config ] && printf "Configured VM storage locations:\\n  - home: ~/.lume (default)\\n  - mine: {vms}\\n"\nexit 0\n', 0o755)
        before = tree(d)
        for extra in ((), ("--no-build",)):
            p, _ = b.get("--confirm", "--yes", "--dir", d, *extra)
            check(f"--confirm {' '.join(extra) or ''}with Lume's VMs stored inside the checkout's .cache: refused, every file kept".replace("  ", " "),
                  p.returncode == 1 and "a Lume storage location inside it" in p.stderr and tree(d) == before
                  and open(os.path.join(vms, "work", "disk.img")).read() == "my VM's disk\n", (p.returncode, p.stderr[-400:]))
        os.remove(os.path.join(b.home, ".local", "bin", "mcomp-vm"))
        # What's accepted: tracked files, logs, verified downloads, settings that only set values.
        d = clean_clone("data")
        write(os.path.join(d, "config", "local.env"),
              '# settings\nBROWSERS="firefox chrome"\nVM_MEMORY=4GB\nVNC_PASSWORD_STORE=keychain\nLUME="$HOME/.local/bin/mcomp-vm"\n')
        write(os.path.join(d, "logs", "build-1.log"), "x\n")
        write(os.path.join(d, ".cache", "ubuntu-20261017", "img"), "x\n")
        p, _ = b.get("--confirm", "--yes", "--dir", d)
        check("--confirm accepts logs, verified downloads and settings that only set values (it then needs the network, which tests refuse)",
              "isn't a plain checkout" not in p.stderr and "couldn't list" in p.stderr, p.stderr[-400:])
        # Storage: rclone's verified download (checked again before every use) and the value-only storage settings.
        d = clean_clone("storage")
        write(os.path.join(d, "config", "local.env"),
              'BROWSERS="firefox"\nSTORAGE_DIR="$HOME/metacomputer-volumes"\nVOLUMES="work:/volume,team:/mnt/team:rw"\n'
              'REFDISKS="docs:/mnt/docs"\nS3_SYNC_INTERVAL=60\nS3_BACKUP_MAX_GB=5\n')
        for f in ("SHA256SUMS", "KEYS.asc", "pinned-key.asc", "signing-key.gpg", "rclone-v1.75.2-osx-arm64.zip",
                  "rclone-v1.75.2-osx-arm64.zip.part"):
            write(os.path.join(d, ".cache", "rclone-v1.75.2", f), "x\n")
        before = tree(d)
        p, _ = b.get("--confirm", "--yes", "--dir", d)
        check("--confirm accepts rclone's verified download (sums, keys, the pinned zip) and the storage settings "
              "(STORAGE_DIR, VOLUMES, REFDISKS, S3_SYNC_INTERVAL, S3_BACKUP_MAX_GB), keeping every file",
              "isn't a plain checkout" not in p.stderr and "couldn't list" in p.stderr and tree(d) == before, p.stderr[-400:])
        # From the plugin, install.sh builds the host tools and mcomp-vm from new clones (offline: --repo is local).
        d = os.path.join(b.t, "fresh")
        git("clone", "-q", repo, d)
        p, _ = b.get("--confirm", "--yes", "--repo", repo, "--dir", d)
        check("--confirm runs install.sh with --fresh-sources", p.returncode == 0 and "INSTALL-RAN --yes --fresh-sources" in p.stdout,
              (p.stdout[-300:], p.stderr[-300:]))
    finally:
        b.close()

# --- install.sh ---------------------------------------------------------------------------------------
def test_install_refusals():
    b = Box()
    try:
        p, _ = b.run([INSTALL, "--help"])
        check("install.sh --help: says it asks once, names get.sh and --replace-vm", p.returncode == 0 and "asks once" in p.stdout
              and "get.sh" in p.stdout and "--replace-vm" in p.stdout, p.stdout[:400])
    finally:
        b.close()
    cases = [
        ("tools missing and no Homebrew", dict(without=("brew", "go", "ffmpeg")), {}, "Homebrew isn't installed"),
        ("only a python3 older than 3.11 and no Homebrew (it counts as missing)", dict(without=("brew",), old_python=True), {},
         "missing: python3, and Homebrew isn't installed"),
        ("no Command Line Tools (mcomp-vm needs Swift)", dict(without=("swift",), real_git=False), {"STUB_CLT": ""},
         "Command Line Tools"),
        ("10 GB free on the one disk", dict(real_git=False), {"STUB_FREE_GB": "10"}, "needs 15 GB there"),
        ("no network", dict(real_git=False), {"STUB_CURL_RC": "7"}, "can't reach github.com"),
    ]
    for name, boxargs, env, words in cases:
        b = Box(**boxargs)
        try:
            p, calls = b.run([INSTALL, "--yes"], **env)
            check(f"install.sh refuses {name}: exit 1, says what to do, nothing written",
                  p.returncode == 1 and words in p.stderr and not os.path.exists(b.env["MCOMP_CONFIG"])
                  and not b.listing() and not ran(calls, "brew", "install") and not ran(calls, "git"),
                  (p.returncode, p.stdout[-400:], p.stderr[-600:]))
        finally:
            b.close()
    # Free space on every disk this run writes to, summed per disk.
    b = Box(real_git=False)
    try:
        p, _ = b.run([INSTALL, "--yes"], STUB_DF=f"{b.acct}|disk-vm|9")
        check("install.sh: Lume's VMs on a 9 GB disk: refused, naming where Lume keeps VMs",
              p.returncode == 1 and "where Lume keeps VMs" in p.stderr and "10 GB" in p.stderr, p.stderr[-400:])
        p, _ = b.run([INSTALL, "--yes"], STUB_DF=f"{LAB}|disk-lab|1")
        check("install.sh: 1 GB where the checkout is: refused, naming the checkout",
              p.returncode == 1 and "in this checkout" in p.stderr, p.stderr[-400:])
        p, _ = b.run([INSTALL, "--yes"], STUB_FREE_GB="2", STUB_DF=f"{b.acct}|disk-vm|100;{LAB}|disk-lab|100")
        check("install.sh: the VM and checkout on big disks, 2 GB on the home folder's disk: refused, naming it",
              p.returncode == 1 and "in your home folder" in p.stderr, p.stderr[-400:])
        p, _ = b.run([INSTALL, "--yes"], STUB_NO_ACCT="1")
        check("install.sh: where Lume keeps VMs can't be told: refused before any change", p.returncode == 1
              and "couldn't tell where Lume will keep the VM" in p.stderr and not os.path.exists(b.env["MCOMP_CONFIG"]), p.stderr[-400:])
        p, _ = b.run([INSTALL, "--yes", "--no-build"], STUB_FREE_GB="10", STUB_CURL_RC="7")
        check("install.sh --no-build with 10 GB free: no disk refusal (it stops later, at the network: nothing to build)",
              "GB free" not in p.stderr and "can't reach github.com" in p.stderr, p.stderr[-400:])
    finally:
        b.close()
    # A stage that fails partway: its own advice, then how to carry on (from a copy of what install.sh reads).
    b = Box()
    try:
        root = install_root(b)
        commit = re.search(r"^MCOMP_VM_COMMIT=(\w+)", open(os.path.join(LAB, "config", "defaults.env")).read(), re.M).group(1)
        write(os.path.join(b.stubs, "git"), STUB_HEAD +
              'case "$*" in *clone*) for a; do d="$a"; done; mkdir -p "$d"\n'
              '  printf "#!/bin/sh\\necho swift build failed >&2\\nexit 1\\n" > "$d/install.sh"; chmod +x "$d/install.sh" ;;\n'
              f'  *rev-parse*) echo {commit} ;;\nesac\n', 0o755)
        p, calls = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude", "--no-build"])
        err = p.stderr
        check("a failing stage (mcomp-vm's build): exit non-zero, names the stage, its advice, and how to carry on",
              p.returncode != 0 and 'stopped in "Lume, the VM runner' in err and "mcomp-vm didn't build" in err
              and "--lume=standard" in err and "run ./install.sh again: finished steps are skipped" in err, (p.stdout[-300:], err[-700:]))
    finally:
        b.close()


def install_root(b):
    """A copy of what install.sh reads (so the checkout's own files are never touched), with a stand-in vault
    build and a stand-in bin/metacomputer that logs what it's asked to do."""
    root = os.path.join(b.t, "checkout")
    os.makedirs(os.path.join(root, "config"))
    shutil.copy(INSTALL, root)
    for f in ("defaults.env", "local.env.example"):
        shutil.copy(os.path.join(LAB, "config", f), os.path.join(root, "config"))
    for f in ("scripts/safe-rmtree.sh", "scripts/lib.sh", "config/defaults.env", "lib/safedelete.py", "lib/vms.py", "lib/termsafe.py"):   # every recursive delete goes through it
        os.makedirs(os.path.dirname(os.path.join(root, f)), exist_ok=True)
        shutil.copy2(os.path.join(LAB, f), os.path.join(root, f))
    write(os.path.join(root, "scripts", "build-vault.sh"), "#!/bin/sh\necho 'vault: skipped by the test'\n", 0o755)
    write(os.path.join(root, "scripts", "build-tools.sh"),
          f'#!/bin/bash\nif [ "$1" = --check ]; then [ -z "${{STUB_TOOLS_STALE:-}}" ]; exit; fi\necho "tools${{*:+ $*}}" >> "{b.t}/tools.log"\n', 0o755)
    write(os.path.join(root, "bin", "metacomputer"), f'#!/bin/bash\necho "$*" >> "{b.t}/mc.log"\n[ -z "${{STUB_MC_FAIL:-}}" ]\n', 0o755)
    return root


def vm_box(vms="", marker=False, ls="ok"):
    """install.sh with an installed, current mcomp-vm stand-in whose `ls` lists VMS (or fails, or is garbage)."""
    b = Box()
    root = install_root(b)
    tag = re.search(r"^MCOMP_VM_TAG=v?(\S+)", open(os.path.join(LAB, "config", "defaults.env")).read(), re.M).group(1)
    write(os.path.join(b.home, ".local", "share", "mcomp-vm", "BUILD_INFO"), f"version={tag}\n")
    vms_dir = os.path.join(b.t, "vms")
    os.makedirs(vms_dir)
    write(os.path.join(b.home, ".local", "bin", "mcomp-vm"),
          '#!/bin/bash\ncase "$1" in\n'
          '  ls) case "$STUB_LS" in fail) exit 1 ;; garbage) echo "not json"; exit 0 ;; esac\n'
          '      "$STUB_PY" -c \'import json, os; print(json.dumps([{"name": n} for n in os.environ.get("STUB_VMS", "").split(",") if n]))\' ;;\n'
          '  run) echo "  --no-host-sharing" ;;\n'
          f'  config) printf "Configured VM storage locations:\\n  - home: {vms_dir} (default)\\n" ;;\n'
          '  *) exit 1 ;;\nesac\n', 0o755)
    write(b.env["MCOMP_CONFIG"], 'VM_NAME=mctest\nBASELINE_NAME=mctest-baseline\nBROWSERS="firefox"\nLUME="$HOME/.local/bin/mcomp-vm"\n')
    if marker:
        write(os.path.join(root, ".run", "build-unfinished-mctest"), "started=20261118-090000\nvm=mctest\n")
    b.env.update(STUB_VMS=vms, STUB_LS=ls)
    return b, root


def test_install_vm_decisions():
    """P1: a rerun never deletes a VM on its own. Only --replace-vm replaces, and --yes doesn't imply it."""
    seen = {}

    def go(vms="", marker=False, ls="ok", extra=(), link=False, **env):
        b, root = vm_box(vms, marker, ls)
        try:
            # get.sh's report file: install.sh writes vm_built=1 there only after building the VM.
            report = os.path.join(b.t, "report")
            write(report, "")
            if link:   # a link in its place: never written through
                os.rename(report, report + ".target")
                os.symlink(report + ".target", report)
            p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude", *extra], MCOMP_INSTALL_REPORT=report, **env)
            log = open(os.path.join(b.t, "mc.log")).read().splitlines() if os.path.exists(os.path.join(b.t, "mc.log")) else []
            conf_written = os.path.exists(os.path.join(b.t, "local.env"))
            seen["report"] = open(report + (".target" if link else "")).read()
            return p, log, conf_written
        finally:
            b.close()
    p, log, _ = go("mctest,mctest-baseline")
    check("VM and its clean snapshot there: 'already built', nothing built or replaced", p.returncode == 0 and not log
          and "already built" in p.stdout, (p.stdout[-300:], p.stderr[-300:], log))
    check("a kept VM isn't reported as built (get.sh then runs only read-only checks on it)", seen["report"] == "", seen)
    p, log, _ = go("")
    check("no VM: built (metacomputer build, without --replace)", p.returncode == 0 and log == ["build"], (log, p.stderr[-300:]))
    check("a VM built this run is reported to get.sh (vm_built=1)", seen["report"] == "vm_built=1\n", seen)
    p, log, _ = go("", STUB_MC_FAIL="1")
    check("a build that fails: install.sh stops, and nothing is reported as built", p.returncode != 0 and log == ["build"]
          and seen["report"] == "", (p.returncode, log, seen))
    p, log, _ = go("", link=True)
    check("MCOMP_INSTALL_REPORT that's a link: built, but nothing is written through the link", p.returncode == 0
          and log == ["build"] and seen["report"] == "", (log, seen))
    p, log, _ = go("mctest")
    check("VM without its snapshot and no build marker (it may hold work), --yes: left alone, exit 1, says how to keep or replace it",
          p.returncode == 1 and not log and "may hold your work" in p.stderr and "metacomputer baseline" in p.stderr
          and "--replace-vm" in p.stderr, (log, p.stderr[-500:]))
    p, log, _ = go("mctest", marker=True)
    check("an interrupted build (VM without snapshot, marker there), --yes: still left alone, exit 1, names --replace-vm",
          p.returncode == 1 and not log and "didn't finish" in p.stderr and "--replace-vm" in p.stderr, (log, p.stderr[-500:]))
    p, log, _ = go("mctest", marker=True, extra=("--replace-vm",))
    check("an interrupted build with --replace-vm: rebuilt (metacomputer build --replace)", p.returncode == 0 and log == ["build --replace"],
          (log, p.stderr[-300:]))
    p, log, _ = go("mctest", extra=("--replace-vm",))
    check("a VM without its snapshot, with --replace-vm: replaced, as asked", p.returncode == 0 and log == ["build --replace"], log)
    check("a VM replaced this run is reported to get.sh (vm_built=1)", seen["report"] == "vm_built=1\n", seen)
    p, log, _ = go("mctest-baseline")
    check("the snapshot without its VM: left alone, exit 1, says metacomputer reset restores it",
          p.returncode == 1 and not log and "metacomputer reset" in p.stderr, (log, p.stderr[-400:]))
    for how in ("fail", "garbage"):
        p, log, conf = go("mctest", ls=how)
        check(f"the runner's VM listing {'fails' if how == 'fail' else 'is unreadable'}: refused before any change, never taken as 'no VM'",
              p.returncode == 1 and not log and "couldn't list the VMs" in p.stderr, (log, p.stderr[-400:]))
    p, log, _ = go("mctest", ls="fail", extra=("--no-build",))
    check("listing fails with --no-build: goes on, leaves the VM alone", p.returncode == 0 and not log, (log, p.stderr[-300:]))
    for vms, marker, what in (("mctest", True, "an interrupted build"), ("mctest", False, "a VM without its snapshot"),
                              ("mctest-baseline", False, "a snapshot without its VM")):
        p, log, _ = go(vms, marker=marker, extra=("--no-build",))
        check(f"--no-build with {what}: left as it is, says so, and the run succeeds (exit 0)",
              p.returncode == 0 and not log and "left as it is (--no-build)" in p.stdout + p.stderr, (p.returncode, log, p.stderr[-300:]))
    src = open(os.path.join(LAB, "scripts", "build-vm.sh")).read()
    i_mark, i_create, i_rm = src.find('> "$BUILD_MARKER"'), src.find('"$LUME" create'), src.find('rm -f "$BUILD_MARKER"')
    i_base = src.find('ok "baseline $BASELINE_NAME created"')
    check("build-vm.sh writes the build marker before it deletes or creates a VM, and removes it only after the baseline",
          0 < i_mark < src.find('say "--replace: deleting') and i_mark < i_create and i_base < i_rm, (i_mark, i_create, i_base, i_rm))


def test_install_existing():
    """An install that's already here: the metacomputer command runs another checkout (not switched without --switch
    or a yes at a terminal, --only-claude included; the same checkout reached another way isn't another), or it's
    newer than this checkout by history (refused without --allow-downgrade)."""
    def go(extra=(), link_to=None, relative=False, downgrade=False, forward=False):
        b, root = vm_box("mctest,mctest-baseline")
        try:
            link = os.path.join(b.home, ".local", "bin", "metacomputer")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            od = os.path.join(b.t, "elsewhere")
            write(os.path.join(od, "install.sh"), "#!/bin/sh\n", 0o755)
            write(os.path.join(od, "bin", "metacomputer"), "#!/bin/sh\n", 0o755)
            os.symlink(os.path.join(b.t, "checkout"), os.path.join(b.t, "alias"))     # the same checkout, another path
            if link_to:
                target = {"other": os.path.join(od, "bin", "metacomputer"),
                          "alias": os.path.join(b.t, "alias", "bin", "metacomputer")}[link_to]
                os.symlink(os.path.relpath(target, os.path.dirname(link)) if relative else target, link)
            if downgrade or forward:
                g = lambda *x: git("-c", "user.email=t@example.com", "-c", "user.name=t", *x, cwd=root)
                g("init", "-q", "-b", "main"); g("add", "-A"); g("commit", "-qm", "one"); g("tag", "v0.4.0")
                write(os.path.join(root, "later.txt"), "x\n"); g("add", "-A"); g("commit", "-qm", "two"); g("tag", "v0.5.0")
                later = git("rev-parse", "HEAD", cwd=root)
                if downgrade:
                    git("reset", "-q", "--hard", "v0.4.0", cwd=root)
                stamp_commit = later if downgrade else git("rev-parse", "v0.4.0", cwd=root)
                write(os.path.join(b.home, ".local", "share", "metacomputer", "installed.json"),
                      json.dumps({"version": "v0.5.0", "tag": None, "after": "v0.5.0", "commit": stamp_commit,
                                  "checkout": root}, indent=2))
            p, _ = b.run([os.path.join(root, "install.sh"), "--yes", *extra])
            return p, (os.readlink(link) if os.path.islink(link) else None), root, od
        finally:
            b.close()
    p, target, root, od = go(link_to="other")
    check("the command runs another checkout, --yes: refused, says how to update that one or switch, link unchanged",
          p.returncode != 0 and "runs another checkout" in p.stderr and "metacomputer update" in p.stderr
          and "--switch" in p.stderr and target.endswith("elsewhere/bin/metacomputer"), (p.stderr[-400:], target))
    p, target, root, od = go(link_to="other", relative=True)
    check("…a relative link to it too", p.returncode != 0 and "runs another checkout" in p.stderr, p.stderr[-300:])
    p, target, root, od = go(link_to="other", extra=("--only-claude",))
    check("…and --only-claude doesn't get around it", p.returncode != 0 and "runs another checkout" in p.stderr,
          p.stderr[-300:])
    p, target, root, od = go(link_to="other", extra=("--switch",))
    check("…with --switch: it goes ahead and the command runs this checkout", p.returncode == 0
          and target == os.path.join(root, "bin", "metacomputer"), (p.returncode, p.stderr[-300:], target))
    p, target, root, od = go(link_to="alias")
    check("the command runs this same checkout through another path: not taken for another checkout", p.returncode == 0,
          p.stderr[-300:])
    p, target, root, od = go(downgrade=True)
    check("this checkout behind what was installed from it (by history, with no exact tag recorded): refused",
          p.returncode != 0 and "older than what's installed" in p.stderr and "--allow-downgrade" in p.stderr, p.stderr[-300:])
    p, target, root, od = go(downgrade=True, extra=("--allow-downgrade",))
    check("…with --allow-downgrade: it goes ahead", p.returncode == 0, p.stderr[-300:])
    p, target, root, od = go(forward=True)
    check("ahead of what was installed (an update): goes ahead", p.returncode == 0, p.stderr[-300:])
    src = open(INSTALL).read()
    check("install.sh ends with the after-install step (lib/update.py), which can't fail the install",
          '"$PY311" "$ROOT/lib/update.py" _after-install || true' in src
          and src.index('"Get yourself a virtual computer') < src.index("_after-install"))

# --- scripts/lib.sh: the VM's folder ------------------------------------------------------------------
def test_vm_dir():
    t = tempfile.mkdtemp(prefix="mcomp-install-")
    try:
        lume = os.path.join(t, "lume")
        write(lume, '#!/bin/bash\ncase "$1" in\n  get) [ -n "${STUB_GET_FAIL:-}" ] && exit 1; printf "%s" "$STUB_GET_JSON"; exit "${STUB_GET_RC:-0}" ;;\n'
                    '  config) printf "%s" "$STUB_CONFIG"; exit "${STUB_CONFIG_RC:-0}" ;;\n  ls) echo "[]" ;;\nesac\n', 0o755)
        conf = os.path.join(t, "test.env")
        write(conf, f'LUME="{lume}"\nRUN_DIR="{t}/run"\nLOG_DIR="{t}/logs"\nSSH_DIR="{t}/keys"\nVM_NAME=mctest\n')
        vms = os.path.join(t, "vms")
        vms2 = os.path.join(t, "vms2")
        os.makedirs(vms)
        os.makedirs(vms2)

        def call(fn, *args, loc="home", mac="aa:bb:cc:dd:ee:ff", config="", **env):
            get = json.dumps([{"name": env.pop("STUB_GET_NAME", "mctest"), "macAddress": mac, **({"locationName": loc} if loc is not None else {})}])
            e = {"HOME": os.path.join(t, "home"), "PATH": "/usr/bin:/bin", "MCOMP_CONFIG": conf, "PY": PY,
                 "STUB_GET_JSON": get, "STUB_CONFIG": config, **env}
            return subprocess.run(["/bin/bash", "-c", f'. "{LAB}/scripts/lib.sh"; {fn} "$@"', "_", *args], env=e,
                                  capture_output=True, text=True, timeout=60)

        def cfg(*entries):
            return "Default VM storage: home (~/.lume)\n\nConfigured VM storage locations:\n" + \
                "".join(f"  - {n}: {p}\n" for n, p in entries)
        p = call("vm_dir", "mctest", config=cfg(("home", vms + " (default)")))
        check("vm_dir: the folder of the location Lume names, made canonical", p.returncode == 0
              and p.stdout.strip() == os.path.join(os.path.realpath(vms), "mctest"), (p.stdout, p.stderr))
        p = call("vm_dir", "mctest", loc="a.b", config=cfg(("axb", "/nonexistent"), ("a.b", vms2)))
        check("vm_dir: location names match literally ('a.b' isn't a pattern for 'axb')",
              p.returncode == 0 and p.stdout.strip() == os.path.join(os.path.realpath(vms2), "mctest"), (p.stdout, p.stderr))
        bad = [
            ("the location named only by a lookalike", dict(loc="a.b", config=cfg(("axb", vms)))),
            ("the location listed twice", dict(config=cfg(("home", vms), ("home", vms2)))),
            ("the location not listed", dict(config=cfg(("other", vms)))),
            ("a relative path", dict(config=cfg(("home", "vms")))),
            ("a folder that doesn't exist", dict(config=cfg(("home", os.path.join(t, "missing"))))),
            ("no locationName from lume get", dict(loc=None, config=cfg(("home", vms)))),
            ("lume config get failing", dict(config=cfg(("home", vms)), STUB_CONFIG_RC="1")),
            ("lume get failing", dict(config=cfg(("home", vms)), STUB_GET_FAIL="1")),
            ("lume get printing usable JSON but then failing", dict(config=cfg(("home", vms)), STUB_GET_RC="1")),
            ("lume get describing another VM", dict(config=cfg(("home", vms)), STUB_GET_NAME="other")),
            ("~/ and a folder that doesn't exist there", dict(config=cfg(("home", "~/definitely-not-a-folder-mcomp-test")))),
        ]
        for why, kw in bad:
            p = call("vm_dir", "mctest", **kw)
            check(f"vm_dir fails closed on {why} (status 1, says why, prints no folder)",
                  p.returncode == 1 and not p.stdout.strip() and p.stderr.strip(), (p.returncode, p.stdout, p.stderr))
        import pwd
        acct = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
        p = call("vm_dir", "mctest", config=cfg(("home", "~ (default)")))
        check("vm_dir: '~' is the account's home folder (as Lume expands it), whatever HOME says",
              p.returncode == 0 and p.stdout.strip() == os.path.join(acct, "mctest"), (p.stdout, p.stderr))
        # vm_dir_is: the folder is the VM's own before anything in it is replaced.
        d = os.path.join(vms, "mctest")
        os.makedirs(d)
        write(os.path.join(d, "disk.img"), "")
        write(os.path.join(d, "config.json"), json.dumps({"macAddress": "AA:BB:CC:DD:EE:FF"}))
        p = call("vm_dir_is", d, "mctest")
        check("vm_dir_is: a folder with disk.img and the MAC address Lume reports (any case) is the VM's", p.returncode == 0, p.stderr)
        p = call("vm_dir_is", d, "mctest", mac="11:22:33:44:55:66")
        check("vm_dir_is: another VM's folder (another MAC address) isn't", p.returncode == 1)
        link = os.path.join(t, "linkdir")
        os.symlink(d, link)
        p = call("vm_dir_is", link, "mctest")
        check("vm_dir_is: a link to the folder isn't accepted", p.returncode == 1)
        os.remove(os.path.join(d, "disk.img"))
        os.symlink(os.path.join(t, "elsewhere.img"), os.path.join(d, "disk.img"))
        p = call("vm_dir_is", d, "mctest")
        check("vm_dir_is: a disk.img that's a link isn't accepted", p.returncode == 1)
        os.remove(os.path.join(d, "disk.img"))
        write(os.path.join(d, "disk.img"), "")
        os.remove(os.path.join(d, "config.json"))
        p = call("vm_dir_is", d, "mctest")
        check("vm_dir_is: no config.json, no claim", p.returncode == 1)
        write(os.path.join(t, "other-config.json"), json.dumps({"macAddress": "AA:BB:CC:DD:EE:FF"}))
        os.symlink(os.path.join(t, "other-config.json"), os.path.join(d, "config.json"))
        p = call("vm_dir_is", d, "mctest")
        check("vm_dir_is: a config.json that's a link isn't accepted", p.returncode == 1)
        os.remove(os.path.join(d, "config.json"))
        write(os.path.join(d, "config.json"), json.dumps({"macAddress": "AA:BB:CC:DD:EE:FF"}))
        p = call("vm_dir_is", d, "mctest", STUB_GET_RC="1")
        check("vm_dir_is: a MAC address from a lume get that failed isn't trusted", p.returncode == 1)
        build = open(os.path.join(LAB, "scripts", "build-vm.sh")).read()
        i = build.find('VMDIR="$(vm_dir "$VM_NAME")" || die')
        check("build-vm.sh stops on either check before it touches disk.img",
              0 < i < build.find('vm_dir_is "$VMDIR" "$VM_NAME" || die') < build.find('rm -f "$VMDIR/disk.img"'))
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --- the live-view password ---------------------------------------------------------------------------
SSH_STUB = ('#!/bin/bash\n{ printf "ARGV %s\\n" "$*"; env; } >> "$STUB_DIR/ssh.log"\n'
            'if [ -n "${STUB_SSH_HANG:-}" ]; then echo $$ > "$STUB_DIR/ssh.pid"; trap "" TERM; exec sleep 60; fi\n'
            'cat > "$STUB_DIR/ssh.stdin"\nexit 0\n')
# `security`, as far as these paths go, including what matters here: `add-generic-password ... -w` with no value
# asks with getpass(), which reads the terminal, never stdin; `-i` reads commands on stdin and its exit status
# says nothing. Every call's argv and environment go to security.log.
SECURITY_STUB = r'''#!PYTHON
import json, os, shlex, signal, sys, time
d = os.environ["STUB_DIR"]
def log(**kw):
    open(os.path.join(d, "security.log"), "a").write(json.dumps(kw) + "\n")
argv = sys.argv[1:]
log(kind="call", argv=argv, env=dict(os.environ))
verb = argv[0] if argv else ""
if os.environ.get("STUB_SECURITY_HANG_AT") == verb:     # a step that never answers, and ignores TERM
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(os.path.join(d, "hang.pid"), "w").write(str(os.getpid()))
    time.sleep(60)
sp = os.path.join(d, "keychain.json")
state = json.load(open(sp)) if os.path.exists(sp) else {}
def save():
    json.dump(state, open(sp, "w"))
kc = os.environ.get("STUB_KC", "")
def key(a):
    return a[a.index("-s") + 1] + "/" + a[a.index("-a") + 1]
def opt(a, flag):
    return a[a.index(flag) + 1] if flag in a else ""
if verb in ("login-keychain", "default-keychain"):
    if os.environ.get("STUB_NO_KEYCHAIN"):
        print("security: SecKeychainCopyLogin: The specified keychain could not be found.", file=sys.stderr); sys.exit(50)
    print(f'    "{kc}"'); sys.exit(0)
if verb == "delete-generic-password":
    if os.environ.get("STUB_SECURITY_RACE") and key(argv) in state:
        sys.exit(0)        # another install writes the item again right after this delete
    if key(argv) in state:
        del state[key(argv)]; save(); sys.exit(0)
    sys.exit(44)
if verb == "find-generic-password":
    if "-w" in argv or "-g" in argv:
        log(kind="secret-read")
    it = state.get(key(argv))
    if not it:
        print("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.", file=sys.stderr); sys.exit(44)
    s, a = key(argv).split("/", 1)
    print(f'keychain: "{kc}"\nattributes:\n    "acct"<blob>="{a}"\n    "icmt"<blob>="{it["comment"]}"\n    "svce"<blob>="{s}"')
    sys.exit(0)
if verb == "add-generic-password":
    if argv[-1] == "-w":
        log(kind="getpass")    # the real tool would now read /dev/tty, not stdin
        print("password data for new item: ", file=sys.stderr, end=""); sys.exit(1)
    log(kind="argv-password"); sys.exit(1)
if verb == "-i":
    for line in sys.stdin:
        a = shlex.split(line)
        if a[:1] != ["add-generic-password"] or os.environ.get("STUB_SECURITY_REFUSE") or a[-1] != kc:
            continue
        if key(a) in state and "-U" not in a:
            continue                                     # the item exists: without -U, nothing changes
        state[key(a)] = {"value": opt(a, "-w"), "comment": opt(a, "-j")}; save()
    sys.exit(0)
print("stub: unhandled", argv, file=sys.stderr); sys.exit(2)
'''


class VncBox:
    def __init__(self, store="keychain"):
        self.t = tempfile.mkdtemp(prefix="mcomp-install-")
        self.stubs = os.path.join(self.t, "stubs")
        write(os.path.join(self.stubs, "ssh"), SSH_STUB, 0o755)
        write(os.path.join(self.stubs, "security"), SECURITY_STUB.replace("PYTHON", PY), 0o755)
        self.sshdir = os.path.join(self.t, "keys")
        self.kc = os.path.join(self.t, "home", "Library", "Keychains", "login.keychain-db")
        write(self.kc, "")
        self.conf = os.path.join(self.t, "test.env")
        write(self.conf, f'VM_NAME=mctest\nBASELINE_NAME=mctest-baseline\nSSH_DIR="{self.sshdir}"\nSSH_PORT=2999\nVNC_PORT=6999\n'
                         f'RUN_DIR="{self.t}/run"\nLOG_DIR="{self.t}/logs"\nVNC_PASSWORD_STORE={store}\nLUME="{self.t}/no-lume"\n')
        py = os.path.dirname(os.path.realpath(PY))
        self.env = {"HOME": os.path.join(self.t, "home"), "PATH": f"{self.stubs}:{py}:/usr/bin:/bin", "LANG": "C.UTF-8",
                    "MCOMP_CONFIG": self.conf, "STUB_DIR": self.stubs, "STUB_KC": self.kc, "PY": PY, "MCOMP_KEYCHAIN_WAIT": "3",
                    "MCOMP_LOCK_DIR": os.path.join(self.t, "locks")}
        self.file = os.path.join(self.sshdir, "vnc-pass")

    def run(self, *args, stdin=None, **extra):
        for f in ("ssh.log", "ssh.stdin", "security.log", "hang.pid", "ssh.pid"):
            if os.path.exists(os.path.join(self.stubs, f)):
                os.remove(os.path.join(self.stubs, f))
        t0 = time.time()
        p = subprocess.run([CLI, *args], env=dict(self.env, **extra), capture_output=True, text=True, timeout=90,
                           input=stdin if stdin is not None else "")
        took = time.time() - t0

        def rd(f):
            q = os.path.join(self.stubs, f)
            return open(q).read() if os.path.exists(q) else ""
        sec = [json.loads(x) for x in rd("security.log").splitlines()]
        return p, {"ssh": rd("ssh.log"), "pw": rd("ssh.stdin").strip(), "sec": sec, "took": took}

    def keychain(self):
        q = os.path.join(self.stubs, "keychain.json")
        return json.load(open(q)) if os.path.exists(q) else {}

    def close(self):
        shutil.rmtree(self.t, ignore_errors=True)


def leaked(pw, p, s):
    """Where the password showed up outside stdin: output, ssh's argv or environment, security's argv or env."""
    out = []
    if pw in p.stdout + p.stderr:
        out.append("output")
    if pw in s["ssh"]:
        out.append("ssh argv/env")
    for c in s["sec"]:
        if c.get("kind") == "call" and (any(pw in a for a in c["argv"]) or any(pw in v for v in c["env"].values())):
            out.append(f"security {c['argv'][:1]} argv/env")
    return out


def test_vnc_password():
    v = VncBox()
    try:
        p, s = v.run("_vnc-install")
        pw = s["pw"]
        check("keychain: a new 8-character password reaches the VM on ssh's stdin",
              p.returncode == 0 and re.fullmatch(r"[A-Za-z0-9]{8}", pw), (p.returncode, p.stdout, p.stderr))
        item = v.keychain().get("metacomputer-vnc/mctest") or {}
        check("keychain: stored in the login keychain as metacomputer-vnc / VM_NAME, through `security -i`, tagged by this write",
              item.get("value") == pw and item.get("comment", "").startswith("mcomp-") and any(c.get("argv") == ["-i"] for c in s["sec"]),
              (v.keychain(), s["sec"][:6]))
        check("keychain: updated in place (-U), never deleted first (no moment without an item)",
              not any(c.get("argv", [""])[0] == "delete-generic-password" for c in s["sec"] if c.get("kind") == "call"), s["sec"])
        check("keychain: never the getpass form (`-w` with no value reads the terminal) and never a password in argv",
              not any(c.get("kind") in ("getpass", "secret-read") for c in s["sec"]), [c.get("kind") for c in s["sec"]])
        check("keychain: the password is in no output, argv or environment (ssh's and security's included)",
              pw and not leaked(pw, p, s), leaked(pw, p, s))
        check("keychain: the output says how to read it; no password file", "security find-generic-password -s metacomputer-vnc -a mctest -w"
              in p.stdout and not os.path.exists(v.file), p.stdout)
        p, _ = v.run("view")
        check("view: shows the Keychain command, not a password, exit 0", p.returncode == 0 and "find-generic-password" in p.stdout
              and pw not in p.stdout, p.stdout)
        # Another install's write in between (it re-creates the item right after any delete): what's stored must be
        # this run's password, in the Keychain or (if the Keychain didn't take it) the fallback file; never the other's.
        for refuse in ("", "1"):
            json.dump({"metacomputer-vnc/mctest": {"value": "Other123", "comment": "mcomp-someoneelse"}},
                      open(os.path.join(v.stubs, "keychain.json"), "w"))
            p, s = v.run("_vnc-install", STUB_SECURITY_RACE="1", **({"STUB_SECURITY_REFUSE": "1"} if refuse else {}))
            held = (v.keychain().get("metacomputer-vnc/mctest") or {}).get("value")
            infile = open(v.file).read() if os.path.exists(v.file) else None
            check(f"keychain, another write in between{' and the Keychain refusing' if refuse else ''}: the VM's password is the one kept "
                  f"({'the fallback file' if refuse else 'the Keychain item, updated in place'}), never the other install's",
                  p.returncode == 0 and s["pw"] and (infile == s["pw"] if refuse else (held == s["pw"] and infile is None)),
                  (held, infile, s["pw"], p.stderr[-200:]))
            if os.path.exists(v.file):
                os.remove(v.file)
        for why, extra, slow in (("there's no login keychain", {"STUB_NO_KEYCHAIN": "1"}, False),
                                 ("the keychain silently doesn't take it (locked)", {"STUB_SECURITY_REFUSE": "1"}, False),
                                 ("finding the login keychain hangs (and ignores TERM)", {"STUB_SECURITY_HANG_AT": "login-keychain"}, True),
                                 ("`security -i` hangs (and ignores TERM)", {"STUB_SECURITY_HANG_AT": "-i"}, True),
                                 ("the check afterwards hangs (and ignores TERM)", {"STUB_SECURITY_HANG_AT": "find-generic-password"}, True)):
            if os.path.exists(v.file):
                os.remove(v.file)
            p, s = v.run("_vnc-install", **extra)
            pw2 = s["pw"]
            mode = stat.S_IMODE(os.stat(v.file).st_mode) if os.path.exists(v.file) else None
            check(f"keychain, {why}: falls back to a 0600 file within the wait, says so, leaks nothing",
                  p.returncode == 0 and mode == 0o600 and open(v.file).read() == pw2 and pw2 and "login Keychain" in p.stderr
                  and not leaked(pw2, p, s) and s["took"] < 3 + 1.5, (p.returncode, mode, round(s["took"], 1), p.stderr[-300:]))
            if slow:
                pid = int(open(os.path.join(v.stubs, "hang.pid")).read()) if os.path.exists(os.path.join(v.stubs, "hang.pid")) else None
                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except OSError:
                        pass
                check("keychain: the hung `security` is killed (TERM, then KILL), not left behind", pid and not alive, pid)
        os.chmod(v.file, 0o644)
        kept = open(v.file).read()
        p, s = v.run("_vnc-install")
        check("a usable password already in the file stays in use, is made 0600 again, and the Keychain isn't touched",
              s["pw"] == kept and stat.S_IMODE(os.stat(v.file).st_mode) == 0o600 and not s["sec"], (s["pw"], s["sec"][:2]))
        p, _ = v.run("view")
        check("view with that file: shows how to read the file", f"cat {v.sshdir}/vnc-pass" in p.stdout and kept not in p.stdout, p.stdout)
        # Never through a link.
        os.remove(v.file)
        target = os.path.join(v.t, "target.txt")
        write(target, "untouched\n")
        os.symlink(target, v.file)
        p, s = v.run("_vnc-install", STUB_NO_KEYCHAIN="1")
        check("a link in place of the password file: refused (exit 1), the link's target untouched, nothing pushed",
              p.returncode == 1 and "link" in p.stderr and open(target).read() == "untouched\n" and not s["pw"], (p.returncode, p.stderr))
        os.remove(v.file)
        # The old _vnc-push entry: the password on stdin, never in the environment.
        p, s = v.run("_vnc-push", stdin="Abc12345\n")
        check("_vnc-push takes the password on stdin and passes it on stdin", p.returncode == 0 and s["pw"] == "Abc12345"
              and "Abc12345" not in s["ssh"], (p.stderr, s["ssh"][:200]))
        p, s = v.run("_vnc-push", VNC_PASSWORD="FromEnv1")
        check("_vnc-push ignores a VNC_PASSWORD in the environment (refuses with nothing on stdin)", p.returncode == 1 and not s["pw"], p.stderr)
        p, s = v.run("_vnc-push", stdin="Abc12345\n", STUB_SSH_HANG="1", MCOMP_VNC_PUSH_WAIT="2")
        pid = int(open(os.path.join(v.stubs, "ssh.pid")).read()) if os.path.exists(os.path.join(v.stubs, "ssh.pid")) else None
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                pass
        check("a VM that holds the password push open: stopped within MCOMP_VNC_PUSH_WAIT, ssh killed, exit 1",
              p.returncode == 1 and s["took"] < 2 + 1.5 and pid and not alive and "could not install the VNC password" in p.stderr,
              (p.returncode, round(s["took"], 1), pid, alive))
        # The password folder: private, under folders only their owners can change (or sticky ones like /tmp).
        shared = os.path.join(v.t, "shared")
        os.makedirs(shared)
        os.chmod(shared, 0o777)
        with open(v.conf, "a") as fh:
            fh.write(f'SSH_DIR="{shared}/keys"\n')
        p, s = v.run("_vnc-install", STUB_NO_KEYCHAIN="1")
        check("a password folder inside a folder others can write to (not sticky): refused, nothing written or pushed",
              p.returncode == 1 and "can be changed by someone else" in p.stderr and not os.path.exists(os.path.join(shared, "keys", "vnc-pass"))
              and not s["pw"], (p.returncode, p.stderr[-300:]))
        os.chmod(shared, 0o1777)
        p, s = v.run("_vnc-install", STUB_NO_KEYCHAIN="1")
        check("... the same folder made sticky (like /tmp): accepted", p.returncode == 0
              and stat.S_IMODE(os.stat(os.path.join(shared, "keys", "vnc-pass")).st_mode) == 0o600, p.stderr[-300:])
        os.chmod(shared, 0o755)
        os.chmod(os.path.join(shared, "keys"), 0o777)
        p, s = v.run("_vnc-install", STUB_NO_KEYCHAIN="1")
        check("a password folder of yours that others could write to: made 0700 before the file is touched",
              p.returncode == 0 and stat.S_IMODE(os.stat(os.path.join(shared, "keys")).st_mode) == 0o700, p.stderr[-300:])
    finally:
        v.close()
    v = VncBox(store="file")
    try:
        write(v.file, "", 0o644)
        p, s = v.run("_vnc-install")
        mode = stat.S_IMODE(os.stat(v.file).st_mode)
        check("file store, an empty 0644 file there: replaced by a new 0600 file holding a new password",
              p.returncode == 0 and mode == 0o600 and open(v.file).read() == s["pw"] and len(s["pw"]) == 8 and not s["sec"], (mode, p.stderr))
        os.chmod(v.file, 0o644)
        before = open(v.file).read()
        p, s = v.run("_vnc-install")
        check("file store, a usable 0644 file: kept, made 0600", s["pw"] == before and stat.S_IMODE(os.stat(v.file).st_mode) == 0o600)
        ex = open(os.path.join(LAB, "config", "local.env.example")).read()
        inst = open(INSTALL).read()
        check("install.sh writes VNC_PASSWORD_STORE=keychain into a new settings file; the example documents it",
              "echo 'VNC_PASSWORD_STORE=keychain' >> \"$CONF\"" in inst and "#VNC_PASSWORD_STORE=keychain" in ex)
    finally:
        v.close()


def test_vnc_real_keychain():
    """The real `security`, on a throwaway keychain that exists only inside a temporary HOME (where it is the
    login keychain): the item is created through `security -i` and found afterwards. Skipped where `security`
    doesn't keep to that HOME."""
    sec = shutil.which("security") or "/usr/bin/security"
    if not os.path.exists(sec):
        print("SKIP the real security (not installed)")
        return
    t = tempfile.mkdtemp(prefix="mcomp-install-")
    home = os.path.join(t, "home")
    kc = os.path.join(home, "Library", "Keychains", "login.keychain-db")
    os.makedirs(os.path.dirname(kc))
    senv = {"HOME": home, "PATH": "/usr/bin:/bin"}
    created = False
    try:
        r = subprocess.run([sec, "create-keychain", "-p", "throwaway-test-only", kc], env=senv, capture_output=True, text=True, timeout=30)
        created = r.returncode == 0
        login = subprocess.run([sec, "login-keychain"], env=senv, capture_output=True, text=True, timeout=30).stdout
        if not created or kc not in login:
            print("SKIP the real security (the throwaway keychain isn't this HOME's login keychain)")
            return
        stubs = os.path.join(t, "stubs")
        write(os.path.join(stubs, "ssh"), SSH_STUB, 0o755)
        conf = os.path.join(t, "test.env")
        write(conf, f'VM_NAME=mcrealtest\nSSH_DIR="{t}/keys"\nSSH_PORT=2999\nRUN_DIR="{t}/run"\nLOG_DIR="{t}/logs"\n'
                    f'VNC_PASSWORD_STORE=keychain\nLUME="{t}/no-lume"\n')
        py = os.path.dirname(os.path.realpath(PY))
        p = subprocess.run([CLI, "_vnc-install"], env={"HOME": home, "PATH": f"{stubs}:{py}:/usr/bin:/bin", "MCOMP_CONFIG": conf,
                                                       "STUB_DIR": stubs, "PY": PY, "MCOMP_LOCK_DIR": os.path.join(t, "locks")},
                           capture_output=True, text=True, timeout=90, input="")
        pw = open(os.path.join(stubs, "ssh.stdin")).read().strip() if os.path.exists(os.path.join(stubs, "ssh.stdin")) else ""
        found = subprocess.run([sec, "find-generic-password", "-s", "metacomputer-vnc", "-a", "mcrealtest", kc], env=senv,
                               capture_output=True, text=True, timeout=30)
        check("real security: the password goes into the (throwaway) login keychain through `security -i`, no fallback file",
              p.returncode == 0 and found.returncode == 0 and not os.path.exists(os.path.join(t, "keys", "vnc-pass"))
              and pw and pw not in p.stdout + p.stderr and '"icmt"<blob>="mcomp-' in found.stdout, (p.returncode, found.returncode, p.stderr[-300:]))
        p2 = subprocess.run([CLI, "_vnc-install"], env={"HOME": home, "PATH": f"{stubs}:{py}:/usr/bin:/bin", "MCOMP_CONFIG": conf,
                                                        "STUB_DIR": stubs, "PY": PY, "MCOMP_LOCK_DIR": os.path.join(t, "locks")},
                            capture_output=True, text=True, timeout=90, input="")
        found2 = subprocess.run([sec, "find-generic-password", "-s", "metacomputer-vnc", "-a", "mcrealtest", kc], env=senv,
                                capture_output=True, text=True, timeout=30)
        check("real security, again: the existing item is updated in place (-U) and verified by the new tag, still no fallback file",
              p2.returncode == 0 and found2.returncode == 0 and found2.stdout != found.stdout
              and not os.path.exists(os.path.join(t, "keys", "vnc-pass")), (p2.returncode, p2.stderr[-300:]))
    finally:
        if created:
            subprocess.run([sec, "delete-keychain", kc], env=senv, capture_output=True, timeout=30)
        shutil.rmtree(t, ignore_errors=True)


def test_setup_in_checkout():
    t = tempfile.mkdtemp(prefix="mcomp-install-")
    try:
        conf = os.path.join(t, "none.env")
        env = {"HOME": os.path.join(t, "home"), "PATH": "/usr/bin:/bin", "MCOMP_CONFIG": conf}
        p = subprocess.run([CLI, "setup"], env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        check("metacomputer setup in a checkout with no terminal and no --yes: explains, exit 3, install.sh not run",
              p.returncode == 3 and "setup --yes" in p.stderr and "==>" not in p.stdout, (p.returncode, p.stdout[-200:], p.stderr))
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_build_vault_quiet():
    """scripts/build-vault.sh leaves out the linker's harmless "search path ... not found" warnings (Swift with
    only the Command Line Tools) and nothing else; a failed compile still fails the build."""
    t = tempfile.mkdtemp(prefix="mcomp-vaultbuild-")
    try:
        fb = os.path.join(t, "bin")
        # a stand-in swiftc: the warnings and other output on both streams, then a binary that passes the selftest
        write(os.path.join(fb, "swiftc"), "#!/bin/bash\n"
              "out=; while [ $# -gt 0 ]; do [ \"$1\" = -o ] && out=\"$2\"; shift; done\n"
              "echo \"ld: warning: search path '/Library/Developer/CommandLineTools/Developer/usr/lib' not found\"\n"
              "echo 'warning: a compiler warning, kept'\n"
              "echo \"ld: warning: search path '/Library/Developer/CommandLineTools/Developer/Library/Frameworks' "
              "not found\" >&2\n"
              "echo 'ld: warning: another linker warning, kept' >&2\n"
              "[ -z \"${STUB_SWIFTC_FAIL:-}\" ] || { echo 'error: a compile error, kept' >&2; exit 1; }\n"
              "printf '#!/bin/sh\\nexit 0\\n' > \"$out\"; chmod +x \"$out\"\n", 0o755)
        write(os.path.join(fb, "codesign"), "#!/bin/sh\nexit 0\n", 0o755)
        env = dict(os.environ, PATH=fb + ":/usr/bin:/bin:/usr/sbin:/sbin", HOME=os.path.join(t, "home"))
        out = os.path.join(t, "out", "mcomp-vault")
        r = subprocess.run([os.path.join(LAB, "scripts", "build-vault.sh"), "--out", out], env=env,
                           capture_output=True, text=True, timeout=120)
        check("build-vault.sh: the linker's 'search path ... not found' warnings aren't shown",
              r.returncode == 0 and os.path.exists(out) and "search path" not in r.stdout + r.stderr,
              (r.returncode, r.stdout[-300:], r.stderr[-300:]))
        check("...every other line is, on its own stream",
              "a compiler warning, kept" in r.stdout and "another linker warning, kept" in r.stderr
              and "built " in r.stdout, (r.stdout[-300:], r.stderr[-300:]))
        os.remove(out)
        r = subprocess.run([os.path.join(LAB, "scripts", "build-vault.sh"), "--out", out],
                           env=dict(env, STUB_SWIFTC_FAIL="1"), capture_output=True, text=True, timeout=120)
        check("...and a compile that fails still fails the build, its error shown",
              r.returncode != 0 and not os.path.exists(out) and "a compile error, kept" in r.stderr,
              (r.returncode, r.stderr[-300:]))
    finally:
        shutil.rmtree(t, ignore_errors=True)


def test_quiet_builds():
    """The builds of mcomp-vm, the host tools and the vault print hundreds of harmless lines (Swift's warnings, Go's
    downloads): install.sh sends all of it, both streams, to a log in its logs folder and shows one line, then a ✓
    naming the log; a build that fails shows the log's last lines (escaped) and its path, and ends as before (mcomp-vm
    and the tools stop the install with their advice and exit status, the vault only warns)."""
    noise = ('for i in $(seq 1 80); do echo "/src/Sources/File$i.swift:$i:5: warning: harmless thing $i"; '
             'echo "warning: on stderr $i" >&2; done\necho "go: downloading example.com/mod v1.0.$RANDOM" >&2\n')

    def logs(root, name):
        d = os.path.join(root, "logs")
        return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.startswith(f"build-{name}-")] if os.path.isdir(d) else []

    def runner_box(exit_code):
        """install.sh that builds mcomp-vm (the stand-in source's install.sh prints NOISE, then exits EXIT_CODE) and
        whose host tools are out of date (the stand-in build prints Go's downloads)."""
        b = Box()
        root = install_root(b)
        commit = re.search(r"^MCOMP_VM_COMMIT=(\w+)", open(os.path.join(LAB, "config", "defaults.env")).read(), re.M).group(1)
        build = ('#!/bin/bash\n' + noise + ('echo "error: the last thing it said, \033]52;c;Zm9v\007 with an escape" >&2\n'
                                            f'exit {exit_code}\n' if exit_code else
                                            'mkdir -p "$HOME/.local/bin"; printf "#!/bin/sh\\n" > "$HOME/.local/bin/mcomp-vm"\n'
                                            'chmod +x "$HOME/.local/bin/mcomp-vm"\n'))
        write(os.path.join(b.t, "mcomp-vm-install.sh"), build, 0o755)
        write(os.path.join(b.stubs, "git"), STUB_HEAD +
              f'case "$*" in *clone*) for a; do d="$a"; done; mkdir -p "$d"; cp "{b.t}/mcomp-vm-install.sh" "$d/install.sh" ;;\n'
              f'  *rev-parse*) echo {commit} ;;\nesac\n', 0o755)
        write(os.path.join(root, "scripts", "build-tools.sh"),
              '#!/bin/bash\nif [ "$1" = --check ]; then exit 1; fi\n'
              'for i in $(seq 1 30); do echo "go: downloading example.com/dep$i v0.$i.0" >&2; done\necho "built the tools"\n', 0o755)
        return b, root

    b, root = runner_box(0)
    try:
        p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude", "--no-build"])
        term = p.stdout + p.stderr
        vm_logs, tool_logs = logs(root, "mcomp-vm"), logs(root, "tools")
        vm_text = open(vm_logs[0]).read() if len(vm_logs) == 1 else ""
        check("mcomp-vm's build prints 160 warning lines and succeeds: none of them on the terminal, the install goes on",
              p.returncode == 0 and "warning" not in term and "harmless thing" not in term and "==> Done" in p.stdout, term[-600:])
        check("...its log in the install's logs folder has every line, from both streams",
              all(f"warning: harmless thing {i}\n" in vm_text and f"warning: on stderr {i}\n" in vm_text for i in range(1, 81)),
              (vm_logs, vm_text[:200]))
        check("...one progress line, then a ✓ line naming the log",
              "building mcomp-vm" in p.stdout and re.search(r"✓.* mcomp-vm built .*build-mcomp-vm-\d{8}-\d{6}\.log", p.stdout),
              p.stdout[-800:])
        check("the host tools' build: Go's 'go: downloading' lines only in its log, and a ✓ line naming it",
              "go: downloading" not in term and len(tool_logs) == 1 and open(tool_logs[0]).read().count("go: downloading") == 30
              and re.search(r"✓.*host tools .*build-tools-\d{8}-\d{6}\.log", p.stdout), (tool_logs, p.stdout[-600:]))
    finally:
        b.close()
    # A logs folder that exists but can't be written: the builds still run, their logs go to temporary files.
    b, root = runner_box(0)
    try:
        ro = os.path.join(root, "logs")
        os.makedirs(ro, exist_ok=True)
        os.chmod(ro, 0o500)
        p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude", "--no-build"])
        check("an unwritable logs folder: the builds still run and the install goes on, logging to a temporary file",
              p.returncode == 0 and "==> Done" in p.stdout and "mcomp-build-mcomp-vm." in p.stdout
              and "warning" not in p.stdout + p.stderr, (p.returncode, (p.stdout + p.stderr)[-500:]))
        os.chmod(ro, 0o700)
    finally:
        b.close()
    b, root = runner_box(3)
    try:
        p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude", "--no-build"])
        err = p.stderr
        vm_logs = logs(root, "mcomp-vm")
        check("mcomp-vm's build fails: install stops with the build's exit status (3), naming the stage and its advice",
              p.returncode == 3 and 'stopped in "Lume, the VM runner' in err and "mcomp-vm didn't build" in err
              and "--lume=standard" in err, (p.returncode, err[-600:]))
        check("...after the end of the build's log (its last lines, not the first) and the log's path",
              "error: the last thing it said" in err and "warning: on stderr 80" in err and "harmless thing 1\n" not in err
              and len(vm_logs) == 1 and os.path.basename(vm_logs[0]) in err,
              (vm_logs, err[-900:]))
        check("...the log's lines escaped on the terminal (no raw escape sequence), the log itself exact",
              "\033]52" not in p.stdout + err and "\\x1b]52" in err and "\033]52" in open(vm_logs[0]).read(), err[-400:])
        check("...in that order: the log's end, its path, then the install's own advice",
              err.find("the last thing it said") < err.find("the whole log:") < err.find("install: stopped"), err[-600:])
    finally:
        b.close()
    # The vault: a build that warns a lot and succeeds, then one that fails (the install only warns and goes on).
    for fails in (False, True):
        b, root = vm_box("mctest,mctest-baseline")
        try:
            write(os.path.join(root, "scripts", "build-vault.sh"), "#!/bin/bash\n" + noise
                  + ("echo 'error: the vault compile failed' >&2\nexit 1\n" if fails else "echo 'installed the vault'\n"), 0o755)
            p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude"])
            term = p.stdout + p.stderr
            v = logs(root, "vault")
            text = open(v[0]).read() if len(v) == 1 else ""
            if not fails:
                check("the vault's build prints 160 warning lines and succeeds: none on the terminal, all in its log, ✓ naming it",
                      p.returncode == 0 and "warning" not in term and text.count("warning:") == 160 and "installed the vault" in text
                      and re.search(r"✓.*secrets vault.*build-vault-\d{8}-\d{6}\.log", p.stdout), (term[-600:], v))
            else:
                check("the vault's build fails: only a warning (exit 0, the install goes on to the VM and Done), "
                      "after the end of its log and its path",
                      p.returncode == 0 and "the vault didn't build" in p.stdout and "error: the vault compile failed" in p.stderr
                      and len(v) == 1 and os.path.basename(v[0]) in p.stderr and "harmless thing 1\n" not in term
                      and "already built" in p.stdout and "==> Done" in p.stdout, (p.returncode, term[-700:]))
        finally:
            b.close()


def test_vm_build_footer():
    """The VM build's advice: --replace-vm and the VM left to look at only when the build began (its marker is there);
    a build that stopped before it began (a refusal, such as settings whose folders are another VM's) made and deleted
    no VM, so its advice says that instead."""
    for began in (False, True):
        b, root = vm_box("")
        try:
            marker = os.path.join(root, ".run", "build-unfinished-mctest")
            write(os.path.join(root, "bin", "metacomputer"),
                  '#!/bin/bash\necho "not building mctest: refused by the test" >&2\n'
                  + (f'mkdir -p "{os.path.dirname(marker)}"; echo started=1 > "{marker}"\n' if began else "") + "exit 1\n", 0o755)
            p, _ = b.run([os.path.join(root, "install.sh"), "--yes", "--no-claude"])
            err = p.stderr
            if began:
                check("a VM build that began and failed: the advice names --replace-vm and the VM left running to look at",
                      p.returncode == 1 and "--replace-vm" in err and "metacomputer ssh admin" in err, err[-500:])
            else:
                check("a VM build that stopped before it began: exit 1 as before, no --replace-vm or VM to look at, says no VM was made",
                      p.returncode == 1 and "--replace-vm" not in err and "provisioning" not in err
                      and "no VM was made or deleted" in err and "run ./install.sh again" in err, err[-500:])
        finally:
            b.close()


def test_no_machine_paths():
    """Nothing in the installers or the defaults names one person's machine or tools."""
    bad = re.compile(r"/Users/[a-z]|secret exec|cua-lab/VNC|taild[0-9a-f]{4}|/Volumes/Elements")
    hits = []
    for rel in ("get.sh", "install.sh", "config/defaults.env", "config/local.env.example", "scripts/build-vm.sh",
                "scripts/fetch-artifacts.sh", "scripts/lib.sh", "bin/metacomputer", "docs/install.md"):
        p = os.path.join(LAB, rel)
        if os.path.exists(p):
            for n, line in enumerate(open(p), 1):
                if bad.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    check("installers, defaults and docs/install.md name no one's machine (home folders, personal tools, hosts)", not hits, hits)


test_get_args()
test_get_refusals()
test_get_space()
test_get_dry_run()
test_get_full_offline()
test_get_health_kept_vm()
test_get_homebrew_and_confirm()
test_get_existing()
test_get_confirm_provenance()
test_get_confirm_local_state()
test_install_refusals()
test_install_vm_decisions()
test_install_lock()
test_build_vm_locks()
test_lock_lifetime()
test_vm_mutation_locks()
test_build_tools_stamps()
test_install_existing()
test_install_tools_and_space()
test_vm_dir()
test_vnc_password()
test_vnc_real_keychain()
test_setup_in_checkout()
test_build_vault_quiet()
test_quiet_builds()
test_vm_build_footer()
test_no_machine_paths()
print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
