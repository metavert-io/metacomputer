"""Mining the user's own sessions with the metacomputer tools (`metacomputer learn report`, docs/learn.md).

It finds the Claude Code transcripts that called the metacomputer tools (either prefix), Codex's, Gemini CLI's,
Antigravity CLI's, OpenClaw's and Hermes Agent's sessions that did (the last two from their SQLite databases, lib/session_dbs.py), plus
action logs, event logs and benchmark results where present, and reports:
- failed driver calls by class: malformed arguments, each hub guard's refusal, element not found, a pixel
  action without a screenshot frame, and the other classes the driver's messages name;
- calls that returned `unverifiable` (or suspected_noop, did not settle), and how often the next call
  acted again without checking;
- the same failure class repeating within a session;
- time per app on the virtual computer;
- retries that changed approach on one app without re-checking the premise;
- sessions that signed in through page-mode Chrome (page tools plus `secrets fill|login` or a sign-in handoff).

What it keeps is structured only: tool names, failure and refusal classes (matched by fixed patterns,
never copied), counts, durations, app names matched against a fixed list, and labels made from the
session's own folder on this Mac. Text from the VM (window titles, page and document text, error
messages) is read only to classify it and is never kept, printed or passed on: a page that says
"Lesson: ..." can at most move a count. Tool names outside the driver's own list become unknown_tool.
Transcripts are read with bounds (lib/transcripts.py), and one that fails is counted, not fatal. Mining
sends nothing anywhere. Stdlib only.
"""
import collections
import json
import os
import re
import stat
import time

import session_dbs
import transcripts

# --- fixed vocabularies -----------------------------------------------------------------------------
READS = {"list_apps", "list_windows", "get_window_state", "get_desktop_state", "get_accessibility_tree",
         "get_screen_size", "get_cursor_position", "get_recording_state", "get_session", "list_sessions",
         "health_report", "check_permissions", "get_config", "get_agent_cursor_state", "get_guide",
         "verify_state", "zoom", "parse_visual_regions", "get_browser_state"}
NEUTRAL = {"start_recording", "stop_recording", "end_session", "set_config", "check_for_update",
           "install_extension", "clipboard_read"}
KEYS = {"type_text", "press_key", "hotkey"}
# Page-mode Chrome (driven through the DevTools route) and the CLI commands that mark a sign-in: a session with
# both signed in through page mode (the `human-signin` lesson). Only the fact is kept, never the command's text.
PAGE_TOOLS = {"browser_prepare", "browser_navigate", "browser_click", "browser_type", "browser_select",
              "browser_dialog", "browser_pointer"}
SIGNIN_CMD = re.compile(r"(?:^|[\s/;&|(])(?:metacomputer|mcomp)\s+(?:--vm\s+[A-Za-z0-9._-]+\s+)?"
                        r"(?:secrets\s+(?:fill|login)\b|handoff\b[^\n;&|]{0,200}?--why[ =](?:login|2fa|captcha)\b)")
ELEMENT_KEYS = ("element_token", "element_index", "role", "name", "target", "selector", "element", "ref", "label")
MODES = {"foreground", "background"}
SCOPES = {"desktop", "window", "screen"}

# Failure classes, most specific first. Each pattern is searched in the start of the result and in the
# failing step's line of a run_steps report; the text itself is never kept.
CLASSES = [
    ("guard_controller", "refused: another controller is using the computer",
     r"^The virtual computer is in use by another controller|couldn't record that this session is using the "
     r"virtual computer"),
    ("guard_coordinate", "refused: coordinate guard (windows changed since the screenshot)",
     r"^Refused by the metacomputer bridge \(coordinate guard\)|windows_changed_since_screenshot"),
    ("guard_chrome", "refused: Chrome guard",
     r"^Refused by the metacomputer bridge: (?:set_value|run_script)"),
    ("guard_bounded", "refused: bounded mode (outside the capability manifest)",
     r"outside the capability manifest|bounded session has ended"),
    ("malformed", "malformed arguments (rejected before anything ran)",
     r"rejected before running anything|unknown (?:field|argument|variant|parameter)|invalid arguments|"
     r"Additional propert|InputValidationError|-32602|[Mm]issing (?:the )?required|needs `role`|"
     r"failed to (?:parse|deserialize)"),
    ("no_frame", "pixel action without a screenshot frame", r"No screenshot frame for window|No screenshot of window"),
    ("foreground_unavailable", "foreground_unavailable (no active window)", r"foreground_unavailable"),
    ("focus_not_restored", "input sent, but focus not restored", r"prior focus could not be restored"),
    ("stale_window", "stale window id", r"is stale or no longer running"),
    ("stale_token", "stale element token", r"element_token is stale|stale element token|token is stale"),
    ("not_found", "element not found", r"ERROR \(find\)"),
    ("check_failed", "an expect or wait_for check failed", r"ERROR \((?:expect|wait_for)\)"),
    ("menu_path", "menu path not found", r"menu path segment \d+ was not found"),
    ("menu_foreground", "menu needs foreground delivery", r"Background delivery is not available"),
    ("no_extents", "no usable element extents, or a point outside the window",
     r"no usable on-screen extents|outside (?:its|the) \d+x\d+ window|outside the window"),
    ("not_installed", "app not installed", r"is not an executable on PATH"),
    ("vm_off", "computer off or connection dropped",
     r"^The virtual computer is off|connection to the virtual computer closed"),
]
CLASS_RE = [(k, re.compile(rx)) for k, _, rx in CLASSES]
LABELS = {k: label for k, label, _ in CLASSES}
LABELS["other"] = "other errors"
GUARDS = ("guard_controller", "guard_chrome", "guard_coordinate", "guard_bounded")
WARNINGS = [("suspected_noop", re.compile(r"suspected_noop")), ("did_not_settle", re.compile(r"did not settle")),
            ("unverifiable", re.compile(r"\bunverifiable\b"))]

# Apps, matched against the app names the driver reports (and a few fixed title endings). Only these
# canonical names are ever kept; any other app counts as "another app".
APPS = [  # canonical name, the skill file with its notes (or None), lowercase name fragments
    ("Firefox", "DESKTOP.md", ("firefox",)),
    ("Chrome", "BROWSER.md", ("google-chrome", "chrome", "chromium")),
    ("LibreOffice Calc", "DESKTOP.md", ("libreoffice-calc", "scalc")),
    ("LibreOffice Writer", "DESKTOP.md", ("libreoffice-writer", "swriter")),
    ("LibreOffice", "DESKTOP.md", ("soffice", "libreoffice")),
    ("Mousepad", "DESKTOP.md", ("mousepad",)),
    ("Thunar", "DESKTOP.md", ("thunar",)),
    ("Calculator", "DESKTOP.md", ("gnome-calculator",)),
    ("Terminal", None, ("xfce4-term",)),
    ("Inkscape", "EXTRA-APPS.md", ("inkscape",)),
    ("Audacity", "EXTRA-APPS.md", ("audacity",)),
    ("Kdenlive", "EXTRA-APPS.md", ("kdenlive", "kden")),
    ("GIMP", None, ("gimp",)),
    ("Ristretto", "EXTRA-APPS.md", ("ristretto",)),
    ("pygame app", "EXTRA-APPS.md", (".py", "pygame")),
    ("Desktop", None, ("xfdesktop", "xfce4-panel", "xfce4-pa", "xfce4-appfinder", "xfce4-session")),
]
APP_NOTES = {a: f for a, f, _ in APPS}
TITLE_ENDINGS = (("libreoffice calc", "LibreOffice Calc"), ("libreoffice writer", "LibreOffice Writer"),
                 ("mozilla firefox", "Firefox"), ("google chrome", "Chrome"), ("- inkscape", "Inkscape"))
OTHER_APP = "another app"
SHELL = "shell in the VM"
CLI_APP = "metacomputer CLI"
MAX_GAP_S = 600                   # a model or tool time above this is cut to it (a person away, a prompt)
CHURN_WINDOW = 10                 # driver calls after a failure in which route changes are counted
LABEL_SAFE = re.compile(r"[^A-Za-z0-9._/-]")


def match_app(name):
    """The canonical app for a driver-reported app name or a model-given `app`, or None."""
    n = str(name or "").strip().lower()
    if not n or len(n) > 200:
        return None
    for canon, _, frags in APPS:
        if any(f in n for f in frags):
            return canon
    return OTHER_APP


def title_app(title):
    t = str(title or "").strip().lower()[-60:]
    for end, canon in TITLE_ENDINGS:
        if t.endswith(end):
            return canon
    return None


STEP_ERROR = re.compile(r"ERROR \(([a-z_]{1,20})\):?\s*")


def classify(text):
    """The failure class of an error result: one of the CLASSES keys, or "other". A run_steps report is
    classified by its failing step's line ("N. click ERROR (kind): message")."""
    t = str(text or "")
    head = t[:600]
    m = STEP_ERROR.search(t)
    kind = m.group(1) if m else None
    part = t[m.end():m.end() + 600] if m else head
    for key, rx in CLASS_RE:                    # in order: a stale window behind a failed find is a stale window
        if (key == "not_found" and kind == "find") or (key == "check_failed" and kind in ("expect", "wait_for")):
            return key
        if rx.search(part) or rx.search(head):
            return key
    return "other"


def warning_of(text):
    t = str(text or "")[:2000]
    for key, rx in WARNINGS:
        if rx.search(t):
            return key
    return None


def label_for(path, cwd, source, agent=None):
    """A short label for a session from its folder on this Mac (two path parts), or its bench run/task (an OpenClaw or
    Hermes Agent session without a folder: the agent's name)."""
    if source == "bench":
        parts = path.split(os.sep)
        base = "/".join(parts[-3:-1])
    else:
        c = (cwd or "").rstrip("/").split("/")
        base = "/".join(x for x in c[-2:] if x) or (source if source in session_dbs.READERS
                                                    else os.path.basename(os.path.dirname(path)))
    base = LABEL_SAFE.sub("_", base)[-48:] or "session"
    return base + (" (subagent)" if agent else "")


# --- one call ---------------------------------------------------------------------------------------
def steps_of(args):
    out = []
    steps = (args or {}).get("steps") if isinstance(args, dict) else None
    for st in steps if isinstance(steps, list) else []:
        if not isinstance(st, dict):
            continue
        if "tool" in st:
            out.append((transcripts.known_tool(st.get("tool")), st.get("args") if isinstance(st.get("args"), dict)
                        else {}, st))
        else:
            for k, v in st.items():
                if k not in ("wait_for", "expect", "timeout_ms") and isinstance(v, dict):
                    out.append((transcripts.known_tool(k), v, st))
    return out


def is_action(tool):
    return bool(tool) and tool not in READS and tool not in NEUTRAL


def route(tool, args):
    """How a call acted, in fixed words: (tool, target kind, delivery mode, scope)."""
    args = args if isinstance(args, dict) else {}
    batch = {}
    if tool == "run_steps":
        batch = args
        acts = [(t, a) for t, a, _ in steps_of(args) if is_action(t)]
        if not acts:
            return ("run_steps", "", "", "")
        tool, args = acts[0]
    tool = transcripts.known_tool(tool)
    if tool == "run_script":
        kind = "script"
    elif any(args.get(k) not in (None, "") for k in ELEMENT_KEYS):
        kind = "element"
    elif any(k in args for k in ("x", "y", "coordinate", "from_x", "to_x", "path")):
        kind = "pixel"
    elif tool in KEYS:
        kind = "keys"
    else:
        kind = ""
    mode = args.get("delivery_mode") or batch.get("delivery_mode") or ""
    scope = args.get("scope") or args.get("coordinate_frame") or batch.get("scope") or ""
    return (tool, kind, mode if mode in MODES else ("other" if mode else ""),
            scope if scope in SCOPES else ("other" if scope else ""))


def route_words(r):
    tool, kind, mode, scope = r
    extra = [x for x in (kind, mode, f"{scope} scope" if scope else "") if x]
    return tool + (f" ({', '.join(extra)})" if extra else "")


def checks_inside(tool, args):
    """A run_steps batch that reads or checks its outcome (observe, expect, wait_for)."""
    if tool != "run_steps" or not isinstance(args, dict):
        return False
    if args.get("observe"):
        return True
    return any(isinstance(st, dict) and (st.get("expect") or st.get("wait_for")) for _, _, st in steps_of(args)) \
        or any(t in READS for t, _, _ in steps_of(args))


def target_of(tool, args):
    """(app name, pid, window id) a call names, from its own arguments."""
    args = args if isinstance(args, dict) else {}
    if tool == "run_steps":
        for key in ("app", "pid", "window_id"):
            if args.get(key) not in (None, ""):
                break
        else:
            for _, a, _ in steps_of(args):
                if any(a.get(k) not in (None, "") for k in ("app", "pid", "window_id")):
                    args = a
                    break
    app = args.get("app") if isinstance(args.get("app"), str) else None
    if tool == "launch_app" and not app:
        app = args.get("name") if isinstance(args.get("name"), str) else (args.get("bundle_id") if isinstance(
            args.get("bundle_id"), str) else None)
    pid = args.get("pid") if isinstance(args.get("pid"), int) else None
    wid = args.get("window_id") if isinstance(args.get("window_id"), int) else None
    return app, pid, wid


LW_LINE = re.compile(r"window_id=(\d+)\s+pid=(\d+)[^\n]*?app=([^\s\"]{1,80})")
JSON_APP = re.compile(r'"app_name"\s*:\s*"([^"]{1,80})"')
JSON_PID = re.compile(r'"pid"\s*:\s*(\d{1,9})\b')
JSON_WIN = re.compile(r'"window_id"\s*:\s*(\d{1,12})\b')
JSON_TITLE = re.compile(r'"title"\s*:\s*"([^"]{0,200})"')


class AppMap:
    """Which app each pid and window id belongs to, learned from list_windows, launch_app and read
    results. Only canonical names from APPS are stored."""

    def __init__(self):
        self.by_pid, self.by_win = {}, {}

    def learn(self, tool, args, text):
        if not text:
            return
        for wid, pid, app in LW_LINE.findall(text[:20000]):
            self.by_win[int(wid)] = self.by_pid[int(pid)] = match_app(app)
        found = list(JSON_APP.finditer(text[:20000]))
        if tool == "get_window_state":
            found = found[:1]                     # the window read; elements don't carry app_name
        _, apid, awid = target_of(tool, args)
        for k, a in enumerate(found):
            seg = text[a.start():found[k + 1].start() if k + 1 < len(found) else a.start() + 1200]
            canon = match_app(a.group(1))
            ti = JSON_TITLE.search(seg)
            if canon in ("LibreOffice", OTHER_APP) and ti:
                canon = title_app(ti.group(1)) or canon
            p, w = JSON_PID.search(seg), JSON_WIN.search(seg)
            pid = int(p.group(1)) if p else (apid if len(found) == 1 else None)
            wid = int(w.group(1)) if w else (awid if len(found) == 1 else None)
            if pid:
                self.by_pid[pid] = canon
            if wid:
                self.by_win[wid] = canon

    def app_for(self, tool, args):
        app, pid, wid = target_of(tool, args)
        if wid is not None and wid in self.by_win:
            return self.by_win[wid]
        if pid is not None and pid in self.by_pid:
            return self.by_pid[pid]
        if app:
            return match_app(app)
        return None


def clamp(x):
    return max(0.0, min(float(x), MAX_GAP_S)) if x is not None else 0.0


# --- one transcript --------------------------------------------------------------------------------
def mine_transcript(tr, source, label):
    calls = tr.calls
    s = {"label": label, "source": source, "session": (tr.session_id or "")[:8], "start": tr.first, "end": tr.last,
         "driver_calls": 0, "failed": 0, "classes": collections.Counter(), "warnings": collections.Counter(),
         "tool_classes": collections.Counter(),
         "examples": collections.defaultdict(list), "repeats": {}, "churn": [], "unverified": 0,
         "acted_without_check": 0, "page_tools": False, "signin": False,
         "time": collections.defaultdict(lambda: {"model_s": 0.0, "tool_s": 0.0, "calls": 0, "failed": 0}),
         "cost_usd": tr.final.get("total_cost_usd") if isinstance(tr.final.get("total_cost_usd"), (int, float)) else None}
    apps = AppMap()
    last_app = None
    per_ctx = collections.defaultdict(list)       # ctx -> [(index, call, app)]
    for i, c in enumerate(calls, 1):
        tool = c["tool"]
        if tool:
            s["driver_calls"] += 1
            s["page_tools"] = s["page_tools"] or tool in PAGE_TOOLS
            app = apps.app_for(tool, c["input"]) or last_app or "Desktop"
            if tool in ("launch_app", "list_windows", "get_window_state", "bring_to_front"):
                apps.learn(tool, c["input"], c["text"] if not c["error"] else "")
                app = apps.app_for(tool, c["input"]) or app
            if tool not in ("list_windows", "health_report", "list_apps", "get_desktop_state"):
                last_app = app
            per_ctx[c["ctx"]].append((i, c, app))
            bucket = s["time"][app]
            if c["error"]:
                cls = classify(c["text"])
                s["failed"] += 1
                s["classes"][cls] += 1
                s["tool_classes"][f"{tool}:{cls}"] += 1
                bucket["failed"] += 1
                if len(s["examples"][cls]) < 5:
                    s["examples"][cls].append(i)
            elif c["error"] is False and is_action(tool) and tool not in ("run_steps", "run_script"):
                w = warning_of(c["text"])
                if w:
                    s["warnings"][w] += 1
        elif c["name"] == "Bash":
            cmd = str(c["input"].get("command") or "")
            subs = transcripts.CLI_SUB.findall(cmd)
            if not subs:
                continue
            s["signin"] = s["signin"] or bool(SIGNIN_CMD.search(cmd[:4000]))
            app = SHELL if any(x in ("sh", "ssh") for x in subs) else CLI_APP
            per_ctx[c["ctx"]].append((i, c, app))
            bucket = s["time"][app]
        else:
            continue
        bucket["calls"] += 1
        bucket["model_s"] += clamp((c["t_use"] - c["t_prev"]) if c["t_use"] is not None and c["t_prev"] is not None else None)
        bucket["tool_s"] += clamp((c["t_result"] - c["t_use"]) if c["t_result"] is not None and c["t_use"] is not None else None)
    for ctx, seq in per_ctx.items():
        _repeats(s, seq)
        _unverified(s, seq)
        _churn(s, seq)
    return s


def _repeats(s, seq):
    seen = collections.Counter(classify(c["text"]) for _, c, _ in seq if c["tool"] and c["error"])
    for cls, n in seen.items():
        if n >= 2:
            s["repeats"][cls] = max(s["repeats"].get(cls, 0), n)


def _is_check(c):
    if c["tool"]:
        return c["tool"] in READS or checks_inside(c["tool"], c["input"])
    cmd = str(c["input"].get("command") or "")
    return any(x in ("sh", "ssh", "inspect") for x in transcripts.CLI_SUB.findall(cmd))


def _unverified(s, seq):
    for k, (i, c, app) in enumerate(seq):
        if not (c["tool"] and c["error"] is False and is_action(c["tool"])
                and c["tool"] not in ("run_steps", "run_script") and warning_of(c["text"])):
            continue
        s["unverified"] += 1
        nxt = seq[k + 1][1] if k + 1 < len(seq) else None
        if nxt is not None and nxt["tool"] and is_action(nxt["tool"]) and not _is_check(nxt):
            s["acted_without_check"] += 1


def _churn(s, seq):
    """After a failed action on an app: the different routes tried on that app before one worked cleanly,
    within CHURN_WINDOW calls. Flagged with 3+ routes and at least one switch made without reading the app."""
    k = 0
    while k < len(seq):
        i, c, app = seq[k]
        if not (c["tool"] and c["error"] and is_action(c["tool"])):
            k += 1
            continue
        tries = [(i, route(c["tool"], c["input"]))]
        looked = False
        blind = 0
        end = k
        for j in range(k + 1, min(len(seq), k + 1 + CHURN_WINDOW)):
            jj, d, dapp = seq[j]
            if dapp != app and d["tool"] and d["tool"] not in READS:
                break
            if d["tool"] in READS or _is_check(d):
                looked = True
                continue
            if not d["tool"] or not is_action(d["tool"]):
                continue
            if d["tool"] in ("run_steps", "run_script"):
                clean = d["error"] is False
            else:
                clean = d["error"] is False and not warning_of(d["text"])
            r = route(d["tool"], d["input"])
            if r != tries[-1][1] and not looked:
                blind += 1
            tries.append((jj, r))
            looked = False
            end = j
            if clean:
                break
        routes = []
        for _, r in tries:
            if r not in routes:
                routes.append(r)
        if len(routes) >= 3 and blind >= 1:
            s["churn"].append({"app": app, "from": tries[0][0], "to": tries[-1][0],
                               "routes": [route_words(r) for r in routes][:6], "blind_switches": blind})
            k = end + 1
        else:
            k += 1


# --- finding transcripts --------------------------------------------------------------------------
def _under(path, root):
    if not path or not root:
        return False
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r.rstrip("/") + "/")


def candidates(env, cutoff, project=None, extra_dirs=(), bench_dirs=(), projects=True):
    """([(path, source)], scanned, unreadable): every transcript file modified since `cutoff` that called a
    metacomputer tool. Files that don't are opened only to look for that marker; nothing else is read from them.
    A file that can't be read (corrupt gzip, ...) is counted, and the others go on."""
    out, scanned, bad = [], 0, 0
    roots = []
    if projects:
        roots += [(r, "project") for r in session_roots(env)]           # each agent's folders of session files
    roots += [(os.path.abspath(os.path.expanduser(d)), "stream") for d in extra_dirs]
    for root, source in roots:
        if not os.path.isdir(root):
            continue
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in ("tool-results", "tool-outputs", "checkpoints", "node_modules", ".git")]
            for f in files:
                if not (f.endswith(".jsonl") or f.endswith(".jsonl.gz")):
                    continue
                p = os.path.join(d, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime < cutoff:   # no links, FIFOs or devices
                    continue
                scanned += 1
                hit = _probe(p)
                if hit is None:
                    bad += 1
                elif hit:
                    out.append((p, source))
    # the agents that list their session files one by one (an Antigravity CLI conversation's compact transcript)
    for p in session_files(env) if projects else []:
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_mtime < cutoff:
            continue
        scanned += 1
        hit = _probe(p)
        if hit is None:
            bad += 1
        elif hit:
            out.append((p, "project"))
    for b in bench_dirs:
        b = os.path.abspath(os.path.expanduser(b))
        if not os.path.isdir(b):
            continue
        for run in sorted(os.listdir(b)):
            rdir = os.path.join(b, run)
            if not os.path.isdir(rdir) or run == "cache":
                continue
            for task in sorted(os.listdir(rdir)):
                p = os.path.join(rdir, task, "transcript.jsonl.gz")
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime < cutoff:
                    continue
                scanned += 1
                hit = _probe(p)
                if hit is None:
                    bad += 1
                elif hit:
                    out.append((p, "bench"))
    return out, scanned, bad


def _readers():
    """{name: its sessions.py} of the registered agents whose sessions are files, and their order."""
    from agentkit import registry
    return dict(transcripts._file_agents()), registry.AGENTS


def session_roots(env):
    """The folders of the agents' session files (each one's sessions.py roots), in the registry's order."""
    mods, order = _readers()
    out = []
    for n in order:
        if n in mods and hasattr(mods[n], "roots"):
            out += mods[n].roots(env)
    return out


def session_files(env):
    """The session files the agents list one by one (each one's sessions.py files), in the registry's order."""
    mods, order = _readers()
    out = []
    for n in order:
        if n in mods and hasattr(mods[n], "files"):
            out += mods[n].files(env)
    return out


def session_sources():
    """[(agent, its count's key in a report's sources, its name)] of every agent learn reads, in a report's order: the
    agents whose sessions are files, then those whose sessions are databases, each in the registry's order."""
    from agentkit import manifests, registry
    out = []
    for source in ("files", "databases"):
        for n in registry.AGENTS:
            ses = manifests.manifest(n).get("sessions")
            if ses and ses["source"] == source:
                out.append((n, ses["key"], manifests.label(n)))
    return out


def source_of(kind):
    """The agent (its folder's name) a Transcript kind is, from the manifests' sessions kinds: a project file
    (Transcript's own default kind) is the agent whose files those are."""
    from agentkit import manifests, registry
    src = {}
    for n in registry.AGENTS:
        for k in (manifests.manifest(n).get("sessions") or {}).get("kinds") or ():
            src[k] = n
    return src.get(kind, src["project"])


def _probe(path):
    """uses_tools(), with anything it raises counted as an unreadable file (None) instead of ending the report."""
    try:
        return transcripts.uses_tools(path)
    except Exception:
        return None


def mine_sessions(env, cutoff, project=None, extra_dirs=(), bench_dirs=(), projects=True):
    """Each session mined once (a headless copy wins over Claude Code's own file of the same session),
    filtered by time and project; a transcript is parsed, mined and dropped before the next one is read.
    Returns (mined sessions in time order, stats)."""
    found, scanned, unreadable = candidates(env, cutoff, project, extra_dirs, bench_dirs, projects)
    items, cut = [], 0
    for path, source in found:
        try:
            item = _mine_one(path, source, cutoff, project)
        except Exception:                       # one malformed transcript never stops the report
            unreadable += 1
            continue
        if item is None:
            continue
        cut += 1 if item.pop("cut") else 0
        items.append(item)
    # OpenClaw's and Hermes Agent's sessions, from their SQLite databases (lib/session_dbs.py): read, never written or
    # locked; what couldn't be read is in the notes, never fatal
    dbs, notes, with_tools = 0, [], len(found)
    if projects:
        try:
            trs, dbs, bad, nb = session_dbs.sessions(env, cutoff)
            unreadable, notes = unreadable + bad, nb.lines()
        except Exception as e:                  # never the report's end: said, by its type only
            trs, notes = [], [f"OpenClaw's and Hermes Agent's sessions weren't read ({type(e).__name__})"]
        for tr in trs:
            try:
                item = _mine_tr(tr, tr.kind, cutoff, project)
            except Exception:
                unreadable += 1
                continue
            if item is not None:
                with_tools += 1
                cut += 1 if item.pop("cut") else 0
                items.append(item)
    kept, stats = _dedupe(items, scanned, with_tools, unreadable, cut)
    stats.update(session_databases=dbs, notes=notes)
    return kept, stats


def _mine_one(path, source, cutoff, project):
    return _mine_tr(transcripts.parse(path, slim=True), source, cutoff, project)


def _mine_tr(tr, source, cutoff, project):
    path = tr.path
    if not tr.driver_calls:
        return None
    if source == "bench":
        src = "bench"
        # the benchmark's `t` counts seconds from the session's start: anchor it to the file's time
        try:
            end = os.path.getmtime(path)
        except OSError:
            end = time.time()
        shift = end - (tr.last or 0.0)
        tr.first, tr.last = (tr.first or 0.0) + shift, (tr.last or 0.0) + shift
    else:
        src = "headless" if tr.kind == "stream" else source_of(tr.kind)
    if project and src != "bench" and not _under(tr.cwd, project):
        return None
    if tr.last is not None and tr.last < cutoff:
        return None
    return {"m": mine_transcript(tr, src, label_for(tr.path, tr.cwd, src, tr.agent_id)), "src": src,
            "sid": tr.session_id, "agent": tr.agent_id, "path": tr.path, "n": len(tr.driver_calls),
            "first": tr.first or 0, "cut": bool(tr.info.get("truncated"))}


def _dedupe(items, scanned, found, unreadable, cut):
    stream_ids = {x["sid"] for x in items if x["src"] == "headless" and x["sid"]}
    best, dupes = {}, 0
    for x in items:
        if x["src"] == source_of("project") and x["sid"] in stream_ids:     # a headless copy's own session file
            dupes += 1
            continue
        key = x["path"] if x["src"] == "bench" or not x["sid"] else (x["sid"], x["agent"])
        if key in best:
            dupes += 1
            if x["n"] <= best[key]["n"]:
                continue
        best[key] = x
    kept = sorted(best.values(), key=lambda x: (x["first"], x["path"]))
    seen = collections.Counter(x["m"]["label"] for x in kept)
    for x in kept:
        if seen[x["m"]["label"]] > 1:
            x["m"]["label"] += ":" + LABEL_SAFE.sub("", x["sid"] or os.path.basename(x["path"]))[:6]
    return [x["m"] for x in kept], {"scanned_files": scanned, "with_tools": found, "duplicates_skipped": dupes,
                                    "unreadable": unreadable, "cut_short": cut}


# --- action logs, event logs ----------------------------------------------------------------------
def mine_action_logs(log_dirs, cutoff):
    out = {"files": 0, "calls": 0, "errors": 0, "refused": collections.Counter(), "error_classes": collections.Counter(),
           "tool_s": collections.Counter()}
    day0 = time.strftime("%Y-%m-%d", time.localtime(cutoff))
    for ld in log_dirs:
        base = os.path.join(ld, "actions")
        if not os.path.isdir(base):
            continue
        for day in sorted(os.listdir(base)):
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or day < day0:
                continue
            for f in sorted(os.listdir(os.path.join(base, day))):
                if not f.endswith(".jsonl"):
                    continue
                out["files"] += 1
                for rec in transcripts.read_events(os.path.join(base, day, f)):
                    t = rec.get("type")
                    tool = transcripts.known_tool(rec.get("tool"))
                    if t == "call":
                        out["calls"] += 1
                    elif t == "result":
                        r = rec.get("result") if isinstance(rec.get("result"), dict) else {}
                        if isinstance(rec.get("ms"), (int, float)):
                            out["tool_s"][tool] += max(0, min(rec["ms"], MAX_GAP_S * 1000)) / 1000.0
                        if r.get("error"):
                            out["errors"] += 1
                            out["error_classes"][classify(r.get("text")) if r.get("text") else "unknown"] += 1
                    elif t == "refused":
                        out["refused"][classify(rec.get("reason"))] += 1
    return out


def mine_event_logs(log_dirs, cutoff):
    out = {"files": 0, "events": 0, "by_type": collections.Counter()}
    for ld in log_dirs:
        p = os.path.join(ld, "events.jsonl")
        if not os.path.isfile(p):
            continue
        out["files"] += 1
        for ev in transcripts.read_events(p):
            when = transcripts._ts({"timestamp": ev.get("time")}) if isinstance(ev.get("time"), str) else None
            if when is not None and when < cutoff:
                continue
            fam, typ = str(ev.get("family") or ""), str(ev.get("type") or "")
            if re.match(r"^[a-z_]{1,20}$", fam) and re.match(r"^[a-z0-9_]{1,40}$", typ):
                out["events"] += 1
                out["by_type"][f"{fam}.{typ}"] += 1
    return out


# --- the whole report -----------------------------------------------------------------------------
def aggregate(mined, stats, action, events, since_label, cutoff, project):
    n = len(mined)
    failed = sum(m["failed"] for m in mined)
    classes = collections.Counter()
    sessions_of = collections.Counter()
    examples = collections.defaultdict(list)
    for m in mined:
        for cls, k in m["classes"].items():
            classes[cls] += k
            sessions_of[cls] += 1
            for idx in m["examples"][cls]:
                if len(examples[cls]) < 6:
                    examples[cls].append(f"{m['label']} #{idx}")
    failures = [{"class": cls, "label": LABELS.get(cls, cls), "count": k, "sessions": sessions_of[cls],
                 "share": round(k / failed, 3) if failed else 0, "examples": examples[cls]}
                for cls, k in classes.most_common()]
    rep_sessions = [m for m in mined if m["repeats"]]
    rep_by = collections.defaultdict(lambda: {"sessions": 0, "max": 0, "examples": []})
    for m in rep_sessions:
        for cls, k in m["repeats"].items():
            r = rep_by[cls]
            r["sessions"] += 1
            r["max"] = max(r["max"], k)
            if len(r["examples"]) < 6:
                r["examples"].append(f"{m['label']} ({k}x)")
    churn = [dict(e, session=m["label"]) for m in mined for e in m["churn"]]
    page_signin = [m["label"] for m in mined if m.get("page_tools") and m.get("signin")]
    time_by = collections.defaultdict(lambda: {"model_s": 0.0, "tool_s": 0.0, "calls": 0, "failed": 0, "sessions": 0})
    for m in mined:
        for app, b in m["time"].items():
            t = time_by[app]
            for k in ("model_s", "tool_s", "calls", "failed"):
                t[k] += b[k]
            t["sessions"] += 1
    total_s = sum(t["model_s"] + t["tool_s"] for t in time_by.values())
    model_s = sum(t["model_s"] for t in time_by.values())
    by_app = sorted(({"app": app, "minutes": round((t["model_s"] + t["tool_s"]) / 60, 1),
                      "share": round((t["model_s"] + t["tool_s"]) / total_s, 3) if total_s else 0,
                      "calls": t["calls"], "failed": t["failed"], "sessions": t["sessions"],
                      "notes": APP_NOTES.get(app)} for app, t in time_by.items()),
                    key=lambda x: -x["minutes"])
    src = collections.Counter(m["source"] for m in mined)
    starts = [m["start"] for m in mined if m["start"]]
    ends = [m["end"] for m in mined if m["end"]]
    driver = sum(m["driver_calls"] for m in mined)
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "since": since_label, "cutoff": time.strftime("%Y-%m-%d %H:%M", time.localtime(cutoff)),
        "project": project,
        "window": {"from": time.strftime("%Y-%m-%d", time.localtime(min(starts))) if starts else None,
                   "to": time.strftime("%Y-%m-%d", time.localtime(max(ends))) if ends else None},
        "sources": {**{key: src[name] for name, key, _label in session_sources()},
                    "headless_transcripts": src["headless"],
                    "benchmark_sessions": src["bench"], **stats},
        "sessions": n, "driver_calls": driver, "failed_calls": failed,
        "failed_share": round(failed / driver, 4) if driver else 0,
        "failures": failures,
        "failures_by_tool": dict(sum((m["tool_classes"] for m in mined), collections.Counter()).most_common(20)),
        "unverified": {"results": sum(m["unverified"] for m in mined),
                       "by_kind": dict(sum((m["warnings"] for m in mined), collections.Counter())),
                       "acted_without_check": sum(m["acted_without_check"] for m in mined)},
        "repeats": {"sessions_with_repeat": len(rep_sessions), "of_sessions": n,
                    "by_class": sorted(({"class": c, "label": LABELS.get(c, c), **v} for c, v in rep_by.items()),
                                       key=lambda x: (-x["sessions"], -x["max"]))},
        "churn": {"episodes": len(churn), "sessions": len({e["session"] for e in churn}), "examples": churn[:8]},
        "page_signin": {"sessions": len(page_signin), "examples": page_signin[:6]},
        "time": {"vm_minutes": round(total_s / 60, 1), "model_share": round(model_s / total_s, 3) if total_s else 0,
                 "by_app": by_app},
        "action_logs": {"files": action["files"], "calls": action["calls"], "errors": action["errors"],
                        "error_classes": dict(action["error_classes"].most_common()),
                        "refused": dict(action["refused"].most_common()),
                        "slowest_tools_s": {k: round(v, 1) for k, v in action["tool_s"].most_common(6)}},
        "events": {"files": events["files"], "events": events["events"],
                   "by_type": dict(events["by_type"].most_common(10))},
        "per_session": [{"label": m["label"], "source": m["source"], "session": m["session"],
                         "date": time.strftime("%Y-%m-%d", time.localtime(m["start"])) if m["start"] else None,
                         "driver_calls": m["driver_calls"], "failed": m["failed"],
                         "classes": dict(m["classes"].most_common(4)), "repeats": m["repeats"],
                         "churn": len(m["churn"]), "acted_without_check": m["acted_without_check"],
                         "minutes": round(sum(b["model_s"] + b["tool_s"] for b in m["time"].values()) / 60, 1),
                         "cost_usd": m["cost_usd"]} for m in mined],
    }


def parse_since(s):
    """'7d', '12h', '30m', '2w' or a date YYYY-MM-DD -> (cutoff epoch seconds, label)."""
    s = str(s or "7d").strip()
    m = re.match(r"^(\d+)\s*([mhdw])$", s)
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return time.time() - int(m.group(1)) * mult, s
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d")), s
    except ValueError:
        raise ValueError(f"--since takes 7d, 12h, 2w or a date YYYY-MM-DD (got {s!r})")


def mine(env, since="7d", project=None, extra_dirs=(), log_dirs=(), bench_dirs=(), projects=True):
    cutoff, label = parse_since(since)
    mined, stats = mine_sessions(env, cutoff, project, extra_dirs, bench_dirs, projects)
    action = mine_action_logs(log_dirs, cutoff)
    events = mine_event_logs(log_dirs, cutoff)
    return aggregate(mined, stats, action, events, label, cutoff, os.path.realpath(project) if project else None)
