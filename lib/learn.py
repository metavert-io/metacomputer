#!/usr/bin/env python3
"""`metacomputer learn`: the virtual computer learns from the user's own sessions (docs/learn.md).

  learn report [--since 7d] [--project PATH] [--transcripts DIR]... [--logs DIR]... [--no-bench]
               [--min N] [--json] [--draft-with-claude [--model M]]
                         mine the sessions that used the metacomputer tools; propose lessons
  learn review           go through the proposals (at your terminal; elsewhere it lists them)
  learn approve ID...    approve proposals (built-in ones from anywhere; drafted or edited text
                         only at your terminal)
  learn reject ID...     set proposals aside
  learn list [--json] [--instructions] [--proposals]
                         the approved lessons (and the exact text the hub adds)
  learn remove ID...     take approved lessons out
  learn edit ID          change a lesson's or a proposal's text (your terminal only)
  learn add              write a lesson of your own (your terminal only)
  learn bench [--config TEST_ENV] [--tasks a,b] [--budget USD] [--cap USD] [--run-id ID] [--yes]
                         run the benchmark without and with the approved lessons and compare
                         (a test VM; it spends your Claude usage and asks first)

Proposals are built only from structured data (lib/learn_mine.py): failure classes, counts, tool
names, app names from a fixed list, durations. Their wording comes from the built-in templates
below, or, with --draft-with-claude, from one headless Claude Code session that gets only that
structured summary. Approved lessons go in a private store outside the checkout
(lib/learn_store.py) and reach every later session through the hub's MCP instructions, as advice
only: no guard reads them. Nothing here sends anything off the Mac, except --draft-with-claude,
which uses your own Claude Code and says so.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

LIB = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(LIB)
sys.path.insert(0, LIB)
import learn_mine  # noqa: E402
import learn_store  # noqa: E402
import termsafe  # noqa: E402
try:
    import events  # noqa: E402  (the optional event stream; never let it stop a command)
except Exception:
    events = None

PY = sys.executable or "python3"
DRAFT_MAX = 3                      # lessons one --draft-with-claude run may propose
DRAFT_TEXT_MAX = 300
DRAFT_BUDGET_USD = "0.50"
DRAFT_TIMEOUT_S = 240
BENCH_EST_PER_TASK = 0.55          # USD per task and run, from the benchmark's recent runs (bench/README.md)
BENCH_MIN_PER_TASK = 1.7           # minutes per task, reset and setup included
# Words a drafted lesson may not use: it must not talk about secrets, or about switching off, getting
# around or overriding the user's controls. Defense in depth; a draft still needs the user at a terminal.
DRAFT_DENY = re.compile(r"passphrase|password|secret|token|api[ _-]?key|credential|vault|disable|turn(?:ing)? off|"
                        r"switch(?:ing)? off|bypass|work ?around|circumvent|override|ignore (?:the|all|any|previous|"
                        r"earlier|your)|MCOMP_|COORD_GUARD|DRIVER_MODE|MCP_HUB|--override|release|https?://|curl |"
                        r"\bsudo\b|\brm -", re.I)


def esc(s):
    return termsafe.escape_for_terminal(str(s))


def die(msg, code=1):
    print(f"metacomputer learn: {esc(msg)}", file=sys.stderr)
    sys.exit(code)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --- the built-in lessons -------------------------------------------------------------------------
# Each template: the pattern it answers, a short title and the rule. The rule's words are fixed here;
# only the evidence (counts, sessions) and, for app notes, an app name and a skill file from the fixed
# lists in learn_mine fill it in. Patterns come from a reading by hand of 30 recorded sessions.
TEMPLATES = {
    "no-frame": ("no_frame", "Screenshot before pixel input",
                 "Before a pixel click, drag or zoom, make that window's last read a screenshot at the size you'll aim "
                 "with (include_accessibility_tree:false is enough). A tree-only read, an observe without "
                 "include_screenshot:true, a role/name step earlier in the same batch or run_script's getState "
                 "cancels it."),
    "foreground": ("foreground_unavailable", "foreground_unavailable: desktop scope",
                   "On foreground_unavailable (no window is active), bring_to_front won't help: use desktop scope "
                   "(press_key or click with scope:\"desktop\", aimed from get_desktop_state) or a background element "
                   "action (a click by token or role and name, Return in a field)."),
    "focus-restore": ("focus_not_restored", "Input sent although focus wasn't restored",
                      "\"The input was sent, but the prior focus could not be restored\" means the input was delivered: "
                      "don't send it again; read the window to see what happened (a dialog probably opened or closed)."),
    "stale-window": ("stale_window", "Stale window ids",
                     "When a window target is stale or no longer running, the dialog closed or the app made a new "
                     "window: call list_windows and use the new window_id before the next read or action."),
    "stale-token": ("stale_token", "Tokens from the latest read only",
                    "Use element tokens only from the latest read of that window; any newer read of it replaces them."),
    "not-found": ("not_found", "Element not found: use the nearest list",
                  "On ERROR (find), pick the target from the nearest list in the error, or read the window with a "
                  "query, before trying again. Names match as substrings, so check which element a step really hit."),
    "check-text": ("check_failed", "Checks from text you've read",
                   "Write expect and wait_for checks from text you have already read (a title, a label, a status bar), "
                   "not from what you expect the app to say. When a check fails, read the actual text and correct the "
                   "check rather than dropping it."),
    "malformed": ("malformed", "Batch and argument shape",
                  "A run_steps step is {tool, args, expect?, wait_for?, timeout_ms?}: delivery_mode, pid, window_id "
                  "and delays go inside args or on the batch, and each expect or wait_for needs role, name or text. "
                  "A call rejected for its shape ran nothing: fix the shape once, then resend."),
    "menu-path": ("menu_path", "Menu path not found",
                  "When a menu path isn't found, read the menu bar before trying again: the app may be in another "
                  "mode (in Calc, a chart in edit mode has its own menus; click a cell to leave it)."),
    "menu-foreground": ("menu_foreground", "Menus that need the foreground",
                        "When invoke_menu says background delivery isn't available, resend it with "
                        "delivery_mode:\"foreground\" as the hint says."),
    "no-extents": ("no_extents", "No extents: pixels from one screenshot size",
                   "When an element has no usable on-screen extents, or a point falls outside the window, aim with "
                   "pixels from a fresh screenshot of that window, keep to one screenshot size, or use the keyboard."),
    "guard-controller": ("guard_controller", "Another controller: stop and ask",
                         "\"In use by another controller\" is the user's one-controller rule: stop and ask the user, or "
                         "with no one to ask, report it and end. Don't wait in sleep loops, and never reach the "
                         "computer another way (metacomputer call, a script, the shell)."),
    "guard-chrome": ("guard_chrome", "Chrome input",
                     "In Chrome the hub refuses set_value and run_script: type with browser_type in page mode, or a "
                     "foreground click plus type_text without a token in human mode. If Chrome stops taking keys, run "
                     "metacomputer input-reset."),
    "guard-coordinate": ("guard_coordinate", "Fresh screenshot after windows change",
                         "When the hub says the windows changed since your screenshot, take a fresh screenshot of that "
                         "window and aim from it. Don't move, resize or open windows between a screenshot and the "
                         "pixels you aim from it."),
    "guard-bounded": ("guard_bounded", "Bounded mode limits",
                      "\"Outside the capability manifest\" is the user's bounded-mode limit: tell them what was refused "
                      "and that they can allow it; don't try another route to the same effect."),
    "unverified": ("acted_without_check", "Check before building on unverified input",
                   "Input reported as sent (ok, unverifiable, did not settle, suspected_noop, any invoke_menu) isn't "
                   "proof: check the outcome with a query read, the title or the saved file before the next action "
                   "that depends on it. Never resend to make sure."),
    "repeat-class": ("repeat", "Apply a fix to every later batch",
                     "When an error comes back a second time in a session, apply its fix to every later batch, not "
                     "just to the next call. Errors that came back in your sessions: {classes}."),
    "route-churn": ("churn", "Two tries, then check the premise",
                    "After two failed tries, or two routes with no effect, stop changing routes: look once at the "
                    "target (a screenshot or a query read) and check the premise (the right window, focus, selection "
                    "or edit mode, a dialog already open or closed, a frozen app), then change mechanism one step at "
                    "a time."),
    "app-notes": ("time", "Read {app}'s notes first",
                  "Before working in {app}, read its notes in the metacomputer skill's {file}: it is one of the "
                  "biggest time sinks in your sessions on the virtual computer."),
}
CLASS_WORDS = {k: v for k, v in learn_mine.LABELS.items()}


def _window(rep):
    w = rep.get("window") or {}
    return f"{w.get('from')} to {w.get('to')}" if w.get("from") else rep.get("since")


def _evidence(rep, count, sessions, what, examples=(), classes=()):
    n = rep["sessions"]
    return {"summary": f"{count} {what} in {sessions} of {n} sessions" if sessions else f"{count} {what}",
            "count": count, "sessions": sessions, "of_sessions": n, "window": _window(rep),
            "examples": [learn_store.clean_text(x, 60) for x in list(examples)[:5]],
            "classes": [c for c in classes if c in CLASS_WORDS], "generated": rep.get("generated")}


def build_proposals(rep, min_count=2):
    """[(id, proposal)] from the report's structured fields and the templates. Only fixed words, counts, the
    fixed class labels, app names from learn_mine.APPS and skill file names go in."""
    out = []
    by_class = {f["class"]: f for f in rep["failures"]}

    def fill(s, fmt):
        for k, v in fmt.items():
            s = s.replace("{" + k + "}", str(v))
        return s

    def add(pid, evidence, **fmt):
        pattern, title, text = TEMPLATES[pid.split(":")[0]]
        out.append({"id": pid.replace(":", "-"), "title": learn_store.clean_title(fill(title, fmt)),
                    "text": learn_store.clean_text(fill(text, fmt)), "source": "built-in", "pattern": pattern,
                    "evidence": evidence})

    for pid, (pattern, _, _) in TEMPLATES.items():
        f = by_class.get(pattern)
        if not f:
            continue
        need = 1 if pattern.startswith("guard_") else min_count
        if f["count"] >= need:
            add(pid, _evidence(rep, f["count"], f["sessions"], "failed calls" if not pattern.startswith("guard_")
                               else "refusals", f["examples"], [pattern]))
    u = rep["unverified"]
    if u["acted_without_check"] >= max(3, min_count):
        add("unverified", _evidence(rep, u["acted_without_check"], 0,
                                    f"times the next call acted again after an unverified result (of {u['results']})"))
    r = rep["repeats"]
    if r["sessions_with_repeat"] >= 1 and r["by_class"]:
        names = [x["class"] for x in r["by_class"] if x["class"] in CLASS_WORDS][:4]
        if names:
            add("repeat-class", _evidence(rep, sum(x["sessions"] for x in r["by_class"]), r["sessions_with_repeat"],
                                          "repeats of one failure class", [e for x in r["by_class"] for e in x["examples"]],
                                          names),
                classes="; ".join(CLASS_WORDS[c] for c in names))
    c = rep["churn"]
    if c["episodes"] >= 1:
        ex = [f"{e['session']} #{e['from']}-#{e['to']}" for e in c["examples"]]
        add("route-churn", _evidence(rep, c["episodes"], c["sessions"], "runs of 3+ different routes after a failure",
                                     ex))
    t = rep["time"]
    for a in t["by_app"]:
        app = a["app"]
        if app not in learn_mine.APP_NOTES or not learn_mine.APP_NOTES[app]:
            continue
        if a["share"] >= 0.15 and a["minutes"] >= 3:
            add(f"app-notes:{re.sub(r'[^a-z0-9]+', '-', app.lower()).strip('-')}",
                _evidence(rep, a["minutes"], a["sessions"], f"minutes ({round(a['share'] * 100)}% of the time on the "
                          "virtual computer)"), app=app, file=learn_mine.APP_NOTES[app])
    # strongest evidence first: sessions affected, then how often; app notes last
    out.sort(key=lambda p: (p["pattern"] == "time", -(p["evidence"].get("sessions") or 0),
                            -(p["evidence"].get("count") or 0)))
    return out


# --- store operations ------------------------------------------------------------------------------
def load_lessons():
    data, problem = learn_store.read_json(learn_store.lessons_path(), {"lessons": []})
    if problem:
        die(f"the lessons store can't be used: {problem}")
    if not isinstance(data, dict) or not isinstance(data.get("lessons"), list):
        die(f"{learn_store.lessons_path()} has no list of lessons")
    return data


def load_proposals():
    data, problem = learn_store.read_json(learn_store.proposals_path(), {"pending": [], "rejected": []})
    if problem:
        die(f"the lessons store can't be used: {problem}")
    if not isinstance(data, dict):
        data = {}
    data.setdefault("pending", [])
    data.setdefault("rejected", [])
    data["pending"] = [p for p in data["pending"] if isinstance(p, dict) and learn_store.valid_id(p.get("id"))]
    data["rejected"] = [p for p in data["rejected"] if isinstance(p, dict) and learn_store.valid_id(p.get("id"))]
    return data


def save(lessons=None, proposals=None):
    learn_store.ensure_dir()
    if lessons is not None:
        learn_store.write_json(learn_store.lessons_path(), lessons)
    if proposals is not None:
        learn_store.write_json(learn_store.proposals_path(), proposals)


def emit(type_, lesson, active):
    """security.lesson_approved | lesson_removed | lesson_edited: the id, a short title and the source; never
    the lesson's text."""
    if events is None:
        return
    try:
        title = learn_store.clean_title(lesson.get("title") or lesson.get("id"), 60)
        verb = {"lesson_approved": "approved", "lesson_removed": "removed", "lesson_edited": "edited"}[type_]
        events.emit("security", type_, f"A lesson for the virtual computer's instructions was {verb}: {title}",
                    data={"id": lesson.get("id"), "title": title, "source": lesson.get("source"), "active": active},
                    origin="cli")
    except Exception:
        pass


def is_tty():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def require_terminal(what):
    """The vault's bar for user-only commands: standard input must be a terminal. Friction, not a lock:
    a program running as you can open a pseudo-terminal (SECURITY.md, "Lessons")."""
    if not is_tty():
        die(f"{what} needs your own terminal: run it yourself in Terminal (not through Claude, a script or a pipe). "
            "Free text goes into every later session's instructions, so only you write it.")


def builtin_text_ok(p):
    """True when a proposal's text is exactly what its template makes, with only fixed names filled in: a
    "built-in" proposal whose stored text was changed afterwards is treated as free text."""
    key = "app-notes" if str(p.get("id", "")).startswith("app-notes-") else p.get("id")
    if key not in TEMPLATES:
        return False
    tmpl = TEMPLATES[key][2]
    text = p.get("text")
    if key == "app-notes":
        return any(text == learn_store.clean_text(tmpl.replace("{app}", app).replace("{file}", f))
                   for app, f in learn_mine.APP_NOTES.items() if f)
    if key == "repeat-class":
        head = learn_store.clean_text(tmpl.split("{classes}")[0])
        if not (isinstance(text, str) and text.startswith(head) and text.endswith(".")):
            return False
        names = text[len(head):-1].strip().split("; ")
        return bool(names) and all(n in CLASS_WORDS.values() for n in names)
    return text == learn_store.clean_text(tmpl)


def needs_terminal(p):
    """Only built-in proposals (their template's fixed wording, filled with fixed names) may be approved without
    the user at a terminal."""
    return p.get("source") != "built-in" or not builtin_text_ok(p)


def show_proposal(p, i=None, n=None, out=sys.stdout):
    head = f"Proposal {i} of {n}: " if i else ""
    src = {"built-in": "built-in", "claude-draft": "drafted by Claude from counts only", "edited": "edited by you"}.get(
        p.get("source"), p.get("source"))
    print(f"  {head}{esc(p['id'])}  {esc(p.get('title', ''))}  ({esc(src)})", file=out)
    print(f"      {esc(p['text'])}", file=out)
    ev = p.get("evidence") or {}
    if ev.get("summary"):
        ex = f" (e.g. {', '.join(ev.get('examples') or [])})" if ev.get("examples") else ""
        print(f"      evidence: {esc(ev['summary'])}{esc(ex)}", file=out)
    if needs_terminal(p):
        print("      approving it needs your own terminal (its wording isn't the built-in text)", file=out)


def find(lst, pid):
    return next((x for x in lst if x.get("id") == pid), None)


def approve_one(pid, lessons, props, ask=True):
    if find(lessons["lessons"], pid):
        print(f"{esc(pid)}: already approved")
        return False
    p = find(props["pending"], pid) or find(props["rejected"], pid)
    if p is None:
        known = ", ".join(x["id"] for x in props["pending"]) or "none (run metacomputer learn report)"
        die(f"no proposal {pid!r}; pending: {known}")
    if needs_terminal(p):
        require_terminal(f"approving {pid} (its text was drafted or edited, not built in)")
        if ask:
            show_proposal(p)
            sys.stderr.write("Approve this text for every later session? [y/N] ")
            sys.stderr.flush()
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                print(f"{esc(pid)}: not approved")
                return False
    text = learn_store.clean_text(p.get("text"))
    if not text:
        die(f"{pid} has no text")
    lesson = {"id": pid, "title": learn_store.clean_title(p.get("title") or pid), "text": text,
              "source": p.get("source"), "pattern": p.get("pattern"), "approved_at": now_iso(),
              "approved_from": "terminal" if is_tty() else "no terminal (from a chat or a script)",
              "evidence": p.get("evidence") or {}}
    lessons["lessons"].append(lesson)
    props["pending"] = [x for x in props["pending"] if x.get("id") != pid]
    props["rejected"] = [x for x in props["rejected"] if x.get("id") != pid]
    print(f"{esc(pid)}: approved")
    return lesson


# --- commands ------------------------------------------------------------------------------------
def default_log_dirs():
    d = os.environ.get("MCOMP_LOG_DIR") or os.path.join(LAB, "logs")
    return [d]


def bench_results_dir():
    return os.environ.get("BENCH_RESULTS") or os.path.join(LAB, "results", "bench")


def print_report(rep, out=sys.stdout):
    p = lambda s="": print(s, file=out)  # noqa: E731
    src = rep["sources"]
    p(f"Sessions with the metacomputer tools: {rep['sessions']} ({_window(rep)}; since {esc(rep['since'])}"
      + (f"; project {esc(rep['project'])}" if rep.get("project") else "") + ")")
    p(f"  sources: {src['claude_code_sessions']} Claude Code sessions, {src['headless_transcripts']} headless transcripts, "
      f"{src['benchmark_sessions']} benchmark sessions ({src['scanned_files']} recent transcript files looked at, "
      f"{src['duplicates_skipped']} duplicate copies skipped)")
    if not rep["sessions"]:
        p("  nothing to learn from yet")
        return
    p()
    p(f"Failed driver calls: {rep['failed_calls']} of {rep['driver_calls']:,} ({rep['failed_share']:.1%})")
    for f in rep["failures"]:
        ex = f"   e.g. {', '.join(f['examples'][:3])}" if f["examples"] else ""
        p(f"  {f['count']:4d}  {f['share']:4.0%}  {esc(f['label']):<58} {f['sessions']:3d} sessions{esc(ex)}")
    g = {f["class"]: f["count"] for f in rep["failures"]}
    p(f"  hub guards: controller {g.get('guard_controller', 0)}, Chrome {g.get('guard_chrome', 0)}, "
      f"coordinate {g.get('guard_coordinate', 0)}, bounded {g.get('guard_bounded', 0)}")
    u = rep["unverified"]
    kinds = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(u["by_kind"].items(), key=lambda x: -x[1]))
    p(f"Unverified results: {u['results']}" + (f" ({kinds})" if kinds else "")
      + f"; the next call acted again without a check {u['acted_without_check']} times")
    r = rep["repeats"]
    p(f"Same failure class again within a session: {r['sessions_with_repeat']} of {r['of_sessions']} sessions")
    for x in r["by_class"][:6]:
        p(f"  {esc(x['label'])}: {x['sessions']} sessions (up to {x['max']}x)")
    c = rep["churn"]
    p(f"Routes changed without re-checking the premise: {c['episodes']} runs in {c['sessions']} sessions")
    for e in c["examples"][:5]:
        p(f"  {esc(e['session'])} #{e['from']}-#{e['to']} in {esc(e['app'])}: {esc(' -> '.join(e['routes']))}")
    t = rep["time"]
    p(f"Time on the virtual computer: {t['vm_minutes']} min (the model {t['model_share']:.0%}, tools the rest)")
    p("  " + ", ".join(f"{esc(a['app'])} {a['minutes']} min ({a['share']:.0%})" for a in t["by_app"][:8]))
    al, ev = rep["action_logs"], rep["events"]
    if al["files"]:
        refused = ", ".join(f"{esc(k)} {v}" for k, v in al["refused"].items()) or "none"
        p(f"Action logs: {al['files']} files, {al['calls']:,} calls, {al['errors']} errors; hub refusals: {refused}")
    if ev["files"]:
        top = ", ".join(f"{esc(k)} {v}" for k, v in list(ev["by_type"].items())[:6]) or "none"
        p(f"Event log: {ev['events']} events ({top})")


def cmd_report(a):
    since_cut, _ = learn_mine.parse_since(a.since)
    if a.project and not os.path.isdir(os.path.expanduser(a.project)):
        die(f"no folder {a.project}")
    rep = learn_mine.mine(os.environ, since=a.since, project=os.path.expanduser(a.project) if a.project else None,
                          extra_dirs=a.transcripts or [], log_dirs=default_log_dirs() + (a.logs or []),
                          bench_dirs=[] if a.no_bench else [bench_results_dir()])
    lessons = load_lessons()
    props = load_proposals()
    approved_ids = {x.get("id") for x in lessons["lessons"]}
    rejected_ids = {x.get("id") for x in props["rejected"]}
    fresh = build_proposals(rep, a.min)
    drafts = []
    if a.draft_with_claude:
        drafts = draft_with_claude(rep, fresh, a.model, quiet=a.json, min_count=a.min)
    new = [p for p in fresh + drafts if p["id"] not in approved_ids and p["id"] not in rejected_ids]
    for p in new:
        p["proposed_at"] = now_iso()
    with learn_store.Lock():
        props = load_proposals()
        props["pending"] = new
        props["generated"] = rep["generated"]
        save(proposals=props)
    already = [p["id"] for p in fresh + drafts if p["id"] in approved_ids]
    set_aside = [p["id"] for p in fresh + drafts if p["id"] in rejected_ids]
    if a.json:
        print(json.dumps({"report": rep, "proposals": new, "already_approved": already, "rejected_earlier": set_aside,
                          "store": learn_store.store_dir()}, indent=1))
        return
    print_report(rep)
    print()
    if new:
        print(f"Proposed lessons ({len(new)}). Approve with `metacomputer learn approve ID ...`, set aside with "
              "`reject ID`, or go through them with `metacomputer learn review`:")
        for p in new:
            show_proposal(p)
    else:
        print("No new lessons to propose.")
    if already:
        print(f"Already approved: {esc(', '.join(already))}")
    if set_aside:
        print(f"Rejected earlier (approve ID still takes them): {esc(', '.join(set_aside))}")
    print(f"Store: {esc(learn_store.store_dir())}")


def cmd_review(a):
    props = load_proposals()
    pending = list(props["pending"])
    if not pending:
        print("No proposals waiting. `metacomputer learn report` makes new ones.")
        return
    if not is_tty():
        print(f"{len(pending)} proposals waiting. Approve with `metacomputer learn approve ID ...` (built-in ones may be "
              "approved from a chat when the user says so; drafted or edited ones need the user's own terminal):")
        for p in pending:
            show_proposal(p)
        return
    for i, p in enumerate(pending, 1):
        print()
        show_proposal(p, i, len(pending))
        while True:
            sys.stderr.write("  [a]pprove, [e]dit then approve, [r]eject, [s]kip, [q]uit: ")
            sys.stderr.flush()
            ans = sys.stdin.readline().strip().lower()[:1]
            if ans in ("a", "e", "r", "s", "q", ""):
                break
        if ans in ("q", ""):
            break
        if ans == "s":
            continue
        with learn_store.Lock():
            lessons, props = load_lessons(), load_proposals()
            if ans == "r":
                q = find(props["pending"], p["id"])
                if q:
                    props["pending"].remove(q)
                    props["rejected"].append(dict(q, rejected_at=now_iso()))
                    save(proposals=props)
                print(f"  {esc(p['id'])}: rejected")
                continue
            if ans == "e":
                q = find(props["pending"], p["id"])
                if not q or not edit_text(q):
                    continue
            lesson = approve_one(p["id"], lessons, props, ask=False)
            if lesson:
                save(lessons, props)
                emit("lesson_approved", lesson, len(lessons["lessons"]))


def cmd_approve(a):
    with learn_store.Lock():
        lessons, props = load_lessons(), load_proposals()
        done = []
        for pid in a.ids:
            if not learn_store.valid_id(pid):
                die(f"not a lesson id: {pid!r}")
            lesson = approve_one(pid, lessons, props)
            if lesson:
                done.append(lesson)
        if done:
            save(lessons, props)
    for lesson in done:
        emit("lesson_approved", lesson, len(lessons["lessons"]))
    if done:
        s = learn_store.summary()
        print(f"Active in the MCP instructions of new sessions: {esc(learn_store.status_line(s))}")


def cmd_reject(a):
    with learn_store.Lock():
        props = load_proposals()
        for pid in a.ids:
            p = find(props["pending"], pid)
            if not p:
                die(f"no pending proposal {pid!r}")
            props["pending"].remove(p)
            props["rejected"].append(dict(p, rejected_at=now_iso()))
            print(f"{esc(pid)}: rejected")
        save(proposals=props)


def cmd_remove(a):
    with learn_store.Lock():
        lessons = load_lessons()
        gone = []
        for pid in a.ids:
            x = find(lessons["lessons"], pid)
            if not x:
                die(f"no approved lesson {pid!r}")
            lessons["lessons"].remove(x)
            gone.append(x)
            print(f"{esc(pid)}: removed")
        save(lessons)
    for x in gone:
        emit("lesson_removed", x, len(lessons["lessons"]))


def read_line(prompt, limit):
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if len(line) > limit * 4:
        die("that line is too long")
    return line.rstrip("\n")


def edit_text(item):
    """Ask for new text (and title) at the terminal; True when it changed. The caller holds the lock."""
    print(f"  current: {esc(item.get('text', ''))}")
    raw = read_line(f"  New text (one line, at most {learn_store.MAX_LESSON_CHARS} characters; Enter keeps it): ",
                    learn_store.MAX_LESSON_CHARS)
    title = read_line("  Title (Enter keeps it): ", 60)
    text = learn_store.clean_text(raw, 10 ** 6)
    if len(text) > learn_store.MAX_LESSON_CHARS:
        die(f"that text is {len(text)} characters; the limit is {learn_store.MAX_LESSON_CHARS}")
    changed = False
    if text and text != item.get("text"):
        item["text"] = text
        changed = True
    if title.strip():
        item["title"] = learn_store.clean_title(title)
        changed = True
    if changed:
        item["source"] = "edited" if item.get("source") != "you" else "you"
        item["edited_at"] = now_iso()
    return changed


def cmd_edit(a):
    require_terminal(f"learn edit {a.id}")
    with learn_store.Lock():
        lessons, props = load_lessons(), load_proposals()
        x = find(lessons["lessons"], a.id)
        if x:
            if edit_text(x):
                save(lessons)
                emit("lesson_edited", x, len(lessons["lessons"]))
                print(f"{esc(a.id)}: changed; new sessions get the new text")
            else:
                print("unchanged")
            return
        p = find(props["pending"], a.id) or find(props["rejected"], a.id)
        if not p:
            die(f"no lesson or proposal {a.id!r}")
        if edit_text(p):
            save(proposals=props)
            print(f"{esc(a.id)}: proposal changed; approve it with `metacomputer learn approve {esc(a.id)}` (here, at "
                  "your terminal)")
        else:
            print("unchanged")


def cmd_add(a):
    require_terminal("learn add")
    with learn_store.Lock():
        lessons = load_lessons()
        n = 1
        while find(lessons["lessons"], f"user-{n}"):
            n += 1
        item = {"id": f"user-{n}", "text": "", "title": ""}
        print("A lesson of your own: one line of advice for every later session on the virtual computer.")
        if not edit_text(item) or not item.get("text"):
            die("no text given")
        lesson = {"id": item["id"], "title": item.get("title") or item["id"], "text": item["text"], "source": "you",
                  "pattern": None, "approved_at": now_iso(), "approved_from": "terminal",
                  "evidence": {"summary": "written by you"}}
        lessons["lessons"].append(lesson)
        save(lessons)
    emit("lesson_approved", lesson, len(lessons["lessons"]))
    print(f"{esc(lesson['id'])}: added")


def cmd_list(a):
    s = learn_store.summary()
    lessons, problem = learn_store.approved()
    props = None if problem else load_proposals()
    if a.json:
        print(json.dumps({"summary": s, "lessons": lessons, "instructions": learn_store.instructions_text(),
                          "pending": (props or {}).get("pending", []), "rejected": (props or {}).get("rejected", [])},
                         indent=1))
        return
    if a.instructions:
        text = learn_store.instructions_text()
        print(esc(text) if text else "(nothing: no approved lessons reach the instructions)")
        return
    print(f"Lessons: {esc(learn_store.status_line(s))}   (store {esc(s['dir'])})")
    for x in lessons:
        ev = (x.get("evidence") or {}).get("summary") or ""
        print(f"  {esc(x['id'])}  {esc(x.get('title', ''))}   approved {esc(str(x.get('approved_at', ''))[:10])}, "
              f"{esc(x.get('source') or '')}" + (f"; {esc(ev)}" if ev else ""))
        print(f"      {esc(x['text'])}")
    if props and props["pending"]:
        print(f"{len(props['pending'])} proposals waiting (metacomputer learn review)")
        if a.proposals:
            for p in props["pending"]:
                show_proposal(p)
    print("They are advice in the MCP instructions only; no guard reads them, and they never override a refusal or "
          "your own instructions.")


# --- drafting with Claude ---------------------------------------------------------------------------
def draft_summary(rep, covered, min_count=2):
    """What --draft-with-claude sends: counts, fixed class keys, tool names and app names from the fixed list.
    No transcript text, labels, paths or examples."""
    covered_patterns = {p["pattern"] for p in covered}
    need = max(1, min_count)
    patterns = []
    for f in rep["failures"]:
        if f["class"] in covered_patterns or f["count"] < need:
            continue
        patterns.append({"pattern": f"errors-{f['class'].replace('_', '-')}", "failure_class": f["class"],
                         "meaning": learn_mine.LABELS.get(f["class"], f["class"]), "count": f["count"],
                         "sessions": f["sessions"]})
    for key, n in rep.get("failures_by_tool", {}).items():
        tool, _, cls = key.partition(":")
        if cls == "other" and n >= need and re.match(r"^[a-z][a-z0-9_]{0,47}$", tool):
            patterns.append({"pattern": f"other-errors-{tool.replace('_', '-')}", "tool": tool,
                             "meaning": "errors of this tool that match no known class", "count": n})
    for a in rep["time"]["by_app"]:
        if a["app"] in learn_mine.APP_NOTES and not learn_mine.APP_NOTES[a["app"]] and a["share"] >= 0.1 \
                and a["minutes"] >= 3:
            patterns.append({"pattern": f"time-{re.sub(r'[^a-z0-9]+', '-', a['app'].lower()).strip('-')}",
                             "app": a["app"], "meaning": "a large share of the time on the virtual computer",
                             "minutes": a["minutes"], "share": a["share"], "failed_calls": a["failed"]})
    return {"sessions": rep["sessions"], "driver_calls": rep["driver_calls"], "failed_calls": rep["failed_calls"],
            "unverified_results": rep["unverified"]["results"],
            "acted_without_check": rep["unverified"]["acted_without_check"],
            "sessions_with_a_repeated_failure_class": rep["repeats"]["sessions_with_repeat"],
            "route_change_runs": rep["churn"]["episodes"],
            "already_covered_by_built_in_lessons": sorted(covered_patterns),
            "patterns": patterns[:12]}


DRAFT_PROMPT = """You help a user of metacomputer, where Claude Code drives a disposable Ubuntu desktop VM through \
Cua Driver's MCP tools (get_window_state, run_steps, click, type_text, press_key, invoke_menu, list_windows, \
zoom and others). Below is a summary of the user's own past sessions: only counts, failure classes, tool names \
and app names. Draft at most {n} short lessons for future sessions, one for each entry of "patterns" that you can \
say something concrete and useful about; skip the others. A lesson is one or two plain sentences under {chars} \
characters: what to do or avoid when driving the desktop. Never suggest switching off, getting around or \
overriding a guard, a refusal, bounded mode, the coordinate guard, the secrets vault or the user's approvals, \
and never mention passwords, passphrases, keys, tokens, URLs or shell commands.

Answer with JSON only, in this shape:
{{"lessons": [{{"pattern": "<a pattern key from the summary>", "title": "<under 60 characters>", \
"text": "<the lesson>"}}]}}

Summary:
{summary}
"""


def trusted_claude():
    p = shutil.which("claude", path=os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                                                    if os.path.isabs(d)))
    if not p:
        die("--draft-with-claude needs Claude Code's `claude` on PATH")
    real = os.path.realpath(p)
    st = os.stat(real)
    if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
        die(f"refusing {real}: owned by another account or writable by others")
    return real


def parse_drafts(text, allowed):
    """The drafted lessons that pass every check, as proposals; and the ones dropped, with why."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    items = data.get("lessons") if isinstance(data, dict) else None
    out, dropped = [], []
    for x in (items if isinstance(items, list) else [])[:DRAFT_MAX * 2]:
        if not isinstance(x, dict):
            continue
        pat = x.get("pattern")
        text_ = learn_store.clean_text(x.get("text"), 10 ** 4)
        title = learn_store.clean_title(x.get("title") or "")
        if pat not in allowed:
            dropped.append((str(pat)[:40], "not a pattern from the summary"))
            continue
        if not text_ or len(text_) > DRAFT_TEXT_MAX:
            dropped.append((pat, f"empty or longer than {DRAFT_TEXT_MAX} characters"))
            continue
        bad = DRAFT_DENY.search(text_ + " " + title)
        if bad:
            dropped.append((pat, f"it mentions {bad.group(0)!r}"))
            continue
        pid = ("draft-" + re.sub(r"[^a-z0-9-]+", "-", pat.lower()))[:48].strip("-")
        if find(out, pid):
            continue
        out.append({"id": pid, "title": title or pid, "text": text_, "source": "claude-draft", "pattern": pat,
                    "evidence": {"summary": f"drafted by Claude from the counts for {pat}", "pattern": pat}})
        if len(out) >= DRAFT_MAX:
            break
    return out, dropped


def draft_with_claude(rep, covered, model=None, quiet=False, min_count=2):
    summary = draft_summary(rep, covered, min_count)
    say = (lambda s: print(s, file=sys.stderr)) if quiet else print
    if not summary["patterns"]:
        say("Drafting with Claude: every pattern found is covered by a built-in lesson; nothing to draft.")
        return []
    say(f"Drafting with Claude: this runs one headless Claude Code session (`claude -p`) on your own Claude account "
        f"and usage (at most ${DRAFT_BUDGET_USD}). It gets only counts, failure classes, tool names and app names "
        f"for {len(summary['patterns'])} patterns, never transcript text.")
    claude = trusted_claude()
    work = tempfile.mkdtemp(prefix="mcomp-learn-draft-")
    try:
        cfg = os.path.join(work, "mcp.json")
        with open(cfg, "w") as f:
            json.dump({"mcpServers": {}}, f)
        prompt = DRAFT_PROMPT.format(n=DRAFT_MAX, chars=DRAFT_TEXT_MAX, summary=json.dumps(summary, indent=1))
        args = [claude, "-p", prompt, "--output-format", "json", "--max-turns", "1", "--tools", "",
                "--strict-mcp-config", "--mcp-config", cfg, "--setting-sources", "project", "--no-session-persistence",
                "--max-budget-usd", DRAFT_BUDGET_USD]
        if model:
            args += ["--model", model]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            r = subprocess.run(args, cwd=work, env=env, capture_output=True, text=True, timeout=DRAFT_TIMEOUT_S,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            say("  Claude didn't answer in time; no drafts.")
            return []
        try:
            res = json.loads(r.stdout or "{}")
        except ValueError:
            res = {}
        cost = res.get("total_cost_usd") if isinstance(res, dict) else None
        drafts, dropped = parse_drafts(res.get("result") if isinstance(res, dict) else "", {p["pattern"] for p in summary["patterns"]})
        say(f"  {len(drafts)} drafted" + (f", {len(dropped)} dropped" if dropped else "")
            + (f"; cost ${cost:.2f}" if isinstance(cost, (int, float)) else ""))
        for pat, why in dropped:
            say(f"  dropped a draft for {esc(pat)}: {esc(why)}")
        return drafts
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- measuring: the benchmark without and with the lessons ------------------------------------------
def cmd_bench(a):
    s = learn_store.summary()
    if s["problem"]:
        die(f"the lessons can't be loaded: {s['problem']}")
    if not s["approved"]:
        die("no approved lessons to measure (metacomputer learn report, then approve some)")
    config = a.config or os.environ.get("MCOMP_CONFIG")
    if not config:
        die("learn bench needs a test VM: pass --config TEST_ENV, the settings file of a VM you don't mind being reset "
            "before every task (bench/README.md, \"Before you run it\"). It never uses your main VM.")
    config = os.path.abspath(os.path.expanduser(config))
    if not os.path.isfile(config):
        die(f"no settings file {config}")
    sys.path.insert(0, os.path.join(LAB, "bench"))
    import bench  # noqa: E402
    import report  # noqa: E402
    tasks = bench.load_tasks(a.tasks)
    n = len(tasks)
    budget = float(a.budget or 3)
    cap = float(a.cap or max(budget * n, 1))
    vm, main_vm = (vm_name_of(c) for c in (config, ""))
    if not vm or vm in ("metacomputer", main_vm):
        die(f"{config} names your main VM ({vm or 'unknown'}); learn bench needs a test VM (bench/README.md)")
    est, mins = 2 * n * BENCH_EST_PER_TASK, 2 * n * BENCH_MIN_PER_TASK
    print(f"learn bench runs the benchmark twice on the test VM {esc(vm)} ({esc(config)}): {n} tasks without your "
          f"{s['approved']} approved lessons, then the same {n} with them.")
    print(f"  That is {2 * n} headless Claude Code sessions on your own Claude account: about ${est:.0f} and "
          f"{mins:.0f} minutes (each run capped at ${cap:.2f}, each task at ${budget:.2f}).")
    print("  Each task resets that VM to its baseline first. The VM is stopped at the end.")
    if not a.yes:
        if not is_tty():
            die("it spends your Claude usage, so it asks first: run it at your terminal, or add --yes")
        sys.stderr.write("Start? [y/N] ")
        sys.stderr.flush()
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            die("cancelled")
    prefix = a.run_id or time.strftime("learn-%Y%m%d-%H%M")
    if not re.match(r"^[A-Za-z0-9._-]{1,60}$", prefix):
        die("--run-id: letters, digits, '.', '_' and '-'")
    env = dict(os.environ, MCOMP_CONFIG=config)
    runs = {}
    for variant, sw in (("without", "off"), ("with", "on")):
        rid = f"{prefix}-{variant}"
        cmd = [PY, os.path.join(LAB, "bench", "bench.py"), "run", "--lessons", sw, "--run-id", rid,
               "--budget", f"{budget:.2f}", "--cap", f"{cap:.2f}"]
        if a.tasks:
            cmd += ["--tasks", a.tasks]
        print(f"\n== {variant} lessons: bench/bench.py run --lessons {sw} --run-id {esc(rid)}", flush=True)
        r = subprocess.run(cmd, env=env)
        if r.returncode != 0:
            die(f"the {variant}-lessons run stopped (exit {r.returncode}); run the same command again to resume it")
        runs[variant] = os.path.join(bench_results_dir(), rid)
    path = report.write_compare(runs["without"], runs["with"])
    print_bench_comparison(report, runs, path)


def vm_name_of(config):
    """VM_NAME from a settings file (empty: this checkout's own config/local.env), the way scripts/lib.sh reads it."""
    env = {k: v for k, v in os.environ.items() if k != "MCOMP_CONFIG"}
    if config:
        env["MCOMP_CONFIG"] = config
    return subprocess.run(["bash", "-c", '. "$1/scripts/lib.sh" >/dev/null 2>&1; printf %s "$VM_NAME"', "_", LAB],
                          env=env, capture_output=True, text=True).stdout.strip()


def print_bench_comparison(report, runs, path):
    ma, xa = report.load(runs["without"])
    mb, xb = report.load(runs["with"])
    A = {r["task"]: report.row(r) for r in xa}
    B = {r["task"]: report.row(r) for r in xb}
    common = [k for k in A if k in B]
    ta, tb = report.totals([A[k] for k in common]), report.totals([B[k] for k in common])
    print(f"\nWithout vs with the approved lessons, over the {len(common)} tasks both runs have:")
    rows = [("passed", f"{ta['passed']}/{ta['n']}", f"{tb['passed']}/{tb['n']}", f"{tb['passed'] - ta['passed']:+d}"),
            ("invalid-argument calls", ta["invalid"], tb["invalid"], f"{tb['invalid'] - ta['invalid']:+d}"),
            ("refused calls", ta["refused"], tb["refused"], f"{tb['refused'] - ta['refused']:+d}"),
            ("other tool errors", ta["errors"], tb["errors"], f"{tb['errors'] - ta['errors']:+d}"),
            ("mean turns", f"{ta['turns_mean']:.1f}", f"{tb['turns_mean']:.1f}", f"{tb['turns_mean'] - ta['turns_mean']:+.1f}"),
            ("mean minutes", f"{ta['min_mean']:.2f}", f"{tb['min_mean']:.2f}", f"{tb['min_mean'] - ta['min_mean']:+.2f}"),
            ("screenshots", ta["shots"], tb["shots"], f"{tb['shots'] - ta['shots']:+d}"),
            ("cost", f"${ta['cost']:.2f}", f"${tb['cost']:.2f}", f"{tb['cost'] - ta['cost']:+.2f}")]
    print(f"  {'':24} {'without':>10} {'with':>10} {'change':>8}")
    for label, x, y, d in rows:
        print(f"  {label:24} {str(x):>10} {str(y):>10} {d:>8}")
    spent = (ma.get("spent_usd") or 0) + (mb.get("spent_usd") or 0)
    print(f"Spent on this comparison: ${spent:.2f}. One run of each is noisy: for a decision, repeat it and compare "
          "the totals.")
    print(f"Per task: {esc(path)}")


# --- main ------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="metacomputer learn", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="mine your sessions and propose lessons")
    r.add_argument("--since", default="7d")
    r.add_argument("--project")
    r.add_argument("--transcripts", action="append", help="another folder of saved transcripts (stream-json)")
    r.add_argument("--logs", action="append", help="another VM's LOG_DIR (action logs, events.jsonl)")
    r.add_argument("--no-bench", action="store_true", help="leave out benchmark results")
    r.add_argument("--min", type=int, default=2, help="how often a failure class must occur to propose a lesson")
    r.add_argument("--json", action="store_true")
    r.add_argument("--draft-with-claude", action="store_true")
    r.add_argument("--model")
    sub.add_parser("review")
    for name in ("approve", "reject", "remove"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+")
    e = sub.add_parser("edit")
    e.add_argument("id")
    sub.add_parser("add")
    li = sub.add_parser("list")
    li.add_argument("--json", action="store_true")
    li.add_argument("--instructions", action="store_true", help="print the exact text the hub adds")
    li.add_argument("--proposals", action="store_true")
    b = sub.add_parser("bench")
    b.add_argument("--config", help="the test VM's settings file (MCOMP_CONFIG)")
    b.add_argument("--tasks")
    b.add_argument("--budget")
    b.add_argument("--cap", help="each run's cap in USD (default: budget x tasks)")
    b.add_argument("--run-id")
    b.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        {"report": cmd_report, "review": cmd_review, "approve": cmd_approve, "reject": cmd_reject,
         "remove": cmd_remove, "edit": cmd_edit, "add": cmd_add, "list": cmd_list, "bench": cmd_bench}[a.cmd](a)
    except learn_store.NotPrivate as e:
        die(f"the lessons store can't be used: {e}")
    except (TimeoutError, ValueError) as e:
        die(str(e))
    except OSError as e:
        die(f"{e.strerror or e}" + (f": {e.filename}" if getattr(e, "filename", None) else ""))
    except KeyboardInterrupt:
        die("interrupted", 130)


if __name__ == "__main__":
    main()
