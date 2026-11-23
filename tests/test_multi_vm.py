#!/usr/bin/env python3
"""More than one VM: which settings a command uses (--vm NAME, MCOMP_VM, MCOMP_CONFIG, config/local.env), the
refusals when that's unclear, the vault folder and event spool that follow the VM, and `vm list | create | delete`.
No VM: a copy of the checkout in a temporary folder, a fake runner in a temporary HOME (it logs every delete), a
stand-in for the vault, and private folders for locks, events and per-VM vaults. Nothing of this Mac's VMs, vault,
settings or keys is read or touched.

    python3 tests/test_multi_vm.py
"""
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
# MCOMP_TEST_PY runs everything the tests start under another Python (3.11 or later: scripts/lib.sh refuses older).
PY = os.environ.get("MCOMP_TEST_PY") or shutil.which("python3") or sys.executable

FAKE_RUNNER = r'''#!/usr/bin/env python3
import json, os, shutil, sys
state, log, storage = os.environ["FAKE_STATE"], os.environ["FAKE_LOG"], os.environ["FAKE_STORAGE"]
def load():
    return json.load(open(state))
def save(d):
    json.dump(d, open(state, "w"))
a = sys.argv[1:]
with open(log, "a") as f:
    f.write(" ".join(a) + "\n")
if os.environ.get("FAKE_LS_FAIL") and a[:1] == ["ls"]:
    sys.exit(1)
if a[:1] == ["ls"]:
    print(json.dumps(load()))
elif a[:1] == ["get"]:
    d = [v for v in load() if v["name"] == a[1]]
    if not d:
        sys.exit(1)
    print(json.dumps(d))
elif a[:1] == ["delete"]:
    save([v for v in load() if v["name"] != a[1]])
    shutil.rmtree(os.path.join(storage, a[1]), ignore_errors=True)    # as the runner does: the VM's whole folder
elif a[:2] == ["config", "get"]:
    if os.environ.get("FAKE_CONFIG_FAIL"):
        sys.exit(1)
    shown = os.environ.get("FAKE_STORAGE_SPELLING") or storage      # the same place spelled another way (tests)
    print(f"Default VM storage: home ({shown})\n\nConfigured VM storage locations:\n  - home: {shown} (default)\n")
elif a[:1] == ["create"]:
    d = os.path.join(storage, a[1])
    os.makedirs(d)
    mac = "aa:bb:cc:dd:ee:01"
    open(os.path.join(d, "disk.img"), "w").write("x" * 4096)
    json.dump({"macAddress": mac}, open(os.path.join(d, "config.json"), "w"))
    save(load() + [{"name": a[1], "status": "stopped", "locationName": "home", "macAddress": mac}])
elif a[:1] == ["stop"]:
    d = load()
    for v in d:
        if v["name"] == a[1]:
            v["status"] = "stopped"
    save(d)
elif a[:1] == ["run"]:
    print("--no-host-sharing")
else:
    sys.exit(1)
'''

FAKE_VAULT = r'''#!/bin/sh
case "$1" in version) echo '{"build":"release"}'; exit 0 ;; esac
echo "VAULT $1 dir=${MCOMP_VAULT_DIR:-unset} spool=${MCOMP_EVENTS_SPOOL:-unset}"
'''


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


def vm(name, status="stopped", gb=5):
    return {"name": name, "status": status, "locationName": "home",
            "diskSize": {"allocated": int(gb * 1e9), "total": 53687091200}}


class Box:
    """A checkout copy with its own HOME, fake runner, vault stand-in and private folders."""

    def __init__(self, td, tag):
        self.root = os.path.join(td, tag)
        self.co = os.path.join(self.root, "co")
        for d in ("bin", "scripts", "lib"):
            shutil.copytree(os.path.join(LAB, d), os.path.join(self.co, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(LAB, "install.sh"), self.co)
        os.makedirs(os.path.join(self.co, "config"))
        for f in os.listdir(os.path.join(LAB, "config")):
            src = os.path.join(LAB, "config", f)
            if f in ("local.env", "vms", "default-vm") or f.endswith(".local.env") or f.startswith(".default-vm."):
                continue                        # never this Mac's own settings (nor its default computer)
            (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, os.path.join(self.co, "config", f))
        self.home = os.path.join(self.root, "home")
        write(os.path.join(self.home, ".local", "bin", "mcomp-vm"), FAKE_RUNNER.replace("/usr/bin/env python3", PY), 0o755)
        self.vault = os.path.join(self.root, "vault-stand-in")
        write(self.vault, FAKE_VAULT, 0o755)
        self.state = os.path.join(self.root, "state.json")
        self.log = os.path.join(self.root, "runner.log")
        # Unix socket paths are short on macOS: the per-VM folders go in a short folder of their own.
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mcmv-", dir="/tmp"))   # canonical, as vaults are handed
        self.app = os.path.join(self.tmp, "v")         # the per-VM vaults' folder (made when one is first used)
        self.storage = os.path.join(self.root, "lume-vms")   # where the fake runner keeps VMs
        os.makedirs(self.storage)
        self.set_vms([])
        self.mc = os.path.join(self.co, "bin", "metacomputer")

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_vms(self, vms):
        json.dump(vms, open(self.state, "w"))
        open(self.log, "w").close()

    def vms(self):
        return [v["name"] for v in json.load(open(self.state))]

    def deletes(self):
        return [ln.split()[1] for ln in open(self.log).read().splitlines() if ln.startswith("delete ")]

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith("MCOMP_") and k not in ("LUME", "PY")}
        e.update(HOME=self.home, PATH=f"{os.path.dirname(PY)}:/usr/bin:/bin:/usr/sbin:/sbin", PY=PY,
                 FAKE_STATE=self.state, FAKE_LOG=self.log, FAKE_STORAGE=self.storage, MCOMP_VMS_APP_DIR=self.app,
                 MCOMP_LOCK_DIR=os.path.join(self.root, "locks"), MCOMP_EVENTS_DIR=os.path.join(self.root, "events"),
                 MCOMP_VAULT_BIN=self.vault, MCOMP_LESSONS_DIR=os.path.join(self.root, "lessons"),
                 MCOMP_STORAGE_HOME=os.path.join(self.root, "storage"),
                 MCOMP_SESSIONS_DIR=os.path.join(self.root, "sessions"))   # lib/sessionvm.py: never the account's
        e.update({k: v for k, v in extra.items() if v is not None})
        for k, v in extra.items():
            if v is None:
                e.pop(k, None)
        return e

    def run(self, *args, env=None, timeout=120):
        return subprocess.run([self.mc, *args], env=env or self.env(), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)

    def settings(self, path, text):
        write(os.path.join(self.co, path), text)
        return os.path.join(self.co, path)

    def resolve(self, vm_arg="", **env):
        """scripts/lib.sh as bin/metacomputer sources it: the settings it loads, or its refusal."""
        script = ('MCOMP_VM_ARG="$1"; . "$2/scripts/lib.sh"; '
                  'printf "%s\\n" "VM_NAME=$VM_NAME" "BASELINE_NAME=$BASELINE_NAME" "SSH_DIR=$SSH_DIR" '
                  '"SSH_PORT=$SSH_PORT" "VNC_PORT=$VNC_PORT" "RUN_DIR=$RUN_DIR" "LOG_DIR=$LOG_DIR" '
                  '"VAULT=$(mc_vault_dir)" "VAULT_DIR=$VAULT_DIR" "EVENTS_SPOOL=$EVENTS_SPOOL" "LOCAL_CONFIG=$LOCAL_CONFIG" '
                  '"KIND=$MC_SETTINGS_KIND" "IMPLICIT=$MC_IMPLICIT" "X_CONFIG=${MCOMP_CONFIG:-}" "X_VM=${MCOMP_VM:-}" '
                  '"FROM_DEFAULT=${MC_FROM_DEFAULT:-}" "X_NODEF=${MCOMP_NO_DEFAULT_VM:-}"')
        r = subprocess.run(["/bin/bash", "-c", script, "_", vm_arg, self.co], env=self.env(**env),
                           capture_output=True, text=True, timeout=60)
        out = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
        return r.returncode, out, r.stderr


def resolution(td):
    b = Box(td, "resolve")
    try:
        co, home, app = b.co, b.home, b.app
        sys_home = os.path.expanduser("~" + os.environ.get("USER", ""))
        b.settings("config/local.env", 'VM_NAME=mainvm\nBASELINE_NAME=mainvm-baseline\nBROWSERS="firefox"\n')
        demo = b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")

        # Back-compat: config/local.env alone is the main VM, with the vault's and the stream's own defaults.
        rc, s, err = b.resolve()
        check("no --vm: config/local.env's VM", rc == 0 and s.get("VM_NAME") == "mainvm" and s.get("KIND") == "local", (rc, s, err))
        check("...its vault is the vault's own folder (unchanged), from the system's home",
              s.get("VAULT_DIR") == "" and s.get("VAULT", "").endswith("/Library/Application Support/metacomputer")
              and not s.get("VAULT", "").startswith(home), s.get("VAULT"))
        check("...its spool is left to the event stream's own default", s.get("EVENTS_SPOOL") == "", s)
        check("...and nothing is pinned for the commands it starts", s.get("X_CONFIG") == "" and s.get("X_VM") == "", s)

        # --vm NAME: config/vms/NAME.env, with the VM's own identity everywhere.
        rc, s, err = b.resolve("demo")
        check("--vm demo: config/vms/demo.env", rc == 0 and s.get("VM_NAME") == "demo" and s.get("KIND") == "vm", (rc, err))
        check("...its baseline, keys and runtime folders are its own",
              s.get("BASELINE_NAME") == "demo-baseline" and s.get("SSH_DIR") == f"{home}/.ssh/metacomputer-demo"
              and s.get("RUN_DIR") == f"{co}/.run-vms/demo" and s.get("LOG_DIR") == f"{co}/logs-vms/demo", s)
        check("...and its own vault folder and spool", s.get("VAULT") == f"{app}/demo"
              and s.get("EVENTS_SPOOL") == f"{app}/demo/events/spool", s)
        check("...passed on to what it starts as its settings file (MCOMP_CONFIG), not a name",
              os.path.realpath(s.get("X_CONFIG", "")) == os.path.realpath(demo) and s.get("X_VM") == "", s)
        rc2, s2, _ = b.resolve(MCOMP_VM="demo")
        check("MCOMP_VM=demo: the same", rc2 == 0 and {k: s2.get(k) for k in s} == s, (s2, s))
        rc2, s2, _ = b.resolve("mainvm", MCOMP_VM="demo")
        check("--vm beats MCOMP_VM", rc2 == 0 and s2.get("VM_NAME") == "mainvm", s2)
        rc2, s2, _ = b.resolve(MCOMP_CONFIG=demo)
        check("MCOMP_CONFIG naming a config/vms/ file: the same per-VM settings",
              rc2 == 0 and s2.get("VM_NAME") == "demo" and s2.get("VAULT") == f"{app}/demo", s2)
        rc2, s2, _ = b.resolve("mainvm")
        check("--vm with config/local.env's VM name: config/local.env", rc2 == 0 and s2.get("KIND") == "local"
              and s2.get("VM_NAME") == "mainvm" and s2.get("VAULT_DIR") == "", s2)

        # MCOMP_CONFIG keeps working, as before.
        legacy = os.path.join(b.root, "legacy.env")
        write(legacy, "VM_NAME=legacy\nBASELINE_NAME=legacy-baseline\nSSH_PORT=2391\nVNC_PORT=6091\n")
        rc, s, err = b.resolve(MCOMP_CONFIG=legacy)
        check("MCOMP_CONFIG=FILE: that file, as before", rc == 0 and s.get("VM_NAME") == "legacy" and s.get("KIND") == "config", (rc, err))
        check("...with the vault's own folder and the stream's spool (unchanged)", s.get("VAULT_DIR") == ""
              and s.get("EVENTS_SPOOL") == "", s)
        rc, s, err = b.resolve(MCOMP_CONFIG=legacy, MCOMP_VAULT_DIR="/tmp/mc-elsewhere")
        check("MCOMP_VAULT_DIR still overrides it (as the folder it is)", rc == 0 and s.get("VAULT") == os.path.realpath("/tmp") + "/mc-elsewhere", s)
        rc, s, err = b.resolve("demo", MCOMP_EVENTS_SPOOL="/tmp/mc-spool-x")
        check("MCOMP_EVENTS_SPOOL naming another spool for a config/vms/ VM: refused (its folders are fixed by its name)",
              rc == 1 and "isn't the spool of config/vms/demo.env" in err, err)
        rc, s, err = b.resolve("demo", MCOMP_EVENTS_SPOOL=f"{app}/demo/events/spool", MCOMP_VAULT_DIR=f"{app}/demo")
        check("...naming its own (as what it starts passes on): fine", rc == 0 and s.get("EVENTS_SPOOL") == f"{app}/demo/events/spool"
              and s.get("VAULT") == f"{app}/demo", (err, s))

        # Refusals: never a guess.
        rc, s, err = b.resolve("nosuch")
        check("--vm with no settings for it: refused", rc == 1 and "no settings for a VM named nosuch" in err and not s, err)
        rc, s, err = b.resolve("demo", MCOMP_CONFIG=legacy)
        check("--vm and an MCOMP_CONFIG that disagree: refused", rc == 1 and "choose different settings" in err, err)
        rc, s, err = b.resolve("demo", MCOMP_CONFIG=demo)
        check("--vm and MCOMP_CONFIG naming the same file: fine", rc == 0 and s.get("VM_NAME") == "demo", err)
        rc, s, err = b.resolve("../x")
        check("a name that isn't one: refused", rc == 1 and "isn't a VM name" in err, err)
        b.settings("config/vms/wrong.env", "VM_NAME=mainvm\nSSH_PORT=2402\nVNC_PORT=6102\n")
        rc, s, err = b.resolve("wrong")
        check("a config/vms/ file naming another VM: refused", rc == 1 and "names the VM 'mainvm'" in err, err)
        b.settings("config/vms/noports.env", "VM_HOSTNAME=noports\n")
        rc, s, err = b.resolve("noports")
        check("a config/vms/ file without its own ports: refused", rc == 1 and "must set SSH_PORT" in err, err)
        b.settings("config/vms/borrow.env", f'. "{co}/config/local.env"\nVM_NAME=borrow\nSSH_PORT=2403\nVNC_PORT=6103\n')
        rc, s, err = b.resolve("borrow")
        check("a config/vms/ file that takes the main VM's baseline (sourcing local.env): refused",
              rc == 1 and "BASELINE_NAME must be borrow-baseline" in err, err)
        b.settings("config/vms/sameport.env", "SSH_PORT=2277\nVNC_PORT=6104\n")
        rc, s, err = b.resolve("sameport")
        check("...or its SSH port: refused", rc == 1 and "port 2277 is the main VM (config/local.env)'s SSH_PORT" in err, err)
        b.settings("config/vms/mainvm.env", "SSH_PORT=2405\nVNC_PORT=6105\n")
        rc, s, err = b.resolve("mainvm")
        check("config/local.env and config/vms/ both for one VM: refused", rc == 1 and "both settings for the VM mainvm" in err, err)
        os.remove(os.path.join(co, "config/vms/mainvm.env"))
        for k in ("SSH_DIR", "RUN_DIR", "LOG_DIR", "VAULT_DIR", "EVENTS_SPOOL"):
            b.settings("config/vms/setsdir.env", f'SSH_PORT=2406\nVNC_PORT=6106\n{k}="/tmp/mc-own-{k.lower()}"\n')
            rc, s, err = b.resolve("setsdir")
            check(f"a config/vms/ file that sets {k}: refused, saying its folders are fixed by its name",
                  rc == 1 and f"config/vms/setsdir.env sets {k}" in err and "fixed by its name" in err, err)
        os.remove(os.path.join(co, "config/vms/setsdir.env"))
        rc, s, err = b.resolve("demo", MCOMP_VAULT_DIR="/tmp/mc-other-vault")
        check("MCOMP_VAULT_DIR naming another folder for a config/vms/ VM: refused", rc == 1
              and "isn't the vault folder of config/vms/demo.env" in err, err)

        # The Python side reads the same file.
        sys.path.insert(0, os.path.join(co, "lib"))
        import importlib
        import labconfig
        importlib.reload(labconfig)
        cfg = labconfig.load({"MCOMP_CONFIG": demo})
        check("labconfig (Python): a config/vms/ file's derived VM_NAME and baseline",
              cfg.get("VM_NAME") == "demo" and cfg.get("BASELINE_NAME") == "demo-baseline" and cfg.get("SSH_PORT") == "2401", cfg.get("VM_NAME"))
        cfg = labconfig.load({"MCOMP_VM": "demo"})
        check("labconfig: MCOMP_VM=demo picks config/vms/demo.env", cfg.get("VM_NAME") == "demo", cfg.get("VM_NAME"))
        cfg = labconfig.load({})
        check("labconfig: config/local.env otherwise", cfg.get("VM_NAME") == "mainvm", cfg.get("VM_NAME"))
        sys.path.pop(0)

        # The commands: the vault and spool reach the vault; a notice says which VM and vault, once.
        r = b.run("--vm", "demo", "secrets", "status")
        check("--vm demo secrets: the vault runs with demo's folder and spool, no variables needed",
              r.returncode == 0 and f"dir={app}/demo " in r.stdout and f"spool={app}/demo/events/spool" in r.stdout, r.stdout + r.stderr)
        lines = r.stderr.splitlines()
        check("...after one line naming the VM, its settings and its vault",
              len(lines) == 1 and "VM demo (config/vms/demo.env), vault" in lines[0] and f"{app}/demo" in lines[0], r.stderr)
        check("...whose parent folder was made private", os.stat(app).st_mode & 0o077 == 0)
        r = b.run("secrets", "status")
        check("the main VM's vault: the vault's own default (nothing passed), as before",
              r.returncode == 0 and "dir=unset spool=unset" in r.stdout and "VM mainvm (config/local.env), vault" in r.stderr, r.stdout + r.stderr)
        r = b.run("--vm", "demo", "hub", "env")
        try:
            he = json.loads(r.stdout)
        except ValueError:
            he = {}
        check("--vm demo: the Python helpers get demo's settings (hub env)",
              he.get("MCOMP_VM_NAME") == "demo" and he.get("MCOMP_SSH_PORT") == "2401"
              and os.path.realpath(he.get("MCOMP_RUN_DIR", "")) == os.path.realpath(co) + "/.run-vms/demo"
              and he.get("MCOMP_VM_EVENTS_SPOOL") == f"{app}/demo/events/spool", r.stdout[-300:] + r.stderr)
        sys.path.insert(0, os.path.join(co, "lib"))
        import events
        old = dict(os.environ)
        try:
            os.environ.pop("MCOMP_EVENTS_SPOOL", None)
            os.environ["MCOMP_VM_EVENTS_SPOOL"] = he.get("MCOMP_VM_EVENTS_SPOOL", "")
            check("...and the event stream reads that VM's spool", events.spool_dir() == f"{app}/demo/events/spool", events.spool_dir())
            os.environ["MCOMP_EVENTS_SPOOL"] = "/tmp/mc-yours"
            check("...unless MCOMP_EVENTS_SPOOL says otherwise", events.spool_dir() == "/tmp/mc-yours")
        finally:
            os.environ.clear()
            os.environ.update(old)
            sys.path.pop(0)
        r = b.run("--vm", "demo", "stop")
        check("a command that changes a VM says which first (stop)", r.stderr.startswith("metacomputer: VM demo (config/vms/demo.env)\n")
              and r.stderr.count("metacomputer: VM demo") == 1, r.stderr)
        r = b.run("--vm", "demo", "where")
        check("where shows the VM, its settings, vault and spool", "vm        demo (baseline demo-baseline; settings: config/vms/demo.env)" in r.stdout
              and f"vault     {app}/demo" in r.stdout and f"spool     {app}/demo/events/spool" in r.stdout, r.stdout)
        r = b.run("--vm", env=b.env())
        check("--vm without a name: refused", r.returncode == 2 and "needs a VM name" in r.stderr, r.stderr)
        r = b.run("--vm", "demo", "setup", "--yes")
        check("setup for a VM chosen by name: refused (it sets up config/local.env's VM)", r.returncode == 1
              and "metacomputer --vm demo build" in r.stderr, r.stderr)
        r = subprocess.run(["/bin/bash", os.path.join(co, "install.sh")], env=b.env(MCOMP_VM="demo"), capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        check("install.sh with MCOMP_VM set: refused", r.returncode == 1 and "MCOMP_VM is set" in r.stderr, r.stderr[-300:])
    finally:
        b.close()


def ambiguity(td):
    b = Box(td, "ambig")
    try:
        # No config/local.env, no --vm: the defaults only when nothing else could be meant.
        b.set_vms([vm("metacomputer"), vm("metacomputer-baseline"), vm("metacomputer-ckpt-a")])
        r = b.run("checkpoints")
        check("no settings, only the default VM (and its copies) in the runner: goes ahead",
              r.returncode == 0 and "a" in r.stdout, r.stdout + r.stderr)
        b.set_vms([vm("metacomputer"), vm("metacomputer-baseline"), vm("mcdemo"), vm("mcdemo-baseline")])
        r = b.run("checkpoints")
        check("no settings, and the runner has another VM: refused (never the wrong one)",
              r.returncode == 1 and "VMs besides metacomputer (mcdemo)" in r.stderr and "--vm NAME" in r.stderr, r.stderr)
        r = b.run("start")
        check("...for start too, before anything runs", r.returncode == 1 and "VMs besides" in r.stderr
              and not any(ln.startswith(("run", "delete", "stop")) for ln in open(b.log).read().splitlines()), r.stderr)
        for cmd in (["help"], ["where"], ["vm", "list"]):
            r = b.run(*cmd)
            check(f"...but '{' '.join(cmd)}' (acts on no VM) still works", r.returncode == 0, r.stderr[-300:])
        r = b.run("--vm", "metacomputer", "checkpoints")
        check("...and --vm metacomputer says which: goes ahead", r.returncode == 0, r.stderr)
        b.set_vms([])
        r = b.run("checkpoints")
        check("no settings and no VMs at all (before a build): goes ahead", r.returncode == 0, r.stderr)
        b.settings("config/vms/side.env", "SSH_PORT=2420\nVNC_PORT=6120\n")
        r = b.run("checkpoints")
        check("no config/local.env but config/vms/ files: refused, naming them", r.returncode == 1 and "settings for: side" in r.stderr, r.stderr)
        os.remove(os.path.join(b.co, "config/vms/side.env"))
        b.set_vms([vm("metacomputer"), vm("other")])
        r = b.run("checkpoints", env=b.env(FAKE_LS_FAIL="1"))
        check("a runner list that fails: refused, not guessed", r.returncode == 1 and "couldn't list the VMs" in r.stderr, r.stderr)
        r = b.run("checkpoints", env=b.env(MCOMP_CONFIG=os.path.join(b.root, "typo.env")))
        check("MCOMP_CONFIG naming a file that isn't there: refused for a VM command", r.returncode == 1
              and "doesn't exist" in r.stderr, r.stderr)
        r = b.run("help", env=b.env(MCOMP_CONFIG=os.path.join(b.root, "typo.env")))
        check("...help still works with it (as before)", r.returncode == 0, r.stderr)
        os.remove(os.path.join(b.home, ".local", "bin", "mcomp-vm"))
        r = b.run("checkpoints")
        check("no runner installed yet: nothing to mix up, goes on", "VMs besides" not in r.stderr and "couldn't list" not in r.stderr, r.stderr)
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        write(os.path.join(b.home, ".local", "bin", "mcomp-vm"), FAKE_RUNNER.replace("/usr/bin/env python3", PY), 0o755)
        b.set_vms([vm("metacomputer"), vm("other")])
        r = b.run("checkpoints")
        check("with config/local.env: its VM, whatever else the runner has (as before)", r.returncode == 0, r.stderr)
        r = subprocess.run(["/bin/bash", os.path.join(b.co, "scripts", "build-vm.sh")], env=b.env(MCOMP_CONFIG=os.path.join(b.root, "typo.env")),
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        check("scripts/build-vm.sh run directly refuses the unclear case too", r.returncode != 0 and "doesn't exist" in r.stdout + r.stderr,
              (r.stdout + r.stderr)[-300:])
        check("...and nothing was deleted anywhere", b.deletes() == [], b.deletes())
    finally:
        b.close()


def listing(td):
    b = Box(td, "list")
    try:
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.settings("config/vms/clash.env", "SSH_PORT=2411\nVNC_PORT=6111\n")
        b.settings("config/vms/pa.env", "SSH_PORT=2431\nVNC_PORT=6131\n")
        b.settings("config/vms/pb.env", "SSH_PORT=2431\nVNC_PORT=6132\n")
        b.settings("config/vms/broken.env", "VM_NAME=other\nSSH_PORT=2402\nVNC_PORT=6102\n")
        b.set_vms([vm("metacomputer", "running", 12), vm("metacomputer-baseline", gb=4), vm("demo", gb=6),
                   vm("demo-ckpt-one", gb=6), vm("showcase", "running", 20), vm("showcase-baseline", gb=11)])
        r = b.run("vm", "list", "--json")
        d = json.loads(r.stdout or "{}")
        by = {v.get("name") or v.get("settings"): v for v in d.get("vms", [])}
        check("vm list --json: one entry per settings file", set(by) >= {"metacomputer", "demo", "clash"}, list(by))
        m, de = by.get("metacomputer", {}), by.get("demo", {})
        check("...state, disk, ports and baseline of the main VM", m.get("state") == "running" and m.get("disk_bytes") == 12_000_000_000
              and m.get("ssh_port") == "2277" and m.get("baseline", {}).get("present") is True and m.get("selected") is True, m)
        check("...a per-VM one with no baseline, its checkpoint and its own vault", de.get("baseline", {}).get("present") is False
              and de.get("checkpoints") == ["one"] and de.get("vault") == f"{b.app}/demo", de)
        check("...a VM with settings but not built", by.get("clash", {}).get("state") == "not built", by.get("clash"))
        check("...a settings file that's refused, with why", any(v.get("error") and "names the VM 'other'" in v["error"] for v in d["vms"]), d["vms"])
        check("...the runner's other VMs, with their baselines", [o["name"] for o in d.get("other", [])] == ["showcase"]
              and d["other"][0]["copies"][0]["name"] == "showcase-baseline", d.get("other"))
        check("...and two VMs that share a port are both refused, saying so", all(
            any(v.get("settings") == f"config/vms/{n}.env" and "port 2431" in (v.get("error") or "") for v in d["vms"]) for n in ("pa", "pb")),
              d["vms"])
        r = b.run("vm", "list")
        check("vm list (text): a table with the selected VM marked", r.returncode == 0 and "* metacomputer" in r.stdout.replace("  ", " ")
              and "showcase" in r.stdout and "not built" in r.stdout, r.stdout)
        r = b.run("vm", "list", env=b.env(FAKE_LS_FAIL="1"))
        check("a runner list that fails: the settings still shown, exit 1", r.returncode == 1 and "demo" in r.stdout
              and "couldn't list the VMs" in r.stdout, r.stdout)
    finally:
        b.close()

# lib/labconfig.py as a standalone helper loads it: "OK FILE VM_NAME", or "REFUSED why".
PY_CHOICE = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
try:
    import labconfig
except ValueError as e:
    print("REFUSED " + str(e))
    sys.exit()
print("OK " + os.path.realpath(labconfig.settings_path()) + " " + labconfig.CONFIG.get("VM_NAME", ""))
"""


def json_of(r):
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def default_computer(td):
    """The default computer: config/default-vm, read strictly and resolved exactly as --vm NAME (bash and Python
    agree), `metacomputer default [NAME]`, `list` (WHERE, the default), `--on PLACE`, and the commands that mean
    config/local.env's VM whatever the default is (each's main, MCOMP_NO_DEFAULT_VM)."""
    b = Box(td, "default")
    try:
        co = b.co
        ptr = os.path.join(co, "config", "default-vm")
        b.settings("config/local.env", "VM_NAME=mainvm\nBASELINE_NAME=mainvm-baseline\n")
        demo = b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.set_vms([vm("mainvm", "running", 12), vm("mainvm-baseline", gb=4), vm("demo", gb=6)])
        main_cfg = os.path.join(co, "config", "local.env")

        def put(data):
            if os.path.islink(ptr) or os.path.isfile(ptr):
                os.remove(ptr)
            elif os.path.isdir(ptr):
                os.rmdir(ptr)
            if data is not None:
                with open(ptr, "wb") as f:
                    f.write(data)

        rc, s, err = b.resolve()
        check("no config/default-vm: config/local.env's VM, as before", rc == 0 and s.get("VM_NAME") == "mainvm"
              and s.get("KIND") == "local" and s.get("FROM_DEFAULT") == "0" and s.get("X_CONFIG") == "", (rc, s, err))
        for data, how in ((b"demo\n", "with a newline"), (b"demo", "without one")):
            put(data)
            rc, s, err = b.resolve()
            check(f"config/default-vm naming demo ({how}): demo, exactly as --vm demo", rc == 0
                  and s.get("VM_NAME") == "demo" and s.get("KIND") == "vm" and s.get("FROM_DEFAULT") == "1"
                  and {k: v for k, v in s.items() if k != "FROM_DEFAULT"}
                  == {k: v for k, v in b.resolve("demo")[1].items() if k != "FROM_DEFAULT"}, (rc, s, err))
        check("...passed on to what it starts as its settings file (MCOMP_CONFIG)",
              os.path.realpath(s.get("X_CONFIG", "")) == os.path.realpath(demo), s)
        for args, env, want, what in ((("mainvm",), {}, "mainvm", "--vm"), ((), {"MCOMP_VM": "mainvm"}, "mainvm", "MCOMP_VM"),
                                      ((), {"MCOMP_CONFIG": main_cfg}, "mainvm", "MCOMP_CONFIG"),
                                      ((), {"MCOMP_NO_DEFAULT_VM": "1"}, "mainvm", "MCOMP_NO_DEFAULT_VM=1")):
            rc, s, err = b.resolve(*args, **env)
            check(f"...{what} wins over it", rc == 0 and s.get("VM_NAME") == want and s.get("FROM_DEFAULT") == "0", (rc, s, err))
        put(b"mainvm\n")
        rc, s, err = b.resolve()
        check("config/default-vm naming the main VM: config/local.env", rc == 0 and s.get("KIND") == "local"
              and s.get("VM_NAME") == "mainvm", (rc, s, err))
        put(b"nosuch\n")
        rc, s, err = b.resolve()
        check("config/default-vm naming a VM with no settings: refused, never another VM", rc == 1 and not s
              and "config/default-vm makes nosuch your default computer" in err and "never falls back" in err
              and "metacomputer default NAME" in err, err)
        b.settings("config/vms/clash.env", "SSH_PORT=2277\nVNC_PORT=6111\n")
        put(b"clash\n")
        rc, s, err = b.resolve()
        check("...or one whose settings are refused (--vm's refusals apply): refused, with the reason", rc == 1
              and "port 2277" in err and "config/default-vm makes clash" in err, err)
        os.remove(os.path.join(co, "config/vms/clash.env"))
        shapes = [(b"demo\n\n", "two newlines"), (b"demo \n", "a space"), (b" demo\n", "a leading space"),
                  (b"demo\r\n", "a carriage return"), (b"de\0mo\n", "a NUL"), (b"", "empty"), (b"\n", "only a newline"),
                  (b"demo\nmainvm\n", "two lines"), (b"-x\n", "a name starting with '-'"), (b".x\n", "a name starting with '.'"),
                  (b"x" * 65 + b"\n", "65 characters"), (b"d\xc3\xa9mo\n", "a non-ASCII letter"), (b"demo\nx", "text after the line")]
        for data, how in shapes:
            put(data)
            rc, s, err = b.resolve()
            check(f"config/default-vm with {how}: refused", rc == 1 and not s and "one VM name on one line" in err, (rc, err))
        put(None)
        os.symlink(demo, ptr)
        rc, s, err = b.resolve()
        check("config/default-vm as a link: refused", rc == 1 and "isn't a plain file" in err, err)
        os.remove(ptr)
        os.mkdir(ptr)
        rc, s, err = b.resolve()
        check("...as a folder: refused", rc == 1 and "isn't a plain file" in err, err)
        os.rmdir(ptr)
        put(b"x" * 64 + b"\n")
        rc, s, err = b.resolve()
        check("...a 64-character name is read (and refused for having no settings)", rc == 1 and "x" * 64 in err, err)

        # The Python side picks the same file, or refuses where bash does: lib/labconfig.py in a process of its own,
        # with the box's environment (its runner, its HOME), as a standalone Python helper would run.
        def py_choice(**env):
            r = subprocess.run([PY, "-B", "-c", PY_CHOICE, os.path.join(co, "lib")], env=b.env(**env),
                               capture_output=True, text=True, timeout=120)
            return r.stdout.strip()

        def sh_choice(**env):
            rc, s, err = b.resolve(**env)
            return f"OK {os.path.realpath(s.get('LOCAL_CONFIG', ''))} {s.get('VM_NAME')}" if rc == 0 else "REFUSED"

        def agree_on(cases):
            bad = []
            for data, env in cases:
                put(data)
                sh, py = sh_choice(**env), py_choice(**env)
                if (py.split(" ", 1)[0] if py.startswith("REFUSED") else py) != sh:
                    bad.append((data, env, sh, py[-300:]))
            return bad
        cases = [(b"demo\n", {}), (b"demo", {}), (b"mainvm\n", {}), (b"nosuch\n", {}), (b"demo\n", {"MCOMP_VM": "mainvm"}),
                 (b"demo\n", {"MCOMP_NO_DEFAULT_VM": "1"}), (None, {})] + [(d, {}) for d, _ in shapes]
        bad = agree_on(cases)
        check("lib/labconfig.py chooses the same settings as scripts/lib.sh for every pointer (or refuses where it does)",
              not bad, bad)
        # Targets bash refuses for what's in them: Python must refuse them too, never load them as they are.
        targets = {"idm": "VM_NAME=mainvm\nSSH_PORT=2461\nVNC_PORT=6161\n", "portc": "SSH_PORT=2277\nVNC_PORT=6162\n",
                   "setsdir": "SSH_PORT=2464\nVNC_PORT=6164\nSSH_DIR=/tmp/mc-x\n", "noports": "VM_HOSTNAME=x\n",
                   "shadow": "SSH_PORT=2401\nVNC_PORT=6165\n", "badbase": "SSH_PORT=2466\nVNC_PORT=6166\nBASELINE_NAME=mainvm-baseline\n"}
        for n, text in targets.items():
            b.settings(f"config/vms/{n}.env", text)
            bad = agree_on([(f"{n}\n".encode(), {})])
            check(f"...a pointer to config/vms/{n}.env, which bash refuses ({text.splitlines()[0]}...): Python refuses too",
                  not bad and py_choice().startswith("REFUSED") and "config/default-vm makes " + n in py_choice(), (bad, py_choice()))
            os.remove(os.path.join(co, f"config/vms/{n}.env"))
        b.settings("config/vms/badparse.env", "SSH_PORT=2467\nVNC_PORT=6167\nif [\n")
        bad = agree_on([(b"demo\n", {}), (b"badparse\n", {})])
        check("...a pointer to demo while another VM's settings can't be read (bash refuses demo then): Python refuses too",
              not bad and py_choice().startswith("REFUSED"), (bad, py_choice()))
        os.remove(os.path.join(co, "config/vms/badparse.env"))
        put(b"demo\n")
        out = py_choice()
        check("...and with a pointer that resolves, its settings are demo's", out.endswith(" demo") and "demo.env" in out, out)
        put(b"nosuch\n")
        out = py_choice()
        check("...with one that doesn't, loading the settings is refused (never another VM's)",
              out.startswith("REFUSED") and "config/default-vm makes nosuch" in out, out)
        put(None)

        # metacomputer default
        r = b.run("default")
        check("default, with no config/default-vm: the main VM and its settings", r.returncode == 0
              and "mainvm (this Mac, config/local.env) is your default computer" in r.stdout, r.stdout + r.stderr)
        d = json_of(b.run("default", "--json"))
        check("...--json", d.get("default") == "mainvm" and d.get("pointer") is None and d.get("settings") == "config/local.env"
              and d.get("place") == "mac" and d.get("ok") is True, d)
        open(b.log, "w").close()
        r = b.run("default", "demo")
        check("default demo: written, saying so", r.returncode == 0 and "demo (this Mac, config/vms/demo.env) is now your default computer"
              in r.stdout and open(ptr, "rb").read() == b"demo\n" and os.path.isfile(ptr) and not os.path.islink(ptr),
              r.stdout + r.stderr)
        check("...in one step (no temporary file left in config/)", sorted(os.listdir(os.path.join(co, "config"))) ==
              sorted(f for f in os.listdir(os.path.join(co, "config")) if not f.startswith(".default-vm")))
        check("...and no VM was started, stopped or changed", not any(ln.split()[0] in ("run", "stop", "delete", "create")
                                                                   for ln in open(b.log).read().splitlines() if ln), open(b.log).read())
        d = json_of(b.run("default", "--json"))
        check("default --json: demo, config/vms/demo.env, config/default-vm", d.get("default") == "demo"
              and d.get("settings") == "config/vms/demo.env" and d.get("pointer") == "config/default-vm", d)
        before = os.stat(ptr).st_mtime_ns
        r = b.run("default", "demo")
        check("default demo again: already, and nothing written", r.returncode == 0 and "already" in r.stdout
              and os.stat(ptr).st_mtime_ns == before, r.stdout)
        for name, words in (("nosuch", "no settings for a VM named nosuch"), ("../x", "isn't a VM name")):
            r = b.run("default", name)
            check(f"default {name}: refused, config/default-vm left as it was", r.returncode == 1 and words in r.stderr
                  and open(ptr, "rb").read() == b"demo\n", r.stderr)
        b.settings("config/vms/wrong.env", "VM_NAME=mainvm\nSSH_PORT=2402\nVNC_PORT=6102\n")
        r = b.run("default", "wrong")
        check("default NAME whose settings are refused: refused with the reason", r.returncode == 1
              and "names the VM 'mainvm'" in r.stderr and open(ptr, "rb").read() == b"demo\n", r.stderr)
        os.remove(os.path.join(co, "config/vms/wrong.env"))
        for args in (("--vm", "demo", "default"), ("--on", "mac", "default"), ("default", "demo", "mainvm"), ("default", "--x")):
            r = b.run(*args)
            check(f"{' '.join(args)}: refused", r.returncode != 0 and open(ptr, "rb").read() == b"demo\n", r.stderr)

        # With the default set, every command acts on it.
        r = b.run("secrets", "status")
        check("with demo the default: secrets reaches demo's vault, and the notice says it's the default",
              f"dir={b.app}/demo " in r.stdout and "VM demo (config/vms/demo.env; your default computer, config/default-vm)"
              in r.stderr, r.stdout + r.stderr)
        he = json_of(b.run("hub", "env"))
        check("...the Python helpers get demo's settings", he.get("MCOMP_VM_NAME") == "demo" and he.get("MCOMP_SSH_PORT") == "2401", he)
        he = json_of(b.run("hub", "env", env=b.env(MCOMP_VM="mainvm")))
        check("...MCOMP_VM=mainvm still picks the main VM", he.get("MCOMP_VM_NAME") == "mainvm", he)
        r = b.run("each", "main,demo", "--", "hub", "env")
        check("each main,demo: main is still config/local.env's VM", r.returncode == 0 and '"MCOMP_VM_NAME": "mainvm"' in r.stdout
              and '"MCOMP_VM_NAME": "demo"' in r.stdout, r.stdout[-600:] + r.stderr)

        # list / vm list
        r1, r2 = b.run("list"), b.run("vm", "list")
        check("list is vm list", r1.returncode == 0 and r1.stdout == r2.stdout, (r1.stdout, r2.stdout))
        lines = r1.stdout.splitlines()
        check("...with a WHERE column, this Mac for each", lines[0].split()[:3] == ["VM", "WHERE", "STATE"]
              and all("this Mac" in ln for ln in lines[1:3]), r1.stdout)
        check("...the default marked *, and said", any(ln.startswith("*") and " demo " in ln for ln in lines)
              and "your default computer (demo)" in r1.stdout, r1.stdout)
        d = json_of(b.run("list", "--json"))
        by = {v["name"]: v for v in d.get("vms", [])}
        check("list --json: place, where and default, and the fields it had", d.get("default") == "demo"
              and by.get("demo", {}).get("default") is True and by.get("mainvm", {}).get("default") is False
              and all(v.get("place") == "mac" and v.get("where") == "this Mac" for v in by.values())
              and all(k in by.get("demo", {}) for k in ("name", "settings", "kind", "error", "selected", "state", "disk_bytes",
                                                         "baseline", "checkpoints", "ssh_port", "vnc_port", "vault", "spool", "volumes"))
              and by["demo"]["selected"] is True, d)
        r = b.run("--vm", "mainvm", "list")
        check("--vm mainvm list: * marks mainvm, and says demo is the default", r.returncode == 0
              and any(ln.startswith("*") and " mainvm " in ln for ln in r.stdout.splitlines())
              and "Your default computer is demo" in r.stdout, r.stdout)
        check("list --on mac: the same computers", json_of(b.run("list", "--on", "mac", "--json")).get("vms") == d.get("vms"))
        # Every computer is on this Mac: cloud has none, a provider's place has none yet, and a word that's no place
        # is said to be none (the providers are your own server and Oracle Cloud).
        said = {"cloud": "has no cloud computers set up", "oci": "no computer on Oracle Cloud yet",
                "server": "no computer on your own server yet", "fly": "no place called 'fly'",
                "cloudflare": "no place called 'cloudflare'"}
        for place in ("cloud", "oci", "server", "fly", "cloudflare"):
            r = b.run("list", "--on", place)
            dj = json_of(b.run("list", "--on", place, "--json"))
            check(f"list --on {place}: none, saying so ({said[place]})", r.returncode == 0
                  and said[place] in r.stdout and dj.get("vms") == [] and dj.get("other") == []
                  and said[place] in (dj.get("note") or ""), (r.stdout, dj))

        # --on PLACE
        open(b.log, "w").close()
        for place in ("cloud", "oci", "fly", "cloudflare", "Cloud"):
            r = b.run("--on", place, "status")
            check(f"--on {place} status: refused, saying there's nothing there ({said[place.lower()]}), nothing run",
                  r.returncode == 1 and said[place.lower()] in r.stderr and "nothing was done" in r.stderr
                  and r.stdout == "", r.stdout + r.stderr)
        check("...and the runner only read its settings (loading demo's, as for --vm demo): no VM was listed or touched",
              set(open(b.log).read().splitlines()) <= {"config get"}, open(b.log).read())
        he = json_of(b.run("--on", "mac", "hub", "env"))
        check("--on mac: the default computer (demo)", he.get("MCOMP_VM_NAME") == "demo", he)
        he = json_of(b.run("--vm", "mainvm", "--on", "mac", "hub", "env"))
        check("--vm mainvm --on mac: they agree, mainvm", he.get("MCOMP_VM_NAME") == "mainvm", he)
        r = b.run("--vm", "mainvm", "--on", "cloud", "hub", "env")
        check("--vm mainvm --on cloud: refused, they disagree", r.returncode == 1 and "disagree" in r.stderr
              and "mainvm is on this Mac" in r.stderr and r.stdout == "", r.stderr)
        r = b.run("--on", "demo", "hub", "env")
        check("--on with a computer's name: refused, pointing at --vm", r.returncode == 1 and "--vm demo" in r.stderr, r.stderr)
        r = b.run("--on", "", "hub", "env")
        check("--on with nothing: a usage error", r.returncode == 2, r.stderr)

        # A pointer that doesn't resolve: commands refuse; the listings, default and help still work.
        put(b"nosuch\n")
        r = b.run("checkpoints")
        check("a default with no settings: a command refuses, saying how to fix it", r.returncode == 1
              and "config/default-vm makes nosuch" in r.stderr and "metacomputer default mainvm" in r.stderr, r.stderr)
        r = b.run("list")
        check("...list still works, marks nothing chosen, and says why", r.returncode == 0
              and not any(ln.startswith("*") for ln in r.stdout.splitlines()) and "doesn't resolve" in r.stdout
              and "config/default-vm makes nosuch" in r.stdout, r.stdout + r.stderr)
        d = json_of(b.run("list", "--json"))
        check("...list --json: the default it names, marked broken, nothing selected, the problem listed", d.get("selected") is None
              and d.get("default") == "nosuch" and d.get("default_broken") is True
              and any("nosuch" in p for p in d.get("problems", [])), d)
        r = b.run("default")
        check("...default says what's wrong (exit 1)", r.returncode == 1 and "config/default-vm makes nosuch" in r.stderr, r.stderr)
        check("...help works", b.run("help").returncode == 0)
        r = b.run("default", "mainvm")
        check("...default mainvm fixes it: config/default-vm removed", r.returncode == 0 and "removed" in r.stdout
              and not os.path.lexists(ptr), r.stdout + r.stderr)
        r = b.run("default", "mainvm")
        check("...and again: already the default, nothing to remove", r.returncode == 0 and "already" in r.stdout, r.stdout)
        put(b"demo\r\n")
        r = b.run("--vm", "demo", "list")
        check("a malformed pointer is shown by list even with --vm", r.returncode == 0 and "one VM name on one line" in r.stdout, r.stdout)
        put(b"demo\n")
        r = b.run("vm", "delete", "demo", "--confirm", "demo")
        check("vm delete of the default computer: refused without --force", r.returncode == 1
              and "your default computer" in r.stderr and b.deletes() == [], r.stderr)
        put(None)

        put(b"demo\n")
        r = b.run("setup")
        check("setup with demo the default: still the main VM's (it asks for a terminal, not for --vm)",
              r.returncode == 3 and "no terminal here to ask on" in r.stderr, r.stderr)
        put(b"nosuch\n")
        r = b.run("setup")
        check("...and with a default that doesn't resolve, setup still runs", r.returncode == 3, r.stderr)
        put(None)
        r = b.run("default", env=b.env(MCOMP_VM="demo"))
        check("default in a shell with MCOMP_VM set: the default, and that MCOMP_VM chooses instead here",
              r.returncode == 0 and "mainvm (this Mac, config/local.env) is your default computer" in r.stdout
              and "MCOMP_VM=demo chooses the VM instead" in r.stdout, r.stdout + r.stderr)

        # A checkout with no config/local.env and two VMs: no default, and --on mac can't tell which.
        os.remove(main_cfg)
        r = b.run("--on", "mac", "checkpoints")
        check("no config/local.env, two VMs, --on mac: unclear, listing them", r.returncode == 1
              and "more than one computer is on this Mac" in r.stderr and "demo" in r.stderr, r.stderr)
        r = b.run("default")
        check("...default: there's none (exit 1), as a command that names no VM refuses there", r.returncode == 1
              and "no default computer here" in r.stderr, r.stdout + r.stderr)
        r = b.run("default", "demo")
        check("...default demo: one now", r.returncode == 0 and open(ptr, "rb").read() == b"demo\n"
              and b.run("checkpoints").returncode == 0, r.stdout + r.stderr)
        r = b.run("default", "metacomputer")
        check("...default with the main VM's name: config/default-vm removed, and it says commands refuse again",
              r.returncode == 0 and not os.path.lexists(ptr) and "they refuse" in r.stdout, r.stdout + r.stderr)
    finally:
        b.close()


def put_pointer(ptr, data):
    """config/default-vm holding data (bytes), or none at all (None); whatever was there removed first."""
    if os.path.islink(ptr) or os.path.isfile(ptr) or (os.path.exists(ptr) and not os.path.isdir(ptr)):
        os.remove(ptr)
    elif os.path.isdir(ptr):
        os.rmdir(ptr)
    if data is not None:
        with open(ptr, "wb") as f:
            f.write(data)


def default_computer_audit(td):
    """The default computer against what changes meanwhile, and names that look like places: a VM named like a place
    is never what --on means; what a command chose is passed on, so a default set meanwhile doesn't move what it
    starts; the pointer is read once (a FIFO, links, a file swapped while it's read); `default NAME` and `vm delete
    NAME` are ordered by NAME's lock; and the listing's default is the stored one, apart from what overrode it."""
    b = Box(td, "default2")
    try:
        co = b.co
        ptr = os.path.join(co, "config", "default-vm")
        b.settings("config/local.env", "VM_NAME=mainvm\nBASELINE_NAME=mainvm-baseline\n")
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.set_vms([vm("mainvm"), vm("mainvm-baseline"), vm("demo"), vm("demo-baseline")])

        # A VM named like a place (only by hand: vm create refuses the names).
        b.settings("config/vms/cloud.env", "SSH_PORT=2451\nVNC_PORT=6151\n")
        b.settings("config/vms/mac.env", "SSH_PORT=2452\nVNC_PORT=6152\n")
        r = b.run("default", "cloud")
        check("a hand-made VM named cloud can still be the default (by its name)", r.returncode == 0, r.stdout + r.stderr)
        r = b.run("--on", "cloud", "hub", "env")
        check("--on cloud with the default a local VM named cloud: refused, no cloud computers (never that VM)",
              r.returncode == 1 and "no cloud computers set up" in r.stderr and r.stdout == "", r.stdout + r.stderr)
        r = b.run("--on", "cloud", "reset", "--yes")
        check("...the same for reset --yes, before anything runs", r.returncode == 1 and "no cloud computers set up" in r.stderr
              and not any(ln.split()[0] in ("delete", "stop", "clone", "run") for ln in open(b.log).read().splitlines() if ln),
              r.stderr + open(b.log).read())
        check("--on mac: that VM, which is on this Mac", json_of(b.run("--on", "mac", "hub", "env")).get("MCOMP_VM_NAME") == "cloud")
        r = b.run("--vm", "cloud", "--on", "cloud", "hub", "env")
        check("--vm cloud --on cloud: refused, they disagree", r.returncode == 1 and "disagree" in r.stderr, r.stderr)
        d = json_of(b.run("list", "--json"))
        check("list says both VMs have a place's name", sum("has a place's name" in p for p in d.get("problems", [])) == 2,
              d.get("problems"))
        put_pointer(ptr, None)
        check("with the main VM the default, a VM named mac doesn't take over --on mac",
              json_of(b.run("--on", "mac", "hub", "env")).get("MCOMP_VM_NAME") == "mainvm")
        for n in ("cloud", "mac"):
            os.remove(os.path.join(co, f"config/vms/{n}.env"))
            r = b.run("vm", "create", n, "--no-build")
            check(f"vm create {n}: refused, a place's name", r.returncode == 1 and "a place's name" in r.stderr
                  and not os.path.exists(os.path.join(co, f"config/vms/{n}.env")), r.stderr)

        # What a command chose is passed on: a default set (or removed) while it runs doesn't move what it starts.
        rc, s, err = b.resolve()
        check("no default computer: the main VM, passed on as MCOMP_NO_DEFAULT_VM=1", rc == 0 and s.get("VM_NAME") == "mainvm"
              and s.get("X_NODEF") == "1" and s.get("X_CONFIG") == "" and s.get("X_VM") == "", (rc, s, err))
        build = os.path.join(co, "scripts", "build-vm.sh")
        original = open(build).read()
        try:
            write(build, '#!/bin/bash\nprintf "demo\\n" > "$(dirname "$0")/../config/default-vm"\n'
                         '. "$(dirname "$0")/lib.sh"\necho "BUILD VM_NAME=$VM_NAME"\n', 0o755)
            r = b.run("build")
            check("build of the main VM, with demo made the default while it runs: what it starts still builds mainvm",
                  r.returncode == 0 and "BUILD VM_NAME=mainvm" in r.stdout and open(ptr).read() == "demo\n", r.stdout + r.stderr)
            write(build, '#!/bin/bash\nrm -f "$(dirname "$0")/../config/default-vm"\n'
                         '. "$(dirname "$0")/lib.sh"\necho "BUILD VM_NAME=$VM_NAME"\n', 0o755)
            r = b.run("build")
            check("build of demo (the default), with the default removed while it runs: what it starts still builds demo",
                  r.returncode == 0 and "BUILD VM_NAME=demo" in r.stdout and not os.path.lexists(ptr), r.stdout + r.stderr)
        finally:
            write(build, original, 0o755)

        # The pointer is read once: no FIFO hangs it, no link is followed, and a file swapped meanwhile is never mixed.
        sys.path.insert(0, os.path.join(co, "lib"))
        import importlib
        import defaultvm
        importlib.reload(defaultvm)
        sys.path.pop(0)

        def reads(path):
            try:
                return defaultvm.read(path)
            except defaultvm.Refused as e:
                return "refused: " + str(e)
        put_pointer(ptr, None)
        os.mkfifo(ptr)
        t0 = time.time()
        rc, s, err = b.resolve()
        check("config/default-vm as a FIFO: refused at once (not a plain file), nothing waits on it",
              rc == 1 and "isn't a plain file" in err and time.time() - t0 < 30, (rc, err, time.time() - t0))
        check("...the reader itself, the same", reads(ptr).startswith("refused") and "plain file" in reads(ptr))
        os.remove(ptr)
        target = os.path.join(b.root, "pointer-target")
        write(target, "demo\n")
        os.symlink(target, ptr)
        check("a link to a good file: refused by the reader (never followed)", reads(ptr).startswith("refused"))
        os.remove(ptr)
        os.symlink(os.path.join(b.root, "nothing-here"), ptr)
        check("...a dangling link: refused, not taken for no pointer", reads(ptr).startswith("refused"))
        os.remove(ptr)
        check("no file: no pointer", reads(ptr) is None)
        b.settings("config/vms/production.env", "SSH_PORT=2471\nVNC_PORT=6171\n")
        good, mixed = os.path.join(co, "config", ".swap-good"), os.path.join(co, "config", ".swap-bad")
        import threading
        stop = threading.Event()

        def swap():
            i = 0
            while not stop.is_set():
                src = good if i % 2 else mixed
                tmp = ptr + f".t{i % 2}"
                with open(src, "rb") as f, open(tmp, "wb") as g:
                    g.write(f.read())
                os.replace(tmp, ptr)
                i += 1
        write(good, "demo\n")
        write(mixed, "pro\nduction\n")
        put_pointer(ptr, b"demo\n")                           # there from the start: every read sees one of the two
        th = threading.Thread(target=swap, daemon=True)
        th.start()
        try:
            seen = {reads(ptr) for _ in range(3000)}
            names = set()
            for _ in range(10):
                rc, s, err = b.resolve()
                names.add(s.get("VM_NAME") if rc == 0 else "refused")
        finally:
            stop.set()
            th.join()
        check("a pointer swapped between demo and a two-line file while it's read: demo or refused, never 'production'",
              "production" not in seen and seen <= {"demo", "refused: " + defaultvm.SHAPE} and "demo" in seen, seen)
        check("...and every command that read it chose demo or refused (never production)",
              names <= {"demo", "refused"} and "production" not in names, names)
        for f in (good, mixed, ptr + ".t0", ptr + ".t1"):
            if os.path.lexists(f):
                os.remove(f)
        put_pointer(ptr, None)
        os.remove(os.path.join(co, "config/vms/production.env"))

        # default NAME and vm delete NAME take NAME's lock: never a default on a VM being deleted.
        locks = os.path.join(b.root, "locks")
        holder = hold_lock(os.path.join(locks, "vm-demo.lock"))     # a build or delete of demo under way
        r = b.run("default", "demo", env=b.env(MCOMP_LOCK_WAIT="1"))
        release(holder)
        check("default demo while demo's lock is held (a build or delete): refused, config/default-vm as it was",
              r.returncode == 1 and "being built, deleted or changed" in r.stderr and not os.path.lexists(ptr), r.stderr)
        holder = hold_lock(os.path.join(locks, "vm-demo.lock"))     # a default demo checking and writing
        r = b.run("vm", "delete", "demo", "--confirm", "demo", env=b.env(MCOMP_LOCK_WAIT="1"))
        release(holder)
        check("vm delete demo while demo's lock is held: refused, nothing deleted", r.returncode == 1
              and "another build or change of the VM demo" in r.stderr and b.deletes() == [], r.stderr)
        p = Pty([b.mc, "vm", "delete", "demo"], b.env())
        at_prompt = p.expect("Type the name of the VM")
        r = b.run("default", "demo", env=b.env(MCOMP_LOCK_WAIT="1"))
        p.send("not-demo\n")
        code = p.wait()
        check("a real vm delete demo at its prompt (holding the lock): default demo refused meanwhile", at_prompt
              and r.returncode == 1 and "being built, deleted or changed" in r.stderr and not os.path.lexists(ptr), r.stderr)
        check("...and the delete, told another name, deleted nothing", code not in (0, None) and b.deletes() == [], code)
        r = b.run("default", "demo")
        check("once the delete is over, default demo goes ahead", r.returncode == 0 and open(ptr).read() == "demo\n", r.stderr)
        r = b.run("vm", "delete", "demo", "--confirm", "demo")
        check("...and now vm delete demo refuses it (your default computer)", r.returncode == 1
              and "your default computer" in r.stderr and b.deletes() == [], r.stderr)

        # The listing's default is the stored one; what overrode the selection is listed apart.
        esc_env = b.env(MCOMP_NO_DEFAULT_VM="1")
        d, dd = json_of(b.run("list", "--json", env=esc_env)), json_of(b.run("default", "--json", env=esc_env))
        by = {v["name"]: v for v in d.get("vms", [])}
        check("MCOMP_NO_DEFAULT_VM=1 list --json: selected mainvm, default still demo, the override named",
              d.get("selected") == "mainvm" and d.get("default") == "demo" and d.get("overridden_by") == "MCOMP_NO_DEFAULT_VM=1"
              and by.get("demo", {}).get("default") is True and by.get("mainvm", {}).get("default") is False
              and by.get("mainvm", {}).get("selected") is True, d)
        check("...and default --json agrees", dd.get("default") == d.get("default") and dd.get("overridden_by") == d.get("overridden_by"),
              (dd, d.get("default"), d.get("overridden_by")))
        r = b.run("list", env=esc_env)
        check("...the table says what chose mainvm, and that demo is the default", "chosen by MCOMP_NO_DEFAULT_VM=1" in r.stdout
              and "Your default computer is demo" in r.stdout, r.stdout)
        d = json_of(b.run("list", "--json", env=b.env(MCOMP_VM="mainvm")))
        check("MCOMP_VM=mainvm list --json: default demo, overridden by MCOMP_VM=mainvm", d.get("default") == "demo"
              and d.get("overridden_by") == "MCOMP_VM=mainvm" and d.get("selected") == "mainvm", d)
        d = json_of(b.run("list", "--json"))
        check("list --json with nothing overriding: overridden_by is null", d.get("overridden_by") is None and d.get("selected") == "demo", d)
        put_pointer(ptr, b"nosuch\n")
        d, r = json_of(b.run("list", "--json", env=esc_env)), b.run("default", "--json", env=esc_env)
        dd = json_of(r)
        check("a pointer with no settings, under MCOMP_NO_DEFAULT_VM=1: list shows the default it names, broken, the "
              "problem once, mainvm selected", d.get("default") == "nosuch" and d.get("default_broken") is True
              and d.get("selected") == "mainvm" and not any(v.get("default") for v in d.get("vms", []))
              and sum("nosuch" in p for p in d.get("problems", [])) == 1, d)
        d2 = json_of(b.run("list", "--json"))
        check("...the same default and problem without it (where nothing is selected)", d2.get("default") == "nosuch"
              and d2.get("default_broken") is True and d2.get("selected") is None
              and sum("nosuch" in p for p in d2.get("problems", [])) == 1, d2)
        check("...and default --json says it doesn't resolve (exit 1)", r.returncode == 1 and dd.get("ok") is False
              and dd.get("default") == d.get("default")
              and "nosuch" in (dd.get("error") or "") and dd.get("overridden_by") == "MCOMP_NO_DEFAULT_VM=1", dd)
        put_pointer(ptr, b"demo\r\n")
        d = json_of(b.run("list", "--json", env=esc_env))
        check("...a malformed one too", d.get("default") is None and any("one VM name on one line" in p for p in d.get("problems", [])), d)
        put_pointer(ptr, None)
    finally:
        b.close()


# lib/labconfig.py's settings when the file is replaced right after scripts/lib.sh checked it: argv[1] the lib folder,
# argv[2] the settings file to rewrite, argv[3] what to write. Prints "VM_NAME BASELINE_NAME SSH_PORT", or "REFUSED why".
PY_SWAP = r"""
import os, sys
os.environ["MCOMP_NO_DEFAULT_VM"] = "1"          # importing loads nothing pointer-chosen
sys.path.insert(0, sys.argv[1])
import labconfig
real = labconfig.resolve_default
def swapped(name, env=None):
    out = real(name, env)
    with open(sys.argv[2], "w") as f:              # an editor replaces it now, after the check
        f.write(sys.argv[3])
    return out
labconfig.resolve_default = swapped
env = {k: v for k, v in os.environ.items() if k != "MCOMP_NO_DEFAULT_VM"}
try:
    c = labconfig.load(env)
except ValueError as e:
    print("REFUSED " + str(e))
    sys.exit()
print(c.get("VM_NAME"), c.get("BASELINE_NAME"), c.get("SSH_PORT"))
"""


def default_computer_recheck(td):
    """The Python side gets exactly what scripts/lib.sh checked: a bash with no startup file or imported function,
    one strict record, the values used as they are (never the file read again, never the defaults); and vm delete
    refuses while config/default-vm can't be read (it may name that VM)."""
    b = Box(td, "default3")
    try:
        co = b.co
        ptr = os.path.join(co, "config", "default-vm")
        demo = b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.settings("config/local.env", "VM_NAME=mainvm\nBASELINE_NAME=mainvm-baseline\n")
        b.set_vms([vm("mainvm"), vm("mainvm-baseline"), vm("demo"), vm("demo-baseline")])

        def py_choice(**env):
            r = subprocess.run([PY, "-B", "-c", PY_CHOICE, os.path.join(co, "lib")], env=b.env(**env),
                               capture_output=True, text=True, timeout=120)
            return r.stdout.strip()
        put_pointer(ptr, b"demo\n")
        want = f"OK {os.path.realpath(demo)} demo"
        check("the default demo, from Python: demo's file and name", py_choice() == want, py_choice())
        banner = os.path.join(b.root, "bashenv.sh")
        write(banner, f'echo "startup banner"\necho "{os.path.realpath(os.path.join(co, "config", "local.env"))}"\n'
                      'VM_NAME=mainvm; printf() { builtin printf "%s\\0" MCOMP-SETTINGS/1 FILE /etc/hosts VM_NAME mainvm END >&"$fd"; }\n')
        check("with a BASH_ENV that prints a banner, a path and defines printf: still demo (no startup file is read)",
              py_choice(BASH_ENV=banner) == want, py_choice(BASH_ENV=banner))
        check("...the same with ENV", py_choice(ENV=banner) == want, py_choice(ENV=banner))
        fake_fn = '() { builtin printf "%s\\0" MCOMP-SETTINGS/1 FILE /etc/hosts VM_NAME mainvm END >&"$fd"; }'
        check("...and with exported functions named printf and sed: still demo (none is imported)",
              py_choice(**{"BASH_FUNC_printf%%": fake_fn, "BASH_FUNC_sed%%": "() { echo VM_NAME; }"}) == want,
              py_choice(**{"BASH_FUNC_printf%%": fake_fn}))
        # The record is checked exactly; anything else refuses (never the defaults).
        sys.path.insert(0, os.path.join(co, "lib"))
        import importlib
        old = os.environ.get("MCOMP_NO_DEFAULT_VM")
        os.environ["MCOMP_NO_DEFAULT_VM"] = "1"
        try:
            import labconfig
            importlib.reload(labconfig)
        finally:
            if old is None:
                os.environ.pop("MCOMP_NO_DEFAULT_VM", None)
            else:
                os.environ["MCOMP_NO_DEFAULT_VM"] = old
        sys.path.pop(0)
        good = b"\0".join([b"MCOMP-SETTINGS/1", b"FILE", demo.encode(), b"VM_NAME", b"demo", b"SSH_PORT", b"2401", b"END"]) + b"\0"

        def decoded(data, rc=0):
            try:
                return labconfig.decode_record(data, rc)
            except labconfig.Refused as e:
                return "refused: " + str(e)
        check("a well-formed record: its file and values", decoded(good) == (demo, {"VM_NAME": "demo", "SSH_PORT": "2401"}),
              decoded(good))
        bad = {"a banner before it": b"startup banner\n" + good, "text after it": good + b"x",
               "no END": good[:-4], "no header": good[len(b"MCOMP-SETTINGS/1\0"):], "an odd field": good[:-4] + b"X\0END\0",
               "a key twice": good[:-4] + b"VM_NAME\0mainvm\0END\0", "a key that isn't one": good[:-4] + b"vm name\0x\0END\0",
               "no VM_NAME": b"MCOMP-SETTINGS/1\0FILE\0/x.env\0END\0", "a relative file": good.replace(demo.encode(), b"x.env"),
               "empty": b"", "bytes that aren't UTF-8": good[:-4] + b"K\0\xff\0END\0"}
        for what, data in bad.items():
            check(f"a record with {what}: refused", str(decoded(data)).startswith("refused"), decoded(data))
        check("a good record from a bash that failed: refused", str(decoded(good, 3)).startswith("refused"))
        refusal = b"MCOMP-SETTINGS/1\0REFUSED\0no settings for a VM named x\0END\0"
        check("a refusal: refused with bash's reason", decoded(refusal, 1) == "refused: no settings for a VM named x")
        check("...but not from a bash that exited 0", decoded(refusal, 0) == "refused: the check of its settings didn't answer as it should")

        # The values bash checked are the ones used: the file isn't read again.
        r = subprocess.run([PY, "-B", "-c", PY_SWAP, os.path.join(co, "lib"), demo, "VM_NAME=mainvm\nSSH_PORT=2277\nVNC_PORT=5977\n"],
                           env=b.env(), capture_output=True, text=True, timeout=120)
        check("demo.env replaced (another VM's name and ports) right after it was checked: Python still has demo's",
              r.stdout.strip() == "demo demo-baseline 2401", r.stdout + r.stderr[-300:])
        write(demo, "SSH_PORT=2401\nVNC_PORT=6101\nif false; then\n  VM_NAME=mainvm\nfi\n")
        check("a name bash never sets (inside 'if false'): Python's VM_NAME is bash's, demo", py_choice() == want, py_choice())
        write(demo, "SSH_PORT=2401\nVNC_PORT=6101\nGUEST_USER=\"$(printf alice)\"\n")
        r = subprocess.run([PY, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import labconfig; "
                            "print(labconfig.CONFIG.get('GUEST_USER'), labconfig.CONFIG.get('VNC_PORT'))", os.path.join(co, "lib")],
                           env=b.env(), capture_output=True, text=True, timeout=120)
        check("...and a value as bash made it (a command substitution), with the defaults' keys too", r.stdout.split()
              == ["alice", "6101"], r.stdout + r.stderr[-300:])
        write(demo, "SSH_PORT=2401\nVNC_PORT=6101\n")

        # vm delete refuses while the pointer can't be read: it may be naming that VM.
        os.chmod(ptr, 0)
        try:
            r = b.run("--vm", "demo", "vm", "delete", "demo", "--confirm", "demo")
        finally:
            os.chmod(ptr, 0o644)
        check("vm delete demo while config/default-vm can't be read (no permission): refused, saying why, nothing deleted",
              r.returncode == 1 and "can't be told" in r.stderr and "isn't a plain file" in r.stderr and b.deletes() == [],
              r.stderr)
        put_pointer(ptr, None)
        os.mkfifo(ptr)
        r = b.run("--vm", "demo", "vm", "delete", "demo", "--confirm", "demo")
        os.remove(ptr)
        check("...as a FIFO: the same", r.returncode == 1 and "can't be told" in r.stderr and b.deletes() == [], r.stderr)
        put_pointer(ptr, b"mainvm\n")
        r = b.run("vm", "delete", "demo", "--confirm", "demo")
        check("...and with a readable pointer naming another VM, the delete of demo goes ahead", r.returncode == 0
              and "demo" in b.deletes(), r.stdout + r.stderr)
    finally:
        b.close()



# A BASH_ENV for every bash a command starts: the shell's own lookups of config/default-vm ([ -e ], [ -L ] and the
# like) fail, as stat() and lstat() do when the lookup gets an I/O error.
EIO_BASH_ENV = r"""[() {
  case "$1|$2|$3" in
    -[eLfrsdh]"|"*/config/default-vm"|]") return 1 ;;
    "!|"-[eLfrsdh]"|"*/config/default-vm) return 0 ;;
  esac
  builtin [ "$@"
}
"""
# A PY whose run of lib/defaultvm.py gets that I/O error from its open of config/default-vm; anything else is the
# real Python (REAL_PY).
EIO_PY = r"""#!/bin/bash
case " $* " in
  *"/lib/defaultvm.py read "*)
    exec "REAL_PY" -I -B -c 'import errno, os, runpy, sys
real = os.open
def failing(path, *a, **k):
    if os.fsdecode(path).endswith("/config/default-vm"):
        raise OSError(errno.EIO, os.strerror(errno.EIO), path)
    return real(path, *a, **k)
os.open = failing
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")' "${@:3}" ;;
esac
exec "REAL_PY" "$@"
"""
# lib/labconfig.py's resolution of the default computer argv[2], from the lib folder argv[1], with a deadline of argv[3]
# seconds; with argv[4] "no-waitid", as a Python without os.waitid runs it (macOS's before 3.13: its kqueue watch).
# Prints "OK VM_NAME SECONDS" or "REFUSED SECONDS why".
PY_RESOLVE = r"""
import os, sys, time
if sys.argv[4:5] == ["no-waitid"] and hasattr(os, "waitid"):
    del os.waitid
sys.path.insert(0, sys.argv[1])
import labconfig
labconfig._DEADLINE = float(sys.argv[3])
t = time.monotonic()
try:
    s = labconfig.resolve_default(sys.argv[2])[1]
    print("OK", s.get("VM_NAME"), round(time.monotonic() - t, 1))
except labconfig.Refused as e:
    print("REFUSED", round(time.monotonic() - t, 1), e)
"""


def helpers(token):
    """The pids of the test's helper processes (named token with exec -a) still running."""
    r = subprocess.run(["pgrep", "-f", token], capture_output=True, text=True)
    return r.stdout.split()


def helpers_gone(token, wait=5.0):
    end = time.monotonic() + wait
    while helpers(token) and time.monotonic() < end:
        time.sleep(0.1)
    return not helpers(token)


def default_computer_recheck2(td):
    """A lookup of config/default-vm that fails isn't "no pointer": the reader is always asked, and vm delete refuses
    (no permission on its folder, an I/O error). The Python side's resolution: a helper the settings leave running
    never holds up the answer (only the record's own step opens its pipe), and one that hangs or says too much is
    killed with everything it started."""
    b = Box(td, "default4")
    tokens = []
    try:
        co = b.co
        ptr = os.path.join(co, "config", "default-vm")
        demo = b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.settings("config/local.env", "VM_NAME=mainvm\nBASELINE_NAME=mainvm-baseline\n")
        b.set_vms([vm("mainvm"), vm("mainvm-baseline"), vm("demo"), vm("demo-baseline")])

        # The shell's reader, mc_read_default, on a pointer whose lookup fails for real (a folder it can't search).
        script = ('MC_LIB_DEFS_ONLY=1 . "$1/scripts/lib.sh" || exit 9; MC_DEFAULT_FILE="$2"; '
                  'if mc_read_default; then printf "ok:%s" "$MC_DEFAULT_PTR"; else printf "refused:%s" "$MC_REFUSAL"; fi')

        def read_ptr(path, **env):
            r = subprocess.run(["/bin/bash", "-c", script, "_", co, path], env=b.env(**env), capture_output=True,
                               text=True, timeout=60)
            return r.stdout
        locked = os.path.join(b.root, "locked")
        write(os.path.join(locked, "default-vm"), "demo\n")
        if os.geteuid() != 0:
            os.chmod(locked, 0o600)                 # readable, not searchable: a lookup inside it fails (EACCES)
            try:
                out = read_ptr(os.path.join(locked, "default-vm"))
            finally:
                os.chmod(locked, 0o700)
            check("mc_read_default, config/default-vm in a folder it can't search (the lookup fails): refused, not "
                  "'no pointer'", out.startswith("refused:") and "isn't a plain file" in out, out)
        check("...the same file, readable: its name", read_ptr(os.path.join(locked, "default-vm")) == "ok:demo")
        check("...no file at all: no pointer (from the reader)", read_ptr(os.path.join(locked, "nothing")) == "ok:")
        check("...a folder on the way that doesn't exist: no pointer", read_ptr(os.path.join(b.root, "gone", "default-vm"))
              == "ok:")
        # vm delete with explicit --vm (the pointer isn't read at startup) while its lookups get an I/O error: the
        # shell's tests say there's nothing and the reader's open fails. Refused, as a pointer that may name demo.
        envfile = os.path.join(b.root, "eio-bash-env.sh")
        write(envfile, EIO_BASH_ENV)
        eio_py = os.path.join(b.root, "eio-python")
        write(eio_py, EIO_PY.replace("REAL_PY", PY), 0o755)
        eio = b.env(BASH_ENV=envfile, PY=eio_py)
        put_pointer(ptr, b"demo\n")
        out = read_ptr(ptr, BASH_ENV=envfile, PY=eio_py)
        check("mc_read_default while config/default-vm's lookups get an I/O error: refused", out.startswith("refused:")
              and "isn't a plain file" in out, out)
        r = b.run("--vm", "demo", "vm", "delete", "demo", "--confirm", "demo", env=eio)
        check("vm delete demo while config/default-vm's lookups get an I/O error: refused without --force, saying why, "
              "nothing deleted", r.returncode == 1 and "can't be told" in r.stderr and b.deletes() == [], r.stderr[-600:])
        r = b.run("--vm", "demo", "vm", "delete", "demo", "--confirm", "demo", env=b.env(BASH_ENV=envfile))
        check("...and when only the shell's own tests fail (the reader opens it): refused as your default computer (the "
              "shell's tests never decide)", r.returncode == 1 and "it's your default computer" in r.stderr
              and b.deletes() == [], r.stderr[-600:])
        put_pointer(ptr, None)

        # The Python side: a settings file that leaves a helper running in the background (its streams redirected,
        # every other descriptor it was given kept). The answer comes when bash exits, not when the helper does.
        def resolve(deadline):
            r = subprocess.run([PY, "-B", "-c", PY_RESOLVE, os.path.join(co, "lib"), "demo", str(deadline)],
                               env=b.env(MCOMP_NO_DEFAULT_VM="1"), capture_output=True, text=True, timeout=180)
            return r.stdout.split(None, 3) or [r.stderr[-300:]]
        stamp = f"{os.getpid()}-{int(time.time() * 1000)}"
        bg = f"mcomp-test-bg-{stamp}"
        tokens.append(bg)
        write(demo, f"SSH_PORT=2401\nVNC_PORT=6101\n( exec -a {bg} sleep 300 ) </dev/null >/dev/null 2>&1 &\n")
        out = resolve(60)
        check("a settings file that leaves a helper running: Python gets demo when bash exits (never waits for the helper)",
              out[:2] == ["OK", "demo"] and float(out[2]) < 30, out)
        check("...and the helper is still running then (it was there, and a check doesn't stop what the settings start)",
              len(helpers(bg)) >= 1, helpers(bg))
        subprocess.run(["pkill", "-9", "-f", bg], capture_output=True)
        # One that hangs: refused at the deadline, with everything it started gone (the hung one and its helper).
        hung = f"mcomp-test-hung-{stamp}"
        tokens.append(hung)
        write(demo, f"SSH_PORT=2401\nVNC_PORT=6101\n( exec -a {hung} sleep 300 ) </dev/null >/dev/null 2>&1 &\n"
                    f"( exec -a {hung} sleep 300 ) </dev/null >/dev/null 2>&1\n")
        out = resolve(3)
        check("a settings file that hangs: refused at the deadline", out[:1] == ["REFUSED"] and "took too long" in out[-1]
              and 2.5 <= float(out[1]) < 30, out)
        check("...and everything the resolution started is gone (the hung helper and the one in the background)",
              helpers_gone(hung), helpers(hung))
        # One that says too much (a value over the record's limit): refused, with its helper gone too.
        big = f"mcomp-test-big-{stamp}"
        tokens.append(big)
        write(demo, f"SSH_PORT=2401\nVNC_PORT=6101\n( exec -a {big} sleep 300 ) </dev/null >/dev/null 2>&1 &\n"
                    "BIG=\"$(head -c 1200000 /dev/zero | tr '\\0' a)\"\n")
        out = resolve(60)
        check("a settings file whose record is over the limit: refused", out[:1] == ["REFUSED"] and "said too much" in out[-1],
              out)
        check("...and its helper is gone with it", helpers_gone(big), helpers(big))
        write(demo, "SSH_PORT=2401\nVNC_PORT=6101\n")
        out = resolve(60)
        check("...and with the settings file as it was, demo again", out[:2] == ["OK", "demo"], out)
    finally:
        for t in tokens:                            # only this test's own helpers: each named with its own stamp
            subprocess.run(["pkill", "-9", "-f", t], capture_output=True)
        b.close()



# The record going over its limit only in what's left once the resolver has exited: a resolver (in place of lib.sh's)
# that leaves a helper named argv[2] running, writes 4 KiB under the limit, waits, writes 8 KiB more and exits. Once
# 4 KiB under has been read, the collector's select() first sleeps a second and says nothing is there (as when the
# bash writes its last bytes and exits between that select() and the check of its exit), so the rest is only read
# after the exit. argv[1] the lib folder; argv[3] "no-waitid" as in PY_RESOLVE. Prints "OK", or "REFUSED SECONDS why".
PY_DRAIN = r"""
import os, sys, time
import select as real_select
if sys.argv[3:4] == ["no-waitid"] and hasattr(os, "waitid"):
    del os.waitid
sys.path.insert(0, sys.argv[1])
import labconfig
under = labconfig._MAX_RECORD - 4096
labconfig._RESOLVE = ('( exec -a "%s" sleep 300 ) </dev/null >/dev/null 2>&1 &\n'
                      'exec 9>"$3"\nhead -c %d /dev/zero >&9\nsleep 0.3\nhead -c 8192 /dev/zero >&9\nexit 0\n'
                      % (sys.argv[2], under))
got = [0]
real_read = labconfig._read
def counting(fd, n=65536):
    b = real_read(fd, n)
    got[0] += len(b)
    return b
class Late:
    def __getattr__(self, k):
        return getattr(real_select, k)
    def select(self, r, w, x, t=None):
        if got[0] >= under:
            time.sleep(1.0)
            return [], [], []
        return real_select.select(r, w, x, t)
labconfig._read, labconfig.select, labconfig._DEADLINE = counting, Late(), 60
t = time.monotonic()
try:
    labconfig.resolve_default("demo")
    print("OK")
except labconfig.Refused as e:
    print("REFUSED", round(time.monotonic() - t, 1), e)
"""


def default_computer_recheck3(td):
    """The resolver is never reaped before its record is whole and its size checked, so a refusal can still kill its
    whole group: a record that goes over the limit only after the bash has exited is refused with the helper it left
    gone. And the same with a Python that has no os.waitid (its kqueue watch), which also resolves demo as before."""
    b = Box(td, "default5")
    tokens = []
    try:
        co = b.co
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.settings("config/local.env", "VM_NAME=mainvm\nBASELINE_NAME=mainvm-baseline\n")
        b.set_vms([vm("mainvm"), vm("mainvm-baseline"), vm("demo"), vm("demo-baseline")])
        stamp = f"{os.getpid()}-{int(time.time() * 1000)}"
        for how in ("", "no-waitid"):
            token = f"mcomp-test-drain-{how or 'waitid'}-{stamp}"
            tokens.append(token)
            r = subprocess.run([PY, "-B", "-c", PY_DRAIN, os.path.join(co, "lib"), token] + ([how] if how else []),
                               env=b.env(MCOMP_NO_DEFAULT_VM="1"), capture_output=True, text=True, timeout=180)
            out = r.stdout.strip().split(None, 2) or [r.stderr[-300:]]
            label = " (a Python without os.waitid: its kqueue watch)" if how else ""
            check(f"a record over the limit only once the bash has exited{label}: refused, as said too much, in what was "
                  "left after it exited", out[:1] == ["REFUSED"] and out[-1].endswith("the check of its settings said too much")
                  and "took too long" not in out[-1], out)
            check("...and the helper it left running is gone (its group killed before the bash was reaped)",
                  helpers_gone(token), helpers(token))
        r = subprocess.run([PY, "-B", "-c", PY_RESOLVE, os.path.join(co, "lib"), "demo", "60", "no-waitid"],
                           env=b.env(MCOMP_NO_DEFAULT_VM="1"), capture_output=True, text=True, timeout=180)
        out = r.stdout.split(None, 3) or [r.stderr[-300:]]
        check("a Python without os.waitid resolves demo as before", out[:2] == ["OK", "demo"], out)
        hung = f"mcomp-test-hung-kq-{stamp}"
        tokens.append(hung)
        b.settings("config/vms/demo.env", f"SSH_PORT=2401\nVNC_PORT=6101\n( exec -a {hung} sleep 300 ) </dev/null "
                                          f">/dev/null 2>&1 &\n( exec -a {hung} sleep 300 ) </dev/null >/dev/null 2>&1\n")
        r = subprocess.run([PY, "-B", "-c", PY_RESOLVE, os.path.join(co, "lib"), "demo", "3", "no-waitid"],
                           env=b.env(MCOMP_NO_DEFAULT_VM="1"), capture_output=True, text=True, timeout=180)
        out = r.stdout.split(None, 3) or [r.stderr[-300:]]
        check("...and one that hangs is refused at the deadline with everything it started gone",
              out[:1] == ["REFUSED"] and "took too long" in out[-1] and helpers_gone(hung), (out, helpers(hung)))
    finally:
        for t in tokens:                            # only this test's own helpers: each named with its own stamp
            subprocess.run(["pkill", "-9", "-f", t], capture_output=True)
        b.close()



# Who reaps the resolver, and when (lib/labconfig.py _collect), in a process of its own: argv[1] the lib folder, argv[2]
# the case, argv[3] "no-waitid" for the kqueue watch. The resolver (in place of lib.sh's) writes a good record and
# exits 7 (0 in exit-before-start), leaving a helper named argv[4]; os.killpg and os.kill with SIGKILL are recorded, and only sent in
# "sigint-close". Cases: early-reap (something else reaps it before the watch begins), lost-status (something else reaps
# it once its exit was seen), sigint-close (a SIGINT as the watch is closed after a good record), early-reap-sigint
# (reaped before the watch, and a SIGINT as the watch is first closed: by the kqueue watch's start, which found it gone),
# kq-error (the kqueue registration fails with ENOMEM), exit-before-start and exit7-before-start (it has exited, exit 0
# or 7, unreaped, before the watch starts: macOS's kqueue says no such process for it). Prints one JSON line: result
# ("OK VM_NAME", "REFUSED why" or "INTERRUPTED"), signals, whether it ended reaped, kqueues left open.
PY_OWN = r"""
import errno, json, os, select as real_select, signal, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)
real_waitid = getattr(os, "waitid", None)       # the test's own way to wait for its exit without reaping it
if sys.argv[3] == "no-waitid" and hasattr(os, "waitid"):
    del os.waitid
sys.path.insert(0, sys.argv[1])
import labconfig
case, token = sys.argv[2], sys.argv[4]
labconfig._RESOLVE = ('( exec -a "%s" sleep 300 ) </dev/null >/dev/null 2>&1 &\n'
                      "printf '%%s\\0' MCOMP-SETTINGS/1 FILE /x/demo.env VM_NAME demo END >\"$3\"\nexit %d\n"
                      % (token, 0 if case == "exit-before-start" else 7))
labconfig._DEADLINE = 60
sent, pids, kqs, held = [], [], [], []
real_killpg, real_kill = os.killpg, os.kill
def killpg(pg, sig):
    sent.append("killpg")
    if case == "sigint-close":
        real_killpg(pg, sig)
def kill(pid, sig):
    if pids and pid == pids[0] and sig == signal.SIGKILL:
        sent.append("kill")
        if case != "sigint-close":
            return
    real_kill(pid, sig)
os.killpg, os.kill = killpg, kill
Watch = labconfig._ExitWatch
class Probe(Watch):
    def __init__(self, p):
        pids.append(p.pid)
        held.append(p)                      # kept, so Popen's own cleanup never reaps it behind the check's back
        if case in ("early-reap", "early-reap-sigint"):
            os.waitpid(p.pid, 0)            # something else in this process reaps it first
        if case in ("exit-before-start", "exit7-before-start"):
            end = time.monotonic() + 20     # it has exited (a zombie, not reaped) before the watch starts
            while time.monotonic() < end and not (real_waitid and real_waitid(
                    os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)):
                time.sleep(0.05 if real_waitid else 1.0)
        Watch.__init__(self, p)
    def exited(self):
        done = Watch.exited(self)
        if done and case == "lost-status" and not getattr(self, "_reaped", False):
            self._reaped = True
            try:
                os.waitpid(self.p.pid, 0)   # ...and here, once its exit was seen
            except ChildProcessError:
                pass
        return done
    def close(self):
        if case in ("sigint-close", "early-reap-sigint") and not getattr(self, "_sent", False):
            self._sent = True
            real_kill(os.getpid(), signal.SIGINT)
        Watch.close(self)
labconfig._ExitWatch = Probe
if case == "kq-error":
    class KQ:
        def __init__(self):
            self.real = real_select.kqueue()
            kqs.append(self.real)
        def control(self, changes, n, t=None):
            if changes:
                raise OSError(errno.ENOMEM, "injected")
            return self.real.control(changes, n, t)
        def close(self):
            self.real.close()
    class Sel:
        def __getattr__(self, k):
            return getattr(real_select, k)
        kqueue = KQ
    labconfig.select = Sel()
kept = None
try:
    result = "OK " + labconfig.resolve_default("demo")[1].get("VM_NAME", "")
except labconfig.Refused as e:
    result, kept = "REFUSED " + str(e), e       # kept: the exception (and its traceback) still held
except KeyboardInterrupt:
    result = "INTERRUPTED"
try:
    reaped = os.waitpid(pids[0], os.WNOHANG) == (0, 0) and "running"
except ChildProcessError:
    reaped = True
print(json.dumps({"result": result, "sent": sent, "reaped": reaped, "kq_open": [k for k in kqs if not k.closed] != []}))
"""


def default_computer_recheck4(td):
    """Who reaps the resolver: when something else in the process reaped it (before the watch, or once its exit was
    seen) the check refuses and sends nothing; its exit status is never Popen's stand-in 0; a SIGINT as it finishes
    still kills its group and reaps it; a failed kqueue registration keeps no kqueue open; one that exited before the
    watch started is told by its own status. On both watches."""
    b = Box(td, "default6")
    tokens = []
    try:
        lib = os.path.join(b.co, "lib")
        stamp = f"{os.getpid()}-{int(time.time() * 1000)}"
        for backend in ("waitid", "no-waitid"):
            label = "waitid" if backend == "waitid" else "kqueue"
            for case in ("early-reap", "lost-status", "sigint-close", "early-reap-sigint", "kq-error",
                         "exit-before-start", "exit7-before-start"):
                if case == "kq-error" and backend == "waitid":
                    continue                    # only the kqueue watch registers anything
                token = f"mcomp-test-own-{case}-{label}-{stamp}"
                tokens.append(token)
                r = subprocess.run([PY, "-B", "-c", PY_OWN, lib, case, backend, token], env=b.env(MCOMP_NO_DEFAULT_VM="1"),
                                   capture_output=True, text=True, timeout=120)
                try:
                    d = json.loads(r.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    d = {"result": "no answer: " + r.stderr[-300:]}
                res = d.get("result", "")
                if case == "early-reap":
                    check(f"[{label}] reaped by something else before the watch began: refused, nothing sent",
                          res.startswith("REFUSED") and "was lost" in res and d.get("sent") == [], d)
                elif case == "lost-status":
                    check(f"[{label}] reaped by something else once its exit was seen (a good record, exit 7): refused, "
                          "never Popen's 0, nothing sent", res.startswith("REFUSED") and "was lost" in res
                          and d.get("sent") == [], d)
                elif case == "early-reap-sigint":
                    check(f"[{label}] reaped by something else before the watch, and a SIGINT as the watch is first "
                          "closed: the interruption goes on, nothing sent", res == "INTERRUPTED" and d.get("sent") == []
                          and d.get("reaped") is True, d)
                elif case == "exit-before-start":
                    check(f"[{label}] a resolver that has exited (exit 0, not reaped) before the watch starts: demo, by "
                          "its own status, nothing sent", res == "OK demo" and d.get("sent") == []
                          and d.get("reaped") is True, d)
                elif case == "exit7-before-start":
                    check(f"[{label}] ...one that exited 7 before the watch starts: refused by its status, not as lost, "
                          "nothing sent", res.startswith("REFUSED") and "didn't answer as it should" in res
                          and "was lost" not in res and d.get("sent") == [] and d.get("reaped") is True, d)
                elif case == "sigint-close":
                    check(f"[{label}] a SIGINT as the watch is closed: the interruption goes on, its group was killed "
                          "and it was reaped", res == "INTERRUPTED" and "killpg" in d.get("sent", [])
                          and d.get("reaped") is True and helpers_gone(token), (d, helpers(token)))
                else:
                    check(f"[{label}] a kqueue registration that fails: refused, its group killed, reaped, and no "
                          "kqueue left open while the refusal is kept", res.startswith("REFUSED") and "injected" in res
                          and "killpg" in d.get("sent", []) and d.get("reaped") is True and d.get("kq_open") is False, d)
                subprocess.run(["pkill", "-9", "-f", token], capture_output=True)
    finally:
        for t in tokens:                            # only this test's own helpers: each named with its own stamp
            subprocess.run(["pkill", "-9", "-f", t], capture_output=True)
        b.close()


def creating(td):
    b = Box(td, "create")
    try:
        b.settings("config/local.env", 'VM_NAME=metacomputer\nBROWSERS="firefox"\nVM_MEMORY=4GB\nVNC_PASSWORD_STORE=command\n'
                   'VNC_PASSWORD_SET="my-store set vnc"\nVOLUMES="work"\nACTION_LOG=on\nLUME="$HOME/.local/bin/lume"\n')
        # The runner this Mac's settings choose (standard Lume's name here): the new VM must use the same one.
        shutil.copy(os.path.join(b.home, ".local", "bin", "mcomp-vm"), os.path.join(b.home, ".local", "bin", "lume"))
        b.set_vms([vm("metacomputer"), vm("metacomputer-baseline"), vm("taken")])
        r = b.run("vm", "create", "side", "--ssh-port", "2433", "--vnc-port", "6133", "--no-build")
        f = os.path.join(b.co, "config", "vms", "side.env")
        text = open(f).read() if os.path.exists(f) else ""
        check("vm create --no-build: writes config/vms/side.env", r.returncode == 0 and text, r.stderr)
        check("...private (0600)", os.path.exists(f) and os.stat(f).st_mode & 0o777 == 0o600)
        check("...with its own name, baseline and ports, and no folder settings (fixed by its name, named in a comment)",
              all(x in text for x in ("VM_NAME=side", "BASELINE_NAME=side-baseline", "SSH_PORT=2433", "VNC_PORT=6133",
                                      "can't be set here", f"vault {b.app}/side"))
              and not any(ln.startswith(k + "=") for ln in text.splitlines()
                          for k in ("SSH_DIR", "RUN_DIR", "LOG_DIR", "VAULT_DIR", "EVENTS_SPOOL")), text)
        check("...the Mac's choices copied (browsers, memory, hardening, runner)",
              "BROWSERS='firefox'" in text and "VM_MEMORY='4GB'" in text and "ACTION_LOG='on'" in text
              and 'LUME="$HOME/.local/bin/lume"' in text, text)
        check("...never the main VM's storage or its password command",
              "VOLUMES" not in text and "VNC_PASSWORD" not in text, text)
        src = open(os.path.join(LAB, "get.sh")).read()
        fn = src[src.index("env_is_data() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        r = subprocess.run(["/bin/bash", "-c", fn + '\nenv_is_data "$1"', "_", f], capture_output=True, text=True, timeout=30)
        check("...and only values, as get.sh --confirm accepts in a checkout", r.returncode == 0, r.stderr)
        check("...and it resolves for --vm side", b.resolve("side")[1].get("VM_NAME") == "side")
        check("...and nothing was built or deleted", b.deletes() == [] and "build" not in open(b.log).read())
        r = b.run("vm", "create", "auto", "--no-build")
        auto = open(os.path.join(b.co, "config", "vms", "auto.env")).read() if r.returncode == 0 else ""
        ports = dict(ln.split("=") for ln in auto.splitlines() if ln.startswith(("SSH_PORT=", "VNC_PORT=")))
        check("without ports: a free pair is picked (live view = SSH + 3700), none another VM's",
              ports.get("SSH_PORT") and int(ports["VNC_PORT"]) == int(ports["SSH_PORT"]) + 3700
              and ports["SSH_PORT"] not in ("2277", "2433"), (ports, r.stderr))
        for args, why, words in ((["side"], "settings exist", "exists already"),
                                 (["taken"], "a VM of that name in the runner", "runner has a VM named taken"),
                                 (["metacomputer"], "the main VM's name", "already use the name"),
                                 (["Bad_Name"], "a name that isn't allowed", "a new VM's name"),
                                 (["x-baseline"], "a baseline's name", "a new VM's name"),
                                 (["dup", "--ssh-port", "2433", "--vnc-port", "6999"], "another VM's port", "already another VM's"),
                                 (["dup", "--ssh-port", "2434"], "only one port", "both --ssh-port and --vnc-port"),
                                 (["dup"], "a build with no terminal and no --yes", "needs your OK")):
            r = b.run("vm", "create", *args, *([] if args[0] == "dup" and len(args) == 1 else ["--no-build"]))
            check(f"vm create refuses: {why}", r.returncode == 1 and words in r.stderr, r.stderr)
        check("...and none of those wrote a file", sorted(os.listdir(os.path.join(b.co, "config", "vms"))) == ["auto.env", "side.env"],
              os.listdir(os.path.join(b.co, "config", "vms")))
        # The build runs with the new VM's settings: a stand-in build reports what it was given.
        write(os.path.join(b.co, "scripts", "build-vm.sh"),
              '#!/bin/bash\n. "$(dirname "$0")/lib.sh"\necho "BUILD VM_NAME=$VM_NAME RUN_DIR=$RUN_DIR CONFIG=$LOCAL_CONFIG"\n'
              'lock_fd_held 7 "$(mc_lock_dir)/$(vm_lock_name "$VM_NAME").lock" && echo "LOCK=held"\n', 0o755)
        r = b.run("vm", "create", "built", "--ssh-port", "2435", "--vnc-port", "6135", "--yes")
        check("vm create --yes: builds the new VM with its own settings",
              r.returncode == 0 and "BUILD VM_NAME=built" in r.stdout and f"RUN_DIR={os.path.realpath(b.co)}/.run-vms/built" in r.stdout
              and "config/vms/built.env" in r.stdout, r.stdout + r.stderr)
        check("...holding the new VM's lock from its checks through the build", "LOCK=held" in r.stdout, r.stdout)
        locks = os.path.join(b.root, "locks")
        os.makedirs(locks, mode=0o700, exist_ok=True)
        holder = subprocess.Popen(["/usr/bin/perl", "-e", 'use Fcntl qw(:flock); open(my $f, ">>", $ARGV[0]) or die; '
                                   'flock($f, LOCK_EX) or die; print "held\\n"; $| = 1; sleep 30', os.path.join(locks, "vm-busy.lock")],
                                  stdout=subprocess.PIPE, text=True)
        holder.stdout.readline()
        r = b.run("vm", "create", "busy", "--no-build", env=b.env(MCOMP_LOCK_WAIT="1"))
        holder.kill()
        holder.wait()
        check("vm create while that name's lock is held (a build): refused, nothing written", r.returncode == 1
              and "another build or change of the VM busy" in r.stderr and not os.path.exists(os.path.join(b.co, "config/vms/busy.env")),
              r.stderr)
    finally:
        b.close()


class Pty:
    def __init__(self, argv, env):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        self.buf = b""

    def expect(self, text, timeout=60):
        end = time.time() + timeout
        while text.encode() not in self.buf and time.time() < end:
            if select.select([self.fd], [], [], 0.2)[0]:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                self.buf += data
        return text.encode() in self.buf

    def send(self, s):
        os.write(self.fd, s.encode())

    def wait(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if select.select([self.fd], [], [], 0.2)[0]:
                try:
                    data = os.read(self.fd, 65536)
                    self.buf += data
                except OSError:
                    pass
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                os.close(self.fd)
                return os.waitstatus_to_exitcode(st)
        os.kill(self.pid, 9)
        os.waitpid(self.pid, 0)
        os.close(self.fd)
        return None


def deleting(td):
    b = Box(td, "delete")
    try:
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        b.settings("config/vms/demo.env", 'SSH_PORT=2401\nVNC_PORT=6101\nVOLUMES="work,cache:/mnt/c"\n')
        b.settings("config/vms/runs.env", "SSH_PORT=2402\nVNC_PORT=6102\n")
        b.settings("config/vms/ckpt.env", "SSH_PORT=2403\nVNC_PORT=6103\n")
        b.settings("config/vms/shared.env", 'SSH_PORT=2404\nVNC_PORT=6104\nVOLUMES="work"\n')
        others = [vm("metacomputer"), vm("metacomputer-baseline"), vm("showcase", "running"), vm("showcase-baseline")]
        b.set_vms(others + [vm("demo"), vm("demo-baseline"), vm("demo-baseline-prev"), vm("runs", "running"), vm("runs-baseline"),
                            vm("ckpt"), vm("ckpt-ckpt-a"), vm("shared")])
        vault = os.path.join(b.app, "demo")
        os.makedirs(vault)
        write(os.path.join(vault, "vault.json"), "{}")
        rundir = os.path.join(b.co, ".run-vms", "demo")
        os.makedirs(rundir)

        def gone():
            return b.deletes()

        r = b.run("vm", "delete", "demo", "--yes")
        check("vm delete with no terminal and only --yes: refused (--yes doesn't count)", r.returncode == 1
              and "--confirm demo" in r.stderr and "--yes doesn't count" in r.stderr and gone() == [], r.stderr)
        r = b.run("vm", "delete", "demo", "--confirm", "demo2")
        check("--confirm with another name: refused", r.returncode == 1 and "not demo" in r.stderr and gone() == [], r.stderr)
        r = b.run("vm", "delete", "metacomputer", "--confirm", "metacomputer")
        check("the VM this checkout uses (config/local.env): refused without --force", r.returncode == 1
              and "uses without --vm" in r.stderr and gone() == [], r.stderr)
        r = b.run("vm", "delete", "runs", "--confirm", "runs")
        check("a running VM: refused without --force", r.returncode == 1 and "it's running" in r.stderr and gone() == [], r.stderr)
        r = b.run("vm", "delete", "ckpt", "--confirm", "ckpt")
        check("a VM with checkpoints: refused, naming them", r.returncode == 1 and "checkpoints (a)" in r.stderr and gone() == [], r.stderr)
        r = b.run("vm", "delete", "showcase", "--confirm", "showcase")
        check("a VM this checkout has no settings for: refused (it doesn't know its baseline)", r.returncode == 1
              and "no settings for a VM named showcase" in r.stderr and gone() == [], r.stderr)
        legacy = os.path.join(b.root, "legacy-del.env")
        write(legacy, f'VM_NAME=legacy\nBASELINE_NAME=legacy-baseline\nSSH_PORT=2391\nVNC_PORT=6091\nSSH_DIR="{b.root}/lg-ssh"\n'
                      f'RUN_DIR="{b.root}/lg-run"\nLOG_DIR="{b.root}/lg-logs"\nVAULT_DIR="/tmp/mc-shared-vault-test"\n')
        b.set_vms(json.load(open(b.state)) + [vm("legacy")])
        r = b.run("vm", "delete", "legacy", "--confirm", "legacy", "--delete-vault", env=b.env(MCOMP_CONFIG=legacy))
        check("--delete-vault for a vault that isn't the VM's own folder (an MCOMP_CONFIG VM's): refused before anything",
              r.returncode == 1 and "only a VM's own vault folder" in r.stderr and gone() == [], r.stderr)
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-volumes")
        check("--delete-volumes when another VM attaches the same volume: refused before anything", r.returncode == 1
              and "shared: work" in r.stderr and gone() == [], r.stderr)
        # The lock: while a build or change of the VM holds it, nothing is deleted.
        locks = os.path.join(b.root, "locks")
        os.makedirs(locks, mode=0o700, exist_ok=True)
        holder = subprocess.Popen(["/usr/bin/perl", "-e", 'use Fcntl qw(:flock); open(my $f, ">>", $ARGV[0]) or die; '
                                   'flock($f, LOCK_EX) or die; print "held\\n"; $| = 1; sleep 30', os.path.join(locks, "vm-demo.lock")],
                                  stdout=subprocess.PIPE, text=True)
        holder.stdout.readline()
        r = b.run("vm", "delete", "demo", "--confirm", "demo", env=b.env(MCOMP_LOCK_WAIT="1"))
        holder.kill()
        holder.wait()
        check("while the VM's lock is held (a build): refused", r.returncode == 1 and "another build or change of the VM demo" in r.stderr
              and gone() == [], r.stderr)

        # Typed at a terminal: a wrong name stops it; the right one deletes the VM, its baseline and its settings.
        p = Pty([b.mc, "vm", "delete", "demo"], b.env())
        p.expect("Type the name of the VM")
        p.send("dmeo\n")
        rc = p.wait()
        check("at a terminal, a mistyped name: nothing deleted", rc == 1 and gone() == [], (rc, p.buf[-300:]))
        p = Pty([b.mc, "vm", "delete", "demo"], b.env())
        shown = p.expect("Type the name of the VM")
        p.send("demo\n")
        rc = p.wait()
        out = p.buf.decode(errors="replace")
        check("at a terminal, the name typed: deleted", rc == 0 and sorted(gone()) == ["demo", "demo-baseline", "demo-baseline-prev"],
              (rc, gone(), out[-400:]))
        check("...after saying which VM and what it deletes and keeps", shown and "VM demo (config/vms/demo.env)" in out
              and "its baseline demo-baseline" in out and "It keeps:" in out and "the vault folder" in out, out[-600:])
        check("...its settings file and runtime folder are gone", not os.path.exists(os.path.join(b.co, "config/vms/demo.env"))
              and not os.path.exists(rundir))
        check("...its vault folder is kept (no --delete-vault)", os.path.exists(os.path.join(vault, "vault.json")))
        check("...and every other VM is still there", all(n in b.vms() for n in ("metacomputer", "metacomputer-baseline", "showcase",
                                                                              "showcase-baseline", "runs", "ckpt", "shared")), b.vms())

        # --delete-vault: the VM's own folder, nothing else; --force: a running VM, stopped first.
        b.set_vms(b and json.load(open(b.state)))
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("settings only (never built) with --delete-vault: the file and the VM's own vault folder go",
              r.returncode == 0 and not os.path.exists(vault) and os.path.isdir(b.app)
              and not os.path.exists(os.path.join(b.co, "config/vms/demo.env")) and gone() == [], r.stderr)
        r = b.run("vm", "delete", "runs", "--confirm", "runs", "--force")
        log = open(b.log).read()
        check("--force on a running VM: stopped, then deleted with its baseline", r.returncode == 0
              and "stop runs --force" in log and sorted(gone()) == ["runs", "runs-baseline"], (r.stderr[-300:], gone()))
        r = b.run("vm", "delete", "metacomputer", "--confirm", "metacomputer", "--force")
        check("--force on the main VM: the VM and baseline go, config/local.env stays", r.returncode == 0
              and "metacomputer" in gone() and os.path.exists(os.path.join(b.co, "config/local.env")), (r.stderr[-300:], gone()))
        check("...and nothing named outside what was asked was ever deleted",
              set(gone()) <= {"runs", "runs-baseline", "metacomputer", "metacomputer-baseline"} and "showcase" in b.vms(), gone())
        r = b.run("vm", "delete", "nothing-here", "--confirm", "nothing-here")
        check("a name with nothing at all: refused", r.returncode == 1, r.stderr)
    finally:
        b.close()


def hold_lock(path):
    """A process holding an exclusive flock on path (as a build, a delete or a session would); kill it to release."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    p = subprocess.Popen(["/usr/bin/perl", "-e", 'use Fcntl qw(:flock); open(my $f, ">>", $ARGV[0]) or die; '
                          'flock($f, LOCK_EX) or die; print "held\\n"; $| = 1; sleep 60', path], stdout=subprocess.PIPE, text=True)
    p.stdout.readline()
    return p


def release(p):
    p.kill()
    p.wait()


def link(path, target):
    """path as a symbolic link to target (its folder made; whatever was at path removed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    os.symlink(target, path)


def drop(path):
    """Remove a file the test made, if it's still there (the code before a fix may have deleted it already)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def audit_fixes(td):
    """The independent audit's findings on the first version, each one failing before its fix."""
    b = Box(td, "audit")
    try:
        co, home, root = b.co, b.home, b.root
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        main = [vm("metacomputer"), vm("metacomputer-baseline"), vm("metacomputer-baseline-prev"), vm("metacomputer-ckpt-keep")]

        # 1. Deleting one VM never deletes another's VM, baseline or copies, whatever the settings name.
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\nBASELINE_NAME=metacomputer\n")
        b.set_vms(main + [vm("demo")])
        rc, s, err = b.resolve("demo")
        check("a config/vms/ file whose baseline is the main VM: refused", rc == 1 and "BASELINE_NAME must be demo-baseline" in err, err)
        r = b.run("vm", "delete", "demo", "--confirm", "demo")
        check("...and vm delete of it deletes nothing (the main VM stays)", r.returncode == 1 and b.deletes() == []
              and "metacomputer" in b.vms(), (r.stderr[-300:], b.deletes()))
        legacy = os.path.join(root, "legacy.env")
        write(legacy, "VM_NAME=legacy\nBASELINE_NAME=metacomputer\nSSH_PORT=2391\nVNC_PORT=6091\n")
        b.set_vms(main + [vm("legacy")])
        r = b.run("vm", "delete", "legacy", "--confirm", "legacy", env=b.env(MCOMP_CONFIG=legacy))
        check("an MCOMP_CONFIG file whose baseline is the main VM: vm delete refuses, deletes nothing", r.returncode == 1
              and "the name metacomputer is the main VM (config/local.env)'s" in r.stderr and b.deletes() == []
              and "metacomputer" in b.vms(), (r.stderr[-300:], b.deletes()))
        write(legacy, "VM_NAME=legacy\nBASELINE_NAME=legacy-baseline\nSSH_PORT=2391\nVNC_PORT=6091\n"
                     f'SSH_DIR="{root}/legacy-ssh"\nRUN_DIR="{root}/legacy-run"\nLOG_DIR="{root}/legacy-logs"\n')
        b.settings("config/vms/legacy-baseline-prev.env", "SSH_PORT=2392\nVNC_PORT=6092\n")
        b.set_vms(main + [vm("legacy"), vm("legacy-baseline"), vm("legacy-baseline-prev")])
        r = b.run("vm", "delete", "legacy", "--confirm", "legacy", env=b.env(MCOMP_CONFIG=legacy))
        check("...or whose previous baseline is a VM another settings file names: refused, nothing deleted",
              r.returncode == 1 and "legacy-baseline-prev is config/vms/legacy-baseline-prev.env's" in r.stderr and b.deletes() == [],
              (r.stderr[-300:], b.deletes()))
        drop(os.path.join(co, "config/vms/legacy-baseline-prev.env"))
        b.settings("config/vms/metacomputer-baseline.env", "SSH_PORT=2393\nVNC_PORT=6093\n")
        rc, s, err = b.resolve("metacomputer-baseline")
        check("a config/vms/ file named like another VM's baseline: refused", rc == 1 and "can't end in -baseline" in err, err)
        r = b.run("vm", "delete", "metacomputer-baseline", "--confirm", "metacomputer-baseline")
        check("...and vm delete of it deletes nothing", r.returncode == 1 and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        for f in ("demo", "metacomputer-baseline"):
            drop(os.path.join(co, f"config/vms/{f}.env"))

        # 2. A per-VM VM's folders are fixed by its name, and compared as the folders they physically are: one that
        # is a link to the main VM's folder (or another VM's), however spelled, is refused.
        os.makedirs(os.path.join(home, ".ssh", "metacomputer"), exist_ok=True)
        os.makedirs(os.path.join(co, ".run"), exist_ok=True)
        os.symlink(os.path.join(co, ".run"), os.path.join(root, "runlink"))
        for n, path, target, why in (
                ("al1", os.path.join(home, ".ssh", "metacomputer-al1"), os.path.join(home, ".ssh", "metacomputer") + "/.",
                 "SSH_DIR, a link to the main SSH_DIR spelled with /."),
                ("al2", os.path.join(home, ".ssh", "metacomputer-al2"), os.path.join(home, ".ssh") + "//metacomputer",
                 "SSH_DIR, a link spelled with //"),
                ("al3", os.path.join(co, ".run-vms", "al3"), os.path.join(root, "runlink"), "RUN_DIR, a link to a link"),
                ("al4", os.path.join(co, "logs-vms", "al4"), os.path.join(co, ".run"), "LOG_DIR, a link to the main RUN_DIR")):
            b.settings(f"config/vms/{n}.env", f"SSH_PORT=24{n[-1]}0\nVNC_PORT=61{n[-1]}0\n")
            link(path, target)
            rc, s, err = b.resolve(n)
            check(f"a per-VM {why} (the main VM's folder): refused", rc == 1 and "is the same folder as the main VM" in err, err)
            drop(os.path.join(co, f"config/vms/{n}.env"))
            os.remove(path)
        os.makedirs(os.path.join(co, "logs-vms", "pa"))
        b.settings("config/vms/pa.env", 'SSH_PORT=2451\nVNC_PORT=6151\n')
        b.settings("config/vms/pb.env", 'SSH_PORT=2452\nVNC_PORT=6152\n')
        link(os.path.join(co, "logs-vms", "pb"), os.path.join(co, "logs-vms", "pa"))
        rc, s, err = b.resolve("pb")
        check("two per-VM files whose folders are one through a link: refused", rc == 1
              and "is the same folder as config/vms/pa.env's LOG_DIR" in err, err)
        for n in ("pa", "pb"):
            drop(os.path.join(co, f"config/vms/{n}.env"))
        os.remove(os.path.join(co, "logs-vms", "pb"))

        # 3. Recursive deletion goes through no link and never takes another VM's folder.
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        elsewhere = os.path.join(root, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "demo"))
        write(os.path.join(elsewhere, "demo", "keep.txt"), "not demo's")
        shutil.rmtree(os.path.join(co, ".run-vms"), ignore_errors=True)   # runtime folders earlier steps made
        os.symlink(elsewhere, os.path.join(co, ".run-vms"))
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        r = b.run("vm", "delete", "demo", "--confirm", "demo")
        check("a runtime folder reached through a linked .run-vms: refused before anything is deleted", r.returncode == 1
              and "is a link" in r.stderr and b.deletes() == [] and os.path.exists(os.path.join(elsewhere, "demo", "keep.txt")),
              (r.stderr[-300:], b.deletes()))
        os.remove(os.path.join(co, ".run-vms"))
        os.makedirs(os.path.join(co, ".run-vms", "demo"))
        appvms = b.app
        os.makedirs(appvms, exist_ok=True)
        os.makedirs(os.path.join(root, "elsewhere2", "demo"))
        write(os.path.join(root, "elsewhere2", "demo", "vault.json"), "{}")
        os.symlink(os.path.join(root, "elsewhere2", "demo"), os.path.join(appvms, "demo"))
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("--delete-vault when the vault folder is a link: refused before anything is deleted", r.returncode == 1
              and b.deletes() == [] and os.path.exists(os.path.join(root, "elsewhere2", "demo", "vault.json")), (r.stderr[-300:], b.deletes()))
        os.remove(os.path.join(appvms, "demo"))
        os.makedirs(os.path.join(appvms, "demo", "inner"))
        b.settings("config/vms/nested.env", 'SSH_PORT=2461\nVNC_PORT=6161\n')
        link(os.path.join(appvms, "nested"), os.path.join(appvms, "demo", "inner"))     # its vault, physically in demo's
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("--delete-vault when another VM's vault is inside demo's: refused, both kept", r.returncode == 1
              and "holds config/vms/nested.env's VAULT_DIR" in r.stderr and b.deletes() == []
              and os.path.isdir(os.path.join(appvms, "demo", "inner")), (r.stderr[-300:], b.deletes()))
        link(os.path.join(appvms, "nested"), os.path.join(appvms, "demo"))
        rc, s, err = b.resolve("demo")
        check("another VM whose vault is (a link to) demo's own folder: demo refused (and so its delete)", rc == 1
              and "is the same folder as config/vms/nested.env's VAULT_DIR" in err, err)
        drop(os.path.join(co, "config/vms/nested.env"))
        os.remove(os.path.join(appvms, "nested"))

        # P2: vault folders as the folders they are; the environment's absolute too.
        os.makedirs(os.path.join(root, "mainvault"))
        b.settings("config/local.env", f'VM_NAME=metacomputer\nVAULT_DIR="{root}/mainvault"\n')
        os.symlink(os.path.join(root, "mainvault"), os.path.join(root, "vaultlink"))
        b.settings("config/vms/sym.env", 'SSH_PORT=2471\nVNC_PORT=6171\n')
        link(os.path.join(b.app, "sym"), os.path.join(root, "vaultlink"))
        r = b.run("--vm", "sym", "secrets", "lock")
        check("a per-VM vault that is a link to the main VM's: refused (no client reaches the main vault)", r.returncode == 1
              and "VAULT_DIR" in r.stderr and "VAULT " not in r.stdout, (r.stdout, r.stderr[-300:]))
        drop(os.path.join(co, "config/vms/sym.env"))
        os.remove(os.path.join(b.app, "sym"))
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        r = b.run("--vm", "demo", "secrets", "status", env=b.env(MCOMP_VAULT_DIR="relative/vault"))
        check("MCOMP_VAULT_DIR that isn't absolute: refused", r.returncode == 1 and "must be an absolute folder" in r.stderr
              and "VAULT " not in r.stdout, r.stderr[-300:])
        os.makedirs(os.path.join(root, "realvault"))
        os.symlink(os.path.join(root, "realvault"), os.path.join(root, "envlink"))
        r = b.run("secrets", "status", env=b.env(MCOMP_VAULT_DIR=os.path.join(root, "envlink")))
        check("MCOMP_VAULT_DIR through a link: the vault gets the folder it is", f"dir={os.path.realpath(root)}/realvault " in r.stdout,
              r.stdout + r.stderr[-200:])

        # P2: nothing can change between the checks and the deletion.
        locks = os.path.join(root, "locks")
        h = hold_lock(os.path.join(locks, "vm-demo.lock"))
        r = b.run("--vm", "demo", "start", env=b.env(MCOMP_LOCK_WAIT="1"))
        release(h)
        check("start takes the VM's lock: refused while a build or delete holds it", r.returncode == 1
              and "another build or change of the VM demo" in r.stderr, r.stderr[-300:])
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        for change, what in ((lambda: b.set_vms(main + [vm("demo", "running"), vm("demo-baseline")]), "it started"),
                             (lambda: b.set_vms(main + [vm("demo"), vm("demo-baseline"), vm("demo-ckpt-new")]), "a checkpoint appeared")):
            b.set_vms(main + [vm("demo"), vm("demo-baseline")])
            p = Pty([b.mc, "vm", "delete", "demo"], b.env())
            asked = p.expect("Type the name of the VM")
            change()
            if asked:
                p.send("demo\n")
            rc = p.wait()
            check(f"what changed while the name was typed ({what}): refused, nothing deleted", asked and rc == 1
                  and b.deletes() == [] and "demo" in b.vms(), (rc, p.buf[-300:]))
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        h = hold_lock(os.path.join(co, ".run-vms", "demo", "owners", ".lock"))
        r = b.run("vm", "delete", "demo", "--confirm", "demo", env=b.env(MCOMP_LOCK_WAIT="1"))
        release(h)
        check("vm delete takes the VM's owners lock: refused while a session holds it, nothing deleted", r.returncode == 1
              and "owners lock" in r.stderr and b.deletes() == [], (r.stderr[-300:], b.deletes()))

        # P2: two creates never pick the same ports; one waits for the other.
        h = hold_lock(os.path.join(locks, "vms-create.lock"))
        r = b.run("vm", "create", "waits", "--no-build", env=b.env(MCOMP_LOCK_WAIT="1"))
        release(h)
        check("vm create while another is choosing ports: refused, nothing written", r.returncode == 1
              and "choosing ports" in r.stderr and not os.path.exists(os.path.join(co, "config/vms/waits.env")), r.stderr[-300:])
        procs = [subprocess.Popen([b.mc, "vm", "create", n, "--no-build"], env=b.env(MCOMP_LOCK_WAIT="30"), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in ("race1", "race2")]
        outs = [p.communicate(timeout=120) for p in procs]
        ports = []
        for n in ("race1", "race2"):
            f = os.path.join(co, "config", "vms", n + ".env")
            ports.append(open(f).read() if os.path.exists(f) else "")
        pick = [sorted(ln for ln in t.splitlines() if ln.startswith(("SSH_PORT=", "VNC_PORT="))) for t in ports]
        check("two vm creates at once: both written, with different ports", all(p.returncode == 0 for p in procs)
              and pick[0] and pick[1] and not set(pick[0]) & set(pick[1]), (pick, [o[1][-200:] for o in outs]))
    finally:
        b.close()


def recheck_fixes(td):
    """The re-check's findings (on 3998be6), each failing before its fix."""
    b = Box(td, "recheck")
    try:
        co, root = b.co, b.root
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        main = [vm("metacomputer"), vm("metacomputer-baseline")]
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        inner = os.path.join(b.app, "demo", "inner")
        os.makedirs(inner)
        write(os.path.join(inner, "vault.json"), "{}")
        # P1: a settings file refused for an unrelated reason still protects its folders (and names) from deletion.
        b.settings("config/vms/other.env", 'SSH_PORT=2402\nVNC_PORT=not-a-port\n')
        link(os.path.join(b.app, "other"), inner)          # its (fixed) vault folder, physically inside demo's
        rc, s, err = b.resolve("other")
        check("(setup) the other VM's settings are refused, for its port", rc == 1 and "VNC_PORT" in err, err)
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("vm delete --delete-vault when a refused file's vault is inside demo's: refused, both kept", r.returncode == 1
              and "holds config/vms/other.env's VAULT_DIR" in r.stderr and os.path.exists(os.path.join(inner, "vault.json")) and b.deletes() == [],
              (r.stderr[-300:], b.deletes()))
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        os.makedirs(inner, exist_ok=True)
        write(os.path.join(inner, "vault.json"), "{}")
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        link(os.path.join(b.app, "other"), os.path.join(b.app, "other"))      # a loop: where it is can't be known
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("...and when a refused file's folder can't be placed (a loop of links): refused, nothing deleted", r.returncode == 1
              and "aren't known" in r.stderr and os.path.exists(os.path.join(inner, "vault.json")) and b.deletes() == [],
              (r.stderr[-300:], b.deletes()))
        drop(os.path.join(co, "config/vms/other.env"))
        os.remove(os.path.join(b.app, "other"))
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        b.settings("config/vms/zz.env", "VM_NAME=zz\nBASELINE_NAME=demo-baseline\nSSH_PORT=2403\nVNC_PORT=6103\n")
        r = b.run("vm", "delete", "demo", "--confirm", "demo")
        check("a refused file that names demo-baseline as its own: vm delete demo refuses, nothing deleted", r.returncode == 1
              and "demo-baseline is config/vms/zz.env's" in r.stderr and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        drop(os.path.join(co, "config/vms/zz.env"))
        shutil.rmtree(inner, ignore_errors=True)

        # P2: MCOMP_VAULT_DIR goes through the identity check like the file's own folder.
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        os.makedirs(os.path.join(root, "mainvault"))
        b.settings("config/local.env", f'VM_NAME=metacomputer\nVAULT_DIR="{root}/mainvault"\n')
        r = b.run("--vm", "demo", "secrets", "lock", env=b.env(MCOMP_VAULT_DIR=os.path.join(root, "mainvault")))
        check("MCOMP_VAULT_DIR naming the main VM's vault for --vm demo: refused (no client reaches it)", r.returncode == 1
              and "isn't the vault folder of config/vms/demo.env" in r.stderr and "VAULT " not in r.stdout, (r.stdout, r.stderr[-300:]))
        b.settings("config/local.env", "VM_NAME=metacomputer\n")

        # P2: a delete refused after the prompt leaves what runs for the VM running.
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        rundir = os.path.join(co, ".run-vms", "demo")
        os.makedirs(rundir, exist_ok=True)
        watcher = subprocess.Popen([PY, "-c", "import time; time.sleep(120)", "events.py", "watch"])
        try:
            write(os.path.join(rundir, "events-watcher.pid"), f"{watcher.pid}\n")
            b.set_vms(main + [vm("demo"), vm("demo-baseline")])
            p = Pty([b.mc, "vm", "delete", "demo"], b.env())
            asked = p.expect("Type the name of the VM")
            b.set_vms(main + [vm("demo", "running"), vm("demo-baseline")])
            if asked:
                p.send("demo\n")
            rc = p.wait()
            check("a delete refused after the prompt (the VM started): its event watcher keeps running", asked and rc == 1
                  and watcher.poll() is None and b.deletes() == [], (rc, watcher.poll(), p.buf[-300:]))
        finally:
            watcher.kill()
            watcher.wait()
    finally:
        b.close()


def recheck2_fixes(td):
    """The second re-check's findings (on d25fd6c), each failing before its fix."""
    b = Box(td, "recheck2")
    try:
        co, root = b.co, b.root
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        main = [vm("metacomputer"), vm("metacomputer-baseline")]
        b.settings("config/vms/demo.env", 'SSH_PORT=2401\nVNC_PORT=6101\nVOLUMES="work"\n')
        inner = os.path.join(b.app, "demo", "inner")

        def fresh():
            os.makedirs(inner, exist_ok=True)
            write(os.path.join(inner, "vault.json"), "{}")
            b.settings("config/vms/demo.env", 'SSH_PORT=2401\nVNC_PORT=6101\nVOLUMES="work"\n')
            b.set_vms(main + [vm("demo"), vm("demo-baseline")])

        # P1: an unreadable settings file is never read as the defaults: its folders are unknown, so nothing that
        # might be one of them is deleted (and the VMs beside it can't be checked against it).
        fresh()
        other = b.settings("config/vms/other.env", f'SSH_PORT=2402\nVNC_PORT=6102\nVAULT_DIR="{inner}"\n')
        os.chmod(other, 0)
        try:
            r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
            check("vm delete --delete-vault beside an unreadable settings file (whose vault is inside demo's): refused, kept",
                  r.returncode == 1 and "can't be read" in r.stderr and os.path.exists(os.path.join(inner, "vault.json"))
                  and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        finally:
            os.chmod(other, 0o600)
        drop(other)
        fresh()
        helper = os.path.join(root, "helper.env")
        write(helper, f'VAULT_DIR="{inner}"\n')
        os.chmod(helper, 0)
        b.settings("config/vms/other.env", f'. "{helper}"\nSSH_PORT=2402\nVNC_PORT=6102\n')
        try:
            r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
            check("...or beside one that reads a file it can't (its folders unknown): refused, kept",
                  r.returncode == 1 and "can't be read" in r.stderr and os.path.exists(os.path.join(inner, "vault.json"))
                  and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        finally:
            os.chmod(helper, 0o600)
        drop(os.path.join(co, "config/vms/other.env"))
        mainf = os.path.join(co, "config", "local.env")
        os.chmod(mainf, 0)
        try:
            r = b.run("checkpoints")
            check("an unreadable config/local.env: refused, never the defaults' VM", r.returncode == 1
                  and "can't read the settings file" in r.stderr, r.stderr[-300:])
        finally:
            os.chmod(mainf, 0o600)

        # P2: a refused settings file's volumes still count for --delete-volumes.
        fresh()
        b.settings("config/vms/other.env", 'SSH_PORT=2402\nVNC_PORT=not-a-port\nVOLUMES="work:/mnt/w"\n')
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-volumes")
        check("--delete-volumes when a refused settings file attaches the same volume: refused, nothing deleted",
              r.returncode == 1 and "attach some of the same volumes" in r.stderr and "work" in r.stderr and b.deletes() == [],
              (r.stderr[-300:], b.deletes()))
        drop(os.path.join(co, "config/vms/other.env"))
    finally:
        b.close()


def recheck3_fixes(td):
    """The third re-check's finding (on 83bfcac), failing before its fix: a readable settings file that reads one it
    can't loaded as if fine, and deleting the main VM trusted its guessed folders."""
    b = Box(td, "recheck3")
    try:
        co, root = b.co, b.root
        mainvault = os.path.join(b.app, "main-vault")
        inner = os.path.join(mainvault, "inner")
        os.makedirs(inner)
        write(os.path.join(inner, "vault.json"), "{}")
        b.settings("config/local.env", f'VM_NAME=metacomputer\nVAULT_DIR="{mainvault}"\n')
        helper = os.path.join(root, "helper.env")
        write(helper, f'VAULT_DIR="{inner}"\n')
        os.chmod(helper, 0)
        b.settings("config/vms/other.env", f'. "{helper}"\nSSH_PORT=2402\nVNC_PORT=6102\n')
        b.set_vms([vm("metacomputer"), vm("metacomputer-baseline"), vm("other")])
        try:
            rc, s, err = b.resolve("other")
            check("a readable settings file that reads one it can't: refused when it's loaded (never its guessed folders)",
                  rc == 1 and "went wrong" in err, err[-300:])
            r = b.run("vm", "list", "--json")
            rows = {v.get("settings"): v for v in json.loads(r.stdout or "{}").get("vms", [])}
            check("...and vm list shows it refused", bool(rows.get("config/vms/other.env", {}).get("error")), rows.get("config/vms/other.env"))
            r = b.run("vm", "delete", "metacomputer", "--confirm", "metacomputer", "--force", "--delete-vault")
            check("deleting the main VM --delete-vault beside it: refused, the other VM's vault (inside) kept, nothing deleted",
                  r.returncode == 1 and "can't be read cleanly" in r.stderr and os.path.exists(os.path.join(inner, "vault.json"))
                  and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        finally:
            os.chmod(helper, 0o600)
        b.settings("config/vms/other.env", 'SSH_PORT=2402\nVNC_PORT=6102\n')
        link(os.path.join(b.app, "other"), inner)          # its vault, physically inside the main one
        r = b.run("vm", "delete", "metacomputer", "--confirm", "metacomputer", "--force")
        check("...once that file reads cleanly (its vault is inside the main one): refused as holding another VM's folder",
              r.returncode == 1 and "holds config/vms/other.env's VAULT_DIR" in r.stderr and os.path.exists(os.path.join(inner, "vault.json")) and b.deletes() == [],
              (r.stderr[-300:], b.deletes()))
    finally:
        b.close()


def final2_fixes(td):
    """The final check's findings (on 351795f), each failing before its fix: deleting the main VM (or an MCOMP_CONFIG
    one) without --delete-vault/--delete-volumes trusted a settings file it couldn't read, and the runner's delete took
    what was in that VM's folder; build-vm.sh deleted its seed folder without looking at any other VM. And the rule
    that closes them: no VM's folder is in another's, holds one, or is in the runner's VM storage."""
    b = Box(td, "final2")
    try:
        co, root, store = b.co, b.root, b.storage
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        main = [vm("metacomputer"), vm("metacomputer-baseline")]
        caught = os.path.join(store, "metacomputer", "other-vault")      # in the folder the runner deletes with the VM

        def fresh(*more):
            os.makedirs(caught, exist_ok=True)
            write(os.path.join(caught, "vault.json"), "{}")
            b.set_vms(main + list(more))

        # 1. Deleting the main VM, no flags, beside a settings file that can't be read (whose vault, unknown to it, is
        # in the main VM's folder in the runner's storage): refused before anything is deleted.
        fresh()
        other = b.settings("config/vms/other.env", f'SSH_PORT=2402\nVNC_PORT=6102\nVAULT_DIR="{caught}"\n')
        os.chmod(other, 0)
        try:
            r = b.run("vm", "delete", "metacomputer", "--confirm", "metacomputer", "--force")
            check("deleting the main VM without flags beside an unreadable settings file: refused, nothing deleted",
                  r.returncode == 1 and "can't be read cleanly" in r.stderr and b.deletes() == []
                  and os.path.exists(os.path.join(caught, "vault.json")), (r.stderr[-300:], b.deletes()))
            legacy = os.path.join(root, "legacy.env")
            write(legacy, f'VM_NAME=legacy\nBASELINE_NAME=legacy-baseline\nSSH_PORT=2391\nVNC_PORT=6091\nSSH_DIR="{root}/legacy-ssh"\n'
                          f'RUN_DIR="{root}/legacy-run"\nLOG_DIR="{root}/legacy-logs"\n')
            fresh(vm("legacy"), vm("legacy-baseline"))
            r = b.run("vm", "delete", "legacy", "--confirm", "legacy", env=b.env(MCOMP_CONFIG=legacy))
            check("...and an MCOMP_CONFIG VM's, without flags: refused too, nothing deleted", r.returncode == 1
                  and "can't be read cleanly" in r.stderr and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        finally:
            os.chmod(other, 0o600)
        r = subprocess.run([PY, os.path.join(co, "lib", "vms.py"), "delete-check", "--target", "metacomputer",
                            "--baseline", "metacomputer-baseline"], capture_output=True, text=True, timeout=30,
                           input=json.dumps({"file": "config/vms/other.env", "ok": False, "known": False, "error": "unreadable"}) + "\n")
        check("vms.py delete-check: a settings row whose names can't be read refuses (never its file name alone)",
              r.returncode == 1 and "can't be read" in r.stdout, (r.returncode, r.stdout))

        # 2. The same VM, readable: a vault in the runner's VM storage (a link there) is refused when the file is loaded,
        # and so is deleting the VM whose folder holds it.
        fresh()
        b.settings("config/vms/other.env", 'SSH_PORT=2402\nVNC_PORT=6102\n')
        link(os.path.join(b.app, "other"), caught)
        rc, s, err = b.resolve("other")
        check("a VAULT_DIR in the runner's VM storage: refused when loaded", rc == 1
              and "is inside the runner's VM storage" in err, err[-300:])
        r = b.run("vm", "delete", "metacomputer", "--confirm", "metacomputer", "--force")
        check("...and deleting the main VM (whose folder there holds it), no flags: refused, nothing deleted",
              r.returncode == 1 and "config/vms/other.env's VAULT_DIR" in r.stderr and "runner's VM storage" in r.stderr
              and b.deletes() == [] and os.path.exists(os.path.join(caught, "vault.json")), (r.stderr[-300:], b.deletes()))
        drop(other)
        os.remove(os.path.join(b.app, "other"))

        # 3. No VM's folder is inside another's, or holds one, whatever spells it (links to anywhere in them).
        hs = os.path.join(b.home, ".ssh")
        for n, path, target, want in (
                ("in1", os.path.join(co, ".run-vms", "in1"), os.path.join(co, ".run", "inside"),
                 "is inside the main VM (config/local.env)'s RUN_DIR"),
                ("in2", os.path.join(co, "logs-vms", "in2"), os.path.join(co, "logs", "x"),
                 "is inside the main VM (config/local.env)'s LOG_DIR"),
                ("in3", os.path.join(hs, "metacomputer-in3"), hs, "holds the main VM (config/local.env)'s SSH_DIR"),
                ("in4", os.path.join(hs, "metacomputer-in4"), os.path.join(hs, "metacomputer", "inner"),
                 "is inside the main VM (config/local.env)'s SSH_DIR")):
            b.settings(f"config/vms/{n}.env", f"SSH_PORT=24{n[-1]}5\nVNC_PORT=61{n[-1]}5\n")
            link(path, target)
            rc, s, err = b.resolve(n)
            check(f"a per-VM folder in or around the main VM's ({n}): refused", rc == 1 and want in err, err[-300:])
            drop(os.path.join(co, f"config/vms/{n}.env"))
            os.remove(path)
        os.makedirs(os.path.join(b.app, "pa"), exist_ok=True)
        b.settings("config/vms/pa.env", 'SSH_PORT=2451\nVNC_PORT=6151\n')
        b.settings("config/vms/pb.env", 'SSH_PORT=2452\nVNC_PORT=6152\n')
        link(os.path.join(b.app, "pb"), os.path.join(b.app, "pa", "inner"))
        rc, s, err = b.resolve("pb")
        check("a vault inside another VM's vault, through a link: refused", rc == 1
              and "is inside config/vms/pa.env's VAULT_DIR" in err, err[-300:])
        rc, s, err = b.resolve("pa")
        check("...and the VM whose vault holds it, too", rc == 1 and "holds config/vms/pb.env's VAULT_DIR" in err, err[-300:])
        os.remove(os.path.join(b.app, "pb"))
        os.makedirs(os.path.join(co, "logs-vms"), exist_ok=True)
        link(os.path.join(co, "logs-vms", "pb"), os.path.join(co, "logs-vms", "PA", "b"))   # neither made yet
        rc, s, err = b.resolve("pb")
        check("...and folders not made yet, spelled in another case: refused", rc == 1
              and "is inside config/vms/pa.env's LOG_DIR" in err, err[-300:])
        os.remove(os.path.join(co, "logs-vms", "pb"))
        for n in ("pa", "pb"):
            drop(os.path.join(co, f"config/vms/{n}.env"))
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        rc, s, err = b.resolve("demo")
        check("a per-VM file's own folders (the defaults) are in nobody's", rc == 0 and s.get("RUN_DIR") == f"{co}/.run-vms/demo"
              and s.get("LOG_DIR") == f"{co}/logs-vms/demo" and s.get("VAULT") == f"{b.app}/demo", (err[-300:], s))
        drop(os.path.join(co, "config/vms/demo.env"))

        # 4. build-vm.sh: the identity rule before anything is deleted (the seed folder under RUN_DIR), for the main VM
        # too; and again after the downloads.
        write(os.path.join(co, "scripts", "build-tools.sh"),
              '#!/bin/bash\n[ -z "${STUB_TOOLS_COPY:-}" ] || cp "${STUB_TOOLS_COPY%%:*}" "${STUB_TOOLS_COPY#*:}"\n', 0o755)
        write(os.path.join(co, "scripts", "fetch-artifacts.sh"), "#!/bin/bash\nexit 0\n", 0o755)
        write(os.path.join(co, "vendor", "bin", "qcow2raw"), '#!/bin/sh\n: > "$2"\n', 0o755)
        seedvault = os.path.join(co, ".run", "seed", "vault")
        marker = os.path.join(co, ".run", "build-unfinished-metacomputer")

        def build(**env):
            shutil.rmtree(os.path.join(store, "metacomputer"), ignore_errors=True)
            if os.path.exists(marker):
                os.remove(marker)
            os.makedirs(seedvault, exist_ok=True)
            write(os.path.join(seedvault, "vault.json"), "{}")
            b.set_vms([])
            return subprocess.run(["/bin/bash", os.path.join(co, "scripts", "build-vm.sh")], env=b.env(**env),
                                  capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)

        link(os.path.join(b.app, "other"), seedvault)      # the other VM's (fixed) vault folder, physically in the seed
        b.settings("config/vms/other.env", 'SSH_PORT=2402\nVNC_PORT=6102\n')
        r = build()
        out = r.stdout + r.stderr
        check("building the main VM when another VM's vault is in its RUN_DIR's seed folder: refused, the vault kept",
              r.returncode != 0 and "not building metacomputer" in out and "holds config/vms/other.env's VAULT_DIR" in out
              and os.path.exists(os.path.join(seedvault, "vault.json")) and "create " not in open(b.log).read(), out[-400:])
        os.chmod(other, 0)
        try:
            r = build()
            out = r.stdout + r.stderr
            check("...or beside a settings file that can't be read: refused before anything is made or deleted",
                  r.returncode != 0 and "can't be read" in out and os.path.exists(os.path.join(seedvault, "vault.json"))
                  and "create " not in open(b.log).read(), out[-400:])
        finally:
            os.chmod(other, 0o600)
        drop(other)
        later = os.path.join(root, "later.env")
        write(later, 'SSH_PORT=2402\nVNC_PORT=6102\n')
        r = build(STUB_TOOLS_COPY=f"{later}:{other}")
        out = r.stdout + r.stderr
        check("...or when that file appears while the tools are built: refused by the second check, before the seed",
              r.returncode != 0 and "holds config/vms/other.env's VAULT_DIR" in out and os.path.exists(os.path.join(seedvault, "vault.json"))
              and "create " not in open(b.log).read() and not os.path.exists(marker), out[-400:])
        drop(other)
        os.remove(os.path.join(b.app, "other"))
        # An MCOMP_CONFIG file that sets no folders of its own (the main VM's, as before v0.3.1): refused, saying how to fix it.
        bare = os.path.join(root, "bare.env")
        write(bare, "VM_NAME=bare\nBASELINE_NAME=bare-baseline\nSSH_PORT=2393\nVNC_PORT=6093\n")
        r = build(MCOMP_CONFIG=bare)
        out = r.stdout + r.stderr
        check("building an MCOMP_CONFIG VM whose folders are the main VM's: refused, naming the fix (its own SSH_DIR, RUN_DIR, LOG_DIR)",
              r.returncode != 0 and "not building bare" in out and "is the same folder as the main VM" in out
              and "set SSH_DIR, RUN_DIR and LOG_DIR in " in out and "config/local.env.example" in out
              and "create " not in open(b.log).read() and not os.path.exists(os.path.join(co, ".run", "build-unfinished-bare")), out[-600:])
    finally:
        b.close()


def final3_fixes(td):
    """The third final check's findings (on 695cfdd), each failing before its fix: a folder whose parent is a link
    escaped the comparison (the alias probe); reset, restore, checkpoint and baseline deleted through the runner with no
    check; the runner's storage was skipped when it couldn't be read or was spelled with ..; and .cache, where the
    downloads clean up, was outside the rule."""
    b = Box(td, "final3")
    try:
        co, root, store, hs = b.co, b.root, b.storage, os.path.join(b.home, ".ssh")
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        main = [vm("metacomputer"), vm("metacomputer-baseline"), vm("metacomputer-baseline-prev"),
                vm("metacomputer-ckpt-one")]
        other = b.settings("config/vms/other.env", "SSH_PORT=2402\nVNC_PORT=6102\n")

        # 1. The alias probe: another VM's SSH_DIR is a link to a folder in the main VM's RUN_DIR/seed. Compared as the
        # folder it physically is, it's inside the main RUN_DIR: refused when loaded, and building the main VM (whose
        # rm -rf of the seed would take those keys) is refused before anything is deleted.
        write(os.path.join(co, "scripts", "build-tools.sh"), "#!/bin/bash\nexit 0\n", 0o755)
        write(os.path.join(co, "scripts", "fetch-artifacts.sh"), "#!/bin/bash\nexit 0\n", 0o755)
        write(os.path.join(co, "vendor", "bin", "qcow2raw"), '#!/bin/sh\n: > "$2"\n', 0o755)
        keys = os.path.join(co, ".run", "seed", "keys")
        os.makedirs(keys)
        write(os.path.join(keys, "admin"), "not the seed's")
        link(os.path.join(hs, "metacomputer-other"), keys)
        rc, s, err = b.resolve("other")
        check("the alias probe: a per-VM SSH_DIR that is a link into the main RUN_DIR's seed: refused when loaded",
              rc == 1 and "is inside the main VM (config/local.env)'s RUN_DIR" in err, err[-300:])
        b.set_vms([])
        r = subprocess.run(["/bin/bash", os.path.join(co, "scripts", "build-vm.sh")], env=b.env(), capture_output=True,
                           text=True, timeout=300, stdin=subprocess.DEVNULL)
        out = r.stdout + r.stderr
        check("...and building the main VM is refused before its seed is deleted (those keys kept)", r.returncode != 0
              and "holds config/vms/other.env's SSH_DIR" in out and os.path.exists(os.path.join(keys, "admin"))
              and "create " not in open(b.log).read(), out[-400:])
        os.remove(os.path.join(hs, "metacomputer-other"))
        shutil.rmtree(os.path.join(co, ".run", "seed"), ignore_errors=True)

        # 2. Every command that deletes, replaces or rotates through the runner checks the rule after taking the VM's
        # lock, the main VM and an MCOMP_CONFIG one too: another VM's vault (a link) in the runner's folder of the VM
        # refuses reset, restore, checkpoint --delete, checkpoint --replace and baseline, deleting nothing.
        caught = os.path.join(store, "metacomputer", "other-vault")
        os.makedirs(caught, exist_ok=True)
        write(os.path.join(caught, "vault.json"), "{}")
        link(os.path.join(b.app, "other"), caught)
        for args, what in ((["reset", "--yes"], "reset"), (["restore", "one", "--yes"], "restore"),
                           (["checkpoint", "--delete", "one", "--yes"], "checkpoint --delete"),
                           (["checkpoint", "one", "--replace"], "checkpoint --replace"), (["baseline", "--yes"], "baseline")):
            b.set_vms(main)
            r = b.run(*args)
            check(f"the main VM's {what} with another VM's vault in the runner's storage: refused, naming it, nothing deleted",
                  r.returncode != 0 and "config/vms/other.env's VAULT_DIR" in r.stderr and "the runner's VM storage" in r.stderr
                  and b.deletes() == [] and os.path.exists(os.path.join(caught, "vault.json")), (r.stderr[-300:], b.deletes()))
        legacy = os.path.join(root, "legacy.env")
        write(legacy, f'VM_NAME=legacy\nBASELINE_NAME=legacy-baseline\nSSH_PORT=2391\nVNC_PORT=6091\nSSH_DIR="{root}/lg-ssh"\n'
                      f'RUN_DIR="{root}/lg-run"\nLOG_DIR="{root}/lg-logs"\n')
        b.set_vms([vm("legacy"), vm("legacy-baseline")])
        r = b.run("reset", "--yes", env=b.env(MCOMP_CONFIG=legacy))
        check("...and an MCOMP_CONFIG VM's reset: refused too, nothing deleted", r.returncode != 0
              and "config/vms/other.env's VAULT_DIR" in r.stderr and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        os.remove(os.path.join(b.app, "other"))
        b.set_vms(main)
        r = b.run("checkpoint", "--delete", "one", "--yes")
        check("(with nothing wrong, checkpoint --delete goes ahead)", r.returncode == 0 and b.deletes() == ["metacomputer-ckpt-one"],
              (r.stderr[-300:], b.deletes()))

        # 3. Fail closed: when the runner's storage can't be known, a per-VM VM, vm create, vm delete, build and the
        # commands above refuse; the main VM's start, stop and status don't depend on it.
        b.set_vms(main)
        r = b.run("--vm", "other", "where", env=b.env(FAKE_CONFIG_FAIL="1"))
        check("the runner's config get failing: a per-VM VM is refused (its storage isn't known)", r.returncode == 1
              and "the runner's VM storage can't be known" in r.stderr, r.stderr[-300:])
        r = b.run("vm", "create", "newone", "--no-build", env=b.env(FAKE_CONFIG_FAIL="1"))
        check("...vm create too (and its file removed)", r.returncode == 1 and "can't be known" in r.stderr
              and not os.path.exists(os.path.join(co, "config/vms/newone.env")), r.stderr[-300:])
        b.set_vms(main[:2])                                # no checkpoint (that refuses first)
        r = b.run("vm", "delete", "metacomputer", "--confirm", "metacomputer", "--force", env=b.env(FAKE_CONFIG_FAIL="1"))
        check("...vm delete of the main VM too, nothing deleted", r.returncode == 1 and "can't be known" in r.stderr
              and b.deletes() == [], (r.stderr[-300:], b.deletes()))
        b.set_vms(main)
        r = b.run("reset", "--yes", env=b.env(FAKE_CONFIG_FAIL="1"))
        check("...and the main VM's reset", r.returncode != 0 and "can't be known" in r.stderr and b.deletes() == [], r.stderr[-300:])
        b.set_vms(main)
        r = b.run("stop", env=b.env(FAKE_CONFIG_FAIL="1"))
        check("...but not the main VM's stop", r.returncode == 0 and "can't be known" not in r.stderr, r.stderr[-300:])
        r = b.run("status", env=b.env(FAKE_CONFIG_FAIL="1"))
        check("...or its status", "can't be known" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-300:])
        os.makedirs(os.path.join(root, "sub"))
        link(os.path.join(b.app, "other"), os.path.join(store, "x"))
        r = b.run("--vm", "other", "where", env=b.env(FAKE_STORAGE_SPELLING=os.path.join(root, "sub", "..", "lume-vms")))
        check("storage the runner spells with ..: resolved, not skipped (a vault in it refused)", r.returncode == 1
              and "is inside the runner's VM storage" in r.stderr, r.stderr[-300:])
        os.remove(os.path.join(b.app, "other"))
        locked = os.path.join(root, "locked")
        os.makedirs(os.path.join(locked, "vms"))
        os.chmod(locked, 0)
        try:
            r = b.run("--vm", "other", "where", env=b.env(FAKE_STORAGE_SPELLING=os.path.join(locked, "vms")))
            check("storage that can't be read (a folder it may not search): refused, not skipped", r.returncode == 1
                  and "can't read the runner's VM storage" in r.stderr, r.stderr[-300:])
        finally:
            os.chmod(locked, 0o700)
        cfg = os.path.join(b.home, ".config", "lume", "config.yaml")
        write(cfg, 'vmLocations:\n  - name: "home"\n    path: "~/.lume"\n')
        os.chmod(cfg, 0)
        try:
            r = b.run("--vm", "other", "where")
            check("Lume's settings file there but unreadable: refused, not skipped", r.returncode == 1
                  and "can't read" in r.stderr and "config.yaml" in r.stderr, r.stderr[-300:])
        finally:
            os.chmod(cfg, 0o600)
            os.remove(cfg)

        # (.cache was a place here; the deletes in it now check their own targets: final4_fixes.)
    finally:
        b.close()


def final4_fixes(td):
    """The fourth final check's findings (on cf6ad10), each failing before its fix: the main VM's storage commands
    deleted the S3 state folder of a volume, and fetching rclone deleted .cache/rclone-vVERSION/x, with no look at
    whether that folder, as it physically is, was another VM's (a link beneath the checked root). Every recursive
    delete now goes through lib/safedelete.py, which checks its own target."""
    b = Box(td, "final4")
    try:
        co, root, app = b.co, b.root, b.app
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        b.settings("config/vms/x.env", "SSH_PORT=2402\nVNC_PORT=6102\n")
        vault = os.path.join(app, "x")                      # VM x's own (fixed) vault folder
        os.makedirs(vault)
        write(os.path.join(vault, "vault.json"), "{}")
        rm = os.path.join(co, "scripts", "safe-rmtree.sh")

        def rmrun(path):
            if not os.path.exists(rm):              # (the code before this round has no helper)
                return subprocess.CompletedProcess([rm], 127, "", "no scripts/safe-rmtree.sh")
            return subprocess.run([rm, path], env=b.env(), capture_output=True, text=True, timeout=120)

        # The helper itself.
        plain = os.path.join(root, "plain", "a")
        os.makedirs(plain)
        write(os.path.join(plain, "f"), "x")
        r = rmrun(os.path.join(root, "plain"))
        check("safe-rmtree.sh: a folder no VM uses is deleted", r.returncode == 0
              and not os.path.exists(os.path.join(root, "plain")), r.stderr[-300:])
        holder = os.path.join(root, "holder")
        os.makedirs(holder)
        os.symlink(vault, os.path.join(holder, "to-vault"))
        r = rmrun(holder)
        check("...a folder holding a link to a VM's vault is deleted, and the link with it, never what it points to",
              r.returncode == 0 and not os.path.lexists(holder) and os.path.exists(os.path.join(vault, "vault.json")),
              r.stderr[-300:])
        os.makedirs(os.path.join(co, ".run"), exist_ok=True)
        r = rmrun(os.path.join(co, ".run"))
        check("...the main VM's RUN_DIR: refused, naming it", r.returncode == 1
              and "is the main VM (config/local.env)'s RUN_DIR" in r.stderr, r.stderr[-300:])
        os.symlink(app, os.path.join(root, "app-alias"))
        r = rmrun(os.path.join(root, "app-alias", "x"))
        check("...a path whose parent is a link, to VM x's vault: refused, the vault kept", r.returncode == 1
              and "is config/vms/x.env's VAULT_DIR" in r.stderr and os.path.exists(os.path.join(vault, "vault.json")),
              r.stderr[-300:])
        r = rmrun(os.path.join(root, "app-alias"))
        check("...a link itself: only the link goes", r.returncode == 0 and not os.path.lexists(os.path.join(root, "app-alias"))
              and os.path.exists(os.path.join(vault, "vault.json")), r.stderr[-300:])
        os.makedirs(os.path.join(root, "elsewhere"))
        os.rename(vault, os.path.join(root, "elsewhere", "x"))
        os.symlink(os.path.join(root, "elsewhere", "x"), vault)          # x's vault path is now a link to it
        r = rmrun(vault)
        check("...a link on the way to a VM's folder (x's vault path is a link): refused", r.returncode == 1
              and "a link on the way to config/vms/x.env's VAULT_DIR" in r.stderr and os.path.islink(vault), r.stderr[-300:])
        r = rmrun(app)
        check("...a folder holding such a link: refused", r.returncode == 1 and "a link on the way to" in r.stderr
              and os.path.islink(vault), r.stderr[-300:])
        os.remove(vault)
        os.rename(os.path.join(root, "elsewhere", "x"), vault)
        os.makedirs(os.path.join(root, "doomed"))
        bad = b.settings("config/vms/unread.env", "SSH_PORT=2403\nVNC_PORT=6103\n")
        os.chmod(bad, 0)
        try:
            r = rmrun(os.path.join(root, "doomed"))
            check("...a settings file that can't be read: nothing is deleted (its folders aren't known)", r.returncode == 1
                  and "can't be read" in r.stderr and os.path.isdir(os.path.join(root, "doomed")), r.stderr[-300:])
        finally:
            os.chmod(bad, 0o600)
            drop(bad)

        # P1 2: a link beneath a checked root. .cache/rclone-vVERSION is a link to the per-VM vaults' folder, so
        # fetching rclone's rm -rf "$R/x" would be VM x's vault. Fetched with stand-ins (no network): refused, kept.
        d = {}
        for ln in open(os.path.join(co, "config", "defaults.env")):
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                d[k.strip()] = v.split("#")[0].strip().strip('"')
        fake = os.path.join(root, "fakebin")
        write(os.path.join(fake, "curl"), "#!/bin/sh\nout=''\nwhile [ $# -gt 0 ]; do [ \"$1\" = -o ] && out=\"$2\"; shift; done\n"
              f"case \"$out\" in *SHA256SUMS) printf '%s  %s\\n' '{d['RCLONE_SHA256']}' '{d['RCLONE_ZIP']}' > \"$out\" ;;\n"
              "  *) printf 'not a zip' > \"$out\" ;; esac\nexit 0\n", 0o755)
        write(os.path.join(fake, "gpgv"), f"#!/bin/sh\necho '[GNUPG:] VALIDSIG {d['RCLONE_SIGNING_KEY_FPR']}'\nexit 0\n", 0o755)
        write(os.path.join(fake, "shasum"), "#!/bin/sh\nexit 0\n", 0o755)
        os.makedirs(os.path.join(co, ".cache"), exist_ok=True)
        rlink = os.path.join(co, ".cache", f"rclone-v{d['RCLONE_VERSION']}")
        link(rlink, app)
        write(os.path.join(app, "signing-key.gpg"), "stand-in")       # the pinned key is there already
        r = subprocess.run(["/bin/bash", os.path.join(co, "scripts", "fetch-artifacts.sh"), "rclone"],
                           env=b.env(PATH=f"{fake}:{os.path.dirname(PY)}:/usr/bin:/bin:/usr/sbin:/sbin"),
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        check("fetching rclone with .cache/rclone-vVERSION linked to the vaults' folder: VM x's vault isn't deleted "
              "(refused, naming it)", r.returncode != 0 and "config/vms/x.env's VAULT_DIR" in r.stdout + r.stderr
              and os.path.exists(os.path.join(vault, "vault.json")), (r.stdout + r.stderr)[-400:])
        os.remove(rlink)

        # P1 1: the main VM's volume delete of an S3 volume whose state folder holds VM x's vault (its fixed path is a
        # link into it): refused before anything changes, the vault and the volume's registration kept. (In-process,
        # with a stand-in for the Keychain tool, so this Mac's Keychain is never touched.)
        sh = os.path.join(root, "storage-home")
        state = os.path.join(sh, "s3", "work")
        os.makedirs(os.path.join(state, "inner"))
        os.rename(vault, os.path.join(state, "inner", "x"))
        os.symlink(os.path.join(state, "inner", "x"), vault)
        reg = os.path.join(sh, "registry", "volumes", "work.json")
        write(reg, json.dumps({"name": "work", "kind": "s3", "path": os.path.join(root, "work-copy")}))
        code = ("import json, os, sys; sys.path.insert(0, sys.argv[1]); import storage\n"
                "storage.SECURITY = '/usr/bin/true'\n"
                "class A: name = 'work'; yes = True; keep_image = False\n"
                "try:\n    storage.cmd_volume_delete(A()); print('DELETED')\n"
                "except storage.StorageError as e:\n    print('REFUSED ' + str(e))\n")
        r = subprocess.run([PY, "-c", code, os.path.join(co, "lib")], env=b.env(MCOMP_STORAGE_HOME=sh),
                           capture_output=True, text=True, timeout=120)
        check("volume delete of an S3 volume whose state folder holds VM x's vault: refused, naming it, the vault kept",
              "REFUSED" in r.stdout and "config/vms/x.env's VAULT_DIR" in r.stdout
              and os.path.exists(os.path.join(state, "inner", "x", "vault.json")), (r.stdout + r.stderr)[-400:])
        check("...and the volume stays registered (nothing was changed first)", os.path.exists(reg))
        if os.path.lexists(vault):
            os.remove(vault)
        if os.path.isdir(os.path.join(state, "inner", "x")):
            os.rename(os.path.join(state, "inner", "x"), vault)
        else:                                               # (the code before this round deleted it)
            os.makedirs(vault)
            write(reg, json.dumps({"name": "work", "kind": "s3", "path": os.path.join(root, "work-copy")}))
            os.makedirs(state, exist_ok=True)
        r = subprocess.run([PY, "-c", code, os.path.join(co, "lib")], env=b.env(MCOMP_STORAGE_HOME=sh),
                           capture_output=True, text=True, timeout=120)
        check("(with nothing of a VM's in it, the same volume delete goes ahead)", "DELETED" in r.stdout
              and not os.path.exists(state) and not os.path.exists(reg), (r.stdout + r.stderr)[-400:])
    finally:
        b.close()


def final5_fixes(td):
    """The fifth check's findings (on 3cf8be0), each failing before its fix: the helper checked one spelling and
    deleted another (a trailing / or /. went through a link to what it points to), tree-walking cleanups (S3 backup
    pruning, old SSH certificates) deleted files without the guard, and the account's own vault and spools weren't
    protected when config/local.env set its own."""
    b = Box(td, "final5")
    try:
        co, root, app = b.co, b.root, b.app
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        b.settings("config/vms/x.env", "SSH_PORT=2402\nVNC_PORT=6102\n")
        vault = os.path.join(app, "x")
        os.makedirs(vault)
        write(os.path.join(vault, "vault.json"), "{}")
        rm = os.path.join(co, "scripts", "safe-rmtree.sh")

        def rmrun(path):
            return subprocess.run([rm, path], env=b.env(), capture_output=True, text=True, timeout=120)

        # P1 1: one path checked, another deleted. A link to VM x's vault, spelled alias/, alias/. and alias/..
        alias = os.path.join(root, "alias")
        os.symlink(vault, alias)
        for spelled in (alias + "/", alias + "/.", alias + "/..", alias + "/../x"):   # the last: x's vault, physically
            r = rmrun(spelled)
            check(f"safe-rmtree.sh alias{spelled[len(alias):]} (a link to VM x's vault): refused, the vault and what "
                  "holds it kept", r.returncode == 1 and os.path.exists(os.path.join(vault, "vault.json")),
                  (r.returncode, r.stderr[-300:]))
        r = rmrun(alias)
        check("...the link itself (alias): only the link goes", r.returncode == 0 and not os.path.lexists(alias)
              and os.path.exists(os.path.join(vault, "vault.json")), r.stderr[-300:])

        # P1 2a: S3 backup pruning walks a tree. VM x's vault path is a link into .../s3/work/backup/x.
        os.makedirs(vault, exist_ok=True)              # (the code before this round deleted it above)
        write(os.path.join(vault, "vault.json"), "{}")
        sh = os.path.join(root, "storage-home")
        backup = os.path.join(sh, "s3", "work", "backup")
        os.makedirs(os.path.join(backup, "x"))
        os.rename(vault, os.path.join(backup, "x", "vault"))
        os.symlink(os.path.join(backup, "x", "vault"), vault)
        write(os.path.join(backup, "old-1.bak"), "x" * 100)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import storage\n"
                "storage.prune_backups(sys.argv[2], 0)\n")
        r = subprocess.run([PY, "-c", code, os.path.join(co, "lib"), backup], env=b.env(MCOMP_STORAGE_HOME=sh),
                           capture_output=True, text=True, timeout=120)
        check("pruning S3 backups when a VM's vault is in the backup folder: refused, nothing pruned, vault.json kept",
              os.path.exists(os.path.join(backup, "x", "vault", "vault.json")) and os.path.exists(os.path.join(backup, "old-1.bak"))
              and "config/vms/x.env's VAULT_DIR" in r.stderr, (r.stdout + r.stderr)[-300:])
        os.remove(vault)
        os.rename(os.path.join(backup, "x", "vault"), vault)
        elsewhere = os.path.join(root, "elsewhere")
        os.makedirs(elsewhere)
        write(os.path.join(elsewhere, "keep.bak"), "x" * 100)
        os.symlink(elsewhere, os.path.join(backup, "linked"))
        r = subprocess.run([PY, "-c", code, os.path.join(co, "lib"), backup], env=b.env(MCOMP_STORAGE_HOME=sh),
                           capture_output=True, text=True, timeout=120)
        check("(with nothing of a VM's there, pruning to 0 bytes deletes the backups, through no link)",
              not os.path.exists(os.path.join(backup, "old-1.bak")) and os.path.exists(os.path.join(vault, "vault.json"))
              and os.path.exists(os.path.join(elsewhere, "keep.bak")), (r.stdout + r.stderr)[-300:])

        # P1 2b: the SSH certificates' cleanup walks $SOCK_DIR/certs. VM x's vault path is a link into certs/x, whose
        # files are over an hour old: cert_for's cleanup leaves them.
        script = ('. "$1/scripts/lib.sh"; ensure_ca() { :; }; CA_KEY=/nonexistent; printf "%s\\n" "$SOCK_DIR"; '
                  'cert_for ops >/dev/null 2>&1; :')
        r = subprocess.run(["/bin/bash", "-c", script, "_", co], env=b.env(), capture_output=True, text=True, timeout=120)
        sock = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        certs = os.path.join(sock, "certs")
        ok_setup = bool(sock) and os.path.isdir(certs)
        if ok_setup:
            os.makedirs(os.path.join(certs, "x"), exist_ok=True)
            os.makedirs(vault, exist_ok=True)          # (the code before this round pruned vault.json above)
            write(os.path.join(vault, "vault.json"), "{}")
            os.rename(vault, os.path.join(certs, "x", "vault"))
            os.symlink(os.path.join(certs, "x", "vault"), vault)
            old = time.time() - 7200
            os.utime(os.path.join(certs, "x", "vault", "vault.json"), (old, old))
            subprocess.run(["/bin/bash", "-c", script, "_", co], env=b.env(), capture_output=True, text=True, timeout=120)
        check("the old SSH certificates' cleanup when a VM's vault is in the certificates' folder: vault.json kept",
              ok_setup and os.path.exists(os.path.join(certs, "x", "vault", "vault.json")), (sock, r.stderr[-300:]))
        if ok_setup:
            os.remove(vault)
            os.rename(os.path.join(certs, "x", "vault"), vault)
        if sock.startswith("/tmp/mcomp-"):
            shutil.rmtree(sock, ignore_errors=True)    # this box's own socket folder (named for its checkout)

        # P1 3: config/local.env names its own vault and spool; the account's own are protected all the same.
        b.settings("config/local.env", f'VM_NAME=metacomputer\nVAULT_DIR="{root}/main-vault"\nEVENTS_SPOOL="{root}/main-spool"\n')
        real_home = os.path.expanduser("~" + __import__("pwd").getpwuid(os.getuid()).pw_name)
        r = subprocess.run([PY, os.path.join(co, "lib", "safedelete.py"), "check", "--",
                            os.path.join(real_home, "Library", "Application Support")], env=b.env(),
                           capture_output=True, text=True, timeout=120)
        check("with config/local.env's own vault and spool, a folder holding the account's vault is still refused "
              "(only checked: nothing deleted)", r.returncode == 1 and ("the account's vault folder" in r.stderr or
              os.path.join(real_home, "Library", "Application Support", "metacomputer") + ")" in r.stderr), r.stderr[-300:])
    finally:
        b.close()

    # Links to the home folder and to the checkout (this box's own copies: the code before this round deleted them).
    b = Box(td, "final5-links")
    try:
        co, root = b.co, b.root
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        rm = os.path.join(co, "scripts", "safe-rmtree.sh")

        def rmrun(path):
            if not os.path.exists(rm):              # (the code before this round deleted the checkout, helper and all)
                return subprocess.CompletedProcess([rm], 127, "", "no scripts/safe-rmtree.sh")
            return subprocess.run([rm, path], env=b.env(), capture_output=True, text=True, timeout=120)

        for target, what in ((b.home, "the home folder"), (co, "the checkout")):
            ln = os.path.join(root, "to-" + os.path.basename(target))
            os.symlink(target, ln)
            r = rmrun(ln + "/")
            check(f"a link to {what}, spelled with a trailing /: refused, {what} kept", r.returncode == 1
                  and os.path.isdir(target) and os.path.exists(os.path.join(co, "bin", "metacomputer")), r.stderr[-300:])
            r = rmrun(ln + "/.")
            check(f"...and {what} spelled with /.: refused", r.returncode == 1 and os.path.isdir(target), r.stderr[-300:])
            r = rmrun(ln)
            check(f"...the link itself: only the link goes, {what} kept", r.returncode == 0 and not os.path.lexists(ln)
                  and os.path.isdir(target), r.stderr[-300:])
    finally:
        b.close()


def sys_python():
    """macOS's own python3 (3.9, which the installer accepts) when it's here and isn't a stub that would ask to install
    the developer tools; else None."""
    if not os.path.exists("/usr/bin/python3") or subprocess.run(["xcode-select", "-p"], capture_output=True).returncode:
        return None
    r = subprocess.run(["/usr/bin/python3", "-c", "import sys; print(sys.version_info < (3, 11))"], capture_output=True,
                       text=True, timeout=60)
    return "/usr/bin/python3" if r.stdout.strip() == "True" else None


def final6_fixes(td):
    """The sixth check's findings (on 96791f2), each failing before its fix: the deletes of a program's own records
    (owners' dead claims, storage's stale space claims, events' broken journal entries) went through a link into
    another VM's vault; vm delete's own protected list left out the account's vault; and deletes crashed on macOS's
    own Python 3.9 (shutil.rmtree's dir_fd needs 3.11)."""
    b = Box(td, "final6")
    try:
        co, root, app = b.co, b.root, b.app
        lib = os.path.join(co, "lib")
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        b.settings("config/vms/x.env", "SSH_PORT=2402\nVNC_PORT=6102\n")
        vault = os.path.join(app, "x")

        def fresh(extra):
            os.makedirs(vault, exist_ok=True)
            write(os.path.join(vault, "vault.json"), "{}")
            for name, body in extra.items():
                write(os.path.join(vault, name), body)
            return ["vault.json", *extra]

        def kept(names):
            return all(os.path.exists(os.path.join(vault, n)) for n in names)

        def inproc(code, py=PY, **env):
            return subprocess.run([py, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1])\n" + code, lib],
                                  env=b.env(**env), capture_output=True, text=True, timeout=120)

        # P1 1a: RUN_DIR/owners a link to VM x's vault: claims() took vault.json (and anything shaped like a claim)
        # for dead claims and deleted them.
        run = os.path.join(co, ".run")
        os.makedirs(run, exist_ok=True)
        os.symlink(vault, os.path.join(run, "owners"))
        names = fresh({"99999.json": '{"bridge_pid": 99999}'})
        r = inproc("import owners; owners.claims()", MCOMP_RUN_DIR=run)
        check("owners: RUN_DIR/owners a link to VM x's vault; vault.json and a file shaped like a dead claim kept",
              kept(names), (r.stdout + r.stderr)[-300:])
        os.remove(os.path.join(run, "owners"))
        own = os.path.join(run, "owners")
        os.makedirs(own)
        write(os.path.join(own, "99999.json"), '{"bridge_pid": 99999}')
        write(os.path.join(own, "notes.json"), "{}")
        r = inproc("import owners; owners.claims()", MCOMP_RUN_DIR=run)
        check("(in its own folder, a dead claim goes; a file that isn't a claim stays)",
              not os.path.exists(os.path.join(own, "99999.json")) and os.path.exists(os.path.join(own, "notes.json")),
              (r.stdout + r.stderr)[-300:])

        # P1 1b: storage's space folder a link to VM x's vault: claims_on() took vault.json for a stale claim.
        sh = os.path.join(root, "storage-home")
        os.makedirs(sh)
        os.symlink(vault, os.path.join(sh, "space"))
        names = fresh({"s3-v-99999-abcdef12.json": '{"pid": 99999, "pstart": "x", "pool": "p", "bytes": 1}'})
        r = inproc("import storage; storage.claims_on('p')", MCOMP_STORAGE_HOME=sh)
        check("storage: its space folder a link to VM x's vault; vault.json and a file shaped like a stale claim kept",
              kept(names), (r.stdout + r.stderr)[-300:])
        os.remove(os.path.join(sh, "space"))
        space = os.path.join(sh, "space")
        os.makedirs(space)
        write(os.path.join(space, "s3-v-99999-abcdef12.json"), '{"pid": 99999, "pstart": "x", "pool": "p", "bytes": 1}')
        write(os.path.join(space, "notes.json"), "{}")
        r = inproc("import storage; storage.claims_on('p')", MCOMP_STORAGE_HOME=sh)
        check("(in its own folder, a stale claim goes; a file that isn't one stays)",
              not os.path.exists(os.path.join(space, "s3-v-99999-abcdef12.json"))
              and os.path.exists(os.path.join(space, "notes.json")), (r.stdout + r.stderr)[-300:])

        # P1 1c: events' admin journal folder a link to VM x's vault: _admin_records() took vault.json for a broken
        # journal entry.
        ev = os.path.join(root, "events")
        os.makedirs(os.path.join(ev, "events-state"))
        os.symlink(vault, os.path.join(ev, "events-state", "admin"))
        rec = "000000000000001-00000000-0000-4000-8000-000000000000.json"
        names = fresh({rec: "{}"})
        r = inproc("import events; events._admin_records()", MCOMP_EVENTS_DIR=ev)
        check("events: its admin journal a link to VM x's vault; vault.json and a file shaped like a broken entry kept",
              kept(names), (r.stdout + r.stderr)[-300:])
        os.remove(os.path.join(ev, "events-state", "admin"))
        adm = os.path.join(ev, "events-state", "admin")
        os.makedirs(adm)
        write(os.path.join(adm, rec), "{}")
        write(os.path.join(adm, "notes.json"), "{}")
        r = inproc("import events; events._admin_records()", MCOMP_EVENTS_DIR=ev)
        check("(in its own folder, a broken entry goes; a file that isn't one stays)",
              not os.path.exists(os.path.join(adm, rec)) and os.path.exists(os.path.join(adm, "notes.json")),
              (r.stdout + r.stderr)[-300:])

        # P1 3: macOS's own Python 3.9 (shutil.rmtree has no dir_fd there): a plain delete, quiet or not. And with
        # rmtree made to refuse dir_fd, in-process under any Python.
        def tree(name):
            t = os.path.join(root, name)
            os.makedirs(os.path.join(t, "a", "b"))
            write(os.path.join(t, "a", "b", "f"), "x")
            write(os.path.join(t, "g"), "x")
            os.symlink(vault, os.path.join(t, "a", "to-vault"))    # a link inside: deleted itself, never followed
            return t
        rm = os.path.join(co, "scripts", "safe-rmtree.sh")
        fresh({})
        code = ("import shutil\n_rm = shutil.rmtree\n"
                "def rmtree(*a, **k):\n    if 'dir_fd' in k:\n        raise TypeError(\"rmtree() got an unexpected keyword argument 'dir_fd'\")\n"
                "    return _rm(*a, **k)\nrmtree.avoids_symlink_attacks = True\nshutil.rmtree = rmtree\n"
                "import safedelete\nsafedelete.rmtree(sys.argv[2])\nsafedelete.rmtree(sys.argv[3], quiet=True)\n")
        t1, t2 = tree("t39a"), tree("t39b")
        r = subprocess.run([PY, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1])\n" + code, lib, t1, t2],
                           env=b.env(), capture_output=True, text=True, timeout=120)
        check("with shutil.rmtree refusing dir_fd (as on 3.9): a folder with a link inside is deleted, quiet or not, "
              "and the link's target is kept", r.returncode == 0 and not os.path.lexists(t1) and not os.path.lexists(t2)
              and kept(["vault.json"]), (r.stdout + r.stderr)[-300:])
        sp = sys_python()
        if sp:
            for quiet in ([], ["--quiet"]):
                t = tree("t39-sys" + ("q" if quiet else ""))
                r = subprocess.run([rm, *quiet, t], env=b.env(PY=sp), capture_output=True, text=True, timeout=120)
                check(f"under {sp} (3.9): safe-rmtree.sh {' '.join(quiet)} deletes a folder with a link inside, the "
                      "link's target kept", r.returncode == 0 and not os.path.lexists(t) and kept(["vault.json"]),
                      (r.stdout + r.stderr)[-300:])
            alias = os.path.join(root, "alias39")
            os.symlink(vault, alias)
            r = subprocess.run([rm, alias + "/"], env=b.env(PY=sp), capture_output=True, text=True, timeout=120)
            check(f"under {sp}: alias/ to VM x's vault still refused, the vault kept", r.returncode == 1
                  and kept(["vault.json"]), (r.stdout + r.stderr)[-300:])
        else:
            print("  (macOS's own python3 isn't here: its checks were skipped)")
    finally:
        b.close()

    # P1 2: vm delete's own protected list. config/local.env names its own vault and spool; the account's vault folder
    # (here: the one under $HOME, this box's home) is a link into .run-vms/x/account. vm delete x deleted it.
    b = Box(td, "final6-del")
    try:
        co, root = b.co, b.root
        b.settings("config/local.env", f'VM_NAME=metacomputer\nVAULT_DIR="{root}/main-vault"\nEVENTS_SPOOL="{root}/main-spool"\n')
        b.settings("config/vms/x.env", "SSH_PORT=2402\nVNC_PORT=6102\n")
        b.set_vms([vm("metacomputer"), vm("metacomputer-baseline"), vm("x"), vm("x-baseline")])
        acct = os.path.join(co, ".run-vms", "x", "account")
        os.makedirs(acct)
        write(os.path.join(acct, "vault.json"), "{}")
        av = os.path.join(b.home, "Library", "Application Support", "metacomputer")
        os.makedirs(os.path.dirname(av), exist_ok=True)
        os.symlink(acct, av)
        r = b.run("vm", "delete", "x", "--confirm", "x")
        check("vm delete x, with the account's vault folder a link into .run-vms/x: refused before anything is deleted, "
              "the vault kept", r.returncode == 1 and os.path.exists(os.path.join(acct, "vault.json"))
              and b.deletes() == [] and ("vault folder" in r.stderr or "holds the main VM" in r.stderr),
              (r.returncode, r.stderr[-400:], b.deletes()))
        # The same list with the account's own home (pwd) standing in for this box's: rmtree_own refuses too.
        os.remove(av)
        h2 = os.path.join(root, "acct-home")
        os.makedirs(os.path.join(h2, "Library", "Application Support"))
        os.symlink(acct, os.path.join(h2, "Library", "Application Support", "metacomputer"))
        code = ("import safedelete\nsafedelete.account_home = lambda: sys.argv[2]\n"
                "try:\n    safedelete.rmtree_own(sys.argv[3], '.run-vms/x', 'x', check_only=True)\n    print('passed')\n"
                "except safedelete.Refused as e:\n    print('refused:', e)\n")
        r = subprocess.run([PY, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1])\n" + code, os.path.join(co, "lib"),
                            h2, os.path.realpath(co)], env=b.env(), capture_output=True, text=True, timeout=120)
        check("(the account's own vault folder, wherever its home is, is in the same list: refused)",
              "refused:" in r.stdout and "the account's vault folder" in r.stdout, (r.stdout + r.stderr)[-300:])
        r = b.run("vm", "delete", "x", "--confirm", "x")
        check("(with nothing of anyone else's in it, vm delete x deletes x and its runtime folder)", r.returncode == 0
              and not os.path.exists(os.path.join(co, ".run-vms", "x")) and "x" in b.deletes(), (r.returncode, r.stderr[-400:]))
    finally:
        b.close()


def final7_fixes(td):
    """The seventh check's findings (on b97d4af), each failing before its fix: the runner's delete of a VM took the
    account's vault with the VM's folder when config/local.env named its own vault and spool (the identity records
    left the account's defaults out, and nothing else checked that folder), and the event spool's consumer deleted a
    file it couldn't quarantine, another VM's vault.json among them when that vault was the spool."""
    b = Box(td, "final7")
    try:
        co, root, store = b.co, b.root, b.storage
        b.settings("config/local.env", f'VM_NAME=metacomputer\nVAULT_DIR="{root}/main-vault"\nEVENTS_SPOOL="{root}/main-spool"\n')
        av = os.path.join(b.home, "Library", "Application Support", "metacomputer")
        os.makedirs(os.path.dirname(av), exist_ok=True)

        def vault_in(vmname):
            """The account's vault folder (this box's: the one under $HOME) a link into the runner's folder of vmname."""
            acct = os.path.join(store, vmname, "account")
            os.makedirs(acct, exist_ok=True)
            write(os.path.join(acct, "vault.json"), "{}")
            if os.path.lexists(av):
                os.remove(av)
            os.symlink(acct, av)
            return os.path.join(acct, "vault.json")

        main = [vm("metacomputer"), vm("metacomputer-baseline"), vm("metacomputer-baseline-prev"), vm("metacomputer-ckpt-one")]
        for args, where, what in ((["vm", "delete", "metacomputer", "--confirm", "metacomputer", "--force"], "metacomputer", "vm delete"),
                                  (["reset", "--yes"], "metacomputer", "reset"),
                                  (["restore", "one", "--yes"], "metacomputer", "restore"),
                                  (["checkpoint", "--delete", "one", "--yes"], "metacomputer-ckpt-one", "checkpoint --delete"),
                                  (["baseline", "--yes"], "metacomputer-baseline-prev", "baseline")):
            b.set_vms(main[:2] if what == "vm delete" else main)
            kept = vault_in(where)
            r = b.run(*args)
            check(f"{what}, the account's vault folder a link into the runner's folder of {where}: refused before the "
                  "runner deletes anything, the vault kept", r.returncode != 0 and os.path.exists(kept) and b.deletes() == [],
                  (r.returncode, r.stderr[-300:], b.deletes()))
        # The runner's delete itself (scripts/lib.sh runner_delete), whoever calls it: the VM's folder is checked with
        # lib/safedelete.py first.
        kept = vault_in("metacomputer")
        b.set_vms(main)
        script = '. "$1/scripts/lib.sh"; runner_delete metacomputer'
        r = subprocess.run(["/bin/bash", "-c", script, "_", co], env=b.env(), capture_output=True, text=True, timeout=120)
        check("runner_delete: a VM folder holding the account's vault isn't deleted (the runner isn't even asked)",
              r.returncode != 0 and os.path.exists(kept) and b.deletes() == [] and "wasn't deleted" in r.stderr,
              (r.returncode, r.stderr[-300:], b.deletes()))
        os.remove(av)
        os.makedirs(os.path.join(store, "metacomputer-ckpt-one"), exist_ok=True)
        script = '. "$1/scripts/lib.sh"; runner_delete metacomputer-ckpt-one'
        r = subprocess.run(["/bin/bash", "-c", script, "_", co], env=b.env(), capture_output=True, text=True, timeout=120)
        check("(with nothing of anyone's in it, runner_delete deletes the VM)", r.returncode == 0
              and b.deletes() == ["metacomputer-ckpt-one"], (r.returncode, r.stderr[-300:], b.deletes()))
        # The identity records always carry the account's own vault and spools, even for a VM that names its own.
        script = 'MC_LIB_DEFS_ONLY=1 . "$1/scripts/lib.sh"; mc_shared_records owner /x/vault /x/spool | tr "\\0" "\\n"'
        r = subprocess.run(["/bin/bash", "-c", script, "_", co], env=b.env(), capture_output=True, text=True, timeout=120)
        check("mc_shared_records: a VM with its own vault and spool still counts the account's (SHARED_*), and its own",
              "SHARED_VAULT_DIR" in r.stdout and "SHARED_EVENTS_SPOOL" in r.stdout and "VAULT_DIR\n/x/vault" in r.stdout
              and "EVENTS_SPOOL\n/x/spool" in r.stdout, r.stdout[-300:])
    finally:
        b.close()

    # P1 2: the event spool's consumer. Another VM's vault (x's) is a link to the spool, which holds its vault.json,
    # and the spool's .quarantine is a file: vault.json was taken for an invalid event and deleted.
    b = Box(td, "final7-spool")
    try:
        co = b.co
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        b.settings("config/vms/x.env", "SSH_PORT=2402\nVNC_PORT=6102\n")
        spool = os.path.join(b.home, "Library", "Application Support", "metacomputer", "events", "spool")
        os.makedirs(spool)
        write(os.path.join(spool, "vault.json"), "{}")
        write(os.path.join(spool, ".quarantine"), "not a folder")
        os.makedirs(b.app, exist_ok=True)
        os.symlink(spool, os.path.join(b.app, "x"))
        lib = os.path.join(co, "lib")
        drain = "import sys; sys.path.insert(0, sys.argv[1]); import events; print(events.drain_spool(events.config_safe()))"
        r = subprocess.run([PY, "-B", "-c", drain, lib], env=b.env(), capture_output=True, text=True, timeout=120)
        check("the spool is also VM x's vault (a link), its .quarantine a file: vault.json kept",
              os.path.exists(os.path.join(spool, "vault.json")), (r.stdout + r.stderr)[-300:])
        write(os.path.join(spool, "ev1.json"), json.dumps({"family": "needs_you", "type": "spool_test", "message": "m"}))
        r = subprocess.run([PY, "-B", "-c", drain, lib], env=b.env(), capture_output=True, text=True, timeout=120)
        state = os.path.join(b.root, "events", "events-state")          # MCOMP_EVENTS_DIR in the box
        errs = os.path.join(state, "errors.log")
        check("...and a valid event there isn't consumed either: the spool is refused as a whole, and that's said",
              os.path.exists(os.path.join(spool, "ev1.json")) and os.path.exists(os.path.join(spool, "vault.json"))
              and os.path.exists(errs) and "isn't consumed" in open(errs).read(), (r.stdout + r.stderr)[-300:])
        # A spool that's nobody else's folder, whose .quarantine is a file: an invalid file is left, counted and said.
        os.remove(os.path.join(b.app, "x"))
        os.makedirs(os.path.join(b.app, "x"))
        for f in (os.path.join(spool, "vault.json"), os.path.join(state, "spool-refused.json")):   # (the refusal is
            if os.path.exists(f):                                                                # remembered a minute)
                os.remove(f)
        write(os.path.join(spool, "bad.json"), "not json")
        r = subprocess.run([PY, "-B", "-c", drain, lib], env=b.env(), capture_output=True, text=True, timeout=120)
        info = os.path.join(state, "spool.json")
        check("a file that can't be quarantined (.quarantine isn't a folder of ours) is left where it is, counted and "
              "said, never deleted", os.path.exists(os.path.join(spool, "bad.json"))
              and os.path.exists(info) and json.load(open(info)).get("left", 0) >= 1
              and "were left in" in open(errs).read(), (r.stdout + r.stderr)[-300:])
        check("...while the valid event is consumed (recorded, then deleted)", not os.path.exists(os.path.join(spool, "ev1.json")),
              (r.stdout + r.stderr)[-300:])
        os.remove(os.path.join(spool, ".quarantine"))
        r = subprocess.run([PY, "-B", "-c", drain, lib], env=b.env(), capture_output=True, text=True, timeout=120)
        check("(with a .quarantine folder, an invalid file is moved there, not deleted)",
              not os.path.exists(os.path.join(spool, "bad.json")) and os.path.exists(os.path.join(spool, ".quarantine", "bad.json")),
              (r.stdout + r.stderr)[-300:])
    finally:
        b.close()


# The recursive deletes that aren't made through lib/safedelete.py (scripts/safe-rmtree.sh), each with why:
#   runner   the runner's delete of a VM, baseline or checkpoint: one place, scripts/lib.sh runner_delete, which checks
#            the VM's folder with lib/safedelete.py first (check-runner); called only after the identity rule
#            (vm_change_lock, vm delete's plan, build-vm.sh's check)
#   guest    runs inside the VM, not on the Mac
#   text     not a delete: a hint printed for the user
DELETE_EXCEPTIONS = [
    ("scripts/lib.sh", '"$LUME" delete "$1" --force', "runner"),     # runner_delete: its folder checked first
    ("bin/metacomputer", "gssh ops 'rm -rf ~/metacomputer-runs/*'", "guest"),
    ("lib/guest_profiles.py", "shutil.rmtree(name, dir_fd=root)", "guest"),   # a browser profile, in the VM
    ("scripts/build-vm.sh", "gssh ops 'rm -rf ~/metacomputer-runs/* 2>/dev/null; true'", "guest"),
    ("lib/guest_put.py", "shutil.rmtree(target)", "guest"),
    ("lib/guest_put.py", "shutil.rmtree(tmp, ignore_errors=True)", "guest"),
    ("lib/guest_put.py", 'remote(ssh, f"rm -rf -- {shlex.quote(tmp)}"', "guest"),
    ("lib/harness.py", 'guest(f"rm -rf {self.guest_dir}', "guest"),
    ("lib/vm_claude_guest.sh", 'rm -rf "$work"', "guest"),
    ("lib/vm_claude_guest.sh", 'rm -rf "$STATE"', "guest"),
    ("lib/vm_shell_guest.py", "shutil.rmtree(p, ignore_errors=True)", "guest"),
    ("lib/vm_shell_guest.py", "shutil.rmtree(d, ignore_errors=True)", "guest"),
    ("lib/page_guest.py", "shutil.rmtree(self.profile, ignore_errors=True)", "guest"),   # its own throwaway profile, in the VM
    ("lib/page_guest.py", "shutil.rmtree(prof, ignore_errors=True)", "guest"),           # ...a kept page's, checked as one
    # A computer elsewhere (scripts/remote-build.sh): the bootstrap's own folder on your server, removed there by the
    # command it runs; the payload's folder in the computer, before a new one is unpacked.
    ("scripts/remote-build.sh", "rc=\\$?; rm -rf ~/$rdir; exit \\$rc", "guest"),
    ("scripts/remote-build.sh", "gssh admin 'sudo rm -rf /opt/metacomputer-payload && sudo install -d", "guest"),
    ("install.sh", "rm -r $SSH_DIR; and last", "text"),
    ("install.sh", "is installed too: rm -rf ~/.local/share/lume-secure", "text"),
    ("install.sh", 'echo "    rm -rf ~/.local/share/lume-secure', "text"),
]
DELETE_HELPER = ("lib/safedelete.py", "scripts/safe-rmtree.sh")
# Python functions that list a folder (os.walk, fwalk, listdir, scandir, glob, rglob, iterdir: themselves or through a
# function of their module) and delete, move or rewrite files themselves rather than through lib/safedelete.py: each
# with why it can't change a file of another VM's, and the code that keeps it so (pinned: each fragment must stay in
# the function). A program's own records go through safedelete.remove_records; an event spool through check_spool.
NAMED = "only files it names itself (not ones it found by listing)"
DELETE_WALK_EXCEPTIONS = {
    # In the VM (`metacomputer browser profiles --delete`): its one listing is /proc, to see whether a browser uses the
    # profile; it deletes one folder, a direct child of the profiles folder reached without following a link.
    ("lib/guest_profiles.py", "main"): ("one browser profile in the VM: a plain name, a real folder directly in the "
                                        "profiles folder (opened link by link with O_NOFOLLOW), by descriptor",
                                        ["root = open_root(kind", "os.stat(name, dir_fd=root, follow_symlinks=False)",
                                         "shutil.rmtree(name, dir_fd=root)"]),
    # Listing only to fingerprint code or guest files (read, hashed); the one write is a record of its own at a fixed path:
    ("lib/update.py", "after_install"): ("metacomputer update / install.sh's last step: lists the checkout's code to "
                                         "fingerprint it; writes only the install record (STAMP), whole, by rename",
                                         ["write_json(STAMP, new_stamp)"]),
    ("lib/update.py", "record_build"): ("build-vm.sh: hashes the guest files; writes only RUN_DIR/vm-build.json",
                                        ['write_json(os.path.join(run_dir, "vm-build.json")']),
    # A developer's tool in the checkout, never run by an installed metacomputer:
    ("scripts/sync-plugin.py", "main"): ("copies the repository's plugin files into the checkout's plugin/ (the fixed "
                                         "COPIES pairs, under LAB); removes only a link that stands where a copy goes, "
                                         "never a folder or a file a listing found (those are reported for git rm)",
                                         ["dst = os.path.join(LAB, dst_rel)", "if os.path.islink(dst):",
                                          'if kind == "extra":', "shutil.copyfile(s, d)"]),
    # Consuming or rewriting what a listing found, behind a guard:
    ("lib/events.py", "drain_spool"): ("the event spool, consumed only after lib/safedelete.py check_spool passed (it "
                                       "may not be, hold or reach another protected folder); a file that can't be "
                                       "quarantined stays", ["why = spool_guard(dfd)", "if quarantine(dfd, qfd, f):"]),
    ("lib/events.py", "quarantine"): ("moves a spool file into the spool's own .quarantine (opened without following "
                                      "a link); never deletes one it can't move",
                                      ["os.rename(f, f, src_dir_fd=dfd, dst_dir_fd=qfd)", "return False"]),
    ("lib/events.py", "flush_admin"): ("rewrites or deletes journal entries _admin_records found: its own names only, "
                                       "in a folder that isn't a link; deletes through safedelete.remove_records",
                                       ["remove_records(admin_dir(), [os.path.basename(p)], ADMIN_NAME)"]),
    ("lib/events.py", "recover_journal"): ("the same journal entries, the same way",
                                           ["remove_records(admin_dir(), [os.path.basename(p)], ADMIN_NAME)"]),
    ("lib/storage.py", "update_holder"): ("rewrites this VM's own lock records: lock names only (LOCK_NAME) that name "
                                          "this VM and checkout",
                                          ['same_holder(rec, vm, lab) and LOCK_NAME.fullmatch(rec["_file"])']),
    ("lib/storage.py", "protect_fd"): ("a read-write folder shared with the VM, by descriptor: only links that lead "
                                       "out of it and special files, never a file or a folder",
                                       ["link_escapes(r, target, rootcanon)", "os.unlink(n, dir_fd=dfd)"]),
    ("lib/storage.py", "stage_scan"): ("a staging folder this program made: only plain files with its own session "
                                       "prefixes, by descriptor", ["n.startswith(cleanup) and stat.S_ISREG(st.st_mode)"]),
    ("lib/storage.py", "clear_state"): ("storage's own fixed names only (its state files, storage-snap-*, "
                                        "storage-watch.json), never a folder",
                                        ['f.startswith("storage-snap-") or f == "storage-watch.json"']),
    ("lib/storage.py", "clean_stage"): ("only names of this program's own sessions, by descriptor, in a staging folder "
                                        "it made (its identity checked); folders only when empty",
                                        ["n.startswith(prefixes)", "os.rmdir(n, dir_fd=fd)", "S_ISREG"]),
    ("lib/guest_import.py", "sweep"): ("the staging folder this import made (mkdtemp), by descriptor, following no link",
                                       ["dir_fd="]),
    ("lib/guest_import.py", "merge"): ("replaces one entry at a time in the destination (a file or link where the VM "
                                       "sent a folder), by descriptor; never a folder's contents", ["dir_fd="]),
    ("lib/vm_shell_guest.py", "sweep"): ("runs inside the VM, not on the Mac", []),
    ("lib/vm_shell_guest.py", "main"): ("runs inside the VM, not on the Mac", []),
    # Files it names itself; the listing elsewhere in the function (or a function it calls) only reads:
    ("lib/events.py", "_config_drift"): (NAMED, ["write_private(hp, cur)", 'write_private(gp, raw.decode("utf-8", "replace"))']),
    ("lib/events.py", "change_sink"): (NAMED + ": the journal entry it's writing", ["write_private(path, json.dumps(rec))"]),
    ("lib/events.py", "journal_announcement"): (NAMED + ": a new journal entry", ["write_private(path, json.dumps(rec))"]),
    ("lib/events.py", "next_seq"): (NAMED + ": the sequence counter", ["write_private(p, str(n))"]),
    ("lib/events.py", "watch"): (NAMED + ": its state and its pid file", ["write_private(sp, json.dumps(st))", "os.remove(pf)"]),
    ("lib/harness.py", "_collect"): ("moves the recordings it just downloaded (its own temporary folder) to names it "
                                     "makes", ["os.replace(vids[0], self.video_host)"]),
    ("lib/recording.py", "cmd_sidecar"): (NAMED + ": the description file, written atomically", ["os.replace(tmp, out)"]),
    ("lib/storage.py", "acquire"): (NAMED + ": a new lock and the disk's sidecar",
                                    ['atomic_json(os.path.join(locks_dir(), name + ".json"), rec)']),
    ("lib/storage.py", "cmd_checkpoint"): (NAMED + ": a checkpoint's manifest", ["os.unlink(man_path)"]),
    ("lib/storage.py", "cmd_prepare"): (NAMED + ": the VM's plan, state and snapshots",
                                        ['atomic_json(f["plan"], guest_plan(items))']),
    ("lib/storage.py", "cmd_refdisk_create"): (NAMED + ": its own temporary file, by descriptor",
                                               ["os.unlink(tmp_name, dir_fd=dfd)"]),
    ("lib/storage.py", "cmd_watch"): (NAMED + ": the watcher's pid file", ["os.unlink(watcher_pid_path())"]),
    ("lib/storage.py", "create_s3"): (NAMED + ": the empty folder it just made, by descriptor", ["os.rmdir(leaf, dir_fd=dfd)"]),
    ("lib/storage.py", "remove_stage"): (NAMED + ": its staging folder once empty, by descriptor",
                                         ["os.rmdir(leaf, dir_fd=dfd)"]),
    ("lib/storage.py", "s3_sync"): (NAMED + ": a volume's sync state", ['atomic_json(os.path.join(d, "last-sync.json"), out)']),
    ("lib/storage.py", "space_claims"): (NAMED + ": the claim files it made in this call", ["files.append(f)", "os.unlink(f)"]),
    ("lib/storage.py", "watch_loop"): (NAMED + ": the watcher's stats", ["atomic_json(watch_stats_path(), stats, mode=0o644)"]),
    ("scripts/vendor-driver-skill.py", "main"): ("a developer tool: puts its own staging folder in place of the "
                                                 "vendored pack in this checkout, after deleting that through the "
                                                 "helper", ["safedelete.rmtree(DEST)", "os.replace(stage, DEST)"]),
}
WALK_CALLS = {"walk", "fwalk", "listdir", "scandir", "glob", "iglob", "rglob", "iterdir"}
# Deletes and overwrites: unlink/rmdir/removedirs/rmtree whatever they're called on; os.remove, os.rename, os.replace,
# shutil.move (not list.remove or str.replace); and the programs' own file writers (an overwrite of a file found by
# listing is as much a change of it as a delete).
DELETE_CALLS = {"unlink", "rmdir", "removedirs", "rmtree", "remove", "rename", "replace", "move",
                "write_private", "atomic_json", "write_json", "atomic_write"}
OS_ONLY_CALLS = {"remove", "rename", "replace", "move"}
# Shell forms that delete a tree: find -delete / -exec rm, xargs rm, rsync --delete, tar --remove-files, ditto.
TREE_SHELL_PAT = (r'\bfind\b.*(-delete\b|-exec\s+rm\b)|\bxargs\b.*\brm\b|\brsync\b.*--delete|--remove-files|'
                  r'^\s*[^#]*\bditto\b|\brm\b[^#|;&]*\*')
TREE_SHELL_EXCEPTIONS = [
    ("scripts/lib.sh", '"$SOCK_DIR"/lume-*.sock', "the runner's own sockets (lume-*.sock) in this checkout's socket folder"),
    ("bin/metacomputer", "gssh ops 'rm -rf ~/metacomputer-runs/*'", "inside the VM"),
    ("scripts/build-vm.sh", "gssh ops 'rm -rf ~/metacomputer-runs/* 2>/dev/null; true'", "inside the VM"),
]
DELETE_PAT = (r'\brm +-[a-zA-Z]*r[a-zA-Z]*\b|shutil\.rmtree\(|(?<![\w.])rmtree\(|removedirs\(|'
              r'"\$LUME" delete|\$LUME delete|\blume delete')


def shell_function(text, line_no):
    """The name of the shell function a line (1-based) is in: the last NAME() { above it at the start of a line."""
    name = None
    for i, ln in enumerate(text.splitlines()[:line_no], 1):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{", ln)
        if m:
            name = m.group(1)
    return name


def change_calls(node):
    """The names of the calls under node that list (WALK_CALLS) or change files (DELETE_CALLS): os.remove/rename/
    replace and shutil.move only as such; calls made through lib/safedelete.py don't count."""
    out = set()
    import ast
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
        if base == "safedelete":
            continue
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in OS_ONLY_CALLS and base not in ("os", "shutil"):
            continue
        out.add(name)
    return out



# What would reap children another part of the process started (lib/labconfig.py's _ExitWatch relies on there being
# none). Python is read as code (ast), with its imports and their aliases resolved: os.wait, wait3 and wait4 (any use);
# waitpid and waitid called with a first argument that isn't a plain variable or attribute (a literal like -1 or 0, a
# negative group) or with P_ALL or P_PGID, or used without being called (an alias can't be checked); any use of SIGCHLD
# or SIGCLD (a handler, ignoring it); asyncio's Safe and Fast child watchers. Other files (shell) by text: a trap on
# CHLD (an exec'd Python inherits an ignored SIGCHLD), and the same calls in embedded Python, line by line.
REAP_MODULES = {"os": "os", "posix": "os", "signal": "signal", "asyncio": "asyncio", "asyncio.unix_events": "asyncio"}
REAP_TEXT = (r"waitpid\(\s*(-1|0)\s*,|\bos\.wait\(|\bwait3\(|\bwait4\(|\bP_ALL\b|\bP_PGID\b|\bSIGCHLD\b|\bSIGCLD\b|"
             r"\btrap\b.*\bCHLD\b|SafeChildWatcher|FastChildWatcher")


def reap_findings_py(src):
    """(line, what) for every way a Python source could reap children it didn't start; a source that doesn't parse
    is one finding (it can't be checked)."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(e.lineno or 0, "doesn't parse, so it can't be checked")]
    mods, names = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in REAP_MODULES:
                    mods[a.asname or a.name.split(".")[0]] = REAP_MODULES[a.name] if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module in REAP_MODULES:
            for a in node.names:
                names[a.asname or a.name] = REAP_MODULES[node.module] + "." + a.name

    def qual(e):
        if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and e.value.id in mods:
            return REAP_MODULES.get(mods[e.value.id], mods[e.value.id]) + "." + e.attr
        if isinstance(e, ast.Name):
            return names.get(e.id)
        return None

    def tail(e):
        return e.attr if isinstance(e, ast.Attribute) else e.id if isinstance(e, ast.Name) else None
    out = []
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in REAP_MODULES:
            for a in node.names:
                if a.name in ("wait", "wait3", "wait4", "SIGCHLD", "SIGCLD", "SafeChildWatcher", "FastChildWatcher", "*"):
                    out.append((node.lineno, f"imports {node.module}.{a.name}"))
        if isinstance(node, (ast.Name, ast.Attribute)):
            t = tail(node)
            if t in ("SIGCHLD", "SIGCLD", "SafeChildWatcher", "FastChildWatcher"):
                out.append((node.lineno, t))
            q = qual(node)
            if q in ("os.wait", "os.wait3", "os.wait4"):
                out.append((node.lineno, q))
            elif q in ("os.waitpid", "os.waitid") and id(node) not in called:
                out.append((node.lineno, q + " used without being called"))
        if isinstance(node, ast.Constant) and node.value in ("SIGCHLD", "SIGCLD"):
            out.append((node.lineno, repr(node.value)))
        if isinstance(node, ast.Call) and qual(node.func) in ("os.waitpid", "os.waitid"):
            first = node.args[0] if node.args else None
            if first is None or not isinstance(first, (ast.Name, ast.Attribute)) \
                    or tail(first) in ("P_ALL", "P_PGID"):
                out.append((node.lineno, f"{qual(node.func)} for any child or a group"))
    return sorted(set(out))


def reap_findings_text(text):
    """(line, text) for the shell's forms (and embedded Python's, by line); comment lines don't count."""
    pat = re.compile(REAP_TEXT)
    return [(i, ln.strip()[:120]) for i, ln in enumerate(text.splitlines(), 1)
            if not ln.lstrip().startswith("#") and pat.search(ln)]


REAP_FORBIDDEN = {
    "a multi-line waitpid(-1, ...)": "import os\nos.waitpid(\n    -1, os.WNOHANG\n)\n",
    "waitid(P_ALL, ...)": "import os\nos.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG)\n",
    "from os import wait; wait()": "from os import wait\nwait()\n",
    "waitpid(-1, 0)": "import os\nos.waitpid(-1, 0)\n",
    "waitpid(0, ...)": "import os\nos.waitpid(0, os.WNOHANG)\n",
    "waitpid(-group, ...)": "import os\nos.waitpid(-pgid, 0)\n",
    "os.wait() through an alias of os": "import os as o\no.wait()\n",
    "posix.wait3": "import posix\nposix.wait3(0)\n",
    "wait4 imported under another name": "from os import wait4 as w4\nw4(pid, 0)\n",
    "waitid(P_PGID, ...)": "import os\nos.waitid(os.P_PGID, 0, os.WEXITED)\n",
    "waitid with P_ALL imported": "from os import waitid, P_ALL\nwaitid(P_ALL, 0, 4)\n",
    "waitid with a literal idtype": "import os\nos.waitid(0, 0, os.WEXITED)\n",
    "waitpid kept under another name": "import os\nreap = os.waitpid\nreap(-1, 0)\n",
    "SIGCHLD ignored": "import signal\nsignal.signal(signal.SIGCHLD, signal.SIG_IGN)\n",
    "a SIGCHLD handler, names imported": "from signal import signal as sig, SIGCHLD\nsig(SIGCHLD, lambda *a: None)\n",
    "SIGCLD": "import signal\nsignal.signal(signal.SIGCLD, h)\n",
    "SIGCHLD by name": "import signal\nsignal.signal(getattr(signal, 'SIGCHLD'), h)\n",
    "a Safe child watcher": "import asyncio\nasyncio.set_child_watcher(asyncio.SafeChildWatcher())\n",
    "a Fast child watcher, imported": "from asyncio import FastChildWatcher\nFastChildWatcher()\n",
    "a star import of os": "from os import *\n",
}
REAP_ALLOWED = {
    "waitpid on its own child": "import os\nos.waitpid(pid, 0)\n",
    "waitpid on p.pid, its status": "import os\nstatus = os.waitpid(p.pid, 0)[1]\n",
    "waitid on its own child": "import os\nos.waitid(os.P_PID, self.p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)\n",
    "SIGTERM ignored": "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n",
    "a Popen's own wait": "import subprocess\nsubprocess.Popen(['x']).wait()\np.wait(timeout=1)\n",
    "the names in a comment": "import os\n# os.wait(), waitpid(-1, 0), SIGCHLD, SafeChildWatcher\nx = 1\n",
}


def reap_inventory(td):
    """No code in bin/, scripts/, lib/ or install.sh reaps children it didn't start (lib/labconfig.py signals the
    resolver's group only while it's unreaped, and relies on that). Python is read as code; comments don't count:
    labconfig's says what it relies on."""
    files = ["install.sh"] + sorted(os.path.relpath(os.path.join(d, f), LAB) for top in ("bin", "scripts", "lib")
                                    for d, _, fs in os.walk(os.path.join(LAB, top)) if "__pycache__" not in d for f in fs)
    hits, pys = [], 0
    for rel in files:
        try:
            text = open(os.path.join(LAB, rel)).read()
        except (UnicodeDecodeError, OSError):
            continue
        if rel.endswith(".py") or (text.startswith("#!") and "python" in text.split("\n", 1)[0]):
            pys += 1
            hits += [f"{rel}:{n}: {what}" for n, what in reap_findings_py(text)]
        else:
            hits += [f"{rel}:{n}: {what}" for n, what in reap_findings_text(text)]
    check(f"nothing in bin/, scripts/, lib/ or install.sh reaps children it didn't start ({pys} Python files read as "
          "code, the rest as text)", not hits and pys > 20, "\n".join(hits) or pys)
    missed = [k for k, src in REAP_FORBIDDEN.items() if not reap_findings_py(src)]
    check(f"...the scanner finds each of {len(REAP_FORBIDDEN)} forbidden forms (a multi-line waitpid(-1, ...), "
          "waitid(P_ALL, ...), from os import wait, aliases, SIGCHLD, the watchers, ...)", not missed, missed)
    wrong = {k: reap_findings_py(src) for k, src in REAP_ALLOWED.items() if reap_findings_py(src)}
    check("...and none in what's allowed (waitpid/waitid on its own child, SIGTERM ignored, a Popen's wait, comments)",
          not wrong, wrong)
    check("...in shell: a trap on CHLD, but not on INT", reap_findings_text("trap '' CHLD\n")
          and not reap_findings_text("trap 'cleanup' INT\n# trap '' CHLD\n"))
    check("...and labconfig's comment says what it relies on", "no waitpid(-1) or os.wait()" in open(
        os.path.join(LAB, "lib", "labconfig.py")).read())


def delete_inventory(td):
    """No bare recursive delete: every rm -r, rmtree and removedirs in bin/, scripts/, lib/ and install.sh goes
    through lib/safedelete.py (scripts/safe-rmtree.sh), except those in DELETE_EXCEPTIONS (the runner's own deletes,
    which run after the identity rule; deletes inside the VM; printed hints). A new one, or an exception no longer in
    the code, fails this test."""
    pat = re.compile(DELETE_PAT)
    files = ["install.sh"] + sorted(os.path.relpath(os.path.join(d, f), LAB) for top in ("bin", "scripts", "lib")
                                    for d, _, fs in os.walk(os.path.join(LAB, top)) if "__pycache__" not in d for f in fs)
    bare, used, runner_lines = [], set(), []
    for rel in files:
        if rel in DELETE_HELPER:
            continue
        try:
            text = open(os.path.join(LAB, rel)).read()
        except (UnicodeDecodeError, OSError):
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            if not pat.search(ln):
                continue
            hits = [e for e in DELETE_EXCEPTIONS if e[0] == rel and e[1] in ln]
            if not hits:
                bare.append(f"{rel}:{i}: {ln.strip()[:120]}")
            for e in hits:
                used.add(e)
                if e[2] == "runner":
                    runner_lines.append((rel, i, text))
    check("no recursive delete (rm -r, rmtree, removedirs) outside lib/safedelete.py, but the listed exceptions", not bare,
          "\n".join(bare))
    stale = [e for e in DELETE_EXCEPTIONS if e not in used]
    check("...and every listed exception is still in the code", not stale, stale)
    # Deletes that walk a tree: Python functions with a walk and a delete of their own, and shell tree-delete forms.
    import ast
    walkers, seen, shell, unpinned, shell_used = [], set(), [], [], set()
    tpat = re.compile(TREE_SHELL_PAT)
    for rel in files:
        if rel in DELETE_HELPER:
            continue
        try:
            text = open(os.path.join(LAB, rel)).read()
        except (UnicodeDecodeError, OSError):
            continue
        if rel.endswith(".py") or text.startswith("#!/usr/bin/env python"):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            fns, edges, nodes = {}, {}, {}
            for node in ast.walk(tree) if tree else ():
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fns.setdefault(node.name, set()).update(change_calls(node))
                    edges.setdefault(node.name, set()).update(
                        c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name))
                    nodes.setdefault(node.name, node)
            lists = {k for k, v in fns.items() if v & WALK_CALLS}
            grew = True
            while grew:                              # through the module's own functions, however deep
                grew = False
                for k, v in edges.items():
                    if k not in lists and v & lists:
                        lists.add(k)
                        grew = True
            for k in sorted(fns):
                if not (fns[k] & DELETE_CALLS and k in lists):
                    continue
                if (rel, k) in DELETE_WALK_EXCEPTIONS:
                    seen.add((rel, k))
                    body = ast.get_source_segment(text, nodes[k]) or ""
                    unpinned += [f"{rel} {k}(): {frag}" for frag in DELETE_WALK_EXCEPTIONS[(rel, k)][1] if frag not in body]
                else:
                    walkers.append(f"{rel}:{nodes[k].lineno} {k}()")
            # Code outside any function, too (a script's top level).
            top = set()
            for st in (tree.body if tree else ()):
                if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    top |= change_calls(st)
            if top & WALK_CALLS and top & DELETE_CALLS:
                walkers.append(f"{rel}: its top level")
        else:
            for i, ln in enumerate(text.splitlines(), 1):
                if tpat.search(ln) and not ln.lstrip().startswith("#"):
                    hit = [e for e in TREE_SHELL_EXCEPTIONS if e[0] == rel and e[1] in ln]
                    shell_used.update(hit)
                    if not hit:
                        shell.append(f"{rel}:{i}: {ln.strip()[:120]}")
    check("no Python function lists a folder (itself or through its module's functions) and deletes, moves or rewrites "
          "files outside lib/safedelete.py, but the listed ones", not walkers, "\n".join(walkers))
    check("...and every listed one is still in the code", seen == set(DELETE_WALK_EXCEPTIONS),
          sorted(set(DELETE_WALK_EXCEPTIONS) - seen))
    check("...and each still keeps the guard it's listed for", not unpinned, "\n".join(unpinned))
    recs = {("lib/owners.py", "drop_dead_claims"): "safedelete.remove_records(OWNERS_DIR, names, CLAIM_NAME)",
            ("lib/storage.py", "claims_on"): "safedelete.remove_records(d, stale, SPACE_NAME)",
            ("lib/events.py", "remove_records"): "safedelete.remove_records(folder, names, pattern)",
            ("lib/events.py", "_admin_records"): "if not stat.S_ISDIR(os.lstat(admin_dir()).st_mode):",
            ("lib/handoff.py", "drop_marks"): "safedelete.remove_records(owners.OWNERS_DIR, marks, VAULT_MARK_NAME)",
            ("lib/storage.py", "release_locks"): 'safedelete.remove_records(locks_dir(), [rec["_file"] for rec in mine], LOCK_NAME)'}
    missing = [f"{r} {fn}" for (r, fn), frag in recs.items()
               if not any(isinstance(n, ast.FunctionDef) and n.name == fn and frag in (ast.get_source_segment(src, n) or "")
                          for src in [open(os.path.join(LAB, r)).read()] for n in ast.walk(ast.parse(src)))]
    check("records (dead claims, stale space claims, broken journal entries, old quarantined files, settled vault "
          "markers, this VM's storage locks) go through safedelete.remove_records", not missing and "remove_records(admin_dir(), broken, ADMIN_NAME)"
          in open(os.path.join(LAB, "lib", "events.py")).read() and "QUARANTINED)" in open(os.path.join(LAB, "lib", "events.py")).read(),
          missing)
    check("no shell tree delete (find -delete, -exec rm, xargs rm, rsync --delete, tar --remove-files, ditto, rm of a "
          "glob), but the listed ones", not shell, "\n".join(shell))
    check("...and every listed one is still in the code", set(TREE_SHELL_EXCEPTIONS) == shell_used,
          sorted(set(TREE_SHELL_EXCEPTIONS) - shell_used))
    hp = os.path.join(LAB, "lib", "safedelete.py")
    helper = open(hp).read() if os.path.exists(hp) else ""
    sr = os.path.join(LAB, "scripts", "safe-rmtree.sh")
    check("the helper checks each target physically against every VM's folders, failing closed", os.path.exists(sr) and
          "def check(path, prot" in helper and "vms.physical(t.path" in helper
          and "remove_tree_at(fd, t.name, t.st, t.spelled)" in helper and "shutil.rmtree(" not in helper
          and "def rmtree_own(" in helper and "exclude_vm=target_vm" in helper and "mc_all_identity_records" in helper
          and 'if ids.get("UNREADABLE"):' in helper and 'MC_LIB_DEFS_ONLY=1' in helper
          and "safedelete.py\" rmtree" in open(sr).read())
    lib_sh = open(os.path.join(LAB, "scripts", "lib.sh")).read()
    rd = re.search(r"^runner_delete\(\) \{.*?^\}$", lib_sh, re.S | re.M)
    rd = rd.group(0) if rd else ""
    check("the runner's delete is one function, which checks the VM's folder with lib/safedelete.py first",
          'd="$(vm_dir "$1")" || {' in rd and 'safedelete.py" check-runner -- "$d" || return 1' in rd
          and rd.index("check-runner") < rd.index('"$LUME" delete "$1" --force') and len(runner_lines) == 1, rd)
    checked = []
    for rel in ("bin/metacomputer", "scripts/build-vm.sh", "scripts/vm-commands.sh", "install.sh"):
        text = open(os.path.join(LAB, rel)).read()
        for i, ln in enumerate(text.splitlines(), 1):
            if not re.search(r'\brunner_delete "', ln) or ln.lstrip().startswith("#"):
                continue
            fn = shell_function(text, i) if rel != "scripts/build-vm.sh" else None
            if rel == "bin/metacomputer":
                ok = fn in ("do_checkpoint", "do_baseline", "restore_from")
                if fn == "restore_from":
                    callers = [shell_function(text, j) for j, l2 in enumerate(text.splitlines(), 1)
                               if re.search(r"\brestore_from \"", l2)]
                    bodies = [re.search(r"^%s\(\) \{.*?^\}$" % c, text, re.S | re.M).group(0) for c in callers]
                    ok = ok and set(callers) <= {"do_reset", "do_restore"} and all("vm_change_lock" in body for body in bodies)
                elif ok:
                    ok = "vm_change_lock" in re.search(r"^%s\(\) \{.*?^\}$" % fn, text, re.S | re.M).group(0)
            elif rel == "scripts/vm-commands.sh":
                plan = re.search(r"^vm_delete_plan\(\) \{.*?^\}$", text, re.S | re.M).group(0)
                ok = fn == "vm_delete" and "mc_identity_check --no-ports" in plan and "vm_other_identities" in plan
            elif rel == "scripts/build-vm.sh":
                lines = text.splitlines()
                last_check = max(j for j, l2 in enumerate(lines, 1) if l2.startswith("build_identity_check"))
                ok = last_check < i
            else:
                ok = False
            checked.append((rel, i, fn, ok))
    check("every runner delete runs after the identity rule (vm_change_lock, vm delete's plan, build-vm.sh's check)",
          checked and all(c[3] for c in checked), [c for c in checked if not c[3]])


CLAUDE_STAND_IN = r'''import json, os, subprocess, sys
# A stand-in for lib/vm_claude.py: what it was started with, and what a child it starts the way its keeper's probes
# and its `secrets` calls do ($MCOMP_SELF, with the environment it inherited) acts on.
r = subprocess.run([os.environ["MCOMP_SELF"], "where"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
print(json.dumps({"config": os.environ.get("MCOMP_CONFIG", ""), "vm": os.environ.get("MCOMP_VM_NAME", ""),
                  "args": sys.argv[1:], "child_where": r.stdout, "child_err": r.stderr}))
'''


def claude_flow(td):
    """`metacomputer claude` (lib/vm_claude.py, from main) on a VM chosen by name: it, its keeper and its probes run
    with that VM's settings, and its acting subcommands say which VM and vault first."""
    b = Box(td, "claude")
    try:
        co = b.co
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        demo = b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        b.set_vms([vm("metacomputer"), vm("metacomputer-baseline"), vm("demo"), vm("demo-baseline")])
        write(os.path.join(co, "lib", "vm_claude.py"), CLAUDE_STAND_IN)
        r = b.run("--vm", "demo", "claude", "stop")
        try:
            got = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = {}
        check("metacomputer --vm demo claude: vm_claude runs with demo's settings file (MCOMP_CONFIG) and name",
              os.path.realpath(got.get("config", "")) == os.path.realpath(demo) and got.get("vm") == "demo", (r.stderr[-300:], got))
        check("...and what it starts as $MCOMP_SELF (its keeper's probes, its secrets calls) acts on demo too",
              "vm        demo (baseline demo-baseline; settings: config/vms/demo.env)" in got.get("child_where", ""),
              got.get("child_where", "")[:300] + got.get("child_err", "")[-200:])
        check("...after one line naming the VM and its vault (an acting subcommand)", r.stderr.startswith(
              f"metacomputer: VM demo (config/vms/demo.env), vault {b.app}/demo\n"), r.stderr[-300:])
        r = b.run("--vm", "demo", "claude", "status")
        check("...but not for one that only looks (status)", "metacomputer: VM demo" not in r.stderr, r.stderr[-300:])
    finally:
        b.close()


HUB_SCRIPT = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import mcp_hub
env, cfg, kh = json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
good_cfg, good_kh = open(cfg).read(), open(kh).read()
h = mcp_hub.Hub(env=env)
out = {"pin": getattr(h, "pin", {}), "steps": {}}
def step(name, cfg_text=None, kh_text=None):
    if cfg_text is None:
        os.remove(cfg)
    else:
        open(cfg, "w").write(cfg_text)
    open(kh, "w").write(kh_text if kh_text is not None else good_kh)
    out["steps"][name] = h.connect() or ""
step("as started", good_cfg)
step("settings file gone")
step("settings name another VM", good_cfg.replace("VM_NAME=pinned", "VM_NAME=metacomputer"))
out["input_reset_other"] = h.input_reset() or ""     # every command the hub runs, not just its bridge
step("another SSH port", good_cfg.replace("SSH_PORT=2481", "SSH_PORT=2482"))
step("another host key", good_cfg, good_kh.replace("AAAAPINNED", "AAAAOTHER"))
step("back as started", good_cfg)
print(json.dumps(out))
'''


def hub_pin(td):
    """A hub serves the VM it started with, and only it: when its settings file is gone, or names another VM, port or
    host key, a reconnect is refused with the reason (never re-resolved to the default VM)."""
    b = Box(td, "hubpin")
    try:
        b.settings("config/local.env", "VM_NAME=metacomputer\n")
        b.set_vms([vm("metacomputer"), vm("metacomputer-baseline")])      # only the default VM: nothing else to refuse on
        ssh = os.path.join(b.root, "sshpin")
        kh = os.path.join(ssh, "known_hosts")
        write(kh, "[127.0.0.1]:2481 ssh-ed25519 AAAAPINNED\n")
        cfg = os.path.join(b.root, "pinned.env")
        write(cfg, f'VM_NAME=pinned\nBASELINE_NAME=pinned-baseline\nSSH_PORT=2481\nVNC_PORT=6181\nSSH_DIR="{ssh}"\n'
                   f'RUN_DIR="{b.root}/pinrun"\nLOG_DIR="{b.root}/pinlog"\n')
        r = b.run("hub", "env", env=b.env(MCOMP_CONFIG=cfg))
        try:
            henv = dict(b.env(MCOMP_CONFIG=cfg), **json.loads(r.stdout))
        except ValueError:
            check("the hub's settings for the pinned VM", False, r.stderr[-300:])
            return
        r = subprocess.run([PY, "-c", HUB_SCRIPT, os.path.join(b.co, "lib"), json.dumps(henv), cfg, kh],
                           capture_output=True, text=True, timeout=300, env=henv, stdin=subprocess.DEVNULL)
        try:
            d = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            check("the hub ran its reconnects", False, (r.stdout[-300:], r.stderr[-500:]))
            return
        pin, steps = d["pin"], d["steps"]
        check("a hub pins its VM's name, SSH port and host key when it starts", pin.get("MCOMP_EXPECT_VM") == "pinned"
              and pin.get("MCOMP_EXPECT_SSH_PORT") == "2481" and "AAAAPINNED" in pin.get("MCOMP_EXPECT_HOSTKEY", ""), pin)
        check("...and connects to it as started (it's off here: the usual answer, no refusal)",
              "refusing" not in steps["as started"] and steps["as started"], steps["as started"])
        for name, words in (("settings file gone", "is gone, and without it the settings name 'metacomputer'"),
                            ("settings name another VM", "now name the VM 'metacomputer', not 'pinned'"),
                            ("another SSH port", "the SSH port 2482, not 2481"),
                            ("another host key", "pinned host key")):
            check(f"a reconnect when the {name}: refused, saying why (never the other VM)",
                  "refusing" in steps[name] and words in steps[name] and "hub stop" in steps[name], steps[name][-400:])
        check("...and once the settings are as they were, it connects again", "refusing" not in steps["back as started"],
              steps["back as started"])
        check("the hub's other commands (input-reset) are pinned too: refused when the settings name another VM",
              "now name the VM 'metacomputer', not 'pinned'" in d.get("input_reset_other", ""), d.get("input_reset_other", "")[-300:])
    finally:
        b.close()


def main():
    td = tempfile.mkdtemp(prefix="mcomp-multivm-")
    try:
        resolution(td)
        ambiguity(td)
        listing(td)
        default_computer(td)
        default_computer_audit(td)
        default_computer_recheck(td)
        default_computer_recheck2(td)
        default_computer_recheck3(td)
        default_computer_recheck4(td)
        creating(td)
        deleting(td)
        audit_fixes(td)
        recheck_fixes(td)
        recheck2_fixes(td)
        recheck3_fixes(td)
        final2_fixes(td)
        final3_fixes(td)
        final4_fixes(td)
        final5_fixes(td)
        final6_fixes(td)
        final7_fixes(td)
        delete_inventory(td)
        reap_inventory(td)
        claude_flow(td)
        hub_pin(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
