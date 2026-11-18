#!/usr/bin/env python3
"""metacomputer task benchmark: the same tasks, model, harness and budgets, one
switch changed at a time (HCU-style). See bench/README.md.

  bench/bench.py list
  bench/bench.py run [--tasks a,b] [--run-id ID] [switches...]     one headless session per task
  bench/bench.py check TASK [--setup]       (re)run a task's setup and/or check, no model
  bench/bench.py report RUN_ID              write results/bench/RUN_ID/summary.md
  bench/bench.py compare RUN_A RUN_B        per-task and total differences
  bench/bench.py remetric RUN_ID            recompute metrics from the saved transcripts

The VM comes from MCOMP_CONFIG (required: the benchmark resets that VM to its
baseline before every task). Switches (flag or environment variable):
  --model / BENCH_MODEL            default claude-opus-5-5
  --skill / BENCH_SKILL            skill directory (default: this checkout's skills/metacomputer)
  --shell / BENCH_SHELL            allow | forbid  (shell inside the VM: metacomputer ssh / sh)
  --coord-guard / BENCH_COORD_GUARD  off | on     (passed to the bridge as MCOMP_COORD_GUARD)
  --hub / BENCH_HUB                on | off       (passed as MCOMP_MCP_HUB; default: the VM config's MCP_HUB)
  --screen / BENCH_SCREEN          WxH, set with xrandr after each reset (default: the VM's own)
  --effort / BENCH_EFFORT          Claude Code effort level (default: the model's default)
  --budget / BENCH_BUDGET          per-task cap in USD (default 3; a task file may set its own)
  --cap / BENCH_CAP                total cap in USD for the run (default 60)
  --reset / BENCH_RESET            each | none    (reset the VM before each task)
  --lessons / BENCH_LESSONS        off | on       (the lessons approved with `metacomputer learn`, from
                                                   MCOMP_LESSONS_DIR or the default store; default off)
"""
import argparse
import datetime
import glob
import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib

BENCH = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(BENCH)
CLI = os.path.join(LAB, "bin", "metacomputer")
sys.path.insert(0, BENCH)
import metrics  # noqa: E402
import report  # noqa: E402

RESULTS = os.environ.get("BENCH_RESULTS") or os.path.join(LAB, "results", "bench")
TASKS = os.path.join(BENCH, "tasks")
FIX = os.path.join(BENCH, "fixtures")
GUEST = os.path.join(BENCH, "guest")
SRV = "~/.cache/bench-srv"          # in the VM: fixture server, its log and helpers (hidden from ~/bench)
PREFIX = ("Use your virtual computer (the metacomputer) for this. Leave the virtual computer running when you "
          "finish; no recording is needed.\n\n")
SHELL_FORBID = ("\n\nDon't use a shell inside the virtual computer for this (no `metacomputer ssh` or "
                "`metacomputer sh`): work through its desktop.")
DEFAULTS = {"model": "claude-opus-5-5", "skill": os.path.join(LAB, "skills", "metacomputer"), "shell": "allow",
            "coord_guard": "off", "hub": "", "screen": "", "effort": "", "budget": "3", "cap": "60", "reset": "each",
            "lessons": "off"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- the VM, through this checkout's CLI ------------------------------------------------------
def mc(*args, input=None, timeout=300):
    try:
        r = subprocess.run([CLI, *args], input=input, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s: metacomputer {' '.join(args[:2])}"


def guest(script, timeout=300):
    return mc("ssh", "ops", "bash -s", input=script, timeout=timeout)


def vm_config():
    out = subprocess.run(["bash", "-c", '. "$1/scripts/lib.sh" >/dev/null 2>&1; '
                          'printf "%s\\n" "$VM_NAME" "$BASELINE_NAME" "$SSH_PORT" "$VM_DISPLAY"', "_", LAB],
                         capture_output=True, text=True).stdout.split("\n")
    return {"vm": out[0], "baseline": out[1], "ssh_port": out[2], "display": out[3]}


def status():
    rc, out, _ = mc("status", "--json", timeout=120)
    try:
        return json.loads(out)
    except ValueError:
        return {"ready": False}


def busy_others():
    rc, out, _ = mc("who", "--json", timeout=60)
    try:
        d = json.loads(out)
    except ValueError:
        return []
    return [s for s in d.get("sessions", []) if s.get("active")] + list(d.get("direct", []))


def reset_vm():
    rc, out, err = mc("reset", "--yes", timeout=900)
    st = status()
    return rc == 0 and st.get("ready"), (err or out)[-400:], st


def set_screen(size):
    script = (". /run/user/$(id -u)/metacomputer/session.env; export DISPLAY XAUTHORITY; "
              "o=$(xrandr | awk '/ connected/{print $1; exit}'); xrandr --output \"$o\" --mode " + size +
              " && sleep 2 && xrandr | awk '/\\*/{print $1; exit}'")
    rc, out, err = guest(script, timeout=60)
    return out.strip() if rc == 0 else f"error: {(err or out).strip()[:200]}"


# --- tasks ----------------------------------------------------------------------------------
def load_tasks(ids=None):
    tasks = []
    for p in sorted(glob.glob(os.path.join(TASKS, "*.toml"))):
        with open(p, "rb") as f:
            t = tomllib.load(f)
        t.setdefault("id", os.path.basename(p)[:-5])
        tasks.append(t)
    if ids:
        want = [i.strip() for i in ids.split(",") if i.strip()]
        by = {t["id"]: t for t in tasks}
        missing = [i for i in want if i not in by]
        if missing:
            sys.exit(f"unknown task(s): {', '.join(missing)} (bench/bench.py list)")
        tasks = [by[i] for i in want]
    return tasks


SETUP_LIB = r"""
set -e
mkdir -p ~/bench/out
to_odf() { # FORMAT SRC OUTDIR: convert with a separate LibreOffice profile (never the user's)
  local so=/usr/bin/soffice
  [ -x "$HOME/.local/lib/lo-calc/program/soffice" ] && so="$HOME/.local/lib/lo-calc/program/soffice"
  mkdir -p "$3"
  "$so" -env:UserInstallation=file:///tmp/lo-bench-setup --headless --convert-to "$1" --outdir "$3" "$2" >/dev/null 2>&1 || true
  rm -rf /tmp/lo-bench-setup
  local b; b="$(basename "$2")"; [ -f "$3/${b%.*}.$1" ] || { echo "to_odf: no $3/${b%.*}.$1" >&2; return 1; }
}
"""


def setup_task(t):
    """Files, the fixture server, Calc if needed, then the task's own setup. Returns a dict."""
    info = {"ok": False}
    t0 = time.time()
    rc, out, err = guest("mkdir -p ~/bench/out " + SRV)
    if rc != 0:
        info["error"] = (err or out)[-300:]
        return info
    for f in t.get("files", []):
        rc, out, err = mc("put", os.path.join(FIX, "files", f), "--to", "~/bench")
        if rc != 0:
            info["error"] = f"put {f}: {(err or out)[-200:]}"
            return info
    if "calc" in t.get("requires", []):
        cache = os.path.join(RESULTS, "cache", "lodeb")
        mc("put", os.path.join(GUEST, "calc-overlay.sh"), "--to", SRV)
        if os.path.isdir(cache):
            mc("put", cache, "--to", SRV)
        rc, out, err = guest(f"cd {SRV} && if [ ! -e /usr/lib/libreoffice/program/libsclo.so ] && ! ls lodeb/*.deb >/dev/null 2>&1; "
                             "then mkdir -p lodeb && (cd lodeb && apt-get download libreoffice-calc libreoffice-uiconfig-calc >/dev/null); fi; "
                             "bash calc-overlay.sh lodeb", timeout=300)
        info["calc"] = out.strip().split("\n")[-1] if rc == 0 else None
        if rc != 0:
            info["error"] = f"calc: {(err or out)[-300:]}"
            return info
    if t.get("serve"):
        mc("put", os.path.join(FIX, "site"), "--to", SRV)
        mc("put", os.path.join(GUEST, "server.py"), "--to", SRV)
        rc, out, err = guest(
            f"cd {SRV} && : > requests.jsonl && (setsid nohup python3 server.py site requests.jsonl 8000 "
            "</dev/null >server.err 2>&1 &) && for i in 1 2 3 4 5 6 7 8 9 10; do python3 -c "
            "'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8000/index.html\", timeout=2)' "
            "2>/dev/null && break; sleep 0.5; done && : > requests.jsonl && echo serving")
        if "serving" not in out:
            info["error"] = f"server: {(err or out)[-300:]}"
            return info
    if t.get("setup"):
        rc, out, err = guest(SETUP_LIB + t["setup"], timeout=300)
        if rc != 0:
            info["error"] = f"setup: {(err or out)[-300:]}"
            return info
    info["ok"] = True
    info["s"] = round(time.time() - t0, 1)
    return info


CHECK_HEAD = "import sys\nsys.path.insert(0, '/tmp/bench-check')\nfrom checklib import *\nC = Checks()\n"


def check_task(t):
    mc("put", os.path.join(GUEST, "checklib.py"), "--to", "/tmp/bench-check")
    script = CHECK_HEAD + t["check"] + "\nC.report()\n"
    rc, out, err = mc("ssh", "ops", "cd /tmp/bench-check && python3 -", input=script, timeout=120)
    for line in reversed(out.strip().split("\n")):
        try:
            r = json.loads(line)
            if isinstance(r, dict) and "score" in r:
                return r
        except ValueError:
            continue
    return {"pass": False, "score": 0.0, "checks": [], "error": (err or out)[-400:]}


# --- the headless session ---------------------------------------------------------------------
def trusted_claude(path=None):
    """The `claude` program as an absolute path, looked up on PATH as it was before the
    shim folder goes in front (absolute entries only), and owned by you or root and not
    writable by other accounts. The session runs this file, never a `claude` found
    through the shim folder."""
    entries = [d for d in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep) if os.path.isabs(d)]
    p = shutil.which("claude", path=os.pathsep.join(entries))
    if not p:
        sys.exit("claude isn't on PATH")
    real = os.path.realpath(p)
    st = os.stat(real)
    if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
        sys.exit(f"refusing {real}: owned by another account or writable by others")
    return real


def prepare_workdir(run_id, sw):
    """A fresh private folder (mkdtemp: 0700, yours, an unpredictable name in your own
    temporary folder) outside any repository (so no CLAUDE.md is picked up), with the
    skill under test as a project skill and `metacomputer` on PATH for this checkout.
    A predictable shared path like /tmp/mcomp-bench/<run> could be made in advance by
    another account, with its own `claude` in the shim folder."""
    work = tempfile.mkdtemp(prefix=f"mcomp-bench-{run_id}-")
    st = os.lstat(work)
    if not os.path.isdir(work) or os.path.islink(work) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        sys.exit(f"refusing the work folder {work}: not a private folder of yours")
    skill_dst = os.path.join(work, ".claude", "skills", "metacomputer")
    shutil.copytree(sw["skill"], skill_dst)
    # The skill names the installed launcher; point the copy at the checkout under test.
    for p in glob.glob(os.path.join(skill_dst, "**", "*.md"), recursive=True):
        s = open(p).read()
        open(p, "w").write(s.replace("~/.local/bin/metacomputer", CLI))
    shim = os.path.join(work, "bin")
    os.mkdir(shim, 0o700)
    for n in ("metacomputer", "mcomp"):
        os.symlink(CLI, os.path.join(shim, n))
    cfg = os.path.join(work, "mcp.json")
    env = {"MCOMP_CONFIG": os.environ["MCOMP_CONFIG"], "MCOMP_COORD_GUARD": sw["coord_guard"]}
    if sw.get("hub"):
        env["MCOMP_MCP_HUB"] = sw.get("hub")
    env.update(lessons_env(sw))
    json.dump({"mcpServers": {"metacomputer": {"type": "stdio", "command": CLI, "args": ["mcp-lazy"], "env": env}}},
              open(cfg, "w"))
    return work, cfg, shim


def session_args(t, sw, cfg):
    # The session's own --mcp-config server gives mcp__metacomputer__*; the plugin's name is allowed too.
    allowed = ["Skill", "Read", "Glob", "Grep", "ToolSearch", "mcp__metacomputer", "mcp__plugin_metacomputer_metacomputer",
               f"Bash({CLI}:*)", "Bash(metacomputer:*)", "Bash(mcomp:*)"]
    denied = []
    if sw["shell"] == "forbid":
        for c in (CLI, "metacomputer", "mcomp"):
            denied += [f"Bash({c} ssh:*)", f"Bash({c} sh:*)"]
    budget = float(t.get("budget_usd") or sw["budget"])
    args = ["-p", PREFIX + t["prompt"].strip() + (SHELL_FORBID if sw["shell"] == "forbid" else ""),
            "--model", sw["model"], "--setting-sources", "project", "--strict-mcp-config", "--mcp-config", cfg,
            "--max-turns", str(int(t.get("max_turns", 60))), "--max-budget-usd", f"{budget:.2f}",
            "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--allowedTools", *allowed]
    if denied:
        args += ["--disallowedTools", *denied]
    if sw["effort"]:
        args += ["--effort", sw["effort"]]
    return args, budget


def lessons_env(sw):
    """The lessons switch for the session's MCP server: MCOMP_LESSONS on|off, and the store it reads
    (MCOMP_LESSONS_DIR as this run has it, else the default store)."""
    env = {"MCOMP_LESSONS": sw.get("lessons") or "off"}
    if os.environ.get("MCOMP_LESSONS_DIR"):
        env["MCOMP_LESSONS_DIR"] = os.environ["MCOMP_LESSONS_DIR"]
    return env


def lessons_record(sw):
    """What the sessions get: how many lessons and a hash of the exact instruction text (lib/learn_store.py)."""
    import hashlib
    sys.path.insert(0, os.path.join(LAB, "lib"))
    import learn_store
    env = dict(os.environ, **lessons_env(sw))
    text = learn_store.instructions_text(env)
    s = learn_store.summary(env)
    return {"lessons_count": s["delivered"] if text else 0,
            "lessons_hash": hashlib.sha256(text.encode()).hexdigest()[:12] if text else None,
            "lessons_problem": s["problem"]}


def run_session(t, sw, work, cfg, shim, tdir, claude=None):
    args, budget = session_args(t, sw, cfg)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    claude = claude or trusted_claude(env.get("PATH", ""))   # resolved before the shim goes first
    env["PATH"] = shim + os.pathsep + env.get("PATH", "")
    env["MCOMP_COORD_GUARD"] = sw["coord_guard"]
    if sw.get("hub"):
        env["MCOMP_MCP_HUB"] = sw.get("hub")
    env.update(lessons_env(sw))
    timeout = int(t.get("timeout_s", 600))
    events, times = [], []
    t0 = time.time()
    tr = gzip.open(os.path.join(tdir, "transcript.jsonl.gz"), "wt")
    err = open(os.path.join(tdir, "session.stderr"), "w")
    proc = subprocess.Popen([claude, *args], cwd=work, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=err, text=True, start_new_session=True)

    def reader():
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            small = metrics.compact(ev)
            dt = round(time.time() - t0, 2)
            events.append(small)
            times.append(dt)
            tr.write(json.dumps({"t": dt, **small}) + "\n")

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        log(f"  timeout after {timeout} s; stopping the session")
        for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 5)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
    th.join(timeout=10)
    tr.close()
    err.close()
    return events, times, {"exit": proc.returncode, "timed_out": timed_out, "wall_s": round(time.time() - t0, 1),
                           "timeout_s": timeout, "budget_usd": budget, "max_turns": int(t.get("max_turns", 60))}


# --- a run --------------------------------------------------------------------------------------
def git_info():
    def g(*a):
        return subprocess.run(["git", "-C", LAB, *a], capture_output=True, text=True).stdout.strip()
    return {"sha": g("rev-parse", "HEAD"), "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(g("status", "--porcelain", "--untracked-files=no"))}


def skill_hash(d):
    import hashlib
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(d, "**", "*"), recursive=True)):
        if os.path.isfile(p):
            h.update(os.path.relpath(p, d).encode() + b"\0" + open(p, "rb").read())
    return h.hexdigest()[:12]


def switches_from(a):
    sw = {}
    for k, d in DEFAULTS.items():
        v = getattr(a, k, None)
        sw[k] = v if v not in (None, "") else os.environ.get("BENCH_" + k.upper(), d)
    sw["skill"] = os.path.abspath(os.path.expanduser(sw["skill"]))
    if sw["shell"] not in ("allow", "forbid") or sw["coord_guard"] not in ("on", "off") or sw["reset"] not in ("each", "none") \
            or sw["hub"] not in ("", "on", "off") or sw["lessons"] not in ("on", "off"):
        sys.exit("bad switch value (shell allow|forbid, coord-guard on|off, hub on|off, reset each|none, lessons on|off)")
    if not os.path.isfile(os.path.join(sw["skill"], "SKILL.md")):
        sys.exit(f"no SKILL.md in {sw['skill']}")
    return sw


def require_config(allow_main):
    if not os.environ.get("MCOMP_CONFIG"):
        sys.exit("Set MCOMP_CONFIG to the test VM's settings file: the benchmark resets that VM before each task.")
    vc = vm_config()
    if vc["vm"] == "metacomputer" and not allow_main:
        sys.exit("MCOMP_CONFIG names the main VM 'metacomputer'; use a test VM (or pass --allow-main-vm).")
    return vc


def cmd_run(a):
    vc = require_config(a.allow_main_vm)
    sw = switches_from(a)
    tasks = load_tasks(a.tasks)
    run_id = a.run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rdir = os.path.join(RESULTS, run_id)
    os.makedirs(rdir, exist_ok=True)
    claude_v = subprocess.run([trusted_claude(), "--version"], capture_output=True, text=True).stdout.strip()
    meta = {"run_id": run_id, "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "metacomputer": git_info(), "vm": vc["vm"], "baseline": vc["baseline"], "claude_code": claude_v,
            "switches": dict(sw, skill_hash=skill_hash(sw["skill"]), **lessons_record(sw),
                             coord_guard_in_bridge=os.path.exists(os.path.join(LAB, "lib", "mcp_hub.py"))
                             and "MCOMP_COORD_GUARD" in open(os.path.join(LAB, "lib", "mcp_hub.py")).read()),
            "tasks": [t["id"] for t in tasks],
            "vm_options": status().get("options")}   # the optional hardening (action log, SSH, driver mode)
    # Resuming: keep the first start time, and count what the run has spent already against the cap.
    old = json.load(open(os.path.join(rdir, "run.json"))) if os.path.exists(os.path.join(rdir, "run.json")) else None
    all_recs = report.load_all(rdir)
    if old:
        meta["started"] = old.get("started", meta["started"])
        meta["resumed"] = old.get("resumed", []) + [datetime.datetime.now().isoformat(timespec="seconds")]
        if old.get("switches") != meta["switches"] or (old.get("metacomputer") or {}).get("sha") != meta["metacomputer"]["sha"]:
            meta["resume_note"] = {"earlier_switches": old.get("switches"), "earlier_metacomputer": old.get("metacomputer")}
    json.dump(meta, open(os.path.join(rdir, "run.json"), "w"), indent=2)
    log(f"run {run_id}: {len(tasks)} task(s) on VM {vc['vm']}, model {sw['model']}, cap ${sw['cap']}")
    work, cfg, shim = prepare_workdir(run_id, sw)
    spent, cap = sum((r.get("metrics") or {}).get("cost_usd") or 0.0 for r in all_recs), float(sw["cap"])
    # done = the latest record of each task, unless that was an infrastructure failure (retried)
    done = {r["task"] for r in report.latest(all_recs) if not infra_reason(r)}
    try:
        for i, t in enumerate(tasks, 1):
            if t["id"] in done:
                log(f"[{i}/{len(tasks)}] {t['id']}: already in this run, skipped")
                continue
            budget = float(t.get("budget_usd") or sw["budget"])
            if spent + budget > cap:
                log(f"stopping: ${spent:.2f} spent, the next task could take ${budget:.2f}, cap ${cap:.2f}")
                meta["stopped"] = f"cap: ${spent:.2f} spent before {t['id']}"
                break
            others = busy_others()
            if others:
                log(f"stopping: another controller is using {vc['vm']}: {json.dumps(others)[:300]}")
                meta["stopped"] = "VM in use by another controller"
                break
            log(f"[{i}/{len(tasks)}] {t['id']}: {t.get('title', '')}")
            rec = run_task(t, sw, rdir, work, cfg, shim)
            spent += rec["metrics"].get("cost_usd") or 0.0
            with open(os.path.join(rdir, "results.jsonl"), "a") as f:
                f.write(json.dumps(rec) + "\n")
            mm = rec["metrics"]
            log(f"  {'PASS' if rec['check'].get('pass') else 'fail'} score={rec['check'].get('score')} "
                f"cost=${mm.get('cost_usd') or 0:.2f} turns={mm.get('turns')} {rec['session'].get('wall_s')} s "
                f"shots={mm.get('screenshots')} tree={mm.get('tree_reads')} shell={mm.get('shell_calls')}  (run total ${spent:.2f})")
            why = infra_reason(rec)
            if why and why.startswith("api"):
                # A usage or rate limit fails every later task in a second; stop, and retry
                # these tasks by running the same --run-id again later.
                log(f"stopping: {why}")
                meta["stopped"] = why
                break
    finally:
        meta["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
        meta["spent_usd"] = round(spent, 4)
        json.dump(meta, open(os.path.join(rdir, "run.json"), "w"), indent=2)
        shutil.rmtree(work, ignore_errors=True)
        if not a.keep_running:
            log(f"stopping {vc['vm']}")
            mc("stop", timeout=300)
    path = report.write_summary(rdir)
    log(f"summary: {path}")


infra_reason = report.infra_reason


def run_task(t, sw, rdir, work, cfg, shim):
    tdir = os.path.join(rdir, t["id"])
    shutil.rmtree(tdir, ignore_errors=True)
    os.makedirs(tdir)
    rec = {"task": t["id"], "title": t.get("title"), "route": t.get("route"), "apps": t.get("apps", []),
           "started": datetime.datetime.now().isoformat(timespec="seconds"), "infra_retries": 0}
    st = {}
    for attempt in range(2):
        if sw["reset"] == "each":
            ok, msg, st = reset_vm()
        else:
            st = status()
            ok, msg = st.get("ready"), ""
            if not ok:
                mc("start", timeout=600)
                st = status()
                ok = st.get("ready")
        setup = {"ok": False, "error": msg}
        if ok:
            rec["screen"] = set_screen(sw["screen"]) if sw["screen"] else None
            setup = setup_task(t)
        if setup.get("ok"):
            break
        rec["infra_retries"] += 1
        log(f"  infrastructure problem ({setup.get('error', '')[:160]}); {'retrying' if attempt == 0 else 'giving up'}")
    g = st.get("guest") or {}
    rec["env"] = {"driver_version": g.get("driver_version"), "resolution": g.get("resolution"),
                  "mcp_server": (st.get("mcp") or {}).get("server"), "calc": setup.get("calc")}
    rec["setup"] = setup
    if not setup.get("ok"):
        rec.update(session={}, check={"pass": False, "score": 0.0, "error": "setup failed"},
                   metrics=metrics.analyze([]), infra_failure=True)
        return rec
    events, times, sess = run_session(t, sw, work, cfg, shim, tdir)
    rec["session"] = sess
    for _ in range(20):              # let the session's bridge exit and drop its claim
        if not busy_others():
            break
        time.sleep(0.5)
    rec["check"] = check_task(t)
    rec["metrics"] = metrics.analyze(events, times)
    if rec["env"]["resolution"] and sw["screen"]:
        rec["env"]["resolution"] = rec.get("screen")
    why = infra_reason(rec)
    if why:
        rec["infra_failure"] = why
    json.dump(rec, open(os.path.join(tdir, "result.json"), "w"), indent=2)
    return rec


def cmd_remetric(a):
    """Recompute every task's metrics from its saved transcript (after changing metrics.py)."""
    rdir = os.path.join(RESULTS, a.run_id)
    recs = report.load_all(rdir)
    last = {r["task"]: i for i, r in enumerate(recs)}   # a task's folder holds its latest attempt only
    for i, rec in enumerate(recs):
        p = os.path.join(rdir, rec["task"], "transcript.jsonl.gz")
        if last[rec["task"]] != i or not os.path.exists(p):
            continue
        events, times = [], []
        for line in gzip.open(p, "rt"):
            ev = json.loads(line)
            times.append(ev.pop("t", 0.0))
            events.append(ev)
        rec["metrics"] = metrics.analyze(events, times)
        json.dump(rec, open(os.path.join(rdir, rec["task"], "result.json"), "w"), indent=2)
    with open(os.path.join(rdir, "results.jsonl"), "w") as f:
        for rec in recs:
            f.write(json.dumps(rec) + "\n")
    print(report.write_summary(rdir))


def cmd_check(a):
    require_config(a.allow_main_vm)
    t = load_tasks(a.task)[0]
    if a.setup:
        print(json.dumps(setup_task(t)))
    print(json.dumps(check_task(t), indent=2))


def cmd_list(a):
    for t in load_tasks():
        req = f"  needs {','.join(t['requires'])}" if t.get("requires") else ""
        print(f"{t['id']:<18} {t.get('route', ''):<7} {t.get('title', '')}{req}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("--tasks")
    r.add_argument("--run-id")
    for k in DEFAULTS:
        r.add_argument("--" + k.replace("_", "-"), dest=k)
    r.add_argument("--keep-running", action="store_true", help="leave the VM running afterwards")
    r.add_argument("--allow-main-vm", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("task")
    c.add_argument("--setup", action="store_true", help="run the task's setup first (no reset)")
    c.add_argument("--allow-main-vm", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("run_id")
    cp = sub.add_parser("compare")
    cp.add_argument("run_a")
    cp.add_argument("run_b")
    rm = sub.add_parser("remetric")
    rm.add_argument("run_id")
    a = ap.parse_args()
    if a.cmd == "run":
        cmd_run(a)
    elif a.cmd == "remetric":
        cmd_remetric(a)
    elif a.cmd == "check":
        cmd_check(a)
    elif a.cmd == "list":
        cmd_list(a)
    elif a.cmd == "report":
        print(open(report.write_summary(os.path.join(RESULTS, a.run_id))).read())
    elif a.cmd == "compare":
        print(open(report.write_compare(os.path.join(RESULTS, a.run_a), os.path.join(RESULTS, a.run_b))).read())


if __name__ == "__main__":
    main()
