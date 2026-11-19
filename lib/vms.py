#!/usr/bin/env python3
"""metacomputer's VMs on this Mac: what `metacomputer vm list` shows, and the facts `vm delete` checks.
bin/metacomputer (scripts/vm-commands.sh) gathers this checkout's settings files, each resolved the way every
command resolves them (scripts/lib.sh), and the runner's list (`lume ls --format json`); this merges the two.

    vms.py list --settings ROWS.jsonl (--runner LIST.json | --runner-failed) [--selected NAME] [--json]
    vms.py facts NAME BASELINE < LIST.json      KEY=VALUE lines: the VM, its baseline, its previous one, checkpoints

Changes nothing. Names from the runner are escaped on a terminal (lib/termsafe.py)."""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from termsafe import escape_for_terminal as esc  # noqa: E402

COPY = re.compile(r"^(?P<owner>.+?)(?:-baseline(?:-prev)?|-ckpt-(?P<ckpt>.+))$")
SAFE = re.compile(r"[A-Za-z0-9._-]{1,128}")


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
    a = ap.parse_args(argv)
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
