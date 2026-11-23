#!/usr/bin/env python3
"""The VM's half of `metacomputer web gpu|debug|read|page` and `web responsive --device` (lib/browser_tools.py).

It runs inside the virtual computer as the desktop user, through the shell channel (`metacomputer sh`, so the hub's
claim, refusals and action log apply), with the standard library only. It starts a browser of its own, on the VM's
desktop (headed: what renders is what a person would see), in a new throwaway profile, with its remote-control port
on the VM's loopback only (Chrome: the DevTools protocol; Firefox: WebDriver BiDi), drives one page, and prints one
JSON object on stdout. The browser it starts is closed again, except for `page open`, which keeps it for later
`page` calls (its port and folder in ~/.cache/metacomputer/page/NAME.json, mode 0600).

Everything it reads from the page is untrusted: it's measured and cut short here (counts, lengths, at most so many
entries), and the Mac checks the shape again and escapes it before showing it.

  page_guest.py ACTION JSON-ARGS
  actions: gpu, debug, read, shots, page-open, page-read, page-fill, page-click, page-select, page-upload, page-close
"""
import base64
import hashlib
import json
import os
import random
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

MAX_ENTRIES = 200            # console messages, failed requests, elements: at most this many of each
MAX_TEXT = 500               # characters of one message, name or value
MAX_READ = 200_000           # characters of a readable extract (the Mac asks for less)
STATE_DIR = os.path.expanduser("~/.cache/metacomputer/page")
SHOTS_DIR = os.path.expanduser("~/metacomputer-runs/web-shots")   # PNGs the Mac copies with `metacomputer get`
CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or "/usr/bin/google-chrome-stable"
FIREFOX = shutil.which("firefox") or "/usr/bin/firefox"

DEVICES = {
    # name: (CSS width, height, device pixel ratio, mobile, user agent) -- common sizes, not exact devices
    "iphone-15": (393, 852, 3, True, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                                     "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"),
    "pixel-8": (412, 915, 2.625, True, "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/130.0.0.0 Mobile Safari/537.36"),
    "galaxy-s24": (360, 780, 3, True, "Mozilla/5.0 (Linux; Android 15; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/130.0.0.0 Mobile Safari/537.36"),
    "ipad-air": (820, 1180, 2, True, "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
                                     "Gecko) Version/18.0 Mobile/15E148 Safari/604.1"),
    "laptop": (1366, 768, 1, False, None),
    "desktop": (1920, 1080, 1, False, None),
}


class Fail(Exception):
    pass


def cut(s, n=MAX_TEXT):
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    s = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in s)
    return s if len(s) <= n else s[:n] + "…"


def strip_query(url, keep=False):
    if not isinstance(url, str):
        return ""
    if keep:
        return cut(url, 1000)
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]*[^?#]*)", url)
    base = m.group(1) if m else url.split("?")[0].split("#")[0]
    return cut(base + ("?…" if "?" in url else ""), 1000)


# --- a minimal WebSocket client (RFC 6455): text frames, client masking, ping/pong, close ------------------------
class WebSocket:
    def __init__(self, url, timeout=30):
        m = re.match(r"^ws://(127\.0\.0\.1|localhost):(\d+)(/.*)?$", url)
        if not m:
            raise Fail("only a ws:// address on the VM's loopback")
        self.sock = socket.create_connection(("127.0.0.1", int(m.group(2))), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = m.group(3) or "/"
        req = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{m.group(2)}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Fail("the browser closed its remote-control connection")
            head += chunk
            if len(head) > 65536:
                raise Fail("the browser's handshake was too long")
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise Fail("the browser refused the remote-control connection")
        want = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if want not in head:
            raise Fail("the browser's handshake didn't match")

    def send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x81])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise Fail("the browser closed its remote-control connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, limit=64 << 20):
        msg, controls = b"", 0
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > limit or len(msg) + n > limit or (op >= 0x8 and n > 125):
                raise Fail("a message from the browser was too large")
            if op >= 0x8:
                controls += 1
                if controls > 1000:
                    raise Fail("the browser sent only control frames")
            if b1 & 0x80:
                mk = self._read(4)
                payload = bytes(b ^ mk[i % 4] for i, b in enumerate(self._read(n)))
            else:
                payload = self._read(n)
            if op == 0x8:
                raise Fail("the browser closed its remote-control connection")
            if op == 0x9:                       # ping: pong back
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + b"\0\0\0\0" + payload)
                continue
            if op == 0xA:
                continue
            msg += payload
            if len(msg) > limit:
                raise Fail("a message from the browser was too large")
            if b0 & 0x80:
                return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --- the two protocols, behind one small interface -------------------------------------------------------------
class Session:
    """send(method, params) -> result; events collected in self.events (method, params), at most so many."""
    def __init__(self, ws, kind):
        self.ws, self.kind, self.next_id, self.events = ws, kind, 0, []

    def send(self, method, params=None, timeout=30, sid=None):
        self.next_id += 1
        mid = self.next_id
        msg = {"id": mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.sock.settimeout(timeout)
        self.ws.send(json.dumps(msg))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            raw = self.ws.recv()
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            if m.get("id") == mid:
                if m.get("error"):
                    err = m["error"]
                    raise Fail(f"{method}: " + cut(err.get("message") if isinstance(err, dict) else err, 300))
                return m.get("result") or {}
            if "method" in m and len(self.events) < 20000:
                self.events.append((m.get("method"), m.get("params") or {}))
        raise Fail(f"{method}: the browser didn't answer in {timeout} s")

    def pump(self, seconds):
        """Collect events for this long."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.ws.sock.settimeout(max(0.05, end - time.monotonic()))
            try:
                raw = self.ws.recv()
            except (socket.timeout, TimeoutError):
                return
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if isinstance(m, dict) and "method" in m and len(self.events) < 20000:
                self.events.append((m.get("method"), m.get("params") or {}))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Browser:
    """A browser this helper started: its process, profile folder and protocol session."""
    def __init__(self, engine, device=None, webgpu=False, keep=False, downloads=None):
        self.engine, self.device, self.keep = engine, device, keep
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix=f"mcomp-page-{engine}-")
        self.proc, self.sess, self.ctx, self.cdp_sid = None, None, None, None
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        dev = DEVICES.get(device) if device else None
        if engine == "chrome":
            args = [CHROME, f"--remote-debugging-port={self.port}", "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check",
                    "--disable-features=Translate", "--new-window", "about:blank"]
            if webgpu:
                # No GPU in the VM: WebGPU through Chrome's software adapter, and WebGL through SwiftShader, which
                # Chrome no longer falls back to by itself (without the second flag a page gets no WebGL at all).
                args[1:1] = ["--enable-unsafe-webgpu", "--enable-unsafe-swiftshader"]
            w, h = (dev[0], dev[1]) if dev else (1366, 900)
            args.insert(1, f"--window-size={max(400, w + 16)},{max(400, h + 140)}")
        else:
            prefs = ['user_pref("remote.active-protocols", 1);',         # BiDi only
                     'user_pref("browser.shell.checkDefaultBrowser", false);',
                     'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
                     'user_pref("browser.startup.homepage_override.mstone", "ignore");']
            if webgpu:
                prefs.append('user_pref("dom.webgpu.enabled", true);')
            if downloads:
                prefs += ['user_pref("browser.download.folderList", 2);',
                          f'user_pref("browser.download.dir", {json.dumps(downloads)});',
                          'user_pref("browser.download.useDownloadDir", true);',
                          'user_pref("browser.download.always_ask_before_handling_new_types", false);']
            if dev and dev[4]:
                prefs.append(f'user_pref("general.useragent.override", {json.dumps(dev[4])});')
            with open(os.path.join(self.profile, "user.js"), "w") as f:
                f.write("\n".join(prefs) + "\n")
            args = [FIREFOX, "--remote-debugging-port", str(self.port), "--profile", self.profile, "--no-remote",
                    "--new-instance", "about:blank"]
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env=env, start_new_session=True)
        try:
            self.connect()
        except BaseException:
            self.close()                     # never leave a browser behind that nothing can close
            raise

    def connect(self, attempts=60):
        last = None
        for _ in range(attempts):
            if self.proc and self.proc.poll() is not None:
                raise Fail(f"{self.engine} exited as it started (status {self.proc.returncode})")
            try:
                if self.engine == "chrome":
                    with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
                        ver = json.load(r)
                    ws = WebSocket(ver["webSocketDebuggerUrl"])
                    self.sess = Session(ws, "cdp")
                    targets = self.sess.send("Target.getTargets").get("targetInfos") or []
                    page = next((t for t in targets if t.get("type") == "page"), None)
                    tid = page["targetId"] if page else self.sess.send("Target.createTarget", {"url": "about:blank"})["targetId"]
                    self.cdp_sid = self.sess.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
                    for dom in ("Page", "Runtime", "Network", "Log"):
                        self.cmd(f"{dom}.enable")
                else:
                    ws = WebSocket(f"ws://127.0.0.1:{self.port}/session")
                    self.sess = Session(ws, "bidi")
                    self.sess.send("session.new", {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": False}}})
                    tree = self.sess.send("browsingContext.getTree", {"maxDepth": 0})
                    self.ctx = tree["contexts"][0]["context"]
                    self.sess.send("session.subscribe", {"events": ["log.entryAdded", "network.beforeRequestSent",
                                                                    "network.responseCompleted", "network.fetchError"]})
                return
            except (OSError, ValueError, KeyError, IndexError, Fail) as e:
                last = e
                time.sleep(0.5)
        raise Fail(f"couldn't reach {self.engine}'s remote-control port ({cut(str(last), 200)})")

    def cmd(self, method, params=None, timeout=30):
        return self.sess.send(method, params, timeout=timeout, sid=self.cdp_sid)

    # -- page operations, both engines --------------------------------------------------------------------------
    def emulate(self):
        dev = DEVICES.get(self.device) if self.device else None
        if not dev:
            return
        w, h, dpr, mobile, ua = dev
        if self.engine == "chrome":
            self.cmd("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": dpr,
                                                            "mobile": mobile})
            if mobile:
                self.cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
            if ua:
                self.cmd("Emulation.setUserAgentOverride", {"userAgent": ua})
        else:
            self.sess.send("browsingContext.setViewport", {"context": self.ctx, "viewport": {"width": w, "height": h},
                                                           "devicePixelRatio": dpr})

    def preload(self, source):
        """Script that runs in every document before the page's own."""
        if self.engine == "chrome":
            self.cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        else:
            self.sess.send("script.addPreloadScript", {"functionDeclaration": f"() => {{ {source} }}"})

    def navigate(self, url, wait_s):
        if self.engine == "chrome":
            r = self.cmd("Page.navigate", {"url": url}, timeout=60)
            if r.get("errorText"):
                raise Fail("the page didn't load: " + cut(r["errorText"], 200))
            end = time.monotonic() + 45
            while time.monotonic() < end:
                if any(m == "Page.loadEventFired" for m, _ in self.sess.events):
                    break
                self.sess.pump(0.25)
        else:
            self.sess.send("browsingContext.navigate", {"context": self.ctx, "url": url, "wait": "complete"}, timeout=60)
        self.sess.pump(wait_s)

    def evaluate(self, expr, timeout=30):
        """The value of a JavaScript expression (a promise is awaited), as JSON-able data."""
        if self.engine == "chrome":
            r = self.cmd("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True},
                         timeout=timeout)
            if r.get("exceptionDetails"):
                raise Fail("the page's script failed: " + cut(json.dumps(r["exceptionDetails"])[:300], 300))
            return (r.get("result") or {}).get("value")
        r = self.sess.send("script.evaluate", {"expression": f"(async () => JSON.stringify(await ({expr})))()",
                                               "target": {"context": self.ctx},
                                               "awaitPromise": True, "resultOwnership": "none"}, timeout=timeout)
        if r.get("type") == "exception":
            raise Fail("the page's script failed: " + cut(json.dumps(r.get("exceptionDetails"))[:300], 300))
        v = (r.get("result") or {}).get("value")
        return json.loads(v) if isinstance(v, str) else None

    def screenshot(self, label="page"):
        """A PNG of the page's viewport, saved in the VM: its path (the Mac copies it), or "" when none came back."""
        if self.engine == "chrome":
            data = self.cmd("Page.captureScreenshot", {"format": "png"}, timeout=60).get("data")
        else:
            data = self.sess.send("browsingContext.captureScreenshot", {"context": self.ctx}, timeout=60).get("data")
        if not isinstance(data, str):
            return ""
        os.makedirs(SHOTS_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(SHOTS_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{re.sub(r'[^a-z0-9-]', '', label)[:40]}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data, validate=False))
        return path

    def close(self):
        if self.sess:
            try:
                if self.engine == "chrome":
                    self.sess.send("Browser.close", timeout=5)
                else:
                    self.sess.send("browser.close", timeout=5)
            except (Fail, OSError):
                pass
            self.sess.ws.close()
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(self.profile, ignore_errors=True)


# --- what a run collects -----------------------------------------------------------------------------------------
def console_and_network(b, keep_query=False):
    """Console messages, uncaught errors, failed and slow requests, from the events the browser sent."""
    console, errors, failed, timing = [], [], [], {}
    for method, p in b.sess.events:
        if b.engine == "chrome":
            if method == "Runtime.consoleAPICalled":
                args = " ".join(cut(a.get("value") if "value" in a else a.get("description"), 200)
                                for a in (p.get("args") or [])[:8] if isinstance(a, dict))
                frame = ((p.get("stackTrace") or {}).get("callFrames") or [{}])[0]
                console.append({"level": cut(p.get("type"), 20), "text": cut(args),
                                "source": strip_query(frame.get("url"), keep_query), "line": frame.get("lineNumber")})
            elif method == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                errors.append({"text": cut((d.get("exception") or {}).get("description") or d.get("text")),
                               "source": strip_query(d.get("url"), keep_query), "line": d.get("lineNumber")})
            elif method == "Log.entryAdded":
                e = p.get("entry") or {}
                console.append({"level": cut(e.get("level"), 20), "text": cut(e.get("text")),
                                "source": strip_query(e.get("url"), keep_query), "line": e.get("lineNumber")})
            elif method == "Network.requestWillBeSent":
                timing[p.get("requestId")] = {"url": strip_query((p.get("request") or {}).get("url"), keep_query),
                                              "method": cut((p.get("request") or {}).get("method"), 10),
                                              "start": p.get("timestamp")}
            elif method == "Network.responseReceived":
                r = p.get("response") or {}
                t = timing.get(p.get("requestId"))
                if t is not None:
                    t["status"] = r.get("status")
                    t["type"] = cut(p.get("type"), 20)
            elif method == "Network.loadingFinished":
                t = timing.get(p.get("requestId"))
                if t is not None and isinstance(t.get("start"), (int, float)) and isinstance(p.get("timestamp"), (int, float)):
                    t["ms"] = round((p["timestamp"] - t["start"]) * 1000)
            elif method == "Network.loadingFailed":
                t = timing.get(p.get("requestId")) or {}
                failed.append({"url": t.get("url", ""), "method": t.get("method", ""),
                               "error": cut(p.get("errorText"), 120), "blocked": cut(p.get("blockedReason"), 60)})
        else:
            if method == "log.entryAdded":
                src = ((p.get("stackTrace") or {}).get("callFrames") or [{}])[0]
                item = {"level": cut(p.get("level"), 20), "text": cut(p.get("text")),
                        "source": strip_query(src.get("url"), keep_query), "line": src.get("lineNumber")}
                (errors if p.get("type") == "javascript" and p.get("level") == "error" else console).append(item)
            elif method == "network.beforeRequestSent":
                rq = p.get("request") or {}
                timing[rq.get("request")] = {"url": strip_query(rq.get("url"), keep_query), "method": cut(rq.get("method"), 10),
                                             "start": p.get("timestamp")}
            elif method == "network.responseCompleted":
                rq = p.get("request") or {}
                t = timing.get(rq.get("request"))
                if t is not None:
                    t["status"] = (p.get("response") or {}).get("status")
                    tm = rq.get("timings") or {}
                    if isinstance(tm.get("responseEnd"), (int, float)) and isinstance(tm.get("requestTime"), (int, float)):
                        t["ms"] = round(tm["responseEnd"] - tm["requestTime"])
            elif method == "network.fetchError":
                rq = p.get("request") or {}
                failed.append({"url": strip_query(rq.get("url"), keep_query), "method": cut(rq.get("method"), 10),
                               "error": cut(p.get("errorText"), 120), "blocked": ""})
    statuses = [t for t in timing.values() if isinstance(t.get("status"), int) and t["status"] >= 400]
    for t in statuses:
        failed.append({"url": t["url"], "method": t.get("method", ""), "error": f"HTTP {t['status']}", "blocked": ""})
    slow = sorted((t for t in timing.values() if isinstance(t.get("ms"), int)), key=lambda t: -t["ms"])[:10]
    return {"console": console[:MAX_ENTRIES], "console_total": len(console), "errors": errors[:MAX_ENTRIES],
            "errors_total": len(errors), "failed_requests": failed[:MAX_ENTRIES], "failed_total": len(failed),
            "requests_total": len(timing), "slowest": [{"url": t["url"], "ms": t["ms"]} for t in slow]}


PERF_JS = r"""(() => {
  const n = performance.getEntriesByType('navigation')[0] || {};
  const lcp = (window.__mcompLCP || null);
  const r = x => (typeof x === 'number' ? Math.round(x) : null);
  return {ttfb_ms: r(n.responseStart), dom_content_loaded_ms: r(n.domContentLoadedEventEnd), load_ms: r(n.loadEventEnd),
          largest_contentful_paint_ms: r(lcp), transfer_bytes: r(n.transferSize),
          resources: performance.getEntriesByType('resource').length,
          title: document.title, url: location.href};
})()"""

LCP_PRELOAD = ("try { new PerformanceObserver(l => { for (const e of l.getEntries()) window.__mcompLCP = e.startTime; })"
               ".observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}")

GPU_PRELOAD = r"""
window.__mcompGPU = {adapterRequests: 0, adapters: 0, devices: 0, contexts: {}};
try {
  if (navigator.gpu) {
    const ra = navigator.gpu.requestAdapter.bind(navigator.gpu);
    navigator.gpu.requestAdapter = async (...a) => { window.__mcompGPU.adapterRequests++; const ad = await ra(...a);
      if (ad) { window.__mcompGPU.adapters++; const rd = ad.requestDevice.bind(ad);
        ad.requestDevice = async (...b) => { const d = await rd(...b); window.__mcompGPU.devices++; return d; }; }
      return ad; };
  }
  const gc = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
    const k = String(kind); window.__mcompGPU.contexts[k] = (window.__mcompGPU.contexts[k] || 0) + 1;
    return gc.call(this, kind, ...rest); };
} catch (e) {}
"""

GPU_PROBE = r"""(async () => {
  const out = {webgl: null, webgl2: null, webgpu: null,
               page: window.__mcompGPU ? JSON.parse(JSON.stringify(window.__mcompGPU)) : null};   // before the probe's own calls
  for (const k of ['webgl', 'webgl2']) {
    try { const c = document.createElement('canvas'); const gl = c.getContext(k);
      if (gl) { const e = gl.getExtension('WEBGL_debug_renderer_info');
        out[k] = {renderer: e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
                  version: gl.getParameter(gl.VERSION)}; } else out[k] = {unavailable: true}; }
    catch (e) { out[k] = {error: String(e)}; }
  }
  if (!navigator.gpu) out.webgpu = {available: false, why: 'no navigator.gpu'};
  else { try { const a = await navigator.gpu.requestAdapter();
      if (!a) out.webgpu = {available: false, why: 'no adapter'};
      else { const i = a.info || {};
        out.webgpu = {available: true, vendor: i.vendor || '', architecture: i.architecture || '', device: i.device || '',
                      description: i.description || '', fallback: !!(i.isFallbackAdapter ?? a.isFallbackAdapter),
                      features: Array.from(a.features || []).length,
                      max_texture_2d: (a.limits || {}).maxTextureDimension2D || null}; } }
    catch (e) { out.webgpu = {available: false, why: String(e)}; } }
  return out;
})()"""

READ_JS = r"""((max) => {
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','NAV','FOOTER','HEADER','ASIDE','FORM','SVG','IFRAME','BUTTON']);
  const score = el => { let t = 0; for (const p of el.querySelectorAll('p')) t += (p.innerText || '').length; return t; };
  let root = document.querySelector('article') || document.querySelector('main') || document.querySelector('[role=main]');
  if (!root) { let best = document.body, bs = 0;
    for (const el of document.querySelectorAll('div,section')) { const s = score(el); if (s > bs) { bs = s; best = el; } }
    root = best; }
  const out = []; let n = 0;
  const walk = el => {
    if (n > max) return;
    for (const c of el.children) {
      if (skip.has(c.tagName) || c.hidden || c.getAttribute('aria-hidden') === 'true') continue;
      const st = getComputedStyle(c); if (st.display === 'none' || st.visibility === 'hidden') continue;
      const tag = c.tagName; const txt = (c.innerText || '').trim().replace(/\s+/g, ' ');
      if (/^H[1-6]$/.test(tag)) { if (txt) { out.push('#'.repeat(+tag[1]) + ' ' + txt); n += txt.length; } }
      else if (tag === 'P' || tag === 'BLOCKQUOTE' || tag === 'PRE' || tag === 'FIGCAPTION') { if (txt) { out.push((tag === 'BLOCKQUOTE' ? '> ' : '') + txt); n += txt.length; } }
      else if (tag === 'LI') { if (txt) { out.push('- ' + txt); n += txt.length; } }
      else if (tag === 'TABLE') { for (const r of c.querySelectorAll('tr')) { const cells = Array.from(r.children).map(x => (x.innerText || '').trim().replace(/\s+/g, ' ')); out.push('| ' + cells.join(' | ') + ' |'); n += cells.join('').length; } }
      else walk(c);
    }
  };
  walk(root);
  const text = out.join('\n\n');
  return {title: document.title, url: location.href, lang: document.documentElement.lang || '',
          text: text.slice(0, max), chars: text.length, links: document.links.length,
          headings: Array.from(document.querySelectorAll('h1,h2,h3')).slice(0, 60).map(h => (h.innerText || '').trim().slice(0, 200))};
})"""

ELEMENTS_JS = r"""(() => {
  const els = []; let i = 0;
  const field = el => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
  const label = el => {          // a field's name comes from its label and attributes, never from what's in it
    if (el.labels && el.labels[0]) return el.labels[0].innerText;
    const a = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') ||
              el.getAttribute('name') || '';
    if (a || field(el)) return a;
    return el.innerText || '';
  };
  const secretish = el => el.type === 'password' || /one-time-code/i.test(el.autocomplete || '');
  for (const el of document.querySelectorAll('a[href],button,input,select,textarea,[role=button],[role=link],[contenteditable=true]')) {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.type === 'hidden') continue;
    const ref = 'e' + (++i); el.setAttribute('data-mcomp-ref', ref);
    els.push({ref, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
              role: el.getAttribute('role') || '', name: String(label(el)).trim().replace(/\s+/g, ' ').slice(0, 200),
              value: secretish(el) ? '' : String(el.value === undefined ? '' : el.value).slice(0, 200),
              checked: !!el.checked, disabled: !!el.disabled,
              options: el.tagName === 'SELECT' ? Array.from(el.options).slice(0, 50).map(o => o.text.slice(0, 80)) : undefined});
    if (els.length >= 400) break;
  }
  return {title: document.title, url: location.href, elements: els};
})()"""


def gpu_summary(b, url, wait):
    b.preload(GPU_PRELOAD)
    b.emulate()
    target = url or "about:blank"
    b.navigate(target, wait)
    probe = b.evaluate(GPU_PROBE, timeout=40)
    cn = console_and_network(b)
    gpu_msgs = [c for c in cn["console"] + cn["errors"] if re.search(r"webgpu|wgsl|gpuadapter|gpudevice|webgl|shader",
                                                                      c.get("text", ""), re.I)]
    return {"probe": probe, "gpu_messages": gpu_msgs[:50], "errors_total": cn["errors_total"],
            "screenshot": b.screenshot(f"gpu-{b.engine}")}


def run(action, a):
    engine = a.get("browser") or "chrome"
    if engine not in ("chrome", "firefox"):
        raise Fail("browser: chrome or firefox")
    url = a.get("url") or ""
    if url and not re.fullmatch(r"https?://[^\s\"'`\\]+", url):
        raise Fail("give an http:// or https:// URL")
    wait = min(60.0, max(0.0, float(a.get("wait", 3))))
    device = a.get("device")
    if device and device not in DEVICES:
        raise Fail("device: one of " + ", ".join(DEVICES))
    if action.startswith("page-") and action != "page-open":
        return page_action(action, a)
    keep = False                          # page-open keeps its browser only once it's saved
    b = Browser(engine, device=device, webgpu=action == "gpu" or bool(a.get("webgpu")), keep=action == "page-open",
                downloads=a.get("downloads"))
    try:
        if action == "gpu":
            out = gpu_summary(b, url, wait)
        elif action == "debug":
            b.preload(LCP_PRELOAD)
            b.emulate()
            b.navigate(url, wait)
            out = console_and_network(b, keep_query=bool(a.get("keep_query")))
            out["timing"] = b.evaluate(PERF_JS)
            out["screenshot"] = b.screenshot(f"debug-{engine}")
        elif action == "read":
            b.emulate()
            b.navigate(url, wait)
            out = b.evaluate(f"({READ_JS})({min(MAX_READ, int(a.get('max_chars', 20000)))})")
        elif action == "shots":
            b.emulate()
            b.navigate(url, wait)
            out = {"screenshot": b.screenshot(f"{device or 'page'}-{engine}"), "timing": b.evaluate(PERF_JS),
                   "viewport": b.evaluate("({w: innerWidth, h: innerHeight, dpr: devicePixelRatio, "
                                          "touch: navigator.maxTouchPoints, ua: navigator.userAgent})"),
                   "overflow_x": b.evaluate("document.documentElement.scrollWidth > innerWidth + 1")}
        elif action == "page-open":
            b.emulate()
            b.navigate(url, wait)
            out = b.evaluate(ELEMENTS_JS)
            out["session"] = save_page(a.get("name") or "default", b)
            keep = True
        else:
            raise Fail(f"unknown action {action}")
        out["engine"] = engine
        out["device"] = device
        return out
    finally:
        if not keep:
            b.close()


# --- kept pages (page open ... page close): Firefox precise mode ---------------------------------------------------
def state_path(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        raise Fail("a page name is letters, digits, - and _ (at most 32)")
    return os.path.join(STATE_DIR, name + ".json")


def save_page(name, b):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    p = state_path(name)
    old = load_page(name, missing_ok=True)
    if old:
        close_page(old)
    rec = {"name": name, "engine": b.engine, "port": b.port, "pid": b.proc.pid, "profile": b.profile, "ctx": b.ctx,
           "started": int(time.time())}
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(p + ".tmp", p)
    end_session(b)                       # the browser stays; later calls connect again
    return name


def end_session(b):
    """Let go of the browser without closing it: Firefox keeps one BiDi session at a time, so each call ends its own."""
    if b.sess is None:
        return
    if b.engine == "firefox":
        try:
            b.sess.send("session.end", timeout=5)
        except (Fail, OSError):
            pass
    b.sess.ws.close()


def load_page(name, missing_ok=False):
    try:
        with open(state_path(name)) as f:
            rec = json.load(f)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise Fail(f"no page '{name}' is open (web page open URL --name {name})")
    if not isinstance(rec, dict) or not isinstance(rec.get("port"), int) or not isinstance(rec.get("pid"), int):
        raise Fail("the page's record is damaged; run web page close and open it again")
    return rec


def close_page(rec):
    try:
        os.killpg(rec["pid"], signal.SIGTERM)
    except OSError:
        pass
    prof = rec.get("profile") or ""
    if prof.startswith(tempfile.gettempdir() + "/mcomp-page-") and "/.." not in prof:
        shutil.rmtree(prof, ignore_errors=True)
    try:
        os.unlink(state_path(rec["name"]))
    except OSError:
        pass


class Kept(Browser):
    """A connection to a browser `page open` left running."""
    def __init__(self, rec):
        self.engine, self.device, self.keep = rec["engine"], None, True
        self.port, self.profile, self.proc, self.cdp_sid = rec["port"], rec["profile"], None, None
        if self.engine == "chrome":
            Browser.connect(self, attempts=4)
        else:
            ws = WebSocket(f"ws://127.0.0.1:{self.port}/session")
            self.sess = Session(ws, "bidi")
            self.sess.send("session.new", {"capabilities": {}})
            tree = self.sess.send("browsingContext.getTree", {"maxDepth": 0})
            self.ctx = tree["contexts"][0]["context"]


SET_JS = r"""((ref, kind, value) => {
  const el = document.querySelector('[data-mcomp-ref="' + ref + '"]');
  if (!el) return {ok: false, why: 'no element ' + ref + ' (read the page again: web page read)'};
  const secret = () => {
    const lbl = [el.autocomplete, el.name, el.id, el.getAttribute('aria-label'), el.placeholder,
                 el.labels && el.labels[0] ? el.labels[0].innerText : ''].join(' ');
    return el.type === 'password' || /one-time-code|one[- ]?time|\botp\b|2fa|verification code|security code|passcode/i.test(lbl);
  };
  const NO = {ok: false, why: 'a password or one-time-code field: fill it with metacomputer secrets fill --human (or secrets code), never web page fill'};
  if (kind !== 'click' && secret()) return NO;
  el.scrollIntoView({block: 'center'}); el.focus();
  if (kind !== 'click' && secret()) return NO;          // a page can change a field when it's focused
  if (kind === 'click') { el.click(); return {ok: true}; }
  if (kind === 'select') { const o = Array.from(el.options || []).find(o => o.text.trim() === value || o.value === value);
    if (!o) return {ok: false, why: 'no such option'}; el.value = o.value; }
  else if (el.isContentEditable) { el.textContent = value; }
  else { const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const set = Object.getOwnPropertyDescriptor(proto, 'value').set; set.call(el, value); }
  if (kind !== 'click' && secret()) {                  // ...or while it's filled: take the value back out
    try { if (el.isContentEditable) el.textContent = ''; else el.value = ''; } catch (e) {}
    return NO; }
  el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, value_now: el.type === 'password' ? '' : String(el.value === undefined ? el.textContent : el.value).slice(0, 200)};
})"""


def page_action(action, a):
    name = a.get("name") or "default"
    rec = load_page(name)
    if action == "page-close":
        close_page(rec)
        return {"closed": name}
    k = Kept(rec)
    try:
        ref = a.get("ref") or ""
        if action != "page-read" and not re.fullmatch(r"e\d{1,5}", ref):
            raise Fail("give an element's ref from web page read (like e12)")
        if action == "page-read":
            out = k.evaluate(ELEMENTS_JS)
            if a.get("text"):
                out["readable"] = k.evaluate(f"({READ_JS})({min(MAX_READ, int(a.get('max_chars', 20000)))})")
        elif action in ("page-fill", "page-select"):
            value = a.get("value")
            if not isinstance(value, str) or len(value) > 10000:
                raise Fail("give the value as text (at most 10000 characters)")
            out = k.evaluate(f"({SET_JS})({json.dumps(ref)}, {json.dumps('select' if action == 'page-select' else 'fill')}, "
                             f"{json.dumps(value)})")
        elif action == "page-click":
            out = k.evaluate(f"({SET_JS})({json.dumps(ref)}, 'click', '')")
            k.sess.pump(float(a.get("wait", 1.5)))
        elif action == "page-upload":
            path = os.path.expanduser(a.get("path") or "")
            if not os.path.isabs(path) or not os.path.isfile(path):
                raise Fail("give the full path of a file in the VM to upload")
            if k.engine != "firefox":
                raise Fail("uploads through web page are for Firefox; Chrome has browser_set_input_files")
            node = k.sess.send("browsingContext.locateNodes", {"context": k.ctx, "locator": {
                "type": "css", "value": f'[data-mcomp-ref="{ref}"]'}}).get("nodes") or []
            if not node:
                raise Fail(f"no element {ref} (read the page again)")
            k.sess.send("input.setFiles", {"context": k.ctx, "element": {"sharedId": node[0]["sharedId"]}, "files": [path]})
            out = {"ok": True, "uploaded": os.path.basename(path)}
        else:
            raise Fail(f"unknown action {action}")
        out["engine"] = k.engine
        return out
    finally:
        end_session(k)


def _help_shaped(s):
    """A request for usage, or a word shaped like one (lib/strictargs.py's rule; this runs in the VM, on its own)."""
    return s in ("-h", "--help", "--h", "--he", "--hel") or s.startswith("--help=") or (s.startswith("-h") and not s.startswith("--"))


ACTIONS = ("gpu", "debug", "read", "shots", "page-open", "page-read", "page-fill", "page-select", "page-click",
           "page-upload", "page-close")


def main(argv):
    if argv in (["-h"], ["--help"]):
        print(json.dumps({"ok": False, "error": "usage: page_guest.py ACTION JSON"}))
        return 0
    if len(argv) != 2 or argv[0] not in ACTIONS or any(_help_shaped(a) for a in argv) or argv[1].startswith("-"):
        print(json.dumps({"ok": False, "error": "usage: page_guest.py ACTION JSON (ACTION: " + ", ".join(ACTIONS) + ")"}))
        return 2
    try:
        a = json.loads(argv[1])
        if not isinstance(a, dict):
            raise Fail("arguments must be a JSON object")
        out = run(argv[0], a)
        print(json.dumps({"ok": True, "result": out}))
        return 0
    except Fail as e:
        print(json.dumps({"ok": False, "error": cut(str(e), 400)}))
        return 1
    except Exception as e:                         # never a traceback with page text in it
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {cut(str(e), 200)}"}))
        return 1


if __name__ == "__main__":
    random.seed()
    sys.exit(main(sys.argv[1:]))
