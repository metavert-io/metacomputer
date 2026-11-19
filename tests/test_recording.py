#!/usr/bin/env python3
"""Desktop recordings and per-task recordings. Needs ffmpeg/ffprobe on the Mac.

    python3 tests/test_recording.py         no VM: the description file (lib/recording.py); where
                                            recordings go by default (RECORD_TO=host) and the warning
                                            for RECORD_TO=guest; the stream writer's limits; record stop
                                            when the stream ended early or the VM can't be reached; the
                                            task copier (TASK_SYNC_INTERVAL) against a stand-in VM
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
  case "$*" in
    *"desktop-record status"*) echo "recording stream:${REC_NAME:-x}" ;;
    *"desktop-record start"*) echo "recording /home/u/metacomputer-runs/manual/${REC_NAME:-x}.mp4 (1920x1080 @ 20 fps)" ;;
    *"desktop-record stop"*) echo "stream:${REC_NAME:-x}" ;;
    *) return 0 ;;
  esac
}
daemonize() { echo "DAEMONIZE $*" >> "$TD/calls"; sleep 20 & echo $! >> "$TD/stub-pids"; }
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
    finally:
        shutil.rmtree(td, ignore_errors=True)


def record_cli_checks():
    """record start / stop from bin/metacomputer, with a stand-in VM."""
    funcs = ("record_dir", "record_sidecar", "do_record")
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
    finally:
        pids = os.path.join(td, "stub-pids")     # the stand-in daemons this test started, and only those
        for pid in (open(pids).read().split() if os.path.exists(pids) else []):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass
        shutil.rmtree(td, ignore_errors=True)


def task_sync_checks():
    """The task copier (TASK_SYNC_INTERVAL) against a stand-in VM: a local folder plays the task's recording."""
    td = tempfile.mkdtemp(prefix="mcomp-rec-sync-")
    name = "mcomp-20261118-120000-sync"
    vm = os.path.join(td, "vm-recording")
    host = os.path.join(td, "lab", "results", name, "guest", "recording")
    pidf = os.path.join(td, "run", f"task-sync.{name}.pid")

    def turn(n, extra=""):
        d = os.path.join(vm, f"turn-{n:05d}" + extra)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "action.json"), "w") as f:
            f.write(f'{{"turn": {n}}}\n')

    # The stand-in VM: the copier's two commands (list, tar) run against the local folder.
    stand_in = r'''
gssh() {
  echo "GSSH $*" >> "$TD/calls"
  case "$*" in
    *"ls -1"*) [ -d "$FAKE_DIR" ] || { echo GONE; return 0; }; ( cd "$FAKE_DIR" && ls -1 && echo END ) ;;
    *"tar -cf -"*) ( cd "$FAKE_DIR" && eval "COPYFILE_DISABLE=1 tar -cf - ${*##*tar -cf - }" ) ;;   # no ._ files
    *) return 0 ;;
  esac
}
guest_import() { MCOMP_IMPORT_MIN_FREE_GB=0 "$PY" "$LAB_ROOT/lib/guest_import.py" "$@"; }
'''
    try:
        for n in (1, 2, 3):
            turn(n)
        os.makedirs(os.path.join(vm, "turn-1;touch pwned"))      # a name the copier must never use
        open(os.path.join(vm, "recording.mp4"), "w").write("partial video")
        body = (stand_in + f'\necho $$ > "{pidf}"\nTASK_SYNC_INTERVAL=1 do_task_sync {name}\necho "EXIT $?"\n')
        os.makedirs(os.path.join(td, "run"), exist_ok=True)
        script = cli_script(td, ("task_sync_pidfile", "task_sync_on", "do_task_sync"), body)
        p = subprocess.Popen(["/bin/bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=dict(os.environ, FAKE_DIR=vm), stdin=subprocess.DEVNULL)

        def have():
            return sorted(os.listdir(host)) if os.path.isdir(host) else []

        def wait_for(cond, s=8):
            end = time.time() + s
            while time.time() < end and not cond():
                time.sleep(0.2)
            return cond()

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
        os.remove(pidf)       # what task finish does first
        out, err = p.communicate(timeout=10)
        check("...and it ends once task finish removes its pid file", "EXIT 0" in out, (out, err))

        # A VM whose task folder is gone (wiped, or reset): the copier gives up on its own.
        shutil.rmtree(vm)
        body = (stand_in + f'\necho $$ > "{pidf}"\nTASK_SYNC_INTERVAL=1 do_task_sync {name}\n'
                f'echo "EXIT $?"; [ -e "{pidf}" ] || echo "PIDFILE GONE"\n')
        script = cli_script(td, ("task_sync_pidfile", "task_sync_on", "do_task_sync"), body)
        r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, FAKE_DIR=vm), stdin=subprocess.DEVNULL)
        check("copier: a task folder that's gone ends it after a few tries, and it cleans up its pid file",
              "EXIT 0" in r.stdout and "PIDFILE GONE" in r.stdout, (r.stdout, r.stderr))
        check("...what it copied before stays on the Mac", have() == ["turn-00001", "turn-00002", "turn-00003"], have())
        r = run_bash(cli_script(td, ("task_sync_pidfile", "task_sync_on", "task_sync_start"),
                                f'TASK_SYNC_INTERVAL=0 task_sync_start {name}; ls "$RUN_DIR"; cat "$TD/calls" 2>/dev/null'))
        check("TASK_SYNC_INTERVAL=0 starts no copier", "DAEMONIZE" not in r.stdout and "task-sync" not in r.stdout,
              r.stdout)
        # A task nobody finishes: an hour (here 3 s) without a new turn ends the copier.
        os.makedirs(vm)
        turn(1)
        body = (stand_in + f'\necho $$ > "{pidf}"\nMCOMP_TASK_SYNC_IDLE=3 TASK_SYNC_INTERVAL=1 do_task_sync {name}\n'
                f'echo "EXIT $?"; [ -e "{pidf}" ] || echo "PIDFILE GONE"\n')
        t0 = time.time()
        r = subprocess.run(["/bin/bash", "-c", cli_script(td, ("task_sync_pidfile", "task_sync_on", "do_task_sync"), body)],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, FAKE_DIR=vm),
                           stdin=subprocess.DEVNULL)
        check("copier: no new turn for its idle limit ends it, pid file and all", "EXIT 0" in r.stdout
              and "PIDFILE GONE" in r.stdout and time.time() - t0 < 15, (r.stdout, r.stderr))
        # task start: a copier per task, none with --no-sync (what web checks without --record pass)
        funcs = ("task_sync_pidfile", "task_sync_on", "task_sync_start", "do_task")
        for args, want in (("probe", True), ("probe --no-sync", False), ("--no-sync probe", False)):
            calls = os.path.join(td, "calls")
            if os.path.exists(calls):
                os.remove(calls)
            r = run_bash(cli_script(td, funcs, f'do_task start {args}; cat "$TD/calls" 2>/dev/null'))
            started = "DAEMONIZE" in r.stdout and re.search(r"_task-sync mcomp-\d{8}-\d{6}-probe\b", r.stdout)
            check(f"task start {args}: {'a copier' if want else 'no copier'}", r.returncode == 0
                  and re.search(r"^NAME=mcomp-\d{8}-\d{6}-probe$", r.stdout, re.M) and bool(started) == want,
                  (r.stdout, r.stderr))
        pids = os.path.join(td, "stub-pids")
        for pid in (open(pids).read().split() if os.path.exists(pids) else []):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                pass
    finally:
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
    where_checks()
    save_checks()
    record_cli_checks()
    task_sync_checks()
    if "--vm" in sys.argv:
        vm_tests()
        if "--wipe" in sys.argv:
            wipe_tests()
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
