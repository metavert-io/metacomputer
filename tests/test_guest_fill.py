#!/usr/bin/env python3
"""Offline checks for the guest side of `metacomputer secrets fill` (vault/guest_fill.py).

No VM needed. Three parts:
  1. its pure helpers: origins, field checks, WebSocket frames;
  2. the whole script against a fake Chrome DevTools server (HTTP + WebSocket),
     including fragmented replies, pings and events between answers;
  3. the whole script against a real headless Chromium, when one is found
     (MCOMP_TEST_CHROME=/path/to/chrome, Google Chrome, or Playwright's copy);
     skipped otherwise.

Run: python3 tests/test_guest_fill.py
Values here are dummies.
"""
import glob
import http.server
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
SCRIPT = os.path.join(LAB, "vault", "guest_fill.py")
sys.dont_write_bytecode = True      # no __pycache__ beside the vault's sources
spec = importlib.util.spec_from_file_location("guest_fill", SCRIPT)
gf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gf)

VALUE = "Dummy-Pass-4815"
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not cond else ""))


# --- 1. pure helpers ------------------------------------------------------------------
def unit():
    po = gf.parse_origin
    check("origin: path and query dropped", po("https://Example.com/login?x=1") == "https://example.com")
    check("origin: default port dropped", po("https://example.com:443/") == "https://example.com")
    check("origin: other port kept", po("https://example.com:8443/a") == "https://example.com:8443")
    check("origin: http with port", po("http://127.0.0.1:8000/x") == "http://127.0.0.1:8000")
    check("origin: userinfo ignored", po("https://u:p@example.com/") == "https://example.com")
    check("origin: not http(s)", po("about:blank") is None and po("data:text/html,x") is None and po("null") is None)
    check("origin: garbage", po("https://example.com:99999/") is None and po(None) is None)
    check("allowed: same site", gf.origin_allowed("https://example.com/a", ["https://example.com"]))
    check("allowed: subdomain is another site", not gf.origin_allowed("https://evil.example.com/", ["https://example.com"]))
    check("allowed: look-alike suffix refused", not gf.origin_allowed("https://example.com.evil.test/", ["https://example.com"]))
    check("allowed: http vs https differ", not gf.origin_allowed("http://example.com/", ["https://example.com"]))

    ff = gf.field_fits
    check("field: password into type=password", ff("password", {"tag": "input", "type": "password"})[0])
    check("field: password refused in type=text", not ff("password", {"tag": "input", "type": "text"})[0])
    check("field: username into email", ff("username", {"tag": "input", "type": "email"})[0])
    check("field: username refused in password", not ff("username", {"tag": "input", "type": "password"})[0])
    check("field: otp into tel/number/text", all(ff("otp", {"tag": "input", "type": t})[0] for t in ("tel", "number", "text")))
    check("field: otp refused in password", not ff("otp", {"tag": "input", "type": "password"})[0])
    check("field: text into textarea", ff("text", {"tag": "textarea", "type": ""})[0])
    check("field: text refused in checkbox", not ff("text", {"tag": "input", "type": "checkbox"})[0])
    check("field: nothing focused", not ff("password", {"tag": "body"})[0])
    check("field: read-only refused", not ff("password", {"tag": "input", "type": "password", "readonly": True})[0])
    check("field: disabled refused", not ff("text", {"tag": "textarea", "disabled": True})[0])
    check("field: contenteditable div refused", not ff("text", {"tag": "div"})[0])

    check("utf16 length", gf.utf16_len("aä\U0001F600") == 4)
    # RFC 6455 section 1.3's example.
    check("websocket accept key", gf.ws_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
    for n in (0, 5, 125, 126, 300, 65535, 65536, 70000):
        payload = bytes(i % 251 for i in range(n))
        f = gf.ws_encode_frame(0x1, payload, mask=b"\x01\x02\x03\x04")
        d = gf.ws_decode_frame(f)
        ok = d is not None and d[0] and d[1] == 0x1 and d[2] == payload and d[3] == len(f) and f[1] & 0x80
        check(f"websocket frame round trip, {n} bytes (masked)", ok)
    f = gf.ws_encode_frame(0x1, b"Hello", mask=b"\x37\xfa\x21\x3d")
    check("websocket masking (RFC 6455 5.7 example)", f == bytes.fromhex("818537fa213d7f9f4d5158"))
    check("websocket: partial frame waits", gf.ws_decode_frame(f[:4]) is None)
    check("websocket: random mask by default", gf.ws_encode_frame(0x1, b"x")[2:6] != gf.ws_encode_frame(0x1, b"x")[2:6])


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
    """/json/list and a WebSocket per page that answers the calls guest_fill.py makes."""

    def __init__(self, pages):
        self.pages = pages
        self.inserted = []
        self.unmasked = 0
        self.pongs = 0
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
            if path.startswith("/json"):
                body = json.dumps([{"id": p["id"], "type": p.get("type", "page"), "url": p["url"],
                                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/{p['id']}"}
                                   for p in self.pages]).encode()
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                          + str(len(body)).encode() + b"\r\n\r\n" + body)
                return
            page = next(p for p in self.pages if path.endswith("/" + p["id"]))
            accept = gf.ws_accept(hdrs["sec-websocket-key"])
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
            self.ws(c, page, rest)
        finally:
            c.close()

    def ws(self, c, page, buf):
        n = 0
        while True:
            f = gf.ws_decode_frame(buf)
            if not f:
                chunk = c.recv(65536)
                if not chunk:
                    return
                buf += chunk
                continue
            if not (buf[1] & 0x80):
                self.unmasked += 1
            buf = buf[f[3]:]
            fin, op, payload, _ = f
            if op == 0x8:
                return
            if op == 0xA:
                self.pongs += 1
                continue
            if op != 0x1:
                continue
            msg = json.loads(payload)
            result = self.answer(page, msg["method"], msg.get("params") or {})
            n += 1
            # An event and a ping before the answer; every other answer in two fragments.
            c.sendall(server_frame(0x1, json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}).encode()))
            c.sendall(server_frame(0x9, b"hi"))
            data = json.dumps({"id": msg["id"], "result": result}).encode()
            if n % 2:
                c.sendall(server_frame(0x1, data[:7], fin=False) + server_frame(0x0, data[7:]))
            else:
                c.sendall(server_frame(0x1, data))

    def answer(self, page, method, params):
        if method == "Runtime.evaluate":
            e = params["expression"]
            if "mcomp:deep-active" in e:
                if not page.get("active", True):
                    return {"result": {"type": "object", "subtype": "null", "value": None}}
                return {"result": {"type": "object", "subtype": "node", "objectId": "el-1"}}
            if "mcomp:focus-selector" in e:
                page["active"] = True
                return {"result": {"type": "number", "value": page.get("selector_matches", 1)}}
            if "document.hasFocus()" in e:
                return {"result": {"type": "object", "value": {"focus": page.get("focus", False), "visible": page.get("visible", True)}}}
            if e == "self.origin":
                return {"result": {"type": "string", "value": page.get("top", gf.parse_origin(page["url"]))}}
        if method == "Runtime.callFunctionOn":
            fn = params["functionDeclaration"]
            if "mcomp:describe" in fn:
                return {"result": {"type": "object", "value": page["info"]}}
            if "mcomp:select" in fn:
                page["value"] = ""
                return {"result": {"type": "boolean", "value": True}}
            if "mcomp:verify" in fn:
                return {"result": {"type": "object", "value": {"same": page.get("same", True), "len": gf.utf16_len(page.get("value", ""))}}}
        if method == "Input.insertText":
            self.inserted.append((page["id"], params["text"]))
            v = page.get("value", "") + params["text"]
            page["value"] = v[:page["maxlen"]] if page.get("maxlen") else v
            return {}
        if method == "Emulation.setFocusEmulationEnabled":
            return {}
        return {}

    def close(self):
        self.sock.close()


def run_script(home, req):
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    r = subprocess.run([sys.executable, "-I", "-c", open(SCRIPT).read()], input=json.dumps(req).encode(),
                       capture_output=True, env=env, timeout=60)
    out, err = r.stdout.decode(), r.stderr.decode()
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {"unparsed": out, "stderr": err}
    return res, out + err


def fake_suite(td):
    home = os.path.join(td, "home")
    prof = os.path.join(home, ".local/state/cua-driver/browser-profiles/p1")
    os.makedirs(prof)
    site = "https://login.example.com"
    pw = {"tag": "input", "type": "password", "frame_origin": site}

    def scenario(name, pages, req, expect):
        chrome = FakeChrome(pages)
        open(os.path.join(prof, "DevToolsActivePort"), "w").write(f"{chrome.port}\n/devtools/browser/x\n")
        base = {"mode": "fill", "field": "password", "origins": [site], "profile": "p1", "value": VALUE}
        base.update(req)
        res, raw = run_script(home, base)
        check(f"fake: {name}", expect(res, chrome), json.dumps(res)[:200])
        check(f"fake: {name}: value not in the script's output", VALUE not in raw)
        if chrome.unmasked:
            check(f"fake: {name}: client frames masked", False, f"{chrome.unmasked} unmasked")
        chrome.close()
        return res, chrome

    def page(**kw):
        p = {"id": "A", "url": site + "/login", "info": dict(pw)}
        p.update(kw)
        return p

    res, chrome = scenario("fills the focused password field", [page()], {},
                           lambda r, c: r.get("ok") and r.get("chars") == len(VALUE) and r.get("origin") == site
                           and c.inserted == [("A", VALUE)])
    check("fake: answered the server's pings", chrome.pongs >= 1)
    scenario("check mode types nothing", [page()], {"mode": "check", "value": None},
             lambda r, c: r.get("ok") and r.get("mode") == "check" and not c.inserted)
    scenario("no tab on an allowed site", [page(url="https://other.example/")], {},
             lambda r, c: r.get("ok") is False and r.get("denied") and "no open tab" in r["reason"] and not c.inserted)
    scenario("tab moved to another site since the listing", [page(top="https://evil.example")], {},
             lambda r, c: r.get("denied") and "isn't allowed" in r["reason"] and not c.inserted)
    scenario("wrong field type", [page(info={"tag": "input", "type": "text", "frame_origin": site})], {},
             lambda r, c: r.get("denied") and "not a password field" in r["reason"] and not c.inserted)
    scenario("username into a text field", [page(info={"tag": "input", "type": "email", "frame_origin": site})],
             {"field": "username", "value": "dummy-user-qx7"},
             lambda r, c: r.get("ok") and c.inserted == [("A", "dummy-user-qx7")])
    scenario("cross-origin frame refused", [page(info={"tag": "iframe", "type": "", "frame_origin": site,
                                                      "frame_src": "https://evil.example/f"})], {},
             lambda r, c: r.get("denied") and "frame from another site" in r["reason"] and "https://evil.example" in r["reason"]
             and not c.inserted)
    scenario("frame on another site refused", [page(info={"tag": "input", "type": "password", "frame_origin": "https://evil.example"})], {},
             lambda r, c: r.get("denied") and "frame from https://evil.example" in r["reason"] and not c.inserted)
    scenario("nothing focused", [page(active=False)], {},
             lambda r, c: r.get("denied") and "no field is focused" in r["reason"] and not c.inserted)
    scenario("read-only field refused", [page(info=dict(pw, readonly=True))], {},
             lambda r, c: r.get("denied") and "read-only" in r["reason"] and not c.inserted)
    scenario("two tabs: the focused one", [page(id="A", focus=False), page(id="B", focus=True)], {},
             lambda r, c: r.get("ok") and c.inserted == [("B", VALUE)])
    scenario("two tabs, neither focused nor alone visible: refused", [page(id="A"), page(id="B")], {},
             lambda r, c: r.get("denied") and "2 tabs" in r["reason"] and not c.inserted)
    scenario("focus moved during the fill", [page(same=False)], {},
             lambda r, c: r.get("ok") is False and not r.get("denied") and "focus moved" in r["reason"])
    scenario("length limit reported", [page(maxlen=4)], {},
             lambda r, c: r.get("ok") is False and "holds 4 characters" in r["reason"])
    scenario("non-ASCII value counted in UTF-16", [page()], {"value": "Dümmy-✓-\U0001F600-pass"},
             lambda r, c: r.get("ok") and r.get("chars") == gf.utf16_len("Dümmy-✓-\U0001F600-pass"))
    scenario("selector focuses exactly one element", [page(active=False)], {"selector": "#pw"},
             lambda r, c: r.get("ok") and c.inserted == [("A", VALUE)])
    scenario("selector matching two elements refused", [page(active=False, selector_matches=2)], {"selector": "input"},
             lambda r, c: r.get("denied") and "matches 2" in r["reason"] and not c.inserted)
    scenario("a page that isn't a tab is ignored", [page(type="service_worker")], {},
             lambda r, c: r.get("denied") and "no open tab" in r["reason"])

    res, raw = run_script(home, {"mode": "fill", "field": "password", "origins": [], "profile": "p1", "value": VALUE})
    check("fake: no origins is a bad request", res.get("reason") == "bad request" and VALUE not in raw)
    res, raw = run_script(home, {"mode": "fill", "field": "password", "origins": [site], "profile": "nope", "value": VALUE})
    check("fake: missing profile reported", "no Chrome profile named nope" in res.get("reason", "") and VALUE not in raw)
    os.remove(os.path.join(prof, "DevToolsActivePort"))
    res, raw = run_script(home, {"mode": "fill", "field": "password", "origins": [site], "profile": "p1", "value": VALUE})
    check("fake: profile without a running Chrome reported", "no Chrome is running on profile p1" in res.get("reason", ""))
    res, raw = run_script(home, {"mode": "fill", "field": "password", "origins": [site], "profile": "../x", "value": VALUE})
    check("fake: bad profile name refused", res.get("denied") and res.get("reason") == "bad profile name")
    r = subprocess.run([sys.executable, "-I", "-c", open(SCRIPT).read()], input=b"not json", capture_output=True, timeout=30)
    check("fake: unreadable request", json.loads(r.stdout).get("reason") == "bad request")


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
    for d in (a_dir, b_dir):
        open(os.path.join(d, "frame.html"), "w").write(FRAME_HTML)
    site, other = f"http://127.0.0.1:{pa}", f"http://127.0.0.1:{pb}"
    home = os.path.join(td, "chome")
    udd = os.path.join(home, ".local/state/cua-driver/browser-profiles/real")
    os.makedirs(udd)
    proc = subprocess.Popen([chrome, "--headless", "--remote-debugging-port=0", f"--user-data-dir={udd}",
                             "--no-first-run", "--no-default-browser-check", "--disable-gpu", f"{site}/login.html"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port = None
        for _ in range(100):
            port = gf._read_port_file(udd)
            if port:
                break
            time.sleep(0.2)
        check("chrome: DevToolsActivePort written", port is not None)
        if not port:
            return
        ws = None
        for _ in range(50):
            tabs = [t for t in gf.page_targets(port) if t["url"].startswith(site)]
            if tabs:
                ws = tabs[0]["ws"]
                break
            time.sleep(0.2)
        h = gf.DevTools(ws)
        for _ in range(50):   # wait for both frames to load
            if h.value("document.readyState === 'complete' && !!document.getElementById('same').contentDocument"
                       " && !!document.getElementById('same').contentDocument.getElementById('fpw')"
                       " && !!document.getElementById('srcdoc').contentDocument.getElementById('sdpw')"):
                break
            time.sleep(0.2)

        def fill(focus_js, field="password", value=VALUE, origins=(site,), mode="fill", selector=None):
            h.value("(function(){ if (document.activeElement) document.activeElement.blur(); " + focus_js + "; return 1; })()")
            req = {"mode": mode, "field": field, "origins": list(origins), "profile": "real", "value": value}
            if selector:
                req["selector"] = selector
            res, raw = run_script(home, req)
            check(f"chrome: {field} via {focus_js[:50] or selector}: value not in the output", value not in raw)
            return res

        r = fill("document.getElementById('pw').focus()")
        check("chrome: fills a password field", r.get("ok") and r.get("chars") == len(VALUE) and r.get("origin") == site, r)
        check("chrome: the page holds the value", h.value(f"document.getElementById('pw').value === {json.dumps(VALUE)}"))
        r = fill("document.getElementById('pw').focus()", value="Second-Dummy-77")
        check("chrome: a second fill replaces the first", r.get("ok")
              and h.value("document.getElementById('pw').value === 'Second-Dummy-77'"), r)
        r = fill("document.getElementById('user').focus()")
        check("chrome: password refused in an email field", r.get("denied") and "not a password field" in r["reason"], r)
        check("chrome: ...and nothing typed", h.value("document.getElementById('user').value === ''"))
        r = fill("document.getElementById('user').focus()", field="username", value="dummy-user-qx7")
        check("chrome: username into the email field", r.get("ok")
              and h.value("document.getElementById('user').value === 'dummy-user-qx7'"), r)
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
        check("chrome: cross-origin frame refused", r.get("denied") and "frame from another site" in r["reason"]
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
        check("chrome: --selector focuses the field", r.get("ok") and h.value(
            f"document.getElementById('pw').value === {json.dumps(VALUE)}"), r)
        r = fill("", selector="input[type=password]")
        check("chrome: --selector matching several refused", r.get("denied") and "matches" in r["reason"], r)
        h.value("document.getElementById('pw').value = ''")
        r = fill("document.getElementById('pw').focus()", mode="check")
        check("chrome: check mode types nothing", r.get("ok") and r.get("mode") == "check"
              and h.value("document.getElementById('pw').value === ''"), r)
        r = fill("")
        check("chrome: nothing focused refused", r.get("denied") and "no field is focused" in r["reason"], r)
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
    td = tempfile.mkdtemp(prefix="mcomp-gf-")
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
