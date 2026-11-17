"""metacomputer secrets: the guest half of a fill. Runs inside the VM as the desktop user.

The unlock process on the Mac (mcomp-vault) runs this over SSH as
`python3 -I -c <this file>` and writes one JSON request to its stdin:

  {"mode": "fill" | "check", "field": "password" | "username" | "otp" | "text",
   "origins": ["https://example.com"], "profile": NAME or null, "selector": CSS or null,
   "value": "..."}                                  # value only when mode is "fill"

It finds the driver's Chrome through its DevTools port, picks the tab on an allowed
site, checks the focused field (the tab's site, the field's frame, the element's
type), types the value with Input.insertText, checks the field's length, and prints
one JSON line:

  {"ok": true, "mode": "fill", "field": "password", "origin": "https://example.com", "chars": 16}
  {"ok": false, "denied": true, "reason": "..."}     # denied: a check refused it
  {"ok": false, "denied": false, "reason": "..."}    # it couldn't be done

The value is never printed, logged or written to disk. Python 3 standard library
only. The copy that runs is the one built into mcomp-vault (scripts/build-vault.sh),
not this file: editing it changes nothing until the vault is rebuilt.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import sys
import urllib.parse

PROFILE_ROOT_REL = ".local/state/cua-driver/browser-profiles"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TIMEOUT = 10

# Which elements a field may go into.
FIELD_INPUT_TYPES = {
    "password": {"password"},
    "username": {"text", "email", "tel"},
    "otp": {"text", "tel", "number"},
}
TEXT_INPUT_TYPES = {"text", "email", "tel", "search", "url", "password", "number"}
FIELDS = ("password", "username", "otp", "text")


class Denied(Exception):
    """A check refused the fill."""


class Failed(Exception):
    """The fill couldn't be done."""


# --- pure helpers (unit-tested on the Mac: tests/test_guest_fill.py) -----------------
def parse_origin(url):
    """'https://Example.com:443/a?b' -> 'https://example.com'. None when it has no http(s) origin."""
    if not isinstance(url, str):
        return None
    try:
        u = urllib.parse.urlsplit(url.strip())
        port = u.port
    except ValueError:
        return None
    scheme = u.scheme.lower()
    host = (u.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    if ":" in host:
        host = "[" + host + "]"
    default = 443 if scheme == "https" else 80
    return scheme + "://" + host + ("" if port in (None, default) else ":" + str(port))


def origin_allowed(origin, allowed):
    o = parse_origin(origin) if origin else None
    return bool(o) and o in {parse_origin(a) for a in allowed}


def field_fits(field, info):
    """(True, "") when the focused element suits the field, else (False, why)."""
    tag = str(info.get("tag") or "").lower()
    typ = str(info.get("type") or "").lower()
    what = "<" + tag + (" type=" + typ if typ and tag == "input" else "") + ">" if tag else "nothing"
    if tag in ("", "body", "html"):
        return False, "no field is focused in the page; click into the field first"
    if info.get("disabled") or info.get("readonly"):
        return False, "the focused field is read-only or disabled"
    if field == "text":
        if tag == "textarea" or (tag == "input" and typ in TEXT_INPUT_TYPES):
            return True, ""
        return False, "the focused element is " + what + ", not a text field"
    want = FIELD_INPUT_TYPES.get(field)
    if want is None:
        return False, "unknown field " + str(field)
    if tag == "input" and typ in want:
        return True, ""
    return False, ("the focused element is " + what + ", not a " + field + " field (needs <input type="
                   + "|".join(sorted(want)) + ">)")


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_mask(payload, mask):
    if not payload:
        return b""
    n = len(payload)
    m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


def ws_encode_frame(opcode, payload, mask=None, fin=True):
    """A client frame (RFC 6455 5.2): masked with a random key unless one is given."""
    mask = os.urandom(4) if mask is None else mask
    b0 = (0x80 if fin else 0) | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b0, 0x80 | n)
    elif n < 65536:
        head = struct.pack("!BBH", b0, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", b0, 0x80 | 127, n)
    return head + mask + ws_mask(payload, mask)


def ws_decode_frame(buf):
    """(fin, opcode, payload, bytes used), or None when buf doesn't hold a whole frame yet."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    n, i = b1 & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, i = struct.unpack("!H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, i = struct.unpack("!Q", buf[2:10])[0], 10
    mask = None
    if b1 & 0x80:
        if len(buf) < i + 4:
            return None
        mask, i = buf[i:i + 4], i + 4
    if len(buf) < i + n:
        return None
    payload = buf[i:i + n]
    if mask:
        payload = ws_mask(payload, mask)
    return bool(b0 & 0x80), b0 & 0x0F, payload, i + n


# --- a minimal WebSocket client and DevTools connection --------------------------------
class WebSocket:
    def __init__(self, url, timeout=TIMEOUT):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws" or u.hostname not in ("127.0.0.1", "localhost"):
            raise Failed("unexpected DevTools address")
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        # No Origin header: Chrome accepts DevTools clients that don't send one.
        req = ("GET " + path + " HTTP/1.1\r\nHost: " + u.hostname + ":" + str(u.port) + "\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key + "\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Failed("DevTools closed the connection during the handshake")
            head += chunk
            if len(head) > 65536:
                raise Failed("DevTools handshake too long")
        head, self.buf = head.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
            raise Failed("DevTools refused the WebSocket: " + lines[0][:80])
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        if headers.get("sec-websocket-accept") != ws_accept(key):
            raise Failed("bad WebSocket handshake from DevTools")

    def send_text(self, text):
        self.sock.sendall(ws_encode_frame(0x1, text.encode("utf-8")))

    def _frame(self):
        while True:
            f = ws_decode_frame(self.buf)
            if f:
                self.buf = self.buf[f[3]:]
                return f
            chunk = self.sock.recv(65536)
            if not chunk:
                raise Failed("DevTools closed the connection")
            self.buf += chunk

    def recv_text(self):
        parts, started = [], False
        while True:
            fin, op, payload, _ = self._frame()
            if op == 0x9:                       # ping -> pong
                self.sock.sendall(ws_encode_frame(0xA, payload))
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise Failed("DevTools closed the connection")
            if op in (0x1, 0x2):
                parts, started = [payload], True
            elif op == 0x0 and started:
                parts.append(payload)
            else:
                continue
            if fin:
                return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(ws_encode_frame(0x8, b""))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class DevTools:
    def __init__(self, ws_url):
        self.ws = WebSocket(ws_url)
        self.next_id = 0

    def call(self, method, params=None, quiet=False):
        """Send a command and wait for its answer (events in between are skipped).
        quiet: never put the protocol's error text in a message (for calls that carry the value)."""
        self.next_id += 1
        mid = self.next_id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                if quiet:
                    raise Failed(method + " failed")
                raise Failed(method + " failed: " + str((msg["error"] or {}).get("message", ""))[:120])
            return msg.get("result") or {}

    def value(self, expression):
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if r.get("exceptionDetails"):
            raise Failed("the page refused a check (script error)")
        return (r.get("result") or {}).get("value")

    def object(self, expression):
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": False})
        if r.get("exceptionDetails"):
            raise Failed("the page refused a check (script error)")
        res = r.get("result") or {}
        return res.get("objectId") if res.get("subtype") != "null" else None

    def on(self, object_id, function, by_value=True):
        r = self.call("Runtime.callFunctionOn", {"objectId": object_id, "functionDeclaration": function,
                                                 "returnByValue": by_value})
        if r.get("exceptionDetails"):
            raise Failed("the page refused a check (script error)")
        return (r.get("result") or {}).get("value")

    def close(self):
        self.ws.close()


# The focused element, through shadow roots and same-origin frames. A cross-origin
# frame can't be looked into, so the walk stops at the frame element itself.
DEEP_ACTIVE = r"""/*mcomp:deep-active*/(function () {
  let doc = document, el = doc.activeElement;
  for (let i = 0; el && i < 32; i++) {
    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let inner = null;
      try { inner = el.contentDocument; } catch (e) { inner = null; }
      if (!inner) break;
      doc = inner; el = inner.activeElement; continue;
    }
    break;
  }
  return el || null;
})()"""

DESCRIBE = r"""/*mcomp:describe*/function () {
  const el = this, tag = (el.tagName || '').toLowerCase();
  let frame = null, src = null;
  try { frame = el.ownerDocument.defaultView.origin; } catch (e) { frame = null; }
  if (tag === 'iframe' || tag === 'frame') { try { src = el.src || null; } catch (e) { src = null; } }
  return { tag: tag, type: (tag === 'input' && el.type) ? String(el.type).toLowerCase() : '',
           frame_origin: frame, frame_src: src, readonly: !!el.readOnly, disabled: !!el.disabled,
           autocomplete: String(el.autocomplete || '') };
}"""

SELECT = r"""/*mcomp:select*/function () {
  try {
    if (this.ownerDocument.activeElement !== this && typeof this.focus === 'function') this.focus();
    if (typeof this.select === 'function') this.select();
  } catch (e) {}
  return true;
}"""

VERIFY = r"""/*mcomp:verify*/function () {
  let doc; try { doc = window.top.document; } catch (e) { doc = document; }
  let el = doc.activeElement;
  for (let i = 0; el && i < 32; i++) {
    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let inner = null;
      try { inner = el.contentDocument; } catch (e) { inner = null; }
      if (!inner) break;
      el = inner.activeElement; continue;
    }
    break;
  }
  return { same: el === this, len: (typeof this.value === 'string') ? this.value.length : -1 };
}"""


def focus_selector_js(selector):
    return ("/*mcomp:focus-selector*/(function (sel) { const l = document.querySelectorAll(sel);"
            " if (l.length !== 1) return l.length; l[0].focus(); return 1; })(" + json.dumps(selector) + ")")


# --- finding the driver's Chrome ------------------------------------------------------
def _home():
    return os.path.expanduser("~")


def _read_port_file(udd):
    try:
        with open(os.path.join(udd, "DevToolsActivePort")) as f:
            port = int(f.readline().strip())
        return port if 0 < port < 65536 else None
    except (OSError, ValueError):
        return None


def _chrome_browsers():
    """[(user-data-dir or None, args)] for this user's Chrome browser processes (not helpers)."""
    out = []
    uid = os.getuid()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return out
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            if os.stat("/proc/" + pid).st_uid != uid:
                continue
            exe = os.path.basename(os.readlink("/proc/" + pid + "/exe"))
            with open("/proc/" + pid + "/cmdline", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "chrome" not in exe and "chromium" not in exe:
            continue
        # Chrome rewrites its helpers' titles, so arguments may be space- rather than NUL-separated.
        args = raw.replace("\0", " ").split()
        if any(a.startswith("--type=") for a in args):
            continue
        udd = next((a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")), None)
        out.append((udd, args))
    return out


def _port_from_args(args):
    for a in args:
        if a.startswith("--remote-debugging-port="):
            try:
                p = int(a.split("=", 1)[1])
            except ValueError:
                continue
            if 0 < p < 65536:
                return p
    return None


def devtools_ports(profile):
    if profile:
        if not NAME_RE.match(profile) or profile in (".", ".."):
            raise Denied("bad profile name")
        udd = os.path.join(_home(), PROFILE_ROOT_REL, profile)
        port = _read_port_file(udd)
        if port:
            return [port]
        if not os.path.isdir(udd):
            raise Failed("no Chrome profile named " + profile + " in the VM (browser_prepare with "
                         "profile {mode: isolated_named, name: " + profile + "} makes it)")
        procs = [args for d, args in _chrome_browsers() if d and os.path.normpath(d) == os.path.normpath(udd)]
        if not procs:
            raise Failed("no Chrome is running on profile " + profile + " (start it with browser_prepare)")
        port = _port_from_args(procs[0])
        if port:
            return [port]
        if any(a == "--remote-debugging-pipe" for a in procs[0]):
            raise Failed("Chrome on profile " + profile + " talks DevTools over a pipe, not a port; fills need a port")
        raise Failed("Chrome on profile " + profile + " has no DevTools port")
    ports = []
    for udd, args in _chrome_browsers():
        p = (_read_port_file(udd) if udd else None) or _port_from_args(args)
        if p and p not in ports:
            ports.append(p)
    if not ports:
        raise Failed("no Chrome with DevTools is running; fills need page-mode Chrome (browser_prepare)")
    return ports


def page_targets(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=TIMEOUT)
    try:
        conn.request("GET", "/json/list", headers={"Host": "127.0.0.1:" + str(port)})
        r = conn.getresponse()
        if r.status != 200:
            raise Failed("DevTools on port " + str(port) + " answered " + str(r.status))
        data = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as e:
        raise Failed("could not list Chrome's tabs: " + type(e).__name__)
    finally:
        conn.close()
    out = []
    for t in data if isinstance(data, list) else []:
        if isinstance(t, dict) and t.get("type") == "page":
            url = t.get("webSocketDebuggerUrl") or ("ws://127.0.0.1:" + str(port) + "/devtools/page/" + str(t.get("id")))
            out.append({"url": str(t.get("url") or ""), "ws": url})
    return out


def choose_target(candidates):
    """One tab among several on allowed sites: the focused one, else the visible one."""
    if len(candidates) == 1:
        return candidates[0]
    states = []
    for c in candidates:
        try:
            dt = DevTools(c["ws"])
            try:
                st = dt.value("({focus: document.hasFocus(), visible: document.visibilityState === 'visible'})") or {}
            finally:
                dt.close()
        except Failed:
            st = {}
        states.append((c, st))
    for key in ("focus", "visible"):
        hits = [c for c, st in states if st.get(key)]
        if len(hits) == 1:
            return hits[0]
    raise Denied(str(len(candidates)) + " tabs are open on the allowed sites; close the others or bring the right one to the front")


# --- the fill ----------------------------------------------------------------------
def run(req):
    mode = req.get("mode")
    field = req.get("field")
    origins = [o for o in (parse_origin(x) for x in req.get("origins") or []) if o]
    if mode not in ("fill", "check") or field not in FIELDS or not origins:
        raise Failed("bad request")
    value = req.get("value") if mode == "fill" else None
    if mode == "fill" and (not isinstance(value, str) or not value):
        raise Failed("bad request")
    profile = req.get("profile") or None
    selector = req.get("selector") or None

    candidates = []
    for port in devtools_ports(profile):
        candidates += [t for t in page_targets(port) if origin_allowed(t["url"], origins)]
    if not candidates:
        raise Denied("no open tab on " + ", ".join(origins) + " in the driver's Chrome; open the site first")
    target = choose_target(candidates)

    dt = DevTools(target["ws"])
    try:
        top = parse_origin(dt.value("self.origin"))
        if top not in origins:
            raise Denied("the tab is on " + str(top) + ", which isn't allowed for this secret")
        if selector:
            n = dt.value(focus_selector_js(str(selector)[:300]))
            if n != 1:
                raise Denied("the selector matches " + str(n) + " elements, not exactly one")
        try:
            dt.call("Emulation.setFocusEmulationEnabled", {"enabled": True})   # background windows
        except Failed:
            pass
        el = dt.object(DEEP_ACTIVE)
        if not el:
            raise Denied("no field is focused in the page; click into the field first")
        info = dt.on(el, DESCRIBE) or {}
        if info.get("tag") in ("iframe", "frame"):
            src = parse_origin(info.get("frame_src") or "") or "another site"
            raise Denied("the focused field is inside a frame from another site (" + src + "); "
                         "fills only go into frames whose site is allowed")
        frame = parse_origin(info.get("frame_origin") or "")
        if frame not in origins:
            raise Denied("the focused field is in a frame from " + str(frame) + ", which isn't allowed for this secret")
        ok, why = field_fits(field, info)
        if not ok:
            raise Denied(why)
        if mode == "check":
            return {"ok": True, "mode": "check", "field": field, "origin": top, "frame_origin": frame,
                    "element": info.get("tag") + ("[type=" + info.get("type") + "]" if info.get("type") else "")}
        dt.on(el, SELECT)
        dt.call("Input.insertText", {"text": value}, quiet=True)
        after = dt.on(el, VERIFY) or {}
        want = utf16_len(value)
        if not after.get("same"):
            return {"ok": False, "denied": False, "origin": top,
                    "reason": "the focus moved while filling; the text may have gone to another field (check the page)"}
        if after.get("len") != want:
            return {"ok": False, "denied": False, "origin": top,
                    "reason": "the field holds " + str(after.get("len")) + " characters, expected " + str(want)
                              + " (a length limit, or the page changed it)"}
        return {"ok": True, "mode": "fill", "field": field, "origin": top, "frame_origin": frame, "chars": want}
    finally:
        dt.close()


def main():
    value = None
    try:
        req = json.loads(sys.stdin.buffer.read(1 << 20).decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError
        value = req.get("value")
        out = run(req)
    except Denied as e:
        out = {"ok": False, "denied": True, "reason": str(e)}
    except Failed as e:
        out = {"ok": False, "denied": False, "reason": str(e)}
    except (ValueError, UnicodeDecodeError):
        out = {"ok": False, "denied": False, "reason": "bad request"}
    except Exception as e:  # noqa: BLE001 - never a traceback (it could hold the value)
        out = {"ok": False, "denied": False, "reason": "guest error: " + type(e).__name__}
    line = json.dumps(out, ensure_ascii=True, separators=(",", ":"))
    if isinstance(value, str) and len(value) >= 4 and (value in line or json.dumps(value)[1:-1] in line):
        line = json.dumps({"ok": False, "denied": False, "reason": "result withheld (it contained the value)"})
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
