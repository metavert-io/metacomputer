// mcomp-vault: human-mode fills (SECURITY.md, "Human-mode fills: the address bar instead of DevTools").
//
// Page-mode fills go to the fill account's secret-fill, which asks Chrome's DevTools for each frame's site and types
// into the element it checked. A human-mode browser (the VM's regular Firefox, or a Chrome started normally) has no
// DevTools port, so a human-mode fill works from what the browser tells the accessibility tree instead:
//  - the site comes from the browser's own address bar (its toolbar entry, never the page's title or text), and must
//    agree with the address the browser reports for the page's document (AT-SPI's DocURL), so an address typed into the
//    bar but not opened doesn't count;
//  - the field is the focused element, which must sit in that top document (not in a frame, whose site the bar doesn't
//    show, and not in the browser's own controls) and be the expected kind: a password text for `password`, an entry
//    for `username` and `otp` (or a spin button for a numeric code);
//  - the value is typed as real foreground keystrokes through Cua Driver (`type_text`, delivery_mode foreground), then
//    the same field must still have the focus and hold that many characters.
// It runs in the VM as the desktop account (the key sealed sessions use), not the fill account, which can't reach the
// desktop's accessibility bus or keyboard: code running as the desktop user can see a human-mode fill on its way (see
// SECURITY.md). The script is part of this binary, sent on the ssh command line; the request, the value included, goes
// only to its stdin. Its answer is held to the same fixed form as a page-mode fill's, and nothing it says is passed on.
import Darwin
import Foundation

/// The VM side of a human-mode fill. Python 3 standard library plus python3-dbus (Ubuntu's, in the VM image), run with
/// `python3 -I` as the desktop user. Request on stdin (one JSON line): {"mode": "check" | "fill", "field": "password" |
/// "username" | "otp" | "card", "origins": [...], "window_id": N or null, "value": "..." (fill only)}. Answer on stdout (one line):
/// {"ok": true, "mode", "field", "origin", "chars", "route": "keyboard"} or {"ok": false, "denied": bool, "reason"}.
/// A purchase's total (PaymentServer.swift): {"mode": "total", "origins": [...], "label": "Total", "window_id": N or null} answers
/// {"ok": true, "mode": "total", "origin", "texts": [...], "befores": [...]}: for every place the label is (at most 10), at
/// most 120 characters of the page's showing text from there and 40 before it; the vault takes the amount right after the
/// label where the words before it don't make it a part of the price, and refuses unless exactly one place has one.
let humanFillScript = #"""
# mcomp-human-fill: a human-mode fill, the VM side (vault/Human.swift). Runs as the desktop user.
import json, os, re, select, signal, subprocess, sys, time, urllib.parse

FIELD_ROLES = {"password": {"password text"}, "username": {"entry"}, "otp": {"entry", "spin button"},
               "card": {"entry", "spin button", "password text"}}
BROWSERS = {"firefox", "google chrome", "google-chrome", "chrome", "chromium", "chromium-browser"}
BAR_NAMES = ("search with", "or enter address", "search or enter address", "address and search bar", "address bar")
DOC_ROLES = {"document web", "document frame"}
ACTIVE, EDITABLE, FOCUSED, SHOWING, READ_ONLY = 1, 7, 12, 25, 43     # AT-SPI state bits
MAX_NODES = 6000
DRIVER = "/usr/local/bin/cua-driver"
POLICY = "/etc/metacomputer/driver-policy"


class Refused(Exception):
    """A check refused it: nothing was typed."""


class Failed(Exception):
    """It couldn't be done."""


def parse_origin(url):
    """'https://Example.com:443/a?b' -> 'https://example.com'; None without an http(s) origin."""
    if not isinstance(url, str):
        return None
    try:
        u = urllib.parse.urlsplit(url.strip())
        port = u.port
    except ValueError:
        return None
    scheme, host = u.scheme.lower(), (u.hostname or "").lower()
    if scheme not in ("http", "https") or not host or u.username is not None or u.password is not None:
        return None
    default = 443 if scheme == "https" else 80
    return scheme + "://" + host + ("" if port in (None, default) else ":" + str(port))


def bar_origin(text, doc_url):
    """The site, from the address bar's text checked against the page's own address (DocURL). The bar is the browser's
    toolbar, not the page; DocURL is the browser's address for the document. A bar showing something else (an address
    typed but not opened, a search) refuses. Firefox and Chrome leave out https:// for a secure page."""
    t = (text or "").strip()
    doc = parse_origin(doc_url or "")
    if not doc:
        raise Refused("the address bar can't be checked: the page reports no http(s) address")
    if not t or any(c.isspace() for c in t) or len(t) > 4096:
        raise Refused("the address bar shows no address (is it being edited?)")
    if "://" in t:
        o = parse_origin(t)
        if not o:
            raise Refused("the address bar doesn't show an http(s) address")
    else:
        host = re.split(r"[/?#]", t, maxsplit=1)[0].lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?", host):
            raise Refused("the address bar doesn't show a site")
        if not doc.startswith("https://"):
            raise Refused("the address bar shows no scheme, and the page isn't https")
        o = parse_origin("https://" + host)
    if o != doc:
        raise Refused("the page's address and the address bar disagree")
    return o


def field_fits(field, role, states):
    if role not in FIELD_ROLES.get(field, ()):
        raise Refused("the focused element is a " + (role or "nothing") + ", not a " + field + " field")
    if not (states >> EDITABLE) & 1 or (states >> READ_ONLY) & 1:
        raise Refused("the focused field is read-only or disabled")


# --- the browser's accessibility tree (AT-SPI over D-Bus) ---------------------------------
class Tree:
    """AT-SPI through python3-dbus: the accessibility bus of the desktop session."""

    def __init__(self):
        import dbus
        self.dbus = dbus
        sb = dbus.SessionBus()
        addr = sb.get_object("org.a11y.Bus", "/org/a11y/bus").GetAddress(dbus_interface="org.a11y.Bus")
        self.bus = dbus.bus.BusConnection(addr)
        self.daemon = self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")

    def _o(self, ref):
        return self.bus.get_object(ref[0], ref[1], introspect=False)

    def apps(self):
        out = []
        for a, b in self._o(("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root")).GetChildren(dbus_interface="org.a11y.atspi.Accessible"):
            ref = (str(a), str(b))
            try:
                pid = int(self.daemon.GetConnectionUnixProcessID(ref[0], dbus_interface="org.freedesktop.DBus"))
            except Exception:
                pid = -1
            out.append((ref, pid))
        return out

    def role(self, ref):
        return str(self._o(ref).GetRoleName(dbus_interface="org.a11y.atspi.Accessible"))

    def name(self, ref):
        return str(self._o(ref).Get("org.a11y.atspi.Accessible", "Name", dbus_interface="org.freedesktop.DBus.Properties"))

    def states(self, ref):
        s = self._o(ref).GetState(dbus_interface="org.a11y.atspi.Accessible")
        return int(s[0]) | (int(s[1]) << 32)

    def children(self, ref):
        return [(str(a), str(b)) for a, b in self._o(ref).GetChildren(dbus_interface="org.a11y.atspi.Accessible")]

    def text(self, ref):
        # To the end (-1): an end past the text's length gives nothing back in Firefox. Capped here.
        return str(self._o(ref).GetText(0, -1, dbus_interface="org.a11y.atspi.Text"))[:4096]

    def char_count(self, ref):
        return int(self._o(ref).Get("org.a11y.atspi.Text", "CharacterCount", dbus_interface="org.freedesktop.DBus.Properties"))

    def doc_url(self, ref):
        return str(self._o(ref).GetAttributeValue("DocURL", dbus_interface="org.a11y.atspi.Document"))

    def extents(self, ref):
        """(x, y, width, height) on the screen, as the browser reports its window."""
        try:
            e = self._o(ref).GetExtents(self.dbus.UInt32(0), dbus_interface="org.a11y.atspi.Component")
            return tuple(int(v) for v in e)
        except Exception:
            return None


def pick_frame(tree, frames, title, geom):
    """The accessibility frame of the X window: the one at its place and size on the screen (a browser's windows can
    share a title), else the one with its title, else the one active window; anything unclear refuses."""
    if geom:
        at = [f for f in frames if (lambda e: e is not None and all(abs(a - b) <= 2 for a, b in zip(e, geom)))(tree.extents(f))]
        if len(at) == 1:
            return at[0]
        if at:
            frames = at
    named = [f for f in frames if title and tree.name(f) == title]
    if len(named) == 1:
        return named[0]
    active = [f for f in (named or frames) if (tree.states(f) >> ACTIVE) & 1]
    if len(active) == 1:
        return active[0]
    raise Refused("no browser window is active (click into the field in the browser first)")


def page_text(tree, pid, title, origins, label, geom=None):
    """(origin, texts, befores): the site from the address bar (as inspect checks it) and, for each place `label` is (at
    most 10), up to 120 characters of the page's text from there and 40 before it (a purchase's total). Only the top
    document's showing text, in order; nothing is focused."""
    apps = [ref for ref, p in tree.apps() if p == pid]
    if not apps:
        raise Refused("no accessibility tree for that browser (is it a Firefox or a Chrome started normally?)")
    app = apps[0]
    if tree.name(app).lower() not in BROWSERS:
        raise Refused("the window isn't a browser (Firefox, or a Chrome started normally)")
    frame = pick_frame(tree, [f for f in tree.children(app) if tree.role(f) == "frame"], title, geom)
    bars, docs, texts = [], [], []
    seen = [0]

    def walk(ref, depth, in_toolbar, in_doc):
        seen[0] += 1
        if seen[0] > MAX_NODES or depth > 60:
            return
        role, st = tree.role(ref), tree.states(ref)
        if depth > 1 and not (st >> SHOWING) & 1:
            return
        if role in DOC_ROLES:
            if in_doc:
                return                      # a frame's document: not the page whose site the bar shows
            docs.append(ref)
            in_doc = True
        if in_toolbar and not in_doc and role in ("combo box", "entry") and (st >> EDITABLE) & 1:
            bars.append(ref)
        kids = tree.children(ref)
        if in_doc and not kids and sum(len(t) for t in texts) < 65536:
            try:
                t = tree.text(ref)
            except Exception:
                t = ""
            t = (t or tree.name(ref) or "").replace("\ufffc", " ").strip()
            if t:
                texts.append(t)
        for c in kids:
            walk(c, depth + 1, in_toolbar or role == "tool bar", in_doc)

    walk(frame, 0, False, False)
    if len(bars) > 1:
        bars = [b for b in bars if any(n in tree.name(b).lower() for n in BAR_NAMES)] or bars
    if len(bars) != 1 or len(docs) != 1:
        raise Refused("the address bar can't be matched to one page")
    origin = bar_origin(tree.text(bars[0]), tree.doc_url(docs[0]))
    if origin not in origins:
        raise Refused("the human-mode site is " + origin + ", which isn't allowed here")
    whole = " \n ".join(texts)
    hits = [m.start() for m in re.finditer(r"(?<![A-Za-z])" + re.escape(label) + r"(?![A-Za-z])", whole, re.I)]
    if not hits:
        raise Refused("the page shows no \"" + label + "\"")
    if len(hits) > 10:
        raise Refused("the page shows \"" + label + "\" more than 10 times")
    return origin, [whole[h:h + 120] for h in hits], [whole[max(0, h - 40):h] for h in hits]


def inspect(tree, pid, title, field, origins, geom=None):
    """(origin, focused ref, its path, its character count) for the browser window of `pid`, or Refused."""
    apps = [ref for ref, p in tree.apps() if p == pid]
    if not apps:
        raise Refused("no accessibility tree for that browser (is it a Firefox or a Chrome started normally?)")
    app = apps[0]
    if tree.name(app).lower() not in BROWSERS:
        raise Refused("the window isn't a browser (Firefox, or a Chrome started normally)")
    frame = pick_frame(tree, [f for f in tree.children(app) if tree.role(f) == "frame"], title, geom)
    bars, docs, focused = [], [], []
    seen = [0]

    def walk(ref, depth, path, in_toolbar, doc_path):
        seen[0] += 1
        if seen[0] > MAX_NODES or depth > 60:
            return
        role, st = tree.role(ref), tree.states(ref)
        if depth > 1 and not (st >> SHOWING) & 1:
            return
        here_doc = doc_path
        if role in DOC_ROLES:
            if doc_path is None:
                docs.append((ref, path))
            here_doc = path if doc_path is None else doc_path
        if (st >> FOCUSED) & 1:
            focused.append((ref, path, role, st, doc_path, role in DOC_ROLES))
        if in_toolbar and doc_path is None and role in ("combo box", "entry") and (st >> EDITABLE) & 1:
            bars.append((ref, st))
        for i, c in enumerate(tree.children(ref)):
            walk(c, depth + 1, path + [i], in_toolbar or role == "tool bar", here_doc)

    walk(frame, 0, [], False, None)
    if len(bars) > 1:
        bars = [b for b in bars if any(n in tree.name(b[0]).lower() for n in BAR_NAMES)] or bars
    if len(bars) != 1:
        raise Refused("the address bar can't be found in the browser's toolbar")
    bar, bar_st = bars[0]
    if (bar_st >> FOCUSED) & 1:
        raise Refused("the focused element is in the browser's own controls (its address bar), not the page")
    if len(docs) != 1:
        raise Refused("the address bar can't be matched to one page (" + str(len(docs)) + " documents showing)")
    doc, doc_path = docs[0]
    origin = bar_origin(tree.text(bar), tree.doc_url(doc))
    if origin not in origins:
        raise Refused("the human-mode site is " + origin + ", which isn't allowed here")
    if not focused:
        raise Refused("no field is focused in the page; click into the field first")
    ref, path, role, st, in_doc, is_doc = focused[-1]
    if in_doc is None and not is_doc:
        raise Refused("the focused element is in the browser's own controls, not the page")
    if is_doc and path == doc_path:
        raise Refused("no field is focused in the page; click into the field first")
    # A document (an internal frame's) between the page and the field: a frame, whose site the bar doesn't show.
    node, inner = doc, path[len(doc_path):]
    for i in inner[:-1]:
        node = tree.children(node)[i]
        if tree.role(node) in DOC_ROLES or tree.role(node) == "internal frame":
            raise Refused("the field is inside a frame, whose site the address bar doesn't show")
    if is_doc:
        raise Refused("the field is inside a frame, whose site the address bar doesn't show")
    field_fits(field, role, st)
    try:
        count = tree.char_count(ref)
    except Exception:
        count = 0
    return origin, ref, path, count


# --- the window and the driver ---------------------------------------------------------------
def session_env():
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "LANG": "C.UTF-8"}
    try:
        with open("/run/user/%d/metacomputer/session.env" % os.getuid()) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
                    env[k] = v
    except OSError:
        raise Failed("the desktop session isn't up")
    env.update(CUA_DRIVER_RS_TELEMETRY_ENABLED="0", CUA_TELEMETRY_ENABLED="0", CUA_DRIVER_RS_UPDATE_CHECK="0")
    return env


def xprop(env, args):
    r = subprocess.run(["/usr/bin/xprop"] + args, env=env, capture_output=True, text=True, timeout=5)
    return r.stdout if r.returncode == 0 else ""


def window_info(env, window_id):
    """(window id, pid, title, (x, y, width, height)) of the given window, or of the active one."""
    if window_id is None:
        m = re.search(r"window id # (0x[0-9a-fA-F]+)", xprop(env, ["-root", "_NET_ACTIVE_WINDOW"]))
        if not m or int(m.group(1), 16) == 0:
            raise Refused("no browser window is active (click into the field in the browser first)")
        window_id = int(m.group(1), 16)
    out = xprop(env, ["-id", str(window_id), "_NET_WM_PID", "_NET_WM_NAME"])
    m = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", out)
    if not m:
        raise Refused("no browser window is active (click into the field in the browser first)")
    t = re.search(r'_NET_WM_NAME\(UTF8_STRING\) = "(.*)"\s*$', out, re.M)
    geo = None
    r = subprocess.run(["/usr/bin/xwininfo", "-id", str(window_id)], env=env, capture_output=True, text=True, timeout=5)
    g = [re.search(p + r":\s+(-?\d+)", r.stdout) for p in ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")]
    if r.returncode == 0 and all(g):
        geo = tuple(int(x.group(1)) for x in g)
    return (window_id, int(m.group(1)), (t.group(1).encode("latin-1", "backslashreplace").decode("unicode_escape", "replace") if t else None), geo)


class Driver:
    """Cua Driver's MCP server over stdio, as driver-mcp starts it (standard mode only: a VM whose driver policy is
    bounded refuses). Nothing it answers is printed."""

    def __init__(self, env):
        try:
            with open(POLICY) as f:
                if any(line.strip() == "bounded" for line in f):
                    raise Failed("human-mode fills need a standard driver session, and this VM takes only bounded ones")
        except OSError:
            pass
        self.p = subprocess.Popen([DRIVER, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.n = 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "mcomp-human-fill", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, o):
        self.p.stdin.write((json.dumps(o) + "\n").encode())
        self.p.stdin.flush()

    def call(self, method, params, timeout=20):
        self.n += 1
        mid = self.n
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            if not select.select([self.p.stdout], [], [], max(0.05, end - time.time()))[0]:
                break
            line = self.p.stdout.readline(4 << 20)
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("id") == mid:
                if "error" in m or (m.get("result") or {}).get("isError"):
                    raise Failed("human-mode typing failed")
                return m.get("result") or {}
        raise Failed("human-mode typing failed (no answer from the driver)")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except Exception:
            self.p.kill()


def run(req, tree=None, env=None, info=None, driver=None):
    mode, field = req.get("mode"), req.get("field")
    origins = [o for o in (parse_origin(x) for x in req.get("origins") or []) if o]
    if mode == "total":
        label = req.get("label")
        wid = req.get("window_id")
        if not isinstance(label, str) or not 0 < len(label) <= 40 or not origins or (wid is not None and (not isinstance(wid, int) or isinstance(wid, bool) or wid <= 0)):
            raise Failed("bad request")
        env = env if env is not None else session_env()
        if tree is None:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = env.get("DBUS_SESSION_BUS_ADDRESS", "")
            try:
                tree = Tree()
            except Exception:
                raise Failed("no accessibility bus in the desktop session")
        window_id, pid, title, geom = info if info is not None else window_info(env, wid)
        origin, texts, befores = page_text(tree, pid, title, origins, label, geom)
        return {"ok": True, "mode": "total", "origin": origin, "texts": texts, "befores": befores}
    if mode not in ("check", "fill") or field not in FIELD_ROLES or not origins:
        raise Failed("bad request")
    value = req.get("value") if mode == "fill" else None
    if mode == "fill" and (not isinstance(value, str) or not value or "\n" in value or "\r" in value):
        raise Failed("bad request")
    wid = req.get("window_id")
    if wid is not None and (not isinstance(wid, int) or isinstance(wid, bool) or wid <= 0):
        raise Failed("bad request")
    env = env if env is not None else session_env()
    if tree is None:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = env.get("DBUS_SESSION_BUS_ADDRESS", "")
        try:
            tree = Tree()
        except Exception:
            raise Failed("no accessibility bus in the desktop session")
    window_id, pid, title, geom = info if info is not None else window_info(env, wid)
    origin, ref, path, count = inspect(tree, pid, title, field, origins, geom)
    if mode == "check":
        return {"ok": True, "mode": "check", "field": field, "origin": origin}
    d = driver if driver is not None else Driver(env)
    try:
        target = {"pid": pid, "window_id": window_id, "delivery_mode": "foreground"}
        if count > 0:      # a field that already holds text: select it, so the value replaces it
            d.call("tools/call", {"name": "hotkey", "arguments": dict(target, keys=["ctrl", "a"])})
        d.call("tools/call", {"name": "type_text", "arguments": dict(target, text=value)}, timeout=10 + len(value) // 10)
    finally:
        d.close()
    time.sleep(0.3)
    try:
        origin2, ref2, path2, count2 = inspect(tree, pid, title, field, origins, geom)
    except Refused:
        return {"ok": False, "denied": False, "origin": origin,
                "reason": "the page changed during the fill; the value may have gone elsewhere (check the page)"}
    # The same field: the same accessible object, or the same place in the tree (a browser's own autocomplete list can
    # appear while typing and move the field's place without moving the focus).
    if origin2 != origin or (path2 != path and ref2 != ref):
        return {"ok": False, "denied": False, "origin": origin,
                "reason": "the focus moved while filling; the text may have gone to another field (check the page)"}
    if count2 != len(value):
        return {"ok": False, "denied": False, "origin": origin, "reason": "the field holds " + str(count2) + " characters"}
    return {"ok": True, "mode": "fill", "field": field, "origin": origin, "chars": len(value), "route": "keyboard"}


def out_of_time(signum, frame):
    raise Failed("ran out of time")


def main():
    value = None
    req = None
    try:
        signal.signal(signal.SIGALRM, out_of_time)
        signal.alarm(40)
        req = json.loads(sys.stdin.buffer.read(1 << 16).decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError
        value = req.get("value")
        out = run(req)
    except Refused as e:
        out = {"ok": False, "denied": True, "reason": str(e)}
    except Failed as e:
        out = {"ok": False, "denied": False, "reason": str(e)}
    except (ValueError, UnicodeDecodeError):
        out = {"ok": False, "denied": False, "reason": "bad request"}
    except Exception as e:  # noqa: BLE001 - never a traceback (it could hold the value)
        out = {"ok": False, "denied": False, "reason": "guest error: " + type(e).__name__}
    signal.alarm(0)
    # (A card's short part, like a month or a security code, can be in a site's address by chance: the vault cuts the answer
    # to fixed forms anyway, so those aren't withheld here.)
    short_card = isinstance(req, dict) and req.get("field") == "card" and isinstance(value, str) and len(value) < 6
    if isinstance(value, str) and value and not short_card and any(isinstance(v, str) and value in v for v in out.values()):
        out = {"ok": False, "denied": False, "reason": "result withheld (it contained the value)"}
    sys.stdout.write(json.dumps(out, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
"""#

/// What `human` runs in the VM, as the desktop account: the script above, by value (nothing in the VM decides what runs).
func humanFillRemote() -> String { "exec /usr/bin/python3 -I -c " + shellQuote(humanFillScript) + " mcomp-human-fill" }

extension GuestRunner {
    /// One human-mode request (check, or fill with the value on ssh's stdin), as the desktop account. The answer is
    /// held to the form a page-mode fill's is (GuestRunner.fill), with the same rules: nothing the VM chose goes on.
    func human(_ t: GuestTarget, request: [String: Any], value: [UInt8]?, timeout: TimeInterval, minWithhold: Int = 1,
               release: (() -> String?)? = nil) throws -> [String: Any] {
        var req = request
        var valueString: String?
        if let v = value { valueString = String(decoding: v, as: UTF8.self); req["value"] = valueString }
        guard var encoded = try? JSONSerialization.data(withJSONObject: req) else { throw VaultError("cannot encode the request", code: Exit.error) }
        req["value"] = nil
        var inputBytes = Array(encoded) + [0x0A]
        wipe(&encoded)
        defer { wipe(&inputBytes) }
        let r = try opsSSH(t, remote: humanFillRemote(), input: inputBytes, timeout: timeout, release: release)
        var stderr = String(decoding: r.stderr, as: UTF8.self)
        if let v = valueString, !v.isEmpty { stderr = stderr.replacingOccurrences(of: v, with: "[redacted]") }
        if r.cancelled { return ["ok": false, "status": "cancelled", "reason": requesterLeftReason] }
        if r.interrupted { return ["ok": false, "status": "error", "reason": "interrupted (the vault is locking)"] }
        if r.timedOut { return ["ok": false, "status": "error", "reason": "the VM didn't answer in time"] }
        if r.status == 255 && stderr.contains("Permission denied") {
            return ["ok": false, "status": "error",
                    "reason": "the VM refused the desktop account's key, which human-mode fills use: they need SSH_AUTH=key for now (as sealed sessions do), or bind again if its keys changed"]
        }
        let last = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
        guard let raw = parseJSONObject(Array(last.utf8)), raw["ok"] is Bool else {
            return ["ok": false, "status": "error", "reason": "the VM side failed (its own output isn't passed on)"]
        }
        if let v = valueString, !v.isEmpty, v.count >= minWithhold, containsString(raw, v) {
            return ["ok": false, "status": "error", "reason": "result withheld (it contained the value)"]
        }
        guard var out = fillResultSchema(raw) else {
            return ["ok": false, "status": "error", "reason": "the VM side answered in an unexpected form"]
        }
        let ok = out["ok"] as? Bool ?? false
        out["status"] = ok ? "ok" : ((out["denied"] as? Bool ?? false) ? "denied" : "error")
        out["denied"] = nil
        if let reason = out["reason"] as? String { out["reason"] = fillReason(reason) }
        let allowed = request["origins"] as? [String] ?? []
        out["origin"] = (out["origin"] as? String).flatMap { o in allowed.first { $0 == o } }
        out["chars"] = (ok && valueString != nil) ? valueString!.utf16.count : nil
        out["mode"] = request["mode"] as? String
        out["field"] = request["field"] as? String
        out["route"] = ok && valueString != nil ? "keyboard" : nil
        out = out.filter { ["ok", "status", "reason", "origin", "chars", "mode", "field", "route"].contains($0.key) }
        return out
    }
}

extension GuestRunner {
    /// A purchase's total in a human-mode browser: (the site from its address bar, the page's text from each place the label
    /// is, why not). The text is untrusted, kept in memory only for the amount it holds.
    func humanTotal(_ t: GuestTarget, origins: [String], label: String, windowID: Int?, timeout: TimeInterval) -> (String?, [String]?, [String]?, String?) {
        var req: [String: Any] = ["mode": "total", "origins": origins, "label": label]
        if let w = windowID { req["window_id"] = w }
        guard let encoded = try? JSONSerialization.data(withJSONObject: req),
              let r = try? opsSSH(t, remote: humanFillRemote(), input: Array(encoded) + [0x0A], timeout: timeout, maxStdout: 64 << 10) else {
            return (nil, nil, nil, "the VM couldn't be reached")
        }
        if r.cancelled { return (nil, nil, nil, requesterLeftReason) }
        if r.timedOut || r.interrupted { return (nil, nil, nil, "the VM didn't answer in time") }
        if r.status == 255 && String(decoding: r.stderr, as: UTF8.self).contains("Permission denied") {
            return (nil, nil, nil, "the VM refused the desktop account's key, which human-mode browsers need (SSH_AUTH=key)")
        }
        let last = String(decoding: r.stdout, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
        guard let raw = parseJSONObject(Array(last.utf8)), let okN = raw["ok"] as? NSNumber, CFGetTypeID(okN) == CFBooleanGetTypeID() else {
            return (nil, nil, nil, "the VM side failed (its own output isn't passed on)")
        }
        guard okN.boolValue else { return (nil, nil, nil, (raw["reason"] as? String).map(fillReason) ?? "the VM couldn't read the page") }
        let origin = (raw["origin"] as? String).flatMap { o in origins.first { $0 == o } }
        guard let arr = raw["texts"] as? [Any], !arr.isEmpty, arr.count <= 10, arr.allSatisfy({ $0 is String }) else {
            return (origin, nil, nil, "the VM side answered in an unexpected form")
        }
        guard let bs = raw["befores"] as? [Any], bs.count == arr.count, bs.allSatisfy({ $0 is String }) else {
            return (origin, nil, nil, "the VM side answered in an unexpected form")
        }
        return (origin, arr.compactMap { ($0 as? String).map { String($0.prefix(120)) } }, bs.compactMap { ($0 as? String).map { String($0.suffix(40)) } },
                origin == nil ? "the page isn't on the purchase's site" : nil)
    }
}
