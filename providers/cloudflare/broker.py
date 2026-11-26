#!/usr/bin/env python3
"""The transport broker of a computer on Cloudflare Containers: the one process besides lib/remote.py that holds the
computer's own key (its control Worker's; never the account's API token, which no broker gets). ssh, the hub, the
vault and every other program never get it: they reach the computer through this process's socket, which carries SSH
connections and answers whether the Worker still answers for the computer, and nothing else.

    broker.py serve        (started by providers/cloudflare/remote.py, never by hand: its settings, the key among them,
                            come as one JSON object on its standard input, which is then closed)

What it does, for one computer:
- Listens on a Unix socket (0600, in a 0700 folder of this account's, /tmp/mcomp-UID-cloudflare/HASH.sock; a peer of
  another account is refused). A request is one JSON line: {"op": "ping" | "check" | "relay" | "quit", "worker": its
  Worker's name}; the answer is one JSON line.
- `check`: the computer's identity as its Worker says it now ({"id": "<worker>-<its Durable Object's id, 16 hex>"} when
  the Worker answers for this computer and the operation that made it; "another" otherwise): lib/proxywrap.py asks it
  (through tunnel.py check) right before each SSH connection, and refuses unless it's the one recorded.
- `relay`: one SSH connection: a websocket to the Worker (signed with the key), which its Durable Object pipes to the
  container's sshd. The answer that opens it names the Durable Object (x-mc-do), which must be the computer's recorded
  one before a byte goes through. A stopped computer stays stopped: the Worker refuses, and the broker never starts it.
- An open connection keeps the computer from its idle stop (the Worker counts them); nothing to renew here.
- It ends when the computer is stopped and no connection is open (looked at every minute), on `quit` (vm delete, a new
  broker for the computer), and on SIGTERM. Its log (the computer's RUN_DIR/cloudflare-broker.log) never holds the key.
Standard library only, besides providers/cloudflare/wire.py."""
import json
import os
import signal
import socket
import stat
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_REQUEST = 4096
TICK_S = 60
CHECK_CACHE_S = 3.0


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
    def __init__(self, worker, cfg, log=None, clock=time.time):
        self.worker = worker
        self.vm, self.name, self.iid = cfg["vm"], cfg["worker"], cfg["iid"]
        self.do, self.op = cfg.get("do") or "", cfg.get("op") or ""
        self.sock_path = cfg["sock"]
        self.relays = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self._cache = (0.0, None)
        self._log = log or (lambda m: None)
        self.clock = clock
        self.seq = 0

    def log(self, msg):
        try:
            self._log(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + self.worker.scrub(str(msg), 600))
        except Exception:      # noqa: BLE001  (logging never stops the broker)
            pass

    # --- the computer, as its Worker says it now ----------------------------------------------------------------
    def live(self, fresh=False):
        """(its identity now, running) or (None, None) when the Worker isn't there; "another" when the Worker refuses
        the key or answers for another computer, operation or object."""
        t, val = self._cache
        if not fresh and val is not None and time.monotonic() - t < CHECK_CACHE_S:
            return val
        try:
            s = self.worker.call("status", {}, 60)
        except Exception as e:      # noqa: BLE001
            code = getattr(e, "status", None)
            if code == 404:
                val = (None, None)
            elif code == 401:
                val = ("another", None)
            else:
                raise
        else:
            do = str(s.get("do") or "")
            ok = (s.get("computer") == self.vm and (not self.op or s.get("op") == self.op)
                  and (not self.do or do == self.do) and len(do) == 64)
            val = (f"{self.name}-{do[:16]}" if ok else "another", bool(s.get("running")))
        self._cache = (time.monotonic(), val)
        return val

    # --- requests -------------------------------------------------------------------------------------------------
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
            if req.get("worker") != self.name:
                self._reply(conn, ok=False, why=f"this broker carries {self.name} only")
                return
            if op == "check":
                try:
                    ident, running = self.live(fresh=True)
                except Exception as e:      # noqa: BLE001  (said, never the request)
                    self._reply(conn, ok=False, why=f"the Worker couldn't be asked ({self.worker.scrub(e, 200)})")
                    return
                self._reply(conn, ok=ident is not None, id=ident or "gone",
                            state="running" if running else "stopped")
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
            ident, running = self.live()
        except Exception as e:      # noqa: BLE001
            self._reply(conn, ok=False, why=f"the Worker couldn't be asked ({self.worker.scrub(e, 200)})")
            return
        if ident != self.iid:
            self._reply(conn, ok=False, why="the Worker of that name doesn't answer for this computer now (gone, or made "
                                            "again since): no connection was made")
            return
        if not running:
            self._reply(conn, ok=False, why=f"{self.vm} is stopped: metacomputer --vm {self.vm} start")
            return
        try:
            ws, do = self.worker.tcp()
        except Exception as e:      # noqa: BLE001
            self._reply(conn, ok=False, why=f"the Worker didn't open the connection ({self.worker.scrub(e, 200)})")
            return
        if not self.do or do != self.do:
            ws.close()
            self._reply(conn, ok=False, why="the connection reached another Durable Object than the computer's: closed "
                                            "before anything went through")
            return
        with self.lock:
            self.seq += 1
            n = self.seq
            self.relays[n] = ws
        self.log(f"relay {n} open")
        self._reply(conn, ok=True)
        conn.settimeout(None)

        def up():
            try:
                while True:
                    d = conn.recv(65536)
                    if not d:
                        break
                    ws.send(d)
            except OSError:
                pass
            ws.close()
        t = threading.Thread(target=up, daemon=True)
        t.start()
        try:
            while True:
                d = ws.recv()
                if d is None:
                    break
                if d:
                    conn.sendall(d)
        except Exception as e:      # noqa: BLE001  (the connection ends; said in the log)
            self.log(f"relay {n}: output ended: {e}")
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            ws.close()
            t.join(5)
            with self.lock:
                self.relays.pop(n, None)
            self.log(f"relay {n} closed")

    # --- its own end ------------------------------------------------------------------------------------------------
    def tick(self):
        """False when the broker should end: the Worker doesn't answer for the computer, or it's stopped and nothing is
        open."""
        try:
            ident, running = self.live(fresh=True)
        except Exception as e:      # noqa: BLE001  (the Worker unreachable now: try again next time)
            self.log(f"couldn't ask the Worker ({e})")
            return True
        if ident != self.iid:
            self.log("the Worker doesn't answer for the computer now: ending")
            return False
        return bool(running) or bool(self.relays)

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
        self.log(f"serving {self.vm} (Worker {self.name}), pid {os.getpid()}")

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
                open_ones = list(self.relays.values())
            for ws in open_ones:
                ws.close()
            self.log("ended")


def make_worker(url, key):
    """The Worker's client, from this checkout's own providers/cloudflare/wire.py (loaded by the provider registry,
    never by putting the repository on sys.path)."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"))
    import cloud
    return cloud.registry.module("cloudflare", "wire").Worker(url, key)


def main(argv):
    if argv != ["serve"]:
        print("usage: broker.py serve (its settings on its standard input; started by providers/cloudflare/remote.py)",
              file=sys.stderr)
        return 2
    if os.environ.get("MCOMP_PARSE_ONLY") == "1":   # tests/test_help.py's valid forms: taken, nothing done
        print("parse-only: broker.py: taken " + json.dumps({"flags": [], "values": [], "words": 1, "max_words": 1}))
        sys.exit(99)        # parse only: nothing was done (never 0: a check run this way refuses)
    raw = sys.stdin.buffer.read(65536)
    sys.stdin.close()
    try:
        cfg = json.loads(raw.decode())
        key, url = cfg.pop("key"), cfg.pop("url")
        for k in ("vm", "worker", "iid", "sock"):
            if not isinstance(cfg.get(k), str) or not cfg[k]:
                raise KeyError(k)
    except (ValueError, KeyError, AttributeError):
        print("broker: its settings didn't come as expected: nothing was started", file=sys.stderr)
        return 2
    os.chdir("/")
    worker = make_worker(url, key)
    del key, raw
    b = Broker(worker, cfg, log=lambda m: (sys.stderr.write(m + "\n"), sys.stderr.flush()))
    signal.signal(signal.SIGTERM, lambda *_: b.stopping.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    b.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
