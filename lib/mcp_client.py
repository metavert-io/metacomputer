#!/usr/bin/env python3
"""Minimal MCP stdio client for metacomputer (no third-party dependencies).

MCPClient speaks MCP to a command's stdin/stdout. Run as `bin/metacomputer mcp`
(the default command) it is the raw transport: MCOMP_DIRECT_BRIDGE=1, its own SSH
connection to Cua Driver in the guest, outside the hub and its guards. Only the
transport tests may open that, and they say so (MCPClient(transport_test=True));
everything else that acts on the desktop goes through the hub (lib/mcomp_sdk.py:
`metacomputer call`, `web`, the GUI test harness, the status probe). ToolResult
is shared with the SDK.

    python3 lib/mcp_client.py --probe                 # handshake + tool count (JSON), through the hub
    python3 lib/mcp_client.py --tools                 # list tool names
    python3 lib/mcp_client.py --describe TOOL         # one tool's input schema
    python3 lib/mcp_client.py --call TOOL '{"k":1}'   # = metacomputer call (through the hub and its guards)
"""
from __future__ import annotations

import base64
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import driver_profiles  # noqa: E402
import labconfig  # noqa: E402
import wirelimits  # noqa: E402

LAB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = [os.path.join(LAB_ROOT, "bin", "metacomputer"), "mcp"]
PROTOCOL = "2025-06-18"
MAX_MSG = 64 << 20          # one message from the VM (a native 4K screenshot is well under this)
MAX_QUEUED = 1024           # messages from the VM waiting to be read
MAX_QUEUED_BYTES = 256 << 20    # and their bytes together
RAW_ONLY_FOR_TESTS = ("the raw driver connection (`metacomputer mcp` with MCOMP_DIRECT_BRIDGE=1) skips the hub's guards; "
                      "only the transport tests open it (MCPClient(transport_test=True)). Use lib/mcomp_sdk.py's Hub.")


class MCPError(RuntimeError):
    pass


class MCPClient:
    """One persistent MCP connection (one Cua Driver runtime in the guest when cmd is the raw transport)."""

    def __init__(self, cmd=None, stderr_path=None, log=None, transport_test=False):
        self.cmd = cmd or BRIDGE
        self.stderr_path = stderr_path
        self.log = log  # callable(dict) for an action log, optional
        self.transport_test = transport_test
        self.proc = None
        self.q: "queue.Queue[dict]" = queue.Queue(MAX_QUEUED)
        self.qbytes = 0
        self.qlock = threading.Lock()
        self.closed_why = None
        self.next_id = 1
        self.server_info = {}
        self.protocol = None
        self.calls = 0
        self.stderr_counter = None

    # -- transport --------------------------------------------------------
    def start(self, timeout=90):
        # `metacomputer mcp` is the raw driver connection only with MCOMP_DIRECT_BRIDGE=1 (else, with the hub
        # on, it's the guarded bridge); `mcp-lazy` and anything else run as given.
        raw = self.cmd[-1:] == ["mcp"]
        if raw and not self.transport_test:
            raise MCPError(RAW_ONLY_FOR_TESTS)
        # The settings this connection starts with (MCOMP_CONFIG may differ per client, e.g. in tests).
        self.bounded = driver_profiles.bounded(labconfig.load())
        env = dict(os.environ, MCOMP_DIRECT_BRIDGE="1") if raw else None
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE if self.stderr_path else subprocess.DEVNULL,
                                     bufsize=0, env=env)
        if self.stderr_path:
            # SSH carries the VM's stderr, which can echo what a page showed: none of its text is kept, as in the hub
            # (wirelimits.StderrCounter): counts, and a fixed class per line in a capped log at stderr_path.
            self.stderr_counter = wirelimits.StderrCounter(self.proc.stderr, self.stderr_path)
        threading.Thread(target=self._reader, daemon=True).start()
        res = self.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "mcomp-harness", "version": "1.0"},
        }, timeout=timeout)
        self.server_info = res.get("serverInfo", {})
        self.protocol = res.get("protocolVersion")
        self.notify("notifications/initialized", {})
        return res

    def _reader(self):
        # Each message is read up to MAX_MSG bytes (the VM is untrusted: a line without an end must not grow
        # without limit), and at most MAX_QUEUED messages or MAX_QUEUED_BYTES wait to be read; past any of
        # these, the connection is dropped.
        out = self.proc.stdout if hasattr(self.proc.stdout, "peek") else io.BufferedReader(self.proc.stdout, 1 << 16)
        try:
            while True:
                try:
                    line = out.readline(MAX_MSG + 1)
                except (OSError, ValueError):
                    line = b""
                if not line:
                    break
                if len(line) > MAX_MSG and not line.endswith(b"\n"):
                    self.closed_why = f"the VM sent a message over {MAX_MSG >> 20} MB"
                    self._kill()
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = wirelimits.loads(line)         # nested too deeply counts as garbage, not a dead reader
                except ValueError:
                    msg = {"__garbage__": line[:200].decode(errors="replace")}
                if not isinstance(msg, dict):
                    msg = {"__garbage__": "not a JSON object"}
                if not self._put(msg, len(line)):
                    return
        finally:
            self._put({"__eof__": True}, 0, final=True)

    def _put(self, msg, size, final=False):
        with self.qlock:
            over = not final and self.qbytes + size > MAX_QUEUED_BYTES
            if not over:
                self.qbytes += size
        if over:
            self.closed_why = f"more than {MAX_QUEUED_BYTES >> 20} MB from the VM waited unread"
        else:
            try:
                self.q.put((msg, size), timeout=30 if not final else 1)
                return True
            except queue.Full:
                with self.qlock:
                    self.qbytes -= size
                if final:
                    return False
                self.closed_why = f"more than {MAX_QUEUED} messages from the VM waited unread"
        self._kill()
        try:
            self.q.get_nowait()
            self.q.put_nowait(({"__eof__": True}, 0))
        except (queue.Empty, queue.Full):
            pass
        return False

    def _get(self, timeout):
        msg, size = self.q.get(timeout=timeout)
        with self.qlock:
            self.qbytes -= size
        return msg

    def _kill(self):
        try:
            self.proc.kill()
        except OSError:
            pass

    def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=120):
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise MCPError(f"timeout after {timeout}s waiting for {method}")
            try:
                msg = self._get(left)
            except queue.Empty:
                continue
            if msg.get("__eof__"):
                said = self.stderr_counter.text(wait=1.0) if self.stderr_counter else ""
                raise MCPError(f"MCP server closed the connection during {method}"
                               + (f" ({self.closed_why}; connection dropped)" if self.closed_why else "")
                               + (f"; {said}" if said else ""))
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise MCPError(f"{method}: {msg['error']}")
                return msg["result"]
            # server->client requests (e.g. ping) get an empty result
            if "method" in msg and "id" in msg:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.close()

    # -- MCP --------------------------------------------------------------
    def tools(self):
        out, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            out += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def call(self, name, args=None, timeout=120):
        args = args or {}
        if name == "launch_app" and getattr(self, "bounded", False):
            args = dict(args)
            driver_profiles.rewrite_launch(args)   # bounded mode launches apps only by absolute path
        t0 = time.time()
        self.calls += 1
        try:
            res = self.request("tools/call", {"name": name, "arguments": args}, timeout=timeout)
            err = None
        except MCPError as e:
            res, err = {"isError": True, "content": [{"type": "text", "text": str(e)}]}, str(e)
        r = ToolResult(name, args, res, time.time() - t0)
        if self.log:
            self.log(r.log_entry(err))
        return r


class ToolResult:
    def __init__(self, name, args, raw, elapsed):
        self.name, self.args, self.raw, self.elapsed = name, args, raw, elapsed

    @property
    def is_error(self):
        return bool(self.raw.get("isError"))

    @property
    def text(self):
        return "\n".join(c.get("text", "") for c in self.raw.get("content", []) if c.get("type") == "text")

    @property
    def structured(self):
        return self.raw.get("structuredContent") or {}

    @property
    def images(self):
        return [c for c in self.raw.get("content", []) if c.get("type") == "image"]

    def save_images(self, prefix):
        paths = []
        for i, im in enumerate(self.images):
            ext = {"image/png": "png", "image/jpeg": "jpg"}.get(im.get("mimeType"), "bin")
            p = f"{prefix}{'' if i == 0 else f'-{i}'}.{ext}"
            with open(p, "wb") as f:
                f.write(base64.b64decode(im["data"]))
            paths.append(p)
        return paths

    def log_entry(self, err=None):
        s = self.structured
        return {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tool": self.name,
            "args": redact(self.args),
            "ok": not self.is_error,
            "elapsed_s": round(self.elapsed, 3),
            "summary": (s.get("summary") or self.text)[:400],
            "route": s.get("route"),
            "images": len(self.images),
            "error": err,
        }


def redact(args):
    """Never log typed secrets: the harness types only test strings, but keep
    the habit (fields named like credentials are masked)."""
    out = {}
    for k, v in (args or {}).items():
        kl = k.lower()
        secretish = kl in ("password", "passwd", "secret", "token", "api_key", "apikey", "access_token") or \
            kl.endswith(("_password", "_secret", "_api_key"))
        out[k] = "***" if secretish else v
    return out


def main(argv):
    if not argv:
        argv = ["--help"]                   # (no words: its usage, as -h)
    import strictargs           # its command line, by metacomputer's rules, before anything is done
    import mcomp_sdk            # (everything here goes through the hub, like `metacomputer call`)
    forms = ["--probe", "--tools", "--describe TOOL"] + ["--call " + f for f in mcomp_sdk.CALL_FORMS]
    p = strictargs.expect(argv, forms, prog="mcp_client.py", usage=__doc__, defer=True)
    if p.form == 3 and argv[0] != "--call":
        print(f"mcp_client.py: --call comes first: --call {mcomp_sdk.CALL_FORMS[0]} (nothing was run)", file=sys.stderr)
        return 2
    if p.form == 3:
        return mcomp_sdk.call_main(argv[1:], of=forms)  # (its own check of call's words: the same form, parse-only there)
    strictargs.parsed("mcp_client.py", p.info)
    if p.form == 0:
        return mcomp_sdk.probe_main()
    if p.form == 1:
        return mcomp_sdk.tools_main(["tools"])
    return mcomp_sdk.tools_main(["describe", p.opts["--describe"]])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
