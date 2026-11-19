#!/usr/bin/env python3
"""The hub's hidden-text marks against a running test VM, with real driver answers (SECURITY.md "Hidden page text"):

    MCOMP_CONFIG=path/to/test-vm.env python3 tests/test_hidden_text_vm.py

tests/fixtures/site/hidden.html (the prompt-injection finding's page: a white 1-px paragraph, a block at
left:-10000px, an aria-hidden block, and the other usual ways of hiding text, beside visible text) is served in the
VM and read through the hub:
1. Firefox (launch_app), the default markdown read: the hidden rows and the instruction-like rows are marked, the
   visible ones aren't; read with HIDDEN_TEXT=off and on (a hub each), the answers are the same once the marks are
   taken out (also for a query read and a cut-short read); scrolled to the end, a run_steps observation still marks
   the far-above block and leaves text you scrolled past alone; `page get_text` marks the wording.
2. The event: one security.hidden_text in an events folder of this test's own (never your sinks), with the host and
   counts, and none of the page's words.
3. Chrome page mode (get_browser_state semantic_v2) and Chrome launched with launch_app (its accessibility tree).
A test VM only: it starts a web server on 127.0.0.1:8000 in the VM, opens and closes Firefox and Chrome, and stops
and starts the VM's hub. It never sends an event to your sinks.
"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import hidden_text  # noqa: E402
import mcomp_sdk  # noqa: E402
import mcp_hub  # noqa: E402

MC = os.path.join(LAB, "bin", "metacomputer")
URL = "http://127.0.0.1:8000/hidden.html"
TAG_RE = re.compile(r"  \u27e6hub #\d+: [^\u27e7]*\u27e7")
PAGE_WORDS = ("ALPHA", "BRAVO", "WHITE1PX", "assistants", "tar up", ".ssh", "claude.json", "collector", "Ignore",
              "previous instructions", "do not tell", "Garden", "MIKE", "KILO", "summarize")
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:500]})" if detail and not cond else ""), flush=True)


def marked(res):
    """Block word -> its item: from the structured field, or (a result with no structured part, such as `page`'s)
    from the note's lines, which name the same signals and kinds in words."""
    field = (res.get("structuredContent") or {}).get(hidden_text.FIELD) or {}
    items = field.get("items")
    if items is None:
        note = str(((res.get("content") or [{}])[0]).get("text", ""))
        items = []
        if note.startswith(hidden_text.NOTE_PREFIX):
            for ln in note.split("\n"):
                q = re.match(r'#\d+ (.*): "(.*)"$', ln)
                if q:
                    items.append({"excerpt": q.group(2),
                                  "hidden": [s for s, d in hidden_text.SIGNALS.items() if d in q.group(1)],
                                  "instruction_like": [k for k, d in hidden_text.KINDS.items() if d in q.group(1)]})
    out = {}
    for it in items:
        m = re.search(r"([A-Z]+[0-9]?)-[A-Z0-9]", it["excerpt"])
        if m:
            out[m.group(1)] = it
    return out


def without_marks(res):
    res = copy.deepcopy(res)
    content = res.get("content") or []
    if content and str(content[0].get("text", "")).startswith(hidden_text.NOTE_PREFIX):
        content.pop(0)
    for c in content:
        if isinstance(c.get("text"), str):
            c["text"] = TAG_RE.sub("", c["text"])
    sc = res.get("structuredContent") or {}
    sc.pop(hidden_text.FIELD, None)
    if isinstance(sc.get("tree_markdown"), str):
        sc["tree_markdown"] = TAG_RE.sub("", sc["tree_markdown"])
    return res


def normalized(res):
    """Snapshot ids, walk times and the driver's cold-start budget differ between two reads of one page."""
    res = copy.deepcopy(res)
    sc = res.get("structuredContent") or {}
    s = json.dumps(res, ensure_ascii=False)
    if sc.get("snapshot_id"):
        s = s.replace(sc["snapshot_id"], "SID")
    s = re.sub(r"walk_ms=\d+", "walk_ms=N", s)
    out = json.loads(s)
    for k in ("invalidated_snapshot_ids", "timeout_ms"):
        out.get("structuredContent", {}).pop(k, None)
    out["content"] = [c for c in out["content"] if not str(c.get("text", "")).startswith("Invalidated snapshots")]
    return out


def main():
    cfg = os.environ.get("MCOMP_CONFIG")
    if not cfg:
        print("set MCOMP_CONFIG to a test VM's settings file (this test starts a web server and apps in that VM)")
        return 2
    td = tempfile.mkdtemp(prefix="mchide-")
    base = dict(os.environ, MCOMP_EVENTS_DIR=os.path.join(td, "events"), MCOMP_EVENTS_SPOOL=os.path.join(td, "spool"),
                MCOMP_LOG_DIR=os.path.join(td, "logs"))
    for k in ("MCOMP_DIRECT_BRIDGE", "MCOMP_HUB_PID", "MCOMP_HIDDEN_TEXT"):
        base.pop(k, None)
    env_on = env_off = None
    try:
        os.environ.update({k: base[k] for k in ("MCOMP_EVENTS_DIR", "MCOMP_EVENTS_SPOOL", "MCOMP_LOG_DIR")})
        env_on = mcomp_sdk.resolve_env(cfg)
        env_on.update(MCOMP_HIDDEN_TEXT="on", MCOMP_EVENTS_DIR=base["MCOMP_EVENTS_DIR"],
                      MCOMP_EVENTS_SPOOL=base["MCOMP_EVENTS_SPOOL"], MCOMP_LOG_DIR=base["MCOMP_LOG_DIR"])
        env_off = dict(env_on, MCOMP_HIDDEN_TEXT="off")
        run(env_on, env_off, base, td)
    finally:
        for e in (env_on, env_off):
            if e:
                try:
                    mcp_hub.stop_hub(e)
                except Exception:
                    pass
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if results and all(results) else 1


def run(env_on, env_off, base, td):
    def mc(*args, timeout=180, stdin=None):
        return subprocess.run([MC, *args], env=env_on, capture_output=True, text=True, timeout=timeout,
                              input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)

    st = mc("status")
    check("the test VM is running and ready", st.returncode == 0 and "READY" in st.stdout, st.stdout[-300:])
    # (scripts go on stdin: `pkill -f` would match a `sh -c` command line naming the same words)
    mc("sh", "--override", "-", stdin="pkill -f 'http.server 8000'; pkill -f firefox; pkill -f google-chrome; "
                                      "rm -rf ~/metacomputer-runs/hidden-test; mkdir -p ~/metacomputer-runs/hidden-test; true\n")
    r = mc("put", os.path.join(LAB, "tests", "fixtures", "site"), "--to", "~/metacomputer-runs/hidden-test")
    r2 = mc("sh", "--override", "-", stdin=(
        "cd ~/metacomputer-runs/hidden-test/site && "
        "(setsid nohup python3 -m http.server 8000 --bind 127.0.0.1 >/tmp/hidden-http.log 2>&1 &)\n"
        "sleep 1; curl -s -o /dev/null -w '%{http_code}' " + URL + "\n"))
    check("the test page is served in the VM", r.returncode == 0 and r2.stdout.strip().endswith("200"),
          (r.stderr[-200:], r2.stdout[-200:], r2.stderr[-200:]))

    # --- 1. Firefox, markdown reads with the marks off and on -------------------------------------------------------
    mcp_hub.stop_hub(env_on)
    h = mcomp_sdk.Hub(env=env_off, timeout=120, session="hidden-off").connect()
    ff = h.call("launch_app", {"name": "firefox", "additional_arguments": [URL]}).structured
    pid = ff.get("pid")
    win = None
    for _ in range(60):
        ws = h.call("list_windows", {"pid": pid}).structured.get("windows") or []
        win = next((w for w in ws if "Garden Club" in (w.get("title") or "")), None)
        if win:
            break
        time.sleep(0.5)
    check("Firefox shows the test page", bool(win), ff)
    if not win:
        return
    wid = win["window_id"]
    # Firefox reports its document row as disabled for a while after loading: wait until two reads agree.
    last = None
    for _ in range(20):
        cur = normalized(h.call("get_window_state", {"pid": pid, "window_id": wid, "include_screenshot": False}).raw)
        if last is not None and cur["structuredContent"].get("tree_markdown") == last["structuredContent"].get(
                "tree_markdown") and "enabled=false actions=[]" not in cur["structuredContent"].get("tree_markdown", ""):
            break
        last = cur
        time.sleep(1)
    reads = {"default": {}, "query": {"query": "ALPHA|OSCAR"}, "cut short": {"max_elements": 5}}
    off = {k: h.call("get_window_state", dict(pid=pid, window_id=wid, include_screenshot=False, **a)).raw
           for k, a in reads.items()}
    h.close()
    check("HIDDEN_TEXT=off: no marks", not any(r["content"][0]["text"].startswith(hidden_text.NOTE_PREFIX)
                                                for r in off.values()))
    mcp_hub.stop_hub(env_off)
    h = mcomp_sdk.Hub(env=env_on, timeout=120, session="hidden-on").connect()
    on = {k: h.call("get_window_state", dict(pid=pid, window_id=wid, include_screenshot=False, **a)).raw
          for k, a in reads.items()}
    res = on["default"]
    m = marked(res)
    note = res["content"][0]["text"]
    check("Firefox: the note comes first and calls it a possible prompt injection",
          note.startswith(hidden_text.NOTE_PREFIX) and "possible prompt injection" in note, note[:300])
    sc = res.get("structuredContent") or {}
    check("Firefox: the structured result (what Claude Code shows the model) leads with the field and its note",
          next(iter(sc), None) == hidden_text.FIELD
          and str(sc[hidden_text.FIELD].get("note", "")).startswith(hidden_text.NOTE_PREFIX), list(sc)[:3])
    check("Firefox: the white 1-px paragraph is marked too small to read and instruction-like",
          "tiny" in m.get("ALPHA", {}).get("hidden", []) and {"addresses_ai", "secrecy", "exfil"} <=
          set(m["ALPHA"]["instruction_like"]), m.get("ALPHA"))
    check("Firefox: the off-screen div (left:-10000px) is marked off the page and instruction-like",
          "off_left" in m.get("BRAVO", {}).get("hidden", []) and m["BRAVO"]["instruction_like"], m.get("BRAVO"))
    check("Firefox: far above, transformed away, zero-height and clipped blocks and 2-px type are marked",
          "off_above" in m.get("MIKE", {}).get("hidden", []) and "tiny" in m.get("JULIET", {}).get("hidden", [])
          and all("no_size" in m.get(k, {}).get("hidden", []) for k in ("NOVEMBER", "INDIA", "HOTEL")), sorted(m))
    check("Firefox: visible text, text below the fold and the (undetectable) colour and opacity blocks aren't marked; "
          "visible text aimed at AI agents is marked instruction-like only",
          not ({"VISIBLE", "OSCAR", "GOLF", "GOLF2", "GOLF3", "FOXTROT"} & set(m))
          and m.get("KILO", {}).get("hidden") == [] and m["KILO"]["instruction_like"], sorted(m))
    blob = json.dumps(res)
    check("Firefox: the aria-hidden block (and display:none, visibility:hidden) never reaches the tree",
          not any(w in blob for w in ("CHARLIE", "DELTA", "ECHO")))
    rows = [ln for ln in res["content"][1]["text"].split("\n") if "\u27e6hub #" in ln]
    check("Firefox: the marked rows end in their tags", len(rows) >= 8 and all(ln.endswith("\u27e7") for ln in rows),
          len(rows))
    for k in reads:
        a, b = normalized(off[k]), normalized(without_marks(on[k]))
        check(f"Firefox, {k} read: with the marks taken out, the same answer as with HIDDEN_TEXT=off", a == b,
              [x for x in a["structuredContent"] if a["structuredContent"].get(x) != b["structuredContent"].get(x)]
              or (a["content"][0]["text"][:300], b["content"][0]["text"][:300]))

    ev = []
    for _ in range(20):
        try:
            with open(os.path.join(base["MCOMP_LOG_DIR"], "events.jsonl")) as f:
                ev = [json.loads(x) for x in f if '"hidden_text"' in x]
        except OSError:
            ev = []
        if ev:
            break
        time.sleep(0.5)
    blob = json.dumps(ev)
    check("one security.hidden_text event (this test's own events folder): warn, host 127.0.0.1, counts",
          len(ev) == 1 and ev[0]["severity"] == "warn" and ev[0]["data"].get("host") == "127.0.0.1"
          and ev[0]["data"].get("hidden", 0) >= 7 and ev[0]["data"].get("instruction_like", 0) >= 3, ev)
    check("the event carries none of the page's words", not any(w.lower() in blob.lower() for w in PAGE_WORDS),
          [w for w in PAGE_WORDS if w.lower() in blob.lower()])

    r = h.call("run_steps", {"pid": pid, "window_id": wid, "delivery_mode": "foreground",
                             "steps": [{"tool": "press_key", "args": {"key": "End"}}],
                             "observe": {"include_screenshot": False, "since": None}}).raw
    m = marked(r)
    state = (r.get("structuredContent") or {}).get("observation", {}).get("state") or {}
    check("scrolled to the end (run_steps): the far-above block is still marked, text scrolled past isn't, and the "
          "observation comes back without the element list", "off_above" in m.get("MIKE", {}).get("hidden", [])
          and not ({"VISIBLE", "OSCAR"} & set(m)) and "elements" not in state, (sorted(m), r["content"][0]["text"][:300]))
    r = h.call("page", {"action": "get_text", "pid": pid, "window_id": wid}).raw
    check("page get_text (no positions): the wording marks the instruction-like blocks",
          r["content"][0]["text"].startswith(hidden_text.NOTE_PREFIX) and {"ALPHA", "BRAVO", "KILO"} <= set(marked(r)),
          sorted(marked(r)))
    h.call("kill_app", {"pid": pid})

    # --- 3. Chrome: page mode, then launch_app ------------------------------------------------------------------
    sess = "hidden-chrome"
    p = h.call("browser_prepare", {"session": sess, "allow_launch": True, "profile": {"mode": "isolated_new"}}).structured
    cpid = p.get("prepared_pid")
    cw = None
    for _ in range(40):
        ws = h.call("list_windows", {"pid": cpid, "session": sess}).structured.get("windows") or [] if cpid else []
        if ws:
            cw = ws[0]
            break
        time.sleep(0.25)
    b = h.call("get_browser_state", {"pid": cpid, "window_id": (cw or {}).get("window_id"), "session": sess}).structured
    tgt = {"target_id": b.get("target_id"), "tab_id": ((b.get("tabs") or [{}])[0]).get("tab_id"), "session": sess}
    h.call("browser_navigate", dict(tgt, url=URL))
    time.sleep(1.5)
    r = h.call("get_browser_state", dict(tgt, snapshot_format="semantic_v2")).raw
    m = marked(r)
    check("Chrome page snapshot: the off-screen block between visible text is marked (off_flow), the white 1-px "
          "paragraph by its wording, text below the fold not", m.get("BRAVO", {}).get("hidden") == ["off_flow"]
          and "exfil" in m.get("ALPHA", {}).get("instruction_like", []) and "OSCAR" not in m, sorted(m))
    h.call("end_session", {"session": sess})
    c = h.call("launch_app", {"launch_path": "/usr/bin/google-chrome-stable",
                              "additional_arguments": ["--no-first-run", "--no-default-browser-check", URL]}).structured
    gpid, gw = c.get("pid"), None
    for _ in range(60):
        ws = h.call("list_windows", {"pid": gpid}).structured.get("windows") or []
        gw = next((w for w in ws if "Garden Club" in (w.get("title") or "")), None)
        if gw:
            break
        time.sleep(0.5)
    time.sleep(2)
    r = h.call("get_window_state", {"pid": gpid, "window_id": (gw or {}).get("window_id"), "include_screenshot": False}).raw
    m = marked(r)
    check("Chrome (launch_app) accessibility tree: 1-px and 2-px type, the off-left, far-above and transformed blocks "
          "are marked", "tiny" in m.get("ALPHA", {}).get("hidden", []) and "tiny" in m.get("JULIET", {}).get("hidden", [])
          and "off_left" in m.get("BRAVO", {}).get("hidden", []) and "off_above" in m.get("MIKE", {}).get("hidden", [])
          and "no_size" in m.get("NOVEMBER", {}).get("hidden", []), sorted(m))
    h.call("kill_app", {"pid": gpid})
    h.close()
    mcp_hub.stop_hub(env_on)
    with open(mcp_hub.paths(env_on)["log"]) as f:
        log = f.read()
    notes = [ln for ln in log.splitlines() if "marked page text" in ln]
    check("hub.log notes the marks with counts only", notes and not any(w in "\n".join(notes) for w in PAGE_WORDS),
          notes[:3])
    mc("sh", "--override", "-", stdin="pkill -f 'http.server 8000'; rm -rf ~/metacomputer-runs/hidden-test; true\n")


if __name__ == "__main__":
    sys.exit(main())
