#!/usr/bin/env python3
"""Which program runs the VM (LUME): mcomp-vm, formerly lume-secure. No VM: a temporary
HOME holds fake runners, and install.sh runs from a temporary copy of the checkout with
a fake git.

    python3 tests/test_vm_runner.py

scripts/lib.sh: the default is ~/.local/bin/mcomp-vm; while the configured runner isn't
installed and ~/.local/bin/lume-secure is, it uses that and prints one notice (once per
command, on stderr); with neither, LUME stays as configured and the usual error appears.
install.sh: it builds mcomp-vm from MCOMP_VM_REPO at MCOMP_VM_TAG and checks the commit;
a local.env's LUME_SECURE_* pins still count, the new names win; --lume=secure is an
alias; an existing lume-secure is left alone; --uninstall names the runner in use.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(LAB, "scripts", "lib.sh")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def fake_runner(home, name):
    write(os.path.join(home, ".local", "bin", name),
          "#!/bin/sh\n"
          "case \"$1\" in\n"
          "  ls) echo '[]' ;;\n"
          "  run) echo '  --no-share-clipboard --no-share-audio --no-share-rosetta --no-host-sharing' ;;\n"
          "  *) exit 1 ;;\n"
          "esac\n", 0o755)


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main():
    td = tempfile.mkdtemp(prefix="mcomp-runner-")
    try:
        lib_cases(td)
        install_cases(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


# --- scripts/lib.sh ---------------------------------------------------------------
def lib_cases(td):
    n = [0]

    def resolve(have, lume_line="", script='printf "%s" "$LUME"'):
        """Source lib.sh with a fresh HOME that has the runners in HAVE; returns (LUME, stderr)."""
        n[0] += 1
        home = os.path.join(td, f"home{n[0]}")
        os.makedirs(home)
        for r in have:
            fake_runner(home, r)
        cfg = os.path.join(home, "test.env")
        write(cfg, f'VM_NAME=fake-runner-vm\nRUN_DIR="{home}/run"\nLOG_DIR="{home}/logs"\n{lume_line}\n')
        env = {k: v for k, v in os.environ.items() if k not in ("MCOMP_LUME_NOTICE", "LUME")}
        env.update(HOME=home, MCOMP_CONFIG=cfg, MCOMP_EVENTS_DIR=os.path.join(home, "events"))
        r = subprocess.run(["bash", "-c", f'. "{LIB}"; {script}'], env=env, capture_output=True, text=True, timeout=30)
        return home, r

    home, r = resolve(["mcomp-vm"])
    check("default: ~/.local/bin/mcomp-vm", r.stdout == f"{home}/.local/bin/mcomp-vm", r.stdout)
    check("...without a notice", r.stderr == "", r.stderr)

    home, r = resolve(["mcomp-vm", "lume-secure"])
    check("mcomp-vm and lume-secure both installed: mcomp-vm", r.stdout == f"{home}/.local/bin/mcomp-vm", r.stdout)
    check("...without a notice", r.stderr == "", r.stderr)

    home, r = resolve(["lume-secure"])
    lines = r.stderr.splitlines()
    check("only lume-secure installed: it's used", r.stdout == f"{home}/.local/bin/lume-secure", r.stdout)
    check("...with one notice line, on stderr", len(lines) == 1 and "mcomp-vm's former name" in r.stderr, r.stderr)
    check("...that names the missing runner and ./install.sh",
          f"{home}/.local/bin/mcomp-vm isn't installed" in r.stderr and "./install.sh installs mcomp-vm" in r.stderr,
          r.stderr)

    home, r = resolve(["lume-secure"], 'LUME="/nonexistent/mcomp-test/mcomp-vm"')
    check("a configured runner that's missing: lume-secure", r.stdout == f"{home}/.local/bin/lume-secure", r.stdout)
    check("...and the notice names the configured path", "/nonexistent/mcomp-test/mcomp-vm isn't installed" in r.stderr,
          r.stderr)

    home, r = resolve(["lume-secure"], script=f'bash -c \'. "{LIB}"; printf "%s" "$LUME"\'')
    check("a script the command runs uses lume-secure too", r.stdout == f"{home}/.local/bin/lume-secure", r.stdout)
    check("...and the notice appears once, not again", r.stderr.count("mcomp-vm's former name") == 1, r.stderr)

    home, r = resolve(["lume-secure", "mcomp-vm"], 'LUME="$HOME/.local/bin/lume-secure"')
    check("a local.env from before the rename (LUME=lume-secure): kept as set",
          r.stdout == f"{home}/.local/bin/lume-secure", r.stdout)
    check("...without a notice", r.stderr == "", r.stderr)

    home, r = resolve(["lume-secure", "lume"], 'LUME="$HOME/.local/bin/lume"')
    check("standard Lume as configured: kept", r.stdout == f"{home}/.local/bin/lume", r.stdout)
    check("...without a notice", r.stderr == "", r.stderr)

    home, r = resolve([])
    check("neither installed: LUME stays mcomp-vm", r.stdout == f"{home}/.local/bin/mcomp-vm", r.stdout)
    check("...without a notice", r.stderr == "", r.stderr)
    home, r = resolve([], script=f'"{LAB}/scripts/build-vm.sh"')
    check("...and build stops with the usual error",
          r.returncode != 0 and f"lume not found ({home}/.local/bin/mcomp-vm)" in r.stdout + r.stderr,
          (r.returncode, (r.stdout + r.stderr)[-300:]))


# --- install.sh -------------------------------------------------------------------
NEW_REPO = "https://github.com/metavert-io/metacomputer-vm.git"
NEW_TAG = "v0.7.2-mcomp.2"
NEW_COMMIT = "26dc0df2b75edaca26cb2306995a0b6437a87349"


def install_cases(td):
    # A copy of what install.sh reads, with a stand-in vault build.
    root = os.path.join(td, "checkout")
    os.makedirs(os.path.join(root, "config"))
    shutil.copy(os.path.join(LAB, "install.sh"), root)
    for f in ("defaults.env", "local.env.example"):
        shutil.copy(os.path.join(LAB, "config", f), os.path.join(root, "config"))
    write(os.path.join(root, "scripts", "build-vault.sh"), "#!/bin/sh\necho 'vault: skipped by the test'\n", 0o755)
    conf = os.path.join(root, "config", "local.env")
    # Fakes: git (clone writes an installer that makes $FAKE_RUNNER; rev-parse prints
    # $FAKE_HEAD), the Swift tools, the tools install.sh checks for, and claude.
    fakebin = os.path.join(td, "fakebin")
    calls = os.path.join(td, "git-calls")
    write(os.path.join(fakebin, "git"),
          "#!/bin/sh\n"
          f"echo \"$*\" >> '{calls}'\n"
          "case \"$1\" in\n"
          "  clone) for a; do dest=\"$a\"; done; mkdir -p \"$dest\"\n"
          "    printf '#!/bin/sh\\nmkdir -p \"$HOME/.local/bin\" \"$HOME/.local/share/%s\"\\n"
          "printf \"#!/bin/sh\\\\necho --no-host-sharing\\\\n\" > \"$HOME/.local/bin/%s\"\\n"
          "chmod +x \"$HOME/.local/bin/%s\"\\n' \"$FAKE_RUNNER\" \"$FAKE_RUNNER\" \"$FAKE_RUNNER\" > \"$dest/install.sh\"\n"
          "    chmod +x \"$dest/install.sh\" ;;\n"
          "  -C) echo \"$FAKE_HEAD\" ;;\n"
          "esac\n", 0o755)
    for t in ("xcode-select", "swift", "curl", "go", "gpg", "ffmpeg", "python3"):
        write(os.path.join(fakebin, t), "#!/bin/sh\necho /fake\n", 0o755)
    write(os.path.join(fakebin, "claude"), "#!/bin/sh\nexit 1\n", 0o755)
    n = [0]

    def install(args, local_env=None, have=(), head=NEW_COMMIT, runner="mcomp-vm", build_info=False,
                build_version="0.7.2-mcomp.2"):
        n[0] += 1
        home = os.path.join(td, f"ihome{n[0]}")
        os.makedirs(home)
        for r in have:
            fake_runner(home, r)
        if build_info:
            write(os.path.join(home, ".local", "share", "mcomp-vm", "BUILD_INFO"),
                  (f"version={build_version}\n" if build_version else "") + "upstream=lume-v0.7.2 f30fbc0\n")
        if local_env is None:
            if os.path.exists(conf):
                os.remove(conf)
        else:
            write(conf, local_env)
        if os.path.exists(calls):
            os.remove(calls)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MCOMP_", "LUME"))}
        env.update(HOME=home, PATH=fakebin + ":/usr/bin:/bin:/usr/sbin:/sbin", FAKE_HEAD=head, FAKE_RUNNER=runner)
        r = subprocess.run([os.path.join(root, "install.sh")] + args, env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=60)
        clone = open(calls).read().splitlines()[0] if os.path.exists(calls) else ""
        lume = [ln for ln in open(conf).read().splitlines() if ln.startswith("LUME=")] if os.path.exists(conf) else []
        return home, r, clone, lume

    flags = ["--no-build", "--no-claude", "--yes"]

    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, "BROWSERS=firefox\n", have=["lume-secure"])
    out = r.stdout + r.stderr
    check("install --lume=mcomp-vm: finishes", r.returncode == 0, out[-400:])
    check("...clones metacomputer-vm at the pinned tag", clone.endswith(f"--branch {NEW_TAG} {NEW_REPO} {root}/.cache/mcomp-vm"),
          clone)
    check("...installs ~/.local/bin/mcomp-vm", os.access(os.path.join(home, ".local", "bin", "mcomp-vm"), os.X_OK))
    check("...and points LUME there", lume == ['LUME="$HOME/.local/bin/mcomp-vm"'], lume)
    ref = os.path.join(td, "ref-home")
    fake_runner(ref, "lume-secure")
    check("...leaves lume-secure as it was",
          digest(os.path.join(home, ".local", "bin", "lume-secure")) == digest(os.path.join(ref, ".local", "bin", "lume-secure")))
    check("...and says it stays", "lume-secure, its former name, stays installed" in out, out[-400:])

    home, r, clone, lume = install(["--lume=secure"] + flags, "BROWSERS=firefox\n")
    check("--lume=secure still works and means mcomp-vm",
          r.returncode == 0 and NEW_TAG in clone and lume == ['LUME="$HOME/.local/bin/mcomp-vm"'], (clone, lume))

    old = ("LUME_SECURE_REPO=https://example.invalid/old.git\nLUME_SECURE_TAG=v0.0.1-old\n"
           "LUME_SECURE_COMMIT=1111111111111111111111111111111111111111\n")
    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, old, head="1111111111111111111111111111111111111111")
    check("pins under the former names in local.env still count",
          r.returncode == 0 and clone.endswith(f"--branch v0.0.1-old https://example.invalid/old.git {root}/.cache/mcomp-vm"),
          (r.returncode, clone, (r.stdout + r.stderr)[-300:]))

    new = ("MCOMP_VM_REPO=https://example.invalid/new.git\nMCOMP_VM_TAG=v9.9.9-new\n"
           "MCOMP_VM_COMMIT=2222222222222222222222222222222222222222\n")
    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, old + new, head="2222222222222222222222222222222222222222")
    check("...and the new names win when both are set",
          r.returncode == 0 and clone.endswith(f"--branch v9.9.9-new https://example.invalid/new.git {root}/.cache/mcomp-vm"),
          (r.returncode, clone))

    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, old, head="1111111111111111111111111111111111111111",
                                   runner="lume-secure")
    check("an old tag that installs lume-secure: LUME points at lume-secure",
          r.returncode == 0 and lume == ['LUME="$HOME/.local/bin/lume-secure"'], (lume, (r.stdout + r.stderr)[-300:]))

    before = 'BROWSERS=firefox\nLUME="$HOME/.local/bin/lume-secure"\n'
    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, before, head="3333333333333333333333333333333333333333")
    out = r.stdout + r.stderr
    check("a clone that isn't the pinned commit is refused",
          r.returncode != 0 and f"mcomp-vm {NEW_TAG} isn't the pinned commit {NEW_COMMIT}" in out, out[-400:])
    check("...nothing is installed and local.env is unchanged",
          not os.path.exists(os.path.join(home, ".local", "bin", "mcomp-vm")) and open(conf).read() == before)

    home, r, clone, lume = install(flags, before, have=["lume-secure"])
    out = r.stdout + r.stderr
    check("no --lume, local.env says lume-secure: keeps the choice as mcomp-vm",
          r.returncode == 0 and "keeping your choice: mcomp-vm (formerly lume-secure)" in out and NEW_TAG in clone,
          (clone, out[-300:]))
    check("...and LUME now points at mcomp-vm", lume == ['LUME="$HOME/.local/bin/mcomp-vm"'], lume)

    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, "BROWSERS=firefox\n", have=["mcomp-vm"], build_info=True)
    check("mcomp-vm already installed: says so and clones nothing",
          r.returncode == 0 and f"already installed: {home}/.local/bin/mcomp-vm" in r.stdout and clone == "",
          (clone, (r.stdout + r.stderr)[-300:]))
    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, "BROWSERS=firefox\n", have=["mcomp-vm"], build_info=True,
                                   build_version=None)
    check("an older mcomp-vm build (no version= line) is rebuilt from the pinned release",
          r.returncode == 0 and NEW_TAG in clone and "already installed" not in r.stdout, (clone, (r.stdout + r.stderr)[-300:]))
    home, r, clone, lume = install(["--lume=mcomp-vm"] + flags, "BROWSERS=firefox\n", have=["mcomp-vm"], build_info=True,
                                   build_version="0.7.2-mcomp.1")
    check("...and so is a build of an earlier release", r.returncode == 0 and NEW_TAG in clone, (clone, (r.stdout + r.stderr)[-300:]))

    # --uninstall names the runner in use.
    def uninstall(local_env, have):
        h, r, _, _ = install(["--uninstall"], local_env, have=have)
        return h, r.stdout + r.stderr

    h, out = uninstall('LUME="$HOME/.local/bin/mcomp-vm"\n', ["mcomp-vm"])
    check("uninstall with mcomp-vm: names it",
          f"with the runner that made it ({h}/.local/bin/mcomp-vm)" in out
          and "rm -rf ~/.local/share/mcomp-vm ~/.local/bin/mcomp-vm" in out and "lume-secure" not in out, out[-500:])
    h, out = uninstall('LUME="$HOME/.local/bin/mcomp-vm"\n', ["mcomp-vm", "lume-secure"])
    check("...and mentions lume-secure when that's installed too",
          f"({h}/.local/bin/mcomp-vm)" in out and "rm -rf ~/.local/share/lume-secure ~/.local/bin/lume-secure" in out,
          out[-500:])
    h, out = uninstall("BROWSERS=firefox\n", ["lume-secure"])
    check("uninstall when only lume-secure is installed: names lume-secure",
          f"({h}/.local/bin/lume-secure)" in out and "rm -rf ~/.local/share/lume-secure ~/.local/bin/lume-secure" in out
          and "mcomp-vm's former name, is installed too" not in out, out[-500:])
    h, out = uninstall('LUME="$HOME/.local/bin/lume"\n', ["lume"])
    check("uninstall with standard Lume: names lume",
          f"({h}/.local/bin/lume)" in out and "rm -rf ~/.local/share/lume ~/.local/bin/lume)" in out, out[-500:])


if __name__ == "__main__":
    sys.exit(main())
