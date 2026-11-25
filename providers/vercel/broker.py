#!/usr/bin/env python3
"""The transport broker of a computer on Vercel Sandbox: the one process besides lib/remote.py that holds the Vercel
token. ssh, the hub, the vault and every other program never get it: they reach the sandbox through this process's
socket, which carries SSH connections and answers whether the sandbox is still the computer's own, and nothing else.

    broker.py serve        (started by providers/vercel/remote.py, never by hand: its settings, the token among them,
                            come as one JSON object on its standard input, which is then closed)

What it does, for one computer:
- Listens on a Unix socket (0600, in a 0700 folder of this account's, /tmp/mcomp-UID-vercel/HASH.sock; a peer of
  another account is refused). A request is one JSON line: {"op": "ping" | "check" | "relay" | "quit", "sandbox":
  its sandbox's name}; the answer is one JSON line.
- `check`: the computer's identity as Vercel has it now ({"id": "<sandbox>-<createdAt>"} when the sandbox of that name
  carries the computer's tags; another id otherwise): lib/proxywrap.py asks it (through tunnel.py check) right before
  each SSH connection, and refuses unless it's the one recorded when the computer was made.
- `relay`: one SSH connection. In the sandbox's running session (never one it starts: a stopped computer stays
  stopped), it runs the guest's relay (/opt/metacomputer/vercel/relay, as Vercel's `ubuntu` account), which connects to
  sshd on the sandbox's 127.0.0.1:22. The socket's bytes go to the relay's standard input (base64 in the API's JSON,
  sent in order, each write with its offset so a retry can't repeat bytes); the relay's output comes back as base64
  lines (the API's output stream is text) and is written to the socket as bytes. Each relay is checked against the
  computer's identity first.
- The idle stop: while a connection is open (the hub's, a command's), the session's timeout is kept IDLE_STOP minutes
  ahead, up to the plan's cap; with none open it isn't, so the session ends at most IDLE_STOP minutes after its last
  use, whatever happens on this Mac (the broker gone included).
- It ends when the session has ended and no connection is open, on `quit` (vm delete, a new broker for the computer),
  and on SIGTERM. Its log (the computer's RUN_DIR/vercel-broker.log) never holds the token or the team's and project's
  IDs (the API's scrub).
Standard library only, besides lib/provider_http.py and providers/vercel/api.py."""
import base64
import json
import os
import signal
import socket
import stat
import struct
import sys
import threading
import time

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib")
RELAY = "/opt/metacomputer/vercel/relay"
MAX_REQUEST = 4096
TICK_S = 20
CHECK_CACHE_S = 3.0
UP_CHUNK = 512 * 1024
MAX_WRITE = 600 * 1024        # raw bytes a write carries: as base64 in JSON it stays under Vercel's 1 MB body limit
STREAM_TIMEOUT_S = 300


def peer_uid(conn):
    """The uid of the process at the other end of a Unix socket, or None when it can't be told."""
    try:
        if sys.platform == "darwin":
            raw = conn.getsockopt(0, 0x001, 76)          # SOL_LOCAL, LOCAL_PEERCRED: struct xucred
            return struct.unpack_from("Ii", raw, 0)[1]
        raw = conn.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), 12)
        return struct.unpack("iii", raw)[1]
    except (OSError, struct.error):
        return None


def private_dir(d):
    """The socket's folder: made 0700 if it isn't there; refused unless it's a real folder of this account's, 0700."""
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise SystemExit(f"broker: {d} isn't a private folder of this account's (0700): refused")


def ask(sock_path, req, timeout=5.0):
    """One request to a broker: its answer (a dict), or None when nothing answers."""
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        c.connect(sock_path)
        c.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n") and len(buf) < 65536:
            d = c.recv(4096)
            if not d:
                break
            buf += d
        c.close()
        return json.loads(buf.decode()) if buf.strip() else None
    except (OSError, ValueError):
        return None


class Broker:
    def __init__(self, api, cfg, log=None, clock=time.time):
        self.api = api
        self.vm, self.sandbox, self.iid, self.op = cfg["vm"], cfg["sandbox"], cfg["iid"], cfg.get("op") or ""
        self.sock_path = cfg["sock"]
        self.idle_ms = int(cfg.get("idle_min") or 15) * 60000
        self.cap_ms = int(cfg["cap_min"]) * 60000 if cfg.get("cap_min") else None
        self.relays = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self._cache = (0.0, None)
        self._log = log or (lambda m: None)
        self.clock = clock
        self.capped = False
        self.seq = 0

    def log(self, msg):
        try:
            self._log(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + self.api.scrub(str(msg), 600))
        except Exception:      # noqa: BLE001  (logging never stops the broker)
            pass

    # --- the computer's sandbox, as Vercel has it now ------------------------------------------------------------
    def live(self, fresh=False):
        """(its identity now, its session) or (None, None) when it's gone; a sandbox of that name the computer didn't
        make has another identity ("another")."""
        t, val = self._cache
        if not fresh and val is not None and time.monotonic() - t < CHECK_CACHE_S:
            return val
        cur = self.api.sandbox(self.sandbox)
        if cur is None:
            val = (None, None)
        else:
            sb = cur["sandbox"]
            tags = sb.get("tags") if isinstance(sb.get("tags"), dict) else {}
            ok = tags.get("metacomputer") == self.vm and (not self.op or tags.get("mcop") == self.op)
            created = sb.get("createdAt")
            ident = f"{self.sandbox}-{created}" if ok and isinstance(created, int) else "another"
            val = (ident, cur.get("session") or {})
        self._cache = (time.monotonic(), val)
        return val

    # --- requests --------------------------------------------------------------------------------------------------
    def handle(self, conn):
        try:
            conn.settimeout(10)
            uid = peer_uid(conn)
            if uid is not None and uid != os.getuid():
                self._reply(conn, ok=False, why="another account's process: refused")
                return
            buf = b""
            while not buf.endswith(b"\n"):
                d = conn.recv(MAX_REQUEST)
                if not d:
                    return
                buf += d
                if len(buf) > MAX_REQUEST:
                    self._reply(conn, ok=False, why="request too long")
                    return
            try:
                req = json.loads(buf.decode())
            except ValueError:
                self._reply(conn, ok=False, why="not a request")
                return
            op = req.get("op") if isinstance(req, dict) else None
            if op == "ping":
                self._reply(conn, ok=True, iid=self.iid, pid=os.getpid(), relays=len(self.relays))
                return
            if op == "quit":
                self._reply(conn, ok=True)
                self.log("quit asked")
                self.stopping.set()
                return
            if req.get("sandbox") != self.sandbox:
                self._reply(conn, ok=False, why=f"this broker carries {self.sandbox} only")
                return
            if op == "check":
                try:
                    ident, s = self.live(fresh=True)
                except Exception as e:      # noqa: BLE001  (said, never the request)
                    self._reply(conn, ok=False, why=f"Vercel couldn't be asked ({self.api.scrub(str(e), 200)})")
                    return
                self._reply(conn, ok=ident is not None, id=ident or "gone", state=(s or {}).get("status"))
                return
            if op == "relay":
                self.relay(conn)
                return
            self._reply(conn, ok=False, why="unknown request")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _reply(conn, **kw):
        conn.sendall((json.dumps(kw) + "\n").encode())

    def relay(self, conn):
        try:
            ident, s = self.live()
        except Exception as e:      # noqa: BLE001
            self._reply(conn, ok=False, why=f"Vercel couldn't be asked ({self.api.scrub(str(e), 200)})")
            return
        if ident != self.iid:
            self._reply(conn, ok=False, why="the sandbox of that name isn't the computer's own now (gone, or made again "
                                            "since): no connection was made")
            return
        if s.get("status") != "running":
            self._reply(conn, ok=False, why=f"{self.vm} is stopped (its session ended): metacomputer --vm {self.vm} start")
            return
        sid = s["id"]
        try:
            cid = self.api.start(sid, ["python3", RELAY], stdin=True)
        except Exception as e:      # noqa: BLE001
            self._reply(conn, ok=False, why=f"Vercel didn't run the relay ({self.api.scrub(str(e), 200)})")
            return
        with self.lock:
            self.seq += 1
            n = self.seq
            self.relays[n] = (sid, cid)
        self.log(f"relay {n} open")
        self._reply(conn, ok=True)
        conn.settimeout(None)
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        except OSError:
            pass
        up = threading.Thread(target=self._up, args=(conn, sid, cid, n), daemon=True)
        up.start()
        try:
            self._down(conn, sid, cid)
        except Exception as e:      # noqa: BLE001  (the connection ends; said in the log)
            self.log(f"relay {n}: output ended: {e}")
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            up.join(5)
            try:
                self.api.kill(sid, cid, 15)
            except Exception:      # noqa: BLE001  (it has ended already, mostly)
                pass
            with self.lock:
                self.relays.pop(n, None)
            self.log(f"relay {n} closed")

    def _up(self, conn, sid, cid, n):
        """The socket's bytes to the relay's standard input, in order, each write at its offset. A reader keeps taking
        what arrives while a write is in flight, so each write carries everything that came meanwhile (a write is a
        round trip to Vercel: one per SSH packet would make an upload crawl)."""
        buf, cond, done = bytearray(), threading.Condition(), []

        def reader():
            try:
                while True:
                    d = conn.recv(262144)
                    with cond:
                        if not d:
                            break
                        buf.extend(d)
                        cond.notify_all()
                        while len(buf) > 4 * UP_CHUNK and not done:
                            cond.wait(1.0)
            except OSError:
                pass
            finally:
                with cond:
                    done.append(True)
                    cond.notify_all()
        threading.Thread(target=reader, daemon=True).start()
        off = 0
        try:
            while True:
                with cond:
                    while not buf and not done:
                        cond.wait()
                    if not buf:
                        break
                    d = bytes(buf[:MAX_WRITE])
                    del buf[:len(d)]
                    cond.notify_all()
                self._write(sid, cid, d, off)
                off += len(d)
        except Exception as e:      # noqa: BLE001
            self.log(f"relay {n}: input ended: {e}")
            with cond:
                done.append(True)
                cond.notify_all()
        try:
            self._write(sid, cid, b"", off, close=True)
        except Exception:      # noqa: BLE001
            pass

    def _write(self, sid, cid, data, off, close=False):
        for attempt in range(6):
            try:
                self.api.write(sid, cid, data, off, close=close)
                return
            except Exception as e:      # noqa: BLE001  (sent again at the same offset: Vercel skips what it has)
                code = getattr(e, "status", None)
                if isinstance(code, int) and 400 <= code < 500 and code != 429:
                    raise
                if attempt == 5:
                    raise
                time.sleep(0.4 * 2 ** attempt)

    def _down(self, conn, sid, cid):
        """The relay's base64 lines, as bytes, to the socket; the output read again from where it was when its stream
        drops before the relay ends."""
        delivered, buf = 0, ""
        for attempt in range(8):
            skip = delivered
            try:
                for stream, text in self.api.logs(sid, cid, timeout=STREAM_TIMEOUT_S):
                    if stream != "stdout":
                        continue
                    if skip:
                        cut = min(skip, len(text))
                        text, skip = text[cut:], skip - cut
                        if not text:
                            continue
                    delivered += len(text)
                    buf += text
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        if line:
                            conn.sendall(base64.b64decode(line))
                return
            except OSError as e:
                if self.stopping.is_set() or attempt == 7:
                    raise
                self.log(f"output stream dropped ({e}); read again from {delivered}")
                time.sleep(0.2 * 2 ** attempt)
                continue

    # --- the idle stop and the broker's own end ----------------------------------------------------------------------
    def tick(self):
        """Keep the session's end IDLE_STOP minutes ahead while a connection is open (up to the plan's cap). Returns
        False when the broker should end: its session has ended and nothing is open."""
        try:
            ident, s = self.live(fresh=True)
        except Exception as e:      # noqa: BLE001  (Vercel unreachable now: try again next time)
            self.log(f"couldn't ask Vercel ({e})")
            return True
        if ident != self.iid:
            self.log("the sandbox isn't the computer's own now: ending")
            return False
        running = (s or {}).get("status") == "running"
        if not running:
            return bool(self.relays)
        if not self.relays or self.capped:
            return True
        start, timeout = s.get("startedAt") or s.get("requestedAt"), s.get("timeout")
        if not isinstance(start, (int, float)) or not isinstance(timeout, (int, float)):
            return True
        left = start + timeout - self.clock() * 1000
        if left >= self.idle_ms - 60000:
            return True
        want = int(self.idle_ms - left)
        want = ((want + 59999) // 60000) * 60000
        if self.cap_ms:
            want = min(want, int(self.cap_ms - timeout))
        if want <= 0:
            self.capped = True
            self.log("the session is at the plan's cap: it ends then, in use or not")
            return True
        try:
            self.api.extend(s["id"], want)
            self._cache = (0.0, None)
            self.log(f"session extended by {want // 60000} min (a connection is open)")
        except Exception as e:      # noqa: BLE001
            if "sandbox_timeout_invalid" in str(e) or getattr(e, "code", "") == "sandbox_timeout_invalid":
                self.capped = True
                self.log("the session is at the plan's cap: it ends then, in use or not")
            else:
                self.log(f"couldn't extend the session ({e})")
        return True

    def serve(self):
        d = os.path.dirname(self.sock_path)
        private_dir(d)
        if ask(self.sock_path, {"op": "ping"}, timeout=2) is not None:
            raise SystemExit("broker: another broker answers on this computer's socket: this one ends")
        try:
            st = os.lstat(self.sock_path)
            if stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid():
                os.unlink(self.sock_path)          # a stale one (nothing answers on it)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            srv.bind(self.sock_path)
        finally:
            os.umask(old)
        os.chmod(self.sock_path, 0o600)
        srv.listen(16)
        srv.settimeout(1.0)
        self.log(f"serving {self.vm} (sandbox {self.sandbox}), pid {os.getpid()}")

        def ticker():
            while not self.stopping.wait(TICK_S):
                if not self.tick():
                    self.stopping.set()
        threading.Thread(target=ticker, daemon=True).start()
        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stopping.is_set():
                        break
                    raise
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        finally:
            srv.close()
            try:
                if os.path.exists(self.sock_path) and stat.S_ISSOCK(os.lstat(self.sock_path).st_mode):
                    os.unlink(self.sock_path)
            except OSError:
                pass
            with self.lock:
                open_relays = list(self.relays.values())
            for sid, cid in open_relays:
                try:
                    self.api.kill(sid, cid, 15)
                except Exception:      # noqa: BLE001
                    pass
            self.log("ended")


def make_api(env):
    """The API with a kept-alive HTTPS connection per thread (a connection's many small writes) and the output stream."""
    sys.path.insert(0, LIB)
    import cloud
    import provider_http
    api_mod = cloud.registry.module("vercel", "api")
    local = threading.local()

    def http(method, url, headers, body=None, timeout=60):
        if not hasattr(local, "session"):
            local.session = provider_http.Session()
        return local.session.send(method, url, headers, body, timeout)
    return api_mod.API(env=env, http=http, stream=provider_http.stream)


def main(argv):
    if argv != ["serve"]:
        print("usage: broker.py serve (its settings on its standard input; started by providers/vercel/remote.py)",
              file=sys.stderr)
        return 2
    if os.environ.get("MCOMP_PARSE_ONLY") == "1":   # tests/test_help.py's valid forms: taken, nothing done
        print("parse-only: broker.py: taken " + json.dumps({"flags": [], "values": [], "words": 1, "max_words": 1}))
        sys.exit(99)        # parse only: nothing was done (never 0: a check run this way refuses)
    raw = sys.stdin.buffer.read(65536)
    sys.stdin.close()
    try:
        cfg = json.loads(raw.decode())
        env = {k: cfg.pop(k) for k in ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID")}
    except (ValueError, KeyError, AttributeError):
        print("broker: its settings didn't come as expected: nothing was started", file=sys.stderr)
        return 2
    os.chdir("/")
    api = make_api(env)
    del env, raw
    b = Broker(api, cfg, log=lambda m: (sys.stderr.write(m + "\n"), sys.stderr.flush()))
    signal.signal(signal.SIGTERM, lambda *_: b.stopping.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    b.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
