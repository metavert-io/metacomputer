#!/usr/bin/env python3
"""The page helper and the commands around it, no VM: lib/page_guest.py's WebSocket client against a stand-in
server (masking, long frames, ping, close, the handshake check), its cutting of page text and query strings, the
Mac's reading of its answer through `metacomputer sh` (lib/browser_tools.py: refusals, a cut-short answer, a
malformed one, screenshots only from the VM's own folder), and `metacomputer each` (lib/each.py). The browsers
themselves are tried in tests/test_web_vm.py (a test VM)."""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(LAB, "lib"))
import page_guest as pg  # noqa: E402
import browser_tools as bt  # noqa: E402

TD = tempfile.mkdtemp(prefix="mcomp-pg-")
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"   [{str(detail)[:300]}]"))


# --- a stand-in WebSocket server ------------------------------------------------------------------------------
class Server:
    def __init__(self, bad_accept=False):
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(1)
        self.port = self.s.getsockname()[1]
        self.got, self.masked, self.bad_accept = [], [], bad_accept
        threading.Thread(target=self.serve, daemon=True).start()

    def frame(self, payload, op=1):
        n = len(payload)
        hdr = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                                    else bytes([127]) + struct.pack(">Q", n))
        return hdr + payload

    def serve(self):
        c, _ = self.s.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += c.recv(4096)
        key = [l.split(b": ", 1)[1] for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        if self.bad_accept:
            acc = b"bm9wZQ=="
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        buf = b""
        while True:
            try:
                chunk = c.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while len(buf) >= 2:
                b1 = buf[1]
                n, off = b1 & 0x7F, 2
                if n == 126:
                    n, off = struct.unpack(">H", buf[2:4])[0], 4
                elif n == 127:
                    n, off = struct.unpack(">Q", buf[2:10])[0], 10
                if len(buf) < off + 4 + n:
                    break
                self.masked.append(bool(b1 & 0x80))
                mk = buf[off:off + 4]
                data = bytes(x ^ mk[i % 4] for i, x in enumerate(buf[off + 4:off + 4 + n]))
                buf = buf[off + 4 + n:]
                if buf[:0] == b"" and data[:1] == b"{":
                    msg = json.loads(data)
                    self.got.append(msg)
                    if msg["method"] == "big":
                        c.sendall(self.frame(b"\x89\x00", 9)[:0] + self.frame(b"", 9))      # a ping first
                        reply = json.dumps({"id": msg["id"], "result": {"x": "y" * 70000}}).encode()
                        c.sendall(self.frame(reply))
                    elif msg["method"] == "fail":
                        c.sendall(self.frame(json.dumps({"id": msg["id"], "error": {"message": "nope\x1b[31m"}}).encode()))
                    elif msg["method"] == "evented":
                        c.sendall(self.frame(json.dumps({"method": "Log.entryAdded", "params": {"entry": {"text": "e"}}}).encode()))
                        c.sendall(self.frame(json.dumps({"id": msg["id"] + 99, "result": {}}).encode()))   # someone else's
                        c.sendall(self.frame(json.dumps({"id": msg["id"], "result": {"ok": True}}).encode()))
                    elif msg["method"] == "close":
                        c.sendall(self.frame(b"", 8))
                    else:
                        c.sendall(self.frame(json.dumps({"id": msg["id"], "result": {"echo": msg["params"]}}).encode()))


srv = Server()
ws = pg.WebSocket(f"ws://127.0.0.1:{srv.port}/devtools/browser/x")
sess = pg.Session(ws, "cdp")
r = sess.send("Echo", {"a": "b" * 300})
check("a request and its answer (client frames masked, a 2-byte length)", r == {"echo": {"a": "b" * 300}}
      and all(srv.masked), (r, srv.masked))
r = sess.send("big")
check("a long answer (8-byte length) after a ping", len(r["x"]) == 70000)
try:
    sess.send("fail")
    ok = False
except pg.Fail as e:
    ok = "nope" in str(e) and "\x1b" not in str(e)
check("an error answer is a Fail, its text with control characters taken out", ok)
r = sess.send("evented")
check("events are kept and someone else's answer skipped", r == {"ok": True}
      and sess.events and sess.events[0][0] == "Log.entryAdded")
try:
    sess.send("close")
    ok = False
except pg.Fail as e:
    ok = "closed" in str(e)
check("a close frame ends it with a Fail", ok)
for bad in ("ws://10.0.0.1:9222/x", "ws://example.com:80/x", "wss://127.0.0.1:1/x", "http://127.0.0.1:1/"):
    try:
        pg.WebSocket(bad)
        ok = False
    except pg.Fail:
        ok = True
    check(f"only the VM's loopback: {bad} refused", ok)
srv2 = Server(bad_accept=True)
try:
    pg.WebSocket(f"ws://127.0.0.1:{srv2.port}/")
    ok = False
except pg.Fail as e:
    ok = "didn't match" in str(e)
check("a handshake whose accept key is wrong is refused", ok)

# --- cutting what the page says ------------------------------------------------------------------------------
check("text is cut and control characters replaced", pg.cut("a\x1b[2Jb" + "x" * 900).startswith("a [2Jb")
      and len(pg.cut("x" * 900)) == pg.MAX_TEXT + 1)
check("query strings are cut from URLs by default", pg.strip_query("https://ex.com/a/b?token=SECRET#f") == "https://ex.com/a/b?…"
      and pg.strip_query("https://ex.com/a?x=1", keep=True) == "https://ex.com/a?x=1")
check("devices: sizes, ratios, mobile", pg.DEVICES["iphone-15"][:4] == (393, 852, 3, True)
      and pg.DEVICES["desktop"][3] is False)
out = subprocess.run([sys.executable, os.path.join(LAB, "lib", "page_guest.py"), "gpu", '{"url": "ftp://x"}'],
                     capture_output=True, text=True, timeout=30)
check("the helper refuses a URL that isn't http(s), with one JSON line", json.loads(out.stdout) ==
      {"ok": False, "error": "give an http:// or https:// URL"}, out.stdout)
out = subprocess.run([sys.executable, os.path.join(LAB, "lib", "page_guest.py"), "page-read", '{"name": "../x"}'],
                     capture_output=True, text=True, timeout=30)
check("a kept page's name can't be a path", json.loads(out.stdout)["ok"] is False and "page name" in out.stdout, out.stdout)
src = open(os.path.join(LAB, "lib", "page_guest.py")).read()
check("the helper refuses filling passwords and one-time codes in the page itself",
      "el.type === 'password'" in src and "one-time-code" in src and "secrets fill --human" in src)
check("its browsers' remote-control port is the VM's loopback", '"--remote-debugging-address=127.0.0.1"' in src
      and 'r"^ws://(127\\.0\\.0\\.1|localhost):(\\d+)(/.*)?$"' in src)

# --- the Mac's side: lib/browser_tools.py page_guest() ---------------------------------------------------------
stub = os.path.join(TD, "metacomputer")
with open(stub, "w") as f:
    f.write("#!/bin/sh\ncat > /dev/null\ncat \"$STUB_OUT\"\n")
os.chmod(stub, 0o755)
bt.MCOMP_CLI = stub


def answer(sh):
    p = os.path.join(TD, "out.json")
    with open(p, "w") as f:
        f.write(sh if isinstance(sh, str) else json.dumps(sh))
    os.environ["STUB_OUT"] = p


def guest(action="read"):
    try:
        return bt.page_guest(action, {"url": "https://example.com"}), None
    except bt.Fail as e:
        return None, str(e)


answer({"status": "exited", "exit_code": 0, "stdout": json.dumps({"ok": True, "result": {"title": "T"}}) + "\n"})
check("an answer: its result", guest() == ({"title": "T"}, None))
answer({"status": "refused", "message": "another session is using it"})
check("a refusal from the shell channel is a Fail with its words", "refused: another session" in (guest()[1] or ""))
answer({"status": "exited", "exit_code": 0, "stdout": "x" * 10, "stdout_truncated": True})
check("an answer cut short at the shell channel's limit is a Fail, never parsed", "larger than" in (guest()[1] or ""))
answer({"status": "exited", "exit_code": 1, "stdout": "Traceback...\n"})
check("a malformed answer is a Fail", "didn't answer as expected" in (guest()[1] or ""))
answer({"status": "exited", "exit_code": 1, "stdout": json.dumps({"ok": False, "error": "the page didn't load"})})
check("the helper's own error comes through", guest()[1] == "the page didn't load")
answer("not json at all")
check("output that isn't the shell channel's JSON is a Fail", (guest()[1] or "").startswith("metacomputer sh"))
for p in ("/etc/passwd", "/home/cua/metacomputer-runs/web-shots/../../.ssh/id.png", "/home/cua/elsewhere/x.png",
          "/home/cua/metacomputer-runs/web-shots/x.txt", 12):
    check(f"a screenshot path outside the VM's web-shots folder isn't fetched: {p!r}",
          bt.fetch_shot(p, TD, "x.png") is None)

# --- metacomputer each ------------------------------------------------------------------------------------------
import each  # noqa: E402
estub = os.path.join(TD, "each-cli")
with open(estub, "w") as f:
    f.write('#!/bin/sh\nsleep 1\nif [ "$1" = --vm ]; then echo "vm $2: $3"; printf "\\033[31mred\\n"; exit 3; fi\n'
            'echo "main: $1"\n')
os.chmod(estub, 0o755)
each.CLI = estub
r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {os.path.join(LAB, 'lib')!r}); import each; "
                    f"each.CLI = {estub!r}; sys.exit(each.main(['main,a,b', '--', 'status']))"],
                   capture_output=True, text=True, timeout=30)
t0 = time.time()
check("each runs the command on every VM (main without --vm) and the exit status is the highest",
      r.returncode == 3 and "=== main: exit 0" in r.stdout and "=== a: exit 3" in r.stdout and "vm b: status" in r.stdout,
      r.stdout + r.stderr)
r2 = subprocess.run([sys.executable, "-c", f"import sys, time; sys.path.insert(0, {os.path.join(LAB, 'lib')!r}); import each; "
                     f"each.CLI = {estub!r}; t = time.time(); each.main(['a,b,c,d', '--', 'status']); "
                     "print('took', round(time.time() - t, 1))"], capture_output=True, text=True, timeout=30)
took = float(r2.stdout.strip().split("took")[-1])
check("…at the same time (4 VMs of 1 s each in well under 4 s)", took < 2.5, took)
for args, why in ((["a,,a", "--", "status"], "the same VM twice"), (["../x", "--", "status"], "a path"),
                  (["a", "--", "each", "status"], "each inside each"), (["a", "--", "setup"], "setup"),
                  (["a", "status"], "no --")):
    check(f"each refuses {why}", each.main(args) == 2)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
