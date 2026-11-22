#!/usr/bin/env python3
"""Versions and updates (`metacomputer version`, `metacomputer update`), and the step every install ends with.

  version [--json]       this checkout's version, what's installed, and for each VM: what built it, and the
                         gateway, hub and event watcher it runs
  update [--check] [--yes] [--ref TAG] [--restart | --no-restart] [--allow-downgrade] [--json]
                         update this checkout to the latest release (or TAG) and finish the install:
                         `--check` only says what would change. A checkout with local changes, or on a branch
                         other than main, is refused (it's yours to update)

After any install (install.sh's last step, so `metacomputer update`, the install line again and a hand-run
`git pull && ./install.sh` all end the same way), for each VM of this checkout:
  - the hub and the event watcher, when they run older code, are stopped (the hub starts again at the next call;
    the watcher is started again);
  - a VM whose gateway is an older program is restarted (stop, start): at a terminal it asks first; without one,
    only with --restart (update) and otherwise it says what to run. A VM another session is using, or that you
    have in a handoff, is never stopped: `metacomputer stop` refuses, and that's reported;
  - when the gateway program changed and a secrets vault exists: `metacomputer secrets bind` again (the vault pins
    the gateway program; at a terminal it offers to run it);
  - a VM built from older guest files: what a rebuild would add (never rebuilt on its own).
The installed version is kept in ~/.local/share/metacomputer/installed.json. `metacomputer status` shows an update
that's available, from a check against the repository's tags at most once a day (UPDATE_CHECK=off turns it off). An
install whose Claude Code step didn't finish is recorded as such ("incomplete"), and `update` runs install.sh again
even when the checkout is already at the release.
"""
import argparse
import fcntl
import hashlib
import signal
import tempfile
import json
import os
import re
import subprocess
import sys
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(LAB, "bin", "metacomputer")
STAMP = os.path.expanduser("~/.local/share/metacomputer/installed.json")
CHECK_CACHE = os.path.expanduser("~/.cache/metacomputer/update-check.json")
CHECK_EVERY_S = 24 * 3600
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
GUEST_KEYS = ("CUA_DRIVER_VERSION", "CUA_DRIVER_SHA256", "UBUNTU_IMAGE", "UBUNTU_RELEASE", "BROWSERS", "EXTRA_PACKAGES",
              "BROWSER_EXTENSIONS", "VIRTUAL_AUDIO", "GUEST_USER", "ADMIN_USER", "SECRETS_PROXY_PORT", "NET_GATEWAY_IP")
GUEST_FILES = ("scripts/build-vm.sh",)    # besides provisioning/guest: what builds the seed and runs in the guest
PUBLIC_REPO = "https://github.com/metavert-io/metacomputer.git"
PROJECT_SLUG = "metavert-io/metacomputer"
SELECTION = ("MCOMP_VM", "MCOMP_CONFIG")  # a VM picked by the caller: every child here names its own
HOOK_CODE = ("lib",)                 # the hub and the watcher run this checkout's lib/


class Refused(Exception):
    pass


def git_env():
    """The environment for git: none of the caller's GIT_* (GIT_DIR, GIT_WORK_TREE and the rest would point it at
    another repository), and never a prompt for credentials."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/usr/bin/false", SSH_ASKPASS="/usr/bin/false")
    return env


def clean_env(**extra):
    """The environment for this checkout's own commands: no VM picked by the caller, none of its GIT_*, and the
    default computer (config/default-vm) left out, so a command without --vm is the main VM's (config/local.env)."""
    env = {k: v for k, v in os.environ.items() if k not in SELECTION and not k.startswith("GIT_")}
    env["MCOMP_NO_DEFAULT_VM"] = "1"
    env.update(extra)
    return env


def own(rel):
    """A path in this checkout that is really in it (no link out of it), or Refused."""
    p = os.path.join(LAB, rel)
    real, root = os.path.realpath(p), os.path.realpath(LAB)
    if not real.startswith(root + os.sep) or not os.path.isfile(real):
        raise Refused(f"{rel} isn't a file of this checkout ({real}); nothing was run")
    return real


def git(*args, timeout=60, check=True):
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", LAB, *args],
                       capture_output=True, text=True, timeout=timeout, env=git_env())
    if check and r.returncode != 0:
        raise Refused(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout.strip()


def vkey(tag):
    m = TAG_RE.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def checkout():
    """This checkout's state: commit, the release tag it's at (or the last one before it), branch, local changes."""
    commit = git("rev-parse", "HEAD")
    exact = git("describe", "--tags", "--exact-match", "--match", "v[0-9]*", "HEAD", check=False)
    near = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD", check=False)
    branch = git("symbolic-ref", "-q", "--short", "HEAD", check=False)
    dirty = bool(git("status", "--porcelain", "--untracked-files=no", check=False))
    origin = git("remote", "get-url", "origin", check=False)
    version = exact if vkey(exact) else (f"{near}+{commit[:7]}" if vkey(near) else commit[:12])
    return {"commit": commit, "tag": exact if vkey(exact) else None, "after": near if vkey(near) else None,
            "version": version, "branch": branch or None, "dirty": dirty, "origin": origin}


def version_key(st):
    """For comparing installs: the release it's at or after, and whether it's past it."""
    t = st.get("tag") or st.get("after")
    k = vkey(t)
    return (k or (0, 0, 0)) + ((0,) if st.get("tag") else (1,))


def latest_release():
    """The newest release tag on main, after a fetch: tags that aren't on origin/main (a branch's, an unrelated one)
    don't count."""
    tags = [t for t in git("tag", "--list", "v[0-9]*").splitlines() if vkey(t)]
    for t in sorted(tags, key=vkey, reverse=True):
        c = git("rev-parse", f"refs/tags/{t}^{{commit}}", check=False)
        if c and subprocess.run(["git", "-C", LAB, "merge-base", "--is-ancestor", c, "refs/remotes/origin/main"],
                                env=git_env(), capture_output=True).returncode == 0:
            return t
    return None


def is_ancestor(a, b):
    return subprocess.run(["git", "-C", LAB, "merge-base", "--is-ancestor", a, b], env=git_env(),
                          capture_output=True).returncode == 0


def anonymous_latest(timeout=15):
    """For the daily check: the project's release tags, asked of its public repository without credentials (nothing
    about the user goes with it); None when this checkout isn't the project's, or the network is out."""
    origin = git("remote", "get-url", "origin", check=False)
    if PROJECT_SLUG not in origin:
        return None
    env = dict(git_env(), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")   # no rewrites, headers or helpers
    r = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", "--tags", "--refs", PUBLIC_REPO, "v[0-9]*"],
                       capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    tags = [ln.split("refs/tags/", 1)[1] for ln in r.stdout.splitlines() if "refs/tags/" in ln]
    tags = [t for t in tags if vkey(t)]
    return max(tags, key=vkey) if tags else None


def whats_new(since, upto_ref, limit=8, only=None):
    """The CHANGELOG's highlights for the versions after `since` up to `upto_ref` (bold titles of their bullets);
    `only`: just the bullets whose text matches that pattern."""
    try:
        text = git("show", f"{upto_ref}:CHANGELOG.md")
    except Refused:
        return []
    lo = (vkey(str(since).split("+")[0]) or (0, 0, 0)) if since else (0, 0, 0)
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^## \[(\d+\.\d+\.\d+)\]", line)
        if m:
            k = tuple(int(x) for x in m.group(1).split("."))
            cur = m.group(1) if k > lo else None
            if cur:
                out.append((cur, []))
            continue
        if line.startswith("## "):
            cur = None
        if cur and line.startswith("- ") and len(out[-1][1]) < limit and (only is None or re.search(only, line, re.I)):
            b = re.match(r"^- \*\*([^*]{3,120})\*\*", line)
            out[-1][1].append((b.group(1) if b else line[2:].split(". ")[0])[:140].rstrip(":."))
    return [(v, items) for v, items in out if items]


def read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    """Written whole or not at all: a new private file beside it (never one that's there already), renamed over it."""
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sha_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def guest_digest(values):
    """What a VM's guest is built from: provisioning/guest and the build script that makes its seed (names and
    contents), and the settings and pins the build puts in it. Two VMs with the same digest got the same guest."""
    h = hashlib.sha256()
    root = os.path.join(LAB, "provisioning", "guest")
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, root).encode() + b"\0")
            h.update((sha_file(p) or "").encode() + b"\0")
    for rel in GUEST_FILES:
        h.update(rel.encode() + b"\0" + (sha_file(os.path.join(LAB, rel)) or "").encode() + b"\0")
    for k in GUEST_KEYS:
        h.update(f"{k}={' '.join(str(values.get(k) or '').split())}\0".encode())
    return h.hexdigest()


def code_mtime():
    newest = 0.0
    for sub in HOOK_CODE:
        for d, _dirs, files in os.walk(os.path.join(LAB, sub)):
            for f in files:
                if f.endswith((".py", ".sh")):
                    try:
                        newest = max(newest, os.stat(os.path.join(d, f)).st_mtime)
                    except OSError:
                        pass
    return newest


class Tty:
    """The user's terminal (also under `curl ... | bash`), opened for reading and for writing."""
    def __init__(self):
        self.r = open("/dev/tty", "r")
        self.w = open("/dev/tty", "w")

    def close(self):
        for f in (self.r, self.w):
            try:
                f.close()
            except OSError:
                pass


def tty():
    """The user's terminal to ask on, or None (Claude, scripts, no controlling terminal)."""
    try:
        return Tty()
    except OSError:
        return None


def ask(t, question, default_yes=True):
    t.w.write(question + (" [Y/n] " if default_yes else " [y/N] "))
    t.w.flush()
    ans = t.r.readline().strip().lower()
    return (ans in ("", "y", "yes")) if default_yes else ans in ("y", "yes")


def runinfo(vm=None):
    try:
        r = subprocess.run([own("bin/metacomputer")] + (["--vm", vm] if vm else []) + ["_runinfo"], capture_output=True,
                           text=True, timeout=60, env=clean_env(), stdin=subprocess.DEVNULL)
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError, subprocess.TimeoutExpired, Refused) as e:
        return {"vm": vm or "main", "error": str(e)[:200]}


def mc(vm, *args, timeout=600, expect=None):
    """This checkout's command for one VM (main: none named), with no VM inherited from the caller; `expect` (the
    VM's name and settings file hash as inspected) makes stop and start refuse another VM (expect_guard). A timeout
    is a result (124), never the end of the whole run, and stops the command's whole process group."""
    env = clean_env()
    if expect:
        env.update(MCOMP_EXPECT_VM=expect.get("vm") or "")
        if expect.get("config_sha"):
            env["MCOMP_EXPECT_CONFIG_SHA"] = expect["config_sha"]
    p = subprocess.Popen([own("bin/metacomputer")] + (["--vm", vm] if vm else []) + list(args), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        return 124, f"didn't finish in {timeout} s"
    return p.returncode, (out or "").strip()


# --- the step every install ends with ---------------------------------------------------------------------------
BIND_REMIND_S = 30 * 86400


def code_digest():
    """The digest a hub records of the code it loaded (lib/mcp_hub.py code_digest: the same recipe)."""
    h = hashlib.sha256()
    d = os.path.join(LAB, "lib")
    try:
        for f in sorted(os.listdir(d)):
            if f.endswith(".py"):
                with open(os.path.join(d, f), "rb") as fh:
                    h.update(f.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
    except OSError:
        return None
    return h.hexdigest()


def after_install(restart="ask", quiet=False):
    """Bring what's running up to this checkout, say what's left, and record what's installed. Never raises."""
    prev = read_json(STAMP) or {}
    if prev and os.path.realpath(str(prev.get("checkout") or "")) != os.path.realpath(LAB):
        prev = {"from_checkout": prev.get("checkout")}       # another checkout's record says nothing about this one
    st = checkout()
    netgw = os.path.join(LAB, "vendor", "bin", "mcomp-netgw")
    now_gw = {"program": netgw, "sha": sha_file(netgw)}
    code_now, mtime = code_digest(), code_mtime()
    t = tty() if restart == "ask" else None
    done, todo, rebuild, notes = [], [], [], []
    main = runinfo(None)
    vms = [None] + [v for v in (main.get("vms") or []) if isinstance(v, str)]
    gateway_changed = (prev.get("gateway") != now_gw) if prev.get("gateway") else None
    pending = {k: v for k, v in (prev.get("bind_pending") or {}).items()
               if isinstance(v, (int, float)) and time.time() - v < BIND_REMIND_S}
    for vm in vms:
        info = main if vm is None else runinfo(vm)
        label = info.get("vm") or vm or "main"
        cmd = "metacomputer" + (f" --vm {vm}" if vm else "")
        if info.get("error"):
            todo.append(f"{label}: couldn't read its state ({info['error']})")
            continue
        hub, watcher, gw = info.get("hub"), info.get("watcher"), info.get("gateway")
        pin = {"vm": info.get("vm"), "config_sha": info.get("config_sha")}
        started = info.get("gateway_started") or {}
        old_gw = bool(gw) and info.get("state") == "running" and (
            started.get("sha256") != now_gw["sha"] or os.path.realpath(started.get("bin") or "") != os.path.realpath(netgw))
        if hub and hub.get("code") != code_now and not old_gw:
            # Only an idle hub is stopped: one with clients is left (it exits by itself after HUB_IDLE with none, and
            # the next call starts it on the new code). A VM restart below stops it after its own owners check.
            # The check and the stop are one command (`hub stop --if-idle`, the hub's own status report): exit 3 = in use.
            rc, out = mc(vm, "hub", "stop", "--if-idle", timeout=60)
            if rc == 0:
                done.append(f"{label}: hub stopped (it starts again, on the new code, at the next call)")
            elif rc == 3:
                todo.append(f"{label}: the hub runs older code and is in use ({out.strip().rsplit(': ', 1)[-1][:120]}): "
                            f"it restarts on the new code once it's idle, or run `{cmd} hub stop` then")
            else:
                todo.append(f"{label}: hub not stopped: {out[-160:]}")
        if watcher and (watcher.get("started") or 0) + 2 < mtime:
            rc1, _ = mc(vm, "events", "watcher", "stop", timeout=60)
            rc2, out = mc(vm, "events", "watcher", "start", timeout=60)
            (done if rc1 == 0 and rc2 == 0 else todo).append(
                f"{label}: event watcher " + ("restarted" if rc1 == 0 and rc2 == 0 else f"not restarted: {out[-160:]}"))
        if old_gw:
            if gateway_changed is None:
                gateway_changed = True                     # no record of the last install: the running one is older
            go = restart == "yes" or (t is not None and ask(t, f"{label}'s VM runs an earlier gateway program. Restart "
                                                                "the VM now (its open apps close)?"))
            if go:
                rc1, out1 = mc(vm, "stop", timeout=300, expect=pin)
                rc2, out2 = mc(vm, "start", timeout=600, expect=pin) if rc1 == 0 else (1, "")
                if rc1 == 0 and rc2 == 0:
                    done.append(f"{label}: VM restarted on the new gateway")
                else:
                    todo.append(f"{label}: VM not restarted ({(out1 if rc1 else out2)[-200:]}); later: {cmd} stop && {cmd} start")
            else:
                todo.append(f"{label}: restart the VM to run the new gateway: {cmd} stop && {cmd} start")
        rec = info.get("build") if isinstance(info.get("build"), dict) else None
        want = info.get("guest_digest")
        if info.get("exists") is not False and want and (rec is None or rec.get("guest") != want):
            rebuild.append((label, vm, (rec or {}).get("version")))
        if info.get("vault_exists") is not False and (gateway_changed or label in pending):
            pending.setdefault(label, time.time())
    for label in list(pending):
        vm = None if label == (main.get("vm") or "main") else label
        cmd = "metacomputer" + (f" --vm {vm}" if vm else "") + " secrets bind"
        if t is not None and ask(t, f"{label}: the secrets vault pins the gateway program, which changed. Run `{cmd}` now "
                                    "(it asks you to approve)?"):
            t.close()
            r = subprocess.run([own("bin/metacomputer")] + (["--vm", vm] if vm else []) + ["secrets", "bind"],
                               env=clean_env())
            t = tty()
            if r.returncode == 0:
                done.append(f"{label}: secrets bind done")
                pending.pop(label)
                continue
        todo.append(f"{label}: if this VM's logins come from the secrets vault and you haven't since this update, run "
                    f"`{cmd}` (the vault pins the gateway program, which changed; fills say so until you do)")
    new_stamp = {"version": st["version"], "tag": st["tag"], "after": st["after"], "commit": st["commit"],
                 "checkout": LAB, "gateway": now_gw, "installed_at": time.time(), "bind_pending": pending}
    # install.sh says which agent steps didn't finish (MCOMP_INSTALL_INCOMPLETE=claude): recorded, so the next
    # `metacomputer update` runs install.sh again even when the checkout is already at the release
    incomplete = [x for x in (os.environ.get("MCOMP_INSTALL_INCOMPLETE") or "").split(",") if x in ("claude", "codex")]
    if incomplete:
        new_stamp["incomplete"] = incomplete
    try:
        write_json(STAMP, new_stamp)
    except OSError as e:
        todo.append(f"couldn't record what's installed ({e})")
    if not quiet:
        was = prev.get("version")
        if was and was != st["version"]:
            print(f"\n==> Updated: {was} → {st['version']}")
            for v, items in whats_new(prev.get("tag") or prev.get("after"), "HEAD"):
                print(f"  {v}: " + "; ".join(items))
        elif not was:
            print(f"\n==> Installed: {st['version']}")
        for line in done:
            print("  ✓ " + line)
        for line in todo:
            print("  • " + line)
        for label, vm, built in rebuild:
            adds = whats_new(built, "HEAD", only=r"next build|a rebuild") if built else []
            what = ("; ".join(i for _v, items in adds for i in items)[:400] if adds else
                    "what the CHANGELOG marks \"next build\" since " + (built or "it was built"))
            print(f"  • {label}: a rebuild (metacomputer{' --vm ' + vm if vm else ''} build --replace; it replaces the "
                  f"desktop and its apps, your volumes, recordings and vault stay) would add: {what}")
    if t is not None:
        t.close()
    return {"done": done, "todo": todo, "rebuild": [r[0] for r in rebuild], "version": st["version"],
            "bind_pending": sorted(pending)}


# --- commands ---------------------------------------------------------------------------------------------------
def cmd_version(a):
    st = checkout()
    stamp = read_json(STAMP) or {}
    main = runinfo(None)
    vms = [main] + [runinfo(v) for v in (main.get("vms") or [])]
    out = {"checkout": LAB, "version": st["version"], "commit": st["commit"], "branch": st["branch"],
           "local_changes": st["dirty"], "installed": stamp or None,
           "vms": [{"vm": i.get("vm"), "state": i.get("state"), "built_by": (i.get("build") or {}).get("version"),
                    "guest_current": i.get("build") is not None and (i.get("build") or {}).get("guest") == i.get("guest_digest"),
                    "gateway": os.path.basename((i.get("gateway_started") or {}).get("bin") or "") or
                               ("(started by an earlier version)" if i.get("gateway") else None),
                    "gateway_current": (i.get("gateway_started") or {}).get("sha256") == sha_file(
                        os.path.join(LAB, "vendor", "bin", "mcomp-netgw")) if i.get("gateway") else None} for i in vms]}
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"metacomputer {st['version']}  ({LAB}{', branch ' + st['branch'] if st['branch'] else ''}"
          f"{', local changes' if st['dirty'] else ''})")
    if stamp and stamp.get("commit") != st["commit"]:
        print(f"installed     {stamp.get('version')}: this checkout changed since; run ./install.sh (or metacomputer update)")
    elif stamp.get("incomplete"):
        print(f"installed     {stamp.get('version')}, but its {' and '.join('Claude Code' if x == 'claude' else 'Codex' for x in stamp['incomplete'])} "
              "setup didn't finish: metacomputer update runs the install again")
    for v in out["vms"]:
        print(f"vm {v['vm']:<12} {v['state'] or '?':<8} built by {v['built_by'] or 'an earlier version'}"
              f"{'' if v['guest_current'] else ' (a rebuild would add newer guest files)'}"
              f"{'; gateway ' + v['gateway'] if v['gateway'] else ''}")
    return 0


def update_lock():
    """One update of this checkout at a time (held until this process exits)."""
    path = os.path.join(git("rev-parse", "--absolute-git-dir"), "metacomputer-update.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise Refused("another update of this checkout is running; try again when it's done")
    return fd


def cmd_update(a):
    _lock = update_lock()                  # noqa: F841  (kept open: the lock lasts as long as the update)
    st = checkout()
    if st["dirty"]:
        raise Refused(f"{LAB} has local changes, so it's yours to update (commit or stash them, then run this again)")
    if st["branch"] and st["branch"] != "main":
        raise Refused(f"{LAB} is on the branch {st['branch']}: updates follow releases on main (switch to main, or update "
                      "it yourself with git, then ./install.sh)")
    if a.ref and not vkey(a.ref):
        raise Refused(f"--ref takes a release tag (like v0.4.1), not {a.ref}")
    git("fetch", "-q", "--tags", "--prune", "origin", timeout=120)
    target = a.ref or latest_release()
    if not target:
        raise Refused("the repository has no release on main yet")
    tcommit = git("rev-parse", f"refs/tags/{target}^{{commit}}")
    if not is_ancestor(tcommit, "refs/remotes/origin/main"):
        raise Refused(f"{target} isn't on the repository's main branch, so it isn't a release to update to")
    stamp = read_json(STAMP) or {}
    same = tcommit == st["commit"]
    forward = same or is_ancestor(st["commit"], tcommit)
    if not forward:
        if not a.allow_downgrade:
            raise Refused(f"{target} isn't ahead of this checkout ({st['version']}): going back needs --allow-downgrade, "
                          "and a checkout with commits of its own is yours to move")
        if st["branch"] == "main":
            raise Refused("main only moves forward: to go back, check out the tag yourself (git checkout TAG), then "
                          "./install.sh --allow-downgrade")
        if not (git("branch", "-r", "--contains", st["commit"], check=False) or
                git("tag", "--contains", st["commit"], check=False)):
            raise Refused("this checkout has commits that no branch or tag of the repository has: they'd be left behind")
    news = whats_new(st["tag"] or st["after"], f"refs/tags/{target}")
    if a.json and a.check:
        print(json.dumps({"current": st["version"], "latest": target, "up_to_date": same,
                          "whats_new": [{"version": v, "items": i} for v, i in news]}))
        return 0
    print(f"metacomputer {st['version']}" + (" (up to date)" if same else f" → {target}"))
    if not same:
        for v, items in news:
            print(f"  {v}: " + "; ".join(items))
    if a.check:
        return 0
    restart = "yes" if a.restart else "no" if a.no_restart else (
        os.environ.get("MCOMP_UPDATE_RESTART") if os.environ.get("MCOMP_UPDATE_RESTART") in ("yes", "no", "ask") else "ask")
    if not same:
        if st["branch"] == "main":
            git("merge", "-q", "--ff-only", tcommit, timeout=120)    # main moves forward to the release, never back
        else:
            git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", tcommit, timeout=120)
        if git("rev-parse", "HEAD") != tcommit:
            raise Refused(f"couldn't move {LAB} to {target}")
    installed_here = (stamp.get("commit") == tcommit and not stamp.get("incomplete") and
                      os.path.realpath(str(stamp.get("checkout") or "")) == os.path.realpath(LAB))
    if same and installed_here:
        after_install(restart=restart)
        return 0
    args = [own("install.sh"), "--yes", "--no-build"] + (["--allow-downgrade"] if a.allow_downgrade else [])
    return subprocess.run(args, env=clean_env(MCOMP_UPDATE_RESTART=restart)).returncode


def runinfo_json(argv):
    """For bin/metacomputer _runinfo (the shell side passes this VM's facts); prints one JSON line. The gateway's pid
    comes checked (scripts/lib.sh netgw_pid: this VM's gw.json on its command line); the hub's from its own record."""
    vm, state, run_dir, netgw, gw, hub_file, watcher, vault_dir, kind = (argv + [""] * 9)[:9]

    def started(pid):
        out = subprocess.run(["ps", "-o", "lstart=", "-p", pid], capture_output=True, text=True).stdout.strip()
        try:
            return time.mktime(time.strptime(" ".join(out.split()[:5]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            return None

    def proc(pid):
        if not pid.isdigit():
            return None
        cmdline = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout.strip()
        return {"pid": int(pid), "started": started(pid), "command": cmdline[:500]} if cmdline else None
    hub = None
    rec = read_json(hub_file) if hub_file else None
    if rec and isinstance(rec.get("pid"), int):
        p = proc(str(rec["pid"]))
        if p and "mcp_hub.py" in p["command"]:
            hub = dict(p, code=rec.get("code"))
    gs = {}
    try:
        with open(os.path.join(run_dir, "netgw.started")) as f:
            gs = dict(ln.strip().split("=", 1) for ln in f if "=" in ln)
    except OSError:
        pass
    vms_dir = os.path.join(LAB, "config", "vms")
    vms = sorted(f[:-4] for f in os.listdir(vms_dir) if f.endswith(".env")) if os.path.isdir(vms_dir) else []
    vault_exists = None
    try:
        r = subprocess.run([own("bin/metacomputer")] + (["--vm", vm] if kind == "vm" else []) + ["secrets", "status", "--json"],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL, env=clean_env())
        vault_exists = bool(json.loads(r.stdout.strip().splitlines()[-1]).get("exists"))
    except (ValueError, IndexError, OSError, subprocess.TimeoutExpired, Refused):
        pass                                        # unknown: the bind reminder stays, worded as conditional
    values = {k: os.environ.get("MCOMP_G_" + k, "") for k in GUEST_KEYS}
    cfg = os.environ.get("MCOMP_G_CONFIG_FILE") or ""
    print(json.dumps({"vm": vm, "config_sha": sha_file(cfg) if cfg else None, "state": state, "run_dir": run_dir, "netgw_bin": netgw, "gateway": proc(gw),
                      "gateway_started": gs, "hub": hub, "watcher": proc(watcher), "vault_dir": vault_dir, "kind": kind,
                      "vms": vms, "vault_exists": vault_exists, "guest_digest": guest_digest(values),
                      "build": read_json(os.path.join(run_dir, "vm-build.json")) if run_dir else None,
                      "exists": os.environ.get("MCOMP_G_EXISTS", "1") == "1"}))


def record_build(run_dir, digest=None):
    """build-vm.sh, once a build succeeded: which version and guest files made this VM. The digest is the one taken when
    the build copied the guest files (so a later edit isn't recorded as built)."""
    st = checkout()
    values = {k: os.environ.get("MCOMP_G_" + k, "") for k in GUEST_KEYS}
    write_json(os.path.join(run_dir, "vm-build.json"), {"version": st["version"], "commit": st["commit"],
                                                        "guest": digest or guest_digest(values), "built_at": time.time()})


CHECK_CLAIM = CHECK_CACHE + ".running"


def status_line():
    """For metacomputer status: an update that's available, from a check at most once a day (in the background, so
    status never waits on the network). Prints nothing when up to date, off, or not known yet."""
    if (os.environ.get("MCOMP_UPDATE_CHECK") or "on").lower() == "off":
        return 0
    cache = read_json(CHECK_CACHE) or {}
    if time.time() - float(cache.get("checked") or 0) > CHECK_EVERY_S:
        spawn_refresh()
    latest = cache.get("latest")
    try:
        st = checkout()
    except Refused:
        return 0
    if vkey(latest) and version_key({"tag": latest}) > version_key(st):
        print(f"update        {latest} is out (this is {st['version']}): metacomputer update")
    return 0


def spawn_refresh():
    """The daily check, detached, one at a time (a claim file younger than 10 minutes means one is running)."""
    try:
        os.makedirs(os.path.dirname(CHECK_CLAIM), mode=0o700, exist_ok=True)
        try:
            if time.time() - os.stat(CHECK_CLAIM).st_mtime < 600:
                return
            os.unlink(CHECK_CLAIM)
        except FileNotFoundError:
            pass
        os.close(os.open(CHECK_CLAIM, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_refresh-check"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=git_env())
    except OSError:
        pass


def refresh_check():
    try:
        latest = anonymous_latest()
    except (Refused, OSError, subprocess.TimeoutExpired):
        latest = None
    if latest is None:
        latest = (read_json(CHECK_CACHE) or {}).get("latest")
    try:
        write_json(CHECK_CACHE, {"checked": time.time(), "latest": latest})
    finally:
        try:
            os.unlink(CHECK_CLAIM)
        except OSError:
            pass
    return 0


def main(argv):
    if argv and argv[0] == "_runinfo-json":
        runinfo_json(argv[1:])
        return 0
    if argv and argv[0] == "_record-build":
        record_build(argv[1], argv[2] if len(argv) > 2 else None)
        return 0
    if argv and argv[0] == "_guest-digest":
        print(guest_digest({k: os.environ.get("MCOMP_G_" + k, "") for k in GUEST_KEYS}))
        return 0
    if argv and argv[0] == "_status-line":
        return status_line()
    if argv and argv[0] == "_refresh-check":
        return refresh_check()
    if argv and argv[0] == "_after-install":
        try:
            after_install(restart=os.environ.get("MCOMP_UPDATE_RESTART") or "ask")
        except Exception as e:                     # never fail an install over its last report
            print(f"  • couldn't finish the after-install check ({type(e).__name__}: {str(e)[:200]})")
        return 0
    ap = argparse.ArgumentParser(prog="metacomputer", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("version")
    v.add_argument("--json", action="store_true")
    u = sub.add_parser("update")
    u.add_argument("--check", action="store_true")
    u.add_argument("--yes", action="store_true", help="(accepted; update asks only about restarting a VM)")
    u.add_argument("--ref")
    g = u.add_mutually_exclusive_group()
    g.add_argument("--restart", action="store_true", help="restart VMs that run an older gateway, without asking")
    g.add_argument("--no-restart", action="store_true", help="never restart a VM; say what to run")
    u.add_argument("--allow-downgrade", action="store_true")
    u.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        return cmd_version(a) if a.cmd == "version" else cmd_update(a)
    except Refused as e:
        print(f"metacomputer {a.cmd}: {e}", file=sys.stderr)
        return 3
    except subprocess.TimeoutExpired:
        print(f"metacomputer {a.cmd}: the repository didn't answer in time (check the network)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
