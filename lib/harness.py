"""virtual-computer GUI test harness.

Each test gets one persistent MCP connection to Cua Driver in the guest (the
same `virtual-computer mcp` bridge Claude Code uses), and:
  - a driver-owned recording (`start_recording` with record_video): the MP4
    (ffmpeg x11grab in the guest) plus a per-action trajectory (before/after
    accessibility state, before/after window images, click markers, action.json)
  - an action log (logs every MCP call: tool, args, ok, elapsed, summary)
  - milestone screenshots + a final desktop screenshot
  - independent verification (accessibility values re-read, window titles read
    out-of-band with xprop, files checked over SSH) kept separate from
    "the tool call succeeded"
  - result.json (machine-readable) and a sidecar next to the MP4

Everything is copied to the host: recordings/<run>/, screenshots/<run>/,
results/<run>/. Nothing needed afterwards stays only in the VM.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import traceback

from mcp_client import LAB_ROOT, MCPClient

SSH = [os.path.join(LAB_ROOT, "bin", "virtual-computer"), "ssh", "ops"]
from labconfig import GUEST_RUNS  # noqa: E402  (guest home of the configured user)


def guest(cmd: str, timeout=60, check=False) -> subprocess.CompletedProcess:
    """Run a shell command in the guest as the automation user (out-of-band:
    used only for setup, artifact verification and copying evidence)."""
    p = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"guest command failed ({p.returncode}): {cmd}\n{p.stderr}")
    return p


def guest_session(cmd: str, timeout=60) -> subprocess.CompletedProcess:
    """Like guest(), inside the desktop session environment (DISPLAY etc.)."""
    return guest("/opt/virtual-computer/bin/session-run sh -c " + shlex.quote(cmd), timeout=timeout)


class Step(Exception):
    pass


class TestRun:
    def __init__(self, run_id: str, test_id: str, title: str, task: str):
        self.run_id, self.test_id, self.title, self.task = run_id, test_id, title, task
        self.slug = f"{test_id}"
        self.res_dir = os.path.join(LAB_ROOT, "results", run_id, test_id)
        self.shot_dir = os.path.join(LAB_ROOT, "screenshots", run_id, test_id)
        self.rec_dir = os.path.join(LAB_ROOT, "recordings", run_id)
        for d in (self.res_dir, self.shot_dir, self.rec_dir):
            os.makedirs(d, exist_ok=True)
        self.guest_dir = f"{GUEST_RUNS}/{run_id}-{test_id}"
        self.actions_path = os.path.join(self.res_dir, "actions.jsonl")
        self.actions = 0
        self.a11y_actions = 0
        self.pixel_actions = 0
        self.screenshots = 0
        self.errors: list[str] = []
        self.recoveries: list[str] = []
        self.evidence: dict = {}
        self.checks: list[dict] = []
        self.milestones: list[dict] = []
        self.video_host = None
        self.recording_state = {}
        self.t0 = None
        self.c = MCPClient(log=self._log, stderr_path=os.path.join(self.res_dir, "mcp-stderr.log"))
        self.session = f"{test_id.lower()}-{run_id[-6:]}"

    # -- logging -----------------------------------------------------------
    def _log(self, entry):
        entry["t_rel"] = round(time.time() - (self.t0 or time.time()), 3)
        with open(self.actions_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        if entry["tool"] not in READ_ONLY:
            self.actions += 1
            # Classify by the route the driver reports it actually used:
            # "accessibility" = AT-SPI action/text/menu; anything else
            # (global_input, synthetic_events, ...) = real input events.
            if entry.get("route") == "accessibility":
                self.a11y_actions += 1
            elif entry.get("route"):
                self.pixel_actions += 1
        if not entry["ok"] and entry["tool"] not in ("stop_recording",):
            self.errors.append(f"{entry['tool']}: {entry['summary'][:200]}")

    def note(self, msg):
        print(f"    · {msg}", flush=True)

    def milestone(self, label, **data):
        self.milestones.append({"t_rel": round(time.time() - self.t0, 2), "label": label, **data})
        self.note(label)

    def check(self, name, passed, detail=""):
        self.checks.append({"check": name, "passed": bool(passed), "detail": str(detail)[:600]})
        self.note(f"{'PASS' if passed else 'FAIL'} {name}: {str(detail)[:140]}")
        return passed

    # -- MCP helpers -------------------------------------------------------
    def call(self, tool, args=None, timeout=120, ok_required=True):
        args = dict(args or {})
        if tool in SESSIONED and "session" not in args:
            args["session"] = self.session
        r = self.c.call(tool, args, timeout=timeout)
        if r.is_error and ok_required:
            raise Step(f"{tool} failed: {r.text[:300]}")
        return r

    def shot(self, label, pid=None, window_id=None):
        """Milestone screenshot: a window (with its accessibility tree) or the desktop."""
        self.screenshots += 1
        name = f"{self.screenshots:02d}-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}"
        if pid is not None and window_id is not None:
            r = self.call("get_window_state", {"pid": pid, "window_id": window_id}, ok_required=False)
        else:
            r = self.call("get_desktop_state", {}, ok_required=False)
        paths = r.save_images(os.path.join(self.shot_dir, name))
        with open(os.path.join(self.shot_dir, name + ".txt"), "w") as f:
            f.write(r.text)
        self.milestone(f"screenshot {name}", files=[os.path.relpath(p, LAB_ROOT) for p in paths])
        return r

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.t0 = time.time()
        guest(f"rm -rf {self.guest_dir}; mkdir -p {self.guest_dir}")
        self.c.start()
        self.milestone("mcp connected", server=self.c.server_info, protocol=self.c.protocol)
        r = self.call("start_recording", {"output_dir": f"{self.guest_dir}/trajectory", "record_video": True})
        st = self.call("get_recording_state", {}, ok_required=False)
        self.recording_state = st.structured or {"text": st.text}
        self.milestone("recording started", state=_brief(self.recording_state))

    def stop(self):
        try:
            self.call("stop_recording", {}, ok_required=False, timeout=180)
            st = self.call("get_recording_state", {}, ok_required=False)
            self.recording_state = st.structured or {"text": st.text}
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"stop_recording: {e}")
        self.c.close()
        self._collect()

    def _collect(self):
        # Pull the guest run dir (video + trajectory) to the host.
        dest = os.path.join(self.res_dir, "guest")
        os.makedirs(dest, exist_ok=True)
        p = subprocess.run(SSH + [f"cd {self.guest_dir} && tar -cf - ."], capture_output=True, timeout=600)
        if p.returncode == 0 and p.stdout:
            subprocess.run(["tar", "-C", dest, "-xf", "-"], input=p.stdout, check=False)
        vids = []
        for d, _, fs in os.walk(dest):
            vids += [os.path.join(d, f) for f in fs if f.endswith(".mp4")]
        if vids:
            vids.sort(key=os.path.getsize, reverse=True)
            self.video_host = os.path.join(self.rec_dir, f"{self.test_id}.mp4")
            os.replace(vids[0], self.video_host)
            for extra in vids[1:]:
                os.replace(extra, os.path.join(self.rec_dir, f"{self.test_id}-{os.path.basename(extra)}"))
        traj = os.path.join(dest, "trajectory")
        self.evidence["trajectory_turns"] = len([d for d in os.listdir(traj) if d.startswith("turn-")]) if os.path.isdir(traj) else 0

    def finish(self, passed, failure=None):
        elapsed = round(time.time() - self.t0, 1) if self.t0 else 0
        video = _probe(self.video_host) if self.video_host else None
        result = {
            "run_id": self.run_id, "test": self.test_id, "title": self.title, "task": self.task,
            "result": "PASS" if passed else "FAIL",
            "tool_calls_ok": not any("failed" in e for e in self.errors),
            "task_verified": passed,
            "failure": failure,
            "elapsed_s": elapsed,
            "actions": self.actions, "a11y_actions": self.a11y_actions, "input_event_actions": self.pixel_actions,
            "mcp_calls": self.c.calls, "screenshots": self.screenshots,
            "checks": self.checks, "milestones": self.milestones,
            "errors": self.errors, "recoveries": self.recoveries,
            "human_intervention": False,
            "recording": os.path.relpath(self.video_host, LAB_ROOT) if self.video_host else None,
            "video": video, "recording_state": _brief(self.recording_state),
            "evidence": self.evidence,
            "trajectory": os.path.relpath(os.path.join(self.res_dir, "guest", "trajectory"), LAB_ROOT),
            "actions_log": os.path.relpath(self.actions_path, LAB_ROOT),
            "screenshots_dir": os.path.relpath(self.shot_dir, LAB_ROOT),
        }
        with open(os.path.join(self.res_dir, "result.json"), "w") as f:
            json.dump(result, f, indent=2)
        if self.video_host:
            with open(self.video_host[:-4] + ".json", "w") as f:
                json.dump({"test": f"{self.test_id}: {self.title}", "task": self.task, "result": result["result"],
                           "run_id": self.run_id, "result_json": os.path.relpath(os.path.join(self.res_dir, "result.json"), LAB_ROOT)},
                          f, indent=2)
        return result


READ_ONLY = {"list_apps", "list_windows", "get_window_state", "get_desktop_state", "get_accessibility_tree",
             "verify_state", "get_screen_size", "get_cursor_position", "zoom", "get_recording_state",
             "start_recording", "stop_recording", "get_session", "list_sessions", "start_session", "end_session",
             "get_config", "check_permissions", "health_report", "debug_window_info", "clipboard_read"}
SESSIONED = {"list_apps", "list_windows", "launch_app", "get_window_state", "get_desktop_state", "verify_state",
             "click", "double_click", "right_click", "type_text", "press_key", "hotkey", "set_value", "scroll",
             "move_cursor", "invoke_menu", "bring_to_front", "zoom", "kill_app", "drag"}


def _brief(d):
    if not isinstance(d, dict):
        return d
    keep = ("active", "recording", "video_active", "last_video_path", "last_error", "output_dir", "turns", "turn_count")
    return {k: d[k] for k in keep if k in d} or {k: d[k] for k in list(d)[:8]}


def _probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,avg_frame_rate,nb_frames,codec_name:format=duration,size",
                        "-of", "json", path], capture_output=True, text=True)
    try:
        j = json.loads(p.stdout)
        s = j["streams"][0]
        fr = s.get("avg_frame_rate", "0/1").split("/")
        return {"width": s.get("width"), "height": s.get("height"), "codec": s.get("codec_name"),
                "fps": round(int(fr[0]) / max(1, int(fr[1])), 1), "frames": s.get("nb_frames"),
                "duration_s": round(float(j["format"]["duration"]), 1), "bytes": int(j["format"]["size"]),
                "playable": True}
    except Exception as e:  # noqa: BLE001
        return {"playable": False, "error": (p.stderr or str(e))[:300]}


def run_test(fn, run_id, test_id, title, task):
    t = TestRun(run_id, test_id, title, task)
    print(f"\n== Test {test_id}: {title}", flush=True)
    passed, failure = False, None
    try:
        t.start()
        passed = bool(fn(t))
    except Exception as e:  # noqa: BLE001
        failure = f"{type(e).__name__}: {e}"
        t.errors.append(failure)
        with open(os.path.join(t.res_dir, "traceback.txt"), "w") as f:
            f.write(traceback.format_exc())
        try:
            t.shot("failure state")
        except Exception:  # noqa: BLE001
            pass
    finally:
        t.stop()
    if passed and not all(c["passed"] for c in t.checks):
        passed, failure = False, "a verification check failed"
    res = t.finish(passed, failure)
    v = res.get("video") or {}
    print(f"   => {res['result']} in {res['elapsed_s']}s, {res['actions']} actions, {res['screenshots']} screenshots, "
          f"video {v.get('width')}x{v.get('height')} {v.get('duration_s')}s", flush=True)
    return res
