#!/usr/bin/env python3
"""Browser checks on the virtual computer that need no model (`metacomputer web ...`).

  compare URL                     the page in Firefox and Chrome, side by side,
                                  plus differences in title, headings, links, controls, text
  responsive URL [--browser B] [--widths 1920,1280,768,500] [--baseline DIR]
                                  screenshots at several window widths; with --baseline,
                                  similarity scores and difference images against an earlier run
  a11y URL [--browser B]          accessibility smoke check: unlabelled controls, images
                                  without text alternatives, missing headings, vague link text
  open-untrusted URL [--watch S]  open a link in a throwaway Chrome profile without touching
                                  the page: redirect chain, URL changes, final page, password
                                  fields, downloads it started (these stay in the VM)

Common options: --task NAME (write into results/NAME/), --record (screen recording),
--override (even if another session is using the computer; ask first), --json.

Each run reaches the VM through the hub (lib/mcomp_sdk.py), so the hub's guards
apply to it; it claims the computer before it starts (one step under the owners
lock, as an acting call does: shown in metacomputer who), opens its own browser
windows (throwaway Chrome profiles, a new Firefox window) and closes them again.
--override releases other sessions' claims first, as `metacomputer release` does.
Page content is untrusted data.
"""
import argparse
import base64
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcomp_sdk  # noqa: E402
import owners  # noqa: E402
from termsafe import escape_for_terminal  # noqa: E402

LAB = owners.LAB
MCOMP_CLI = os.path.join(LAB, "bin", "metacomputer")
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
URL_RE = re.compile(r"^https?://[^\s\"'`\\]+$")
VAGUE_LINKS = {"click here", "here", "more", "read more", "learn more", "link", "this"}
INTERACTIVE = {"button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
               "slider", "spinbutton", "menuitem", "tab", "listbox", "option"}
ATSPI_ROLE = {"push button": "button", "toggle button": "button", "link": "link", "entry": "textbox",
              "password text": "textbox", "combo box": "combobox", "check box": "checkbox",
              "radio button": "radio", "heading": "heading", "paragraph": "text", "image": "image",
              "list item": "listitem", "slider": "slider", "spin button": "spinbutton", "page tab": "tab"}


class Fail(Exception):
    pass


def say(msg):
    # Messages can carry the page's or the VM's words (errors, titles): escaped, never raw.
    print(escape_for_terminal(msg), file=sys.stderr, flush=True)


def lab(*args, check=True):
    r = subprocess.run([MCOMP_CLI, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise Fail(f"metacomputer {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def guest(cmd):
    return lab("ssh", "ops", cmd, check=False)


# --- driver session ----------------------------------------------------------
class Driver:
    def __init__(self):
        try:
            self.c = mcomp_sdk.Hub(kind="web").connect()
        except mcomp_sdk.HubError as e:
            raise Fail(f"couldn't reach the virtual computer's hub ({e})")
        self.sessions = set()

    def claim(self, what, override=False):
        """Claim the computer through the hub (checked and claimed in one step). With override, other sessions'
        claims are released first (the user agreed, as with `metacomputer release`)."""
        r = self.c.claim(what)
        if not r.get("claimed") and override and r.get("others"):
            for o in owners.others():
                if o["kind"] == "session":
                    owners.drop_claim(o["bridge_pid"])
                    say("released: " + owners.describe(o))
            r = self.c.claim(what)
        if not r.get("claimed"):
            raise Fail("another session is using the virtual computer (metacomputer who): "
                       + "; ".join(r.get("others") or [r.get("message") or "?"])
                       + (". Ask before taking over; then re-run with --override" if not override else ""))

    def call(self, tool, args=None, ok=True, timeout=120):
        r = self.c.call(tool, args or {}, timeout=timeout)
        if ok and r.is_error:
            raise Fail(f"{tool}: {(r.text or '').strip()[:300]}")
        return r

    def close(self):
        for s in list(self.sessions):
            self.call("end_session", {"session": s}, ok=False)
        self.c.close()

    # windows ------------------------------------------------------------------
    def windows(self, **filt):
        return (self.call("list_windows", filt).structured or {}).get("windows", [])

    def wait_new_window(self, before, pid=None, app=None, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            ws = self.windows(**({"pid": pid} if pid else {"app": app} if app else {}))
            new = [w for w in ws if w["window_id"] not in before and w.get("width", 0) > 300]
            if new:
                return new[0]
            time.sleep(0.4)
        raise Fail(f"no new window appeared ({app or pid})")

    def frame(self, w, width, height=1000):
        self.call("set_window_frame", {"pid": w["pid"], "window_id": w["window_id"],
                                       "x": 0, "y": 27, "width": width, "height": height}, ok=False)

    def window_png(self, w, path):
        r = self.call("get_window_state", {"pid": w["pid"], "window_id": w["window_id"],
                                           "include_accessibility_tree": False})
        if not r.images:
            raise Fail("no screenshot returned")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r.images[0]["data"]))
        return path

    # Firefox (accessibility tree; a new window, closed afterwards) -----------
    def open_firefox(self, url, width=1920):
        before = {w["window_id"] for w in self.windows(app="firefox")}
        self.call("launch_app", {"name": "firefox", "additional_arguments": ["--new-window", url]})
        w = self.wait_new_window(before, app="firefox", timeout=30)
        self.frame(w, width)
        self.wait_page_firefox(w)
        # every Firefox window this launch opened, so cleanup closes exactly those
        w["opened"] = [x["window_id"] for x in self.windows(app="firefox") if x["window_id"] not in before]
        return w

    def wait_page_firefox(self, w, timeout=25):
        end, last = time.time() + timeout, None
        while time.time() < end:
            t = next((x.get("title") for x in self.windows(pid=w["pid"]) if x["window_id"] == w["window_id"]), "")
            if t and t != last and not t.startswith(("Mozilla Firefox", "New Tab")):
                last = t
                time.sleep(1.5)  # let late content settle
                cur = next((x.get("title") for x in self.windows(pid=w["pid"]) if x["window_id"] == w["window_id"]), "")
                if cur == t:
                    w["title"] = t
                    return
            time.sleep(0.5)
        w["title"] = last or ""

    def close_firefox(self, w):
        """Close the windows this run opened, as the window manager's close button does
        (WM_DELETE_WINDOW via wmctrl), then confirm they're gone. Other Firefox windows stay."""
        ids = w.get("opened") or [w["window_id"]]
        for wid in ids:
            guest(f"/opt/metacomputer/bin/session-run wmctrl -ic {hex(wid)}")
        end = time.time() + 10
        while time.time() < end:
            left = [x for x in self.windows(app="firefox") if x["window_id"] in ids]
            if not left:
                return True
            time.sleep(0.5)
        say(f"warning: Firefox window(s) {[hex(x['window_id']) for x in left]} did not close")
        return False

    def firefox_page(self, w):
        r = self.call("get_window_state", {"pid": w["pid"], "window_id": w["window_id"], "include_screenshot": False,
                                           "tree_format": "elements", "max_elements": 3000, "timeout_ms": 5000})
        els = (r.structured or {}).get("elements", [])
        def idx(e):
            tok = e.get("element_token") or ""
            return int(tok.rsplit(":", 1)[1]) if ":" in tok else e.get("element_index")
        by = {idx(e): e for e in els}

        def under(e, root):
            seen, p = 0, e.get("parent_index")
            while p is not None and seen < 64:
                if p == root:
                    return True
                p, seen = (by.get(p) or {}).get("parent_index"), seen + 1
            return False
        # A read returns every window of the Firefox process; use this window's frame.
        title = (w.get("title") or "").strip()
        frame = next((e for e in els if e.get("role") == "frame" and (e.get("label") or "").strip() == title), None)
        doc = next((e for e in els if e.get("role") == "document web" and (frame is None or under(e, idx(frame)))), None)
        if not doc:
            return {"title": title, "items": [], "text": ""}
        root = idx(doc)

        def inside(e):
            return under(e, root)
        items, words = [], []
        for e in els:
            if e is doc or not inside(e):
                continue
            role = ATSPI_ROLE.get(e.get("role", ""), e.get("role", ""))
            name = (e.get("label") or "").replace("\ufffc", "").strip()
            if role == "button" and " Browse\u2026" in name:
                name = name.split(" Browse\u2026", 1)[0]  # Firefox names file inputs "<label> Browse… <state>"
            items.append({"role": role, "name": name, "ref": e.get("element_token")})
            if role in ("text", "heading", "link", "listitem", "label", "static", "button") and name:
                words.append(name)
        return {"title": (doc.get("label") or "").strip(), "items": items, "text": " ".join(words)}

    # Chrome (driver-owned throwaway profile; page tools) ---------------------
    def open_chrome(self, url, session, width=1920, profile=None):
        # A name of its own for every run: the hub keeps one driver connection, and Cua Driver keeps an
        # ended session's name ended for that connection's lifetime, so a fixed name works only once.
        session = f"{session}-{os.getpid()}-{len(self.sessions) + 1}-{int(time.time() * 1000) % 1000000}"
        r = self.call("browser_prepare", {"session": session, "allow_launch": True,
                                          "profile": profile or {"mode": "isolated_new"}})
        self.sessions.add(session)
        pid = (r.structured or {}).get("prepared_pid")
        w = self.wait_new_window(set(), pid=pid)
        self.frame(w, width)
        b = self.call("get_browser_state", {"pid": pid, "window_id": w["window_id"], "session": session}).structured
        tgt = {"target_id": b["target_id"], "tab_id": b["tabs"][0]["tab_id"], "session": session}
        w["target"] = tgt
        if url:
            self.call("browser_navigate", {**tgt, "url": url})
            self.wait_page_chrome(w)
        return w

    def bind(self, w):
        return self.call("get_browser_state", {"pid": w["pid"], "window_id": w["window_id"],
                                               "session": w["target"]["session"]}, ok=False).structured or {}

    def wait_page_chrome(self, w, timeout=25):
        end, last = time.time() + timeout, None
        while time.time() < end:
            tabs = self.bind(w).get("tabs") or [{}]
            cur = (tabs[0].get("url"), tabs[0].get("title"))
            if cur == last and cur[0] and cur[0] != "about:blank":
                return
            last = cur
            time.sleep(0.8)

    def chrome_page(self, w):
        s = self.call("get_browser_state", {**w["target"], "snapshot_format": "semantic_v2"}).structured or {}
        items = []
        for r in (s.get("refs") or []) + (s.get("content_refs") or []):
            items.append({"role": (r.get("role") or "").lower(), "name": (r.get("name") or "").strip(),
                          "ref": r.get("ref"), "states": r.get("states") or {}})
        words = [i["name"] for i in items if i["role"] in ("statictext", "heading", "link", "button") and i["name"]]
        dom = self.call("get_browser_state", {**w["target"], "snapshot_format": "dom_refs_v1"}, ok=False).structured or {}
        page = s.get("page") or {}
        return {"title": page.get("title", ""), "url": page.get("url", ""), "items": items, "text": " ".join(words),
                "outline": s.get("outline", ""), "dom": dom.get("refs") or []}

    def chrome_viewport_png(self, w, path):
        r = self.call("get_browser_state", {**w["target"], "snapshot_format": "semantic_v2", "include_screenshot": True})
        if r.images:
            with open(path, "wb") as f:
                f.write(base64.b64decode(r.images[0]["data"]))
            return path
        return self.window_png(w, path)


# --- shared plumbing -----------------------------------------------------------
CURRENT = []   # the active Run, for cleanup when a command fails


class Run:
    def __init__(self, kind, a):
        self.a = a
        if not URL_RE.fullmatch(a.url):
            raise Fail("give an http:// or https:// URL")
        self.d = Driver()
        CURRENT.append(self)
        self.d.claim(f"web {kind}", a.override)
        out = lab("task", "start", f"web-{kind}")
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        self.task = kv["NAME"]
        self.rec_dir = kv["RECORDING_DIR"].split()[0]
        self.dir = os.path.join(LAB, "results", a.task or self.task)
        os.makedirs(self.dir, exist_ok=True)
        if a.record:
            self.d.call("start_recording", {"output_dir": self.rec_dir, "record_video": True})

    def finish(self, report, md):
        if self.a.record:
            self.d.call("stop_recording", ok=False)
        self.d.close()               # the hub lets go of this run's claim
        if self.a.record:
            lab("task", "finish", self.task, check=False)
        with open(os.path.join(self.dir, "report.json"), "w") as f:
            json.dump(report, f, indent=2)
        with open(os.path.join(self.dir, "report.md"), "w") as f:
            f.write(md + "\n")
        report["folder"] = self.dir
        return report

    def path(self, name):
        return os.path.join(self.dir, name)


def side_by_side(pngs, out, height=700):
    args, filt = [], []
    for i, p in enumerate(pngs):
        args += ["-i", p]
        filt.append(f"[{i}:v]scale=-2:{height}[v{i}]")
    filt.append("".join(f"[v{i}]" for i in range(len(pngs))) + f"hstack=inputs={len(pngs)}")
    subprocess.run([FFMPEG, "-v", "error", "-y", *args, "-filter_complex", ";".join(filt), "-frames:v", "1", out],
                   check=True)
    return out


def png_size(p):
    with open(p, "rb") as f:
        h = f.read(24)
    return int.from_bytes(h[16:20], "big"), int.from_bytes(h[20:24], "big")


def ssim_and_diff(new, old, diff_out):
    w, h = png_size(new)
    fl = f"[1:v]scale={w}:{h}[o];[0:v][o]"
    r = subprocess.run([FFMPEG, "-v", "info", "-i", new, "-i", old, "-lavfi", fl + "ssim", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"All:([0-9.]+)", r.stderr)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", new, "-i", old, "-filter_complex",
                    fl + "blend=all_mode=difference,eq=contrast=3:brightness=0.05", "-frames:v", "1", diff_out],
                   check=False)
    return float(m.group(1)) if m else None


# --- commands --------------------------------------------------------------------
def cmd_compare(a):
    run = Run("compare", a)
    d = run.d
    ff = d.open_firefox(a.url)
    ff_page = d.firefox_page(ff)
    d.window_png(ff, run.path("firefox.png"))
    ch = d.open_chrome(a.url, "web-compare")
    ch_page = d.chrome_page(ch)
    d.window_png(ch, run.path("chrome.png"))
    d.close_firefox(ff)
    side_by_side([run.path("firefox.png"), run.path("chrome.png")], run.path("side-by-side.png"))

    def pick(page, roles):
        return [i["name"] for i in page["items"] if i["role"] in roles and i["name"]]
    rows, diffs = [], {}
    for label, roles in (("headings", {"heading"}), ("links", {"link"}),
                         ("controls", {"button", "textbox", "combobox", "checkbox", "radio"})):
        f, c = pick(ff_page, roles), pick(ch_page, roles)
        only_f, only_c = sorted(set(f) - set(c)), sorted(set(c) - set(f))
        diffs[label] = {"firefox": len(f), "chrome": len(c), "only_firefox": only_f[:30], "only_chrome": only_c[:30]}
        rows.append(f"| {label} | {len(f)} | {len(c)} | {', '.join(only_f[:6]) or '-'} | {', '.join(only_c[:6]) or '-'} |")
    sim = difflib.SequenceMatcher(None, ff_page["text"].split(), ch_page["text"].split()).ratio()
    report = {"url": a.url, "titles": {"firefox": ff.get("title"), "chrome": ch_page["title"]},
              "text_similarity": round(sim, 3), "differences": diffs,
              "files": {"side_by_side": run.path("side-by-side.png"), "firefox": run.path("firefox.png"),
                        "chrome": run.path("chrome.png")}}
    md = "\n".join([f"# Cross-browser check: {a.url}", "",
                    f"- Firefox window title: {ff.get('title')}", f"- Chrome page title: {ch_page['title']}",
                    f"- Visible-text similarity (word sequence): {sim:.0%}", "",
                    "| | Firefox | Chrome | only in Firefox | only in Chrome |", "|---|---|---|---|---|", *rows, "",
                    f"Side by side: {run.path('side-by-side.png')}",
                    "Names compare the accessibility tree (Firefox) with Chrome's page snapshot; small "
                    "wording differences can come from how each browser names things."])
    return run.finish(report, md)


def cmd_responsive(a):
    run = Run("responsive", a)
    d = run.d
    widths = [int(x) for x in a.widths.split(",")]
    w = d.open_chrome(a.url, "web-responsive", widths[0]) if a.browser == "chrome" else d.open_firefox(a.url, widths[0])
    shots, scores = [], []
    for wd in widths:
        d.frame(w, wd)
        time.sleep(1.2)
        p = run.path(f"{a.browser}-{wd}.png")
        (d.chrome_viewport_png(w, p) if a.browser == "chrome" else d.window_png(w, p))
        shots.append(p)
        if a.baseline:
            old = os.path.join(a.baseline, os.path.basename(p))
            if os.path.exists(old):
                s = ssim_and_diff(p, old, run.path(f"diff-{a.browser}-{wd}.png"))
                scores.append({"width": wd, "ssim": s, "changed": s is not None and s < a.threshold,
                               "diff": run.path(f"diff-{a.browser}-{wd}.png")})
            else:
                scores.append({"width": wd, "ssim": None, "changed": None, "note": "no baseline image"})
    if a.browser == "firefox":
        d.close_firefox(w)
    side_by_side(shots, run.path("sheet.png"), height=600)
    report = {"url": a.url, "browser": a.browser, "widths": widths, "screenshots": shots,
              "sheet": run.path("sheet.png"), "baseline": a.baseline, "comparison": scores}
    lines = [f"# Responsive check: {a.url} ({a.browser})", "", f"Sheet: {run.path('sheet.png')}", ""]
    if scores:
        lines += ["| width | similarity (SSIM) | changed |", "|---|---|---|"]
        lines += [f"| {s['width']} | {s['ssim'] if s['ssim'] is not None else '-'} | "
                  f"{'yes' if s.get('changed') else 'no' if s.get('changed') is False else '-'} |" for s in scores]
        lines += ["", f"Changed means SSIM below {a.threshold}; difference images are next to the screenshots."]
    lines += ["", f"Use this folder as --baseline for the next run: {run.dir}"]
    return run.finish(report, "\n".join(lines))


def cmd_a11y(a):
    run = Run("a11y", a)
    d = run.d
    findings = []
    if a.browser == "chrome":
        w = d.open_chrome(a.url, "web-a11y")
        page = d.chrome_page(w)
        for i in page["items"]:
            if i["role"] in INTERACTIVE and not i["name"] and i["role"] != "option":
                findings.append({"issue": f"unlabelled {i['role']}", "ref": i["ref"]})
            if i["role"] in ("image", "img") and not i["name"]:
                findings.append({"issue": "image without a text alternative", "ref": i["ref"]})
            if i["role"] == "link" and i["name"].lower() in VAGUE_LINKS:
                findings.append({"issue": f"vague link text \"{i['name']}\"", "ref": i["ref"]})
        for r in page["dom"]:
            if r.get("node") == "img" and "alt=" not in (r.get("label") or ""):
                findings.append({"issue": "img element without an alt attribute", "ref": r.get("ref")})
        d.window_png(w, run.path("page.png"))
    else:
        w = d.open_firefox(a.url)
        page = d.firefox_page(w)
        for i in page["items"]:
            if i["role"] in INTERACTIVE and not i["name"]:
                findings.append({"issue": f"unlabelled {i['role']}", "ref": i["ref"]})
            if i["role"] == "image" and not i["name"]:
                findings.append({"issue": "image without a text alternative", "ref": i["ref"]})
            if i["role"] == "link" and i["name"].lower() in VAGUE_LINKS:
                findings.append({"issue": f"vague link text \"{i['name']}\"", "ref": i["ref"]})
        d.window_png(w, run.path("page.png"))
        d.close_firefox(w)
    headings = [i for i in page["items"] if i["role"] == "heading"]
    if not headings:
        findings.append({"issue": "no headings on the page"})
    if not page["title"]:
        findings.append({"issue": "page has no title"})
    counts = {}
    for f in findings:
        counts[f["issue"]] = counts.get(f["issue"], 0) + 1
    report = {"url": a.url, "browser": a.browser, "title": page["title"], "headings": len(headings),
              "findings": findings, "summary": counts, "screenshot": run.path("page.png")}
    md = "\n".join([f"# Accessibility smoke check: {a.url} ({a.browser})", "",
                    f"Title: {page['title'] or '(none)'}; headings: {len(headings)}; findings: {len(findings)}", "",
                    *(f"- {k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])),
                    "", "A smoke check from the accessibility data, not an audit: it can't judge contrast, "
                    "focus order, or whether text alternatives are meaningful."])
    return run.finish(report, md)


HOSTNAME_RE = re.compile(r"^(?:[A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}$|^[0-9A-Fa-f:.]{2,45}$")


def origin(url):
    """scheme://host[:port] of a URL: no path, query or credentials, and only a host made of letters, digits,
    dots and dashes (or an IP address); anything else is None. This is what the event stream may carry."""
    if not isinstance(url, str):
        return None
    try:
        u = urlsplit(url or "")
        if u.scheme not in ("http", "https") or not u.hostname or not HOSTNAME_RE.fullmatch(u.hostname):
            return None
        host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")
    except ValueError:
        return None


def site(url):
    if not isinstance(url, str):
        return ""
    try:
        h = (urlsplit(url or "").hostname or "").lower()
    except ValueError:                      # a malformed address from the page or its server
        return ""
    return h[4:] if h.startswith("www.") else h


def untrusted_findings(start, chain, seen, final_url, password_fields, asked, downloads):
    """What open-untrusted found that matters (a password field, a download, ending on another site), by
    origin only, or None. This is what reaches the event stream. Every address here came from the untrusted
    server or page, so a malformed one is skipped rather than trusted."""
    visited, cur = [], start
    for c in chain:
        loc = c.get("location")
        if isinstance(loc, str) and loc:
            try:
                cur = urljoin(cur, loc)
            except ValueError:
                continue
            visited.append(cur)
    for st in seen:
        visited += [u for u, _ in st.get("tabs") or [] if isinstance(u, str) and u]
    final = final_url or (visited[-1] if visited else start)
    off_site = bool(origin(final)) and site(final) != site(start)
    reasons = []                            # fixed words only: the hosts stay in the structured fields
    if password_fields or asked:
        reasons.append("asked for a password")
    if downloads:
        reasons.append(f"started {len(downloads)} download(s)")
    if off_site:
        reasons.append("ended on another site")
    if not reasons:
        return None
    origins = []
    for u in [start] + visited + [final]:
        o = origin(u)
        if o and o not in origins:
            origins.append(o)
    return {"summary": "; ".join(reasons), "start_origin": origin(start), "final_origin": origin(final),
            "password_fields": password_fields, "asked_for_password_on": sorted({origin(u) for u in asked if origin(u)}),
            "downloads": len(downloads), "redirected_off_site": off_site,
            "origins": origins[:10]}


def cmd_open_untrusted(a):
    run = Run("untrusted", a)
    d = run.d
    q = shlex.quote(a.url)
    hdr = guest(f"curl -sS -o /dev/null -D - -L --max-redirs 10 -m 20 --proto =http,https {q} 2>&1 | "
                r"grep -iE '^(HTTP/|location:)' | tr -d '\r'")
    chain, status = [], None
    for line in hdr.splitlines():
        if line.upper().startswith("HTTP/"):
            status = line.split()[1] if len(line.split()) > 1 else line
            chain.append({"status": status})
        elif line.lower().startswith("location:") and chain:
            chain[-1]["location"] = line.split(":", 1)[1].strip()
    before = set(guest("ls -1 ~/Downloads 2>/dev/null").split("\n"))
    w = d.open_chrome(None, "web-untrusted")
    d.call("browser_navigate", {**w["target"], "url": a.url})
    seen, end = [], time.time() + a.watch
    while time.time() < end:
        b = d.bind(w)
        tabs = b.get("tabs") or []
        state = [(t.get("url"), t.get("title")) for t in tabs]
        if not seen or seen[-1]["tabs"] != state:
            dom = d.call("get_browser_state", {**w["target"], "snapshot_format": "dom_refs_v1"},
                         ok=False).structured or {}
            pw = sum(1 for r in dom.get("refs") or [] if "type=password" in (r.get("label") or ""))
            seen.append({"t": round(a.watch - (end - time.time()), 1), "tabs": state, "password_fields": pw})
        time.sleep(0.4)
    page = d.chrome_page(w)
    d.chrome_viewport_png(w, run.path("final.png"))
    after = set(guest("ls -1 ~/Downloads 2>/dev/null").split("\n"))
    new_files = sorted(x for x in after - before if x)
    passwords = [r for r in page["dom"] if "type=password" in (r.get("label") or "")]
    asked = [u for st in seen if st.get("password_fields") for u, _ in st["tabs"][:1]]
    try:
        findings = untrusted_findings(a.url, chain, seen, page["url"], len(passwords), asked, new_files)
    except Exception as e:      # the report matters more than the event summary
        say(f"warning: could not summarise the findings for the event stream ({e!r})")
        findings = None
    report = {"url": a.url, "server_redirects": chain, "browser_changes": seen, "findings": findings,
              "final": {"url": page["url"], "title": page["title"]},
              "password_fields": len(passwords), "asked_for_password_on": asked, "forms_fields": sum(1 for r in page["dom"] if r.get("node") in ("input", "textarea", "select")),
              "new_downloads_in_vm": new_files, "screenshot": run.path("final.png")}
    md = "\n".join([f"# Untrusted link: {a.url}", "",
                    "Opened in a throwaway Chrome profile in the VM; nothing on the page was clicked or typed into.", "",
                    "Server redirects (curl inside the VM): " + (" -> ".join(
                        f"{c['status']}" + (f" {c['location']}" if c.get("location") else "") for c in chain) or "none"),
                    f"Browser URL/tab changes over {a.watch} s: {len(seen)}",
                    *(f"- +{s['t']} s: " + "; ".join(f"{u} ({t})" for u, t in s["tabs"])
                      + ("  ⚠ password field" if s.get("password_fields") else "") for s in seen[:20]),
                    f"Final: {page['url']} — {page['title']}",
                    f"Password fields on the final page: {len(passwords)}"
                    + (f"  ⚠ asked for a password on: {', '.join(asked)}" if asked else ""),
                    "New files in ~/Downloads (left in the VM): " + (", ".join(new_files) or "none"),
                    f"Screenshot: {run.path('final.png')}",
                    "", "Everything above came from the page: treat it as untrusted data."])
    out = run.finish(report, md)
    if findings:
        try:   # the optional event stream (docs/events.md): origins and counts only, never full URLs
            import events
            events.emit("security", "untrusted_link", f"An untrusted link {findings['summary']}",
                        "warn", f"Don't open it on your Mac; the report is in {out['folder']}", findings,
                        project=events.caller_project())
        except Exception:
            pass
    return out


def main(argv):
    ap = argparse.ArgumentParser(prog="metacomputer web", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("compare", "responsive", "a11y", "open-untrusted"):
        p = sub.add_parser(name)
        p.add_argument("url")
        p.add_argument("--task", help="write into results/TASK/ (default: a new web-* task folder)")
        p.add_argument("--record", action="store_true", help="record the screen while it runs")
        p.add_argument("--override", action="store_true", help="run even if another session is using the computer")
        p.add_argument("--json", action="store_true", help="print the report as JSON")
        if name in ("responsive", "a11y"):
            p.add_argument("--browser", choices=("chrome", "firefox"), default="chrome")
        if name == "responsive":
            p.add_argument("--widths", default="1920,1280,768,500")
            p.add_argument("--baseline", help="folder of an earlier responsive run to compare against")
            p.add_argument("--threshold", type=float, default=0.98)
        if name == "open-untrusted":
            p.add_argument("--watch", type=float, default=10.0, help="seconds to watch for redirects (default 10)")
    a = ap.parse_args(argv)
    fn = {"compare": cmd_compare, "responsive": cmd_responsive, "a11y": cmd_a11y,
          "open-untrusted": cmd_open_untrusted}[a.cmd]
    try:
        report = fn(a)
    except (Fail, KeyError, OSError, subprocess.CalledProcessError) as e:
        for r in CURRENT:
            try:
                r.d.close()        # ends its sessions (driver-owned Chrome windows close) and the claim
            except Exception:
                pass
        say(f"metacomputer web {a.cmd}: {e}")
        return 1
    if a.json:
        print(json.dumps(report, indent=2))   # exact; json.dumps escapes control characters itself
    else:
        # The report quotes the page (titles, headings, links, text): escaped for the terminal.
        # report.md on disk keeps the page's own text.
        print(escape_for_terminal(open(os.path.join(report["folder"], "report.md"), newline="").read().rstrip()))
        print(f"\nfolder: {escape_for_terminal(report['folder'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
