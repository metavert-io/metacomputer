#!/usr/bin/env python3
"""The description file written beside each desktop recording (`metacomputer record`).

  recording.py sidecar MP4 --name NAME --started EPOCH --ended EPOCH --fps N
                       --quality Q --mode guest|host --vm VM [--actions-dir DIR]
  recording.py moments MP4.json          print a recording's key moments
  recording.py probe FILE                print its length in seconds (exit 1 if unreadable)

The sidecar (MP4 with .json in place of .mp4) says what the file is: when it
started and ended, its settings, size, length and SHA-256. When the signed action
log is on (ACTION_LOG=on), it also lists the log files that cover the recording
and its key moments: every tool call made on this VM while it was recording, with
its time in the video, so a moment can be found without watching it all
("0:42 launch_app inkscape"). Times are the Mac's clock, to about a second.
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time

# Arguments worth showing in a moment, in order; the rest of a call is in the action log.
SHOW = ("app", "name", "bundle_id", "title", "url", "key", "keys", "action", "path", "role",
        "x", "y", "text", "value")
STEP_LIST = ("steps", "actions")


def probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60).stdout
        return round(float(out.strip()), 2)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_ts(ts):
    try:
        return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp() if ts else None
    except ValueError:
        return None


def describe(args):
    """A short, readable summary of a call's arguments (redacted text stays redacted)."""
    if not isinstance(args, dict):
        return ""
    parts = []
    for k in SHOW:
        v = args.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, dict) and v.get("redacted"):
            v = f"<{v.get('chars', '?')} chars>"
        v = str(v).replace("\n", " ")
        parts.append(f"{k}={v[:60] + '…' if len(v) > 60 else v}")
    for k in STEP_LIST:
        if isinstance(args.get(k), list):
            parts.append(f"{len(args[k])} {k}")
    return " ".join(parts)[:200]


def moments(actions_dir, vm, started, ended):
    """Tool calls on this VM between started and ended, from the action log."""
    found, files = [], []
    if not actions_dir or not os.path.isdir(actions_dir):
        return found, files
    days = {time.strftime("%Y-%m-%d", time.localtime(t)) for t in (started, ended)}
    for day in sorted(days):
        d = os.path.join(actions_dir, day)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(d, fn)
            if os.path.getmtime(path) < started - 2:
                continue
            used = False
            try:
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if i == 0:
                            if rec.get("type") != "start" or (rec.get("session") or {}).get("vm") != vm:
                                break
                            continue
                        if rec.get("type") not in ("call", "refused"):
                            continue
                        t = parse_ts(rec.get("ts"))
                        if t is None or t < started - 1 or t > ended + 1:
                            continue
                        used = True
                        found.append({"t": round(max(0.0, t - started), 1), "at": rec["ts"],
                                      "tool": rec.get("tool"), "what": describe(rec.get("args")),
                                      **({"refused": True} if rec["type"] == "refused" else {}),
                                      "log": f"{day}/{fn}", "seq": rec.get("seq")})
            except OSError:
                continue
            if used:
                files.append(f"{day}/{fn}")
    found.sort(key=lambda m: (m["t"], m["log"], m["seq"] or 0))
    return found, files


def fmt_t(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def cmd_sidecar(a):
    out = a.mp4[:-4] + ".json" if a.mp4.endswith(".mp4") else a.mp4 + ".json"
    found, files = moments(a.actions_dir, a.vm, a.started, a.ended)
    side = {
        "name": a.name,
        "kind": "desktop recording (metacomputer record)",
        "file": os.path.basename(a.mp4),
        "vm": a.vm,
        "mode": a.mode,
        "fps": a.fps,
        "quality": a.quality,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(a.started)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(a.ended)),
        "duration_s": probe_duration(a.mp4),
        "bytes": os.path.getsize(a.mp4),
        "sha256": sha256(a.mp4),
        "clock": "the Mac's; moments are within about a second",
        "action_log": ({"files": files, "verify": "metacomputer actions verify " + " ".join(files)} if files
                       else "none (ACTION_LOG=off, or no tool calls on this VM while recording)"),
        "moments": found,
    }
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(side, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, out)
    print(out)


def cmd_probe(a):
    d = probe_duration(a.file)
    if d is None or d <= 0:
        return 1
    print(d)


def cmd_moments(a):
    side = json.load(open(a.json, encoding="utf-8"))
    ms = side.get("moments") or []
    if not ms:
        print("no key moments (the action log was off or nothing was called while recording)")
        return
    for m in ms:
        print(f"{fmt_t(m['t']):>8}  {m['tool']}{' (refused)' if m.get('refused') else ''}  {m.get('what', '')}".rstrip())


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sidecar")
    s.add_argument("mp4")
    s.add_argument("--name", required=True)
    s.add_argument("--started", type=float, required=True)
    s.add_argument("--ended", type=float, required=True)
    s.add_argument("--fps", type=int, required=True)
    s.add_argument("--quality", required=True)
    s.add_argument("--mode", required=True, choices=("guest", "host"))
    s.add_argument("--vm", required=True)
    s.add_argument("--actions-dir", default="")
    m = sub.add_parser("moments")
    m.add_argument("json")
    pr = sub.add_parser("probe")
    pr.add_argument("file")
    a = p.parse_args()
    return {"sidecar": cmd_sidecar, "moments": cmd_moments, "probe": cmd_probe}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
