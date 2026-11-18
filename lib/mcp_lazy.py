#!/usr/bin/env python3
"""On-demand MCP bridge to the virtual computer (`metacomputer mcp-lazy`).

Claude Code registers this for every project (user scope). It starts instantly
whether or not the VM is running, so the `metacomputer` tools exist in every
session without a restart:

- initialize and tools/list (and the resources/prompts lists) are answered from
  a cached copy of Cua Driver's own handshake for the pinned driver version
  (config/driver-mcp-cache.json, refreshed in .run/ on every live connection),
  with an identity note in front of the driver's instructions;
- the first call that needs the driver connects to this VM's hub (lib/mcp_hub.py,
  started if it isn't running) and from then on messages pass through it. The
  hub holds the one connection to the VM and applies every guard: one controller
  at a time, the Chrome guard, bounded mode, the coordinate guard, the action
  log and the event stream;
- while the VM is off, a call returns a clear "start it, then retry" error;
- a dropped connection (the VM or the hub stopped) is reopened on the next call.

With MCP_HUB=off the same guards run in this process instead, on a connection of
its own (the hub's core, lib/mcp_hub.py, with this session as its only client).

  mcp_lazy.py                      run as the stdio MCP server
  mcp_lazy.py --refresh-cache [--seed]   connect once and save the handshake
"""
import json
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_hub  # noqa: E402
import mcomp_sdk  # noqa: E402
import wirelimits  # noqa: E402

LAB = mcp_hub.LAB
CLI = mcp_hub.CLI
IDENTITY, OFF_MSG, DROPPED_MSG = mcp_hub.IDENTITY, mcp_hub.OFF_MSG, mcp_hub.DROPPED_MSG
NO_HUB = ("The metacomputer bridge couldn't reach this VM's hub ({}). Check `" + CLI + " status` and `" + CLI
          + " hub status`, then call again. (This is the virtual computer, an Ubuntu VM, not the user's Mac.)")
WATCH_PARENT_S = 5


class Front:
    def __init__(self, env=None, out=None):
        self.env = dict(os.environ if env is None else env)
        self.out = out or sys.stdout
        self.out_lock = threading.Lock()
        self.conn_lock = threading.Lock()
        self.conn = None
        self.outstanding = set()       # ids of requests passed to the hub, still unanswered
        self.olock = threading.Lock()
        self.cache = mcp_hub.load_cache(mcp_hub.paths(self.env)["cache"]) or {}
        self.served = None             # names-hash of the tools this session was given from the cache
        self.pid = os.getpid()
        self.stopping = False

    # --- output ------------------------------------------------------------------------
    def send(self, msg):
        data = json.dumps(msg, separators=(",", ":")) + "\n"
        with self.out_lock:
            try:
                self.out.write(data)
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def reply(self, msg, result):
        self.send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def tool_error(self, msg, text):
        self.reply(msg, {"content": [{"type": "text", "text": text}], "isError": True})

    # --- the hub -------------------------------------------------------------------------
    def connected(self):
        c = self.conn
        return c is not None and c.alive

    def connect(self):
        """None when connected to the hub (or this process's own core), else a message for the caller."""
        with self.conn_lock:
            if self.connected():
                return None
            try:
                conn = mcomp_sdk.open_connection(self.env, kind="claude-code", tools_hash=self.served)
            except mcp_hub.HubError as e:
                return NO_HUB.format(e)
            except OSError as e:
                return NO_HUB.format(e)
            conn.on_message = self.from_hub
            conn.on_close = lambda c=conn: self.hub_closed(c)
            self.conn = conn
            conn.start()
            return None

    def from_hub(self, msg):
        if "method" not in msg:
            with self.olock:
                self.outstanding.discard(json.dumps(msg.get("id")))
        self.send(msg)

    def hub_closed(self, conn):
        if self.conn is conn:
            self.conn = None
        if self.stopping:
            return
        with self.olock:
            ids, self.outstanding = list(self.outstanding), set()
        for key in ids:
            self.send({"jsonrpc": "2.0", "id": json.loads(key), "error": {"code": -32603, "message": DROPPED_MSG}})

    def forward(self, msg):
        if msg.get("id") is not None:
            with self.olock:
                self.outstanding.add(json.dumps(msg["id"]))
        c = self.conn
        if c is None or not c.send(msg):
            if msg.get("id") is not None:
                with self.olock:
                    self.outstanding.discard(json.dumps(msg["id"]))
                if msg.get("method") == "tools/call":
                    self.tool_error(msg, DROPPED_MSG)
                else:
                    self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": DROPPED_MSG}})

    # --- messages from Claude Code --------------------------------------------------------
    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if method is None:                       # an answer to a server request: the hub never sends those
            return
        if method == "initialize":
            want = (msg.get("params") or {}).get("protocolVersion")
            if not self.cache.get("initialize") and self.connect() is None:
                self.forward(msg)
                return
            self.reply(msg, mcp_hub.initialize_result(self.cache, want))
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            self.reply(msg, {})
        elif method == "tools/list" and not self.connected():
            tools = mcp_hub.site_login.with_tools((self.cache.get("tools") or {}).get("tools") or [])
            self.served = mcp_hub.tool_names_hash(tools)
            self.reply(msg, {"tools": tools})
        elif method in ("resources/list", "resources/templates/list", "prompts/list") and not self.connected():
            key = method.split("/")[0]
            cached = self.cache.get(key) if method != "resources/templates/list" else None
            empty = {"resources": []} if method == "resources/list" else (
                {"resourceTemplates": []} if method == "resources/templates/list" else {"prompts": []})
            self.reply(msg, cached or empty)
        elif method == "logging/setLevel":
            self.reply(msg, {})
        elif mid is None:                        # notifications (cancelled, ...)
            if self.connected():
                self.forward(msg)
        else:                                    # tools/call and anything else: through the hub
            err = self.connect()
            if err:
                if method == "tools/call":
                    self.tool_error(msg, err)
                else:
                    self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": err}})
                return
            self.forward(msg)

    def shutdown(self, *_):
        self.stopping = True
        c = self.conn
        if c is not None:
            c.close()                            # the hub drops this session's claim and ends its driver session
        os._exit(0)

    def watch_parent(self):
        """Claude Code went away without closing our stdin: don't linger (and keep the computer claimed)."""
        while True:
            time.sleep(WATCH_PARENT_S)
            if os.getppid() == 1:
                self.shutdown()

    def serve(self, stdin=None):
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGHUP, self.shutdown)
        threading.Thread(target=self.watch_parent, daemon=True).start()
        for line in (stdin or sys.stdin):
            line = line.strip()
            if not line:
                continue
            try:
                msg = wirelimits.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                self.handle(msg)
            except Exception as e:  # never let one message kill the bridge
                if msg.get("id") is not None and msg.get("method"):
                    self.send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": str(e)}})
        self.shutdown()


if __name__ == "__main__":
    if "--refresh-cache" in sys.argv:
        sys.exit(mcp_hub.main(["mcp_hub.py", "refresh-cache"] + (["--seed"] if "--seed" in sys.argv else [])))
    if os.environ.get("MCOMP_HUB_PID"):
        # The hub's own driver connection must never come back here (a loop through the hub).
        print("metacomputer: mcp-lazy can't run inside the hub's own connection", file=sys.stderr)
        sys.exit(1)
    Front().serve()
