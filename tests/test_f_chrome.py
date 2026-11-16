"""Test F: Google Chrome through the driver's page tools (page-level mode).

A driver-owned Chrome on a throwaway profile fills a local test form through
the page's own elements (text, dropdown, check box, file upload, submit), in
the background where Linux allows it. It then:
- downloads a file into the run folder and renames it from the link's
  address;
- answers a JavaScript confirm dialog;
- types key by key (hybrid mode);
- closes the browser by ending its session.

Pages are served inside the VM from tests/fixtures/site (127.0.0.1:8765).
Every result is read back from a fresh page snapshot or from the guest's disk.
"""
import os
import re
import subprocess
import time

from harness import Step, guest
from labconfig import GUEST_RUNS

TITLE = "Chrome page tools"
TASK = ("Launch Chrome on a throwaway profile through the driver, fill and submit a form through the page's "
        "elements, download and rename a file, answer a confirm dialog, type key by key, close it")
LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def serve():
    subprocess.run([os.path.join(LAB, "bin", "virtual-computer"), "put", os.path.join(LAB, "tests", "fixtures", "site"),
                    "--to", GUEST_RUNS], check=True, capture_output=True)
    guest(f"cd {GUEST_RUNS}/site && (nohup python3 -m http.server {PORT} --bind 127.0.0.1 "
          f">/dev/null 2>&1 & echo $! > /tmp/cua-test-http.pid); sleep 1", check=True)


def stop_serving():
    guest("kill $(cat /tmp/cua-test-http.pid) 2>/dev/null; rm -f /tmp/cua-test-http.pid; true")


def snapshot(t, tgt, query=None, fmt="semantic_v2"):
    args = {**tgt, "snapshot_format": fmt}
    if query:
        args["query"] = query
    return t.call("get_browser_state", args).structured or {}


def refs(snap):
    return {r.get("name"): r["ref"] for r in snap.get("refs") or [] if r.get("name")}


def page_text(snap):
    return " ".join((r.get("name") or "") for r in (snap.get("refs") or []) + (snap.get("content_refs") or []))


def run(t):
    serve()
    try:
        return _run(t)
    finally:
        stop_serving()


def _run(t):
    sess = f"chrome-{t.run_id[-6:]}"   # not the harness's own session label
    r = t.call("browser_prepare", {"session": sess, "allow_launch": True, "profile": {"mode": "isolated_new"}})
    st = r.structured or {}
    t.check("driver launched its own Chrome on a throwaway profile",
            st.get("status") == "ok" and st.get("action") == "launched_isolated_browser"
            and not (st.get("lifecycle") or {}).get("profile_persists_after_session"), st.get("message", "")[:120])
    pid = st["prepared_pid"]
    win = None
    for _ in range(30):
        ws = (t.call("list_windows", {"pid": pid}).structured or {}).get("windows", [])
        if ws:
            win = ws[0]
            break
        time.sleep(0.3)
    if not win:
        raise Step("Chrome's window never appeared")
    wid = win["window_id"]
    t.call("set_window_frame", {"pid": pid, "window_id": wid, "x": 0, "y": 27, "width": 1920, "height": 1000})
    b = t.call("get_browser_state", {"pid": pid, "window_id": wid, "session": sess}).structured
    t.check("page tools bound exactly to the window", b.get("binding_quality") == "exact" and b.get("mutation_allowed"),
            f"{b.get('binding_quality')} mutation_allowed={b.get('mutation_allowed')}")
    tgt = {"target_id": b["target_id"], "tab_id": b["tabs"][0]["tab_id"], "session": sess}

    # 1. the form, through the page's elements
    t.call("browser_navigate", {**tgt, "url": f"{BASE}/form.html"})
    time.sleep(0.8)
    m = refs(snapshot(t, tgt))
    for need in ("Name", "Colour", "I agree", "Attachment", "Submit", "Download report"):
        if need not in m:
            raise Step(f"no ref for {need!r} in the page snapshot: {sorted(m)}")
    t0 = time.time()
    t.call("browser_type", {**tgt, "ref": m["Name"], "text": "Ada Lovelace"})
    t.call("browser_select", {**tgt, "ref": m["Colour"], "label": "Blue"})
    t.call("browser_set_input_files", {**tgt, "ref": m["Attachment"],
                                       "files": [f"{GUEST_RUNS}/site/upload.txt"]})
    t.call("browser_click", {**tgt, "ref": m["I agree"], "input_route": "dom_event"})
    t.call("browser_click", {**tgt, "ref": m["Submit"], "delivery_mode": "foreground"})
    t.evidence["form_seconds"] = round(time.time() - t0, 2)
    time.sleep(0.8)
    out = page_text(snapshot(t, tgt, query="name="))
    t.evidence["form_result"] = out[:200]
    t.check("form submitted with every value (text, dropdown, check box, file)",
            "name=Ada Lovelace colour=blue agree=true file=upload.txt" in out, out[:160])
    t.shot("form submitted", pid, wid)

    # 2. download into the run folder, renamed from the link's address
    guest(f"mkdir -p {t.guest_dir}/files", check=True)
    m = refs(snapshot(t, tgt))
    dl = t.call("browser_download", {**tgt, "ref": m["Download report"],
                                     "destination_root": f"{t.guest_dir}/files"}).structured or {}
    dom = snapshot(t, tgt, fmt="dom_refs_v1")
    href = next((re.search(r"href=(\S+)", r.get("label") or "").group(1) for r in dom.get("refs") or []
                 if "id=dl" in (r.get("label") or "")), "download.bin")
    name = os.path.basename(href)
    ren = subprocess.run([os.path.join(LAB, "bin", "virtual-computer"), "rename",
                          f"{t.guest_dir}/files/{dl.get('download_id')}", name], capture_output=True, text=True)
    content = guest(f"cat {t.guest_dir}/files/{name}").stdout.strip()
    t.evidence["download"] = {"result": dl, "renamed_to": ren.stdout.strip(), "content": content}
    t.check("download saved, renamed from the link's address, content right",
            dl.get("status") == "completed" and content == "quarterly report: 42", f"{name}: {content!r}")

    # 3. a JavaScript confirm dialog
    t.call("browser_navigate", {**tgt, "url": f"{BASE}/dialog.html"})
    time.sleep(0.8)
    m = refs(snapshot(t, tgt))
    t.call("browser_click", {**tgt, "ref": m["Ask"], "delivery_mode": "foreground"})
    d = t.call("browser_dialog", {**tgt, "action": "inspect"}).structured or {}
    if d.get("present"):
        t.call("browser_dialog", {**tgt, "action": "accept", "dialog_id": d["dialog_id"], "delivery_mode": "foreground"})
    time.sleep(0.5)
    out = page_text(snapshot(t, tgt))
    t.check("confirm dialog seen and accepted", d.get("kind") == "confirm" and " yes" in f" {out}", out[:120])

    # 4. hybrid: key-by-key typing through the page
    t.call("browser_navigate", {**tgt, "url": f"{BASE}/form.html"})
    time.sleep(0.8)
    m = refs(snapshot(t, tgt))
    t.call("browser_type", {**tgt, "ref": m["Name"], "text": "Grace", "mode": "keystrokes"})
    t.call("browser_click", {**tgt, "ref": m["Submit"], "input_route": "dom_event"})
    time.sleep(0.6)
    out = page_text(snapshot(t, tgt, query="name="))
    t.check("key-by-key typing reached the page", "name=Grace" in out, out[:120])
    t.shot("hybrid typing", pid, wid)

    # 5. ending the session closes the driver's Chrome
    t.call("end_session", {"session": sess})
    gone = False
    for _ in range(20):
        if not (t.call("list_windows", {"pid": pid}).structured or {}).get("windows"):
            gone = True
            break
        time.sleep(0.3)
    t.check("ending the session closed the driver's Chrome", gone, f"pid {pid}")
    return True
