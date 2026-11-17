#!/usr/bin/env python3
"""Offline checks for the signed action log (lib/actionlog.py, lib/mcp_relay.py).

No VM needed: the relay is pointed at a fake MCP server. Run:
    python3 tests/test_actionlog.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "lib"))
import actionlog  # noqa: E402

FAKE_SERVER = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" in m and "method" in m:
        r = {"content": [{"type": "text", "text": "done " + m["method"]}]}
        if m["method"] == "tools/call" and m["params"]["name"] == "boom":
            r["isError"] = True
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''

results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


def lines(p):
    return open(p).read().splitlines()


def write_lines(p, ls):
    open(p, "w").write("\n".join(ls) + "\n")


def verify(path, base, pub):
    return actionlog.verify_file(path, base, pub)


def main():
    td = tempfile.mkdtemp(prefix="mcomp-alog-")
    try:
        base = os.path.join(td, "actions")
        key = os.path.join(td, "keys", "action-log")
        cfg = {"on": True, "keep_text": False, "dir": base, "key": key}

        # 1. a clean session: 45 calls, closed normally
        log = actionlog.ActionLog({"bridge_pid": 111, "cwd": "/example/project"}, cfg)
        for i in range(45):
            log.call(i, "type_text", {"pid": 42, "text": f"hunter{i}"})
            log.result(i, "type_text", {"result": {"content": [{"type": "text", "text": "ok"}]}})
        log.refused("set_value", {"value": "secret-value"}, "refused for the test")
        log.close()
        path, pub = log.path, key + ".pub"
        v = verify(path, base, pub)
        check("clean log verifies", v["ok"] and v["final"] and v["unsigned"] == 0, json.dumps(v)[:300])
        recs = [json.loads(x) for x in lines(path)]
        cps = [r for r in recs if r["type"] == "checkpoint"]
        check("checkpoints every 20 records plus a final one", len(cps) >= 5 and cps[-1]["final"], str(len(cps)))
        raw = open(path).read()
        check("typed text and values are redacted", "hunter" not in raw and "secret-value" not in raw)
        check("file is private (0600)", oct(os.stat(path).st_mode & 0o777) == "0o600", oct(os.stat(path).st_mode))

        # 2. edit one record
        ls = lines(path)
        r = json.loads(ls[5]); r["tool"] = "click"; ls2 = ls[:]; ls2[5] = json.dumps(r)
        t = os.path.join(base, "edited.jsonl"); write_lines(t, ls2)
        v = verify(t, base, pub)
        check("an edited record is caught", not v["ok"] and any("changed" in p for p in v["problems"]), v["problems"][:2])

        # 3. edit a record and recompute its hash (the chain still breaks)
        r = json.loads(ls[5]); r["tool"] = "click"; r["hash"] = actionlog.record_hash(r)
        ls3 = ls[:]; ls3[5] = json.dumps(r); write_lines(t, ls3)
        v = verify(t, base, pub)
        check("an edited record with a fixed-up hash is caught", not v["ok"] and any("chain" in p for p in v["problems"]))

        # 4. delete a record
        write_lines(t, ls[:7] + ls[8:])
        v = verify(t, base, pub)
        check("a deleted record is caught", not v["ok"])

        # 5. cut the end off (drop the final signature)
        last_cp = max(i for i, x in enumerate(ls) if json.loads(x)["type"] == "checkpoint" and not json.loads(x)["final"])
        write_lines(t, ls[:last_cp + 1])
        v = verify(t, base, pub)
        check("a cut-short log verifies but shows no final signature", v["ok"] and not v["final"], json.dumps(v)[:200])

        # 6. the whole log rewritten and re-signed with another key
        other = os.path.join(td, "keys", "other")
        actionlog.ensure_key(other)
        cfg2 = dict(cfg, key=other)
        forged = actionlog.ActionLog({"bridge_pid": 222, "cwd": "/example/project"}, cfg2)
        forged.call(1, "click", {"x": 1, "y": 2})
        forged.close()
        v = verify(forged.path, base, pub)
        check("a log signed with another key is caught", not v["ok"] and any("signature" in p for p in v["problems"]))

        # 7. a copy under another name (task folder) still verifies
        cp = os.path.join(td, "task-copy.jsonl"); shutil.copy(path, cp)
        v = verify(cp, base, pub)
        check("a copied log still verifies", v["ok"] and v["final"])

        # 8. the CLI
        env = dict(os.environ, MCOMP_ACTION_LOG_DIR=base, MCOMP_ACTION_LOG_KEY=key)
        out = subprocess.run([sys.executable, os.path.join(LAB, "lib", "actionlog.py"), "verify", "--all"],
                             capture_output=True, text=True, env=env)
        check("CLI verify --all fails when a file is tampered", out.returncode == 1 and "TAMPERED" in out.stdout, out.stdout[-300:])
        os.remove(forged.path); os.remove(t)
        out = subprocess.run([sys.executable, os.path.join(LAB, "lib", "actionlog.py"), "verify", "--all"],
                             capture_output=True, text=True, env=env)
        check("CLI verify --all passes on clean logs", out.returncode == 0, out.stdout[-300:])

        # 9. the relay passes messages through unchanged and logs calls
        fake = os.path.join(td, "fake_server.py"); open(fake, "w").write(FAKE_SERVER)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "click", "arguments": {"x": 5}}},
                {"jsonrpc": "2.0", "method": "notifications/metacomputer/refused",
                 "params": {"tool": "set_value", "args": {"value": "pw"}, "reason": "test refusal"}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "boom", "arguments": {}}}]
        env = dict(os.environ, MCOMP_ACTION_LOG="on", MCOMP_ACTION_LOG_DIR=base, MCOMP_ACTION_LOG_KEY=key)
        p = subprocess.run([sys.executable, os.path.join(LAB, "lib", "mcp_relay.py"), sys.executable, fake],
                           input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, env=env, timeout=30)
        replies = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
        check("relay passes every request through and every reply back", [r["id"] for r in replies] == [1, 2, 3], p.stdout[:300])
        newest = actionlog.files(base)[-1]
        recs = [json.loads(x) for x in lines(newest)]
        kinds = [r["type"] for r in recs]
        check("relay logs calls, results and the bridge's refusal",
              kinds.count("call") == 2 and kinds.count("result") == 2 and "refused" in kinds and kinds[-2:] == ["end", "checkpoint"],
              str(kinds))
        boom = [r for r in recs if r["type"] == "result" and r["tool"] == "boom"]
        check("relay records a tool error", boom and boom[0]["result"]["error"] is True)
        check("relay's log verifies", verify(newest, base, key + ".pub")["ok"])
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
