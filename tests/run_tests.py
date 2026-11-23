#!/usr/bin/env python3
"""metacomputer GUI smoke suite: `metacomputer test [A B C D E F] [--run-id ID]`.

Each test drives the Linux guest only through Cua Driver's MCP tools (the
same bridge Claude Code uses), records itself, and verifies the outcome
independently. Writes results/<run>/summary.{json,md}.
"""
import importlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LAB, "lib"))
sys.path.insert(0, HERE)

from harness import run_test  # noqa: E402

TESTS = {
    "A": "test_a_observe",
    "B": "test_b_calculator",
    "C": "test_c_browser",
    "D": "test_d_libreoffice",
    "E": "test_e_multiapp",
    "F": "test_f_chrome",
}


def main(argv):
    import strictargs           # its command line, by metacomputer's rules (lib/ is on the path), before anything runs
    strictargs.expect(argv, ["[TEST...] [--run-id ID]"], prog="metacomputer test", usage=__doc__)
    run_id = time.strftime("run-%Y%m%d-%H%M%S")
    sel = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--run-id":
            if i + 1 >= len(argv) or "--run-id" in argv[:i]:
                print("--run-id takes one value, once"); return 2
            run_id = argv[i + 1]; i += 2; continue
        if a.upper() in TESTS:
            sel.append(a.upper())
        elif a.lower() != "all":
            print(f"unknown test {a!r}; choose from {', '.join(TESTS)}"); return 2
        i += 1
    sel = sel or list(TESTS)
    st = subprocess.run([os.path.join(LAB, "bin", "metacomputer"), "status", "--json"], capture_output=True, text=True)
    try:
        status = json.loads(st.stdout)
    except json.JSONDecodeError:
        status = {}
    if not status.get("ready"):
        print("metacomputer: the VM is not ready (metacomputer status). Start it with: metacomputer start")
        return 1
    print(f"metacomputer test run {run_id}: {' '.join(sel)}")
    results = []
    for tid in sel:
        mod = importlib.import_module(TESTS[tid])
        results.append(run_test(mod.run, run_id, tid, mod.TITLE, mod.TASK))
    out_dir = os.path.join(LAB, "results", run_id)
    os.makedirs(out_dir, exist_ok=True)
    summary = {"run_id": run_id, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "guest": {k: status.get("guest", {}).get(k) for k in ("driver_version", "resolution", "hostname")},
               "mcp": status.get("mcp"), "results": results}
    json.dump(summary, open(os.path.join(out_dir, "summary.json"), "w"), indent=2)
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write(f"# metacomputer test run {run_id}\n\n")
        f.write("| Test | Result | Time | Actions (AT-SPI / input events) | Screenshots | Recording | Checks |\n|---|---|---|---|---|---|---|\n")
        for r in results:
            v = r.get("video") or {}
            rec = f"`{r['recording']}` ({v.get('width')}x{v.get('height')}, {v.get('duration_s')} s)" if r.get("recording") else "none"
            ok = sum(c["passed"] for c in r["checks"])
            f.write(f"| {r['test']}: {r['title']} | **{r['result']}** | {r['elapsed_s']} s | {r['actions']} ({r['a11y_actions']} / {r['input_event_actions']}) "
                    f"| {r['screenshots']} | {rec} | {ok}/{len(r['checks'])} |\n")
        for r in results:
            f.write(f"\n## {r['test']}: {r['title']} — {r['result']}\n\nTask: {r['task']}\n\n")
            for c in r["checks"]:
                f.write(f"- {'✅' if c['passed'] else '❌'} {c['check']}: {c['detail']}\n")
            if r.get("failure"):
                f.write(f"\nFailure: `{r['failure']}`\n")
            if r["errors"]:
                f.write("\nErrors during the run:\n" + "".join(f"- `{e}`\n" for e in r["errors"]))
            if r["recoveries"]:
                f.write("\nRecoveries:\n" + "".join(f"- {e}\n" for e in r["recoveries"]))
    passed = sum(r["result"] == "PASS" for r in results)
    print(f"\n{passed}/{len(results)} passed. Summary: results/{run_id}/summary.md")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
