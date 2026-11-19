#!/usr/bin/env python3
"""Desktop recordings on the Mac (`metacomputer record`): description files, streams, where they go.

The description file written beside each recording, the Mac's half of a streamed one, and what
`status` and `security` say about where recordings go.

  recording.py sidecar MP4 --name NAME --started EPOCH --ended EPOCH --fps N
                       --quality Q --mode guest|host --vm VM [--actions-dir DIR] [--ended-early]
  recording.py moments MP4.json          print a recording's key moments
  recording.py probe FILE                print its length in seconds (exit 1 if unreadable)
  recording.py save OUT [--min-free-gb G] [--max-bytes N] [--idle-timeout S] [--timeout S] -- CMD...
                                         run CMD (the SSH command that streams from the VM,
                                         RECORD_TO=host, or copies its video, RECORD_TO=guest) and
                                         save its output to OUT as it arrives
  recording.py finalize MKV MP4 [--min-free-gb G]
                                         the streamed .mkv as an MP4 (no re-encoding), only if
                                         the Mac keeps its reserve; exit 3 (MKV kept) if it can't
  recording.py capture [--max-bytes N] [--timeout S] -- CMD...
                                         CMD's output (a VM's answer), at most N bytes within S
                                         seconds: past either, CMD is stopped and nothing is printed

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
space falls below --min-free-gb (IMPORT_MIN_FREE_GB, as for every copy from the VM,
beside the space other operations have claimed) or past --max-bytes, or, with
--idle-timeout and --timeout (a guest-mode copy), when the VM goes quiet or takes too
long. `finalize` turns the stream into an MP4 only when the copy fits above that
reserve too (the VM chose the stream's size), claims that space while ffmpeg writes,
and stops ffmpeg if the free space nears the reserve anyway or its output passes its
limit; whatever stops it, the .mkv is kept as it is. `capture` reads the VM's short
answers (a listing, the recorder's reply) with a byte limit and a deadline, so a VM
that sends without end, or never answers, can't grow or stall the command.
"""
import argparse
import contextlib
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


def free_bytes(path_or_fd):
    st = os.fstatvfs(path_or_fd) if isinstance(path_or_fd, int) else os.statvfs(path_or_fd)
    return st.f_bavail * st.f_frsize


FINALIZE_GUARD = 512 << 20        # finalize stops ffmpeg this far above the reserve (what it can write between looks)
FINALIZE_POLL = 0.2               # seconds between finalize's looks at the free space and the MP4's size
STOP_GRACE = 10                   # seconds save waits for a stopped stream's last bytes


def claims_by_others(path):
    """Bytes other live operations have claimed on the disk holding path (guest_import.claimed_space)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import guest_import
    return guest_import.claimed_space(path)


@contextlib.contextmanager
def space_claim(path, nbytes, reserve, what):
    """Claim nbytes of free space on the disk holding path (lib/storage.py's claims, which S3 syncs and every copy
    from the VM count against the reserve), if that much is free above the reserve and what others hold. Yields
    True when claimed, False when there isn't room; the claim goes when the block ends."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import storage
    import uuid
    pool = storage.space_pool(path)
    f = None
    try:
        with storage.GlobalLock():
            if free_bytes(path) - reserve - storage.claims_on(pool) >= nbytes:
                f = os.path.join(storage.space_dir(), f"{what}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json")
                storage.atomic_json(f, {"pool": pool, "bytes": int(nbytes), "pid": os.getpid(),
                                        "pstart": storage.proc_start(os.getpid()), "what": what, "since": storage.now()})
        yield f is not None
    finally:
        if f:
            with contextlib.suppress(OSError):
                os.unlink(f)


def cmd_save(a):
    cmd = a.command
    if not cmd:
        print("recording.py save: no command after --", file=sys.stderr)
        return 2
    min_free = int(max(0.0, a.min_free_gb) * GIB)
    max_bytes = a.max_bytes if a.max_bytes and a.max_bytes > 0 else None
    idle = a.idle_timeout if a.idle_timeout and a.idle_timeout > 0 else None
    deadline = time.monotonic() + a.timeout if a.timeout and a.timeout > 0 else None
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
        stopped.append(time.monotonic())
        try:
            proc.terminate()      # the stream ends; what arrived stays
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop("signal"))
    rc, since, total = 0, SAVE_CHECK_EVERY, 0
    import selectors
    sel = selectors.DefaultSelector()
    try:
        src = proc.stdout.fileno()
        sel.register(src, selectors.EVENT_READ)
        last = time.monotonic()
        while True:
            if since >= SAVE_CHECK_EVERY:
                since = 0
                if free_bytes(fd) - SAVE_CHECK_EVERY < min_free + claims_by_others(a.out):
                    print(f"metacomputer: the recording stopped saving here: the Mac would have less than "
                          f"{min_free / GIB:g} GiB free (IMPORT_MIN_FREE_GB, with what other copies have claimed); "
                          "what arrived before is kept", file=sys.stderr, flush=True)
                    stop("space")
                    rc = 3
                    break
            now_ = time.monotonic()
            if deadline is not None and now_ >= deadline or idle is not None and now_ - last >= idle:
                print("metacomputer: stopped saving: the VM " + ("took too long" if deadline is not None
                      and now_ >= deadline else f"sent nothing for {idle:g} s") + "; what arrived before is kept",
                      file=sys.stderr, flush=True)
                stop("time")
                rc = 5
                break
            if stopped and now_ - stopped[0] > STOP_GRACE:
                break           # stopped (a signal) and the stream still hasn't ended: what arrived is kept
            waits = [w for w in (deadline - now_ if deadline is not None else None,
                                 idle - (now_ - last) if idle is not None else None) if w is not None]
            if not sel.select(timeout=max(0.0, min(waits + [1.0]))):
                continue
            b = os.read(src, min(1 << 20, SAVE_CHECK_EVERY - since))
            if not b:
                break
            last = time.monotonic()
            if max_bytes is not None and total + len(b) > max_bytes:
                b = b[:max_bytes - total]
                stop("size")
                rc = 4
            view = memoryview(b)
            while view:
                view = view[os.write(fd, view):]
            since += len(b)
            total += len(b)
            if rc == 4:
                print(f"metacomputer: stopped saving at {max_bytes:,} bytes (more than the VM's disk holds); "
                      "what arrived before is kept", file=sys.stderr, flush=True)
                break
    finally:
        sel.close()
        os.close(fd)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return rc or (0 if stopped else (proc.returncode if proc.returncode and proc.returncode > 0 else 0))


def cmd_finalize(a):
    """The streamed .mkv as an MP4, the same video in a container players and editors open at once (no
    re-encoding). The MP4 is about the MKV's size plus a little; the stream's size is the VM's choice, so:
    - the space is claimed first (lib/storage.py's claims), so every copy from the VM, the task copier's included,
      and every S3 sync counts it against the reserve while ffmpeg writes;
    - while ffmpeg runs, the free space and the MP4's size are watched every FINALIZE_POLL seconds: ffmpeg is
      stopped when the free space comes within FINALIZE_GUARD of the reserve (writes nobody claimed: another
      program, a copy started before) or the MP4 passes its limit (ffmpeg's own -fs is only advice);
    - whatever stops ffmpeg (that, a failure, a signal), the partial MP4 is deleted and the MKV kept.
    0: made (the caller removes the MKV); 3: no room, before or during (the MKV kept); 1: ffmpeg failed, ran past its
    limit or was stopped (the MKV kept)."""
    min_free = int(max(0.0, a.min_free_gb) * GIB)
    try:
        st = os.stat(a.mkv, follow_symlinks=False)
    except OSError as e:
        print(f"metacomputer: no stream to convert ({e.strerror})", file=sys.stderr)
        return 1
    limit = st.st_size + max(16 << 20, st.st_size // 50)        # container overhead, generously
    folder = os.path.dirname(os.path.abspath(a.mp4))
    if os.path.lexists(a.mp4):
        print(f"metacomputer: {a.mp4} exists; not writing over it", file=sys.stderr)
        return 1
    no_room = (f"metacomputer: not converting to MP4: a copy of {st.st_size / GIB:.1f} GiB would leave this Mac "
               f"less than {min_free / GIB:g} GiB free (IMPORT_MIN_FREE_GB, with what other copies have claimed); "
               "the .mkv is kept and plays as it is")
    try:
        claim = space_claim(folder, limit + 2 * FINALIZE_GUARD, min_free, "recording-finalize")
        got = claim.__enter__()
    except Exception as e:      # the claims can't be read or written: don't convert, keep the MKV
        print(f"metacomputer: not converting to MP4 (free-space claims unavailable: {type(e).__name__}); "
              "the .mkv is kept and plays as it is", file=sys.stderr)
        return 1
    why = None
    proc = None
    stop = []
    old = {}
    try:
        if not got:
            print(no_room, file=sys.stderr)
            return 3
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old[sig] = signal.signal(sig, lambda *_: stop.append("signal"))
        proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", "-i", a.mkv,
                                 "-c", "copy", "-movflags", "+faststart", "-fs", str(limit), a.mp4],
                                stdin=subprocess.DEVNULL, start_new_session=True)
        while proc.poll() is None:
            try:
                low = free_bytes(folder) < min_free + FINALIZE_GUARD
            except OSError:
                low = None          # the folder can't be checked (a drive gone): stop rather than write blind
            if stop:
                why = "it was interrupted"
            elif low is None:
                why = "its folder's free space couldn't be checked"
            elif low:
                why = "space"
            else:
                try:
                    if os.lstat(a.mp4).st_size > limit:
                        why = "it grew past its size limit"
                except OSError:
                    pass
            if why:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                break
            time.sleep(FINALIZE_POLL)
        rc = proc.returncode
        size = os.path.getsize(a.mp4) if os.path.isfile(a.mp4) and not os.path.islink(a.mp4) else -1
        if why is None and size >= limit - (1 << 20):
            why = "it reached its size limit"     # -fs ended it early: not the whole video
        if why is None and (rc != 0 or size <= 0 or probe_duration(a.mp4) is None):
            why = "ffmpeg failed"
        if why is not None:
            with contextlib.suppress(OSError):
                os.unlink(a.mp4)
            if why == "space":
                print("metacomputer: stopped converting to MP4: this Mac's free space came within "
                      f"{FINALIZE_GUARD >> 20} MiB of the reserve (IMPORT_MIN_FREE_GB) while it ran; the partial MP4 is "
                      "deleted and the .mkv is kept and plays as it is", file=sys.stderr)
                return 3
            print(f"metacomputer: not converted to MP4 ({why}); the partial MP4 is deleted and the .mkv is kept and "
                  "plays as it is", file=sys.stderr)
            return 1
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            with contextlib.suppress(OSError):
                os.unlink(a.mp4)
        for sig, h in old.items():
            signal.signal(sig, h)
        claim.__exit__(None, None, None)


def cmd_capture(a):
    """CMD's stdout, at most --max-bytes within --timeout seconds, printed only when it fits both. Past either,
    CMD and anything it started are stopped and the exit status says which (2 too much, 124 too slow); otherwise
    it's CMD's own. Its stderr is dropped."""
    cmd = a.command
    if not cmd:
        print("recording.py capture: no command after --", file=sys.stderr)
        return 2
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return 1
    deadline = time.monotonic() + max(0.1, a.timeout)
    got, why = bytearray(), None
    fd = proc.stdout.fileno()
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            why = 124
            break
        if not sel.select(timeout=min(left, 1.0)):
            continue
        b = os.read(fd, 65536)
        if not b:
            break
        got += b
        if len(got) > a.max_bytes:
            why = 2
            break
    sel.close()
    if why is not None:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return why
    try:
        rc = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return 124
    sys.stdout.buffer.write(bytes(got))
    sys.stdout.flush()
    return rc if rc >= 0 else 1


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
    sv.add_argument("--max-bytes", type=int, default=0)
    sv.add_argument("--idle-timeout", type=float, default=0)
    sv.add_argument("--timeout", type=float, default=0)
    fz = sub.add_parser("finalize")
    fz.add_argument("mkv")
    fz.add_argument("mp4")
    fz.add_argument("--min-free-gb", type=float, default=10.0)
    cp = sub.add_parser("capture")
    cp.add_argument("--max-bytes", type=int, default=1 << 20)
    cp.add_argument("--timeout", type=float, default=30.0)
    m = sub.add_parser("moments")
    m.add_argument("json")
    pr = sub.add_parser("probe")
    pr.add_argument("file")
    argv, command = sys.argv[1:], []
    if argv[:1] in (["save"], ["capture"]) and "--" in argv:   # their command: everything after the first --
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    a = p.parse_args(argv)
    a.command = command
    return {"sidecar": cmd_sidecar, "moments": cmd_moments, "probe": cmd_probe, "save": cmd_save,
            "finalize": cmd_finalize, "capture": cmd_capture}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
