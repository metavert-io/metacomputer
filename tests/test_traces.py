#!/usr/bin/env python3
"""Traces and the Playwright export (lib/traces.py), no VM: the hub's recorder (on only while a trace is active, one
session or all, capped, never raising), rows from reads resolving element tokens, the translation of clicks, typing,
fills, keys, checks and address-bar navigations, other apps left out, and page text that tries to break out of the
generated code staying inside escaped string literals."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LAB, "lib"))
import traces  # noqa: E402

TD = tempfile.mkdtemp(prefix="mcomp-traces-")
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"   [{str(detail)[:400]}]"))


def cli(*args, run=TD):
    r = subprocess.run([sys.executable, os.path.join(LAB, "lib", "traces.py"), *args], capture_output=True, text=True,
                       env=dict(os.environ, MCOMP_RUN_DIR=run), timeout=30)
    return r.returncode, r.stdout + r.stderr


def read(sid, rows):
    md = "\n".join(f'  - [{i}] {role} "{name}" [actions=[]]' for i, (role, name) in rows.items())
    return {"content": [{"type": "text", "text": "window_id=1 snapshot_id=" + sid}],
            "structuredContent": {"snapshot_id": sid, "tree_markdown": md, "window_title": "Shop"}}


# --- the recorder --------------------------------------------------------------------------------------------
rec = traces.Recorder(TD)
rec.call("click", {"x": 1, "y": 2}, "s1", "Firefox")
check("nothing is recorded while no trace is active", not os.path.exists(os.path.join(TD, "traces")))
rc, out = cli("start", "shop")
check("trace start", rc == 0 and "recording" in out, out)
rec = traces.Recorder(TD)
rec.call("click", {"element_token": "s0000000a:3", "pid": 9}, "s1", "Firefox")
rec.result("get_window_state", read("s0000000a", {3: ("push button", "Buy now")}), "s1")
lines = open(os.path.join(TD, "traces", "shop.jsonl")).read().splitlines()
check("while it is: calls and the rows of reads go into the trace", len(lines) == 2
      and json.loads(lines[1])["rows"]["3"] == ["push button", "Buy now"], lines)
check("…written private (0600)", os.stat(os.path.join(TD, "traces", "shop.jsonl")).st_mode & 0o777 == 0o600)
rc, out = cli("start", "one", "--session", "s2")
rec = traces.Recorder(TD)
rec.call("click", {"x": 1, "y": 1}, "s1", "Firefox")
rec.call("click", {"x": 2, "y": 2}, "s2", "Firefox")
one = open(os.path.join(TD, "traces", "one.jsonl")).read().splitlines()
check("--session: only that session's calls", len(one) == 1 and json.loads(one[0])["session"] == "s2", one)
with open(os.path.join(TD, "traces", "active.json"), "w") as f:
    json.dump({"name": "old", "started": time.time() - 25 * 3600}, f)
rec = traces.Recorder(TD)
check("a trace started more than 24 hours ago is over", rec.active() is None)
with open(os.path.join(TD, "traces", "active.json"), "w") as f:
    f.write("{not json")
check("an active file that can't be read: no trace, no exception", traces.Recorder(TD).active() is None)
rc, out = cli("start", "../evil")
check("a trace name with a path in it is refused", rc == 3, out)
rc, out = cli("start", "big")
rec = traces.Recorder(TD)
with open(os.path.join(TD, "traces", "big.jsonl"), "w") as f:
    f.truncate(traces.MAX_BYTES + 1)
rec.call("click", {"x": 1, "y": 1}, "s1", "Firefox")
check("a trace past 20 MB isn't written to any more", os.path.getsize(os.path.join(TD, "traces", "big.jsonl"))
      == traces.MAX_BYTES + 1)
rc, out = cli("stop")
check("trace stop", rc == 0 and not os.path.exists(os.path.join(TD, "traces", "active.json")), out)


# --- the export ----------------------------------------------------------------------------------------------
def export(recs, url=None, name="t"):
    return traces.Exporter(recs, url=url).run(name)


def call(tool, args, app="Firefox"):
    return {"kind": "call", "tool": tool, "args": args, "app": app}


code = export([
    {"kind": "read", "snapshot": "s0000000b", "rows": {"5": ["entry", "Email"], "6": ["push button", "Continue"],
                                                       "7": ["check box", "Remember me"]}},
    call("click", {"element_token": "s0000000b:5"}),
    call("type_text", {"text": "ada@example.com"}),
    call("click", {"element_token": "s0000000b:7"}),
    call("press_key", {"key": "Tab"}),
    call("run_steps", {"steps": [{"click": {"element_token": "s0000000b:6"}, "expect": [{"text": "Check your email"}]},
                                 {"wait_for": {"role": "heading", "name": "Welcome"}}]}),
    call("hotkey", {"keys": ["ctrl", "l"]}), call("type_text", {"text": "https://shop.example.com/cart"}),
    call("press_key", {"key": "Return"}),
    call("click", {"x": 120, "y": 340}),
    call("set_value", {"role": "combo box", "name": "Size", "value": "M"}),
    call("click", {"x": 5, "y": 5}, app="Mousepad"),
], url="https://shop.example.com/")
check("a token resolves to its row's role and name; click then typing is one fill",
      'await page.getByRole("textbox", { name: "Email" }).fill("ada@example.com");' in code, code)
check("check boxes, keys, buttons", 'page.getByRole("checkbox", { name: "Remember me" }).click()' in code
      and 'page.keyboard.press("Tab")' in code and 'page.getByRole("button", { name: "Continue" }).click()' in code, code)
check("a step's expect and a wait_for become assertions", 'await expect(page.getByText("Check your email")).toBeVisible();'
      in code and 'await expect(page.getByRole("heading", { name: "Welcome" })).toBeVisible();' in code, code)
check("ctrl+L, an address, Return: page.goto", 'await page.goto("https://shop.example.com/cart");' in code
      and "keyboard.type(\"https://shop" not in code, code)
check("a pixel click stays coordinates, marked", "// A pixel click, aimed from a screenshot: replace it with a locator."
      in code and "await page.mouse.click(120, 340);" in code, code)
check("set_value becomes a fill", 'page.getByRole("combobox", { name: "Size" }).fill("M")' in code, code)
check("a step in another app is left out, and counted", "mouse.click(5, 5)" not in code and
      "left out (other apps, or not translatable): 1." in code, code)
check("the start URL comes first", code.index('page.goto("https://shop.example.com/")') < code.index("Email"), code)

evil = ['x"); require("child_process").exec("id"); ("', "line\nbreak */ code();", "  sep  ", "`${process.env}`",
        "</script><script>alert(1)</script>", "\\\" \\\\ ", "*/ evil(); /*"]
code = export([{"kind": "read", "snapshot": "s0000000c", "rows": {str(i): ["push button", n] for i, n in enumerate(evil)}}]
              + [call("click", {"element_token": f"s0000000c:{i}"}) for i in range(len(evil))]
              + [call("type_text", {"text": n}) for n in evil]
              + [call("run_steps", {"steps": [{"wait_for": {"text": n}} for n in evil]})], name="x")
body = "\n".join(l for l in code.splitlines() if l.startswith("  "))
strings = re.findall(r'"(?:[^"\\]|\\.)*"', body)
outside = re.sub(r'"(?:[^"\\]|\\.)*"', '""', body)
check("page text that tries to end the string, open a comment or start a template stays inside escaped literals",
      all(json.loads(s) in evil or json.loads(s) in ("button",) for s in strings)
      and not re.search(r"require|child_process|evil|alert|process\.env|\$\{|`", outside)
      and " " not in code and " " not in code, outside[:500])
check("…and no page text appears in a comment", all("child_process" not in l and "evil" not in l
                                                     for l in code.splitlines() if l.strip().startswith("//")), code)
check("the generated test parses as JavaScript (node --check, when node is here)",
      subprocess.run(["node", "--check", "-"], input=code.replace("import { test, expect } from '@playwright/test';",
                                                                    "const test = () => {}, expect = () => {};"),
                     capture_output=True, text=True).returncode == 0 if subprocess.run(["which", "node"],
                                                                                         capture_output=True).returncode == 0
      else True)

rc, out = cli("start", "e2")
traces.Recorder(TD).call("click", {"element_token": "s0000000d:1"}, "s1", "Google-chrome")
cli("stop")
rc, out = cli("export", "e2", "--url", "javascript:alert(1)")
check("export --url takes only http(s)", rc == 3, out)
rc, out = cli("export", "e2", "--out", os.path.join(TD, "e2.spec.ts"))
check("export --out writes the file", rc == 0 and os.path.exists(os.path.join(TD, "e2.spec.ts")), out)
rc, out = cli("list")
check("trace list names them", rc == 0 and "e2" in out and "shop" in out, out)
rc, out = cli("delete", "e2")
check("trace delete", rc == 0 and not os.path.exists(os.path.join(TD, "traces", "e2.jsonl")), out)

# A click by element token alone (no pid): the hub records the app of the read the token came from.
import types  # noqa: E402
import mcp_hub  # noqa: E402
got = []
fake = types.SimpleNamespace(tracer=types.SimpleNamespace(wants=lambda s: True, call=lambda n, a, s, app: got.append(app)),
                             snapshots={"s0000000e": (77, 5, "firefox")}, app_of={77: "firefox"}, window_pid={},
                             note=lambda *a, **k: None)
mcp_hub.Hub.trace_call(fake, "click", {"element_token": "s0000000e:41"}, "s1")
mcp_hub.Hub.trace_call(fake, "click", {"element_token": "s0000000f:2"}, "s1")
check("a call by element token alone is recorded with its window's app (from the read the token came from); an "
      "unknown snapshot stays without one", got == ["firefox", ""], got)
# …and a trace recorded before that (no app on such calls) still exports, from the read's window title.
old = [{"kind": "read", "snapshot": "s00000006", "title": "Larkspur Supply \u2014 Mozilla Firefox",
        "rows": {"41": ["link", "Account"]}},
       {"kind": "call", "tool": "click", "app": "", "args": {"element_token": "s00000006:41"}},
       {"kind": "read", "snapshot": "s00000007", "title": "notes.txt - Mousepad", "rows": {"3": ["push button", "Save"]}},
       {"kind": "call", "tool": "click", "app": "", "args": {"element_token": "s00000007:3"}}]
ex = traces.Exporter(old)
code = ex.run("old")
check("an older trace's token-only click in a browser window is exported (from the window title); one in another "
      "app is still left out", 'getByRole("link", { name: "Account" })' in code and "Save" not in code
      and ex.skipped == 1, code)
rep = [{"kind": "read", "snapshot": "s00000009", "title": "Invoices \u2014 Mozilla Firefox", "whole": True,
        "rows": {"20": ["link", "Download invoice"], "37": ["document web", "Invoices"],
                 "50": ["link", "Download invoice summary"], "55": ["link", "Download invoice"],
                 "61": ["link", "Download invoice"], "67": ["link", "Download invoice"], "70": ["link", "Help"]}},
       {"kind": "call", "tool": "click", "app": "firefox", "args": {"element_token": "s00000009:61"}},
       {"kind": "call", "tool": "click", "app": "firefox", "args": {"element_token": "s00000009:70"}}]
code = traces.Exporter(rep).run("rep")
check("several elements with the same role and exact name, in a read of the whole window: the one clicked is picked "
      "by its place on the page (.nth with exact: true, counted from the page's document, not the browser's own "
      "controls, and not counting a longer name that contains it); a unique one has neither",
      'getByRole("link", { name: "Download invoice", exact: true }).nth(1).click()' in code
      and 'getByRole("link", { name: "Help" }).click()' in code, code)
part = [dict(rep[0], whole=False, snapshot="s0000000a"), {"kind": "call", "tool": "click", "app": "firefox",
                                                           "args": {"element_token": "s0000000a:61"}}]
code = traces.Exporter(part).run("part")
check("…but from a read that listed only part of the window (a query, a diff), no .nth: a comment says to add one",
      "}).nth(" not in code and "listed only part of the window" in code
      and 'getByRole("link", { name: "Download invoice" }).click()' in code, code)
txt = [{"kind": "read", "snapshot": "s0000000d", "title": "x \u2014 Mozilla Firefox", "whole": True,
        "rows": {"1": ["document web", "x"], "2": ["paragraph", "A"], "3": ["section", "A"], "4": ["paragraph", "A"]}},
       {"kind": "call", "tool": "click", "app": "firefox", "args": {"element_token": "s0000000d:4"}}]
code = traces.Exporter(txt).run("txt")
check("a text locator (getByText matches any element with that text) never gets .nth from a count by role: a comment",
      "}).nth(" not in code and ").nth(" not in code.split("test(")[1].split("\n", 2)[2].split("//")[0]
      and "More than one element has this text" in code and 'page.getByText("A").click()' in code, code)
nav = [{"kind": "call", "tool": "press_key", "app": "firefox", "args": {"key": "l", "modifiers": ["ctrl"]}},
       {"kind": "call", "tool": "type_text", "app": "firefox", "args": {"text": "localhost:8000/admin/"}},
       {"kind": "call", "tool": "press_key", "app": "firefox", "args": {"key": "enter"}},
       {"kind": "call", "tool": "press_key", "app": "firefox", "args": {"key": "a", "modifiers": ["ctrl", "shift"]}},
       {"kind": "call", "tool": "press_key", "app": "firefox", "args": {"key": "a", "modifiers": ["hyper"]}}]
code = traces.Exporter(nav).run("nav")
check("press_key's modifiers field counts: ctrl+L, an address, Enter is a page.goto (not a press of \"l\" and typing); "
      "ctrl+shift+a is Control+Shift+a; an unknown modifier is left out, never pressed without it",
      'page.goto("http://localhost:8000/admin/")' in code and 'press("l")' not in code and 'press("Control+Shift+a")' in code
      and 'press("a")' not in code, code)
code = traces.Exporter([dict(nav[0]), {"kind": "call", "tool": "type_text", "app": "firefox", "args": {"text": "example.com/a"}},
                        dict(nav[2])]).run("bare")
check("an address typed without a scheme: https:// for another host, with a comment to check it",
      'page.goto("https://example.com/a")' in code and "the browser chose the scheme" in code, code)
code = traces.Exporter([{"kind": "call", "tool": "press_key", "app": "firefox",
                         "args": {"key": "a", "modifiers": ["ctrl", "ctrl", "ctrl", "ctrl", "shift"]}}]).run("many")
check("every modifier counts, however many (repeats once): ctrl x4 + shift is Control+Shift+a",
      'press("Control+Shift+a")' in code, code)
check("addresses are checked, not only their shape: 999.999.999.999, [abc] and a port over 65535 aren't navigations",
      [traces.bare_address(t) for t in ("999.999.999.999", "[abc]", "localhost:70000", "example.com:0")] == [None] * 4
      and traces.bare_address("10.0.0.5:8080/x") == ("https://10.0.0.5:8080/x", False)
      and traces.bare_address("127.0.0.1:8000") == ("http://127.0.0.1:8000", True)
      and traces.bare_address("[::1]:9000/") == ("http://[::1]:9000/", True), [traces.bare_address(t) for t in ("10.0.0.5:8080/x",)])
rec = traces.Recorder(TD)
cli("start", "whole")
res = {"structuredContent": {"snapshot_id": "s0000000b", "window_title": "x \u2014 Mozilla Firefox",
                             "tree_markdown": '- [3] link "A" [actions=[jump]]'}}
rec.result("get_window_state", res, "s1", {"pid": 5, "window_id": 6, "include_screenshot": False})
rec.result("get_window_state", dict(res, structuredContent=dict(res["structuredContent"], snapshot_id="s0000000c")),
           "s1", {"pid": 5, "window_id": 6, "query": "A"})
rec.result("get_window_state", dict(res, structuredContent=dict(res["structuredContent"], snapshot_id="s0000000e",
                                                                truncated=True)), "s1", {"pid": 5, "window_id": 6})
rec.result("get_window_state", dict(res, structuredContent=dict(res["structuredContent"], snapshot_id="s0000000f")),
           "s1", {"pid": 5, "window_id": 6, "include_accessibility_tree": True})
cli("stop")
reads = [json.loads(l) for l in open(os.path.join(TD, "traces", "whole.jsonl")) if '"read"' in l]
check("the recorder marks a read as whole only without the driver's filters (a query read isn't) and when the "
      "driver didn't cut it short (truncated); asking for the tree explicitly is still whole",
      [r.get("whole") for r in reads] == [True, False, False, True], reads)
hub = open(os.path.join(LAB, "lib", "mcp_hub.py")).read()
disp = hub[hub.index("def on_tools_call("):hub.index("def session_for(")]
check("the hub records a call after every guard and pacing, as it goes to the driver",
      disp.index("err, note = self.chrome_guard(name, args)") < disp.index("self.trace_call(name, args, sess)")
      and disp.index("pace_note = self.pace(name, args, sess)") < disp.index("self.trace_call(name, args, sess)"))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
