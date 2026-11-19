#!/usr/bin/env python3
"""metacomputer's VMs on this Mac: what `metacomputer vm list` shows, and the facts `vm delete` checks.
bin/metacomputer (scripts/vm-commands.sh) gathers this checkout's settings files, each resolved the way every
command resolves them (scripts/lib.sh), and the runner's list (`lume ls --format json`); this merges the two.

    vms.py list --settings ROWS.jsonl (--runner LIST.json | --runner-failed) [--selected NAME] [--json]
    vms.py facts NAME BASELINE < LIST.json      KEY=VALUE lines: the VM, its baseline, its previous one, checkpoints
    vms.py identity --home HOME < RECORDS      one VM's names, ports and folders against every other VM's
    vms.py delete-check --target NAME --baseline B --home HOME < ROWS.jsonl
                                               what `vm delete` would delete, against every other VM's names
    vms.py rmtree --base DIR --rel A/NAME --target-vm NAME --home HOME [--guard PATH]... [--check] < ROWS.jsonl
                                               delete one VM's own folder, never through a link, never another's

Only rmtree changes anything. Names from the runner are escaped on a terminal (lib/termsafe.py)."""
import argparse
import json
import os
import re
import shutil
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from termsafe import escape_for_terminal as esc  # noqa: E402

COPY = re.compile(r"^(?P<owner>.+?)(?:-baseline(?:-prev)?|-ckpt-(?P<ckpt>.+))$")
SAFE = re.compile(r"[A-Za-z0-9._-]{1,128}")
PATH_KEYS = ("SSH_DIR", "RUN_DIR", "LOG_DIR", "VAULT_DIR", "EVENTS_SPOOL")


class Refused(Exception):
    pass


# --- folders as the folders they are --------------------------------------------------------------------------
def absolute(path, home, what="a folder"):
    """path as an absolute path (~/ is the account's home); no . or .. components. Refused otherwise."""
    if not isinstance(path, str) or not path:
        raise Refused(f"{what} is empty")
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    if not path.startswith("/"):
        raise Refused(f"{what} must be an absolute folder, not {path!r}")
    parts = [c for c in path.split("/") if c]
    if any(c in (".", "..") for c in parts):
        if ".." in parts:
            raise Refused(f"{what} can't have .. in it ({path})")
        parts = [c for c in parts if c != "."]
    return "/" + "/".join(parts)


def identity(path, home, what="a folder"):
    """What the folder at path is, whatever spells it: (device, inode) of the path when it exists (links followed),
    else of its nearest existing ancestor plus the rest of the path. Two spellings of one folder (a link, a firmlink,
    `x/.`, `x//y`) get the same identity."""
    p = absolute(path, home, what)
    parts = [c for c in p.split("/") if c]
    rest = []
    while True:
        try:
            st = os.stat("/" + "/".join(parts))
            return (st.st_dev, st.st_ino, tuple(rest))
        except (FileNotFoundError, NotADirectoryError):
            if not parts:
                raise Refused(f"can't read {what} ({path})")
            rest.insert(0, parts.pop())
        except OSError as e:
            raise Refused(f"can't read {what} ({path}): {e.strerror}")


def canonical(path, home, what="a folder"):
    """path with the links in the part that exists resolved (what the vault is handed)."""
    return os.path.realpath(absolute(path, home, what))


def ancestors(path, home):
    """The identities of path's existing folder and every folder above it (to see whether path is inside another)."""
    out = set()
    p = canonical(path, home)
    while True:
        try:
            st = os.stat(p)
            out.add((st.st_dev, st.st_ino))
        except OSError:
            pass
        if p == "/":
            return out
        p = os.path.dirname(p)


def read_records(stream):
    """NUL-separated OWNER KEY VALUE records -> {owner: {key: [values]}} in the order owners first appear."""
    data = stream.read().split("\0")
    owners = {}
    for i in range(0, len(data) - 2, 3):
        owner, key, value = data[i], data[i + 1], data[i + 2]
        owners.setdefault(owner, {}).setdefault(key, []).append(value)
    return owners


def names_of(ids):
    """A VM's own names: itself, its baseline and the baseline's previous copy; and its checkpoint prefix."""
    vm = (ids.get("VM_NAME") or [""])[0]
    base = (ids.get("BASELINE_NAME") or [""])[0]
    return {n for n in (vm, base, base + "-prev" if base else "") if n}, (vm + "-ckpt-" if vm else None)


def check_identity(owners, home):
    """One VM ("self") against every other: no name of one is a name or checkpoint of another, no port is shared,
    and no folder (SSH_DIR, RUN_DIR, LOG_DIR, VAULT_DIR, EVENTS_SPOOL) is another's, whatever spells it.
    Returns self's vault folder and spool as their canonical paths."""
    me = owners.pop("self", None)
    if not me:
        raise Refused("nothing to check")
    my_names, my_ckpt = names_of(me)
    my_ports = {p for k in ("SSH_PORT", "VNC_PORT") for p in me.get(k, []) if p}
    mine = {}
    for k in PATH_KEYS:
        for v in me.get(k, []):
            mine.setdefault(identity(v, home, k), (k, v))
    for owner, ids in owners.items():
        if ids.get("UNREADABLE"):
            raise Refused(f"{owner} can't be read, so this VM can't be checked against it; fix it (or remove it) first")
        names, ckpt = names_of(ids)
        for n in sorted(my_names):
            if n in names or (ckpt and n.startswith(ckpt)):
                raise Refused(f"the name {n} is {owner}'s (its VM, baseline or a copy); every VM needs its own")
        for n in sorted(names):
            if my_ckpt and n.startswith(my_ckpt):
                raise Refused(f"{owner}'s {n} would be one of this VM's checkpoints")
        for k in ("SSH_PORT", "VNC_PORT"):
            for p in ids.get(k, []):
                if p and p in my_ports:
                    raise Refused(f"port {p} is {owner}'s {k}; every VM needs its own ports")
        for k in PATH_KEYS:
            for v in ids.get(k, []):
                try:
                    theirs = identity(v, home, k)
                except Refused:
                    continue                  # theirs can't be read: it can't be the folder ours is
                if theirs in mine:
                    mk, mv = mine[theirs]
                    raise Refused(f"its {mk} ({mv}) is the same folder as {owner}'s {k} ({v}); every VM needs its own")
    return {k: canonical(me[k][0], home, k) for k in ("VAULT_DIR", "EVENTS_SPOOL") if me.get(k)}


# --- what vm delete deletes -----------------------------------------------------------------------------------
def rows_from(stream):
    out = []
    for line in stream:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


VMS_FILE = re.compile(r"^config/vms/(?P<name>[A-Za-z0-9_][A-Za-z0-9._-]{0,63})\.env$")


def error_row(owners, file, vm, error):
    """The row for a settings file that was refused: its names and folders as the file says them (unchecked,
    `mc_identity_records`), kept so `vm delete` still protects them."""
    ids = next(iter(owners.values()), {})
    first = lambda k: (ids.get(k) or [""])[0]     # noqa: E731
    known = bool(ids) and not ids.get("UNREADABLE")     # an unreadable file says nothing: no guessed defaults
    return {"file": file, "vm": vm or (first("VM_NAME") if known else "") or None, "ok": False, "error": error,
            "raw_vm": first("VM_NAME") if known else "", "raw_baseline": first("BASELINE_NAME") if known else "",
            "folders": [v for k in PATH_KEYS for v in ids.get(k, [])] if known else [],
            "volumes": first("VOLUMES") if known else None, "known": known}


def delete_check(rows, target, baseline):
    """The names `vm delete TARGET` deletes (the VM, BASELINE, BASELINE-prev) against every other VM this checkout
    has settings for: none may be another VM, its baseline, its previous baseline or one of its checkpoints. A
    settings file that was refused counts by the names it says (unchecked) and, for config/vms/NAME.env, by NAME and
    NAME's copies too; one whose names can't be read at all refuses."""
    if baseline != target + "-baseline":
        raise Refused(f"its baseline is named {baseline}, not {target}-baseline; delete that VM by hand if you mean to")
    doomed = {target, baseline, baseline + "-prev"}
    for r in rows:
        owner = r.get("file") or "another VM's settings"
        if r.get("ok"):
            if r.get("vm") == target:
                continue
            groups = [names_of({"VM_NAME": [r.get("vm") or ""], "BASELINE_NAME": [r.get("baseline") or ""]})]
        else:
            m = VMS_FILE.match(r.get("file") or "")
            groups = []
            if m:
                if m.group("name") == target:
                    continue
                n = m.group("name")
                groups.append(({n, n + "-baseline", n + "-baseline-prev"}, n + "-ckpt-"))
            if r.get("raw_vm"):
                groups.append(names_of({"VM_NAME": [r["raw_vm"]], "BASELINE_NAME": [r.get("raw_baseline") or ""]}))
            if not groups:
                raise Refused(f"{owner} can't be read ({r.get('error')}), so its VMs aren't known; fix it first")
        for names, ckpt in groups:
            for d in sorted(doomed):
                if d in names or (ckpt and d.startswith(ckpt)):
                    raise Refused(f"{d} is {owner}'s (its VM, baseline or a copy); nothing was deleted")
            for n in sorted(names):
                if n.startswith(target + "-ckpt-"):
                    raise Refused(f"{owner}'s {n} looks like one of {target}'s checkpoints; nothing was deleted")


def protected(rows, target, home, extra=()):
    """Every folder another VM uses (its keys, runtime, logs, vault, spool), the account's home and the checkout:
    what a VM's own folder must neither be nor hold. A refused settings file counts by the folders it says
    (unchecked); one whose folders can't be read, or aren't absolute, refuses: an unknown folder can't be kept safe."""
    out = list(extra)
    for r in rows:
        if r.get("ok"):
            if r.get("vm") == target:
                continue
            out += [r.get(k) for k in ("ssh_dir", "run_dir", "log_dir", "vault", "spool") if r.get(k)]
            if r.get("kind") in ("local", "defaults", "config") and not r.get("spool"):
                out += [home + "/Library/Application Support/metacomputer/events/spool"]
        else:
            folders = r.get("folders") or []
            if not r.get("known") or not folders or any(not isinstance(f, str) or not f for f in folders):
                raise Refused(f"{r.get('file')} was refused ({r.get('error')}) and its folders can't be read, so "
                              "nothing is deleted that might be one of them; fix it first")
            for f in folders:
                try:
                    absolute(f, home)
                except Refused:
                    raise Refused(f"{r.get('file')} names a folder that isn't an absolute path ({f}), so nothing is "
                                  "deleted that might be it; fix it first")
            out += folders
    return out


# --- deleting one VM's own folder -----------------------------------------------------------------------------
def _open_dir_nofollow(name, dir_fd):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)


def rmtree_own(base, rel, guard, home, check_only=False):
    """Delete base/rel (a VM's own folder) with nothing followed through a link: base is walked from / one folder at
    a time (O_NOFOLLOW openat), then each part of rel; a link anywhere refuses. The folder must be this account's
    and must neither be nor hold anything in guard (another VM's folders, the home folder, the checkout). The
    deletion itself goes through the parent's descriptor (shutil.rmtree with dir_fd, which never follows links).
    Returns False (nothing to do) when the folder isn't there."""
    parts = [c for c in absolute(base, home, "the base folder").split("/") if c]
    relparts = [c for c in rel.split("/") if c]
    if not relparts or any(c in (".", "..") for c in relparts):
        raise Refused(f"not a folder name: {rel}")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        walked = "/"
        for c in parts + relparts[:-1]:
            try:
                nfd = _open_dir_nofollow(c, fd)
            except FileNotFoundError:
                return False
            except OSError:
                raise Refused(f"{os.path.join(walked, c)} is a link or not a folder; nothing there was deleted")
            os.close(fd)
            fd, walked = nfd, os.path.join(walked, c)
        leaf = relparts[-1]
        try:
            st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        target = os.path.join(walked, leaf)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{target} is a link or not a folder; it wasn't deleted")
        if st.st_uid != os.getuid():
            raise Refused(f"{target} isn't this account's; it wasn't deleted")
        me = (st.st_dev, st.st_ino)
        for g in guard:
            try:
                held = me in ancestors(g, home)
            except Refused:
                raise Refused(f"another VM's folder {g!r} isn't an absolute path, so {target} wasn't deleted")
            if held:
                raise Refused(f"{target} is or holds {g}, which another VM (or this account) uses; it wasn't deleted")
        if check_only:
            return True
        st2 = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
        if (st2.st_dev, st2.st_ino) != me:
            raise Refused(f"{target} changed while it was being checked; it wasn't deleted")
        shutil.rmtree(leaf, dir_fd=fd)
        return True
    finally:
        os.close(fd)


def human(n):
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        return "-"
    if n < 1e9:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e9:.1f} GB"


def runner_index(runner):
    """name -> {"state", "bytes"} for every VM the runner lists; skips entries that aren't VMs."""
    out = {}
    for v in runner or []:
        if not isinstance(v, dict) or not isinstance(v.get("name"), str):
            continue
        disk = v.get("diskSize") if isinstance(v.get("diskSize"), dict) else {}
        alloc = disk.get("allocated")
        out[v["name"]] = {"state": str(v.get("status") or "unknown"),
                          "bytes": alloc if isinstance(alloc, int) and not isinstance(alloc, bool) else None}
    return out


def facts(runner, name, baseline):
    idx = runner_index(runner)
    prev = baseline + "-prev"
    pre = name + "-ckpt-"
    ckpts = sorted(n[len(pre):] for n in idx if n.startswith(pre))
    f = {"vm": "present" if name in idx else "absent", "vm_state": idx.get(name, {}).get("state", "absent"),
         "vm_bytes": idx.get(name, {}).get("bytes"), "base": "present" if baseline in idx else "absent",
         "base_bytes": idx.get(baseline, {}).get("bytes"), "prev": "present" if prev in idx else "absent",
         "prev_bytes": idx.get(prev, {}).get("bytes"), "ckpts": " ".join(c for c in ckpts if SAFE.fullmatch(c))}
    return f


def build(rows, runner, selected=None):
    """The listing: this checkout's VMs (one per settings file), the runner's other VMs, and problems."""
    idx = runner_index(runner)
    claimed = set()
    vms, problems = [], []
    seen = {}
    for r in rows:
        name = r.get("vm") or ""
        item = {"name": name or None, "settings": r.get("file"), "kind": r.get("kind"), "error": r.get("error"),
                "selected": bool(r.get("ok") and selected and name == selected)}
        if r.get("ok"):
            base = r.get("baseline") or ""
            prev = base + "-prev"
            pre = name + "-ckpt-"
            ckpts = sorted(n for n in idx if n.startswith(pre))
            claimed.update([name, base, prev, *ckpts])
            item.update({
                "state": idx[name]["state"] if name in idx else ("not built" if runner is not None else "unknown"),
                "disk_bytes": idx.get(name, {}).get("bytes"),
                "baseline": {"name": base, "present": base in idx, "disk_bytes": idx.get(base, {}).get("bytes")},
                "previous_baseline": ({"name": prev, "disk_bytes": idx[prev]["bytes"]} if prev in idx else None),
                "checkpoints": [c[len(pre):] for c in ckpts],
                "ssh_port": r.get("ssh_port"), "vnc_port": r.get("vnc_port"),
                "vault": r.get("vault"), "spool": r.get("spool"), "volumes": r.get("volumes") or ""})
            if name in seen:
                problems.append(f"{name} has two settings files here: {seen[name]} and {r.get('file')}")
            seen.setdefault(name, r.get("file"))
        else:
            problems.append(f"{r.get('file')}: {r.get('error')}")
        vms.append(item)
    # Ports: no two VMs here may share one (a gateway can't start on a taken port).
    ports = {}
    for v in vms:
        for k in ("ssh_port", "vnc_port"):
            p = v.get(k)
            if p:
                ports.setdefault(str(p), []).append(f"{v['name']} ({k[:3].upper()})")
    for p, who in sorted(ports.items()):
        if len(who) > 1:
            problems.append(f"port {p} is used by " + " and ".join(who))
    other = []
    if runner is not None:
        rest = sorted(n for n in idx if n not in claimed)
        owners = {}
        for n in rest:
            m = COPY.match(n)
            if m and m.group("owner") in idx and m.group("owner") not in claimed:
                owners.setdefault(m.group("owner"), []).append(n)
        copies = {c for cs in owners.values() for c in cs}
        for n in rest:
            if n in copies:
                continue
            o = {"name": n, "state": idx[n]["state"], "disk_bytes": idx[n]["bytes"], "copies": []}
            for c in sorted(owners.get(n, [])):
                o["copies"].append({"name": c, "disk_bytes": idx[c]["bytes"]})
            other.append(o)
    return {"vms": vms, "other": other, "selected": selected or None, "runner_ok": runner is not None,
            "problems": problems}


def render(listing):
    lines = []
    head = ("", "VM", "STATE", "DISK", "BASELINE", "SSH", "VNC", "SETTINGS")
    rows = []
    for v in listing["vms"]:
        if v.get("error"):
            rows.append(("", esc(v.get("name") or "?"), "settings refused", "", "", "", "", esc(v.get("settings") or "")))
            continue
        b = v["baseline"]
        base = human(b["disk_bytes"]) if b["present"] else "none"
        if v["checkpoints"]:
            base += f" +{len(v['checkpoints'])} ckpt"
        rows.append(("*" if v["selected"] else "", esc(v["name"]), esc(v["state"]), human(v["disk_bytes"]), base,
                     esc(str(v["ssh_port"])), esc(str(v["vnc_port"])), esc(v["settings"] or "")))
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    for r in [head, *rows]:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    if listing["selected"]:
        lines.append(f"* the VM these settings act on ({esc(listing['selected'])}); another: metacomputer --vm NAME ...")
    else:
        lines.append("no VM is chosen here without --vm NAME (this checkout has no config/local.env, and there's more "
                     "than one to choose from)")
    if not listing["runner_ok"]:
        lines.append("! couldn't list the VMs with the runner: states and disk use are unknown")
    if listing["other"]:
        lines.append("")
        lines.append("Other VMs in the runner (no settings for them in this checkout):")
        orows = []
        for o in listing["other"]:
            base = next((c for c in o["copies"] if c["name"] == o["name"] + "-baseline"), None)
            more = len(o["copies"]) - (1 if base else 0)
            orows.append(("", esc(o["name"]), esc(o["state"]), human(o["disk_bytes"]),
                          (human(base["disk_bytes"]) if base else "none") + (f" +{more} more" if more else "")))
        ohead = ("", "VM", "STATE", "DISK", "BASELINE")
        ow = [max(len(r[i]) for r in [ohead, *orows]) for i in range(len(ohead))]
        for r in [ohead, *orows]:
            lines.append("  ".join(c.ljust(ow[i]) for i, c in enumerate(r)).rstrip())
    for p in listing["problems"]:
        lines.append(f"! {esc(p)}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vms.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--settings", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--runner")
    g.add_argument("--runner-failed", action="store_true")
    p.add_argument("--selected", default="")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("facts")
    p.add_argument("name")
    p.add_argument("baseline")
    p = sub.add_parser("identity")
    p.add_argument("--home", required=True)
    p = sub.add_parser("delete-check")
    p.add_argument("--target", required=True)
    p.add_argument("--baseline", required=True)
    p = sub.add_parser("error-row")
    p.add_argument("--file", required=True)
    p.add_argument("--vm", default="")
    p.add_argument("--error", default="")
    p = sub.add_parser("rmtree")
    p.add_argument("--base", required=True)
    p.add_argument("--rel", required=True)
    p.add_argument("--target-vm", required=True)
    p.add_argument("--home", required=True)
    p.add_argument("--guard", action="append", default=[])
    p.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "error-row":
        print(json.dumps(error_row(read_records(sys.stdin), a.file, a.vm, a.error)))
        return 0
    if a.cmd in ("identity", "delete-check", "rmtree"):
        try:
            if a.cmd == "identity":
                for k, v in check_identity(read_records(sys.stdin), a.home).items():
                    print(f"{k}={v}")
            elif a.cmd == "delete-check":
                delete_check(rows_from(sys.stdin), a.target, a.baseline)
            else:
                guard = protected(rows_from(sys.stdin), a.target_vm, a.home, a.guard)
                done = rmtree_own(a.base, a.rel, guard, a.home, check_only=a.check)
                print("present" if done else "absent")
        except Refused as e:
            print(str(e))
            return 1
        return 0
    if a.cmd == "facts":
        try:
            runner = json.load(sys.stdin)
        except ValueError:
            return 1
        if not isinstance(runner, list):
            return 1
        for k, v in facts(runner, a.name, a.baseline).items():
            print(f"{k}={'' if v is None else v}")
        return 0
    rows = []
    with open(a.settings) as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                rows.append(d)
    runner = None
    if not a.runner_failed:
        try:
            with open(a.runner) as f:
                runner = json.load(f)
            if not isinstance(runner, list):
                runner = None
        except (OSError, ValueError):
            runner = None
    listing = build(rows, runner, a.selected or None)
    print(json.dumps(listing, indent=2) if a.json else render(listing))
    return 0 if listing["runner_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
