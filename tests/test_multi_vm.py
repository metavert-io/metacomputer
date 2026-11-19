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
import select
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []
PY = shutil.which("python3") or sys.executable

FAKE_RUNNER = r'''#!/usr/bin/env python3
import json, os, sys
state, log = os.environ["FAKE_STATE"], os.environ["FAKE_LOG"]
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
    return {"name": name, "status": status, "diskSize": {"allocated": int(gb * 1e9), "total": 53687091200}}


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
            if f in ("local.env", "vms") or f.endswith(".local.env"):
                continue                        # never this Mac's own settings
            (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, os.path.join(self.co, "config", f))
        self.home = os.path.join(self.root, "home")
        write(os.path.join(self.home, ".local", "bin", "mcomp-vm"), FAKE_RUNNER.replace("/usr/bin/env python3", PY), 0o755)
        self.vault = os.path.join(self.root, "vault-stand-in")
        write(self.vault, FAKE_VAULT, 0o755)
        self.state = os.path.join(self.root, "state.json")
        self.log = os.path.join(self.root, "runner.log")
        # Unix socket paths are short on macOS: the per-VM folders go in a short folder of their own.
        self.app = os.path.realpath(tempfile.mkdtemp(prefix="mcmv-", dir="/tmp"))   # canonical, as vaults are handed
        self.set_vms([])
        self.mc = os.path.join(self.co, "bin", "metacomputer")

    def close(self):
        shutil.rmtree(self.app, ignore_errors=True)

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
                 FAKE_STATE=self.state, FAKE_LOG=self.log, MCOMP_APP_DIR=self.app,
                 MCOMP_LOCK_DIR=os.path.join(self.root, "locks"), MCOMP_EVENTS_DIR=os.path.join(self.root, "events"),
                 MCOMP_VAULT_BIN=self.vault, MCOMP_LESSONS_DIR=os.path.join(self.root, "lessons"),
                 MCOMP_STORAGE_HOME=os.path.join(self.root, "storage"))
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
                  '"KIND=$MC_SETTINGS_KIND" "IMPLICIT=$MC_IMPLICIT" "X_CONFIG=${MCOMP_CONFIG:-}" "X_VM=${MCOMP_VM:-}"')
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
              and s.get("RUN_DIR") == f"{co}/.run/vms/demo" and s.get("LOG_DIR") == f"{co}/logs/vms/demo", s)
        check("...and its own vault folder and spool", s.get("VAULT") == f"{app}/vms/demo"
              and s.get("EVENTS_SPOOL") == f"{app}/vms/demo/events/spool", s)
        check("...passed on to what it starts as its settings file (MCOMP_CONFIG), not a name",
              os.path.realpath(s.get("X_CONFIG", "")) == os.path.realpath(demo) and s.get("X_VM") == "", s)
        rc2, s2, _ = b.resolve(MCOMP_VM="demo")
        check("MCOMP_VM=demo: the same", rc2 == 0 and {k: s2.get(k) for k in s} == s, (s2, s))
        rc2, s2, _ = b.resolve("mainvm", MCOMP_VM="demo")
        check("--vm beats MCOMP_VM", rc2 == 0 and s2.get("VM_NAME") == "mainvm", s2)
        rc2, s2, _ = b.resolve(MCOMP_CONFIG=demo)
        check("MCOMP_CONFIG naming a config/vms/ file: the same per-VM settings",
              rc2 == 0 and s2.get("VM_NAME") == "demo" and s2.get("VAULT") == f"{app}/vms/demo", s2)
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
        check("MCOMP_EVENTS_SPOOL overrides a per-VM spool the file doesn't set", rc == 0 and s.get("EVENTS_SPOOL") == os.path.realpath("/tmp") + "/mc-spool-x", s)

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
        b.settings("config/vms/vaulted.env", "SSH_PORT=2406\nVNC_PORT=6106\nVAULT_DIR=/tmp/mc-own-vault\n")
        rc, s, err = b.resolve("vaulted", MCOMP_VAULT_DIR="/tmp/mc-other-vault")
        check("a file's own VAULT_DIR and a different MCOMP_VAULT_DIR: refused", rc == 1 and "name different vault folders" in err, err)
        rc, s, err = b.resolve("vaulted")
        check("...and the file's own VAULT_DIR without it: used", rc == 0 and s.get("VAULT") == os.path.realpath("/tmp") + "/mc-own-vault", s)
        b.settings("config/vms/relvault.env", "SSH_PORT=2407\nVNC_PORT=6107\nVAULT_DIR=vault-here\n")
        rc, s, err = b.resolve("relvault")
        check("a VAULT_DIR that isn't absolute: refused", rc == 1 and "absolute folder" in err, err)

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
              r.returncode == 0 and f"dir={app}/vms/demo " in r.stdout and f"spool={app}/vms/demo/events/spool" in r.stdout, r.stdout + r.stderr)
        lines = r.stderr.splitlines()
        check("...after one line naming the VM, its settings and its vault",
              len(lines) == 1 and "VM demo (config/vms/demo.env), vault" in lines[0] and f"{app}/vms/demo" in lines[0], r.stderr)
        check("...whose parent folder was made private", os.stat(os.path.join(app, "vms")).st_mode & 0o077 == 0)
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
              and os.path.realpath(he.get("MCOMP_RUN_DIR", "")) == os.path.realpath(co) + "/.run/vms/demo"
              and he.get("MCOMP_VM_EVENTS_SPOOL") == f"{app}/vms/demo/events/spool", r.stdout[-300:] + r.stderr)
        sys.path.insert(0, os.path.join(co, "lib"))
        import events
        old = dict(os.environ)
        try:
            os.environ.pop("MCOMP_EVENTS_SPOOL", None)
            os.environ["MCOMP_VM_EVENTS_SPOOL"] = he.get("MCOMP_VM_EVENTS_SPOOL", "")
            check("...and the event stream reads that VM's spool", events.spool_dir() == f"{app}/vms/demo/events/spool", events.spool_dir())
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
              and f"vault     {app}/vms/demo" in r.stdout and f"spool     {app}/vms/demo/events/spool" in r.stdout, r.stdout)
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
              and de.get("checkpoints") == ["one"] and de.get("vault", "").endswith("/vms/demo"), de)
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
        check("...with its own name, baseline, ports, keys, vault and spool",
              all(x in text for x in ("VM_NAME=side", "BASELINE_NAME=side-baseline", "SSH_PORT=2433", "VNC_PORT=6133",
                                      f"SSH_DIR='{b.home}/.ssh/metacomputer-side'", f"VAULT_DIR='{b.app}/vms/side'",
                                      f"EVENTS_SPOOL='{b.app}/vms/side/events/spool'")), text)
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
              r.returncode == 0 and "BUILD VM_NAME=built" in r.stdout and f"RUN_DIR={os.path.realpath(b.co)}/.run/vms/built" in r.stdout
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
        b.settings("config/vms/shared.env", 'SSH_PORT=2404\nVNC_PORT=6104\nVAULT_DIR="/tmp/mc-shared-vault-test"\nVOLUMES="work"\n')
        others = [vm("metacomputer"), vm("metacomputer-baseline"), vm("showcase", "running"), vm("showcase-baseline")]
        b.set_vms(others + [vm("demo"), vm("demo-baseline"), vm("demo-baseline-prev"), vm("runs", "running"), vm("runs-baseline"),
                            vm("ckpt"), vm("ckpt-ckpt-a"), vm("shared")])
        vault = os.path.join(b.app, "vms", "demo")
        os.makedirs(vault)
        write(os.path.join(vault, "vault.json"), "{}")
        rundir = os.path.join(b.co, ".run", "vms", "demo")
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
        r = b.run("vm", "delete", "shared", "--confirm", "shared", "--delete-vault")
        check("--delete-vault for a vault that isn't the VM's own folder: refused before anything", r.returncode == 1
              and "only a VM's own vault folder" in r.stderr and gone() == [], r.stderr)
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
              r.returncode == 0 and not os.path.exists(vault) and os.path.isdir(os.path.join(b.app, "vms"))
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
              and "baseline is named metacomputer" in r.stderr and b.deletes() == [] and "metacomputer" in b.vms(), (r.stderr[-300:], b.deletes()))
        write(legacy, "VM_NAME=legacy\nBASELINE_NAME=legacy-baseline\nSSH_PORT=2391\nVNC_PORT=6091\n")
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

        # 2. A per-VM file can't reach the main VM's folders (or another's) by another spelling of the same path.
        os.makedirs(os.path.join(home, ".ssh", "metacomputer"), exist_ok=True)
        os.makedirs(os.path.join(co, ".run"), exist_ok=True)
        os.symlink(os.path.join(co, ".run"), os.path.join(root, "runlink"))
        for n, line, why in (("al1", 'SSH_DIR="$HOME/.ssh/metacomputer/."', "SSH_DIR with /. at the end"),
                             ("al2", 'SSH_DIR="$HOME/.ssh//metacomputer"', "SSH_DIR with //"),
                             ("al3", f'RUN_DIR="{root}/runlink"', "RUN_DIR through a link"),
                             ("al4", f'LOG_DIR="{co}/.run"', "LOG_DIR that is the main RUN_DIR")):
            b.settings(f"config/vms/{n}.env", f"SSH_PORT=24{n[-1]}0\nVNC_PORT=61{n[-1]}0\n{line}\n")
            rc, s, err = b.resolve(n)
            check(f"a per-VM {why} (the main VM's folder): refused", rc == 1 and "is the same folder as the main VM" in err, err)
            drop(os.path.join(co, f"config/vms/{n}.env"))
        os.makedirs(os.path.join(root, "shared-logs"))
        os.symlink(os.path.join(root, "shared-logs"), os.path.join(root, "logs-alias"))
        b.settings("config/vms/pa.env", f'SSH_PORT=2451\nVNC_PORT=6151\nLOG_DIR="{root}/shared-logs"\n')
        b.settings("config/vms/pb.env", f'SSH_PORT=2452\nVNC_PORT=6152\nLOG_DIR="{root}/logs-alias"\n')
        rc, s, err = b.resolve("pb")
        check("two per-VM files whose folders are one through a link: refused", rc == 1
              and "is the same folder as config/vms/pa.env's LOG_DIR" in err, err)
        for n in ("pa", "pb"):
            drop(os.path.join(co, f"config/vms/{n}.env"))

        # 3. Recursive deletion goes through no link and never takes another VM's folder.
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        elsewhere = os.path.join(root, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "demo"))
        write(os.path.join(elsewhere, "demo", "keep.txt"), "not demo's")
        os.makedirs(os.path.join(co, ".run"), exist_ok=True)
        shutil.rmtree(os.path.join(co, ".run", "vms"), ignore_errors=True)   # runtime folders earlier steps made
        os.symlink(elsewhere, os.path.join(co, ".run", "vms"))
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        r = b.run("vm", "delete", "demo", "--confirm", "demo")
        check("a runtime folder reached through a linked .run/vms: refused before anything is deleted", r.returncode == 1
              and "is a link" in r.stderr and b.deletes() == [] and os.path.exists(os.path.join(elsewhere, "demo", "keep.txt")),
              (r.stderr[-300:], b.deletes()))
        os.remove(os.path.join(co, ".run", "vms"))
        os.makedirs(os.path.join(co, ".run", "vms", "demo"))
        appvms = os.path.join(b.app, "vms")
        os.makedirs(os.path.join(root, "elsewhere2", "demo"))
        write(os.path.join(root, "elsewhere2", "demo", "vault.json"), "{}")
        os.symlink(os.path.join(root, "elsewhere2"), appvms)
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("--delete-vault through a linked vms/ folder: refused before anything is deleted", r.returncode == 1
              and b.deletes() == [] and os.path.exists(os.path.join(root, "elsewhere2", "demo", "vault.json")), (r.stderr[-300:], b.deletes()))
        os.remove(appvms)
        os.makedirs(os.path.join(appvms, "demo", "inner"))
        b.settings("config/vms/nested.env", f'SSH_PORT=2461\nVNC_PORT=6161\nVAULT_DIR="{appvms}/demo/inner"\n')
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("--delete-vault when another VM's vault is inside demo's: refused, both kept", r.returncode == 1
              and "is or holds" in r.stderr and b.deletes() == [] and os.path.isdir(os.path.join(appvms, "demo", "inner")),
              (r.stderr[-300:], b.deletes()))
        b.settings("config/vms/nested.env", f'SSH_PORT=2461\nVNC_PORT=6161\nVAULT_DIR="{appvms}/demo"\n')
        rc, s, err = b.resolve("demo")
        check("another VM whose VAULT_DIR is demo's own folder: demo refused (and so its delete)", rc == 1
              and "is the same folder as config/vms/nested.env's VAULT_DIR" in err, err)
        drop(os.path.join(co, "config/vms/nested.env"))

        # P2: vault folders as the folders they are; the environment's absolute too.
        os.makedirs(os.path.join(root, "mainvault"))
        b.settings("config/local.env", f'VM_NAME=metacomputer\nVAULT_DIR="{root}/mainvault"\n')
        os.symlink(os.path.join(root, "mainvault"), os.path.join(root, "vaultlink"))
        b.settings("config/vms/sym.env", f'SSH_PORT=2471\nVNC_PORT=6171\nVAULT_DIR="{root}/vaultlink"\n')
        r = b.run("--vm", "sym", "secrets", "lock")
        check("a per-VM vault that is a link to the main VM's: refused (no client reaches the main vault)", r.returncode == 1
              and "VAULT_DIR" in r.stderr and "VAULT " not in r.stdout, (r.stdout, r.stderr[-300:]))
        drop(os.path.join(co, "config/vms/sym.env"))
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
        h = hold_lock(os.path.join(co, ".run", "vms", "demo", "owners", ".lock"))
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
        appvms = os.path.join(b.app, "vms")
        inner = os.path.join(appvms, "demo", "inner")
        os.makedirs(inner)
        write(os.path.join(inner, "vault.json"), "{}")
        # P1: a settings file refused for an unrelated reason still protects its folders (and names) from deletion.
        b.settings("config/vms/other.env", f'SSH_PORT=2402\nVNC_PORT=not-a-port\nVAULT_DIR="{inner}"\n')
        rc, s, err = b.resolve("other")
        check("(setup) the other VM's settings are refused, for its port", rc == 1 and "VNC_PORT" in err, err)
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("vm delete --delete-vault when a refused file's vault is inside demo's: refused, both kept", r.returncode == 1
              and "is or holds" in r.stderr and os.path.exists(os.path.join(inner, "vault.json")) and b.deletes() == [],
              (r.stderr[-300:], b.deletes()))
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        os.makedirs(inner, exist_ok=True)
        write(os.path.join(inner, "vault.json"), "{}")
        b.set_vms(main + [vm("demo"), vm("demo-baseline")])
        b.settings("config/vms/other.env", 'SSH_PORT=2402\nVNC_PORT=not-a-port\nVAULT_DIR="relative/vault"\n')
        r = b.run("vm", "delete", "demo", "--confirm", "demo", "--delete-vault")
        check("...and when a refused file's folder can't be placed (not absolute): refused, nothing deleted", r.returncode == 1
              and "isn't an absolute path" in r.stderr and os.path.exists(os.path.join(inner, "vault.json")) and b.deletes() == [],
              (r.stderr[-300:], b.deletes()))
        drop(os.path.join(co, "config/vms/other.env"))
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
              and "is the same folder as the main VM" in r.stderr and "VAULT " not in r.stdout, (r.stdout, r.stderr[-300:]))
        b.settings("config/local.env", "VM_NAME=metacomputer\n")

        # P2: a delete refused after the prompt leaves what runs for the VM running.
        b.settings("config/vms/demo.env", "SSH_PORT=2401\nVNC_PORT=6101\n")
        rundir = os.path.join(co, ".run", "vms", "demo")
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
        creating(td)
        deleting(td)
        audit_fixes(td)
        recheck_fixes(td)
        hub_pin(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
