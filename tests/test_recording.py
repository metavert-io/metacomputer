#!/usr/bin/env python3
"""Desktop recordings. Needs ffmpeg/ffprobe on the Mac.

    python3 tests/test_recording.py         the description file (lib/recording.py); no VM
    MCOMP_CONFIG=test-vm.env python3 tests/test_recording.py --vm
                                            also `metacomputer record` in both RECORD_TO modes and
                                            both qualities on a running test VM (about a minute)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC = os.path.join(LAB, "lib", "recording.py")
FAILS = []


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
    if "--vm" in sys.argv:
        vm_tests()
    print(f"\n{'all passed' if not FAILS else str(len(FAILS)) + ' failed'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
