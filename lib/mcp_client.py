#!/usr/bin/env python3
"""Minimal MCP stdio client for metacomputer (no third-party dependencies).

It launches `bin/metacomputer mcp` -- the same SSH bridge Claude Code uses -- so
everything it does goes through Cua Driver's MCP server inside the guest.

    python3 lib/mcp_client.py --probe                 # handshake + tool count (JSON)
    python3 lib/mcp_client.py --tools                 # list tool names
    python3 lib/mcp_client.py --describe TOOL         # one tool's input schema
    python3 lib/mcp_client.py --call TOOL '{"k":1}'   # call a tool, print text, save images
"""
from __future__ import annotations

import base64
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

LAB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = [os.path.join(LAB_ROOT, "bin", "metacomputer"), "mcp"]
PROTOCOL = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPClient:
    """One persistent MCP connection (one Cua Driver runtime in the guest)."""

    def __init__(self, cmd=None, stderr_path=None, log=None):
        self.cmd = cmd or BRIDGE
        self.stderr_path = stderr_path
        self.log = log  # callable(dict) for an action log, optional
        self.proc = None
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.next_id = 1
        self.server_info = {}
        self.protocol = None
        self.calls = 0

    # -- transport --------------------------------------------------------
    def start(self, timeout=90):
        # The settings this connection starts with (MCOMP_CONFIG may differ per client, e.g. in tests).
        self.bounded = driver_profiles.bounded(labconfig.load())
        err = open(self.stderr_path, "ab") if self.stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=err, bufsize=0)
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
        buf = b""
        while True:
            chunk = self.proc.stdout.read1(65536) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(65536)
            if not chunk:
                self.q.put({"__eof__": True})
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    self.q.put(json.loads(line))
                except json.JSONDecodeError:
                    self.q.put({"__garbage__": line[:200].decode(errors="replace")})

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
                msg = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if msg.get("__eof__"):
                raise MCPError(f"MCP server closed the connection during {method}")
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
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--probe":
        t0 = time.time()
        try:
            with MCPClient() as c:
                n = len(c.tools())
                print(json.dumps({"ok": True, "server": f"{c.server_info.get('name', '')} {c.server_info.get('version', '')}".strip(),
                                  "protocol": c.protocol, "tools": n, "ms": int((time.time() - t0) * 1000)}))
                return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
    with MCPClient(stderr_path=os.path.join(LAB_ROOT, "logs", "mcp-client-stderr.log")) as c:
        if argv[0] == "--tools":
            for t in c.tools():
                print(f"{t['name']:28s} {(t.get('description') or '').splitlines()[0][:110]}")
        elif argv[0] == "--describe":
            for t in c.tools():
                if t["name"] == argv[1]:
                    print(json.dumps(t, indent=2))
        elif argv[0] == "--call":
            r = c.call(argv[1], json.loads(argv[2]) if len(argv) > 2 else {})
            print(r.text)
            if r.structured:
                print(json.dumps(r.structured, indent=2)[:6000])
            saved = r.save_images(os.path.join(LAB_ROOT, "screenshots", f"adhoc-{argv[1]}-{int(time.time())}"))
            for p in saved:
                print("image:", p)
            return 1 if r.is_error else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
