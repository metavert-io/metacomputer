#!/usr/bin/env python3
"""metacomputer version and update, and the step every install ends with (lib/update.py), no VM: a temporary
repository with three releases, a checkout of it with a stand-in install.sh and bin/metacomputer. `update --check`
says what's new and changes nothing; `update` moves main forward to the latest release (or a detached checkout to
it) and runs install.sh; local changes, another branch and going back are refused. The after-install step: the hub
and the watcher on older code are stopped (and the watcher started), a VM on an earlier gateway is restarted only
when allowed, a vault is asked to be bound again when the gateway changed, a VM built from other guest files gets a
rebuild note, and what's installed is recorded. status's update line comes from a daily check."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
TD = tempfile.mkdtemp(prefix="mcomp-update-")
HOME = os.path.join(TD, "home")
os.makedirs(HOME)
GENV = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "HOME": HOME, "GIT_ALLOW_PROTOCOL": "file"}
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"   [{str(detail)[-500:]}]"))


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], env=GENV, cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


STUB_CLI = r'''#!/bin/bash
echo "$*" >> "$STUB_LOG"
vm=main; [ "$1" = --vm ] && { vm="$2"; shift 2; }
case "$1" in
  _runinfo) cat "$STUB_RUNINFO/$vm.json" ;;
  stop|start|hub|events) [ -z "${STUB_FAIL_STOP:-}" ] || [ "$1" != stop ] || { echo "the VM is in use by another controller"; exit 1; } ;;
esac
exit 0
'''
STUB_INSTALL = '#!/bin/bash\necho "install $* restart=${MCOMP_UPDATE_RESTART:-}" >> "$STUB_LOG"\n'


def changelog(versions):
    out = "# Changelog\n\n## [Unreleased]\n\n"
    for v in reversed(versions):
        out += f"## [{v}] - 2026-11-0{versions.index(v) + 1}\n\n### Added\n\n- **Feature of {v}**: details.\n- **Second thing in {v}** too.\n\n"
    return out


origin = os.path.join(TD, "origin")
os.makedirs(origin)
git("init", "-q", "-b", "main", cwd=origin)
for i, v in enumerate(["0.1.0", "0.2.0", "0.3.0"]):
    shutil.copy(os.path.join(LAB, "lib", "update.py"), os.path.join(origin, "lib", "update.py")) if i else None
    if i == 0:
        os.makedirs(os.path.join(origin, "lib"))
        shutil.copy(os.path.join(LAB, "lib", "update.py"), os.path.join(origin, "lib", "update.py"))
        write(os.path.join(origin, "bin", "metacomputer"), STUB_CLI, 0o755)
        write(os.path.join(origin, "install.sh"), STUB_INSTALL, 0o755)
        write(os.path.join(origin, "provisioning", "guest", "provision.sh"), "echo guest\n")
    write(os.path.join(origin, "CHANGELOG.md"), changelog(["0.1.0", "0.2.0", "0.3.0"][:i + 1]))
    git("add", "-A", cwd=origin)
    git("commit", "-qm", f"release {v}", cwd=origin)
    git("tag", f"v{v}", cwd=origin)
tags = {v: git("rev-parse", f"v{v}^{{commit}}", cwd=origin) for v in ("0.1.0", "0.2.0", "0.3.0")}
LOG = os.path.join(TD, "calls.log")
RUNINFO = os.path.join(TD, "runinfo")
os.makedirs(RUNINFO)


def fresh(at="0.1.0", detached=False, branch=None):
    d = tempfile.mkdtemp(prefix="co-", dir=TD)
    git("clone", "-q", origin, d, cwd=TD)
    if detached:
        git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", f"v{at}", cwd=d)
    else:
        git("reset", "-q", "--hard", f"v{at}", cwd=d)
    if branch:
        git("checkout", "-q", "-b", branch, cwd=d)
    return d


def upd(co, *args, **env):
    if os.path.exists(LOG):
        os.remove(LOG)
    r = subprocess.run([sys.executable, os.path.join(co, "lib", "update.py"), *args], capture_output=True, text=True,
                       timeout=120, stdin=subprocess.DEVNULL, start_new_session=True,
                       env=dict(GENV, STUB_LOG=LOG, STUB_RUNINFO=RUNINFO, **env))
    calls = open(LOG).read().splitlines() if os.path.exists(LOG) else []
    return r.returncode, r.stdout + r.stderr, calls


def runinfo(vm, **kw):
    base = {"vm": vm, "state": "off", "run_dir": os.path.join(TD, "run-" + vm), "netgw_bin": "/x/vendor/bin/mcomp-netgw",
            "gateway": None, "hub": None, "watcher": None, "vault_dir": "", "kind": "", "vms": [], "vault_exists": False,
            "guest_digest": "g1", "build": None, "exists": True}
    base.update(kw)
    os.makedirs(base["run_dir"], exist_ok=True)
    write(os.path.join(RUNINFO, vm + ".json"), json.dumps(base) + "\n")


runinfo("main")
# --- update --check, update -----------------------------------------------------------------------------------
co = fresh("0.1.0")
rc, out, calls = upd(co, "update", "--check")
check("update --check: from v0.1.0 to the latest, v0.3.0, with what's new in 0.2.0 and 0.3.0, nothing changed",
      rc == 0 and "v0.1.0 → v0.3.0" in out and "0.2.0: Feature of 0.2.0; Second thing in 0.2.0" in out
      and "0.3.0:" in out and "0.1.0:" not in out and git("rev-parse", "HEAD", cwd=co) == tags["0.1.0"] and not calls, (out, calls))
rc, out, _ = upd(co, "update", "--check", "--json")
j = json.loads(out)
check("update --check --json", j["current"] == "v0.1.0" and j["latest"] == "v0.3.0" and j["up_to_date"] is False
      and [w["version"] for w in j["whats_new"]] == ["0.3.0", "0.2.0"], j)
rc, out, calls = upd(co, "update")
check("update: main moves forward to v0.3.0 (still on main) and install.sh runs (--yes --no-build, asking before a restart)",
      rc == 0 and git("rev-parse", "HEAD", cwd=co) == tags["0.3.0"] and git("symbolic-ref", "--short", "HEAD", cwd=co) == "main"
      and calls == ["install --yes --no-build restart=ask"], (out, calls))
co2 = fresh("0.1.0")
rc, out, calls = upd(co2, "update", "--restart")
check("update --restart: install.sh is told to restart without asking", calls == ["install --yes --no-build restart=yes"], calls)
co3 = fresh("0.1.0", detached=True)
rc, out, calls = upd(co3, "update")
check("a detached checkout (as the install line leaves it) moves to the release, detached",
      rc == 0 and git("rev-parse", "HEAD", cwd=co3) == tags["0.3.0"] and subprocess.run(
          ["git", "symbolic-ref", "-q", "HEAD"], cwd=co3, env=GENV).returncode != 0, out)
co4 = fresh("0.1.0")
with open(os.path.join(co4, "CHANGELOG.md"), "a") as f:
    f.write("my edit\n")
rc, out, calls = upd(co4, "update")
check("local changes: refused (exit 3), nothing moved or installed", rc == 3 and "local changes" in out
      and git("rev-parse", "HEAD", cwd=co4) == tags["0.1.0"] and not calls, (rc, out))
co5 = fresh("0.1.0", branch="mywork")
rc, out, calls = upd(co5, "update")
check("another branch: refused", rc == 3 and "branch mywork" in out and not calls, out)
co6 = fresh("0.3.0")
rc, out, calls = upd(co6, "update", "--ref", "v0.2.0")
check("going back (--ref an older release): refused", rc == 3 and "older" in out and not calls, out)
rc, out, calls = upd(co6, "update", "--ref", "v0.2.0", "--allow-downgrade")
check("…and on main it can't be moved back even with --allow-downgrade (main only goes forward)", rc == 3
      and git("rev-parse", "HEAD", cwd=co6) == tags["0.3.0"] and not calls, out)

# --- the after-install step ------------------------------------------------------------------------------------
DRIVER = r'''
import json, sys, time
sys.path.insert(0, sys.argv[1] + "/lib")
import update
update.tty = lambda: None
res = update.after_install(restart=sys.argv[2])
print("RESULT " + json.dumps(res))
'''


def after(co, restart="no"):
    if os.path.exists(LOG):
        os.remove(LOG)
    r = subprocess.run([sys.executable, "-c", DRIVER, co, restart], capture_output=True, text=True, timeout=120,
                       env=dict(GENV, STUB_LOG=LOG, STUB_RUNINFO=RUNINFO), stdin=subprocess.DEVNULL, start_new_session=True)
    calls = open(LOG).read().splitlines() if os.path.exists(LOG) else []
    res = json.loads(r.stdout.split("RESULT ", 1)[1]) if "RESULT " in r.stdout else {}
    return r.stdout + r.stderr, calls, res


co = fresh("0.3.0")
os.makedirs(os.path.join(co, "vendor", "bin"))
write(os.path.join(co, "vendor", "bin", "mcomp-netgw"), "new gateway\n", 0o755)
long_ago = time.time() - 30 * 86400
stamp = os.path.join(HOME, ".local", "share", "metacomputer", "installed.json")
write(stamp, json.dumps({"version": "v0.1.0", "tag": "v0.1.0", "commit": tags["0.1.0"],
                         "gateway": {"program": "/old/cua-netgw", "sha": "old"}}))
runinfo("main", state="running", vms=["b"], vault_exists=True, netgw_bin=os.path.join(co, "vendor", "bin", "mcomp-netgw"),
        gateway={"pid": 1, "started": long_ago, "program": "/old/vendor/bin/cua-netgw"},
        hub={"pid": 2, "started": long_ago, "program": "python3"}, watcher={"pid": 3, "started": long_ago, "program": "python3"})
runinfo("b", state="running", netgw_bin=os.path.join(co, "vendor", "bin", "mcomp-netgw"),
        gateway={"pid": 4, "started": time.time() + 60, "program": os.path.join(co, "vendor", "bin", "mcomp-netgw")},
        hub={"pid": 5, "started": time.time() + 60, "program": "python3"}, build={"guest": "g1", "version": "v0.3.0"})
out, calls, res = after(co, "no")
check("the hub and the watcher on older code: hub stopped, watcher stopped and started; the current VM b left alone",
      "hub stop" in calls and "events watcher stop" in calls and "events watcher start" in calls
      and not any(c.startswith("--vm b hub") for c in calls), calls)
check("a VM on an earlier gateway, restarts not allowed: not stopped, and told how", "stop" not in calls
      and any("restart the VM to run the new gateway" in t for t in res.get("todo", [])), (calls, res))
check("the gateway changed and a vault exists: told to bind it again", any("secrets bind" in t for t in res.get("todo", [])), res)
check("a VM with no build record: a rebuild note; one built from the same guest files: none",
      res.get("rebuild") == ["main"], res)
st = json.load(open(stamp))
check("what's installed is recorded (version, commit, the gateway program)", st["version"] == "v0.3.0"
      and st["commit"] == tags["0.3.0"] and st["gateway"]["program"].endswith("mcomp-netgw") and st["gateway"]["sha"], st)
check("…and the update is summed up with what's new since v0.1.0", "Updated: v0.1.0 → v0.3.0" in out and "0.2.0:" in out, out)
write(stamp, json.dumps({"version": "v0.1.0", "tag": "v0.1.0", "commit": tags["0.1.0"],
                         "gateway": {"program": "/old/cua-netgw", "sha": "old"}}))
out, calls, res = after(co, "yes")
check("restarts allowed (update --restart): the VM is stopped and started", "stop" in calls and "start" in calls
      and any("restarted on the new gateway" in t for t in res.get("done", [])), (calls, res))
out, calls, res = after(co, "yes")
check("the next run, nothing changed: no bind request (the gateway is the one recorded)",
      not any("secrets bind" in t for t in res.get("todo", [])), res)
out, calls, res = (lambda: (None, None, None))()
r = subprocess.run([sys.executable, "-c", DRIVER, co, "yes"], capture_output=True, text=True, timeout=120,
                   env=dict(GENV, STUB_LOG=LOG, STUB_RUNINFO=RUNINFO, STUB_FAIL_STOP="1"), stdin=subprocess.DEVNULL)
res = json.loads(r.stdout.split("RESULT ", 1)[1])
check("a VM another session is using (stop refuses): not restarted, and that's reported, with what to run later",
      any("not restarted" in t and "stop && metacomputer start" in t for t in res.get("todo", [])), res)

# --- status's update line ----------------------------------------------------------------------------------------
SL = r'''
import sys, subprocess
sys.path.insert(0, sys.argv[1] + "/lib")
import update
started = []
update.spawn_refresh = lambda: started.append(1)
update.status_line()
print("STARTED", len(started))
'''
cache = os.path.join(HOME, ".cache", "metacomputer", "update-check.json")


def status(co, **env):
    r = subprocess.run([sys.executable, "-c", SL, co], capture_output=True, text=True, timeout=60, env=dict(GENV, **env))
    return r.stdout + r.stderr


old_co = fresh("0.1.0")
write(cache, json.dumps({"checked": time.time(), "latest": "v0.3.0"}))
out = status(old_co)
check("status: a newer release out, from today's check", "v0.3.0 is out (this is v0.1.0): metacomputer update" in out
      and "STARTED 0" in out, out)
out = status(co)
check("…nothing when up to date", "is out" not in out, out)
out = status(old_co, MCOMP_UPDATE_CHECK="off")
check("…and nothing with UPDATE_CHECK=off", "is out" not in out and "STARTED 0" in out, out)
write(cache, json.dumps({"checked": time.time() - 2 * 86400, "latest": "v0.3.0"}))
out = status(old_co)
check("a check older than a day: a new one starts in the background (status doesn't wait for it)", "STARTED 1" in out, out)

src = open(os.path.join(LAB, "lib", "update.py")).read()
check("update never moves main backwards (fast-forward only)", '"merge", "-q", "--ff-only"' in src)
shutil.rmtree(TD, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
