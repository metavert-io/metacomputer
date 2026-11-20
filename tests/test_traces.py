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

hub = open(os.path.join(LAB, "lib", "mcp_hub.py")).read()
disp = hub[hub.index("def on_tools_call("):hub.index("def session_for(")]
check("the hub records a call after every guard and pacing, as it goes to the driver",
      disp.index("err, note = self.chrome_guard(name, args)") < disp.index("self.trace_call(name, args, sess)")
      and disp.index("pace_note = self.pace(name, args, sess)") < disp.index("self.trace_call(name, args, sess)"))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
