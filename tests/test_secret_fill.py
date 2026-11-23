#!/usr/bin/env python3
"""Offline checks for the VM side of `metacomputer secrets fill`
(provisioning/guest/rootfs/opt/metacomputer/bin/secret-fill).

No VM needed. Three parts:
  1. its pure helpers: origins, field checks, /proc/net/tcp parsing (fixtures),
     WebSocket frames, redaction;
  2. the whole program against a fake Chrome DevTools server (HTTP + WebSocket) that
     models frames, isolated worlds, DOM.describeNode and SystemInfo, found through a
     /proc/net/tcp fixture beside decoy listeners;
  3. the whole program against a real headless Chromium, when one is found
     (MCOMP_TEST_CHROME=/path/to/chrome, Google Chrome, or Playwright's copy),
     including a page that tampers with its own globals; skipped otherwise.

Run: python3 tests/test_secret_fill.py
Values here are dummies.
"""
import glob
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(LAB, "provisioning", "guest", "rootfs", "opt", "metacomputer", "bin", "secret-fill")
sys.dont_write_bytecode = True
_loader = importlib.machinery.SourceFileLoader("secret_fill", SCRIPT)
_spec = importlib.util.spec_from_loader("secret_fill", _loader)
sf = importlib.util.module_from_spec(_spec)
_loader.exec_module(sf)

VALUE = "Dummy-Pass-4815"
UID = 1000
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({str(detail)[:300]})" if detail and not cond else ""))


def tcp_line(i, ip_hex, port, st="0A", uid=UID):
    return (f"  {i:3d}: {ip_hex}:{port:04X} 00000000:0000 {st} 00000000:00000000 00:00000000 00000000 "
            f"{uid:5d}        0 {10000 + i} 1 0000000000000000 100 0 0 10 0")


TCP_HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
TCP6_HEAD = "  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"


# --- 1. pure helpers ------------------------------------------------------------------
def unit():
    po = sf.parse_origin
    check("origin: path and query dropped", po("https://Example.com/login?x=1") == "https://example.com")
    check("origin: default port dropped", po("https://example.com:443/") == "https://example.com")
    check("origin: other port kept", po("https://example.com:8443/a") == "https://example.com:8443")
    check("origin: http with port", po("http://127.0.0.1:8000/x") == "http://127.0.0.1:8000")
    check("origin: not http(s)", po("about:blank") is None and po("://") is None and po("null") is None)
    check("allowed: subdomain is another site", not sf.origin_allowed("https://evil.example.com/", ["https://example.com"]))
    check("allowed: look-alike suffix refused", not sf.origin_allowed("https://example.com.evil.test/", ["https://example.com"]))

    ff = sf.field_fits
    check("field: password into type=password", ff("password", {"tag": "input", "type": "password"})[0])
    check("field: password refused in type=text", not ff("password", {"tag": "input", "type": "text"})[0])
    check("field: username into email", ff("username", {"tag": "input", "type": "email"})[0])
    check("field: otp into tel/number/text", all(ff("otp", {"tag": "input", "type": t})[0] for t in ("tel", "number", "text")))
    check("field: text into textarea", ff("text", {"tag": "textarea", "type": ""})[0])
    check("field: read-only refused", not ff("password", {"tag": "input", "type": "password", "readonly": True})[0])
    check("field: nothing focused", not ff("password", {"tag": "body"})[0])

    check("loopback: 127.0.0.1 as /proc prints it", sf.loopback_hex("0100007F"))
    check("loopback: 127.1.2.3", sf.loopback_hex("0302017F"))
    check("loopback: 0.0.0.0 and 10.0.0.1 aren't", not sf.loopback_hex("00000000") and not sf.loopback_hex("0100000A"))
    check("loopback: ::1", sf.loopback_hex("00000000000000000000000001000000"))
    check("loopback: ::ffff:127.0.0.1", sf.loopback_hex("0000000000000000FFFF00000100007F"))
    check("loopback: :: isn't", not sf.loopback_hex("00000000000000000000000000000000"))
    tcp = "\n".join([TCP_HEAD, tcp_line(0, "0100007F", 9222), tcp_line(1, "00000000", 5900),
                     tcp_line(2, "0100007F", 8000, uid=0), tcp_line(3, "0100007F", 9333, st="01"),
                     tcp_line(4, "0100007F", 9222), tcp_line(5, "0302017F", 41000)])
    tcp6 = "\n".join([TCP6_HEAD, tcp_line(0, "00000000000000000000000001000000", 9444),
                      tcp_line(1, "00000000000000000000000000000000", 22)])
    check("listeners: loopback, LISTEN, this user, once each", sf.tcp_listeners([tcp, tcp6], UID) == [9222, 41000, 9444],
          sf.tcp_listeners([tcp, tcp6], UID))
    check("listeners: any user when none is configured", 8000 in sf.tcp_listeners([tcp], None))
    check("listeners: malformed lines skipped", sf.tcp_listeners(["header\nnot a line\n  0: zz:zz", ""], UID) == [])
    ok_v = {"Browser": "Chrome/150.0.1.2", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}
    check("devtools: Chrome's /json/version", sf.is_devtools_version(ok_v, 9222))
    check("devtools: headless Chrome too", sf.is_devtools_version(dict(ok_v, Browser="HeadlessChrome/150"), 9222))
    check("devtools: another browser refused", not sf.is_devtools_version(dict(ok_v, Browser="Firefox/140"), 9222))
    check("devtools: an endpoint for another port refused", not sf.is_devtools_version(ok_v, 9223))
    check("devtools: not JSON object", not sf.is_devtools_version(["x"], 9222))
    check("cmdline: --user-data-dir (NUL-separated)",
          sf.user_data_dir(b"/opt/google/chrome/chrome\0--user-data-dir=/home/u/p/x\0--remote-debugging-port=0\0") == "/home/u/p/x")
    check("cmdline: rewritten with spaces", sf.user_data_dir(b"chrome --type=x --user-data-dir=/a/b") == "/a/b")
    check("cmdline: none", sf.user_data_dir(b"chrome\0--flag\0") is None)
    check("redaction: any length", sf.withheld({"reason": "got 12 here"}, "12") and not sf.withheld({"chars": 2}, "12"))
    check("utf16 length", sf.utf16_len("aä\U0001F600") == 4)
    check("websocket accept key (RFC 6455 1.3)", sf.ws_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
    for n in (0, 125, 126, 65535, 65536):
        payload = bytes(i % 251 for i in range(n))
        f = sf.ws_encode_frame(0x1, payload, mask=b"\x01\x02\x03\x04")
        d = sf.ws_decode_frame(f)
        check(f"websocket frame round trip, {n} bytes (masked)", d is not None and d[2] == payload and d[3] == len(f) and f[1] & 0x80)
    check("websocket masking (RFC 6455 5.7)", sf.ws_encode_frame(0x1, b"Hello", mask=b"\x37\xfa\x21\x3d") == bytes.fromhex("818537fa213d7f9f4d5158"))


# --- 2. against a fake DevTools server ------------------------------------------------
def server_frame(op, payload, fin=True):
    b0 = (0x80 if fin else 0) | op
    n = len(payload)
    if n < 126:
        return struct.pack("!BB", b0, n) + payload
    if n < 65536:
        return struct.pack("!BBH", b0, 126, n) + payload
    return struct.pack("!BBQ", b0, 127, n) + payload


class FakeChrome:
    """Chrome's DevTools as secret-fill sees it: /json/version, /json/list, a browser socket
    (SystemInfo) and page sockets that answer for a model of frames and focus."""

    def __init__(self, pages, pid=4242, browser="Chrome/150.0", routes=None):
        self.pages, self.pid, self.browser = pages, pid, browser
        # What the browser endpoint answers about its command line (absent: that call fails).
        self.browser_routes = routes if routes is not None else {
            "getInfo": "/opt/google/chrome/chrome --remote-debugging-port=0 --password-store=basic "
                       "--user-data-dir=/home/u/.local/state/cua-driver/browser-profiles/p1 about:blank"}
        self.inserted, self.unmasked, self.pongs, self.routes = [], 0, 0, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.conn, args=(c,), daemon=True).start()

    def http(self, c, obj):
        body = json.dumps(obj).encode()
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)

    def conn(self, c):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = c.recv(4096)
                if not chunk:
                    return
                head += chunk
            head, rest = head.split(b"\r\n\r\n", 1)
            lines = head.decode().split("\r\n")
            path = lines[0].split()[1]
            hdrs = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
            if path == "/json/version":
                return self.http(c, {"Browser": self.browser, "Protocol-Version": "1.3",
                                     "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/b1"})
            if path.startswith("/json"):
                return self.http(c, [{"id": p["id"], "type": p.get("type", "page"), "url": p["url"],
                                      "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/{p['id']}"}
                                     for p in self.pages])
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Accept: " + sf.ws_accept(hdrs["sec-websocket-key"]) + "\r\n\r\n").encode())
            page = None if path.endswith("/browser/b1") else next(p for p in self.pages if path.endswith("/" + p["id"]))
            self.ws(c, page, rest)
        finally:
            c.close()

    def ws(self, c, page, buf):
        n = 0
        while True:
            f = sf.ws_decode_frame(buf)
            if not f:
                chunk = c.recv(65536)
                if not chunk:
                    return
                buf += chunk
                continue
            if not (buf[1] & 0x80):
                self.unmasked += 1
            buf = buf[f[3]:]
            _, op, payload, _ = f
            if op == 0x8:
                return
            if op == 0xA:
                self.pongs += 1
                continue
            if op != 0x1:
                continue
            msg = json.loads(payload)
            if page is not None:
                page["_mid"] = msg["id"]
            try:
                result = self.answer(page, msg["method"], msg.get("params") or {})
                reply = None if result is DEFER else {"id": msg["id"], "result": result}
            except LookupError as e:
                reply = {"id": msg["id"], "error": {"code": -32000, "message": str(e)}}
            n += 1
            # An event and a ping before each answer; every other answer in two fragments.
            c.sendall(server_frame(0x1, json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}).encode()))
            c.sendall(server_frame(0x9, b"hi"))
            if reply is not None:
                data = json.dumps(reply).encode()
                c.sendall(server_frame(0x1, data[:7], fin=False) + server_frame(0x0, data[7:]) if n % 2 else server_frame(0x1, data))
            # What the page queued meanwhile: paused requests (Fetch) and an answer it held back (Page.navigate).
            while page is not None and page.get("outbox"):
                c.sendall(server_frame(0x1, json.dumps(page["outbox"].pop(0)).encode()))

    def tree(self, page, fid):
        f = page["frames"][fid]
        node = {"frame": {"id": fid, "securityOrigin": f["origin"], "url": f["url"], "loaderId": f.get("loader", "L-" + fid)}}
        kids = [k for k, v in page["frames"].items() if v.get("parent") == fid]
        if kids:
            node["childFrames"] = [self.tree(page, k) for k in kids]
        return node

    def answer(self, page, method, params):
        if page is None:
            b = self.browser_routes
            if method == "SystemInfo.getInfo":
                if "getInfo" not in b:
                    raise LookupError("GPU info not available")
                return {"gpu": {}, "modelName": "", "modelVersion": "", "commandLine": b["getInfo"]}
            if method == "SystemInfo.getProcessInfo":
                return {"processInfo": [{"type": "renderer", "id": self.pid + 1}, {"type": "browser", "id": self.pid}]}
            if method == "Browser.getBrowserCommandLine":
                if "automation" not in b:
                    raise LookupError("Command line not returned because --enable-automation not set.")
                return {"arguments": b["automation"]}
            return {}
        frames = page["frames"]
        ctx_of = {fid: 100 + i for i, fid in enumerate(frames)}
        fid_of = {v: k for k, v in ctx_of.items()}
        if method in ("Fetch.enable", "Fetch.disable"):
            page["fetch"] = method == "Fetch.enable"
            if method == "Fetch.disable" and page.get("nav"):
                # As Chromium's interceptor does when it's torn down: what's still paused goes on.
                nav, page["nav"] = page["nav"], None
                page.setdefault("visited", []).append(nav["hops"][nav["i"]])
                self.commit(page, nav["hops"][nav["i"]])
            return {}
        if method == "Page.stopLoading":
            page["stopped_loading"] = True
            if page.get("nav"):
                page["nav"] = None           # the paused navigation is cancelled
            return {}
        if method == "Page.navigate":
            # An opened link (secret-fill's open mode). The hops: the link's site, then each server redirect (hops=[...],
            # or redirect_to). Without Fetch the tab simply ends on the last hop. With Fetch each hop's request is paused
            # (Fetch.requestPaused) until it's continued or failed, and the answer to Page.navigate waits for that.
            # no_commit: it never commits.
            page["navigated"] = params.get("url")
            hops = [sf.parse_origin(params["url"])] + list(page.get("hops") or ([page["redirect_to"]] if page.get("redirect_to") else []))
            page["visited"] = []
            if page.get("no_commit"):
                return {"frameId": "F0", "loaderId": "L-0"}
            if not page.get("fetch"):
                self.commit(page, hops[-1])
                page["visited"] += hops
                return {"frameId": "F0", "loaderId": "L-nav"}
            page["nav"] = {"id": page["_mid"], "hops": hops, "i": 0}
            self.pause(page)
            return DEFER
        if method in ("Fetch.continueRequest", "Fetch.failRequest"):
            nav = page.get("nav")
            if not nav:
                return {}
            hop = nav["hops"][nav["i"]]
            if method == "Fetch.failRequest":
                page.setdefault("blocked", []).append(hop)
                if nav["id"] is not None:
                    page.setdefault("outbox", []).append({"id": nav["id"], "result": {"frameId": "F0", "loaderId": "L-nav",
                                                                                      "errorText": "net::ERR_BLOCKED_BY_CLIENT"}})
                page["nav"] = None
                return {}
            page["visited"].append(hop)
            nav["i"] += 1
            if nav["i"] < len(nav["hops"]):
                self.pause(page)
            else:
                self.commit(page, hop)
                if nav["id"] is not None:
                    page.setdefault("outbox", []).append({"id": nav["id"], "result": {"frameId": "F0", "loaderId": "L-nav"}})
                page["nav"] = None
            return {}
        if method == "Page.getFrameTree":
            page["tree_calls"] = page.get("tree_calls", 0) + 1
            if "after_nav" in page and page.get("tree_error_at") and page["after_nav"] + 1 == page["tree_error_at"] and page.get("fetch"):
                # A protocol error while it watches, with a move to another site paused right behind the error's answer.
                page["after_nav"] += 1
                page["nav"] = {"id": None, "hops": [page["tree_error_to"]], "i": 0}
                self.pause(page)
                raise LookupError("Internal error while reading the frame tree")
            if "after_nav" in page:
                page["after_nav"] += 1
                # The page's own script moves it on, a few tree reads (a quarter second each) after it loaded.
                if page.get("redirect_later") and page["after_nav"] == page.get("later_at", 3):
                    if page.get("fetch"):
                        page["nav"] = {"id": None, "hops": [page["redirect_later"]], "i": 0}
                        self.pause(page)
                    else:
                        self.commit(page, page["redirect_later"])
            if page.get("hang"):
                time.sleep(60)
            if page.get("reload_after") and page["tree_calls"] > page["reload_after"]:
                for f in frames.values():
                    f["loader"] = "L-new"
            return {"frameTree": self.tree(page, "F0")}
        if method == "Page.createIsolatedWorld":
            fid = params["frameId"]
            if fid not in frames or frames[fid].get("oopif"):
                raise LookupError("No frame for given id found")
            return {"executionContextId": ctx_of[fid]}
        if method == "Runtime.evaluate":
            e, fid = params["expression"], fid_of.get(params.get("contextId"))
            if "document.hasFocus()" in e:
                return {"result": {"type": "object", "value": {"focus": page.get("focus", False), "visible": page.get("visible", True)}}}
            if fid is None:
                raise LookupError("evaluate outside an isolated world")
            fr = frames[fid]
            if "mcomp:deep-active" in e:
                if not fr.get("active"):
                    return {"result": {"type": "object", "subtype": "null", "value": None}}
                return {"result": {"type": "object", "subtype": "node", "objectId": "el:" + fid}}
            if "mcomp:total" in e:
                return {"result": {"type": "object", "value": {"n": 1, "text": page.get("total_text", ""), "visible": page.get("total_visible", True)}}}
            if "mcomp:frame-origin" in e:
                return {"result": {"type": "string", "value": fr.get("self_origin", fr["origin"])}}
            if "mcomp:focus-selector" in e:
                n = page.get("selector_matches", 1)
                if n == 1:
                    fr["active"] = fr.get("active") or {"element": dict(PW)}
                return {"result": {"type": "number", "value": n}}
        if method == "DOM.describeNode":
            fid = params["objectId"].split(":", 1)[1]
            a = frames[fid]["active"]
            if "iframe" in a:
                return {"node": {"nodeName": "IFRAME", "backendNodeId": 50, "frameId": a["iframe"]}}
            return {"node": {"nodeName": a["element"]["tag"].upper(), "backendNodeId": page.get("backend", 7)}}
        if method == "Runtime.callFunctionOn":
            fn = params["functionDeclaration"]
            fid = params["objectId"].split(":", 1)[1]
            if "mcomp:describe" in fn:
                return {"result": {"type": "object", "value": frames[fid]["active"]["element"]}}
            if "mcomp:select" in fn:
                page["value"] = ""
                return {"result": {"type": "boolean", "value": True}}
            if "mcomp:value-length" in fn:
                return {"result": {"type": "number", "value": sf.utf16_len(page.get("value", ""))}}
            if "mcomp:is-focused" in fn:
                return {"result": {"type": "boolean", "value": True}}
            if "mcomp:insert" in fn:
                if page.get("lost_focus"):
                    return {"result": {"type": "object", "value": {"done": False, "focus": False}}}
                if page.get("no_exec"):
                    return {"result": {"type": "object", "value": {"done": False, "focus": True}}}
                text = params["arguments"][0]["value"]
                self.inserted.append((page["id"], text))
                self.routes.append("element")
                page["value"] = text[:page["maxlen"]] if page.get("maxlen") else text
                if page.get("moves_focus"):
                    page["backend"] = 99
                if page.get("insert_error"):
                    raise LookupError("cannot insert " + text)
                if page.get("navigate_on_insert"):
                    for f in frames.values():
                        f["loader"] = "L-new"
                return {"result": {"type": "object", "value": {"done": True, "focus": True}}}
        if method == "Input.insertText":
            self.routes.append("keyboard")
            self.inserted.append((page["id"], params["text"]))
            v = page.get("value", "") + params["text"]
            page["value"] = v[:page["maxlen"]] if page.get("maxlen") else v
            if page.get("moves_focus"):
                page["backend"] = 99
            if page.get("insert_error"):
                raise LookupError("cannot insert " + params["text"])
            return {}
        return {}

    def commit(self, page, origin):
        f0 = page["frames"]["F0"]
        f0.update({"origin": origin, "url": origin + "/", "loader": "L-nav" if f0.get("loader", "L-F0") != "L-nav" else "L-nav2"})
        f0.pop("self_origin", None)
        page["after_nav"] = 0

    def pause(self, page):
        nav = page["nav"]
        page.setdefault("outbox", []).append({"method": "Fetch.requestPaused", "params": {
            "requestId": "R%d-%d" % (len(page.get("visited") or []), nav["i"]), "frameId": "F0", "resourceType": "Document",
            "request": {"url": nav["hops"][nav["i"]] + "/next"}}})

    def close(self):
        self.sock.close()


DEFER = object()
SITE = "https://login.example.com"
EVIL = "https://evil.example"
PW = {"tag": "input", "type": "password"}


def page(pid="A", url=SITE + "/login", frames=None, **kw):
    p = {"id": pid, "url": url, "frames": frames or {"F0": {"origin": SITE, "url": url, "active": {"element": dict(PW)}}}}
    p.update(kw)
    return p


def run_script(req, net=(), uid=UID, proc_root="/nonexistent", extra=()):
    args = [sys.executable, "-I", SCRIPT, "--proc-root", proc_root, *extra]
    if uid is not None:
        args += ["--desktop-uid", str(uid)]
    for n in net:
        args += ["--proc-net", n]
    r = subprocess.run(args, input=json.dumps(req).encode(), capture_output=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    out, err = r.stdout.decode(), r.stderr.decode()
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {"unparsed": out, "stderr": err}
    return res, out + err


def fake_suite(td):
    # A plain web server and a non-HTTP listener on loopback: listed, probed, ignored.
    class NotFound(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_error(404)

        def log_message(self, *a):
            pass
    decoy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
    threading.Thread(target=decoy.serve_forever, daemon=True).start()
    mute = socket.socket()
    mute.bind(("127.0.0.1", 0))
    mute.listen(1)
    proc = os.path.join(td, "proc")
    os.makedirs(os.path.join(proc, "4242"))
    open(os.path.join(proc, "4242", "cmdline"), "wb").write(
        b"/opt/google/chrome/chrome\0--remote-debugging-port=0\0--user-data-dir=/home/u/.local/state/cua-driver/browser-profiles/p1\0")

    def net_for(*ports, other_uid_port=None):
        p = os.path.join(td, f"tcp-{time.time_ns()}")
        lines = [TCP_HEAD] + [tcp_line(i, "0100007F", port) for i, port in enumerate(ports)]
        lines.append(tcp_line(90, "0100007F", decoy.server_address[1]))
        lines.append(tcp_line(91, "0100007F", mute.getsockname()[1]))
        lines.append(tcp_line(92, "00000000", 5900))
        if other_uid_port:
            lines.append(tcp_line(93, "0100007F", other_uid_port, uid=0))
        open(p, "w").write("\n".join(lines) + "\n")
        return [p]

    def scenario(name, pages, req, expect, uid=UID, extra=(), **chrome_kw):
        chrome = FakeChrome(pages, **chrome_kw)
        base = {"mode": "fill", "field": "password", "origins": [SITE], "value": VALUE}
        base.update(req)
        res, raw = run_script(base, net_for(chrome.port), uid=uid, proc_root=proc, extra=extra)
        check(f"fake: {name}", expect(res, chrome), json.dumps(res)[:240])
        check(f"fake: {name}: value not in the output", VALUE not in raw)
        if chrome.unmasked:
            check(f"fake: {name}: client frames masked", False, f"{chrome.unmasked} unmasked")
        chrome.close()
        return chrome

    c = scenario("fills the focused password field, through the element itself", [page()], {},
                 lambda r, c: r.get("ok") and r.get("chars") == len(VALUE) and r.get("origin") == SITE and c.inserted == [("A", VALUE)]
                 and r.get("route") == "element" and c.routes == ["element"])
    check("fake: answered the server's pings", c.pongs >= 1)
    scenario("a page that won't take execCommand: the keyboard route, after re-checking", [page(no_exec=True)], {},
             lambda r, c: r.get("ok") and r.get("route") == "keyboard" and c.routes == ["keyboard"])
    scenario("the field lost the focus just before the fill: nothing typed", [page(lost_focus=True)], {},
             lambda r, c: r.get("denied") and "lost the focus" in r["reason"] and not c.inserted)
    scenario("the page reloaded between the check and the typing: nothing typed", [page(reload_after=1)], {},
             lambda r, c: r.get("denied") and "changed after the check" in r["reason"] and not c.inserted)
    scenario("the page navigated during the typing: reported", [page(navigate_on_insert=True)], {},
             lambda r, c: r.get("ok") is False and not r.get("denied") and "the page changed during the fill" in r["reason"])
    scenario("the browser and the page's own world disagree about the site: refused",
             [page(frames={"F0": {"origin": SITE, "url": SITE, "self_origin": EVIL, "active": {"element": PW}}})], {},
             lambda r, c: r.get("denied") and "no confirmed site" in r["reason"] and not c.inserted)
    scenario("without the desktop user's uid, nothing is filled (fail closed)", [page()], {},
             lambda r, c: r.get("ok") is False and "which user runs the desktop" in r["reason"] and not c.inserted, uid=None)
    t0 = time.time()
    scenario("an overall deadline: a hung browser can't keep it waiting with the value", [page(hang=True)], {},
             lambda r, c: r.get("ok") is False and "ran out of time" in r["reason"] and time.time() - t0 < 15,
             extra=("--deadline", "3"))
    scenario("check mode types nothing", [page()], {"mode": "check", "value": None},
             lambda r, c: r.get("ok") and r.get("mode") == "check" and not c.inserted)
    scenario("no tab on an allowed site", [page(url="https://other.example/")], {},
             lambda r, c: r.get("denied") and "no open tab" in r["reason"] and not c.inserted)
    scenario("the browser's frame tree beats the tab list", [page(frames={"F0": {"origin": EVIL, "url": EVIL + "/", "active": {"element": PW}}})], {},
             lambda r, c: r.get("denied") and "the tab is on " + EVIL in r["reason"] and not c.inserted)
    scenario("wrong field type", [page(frames={"F0": {"origin": SITE, "url": SITE, "active": {"element": {"tag": "input", "type": "text"}}}})], {},
             lambda r, c: r.get("denied") and "not a password field" in r["reason"] and not c.inserted)
    scenario("username into an email field", [page(frames={"F0": {"origin": SITE, "url": SITE, "active": {"element": {"tag": "input", "type": "email"}}}})],
             {"field": "username", "value": "dummy-user-qx7"}, lambda r, c: r.get("ok") and c.inserted == [("A", "dummy-user-qx7")])
    # A purchase's payment frame (top_origins): the frame's site may hold the field, never be the tab itself.
    PAY = "https://pay.example"
    TXT = {"tag": "input", "type": "text"}
    scenario("top_origins: a tab on the payment frame's own site isn't a destination: refused, nothing typed",
             [page(url=PAY + "/card", frames={"F0": {"origin": PAY, "url": PAY + "/card", "active": {"element": dict(TXT)}}})],
             {"field": "text", "origins": [SITE, PAY], "top_origins": [SITE]},
             lambda r, c: r.get("denied") and "no open tab on " + SITE in r["reason"] and not c.inserted and "top_checked" not in r)
    scenario("top_origins: ...a tab that is on the shop, whose frame holds the field, is filled, and the answer says top_checked",
             [page(frames={"F0": {"origin": SITE, "url": SITE + "/pay", "active": {"iframe": "F1"}},
                           "F1": {"origin": PAY, "url": PAY + "/card", "parent": "F0", "active": {"element": dict(TXT)}}})],
             {"field": "text", "origins": [SITE, PAY], "top_origins": [SITE]},
             lambda r, c: r.get("ok") is True and r.get("top_checked") is True and r.get("origin") == SITE and c.inserted == [("A", VALUE)])
    scenario("top_origins: a top_origins naming a site the request's origins don't hold is refused as a bad request",
             [page()], {"origins": [SITE], "top_origins": [PAY]},
             lambda r, c: r.get("ok") is False and r.get("reason") == "bad request" and not c.inserted)
    scenario("read total: the element's rendered text and whether it's shown", [page(total_text="Total $12.34")],
             {"mode": "read", "what": "total", "selector": "#total", "value": None, "top_origins": [SITE]},
             lambda r, c: r.get("ok") is True and r.get("text") == "Total $12.34" and r.get("visible") is True and r.get("top_checked") is True)
    scenario("read total: an element that isn't shown says so", [page(total_text="Total $1.00", total_visible=False)],
             {"mode": "read", "what": "total", "selector": "#total", "value": None},
             lambda r, c: r.get("ok") is True and r.get("visible") is False)
    # Open mode (a one-time sign-in link): every navigation of the tab's page to a site the vault didn't name is stopped
    # before it's sent (Fetch interception), for the whole window; the tab must end on a named site.
    link = SITE + "/verify?t=" + VALUE
    opn = {"mode": "open", "field": "text", "value": link, "link_origin": SITE, "final_origins": [SITE]}
    lw = ("--land-wait", "3")
    scenario("open: a link that lands on the site is opened, and says where it landed", [page()], opn,
             lambda r, c: r.get("ok") is True and r.get("mode") == "open" and r.get("origin") == SITE and c.pages[0].get("navigated") == link,
             extra=lw)
    scenario("open: a server redirect to another site is stopped before the tab visits it, without naming it", [page(redirect_to=EVIL)], opn,
             lambda r, c: r.get("ok") is False and "stopped before it was visited" in r.get("reason", "") and "evil" not in json.dumps(r)
             and EVIL not in c.pages[0].get("visited", []) and c.pages[0].get("blocked") == [EVIL], extra=lw)
    scenario("open: a redirect through another site and back to the site is stopped at that hop (it never passes)",
             [page(hops=[EVIL, SITE])], opn,
             lambda r, c: r.get("ok") is False and "stopped" in r.get("reason", "") and EVIL not in c.pages[0].get("visited", []),
             extra=lw)
    scenario("open: a page that moves to another site soon after it loads is stopped too", [page(redirect_later=EVIL)], opn,
             lambda r, c: r.get("ok") is False and "stopped" in r.get("reason", "") and "evil" not in json.dumps(r), extra=lw)
    scenario("open: a page that moves two seconds after it loads (long after it looked settled) is stopped too",
             [page(redirect_later=EVIL, later_at=9)], opn,
             lambda r, c: r.get("ok") is False and "stopped" in r.get("reason", "") and "evil" not in json.dumps(r), extra=lw)
    scenario("open: an error while it watches, with a move to another site paused behind it: the paused request is "
             "refused and loading stopped before the interception ends (the tab never visits it), and it's a failure",
             [page(tree_error_at=2, tree_error_to=EVIL)], opn,
             lambda r, c: r.get("ok") is False and "couldn't be watched to the end" in r.get("reason", "")
             and EVIL not in c.pages[0].get("visited", []) and c.pages[0].get("stopped_loading") is True and "evil" not in json.dumps(r),
             extra=lw)
    scenario("open: a navigation that never commits isn't reported as opened", [page(no_commit=True)], opn,
             lambda r, c: r.get("ok") is False and "didn't settle" in r.get("reason", ""), extra=lw)
    scenario("open: a link whose own site isn't the one the vault named is refused before anything is opened", [page()],
             dict(opn, value=EVIL + "/x?t=" + VALUE),
             lambda r, c: r.get("denied") and "the link isn't one" in r.get("reason", "") and not c.pages[0].get("navigated"))
    two = {"F0": {"origin": SITE, "url": SITE, "active": {"iframe": "F1"}},
           "F1": {"origin": EVIL, "url": EVIL + "/f", "parent": "F0", "active": {"element": PW}}}
    scenario("field in a cross-origin frame refused", [page(frames=two)], {},
             lambda r, c: r.get("denied") and "frame from another site (" + EVIL + ")" in r["reason"] and not c.inserted)
    same = {"F0": {"origin": SITE, "url": SITE, "active": {"iframe": "F1"}},
            "F1": {"origin": SITE, "url": SITE + "/f", "parent": "F0", "active": {"element": PW}}}
    scenario("field in a same-origin frame filled", [page(frames=same)], {},
             lambda r, c: r.get("ok") and r.get("frame_origin") == SITE and c.inserted == [("A", VALUE)])
    srcdoc = {"F0": {"origin": SITE, "url": SITE, "active": {"iframe": "F1"}},
              "F1": {"origin": "://", "url": "about:srcdoc", "parent": "F0", "self_origin": SITE, "active": {"element": PW}}}
    scenario("srcdoc frame: its inherited site", [page(frames=srcdoc)], {}, lambda r, c: r.get("ok") and r.get("frame_origin") == SITE)
    sandboxed = {"F0": {"origin": SITE, "url": SITE, "active": {"iframe": "F1"}},
                 "F1": {"origin": "://", "url": "about:srcdoc", "parent": "F0", "self_origin": "null", "active": {"element": PW}}}
    scenario("sandboxed srcdoc frame refused", [page(frames=sandboxed)], {},
             lambda r, c: r.get("denied") and "no site" in r["reason"] and not c.inserted)
    oopif = {"F0": {"origin": SITE, "url": SITE, "active": {"iframe": "F1"}},
             "F1": {"origin": SITE, "url": SITE + "/f", "parent": "F0", "oopif": True, "active": {"element": PW}}}
    scenario("a frame the fill can't look into refused", [page(frames=oopif)], {},
             lambda r, c: r.get("denied") and "can't look into" in r["reason"] and not c.inserted)
    scenario("nothing focused", [page(frames={"F0": {"origin": SITE, "url": SITE, "active": None}})], {},
             lambda r, c: r.get("denied") and "no field is focused" in r["reason"] and not c.inserted)
    scenario("read-only field refused", [page(frames={"F0": {"origin": SITE, "url": SITE, "active": {"element": dict(PW, readonly=True)}}})], {},
             lambda r, c: r.get("denied") and "read-only" in r["reason"] and not c.inserted)
    scenario("two tabs, even with one focused: refused (focus or visibility never chooses where a value goes)",
             [page(pid="A", focus=False), page(pid="B", focus=True)], {},
             lambda r, c: r.get("denied") and "2 tabs" in r["reason"] and not c.inserted)
    scenario("browser_pid: the Chrome whose browser process has that id", [page()], {"browser_pid": 4242},
             lambda r, c: r.get("ok") and c.inserted == [("A", VALUE)])
    scenario("browser_pid: another Chrome's id finds none, and nothing is typed", [page()], {"browser_pid": 5555},
             lambda r, c: r.get("ok") is False and "browser process 5555" in r["reason"] and not c.inserted)
    scenario("browser_pid: not a number is refused", [page()], {"browser_pid": "4242"},
             lambda r, c: r.get("denied") and r["reason"] == "bad browser pid" and not c.inserted)
    scenario("two tabs, neither focused nor alone visible: refused", [page(pid="A"), page(pid="B")], {},
             lambda r, c: r.get("denied") and "2 tabs" in r["reason"] and not c.inserted)
    scenario("focus moved during the fill", [page(moves_focus=True)], {},
             lambda r, c: r.get("ok") is False and not r.get("denied") and "focus moved" in r["reason"])
    scenario("length limit reported", [page(maxlen=4)], {}, lambda r, c: r.get("ok") is False and "holds 4 characters" in r["reason"])
    scenario("an error answer to the typing never repeats the value", [page(insert_error=True)], {},
             lambda r, c: r.get("ok") is False and "nothing was typed" in r.get("reason", ""))
    scenario("...nor on the keyboard route", [page(insert_error=True, no_exec=True)], {},
             lambda r, c: r.get("ok") is False and r.get("reason") == "Input.insertText failed")
    scenario("non-ASCII value counted in UTF-16", [page()], {"value": "Dümmy-✓-\U0001F600"},
             lambda r, c: r.get("ok") and r.get("chars") == sf.utf16_len("Dümmy-✓-\U0001F600"))
    scenario("selector focuses exactly one element", [page(frames={"F0": {"origin": SITE, "url": SITE, "active": None}})], {"selector": "#pw"},
             lambda r, c: r.get("ok") and c.inserted == [("A", VALUE)])
    scenario("selector matching two elements refused", [page(selector_matches=2)], {"selector": "input"},
             lambda r, c: r.get("denied") and "matches 2" in r["reason"] and not c.inserted)
    scenario("invalid selector: a clear message", [page(selector_matches=-1)], {"selector": "#a >>> #b"},
             lambda r, c: r.get("denied") and "isn't valid CSS" in r["reason"] and not c.inserted)
    scenario("a target that isn't a tab is ignored", [page(type="service_worker")], {},
             lambda r, c: r.get("denied") and "no open tab" in r["reason"])
    scenario("profile: from the browser's command line (SystemInfo.getInfo), no /proc needed", [page()], {"profile": "p1"},
             lambda r, c: r.get("ok") and c.inserted == [("A", VALUE)], pid=999999)
    scenario("profile: from the browser pid and /proc when getInfo fails", [page()], {"profile": "p1"},
             lambda r, c: r.get("ok") and c.inserted == [("A", VALUE)], routes={})
    scenario("profile: from getBrowserCommandLine when that's the one that answers", [page()], {"profile": "p1"},
             lambda r, c: r.get("ok") and c.inserted == [("A", VALUE)], pid=999999,
             routes={"automation": ["--remote-debugging-port=0", "--user-data-dir=/x/browser-profiles/p1"]})
    scenario("profile: when no route works, the reason says why for each", [page()], {"profile": "p1"},
             lambda r, c: r.get("ok") is False and "no Chrome is running on profile p1" in r["reason"]
             and "getInfo: Failed" in r["reason"] and "/proc/999999/cmdline: FileNotFoundError" in r["reason"]
             and "getBrowserCommandLine: Failed" in r["reason"] and not c.inserted, pid=999999, routes={})
    scenario("profile: another profile's Chrome isn't used (and the reason names it)", [page()], {"profile": "p2"},
             lambda r, c: r.get("ok") is False and "no Chrome is running on profile p2" in r["reason"] and "profile p1" in r["reason"]
             and not c.inserted)
    scenario("a DevTools-like server for another browser is ignored", [page()], {},
             lambda r, c: r.get("ok") is False and "no Chrome with DevTools" in r["reason"] and not c.inserted, browser="Firefox/140")

    chrome = FakeChrome([page()])
    res, raw = run_script({"mode": "fill", "field": "password", "origins": [SITE], "value": VALUE},
                          net_for(other_uid_port=chrome.port), proc_root=proc)
    check("fake: a listener owned by another user isn't probed", "no Chrome with DevTools" in res.get("reason", "") and not chrome.inserted)
    chrome.close()
    res, raw = run_script({"mode": "fill", "field": "password", "origins": [], "value": VALUE}, net_for())
    check("fake: no origins is a bad request", res.get("reason") == "bad request" and VALUE not in raw)
    res, raw = run_script({"mode": "fill", "field": "password", "origins": [SITE], "profile": "../x", "value": VALUE}, net_for())
    check("fake: bad profile name refused", res.get("denied") and res.get("reason") == "bad profile name")
    r = subprocess.run([sys.executable, "-I", SCRIPT], input=b"not json", capture_output=True, timeout=30)
    check("fake: unreadable request", json.loads(r.stdout).get("reason") == "bad request")
    decoy.shutdown()
    mute.close()


# --- 3. against a real headless Chromium ----------------------------------------------
def find_chrome():
    c = os.environ.get("MCOMP_TEST_CHROME")
    if c:
        return c if os.access(c, os.X_OK) else None
    newest_first = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")), reverse=True)
    for c in newest_first + ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                             "/usr/bin/google-chrome-stable", "/opt/google/chrome/chrome"]:
        if os.access(c, os.X_OK):
            return c
    return None


LOGIN_HTML = """<!doctype html><meta charset="utf-8"><title>dummy login</title>
<form onsubmit="return false">
<input id="user" type="email" autocomplete="username">
<input id="pw" type="password" autocomplete="current-password">
<input id="code" type="text" inputmode="numeric">
<textarea id="notes"></textarea>
<input id="ro" type="password" readonly>
<input id="short" type="password" maxlength="4">
</form>
<iframe id="same" src="/frame.html"></iframe>
<iframe id="cross" src="http://127.0.0.1:%(other)d/frame.html"></iframe>
<iframe id="srcdoc" srcdoc='<input id="sdpw" type="password">'></iframe>
<div id="host"></div>
<script>document.getElementById('host').attachShadow({mode: 'open'}).innerHTML = '<input id="spw" type="password">';</script>
"""
FRAME_HTML = """<!doctype html><meta charset="utf-8"><input id="fpw" type="password">"""
# A page that lies to scripts in its own world: every input claims to be a password field,
# the focused element is always #pw2, and self.origin names another site.
# Totals a person can't see, each a different way (F12): the read must say they aren't shown; only #shown is.
TOTALS_HTML = """<!doctype html><meta charset="utf-8"><title>totals</title><style>body{font:16px sans-serif}</style>
<p id="shown">Total $12.34</p>
<div style="opacity:0"><p id="opa">Total $1.00</p></div>
<div style="visibility:hidden"><p id="vis">Total $1.00</p></div>
<p id="off" style="position:absolute; left:-10000px; top:0">Total $1.00</p>
<p id="sr" style="position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0)">Total $1.00</p>
<div style="height:0; overflow:hidden"><p id="zero">Total $1.00</p></div>
<p id="tiny" style="font-size:1px">Total $1.00</p>
<p id="clear" style="color:transparent">Total $1.00</p>
<div style="filter:opacity(0)"><p id="filt">Total $1.00</p></div>
<div style="clip-path:inset(50%)"><p id="cpath">Total $1.00</p></div>
<div style="clip-path:polygon(0 0, 10px 0, 20px 0)"><p id="cpoly">Total $1.00</p></div>
<div style="clip-path:path('M0 0 L1 0 Z')"><p id="cpathfn">Total $1.00</p></div>
<div style="width:300px; height:0; contain:paint"><p id="contain">Total $1.00</p></div>
<div style="width:200px; height:100px; clip-path:polygon(0px 0px, 50% 100%, 100px 100px)"><p id="mixpoly">Total $1.00</p></div>
<div style="clip-path:inset(0px 100% 0px 0px)"><p id="collapse">Total $1.00</p></div>
<div style="clip-path:inset(0px 10px 0px 0px)"><p id="okclip">Total $2.00</p></div>
<div style="width:200px; height:100px; clip-path:polygon(evenodd, 0px 0px, 100% 0px, 100% 100%, 0px 100%, 0px 0px, 100% 0px, 100% 100%, 0px 100%)"><p id="evenodd">Total $1.00</p></div>
<div style="width:200px; height:100px; clip-path:circle(10px at 10000px 10000px)"><p id="farcircle">Total $1.00</p></div>
<div style="width:200px; height:100px; clip-path:polygon(500px 500px, 600px 500px, 550px 600px)"><p id="outpoly">Total $1.00</p></div>
<div style="width:200px; height:100px; clip-path:polygon(-50px -50px, 150px -50px, 150px 150px, -50px 150px)"><p id="partpoly">Total $2.00</p></div>
<div style="width:200px; height:100px; clip-path:circle(80px at 50% 50%)"><p id="okcircle">Total $2.00</p></div>
<div style="position:relative; width:200px; height:100px; clip-path:inset(0px 50% 0px 0px)"><p id="r23half" style="position:absolute; left:120px; top:10px; margin:0">Total $1.00</p></div>
<div style="width:200px; height:100px; clip-path:inset(0px 50% 0px 0px)"><div style="width:200px; height:100px; clip-path:inset(0px 0px 0px 50%)"><p id="r23opp" style="margin:0">Total $1.00</p></div></div>
<div style="position:relative; width:200px; height:100px; clip-path:polygon(0px 0px, 200px 0px, 200px 40px, 40px 40px, 40px 100px, 0px 100px)"><p id="r23ell" style="position:absolute; left:60px; top:55px; margin:0">Total $1.00</p></div>
<div style="width:200px; height:100px; clip-path:inset(0px 50% 0px 0px)"><p id="r23left" style="margin:0; width:90px">Total $2.00</p></div>
<div style="position:relative; width:200px; height:100px; clip-path:polygon(0px 0px, 200px 0px, 200px 40px, 40px 40px, 40px 100px, 0px 100px)"><p id="r23top" style="position:absolute; left:60px; top:5px; margin:0">Total $2.00</p></div>
<div style="width:200px; height:100px; transform:translateX(10px); clip-path:inset(0px 10px 0px 0px)"><p id="r23shift" style="margin:0">Total $2.00</p></div>
<div style="width:200px; height:100px; transform:scale(2); transform-origin:0 0; clip-path:circle(10px at 300px 50px)"><p id="r23scale" style="margin:0">Total $1.00</p></div>
<div style="height:220px"></div>
<div style="overflow-x:clip; height:20px"><p id="r29clipx" style="margin:0; position:relative; top:40px">Total $2.00</p></div>
<div style="overflow-x:clip; height:30px"><div style="display:flex"><div style="width:100px; height:120px"></div><aside style="position:sticky; top:0; align-self:flex-end; width:140px"><p id="r29sticky" style="margin:0">Total $2.00</p></aside></div></div>
<div style="height:120px"></div>
<div style="overflow-x:auto; width:300px"><table><tr><td>Item</td><td id="r29scroll">Total $2.00</td></tr></table></div>
<div style="overflow-x:auto; width:150px"><table><tr><td style="min-width:400px">Item</td><td id="r29scrolled">Total $1.00</td></tr></table></div>
<div style="overflow-y:clip; height:20px"><p id="r29clipy" style="margin:0; position:relative; top:40px">Total $1.00</p></div>
"""
# R29: the body's overflow-x is the viewport's (a common way to stop sideways scrolling): it doesn't cut what's below.
TOTALS2_HTML = """<!doctype html><meta charset="utf-8"><title>totals2</title><style>body{font:16px sans-serif; margin:0; overflow-x:hidden}</style>
<div style="height:40px">Shop</div>
<aside style="position:absolute; top:1500px; right:20px; width:200px"><p id="r29abs">Total $2.00</p></aside>
<div style="height:20px; overflow:hidden"><p id="r34fixed" style="position:fixed; top:250px; left:20px; margin:0">Total $2.00</p></div>
<div style="height:20px; overflow:hidden; transform:translateX(0px)"><p id="r34fixedcb" style="position:fixed; top:100px; left:300px; margin:0">Total $1.00</p></div>
<div style="height:20px; overflow:hidden"><p id="r34abs" style="position:absolute; top:400px; left:20px; margin:0">Total $2.00</p></div>
<div style="position:relative; height:20px; overflow:hidden"><p id="r34abscb" style="position:absolute; top:60px; left:0px; margin:0">Total $1.00</p></div>
<div style="height:20px; overflow:hidden; transform-style:preserve-3d"><p id="r39p3d" style="position:fixed; top:300px; left:20px; margin:0">Total $1.00</p></div>
<div style="position:relative; margin-left:300px; width:200px"><p id="r39covered" style="margin:0">Total $1.00</p><div style="position:absolute; inset:0; background:white"></div></div>
<div style="margin-left:300px"><p id="r39nopointer" style="margin:0; pointer-events:none">Total $1.00</p></div>
<p id="r41hiddenspan" style="margin:0; margin-left:300px">Total $2.00<span hidden>$3.00</span><span style="visibility:hidden">$4.00</span></p>
<p id="r42coverdiv" style="position:relative; margin:0; margin-left:300px; width:200px">Total $1.00<span style="position:absolute; inset:0; background:white"></span></p>
<p id="r42coverframe" style="position:relative; margin:0; margin-left:300px; width:200px">Total $1.00<iframe srcdoc="&lt;body style='margin:0;background:white'&gt;" style="position:absolute; left:0; top:0; width:100%; height:100%; border:0"></iframe></p>
<p id="r42inline" style="margin:0; margin-left:300px">Total <b>$2</b><span>.00</span></p>
"""

SHOWN_TOTALS = ("#shown", "#okclip", "#partpoly", "#okcircle", "#r23left", "#r23top", "#r23shift", "#r29clipx", "#r29sticky", "#r29scroll")

TAMPER_HTML = """<!doctype html><meta charset="utf-8"><input id="plain" type="text"><input id="pw2" type="password">
<script>
Object.defineProperty(HTMLInputElement.prototype, 'type', {get() { return 'password'; }});
Object.defineProperty(Document.prototype, 'activeElement', {get() { return document.getElementById('pw2'); }});
self.origin = 'https://evil.example';
</script>"""


def serve(directory):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def chrome_suite(td, chrome):
    a_dir, b_dir = os.path.join(td, "site-a"), os.path.join(td, "site-b")
    os.makedirs(a_dir)
    os.makedirs(b_dir)
    sa, sb = serve(a_dir), serve(b_dir)
    pa, pb = sa.server_address[1], sb.server_address[1]
    open(os.path.join(a_dir, "login.html"), "w").write(LOGIN_HTML % {"other": pb})
    open(os.path.join(a_dir, "tamper.html"), "w").write(TAMPER_HTML)
    open(os.path.join(a_dir, "totals.html"), "w").write(TOTALS_HTML)
    open(os.path.join(a_dir, "totals2.html"), "w").write(TOTALS2_HTML)
    for d in (a_dir, b_dir):
        open(os.path.join(d, "frame.html"), "w").write(FRAME_HTML)
    site, other = f"http://127.0.0.1:{pa}", f"http://127.0.0.1:{pb}"
    udd = os.path.join(td, "profiles", "real")
    os.makedirs(udd)
    proc = subprocess.Popen([chrome, "--headless", "--remote-debugging-port=0", f"--user-data-dir={udd}",
                             "--no-first-run", "--no-default-browser-check", "--disable-gpu", f"{site}/login.html"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port = None
        for _ in range(100):
            try:
                port = int(open(os.path.join(udd, "DevToolsActivePort")).readline())
                break
            except (OSError, ValueError):
                time.sleep(0.2)
        check("chrome: DevTools port", port is not None)
        if not port:
            return
        # What the VM's /proc would show: Chrome's loopback listener, and the browser's command line.
        net = os.path.join(td, "chrome-tcp")
        open(net, "w").write("\n".join([TCP_HEAD, tcp_line(0, "0100007F", port, uid=os.getuid()),
                                        tcp_line(1, "0100007F", pa, uid=os.getuid())]) + "\n")
        proc_root = os.path.join(td, "chrome-proc")
        os.makedirs(os.path.join(proc_root, str(proc.pid)))
        open(os.path.join(proc_root, str(proc.pid), "cmdline"), "wb").write(
            b"\0".join([chrome.encode(), b"--headless", f"--user-data-dir={udd}".encode(), b""]))
        ws = None
        for _ in range(50):
            tabs = [t for t in sf.page_targets(port) if t["url"].startswith(site)]
            if tabs:
                ws = tabs[0]["ws"]
                break
            time.sleep(0.2)
        h = sf.DevTools(ws)

        def wait_for(js):
            for _ in range(50):
                try:
                    if h.value(js):
                        return True
                except sf.Failed:      # not loaded yet
                    pass
                time.sleep(0.2)
            return False
        wait_for("document.readyState === 'complete' && !!document.getElementById('same').contentDocument"
                 " && !!document.getElementById('same').contentDocument.getElementById('fpw')"
                 " && !!document.getElementById('srcdoc').contentDocument.getElementById('sdpw')")

        def fill(focus_js, field="password", value=VALUE, origins=(site,), mode="fill", selector=None, profile=None):
            h.value("(function(){ const a = document.activeElement; if (a && a.blur) a.blur(); " + focus_js + "; return 1; })()")
            req = {"mode": mode, "field": field, "origins": list(origins), "value": value}
            if selector:
                req["selector"] = selector
            if profile:
                req["profile"] = profile
            # No /proc here: the profile comes from the browser's own command line (SystemInfo.getInfo).
            res, raw = run_script(req, [net], uid=os.getuid(), proc_root="/nonexistent")
            check(f"chrome: {field} via {(focus_js or selector or '')[:48]}: value not in the output", value not in raw)
            return res

        r = fill("document.getElementById('pw').focus()")
        check("chrome: fills a password field, through the element itself", r.get("ok") and r.get("chars") == len(VALUE)
              and r.get("origin") == site and r.get("route") == "element", r)
        check("chrome: the page holds the value", h.value(f"document.getElementById('pw').value === {json.dumps(VALUE)}"))
        r = fill("document.getElementById('pw').focus()", value="Second-Dummy-77", profile="real")
        check("chrome: a second fill replaces the first; the profile is found from the browser's pid", r.get("ok")
              and h.value("document.getElementById('pw').value === 'Second-Dummy-77'"), r)
        r = fill("document.getElementById('pw').focus()", profile="other")
        check("chrome: another profile's name finds no Chrome", r.get("ok") is False and "profile other" in r.get("reason", ""), r)
        r = fill("document.getElementById('user').focus()")
        check("chrome: password refused in an email field", r.get("denied") and "not a password field" in r["reason"], r)
        r = fill("document.getElementById('user').focus()", field="username", value="dummy-user-qx7")
        check("chrome: username into the email field", r.get("ok") and h.value("document.getElementById('user').value === 'dummy-user-qx7'"), r)
        r = fill("document.getElementById('code').focus()", field="otp", value="123456")
        check("chrome: code into a text field", r.get("ok") and h.value("document.getElementById('code').value === '123456'"), r)
        r = fill("document.getElementById('notes').focus()", field="text", value="dummy text value 2342")
        check("chrome: text into a textarea", r.get("ok") and r.get("chars") == 21, r)
        r = fill("document.getElementById('ro').focus()")
        check("chrome: read-only field refused", r.get("denied") and "read-only" in r["reason"], r)
        r = fill("document.getElementById('same').contentDocument.getElementById('fpw').focus()")
        check("chrome: same-origin frame filled", r.get("ok") and h.value(
            f"document.getElementById('same').contentDocument.getElementById('fpw').value === {json.dumps(VALUE)}"), r)
        r = fill("document.getElementById('srcdoc').contentDocument.getElementById('sdpw').focus()")
        check("chrome: srcdoc frame (inherits the page's site) filled", r.get("ok") and h.value(
            f"document.getElementById('srcdoc').contentDocument.getElementById('sdpw').value === {json.dumps(VALUE)}"), r)
        r = fill("document.getElementById('cross').focus()")
        check("chrome: field in a frame from another origin refused", r.get("denied") and "frame from another site" in r["reason"]
              and other in r["reason"], r)
        r = fill("document.getElementById('host').shadowRoot.getElementById('spw').focus()")
        check("chrome: field in a shadow root filled", r.get("ok") and h.value(
            f"document.getElementById('host').shadowRoot.getElementById('spw').value === {json.dumps(VALUE)}"), r)
        r = fill("document.getElementById('pw').focus()", origins=(other,))
        check("chrome: tab on a site the secret doesn't name refused", r.get("denied") and "no open tab" in r["reason"], r)
        r = fill("document.getElementById('short').focus()")
        check("chrome: length limit reported", r.get("ok") is False and "holds 4 characters" in r.get("reason", ""), r)
        h.value("document.getElementById('pw').value = ''")
        r = fill("", selector="#pw")
        check("chrome: --selector focuses the field", r.get("ok") and h.value(f"document.getElementById('pw').value === {json.dumps(VALUE)}"), r)
        r = fill("", selector="input[type=password]")
        check("chrome: --selector matching several refused", r.get("denied") and "matches" in r["reason"], r)
        r = fill("", selector="#same-site >>> #frame-password")
        check("chrome: an invalid selector gets a clear message", r.get("denied") and "isn't valid CSS" in r["reason"], r)
        h.value("document.getElementById('pw').value = ''")
        r = fill("document.getElementById('pw').focus()", mode="check")
        check("chrome: check mode types nothing", r.get("ok") and r.get("mode") == "check"
              and h.value("document.getElementById('pw').value === ''"), r)
        r = fill("")
        check("chrome: nothing focused refused", r.get("denied") and "no field is focused" in r["reason"], r)

        # The page's own scripts can't fool the checks (isolated worlds, the browser's origins).
        h.call("Page.navigate", {"url": f"{site}/tamper.html"})
        wait_for("location.pathname === '/tamper.html' && document.readyState === 'complete'")
        r = fill("document.getElementById('plain').focus()")
        check("chrome: a page that redefines type and activeElement can't pass a text field off as a password field",
              r.get("denied") and "not a password field" in r["reason"]
              and h.value("document.getElementById('plain').value === '' && document.getElementById('pw2').value === ''"), r)
        r = fill("document.getElementById('pw2').focus()")
        check("chrome: ...and the real password field still fills (self.origin rewritten by the page)", r.get("ok") and r.get("origin") == site, r)

        # A purchase's total (F12): shown to a person, or not, whatever hides it.
        h.call("Page.navigate", {"url": f"{site}/totals.html"})
        wait_for("location.pathname === '/totals.html' && document.readyState === 'complete'")
        seen = {}
        for sel in ("#shown", "#opa", "#vis", "#off", "#sr", "#zero", "#tiny", "#clear", "#filt", "#cpath", "#cpoly", "#cpathfn", "#contain",
                    "#mixpoly", "#collapse", "#okclip", "#evenodd", "#farcircle", "#outpoly", "#partpoly", "#okcircle",
                    "#r23half", "#r23opp", "#r23ell", "#r23left", "#r23top", "#r23shift", "#r23scale",
                    "#r29clipx", "#r29sticky", "#r29scroll", "#r29scrolled", "#r29clipy"):
            res, raw = run_script({"mode": "read", "what": "total", "field": "text", "origins": [site], "top_origins": [site], "selector": sel},
                                  [net], uid=os.getuid(), proc_root="/nonexistent")
            seen[sel] = (res.get("ok"), res.get("visible"), res.get("text"))
        check("chrome: a total is read as shown only when a person sees it: not inside a see-through or hidden ancestor, off the page, "
              "clipped away (an sr-only box, a zero-height ancestor, clip-path inset, a polygon with no area or a path, a zero-height "
              "paint-contained ancestor, a polygon of mixed units with no area, an inset that collapses its width, an even-odd "
              "polygon that traces its square twice, a circle or polygon outside the element), in 1px or transparent text, or "
              "under an opacity filter; a clip-path that leaves an area (an inset, a polygon partly outside, a circle) doesn't hide it",
              seen["#shown"] == (True, True, "Total $12.34") and all(seen[k][:2] == (True, True) for k in SHOWN_TOTALS[1:])
              and all(v[0] is True and v[1] is False for k, v in seen.items() if k not in SHOWN_TOTALS), seen)
        check("chrome (R23): what's shown is the total's box cut by every clip on the way up, together: a total outside its "
              "parent's clip, inside two clips that leave nothing between them, outside an L-shaped polygon's arms, or under a "
              "clip-path on a scaled box (the shape can't be placed exactly) is hidden; one inside its parent's clip, inside an "
              "L's arm, or under a clip-path on a moved (translated) box is shown",
              all(seen[k][:2] == (True, False) for k in ("#r23half", "#r23opp", "#r23ell", "#r23scale"))
              and all(seen[k][:2] == (True, True) for k in ("#r23left", "#r23top", "#r23shift")),
              {k: v for k, v in seen.items() if k.startswith("#r23")})
        h.call("Page.navigate", {"url": f"{site}/totals2.html"})
        wait_for("location.pathname === '/totals2.html' && document.readyState === 'complete'")
        before_scroll = h.value("window.scrollX + ',' + window.scrollY")
        for sel in ("#r29abs", "#r34fixed", "#r34fixedcb", "#r34abs", "#r34abscb", "#r39p3d", "#r39covered", "#r39nopointer",
                    "#r41hiddenspan", "#r42coverdiv", "#r42coverframe", "#r42inline"):
            res, raw = run_script({"mode": "read", "what": "total", "field": "text", "origins": [site], "top_origins": [site], "selector": sel},
                                  [net], uid=os.getuid(), proc_root="/nonexistent")
            seen[sel] = (res.get("ok"), res.get("visible"), res.get("text"))
        check("chrome (R29): overflow clips only the axes it names: a total below an overflow-x:clip box, or in a sticky order "
              "summary that hangs below one, is shown, as is one inside a sideways-scrolling table; one scrolled out of that table, or "
              "below an overflow-y:clip box, is hidden; and the body's overflow-x:hidden (the viewport's) doesn't hide a summary "
              "further down the page", all(seen[k][:2] == (True, True) for k in ("#r29clipx", "#r29sticky", "#r29scroll", "#r29abs"))
              and all(seen[k][:2] == (True, False) for k in ("#r29scrolled", "#r29clipy")),
              {k: v for k, v in seen.items() if k.startswith("#r29")})
        check("chrome (R34): an ancestor clips a fixed or absolute total only when it's in the total's chain of containing "
              "blocks: a fixed total below a 20px overflow:hidden wrapper, and an absolute one below such a wrapper that isn't "
              "positioned, are shown; under a transformed wrapper (fixed) or a positioned one (absolute) they're hidden",
              seen["#r34fixed"][:2] == (True, True) and seen["#r34abs"][:2] == (True, True)
              and seen["#r34fixedcb"][:2] == (True, False) and seen["#r34abscb"][:2] == (True, False),
              {k: v for k, v in seen.items() if k.startswith("#r34")})
        after_scroll = h.value("window.scrollX + ',' + window.scrollY")
        check("chrome (R39): the browser's own hit test must agree with the geometry: a fixed total clipped by a transform-style: "
              "preserve-3d ancestor (a containing block the rules didn't list), one covered by another element, and one with "
              "pointer-events: none are hidden; the shown ones above stay shown, and the page's scroll is put back after each read",
              all(seen[k][:2] == (True, False) for k in ("#r39p3d", "#r39covered", "#r39nopointer"))
              and before_scroll == after_scroll, ({k: v for k, v in seen.items() if k.startswith("#r39")}, before_scroll, after_scroll))
        check("chrome (R41, R42): the hit test looks only at digits that render (a hidden or invisible span's aren't in the text "
              "read, so a total with one is shown), and the point must land on the digits' own element or an ancestor in the "
              "total: covered by a descendant (an opaque box or iframe inside the total), it's hidden; digits split across "
              "inline elements are still shown", seen["#r41hiddenspan"][:2] == (True, True) and seen["#r41hiddenspan"][2] == "Total $2.00"
              and seen["#r42coverdiv"][:2] == (True, False) and seen["#r42coverframe"][:2] == (True, False)
              and seen["#r42inline"][:2] == (True, True), {k: v for k, v in seen.items() if k.startswith(("#r41", "#r42"))})
        h.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        sa.shutdown()
        sb.shutdown()


def main():
    unit()
    td = tempfile.mkdtemp(prefix="mcomp-sf-")
    try:
        fake_suite(td)
        chrome = find_chrome()
        if chrome:
            print(f"(real Chromium: {chrome})")
            chrome_suite(td, chrome)
        else:
            print("SKIP  real Chromium checks (set MCOMP_TEST_CHROME=/path/to/chrome to run them)")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
