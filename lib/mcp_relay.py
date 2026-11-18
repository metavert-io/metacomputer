#!/usr/bin/env python3
"""stdio relay that records a driver connection in the signed action log.

`metacomputer mcp` runs this instead of plain ssh when ACTION_LOG=on:

  mcp_relay.py ssh -T ...ssh arguments...

Messages pass through unchanged, in order. tools/call requests and their
responses are recorded (actionlog.py). Notes from the hub (lib/mcp_hub.py) are
recorded and never passed on to the driver:
- `notifications/metacomputer/client` names the client (a Claude Code session,
  `metacomputer call`, a script) of the tools/call that follows it, since one hub
  connection carries every client's calls;
- `notifications/metacomputer/refused` is a call the hub refused.
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
import wirelimits  # noqa: E402
try:
    import events  # noqa: E402  (the optional event stream, docs/events.md)
except Exception:  # never let it stop the relay
    events = None

REFUSED = "notifications/metacomputer/refused"
CLOSING = "notifications/metacomputer/closing"     # to the client: why the relay is closing the connection
MAX_MSG = 64 << 20          # one message either way (the VM is untrusted: a line must end within this)
CLIENT = "notifications/metacomputer/client"
OURS = "notifications/metacomputer/"
DROPPED = "the connection to the VM closed before this call finished"


def session_info():
    pid = os.getpid()
    table = owners.ps_table()
    parent = owners.full_command(os.getppid())
    via = ("hub" if "mcp_hub" in parent else "claude-code" if "mcp_lazy" in parent
           else "call" if "mcp_client" in parent or "mcomp_sdk" in parent
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
                          origin="bridge", exempt=True)      # every tool call: the family limit doesn't apply
    return listener


def main(cmd):
    log = actionlog.ActionLog(session_info(), listener=actions_listener())
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    pending = {}            # json(id) -> tool
    clients = {}            # json(id) -> the hub's note on whose call it is
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

    client_done = threading.Event()

    def downstream():           # VM -> client
        why = None
        try:
            while True:
                try:
                    line = proc.stdout.readline(MAX_MSG + 1)
                except (OSError, ValueError):
                    break
                if not line:
                    break
                if len(line) > MAX_MSG and not line.endswith(b"\n"):
                    why = "oversize"     # a line from the VM that doesn't end within the limit: drop the connection
                    log.event("protocol_error", reason=f"the VM sent a message over {MAX_MSG >> 20} MB; connection closed")
                    break
                try:
                    msg = wirelimits.loads(line)     # not JSON or nested too deeply: passed on unlogged (the hub skips it)
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and "method" not in msg and "id" in msg:
                    key = json.dumps(msg["id"])
                    with plock:
                        tool = pending.pop(key, None)
                    if tool:
                        log.result(msg["id"], tool, msg)
                try:
                    out.write(line)
                    out.flush()
                except (BrokenPipeError, OSError, ValueError):
                    break
        except Exception as e:   # whatever stops this thread, the connection ends below and the log is signed
            why = why or "error"
            try:
                log.event("protocol_error", reason=f"the relay stopped reading the VM: {type(e).__name__}")
            except Exception:
                pass
        finally:
            closing(why)

    def closing(why):
        if client_done.is_set():
            return               # the client closed first: main() finishes
        # The driver's side ended (or misbehaved): nothing more can happen on this connection. Say why, sign
        # the log's end and exit, so the client sees the end now rather than at its next write.
        try:
            proc.kill()
        except OSError:
            pass
        try:
            if why:
                out.write((json.dumps({"jsonrpc": "2.0", "method": CLOSING,
                                       "params": {"reason": why, "limit_mb": MAX_MSG >> 20}}) + "\n").encode())
            out.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finish()
        os._exit(0 if not why else 1)

    t = threading.Thread(target=downstream, daemon=True)
    t.start()
    inp = sys.stdin.buffer
    while True:                     # client -> VM
        line = inp.readline(MAX_MSG + 1)
        if not line:
            break
        if len(line) > MAX_MSG and not line.endswith(b"\n"):
            log.event("protocol_error", reason=f"the client sent a message over {MAX_MSG >> 20} MB; connection closed")
            break
        try:
            msg = wirelimits.loads(line)
        except ValueError:
            msg = None
        if isinstance(msg, dict):
            method = msg.get("method")
            if method == REFUSED:
                p = msg.get("params") or {}
                log.refused(str(p.get("tool", "")), p.get("args") or {}, str(p.get("reason", "")),
                            client=p.get("client") if isinstance(p.get("client"), dict) else None)
                continue
            if method == CLIENT:
                p = msg.get("params") or {}
                if "id" in p and isinstance(p.get("client"), dict) and len(clients) < 10000:
                    clients[json.dumps(p["id"])] = p["client"]
                continue
            if isinstance(method, str) and method.startswith(OURS):
                continue            # anything else of ours never reaches the driver
            if method == "tools/call" and "id" in msg:
                p = msg.get("params") or {}
                with plock:
                    pending[json.dumps(msg["id"])] = p.get("name", "")
                log.call(msg["id"], p.get("name", ""), p.get("arguments") or {},
                         client=clients.pop(json.dumps(msg["id"]), None))
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            break
    client_done.set()
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
