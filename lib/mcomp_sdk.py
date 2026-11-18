#!/usr/bin/env python3
"""Python SDK for the virtual computer: Cua Driver's tools through the metacomputer
hub, with every guard the hub applies (one controller at a time, the Chrome guard,
bounded mode, the coordinate guard, the action log, events). Stdlib only.

    import sys; sys.path.insert(0, "/path/to/metacomputer/lib")
    from mcomp_sdk import Hub

    with Hub() as h:                                  # this checkout's config/local.env
        win = h.call("list_windows", {"app": "mousepad"}).structured["windows"][0]
        st = h.call("get_window_state", {"pid": win["pid"], "window_id": win["window_id"]})
        print(st.text)

    with Hub("/path/to/test-vm.env", session="report-1") as h:   # another VM; a named driver session
        ...

`Hub(cfg=None, session=None, timeout=120)`: cfg is a settings file (as
MCOMP_CONFIG); session names a driver session that outlives this connection
(element tokens, recordings, a Chrome from browser_prepare), letters, digits,
'.', '_' or '-'. Without one, calls run in the hub's default driver session, which
also lasts while the hub runs. `h.call(tool, args)` returns a ToolResult (.text,
.structured, .images, .is_error, .raw); `h.request(method, params)` sends any MCP
request; `h.tools()` lists the tools; `h.status()` is the hub's own report.

With MCP_HUB=off the same guards run inside this process instead, on a connection
of its own that ends with close().

CLI (what `metacomputer call` runs):
  mcomp_sdk.py call [--session NAME] [--timeout S] [--json | --raw] TOOL ['JSON']
On a terminal, the result's text has control characters escaped (termsafe.py);
--raw prints it exactly, --json prints the whole result as JSON.
"""
import itertools
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

LIB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB)
import mcp_hub  # noqa: E402
import termsafe  # noqa: E402
from mcp_client import ToolResult  # noqa: E402

LAB = os.path.dirname(LIB)
CLI = os.path.join(LAB, "bin", "metacomputer")
HubError = mcp_hub.HubError


def resolve_env(cfg=None):
    """The resolved settings for a VM (what bin/metacomputer exports), from its config file."""
    if cfg is None and os.environ.get("MCOMP_RUN_DIR") and "MCOMP_ACTION_LOG" in os.environ:
        return dict(os.environ)                  # already resolved by bin/metacomputer
    env = dict(os.environ)
    if cfg:
        env["MCOMP_CONFIG"] = os.path.abspath(os.path.expanduser(cfg))
    try:
        r = subprocess.run([CLI, "hub", "env", "--json"], env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HubError(f"could not read the virtual computer's settings ({e})")
    if r.returncode != 0:
        raise HubError(f"could not read the virtual computer's settings: {(r.stderr or r.stdout).strip()[-300:]}")
    env.update(json.loads(r.stdout))
    return env


class Connection:
    """One client connection after its hello: to the hub's socket, or (MCP_HUB=off) to a core in this process.
    on_message(msg) gets everything the hub sends; on_close() runs once when it closes."""

    def __init__(self, sock, core=None, on_message=None, on_close=None):
        self.sock, self.core = sock, core
        self.on_message, self.on_close = on_message, on_close
        self.alive = True
        self.wlock = threading.Lock()
        self.info = {}
        self._f = sock.makefile("rb")

    def send(self, msg):
        data = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
        try:
            with self.wlock:
                self.sock.sendall(data)
            return True
        except OSError:
            return False

    def read_one(self, timeout=None):
        self.sock.settimeout(timeout)
        try:
            line = self._f.readline(mcp_hub.MAX_UPSTREAM_MSG + 1)
        finally:
            self.sock.settimeout(None)
        return json.loads(line) if line else None

    def start(self):
        threading.Thread(target=self._reader, daemon=True, name="mcomp-sdk-reader").start()

    def _reader(self):
        try:
            while True:
                line = self._f.readline(mcp_hub.MAX_UPSTREAM_MSG + 1)
                if not line:
                    break
                if len(line) > mcp_hub.MAX_UPSTREAM_MSG and not line.endswith(b"\n"):
                    break                        # a frame over the limit: drop the connection, never parse pieces
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and self.on_message:
                    try:
                        self.on_message(msg)
                    except Exception:
                        pass
        except (OSError, ValueError):
            pass
        self.alive = False
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if self.core is not None:
            end = time.monotonic() + 2
            while self.core.active_clients() and time.monotonic() < end:
                time.sleep(0.02)                 # the core lets go of this client (its claim, its event) first
            self.core.shutdown()


def open_connection(env=None, kind="sdk", session=None, tools_hash=None, timeout=20.0, spawn=None):
    """Connect to this VM's hub (starting it if needed), or with MCP_HUB=off to a core of our own, and say
    hello. Returns a Connection (not yet reading: call start() after setting on_message)."""
    env = dict(os.environ if env is None else env)
    hello = {"jsonrpc": "2.0", "id": "mcomp-hello", "method": "metacomputer/hello",
             "params": {"protocol": mcp_hub.PROTOCOL, "kind": kind, "session": session, "cwd": os.getcwd(),
                        "settings": mcp_hub.settings_of(env), "tools_hash": tools_hash, "pid": os.getpid()}}
    for attempt in range(3):
        core = None
        if mcp_hub.mode(env) == "off":
            core = mcp_hub.Hub(env=env, inject_sessions=False, local=True, spawn=spawn)
            a, b = socket.socketpair()
            core.attach(a, os.getpid())
            sock = b
        else:
            sock = mcp_hub.connect_or_start(env, timeout=timeout)
        conn = Connection(sock, core)
        if not conn.send(hello):
            conn.close()
            raise HubError("the metacomputer hub closed the connection")
        try:
            reply = conn.read_one(timeout)
        except (OSError, ValueError) as e:
            conn.close()
            raise HubError(f"no answer from the metacomputer hub ({e})")
        if reply and "result" in reply:
            conn.info = reply["result"]
            return conn
        conn.close()
        err = (reply or {}).get("error") or {}
        if (err.get("data") or {}).get("code") == "hub_restarting" and attempt < 2:
            pid = mcp_hub.hub_pid(env)
            end = time.monotonic() + 10
            while pid and mcp_hub.hub_pid(env) == pid and time.monotonic() < end:
                time.sleep(0.05)                 # until it lets go of its lock
            continue
        raise HubError(err.get("message") or "the metacomputer hub refused the connection")
    raise HubError("the metacomputer hub kept restarting")


class Hub:
    """A connection to the virtual computer through the metacomputer hub. See the module's docstring."""

    def __init__(self, cfg=None, session=None, timeout=120, kind="sdk", env=None):
        if session is not None and not mcp_hub.SESSION_RE.match(str(session)):
            raise ValueError("session names: 1-64 letters, digits, '.', '_' or '-'")
        self.cfg, self.session, self.timeout, self.kind, self._env = cfg, session, timeout, kind, env
        self.conn = None
        self.ids = itertools.count(1)
        self.waiting = {}
        self.lock = threading.Lock()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()

    def connect(self):
        env = self._env if self._env is not None else resolve_env(self.cfg)
        self.conn = open_connection(env, kind=self.kind, session=self.session)
        self.conn.on_message = self._on_message
        self.conn.on_close = self._on_close
        self.conn.start()
        return self

    @property
    def info(self):
        return self.conn.info if self.conn else {}

    def _on_message(self, msg):
        if "method" in msg:
            return                               # notifications (tools/list_changed, progress)
        with self.lock:
            slot = self.waiting.pop(json.dumps(msg.get("id")), None)
        if slot:
            slot.put(msg)

    def _on_close(self):
        with self.lock:
            slots, self.waiting = list(self.waiting.values()), {}
        for s in slots:
            s.put(None)

    def request(self, method, params=None, timeout=None):
        if self.conn is None or not self.conn.alive:
            raise HubError("not connected to the metacomputer hub")
        rid = f"sdk-{next(self.ids)}"
        slot = queue.Queue(1)
        with self.lock:
            self.waiting[json.dumps(rid)] = slot
        if not self.conn.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}):
            with self.lock:
                self.waiting.pop(json.dumps(rid), None)
            raise HubError("the metacomputer hub closed the connection")
        try:
            msg = slot.get(timeout=timeout or self.timeout)
        except queue.Empty:
            with self.lock:
                self.waiting.pop(json.dumps(rid), None)
            self.conn.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": {"requestId": rid, "reason": "timeout"}})
            raise HubError(f"no answer to {method} within {timeout or self.timeout} s")
        if msg is None:
            raise HubError(f"the metacomputer hub closed the connection during {method}")
        if "error" in msg:
            raise HubError(f"{method}: {(msg['error'] or {}).get('message', msg['error'])}")
        return msg.get("result")

    def call(self, tool, args=None, timeout=None):
        t0 = time.time()
        try:
            res = self.request("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        except HubError as e:
            res = {"isError": True, "content": [{"type": "text", "text": str(e)}]}
        return ToolResult(tool, args or {}, res if isinstance(res, dict) else {}, time.time() - t0)

    def tools(self):
        return (self.request("tools/list") or {}).get("tools") or []

    def status(self):
        return self.request("metacomputer/status")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


# --- `metacomputer call` ------------------------------------------------------------------
USAGE = "usage: metacomputer call [--session NAME] [--timeout S] [--json | --raw] TOOL ['JSON']"


def is_tty(f=None):
    try:
        return (f or sys.stdout).isatty()
    except (AttributeError, ValueError):
        return False


def for_tty(text, f=None):
    """The VM's text for a terminal: escaped there (termsafe.escape_for_terminal), exact in a pipe."""
    return termsafe.escape_for_terminal(text) if is_tty(f) else text


def say_err(text):
    """Diagnostics can carry the VM's words: always escaped."""
    sys.stderr.write(f"metacomputer call: {termsafe.escape_for_terminal(str(text))}\n")


def call_main(argv):
    session, timeout, rest, as_json, raw = None, 120.0, [], False, False
    it = iter(argv)
    for a in it:
        if a == "--session":
            session = next(it, None)
        elif a.startswith("--session="):
            session = a.split("=", 1)[1]
        elif a == "--timeout":
            try:
                timeout = float(next(it, ""))
            except ValueError:
                print(USAGE, file=sys.stderr)
                return 2
        elif a == "--json":
            as_json = True
        elif a == "--raw":
            raw = True
        elif a in ("-h", "--help"):
            print(USAGE)
            return 0
        else:
            rest.append(a)
    if not rest or (session is not None and not mcp_hub.SESSION_RE.match(session)):
        print(USAGE + ("\n(session names: 1-64 letters, digits, '.', '_' or '-')" if rest else ""), file=sys.stderr)
        return 2
    tool = rest[0]
    try:
        args = json.loads(rest[1]) if len(rest) > 1 else {}
    except ValueError as e:
        say_err(f"the arguments aren't JSON ({e})")
        return 2
    if not isinstance(args, dict):
        say_err("the arguments must be a JSON object")
        return 2
    try:
        env = resolve_env()
        if session and mcp_hub.mode(env) == "off":
            say_err("note: MCP_HUB=off, so each call has its own connection and --session carries nothing over")
        with Hub(session=session, timeout=timeout, kind="call", env=env) as h:
            r = h.call(tool, args, timeout=timeout)
    except HubError as e:
        say_err(e)
        return 1
    if as_json:
        # The result exactly as it came (images included); \\u escapes only on a terminal.
        print(json.dumps(r.raw, indent=1, ensure_ascii=is_tty()))
        return 1 if r.is_error else 0
    # The VM's words reach a terminal escaped (no cursor moves, screen rewrites, OSC 52 clipboard writes or
    # bidi tricks); a pipe, or --raw, gets them exactly. The JSON below escapes everything outside ASCII.
    print(r.text if raw else for_tty(r.text))
    if r.structured:
        print(json.dumps(r.structured, indent=2)[:6000])
    shots = os.path.join(LAB, "screenshots")
    os.makedirs(shots, exist_ok=True)
    for p in r.save_images(os.path.join(shots, f"adhoc-{re.sub(r'[^A-Za-z0-9_.-]', '', tool)[:40]}-{int(time.time())}")):
        print("image:", p)
    return 1 if r.is_error else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "call":
        sys.exit(call_main(sys.argv[2:]))
    print(__doc__)
    sys.exit(2)
