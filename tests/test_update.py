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
  hub) [ "$2 $3" = "stop --if-idle" ] && [ -n "${STUB_HUB_CLIENTS:-}" ] && { echo "hub not stopped: 1 client(s) connected"; exit 3; } ;;
  stop|start|events) [ -z "${STUB_FAIL_STOP:-}" ] || [ "$1" != stop ] || { echo "the VM is in use by another controller"; exit 1; }
     [ "$1" = events ] || echo "expect=${MCOMP_EXPECT_VM:-}:${MCOMP_EXPECT_CONFIG_SHA:-}" >> "$STUB_LOG" ;;
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
        shutil.copy(os.path.join(LAB, "lib", "strictargs.py"), os.path.join(origin, "lib", "strictargs.py"))   # its parser
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
check("going back (--ref an older release): refused", rc == 3 and "isn't ahead" in out and "--allow-downgrade" in out
      and not calls, out)
check("…on main, the refusal says how to go back (check out the tag, then install.sh), not --allow-downgrade alone",
      "main only moves forward" in out and "git checkout v0.2.0" in out and "./install.sh --allow-downgrade" in out, out)
rc, out, calls = upd(co6, "update", "--ref", "v0.2.0", "--allow-downgrade")
check("…and on main it can't be moved back even with --allow-downgrade (main only goes forward)", rc == 3
      and git("rev-parse", "HEAD", cwd=co6) == tags["0.3.0"] and not calls, out)
# Past the newest release: main pulled from the repository after v0.3.0 (its own origin, so the others' isn't changed).
past = tempfile.mkdtemp(prefix="origin-past-", dir=TD)
git("clone", "-q", origin, past, cwd=TD)
write(os.path.join(past, "NOTES.md"), "after 0.3.0\n")
git("add", "-A", cwd=past)
git("commit", "-qm", "after 0.3.0", cwd=past)
co8 = tempfile.mkdtemp(prefix="co-", dir=TD)
git("clone", "-q", past, co8, cwd=TD)
head8 = git("rev-parse", "HEAD", cwd=co8)
rc, out, calls = upd(co8, "update")
check("past the newest release on the repository's main: nothing to do (exit 0), not a way back, nothing moved or run",
      rc == 0 and "past the newest release, v0.3.0" in out and "./install.sh" in out and "allow-downgrade" not in out
      and git("rev-parse", "HEAD", cwd=co8) == head8 and not calls, (rc, out, calls))
rc, out, _ = upd(co8, "update", "--check", "--json")
j = json.loads(out)
check("…update --check --json: up to date, past the latest", rc == 0 and j["latest"] == "v0.3.0" and j["up_to_date"] is True
      and j["past_latest"] is True and j["whats_new"] == [], j)
rc, out, calls = upd(co8, "update", "--ref", "v0.3.0")
check("…but asking for that release by name is going back: refused", rc == 3 and "main only moves forward" in out
      and git("rev-parse", "HEAD", cwd=co8) == head8 and not calls, out)
rc, out, calls = upd(co8, "update", "--allow-downgrade")
check("…and --allow-downgrade on main: still refused, main only moves forward (nothing moved or run)", rc == 3
      and "main only moves forward" in out and git("rev-parse", "HEAD", cwd=co8) == head8 and not calls, (rc, out, calls))
# The same commit, detached (an upstream commit checked out to try it): nothing newer without --allow-downgrade; with
# it, back to the release, detached, and install.sh runs with --allow-downgrade.
co10 = tempfile.mkdtemp(prefix="co-", dir=TD)
git("clone", "-q", past, co10, cwd=TD)
git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "HEAD", cwd=co10)
rc, out, calls = upd(co10, "update")
check("past the newest release, detached at a commit of the repository's main: nothing newer (exit 0), nothing moved "
      "or run", rc == 0 and "past the newest release, v0.3.0" in out and git("rev-parse", "HEAD", cwd=co10) == head8
      and not calls, (rc, out, calls))
rc, out, calls = upd(co10, "update", "--allow-downgrade")
check("…with --allow-downgrade: back to v0.3.0, detached, and install.sh runs with --allow-downgrade",
      rc == 0 and git("rev-parse", "HEAD", cwd=co10) == tags["0.3.0"] and subprocess.run(
          ["git", "symbolic-ref", "-q", "HEAD"], cwd=co10, env=GENV).returncode != 0
      and calls == ["install --yes --no-build --allow-downgrade restart=ask"], (rc, out, calls))
# The repository can't be reached: refused at the fetch, nothing moved or run.
co12 = fresh("0.1.0")
git("remote", "set-url", "origin", os.path.join(TD, "gone"), cwd=co12)
rc, out, calls = upd(co12, "update")
check("the fetch fails (the repository can't be reached): refused (exit 3), nothing moved or run", rc == 3
      and "git fetch" in out and git("rev-parse", "HEAD", cwd=co12) == tags["0.1.0"] and not calls, (rc, out, calls))
co9 = fresh("0.3.0")
write(os.path.join(co9, "MINE.md"), "my commit\n")
git("add", "-A", cwd=co9)
git("commit", "-qm", "mine", cwd=co9)
head9 = git("rev-parse", "HEAD", cwd=co9)
rc, out, calls = upd(co9, "update")
check("past the newest release with commits of its own: refused as the owner's to move, not as a way back",
      rc == 3 and "commits of its own after v0.3.0" in out and "allow-downgrade" not in out
      and git("rev-parse", "HEAD", cwd=co9) == head9 and not calls, out)
# An install whose Claude Code step didn't finish (install.sh hands MCOMP_INSTALL_INCOMPLETE=claude to the after-install
# step) is recorded as such, and `update` runs install.sh again although the checkout is at the release.
co7 = fresh("0.3.0")
stamp7 = os.path.join(HOME, ".local", "share", "metacomputer", "installed.json")
write(stamp7, json.dumps({"version": "v0.3.0", "tag": "v0.3.0", "commit": tags["0.3.0"], "checkout": co7}))
rc, out, calls = upd(co7, "update")
check("up to date and installed from here: install.sh isn't run again", rc == 0
      and not [c for c in calls if c.startswith("install ")], (out, calls))
write(stamp7, json.dumps({"version": "v0.3.0", "tag": "v0.3.0", "commit": tags["0.3.0"], "checkout": co7,
                          "incomplete": ["claude"]}))
rc, out, calls = upd(co7, "update")
check("…but when that install left Claude Code's setup unfinished, update runs install.sh again",
      rc == 0 and "install --yes --no-build restart=ask" in calls, (out, calls))
rc, out, _ = upd(co7, "version")
check("…and `metacomputer version` says so", "setup didn't finish: metacomputer update runs the install again" in out, out)
for env, want in (({"MCOMP_INSTALL_INCOMPLETE": "claude"}, ["claude"]), ({}, None)):
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1] + '/lib'); import update; "
                        "update.tty = lambda: None; update.after_install(restart='no', quiet=True)", co7],
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, start_new_session=True,
                       env=dict(GENV, STUB_LOG=LOG, STUB_RUNINFO=RUNINFO, **env))
    got = json.load(open(stamp7)).get("incomplete")
    check(f"the after-install step records {'an unfinished' if want else 'a finished'} Claude Code setup "
          f"({'incomplete: claude' if want else 'no incomplete key'})", got == want, (got, r.stdout[-300:], r.stderr[-300:]))

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
NETGW = os.path.join(co, "vendor", "bin", "mcomp-netgw")
import hashlib  # noqa: E402
GW_SHA = hashlib.sha256(open(NETGW, "rb").read()).hexdigest()
CODE = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {os.path.join(co, 'lib')!r}); import update; "
                       "print(update.code_digest())"], capture_output=True, text=True, env=GENV).stdout.strip()


def old_stamp():
    write(stamp, json.dumps({"version": "v0.1.0", "tag": "v0.1.0", "commit": tags["0.1.0"], "checkout": co,
                             "gateway": {"program": "/old/cua-netgw", "sha": "old"}}))


old_stamp()
runinfo("main", state="running", vms=["b"], vault_exists=True, netgw_bin=NETGW, config_sha="cfgmain",
        gateway={"pid": 1, "started": long_ago, "command": "/old/vendor/bin/cua-netgw -config /x/gw.json"},
        gateway_started={}, hub={"pid": 2, "started": long_ago, "command": "python3 mcp_hub.py", "code": "older"},
        watcher={"pid": 3, "started": long_ago, "command": "python3 events.py watch"})
runinfo("b", state="running", netgw_bin=NETGW, vault_exists=False, config_sha="cfgb",
        gateway={"pid": 4, "started": time.time(), "command": NETGW + " -config /y/gw.json"},
        gateway_started={"bin": NETGW, "sha256": GW_SHA}, hub={"pid": 5, "started": time.time(), "command": "x", "code": "older"},
        build={"guest": "g1", "version": "v0.3.0"})
out, calls, res = after(co, "no")
check("an idle hub on older code, on a VM whose gateway is current (b): stopped; the watcher on older code: restarted",
      "--vm b hub stop --if-idle" in calls and "--vm b hub stop" not in calls and "events watcher stop" in calls and "events watcher start" in calls, calls)
check("…but main's hub, whose VM needs a restart anyway, is left to that restart (it stops the hub after its own "
      "owners check)", "hub stop --if-idle" not in calls and "hub stop" not in calls, calls)
check("a VM on an earlier gateway, restarts not allowed: not stopped, and told how", "stop" not in calls
      and any("restart the VM to run the new gateway" in t for t in res.get("todo", [])), (calls, res))
check("the gateway changed and a vault exists: told to bind it again", any("secrets bind" in t for t in res.get("todo", [])), res)
check("a VM with no build record: a rebuild note; one built from the same guest files: none",
      res.get("rebuild") == ["main"], res)
st = json.load(open(stamp))
check("what's installed is recorded (version, commit, the gateway program)", st["version"] == "v0.3.0"
      and st["commit"] == tags["0.3.0"] and st["gateway"]["program"].endswith("mcomp-netgw") and st["gateway"]["sha"], st)
check("…and the update is summed up with what's new since v0.1.0", "Updated: v0.1.0 → v0.3.0" in out and "0.2.0:" in out, out)
old_stamp()
out, calls, res = after(co, "yes")
check("restarts allowed (update --restart): the VM is stopped and started", "stop" in calls and "start" in calls
      and any("restarted on the new gateway" in t for t in res.get("done", [])), (calls, res))
check("…pinned to the VM and settings file it inspected (stop and start refuse any other: expect_guard)",
      calls.count("expect=main:cfgmain") == 2, calls)
os.environ["STUB_HUB_CLIENTS"] = '[{"kind": "session"}]'
out, calls, res = (None, None, None)
os.remove(LOG) if os.path.exists(LOG) else None
r = subprocess.run([sys.executable, "-c", DRIVER, co, "no"], capture_output=True, text=True, timeout=120,
                   env=dict(GENV, STUB_LOG=LOG, STUB_RUNINFO=RUNINFO, STUB_HUB_CLIENTS='[{"kind": "session"}]'),
                   stdin=subprocess.DEVNULL)
res = json.loads(r.stdout.split("RESULT ", 1)[1])
calls = open(LOG).read().splitlines()
check("a hub on older code that a session is using: only the guarded stop is asked for (the hub refuses it: exit 3), "
      "and said so", "--vm b hub stop --if-idle" in calls and "--vm b hub stop" not in calls
      and any("is in use (1 client(s) connected)" in t for t in res.get("todo", [])), (calls, res))
del os.environ["STUB_HUB_CLIENTS"]
sys.path.insert(0, os.path.join(LAB, "lib"))
import mcp_hub  # noqa: E402
idle = {"pid": 9, "clients": [], "driver": {"connected": True, "pending": 0, "left_running": 0}, "recordings": 0, "idle_s": 40}
check("`hub stop --if-idle`: idle = no client, no call pending or left running, no recording, unused for a few seconds",
      mcp_hub.idle_reason(idle) is None
      and mcp_hub.idle_reason(dict(idle, clients=[{"kind": "claude-code"}])) == "1 client(s) connected"
      and mcp_hub.idle_reason(dict(idle, driver={"pending": 1, "left_running": 0})) == "a call is still running"
      and mcp_hub.idle_reason(dict(idle, driver={"pending": 0, "left_running": 2})) == "a call is still running"
      and mcp_hub.idle_reason(dict(idle, recordings=1)) == "a recording is running"
      and mcp_hub.idle_reason(dict(idle, idle_s=1)) == "it was used moments ago", idle)
check("…and a report it can't read is never taken for idle", all(mcp_hub.idle_reason(x) for x in
      (None, {}, {"clients": None}, dict(idle, driver=None), dict(idle, idle_s="40"), dict(idle, recordings=None))))
runinfo("main", state="running", vms=["b"], vault_exists=True, netgw_bin=NETGW,
        gateway={"pid": 1, "started": time.time(), "command": NETGW + " -config /x/gw.json"},
        gateway_started={"bin": NETGW, "sha256": GW_SHA}, hub={"pid": 2, "started": time.time(), "command": "x", "code": CODE})
out, calls, res = after(co, "no")
check("the next run, everything current: nothing restarted, but the bind reminder stays until it's done (or 30 days)",
      "stop" not in calls and "hub stop" not in calls and "hub stop --if-idle" not in calls
      and res.get("bind_pending") == ["main"]
      and any("secrets bind" in t for t in res.get("todo", [])), (calls, res))
st = json.load(open(stamp))
st["bind_pending"] = {"main": time.time() - 31 * 86400}
write(stamp, json.dumps(st))
out, calls, res = after(co, "no")
check("…and a reminder older than 30 days lapses", res.get("bind_pending") == [], res)
write(stamp, json.dumps({"version": "v0.1.0", "tag": "v0.1.0", "commit": tags["0.1.0"], "checkout": "/somewhere/else",
                         "gateway": {"program": NETGW, "sha": GW_SHA}}))
out, calls, res = after(co, "no")
check("another checkout's record says nothing about this one: no 'Updated from' it", "Updated:" not in out, out)
out, calls, res = (lambda: (None, None, None))()
runinfo("main", state="running", vms=[], vault_exists=False, netgw_bin=NETGW,
        gateway={"pid": 1, "started": long_ago, "command": "/old/cua-netgw -config /x/gw.json"}, gateway_started={})
r = subprocess.run([sys.executable, "-c", DRIVER, co, "yes"], capture_output=True, text=True, timeout=120,
                   env=dict(GENV, STUB_LOG=LOG, STUB_RUNINFO=RUNINFO, STUB_FAIL_STOP="1"), stdin=subprocess.DEVNULL)
res = json.loads(r.stdout.split("RESULT ", 1)[1])
import fcntl  # noqa: E402
lk = os.open(os.path.join(fresh_lock_co := fresh("0.1.0"), ".git", "metacomputer-update.lock"), os.O_RDWR | os.O_CREAT)
fcntl.flock(lk, fcntl.LOCK_EX)
rc2, out2, calls2 = upd(fresh_lock_co, "update")
os.close(lk)
check("two updates of one checkout at once: the second is refused (nothing moved)", rc2 == 3 and "another update" in out2
      and git("rev-parse", "HEAD", cwd=fresh_lock_co) == tags["0.1.0"], out2)
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
low = fresh("0.1.0")
r = subprocess.run([sys.executable, os.path.join(low, "lib", "update.py"), "update", "--check"], capture_output=True, text=True,
                   env=dict(GENV, STUB_LOG=LOG, STUB_RUNINFO=RUNINFO, GIT_DIR=os.path.join(co3, ".git"),
                            GIT_WORK_TREE=co3), timeout=60)
check("the caller's GIT_DIR / GIT_WORK_TREE (another checkout, at v0.3.0) don't point it at another repository",
      "v0.1.0 → v0.3.0" in r.stdout, r.stdout + r.stderr)
tag_branch = fresh("0.3.0")
git("checkout", "-q", "-b", "side", cwd=origin)
write(os.path.join(origin, "side.txt"), "x\n")
git("add", "-A", cwd=origin)
git("commit", "-qm", "side", cwd=origin)
git("tag", "v0.9.0", cwd=origin)
git("checkout", "-q", "main", cwd=origin)
rc, out, calls = upd(tag_branch, "update", "--check")
check("a release tag that isn't on main (a side branch's v0.9.0) isn't taken as the latest", "up to date" in out
      and "v0.9.0" not in out, out)
rc, out, calls = upd(tag_branch, "update", "--ref", "v0.9.0")
check("…nor accepted with --ref", rc == 3 and "isn't on the repository's main branch" in out and not calls, out)
rc, out, calls = upd(tag_branch, "update", "--ref", "main")
check("--ref takes only a release tag", rc == 3 and "release tag" in out, out)
# The same off-main tag, seen from a detached checkout past the newest release on main: not taken as newer, and not
# a place to go back to either.
git("fetch", "-q", "origin", "refs/tags/v0.9.0:refs/tags/v0.9.0", cwd=past)
co11 = tempfile.mkdtemp(prefix="co-", dir=TD)
git("clone", "-q", past, co11, cwd=TD)
git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "HEAD", cwd=co11)
rc, out, calls = upd(co11, "update", "--check")
check("past the newest release with an off-main v0.9.0 about: nothing newer than v0.3.0", rc == 0
      and "past the newest release, v0.3.0" in out and "v0.9.0" not in out and not calls, (rc, out))
rc, out, calls = upd(co11, "update", "--ref", "v0.9.0", "--allow-downgrade")
check("…and --ref v0.9.0 --allow-downgrade is refused (not on main), nothing moved or run", rc == 3
      and "isn't on the repository's main branch" in out and git("rev-parse", "HEAD", cwd=co11) == head8 and not calls,
      (rc, out, calls))
link_co = fresh("0.1.0")
os.rename(os.path.join(link_co, "install.sh"), os.path.join(TD, "elsewhere-install.sh"))
os.symlink(os.path.join(TD, "elsewhere-install.sh"), os.path.join(link_co, "install.sh"))
git("update-index", "--assume-unchanged", "install.sh", cwd=link_co)
rc, out, calls = upd(link_co, "update")
check("an install.sh that's a link out of the checkout isn't run", rc == 3 and "isn't a file of this checkout" in out
      and not calls, (rc, out, calls))
shutil.rmtree(TD, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
