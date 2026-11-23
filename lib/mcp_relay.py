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

It is the last hop on the Mac, and signing a record can take a while, so it looks for a handoff once more right
before it forwards each acting call (under the dispatch lock, owners.acting(), held across the write): while the
user has the computer (`metacomputer handoff`) it answers the call with the hub's refusal itself.
"""
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actionlog  # noqa: E402
import mcp_hub  # noqa: E402  (READ_ONLY, the handoff refusal)
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
TOO_COMPLEX = "the VM's answer was over the hub's value or depth limit; the hub answered the call with an error"
# The ids the hub gives its requests (mcp_hub.HUB_ID_RE): an answer too complex to parse is recorded for the request
# its top-level "id" names (wirelimits.top_level_id), the one the hub answers; nested ids never count.
HUB_ID_RE = re.compile(r"c\d{1,9}\.\d{1,12}|mcomp-hub-\d{1,12}")
# A message to the VM must be taken within this long (the hub's own WRITE_TIMEOUT_S); one that isn't ends the
# connection: a VM that stops reading can't hold the relay, nor the dispatch lock an acting call is sent under.
WRITE_TIMEOUT_S = float(os.environ.get("MCOMP_RELAY_WRITE_TIMEOUT_S") or 30)
STALLED = ("The virtual computer didn't take this call within {} s, so the action-log relay closed its connection "
           "(the call wasn't sent whole). Check `metacomputer status`; after a reconnect, element tokens from earlier "
           "reads are stale.")


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
    up = proc.stdin.fileno()
    os.set_blocking(up, False)          # writes go through to_vm(), with a deadline

    def to_vm(data):
        """data to the VM within WRITE_TIMEOUT_S: True, or False when it isn't taken (the connection is then ended)."""
        view, end = memoryview(data), time.monotonic() + WRITE_TIMEOUT_S
        while view:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                if not select.select([], [up], [], left)[1]:
                    continue
                view = view[os.write(up, view):]
            except BlockingIOError:
                continue
            except (OSError, ValueError):
                break
        if not view:
            return True
        try:
            proc.kill()                 # a partial message can't be taken back: this connection is over
        except OSError:
            pass
        return False
    pending = {}            # json(id) -> tool
    clients = {}            # json(id) -> the hub's note on whose call it is
    plock = threading.Lock()
    out = sys.stdout.buffer
    olock = threading.Lock()            # the relay's own answers and the VM's go out on the same stream

    def to_client(data):
        with olock:
            out.write(data)
            out.flush()

    def refuse(mid, text):
        to_client((json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}], "isError": True,
            "structuredContent": mcp_hub.HANDOFF_REFUSED}}) + "\n").encode())

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
                except wirelimits.LimitError:
                    msg = None                       # too complex: the hub answers the call its top-level id names
                    key = wirelimits.top_level_id(line)              # with an error, and the log says so
                    if isinstance(key, str) and HUB_ID_RE.fullmatch(key):
                        with plock:
                            tool = pending.pop(json.dumps(key), None)
                        if tool:
                            log.result(key, tool, {"error": {"message": TOO_COMPLEX}})
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and "method" not in msg and "id" in msg:
                    key = json.dumps(msg["id"])
                    with plock:
                        tool = pending.pop(key, None)
                    if tool:
                        log.result(msg["id"], tool, msg)
                try:
                    to_client(line)
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
                to_client((json.dumps({"jsonrpc": "2.0", "method": CLOSING,
                                       "params": {"reason": why, "limit_mb": MAX_MSG >> 20}}) + "\n").encode())
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
                name = p.get("name", "")
                client = clients.pop(json.dumps(msg["id"]), None)
                acting = not isinstance(name, str) or name not in mcp_hub.READ_ONLY
                h = owners.open_handoff() if acting else None
                if h:                       # the user has the computer: not recorded as a call, nothing sent
                    text = owners.handoff_refusal(h)
                    log.refused(str(name), p.get("arguments") or {}, text, client=client)
                    refuse(msg["id"], text)
                    continue
                with plock:
                    pending[json.dumps(msg["id"])] = name
                log.call(msg["id"], name, p.get("arguments") or {}, client=client)
                if acting:
                    # Signing the record can take a while: look again right before it goes, holding the dispatch
                    # lock across the write, so a handoff that opened meanwhile stops it here.
                    sent = None
                    try:
                        with owners.acting():
                            sent = to_vm(line)   # bounded: the lock is let go of within WRITE_TIMEOUT_S
                        if sent:
                            continue
                        text = STALLED.format(int(WRITE_TIMEOUT_S))
                    except owners.HandoffOpen as e:
                        text = owners.handoff_refusal(e.rec)
                    except (OSError, TimeoutError):
                        text = ("The action-log relay couldn't check whether the user has the computer (a handoff "
                                "being opened?), so it didn't pass the call on. Check `metacomputer handoff status`.")
                    with plock:
                        pending.pop(json.dumps(msg["id"]), None)
                    log.result(msg["id"], name, {"error": {"message": text}})
                    if sent is False:       # the VM stopped taking messages: fail the call, end the connection
                        to_client((json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "content": [{"type": "text", "text": text}], "isError": True}}) + "\n").encode())
                        break
                    refuse(msg["id"], text)
                    continue
        if not to_vm(line):
            break
    client_done.set()
    try:
        proc.stdin.close()          # nothing was ever buffered in it: every write went through to_vm()
    except (OSError, ValueError):
        pass
    rc = proc.wait()
    t.join(timeout=5)
    finish()
    return rc


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):
        print(__doc__)          # usage only: no log key is made, nothing is started
        sys.exit(0)
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
