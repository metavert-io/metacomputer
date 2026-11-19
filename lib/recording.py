#!/usr/bin/env python3
"""Desktop recordings on the Mac (`metacomputer record`): description files, streams, where they go.

The description file written beside each recording, the Mac's half of a streamed one, and what
`status` and `security` say about where recordings go.

  recording.py sidecar MP4 --name NAME --started EPOCH --ended EPOCH --fps N
                       --quality Q --mode guest|host --vm VM [--actions-dir DIR] [--ended-early]
  recording.py moments MP4.json          print a recording's key moments
  recording.py probe FILE                print its length in seconds (exit 1 if unreadable)
  recording.py save OUT [--min-free-gb G] -- CMD...
                                         run CMD (the SSH command that streams from the VM,
                                         RECORD_TO=host) and save its output to OUT as it arrives

The sidecar (MP4 with .json in place of .mp4) says what the file is: when it
started and ended, its settings, size, length, sound track (if any) and SHA-256. When the signed action
log is on (ACTION_LOG=on), it also lists the log files that cover the recording
and its key moments: every tool call made on this VM while it was recording, with
its time in the video, so a moment can be found without watching it all
("0:42 launch_app inkscape"). Times are the Mac's clock, to about a second. A call
that didn't run is marked as the action log records it: `refused` (refused by a
guard on the Mac, or a shell run refused) or `error` (it came back as an error,
which includes Cua Driver's own refusals in bounded mode).

`save` is the Mac's half of a streamed recording (RECORD_TO=host, the default). What
arrives is on the Mac at once, out of reach of anything in the VM; the VM decides
how much it sends, so the copy stops, keeping what arrived, before the Mac's free
space falls below --min-free-gb (IMPORT_MIN_FREE_GB, as for every copy from the VM).
"""
import argparse
import datetime
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

GIB = 1 << 30
SAVE_CHECK_EVERY = 64 << 20     # bytes saved between free-space checks (and one before the first)

# Arguments worth showing in a moment, in order; the rest of a call is in the action log.
SHOW = ("app", "name", "bundle_id", "title", "url", "key", "keys", "action", "path", "role",
        "x", "y", "text", "value", "script")
STEP_LIST = ("steps", "actions")


def probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60).stdout
        return round(float(out.strip()), 2)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def probe_audio(path):
    """The first sound track's codec, rate and channels, or None for a silent video."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=codec_name,sample_rate,channels", "-of", "json", path],
                             capture_output=True, text=True, timeout=60).stdout
        st = (json.loads(out or "{}").get("streams") or [None])[0]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not st:
        return None
    return {"codec": st.get("codec_name"), "sample_rate": int(st.get("sample_rate") or 0),
            "channels": st.get("channels"), "source": "the VM's virtual sound card (VIRTUAL_AUDIO=on)"}


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
            open_calls = {}   # call id -> its moment, until the call's result says how it ended
            try:
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(rec, dict):
                            continue
                        if i == 0:
                            if rec.get("type") != "start" or (rec.get("session") or {}).get("vm") != vm:
                                break
                            continue
                        if rec.get("type") == "result":
                            # The log's own record of how a call ended, as `actions show` reads it: refused (the
                            # shell refusing after the call was logged) or an error (the driver refusing it, in
                            # bounded mode for one, or failing). Such a call is never shown as one that ran.
                            m = open_calls.pop(json.dumps(rec.get("id")), None)
                            if m is not None:
                                res = rec.get("result") if isinstance(rec.get("result"), dict) else {}
                                if res.get("status") == "refused":
                                    m["refused"] = True
                                elif rec.get("rpc_error") or res.get("error"):
                                    m["error"] = True
                            continue
                        if rec.get("type") not in ("call", "refused"):
                            continue
                        t = parse_ts(rec.get("ts"))
                        if t is None or t < started - 1 or t > ended + 1:
                            continue
                        used = True
                        m = {"t": round(max(0.0, t - started), 1), "at": rec["ts"],
                             "tool": rec.get("tool"), "what": describe(rec.get("args")),
                             **({"refused": True} if rec["type"] == "refused" else {}),
                             "log": f"{day}/{fn}", "seq": rec.get("seq")}
                        found.append(m)
                        if rec["type"] == "call":
                            open_calls[json.dumps(rec.get("id"))] = m
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
        "audio": probe_audio(a.mp4),
        "bytes": os.path.getsize(a.mp4),
        "sha256": sha256(a.mp4),
        "clock": "the Mac's; moments are within about a second",
        **({"stream_ended_early": "the VM stopped sending before record stop (its recorder ended or was "
                                  "stopped, the VM went away, or the Mac's disk reached its reserve); the "
                                  "video covers what arrived until then"} if a.ended_early else {}),
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


def settings_summary(env=None):
    """Where recordings go, for `metacomputer status` and `security` (bin/metacomputer record_env sets these)."""
    env = os.environ if env is None else env
    to = env.get("MCOMP_RECORD_TO") or "host"
    every = env.get("MCOMP_TASK_SYNC_INTERVAL") or "15"
    now = env.get("MCOMP_RECORDING_NOW") or ""
    return {"desktop_to": to if to in ("host", "guest") else "invalid",
            "desktop_recording": now if now and len(now) <= 128 and all(c.isalnum() or c in "._-" for c in now) else None,
            # seconds between copies of a task's finished recording turns; 0 = only at task finish; None = not a number
            "task_sync_interval_s": int(every) if every.isdigit() and len(every) <= 5 else None}


def status_line(d):
    """One line for `metacomputer status`."""
    now = f"   recording now: {d['desktop_recording']}" if d.get("desktop_recording") else ""
    if d["desktop_to"] == "host":
        return f"recordings    desktop ones stream to this Mac as they record (RECORD_TO=host){now}"
    if d["desktop_to"] == "guest":
        return ("recordings    \033[33mdesktop ones stay in the VM until record stop\033[0m (RECORD_TO=guest): anything "
                f"running there can delete them first{now}")
    return "recordings    \033[31mRECORD_TO isn't host or guest\033[0m: record start refuses"


def security_lines(d):
    """The recordings section of `metacomputer security`."""
    y = "\033[33m{}\033[0m".format
    lines = ['Recordings (SECURITY.md, "Recordings")']
    if d["desktop_to"] == "host":
        lines.append("  desktop      to this Mac as it records (RECORD_TO=host): what arrived is out of the VM's reach")
    elif d["desktop_to"] == "guest":
        lines.append(f"  desktop      {y('in the VM until record stop')} (RECORD_TO=guest): anything running there can "
                     "delete it first; RECORD_TO=host streams it to this Mac")
    else:
        lines.append(f"  desktop      {y('RECORD_TO is not host or guest')}: record start refuses")
    n = d.get("task_sync_interval_s")
    if n:
        lines.append(f"  per task     Cua Driver's, in the VM: finished turns copied to this Mac every {n} s while a task "
                     "folder is open (TASK_SYNC_INTERVAL), the video at task finish")
    elif n == 0:
        lines.append(f"  per task     Cua Driver's, in the VM: {y('copied only at task finish')} (TASK_SYNC_INTERVAL=0)")
    else:
        lines.append(f"  per task     {y('TASK_SYNC_INTERVAL is not a number of seconds')}: copied only at task finish")
    return lines


def cmd_save(a):
    cmd = a.command
    if not cmd:
        print("recording.py save: no command after --", file=sys.stderr)
        return 2
    min_free = int(max(0.0, a.min_free_gb) * GIB)
    try:
        # A new file only: never an existing one, never through a link.
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o644)
    except OSError as e:
        print(f"metacomputer: can't save the recording as {a.out}: {e.strerror}", file=sys.stderr)
        return 1
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    except OSError as e:
        os.close(fd)
        os.unlink(a.out)
        print(f"metacomputer: can't start the stream: {e.strerror}", file=sys.stderr)
        return 1
    stopped = []

    def stop(why):
        stopped.append(why)
        try:
            proc.terminate()      # the stream ends; what arrived stays
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop("signal"))
    rc, since = 0, SAVE_CHECK_EVERY
    try:
        src = proc.stdout.fileno()
        while True:
            if since >= SAVE_CHECK_EVERY:
                since = 0
                st = os.fstatvfs(fd)
                if st.f_bavail * st.f_frsize - SAVE_CHECK_EVERY < min_free:
                    print(f"metacomputer: the recording stopped saving here: the Mac would have less than "
                          f"{min_free / GIB:g} GiB free (IMPORT_MIN_FREE_GB); what arrived before is kept",
                          file=sys.stderr, flush=True)
                    stop("space")
                    rc = 3
                    break
            b = os.read(src, 1 << 20)
            if not b:
                break
            view = memoryview(b)
            while view:
                view = view[os.write(fd, view):]
            since += len(b)
    finally:
        os.close(fd)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return rc or (0 if stopped else (proc.returncode if proc.returncode and proc.returncode > 0 else 0))


def cmd_moments(a):
    side = json.load(open(a.json, encoding="utf-8"))
    ms = side.get("moments") or []
    if not ms:
        print("no key moments (the action log was off or nothing was called while recording)")
        return
    # A description file can come from the VM (recordings --sync): its words are escaped.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from termsafe import escape_for_terminal as esc
    for m in ms:
        how = " (refused)" if m.get("refused") else " (error)" if m.get("error") else ""
        print(esc(f"{fmt_t(m['t']):>8}  {m['tool']}{how}  {m.get('what', '')}".rstrip()))


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
    s.add_argument("--ended-early", action="store_true", help="the stream ended before record stop")
    sv = sub.add_parser("save")
    sv.add_argument("out")
    sv.add_argument("--min-free-gb", type=float, default=10.0)
    m = sub.add_parser("moments")
    m.add_argument("json")
    pr = sub.add_parser("probe")
    pr.add_argument("file")
    argv, command = sys.argv[1:], []
    if argv[:1] == ["save"] and "--" in argv:     # save's command: everything after the first --, untouched
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    a = p.parse_args(argv)
    a.command = command
    return {"sidecar": cmd_sidecar, "moments": cmd_moments, "probe": cmd_probe, "save": cmd_save}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
