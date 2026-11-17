#!/usr/bin/env python3
"""stdio relay that records a driver connection in the signed action log.

`metacomputer mcp` runs this instead of plain ssh when ACTION_LOG=on:

  mcp_relay.py ssh -T ...ssh arguments...

Messages pass through unchanged, in order. tools/call requests and their
responses are recorded (actionlog.py); a `notifications/metacomputer/refused`
message from Claude Code's bridge is recorded and not passed on.
"""
import json
import os
import signal
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actionlog  # noqa: E402
import owners  # noqa: E402
try:
    import events  # noqa: E402  (the optional event stream, docs/events.md)
except Exception:  # never let it stop the relay
    events = None

REFUSED = "notifications/metacomputer/refused"
DROPPED = "the connection to the VM closed before this call finished"


def session_info():
    pid = os.getpid()
    table = owners.ps_table()
    parent = owners.full_command(os.getppid())
    via = ("claude-code" if "mcp_lazy" in parent else "call" if "mcp_client" in parent
           else "web" if "browser_tools" in parent else "tests" if "run_tests" in parent or "test_" in parent
           else "direct")
    return {"bridge_pid": pid, "claude_pid": owners.claude_of(pid, table), "cwd": os.getcwd(), "via": via,
            "vm": os.environ.get("MCOMP_VM_NAME", ""), "driver_mode": os.environ.get("MCOMP_DRIVER_MODE", "standard"),
            "driver_profile": os.environ.get("MCOMP_DRIVER_PROFILE", ""), "ssh_auth": os.environ.get("MCOMP_SSH_AUTH", "key")}


def actions_listener():
    """With a sink subscribed to the opt-in `actions` family: one event per record written (tool, hashes; never
    arguments or typed text), sent on a background thread so the relay never waits."""
    if events is None:
        return None
    try:
        if not events.wants(events.load_config(), "actions"):
            return None
    except Exception:
        return None
    cwd = os.getcwd()

    def listener(rec, path):
        e = events.action_record_event(rec, path)
        events.emit_async(e["family"], e["type"], e["message"], e["severity"], None, e["data"], project=cwd,
                          origin="bridge")
    return listener


def main(cmd):
    log = actionlog.ActionLog(session_info(), listener=actions_listener())
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    pending = {}            # json(id) -> tool
    plock = threading.Lock()
    out = sys.stdout.buffer

    def finish(*_):
        with plock:
            for key, tool in list(pending.items()):
                log.result(json.loads(key), tool, {"error": {"message": DROPPED}})
            pending.clear()
        log.close()
        if events is not None:
            events.flush_async(3.0)

    def on_signal(signum, _frame):
        finish()
        try:
            proc.terminate()
        except OSError:
            pass
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    def downstream():           # VM -> client
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if isinstance(msg, dict) and "method" not in msg and "id" in msg:
                key = json.dumps(msg["id"])
                with plock:
                    tool = pending.pop(key, None)
                if tool:
                    log.result(msg["id"], tool, msg)
            out.write(line)
            out.flush()

    t = threading.Thread(target=downstream, daemon=True)
    t.start()
    for line in sys.stdin.buffer:   # client -> VM
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        if isinstance(msg, dict):
            if msg.get("method") == REFUSED:
                p = msg.get("params") or {}
                log.refused(str(p.get("tool", "")), p.get("args") or {}, str(p.get("reason", "")))
                continue
            if msg.get("method") == "tools/call" and "id" in msg:
                p = msg.get("params") or {}
                with plock:
                    pending[json.dumps(msg["id"])] = p.get("name", "")
                log.call(msg["id"], p.get("name", ""), p.get("arguments") or {})
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            break
    try:
        proc.stdin.close()
    except OSError:
        pass
    rc = proc.wait()
    t.join(timeout=5)
    finish()
    return rc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
