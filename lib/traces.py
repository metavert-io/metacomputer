#!/usr/bin/env python3
"""Traces of browser work in the VM (`metacomputer trace`), and turning one into a Playwright test.

  trace start NAME [--session S]   from now, the hub records this VM's acting calls (every client's, or one
                                   session's) and the element rows of their window reads
  trace stop                       stop recording (also: a new start, or 24 hours)
  trace list | show NAME [--json] | delete NAME
  trace export NAME [--out FILE] [--url URL]
                                   a Playwright test (TypeScript) of the browser steps: locators by role and name
                                   from the accessibility tree, typed text, key presses, checks (expect and
                                   wait_for) as assertions, the address-bar navigations as page.goto

Claude explores a flow once, with judgement; the export replays it deterministically, in CI, without a model. It
is a draft to review: pixel clicks stay coordinates (marked), steps in apps other than the browsers are left out
(counted), and sign-ins done through `metacomputer secrets` never pass through the hub, so they aren't in it.

The trace (RUN_DIR/traces/NAME.jsonl, 0600, at most 20 MB) holds what the calls said, typed text included, and
names read from pages. Page text is untrusted: in the exported code it appears only inside JSON-escaped string
literals, never in comments or code, so a page can't put code into the test.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TOKEN_RE = re.compile(r"^(s[0-9a-f]{8}):(\d{1,9})$")
ROW_RE = re.compile(r'^\s*[-+~] \[(\d{1,9})\] ([a-z][a-z ]{0,40}?)(?: "((?:[^"\\]|\\.){0,400})"|(?= \[|\s*$| value=))')
MAX_BYTES = 20 << 20
MAX_ROWS = 4000                 # element rows kept per read
MAX_AGE_S = 24 * 3600
BROWSER_APP_RE = re.compile(r"firefox|chrome|chromium|epiphany", re.I)
# A browser window's title ends with the browser's name (traces from hubs before 0.4.2 recorded no app for a call made
# by element token alone; the read the token came from has the window's title).
BROWSER_TITLE_RE = re.compile(r"\s[\u2014-]\s(?:Mozilla Firefox|Google Chrome|Chromium)\s*$|^Web$")
URL_TEXT_RE = re.compile(r"^https?://[^\s\"'`\\<>]{1,2000}$")
ARIA = {"push button": "button", "toggle button": "button", "button": "button", "link": "link", "entry": "textbox",
        "text": None, "password text": "textbox", "text box": "textbox", "textbox": "textbox", "combo box": "combobox",
        "check box": "checkbox", "radio button": "radio", "menu item": "menuitem", "page tab": "tab",
        "list item": "listitem", "heading": "heading", "spin button": "spinbutton", "slider": "slider",
        "image": "img", "search box": "searchbox", "switch": "switch", "option": "option", "list box": "listbox",
        "menu": "menu", "dialog": "dialog", "tree item": "treeitem", "row": "row", "cell": "cell", "table": "table"}
KEYS = {"return": "Enter", "enter": "Enter", "tab": "Tab", "escape": "Escape", "esc": "Escape", "backspace": "Backspace",
        "delete": "Delete", "space": "Space", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
        "right": "ArrowRight", "home": "Home", "end": "End", "pageup": "PageUp", "page_up": "PageUp",
        "pagedown": "PageDown", "page_down": "PageDown"}
MODS = {"ctrl": "Control", "control": "Control", "shift": "Shift", "alt": "Alt", "super": "Meta", "meta": "Meta",
        "cmd": "Meta"}


class Refused(Exception):
    pass


def tdir(run_dir):
    return os.path.join(run_dir, "traces")


def active_path(run_dir):
    return os.path.join(tdir(run_dir), "active.json")


def _open_append(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "a")


# --- the hub's side: cheap, never raises ----------------------------------------------------------------------
class Recorder:
    """For the hub: is a trace on (checked at most once a second), and append records to it."""
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self._checked, self._active = 0.0, None

    def active(self):
        now = time.monotonic()
        if now - self._checked < 1.0:
            return self._active
        self._checked = now
        try:
            fd = os.open(active_path(self.run_dir), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as f:
                a = json.load(f)
            if not (isinstance(a, dict) and NAME_RE.match(str(a.get("name") or ""))
                    and isinstance(a.get("started"), (int, float)) and time.time() - a["started"] < MAX_AGE_S):
                a = None
        except (OSError, ValueError):
            a = None
        self._active = a
        return a

    def wants(self, session):
        a = self.active()
        return bool(a) and (not a.get("session") or a.get("session") == session)

    def write(self, rec):
        a = self.active()
        if not a:
            return
        try:
            path = os.path.join(tdir(self.run_dir), a["name"] + ".jsonl")
            try:
                if os.lstat(path).st_size > MAX_BYTES:
                    return
            except FileNotFoundError:
                pass
            line = json.dumps(rec, ensure_ascii=True, default=str)
            if len(line) > 1 << 20:
                return
            with _open_append(path) as f:
                f.write(line + "\n")
        except OSError:
            pass

    def call(self, tool, args, session, app):
        if self.wants(session):
            self.write({"t": round(time.time(), 3), "kind": "call", "tool": str(tool)[:60], "session": session,
                        "app": (app or "")[:80], "args": args})

    def result(self, tool, res, session):
        """A read's element rows (for tokens), and whether an acting call failed."""
        if not self.wants(session) or not isinstance(res, dict):
            return
        sc = res.get("structuredContent") if isinstance(res.get("structuredContent"), dict) else {}
        if tool == "get_window_state" and not res.get("isError"):
            sid = sc.get("snapshot_id")
            md = sc.get("tree_markdown")
            if not isinstance(md, str):
                md = "\n".join(c.get("text", "") for c in (res.get("content") or [])[:64]
                               if isinstance(c, dict) and isinstance(c.get("text"), str))
            if isinstance(sid, str) and re.fullmatch(r"s[0-9a-f]{8}", sid):
                rows = {}
                for line in md.splitlines():
                    m = ROW_RE.match(line)
                    if m:
                        rows[m.group(1)] = [m.group(2).strip(), _unq(m.group(3) or "")]
                        if len(rows) >= MAX_ROWS:
                            break
                self.write({"t": round(time.time(), 3), "kind": "read", "session": session, "snapshot": sid,
                            "title": str(sc.get("window_title") or "")[:300], "rows": rows})
        elif res.get("isError"):
            self.write({"t": round(time.time(), 3), "kind": "error", "tool": str(tool)[:60], "session": session})


def _unq(s):
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s.replace('\\"', '"')


# --- the CLI --------------------------------------------------------------------------------------------------
def load(run_dir, name):
    if not NAME_RE.match(name or ""):
        raise Refused("a trace name is letters, digits, - and _ (at most 64)")
    path = os.path.join(tdir(run_dir), name + ".jsonl")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise Refused(f"no trace '{name}' (metacomputer trace list)") from None
    out = []
    with os.fdopen(fd) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def step_parts(st):
    """(tool, args) of one run_steps step, in every form the driver accepts (lib/pacing.py has the same)."""
    try:
        import pacing
        return pacing.step_parts(st)
    except Exception:
        if isinstance(st, dict) and "tool" in st:
            return st.get("tool"), st.get("args") if isinstance(st.get("args"), dict) else {}
        if isinstance(st, dict) and len(st) == 1:
            (k, v), = st.items()
            return k, v if isinstance(v, dict) else {}
        return None, {}


def js(s):
    """A JavaScript string literal for any text (page text included): JSON-escaped, ASCII only, so quotes, newlines,
    U+2028/2029, backticks and ${} can't end it or run as code."""
    return json.dumps(str(s), ensure_ascii=True)


def key_of(k):
    k = str(k or "").strip()
    if not k:
        return None
    if "+" in k:
        parts = [p.strip().lower() for p in k.split("+")]
        mods = [MODS.get(p) for p in parts[:-1]]
        if None in mods:
            return None
        last = key_of(parts[-1])
        return "+".join(mods + [last]) if last else None
    low = k.lower()
    if low in KEYS:
        return KEYS[low]
    if re.fullmatch(r"f([1-9]|1[0-2])", low):
        return low.upper()
    if len(k) == 1 and k.isprintable():
        return k
    return None


class Exporter:
    def __init__(self, recs, url=None):
        self.recs, self.url = recs, url
        self.rows = {}                    # snapshot id -> {index: [role, name]}
        self.titles = {}                  # snapshot id -> the window's title at that read
        self.lines, self.skipped, self.pixels, self.steps = [], 0, 0, 0
        self.pending_url = None           # ctrl+L typed: the next text is an address
        self.last_field = None            # (line index, locator) of a click on a text field: typing next is a fill

    def locator(self, a):
        """page.getBy... for an element named by role/name or by a token from a read; None when it can't be told."""
        role, name = a.get("role"), a.get("name")
        tok = a.get("element_token")
        nth = ""
        if isinstance(tok, str):
            m = TOKEN_RE.match(tok)
            row = self.rows.get(m.group(1), {}).get(m.group(2)) if m else None
            if row:
                role, name = row
                nth = self.nth(m.group(1), m.group(2), row)
        if not isinstance(role, str):
            if isinstance(a.get("text"), str) and a.get("text"):
                return f"page.getByText({js(a['text'][:400])})"
            return None
        aria = ARIA.get(role.strip().lower(), "")
        name = name if isinstance(name, str) else ""
        if aria:
            return (f"page.getByRole({js(aria)}, {{ name: {js(name[:400])} }})" if name else f"page.getByRole({js(aria)})") + nth
        if name:
            return f"page.getByText({js(name[:400])})" + nth
        return None

    def nth(self, snap, idx, row):
        """`.nth(k)` when the page has several elements with this role and name (five "Download invoice" links), k
        counted in the read's order within the page (rows after its document); "" when it's the only one."""
        rows = self.rows.get(snap, {})
        num = {int(i): v for i, v in rows.items() if str(i).isdigit()}
        doc = min((i for i, v in num.items() if str(v[0]).strip().lower() == "document web"), default=-1)
        same = sorted(i for i, v in num.items() if i > doc and v == row)
        return f".nth({same.index(int(idx))})" if len(same) > 1 and int(idx) in same else ""

    def emit(self, line):
        self.lines.append("  " + line)
        self.steps += 1

    def check(self, chk, kind="expect"):
        if not isinstance(chk, dict):
            return
        loc = self.locator(chk)
        if loc:
            if isinstance(chk.get("value"), str) and chk.get("value"):
                self.emit(f"await expect({loc}).toHaveValue({js(chk['value'][:400])});")
            else:
                self.emit(f"await expect({loc}).toBeVisible();")

    def act(self, tool, a):
        if not isinstance(a, dict):
            return
        if tool not in ("type_text",):
            self.last_field = None if tool not in ("click",) else self.last_field
        if tool in ("click", "double_click", "right_click"):
            loc = self.locator(a)
            if loc:
                self.emit(f"await {loc}.{'dblclick' if tool == 'double_click' else 'click'}("
                          f"{'{ button: ' + js('right') + ' }' if tool == 'right_click' else ''});")
                if tool == "click" and re.match(r'page\.getByRole\("(textbox|searchbox|combobox)"', loc):
                    self.last_field = (len(self.lines) - 1, loc)
                return
            elif all(isinstance(a.get(k), (int, float)) and not isinstance(a.get(k), bool) for k in ("x", "y")):
                self.lines.append("  // A pixel click, aimed from a screenshot: replace it with a locator.")
                self.emit(f"await page.mouse.click({int(a['x'])}, {int(a['y'])});")
                self.pixels += 1
            else:
                self.skipped += 1
        elif tool == "type_text":
            text = a.get("text")
            if not isinstance(text, str):
                return
            if self.pending_url and URL_TEXT_RE.match(text.strip()):
                self.pending_url = text.strip()
                return
            loc = self.locator(a) if (a.get("element_token") or a.get("role")) else None
            if not loc and self.last_field and self.last_field[0] == len(self.lines) - 1 and "\n" not in text:
                idx, loc = self.last_field     # a click on a field, then typing: one fill
                self.lines[idx] = f"  await {loc}.fill({js(text)});"
                self.last_field = None
                return
            self.emit(f"await {loc}.fill({js(text)});" if loc else f"await page.keyboard.type({js(text)});")
        elif tool == "set_value":
            loc = self.locator(a)
            if loc and isinstance(a.get("value"), str):
                self.emit(f"await {loc}.fill({js(a['value'])});")
            else:
                self.skipped += 1
        elif tool in ("press_key", "hotkey"):
            raw = a.get("key") if tool == "press_key" else "+".join(str(k) for k in (a.get("keys") or [])) or a.get("key")
            k = key_of(raw)
            if k in ("Control+l", "Control+L", "F6", "Alt+d", "Alt+D"):
                self.pending_url = True       # the address bar: the next text, then Enter, is a navigation
                return
            if k == "Enter" and isinstance(self.pending_url, str):
                self.emit(f"await page.goto({js(self.pending_url)});")
                self.pending_url = None
                return
            self.pending_url = None
            if k:
                self.emit(f"await page.keyboard.press({js(k)});")
            else:
                self.skipped += 1
        elif tool == "scroll":
            dy = a.get("amount") if isinstance(a.get("amount"), (int, float)) else 3
            sign = -1 if str(a.get("direction") or "down").lower() == "up" else 1
            self.emit(f"await page.mouse.wheel(0, {int(sign * abs(dy) * 100)});")
        elif tool in ("wait_for",):
            self.check(a, "wait_for")
        else:
            self.skipped += 1

    def run(self, name):
        for r in self.recs:
            k = r.get("kind")
            if k == "read":
                rows = r.get("rows") if isinstance(r.get("rows"), dict) else {}
                self.rows[str(r.get("snapshot"))] = {str(i): v for i, v in rows.items()
                                                     if isinstance(v, list) and len(v) == 2}
                self.titles[str(r.get("snapshot"))] = str(r.get("title") or "")
            elif k == "call":
                tool, a = r.get("tool"), r.get("args") if isinstance(r.get("args"), dict) else {}
                browser = bool(BROWSER_APP_RE.search(str(r.get("app") or "")))
                tok = a.get("element_token")
                if not browser and not r.get("app") and isinstance(tok, str) and TOKEN_RE.match(tok):
                    browser = bool(BROWSER_TITLE_RE.search(self.titles.get(TOKEN_RE.match(tok).group(1), "")))
                if tool == "run_steps":
                    for st in a.get("steps") or []:
                        t, sa = step_parts(st)
                        if not browser and not BROWSER_APP_RE.search(str((sa or {}).get("app") or "")):
                            self.skipped += 1
                            continue
                        if t:
                            self.act(t, sa or {})
                        if isinstance(st, dict):
                            for chk in (st.get("expect") if isinstance(st.get("expect"), list) else
                                        [st.get("expect")] if isinstance(st.get("expect"), dict) else []):
                                self.check(chk)
                            if isinstance(st.get("wait_for"), dict):
                                self.check(st["wait_for"], "wait_for")
                elif browser:
                    self.act(tool, a)
                elif tool not in ("launch_app", "start_recording", "stop_recording", "end_session", "start_session"):
                    self.skipped += 1
        start = self.url
        head = ["import { test, expect } from '@playwright/test';", "",
                "// Exported by `metacomputer trace export` from a metacomputer trace. Review it before you rely on it:",
                "// locators come from the accessibility tree, typed text is as it was typed, pixel clicks are",
                "// coordinates (marked) that won't survive a layout change, and sign-ins done through",
                "// `metacomputer secrets` aren't here (add them, from process.env).",
                f"// Steps: {self.steps}; pixel clicks: {self.pixels}; left out (other apps, or not translatable): {self.skipped}.",
                "", f"test({js(name)}, async ({{ page }}) => {{"]
        if start:
            head.append(f"  await page.goto({js(start)});")
        return "\n".join(head + self.lines + ["});", ""])


def start_trace(run_dir, name, session):
    if not NAME_RE.match(name):
        raise Refused("a trace name is letters, digits, - and _ (at most 64)")
    if session is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", session):
        raise Refused("--session: a session label (letters, digits, _ . : -)")
    tmp = os.path.join(tdir(run_dir), ".active.tmp")
    with open(tmp, "w") as f:
        json.dump({"name": name, "session": session, "started": time.time()}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, active_path(run_dir))
    print(f"trace '{name}' recording" + (f" session {session}" if session else " every client") +
          " (the hub picks it up within a second; metacomputer trace stop ends it)")


def stop_trace(run_dir):
    try:
        os.unlink(active_path(run_dir))
        print("trace stopped")
    except FileNotFoundError:
        print("no trace is recording")


def delete_trace(run_dir, name):
    """Deletes the one trace it's named (a checked name), never one it found by listing."""
    if not NAME_RE.match(name or ""):
        raise Refused("a trace name is letters, digits, - and _")
    os.unlink(os.path.join(tdir(run_dir), name + ".jsonl"))
    print(f"deleted trace '{name}'")


def list_traces(run_dir):
    d = tdir(run_dir)
    act = Recorder(run_dir).active()
    for f in sorted(os.listdir(d)):
        if f.endswith(".jsonl"):
            st = os.lstat(os.path.join(d, f))
            n = f[:-6]
            print(f"{n}  {st.st_size // 1024} KB  {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}"
                  + ("  (recording)" if act and act.get("name") == n else ""))


def cmd(argv):
    ap = argparse.ArgumentParser(prog="metacomputer trace", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("name"); s.add_argument("--session")
    sub.add_parser("stop"); sub.add_parser("list")
    s = sub.add_parser("show"); s.add_argument("name"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("delete"); s.add_argument("name")
    s = sub.add_parser("export"); s.add_argument("name"); s.add_argument("--out"); s.add_argument("--url")
    s.add_argument("--playwright", action="store_true", help="(the only format)")
    a = ap.parse_args(argv)
    run_dir = os.environ.get("MCOMP_RUN_DIR") or ""
    if not os.path.isabs(run_dir):
        raise Refused("no run folder for this VM (run this through `metacomputer trace`)")
    d = tdir(run_dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if a.cmd == "start":
        start_trace(run_dir, a.name, a.session)
    elif a.cmd == "stop":
        stop_trace(run_dir)
    elif a.cmd == "list":
        list_traces(run_dir)
    elif a.cmd == "show":
        recs = load(run_dir, a.name)
        calls = [r for r in recs if r.get("kind") == "call"]
        summary = {"calls": len(calls), "reads": sum(1 for r in recs if r.get("kind") == "read"),
                   "errors": sum(1 for r in recs if r.get("kind") == "error"),
                   "tools": sorted({str(r.get("tool")) for r in calls})}
        print(json.dumps(summary) if a.json else
              f"{a.name}: {summary['calls']} calls, {summary['reads']} reads, {summary['errors']} errors; "
              f"tools: {', '.join(summary['tools'])}")
    elif a.cmd == "delete":
        delete_trace(run_dir, a.name)
    elif a.cmd == "export":
        if a.url and not URL_TEXT_RE.match(a.url):
            raise Refused("--url: an http:// or https:// URL")
        code = Exporter(load(run_dir, a.name), url=a.url).run(a.name)
        if a.out:
            out = os.path.abspath(os.path.expanduser(a.out))
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(code)
            print(out)
        else:
            sys.stdout.write(code)
    return 0


def main(argv):
    try:
        return cmd(argv)
    except Refused as e:
        print(f"metacomputer trace: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"metacomputer trace: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
