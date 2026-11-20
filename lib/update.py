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
that's available, from a check against the repository's tags at most once a day (UPDATE_CHECK=off turns it off).
"""
import argparse
import hashlib
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
              "BROWSER_EXTENSIONS", "VIRTUAL_AUDIO", "GUEST_USER", "SECRETS_PROXY_PORT")
HOOK_CODE = ("lib",)                 # the hub and the watcher run this checkout's lib/


class Refused(Exception):
    pass


def git(*args, timeout=60, check=True):
    r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", LAB, *args],
                       capture_output=True, text=True, timeout=timeout)
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


def latest_release(timeout=20):
    out = git("ls-remote", "--tags", "--refs", "origin", "v[0-9]*", timeout=timeout)
    tags = [ln.split("refs/tags/", 1)[1] for ln in out.splitlines() if "refs/tags/" in ln]
    tags = [t for t in tags if vkey(t)]
    return max(tags, key=vkey) if tags else None


def whats_new(since, upto_ref, limit=8):
    """The CHANGELOG's highlights for the versions after `since` up to `upto_ref` (bold titles of their bullets)."""
    try:
        text = git("show", f"{upto_ref}:CHANGELOG.md")
    except Refused:
        return []
    lo = vkey(since) or (0, 0, 0) if since else (0, 0, 0)
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
        if cur and line.startswith("- ") and len(out[-1][1]) < limit:
            b = re.match(r"^- \*\*([^*]{3,120})\*\*", line)
            out[-1][1].append((b.group(1) if b else line[2:].split(". ")[0])[:140].rstrip(":."))
    return out


def read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sha_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def guest_digest(values):
    """What a VM's guest is built from: provisioning/guest (names and contents) and the settings and pins the build
    puts in it. Two VMs with the same digest got the same guest."""
    h = hashlib.sha256()
    root = os.path.join(LAB, "provisioning", "guest")
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, root).encode() + b"\0")
            h.update((sha_file(p) or "").encode() + b"\0")
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


def tty():
    """The user's terminal, to ask on (also under `curl ... | bash`), or None (Claude, scripts)."""
    try:
        return open("/dev/tty", "r+")
    except OSError:
        return None


def ask(t, question, default_yes=True):
    t.write(question + (" [Y/n] " if default_yes else " [y/N] "))
    t.flush()
    ans = t.readline().strip().lower()
    return (ans in ("", "y", "yes")) if default_yes else ans in ("y", "yes")


def runinfo(vm=None):
    args = [CLI] + (["--vm", vm] if vm else []) + ["_runinfo"]
    env = {k: v for k, v in os.environ.items() if not (vm is None and k in ("MCOMP_VM", "MCOMP_CONFIG"))}
    r = subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"vm": vm or "main", "error": (r.stderr or r.stdout).strip()[-200:] or f"exit {r.returncode}"}


def mc(vm, *args, timeout=600):
    cmd = [CLI] + (["--vm", vm] if vm else []) + list(args)
    env = {k: v for k, v in os.environ.items() if not (vm is None and k in ("MCOMP_VM", "MCOMP_CONFIG"))}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    return r.returncode, (r.stdout + r.stderr).strip()


# --- the step every install ends with ---------------------------------------------------------------------------
def after_install(restart="ask", quiet=False):
    """Bring what's running up to this checkout, say what's left, and record what's installed. Never raises."""
    prev = read_json(STAMP) or {}
    st = checkout()
    netgw = os.path.join(LAB, "vendor", "bin", "mcomp-netgw")
    now_gw = {"program": netgw, "sha": sha_file(netgw)}
    mtime = code_mtime()
    t = tty() if restart == "ask" else None
    done, todo, rebuild = [], [], []
    main = runinfo(None)
    vms = [None] + [v for v in (main.get("vms") or []) if isinstance(v, str)]
    gateway_changed = prev.get("gateway") != now_gw if prev.get("gateway") else None
    vault_people = []
    for vm in vms:
        info = main if vm is None else runinfo(vm)
        label = info.get("vm") or vm or "main"
        if info.get("error"):
            todo.append(f"{label}: couldn't read its state ({info['error']})")
            continue
        hub, watcher, gw = info.get("hub"), info.get("watcher"), info.get("gateway")
        if hub and (hub.get("started") or 0) < mtime:
            rc, out = mc(vm, "hub", "stop", timeout=60)
            (done if rc == 0 else todo).append(f"{label}: hub " + ("stopped (it starts again, on the new code, at the "
                                                                   "next call)" if rc == 0 else f"not stopped: {out[-160:]}"))
        if watcher and (watcher.get("started") or 0) < mtime:
            rc1, _ = mc(vm, "events", "watcher", "stop", timeout=60)
            rc2, out = mc(vm, "events", "watcher", "start", timeout=60)
            (done if rc1 == 0 and rc2 == 0 else todo).append(
                f"{label}: event watcher " + ("restarted" if rc1 == 0 and rc2 == 0 else f"not restarted: {out[-160:]}"))
        old_gw = bool(gw) and (os.path.realpath(gw.get("program") or "") != os.path.realpath(info.get("netgw_bin") or netgw)
                               or (gw.get("started") or 0) < (os.stat(netgw).st_mtime if os.path.exists(netgw) else 0))
        if old_gw:
            if gateway_changed is None:
                gateway_changed = True                     # no record of the last install: the running one is older
            go = restart == "yes" or (t is not None and ask(t, f"{label}'s VM runs an earlier gateway ("
                                                                f"{os.path.basename(gw.get('program') or '?')}). "
                                                                "Restart the VM now (its open apps close)?"))
            if go:
                rc1, out1 = mc(vm, "stop", timeout=300)
                rc2, out2 = mc(vm, "start", timeout=600) if rc1 == 0 else (1, "")
                if rc1 == 0 and rc2 == 0:
                    done.append(f"{label}: VM restarted on the new gateway")
                else:
                    todo.append(f"{label}: VM not restarted ({(out1 if rc1 else out2)[-200:]}); later: "
                                f"metacomputer{' --vm ' + vm if vm else ''} stop && metacomputer{' --vm ' + vm if vm else ''} start")
            else:
                todo.append(f"{label}: restart the VM to run the new gateway: metacomputer{' --vm ' + vm if vm else ''} "
                            f"stop && metacomputer{' --vm ' + vm if vm else ''} start")
        rec = info.get("build") if isinstance(info.get("build"), dict) else None
        want = info.get("guest_digest")
        if info.get("exists") is not False and want and (rec is None or rec.get("guest") != want):
            rebuild.append((label, vm, (rec or {}).get("version")))
        if info.get("vault_exists"):
            vault_people.append((label, vm))
    if gateway_changed and vault_people:
        for label, vm in vault_people:
            cmd = f"metacomputer{' --vm ' + vm if vm else ''} secrets bind"
            if t is not None and ask(t, f"The gateway program changed, and the secrets vault pins it. Run `{cmd}` "
                                        "now (it asks you to approve)?"):
                t.close()
                r = subprocess.run([CLI] + (["--vm", vm] if vm else []) + ["secrets", "bind"])
                (done if r.returncode == 0 else todo).append(f"{label}: secrets bind " +
                                                             ("done" if r.returncode == 0 else f"failed (exit {r.returncode}): {cmd}"))
                t = tty()
            else:
                todo.append(f"{label}: if this VM's logins come from the secrets vault, run `{cmd}` (the vault pins "
                            "the gateway program, which changed; fills say so until you do)")
    new_stamp = {"version": st["version"], "tag": st["tag"], "commit": st["commit"], "checkout": LAB,
                 "gateway": now_gw, "installed_at": time.time()}
    write_json(STAMP, new_stamp)
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
            print(f"  • {label}: its VM was built from {'version ' + built if built else 'earlier'} guest files; a rebuild "
                  f"adds what's marked \"next build\" in the CHANGELOG since then: metacomputer{' --vm ' + vm if vm else ''} "
                  "build --replace (it replaces the desktop and its apps; your volumes, recordings and vault stay)")
    if t is not None:
        t.close()
    return {"done": done, "todo": todo, "rebuild": [r[0] for r in rebuild], "version": st["version"]}


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
                    "gateway": os.path.basename((i.get("gateway") or {}).get("program") or "") or None} for i in vms]}
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"metacomputer {st['version']}  ({LAB}{', branch ' + st['branch'] if st['branch'] else ''}"
          f"{', local changes' if st['dirty'] else ''})")
    if stamp and stamp.get("commit") != st["commit"]:
        print(f"installed     {stamp.get('version')}: this checkout changed since; run ./install.sh (or metacomputer update)")
    for v in out["vms"]:
        print(f"vm {v['vm']:<12} {v['state'] or '?':<8} built by {v['built_by'] or 'an earlier version'}"
              f"{'' if v['guest_current'] else ' (a rebuild would add newer guest files)'}"
              f"{'; gateway ' + v['gateway'] if v['gateway'] else ''}")
    return 0


def cmd_update(a):
    st = checkout()
    if st["dirty"]:
        raise Refused(f"{LAB} has local changes, so it's yours to update (commit or stash them, then run this again)")
    if st["branch"] and st["branch"] != "main":
        raise Refused(f"{LAB} is on the branch {st['branch']}: updates follow releases on main (switch to main, or update "
                      "it yourself with git, then ./install.sh)")
    git("fetch", "-q", "--tags", "--prune", "origin", timeout=120)
    target = a.ref or latest_release()
    if not target:
        raise Refused("the repository has no release yet")
    if not vkey(target) and not a.ref:
        raise Refused(f"not a release tag: {target}")
    tcommit = git("rev-parse", f"refs/tags/{target}^{{commit}}") if vkey(target) else git("rev-parse", f"{target}^{{commit}}")
    stamp = read_json(STAMP) or {}
    same = tcommit == st["commit"]
    if not a.allow_downgrade and vkey(target) and version_key({"tag": target}) < version_key(st):
        raise Refused(f"{target} is older than this checkout ({st['version']}); --allow-downgrade to go back anyway")
    news = whats_new(st["tag"] or st["after"], f"refs/tags/{target}" if vkey(target) else target)
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
    if not same:
        if st["branch"] == "main":
            git("merge", "-q", "--ff-only", tcommit, timeout=120)    # main moves forward to the release, never back
        else:
            git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", tcommit, timeout=120)
        if git("rev-parse", "HEAD") != tcommit:
            raise Refused(f"couldn't move {LAB} to {target}")
    if same and stamp.get("commit") == st["commit"]:
        res = after_install(restart="yes" if a.restart else "no" if a.no_restart else "ask")
        return 0
    env = dict(os.environ, MCOMP_UPDATE_RESTART="yes" if a.restart else "no" if a.no_restart else "ask")
    r = subprocess.run([os.path.join(LAB, "install.sh"), "--yes", "--no-build"], env=env)
    return r.returncode


def runinfo_json(argv):
    """For bin/metacomputer _runinfo (the shell side passes this VM's facts); prints one JSON line."""
    vm, state, run_dir, netgw, gw, hub, watcher, vault_dir, kind = (argv + [""] * 9)[:9]

    def proc(pid):
        if not pid.isdigit():
            return None
        out = subprocess.run(["ps", "-o", "lstart=,command=", "-p", pid], capture_output=True, text=True).stdout.strip()
        parts = out.split()
        if len(parts) < 6:
            return None
        try:
            started = time.mktime(time.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            started = None
        return {"pid": int(pid), "started": started, "program": parts[5]}
    vms_dir = os.path.join(LAB, "config", "vms")
    vms = sorted(f[:-4] for f in os.listdir(vms_dir) if f.endswith(".env")) if os.path.isdir(vms_dir) else []
    vault_exists = False
    try:
        r = subprocess.run([CLI] + (["--vm", vm] if kind == "vm" else []) + ["secrets", "status", "--json"],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        vault_exists = bool(json.loads(r.stdout.strip().splitlines()[-1]).get("exists"))
    except (ValueError, IndexError, OSError, subprocess.TimeoutExpired):
        pass
    values = {k: os.environ.get("MCOMP_G_" + k, "") for k in GUEST_KEYS}
    print(json.dumps({"vm": vm, "state": state, "run_dir": run_dir, "netgw_bin": netgw, "gateway": proc(gw),
                      "hub": proc(hub), "watcher": proc(watcher), "vault_dir": vault_dir, "kind": kind, "vms": vms,
                      "vault_exists": vault_exists, "guest_digest": guest_digest(values),
                      "build": read_json(os.path.join(run_dir, "vm-build.json")) if run_dir else None,
                      "exists": os.environ.get("MCOMP_G_EXISTS", "1") == "1"}))


def record_build(run_dir):
    """build-vm.sh, once a build succeeded: which version and guest files made this VM."""
    st = checkout()
    values = {k: os.environ.get("MCOMP_G_" + k, "") for k in GUEST_KEYS}
    write_json(os.path.join(run_dir, "vm-build.json"), {"version": st["version"], "commit": st["commit"],
                                                        "guest": guest_digest(values), "built_at": time.time()})


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
    """The daily check, detached: status goes on without waiting for the network."""
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_refresh-check"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def refresh_check():
    try:
        latest = latest_release(timeout=15)
    except (Refused, OSError, subprocess.TimeoutExpired):
        latest = (read_json(CHECK_CACHE) or {}).get("latest")
    write_json(CHECK_CACHE, {"checked": time.time(), "latest": latest})
    return 0


def main(argv):
    if argv and argv[0] == "_runinfo-json":
        runinfo_json(argv[1:])
        return 0
    if argv and argv[0] == "_record-build":
        record_build(argv[1])
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
