#!/usr/bin/env python3
"""Desktop recordings and per-task recordings. Needs ffmpeg/ffprobe on the Mac.

    python3 tests/test_recording.py         no VM: the description file (lib/recording.py); where
                                            recordings go by default (RECORD_TO=host) and the warning
                                            for RECORD_TO=guest; the stream writer's limits and deadlines;
                                            the MP4 conversion under disk pressure (a stand-in ffmpeg) and
                                            the free-space claims it shares with every copy; guest-mode
                                            deadlines; record stop when the stream ended early or the VM
                                            can't be reached; the task copier (TASK_SYNC_INTERVAL) against
                                            a stand-in VM
    MCOMP_CONFIG=test-vm.env python3 tests/test_recording.py --vm
                                            also `metacomputer record` in both RECORD_TO modes and
                                            both qualities on a running test VM (about a minute)
    MCOMP_CONFIG=test-vm.env python3 tests/test_recording.py --vm --wipe
                                            also checks the VM's key lists are root-owned and outside the
                                            home, then wipes the test VM's home folder in the middle of a
                                            default recording and of a task's driver recording, checks what
                                            the Mac kept and that it still reaches the VM (sh, recordings
                                            --sync, task finish), then resets the test VM (about 3 minutes)
"""
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC = os.path.join(LAB, "lib", "recording.py")
CLI = os.path.join(LAB, "bin", "metacomputer")
FAILS = []
sys.path.insert(0, os.path.join(LAB, "lib"))


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def ts(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def write_log(path, vm, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps({"seq": 0, "ts": records[0][0] if records else ts(time.time()), "type": "start",
                            "session": {"vm": vm}}) + "\n")
        for i, (t, typ, tool, args) in enumerate(records, 1):
            f.write(json.dumps({"seq": i, "ts": t, "type": typ, "id": i, "tool": tool, "args": args}) + "\n")


def vm_tests():
    cli = os.path.join(LAB, "bin", "metacomputer")
    out_dir = tempfile.mkdtemp(prefix="mcomp-rec-vm-")
    gs = subprocess.run([cli, "ssh", "ops", "/opt/metacomputer/bin/guest-status"], capture_output=True, text=True)
    try:
        has_sound = bool(json.loads(gs.stdout or "{}").get("virtual_audio"))
    except ValueError:
        has_sound = False
    print(f"(the VM {'has a' if has_sound else 'has no'} virtual sound card)")
    try:
        for mode, quality, fps in (("guest", "standard", 20), ("host", "high", 30)):
            env = dict(os.environ, RECORD_TO=mode, RECORD_QUALITY=quality, RECORD_DIR=out_dir)
            # The config file sets RECORD_*; a wrapper file layers these on top of it.
            cfg = os.path.join(out_dir, f"{mode}.env")
            base = os.environ.get("MCOMP_CONFIG") or os.path.join(LAB, "config", "local.env")
            with open(cfg, "w") as f:
                f.write(f'. "{base}"\nRECORD_TO={mode}\nRECORD_QUALITY={quality}\nRECORD_DIR="{out_dir}"\n')
            env["MCOMP_CONFIG"] = cfg
            name = f"t-{mode}-{int(time.time())}"
            r = subprocess.run([cli, "record", "start", name, str(fps)], env=env, capture_output=True, text=True)
            check(f"{mode}: start", r.returncode == 0, r.stderr.strip())
            st = subprocess.run([cli, "record", "status"], env=env, capture_output=True, text=True).stdout
            check(f"{mode}: status says recording", f"recording {name}" in st, st)
            again = subprocess.run([cli, "record", "start", name + "b"], env=env, capture_output=True, text=True)
            check(f"{mode}: a second start is refused", again.returncode != 0)
            time.sleep(6)
            r = subprocess.run([cli, "record", "stop"], env=env, capture_output=True, text=True)
            check(f"{mode}: stop", r.returncode == 0, r.stderr.strip())
            mp4 = os.path.join(out_dir, name + ".mp4")
            side = json.load(open(mp4[:-4] + ".json")) if os.path.exists(mp4[:-4] + ".json") else {}
            check(f"{mode}: MP4 of about 6 s", side.get("duration_s") and 4 <= side["duration_s"] <= 9,
                  side.get("duration_s"))
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=r_frame_rate,width,height", "-of", "csv=p=0", mp4],
                                   capture_output=True, text=True).stdout.strip()
            check(f"{mode}: {fps} fps at the VM's resolution", probe.startswith("1920,1080,") and
                  probe.endswith(f"{fps}/1"), probe)
            check(f"{mode}: description file", (side.get("mode"), side.get("quality"), side.get("fps")) ==
                  (mode, quality, fps), side)
            check(f"{mode}: sound track {'present' if has_sound else 'absent'}, as the VM's sound card says",
                  bool(side.get("audio")) == has_sound, side.get("audio"))
            check(f"{mode}: nothing else left", sorted(os.listdir(out_dir)) ==
                  sorted(set(os.listdir(out_dir)) - {name + ".mkv", name + ".stream.log"}))
            left = subprocess.run([cli, "ssh", "ops", f"ls ~/metacomputer-runs/manual/{name}.mp4 2>/dev/null"],
                                  env=env, capture_output=True, text=True).stdout.strip()
            check(f"{mode}: VM keeps {'its copy' if mode == 'guest' else 'no video'}",
                  bool(left) == (mode == "guest"), left)
            if mode == "guest" and left:
                subprocess.run([cli, "ssh", "ops", f"rm -f ~/metacomputer-runs/manual/{name}.mp4 "
                                f"~/metacomputer-runs/manual/{name}.ffmpeg.log"], env=env, capture_output=True)
        if has_sound:   # RECORD_AUDIO=off leaves the sound out even when the VM has it
            cfg = os.path.join(out_dir, "quiet.env")
            with open(cfg, "w") as f:
                f.write(f'. "{os.environ.get("MCOMP_CONFIG") or os.path.join(LAB, "config", "local.env")}"\n'
                        f'RECORD_TO=host\nRECORD_AUDIO=off\nRECORD_DIR="{out_dir}"\n')
            env = dict(os.environ, MCOMP_CONFIG=cfg)
            name = f"t-quiet-{int(time.time())}"
            subprocess.run([cli, "record", "start", name], env=env, capture_output=True, text=True)
            time.sleep(3)
            subprocess.run([cli, "record", "stop"], env=env, capture_output=True, text=True)
            side = json.load(open(os.path.join(out_dir, name + ".json"))) if os.path.exists(
                os.path.join(out_dir, name + ".json")) else {"audio": "missing"}
            check("RECORD_AUDIO=off: no sound track", side.get("audio") is None, side.get("audio"))
        bad = dict(os.environ)
        cfg = os.path.join(out_dir, "bad-audio.env")
        with open(cfg, "w") as f:
            f.write(f'. "{os.environ.get("MCOMP_CONFIG") or os.path.join(LAB, "config", "local.env")}"\n'
                    f'RECORD_AUDIO="on; true"\nRECORD_DIR="{out_dir}"\n')
        bad["MCOMP_CONFIG"] = cfg
        r = subprocess.run([cli, "record", "start", "t-bad-audio"], env=bad, capture_output=True, text=True)
        check("a RECORD_AUDIO other than auto/off is refused", r.returncode != 0 and "RECORD_AUDIO" in r.stderr,
              r.stderr.strip())
        missing = os.path.join(out_dir, "no-such-parent", "child")
        env = dict(os.environ)
        cfg = os.path.join(out_dir, "missing.env")
        with open(cfg, "w") as f:
            f.write(f'. "{os.environ.get("MCOMP_CONFIG") or os.path.join(LAB, "config", "local.env")}"\n'
                    f'RECORD_DIR="{missing}"\n')
        env["MCOMP_CONFIG"] = cfg
        r = subprocess.run([cli, "record", "start", "t-missing"], env=env, capture_output=True, text=True)
        check("a RECORD_DIR on a missing drive is refused, not created", r.returncode != 0
              and not os.path.exists(os.path.dirname(missing)), r.stderr.strip())
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def function(src, name):
    """One shell function, as written in bin/metacomputer."""
    m = re.search(r"^%s\(\) \{.*?^\}\n" % re.escape(name), src, re.S | re.M)
    assert m, name
    return m.group(0)


# Stand-ins for the VM and the CLI's helpers; the real functions under test come from bin/metacomputer.
STUBS = r'''
set -uo pipefail
warn() { echo "WARN $*" >&2; }
ok() { echo "OK $*"; }
say() { echo "SAY $*"; }
die() { echo "DIE $*" >&2; exit 9; }
need_running() { :; }
export_options() { :; }
emit_event() { echo "EVENT $*" >> "$TD/calls"; }
term_safe() { cat; }
esc_text() { printf '%s' "$1"; }
gssh() { # what the stand-in VM answers; FAKE_VM=down: it can't be reached
  echo "GSSH $*" >> "$TD/calls"
  [ "${FAKE_VM:-up}" = down ] && return 255
  [ "${FAKE_VM:-up}" = hang ] && { sleep 30; return 0; }
  case "$*" in
    *"desktop-record status"*) echo "recording stream:${REC_NAME:-x}" ;;
    *"desktop-record start"*) echo "recording /home/u/metacomputer-runs/manual/${REC_NAME:-x}.mp4 (1920x1080 @ 20 fps)" ;;
    *"desktop-record stop"*) echo "stream:${REC_NAME:-x}" ;;
    *) return 0 ;;
  esac
}
daemonize() { echo "DAEMONIZE $*" >> "$TD/calls"; sleep 20 & echo $! >> "$TD/stub-pids"; }
vm_answer() { shift 2; gssh ops "$@"; }   # the real one (bounded) is checked on its own below
vm_copy_video() { echo "COPY $*" >> "$TD/calls"; [ "${FAKE_VM:-up}" = down ] && return 255; printf 'video' > "$2"; }
'''


def cli_script(td, funcs, body, defaults=True, cfg=""):
    """A bash script: the shipped defaults (or none), a config, stand-ins, the real functions, then body."""
    src = open(CLI).read()
    lab = os.path.join(td, "lab")
    os.makedirs(os.path.join(lab, "recordings"), exist_ok=True)
    if not os.path.exists(os.path.join(lab, "lib")):
        os.symlink(os.path.join(LAB, "lib"), os.path.join(lab, "lib"))
    return "\n".join([
        f'TD="{td}"',
        f'. "{LAB}/config/defaults.env"' if defaults else "unset RECORD_TO",
        cfg,
        f'LAB_ROOT="{lab}"; RUN_DIR="{td}/run"; LOG_DIR="{td}/logs"; VM_NAME=t; PY="{sys.executable}"; SELF=/bin/true',
        'GUEST_RUN_DIR=/home/u/metacomputer-runs; mkdir -p "$RUN_DIR" "$LOG_DIR"',
        STUBS, *(function(src, f) for f in funcs), body])


def run_bash(script, env=None, timeout=60):
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=dict(os.environ, **(env or {})))


def where_checks():
    """What status and security say about recordings (lib/recording.py), and the shipped default."""
    import recording
    defaults = open(os.path.join(LAB, "config", "defaults.env")).read()
    check("the shipped default streams desktop recordings to the Mac (RECORD_TO=host)",
          re.search(r"^RECORD_TO=host\b", defaults, re.M) is not None)
    check("...and the task copier is on (TASK_SYNC_INTERVAL=15)", re.search(r"^TASK_SYNC_INTERVAL=15\b", defaults, re.M)
          is not None)
    d = recording.settings_summary({})
    check("with nothing set: host, copier every 15 s", d == {"desktop_to": "host", "desktop_recording": None,
                                                            "task_sync_interval_s": 15}, d)
    g = recording.settings_summary({"MCOMP_RECORD_TO": "guest", "MCOMP_TASK_SYNC_INTERVAL": "0",
                                    "MCOMP_RECORDING_NOW": "demo-1"})
    check("guest mode, copier off, a recording running", g == {"desktop_to": "guest", "desktop_recording": "demo-1",
                                                               "task_sync_interval_s": 0}, g)
    bad = recording.settings_summary({"MCOMP_RECORD_TO": "cloud", "MCOMP_TASK_SYNC_INTERVAL": "soon",
                                      "MCOMP_RECORDING_NOW": "x\x1b[2J"})
    check("anything else is reported as invalid, never echoed", bad == {"desktop_to": "invalid", "desktop_recording": None,
                                                                         "task_sync_interval_s": None}, bad)
    check("status: host mode in plain words", "stream to this Mac as they record (RECORD_TO=host)"
          in recording.status_line(d))
    line = recording.status_line(g)
    check("status: guest mode warns that the VM can delete it, and names the running recording",
          "RECORD_TO=guest" in line and "can delete" in line and "recording now: demo-1" in line, line)
    sec = "\n".join(recording.security_lines(g))
    check("security: guest mode and a copier that's off are both flagged", "RECORD_TO=guest" in sec
          and "delete it first" in sec and "TASK_SYNC_INTERVAL=0" in sec, sec)
    sec = "\n".join(recording.security_lines(d))
    check("security: the defaults explained", "RECORD_TO=host" in sec and "every 15 s" in sec, sec)
    import events
    td = tempfile.mkdtemp(prefix="mcomp-rec-ev-")
    try:
        mp4 = os.path.join(td, "cut.mp4")
        open(mp4, "wb").close()
        json.dump({"name": "cut", "mode": "host", "duration_s": 12.0, "stream_ended_early": "..."},
                  open(os.path.join(td, "cut.json"), "w"))
        ev = events.recording_event(mp4)
        check("a recording whose stream ended early is a warning event", ev["severity"] == "warn"
              and ev["data"]["ended_early"] is True and "stopped sending" in ev["message"], ev)
        json.dump({"name": "cut", "mode": "host", "duration_s": 12.0}, open(os.path.join(td, "cut.json"), "w"))
        ev = events.recording_event(mp4)
        check("...a whole one stays info", ev["severity"] == "info" and ev["data"]["ended_early"] is False, ev)
        # The video is the VM's: its size and its own length could spell a value it was given. The event carries the
        # length this Mac's clock measured and the size only as a class.
        secret = int.from_bytes(b"hunter2pass", "big")             # a "size" that spells a password
        json.dump({"name": "cut", "mode": "host", "duration_s": 31337.25, "bytes": secret,
                   "started_at": "2026-11-18T20:00:00-0500", "ended_at": "2026-11-18T20:01:05-0500"},
                  open(os.path.join(td, "cut.json"), "w"))
        ev = events.recording_event(mp4)
        flat = json.dumps(ev)
        check("recording_stopped: the duration is this Mac's (record start to stop), not the video's own",
              ev["data"]["duration_s"] == 65 and "(65 s)" in ev["message"] and "31337" not in flat, ev)
        check("...the size is one of eight bounds, never the VM's number", ev["data"]["size_bound"] == 1 << 40
              and "bytes" not in ev["data"] and str(secret) not in flat and str(secret)[:8] not in flat, ev["data"])
        check("...size bounds as the vault's: the smallest of 0, 1 KiB, 64 KiB, 1 MiB, 16 MiB, 256 MiB, 4 GiB, 1 TiB",
              [events.size_bound(n) for n in (0, 1, 1024, 1025, 70000, 5 << 20, 300 << 20, 5 << 30, 1 << 50)]
              == [0, 1024, 1024, 65536, 1 << 20, 1 << 24, 1 << 32, 1 << 40, 1 << 40] and events.size_bound("12") is None)
        check("count classes as the vault's", [events.count_class(n) for n in (0, 1, 9, 10, 99, 100, 10 ** 9)]
              == ["none", "1-9", "1-9", "10-99", "10-99", "100 or more", "100 or more"])
    finally:
        shutil.rmtree(td, ignore_errors=True)


def save_checks():
    """The Mac's half of a streamed recording (recording.py save): limits the VM can't move."""
    td = tempfile.mkdtemp(prefix="mcomp-rec-save-")
    try:
        out = os.path.join(td, "a.mkv")
        r = subprocess.run([sys.executable, REC, "save", out, "--min-free-gb", "0", "--", "sh", "-c",
                            "printf 'frames'; printf 'stderr' >&2"], capture_output=True, text=True, timeout=30)
        check("save: what the command writes is saved as it is", r.returncode == 0 and open(out, "rb").read() == b"frames"
              and "stderr" in r.stderr, (r.returncode, r.stderr))
        r = subprocess.run([sys.executable, REC, "save", out, "--", "true"], capture_output=True, text=True, timeout=30)
        check("save: never over an existing file", r.returncode != 0 and open(out, "rb").read() == b"frames", r.stderr)
        link = os.path.join(td, "link.mkv")
        os.symlink(os.path.join(td, "target"), link)
        r = subprocess.run([sys.executable, REC, "save", link, "--", "true"], capture_output=True, text=True, timeout=30)
        check("save: never through a link", r.returncode != 0 and not os.path.exists(os.path.join(td, "target")), r.stderr)
        t0 = time.time()
        r = subprocess.run([sys.executable, REC, "save", os.path.join(td, "b.mkv"), "--min-free-gb", "1e9", "--", "yes"],
                           capture_output=True, text=True, timeout=30)
        check("save: stops (and stops the VM's stream) before the Mac's free space falls below the reserve",
              r.returncode == 3 and "IMPORT_MIN_FREE_GB" in r.stderr and time.time() - t0 < 10
              and os.path.getsize(os.path.join(td, "b.mkv")) == 0, (r.returncode, r.stderr))
        # Stopped (record stop's fallback, a reset): the stream's command ends too, and what arrived stays.
        marker = os.path.join(td, "child-alive")
        p = subprocess.Popen([sys.executable, REC, "save", os.path.join(td, "c.mkv"), "--min-free-gb", "0", "--", "sh",
                              "-c", f"trap 'exit 0' TERM; while :; do printf x; touch {marker}; sleep 0.1; done"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        p.send_signal(signal.SIGTERM)
        rc = p.wait(timeout=15)
        size = os.path.getsize(os.path.join(td, "c.mkv"))
        os.remove(marker)
        time.sleep(0.5)
        check("save: a TERM ends it and the command it runs, keeping what arrived", rc == 0 and size > 0
              and not os.path.exists(marker), (rc, size))
        r = subprocess.run([sys.executable, REC, "save", os.path.join(td, "d.mp4"), "--min-free-gb", "0", "--max-bytes",
                            "100000", "--", "yes"], capture_output=True, text=True, timeout=30)
        check("save --max-bytes (a guest-mode copy: no more than the VM's disk holds): stops there, keeps what arrived",
              r.returncode == 4 and os.path.getsize(os.path.join(td, "d.mp4")) == 100000, (r.returncode, r.stderr))
        # Deadlines (a guest-mode copy): a VM that goes quiet, or drips bytes, can't hold the copy open.
        t0 = time.time()
        r = subprocess.run([sys.executable, REC, "save", os.path.join(td, "q.mp4"), "--min-free-gb", "0", "--idle-timeout",
                            "1", "--", "sh", "-c", "printf abc; exec sleep 30"], capture_output=True, text=True, timeout=60)
        check("save --idle-timeout: a VM that sends nothing more is cut off, keeping what arrived (exit 5)",
              r.returncode == 5 and time.time() - t0 < 8 and open(os.path.join(td, "q.mp4"), "rb").read() == b"abc"
              and "sent nothing for 1 s" in r.stderr, (r.returncode, round(time.time() - t0, 1), r.stderr))
        t0 = time.time()
        r = subprocess.run([sys.executable, REC, "save", os.path.join(td, "r.mp4"), "--min-free-gb", "0", "--idle-timeout",
                            "5", "--timeout", "1.5", "--", "sh", "-c", "while :; do printf x; sleep 0.2; done"],
                           capture_output=True, text=True, timeout=60)
        check("save --timeout: a VM that drips bytes is cut off at the deadline, keeping what arrived (exit 5)",
              r.returncode == 5 and time.time() - t0 < 8 and os.path.getsize(os.path.join(td, "r.mp4")) > 0
              and "took too long" in r.stderr, (r.returncode, round(time.time() - t0, 1), r.stderr))

        # finalize: the MP4 only when the copy leaves the reserve; otherwise the MKV stays and nothing is written.
        mkv = os.path.join(td, "s.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "2",
                        "-pix_fmt", "yuv420p", "-f", "matroska", mkv], check=True, stdin=subprocess.DEVNULL)
        r = subprocess.run([sys.executable, REC, "finalize", mkv, os.path.join(td, "s.mp4"), "--min-free-gb", "1e9"],
                           capture_output=True, text=True, timeout=60)
        check("finalize: no room above the reserve: nothing converted, the .mkv kept, and it says so (exit 3)",
              r.returncode == 3 and os.path.exists(mkv) and not os.path.exists(os.path.join(td, "s.mp4"))
              and "IMPORT_MIN_FREE_GB" in r.stderr, (r.returncode, r.stderr))
        r = subprocess.run([sys.executable, REC, "finalize", mkv, os.path.join(td, "s.mp4"), "--min-free-gb", "0"],
                           capture_output=True, text=True, timeout=60)
        check("finalize: with room, the MP4 is made and plays", r.returncode == 0
              and subprocess.run([sys.executable, REC, "probe", os.path.join(td, "s.mp4")]).returncode == 0, r.stderr)
        r = subprocess.run([sys.executable, REC, "finalize", mkv, os.path.join(td, "s.mp4"), "--min-free-gb", "0"],
                           capture_output=True, text=True, timeout=60)
        check("finalize: never over an existing file", r.returncode == 1, r.stderr)

        # capture: the VM's short answers, with a byte limit and a deadline.
        r = subprocess.run([sys.executable, REC, "capture", "--max-bytes", "64", "--timeout", "10", "--", "printf", "hello"],
                           capture_output=True, timeout=30)
        check("capture: an answer within its limits is passed on", r.returncode == 0 and r.stdout == b"hello", r)
        t0 = time.time()
        r = subprocess.run([sys.executable, REC, "capture", "--max-bytes", "4096", "--timeout", "10", "--", "sh", "-c",
                            "yes | tr -d '\\n'"], capture_output=True, timeout=30)
        check("capture: endless bytes without a newline: stopped at the limit, nothing passed on (exit 2)",
              r.returncode == 2 and r.stdout == b"" and time.time() - t0 < 8, (r.returncode, len(r.stdout)))
        t0 = time.time()
        r = subprocess.run([sys.executable, REC, "capture", "--max-bytes", "4096", "--timeout", "1", "--", "sleep", "30"],
                           capture_output=True, timeout=30)
        check("capture: no answer by the deadline: stopped (exit 124)", r.returncode == 124 and time.time() - t0 < 5,
              (r.returncode, round(time.time() - t0, 1)))
    finally:
        shutil.rmtree(td, ignore_errors=True)


FAKE_FFMPEG = r"""#!/usr/bin/env python3
# A stand-in ffmpeg for finalize: ignores -fs, writes FAKE_FF_MIB MiB to its output a MiB every FAKE_FF_DELAY s,
# notes its pid, whether finalize's space claim was there while it ran, and whether it got to the end.
import glob, os, sys, time
d, out = os.environ["FAKE_FF_DIR"], sys.argv[-1]
open(os.path.join(d, "pid"), "w").write(str(os.getpid()))
if glob.glob(os.path.join(os.environ.get("MCOMP_STORAGE_HOME", "/nonexistent"), "space", "recording-finalize-*.json")):
    open(os.path.join(d, "claimed"), "w").close()
with open(out, "xb") as f:
    for _ in range(int(os.environ.get("FAKE_FF_MIB", "50"))):
        f.write(b"\0" * (1 << 20))
        f.flush()
        time.sleep(float(os.environ.get("FAKE_FF_DELAY", "0.05")))
open(os.path.join(d, "done"), "w").close()
"""


def finalize_guard_checks():
    """finalize keeps the reserve while ffmpeg writes (a watchdog), shares one reservation with every copy from the
    VM (lib/storage.py's claims), and keeps the MKV whenever ffmpeg doesn't finish."""
    import argparse
    import contextlib
    import io
    import recording
    import storage
    td = tempfile.mkdtemp(prefix="mcomp-rec-fin-")
    old_env = {k: os.environ.get(k) for k in ("PATH", "MCOMP_STORAGE_HOME", "FAKE_FF_DIR", "FAKE_FF_MIB", "FAKE_FF_DELAY")}
    saved = (recording.free_bytes, getattr(recording, "FINALIZE_GUARD", None), getattr(recording, "FINALIZE_POLL", None))
    try:
        bindir, ffdir, state = (os.path.join(td, n) for n in ("bin", "ff", "state"))
        for d in (bindir, ffdir, state):
            os.makedirs(d)
        with open(os.path.join(bindir, "ffmpeg"), "w") as f:
            f.write(FAKE_FFMPEG)
        os.chmod(os.path.join(bindir, "ffmpeg"), 0o755)
        os.environ.update(PATH=bindir + os.pathsep + old_env["PATH"], MCOMP_STORAGE_HOME=state, FAKE_FF_DIR=ffdir)
        MIB = 1 << 20

        def reset_ff():
            for n in os.listdir(ffdir):
                os.remove(os.path.join(ffdir, n))

        def claims_left():
            d = os.path.join(state, "space")
            return os.listdir(d) if os.path.isdir(d) else []

        def sparse_mkv(name, size):
            p = os.path.join(td, name)
            with open(p, "wb") as f:
                f.truncate(size)
            return p

        # The free space falls while ffmpeg writes (another program, a copy): ffmpeg is stopped before the reserve,
        # not left to run to the end. Reserve 64 MiB, a 40 MiB stream (limit 56 MiB); the stand-in writes 50 MiB
        # in 2.5 s while something else takes 60 MiB/s.
        mkv, mp4 = sparse_mkv("w.mkv", 40 * MIB), os.path.join(td, "w.mp4")
        recording.FINALIZE_GUARD, recording.FINALIZE_POLL = 2 * MIB, 0.02
        t0, seen = [], []

        def falling(_path):
            if not t0:
                t0.append(time.monotonic())
            used = os.path.getsize(mp4) if os.path.exists(mp4) else 0
            free = 132 * MIB - used - int((time.monotonic() - t0[0]) * 60 * MIB)
            seen.append(free)
            return free

        recording.free_bytes = falling
        os.environ.update(FAKE_FF_MIB="50", FAKE_FF_DELAY="0.05")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = recording.cmd_finalize(argparse.Namespace(mkv=mkv, mp4=mp4, min_free_gb=64 / 1024))
        recording.free_bytes = saved[0]
        pid = int(open(os.path.join(ffdir, "pid")).read()) if os.path.exists(os.path.join(ffdir, "pid")) else 0
        check("finalize: free space falling during the conversion stops ffmpeg before the reserve (exit 3)",
              rc == 3 and not os.path.exists(os.path.join(ffdir, "done")) and pid, (rc, err.getvalue()[-300:]))
        check("...the partial MP4 is deleted and the .mkv kept, and it says so and why",
              not os.path.exists(mp4) and os.path.exists(mkv) and ".mkv is kept" in err.getvalue()
              and "reserve" in err.getvalue() and "partial MP4 is deleted" in err.getvalue(), err.getvalue()[-300:])
        check("...ffmpeg itself is gone", pid and not _alive(pid), pid)
        check("...and its space claim is gone", claims_left() == [],
              claims_left())

        # Interrupted (TERM, Ctrl-C): ffmpeg stopped, the partial MP4 deleted, the .mkv kept.
        reset_ff()
        mkv, mp4 = sparse_mkv("i.mkv", 40 * MIB), os.path.join(td, "i.mp4")
        p = subprocess.Popen([sys.executable, REC, "finalize", mkv, mp4, "--min-free-gb", "0"], stderr=subprocess.PIPE,
                             text=True, env=dict(os.environ, FAKE_FF_MIB="200", FAKE_FF_DELAY="0.05"))
        for _ in range(100):
            if os.path.exists(mp4) and os.path.exists(os.path.join(ffdir, "pid")):
                break
            time.sleep(0.05)
        claimed = os.path.exists(os.path.join(ffdir, "claimed"))
        p.send_signal(signal.SIGTERM)
        _, perr = p.communicate(timeout=30)
        pid = int(open(os.path.join(ffdir, "pid")).read())
        check("finalize: while ffmpeg runs, its space is claimed where every copy from the VM and S3 syncs count it",
              claimed, os.listdir(ffdir))
        check("finalize interrupted: ffmpeg stopped, the partial MP4 deleted, the .mkv kept, and it says so",
              p.returncode == 1 and not _alive(pid) and not os.path.exists(mp4) and os.path.exists(mkv)
              and "interrupted" in perr and ".mkv is kept" in perr and claims_left() == [],
              (p.returncode, perr[-300:]))

        # ffmpeg's -fs is only advice: an MP4 past its limit is stopped and deleted; one that ends at it (cut short
        # by -fs) isn't the whole video either. The .mkv stays both times.
        reset_ff()
        mkv, mp4 = sparse_mkv("g.mkv", 40 * MIB), os.path.join(td, "g.mp4")
        r = subprocess.run([sys.executable, REC, "finalize", mkv, mp4, "--min-free-gb", "0"], capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, FAKE_FF_MIB="120", FAKE_FF_DELAY="0.01"))
        check("finalize: an MP4 growing past its size limit is stopped and deleted, the .mkv kept",
              r.returncode == 1 and not os.path.exists(mp4) and os.path.exists(mkv)
              and not os.path.exists(os.path.join(ffdir, "done")) and "size limit" in r.stderr, (r.returncode, r.stderr))
        reset_ff()
        r = subprocess.run([sys.executable, REC, "finalize", mkv, mp4, "--min-free-gb", "0"], capture_output=True,
                           text=True, timeout=60, env=dict(os.environ, FAKE_FF_MIB="56", FAKE_FF_DELAY="0"))
        check("finalize: an MP4 that ends at its limit (cut short) isn't taken for the video; the .mkv kept",
              r.returncode == 1 and not os.path.exists(mp4) and os.path.exists(mkv), (r.returncode, r.stderr))

        # One reservation: another operation's live claim counts against the reserve for the conversion, the task
        # copier (every copy through lib/guest_import.py) and the stream alike.
        reset_ff()
        claim = os.path.join(storage.space_dir(), "test-claim.json")
        storage.atomic_json(claim, {"pool": storage.space_pool(td), "bytes": 1 << 60, "pid": os.getpid(),
                                    "pstart": storage.proc_start(os.getpid()), "what": "test", "since": storage.now()})
        r = subprocess.run([sys.executable, REC, "finalize", mkv, mp4, "--min-free-gb", "0"], capture_output=True,
                           text=True, timeout=60)
        check("finalize: space another operation claimed counts: no room, nothing converted, the .mkv kept (exit 3)",
              r.returncode == 3 and not os.path.exists(os.path.join(ffdir, "pid")) and os.path.exists(mkv)
              and ".mkv is kept" in r.stderr, (r.returncode, r.stderr))
        src = os.path.join(td, "vmfiles")
        os.makedirs(os.path.join(src, "turn-00001"))
        with open(os.path.join(src, "turn-00001", "action.json"), "w") as f:
            f.write("{}")
        dest = os.path.join(td, "copied")
        tar = subprocess.run(["tar", "-C", src, "-cf", "-", "."], capture_output=True, check=True,
                             env=dict(os.environ, COPYFILE_DISABLE="1")).stdout
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "guest_import.py"), dest], input=tar,
                           capture_output=True, timeout=60, env=dict(os.environ, MCOMP_IMPORT_MIN_FREE_GB="0"))
        check("the task copier (guest_import) counts it too: the copy stops before the claimed space",
              r.returncode == 1 and not os.path.exists(os.path.join(dest, "turn-00001", "action.json"))
              and b"claimed" in r.stderr, (r.returncode, r.stderr[-300:]))
        r = subprocess.run([sys.executable, REC, "save", os.path.join(td, "claimed.mkv"), "--min-free-gb", "0", "--",
                            "printf", "frames"], capture_output=True, text=True, timeout=60)
        check("...and so does a stream", r.returncode == 3 and "claimed" in r.stderr, (r.returncode, r.stderr))
        os.remove(claim)
        r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "guest_import.py"), dest], input=tar,
                           capture_output=True, timeout=60, env=dict(os.environ, MCOMP_IMPORT_MIN_FREE_GB="0"))
        check("...once the claim is gone, the copy goes through", r.returncode == 0
              and os.path.exists(os.path.join(dest, "turn-00001", "action.json")), (r.returncode, r.stderr[-300:]))
        # Finalize's own claim reaches the copier: while a conversion runs, a copy sees the space it holds.
        reset_ff()
        mkv, mp4 = sparse_mkv("c.mkv", 40 * MIB), os.path.join(td, "c.mp4")
        p = subprocess.Popen([sys.executable, REC, "finalize", mkv, mp4, "--min-free-gb", "0"], stderr=subprocess.PIPE,
                             text=True, env=dict(os.environ, FAKE_FF_MIB="40", FAKE_FF_DELAY="0.05"))
        for _ in range(100):
            if os.path.exists(os.path.join(ffdir, "pid")):
                break
            time.sleep(0.05)
        import guest_import
        held = getattr(guest_import, "claimed_space", lambda _p: 0)(td)
        p.communicate(timeout=60)
        check("...and a conversion's own claim is what a copy sees while it runs (the MP4's limit and a margin)",
              held > 0 and held >= (56 << 20) + 2 * (saved[1] or 0), held)
    finally:
        recording.free_bytes = saved[0]
        if saved[1] is not None:
            recording.FINALIZE_GUARD, recording.FINALIZE_POLL = saved[1], saved[2]
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(td, ignore_errors=True)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (finished, not yet reaped by a parent that's gone) isn't running.
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def record_cli_checks():
    """record start / stop from bin/metacomputer, with a stand-in VM."""
    funcs = ("record_dir", "record_sidecar", "proc_tree", "run_until", "do_record")
    td = tempfile.mkdtemp(prefix="mcomp-rec-cli-")
    env = {"MCOMP_EVENTS_DIR": os.path.join(td, "events"), "REC_NAME": "t1"}
    calls = os.path.join(td, "calls")
    state = os.path.join(td, "run", "manual-recording")
    try:
        def start(defaults=True, cfg=""):
            for f in (calls, state):
                if os.path.exists(f):
                    os.remove(f)
            r = run_bash(cli_script(td, funcs, 'do_record start t1', defaults, cfg), env)
            log = open(calls).read() if os.path.exists(calls) else ""
            st = open(state).read() if os.path.exists(state) else ""
            return r, log, st

        r, log, st = start()
        check("record start with the shipped defaults streams to the Mac (host mode)", r.returncode == 0
              and "DAEMONIZE" in log and "_record-pipe t1" in log and "mode=host" in st, (r.stderr, log, st))
        check("...and gives no warning", "WARN" not in r.stderr, r.stderr)
        r, log, st = start(defaults=False)
        check("with RECORD_TO unset altogether, host too", r.returncode == 0 and "mode=host" in st, (r.stderr, st))
        r, log, st = start(cfg="RECORD_TO=guest")
        check("RECORD_TO=guest still records in the VM", r.returncode == 0 and "desktop-record start" in log
              and "DAEMONIZE" not in log and "mode=guest" in st, (r.stderr, log, st))
        check("...and warns that anything in the VM can delete it, pointing at RECORD_TO=host",
              "WARN RECORD_TO=guest" in r.stderr and "can delete it" in r.stderr and "RECORD_TO=host" in r.stderr,
              r.stderr)
        # Guest mode: a VM that never answers record start or record status is cut off at the deadline.
        for args, what in (("start t1", "record start"), ("status", "record status")):
            for f in (calls, state):
                if os.path.exists(f):
                    os.remove(f)
            t0 = time.time()
            r = run_bash(cli_script(td, funcs, f"do_record {args}", cfg="RECORD_TO=guest"),
                         dict(env, FAKE_VM="hang", MCOMP_RECORD_WAIT="2"))
            check(f"RECORD_TO=guest {what}: a VM that never answers is cut off at the deadline, and it says so",
                  r.returncode != 0 and time.time() - t0 < 10 and "didn't answer within 2 s" in r.stderr
                  and not os.path.exists(state), (r.returncode, round(time.time() - t0, 1), r.stderr[-300:]))
        bindir = os.path.join(td, "quietbin")
        os.makedirs(bindir, exist_ok=True)
        with open(os.path.join(bindir, "ssh"), "w") as f:
            f.write("#!/bin/sh\nprintf 'video-start'\nexec sleep 30\n")
        os.chmod(os.path.join(bindir, "ssh"), 0o755)
        t0 = time.time()
        r = run_bash(cli_script(td, ("vm_copy_video",), 'ssh_base() { SSH_ARGS=(x); }\n'
                                f'vm_copy_video /home/u/metacomputer-runs/manual/q.mp4 "{td}/quiet.mp4"; echo "RC=$?"'),
                     dict(env, PATH=bindir + os.pathsep + os.environ["PATH"], MCOMP_RECORD_WAIT="1"))
        check("RECORD_TO=guest record stop: a VM that goes quiet mid-copy is cut off, keeping what arrived",
              "RC=5" in r.stdout and time.time() - t0 < 10 and open(os.path.join(td, "quiet.mp4"), "rb").read()
              == b"video-start", (r.stdout, round(time.time() - t0, 1), r.stderr[-300:]))
        r, log, st = start(cfg='RECORD_TO="cloud"')
        check("any other RECORD_TO is refused before anything starts", r.returncode != 0
              and "RECORD_TO must be host or guest" in r.stderr and "GSSH" not in log and not st, (r.stderr, log))

        # stop, host mode: a stream whose Mac side had already ended (the VM stopped sending)
        rec = os.path.join(td, "rec")
        os.makedirs(rec, exist_ok=True)

        def mkv(name):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "2",
                            "-pix_fmt", "yuv420p", "-f", "matroska", os.path.join(rec, name + ".mkv")], check=True,
                           stdin=subprocess.DEVNULL)

        def host_state(name, pid):
            with open(state, "w") as f:
                f.write(f"name={name}\nmode=host\ndir={rec}\npid={pid}\nstarted={time.time() - 5:.2f}\nfps=10\n"
                        "quality=standard\n")

        dead = subprocess.Popen(["true"])
        dead.wait()
        mkv("early")
        host_state("early", dead.pid)
        r = run_bash(cli_script(td, funcs, "do_record stop"), dict(env, REC_NAME="early"))
        side = json.load(open(os.path.join(rec, "early.json"))) if os.path.exists(os.path.join(rec, "early.json")) else {}
        check("stop: a stream that had already ended is saved all the same", r.returncode == 0
              and os.path.getsize(os.path.join(rec, "early.mp4")) > 0 and not os.path.exists(os.path.join(rec, "early.mkv")),
              (r.returncode, r.stderr))
        check("...says so, and the description file records it", "the stream had ended before this stop" in r.stderr
              and "stream_ended_early" in side, (r.stderr, side))

        # stop, host mode, the VM can't be reached (on a VM built before root-owned key lists, a wiped home
        # takes the SSH key): cut on the Mac, at once
        live = subprocess.Popen(["sleep", "60"])
        try:
            mkv("cut")
            host_state("cut", live.pid)
            t0 = time.time()
            r = run_bash(cli_script(td, funcs, "do_record stop"), dict(env, REC_NAME="cut", FAKE_VM="down"))
            took = time.time() - t0
            live.wait(timeout=5)
            check("stop with the VM unreachable: the stream is cut on the Mac within seconds, and saved",
                  r.returncode == 0 and took < 10 and live.returncode is not None
                  and os.path.getsize(os.path.join(rec, "cut.mp4")) > 0, (r.returncode, round(took, 1), r.stderr))
            check("...and it says why", "couldn't be reached to stop its recorder" in r.stderr, r.stderr)
        finally:
            if live.poll() is None:
                live.kill()
        check("the shared state is gone after each stop", not os.path.exists(state))

        # The VM's answers to record start/stop/status go through vm_answer: a byte limit and a deadline.
        src = open(CLI).read()
        rec_src = function(src, "do_record")
        check("record start/stop read the recorder's answers through vm_answer (bounded), not a raw ssh capture",
              '$(gssh' not in rec_src and "gssh ops /opt/metacomputer/bin/desktop-record status 2>/dev/null |" not in rec_src
              and 'gssh ops "cat' not in rec_src
              and rec_src.count("vm_answer 4096") >= 4 and rec_src.count("vm_copy_video") == 2,
              rec_src.count("vm_answer 4096"))
        check("stop asks the VM about a running recorder through vm_answer (bounded), not a raw ssh",
              "vm_answer 64 15 'pgrep -x ffmpeg" in function(src, "do_stop") and "gssh ops 'pgrep" not in function(src, "do_stop"))
        check("task finish and recordings --sync copy from the VM with a deadline (run_until)",
              'run_until $((SECONDS + 3600)) gssh ops "test -d' in function(src, "do_task")
              and "run_until $((SECONDS + 3600)) gssh ops 'if cd ~/metacomputer-runs" in function(src, "do_recordings"))
        bindir = os.path.join(td, "floodbin")
        os.makedirs(bindir, exist_ok=True)
        with open(os.path.join(bindir, "ssh"), "w") as f:
            f.write("#!/bin/sh\nyes | tr -d '\\n'\n")
        os.chmod(os.path.join(bindir, "ssh"), 0o755)
        t0 = time.time()
        r = run_bash(cli_script(td, ("vm_answer",), 'ssh_base() { SSH_ARGS=(x); }\n'
                                'out="$(vm_answer 4096 20 /opt/metacomputer/bin/desktop-record stop)"; echo "RC=$? LEN=${#out}"'),
                     dict(env, PATH=bindir + os.pathsep + os.environ["PATH"]))
        check("vm_answer: a VM that answers with endless bytes and no newline: nothing kept, a failure, at once",
              "LEN=0" in r.stdout and "RC=0" not in r.stdout and time.time() - t0 < 10, (r.stdout, r.stderr[-200:]))

        # A recording whose VM went away (crashed, stopped) is saved by reset/restore and stop, VM or no VM.
        stubs = ('vm_lock_or_die() { :; }\nexpect_guard() { :; }\nvm_exists() { return 0; }\nvm_running() { return 1; }\n'
                 'events_watcher_stop() { :; }\nstorage_py() { :; }\nstorage_release() { :; }\nnetgw_stop() { :; }\n'
                 'hub_stop() { :; }\nvm_stop() { :; }\ndo_recordings() { :; }\ndo_start() { echo STARTED; }\n'
                 'parse_flags() { REST=("$@"); }\nowners_check() { :; }\nLUME=/usr/bin/true\n')
        for caller, body in (("restore_from", "restore_from base baseline reset"), ("do_stop", "do_stop")):
            dead = subprocess.Popen(["true"])
            dead.wait()
            mkv(f"gone-{caller}")
            host_state(f"gone-{caller}", dead.pid)
            r = run_bash(cli_script(td, funcs + (caller,), stubs + body), dict(env, REC_NAME=f"gone-{caller}", FAKE_VM="down"))
            check(f"{caller} with the VM already down still saves the recording it was streaming",
                  os.path.isfile(os.path.join(rec, f"gone-{caller}.mp4")) and not os.path.exists(state),
                  (r.returncode, r.stderr[-400:]))
        # record start with a stale streamed recording (its Mac side ended): saved first, then the new one starts.
        dead = subprocess.Popen(["true"])
        dead.wait()
        mkv("stale")
        host_state("stale", dead.pid)
        r = run_bash(cli_script(td, funcs, "do_record start fresh"), dict(env, REC_NAME="fresh"))
        st = open(state).read() if os.path.exists(state) else ""
        check("record start: a stale streamed recording is saved first instead of blocking the new one",
              os.path.isfile(os.path.join(rec, "stale.mp4")) and "name=fresh" in st, (r.stderr[-400:], st))
    finally:
        pids = os.path.join(td, "stub-pids")     # the stand-in daemons this test started, and only those
        for pid in (open(pids).read().split() if os.path.exists(pids) else []):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        shutil.rmtree(td, ignore_errors=True)


FAKE_SSH = r"""#!/usr/bin/env python3
# A stand-in VM for the copier: `ssh ARGS... REMOTE_COMMAND` runs the command locally, with the task's guest path
# mapped to a local folder. FAKE_MODE plays a hostile or broken VM.
import io, os, subprocess, sys, tarfile
cmd = sys.argv[-1].replace(os.environ["FAKE_FROM"], os.environ["FAKE_TO"])
mode, marker = os.environ.get("FAKE_MODE", ""), os.environ.get("FAKE_MARKER", "")
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(cmd + "\n")
if "ls -1" in cmd and mode == "flood":
    out = sys.stdout.buffer
    for _ in range(64):
        out.write(b"x" * (1 << 20))        # 64 MB and no newline: more than any listing may be
    sys.exit(0)
if "tar -cf -" in cmd and mode in ("extra", "cutonce"):
    names = cmd.split("tar -cf - ", 1)[1].split()
    folder = os.environ["FAKE_TO"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for n in names:
            tf.add(os.path.join(folder, n), arcname=n)
        if mode == "extra":                # what wasn't asked for: a doctored copy of a turn the Mac already has
            data = b'{"doctored": true}\n'
            ti = tarfile.TarInfo("turn-00001/action.json"); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
            ti = tarfile.TarInfo("turn-00002/action.json"); ti.type = tarfile.DIRTYPE
            tf.addfile(ti)
    data = buf.getvalue()
    if mode == "cutonce" and not os.path.exists(marker):
        open(marker, "w").close()
        sys.stdout.buffer.write(data[:700])   # the first header and part of a file, then the stream breaks
        sys.exit(1)
    sys.stdout.buffer.write(data)
    sys.exit(0)
os.execvpe("bash", ["bash", "-c", cmd], dict(os.environ, COPYFILE_DISABLE="1"))
"""


def task_sync_checks():
    """The task copier (TASK_SYNC_INTERVAL) against a stand-in VM: a local folder plays the task's recording, and a
    stand-in `ssh` runs the copier's real commands (vm_answer, guest_import -- ssh) against it."""
    td = tempfile.mkdtemp(prefix="mcomp-rec-sync-")
    name = "mcomp-20261118-120000-sync"
    vm = os.path.join(td, "vm-recording")
    host = os.path.join(td, "lab", "results", name, "guest", "recording")
    pidf = os.path.join(td, "run", f"task-sync.{name}.pid")
    bindir = os.path.join(td, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "ssh"), "w") as f:
        f.write(FAKE_SSH.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    os.chmod(os.path.join(bindir, "ssh"), 0o755)
    funcs = ("task_sync_pidfile", "task_sync_on", "do_task_sync", "vm_answer", "guest_import")
    stand_in = 'ssh_base() { SSH_ARGS=(stand-in-vm); }\nIMPORT_MIN_FREE_GB=0\n'

    def turn(n, extra=""):
        d = os.path.join(vm, f"turn-{n:05d}" + extra)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "action.json"), "w") as f:
            f.write(f'{{"turn": {n}}}\n')

    def env(mode=""):
        return dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"], FAKE_TO=vm, FAKE_MODE=mode,
                    FAKE_FROM=f"/home/u/metacomputer-runs/{name}/recording", FAKE_LOG=os.path.join(td, "ssh.log"),
                    FAKE_MARKER=os.path.join(td, "cut-done"))

    def copier(mode="", extra="", wait=False, timeout=30):
        body = stand_in + f'echo $$ > "{pidf}"\n{extra}TASK_SYNC_INTERVAL=1 do_task_sync {name}\necho "EXIT $?"\n' \
            f'[ -e "{pidf}" ] || echo "PIDFILE GONE"\n'
        script = cli_script(td, funcs, body)
        if wait:
            return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=timeout,
                                  env=env(mode), stdin=subprocess.DEVNULL)
        return subprocess.Popen(["/bin/bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=env(mode), stdin=subprocess.DEVNULL)

    def have():
        return sorted(n for n in os.listdir(host) if not n.startswith(".")) if os.path.isdir(host) else []

    def wait_for(cond, s=10):
        end = time.time() + s
        while time.time() < end and not cond():
            time.sleep(0.2)
        return cond()

    def reset_host():
        shutil.rmtree(os.path.join(td, "lab", "results"), ignore_errors=True)
        for f in (f"task-sync.{name}.done", f"task-sync.{name}.want"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(td, "run", f))

    def stop(p):
        with contextlib.suppress(FileNotFoundError):
            os.remove(pidf)              # what task finish does first
        return p.communicate(timeout=15)

    import contextlib
    try:
        os.makedirs(os.path.join(td, "run"), exist_ok=True)
        for n in (1, 2, 3):
            turn(n)
        os.makedirs(os.path.join(vm, "turn-1;touch pwned"))      # a name the copier must never use
        open(os.path.join(vm, "recording.mp4"), "w").write("partial video")
        p = copier()
        check("copier: finished turns arrive while the task runs, the newest (still being written) doesn't",
              wait_for(lambda: have() == ["turn-00001", "turn-00002"]), have())
        check("...nor the video, which is only whole at stop_recording", "recording.mp4" not in have())
        with open(os.path.join(vm, "turn-00001", "action.json"), "w") as f:
            f.write('{"doctored": true}\n')
        turn(4)
        check("...once a newer turn exists, the one before it follows", wait_for(lambda: "turn-00003" in have()), have())
        check("...each turn is copied once: a change in the VM afterwards doesn't reach the Mac",
              open(os.path.join(host, "turn-00001", "action.json")).read() == '{"turn": 1}\n')
        check("...names other than turn-<digits> are never used", not os.path.exists(os.path.join(td, "pwned"))
              and not any(";" in n for n in have()), have())
        out, err = stop(p)
        check("...and it ends once task finish removes its pid file", "EXIT 0" in out, (out, err))

        # A hostile answer: asked for turn-00003, the VM also sends a doctored turn-00001 and a folder where
        # turn-00002's file is. Neither reaches what the Mac saved.
        reset_host()
        shutil.rmtree(vm)
        for n in (1, 2, 3):
            turn(n)
        p = copier()
        wait_for(lambda: have() == ["turn-00001", "turn-00002"])
        stop(p)
        turn(4)
        p = copier("extra")
        check("copier, hostile answer: the turn asked for still arrives", wait_for(lambda: "turn-00003" in have()), have())
        out, err = stop(p)
        check("...a turn the Mac already saved isn't replaced by what the VM slipped into the answer",
              open(os.path.join(host, "turn-00001", "action.json")).read() == '{"turn": 1}\n'
              and os.path.isfile(os.path.join(host, "turn-00002", "action.json")),
              open(os.path.join(host, "turn-00001", "action.json")).read())

        # A copy cut short is asked for again on the next pass (a folder that exists isn't "done").
        reset_host()
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(td, "cut-done"))
        p = copier("cutonce")
        check("copier, a copy cut short: retried on the next pass until the turns arrive whole",
              wait_for(lambda: all(os.path.isfile(os.path.join(host, t, "action.json"))
                                   for t in ("turn-00001", "turn-00002", "turn-00003")))
              and os.path.exists(os.path.join(td, "cut-done")), have())
        stop(p)

        # A VM that answers the listing with endless bytes (no newline): bounded, counted as no answer, ignored.
        reset_host()
        t0 = time.time()
        r = copier("flood", wait=True, timeout=60)
        check("copier, a listing past its byte limit: dropped, and after 8 such passes the copier ends; nothing copied",
              "EXIT 0" in r.stdout and "PIDFILE GONE" in r.stdout and have() == [] and time.time() - t0 < 45,
              (r.stdout[-200:], r.stderr[-300:], round(time.time() - t0, 1)))

        # A VM whose task folder is gone (wiped, or reset): the copier gives up on its own.
        reset_host()
        p = copier()
        wait_for(lambda: have() == ["turn-00001", "turn-00002", "turn-00003"])
        shutil.rmtree(vm)
        out, err = p.communicate(timeout=30)
        check("copier: a task folder that's gone ends it after a few tries, and it cleans up its pid file",
              "EXIT 0" in out and "PIDFILE GONE" in out, (out, err))
        check("...what it copied before stays on the Mac", have() == ["turn-00001", "turn-00002", "turn-00003"], have())
        # task.finished: what the VM made goes out only as classes, with no names and no video lengths.
        tname = "mcomp-20261118-130000-evt"
        thost = os.path.join(td, "lab", "results", tname)
        for i in range(12):
            os.makedirs(os.path.join(thost, "guest", "recording", f"turn-{i:05d}"), exist_ok=True)
        os.makedirs(os.path.join(thost, "guest", "files"), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "3",
                        "-pix_fmt", "yuv420p", os.path.join(thost, "guest", "files", "CALL-555-0100-hunter2.mp4")],
                       check=True, stdin=subprocess.DEVNULL)
        json.dump({"name": tname, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "requested_from": td},
                  open(os.path.join(thost, "task.json"), "w"))
        logs = os.path.join(td, "evlogs")
        r = run_bash(cli_script(td, ("task_sync_pidfile", "task_sync_stop", "do_task"),
                                'vm_running() { return 1; }\ndo_contact_sheet() { :; }\n'
                                f'do_task finish {tname}'),
                     {"MCOMP_LOG_DIR": logs, "MCOMP_EVENTS_DIR": os.path.join(td, "evcfg"), "MCOMP_VM_NAME": "t"})
        evs = [json.loads(x) for x in open(os.path.join(logs, "events.jsonl"))] \
            if os.path.exists(os.path.join(logs, "events.jsonl")) else []
        fin = [e for e in evs if e.get("type") == "finished"]
        flat = json.dumps(fin)
        check("task.finished: counts of what the VM made as classes, no file names, no video lengths",
              fin and fin[-1]["data"].get("videos") == "1-9" and fin[-1]["data"].get("recording_turns") == "10-99"
              and "CALL" not in flat and "hunter2" not in flat and "seconds" not in flat
              and "12 recording" not in flat, (r.stderr[-300:], fin))
        r = run_bash(cli_script(td, ("task_sync_pidfile", "task_sync_on", "task_sync_start"),
                                f'TASK_SYNC_INTERVAL=0 task_sync_start {name}; ls "$RUN_DIR"; cat "$TD/calls" 2>/dev/null'))
        check("TASK_SYNC_INTERVAL=0 starts no copier", "DAEMONIZE" not in r.stdout and "task-sync.mcomp" not in r.stdout,
              r.stdout)
        # A task nobody finishes: an hour (here 3 s) without a new turn ends the copier.
        os.makedirs(vm)
        turn(1)
        t0 = time.time()
        r = copier(extra="MCOMP_TASK_SYNC_IDLE=3 ", wait=True)
        check("copier: no new turn for its idle limit ends it, pid file and all", "EXIT 0" in r.stdout
              and "PIDFILE GONE" in r.stdout and time.time() - t0 < 15, (r.stdout, r.stderr))
        # task start: a copier per task, none with --no-sync (what web checks without --record pass)
        tfuncs = ("task_sync_pidfile", "task_sync_on", "task_sync_start", "do_task")
        for args, want in (("probe", True), ("probe --no-sync", False), ("--no-sync probe", False)):
            calls = os.path.join(td, "calls")
            if os.path.exists(calls):
                os.remove(calls)
            r = run_bash(cli_script(td, tfuncs, f'do_task start {args}; cat "$TD/calls" 2>/dev/null'))
            started = "DAEMONIZE" in r.stdout and re.search(r"_task-sync mcomp-\d{8}-\d{6}-probe\b", r.stdout)
            check(f"task start {args}: {'a copier' if want else 'no copier'}", r.returncode == 0
                  and re.search(r"^NAME=mcomp-\d{8}-\d{6}-probe$", r.stdout, re.M) and bool(started) == want,
                  (r.stdout, r.stderr))
    finally:
        pids = os.path.join(td, "stub-pids")
        for pid in (open(pids).read().split() if os.path.exists(pids) else []):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        shutil.rmtree(td, ignore_errors=True)


def wipe_tests():
    """On the test VM: what a VM that wipes its own home folder mid-recording can and can't take back."""
    env = dict(os.environ)
    cfg_base = os.environ.get("MCOMP_CONFIG")
    if not cfg_base:
        print("SKIP wipe tests: they need a test VM's MCOMP_CONFIG (they reset it)")
        return
    out_dir = tempfile.mkdtemp(prefix="mcomp-rec-wipe-")
    cfg = os.path.join(out_dir, "wipe.env")
    with open(cfg, "w") as f:   # the default RECORD_TO, whatever the test VM's config says
        f.write(f'. "{cfg_base}"\nRECORD_TO=host\nRECORD_DIR="{out_dir}"\nTASK_SYNC_INTERVAL=5\n')
    env["MCOMP_CONFIG"] = cfg
    sh = lambda c, t=60: subprocess.run([CLI, "sh", "-c", c], env=env, capture_output=True, text=True, timeout=t)  # noqa
    cli = lambda *a, t=120: subprocess.run([CLI, *a], env=env, capture_output=True, text=True, timeout=t)  # noqa
    try:
        # 0. The VM's key lists: root-owned, outside the home, not writable by the desktop user, no copy in ~/.ssh.
        r = sh("stat -c '%U %a' /etc/ssh/metacomputer-keys /etc/ssh/metacomputer-keys/$(id -un); "
               "test -w /etc/ssh/metacomputer-keys/$(id -un) && echo WRITABLE || echo read-only; "
               "test -e ~/.ssh/authorized_keys && echo HOME-KEYS || echo no-home-keys")
        check("keys: the desktop user's key list is root-owned, read-only to it, and not in its home",
              r.returncode == 0 and r.stdout.split() == ["root", "755", "root", "644", "read-only", "no-home-keys"],
              (r.stdout, r.stderr[-200:]))
        r = cli("status")
        check("keys: status says the key lists are outside the home folder", "outside the home folder" in r.stdout,
              r.stdout[-600:])

        # 1. A desktop recording with the defaults; the VM wipes ~ in the middle of it.
        name = f"t-wipe-{int(time.time())}"
        r = cli("record", "start", name)
        check("wipe: record start with the defaults", r.returncode == 0 and "streaming to" in r.stdout, r.stderr)
        sh("DISPLAY=:0 nohup mousepad >/dev/null 2>&1 & sleep 4")
        r = sh("rm -rf ~ 2>/dev/null; ls -A ~ | wc -l")
        check("wipe: the VM's home folder is wiped mid-recording", r.returncode == 0, r.stderr)
        time.sleep(4)
        t0 = time.time()
        r = cli("record", "stop", t=180)
        took = time.time() - t0
        mp4 = os.path.join(out_dir, name + ".mp4")
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", mp4, "-f", "null", "-"], capture_output=True, text=True) \
            if os.path.exists(mp4) else None
        side = json.load(open(mp4[:-4] + ".json")) if os.path.exists(mp4[:-4] + ".json") else {}
        check("wipe: the MP4 is on the Mac, decodes whole, and covers the time after the wipe",
              r.returncode == 0 and dec is not None and dec.returncode == 0 and dec.stderr.strip() == ""
              and (side.get("duration_s") or 0) >= 8, (r.returncode, r.stderr[-400:], side.get("duration_s")))
        check("wipe: record stop still reaches the VM and ends the recording the usual way",
              "couldn't be reached" not in r.stderr and "had ended before" not in r.stderr
              and "stream_ended_early" not in side and took < 25, (round(took, 1), r.stderr[-400:]))
        r = sh("echo alive; id -un")
        check("wipe: metacomputer sh still works after the wipe", r.returncode == 0 and r.stdout.startswith("alive"),
              (r.returncode, r.stderr[-300:]))
        r = cli("recordings", "--sync")
        check("wipe: recordings --sync still works (nothing to copy is not a failure)", r.returncode == 0,
              (r.returncode, r.stderr[-300:]))
        cli("reset", "--yes", t=400)

        # 2. A task's driver recording: finished turns copied while it runs survive the same wipe.
        r = cli("task", "start", "wipe-sync")
        tname = re.search(r"^NAME=(\S+)", r.stdout, re.M)
        rdir = re.search(r"^RECORDING_DIR=(\S+)", r.stdout, re.M)
        check("wipe: task start", r.returncode == 0 and tname and rdir, r.stderr)
        if tname and rdir:
            tname, rdir = tname.group(1), rdir.group(1)
            sess = ["call", "--session", "wipe"]
            cli(*sess, "start_recording", json.dumps({"output_dir": rdir, "record_video": True}))
            r = cli(*sess, "launch_app", '{"name": "mousepad"}')
            pid = re.search(r"pid (\d+)", r.stdout)
            for txt in ("one", "two", "three"):
                cli(*sess, "type_text", json.dumps({"pid": int(pid.group(1)) if pid else 0, "text": txt}))
            sh("sleep 8")
            host = os.path.join(LAB, "results", tname)
            copied = sorted(os.listdir(os.path.join(host, "guest", "recording"))) \
                if os.path.isdir(os.path.join(host, "guest", "recording")) else []
            check("wipe: finished turns reached the Mac while the task ran", len(copied) >= 2, copied)
            sh("rm -rf ~ 2>/dev/null; true")
            r = cli("task", "finish", tname)
            arts = json.load(open(os.path.join(host, "artifacts.json"))) if os.path.exists(
                os.path.join(host, "artifacts.json")) else {}
            check("wipe: after the wipe, task finish works and still lists the turns copied before it",
                  r.returncode == 0 and arts.get("recording_turns", 0) >= len(copied) >= 2 and "turns" in r.stdout
                  and "Permission denied" not in r.stderr, (r.returncode, r.stdout, r.stderr[-300:]))
            r = sh("echo alive")
            check("wipe: and metacomputer sh still works", r.returncode == 0 and r.stdout.startswith("alive"),
                  (r.returncode, r.stderr[-300:]))
            shutil.rmtree(host, ignore_errors=True)
        cli("reset", "--yes", t=400)
    finally:
        # The calls above started a hub with this temporary config; it mustn't outlive the file.
        cli("hub", "stop", t=60)
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg is not installed")
        return 0
    tmp = tempfile.mkdtemp(prefix="mcomp-rec-")
    try:
        mp4 = os.path.join(tmp, "clip.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                        "-t", "3", "-pix_fmt", "yuv420p", mp4], check=True)
        started = float(int(time.time()) - 30)   # the log keeps whole seconds
        ended = started + 20
        actions = os.path.join(tmp, "actions")
        day = time.strftime("%Y-%m-%d", time.localtime(started))
        write_log(os.path.join(actions, day, "100000-1.jsonl"), "showvm", [
            (ts(started - 60), "call", "list_windows", {}),                       # before: left out
            (ts(started + 2), "call", "launch_app", {"name": "inkscape"}),
            (ts(started + 5), "result", "launch_app", {}),                        # results: left out
            (ts(started + 7), "call", "type_text", {"text": {"redacted": True, "chars": 12}}),
            (ts(started + 9), "refused", "set_value", {"value": "x"}),
            (ts(started + 11), "call", "run_steps", {"steps": [{}, {}, {}]}),
            (ts(ended + 60), "call", "list_windows", {}),                         # after: left out
        ])
        write_log(os.path.join(actions, day, "100001-2.jsonl"), "othervm", [
            (ts(started + 3), "call", "launch_app", {"name": "firefox"}),         # another VM: left out
        ])
        out = subprocess.run([sys.executable, REC, "sidecar", mp4, "--name", "clip", "--started", str(started),
                              "--ended", str(ended), "--fps", "30", "--quality", "high", "--mode", "host",
                              "--vm", "showvm", "--actions-dir", actions], capture_output=True, text=True)
        check("sidecar runs", out.returncode == 0, out.stderr)
        side = json.load(open(os.path.join(tmp, "clip.json")))
        check("file, settings and mode recorded",
              (side["file"], side["fps"], side["quality"], side["mode"], side["vm"]) == ("clip.mp4", 30, "high", "host", "showvm"))
        check("length from ffprobe", side["duration_s"] and abs(side["duration_s"] - 3.0) < 0.3, side["duration_s"])
        check("size and sha256", side["bytes"] == os.path.getsize(mp4) and len(side["sha256"]) == 64)
        ms = side["moments"]
        check("only this VM's calls inside the recording", [m["tool"] for m in ms] ==
              ["launch_app", "type_text", "set_value", "run_steps"], [m["tool"] for m in ms])
        check("times are offsets into the video", [m["t"] for m in ms] == [2.0, 7.0, 9.0, 11.0], [m["t"] for m in ms])
        check("summaries", ms[0]["what"] == "name=inkscape" and ms[1]["what"] == "text=<12 chars>"
              and ms[3]["what"] == "3 steps", [m["what"] for m in ms])
        check("refused calls are marked", ms[2].get("refused") is True)
        check("log file listed for verification", side["action_log"]["files"] == [f"{day}/100000-1.jsonl"])
        listing = subprocess.run([sys.executable, REC, "moments", os.path.join(tmp, "clip.json")],
                                 capture_output=True, text=True).stdout
        check("moments listing", "0:02  launch_app  name=inkscape" in listing and "(refused)" in listing, listing)

        # A call the log records as sent and then ended without running: Cua Driver refusing it in bounded mode
        # (an error result), a shell run refused after its call was logged (status refused). Its moment says so,
        # as `actions show` does; a call that worked, or whose result came after the recording, stays plain.
        actions2 = os.path.join(tmp, "actions2")
        recs = [{"type": "start", "session": {"vm": "showvm"}},
                {"type": "call", "id": "c9.12", "tool": "launch_app", "args": {"name": "xfce4-terminal"}, "t": 2},
                {"type": "result", "id": "c9.12", "tool": "launch_app", "result": {"error": True, "images": 0}, "t": 2},
                {"type": "call", "id": "c10.13", "tool": "launch_app", "args": {"launch_path": "/usr/bin/mousepad"}, "t": 4},
                {"type": "result", "id": "c10.13", "tool": "launch_app", "result": {"error": False, "images": 0}, "t": 4},
                {"type": "call", "id": "c11.14", "tool": "click", "args": {"x": 1, "y": 2}, "t": 6},
                {"type": "result", "id": "c11.14", "tool": "click", "rpc_error": "the driver went away", "t": 6},
                {"type": "call", "id": "c12.15", "tool": "list_windows", "args": {}, "t": 8},
                {"type": "result", "id": "c12.15", "tool": "list_windows", "result": {"error": False}, "t": 30},
                {"type": "call", "id": "c13.16", "tool": "get_window_state", "args": {}, "t": 9}]   # no result yet
        write_raw = os.path.join(actions2, day, "100003-4.jsonl")
        os.makedirs(os.path.dirname(write_raw), exist_ok=True)
        with open(write_raw, "w") as f:
            for i, r in enumerate(recs):
                f.write(json.dumps(dict({k: v for k, v in r.items() if k != "t"}, seq=i,
                                        ts=ts(started + r.get("t", 0)))) + "\n")
        shell = os.path.join(actions2, day, "100004-5.jsonl")    # lib/vm_shell.py's records, its id always 1
        with open(shell, "w") as f:
            for i, r in enumerate([{"type": "start", "session": {"vm": "showvm"}},
                                   {"type": "call", "id": 1, "tool": "vm_shell", "args": {"script": {"redacted": True, "chars": 9}}},
                                   {"type": "result", "id": 1, "tool": "vm_shell",
                                    "result": {"status": "refused", "kind": "log", "error": True}}]):
                f.write(json.dumps(dict(r, seq=i, ts=ts(started + 10))) + "\n")
        out = subprocess.run([sys.executable, REC, "sidecar", mp4, "--name", "clip2", "--started", str(started),
                              "--ended", str(ended), "--fps", "30", "--quality", "high", "--mode", "host",
                              "--vm", "showvm", "--actions-dir", actions2], capture_output=True, text=True)
        side = json.load(open(os.path.join(tmp, "clip.json")))
        by = {(m["tool"], m["what"]): m for m in side["moments"]}
        term, mouse = by.get(("launch_app", "name=xfce4-terminal"), {}), by.get(("launch_app", ""), {})
        check("a call the driver refused (an error result) is marked, not listed as if it ran",
              out.returncode == 0 and term.get("error") is True and not term.get("refused"), side["moments"])
        check("…a call whose result is an RPC error too",
              by.get(("click", "x=1 y=2"), {}).get("error") is True, side["moments"])
        check("…a shell run refused after its call was logged is marked refused",
              by.get(("vm_shell", "script=<9 chars>"), {}).get("refused") is True, side["moments"])
        check("…while calls that worked, or have no result yet, stay plain",
              not any(m.get("error") or m.get("refused") for m in (mouse, by.get(("list_windows", ""), {}),
                                                                    by.get(("get_window_state", ""), {})))
              and len(side["moments"]) == 6, side["moments"])
        listing = subprocess.run([sys.executable, REC, "moments", os.path.join(tmp, "clip.json")],
                                 capture_output=True, text=True).stdout
        check("…and the listing shows each one's outcome",
              "launch_app (error)  name=xfce4-terminal" in listing and "vm_shell (refused)" in listing
              and "0:04  launch_app\n" in listing + "\n", listing)

        out = subprocess.run([sys.executable, REC, "sidecar", mp4, "--name", "clip", "--started", str(started),
                              "--ended", str(ended), "--fps", "20", "--quality", "standard", "--mode", "guest",
                              "--vm", "showvm"], capture_output=True, text=True)
        side = json.load(open(os.path.join(tmp, "clip.json")))
        check("without an action log: no moments, says why", out.returncode == 0 and side["moments"] == []
              and isinstance(side["action_log"], str))
        check("a silent video has no sound track", side.get("audio") is None, side.get("audio"))
        loud = os.path.join(tmp, "loud.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-ac", "2",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", loud], check=True)
        subprocess.run([sys.executable, REC, "sidecar", loud, "--name", "loud", "--started", str(started),
                        "--ended", str(ended), "--fps", "10", "--quality", "high", "--mode", "host",
                        "--vm", "showvm"], capture_output=True, text=True)
        a = json.load(open(os.path.join(tmp, "loud.json"))).get("audio") or {}
        check("a video with sound: its track is described", (a.get("codec"), a.get("sample_rate"), a.get("channels"))
              == ("aac", 48000, 2), a)
        check("probe: a video has a length", subprocess.run([sys.executable, REC, "probe", mp4],
                                                         capture_output=True).returncode == 0)
        bad = os.path.join(tmp, "bad.mp4")
        open(bad, "wb").write(b"not a video")
        check("probe: not a video", subprocess.run([sys.executable, REC, "probe", bad],
                                                   capture_output=True).returncode == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # The no-VM checks' free-space claims (finalize) go to a folder of their own, not this Mac's.
    claims = tempfile.mkdtemp(prefix="mcomp-rec-claims-")
    before = os.environ.get("MCOMP_STORAGE_HOME")
    os.environ["MCOMP_STORAGE_HOME"] = claims
    try:
        where_checks()
        save_checks()
        finalize_guard_checks()
        record_cli_checks()
        task_sync_checks()
    finally:
        if before is None:
            os.environ.pop("MCOMP_STORAGE_HOME", None)
        else:
            os.environ["MCOMP_STORAGE_HOME"] = before
        shutil.rmtree(claims, ignore_errors=True)
    if "--vm" in sys.argv:
        vm_tests()
        if "--wipe" in sys.argv:
            wipe_tests()
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
