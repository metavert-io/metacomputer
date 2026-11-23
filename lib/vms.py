#!/usr/bin/env python3
"""metacomputer's VMs on this Mac: what `metacomputer vm list` shows, and the facts `vm delete` checks.
bin/metacomputer (scripts/vm-commands.sh) gathers this checkout's settings files, each resolved the way every
command resolves them (scripts/lib.sh), and the runner's list (`lume ls --format json`); this merges the two.

    vms.py list --settings ROWS.jsonl (--runner LIST.json | --runner-failed) [--selected NAME] [--default NAME]
               [--default-problem TEXT] [--overridden-by TEXT] [--on PLACE] [--json]
                                               `metacomputer list` / `vm list`: every computer, where it is (all on
                                               this Mac: lib/placement.py), the one these settings act on, the default
    vms.py facts NAME BASELINE < LIST.json      KEY=VALUE lines: the VM, its baseline, its previous one, checkpoints
    vms.py identity --home HOME [--no-ports] < RECORDS
                                               the identity rule: one VM's names, ports and folders against every
                                               other VM's, and every VM's folders against the runner's VM storage
    vms.py delete-check --target NAME --baseline B --home HOME < ROWS.jsonl
                                               what `vm delete` would delete, against every other VM's names

Nothing here changes anything: vm delete's delete of a VM's own folders is lib/safedelete.py's (`own`), checked
against the same protected folders as every other delete. Names from the runner are escaped on a terminal
(lib/termsafe.py)."""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from termsafe import escape_for_terminal as esc  # noqa: E402

COPY = re.compile(r"^(?P<owner>.+?)(?:-baseline(?:-prev)?|-ckpt-(?P<ckpt>.+))$")
SAFE = re.compile(r"[A-Za-z0-9._-]{1,128}")
PATH_KEYS = ("SSH_DIR", "RUN_DIR", "LOG_DIR", "VAULT_DIR", "EVENTS_SPOOL")
# The account's own vault folder and event spool, which the main VM and MCOMP_CONFIG VMs share unless their settings
# give them their own (as before there was more than one VM): each such VM's folder, but not one they keep apart.
SHARED_KEYS = ("SHARED_VAULT_DIR", "SHARED_EVENTS_SPOOL")
FOLDER_KEYS = PATH_KEYS + SHARED_KEYS
# Places no VM's folder may be in or hold (scripts/lib.sh mc_identity_input sends them as "@NAME PATH value" records;
# "@runner ERROR reason" when its storage can't be read): only the runner's VM storage, where the runner's own delete
# of a VM takes its whole folder. Every other recursive delete checks its own target (lib/safedelete.py), and
# tests/test_multi_vm.py (delete_inventory) fails on one that doesn't.
PLACES = {"@runner": "the runner's VM storage"}
RUNNER = "@runner"


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strictargs  # noqa: E402  (argparse that refuses what a command wouldn't use)


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


def physical(path, home, what="a folder"):
    """(identity, chain) of the folder path physically is. The part that exists is resolved with every link at every
    level followed (realpath), then the part that doesn't exist yet is added. A folder's identity is (device, inode)
    of its nearest existing folder plus the rest of the path (without case, as macOS's volumes compare names); its
    chain holds the identities of it and of every folder physically above it, so B's identity is in A's chain exactly
    when A is B or is inside it, however either is spelled (a link anywhere above, a firmlink, `x/.`, `..`, `//`).
    Anything that can't be read (a folder it may not search, a loop of links) refuses: it isn't known."""
    if not isinstance(path, str) or not path:
        raise Refused(f"{what} is empty")
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    if not path.startswith("/"):
        raise Refused(f"{what} must be an absolute folder, not {path!r}")
    try:
        parts = [c for c in os.path.realpath(path).split("/") if c]
    except (OSError, ValueError) as e:
        raise Refused(f"can't read {what} ({path}): {e}")
    k = len(parts)
    while True:
        try:
            st = os.stat("/" + "/".join(parts[:k]))
            break
        except (FileNotFoundError, NotADirectoryError):
            if k == 0:
                raise Refused(f"can't read {what} ({path})")
            k -= 1
        except OSError as e:
            raise Refused(f"can't read {what} ({path}): {e.strerror}")
    ids = []
    for j in range(k + 1):
        try:
            sj = os.stat("/" + "/".join(parts[:j]))
        except OSError as e:
            raise Refused(f"can't read {what} ({path}): {e.strerror}")
        ids.append((sj.st_dev, sj.st_ino, ()))
    for j in range(k + 1, len(parts) + 1):
        ids.append((st.st_dev, st.st_ino, tuple(c.casefold() for c in parts[k:j])))
    return ids[-1], set(ids)


def identity(path, home, what="a folder"):
    """The folder's identity (physical): two spellings of one folder get the same one."""
    return physical(path, home, what)[0]


def chain(path, home, what="a folder"):
    """The identities of the folder and of every folder physically above it (physical)."""
    return physical(path, home, what)[1]


def canonical(path, home, what="a folder"):
    """path with the links in the part that exists resolved (what the vault is handed)."""
    return os.path.realpath(absolute(path, home, what))


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


def shown(k):
    return k.replace("SHARED_", "")


def folders_of(ids, home, who):
    """[(KEY, value, identity, chain)] for an owner's folders."""
    out = []
    for k in FOLDER_KEYS:
        for v in ids.get(k, []):
            out.append((k, v, *physical(v, home, f"{who} {shown(k)}")))
    return out


def places_of(owners, home):
    """The places no VM's folder may be in or hold ("@NAME PATH value" records), as (label, value, identity, chain).
    Fails closed: the runner's storage must be listed, and every place must resolve."""
    out = []
    for owner, label in PLACES.items():
        recs = owners.pop(owner, {})
        for reason in recs.get("ERROR", []):
            raise Refused(f"{label} can't be known ({reason}), so no VM's folder can be checked against it")
        paths = [v for v in recs.get("PATH", [])]
        if owner == RUNNER and not paths:
            raise Refused(f"{label} isn't known (the runner listed no storage location), so no VM's folder can be "
                          "checked against it")
        for v in paths:
            out.append((label, v, *physical(v, home, label)))
    for owner in [o for o in owners if o.startswith("@")]:
        raise Refused(f"unknown place {owner}")
    return out


def relation(a, b):
    """'is' when folder a is folder b, 'is inside' when a is in b, 'holds' when b is in a, else None
    (a and b are (KEY, value, identity, chain))."""
    if a[2] == b[2]:
        return "is"
    if b[2] in a[3]:
        return "is inside"
    if a[2] in b[3]:
        return "holds"
    return None


def check_identity(owners, home, ports=True):
    """The identity rule, for one VM ("self") against every other: no name of one is a name or checkpoint of another,
    no port is shared (unless ports=False: deleting a VM ends a clash), no folder of its (SSH_DIR, RUN_DIR, LOG_DIR,
    VAULT_DIR, EVENTS_SPOOL) is, is inside or holds another's, compared physically, and no VM's folder (its own or
    another's) is in or holds a place in PLACES (the runner's VM storage and the other roots something deletes in on
    its own); a place that can't be known refuses. The account's shared vault
    and spool (SHARED_*) may be shared by the VMs that use them, and nothing else. Another VM whose settings can't be
    read refuses: its folders aren't known. Returns self's vault folder and spool as their canonical paths."""
    places = places_of(owners, home)
    me = owners.pop("self", None)
    if not me:
        raise Refused("nothing to check")
    my_names, my_ckpt = names_of(me)
    my_ports = {p for k in ("SSH_PORT", "VNC_PORT") for p in me.get(k, []) if p} if ports else set()
    mine = folders_of(me, home, "its")

    def in_storage(f, who):
        for p in places:
            rel = relation(f, p)
            if rel:
                raise Refused(f"{who} {shown(f[0])} ({f[1]}) {rel} {p[0]} ({p[1]}); no VM's folder may be in it or "
                              "hold it (what's in it gets deleted on its own)")
    for f in mine:
        in_storage(f, "its")
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
        try:
            theirs = folders_of(ids, home, f"{owner}'s")
        except Refused as e:
            raise Refused(f"{e}: {owner}'s folders aren't known, so this VM can't be checked against them; fix it first")
        for t in theirs:
            in_storage(t, f"{owner}'s")
            for f in mine:
                if f[0] in SHARED_KEYS and t[0] in SHARED_KEYS:
                    continue                  # the account's own vault and spool, which both use (as before)
                rel = relation(f, t)
                if rel == "is":
                    raise Refused(f"its {shown(f[0])} ({f[1]}) is the same folder as {owner}'s {shown(t[0])} ({t[1]}); "
                                  "every VM needs its own")
                if rel:
                    raise Refused(f"its {shown(f[0])} ({f[1]}) {rel} {owner}'s {shown(t[0])} ({t[1]}); no VM's folder "
                                  "may be in another's or hold one")
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
            "folders": [v for k in FOLDER_KEYS for v in ids.get(k, [])] if known else [],
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
            if not r.get("known"):
                # Its names can't be read, and the file's own name isn't enough (it could set any VM's): nothing that
                # might be one of them is deleted. (vm delete has checked that every file reads cleanly by now.)
                raise Refused(f"{owner} can't be read ({r.get('error')}), so its VMs aren't known; fix it first")
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


def build(rows, runner, selected=None, default=None, on=None, default_problem=None, overridden_by=None):
    """The listing: this checkout's VMs (one per settings file), the runner's other VMs, and problems. Each VM has a
    place (every one here is on this Mac) and says whether it's the default computer (`default`, a name); `on`
    keeps only the computers at that place (lib/placement.py), and one with none there says so in `note`."""
    # Imported here, not above: lib/safedelete.py imports this module for the identity rule, under any python3.
    import placement
    idx = runner_index(runner)
    claimed = set()
    vms, problems = [], []
    seen = {}
    for r in rows:
        name = r.get("vm") or ""
        place = r.get("place") or placement.MAC
        item = {"name": name or None, "settings": r.get("file"), "kind": r.get("kind"), "error": r.get("error"),
                "selected": bool(r.get("ok") and selected and name == selected),
                "place": place, "where": placement.label(place),
                "default": bool(r.get("ok") and default and name == default)}
        if r.get("ok"):
            base = r.get("baseline") or ""
            prev = base + "-prev"
            pre = name + "-ckpt-"
            ckpts = sorted(n for n in idx if n.startswith(pre))
            claimed.update([name, base, prev, *ckpts])
            remote = place != placement.MAC
            if remote:
                # A computer elsewhere: not one of the runner's VMs. Its SSH answering or not (lib/remote.py probe).
                state = {"reachable": "running", "unreachable": "unreachable"}.get(r.get("remote_state"),
                                                                                   r.get("remote_state") or "unknown")
            item.update({
                "state": state if remote else (idx[name]["state"] if name in idx else ("not built" if runner is not None else "unknown")),
                "disk_bytes": idx.get(name, {}).get("bytes"),
                "baseline": {"name": base, "present": base in idx, "disk_bytes": idx.get(base, {}).get("bytes")},
                "previous_baseline": ({"name": prev, "disk_bytes": idx[prev]["bytes"]} if prev in idx else None),
                "checkpoints": [c[len(pre):] for c in ckpts],
                "ssh_port": r.get("ssh_port"), "vnc_port": r.get("vnc_port"),
                "provider": r.get("provider") if remote else None,
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
            o = {"name": n, "state": idx[n]["state"], "disk_bytes": idx[n]["bytes"], "copies": [],
                 "place": placement.MAC, "where": placement.label(placement.MAC)}
            for c in sorted(owners.get(n, [])):
                o["copies"].append({"name": c, "disk_bytes": idx[c]["bytes"]})
            other.append(o)
    # A VM named like a place (made by hand: vm create refuses those names) is never what --on means.
    for v in vms:
        if v.get("name") and placement.place_name(v["name"]):
            problems.append(f"the VM {v['name']} has a place's name: --on {v['name'].lower()} means the place, never "
                            f"this VM; name it with --vm {v['name']}")
    # The default computer (config/default-vm's, or the main VM's) must be one of these, with settings that load.
    broken = bool(default_problem)
    if default and not any(v["default"] for v in vms):
        broken = True
        if default_problem:
            pass                                     # why, as scripts/lib.sh found it (below)
        elif any(v.get("error") and v.get("name") == default for v in vms):
            problems.append(f"your default computer, {default} (config/default-vm), has settings that are refused "
                            "(above), so a command that names no VM refuses: metacomputer default NAME chooses another")
        else:
            problems.append(f"config/default-vm makes {default} your default computer, but this checkout has no "
                            f"settings for it, so a command that names no VM refuses: metacomputer default NAME "
                            "chooses another")
    if default_problem:
        problems.append(default_problem)
    note = None
    if on:
        where = on.strip().lower()
        vms = [v for v in vms if placement.is_place(where) and placement.at(v["place"], where)]
        other = [o for o in other if placement.is_place(where) and placement.at(o["place"], where)]
        if not vms and not other:
            note = placement.nothing_at(on)
    return {"vms": vms, "other": other, "selected": selected or None, "default": default or None,
            "default_broken": broken, "overridden_by": overridden_by or None, "on": on or None, "note": note,
            "runner_ok": runner is not None, "problems": problems}


def render(listing):
    lines = []
    head = ("", "VM", "WHERE", "STATE", "DISK", "BASELINE", "SSH", "VNC", "SETTINGS")
    rows = []
    for v in listing["vms"]:
        if v.get("error"):
            rows.append(("", esc(v.get("name") or "?"), esc(v["where"]), "settings refused", "", "", "", "",
                         esc(v.get("settings") or "")))
            continue
        b = v["baseline"]
        base = human(b["disk_bytes"]) if b["present"] else "none"
        if v["checkpoints"]:
            base += f" +{len(v['checkpoints'])} ckpt"
        here = v.get("place", "mac") == "mac"    # a computer elsewhere: SSH goes to its own address, not a port here
        rows.append(("*" if v["selected"] else "", esc(v["name"]), esc(v["where"]), esc(v["state"]),
                     human(v["disk_bytes"]), base, esc(str(v["ssh_port"])) if here else "-", esc(str(v["vnc_port"])),
                     esc(v["settings"] or "")))
    if listing.get("note"):
        lines.append(esc(listing["note"]))
        for p in listing["problems"]:
            lines.append(f"! {esc(p)}")
        return "\n".join(lines)
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    for r in [head, *rows]:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    sel, dflt = listing["selected"], listing.get("default")
    if sel and sel == dflt:
        lines.append(f"* your default computer ({esc(sel)}): what a command acts on when no VM is named; another: "
                     "metacomputer --vm NAME ..., or metacomputer default NAME to change it")
    elif sel:
        by = f", chosen by {esc(listing['overridden_by'])}" if listing.get("overridden_by") else ""
        about = ""
        if dflt and listing.get("default_broken"):
            about = f" Your default computer, {esc(dflt)}, doesn't resolve (below)."
        elif dflt:
            about = f" Your default computer is {esc(dflt)} (metacomputer default)."
        lines.append(f"* the VM these settings act on ({esc(sel)}{by}); another: metacomputer --vm NAME ...{about}")
    elif listing.get("default_broken"):
        lines.append("no VM is chosen here without --vm NAME: your default computer doesn't resolve (below)")
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
    ap = strictargs.Parser(prog="vms.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--settings", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--runner")
    g.add_argument("--runner-failed", action="store_true")
    p.add_argument("--selected", default="")
    p.add_argument("--default", default="")
    p.add_argument("--default-problem", default="")
    p.add_argument("--overridden-by", default="", action="text")     # what chose the VM: "--vm NAME", "MCOMP_VM=NAME"
    p.add_argument("--on", default="")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("facts")
    p.add_argument("name")
    p.add_argument("baseline")
    p = sub.add_parser("identity")
    p.add_argument("--home", required=True)
    p.add_argument("--no-ports", action="store_true")
    p = sub.add_parser("delete-check")
    p.add_argument("--target", required=True)
    p.add_argument("--baseline", required=True)
    p = sub.add_parser("error-row")
    p.add_argument("--file", required=True)
    p.add_argument("--vm", default="")
    p.add_argument("--error", default="")
    sub.add_parser("all-known")
    a = ap.parse_args(argv)
    if a.cmd == "all-known":
        # Every other settings file must read cleanly (mc_identity_records): one that can't is unknown, and nothing
        # that might be one of its folders may be deleted.
        bad = [o for o, ids in read_records(sys.stdin).items() if ids.get("UNREADABLE") and not o.startswith("@")]
        if bad:
            print(", ".join(bad))
            return 1
        return 0
    if a.cmd == "error-row":
        print(json.dumps(error_row(read_records(sys.stdin), a.file, a.vm, a.error)))
        return 0
    if a.cmd in ("identity", "delete-check"):
        try:
            if a.cmd == "identity":
                for k, v in check_identity(read_records(sys.stdin), a.home, ports=not a.no_ports).items():
                    print(f"{k}={v}")
            else:
                delete_check(rows_from(sys.stdin), a.target, a.baseline)
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
    listing = build(rows, runner, a.selected or None, a.default or None, a.on or None, a.default_problem or None,
                    a.overridden_by or None)
    print(json.dumps(listing, indent=2) if a.json else render(listing))
    return 0 if listing["runner_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
