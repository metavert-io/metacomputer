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

`h.claim(what)` checks for other controllers and claims the computer before a
script starts acting (an acting call does the same on its own); it returns
{"claimed": bool, "others": [...]}.

`h.request_site_login(url, options)` signs in with a login saved in `metacomputer
secrets`, as Cua Spaces' `space.request_site_login(url, options)` does (the hub's
request_site_login tool; SiteLoginOptions, SiteLoginReport, SiteLoginError).

CLI (what `metacomputer call` runs, and the status probe):
  mcomp_sdk.py call [--session NAME] [--timeout S] [--json | --raw] TOOL ['JSON']
  mcomp_sdk.py probe | tools | describe TOOL
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
import wirelimits  # noqa: E402
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
        if len(line) > mcp_hub.MAX_UPSTREAM_MSG and not line.endswith(b"\n"):
            raise ValueError("a message over the limit")
        return wirelimits.loads(line) if line else None

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
                    msg = wirelimits.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and self.on_message:
                    try:
                        self.on_message(msg)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:                                 # whatever ended the reading, the waiting calls hear of it
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
                        "settings": mcp_hub.settings_of(env), "tools_hash": tools_hash, "pid": os.getpid(),
                        "lessons": mcp_hub.lessons_of(env)}}
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


class SiteLoginOptions:
    """Options for Hub.request_site_login, named as Cua Spaces' SiteLoginOptions record names them."""

    def __init__(self, username=None, agent=None, session=None, target_id=None, tab_id=None, request_id=None,
                 wait_secs=None, profile=None, submit=None, pid=None):
        self.username, self.agent, self.session = username, agent, session
        self.target_id, self.tab_id, self.request_id, self.wait_secs = target_id, tab_id, request_id, wait_secs
        self.profile, self.submit, self.pid = profile, submit, pid


class SiteLoginReport:
    """What request_site_login did. Never the password, and nothing the page chose: same_site says whether it's still
    on the login's origin (no page address comes back)."""
    FIELDS = ("status", "request_id", "site", "origin", "username_hint", "submitted", "same_site", "session",
              "target_id", "tab_id")

    def __init__(self, **kw):
        for k in self.FIELDS:
            setattr(self, k, kw.get(k))
        self.submitted = bool(self.submitted)

    def __repr__(self):
        return "SiteLoginReport(" + ", ".join(f"{k}={getattr(self, k)!r}" for k in self.FIELDS if getattr(self, k) is not None) + ")"


class SiteLoginError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Hub:
    """A connection to the virtual computer through the metacomputer hub. See the module's docstring."""

    def __init__(self, cfg=None, session=None, timeout=120, kind="sdk", env=None, log=None):
        if session is not None and not mcp_hub.SESSION_RE.match(str(session)):
            raise ValueError("session names: 1-64 letters, digits, '.', '_' or '-'")
        self.cfg, self.session, self.timeout, self.kind, self._env = cfg, session, timeout, kind, env
        self.log = log                           # callable(dict) for each call (ToolResult.log_entry), optional
        self.calls = 0
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
        err = None
        self.calls += 1
        try:
            res = self.request("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        except HubError as e:
            err = str(e)
            res = {"isError": True, "content": [{"type": "text", "text": err}]}
        r = ToolResult(tool, args or {}, res if isinstance(res, dict) else {}, time.time() - t0)
        if self.log:
            self.log(r.log_entry(err))
        return r

    def tools(self):
        return (self.request("tools/list") or {}).get("tools") or []

    def request_site_login(self, url, options=None, **kw):
        """Sign in to url in the VM's page-mode Chrome with a login saved in `metacomputer secrets`, the way Cua
        Spaces' `space.request_site_login(url, options)` is called (synchronous here). options: a dict or a
        SiteLoginOptions (username, agent, session, target_id, tab_id, request_id, wait_secs; profile, pid and
        submit are this project's own: a tab given needs its Chrome's pid or named profile), and keyword arguments
        override it. Returns a SiteLoginReport: status "pending"
        (with request_id: call again with it once the user approved) or "filled"; never a password. Raises
        SiteLoginError (its .code: not_found, invalid_argument, login_refused, locked, ...) otherwise."""
        args = dict(options.__dict__ if isinstance(options, SiteLoginOptions) else (options or {}))
        args.update(kw)
        args = {k: v for k, v in args.items() if v is not None}
        args["url"] = url
        wait = int(args.get("wait_secs") or 20)
        res = self.call("request_site_login", args, timeout=max(self.timeout, wait + 60))
        sc = res.structured or {}
        if res.is_error:
            raise SiteLoginError(sc.get("error") or "error", sc.get("message") or res.text)
        return SiteLoginReport(**{k: sc.get(k) for k in SiteLoginReport.FIELDS})

    def status(self):
        return self.request("metacomputer/status")

    def claim(self, what=None):
        """Check for other controllers and claim the computer now (one step under the owners lock, as an acting
        call does). {"claimed": True} or {"claimed": False, "others": [descriptions]}."""
        return self.request("metacomputer/claim", {"tool": what or self.kind}) or {}

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


def probe_main():
    """`metacomputer status` / `start`: does the driver answer? Through the hub (a status client: it changes
    nothing and doesn't keep the hub running), one JSON line."""
    t0 = time.time()
    try:
        with Hub(kind="status", timeout=60, env=resolve_env()) as h:
            r = h.call("get_screen_size", {})            # a read the driver itself has to answer
            if r.is_error:
                raise HubError(r.text[:300])
            init = h.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "mcomp-probe", "version": "1"}}) or {}
            n = len(h.tools())
        server = init.get("serverInfo") or {}
        print(json.dumps({"ok": True, "server": f"{server.get('name', '')} {server.get('version', '')}".strip(),
                          "protocol": init.get("protocolVersion"), "tools": n, "ms": int((time.time() - t0) * 1000)}))
        return 0
    except (HubError, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


def tools_main(argv):
    """The driver's tools (names and first lines), or one tool's schema: `mcp_client.py --tools | --describe`."""
    try:
        with Hub(kind="status", timeout=60, env=resolve_env()) as h:
            tools = h.tools()
    except HubError as e:
        say_err(e)
        return 1
    if argv[:1] == ["describe"]:
        hit = [t for t in tools if t.get("name") == (argv[1] if len(argv) > 1 else None)]
        for t in hit:
            print(json.dumps(t, indent=2))
        return 0 if hit else 1
    for t in tools:
        line = f"{str(t.get('name')):28s} {((t.get('description') or '').splitlines() or [''])[0][:110]}"
        print(for_tty(line))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "call":
        sys.exit(call_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        sys.exit(probe_main())
    if len(sys.argv) > 1 and sys.argv[1] in ("tools", "describe"):
        sys.exit(tools_main(sys.argv[1:]))
    print(__doc__)
    sys.exit(2)
