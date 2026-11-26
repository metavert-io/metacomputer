"""What a computer on Cloudflare talks to besides Cloudflare's REST API: its control Worker (at its workers.dev address,
signed with the computer's own key) and the image registries (images.py). Standard library only, and Python 3.9.

The rules, as lib/provider_http.py's for the REST API: https only; each request's host checked against the one kind it
may be (a Worker's workers.dev name; registry.cloudflare.com; Docker Hub's); redirects never followed (images.py
follows Docker Hub's one blob redirect itself, with no credential, and checks the blob by its digest); answers read to
a cap; an error's text never carries a request (so never a signature or a credential).

The Worker's key never travels: each request carries X-Mc-Auth, v1.<unix time>.<nonce>.<HMAC-SHA256 of "v1", the method,
the path, the query, that time, the nonce, the body's length and its SHA-256, one per line>, and X-Mc-Body,
<the body's length>.<its SHA-256>, so the Worker checks the signature in constant time within 300 s
(providers/cloudflare/worker/logic.js verify) before it reads a byte of the body, and then that the body is exactly
the one signed; the nonce (12 random bytes, new for every request) is refused when it comes again, so a request can't
be sent twice.

    Worker(url, key).call(verb, body)        one request: its JSON answer, or WorkerError (its status, what it said)
    Worker(url, key).tcp()                   a websocket to the computer's sshd (WebSocket), its Durable Object's id
                                             read from the answer (x-mc-do) before a byte goes through"""
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import threading
import time

WORKER_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.workers\.dev")
MAX_ANSWER = 16 * 1024 * 1024
MAX_HEAD = 64 * 1024
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
ACCOUNT_SHAPED = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
TOKEN_SHAPED = re.compile(r"(?i)(?:bearer\s+|basic\s+)?[A-Za-z0-9_\-+/=]{40,}")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WireError(Exception):
    """A request that failed or was refused: status (an HTTP status, or 0 for no answer) and what was said, scrubbed."""

    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class WorkerError(WireError):
    pass


def scrub(text, cap=300):
    """Text fit for a message: no control characters, nothing shaped like an account's ID (32 hex digits) or a
    credential, capped."""
    out = CONTROL.sub(" ", str(text or ""))
    out = ACCOUNT_SHAPED.sub("<account>", out)
    out = TOKEN_SHAPED.sub(lambda m: m.group(0) if re.fullmatch(r"[0-9a-f]{64}", m.group(0)) else "[redacted]", out)
    return out[:cap]


def worker_url(name, subdomain):
    url = f"https://{name}.{subdomain}.workers.dev"
    check_worker_url(url)
    return url


def check_worker_url(url):
    """The host of a Worker's workers.dev address, or WireError: never anything else."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise WireError(0, "a Worker's address is https://NAME.SUBDOMAIN.workers.dev")
    host = url[len("https://"):].rstrip("/")
    if not WORKER_HOST.fullmatch(host):
        raise WireError(0, "a Worker's address is https://NAME.SUBDOMAIN.workers.dev")
    return host


def body_header(body):
    """X-Mc-Body: the body's length and SHA-256, which the signature covers."""
    return "%d.%s" % (len(body or b""), hashlib.sha256(body or b"").hexdigest())


def sign(key, method, path, query, body, now=None, nonce=None):
    ts = str(int(time.time() if now is None else now))
    nonce = nonce or os.urandom(12).hex()
    data = "\n".join(["v1", method, path, query or "", ts, nonce, str(len(body or b"")),
                       hashlib.sha256(body or b"").hexdigest()]).encode()
    return "v1.%s.%s.%s" % (ts, nonce, hmac.new(key.encode(), data, hashlib.sha256).hexdigest())


def _context():
    return ssl.create_default_context()


def https(host, method, path, headers, body=None, timeout=60, cap=MAX_ANSWER):
    """One request over a connection of its own: (status, headers with lower-case names, the answer's bytes). Never
    follows a redirect (a 3xx comes back as it is). OSError-like failures raise WireError(0, ...), without the request."""
    import http.client
    conn = http.client.HTTPSConnection(host, timeout=timeout, context=_context())
    try:
        try:
            conn.request(method, path, body=body, headers=dict(headers))
            r = conn.getresponse()
            data = r.read(cap + 1)
        except (OSError, http.client.HTTPException) as e:
            raise WireError(0, f"{host}: {scrub(e, 200)}") from None
        if len(data) > cap:
            raise WireError(r.status, f"{host}: the answer was longer than {cap} bytes")
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        conn.close()


class Worker:
    """The computer's control Worker. `send` (https's signature) and `connect` (a socket factory for the websocket)
    are stand-ins in tests; the key is held here and nowhere else in this process."""

    def __init__(self, url, key, send=None, connect=None, clock=None):
        self.host = check_worker_url(url)
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise WireError(0, "the computer's Worker key isn't one metacomputer made (64 hex digits)")
        self.__key = key
        self._send = send or https
        self._connect = connect
        self._clock = clock or time.time

    def __repr__(self):
        return f"<Worker {self.host.split('.')[0]}>"

    def scrub(self, text, cap=300):
        out = str(text or "").replace(self.__key, "[redacted]")
        return scrub(out, cap)

    def call(self, verb, body=None, timeout=900):
        if not re.fullmatch(r"[a-z][a-z-]{1,30}", verb or ""):
            raise WireError(0, f"not a request: {verb!r}")
        path = f"/v1/{verb}"
        data = json.dumps(body or {}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "metacomputer",
                   "X-Mc-Auth": sign(self.__key, "POST", path, "", data, self._clock()), "X-Mc-Body": body_header(data)}
        status, _h, answer = self._send(self.host, "POST", path, headers, data, timeout)
        try:
            j = json.loads(answer) if answer.strip() else {}
        except ValueError:
            j = None
        if not 200 <= status < 300:
            why = (j or {}).get("error") if isinstance(j, dict) else None
            raise WorkerError(status, self.scrub(why or answer[:300].decode(errors="replace") or f"HTTP {status}"))
        if not isinstance(j, dict):
            raise WorkerError(status, "the Worker answered with something that isn't a JSON object")
        return j

    def tcp(self, timeout=30):
        """A websocket to the computer's sshd, through its Durable Object: (WebSocket, the object's id as the 101 said
        it). Refused (WorkerError) when the computer is stopped or its key isn't the Worker's."""
        path = "/v1/tcp"
        key = base64.b64encode(os.urandom(16)).decode()
        head = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\nUser-Agent: metacomputer\r\nX-Mc-Auth: %s\r\n\r\n"
                % (path, self.host, key, sign(self.__key, "GET", path, "", b"", self._clock())))
        sock = self._connect(self.host, timeout) if self._connect else _tls(self.host, timeout)
        try:
            sock.sendall(head.encode())
            status, headers, rest = _read_head(sock)
            if status != 101:
                body = rest + _read_some(sock, 4096)
                try:
                    why = json.loads(body.split(b"\r\n\r\n")[-1] or b"{}").get("error")
                except (ValueError, AttributeError):
                    why = None
                raise WorkerError(status, self.scrub(why or f"HTTP {status}"))
            want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            if headers.get("sec-websocket-accept") != want:
                raise WorkerError(status, "the Worker's websocket answer doesn't match the request")
            do = headers.get("x-mc-do", "")
            if not re.fullmatch(r"[0-9a-f]{64}", do):
                raise WorkerError(status, "the Worker didn't say which computer it reached")
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise
        sock.settimeout(None)
        return WebSocket(sock, rest), do


def _tls(host, timeout):
    raw = socket.create_connection((host, 443), timeout=timeout)
    try:
        return _context().wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise


def _read_some(sock, n):
    try:
        return sock.recv(n)
    except OSError:
        return b""


def _read_head(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = sock.recv(4096)
        if not d:
            raise WorkerError(0, "the Worker closed the connection before answering")
        buf += d
        if len(buf) > MAX_HEAD:
            raise WorkerError(0, "the Worker's answer head was too long")
    head, rest = buf.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    m = re.match(r"HTTP/1\.[01] (\d{3})", lines[0])
    if not m:
        raise WorkerError(0, "the Worker's answer wasn't HTTP")
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return int(m.group(1)), headers, rest


def mask(data, key):
    """A websocket client frame's payload, masked with its 4-byte key (as one big integer: fast for 64 KB)."""
    n = len(data)
    if not n:
        return b""
    k = (key * ((n + 3) // 4))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


class WebSocket:
    """A client websocket over a connected socket: send() binary frames (masked, as a client's must be), recv() the
    next message's bytes (None once the other end has closed); pings answered. send() may be called from another
    thread than recv()."""
    MAX_FRAME = 64 * 1024
    MAX_MESSAGE = 16 * 1024 * 1024

    def __init__(self, sock, rest=b""):
        self.sock = sock
        self.buf = bytearray(rest)
        self.wlock = threading.Lock()
        self.closed = False

    def _frame(self, op, data):
        head = bytearray([0x80 | op])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        k = os.urandom(4)
        return bytes(head) + k + mask(data, k)

    def send(self, data, op=0x2):
        view = memoryview(bytes(data))
        with self.wlock:
            if op != 0x2 or not len(view):
                self.sock.sendall(self._frame(op, bytes(view)))
                return
            for i in range(0, len(view), self.MAX_FRAME):
                self.sock.sendall(self._frame(op, bytes(view[i:i + self.MAX_FRAME])))

    def _need(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(max(65536, n - len(self.buf)))
            if not d:
                raise EOFError
            self.buf += d
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv(self):
        msg = bytearray()
        try:
            while True:
                b0, b1 = self._need(2)
                fin, op, masked, n = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
                if n == 126:
                    n = int.from_bytes(self._need(2), "big")
                elif n == 127:
                    n = int.from_bytes(self._need(8), "big")
                if n > self.MAX_MESSAGE:
                    raise WireError(0, "a websocket frame was too long")
                k = self._need(4) if masked else None
                data = self._need(n)
                if k:
                    data = mask(data, k)
                if op == 0x8:
                    try:
                        self.send(data[:2], op=0x8)
                    except OSError:
                        pass
                    self.closed = True
                    return None
                if op == 0x9:
                    self.send(data, op=0xA)
                    continue
                if op == 0xA:
                    continue
                msg += data
                if len(msg) > self.MAX_MESSAGE:
                    raise WireError(0, "a websocket message was too long")
                if fin:
                    return bytes(msg)
        except (EOFError, OSError):
            self.closed = True
            return None

    def close(self):
        if not self.closed:
            try:
                self.send((1000).to_bytes(2, "big"), op=0x8)
            except OSError:
                pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
