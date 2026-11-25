"""The one HTTPS call a provider's REST API needs (the providers on lib/cloud/vps.py), kept out of lib/cloud and the
providers' adapters, which import nothing that reaches the network: lib/remote.py hands send() to the adapter it
makes, and tests hand in a stand-in.

    send(method, url, headers, body, timeout) -> (status, headers, body bytes)
    Session().send(...)                         the same, over one kept-alive connection (many small calls)
    stream(method, url, headers, body, timeout) -> the answer's lines as they come (a command's output)

Only https URLs on the providers' own API hosts (each one's manifest names its own: api_host) are sent, redirects are
never followed (a redirect would carry the token's Authorization header to wherever it points), answers are read to a
cap, and an HTTP error status is returned like any other (the adapter says what it means). A connection that fails
raises OSError, its text without the request (no header, so no token, is ever in it)."""
import urllib.error
import urllib.request

from cloud import manifests

HOSTS = manifests.api_hosts()
MAX_ANSWER = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None             # the 3xx comes back as an HTTPError: returned as is, never followed


_OPENER = urllib.request.build_opener(_NoRedirect)


def send(method, url, headers, body=None, timeout=60):
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if not url.startswith("https://") or host not in HOSTS:
        raise OSError(f"not a provider's API address: {host}")
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read(MAX_ANSWER)
    except urllib.error.HTTPError as e:
        try:
            data = e.read(MAX_ANSWER)
        except OSError:
            data = b""
        return e.code, dict((e.headers or {}).items()), data
    except urllib.error.URLError as e:
        raise OSError(f"{host}: {getattr(e, 'reason', e)}") from None


def _target(url):
    """(host, path and query) of an https URL on a provider's API host; OSError for anything else."""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if not url.startswith("https://") or host not in HOSTS:
        raise OSError(f"not a provider's API address: {host}")
    rest = url[len("https://") + len(host):]
    return host, rest or "/"


class Session:
    """send(), over one kept-alive connection (a program that makes many small calls, each a round trip: a provider's
    transport writing a connection's bytes): the same rules (https, the providers' API hosts only, no redirect
    followed, answers read to a cap). Not shared between threads. A connection that fails is made again once for a
    call that can be sent again (a GET); any other raises OSError, as send() does."""

    def __init__(self):
        self._conn, self._host = None, None

    def _connect(self, host, timeout):
        import http.client
        import ssl
        if self._conn is None or self._host != host:
            self.close()
            self._conn = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
            self._host = host
        self._conn.timeout = timeout
        if self._conn.sock is not None:
            self._conn.sock.settimeout(timeout)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    def send(self, method, url, headers, body=None, timeout=60):
        import http.client
        host, path = _target(url)
        for attempt in (0, 1):
            conn = self._connect(host, timeout)
            try:
                conn.request(method, path, body=body, headers=dict(headers))
                r = conn.getresponse()
                data = r.read(MAX_ANSWER + 1)
                if len(data) > MAX_ANSWER:
                    self.close()
                    data = data[:MAX_ANSWER]
                elif r.getheader("connection", "").lower() == "close":
                    self.close()
                return r.status, dict(r.getheaders()), data
            except (OSError, http.client.HTTPException) as e:
                self.close()
                if attempt == 0 and method == "GET":
                    continue
                raise OSError(f"{host}: {e}") from None


MAX_LINE = 16 * 1024 * 1024


def stream(method, url, headers, body=None, timeout=60):
    """An answer read as it comes, line by line (str, without the newline), on a connection of its own that's closed
    when the lines end or the caller stops reading: a provider's stream of a command's output. The same rules as
    send(); an HTTP error status raises OSError with its code (its body isn't read as lines); a line past MAX_LINE ends
    it."""
    import http.client
    import ssl
    host, path = _target(url)
    conn = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
    try:
        try:
            conn.request(method, path, body=body, headers=dict(headers))
            r = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            raise OSError(f"{host}: {e}") from None
        if not 200 <= r.status < 300:
            detail = r.read(4096).decode(errors="replace")
            raise OSError(f"{host}: HTTP {r.status} {detail[:300]}")
        while True:
            try:
                line = r.readline(MAX_LINE + 1)
            except (OSError, http.client.HTTPException) as e:
                raise OSError(f"{host}: {e}") from None
            if not line:
                return
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                raise OSError(f"{host}: a line of the answer was too long")
            yield line.decode(errors="replace").rstrip("\r\n")
    finally:
        conn.close()
